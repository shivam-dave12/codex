"""
Advanced ICT + SMC + FVG Trading Strategy - v9 (Institutional Engine)
=====================================================================
v9 changes over v8
------------------
1. NESTED DEALING RANGES (IPDA)
   Three simultaneous DRs: Weekly (1D BOS), Daily (4H BOS), Intraday (1H BOS).
   All three aligned → full size. Partial → scaled down. Premium/discount
   alignment is checked per DR tier before any entry is considered.

2. GATED CASCADE ENTRY LOGIC
   Replaces flat score-adder with a 3-level hard gate:
     L1 (ALL must pass) : HTF bias, DR zone, no active failed-setup
     L2 (2-of-3)        : sweep+displacement, OB/FVG touch, MSS in direction
     L3 (1-of-N)        : CVD aligned, absorption event, killzone, TCE bonus
   Score is still computed for logging but cannot override L1/L2/L3 gates.

3. SINGLE TP + DYNAMIC STRUCTURE TRAILING
   One structure-derived TP (CoinSwitch-compatible).
   Dynamic SL re-anchoring follows swing structure + ATR progression.

4. REGIME-AWARE POSITION SIZING
   RegimeEngine.size_multiplier scales base risk per regime.
   DR alignment score: 3/3=1.0x, 2/3=0.75x, 1/3=0.50x.
   Consecutive-loss mode: 0.50x until next winner.

5. PRICE-DISTANCE TCE DECAY
   pullback_probability = 1 - (0.70 * distance_decay + 0.30 * time_decay)
   3 pct away from target zone → probability near zero regardless of time.

6. SELF-ADAPTATION FEEDBACK LOOP
   RegimePerformanceTracker records outcome per regime.
   win_rate < 40 pct over last 20 trades → threshold +10, size 0.70x.
   consecutive_losses >= 3 → threshold +15, size 0.50x, Telegram alert.

7. VIRGIN OB QUALIFIER
   OB never revisited → 1.30x score. One touch → 1.0x. Two touches → 0.70x.
   Three+ revisits → invalidated.

8. INTERMARKET CONTEXT HOOKS
   Optional: data_manager.get_funding_rate(), get_oi_delta()
   Funding strongly positive + long = -10. OI divergence = +8 short.
"""

import time
import logging
import threading
from typing import List, Dict, Optional, Tuple
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

import config
from telegram_notifier import send_telegram_message
from order_manager import GlobalRateLimiter, CancelResult
from state_machine import TradingStateMachine
from regime_engine import (
    RegimeEngine, RegimeSnapshot,
    REGIME_TRENDING_BULL, REGIME_TRENDING_BEAR,
    REGIME_RANGING, REGIME_VOLATILE_EXPANSION,
    REGIME_DISTRIBUTION, REGIME_ACCUMULATION, NestedDealingRanges
)

logger = logging.getLogger(__name__)

# ── Module-level constants ─────────────────────────────────────────────────────
PLACEMENT_LOCK_SECONDS    = 60
TCE_MOMENTUM_THRESHOLD    = 0.25
TCE_MAX_AGE_MS            = 4 * 3600 * 1000

# Cascade gate
CASCADE_L2_MIN_TRIGGERS   = 2
CASCADE_L3_MIN_CONFIRMS   = 1

# Self-adaptation
ADAPTATION_MIN_SAMPLES    = 20
ADAPTATION_POOR_WIN_RATE  = 0.40
ADAPTATION_THRESHOLD_ADD  = 10.0
ADAPTATION_SIZE_DEGRADED  = 0.70
CONSEC_LOSS_PRESERVE_AT   = 3
PRESERVE_THRESHOLD_ADD    = 15.0
PRESERVE_SIZE_MULT        = 0.50

# Virgin OB
OB_VIRGIN_MULT            = 1.30
OB_ONE_TOUCH_MULT         = 1.00
OB_TWO_TOUCH_MULT         = 0.70
OB_INVALIDATE_TOUCHES     = 3

# DR alignment size multipliers
DR_ALL_ALIGNED_MULT       = 1.00
DR_TWO_ALIGNED_MULT       = 0.75
DR_ONE_ALIGNED_MULT       = 0.50

# TP target zones relative to dealing range (for tranche targeting)
DR_TP1_ZONE_PCT           = 0.50   # equilibrium of current DR
DR_TP2_ZONE_PCT           = 0.79   # OTE edge of opposing DR

# Neutral HTF handling (micro-bias execution profile)
MICRO_BIAS_5M_WINDOW_MS   = 45 * 60_000
MICRO_BIAS_15M_WINDOW_MS  = 180 * 60_000
MICRO_BIAS_MSS_MAX_WEIGHT = 0.35
MICRO_BIAS_DAILY_WEIGHT   = 0.45
MICRO_BIAS_CVD_WEIGHT     = 0.20
MICRO_BIAS_MIN_EDGE       = 0.10
NEUTRAL_SL_MAX_PCT        = 0.010

# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class OrderBlock:
    """ICT Order Block — last opposite candle before strong impulse move."""
    low:               float
    high:              float
    timestamp:         int
    has_wick_rejection: bool
    strength:          float          # 0-100
    broken:            bool  = False
    direction:         str   = "bullish"      # bullish / bearish
    has_displacement:  bool  = False          # candle after OB broke prior swing
    inducement_near:   bool  = False          # swept pool was just before this OB
    bos_confirmed:     bool  = False          # impulse from this OB broke a swing (v9)
    visit_count:       int   = 0              # how many times price returned here (v9)

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2

    @property
    def size(self) -> float:
        return self.high - self.low

    def in_optimal_zone(self, price: float) -> bool:
        """0.50–0.79 Fibonacci retracement (ICT Optimal Trade Entry)."""
        if self.direction == "bullish":
            zone_low  = self.low  + (self.size * config.OB_OPTIMAL_ENTRY_MIN)
            zone_high = self.low  + (self.size * config.OB_OPTIMAL_ENTRY_MAX)
        else:
            zone_low  = self.high - (self.size * config.OB_OPTIMAL_ENTRY_MAX)
            zone_high = self.high - (self.size * config.OB_OPTIMAL_ENTRY_MIN)
        return zone_low <= price <= zone_high

    def contains_price(self, price: float) -> bool:
        return self.low <= price <= self.high

    def is_active(self, current_time: int) -> bool:
        age_min = (current_time - self.timestamp) / 60000
        return (not self.broken
                and age_min < config.OB_MAX_AGE_MINUTES
                and self.visit_count < OB_INVALIDATE_TOUCHES)

    def score_multiplier(self) -> float:
        """v9 virgin OB multiplier."""
        if self.visit_count == 0:
            return OB_VIRGIN_MULT
        if self.visit_count == 1:
            return OB_ONE_TOUCH_MULT
        return OB_TWO_TOUCH_MULT


@dataclass
class FairValueGap:
    """ICT Fair Value Gap — 3-candle inefficiency zone."""
    bottom:          float
    top:             float
    timestamp:       int
    direction:       str           # bullish / bearish
    is_ifvg:         bool = False  # Inversion FVG
    filled:          bool = False
    fill_percentage: float = 0.0

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def size(self) -> float:
        return self.top - self.bottom

    def is_price_in_gap(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    def update_fill(self, candles: List[Dict]) -> None:
        """Live fill-percentage update; auto-flip to IFVG when overfilled."""
        if not candles or self.size <= 0:
            return
        max_fill = 0.0
        for c in candles[-20:]:
            if self.direction == "bullish":
                if float(c['l']) <= self.top:
                    max_fill = max(max_fill,
                                   (self.top - float(c['l'])) / self.size)
            else:
                if float(c['h']) >= self.bottom:
                    max_fill = max(max_fill,
                                   (float(c['h']) - self.bottom) / self.size)
        self.fill_percentage = min(max_fill, 1.0)
        if self.fill_percentage >= 1.0:
            self.filled = True

    def is_active(self, current_time: int) -> bool:
        age_min = (current_time - self.timestamp) / 60000
        return not self.filled and age_min < config.FVG_MAX_AGE_MINUTES


@dataclass
class LiquidityPool:
    """SMC Liquidity Pool — EQH / EQL level."""
    price:                  float
    pool_type:              str     # "EQH" or "EQL"
    timestamp:              int
    touch_count:            int
    swept:                  bool  = False
    sweep_timestamp:        int   = 0
    wick_rejection:         bool  = False
    displacement_confirmed: bool  = False

    def distance_from_price(self, current_price: float) -> float:
        return abs(current_price - self.price) / current_price * 100


@dataclass
class SwingPoint:
    price:     float
    swing_type: str     # "high" or "low"
    timestamp: int
    confirmed: bool = False
    timeframe: str  = "5m"


@dataclass
class MarketStructure:
    structure_type:     str   # "BOS" or "CHoCH"
    price:              float
    timestamp:          int
    direction:          str   # "bullish" or "bearish"
    timeframe:          str   = "5m"
    confirmed_sequence: bool  = False


@dataclass
class TriggerContext:
    """
    Bundles the exact trigger structure so Entry/SL/TP are always derived
    from the SAME structure — no mismatched level bugs.
    """
    trigger_ob:           Optional['OrderBlock']    = None
    trigger_fvg:          Optional['FairValueGap']  = None
    nearest_swing_low:    Optional[float]           = None
    nearest_swing_high:   Optional[float]           = None
    sweep_detected:       bool                      = False
    sweep_price:          Optional[float]           = None
    inducement_confirmed: bool                      = False


@dataclass
class FailedSetupRecord:
    """
    Records context of a stopped-out trade to gate intelligent re-entry.
    """
    side:               str
    entry_price:        float
    sl_price:           float
    trigger_ob_low:     Optional[float]
    trigger_ob_high:    Optional[float]
    trigger_fvg_bottom: Optional[float]
    trigger_fvg_top:    Optional[float]
    risk:               float
    timestamp:          int
    invalidated:        bool = False


@dataclass
class TrendChangeRecord:
    """One lifecycle of a trend-change confirmation cycle."""
    bias_direction:          str
    bias_change_time:        int
    tce_state:               str   = "AWAITING_CONFIRMATION"
    confirmation_ms:         Optional['MarketStructure'] = None
    confirmation_time:       int   = 0
    target_ob:               Optional['OrderBlock']   = None
    target_fvg:              Optional['FairValueGap'] = None
    target_zone_low:         float = 0.0
    target_zone_high:        float = 0.0
    pullback_probability:    float = 1.0
    last_probability_update: int   = 0
    entry_taken:             bool  = False
    expired_reason:          str   = ""
    expire_time:             int   = 0
    max_age_ms:              int   = TCE_MAX_AGE_MS
    price_at_flip:           float = 0.0
    momentum_mode_start:     int   = 0


# ── NEW v9 DATACLASSES ─────────────────────────────────────────────────────────

@dataclass
class DealingRange:
    """Single dealing range (premium / discount / equilibrium)."""
    high:       float
    low:        float
    formed_ts:  int
    source:     str  = "INIT"

    @property
    def size(self) -> float:
        return max(self.high - self.low, 1e-9)

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2

    def zone_pct(self, price: float) -> float:
        return max(min((price - self.low) / self.size, 1.0), 0.0)

    def is_premium(self, price: float) -> bool:
        return self.zone_pct(price) > 0.618

    def is_discount(self, price: float) -> bool:
        return self.zone_pct(price) < 0.382

    def is_equilibrium(self, price: float) -> bool:
        p = self.zone_pct(price)
        return 0.382 <= p <= 0.618

@dataclass
class RegimePerformanceRecord:
    """Single trade outcome tagged with regime."""
    regime:       str
    side:         str
    pnl:          float
    won:          bool
    timestamp:    int


class RegimePerformanceTracker:
    """
    Rolling window (last ADAPTATION_MIN_SAMPLES trades per regime).
    Exposes win_rate and degradation flag per regime.
    """
    def __init__(self, window: int = ADAPTATION_MIN_SAMPLES):
        self._window  = window
        self._records: deque = deque(maxlen=window * 6)  # all regimes mixed

    def record(self, regime: str, side: str, pnl: float, won: bool) -> None:
        self._records.append(RegimePerformanceRecord(
            regime=regime, side=side, pnl=pnl, won=won,
            timestamp=int(time.time() * 1000)))

    def regime_stats(self, regime: str) -> Dict:
        recs = [r for r in self._records if r.regime == regime]
        if len(recs) < ADAPTATION_MIN_SAMPLES:
            return {"samples": len(recs), "win_rate": None, "degraded": False}
        wins     = sum(1 for r in recs if r.won)
        win_rate = wins / len(recs)
        return {
            "samples":  len(recs),
            "win_rate": round(win_rate, 3),
            "degraded": win_rate < ADAPTATION_POOR_WIN_RATE,
        }

    def threshold_add(self, regime: str) -> float:
        stats = self.regime_stats(regime)
        return ADAPTATION_THRESHOLD_ADD if stats["degraded"] else 0.0

    def size_mult(self, regime: str) -> float:
        stats = self.regime_stats(regime)
        return ADAPTATION_SIZE_DEGRADED if stats["degraded"] else 1.0

# ============================================================================
# VOLUME PROFILE ANALYZER
# ============================================================================

class VolumeProfileAnalyzer:
    """
    Tracks CVD, Point of Control (POC), and Value Area (VAH/VAL)
    from candle approximations when raw tick data is unavailable.
    """

    def __init__(self, history_size: int = 1000):
        self.cvd_history: deque = deque(maxlen=history_size)
        self._volume_by_price:  Dict[float, float] = {}
        self.poc_price:  float = 0.0
        self.vah_price:  float = 0.0
        self.val_price:  float = 0.0
        self._lock = threading.Lock()

    def on_candle(self, candle: Dict) -> None:
        """Approximate CVD from OHLCV candle."""
        with self._lock:
            try:
                o  = float(candle['o'])
                c  = float(candle['c'])
                v  = float(candle.get('v', 0))
                ts = candle.get('t', int(time.time() * 1000))
                if v <= 0:
                    return
                body_ratio = (c - o) / max(abs(c - o) + 1e-9, 1e-9)
                buy_vol    = v * (0.5 + 0.5 * body_ratio)
                sell_vol   = v - buy_vol
                mid        = (o + c) / 2

                self.cvd_history.append({
                    'price':  mid,
                    'qty':    v,
                    'delta':  buy_vol - sell_vol,
                    'is_buy': c >= o,
                    'ts':     ts,
                })

                # Volume profile: 10-point bins (suits BTC)
                bin_p = round(mid / 10) * 10
                self._volume_by_price[bin_p] = (
                    self._volume_by_price.get(bin_p, 0) + v)
            except Exception:
                pass

    def get_cvd_signal(self, lookback: int = 100) -> Dict:
        """
        Returns {signal: STRONG_BULL|BULL|NEUTRAL|BEAR|STRONG_BEAR,
                 cvd_total, cvd_slope}.
        """
        with self._lock:
            if len(self.cvd_history) < 10:
                return {"signal": "NEUTRAL", "cvd_total": 0.0, "cvd_slope": 0.0}

            recent  = list(self.cvd_history)[-min(lookback,
                                                    len(self.cvd_history)):]
            deltas  = [t['delta'] for t in recent]
            total   = sum(deltas)
            mid     = len(deltas) // 2
            slope   = sum(deltas[mid:]) - sum(deltas[:mid])
            vol_sum = sum(abs(d) for d in deltas) or 1.0
            pct     = total / vol_sum

            if   pct >  0.30 and slope > 0: signal = "STRONG_BULL"
            elif pct >  0.10:                signal = "BULL"
            elif pct < -0.30 and slope < 0: signal = "STRONG_BEAR"
            elif pct < -0.10:                signal = "BEAR"
            else:                            signal = "NEUTRAL"

            return {"signal": signal, "cvd_total": total, "cvd_slope": slope}

    def price_at_poc(self, price: float, tol_pct: float = 0.05) -> bool:
        self._rebuild_poc()
        if self.poc_price <= 0:
            return False
        return abs(price - self.poc_price) / self.poc_price * 100 <= tol_pct

    def price_at_value_area_edge(self, price: float,
                                   tol_pct: float = 0.05) -> Optional[str]:
        self._rebuild_poc()
        if self.vah_price > 0:
            if abs(price - self.vah_price) / self.vah_price * 100 <= tol_pct:
                return "VAH"
        if self.val_price > 0:
            if abs(price - self.val_price) / self.val_price * 100 <= tol_pct:
                return "VAL"
        return None

    def _rebuild_poc(self) -> None:
        with self._lock:
            if not self._volume_by_price:
                return
            sorted_bins = sorted(self._volume_by_price.items(),
                                  key=lambda x: x[1], reverse=True)
            self.poc_price = sorted_bins[0][0] if sorted_bins else 0.0

            price_list  = sorted(self._volume_by_price.keys())
            if not price_list:
                return
            total_vol   = sum(self._volume_by_price.values())
            target      = total_vol * 0.70

            try:
                poc_idx = price_list.index(self.poc_price)
            except ValueError:
                return

            lo = hi = poc_idx
            acc = self._volume_by_price.get(price_list[poc_idx], 0)

            while acc < target:
                lo_v = (self._volume_by_price.get(price_list[lo - 1], 0)
                        if lo > 0 else 0)
                hi_v = (self._volume_by_price.get(price_list[hi + 1], 0)
                        if hi < len(price_list) - 1 else 0)
                if lo_v == 0 and hi_v == 0:
                    break
                if lo_v >= hi_v and lo > 0:
                    lo -= 1; acc += lo_v
                elif hi < len(price_list) - 1:
                    hi += 1; acc += hi_v
                else:
                    break

            self.val_price = price_list[lo]
            self.vah_price = price_list[min(hi, len(price_list) - 1)]


# ============================================================================
# ADVANCED LIQUIDITY MODEL
# ============================================================================

class AdvancedLiquidityModel:
    """
    Detects institutional absorption (high volume, tiny body) at key levels.
    """

    def __init__(self):
        self._events: deque = deque(maxlen=200)

    def on_candle(self, candle: Dict) -> None:
        try:
            h  = float(candle['h']); l  = float(candle['l'])
            o  = float(candle['o']); c  = float(candle['c'])
            v  = float(candle.get('v', 0))
            ts = candle.get('t', int(time.time() * 1000))
            rng = h - l
            if v <= 0 or rng <= 0:
                return
            body_ratio = abs(c - o) / rng
            if body_ratio < 0.25:
                self._events.append({
                    'price':      (h + l) / 2,
                    'high': h, 'low': l,
                    'volume':     v,
                    'body_ratio': body_ratio,
                    'direction':  "bull" if c >= o else "bear",
                    'ts':         ts,
                })
        except Exception:
            pass

    def absorption_confluence(self, current_price: float, side: str,
                               lookback_ms: int = 3_600_000) -> float:
        """Returns score bonus 0–15 for aligned absorption near price."""
        now = int(time.time() * 1000)
        tol = current_price * 0.003
        relevant = [
            ev for ev in self._events
            if (now - ev['ts']) <= lookback_ms
            and abs(ev['price'] - current_price) <= tol
        ]
        if not relevant:
            return 0.0
        aligned = sum(
            1 for ev in relevant
            if (side == "long"  and ev['direction'] == "bull") or
               (side == "short" and ev['direction'] == "bear"))
        return 15.0 if aligned >= 2 else (8.0 if aligned == 1 else 0.0)


# ============================================================================
# ADVANCED ICT STRATEGY  (class definition + __init__)
# ============================================================================

class AdvancedICTStrategy:
    """
    v9 Institutional Engine — see module docstring for full feature list.
    """

    _STRUCTURE_UPDATE_MS = (
        getattr(config, "STRUCTURE_UPDATE_INTERVAL_SECONDS", 30) * 1000)
    _ENTRY_EVAL_MS = (
        getattr(config, "ENTRY_EVALUATION_INTERVAL_SECONDS", 5) * 1000)

    def __init__(self, order_manager):
        # ── Core dependencies ─────────────────────────────────────────────────
        self._order_manager  = order_manager
        self._risk_manager   = None    # stored on first on_tick call

        # ── Regime + IPDA ─────────────────────────────────────────────────────
        self.regime_engine   = RegimeEngine()
        self._ndr            = NestedDealingRanges()
        self._perf_tracker   = RegimePerformanceTracker()

        # ── Optional analytics ────────────────────────────────────────────────
        try:
            self.volume_analyzer = VolumeProfileAnalyzer()
        except Exception:
            self.volume_analyzer = None
        try:
            self.liquidity_model = AdvancedLiquidityModel()
        except Exception:
            self.liquidity_model = None

        # ── HTF Bias ──────────────────────────────────────────────────────────
        self.htf_bias             = "NEUTRAL"
        self.htf_bias_strength    = 0.0
        self.htf_bias_components: Dict = {}
        self.daily_bias           = "NEUTRAL"

        # ── Market structures ─────────────────────────────────────────────────
        self.order_blocks_bull: deque = deque(
            maxlen=config.MAX_ORDER_BLOCKS)
        self.order_blocks_bear: deque = deque(
            maxlen=config.MAX_ORDER_BLOCKS)
        self.fvgs_bull:         deque = deque(maxlen=config.MAX_FVGS)
        self.fvgs_bear:         deque = deque(maxlen=config.MAX_FVGS)
        self.liquidity_pools:   deque = deque(
            maxlen=config.MAX_LIQUIDITY_ZONES)
        self.swing_highs:       deque = deque(maxlen=300)
        self.swing_lows:        deque = deque(maxlen=300)
        self.market_structures: deque = deque(maxlen=150)
        self.failed_setups:     deque = deque(maxlen=50)

        # ── Dedup ─────────────────────────────────────────────────────────────
        self._registered_sweeps: set = set()

        # ── TCE ───────────────────────────────────────────────────────────────
        self._active_tce: Optional[TrendChangeRecord] = None

        # ── Session / AMD ─────────────────────────────────────────────────────
        self.current_session = "REGULAR"
        self.in_killzone     = False
        self.amd_phase       = "DISTRIBUTION"

        # ── Position state ────────────────────────────────────────────────────
        self.state                  = "READY"
        self.active_position:  Optional[Dict]         = None
        self.entry_order_id:   Optional[str]          = None
        self.sl_order_id:      Optional[str]          = None
        self.tp_order_id:      Optional[str]          = None
        self._pending_ctx:     Optional[TriggerContext] = None

        self.initial_entry_price:   Optional[float] = None
        self.initial_sl_price:      Optional[float] = None
        self.initial_tp_price:      Optional[float] = None
        self.current_sl_price:      Optional[float] = None
        self.current_tp_price:      Optional[float] = None
        self.highest_price_reached: Optional[float] = None
        self.lowest_price_reached:  Optional[float] = None
        self.breakeven_moved        = False
        self.profit_locked_pct      = 0.0
        self.entry_pending_start:   Optional[int] = None

        # ── Self-adaptation ───────────────────────────────────────────────────
        self._capital_preserve_mode = False
        self.consecutive_losses     = 0

        # ── Stats ─────────────────────────────────────────────────────────────
        self.total_entries          = 0
        self.total_exits            = 0
        self.winning_trades         = 0
        self.total_pnl              = 0.0
        self.daily_pnl              = 0.0
        self.confluences_detected   = 0

        # ── Timing ────────────────────────────────────────────────────────────
        self._last_structure_update_ms   = 0
        self._last_entry_eval_ms         = 0
        self._last_gate_reject_key: Optional[str] = None
        self._last_gate_reject_ms:  int = 0
        self._last_regime_bos_ts:   int = 0
        self._latest_closed_5m_ts:  int = 0
        self._last_processed_5m_ts: int = 0
        self._last_sl_hit_time           = 0
        self._placement_locked_until     = 0
        self.last_sl_update              = 0
        self.last_potential_trade_report = 0

        # ── Init flag ─────────────────────────────────────────────────────────
        self._initialized = False
        self._state_machine = TradingStateMachine()

        logger.info("✅ AdvancedICTStrategy v9 created")

    def _sm_transition(self, new_state: str, reason: str = "") -> None:
        try:
            if self._state_machine.current_state != new_state:
                self._state_machine.transition(new_state, reason=reason)
        except Exception:
            pass

    def on_closed_candle(self, timeframe: str, candle: Dict) -> None:
        """DataManager callback: emits only on confirmed candle close."""
        try:
            if timeframe == "5m":
                ts = int(candle.get("t", 0))
                if ts > self._latest_closed_5m_ts:
                    self._latest_closed_5m_ts = ts
        except Exception:
            pass

    # =========================================================================
    # GET POSITION (called by main.py)
    # =========================================================================

    def get_position(self) -> Optional[Dict]:
        """Returns active position dict or None."""
        return (self.active_position
                if self.state == "POSITION_ACTIVE" else None)

    def get_runtime_state(self) -> str:
        try:
            return self._state_machine.current_state
        except Exception:
            return self.state

    # =========================================================================
    # ON TICK  — main entry point called every 250ms
    # =========================================================================

    def on_tick(self, data_manager, order_manager, risk_manager,
                current_time: int) -> None:
        try:
            # Store risk_manager ref (used in _close_position)
            if self._risk_manager is None:
                self._risk_manager = risk_manager

            # Readiness gate — do nothing until DataManager is warmed up
            if not data_manager.is_ready: 
                return

            current_price = data_manager.get_last_price()
            if not current_price or current_price <= 0:
                return

            # ── One-time warmup ───────────────────────────────────────────────
            if not self._initialized:
                self._run_initialization(data_manager, current_time)
                return

            if self.state == "READY":
                self._sm_transition("SCANNING", "ready_for_scan")

            # ── Placement lock ────────────────────────────────────────────────
            if (self.state == "READY" and
                    current_time < self._placement_locked_until):
                return

            # ── Session / AMD ─────────────────────────────────────────────────
            self._update_session_and_killzone(current_time)
            self._update_amd_phase(current_time)

            # ── Structure rebuild ─────────────────────────────────────────────
            has_new_5m_close = (
                self._latest_closed_5m_ts > self._last_processed_5m_ts
            )
            if has_new_5m_close:
                self._update_all_structures(
                    data_manager, current_price, current_time)
                self._last_structure_update_ms = current_time
                self._last_processed_5m_ts = self._latest_closed_5m_ts

            # ── State machine ─────────────────────────────────────────────────
            if self.state == "ENTRY_PENDING":
                self._handle_entry_pending(
                    order_manager, risk_manager, current_time)

            elif self.state == "POSITION_ACTIVE":
                self._manage_active_position(
                    data_manager, order_manager, current_time)

            elif self.state == "READY":
                if (current_time - self._last_entry_eval_ms
                        >= self._ENTRY_EVAL_MS):
                    self._evaluate_entry(
                        data_manager, order_manager,
                        risk_manager, current_time)
                    self._last_entry_eval_ms = current_time

        except Exception as e:
            logger.error(f"❌ on_tick error: {e}", exc_info=True)

    # =========================================================================
    # ONE-TIME INITIALIZATION
    # =========================================================================

    def _run_initialization(self, data_manager, current_time: int) -> None:
        try:
            logger.info("🔧 Strategy v9 initialization starting...")

            current_price = data_manager.get_last_price()
            if not current_price:
                return

            c5m  = data_manager.get_candles("5m")  or []
            c15m = data_manager.get_candles("15m") or []
            c1h  = data_manager.get_candles("1h")  or []
            c4h  = data_manager.get_candles("4h")  or []
            c1d  = data_manager.get_candles("1d")  or []

            if len(c5m) < 30:
                logger.warning("⏳ Insufficient 5m candles — waiting...")
                return

            # ── Swing points ──────────────────────────────────────────────────
            self._detect_swing_points(c5m,  current_price, tf="5m")
            if c15m: self._detect_swing_points(c15m, current_price, tf="15m")
            if c4h:  self._detect_swing_points(c4h,  current_price, tf="4h")

            # ── Liquidity ─────────────────────────────────────────────────────
            self._detect_liquidity_pools(current_price, current_time)
            if c15m:
                self._detect_liquidity_sweeps(
                    c5m, c15m, current_price, current_time)

            # ── OB / FVG ──────────────────────────────────────────────────────
            self._detect_order_blocks(c5m,  current_time, current_price, "5m")
            if c15m:
                self._detect_order_blocks(c15m, current_time,
                                           current_price, "15m")
            self._detect_fvgs(c5m,  current_time, current_price, "5m")
            if c15m:
                self._detect_fvgs(c15m, current_time, current_price, "15m")
            self._update_fvg_fills(c5m)

            # ── Structure ─────────────────────────────────────────────────────
            self._detect_market_structure(c5m,  current_price, "5m")
            if c15m: self._detect_market_structure(c15m, current_price, "15m")
            if c4h:  self._detect_market_structure(c4h,  current_price, "4h")

            # ── HTF bias ──────────────────────────────────────────────────────
            if len(c4h) >= 10:
                self._update_htf_bias(c4h, current_price, current_time)
            self._update_daily_bias(c5m, current_price)

            # ── Nested DR ─────────────────────────────────────────────────────
            self._update_nested_dealing_ranges(
                c5m, c1h, c4h, c1d, current_price, current_time)

            # ── Regime engine ─────────────────────────────────────────────────
            self.regime_engine.update(
                c5m, c1h, current_price, current_time)

            # ── Volume / liquidity warmup ─────────────────────────────────────
            if self.volume_analyzer:
                for c in c5m[-100:]:
                    self.volume_analyzer.on_candle(c)
            if self.liquidity_model:
                for c in c5m[-100:]:
                    self.liquidity_model.on_candle(c)

            # ── TCE recovery ──────────────────────────────────────────────────
            self._tce_reinit_if_needed(current_price, current_time)

            # ── Session / AMD ─────────────────────────────────────────────────
            self._update_session_and_killzone(current_time)
            self._update_amd_phase(current_time)

            self._initialized              = True
            self._last_structure_update_ms = current_time
            rs = self.regime_engine.state

            logger.info(
                f"✅ Strategy initialized — "
                f"HTF={self.htf_bias} ({self.htf_bias_strength:.2f}) "
                f"Regime={rs.regime} ADX={rs.adx:.1f} "
                f"OB={len(self.order_blocks_bull)}B/"
                f"{len(self.order_blocks_bear)}R "
                f"FVG={len(self.fvgs_bull)}B/{len(self.fvgs_bear)}R "
                f"Pools={len(self.liquidity_pools)} "
                f"TCE={self._active_tce.tce_state if self._active_tce else 'NONE'}")

            send_telegram_message(
                f"✅ *Strategy v9 Initialized*\n"
                f"HTF={self.htf_bias} str={self.htf_bias_strength:.2f}\n"
                f"Regime={rs.regime} ADX={rs.adx:.1f}\n"
                f"Bull OBs={len(self.order_blocks_bull)} "
                f"Bear OBs={len(self.order_blocks_bear)}\n"
                f"FVGs={len(self.fvgs_bull)}B/{len(self.fvgs_bear)}R\n"
                f"Pools={len(self.liquidity_pools)}\n"
                f"TCE={self._active_tce.tce_state if self._active_tce else 'NONE'}\n"
                f"DR_W={'OK' if self._ndr.weekly else 'N/A'} "
                f"DR_D={'OK' if self._ndr.daily else 'N/A'} "
                f"DR_I={'OK' if self._ndr.intraday else 'N/A'}")

        except Exception as e:
            logger.error(f"❌ _run_initialization error: {e}", exc_info=True)

    # =========================================================================
    # STRUCTURE UPDATE  (runs every 30s)
    # =========================================================================

    def _update_all_structures(self, data_manager, current_price: float,
                                 current_time: int) -> None:
        try:
            c5m  = data_manager.get_candles("5m")  or []
            c15m = data_manager.get_candles("15m") or []
            c1h  = data_manager.get_candles("1h")  or []
            c4h  = data_manager.get_candles("4h")  or []
            c1d  = data_manager.get_candles("1d")  or []

            if not c5m:
                return

            # ── Swings ───────────────────────────────────────────────────────
            self._detect_swing_points(c5m, current_price, tf="5m")
            if c15m: self._detect_swing_points(c15m, current_price, tf="15m")
            if c4h:  self._detect_swing_points(c4h,  current_price, tf="4h")

            # ── Liquidity ─────────────────────────────────────────────────────
            self._detect_liquidity_pools(current_price, current_time)
            self._detect_liquidity_sweeps(
                c5m, c15m or [], current_price, current_time)

            # ── OB / FVG ──────────────────────────────────────────────────────
            self._detect_order_blocks(c5m,  current_time, current_price, "5m")
            if c15m:
                self._detect_order_blocks(c15m, current_time,
                                           current_price, "15m")
            self._detect_fvgs(c5m,  current_time, current_price, "5m")
            if c15m:
                self._detect_fvgs(c15m, current_time, current_price, "15m")
            self._update_fvg_fills(c5m)

            # ── OB visit count update ─────────────────────────────────────────
            for ob in list(self.order_blocks_bull) + list(self.order_blocks_bear):
                if not ob.broken and ob.is_active(current_time):
                    was_in_zone = getattr(ob, '_price_was_in_zone', False)
                    now_in_zone = ob.contains_price(current_price)
                    if now_in_zone and not was_in_zone:
                        ob.visit_count += 1
                    ob._price_was_in_zone = now_in_zone

            # ── Market structure ──────────────────────────────────────────────
            prev_bias = self.htf_bias

            self._detect_market_structure(c5m,  current_price, "5m")
            if c15m: self._detect_market_structure(c15m, current_price, "15m")
            if c4h:  self._detect_market_structure(c4h,  current_price, "4h")

            # ── HTF bias (re-evaluate on every structure cycle) ───────────────
            if len(c4h) >= 10:
                self._update_htf_bias(c4h, current_price, current_time)
                if self.htf_bias != prev_bias:
                    logger.info(
                        f"📊 HTF bias changed: "
                        f"{prev_bias} → {self.htf_bias} "
                        f"(str={self.htf_bias_strength:.2f})")
                    self._tce_on_bias_change(
                        self.htf_bias, current_time,
                        from_neutral=(prev_bias == "NEUTRAL"),
                        price_at_flip=current_price)

            self._update_daily_bias(c5m, current_price)

            # ── Nested DR ─────────────────────────────────────────────────────
            self._update_nested_dealing_ranges(
                c5m, c1h, c4h, c1d, current_price, current_time)

            # ── Regime engine ─────────────────────────────────────────────────
            recent_bos = next(
                (ms for ms in reversed(list(self.market_structures))
                 if ms.structure_type == "BOS"
                 and (current_time - ms.timestamp) < 300_000),
                None)
            bos_dir_for_regime = None
            if recent_bos and recent_bos.timestamp != self._last_regime_bos_ts:
                bos_dir_for_regime = recent_bos.direction
                self._last_regime_bos_ts = recent_bos.timestamp
            self.regime_engine.update(
                c5m, c1h, current_price, current_time,
                bos_dir_for_regime)

            # ── Volume / liquidity model ──────────────────────────────────────
            if self.volume_analyzer and c5m:
                for c in c5m[-5:]:
                    self.volume_analyzer.on_candle(c)
            if self.liquidity_model and c5m:
                for c in c5m[-5:]:
                    self.liquidity_model.on_candle(c)

            # ── Cleanup ───────────────────────────────────────────────────────
            self._cleanup_structures(current_price, current_time)

            logger.debug(
                f"Structures: "
                f"OB={len(self.order_blocks_bull)}B/"
                f"{len(self.order_blocks_bear)}R "
                f"FVG={len(self.fvgs_bull)}B/{len(self.fvgs_bear)}R "
                f"Pools={len(self.liquidity_pools)} "
                f"MS={len(self.market_structures)} "
                f"Regime={self.regime_engine.state.regime} "
                f"ADX={self.regime_engine.state.adx:.1f}")

        except Exception as e:
            logger.error(
                f"❌ _update_all_structures error: {e}", exc_info=True)

    # =========================================================================
    # HTF BIAS  (4H EMA + structure + BOS multi-factor)
    # =========================================================================

    def _update_htf_bias(self, candles_4h: List[Dict],
                          current_price: float,
                          current_time: int) -> None:
        try:
            if len(candles_4h) < 10:
                return

            closes     = [float(c['c']) for c in candles_4h]
            ema_period = getattr(config, "HTF_TREND_EMA", 34)
            ema_val    = self._calculate_ema(closes, ema_period)
            ema_dist   = abs(current_price - ema_val) / ema_val * 100
            min_dist   = getattr(config, "HTF_EMA_MIN_DISTANCE", 0.5)

            bull = 0.0; bear = 0.0
            comp: Dict = {}

            # 1. Price vs EMA (30%)
            if current_price > ema_val:
                bull += 0.30; comp["ema"] = "BULL"
            else:
                bear += 0.30; comp["ema"] = "BEAR"

            # 2. EMA slope (20%)
            if len(closes) >= ema_period + 4:
                ema_prev = self._calculate_ema(closes[:-4], ema_period)
                if ema_val > ema_prev:
                    bull += 0.20; comp["ema_slope"] = "BULL"
                else:
                    bear += 0.20; comp["ema_slope"] = "BEAR"

            # 3. Price structure HH/HL vs LH/LL (30%)
            highs = [float(c['h']) for c in candles_4h[-6:]]
            lows  = [float(c['l']) for c in candles_4h[-6:]]
            if len(highs) >= 4:
                hh = highs[-1] > max(highs[-4:-1])
                hl = lows[-1]  > min(lows[-4:-1])
                lh = highs[-1] < max(highs[-4:-1])
                ll = lows[-1]  < min(lows[-4:-1])
                if hh and hl:
                    bull += 0.30; comp["structure"] = "HH_HL"
                elif lh and ll:
                    bear += 0.30; comp["structure"] = "LH_LL"
                elif hh:
                    bull += 0.15; comp["structure"] = "HH"
                elif ll:
                    bear += 0.15; comp["structure"] = "LL"
                else:
                    comp["structure"] = "NEUTRAL"

            # 4. Recent 4H BOS (20%)
            recent_bos = [
                ms for ms in list(self.market_structures)
                if ms.timeframe == "4h"
                and ms.structure_type == "BOS"
                and (current_time - ms.timestamp) < 24 * 3_600_000
            ]
            if recent_bos:
                lb = sorted(recent_bos, key=lambda x: x.timestamp)[-1]
                if lb.direction == "bullish":
                    bull += 0.20; comp["bos"] = "BULL"
                else:
                    bear += 0.20; comp["bos"] = "BEAR"

            total = bull + bear or 1.0
            bull_pct = bull / total; bear_pct = bear / total

            THRESH = 0.60
            if bull_pct >= THRESH and ema_dist >= min_dist:
                self.htf_bias          = "BULLISH"
                self.htf_bias_strength = round(bull_pct, 3)
            elif bear_pct >= THRESH and ema_dist >= min_dist:
                self.htf_bias          = "BEARISH"
                self.htf_bias_strength = round(bear_pct, 3)
            else:
                self.htf_bias          = "NEUTRAL"
                self.htf_bias_strength = round(max(bull_pct, bear_pct), 3)

            self.htf_bias_components = comp

        except Exception as e:
            logger.error(f"❌ _update_htf_bias: {e}", exc_info=True)

    # =========================================================================
    # DAILY BIAS  (5M EMA8 vs EMA20 + momentum)
    # =========================================================================

    def _update_daily_bias(self, candles_5m: List[Dict],
                            current_price: float) -> None:
        try:
            if len(candles_5m) < 80:
                return
            closes      = [float(c['c']) for c in candles_5m[-80:]]
            ema_fast    = self._calculate_ema(closes, 8)
            ema_slow    = self._calculate_ema(closes, 20)
            recent_avg  = sum(closes[-4:]) / 4
            prior_avg   = sum(closes[-8:-4]) / 4
            if ema_fast > ema_slow and recent_avg > prior_avg:
                self.daily_bias = "BULLISH"
            elif ema_fast < ema_slow and recent_avg < prior_avg:
                self.daily_bias = "BEARISH"
            else:
                self.daily_bias = "NEUTRAL"
        except Exception as e:
            logger.error(f"❌ _update_daily_bias: {e}", exc_info=True)

    def _resolve_directional_bias(self, current_time: int) -> Tuple[str, float, Dict[str, float]]:
        """
        Returns execution bias used by entry engine.
        - If HTF bias is directional, it is authoritative.
        - If HTF is neutral, require firm STF structure confirmation.
        """
        if self.htf_bias in ("BULLISH", "BEARISH"):
            return self.htf_bias, self.htf_bias_strength, {"htf": self.htf_bias_strength}

        bull = 0.0
        bear = 0.0
        components: Dict[str, float] = {}

        # 1) Daily directional context (supportive, not decisive)
        daily_dir = "NEUTRAL"
        if self.daily_bias == "BULLISH":
            bull += MICRO_BIAS_DAILY_WEIGHT
            components["daily"] = MICRO_BIAS_DAILY_WEIGHT
            daily_dir = "BULLISH"
        elif self.daily_bias == "BEARISH":
            bear += MICRO_BIAS_DAILY_WEIGHT
            components["daily"] = -MICRO_BIAS_DAILY_WEIGHT
            daily_dir = "BEARISH"

        # 2) Firm STF structure confirmation with anti-stall policy:
        #    Prefer 5m+15m agreement; if 15m not available, allow strong 5m with extra support.
        last_5m = next(
            (ms for ms in reversed(list(self.market_structures))
             if ms.timeframe == "5m"
             and ms.structure_type in ("BOS", "CHoCH")
             and (current_time - ms.timestamp) <= MICRO_BIAS_5M_WINDOW_MS),
            None,
        )
        last_15m = next(
            (ms for ms in reversed(list(self.market_structures))
             if ms.timeframe == "15m"
             and ms.structure_type in ("BOS", "CHoCH")
             and (current_time - ms.timestamp) <= MICRO_BIAS_15M_WINDOW_MS),
            None,
        )

        if not last_5m and not last_15m:
            return "NEUTRAL", 0.0, components

        strict_agreement = (last_5m is not None and last_15m is not None)
        if strict_agreement and last_5m.direction != last_15m.direction:
            return "NEUTRAL", 0.0, components

        if strict_agreement:
            structural_dir = "BULLISH" if last_5m.direction == "bullish" else "BEARISH"
            structural_weight = MICRO_BIAS_MSS_MAX_WEIGHT
        else:
            # Only one structure source available -> reduced structural confidence.
            anchor = last_5m or last_15m
            structural_dir = "BULLISH" if anchor.direction == "bullish" else "BEARISH"
            structural_weight = MICRO_BIAS_MSS_MAX_WEIGHT * 0.65

        if structural_dir == "BULLISH":
            bull += structural_weight
            components["mss"] = structural_weight
        else:
            bear += structural_weight
            components["mss"] = -structural_weight

        # 3) CVD momentum confirmation (supportive)
        cvd_dir = "NEUTRAL"
        if self.volume_analyzer and len(self.volume_analyzer.cvd_history) >= 10:
            cvd_sig = self.volume_analyzer.get_cvd_signal(lookback=80)
            signal = cvd_sig.get("signal", "NEUTRAL")
            if "BULL" in signal:
                w = MICRO_BIAS_CVD_WEIGHT if signal == "BULL" else MICRO_BIAS_CVD_WEIGHT * 1.2
                bull += w
                components["cvd"] = w
                cvd_dir = "BULLISH"
            elif "BEAR" in signal:
                w = MICRO_BIAS_CVD_WEIGHT if signal == "BEAR" else MICRO_BIAS_CVD_WEIGHT * 1.2
                bear += w
                components["cvd"] = -w
                cvd_dir = "BEARISH"

        edge = abs(bull - bear)
        strength = min(max(max(bull, bear), 0.0), 1.0)

        # Firmness gate: when 5m+15m agree -> require 1 supporter;
        # when only one timeframe structure is available -> require 2 supporters.
        supporters = 0
        if daily_dir == structural_dir:
            supporters += 1
        if cvd_dir == structural_dir:
            supporters += 1

        min_supporters = 1 if strict_agreement else 2
        min_edge = MICRO_BIAS_MIN_EDGE if strict_agreement else (MICRO_BIAS_MIN_EDGE + 0.05)
        if supporters < min_supporters or edge < min_edge:
            return "NEUTRAL", strength, components

        return structural_dir, strength, components

    # =========================================================================
    # NESTED DEALING RANGES  (3-tier IPDA)
    # =========================================================================

    def _update_nested_dealing_ranges(
            self, c5m: List[Dict], c1h: List[Dict],
            c4h: List[Dict], c1d: List[Dict],
            current_price: float, current_time: int) -> None:
        try:
            # ── Weekly DR ─────────────────────────────────────────────────────
            if c1d and len(c1d) >= 4:
                w_bos = next(
                    (ms for ms in reversed(list(self.market_structures))
                     if ms.timeframe == "4h"
                     and ms.structure_type == "BOS"
                     and (current_time - ms.timestamp)
                     < 7 * 24 * 3_600_000),
                    None)
                if self._ndr.weekly is None or w_bos:
                    self._ndr.update_weekly(
                        c1d, current_time,
                        bos_direction=w_bos.direction if w_bos else None)
            elif len(c4h) >= 10 and self._ndr.weekly is None:
                self._ndr.update_weekly(c4h[-20:], current_time)

            # ── Daily DR ──────────────────────────────────────────────────────
            if len(c4h) >= 4:
                d_bos = next(
                    (ms for ms in reversed(list(self.market_structures))
                     if ms.timeframe in ("4h", "1h")
                     and ms.structure_type == "BOS"
                     and (current_time - ms.timestamp) < 24 * 3_600_000),
                    None)
                if self._ndr.daily is None or d_bos:
                    self._ndr.update_daily(
                        c4h, current_time,
                        bos_direction=d_bos.direction if d_bos else None)

            # ── Intraday DR ───────────────────────────────────────────────────
            if len(c1h) >= 4:
                i_bos = next(
                    (ms for ms in reversed(list(self.market_structures))
                     if ms.timeframe in ("1h", "15m")
                     and ms.structure_type == "BOS"
                     and (current_time - ms.timestamp) < 4 * 3_600_000),
                    None)
                if self._ndr.intraday is None or i_bos:
                    self._ndr.update_intraday(
                        c1h, current_time,
                        bos_direction=i_bos.direction if i_bos else None)
            elif len(c5m) >= 12 and self._ndr.intraday is None:
                self._ndr.update_intraday(c5m[-12:], current_time)

        except Exception as e:
            logger.error(
                f"❌ _update_nested_dealing_ranges: {e}", exc_info=True)


    # =========================================================================
    # SWING POINT DETECTION (unchanged from v8)
    # =========================================================================

    def _detect_swing_points(self, candles: List[Dict], current_price: float,
                              tf: str = "5m"):
        n        = len(candles)
        lb_left  = getattr(config, "SWING_LOOKBACK_LEFT",  3)
        lb_right = getattr(config, "SWING_LOOKBACK_RIGHT", 2)
        if n < lb_left + lb_right + 1:
            return
        dedup_tol = current_price * getattr(
            config, "STRUCTURE_MIN_SWING_SIZE_PCT", 0.10) / 100

        for i in range(lb_left, n - lb_right):
            candle        = candles[i]
            high, low, ts = float(candle['h']), float(candle['l']), self._ts_ms(candle)  # ← ONLY THIS LINE CHANGED

            left_highs  = [float(candles[j]['h']) for j in range(i - lb_left, i)]
            right_highs = [float(candles[j]['h'])
                           for j in range(i + 1, i + 1 + lb_right)]
            if (all(high > h for h in left_highs) and
                    all(high >= h for h in right_highs)):
                if not any(abs(s.price - high) <= dedup_tol
                           and s.swing_type == "high"
                           for s in self.swing_highs):
                    self.swing_highs.append(SwingPoint(
                        price=high, swing_type="high",
                        timestamp=ts, confirmed=True, timeframe=tf))

            left_lows  = [float(candles[j]['l']) for j in range(i - lb_left, i)]
            right_lows = [float(candles[j]['l'])
                          for j in range(i + 1, i + 1 + lb_right)]
            if (all(low < l for l in left_lows) and
                    all(low <= l for l in right_lows)):
                if not any(abs(s.price - low) <= dedup_tol
                           and s.swing_type == "low"
                           for s in self.swing_lows):
                    self.swing_lows.append(SwingPoint(
                        price=low, swing_type="low",
                        timestamp=ts, confirmed=True, timeframe=tf))

    # =========================================================================
    # LIQUIDITY POOL DETECTION (unchanged from v8)
    # =========================================================================

    def _detect_liquidity_pools(self, current_price: float, current_time: int):
        min_touches      = config.LIQ_MIN_TOUCHES
        touch_tol_pct    = getattr(config, "LIQ_TOUCH_TOLERANCE_PCT", 0.08)
        max_distance_pct = config.LIQ_MAX_DISTANCE_PCT
        tolerance        = current_price * touch_tol_pct / 100

        for raw_swings, pool_type in [
            (list(self.swing_highs)[-30:], "EQH"),
            (list(self.swing_lows)[-30:],  "EQL"),
        ]:
            prices = [s.price for s in raw_swings]
            seen: set = set()
            for p in prices:
                if any(abs(p - s) < tolerance * 0.5 for s in seen):
                    continue
                seen.add(p)
                touches = sum(1 for q in prices if abs(q - p) <= tolerance)
                if touches >= min_touches:
                    avg_p = sum(q for q in prices
                                if abs(q - p) <= tolerance) / touches
                    if (abs(current_price - avg_p) / current_price * 100
                            <= max_distance_pct):
                        if not any(abs(lp.price - avg_p) <= tolerance
                                   and lp.pool_type == pool_type
                                   for lp in self.liquidity_pools):
                            self.liquidity_pools.append(LiquidityPool(
                                price=round(avg_p, 2),
                                pool_type=pool_type,
                                timestamp=current_time,
                                touch_count=touches,
                            ))

    # =========================================================================
    # LIQUIDITY SWEEP DETECTION (unchanged from v8)
    # =========================================================================

    def _detect_liquidity_sweeps(self, candles_5m: List[Dict],
                                  candles_15m: List[Dict],
                                  current_price: float, now: int):
        require_wick  = config.SWEEP_WICK_REQUIREMENT
        sweep_max_age = getattr(config, "SWEEP_MAX_AGE_MINUTES", 120) * 60_000

        recent_5m  = [c for c in candles_5m[-20:]
                      if (now - c['t']) <= sweep_max_age]
        recent_15m = [c for c in candles_15m[-10:]
                      if (now - c['t']) <= sweep_max_age]
        recent_all = recent_5m + recent_15m

        # Pool-based sweeps
        for pool in list(self.liquidity_pools):
            if pool.swept:
                continue
            for c in recent_all:
                h, l   = float(c['h']), float(c['l'])
                cl, op = float(c['c']), float(c['o'])
                body   = abs(cl - op)
                rng    = h - l
                dedup_k = (round(pool.price, 0), c['t'])
                if dedup_k in self._registered_sweeps:
                    continue
                if pool.pool_type == "EQH" and h > pool.price:
                    wick_ok = cl < pool.price
                    disp_ok = (body / rng) >= 0.4 if rng > 0 else False
                    if wick_ok and (disp_ok or not require_wick):
                        pool.swept                  = True
                        pool.sweep_timestamp        = now
                        pool.wick_rejection         = wick_ok
                        pool.displacement_confirmed = disp_ok
                        self._registered_sweeps.add(dedup_k)
                        logger.info(f"💧 EQH swept @ ${pool.price:.0f} "
                                    f"wr={wick_ok} disp={disp_ok}")
                        break
                elif pool.pool_type == "EQL" and l < pool.price:
                    wick_ok = cl > pool.price
                    disp_ok = (body / rng) >= 0.4 if rng > 0 else False
                    if wick_ok and (disp_ok or not require_wick):
                        pool.swept                  = True
                        pool.sweep_timestamp        = now
                        pool.wick_rejection         = wick_ok
                        pool.displacement_confirmed = disp_ok
                        self._registered_sweeps.add(dedup_k)
                        logger.info(f"💧 EQL swept @ ${pool.price:.0f} "
                                    f"wr={wick_ok} disp={disp_ok}")
                        break

        # Direct swing-level sweeps (no pool required)
        swing_max_age = getattr(config, "SWING_SWEEP_MAX_AGE_MIN", 60) * 60_000
        tol           = current_price * 0.0005

        for sh in list(self.swing_highs)[-20:]:
            if (now - sh.timestamp) > swing_max_age:
                continue
            for c in recent_5m[-5:]:
                h, l   = float(c['h']), float(c['l'])
                cl, op = float(c['c']), float(c['o'])
                body   = abs(cl - op)
                rng    = h - l
                dedup_k = (round(sh.price, 0), c['t'])
                if dedup_k in self._registered_sweeps:
                    continue
                if h > sh.price and cl < sh.price:
                    disp_ok = (body / rng) >= 0.4 if rng > 0 else False
                    self._registered_sweeps.add(dedup_k)
                    existing = next((lp for lp in self.liquidity_pools
                                     if abs(lp.price - sh.price) <= tol
                                     and lp.pool_type == "EQH"), None)
                    if existing and not existing.swept:
                        existing.swept                  = True
                        existing.sweep_timestamp        = now
                        existing.wick_rejection         = True
                        existing.displacement_confirmed = disp_ok
                        logger.info(f"💧 Swing EQH swept @ {sh.price:.0f}")
                    break

        for sl in list(self.swing_lows)[-20:]:
            if (now - sl.timestamp) > swing_max_age:
                continue
            for c in recent_5m[-5:]:
                h, l   = float(c['h']), float(c['l'])
                cl, op = float(c['c']), float(c['o'])
                body   = abs(cl - op)
                rng    = h - l
                dedup_k = (round(sl.price, 0), c['t'])
                if dedup_k in self._registered_sweeps:
                    continue
                if l < sl.price and cl > sl.price:
                    disp_ok = (body / rng) >= 0.4 if rng > 0 else False
                    self._registered_sweeps.add(dedup_k)
                    existing = next((lp for lp in self.liquidity_pools
                                     if abs(lp.price - sl.price) <= tol
                                     and lp.pool_type == "EQL"), None)
                    if existing and not existing.swept:
                        existing.swept                  = True
                        existing.sweep_timestamp        = now
                        existing.wick_rejection         = True
                        existing.displacement_confirmed = disp_ok
                        logger.info(f"💧 Swing EQL swept @ {sl.price:.0f}")
                    break

    # =========================================================================
    # ORDER BLOCK DETECTION
    # v9 addition: bos_confirmed flag — OB only qualifies if its impulse
    # broke a prior swing point.
    # =========================================================================

    def _detect_order_blocks(self, candles: List[Dict], current_time: int,
                              current_price: float, tf: str = "5m"):
        if len(candles) < 5:
            return

        min_impulse_pct = getattr(config, "OB_MIN_IMPULSE_PCT", 0.3)
        tol             = current_price * 0.001

        # Collect prior swing highs/lows for BOS confirmation
        prior_highs = sorted([s.price for s in self.swing_highs
                               if s.timeframe == tf], reverse=True)
        prior_lows  = sorted([s.price for s in self.swing_lows
                               if s.timeframe == tf])

        for i in range(2, len(candles) - 1):
            cur  = candles[i]
            nxt  = candles[i + 1]
            prev = candles[i - 1]

            cur_o  = float(cur['o']);  cur_c  = float(cur['c'])
            cur_h  = float(cur['h']);  cur_l  = float(cur['l'])
            nxt_o  = float(nxt['o']);  nxt_c  = float(nxt['c'])
            nxt_h  = float(nxt['h']);  nxt_l  = float(nxt['l'])
            prev_h = float(prev['h']); prev_l = float(prev['l'])

            impulse_up   = nxt_c > nxt_o and \
                           (nxt_c - nxt_o) / nxt_o * 100 >= min_impulse_pct
            impulse_down = nxt_c < nxt_o and \
                           (nxt_o - nxt_c) / nxt_o * 100 >= min_impulse_pct

            # Bullish OB: last bearish candle before a bullish impulse
            if impulse_up and cur_c < cur_o:
                # v9: check if impulse broke a prior swing high
                bos_ok = any(nxt_h > ph for ph in prior_highs[:3]) \
                         if prior_highs else False

                has_disp = nxt_h > prev_h
                wick_rej = (cur_h - max(cur_o, cur_c)) / max(cur_h - cur_l, 1) > 0.3
                strength = min(
                    (nxt_c - nxt_o) / nxt_o * 100 * 20 +
                    (10 if bos_ok   else 0) +
                    (10 if has_disp else 0) +
                    (10 if wick_rej else 0),
                    100)

                # Check inducement: was there a sweep just before this OB?
                cur_ts_ms = self._ts_ms(cur)
                inducement = any(
                    lp.swept and lp.pool_type == "EQL"
                    and abs(lp.price - cur_l) / current_price * 100 < 0.5
                    and lp.sweep_timestamp < cur_ts_ms
                    for lp in self.liquidity_pools)

                if not any(abs(ob.low - cur_l) <= tol and
                           abs(ob.high - cur_h) <= tol
                           for ob in self.order_blocks_bull):
                    self.order_blocks_bull.append(OrderBlock(
                        low=cur_l, high=cur_h, timestamp=cur_ts_ms,
                        has_wick_rejection=wick_rej, strength=strength,
                        direction="bullish", has_displacement=has_disp,
                        inducement_near=inducement, bos_confirmed=bos_ok,
                        visit_count=0))

            # Bearish OB: last bullish candle before a bearish impulse
            elif impulse_down and cur_c > cur_o:
                bos_ok   = any(nxt_l < pl for pl in prior_lows[:3]) \
                           if prior_lows else False
                has_disp = nxt_l < prev_l
                wick_rej = (min(cur_o, cur_c) - cur_l) / max(cur_h - cur_l, 1) > 0.3
                strength = min(
                    (nxt_o - nxt_c) / nxt_o * 100 * 20 +
                    (10 if bos_ok   else 0) +
                    (10 if has_disp else 0) +
                    (10 if wick_rej else 0),
                    100)

                cur_ts_ms = self._ts_ms(cur)
                inducement = any(
                    lp.swept and lp.pool_type == "EQH"
                    and abs(lp.price - cur_h) / current_price * 100 < 0.5
                    and lp.sweep_timestamp < cur_ts_ms
                    for lp in self.liquidity_pools)

                if not any(abs(ob.low - cur_l) <= tol and
                           abs(ob.high - cur_h) <= tol
                           for ob in self.order_blocks_bear):
                    self.order_blocks_bear.append(OrderBlock(
                        low=cur_l, high=cur_h, timestamp=cur_ts_ms,
                        has_wick_rejection=wick_rej, strength=strength,
                        direction="bearish", has_displacement=has_disp,
                        inducement_near=inducement, bos_confirmed=bos_ok,
                        visit_count=0))

    # =========================================================================
    # FVG DETECTION (unchanged from v8)
    # =========================================================================

    def _detect_fvgs(self, candles: List[Dict], current_time: int,
                     current_price: float, tf: str = "5m"):
        if len(candles) < 3:
            return
        min_size_pct = getattr(config, "FVG_MIN_SIZE_PCT", 0.05)
        tol          = current_price * 0.0003

        for i in range(1, len(candles) - 1):
            c1 = candles[i - 1]
            c3 = candles[i + 1]
            c1_h = float(c1['h']); c1_l = float(c1['l'])
            c3_h = float(c3['h']); c3_l = float(c3['l'])

            # Bullish FVG: c1 high < c3 low
            if c3_l > c1_h:
                gap_size = c3_l - c1_h
                if gap_size / current_price * 100 >= min_size_pct:
                    if not any(abs(f.bottom - c1_h) <= tol and
                               abs(f.top - c3_l) <= tol
                               for f in self.fvgs_bull):
                        self.fvgs_bull.append(FairValueGap(
                            bottom=c1_h, top=c3_l,
                            timestamp=self._ts_ms(candles[i]),
                            direction="bullish"))

            # Bearish FVG: c1 low > c3 high
            elif c1_l > c3_h:
                gap_size = c1_l - c3_h
                if gap_size / current_price * 100 >= min_size_pct:
                    if not any(abs(f.bottom - c3_h) <= tol and
                               abs(f.top - c1_l) <= tol
                               for f in self.fvgs_bear):
                        self.fvgs_bear.append(FairValueGap(
                            bottom=c3_h, top=c1_l,
                            timestamp=self._ts_ms(candles[i]),
                            direction="bearish"))

    def _update_fvg_fills(self, candles: List[Dict]):
        for fvg in list(self.fvgs_bull) + list(self.fvgs_bear):
            fvg.update_fill(candles)

    # =========================================================================
    # MARKET STRUCTURE DETECTION (unchanged from v8)
    # =========================================================================

    @staticmethod
    def _ts_ms(candle: Dict) -> int:
        """
        Normalize candle open-time to milliseconds.

        CoinSwitch REST API  →  microseconds  (~1.77 × 10¹⁵ for year 2026)
        CoinSwitch WebSocket →  milliseconds  (~1.77 × 10¹²  for year 2026)

        Boundary: year 9999 in ms = 253_402_300_799_999.
        Any timestamp above that boundary is in µs → divide by 1000.
        """
        ts = int(candle.get('t', 0))
        return ts // 1000 if ts > 253_402_300_799_999 else ts

    @staticmethod
    def _norm_ts(ts: int) -> int:
        """Normalize any stored timestamp to milliseconds (same rule as _ts_ms)."""
        return ts // 1000 if ts > 253_402_300_799_999 else ts

    def _detect_market_structure(self, candles: List[Dict],
                                  current_price: float,
                                  tf: str = "5m") -> None:
        """
        Full-history BOS / CHoCH scanner.

        Pass ALL warmup candles on init; pass recent candles on live updates.
        Scans in chronological order and builds a complete structural record.

        Classification:
          - First break of a pivot when trend is unknown  → BOS
          - Break that CONTINUES the current trend        → BOS
          - Break that OPPOSES the current trend          → CHoCH

        Displacement filter: breaking-candle body must be ≥ 35% of its total
        range. Eliminates doji spikes and wick-only breaks.

        Deduplication: no two structures of same tf + direction + type within
        0.15% price AND 6h timestamp window.
        """
        n        = len(candles)
        lb_left  = getattr(config, "SWING_LOOKBACK_LEFT",  3)
        lb_right = getattr(config, "SWING_LOOKBACK_RIGHT", 2)

        if n < lb_left + lb_right + 5:
            return

        # ── Step 1: Detect all confirmed pivot highs and lows ─────────────
        pivots_h: List[Tuple[int, float, int]] = []   # (idx, price, ts_ms)
        pivots_l: List[Tuple[int, float, int]] = []

        for i in range(lb_left, n - lb_right):
            h  = float(candles[i]['h'])
            l  = float(candles[i]['l'])
            ts = self._ts_ms(candles[i])

            left_range  = range(i - lb_left, i)
            right_range = range(i + 1, i + lb_right + 1)

            is_pivot_h = (
                all(h >= float(candles[j]['h']) for j in left_range) and
                all(h >= float(candles[j]['h']) for j in right_range))

            is_pivot_l = (
                all(l <= float(candles[j]['l']) for j in left_range) and
                all(l <= float(candles[j]['l']) for j in right_range))

            if is_pivot_h:
                pivots_h.append((i, h, ts))
            if is_pivot_l:
                pivots_l.append((i, l, ts))

        if not pivots_h or not pivots_l:
            return

        # ── Step 2: Dedup helpers ─────────────────────────────────────────
        price_tol = max(current_price * 0.0015, 10.0)
        time_tol  = 6 * 3_600_000

        cached_ms: List[MarketStructure] = [
            ms for ms in self.market_structures
            if ms.timeframe == tf]

        def _is_dup(direction: str, stype: str,
                    price: float, ts: int) -> bool:
            for ms in cached_ms:
                if (ms.direction       == direction
                        and ms.structure_type == stype
                        and abs(ms.price - price) <= price_tol
                        and abs(ms.timestamp - ts) <= time_tol):
                    return True
            return False

        # ── Step 3: Infer starting trend direction from prior structures ──
        prior = next(
            (ms for ms in reversed(list(self.market_structures))
             if ms.timeframe      == tf
             and ms.structure_type in ("BOS", "CHoCH")),
            None)
        current_trend: Optional[str] = prior.direction if prior else None

        # ── Step 4: Running pivot pointers ────────────────────────────────
        # ph_ptr always points to the latest usable pivot BEFORE candle i.
        # "Usable" = pivot index confirmed at least lb_right candles ago.
        ph_ptr          = 0
        pl_ptr          = 0
        last_bos_ph_idx = -1   # prevent same pivot firing two BOS events
        last_bos_pl_idx = -1

        scan_start = lb_left + lb_right + 1

        for i in range(scan_start, n):
            c      = candles[i]
            close  = float(c['c'])
            open_  = float(c['o'])
            ts     = self._ts_ms(c)
            body   = abs(close - open_)
            rng    = float(c['h']) - float(c['l'])

            # Displacement filter — body ≥ 35% of range
            if rng <= 0 or body / rng < 0.35:
                continue

            # Advance ph_ptr to the latest pivot confirmed before candle i.
            # A pivot at index p is confirmed after index p + lb_right,
            # so usable when i > p + lb_right  →  p < i - lb_right.
            while (ph_ptr + 1 < len(pivots_h) and
                   pivots_h[ph_ptr + 1][0] < i - lb_right):
                ph_ptr += 1
            while (pl_ptr + 1 < len(pivots_l) and
                   pivots_l[pl_ptr + 1][0] < i - lb_right):
                pl_ptr += 1

            ph_idx, ph_price, _ph_ts = pivots_h[ph_ptr]
            pl_idx, pl_price, _pl_ts = pivots_l[pl_ptr]

            # Guard: pivot must actually be confirmed before this candle
            if ph_idx >= i - lb_right or pl_idx >= i - lb_right:
                continue

            # ── Bullish break of prior swing high ─────────────────────────
            if close > ph_price and ph_idx != last_bos_ph_idx:
                stype = ("CHoCH"
                         if current_trend == "bearish" else "BOS")
                if not _is_dup("bullish", stype, ph_price, ts):
                    new_ms = MarketStructure(
                        structure_type     = stype,
                        price              = ph_price,
                        timestamp          = ts,
                        direction          = "bullish",
                        timeframe          = tf,
                        confirmed_sequence = True)
                    self.market_structures.append(new_ms)
                    cached_ms.append(new_ms)
                    current_trend   = "bullish"
                    last_bos_ph_idx = ph_idx
                    logger.debug(
                        f"MS {stype} BULLISH tf={tf} "
                        f"price={ph_price:.2f} "
                        f"candle={i}/{n}")

            # ── Bearish break of prior swing low ──────────────────────────
            elif close < pl_price and pl_idx != last_bos_pl_idx:
                stype = ("CHoCH"
                         if current_trend == "bullish" else "BOS")
                if not _is_dup("bearish", stype, pl_price, ts):
                    new_ms = MarketStructure(
                        structure_type     = stype,
                        price              = pl_price,
                        timestamp          = ts,
                        direction          = "bearish",
                        timeframe          = tf,
                        confirmed_sequence = True)
                    self.market_structures.append(new_ms)
                    cached_ms.append(new_ms)
                    current_trend   = "bearish"
                    last_bos_pl_idx = pl_idx
                    logger.debug(
                        f"MS {stype} BEARISH tf={tf} "
                        f"price={pl_price:.2f} "
                        f"candle={i}/{n}")


    # =========================================================================
    # SESSION & KILLZONE (unchanged from v8)
    # =========================================================================

    def _update_session_and_killzone(self, current_time: int):
        dt           = datetime.fromtimestamp(current_time / 1000, tz=timezone.utc)
        hour         = dt.hour
        weekday      = dt.weekday()  # 0=Mon … 6=Sun

        if weekday >= 5:
            self.current_session = "WEEKEND"
            self.in_killzone     = False
            return

        if   0  <= hour <  7: self.current_session = "ASIAN"
        elif 7  <= hour < 12: self.current_session = "LONDON"
        elif 12 <= hour < 17: self.current_session = "NEW_YORK"
        elif 17 <= hour < 20: self.current_session = "LONDON_CLOSE"
        else:                 self.current_session = "REGULAR"

        # ICT Killzones (UTC): London 7-9, NY 12-14, NY PM 19-20
        self.in_killzone = (
            (7  <= hour < 9)  or
            (12 <= hour < 14) or
            (19 <= hour < 20)
        )

    # =========================================================================
    # AMD PHASE (unchanged from v8)
    # =========================================================================

    def _update_amd_phase(self, current_time: int):
        dt   = datetime.fromtimestamp(current_time / 1000, tz=timezone.utc)
        hour = dt.hour
        if   2  <= hour <  8:  self.amd_phase = "ACCUMULATION"
        elif 8  <= hour < 14:  self.amd_phase = "MANIPULATION"
        elif 14 <= hour < 20:  self.amd_phase = "DISTRIBUTION"
        else:                  self.amd_phase = "REVERSAL"

    # =========================================================================
    # TCE — BIAS CHANGE HANDLER
    # =========================================================================

    def _tce_on_bias_change(self, new_bias: str, ts_now: int,
                             from_neutral: bool = False,
                             price_at_flip: float = 0.0):
        """Opens a new TrendChangeRecord on every HTF bias flip."""
        if self._active_tce and self._active_tce.tce_state != "EXPIRED":
            self._active_tce.tce_state      = "EXPIRED"
            self._active_tce.expired_reason = "Superseded by new bias flip"
            self._active_tce.expire_time    = ts_now
            logger.info("🔄 Previous TCE cycle superseded")

        self._active_tce = TrendChangeRecord(
            bias_direction=new_bias,
            bias_change_time=ts_now,
            tce_state="AWAITING_CONFIRMATION",
            price_at_flip=price_at_flip,
            max_age_ms=TCE_MAX_AGE_MS,
        )

        # If flipping from NEUTRAL, grant an immediate soft-confirm
        # if there is already a fresh BOS in the new direction
        if from_neutral:
            target_dir = "bullish" if new_bias == "BULLISH" else "bearish"
            recent_bos = next(
                (ms for ms in reversed(list(self.market_structures))
                 if ms.direction == target_dir
                 and ms.structure_type in ("BOS", "CHoCH")
                 and (ts_now - ms.timestamp) < 4 * 3600_000),
                None)
            if recent_bos:
                self._active_tce.confirmation_ms   = recent_bos
                self._active_tce.confirmation_time = recent_bos.timestamp
                self._active_tce.tce_state         = "AWAITING_PULLBACK"
                logger.info(
                    f"⚡ TCE soft-confirm from NEUTRAL flip: "
                    f"{recent_bos.structure_type} @ {recent_bos.price:.2f}")

        logger.info(
            f"🆕 TCE cycle opened: bias={new_bias} "
            f"state={self._active_tce.tce_state} "
            f"price_at_flip={price_at_flip:.2f}")
        send_telegram_message(
            f"🆕 *TCE Cycle Opened*\n"
            f"Bias={new_bias} | State={self._active_tce.tce_state}\n"
            f"Flip price={price_at_flip:.2f}")

    # =========================================================================
    # TCE — MAIN STATE MACHINE ADVANCE
    # v9: price-distance probability decay replaces pure time decay
    # =========================================================================

    def _tce_update(self, current_price: float, current_time: int,
                    candles_5m: Optional[List[Dict]] = None) -> str:
        tce = self._active_tce
        if tce is None or tce.tce_state == "EXPIRED":
            return "EXPIRED"

        # Regime-adaptive age cap
        regime_max_age = self.regime_engine.state.tce_max_age_ms
        effective_max  = min(tce.max_age_ms, regime_max_age)
        elapsed        = current_time - tce.bias_change_time
        elapsed_h      = elapsed / 3_600_000

        if elapsed > effective_max:
            tce.tce_state      = "EXPIRED"
            tce.expired_reason = (
                f"Age cap {effective_max / 3_600_000:.1f}h "
                f"(regime={self.regime_engine.state.regime})")
            tce.expire_time = current_time
            logger.info(
                f"⏱️ TCE EXPIRED — age cap {effective_max / 3_600_000:.1f}h "
                f"[{self.regime_engine.state.regime}]")
            return "EXPIRED"

        # ── AWAITING_CONFIRMATION ─────────────────────────────────────────
        if tce.tce_state == "AWAITING_CONFIRMATION":
            target_dir = "bullish" if tce.bias_direction == "BULLISH" else "bearish"
            ms_window  = 2 * 3_600_000

            recent_ms = [
                ms for ms in list(self.market_structures)
                if ms.timestamp > tce.bias_change_time
                and (current_time - ms.timestamp) <= ms_window
                and ms.direction == target_dir
                and ms.structure_type in ("BOS", "CHoCH")
            ]

            if recent_ms:
                confirmation            = sorted(recent_ms,
                                                  key=lambda x: x.timestamp)[0]
                tce.confirmation_ms     = confirmation
                tce.confirmation_time   = confirmation.timestamp

                if tce.bias_direction == "BULLISH":
                    cand_obs  = [ob for ob in self.order_blocks_bull
                                 if ob.timestamp > tce.bias_change_time
                                 and ob.is_active(current_time)]
                    cand_fvgs = [fvg for fvg in self.fvgs_bull
                                 if fvg.timestamp > tce.bias_change_time
                                 and fvg.is_active(current_time)]
                else:
                    cand_obs  = [ob for ob in self.order_blocks_bear
                                 if ob.timestamp > tce.bias_change_time
                                 and ob.is_active(current_time)]
                    cand_fvgs = [fvg for fvg in self.fvgs_bear
                                 if fvg.timestamp > tce.bias_change_time
                                 and fvg.is_active(current_time)]

                if cand_obs:
                    best_ob              = sorted(cand_obs,
                                                   key=lambda x: x.strength,
                                                   reverse=True)[0]
                    tce.target_ob        = best_ob
                    tce.target_zone_low  = best_ob.low
                    tce.target_zone_high = best_ob.high
                elif cand_fvgs:
                    tce.target_fvg       = cand_fvgs[0]
                    tce.target_zone_low  = cand_fvgs[0].bottom
                    tce.target_zone_high = cand_fvgs[0].top

                tce.tce_state = "AWAITING_PULLBACK"
                logger.info(
                    f"🔄 TCE: AWAITING_CONFIRMATION → AWAITING_PULLBACK "
                    f"via {confirmation.structure_type} @ {confirmation.price:.2f} "
                    f"zone=[{tce.target_zone_low:.2f}–{tce.target_zone_high:.2f}]")
            else:
                logger.debug(
                    f"TCE AWAITING_CONFIRMATION elapsed={elapsed_h:.1f}h "
                    f"bias={tce.bias_direction}")

        # ── AWAITING_PULLBACK ─────────────────────────────────────────────
        elif tce.tce_state == "AWAITING_PULLBACK":
            has_target = tce.target_zone_low > 0 and tce.target_zone_high > 0
            # If no zone (cold start reconstruction), skip zone gate entirely
            # and let time/momentum decay advance the state naturally
            if not has_target:
                if tce.pullback_probability <= TCE_MOMENTUM_THRESHOLD:
                    tce.tce_state = "MOMENTUM_ENTRY_ALLOWED"
                    tce.momentum_mode_start = current_time
                    logger.info(
                        f"TCE AWAITING_PULLBACK → MOMENTUM_ENTRY_ALLOWED "
                        f"(no zone — time decay) prob={tce.pullback_probability:.2f}")
                return tce.tce_state

            # v9: price-distance-dominant probability decay
            if tce.last_probability_update == 0:
                tce.last_probability_update = current_time

            if has_target:
                zone_mid      = (tce.target_zone_low + tce.target_zone_high) / 2
                price_dist    = abs(current_price - zone_mid) / max(zone_mid, 1)
                # Distance decay: 0% at zone, 100% at 3% away
                dist_decay    = min(price_dist / 0.03, 1.0)
                # Time decay: linear over effective lifecycle
                time_decay    = min(elapsed / effective_max, 1.0)
                # Weighted combination: distance dominates (70/30)
                tce.pullback_probability = max(
                    0.0,
                    1.0 - (0.70 * dist_decay + 0.30 * time_decay))
                tce.last_probability_update = current_time

                logger.debug(
                    f"TCE pullback_prob={tce.pullback_probability:.2f} "
                    f"dist_decay={dist_decay:.2f} time_decay={time_decay:.2f} "
                    f"elapsed={elapsed_h:.1f}h")

                in_zone = tce.target_zone_low <= current_price <= tce.target_zone_high
                if in_zone:
                    tce.tce_state = "PULLBACK_REACHED"
                    logger.info(
                        f"✅ TCE: AWAITING_PULLBACK → PULLBACK_REACHED "
                        f"price={current_price:.2f} "
                        f"zone=[{tce.target_zone_low:.2f}–{tce.target_zone_high:.2f}]")
            else:
                # No target zone: pure time decay at 0.08/15min
                time_since = current_time - tce.last_probability_update
                decay_steps = time_since // (15 * 60_000)
                if decay_steps > 0:
                    tce.pullback_probability = max(
                        0.0, tce.pullback_probability - 0.08 * decay_steps)
                    tce.last_probability_update = current_time

            # Switch to momentum mode if probability decayed past threshold
            if (tce.pullback_probability <= TCE_MOMENTUM_THRESHOLD
                    and tce.tce_state == "AWAITING_PULLBACK"):
                tce.tce_state           = "MOMENTUM_ENTRY_ALLOWED"
                tce.momentum_mode_start = current_time
                logger.info(
                    f"⚡ TCE: AWAITING_PULLBACK → MOMENTUM_ENTRY_ALLOWED "
                    f"prob={tce.pullback_probability:.2f} elapsed={elapsed_h:.1f}h")

        # ── PULLBACK_REACHED ──────────────────────────────────────────────
        elif tce.tce_state == "PULLBACK_REACHED":
            if tce.target_zone_low > 0 and tce.target_zone_high > 0:
                if tce.bias_direction == "BULLISH":
                    zone_busted = current_price < tce.target_zone_low * 0.995
                else:
                    zone_busted = current_price > tce.target_zone_high * 1.005
                if zone_busted:
                    tce.tce_state      = "EXPIRED"
                    tce.expired_reason = "Target zone busted"
                    tce.expire_time    = current_time
                    logger.info(
                        f"❌ TCE EXPIRED — zone busted "
                        f"price={current_price:.2f} "
                        f"zone=[{tce.target_zone_low:.2f}–{tce.target_zone_high:.2f}]")

        # ── MOMENTUM_ENTRY_ALLOWED ────────────────────────────────────────
        elif tce.tce_state == "MOMENTUM_ENTRY_ALLOWED":
            logger.debug(
                f"TCE MOMENTUM elapsed={elapsed_h:.1f}h "
                f"prob={tce.pullback_probability:.2f}")

        return tce.tce_state

    # =========================================================================
    # TCE — ENTRY MODIFIER
    # =========================================================================

    def _tce_get_entry_modifier(self) -> Tuple[float, str]:
        """
        Returns (score_bonus, gate_description).
        PULLBACK_REACHED      → +25 bonus
        MOMENTUM_ENTRY_ALLOWED → +0  bonus (confluence gate only)
        EXPIRED / None         → +0  steady-state, no restriction
        AWAITING_*             → -999 hard gate
        """
        if self._active_tce is None or self._active_tce.tce_state == "EXPIRED":
            return 0.0, "STEADY_STATE"
        state = self._active_tce.tce_state
        if state == "PULLBACK_REACHED":
            return 25.0, "TCE_PULLBACK_REACHED"
        if state == "MOMENTUM_ENTRY_ALLOWED":
            return 0.0, "TCE_MOMENTUM"
        if state in ("AWAITING_CONFIRMATION", "AWAITING_PULLBACK"):
            return -999.0, f"TCE_BLOCKED_{state}"
        return 0.0, "STEADY_STATE"

    def _tce_mark_entry_taken(self):
        if self._active_tce and self._active_tce.tce_state != "EXPIRED":
            self._active_tce.entry_taken    = True
            self._active_tce.tce_state      = "EXPIRED"
            self._active_tce.expired_reason = "Entry taken"
            self._active_tce.expire_time    = int(time.time() * 1000)
            logger.info("✅ TCE cycle closed — entry taken")

    def _tce_reinit_if_needed(self, current_price: float,
                               current_time: int) -> None:
        """
        Startup TCE reconstruction from detected market structures.
        No fallbacks. If reconstruction returns None, active_tce = None
        (STEADY_STATE). The next live HTF bias change opens a proper cycle.
        """
        if self.htf_bias == "NEUTRAL":
            return

        if self._active_tce and self._active_tce.tce_state != "EXPIRED":
            logger.info(
                f"TCE startup: existing cycle "
                f"[{self._active_tce.tce_state}] — no reconstruction needed")
            return

        all_ms = list(self.market_structures)
        bear_count = sum(1 for m in all_ms if m.direction == "bearish")
        bull_count = sum(1 for m in all_ms if m.direction == "bullish")
        bos_count  = sum(1 for m in all_ms if m.structure_type == "BOS")
        logger.info(
            f"🔬 TCE reconstruction starting — "
            f"HTF={self.htf_bias} price={current_price:.2f} "
            f"structures={len(all_ms)} "
            f"(bear={bear_count} bull={bull_count} BOS={bos_count})")

        try:
            tce = self._reconstruct_tce_from_structures(
                current_price, current_time)

            if tce is None:
                self._active_tce = None
                logger.warning(
                    "⚠️ TCE reconstruction: no qualifying structure found "
                    "— active_tce=None (STEADY_STATE until next live BOS)")
                return

            if tce.tce_state == "EXPIRED":
                self._active_tce = None
                logger.info(
                    "TCE reconstruction: origin BOS exceeded age cap "
                    "— active_tce=None (STEADY_STATE)")
                return

            self._active_tce = tce

            tce_age_h  = (self._norm_ts(current_time) - tce.bias_change_time) / 3_600_000
            tce_tf     = tce.confirmation_ms.timeframe if tce.confirmation_ms else "?"
            tce_prob   = f"{tce.pullback_probability:.2f}"
            tce_zone   = f"{tce.target_zone_low:.0f}–{tce.target_zone_high:.0f}"

            logger.info(
                f"✅ TCE reconstructed → state={tce.tce_state} "
                f"tf={tce_tf} "
                f"origin={tce.price_at_flip:.0f} "
                f"zone={tce_zone} "
                f"prob={tce_prob} "
                f"age={tce_age_h:.1f}h")

            send_telegram_message(
                f"🔬 *TCE Reconstructed*\n"
                f"Bias={self.htf_bias}  State={tce.tce_state}\n"
                f"Origin={tce.price_at_flip:.0f}  Age={tce_age_h:.1f}h\n"
                f"Zone={tce_zone}\n"
                f"Prob={tce.pullback_probability:.0%}")

        except Exception as e:
            self._active_tce = None
            logger.error(f"❌ TCE reconstruction failed: {e}", exc_info=True)

    def _reconstruct_tce_from_structures(
            self,
            current_price: float,
            current_time: int) -> Optional[TrendChangeRecord]:
        """
        Reconstruct TrendChangeRecord from detected market structures.

        Steps:
          1. Normalize all timestamps to ms (CoinSwitch REST = µs).
          2. Find origin BOS in HTF bias direction (4h → 1h → 15m → 5m).
          3. Confirm displacement: a swing extreme past the BOS price
             must exist AFTER the BOS (proves displacement happened).
          4. Find target zone: OB/FVG in the CORRECT SPATIAL DIRECTION
             — for BEARISH: OB must be ABOVE current price (pullback = up).
             — for BULLISH: OB must be BELOW current price (pullback = down).
          5. Compute probability using live decay formula (coherent with
             tce_update so no behavioral discontinuity on restart).
          6. Classify state.
        """
        target_dir = "bullish" if self.htf_bias == "BULLISH" else "bearish"

        # ── Normalize current_time to ms ──────────────────────────────────
        ct_ms = self._norm_ts(current_time)

        # ── 1. Origin BOS ─────────────────────────────────────────────────
        origin_bos: Optional[MarketStructure] = None
        for tf_pref in ("4h", "1h", "15m", "5m"):
            origin_bos = next(
                (ms for ms in reversed(list(self.market_structures))
                 if ms.direction      == target_dir
                 and ms.structure_type in ("BOS", "CHoCH")
                 and ms.timeframe     == tf_pref
                 and (ct_ms - self._norm_ts(ms.timestamp))
                     < 10 * 24 * 3_600_000),
                None)
            if origin_bos:
                break

        if origin_bos is None:
            logger.debug(
                "TCE reconstruct: no origin BOS in bias direction within 10d")
            return None

        bias_change_time = self._norm_ts(origin_bos.timestamp)
        price_at_flip    = origin_bos.price

        logger.debug(
            f"TCE reconstruct: origin_bos "
            f"tf={origin_bos.timeframe} "
            f"price={price_at_flip:.2f} "
            f"type={origin_bos.structure_type} "
            f"age={(ct_ms - bias_change_time)/3_600_000:.1f}h")

        # ── 2. Displacement confirmation ──────────────────────────────────
        # After the origin BOS, price must have moved significantly
        # in the breakout direction to confirm real displacement occurred.
        min_disp_pct = getattr(config, "TCE_MIN_DISPLACEMENT_PCT", 0.002)

        if self.htf_bias == "BEARISH":
            # Need a swing LOW after BOS that is below BOS price
            displacement_confirmed = any(
                self._norm_ts(sw.timestamp) > bias_change_time
                and sw.price < price_at_flip * (1.0 - min_disp_pct)
                for sw in self.swing_lows)
        else:
            # Need a swing HIGH after BOS that is above BOS price
            displacement_confirmed = any(
                self._norm_ts(sw.timestamp) > bias_change_time
                and sw.price > price_at_flip * (1.0 + min_disp_pct)
                for sw in self.swing_highs)

        if not displacement_confirmed:
            logger.info(
                f"TCE reconstruct: displacement not confirmed after "
                f"{origin_bos.timeframe} {target_dir} BOS@{price_at_flip:.0f} "
                f"(min_disp={min_disp_pct:.1%}) — skipping")
            return None

        # ── 3. Target zone ────────────────────────────────────────────────
        # For BEARISH: pullback = price goes UP → target OB must be ABOVE price.
        # For BULLISH: pullback = price goes DOWN → target OB must be BELOW price.
        if self.htf_bias == "BEARISH":
            cand_obs = [
                ob for ob in self.order_blocks_bear
                if self._norm_ts(ob.timestamp) > bias_change_time
                and ob.is_active(ct_ms)
                and ob.low > current_price]          # OB above current price ←
            cand_fvgs = [
                fvg for fvg in self.fvgs_bear
                if self._norm_ts(fvg.timestamp) > bias_change_time
                and fvg.is_active(ct_ms)
                and fvg.bottom > current_price]      # FVG above current price ←
        else:
            cand_obs = [
                ob for ob in self.order_blocks_bull
                if self._norm_ts(ob.timestamp) > bias_change_time
                and ob.is_active(ct_ms)
                and ob.high < current_price]         # OB below current price ←
            cand_fvgs = [
                fvg for fvg in self.fvgs_bull
                if self._norm_ts(fvg.timestamp) > bias_change_time
                and fvg.is_active(ct_ms)
                and fvg.top < current_price]         # FVG below current price ←

        target_ob:  Optional[OrderBlock]   = None
        target_fvg: Optional[FairValueGap] = None
        zone_low  = 0.0
        zone_high = 0.0

        if cand_obs:
            # Best = highest strength; for bearish prefer lowest OB above price
            # (closest to current price = highest probability of being reached)
            if self.htf_bias == "BEARISH":
                target_ob = min(cand_obs, key=lambda x: x.low)
            else:
                target_ob = max(cand_obs, key=lambda x: x.high)
            zone_low  = target_ob.low
            zone_high = target_ob.high

        if cand_fvgs:
            if self.htf_bias == "BEARISH":
                best_fvg = min(cand_fvgs, key=lambda x: x.bottom)
            else:
                best_fvg = max(cand_fvgs, key=lambda x: x.top)
            if target_ob is None:
                target_fvg = best_fvg
                zone_low   = best_fvg.bottom
                zone_high  = best_fvg.top
            else:
                target_fvg = best_fvg   # secondary reference only

        # ── 4. Probability decay ──────────────────────────────────────────
        in_zone = (zone_low > 0
                   and zone_low <= current_price <= zone_high)

        if zone_low > 0:
            if self.htf_bias == "BEARISH":
                # Price must rally UP to zone; distance = zone_low - price
                raw_dist = max(0.0, zone_low - current_price)
            else:
                # Price must fall DOWN to zone; distance = price - zone_high
                raw_dist = max(0.0, current_price - zone_high)
            dist_pct = raw_dist / max(current_price, 1.0)
        else:
            dist_pct = 0.0   # no qualifying zone — time decay only

        dist_decay    = min(dist_pct / 0.03, 1.0)
        elapsed_ms    = ct_ms - bias_change_time
        regime_max    = self.regime_engine.state.tce_max_age_ms
        effective_max = min(TCE_MAX_AGE_MS, regime_max)
        time_decay    = min(elapsed_ms / max(effective_max, 1), 1.0)
        prob          = max(0.0,
                            1.0 - (0.70 * dist_decay + 0.30 * time_decay))

        # ── 5. State classification ───────────────────────────────────────
        if elapsed_ms >= effective_max:
            tce_state = "EXPIRED"
        elif in_zone:
            tce_state = "PULLBACK_REACHED"
        elif prob <= TCE_MOMENTUM_THRESHOLD:
            tce_state = "MOMENTUM_ENTRY_ALLOWED"
        else:
            tce_state = "AWAITING_PULLBACK"

        logger.info(
            f"TCE reconstruct detail: "
            f"origin={origin_bos.timeframe}@{price_at_flip:.0f} "
            f"displacement=✓ "
            f"age={elapsed_ms/3_600_000:.1f}h "
            f"zone={zone_low:.0f}–{zone_high:.0f} "
            f"above_price={'✓' if zone_low > current_price else '✗'} "
            f"in_zone={in_zone} "
            f"dist_pct={dist_pct:.3f} "
            f"dist_decay={dist_decay:.2f} "
            f"time_decay={time_decay:.2f} "
            f"prob={prob:.2f} → {tce_state}")

        return TrendChangeRecord(
            bias_direction          = self.htf_bias,
            bias_change_time        = bias_change_time,
            tce_state               = tce_state,
            confirmation_ms         = origin_bos,
            confirmation_time       = bias_change_time,
            target_ob               = target_ob,
            target_fvg              = target_fvg,
            target_zone_low         = zone_low,
            target_zone_high        = zone_high,
            pullback_probability    = prob,
            last_probability_update = ct_ms,
            entry_taken             = False,
            price_at_flip           = price_at_flip,
            max_age_ms              = TCE_MAX_AGE_MS,
        )


    # =========================================================================
    # STRUCTURE CLEANUP (unchanged from v8)
    # =========================================================================

    def _cleanup_structures(self, current_price: float, current_time: int):
        max_dist_pct = getattr(config, "STRUCTURE_CLEANUP_DISTANCE_PCT", 5.0)

        for col in (self.order_blocks_bull, self.order_blocks_bear):
            for ob in list(col):
                if not ob.is_active(current_time):
                    continue
                if abs(ob.midpoint - current_price) / current_price * 100 \
                        > max_dist_pct:
                    ob.broken = True

        for col in (self.fvgs_bull, self.fvgs_bear):
            for fvg in list(col):
                if not fvg.is_active(current_time):
                    continue
                if abs(fvg.midpoint - current_price) / current_price * 100 \
                        > max_dist_pct:
                    fvg.filled = True

        # Purge old sweeps from dedup set to avoid unbounded growth
        cutoff = current_time - 48 * 3_600_000
        self._registered_sweeps = {
            key for key in self._registered_sweeps
            if key[1] > cutoff
        }

    # =========================================================================
    # FAILED SETUP INTELLIGENCE (unchanged from v8)
    # =========================================================================

    def _record_failed_setup(self, ctx: TriggerContext, side: str,
                              entry_price: float, sl_price: float):
        risk = abs(entry_price - sl_price)
        self.failed_setups.append(FailedSetupRecord(
            side=side,
            entry_price=entry_price,
            sl_price=sl_price,
            trigger_ob_low=ctx.trigger_ob.low   if ctx.trigger_ob  else None,
            trigger_ob_high=ctx.trigger_ob.high  if ctx.trigger_ob  else None,
            trigger_fvg_bottom=ctx.trigger_fvg.bottom if ctx.trigger_fvg else None,
            trigger_fvg_top=ctx.trigger_fvg.top    if ctx.trigger_fvg else None,
            risk=risk,
            timestamp=int(time.time() * 1000),
        ))
        self._last_sl_hit_time = int(time.time() * 1000)

    def _is_failed_setup_invalidated(self, record: FailedSetupRecord,
                                      current_price: float,
                                      current_time: int) -> bool:
        """
        A failed setup is invalidated (re-entry allowed) when at least one
        of the following is true:
          1. A new BOS in the same direction has formed since the SL hit.
          2. Price has moved more than 3× the original risk away.
          3. More than 4 hours have elapsed.
        """
        # 1. New BOS since SL hit
        target_dir = "bullish" if record.side == "long" else "bearish"
        new_bos = any(
            ms.direction == target_dir
            and ms.structure_type == "BOS"
            and ms.timestamp > record.timestamp
            for ms in self.market_structures)
        if new_bos:
            return True

        # 2. Price moved > 3× risk
        if record.risk > 0:
            dist = abs(current_price - record.entry_price)
            if dist > record.risk * 3:
                return True

        # 3. Age > 4h
        age_ms = current_time - record.timestamp
        if age_ms > 4 * 3_600_000:
            return True

        return False

    def _check_failed_setups(self, side: str, current_price: float,
                              current_time: int) -> bool:
        """Returns True if entry is blocked by a recent failed setup."""
        for record in self.failed_setups:
            if record.invalidated or record.side != side:
                continue
            if self._is_failed_setup_invalidated(
                    record, current_price, current_time):
                record.invalidated = True
                continue
            logger.info(
                f"🚫 Failed setup gate [{side}] entry={record.entry_price:.2f} "
                f"sl={record.sl_price:.2f} "
                f"age={(current_time - record.timestamp) / 60_000:.0f}m")
            return True
        return False

    # =========================================================================
    # SELF-ADAPTATION FEEDBACK (v9)
    # =========================================================================

    def _update_adaptation_state(self):
        """Called after every closed trade. Updates capital preservation mode."""
        if self.consecutive_losses >= CONSEC_LOSS_PRESERVE_AT:
            if not self._capital_preserve_mode:
                self._capital_preserve_mode = True
                logger.warning(
                    f"⚠️ CAPITAL PRESERVATION MODE ON "
                    f"({self.consecutive_losses} consecutive losses)")
                send_telegram_message(
                    f"⚠️ *Capital Preservation Mode*\n"
                    f"{self.consecutive_losses} consecutive losses.\n"
                    f"Threshold +{PRESERVE_THRESHOLD_ADD:.0f} | "
                    f"Size {PRESERVE_SIZE_MULT:.0%}")
        else:
            if self._capital_preserve_mode:
                self._capital_preserve_mode = False
                logger.info("✅ Capital preservation mode lifted")
                send_telegram_message("✅ *Capital preservation mode lifted*")

    def _get_adaptive_threshold_add(self) -> float:
        """Extra points added to entry threshold from self-adaptation."""
        rs    = self.regime_engine.state
        extra = self._perf_tracker.threshold_add(rs.regime)
        if self._capital_preserve_mode:
            extra = max(extra, PRESERVE_THRESHOLD_ADD)
        return extra

    def _get_adaptive_size_mult(self) -> float:
        """Combined size multiplier from regime performance + preserve mode."""
        rs   = self.regime_engine.state
        mult = self._perf_tracker.size_mult(rs.regime)
        if self._capital_preserve_mode:
            mult = min(mult, PRESERVE_SIZE_MULT)
        return mult

    # =========================================================================
    # EMA HELPER (unchanged from v8)
    # =========================================================================

    def _calculate_ema(self, prices: List[float], period: int) -> float:
        if len(prices) < period:
            return prices[-1] if prices else 0.0
        k   = 2.0 / (period + 1)
        ema = sum(prices[:period]) / period
        for p in prices[period:]:
            ema = p * k + ema * (1 - k)
        return ema

    # =========================================================================
    # CONFLUENCE SCORING
    # v9: virgin OB multiplier, intermarket hooks, DR scoring, regime scaling
    # =========================================================================

    def _calculate_confluence_score(
            self, side: str, current_price: float,
            data_manager, current_time: int,
    ) -> Tuple[float, List[str], TriggerContext]:
        try:
            score    = 0.0
            reasons: List[str] = []
            ctx      = TriggerContext()
            now_ms   = current_time

            # ── Directional bias filter (HTF primary, STF if HTF neutral) ──
            exec_bias, exec_strength, _ = self._resolve_directional_bias(current_time)
            neutral_htf_mode = (self.htf_bias == "NEUTRAL")

            if exec_bias == "NEUTRAL":
                return 0.0, ["No directional bias"], ctx
            if exec_bias == "BULLISH" and side == "short":
                return 0.0, ["Directional bias BULLISH — shorts blocked"], ctx
            if exec_bias == "BEARISH" and side == "long":
                return 0.0, ["Directional bias BEARISH — longs blocked"], ctx

            # ── 1. Bias score (HTF or neutral/STF execution mode) ───────────
            if (side == "long" and exec_bias == "BULLISH") or \
               (side == "short" and exec_bias == "BEARISH"):
                bias_weight = config.SCORE_HTF_BIAS * (0.65 if neutral_htf_mode else 1.0)
                bias_score = exec_strength * bias_weight
                score += bias_score
                if bias_score > 0:
                    src = "STF" if neutral_htf_mode else "HTF"
                    reasons.append(f"{src} {exec_bias} +{bias_score:.1f}")

            # ── 2. Sweep / Liquidity (0-25) ───────────────────────────────
            sweep_age_max  = getattr(config, "SWEEP_MAX_AGE_MINUTES", 120) * 60_000
            relevant_pools = [
                lp for lp in self.liquidity_pools
                if lp.swept and lp.displacement_confirmed
                and (now_ms - lp.sweep_timestamp) <= sweep_age_max
            ]
            if side == "long":
                for lp in relevant_pools:
                    if lp.pool_type == "EQL":
                        age_f  = max(1.0 - (now_ms - lp.sweep_timestamp)
                                     / sweep_age_max, 0.3)
                        s_scr  = config.SCORE_SWEEP * age_f
                        score += s_scr
                        ctx.sweep_detected = True
                        ctx.sweep_price    = lp.price
                        reasons.append(f"EQL sweep {lp.price:.0f} +{s_scr:.1f}")
                        break
            else:
                for lp in relevant_pools:
                    if lp.pool_type == "EQH":
                        age_f  = max(1.0 - (now_ms - lp.sweep_timestamp)
                                     / sweep_age_max, 0.3)
                        s_scr  = config.SCORE_SWEEP * age_f
                        score += s_scr
                        ctx.sweep_detected = True
                        ctx.sweep_price    = lp.price
                        reasons.append(f"EQH sweep {lp.price:.0f} +{s_scr:.1f}")
                        break

            # ── 3. Order Block (0-40, v9 virgin multiplier) ───────────────
            ob_scored  = False
            obs        = (self.order_blocks_bull if side == "long"
                          else self.order_blocks_bear)
            active_obs = [ob for ob in obs if ob.is_active(now_ms)]
            for ob in sorted(active_obs, key=lambda x: x.strength, reverse=True):
                dist_pct   = abs(current_price - ob.midpoint) / current_price * 100
                age_min    = (now_ms - ob.timestamp) / 60_000
                recency_b  = max(0.0, 8.0 * (1 - age_min / 120))
                disp_bonus = 5.0 if ob.has_displacement  else 0.0
                ind_bonus  = 5.0 if ob.inducement_near   else 0.0
                bos_bonus  = 5.0 if ob.bos_confirmed     else 0.0   # v9
                virgin_m   = ob.score_multiplier()                  # v9

                if ob.contains_price(current_price):
                    wick_m   = 1.0 if ob.has_wick_rejection else 0.80
                    ob_score = ((ob.strength / 100) * config.SCORE_OB_OPTIMAL
                                * wick_m * virgin_m
                                + recency_b + disp_bonus + ind_bonus + bos_bonus)
                    score   += ob_score
                    reasons.append(
                        f"IN OB {ob.low:.0f}-{ob.high:.0f} "
                        f"str={ob.strength:.0f} "
                        f"disp={'Y' if ob.has_displacement else 'N'} "
                        f"bos={'Y' if ob.bos_confirmed else 'N'} "
                        f"visits={ob.visit_count} "
                        f"+{ob_score:.1f}")
                    ctx.trigger_ob = ob
                    if ob.inducement_near:
                        ctx.inducement_confirmed = True
                    ob_scored = True
                    break
                elif ob.in_optimal_zone(current_price):
                    wick_m   = 1.0 if ob.has_wick_rejection else 0.80
                    ob_score = ((ob.strength / 100) * config.SCORE_OB_OPTIMAL
                                * wick_m * virgin_m
                                + recency_b + disp_bonus + bos_bonus)
                    score   += ob_score
                    reasons.append(
                        f"OB OTE {ob.low:.0f}-{ob.high:.0f} "
                        f"str={ob.strength:.0f} "
                        f"bos={'Y' if ob.bos_confirmed else 'N'} "
                        f"+{ob_score:.1f}")
                    ctx.trigger_ob = ob
                    ob_scored = True
                    break
                elif dist_pct <= 0.3:
                    prox     = 1.0 - dist_pct / 0.3
                    ob_score = ((ob.strength / 100) * config.SCORE_OB_PROXIMITY
                                * prox * virgin_m + recency_b)
                    score   += ob_score
                    reasons.append(
                        f"Near OB {ob.low:.0f}-{ob.high:.0f} "
                        f"dist={dist_pct:.2f}% "
                        f"visits={ob.visit_count} "
                        f"+{ob_score:.1f}")
                    ctx.trigger_ob = ob
                    ob_scored = True
                    break

            # ── 4. Fair Value Gap (0-35) ──────────────────────────────────
            fvg_scored = False
            fvgs       = self.fvgs_bull if side == "long" else self.fvgs_bear
            for fvg in [f for f in fvgs if f.is_active(now_ms)]:
                dist_pct = abs(current_price - fvg.midpoint) / current_price * 100
                if fvg.is_price_in_gap(current_price):
                    freshness = 1.0 - fvg.fill_percentage
                    fvg_score = (config.SCORE_FVG +
                                 (config.SCORE_IFVG if fvg.is_ifvg else 0)) \
                                * (0.5 + 0.5 * freshness)
                    fvg_score = min(fvg_score, 32)
                    score    += fvg_score
                    tag       = "IFVG" if fvg.is_ifvg else "FVG"
                    reasons.append(
                        f"IN {tag} {fvg.bottom:.0f}-{fvg.top:.0f} "
                        f"fill={fvg.fill_percentage * 100:.0f}% "
                        f"+{fvg_score:.1f}")
                    ctx.trigger_fvg = fvg
                    fvg_scored = True
                    break
                elif dist_pct <= 0.3:
                    prox      = 1.0 - dist_pct / 0.3
                    fvg_score = 15 * prox
                    score    += fvg_score
                    reasons.append(
                        f"Near FVG {fvg.bottom:.0f}-{fvg.top:.0f} "
                        f"+{fvg_score:.1f}")
                    ctx.trigger_fvg = fvg
                    fvg_scored = True
                    break

            # ── 5. OB + FVG confluence bonus ──────────────────────────────
            if ob_scored and fvg_scored:
                score += 8
                reasons.append("OB+FVG confluence +8")

            # ── 6. Market Structure (0-15) ────────────────────────────────
            recent_ms = [
                ms for ms in list(self.market_structures)[-5:]
                if (now_ms - ms.timestamp) <= 1_800_000
            ]
            for ms in reversed(recent_ms):
                if (side == "long"  and ms.direction == "bullish") or \
                   (side == "short" and ms.direction == "bearish"):
                    ms_score = (15 if ms.confirmed_sequence else 10) \
                               if ms.structure_type == "CHoCH" else 10
                    score   += ms_score
                    reasons.append(
                        f"{ms.structure_type} {ms.direction} "
                        f"[{ms.timeframe}] +{ms_score}")
                    break

            # ── 7. Volume Delta / CVD (0-25) ──────────────────────────────
            if self.volume_analyzer and \
                    len(self.volume_analyzer.cvd_history) >= 10:
                cvd_sig = self.volume_analyzer.get_cvd_signal(lookback=100)
                signal  = cvd_sig.get("signal", "NEUTRAL")
                if side == "long":
                    if "STRONG" in signal and "BULL" in signal:
                        score += 10; reasons.append("CVD Strong Bullish +10")
                    elif "BULL" in signal:
                        score += 5;  reasons.append("CVD Bullish +5")
                else:
                    if "STRONG" in signal and "BEAR" in signal:
                        score += 10; reasons.append("CVD Strong Bearish +10")
                    elif "BEAR" in signal:
                        score += 5;  reasons.append("CVD Bearish +5")

                if self.volume_analyzer.price_at_poc(current_price):
                    score += 5
                    reasons.append(
                        f"At POC {self.volume_analyzer.poc_price:.0f} +5")
                va_edge = self.volume_analyzer.price_at_value_area_edge(
                    current_price)
                if va_edge == "VAL" and side == "long":
                    score += 5;  reasons.append("At VAL +5")
                elif va_edge == "VAH" and side == "short":
                    score += 5;  reasons.append("At VAH +5")
            else:
                vd = data_manager.get_volume_delta(lookback_seconds=300) \
                     if hasattr(data_manager, "get_volume_delta") else None
                if vd is not None:
                    delta_pct = vd.get("delta_pct", 0.0) \
                                if isinstance(vd, dict) else 0.0
                    if side == "long":
                        if delta_pct >= config.VOLUME_DELTA_STRONG_THRESHOLD:
                            score += 10; reasons.append("Strong Buy Vol +10")
                        elif delta_pct >= config.VOLUME_DELTA_MODERATE_THRESHOLD:
                            score += 5;  reasons.append("Moderate Buy Vol +5")
                    else:
                        if delta_pct <= -config.VOLUME_DELTA_STRONG_THRESHOLD:
                            score += 10; reasons.append("Strong Sell Vol +10")
                        elif delta_pct <= -config.VOLUME_DELTA_MODERATE_THRESHOLD:
                            score += 5;  reasons.append("Moderate Sell Vol +5")

            # ── 8. Institutional Absorption (0-15) ────────────────────────
            if self.liquidity_model:
                abs_bonus = self.liquidity_model.absorption_confluence(
                    current_price, side)
                if abs_bonus > 0:
                    score += abs_bonus
                    reasons.append(
                        f"Institutional Absorption +{abs_bonus:.0f}")

            # ── 9. AMD Phase (0-12) ───────────────────────────────────────
            if self.amd_phase == "MANIPULATION" and ctx.sweep_detected:
                score += 12; reasons.append("AMD Manipulation Sweep +12")
            elif self.amd_phase == "REVERSAL" and ctx.sweep_detected:
                score += 8;  reasons.append("AMD Reversal Sweep +8")

            # ── 10. Inducement (0-8) ──────────────────────────────────────
            if ctx.inducement_confirmed:
                score += 8; reasons.append("Inducement confirmed pre-OB +8")

            # ── 11. Intermarket context (optional hooks) ──────────────────
            try:
                funding = data_manager.get_funding_rate() \
                          if hasattr(data_manager, "get_funding_rate") else None
                if funding is not None:
                    if side == "long" and funding > 0.03:
                        score -= 10
                        reasons.append(
                            f"⚠️ Funding +{funding:.3f}% anti-long -10")
                    elif side == "short" and funding < -0.03:
                        score -= 10
                        reasons.append(
                            f"⚠️ Funding {funding:.3f}% anti-short -10")
                    elif side == "short" and funding > 0.05:
                        score += 8
                        reasons.append(
                            f"Funding crowded long → short +8")
            except Exception:
                pass

            try:
                oi = data_manager.get_oi_delta() \
                     if hasattr(data_manager, "get_oi_delta") else None
                if oi is not None:
                    price_up = current_price > oi.get("ref_price", current_price)
                    oi_up    = oi.get("delta", 0) > 0
                    # OI divergence: price up but OI falling = distribution
                    if price_up and not oi_up and side == "short":
                        score += 8
                        reasons.append("OI divergence → short +8")
                    elif not price_up and oi_up and side == "long":
                        score += 8
                        reasons.append("OI divergence → long +8")
            except Exception:
                pass

            # ── 12. Nearest swings for SL/TP context ─────────────────────
            if side == "long":
                lows_b  = [s.price for s in list(self.swing_lows)[-8:]
                           if s.price < current_price]
                highs_a = [s.price for s in list(self.swing_highs)[-8:]
                           if s.price > current_price]
                if lows_b:  ctx.nearest_swing_low  = max(lows_b)
                if highs_a: ctx.nearest_swing_high = min(highs_a)
            else:
                highs_a = [s.price for s in list(self.swing_highs)[-8:]
                           if s.price > current_price]
                lows_b  = [s.price for s in list(self.swing_lows)[-8:]
                           if s.price < current_price]
                if highs_a: ctx.nearest_swing_high = min(highs_a)
                if lows_b:  ctx.nearest_swing_low  = max(lows_b)

            # ── Regime: OB / FVG / sweep multipliers ─────────────────────
            rs = self.regime_engine.state

            if ob_scored and rs.ob_score_multiplier != 1.0:
                adj   = config.SCORE_OB_PROXIMITY * 0.6 * (rs.ob_score_multiplier - 1.0)
                score = max(0.0, score + adj)
                if abs(adj) >= 1.0:
                    reasons.append(f"Regime OB adj {adj:+.1f} [{rs.regime}]")

            if fvg_scored and rs.fvg_score_multiplier != 1.0:
                adj   = config.SCORE_FVG * 0.6 * (rs.fvg_score_multiplier - 1.0)
                score = max(0.0, score + adj)
                if abs(adj) >= 1.0:
                    reasons.append(f"Regime FVG adj {adj:+.1f} [{rs.regime}]")

            if ctx.sweep_detected and rs.sweep_score_multiplier != 1.0:
                adj   = 15.0 * (rs.sweep_score_multiplier - 1.0)
                score = max(0.0, score + adj)
                if abs(adj) >= 1.0:
                    reasons.append(f"Regime sweep adj {adj:+.1f}")

            # ── Regime: Dealing range premium/discount ────────────────────
            dr = self._ndr.best_dr()
            if dr is not None:
                zone = dr.zone_pct(current_price)
                if side == "long":
                    if dr.is_discount(current_price):
                        dr_score = round(12.0 * (0.382 - zone) / 0.382, 1)
                        score   += dr_score
                        reasons.append(
                            f"Discount zone {zone*100:.0f}% +{dr_score:.1f}")
                    elif dr.is_premium(current_price):
                        score -= 18.0
                        reasons.append("⚠️ Premium zone long -18")
                    else:
                        reasons.append(f"Equilibrium zone {zone*100:.0f}%")
                else:
                    if dr.is_premium(current_price):
                        dr_score = round(
                            12.0 * (zone - 0.618) / (1.0 - 0.618), 1)
                        score   += dr_score
                        reasons.append(
                            f"Premium zone {zone*100:.0f}% +{dr_score:.1f}")
                    elif dr.is_discount(current_price):
                        score -= 18.0
                        reasons.append("⚠️ Discount zone short -18")
                    else:
                        reasons.append(f"Equilibrium zone {zone*100:.0f}%")

            score = min(max(score, 0.0), 100.0)
            return score, reasons, ctx

        except Exception as e:
            logger.error(f"❌ Confluence scoring error: {e}", exc_info=True)
            return 0.0, [], TriggerContext()

    # =========================================================================
    # CASCADE GATE CHECKS (v9)
    # =========================================================================

    def _cascade_l1_pass(self, side: str, current_price: float,
                          current_time: int) -> Tuple[bool, str]:
        """
        Level-1 gate — ALL must pass:
          1. HTF bias aligned
          2. Weekly DR not hard-opposed
          3. No active failed setup on this side
        """

        exec_bias, _, _ = self._resolve_directional_bias(current_time)
        if exec_bias == "NEUTRAL":
            return False, "No directional bias"
        if side == "long" and exec_bias == "BEARISH":
            return False, "Directional BEARISH blocks long"
        if side == "short" and exec_bias == "BULLISH":
            return False, "Directional BULLISH blocks short"

        # Daily DR hard-oppose (v9 fix — premium/discount gate at L1)
        if self._ndr.daily is not None:
            if side == "long"  and self._ndr.daily.is_premium(current_price):
                return False, "Daily DR premium blocks long"
            if side == "short" and self._ndr.daily.is_discount(current_price):
                return False, "Daily DR discount blocks short"

        # 2. Weekly DR hard-opposed
        if self._ndr.hard_opposed(current_price, side):
            return False, "Weekly DR hard-opposed"

        # 3. Failed setup gate
        if self._check_failed_setups(side, current_price, current_time):
            return False, "Active failed setup"

        return True, "L1_OK"

    def _cascade_l2_triggers(self, side: str, current_price: float,
                               ctx: TriggerContext,
                               current_time: int) -> Tuple[int, List[str]]:
        """
        Level-2 triggers — need CASCADE_L2_MIN_TRIGGERS (2) of:
          A. Sweep confirmed with displacement
          B. OB or FVG touched
          C. MSS (BOS/CHoCH) in entry direction within last 30 min
        Returns (count_met, labels_met).
        """
        met: List[str] = []

        # A. Sweep + displacement
        if ctx.sweep_detected:
            sw_pool = next(
                (lp for lp in reversed(list(self.liquidity_pools))
                 if lp.swept and lp.displacement_confirmed
                 and (current_time - lp.sweep_timestamp)
                 <= getattr(config, "SWEEP_MAX_AGE_MINUTES", 120) * 60_000
                 and ((side == "long"  and lp.pool_type == "EQL") or
                      (side == "short" and lp.pool_type == "EQH"))),
                None)
            if sw_pool:
                met.append("SWEEP_DISP")

        # B. OB or FVG touch
        if ctx.trigger_ob is not None and \
                (ctx.trigger_ob.contains_price(current_price) or
                 ctx.trigger_ob.in_optimal_zone(current_price)):
            met.append("OB_TOUCH")
        elif ctx.trigger_fvg is not None and \
                ctx.trigger_fvg.is_price_in_gap(current_price):
            met.append("FVG_TOUCH")

        # C. Recent MSS
        ms_window = 30 * 60_000
        target    = "bullish" if side == "long" else "bearish"
        recent_ms = next(
            (ms for ms in reversed(list(self.market_structures))
             if (current_time - ms.timestamp) <= ms_window
             and ms.direction == target
             and ms.structure_type in ("BOS", "CHoCH")),
            None)
        if recent_ms:
            met.append(f"MSS_{recent_ms.structure_type}")

        return len(met), met

    def _cascade_l3_confirmations(self, side: str, current_price: float,
                                   ctx: TriggerContext,
                                   score: float,
                                   score_bonus: float) -> Tuple[int, List[str]]:
        """
        Level-3 confirmations — need CASCADE_L3_MIN_CONFIRMS (1) of:
          A. CVD aligned
          B. Absorption event at level
          C. Killzone active
          D. TCE pullback bonus active
        """
        met: List[str] = []

        # A. CVD
        if self.volume_analyzer and \
                len(self.volume_analyzer.cvd_history) >= 10:
            cvd_sig = self.volume_analyzer.get_cvd_signal(lookback=100)
            signal  = cvd_sig.get("signal", "NEUTRAL")
            if side == "long"  and ("BULL" in signal):
                met.append("CVD_BULL")
            elif side == "short" and ("BEAR" in signal):
                met.append("CVD_BEAR")

        # B. Absorption
        if self.liquidity_model:
            ab = self.liquidity_model.absorption_confluence(
                current_price, side)
            if ab > 0:
                met.append("ABSORPTION")

        # C. Killzone
        if self.in_killzone:
            met.append("KILLZONE")

        # D. TCE bonus
        if score_bonus > 0:
            met.append("TCE_BONUS")

        return len(met), met

    def _log_gate_reject(self, level: str, side: str,
                         detail: str, current_time: int) -> None:
        """Debounced gate-rejection logging to avoid repeated spam."""
        throttle_ms = 20_000
        key = f"{level}:{side}:{detail}"
        if (key == self._last_gate_reject_key
                and (current_time - self._last_gate_reject_ms) < throttle_ms):
            return
        self._last_gate_reject_key = key
        self._last_gate_reject_ms = current_time
        logger.info(f"❌ {level} [{side}]: {detail}")

    # =========================================================================
    # ENTRY EVALUATION — FULL CASCADE
    # =========================================================================

    def _evaluate_entry(self, data_manager, order_manager,
                         risk_manager, current_time: int):
        try:
            can_trade, reason = risk_manager.can_trade()
            if not can_trade:
                logger.info(f"🚫 Trading blocked: {reason}")
                return

            current_price = data_manager.get_last_price()
            if not current_price:
                return

            # ── Advance TCE ───────────────────────────────────────────────
            candles_5m         = data_manager.get_candles("5m") \
                                 if hasattr(data_manager, "get_candles") else None
            tce_state          = self._tce_update(
                current_price, current_time, candles_5m=candles_5m)
            score_bonus, gate_desc = self._tce_get_entry_modifier()

            # ── TCE hard gate with expansion bypass ───────────────────────
            if score_bonus <= -999:
                rs = self.regime_engine.state
                if rs.bypass_tce_on_sweep and self._active_tce is not None:
                    tce = self._active_tce
                    if tce.tce_state in (
                            "AWAITING_PULLBACK", "AWAITING_CONFIRMATION"):
                        sw_ms        = 30 * 60_000
                        q_sweep      = next(
                            (lp for lp in reversed(list(self.liquidity_pools))
                             if lp.swept and lp.displacement_confirmed
                             and (current_time - lp.sweep_timestamp) < sw_ms
                             and (
                                 (tce.bias_direction == "BULLISH"
                                  and lp.pool_type == "EQL") or
                                 (tce.bias_direction == "BEARISH"
                                  and lp.pool_type == "EQH"))),
                            None)
                        if q_sweep is not None:
                            score_bonus = 0.0
                            gate_desc   = "EXPANSION_SWEEP_BYPASS"
                            logger.warning(
                                f"⚡ TCE bypass — VOLATILE_EXPANSION + "
                                f"{q_sweep.pool_type} @ {q_sweep.price:.2f}")
                            send_telegram_message(
                                f"⚡ *TCE bypass — Expansion*\n"
                                f"Sweep @ {q_sweep.price:.0f}")

                if score_bonus <= -999:
                    tce         = self._active_tce
                    elapsed_min = (current_time -
                                   tce.bias_change_time) / 60_000
                    logger.info(
                        f"🔒 TCE gate [{gate_desc}] "
                        f"elapsed={elapsed_min:.1f}m "
                        f"prob={tce.pullback_probability:.0%} "
                        f"regime={self.regime_engine.state.regime}")
                    return

            # ── Score both sides ──────────────────────────────────────────
            long_score,  long_reasons,  long_ctx  = \
                self._calculate_confluence_score(
                    "long",  current_price, data_manager, current_time)
            short_score, short_reasons, short_ctx = \
                self._calculate_confluence_score(
                    "short", current_price, data_manager, current_time)

            # ── Apply TCE bonus ───────────────────────────────────────────
            if score_bonus > 0 and self._active_tce:
                if self._active_tce.bias_direction == "BULLISH":
                    long_score  += score_bonus
                    long_reasons.append(
                        f"TCE Pullback +{score_bonus:.0f} "
                        f"prob={self._active_tce.pullback_probability:.0%}")
                else:
                    short_score += score_bonus
                    short_reasons.append(
                        f"TCE Pullback +{score_bonus:.0f} "
                        f"prob={self._active_tce.pullback_probability:.0%}")

            # ── Adaptive threshold ────────────────────────────────────────
            rs = self.regime_engine.state
            if self.current_session == "WEEKEND":
                base_threshold = config.ENTRY_THRESHOLD_WEEKEND
            elif self.in_killzone:
                base_threshold = config.ENTRY_THRESHOLD_KILLZONE
            else:
                base_threshold = config.ENTRY_THRESHOLD_REGULAR
            threshold  = (base_threshold * rs.entry_threshold_modifier
                          + self._get_adaptive_threshold_add())

            # Neutral HTF mode is tradable, but requires tighter execution.
            neutral_trade_mode = (self.htf_bias == "NEUTRAL")
            if neutral_trade_mode:
                threshold += 5.0

            # ── Near-miss reporting ───────────────────────────────────────
            if (long_score >= 75 or short_score >= 75) and \
                    (current_time - self.last_potential_trade_report) >= 600_000:
                self._report_potential_trades(
                    long_score, short_score, long_reasons, short_reasons)
                self.last_potential_trade_report = current_time

            # ── Evaluate each qualifying side through cascade ─────────────
            for side, s_score, s_reasons, s_ctx in [
                ("long",  long_score,  long_reasons,  long_ctx),
                ("short", short_score, short_reasons, short_ctx),
            ]:
                if s_score < threshold:
                    continue
                # opposite side must not outscore
                other = short_score if side == "long" else long_score
                if other >= s_score:
                    continue

                # L1 gate
                l1_ok, l1_reason = self._cascade_l1_pass(
                    side, current_price, current_time)
                if not l1_ok:
                    self._log_gate_reject("L1", side, l1_reason, current_time)
                    continue

                # L2 gate
                l2_count, l2_met = self._cascade_l2_triggers(
                    side, current_price, s_ctx, current_time)
                if l2_count < CASCADE_L2_MIN_TRIGGERS:
                    self._log_gate_reject(
                        "L2", side,
                        f"{l2_count}/{CASCADE_L2_MIN_TRIGGERS} triggers {l2_met}",
                        current_time,
                    )
                    continue

                # L3 gate
                l3_count, l3_met = self._cascade_l3_confirmations(
                    side, current_price, s_ctx, s_score, score_bonus)
                if l3_count < CASCADE_L3_MIN_CONFIRMS:
                    self._log_gate_reject(
                        "L3", side,
                        f"{l3_count}/{CASCADE_L3_MIN_CONFIRMS} confirms {l3_met}",
                        current_time,
                    )
                    continue

                logger.info(
                    f"✅ CASCADE PASS [{side}] "
                    f"score={s_score:.0f} "
                    f"L2={l2_met} L3={l3_met} "
                    f"threshold={threshold:.0f}")
                self._execute_entry(
                    side, current_price, order_manager, risk_manager,
                    s_score, s_reasons, s_ctx, current_time)
                return   # one side only per tick

            # ── Next-setup log ────────────────────────────────────────────
            if (current_time - self.last_potential_trade_report) >= 30_000:
                best_side  = "LONG"  if long_score > short_score else "SHORT"
                best_score = max(long_score, short_score)
                best_rsns  = (long_reasons if long_score > short_score
                              else short_reasons)
                logger.info(
                    f"⏳ NEXT SETUP: {best_side} "
                    f"{best_score:.0f}/100 need={threshold:.0f} "
                    f"[{gate_desc}] regime={rs.regime} "
                    f"preserve={self._capital_preserve_mode}")
                for r in best_rsns[:5]:
                    logger.info(f"   {r}")
                self.last_potential_trade_report = current_time

        except Exception as e:
            logger.error(f"❌ Entry evaluation error: {e}", exc_info=True)

    # =========================================================================
    # COHERENT LEVEL CALCULATION (strict, structure-derived)
    # =========================================================================

    def _find_neutral_tp(self, side: str, entry_price: float, current_time: int) -> Optional[float]:
        """Nearest opposing OB / liquidity target for neutral HTF execution mode."""
        candidates: List[float] = []

        if side == "long":
            candidates.extend([
                lp.price for lp in self.liquidity_pools
                if lp.pool_type == "EQH" and not lp.swept and lp.price > entry_price
            ])
            candidates.extend([
                ob.low for ob in self.order_blocks_bear
                if ob.is_active(current_time) and ob.low > entry_price
            ])
            candidates.extend([
                sw.price for sw in list(self.swing_highs)[-12:]
                if sw.price > entry_price
            ])
            return min(candidates) if candidates else None

        candidates.extend([
            lp.price for lp in self.liquidity_pools
            if lp.pool_type == "EQL" and not lp.swept and lp.price < entry_price
        ])
        candidates.extend([
            ob.high for ob in self.order_blocks_bull
            if ob.is_active(current_time) and ob.high < entry_price
        ])
        candidates.extend([
            sw.price for sw in list(self.swing_lows)[-12:]
            if sw.price < entry_price
        ])
        return max(candidates) if candidates else None

    def _calculate_coherent_levels(
            self, side: str, current_price: float,
            ctx: TriggerContext, current_time: int,
    ) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float], Optional[float]]:
        buffer       = config.SL_BUFFER_TICKS * config.TICK_SIZE
        entry_offset = config.LIMIT_ORDER_OFFSET_TICKS * config.TICK_SIZE
        tick         = config.TICK_SIZE

        try:
            neutral_htf_mode = (self.htf_bias == "NEUTRAL")

            if side == "long":
                entry_price = current_price - entry_offset
                sl_candidates: List[float] = []

                if ctx.trigger_ob is not None:
                    cand = ctx.trigger_ob.low - buffer
                    if cand < entry_price:
                        sl_candidates.append(cand)
                if ctx.nearest_swing_low is not None:
                    cand = ctx.nearest_swing_low - buffer
                    if cand < entry_price:
                        sl_candidates.append(cand)
                if ctx.trigger_fvg is not None:
                    cand = ctx.trigger_fvg.bottom - buffer
                    if cand < entry_price:
                        sl_candidates.append(cand)
                if not sl_candidates:
                    return None, None, None, None, None
                sl_price = max(sl_candidates)

                sl_dist = (entry_price - sl_price) / entry_price
                max_sl_pct = min(config.MAX_SL_DISTANCE_PCT, NEUTRAL_SL_MAX_PCT) if neutral_htf_mode else config.MAX_SL_DISTANCE_PCT
                if sl_dist < config.MIN_SL_DISTANCE_PCT:
                    sl_price = entry_price * (1 - config.MIN_SL_DISTANCE_PCT)
                elif sl_dist > max_sl_pct:
                    sl_price = entry_price * (1 - max_sl_pct)

                risk    = entry_price - sl_price
                tp1     = None
                tp2     = None
                tp_final = None

                if neutral_htf_mode:
                    tp_near = self._find_neutral_tp(side, entry_price, current_time)
                    if tp_near is None or tp_near <= entry_price:
                        return None, None, None, None, None
                    tp1 = tp2 = tp_final = tp_near
                else:
                    if ctx.nearest_swing_high is not None:
                        rr = (ctx.nearest_swing_high - entry_price) / risk if risk > 0 else 0
                        if rr >= config.MIN_RISK_REWARD_RATIO:
                            tp1 = ctx.nearest_swing_high

                    dr = self._ndr.intraday or self._ndr.daily
                    if dr is not None:
                        tp2_cand = dr.high
                        if tp2_cand > entry_price:
                            tp2 = tp2_cand
                    # Require explicit DR/HTF structural target for TP2.
                    if tp2 is None or tp2 <= entry_price:
                        return None, None, None, None, None
                    tp_final = tp2

                    if tp_final is not None:
                        rr_final = (tp_final - entry_price) / risk if risk > 0 else 0
                        if rr_final > config.MAX_RR_RATIO:
                            tp_final = (entry_price + risk * config.MAX_RR_RATIO)

            else:  # short
                entry_price = current_price + entry_offset
                sl_candidates: List[float] = []

                if ctx.trigger_ob is not None:
                    cand = ctx.trigger_ob.high + buffer
                    if cand > entry_price:
                        sl_candidates.append(cand)
                if ctx.nearest_swing_high is not None:
                    cand = ctx.nearest_swing_high + buffer
                    if cand > entry_price:
                        sl_candidates.append(cand)
                if ctx.trigger_fvg is not None:
                    cand = ctx.trigger_fvg.top + buffer
                    if cand > entry_price:
                        sl_candidates.append(cand)
                if not sl_candidates:
                    return None, None, None, None, None
                sl_price = min(sl_candidates)

                sl_dist = (sl_price - entry_price) / entry_price
                max_sl_pct = min(config.MAX_SL_DISTANCE_PCT, NEUTRAL_SL_MAX_PCT) if neutral_htf_mode else config.MAX_SL_DISTANCE_PCT
                if sl_dist < config.MIN_SL_DISTANCE_PCT:
                    sl_price = entry_price * (1 + config.MIN_SL_DISTANCE_PCT)
                elif sl_dist > max_sl_pct:
                    sl_price = entry_price * (1 + max_sl_pct)

                risk    = sl_price - entry_price
                tp1     = None
                tp2     = None
                tp_final = None

                if neutral_htf_mode:
                    tp_near = self._find_neutral_tp(side, entry_price, current_time)
                    if tp_near is None or tp_near >= entry_price:
                        return None, None, None, None, None
                    tp1 = tp2 = tp_final = tp_near
                else:
                    if ctx.nearest_swing_low is not None:
                        rr = (entry_price - ctx.nearest_swing_low) / risk if risk > 0 else 0
                        if rr >= config.MIN_RISK_REWARD_RATIO:
                            tp1 = ctx.nearest_swing_low

                    dr = self._ndr.intraday or self._ndr.daily
                    if dr is not None:
                        tp2_cand = dr.low
                        if tp2_cand < entry_price:
                            tp2 = tp2_cand
                    # Require explicit DR/HTF structural target for TP2.
                    if tp2 is None or tp2 >= entry_price:
                        return None, None, None, None, None
                    tp_final = tp2

                    if tp_final is not None:
                        rr_final = (entry_price - tp_final) / risk if risk > 0 else 0
                        if rr_final > config.MAX_RR_RATIO:
                            tp_final = (entry_price - risk * config.MAX_RR_RATIO)

            entry_price = round(entry_price / tick) * tick
            sl_price    = round(sl_price    / tick) * tick
            tp_final    = round(tp_final    / tick) * tick if tp_final else None
            tp1         = round(tp1         / tick) * tick if tp1      else None
            tp2         = round(tp2         / tick) * tick if tp2      else None

            if side == "long" and not (sl_price < entry_price < tp_final):
                return None, None, None, None, None
            if side == "short" and not (tp_final < entry_price < sl_price):
                return None, None, None, None, None

            return entry_price, sl_price, tp_final, tp1, tp2

        except Exception as e:
            logger.error(f"❌ Coherent levels error: {e}", exc_info=True)
            return None, None, None, None, None

    # =========================================================================
    # EXECUTE ENTRY
    # =========================================================================

    def _execute_entry(self, side: str, current_price: float,
                        order_manager, risk_manager, score: float,
                        reasons: List[str], ctx: TriggerContext,
                        current_time: int):
        try:
            logger.info("=" * 80)
            logger.info(f"🎯 HIGH CONFLUENCE [{side.upper()}] Score={score:.0f}")
            for r in reasons:
                logger.info(f"   {r}")
            logger.info("=" * 80)

            levels = self._calculate_coherent_levels(side, current_price, ctx, current_time)
            entry_price, sl_price, tp_price, tp1, tp2 = levels

            if entry_price is None:
                logger.warning("⚠️ Coherent levels failed — no entry")
                return

            # Sanity checks
            if side == "long":
                if sl_price >= entry_price:
                    logger.error("⚠️ Invalid LONG levels: SL >= Entry")
                    return
                if tp_price is not None and tp_price <= entry_price:
                    logger.error("⚠️ TP ≤ Entry for LONG — skipping")
                    return
            else:
                if sl_price <= entry_price:
                    logger.error("⚠️ Invalid SHORT levels: SL <= Entry")
                    return
                if tp_price is not None and tp_price >= entry_price:
                    logger.error("⚠️ TP ≥ Entry for SHORT — skipping")
                    return

            risk   = abs(entry_price - sl_price)
            reward = abs(tp_price - entry_price) if tp_price else risk * 2
            rr     = reward / risk if risk > 0 else 0

            if rr < config.MIN_RISK_REWARD_RATIO:
                logger.warning(f"⚠️ RR={rr:.2f} < min {config.MIN_RISK_REWARD_RATIO}")
                return

            # Regime-aware + DR-aligned + adaptive size
            rs      = self.regime_engine.state
            _, dr_m = self._ndr.alignment_score(current_price, side)
            adapt_m = self._get_adaptive_size_mult()

            position_size = risk_manager.calculate_position_size(
                entry_price, sl_price, side.upper())
            if not position_size or position_size <= 0:
                logger.warning("⚠️ Invalid position size")
                return

            neutral_size_mult = 0.60 if self.htf_bias == "NEUTRAL" else 1.0
            position_size = round(
                position_size * rs.size_multiplier * dr_m * adapt_m * neutral_size_mult, 8)
            if position_size <= 0:
                logger.warning("⚠️ Position size zeroed by multipliers")
                return

            min_qty = getattr(config, "MIN_ORDER_QTY", 0.001)
            if position_size < min_qty:
                logger.warning(
                    f"⚠️ Position size {position_size:.6f} < min_qty {min_qty} — skipped")
                return

            logger.info(
                f"📐 Entry={entry_price:.2f} SL={sl_price:.2f} "
                f"TP={tp_price:.2f} RR={rr:.2f}:1 Qty={position_size} "
                f"regime_m={rs.size_multiplier:.2f} "
                f"dr_m={dr_m:.2f} adapt_m={adapt_m:.2f}")

            exit_side = "SHORT" if side.upper() == "LONG" else "LONG"

            # Place limit entry
            GlobalRateLimiter.wait()
            entry_resp   = order_manager.place_limit_order(
                side=side.upper(), quantity=position_size, price=entry_price)
            entry_order_id = entry_resp.get("order_id") if entry_resp else None
            if not entry_order_id:
                logger.error("❌ Limit order placement failed")
                self._placement_locked_until = (
                    current_time + PLACEMENT_LOCK_SECONDS * 1000)
                return
            logger.info(f"✅ Limit placed {entry_order_id}")
            time.sleep(0.3)

            # Place SL
            GlobalRateLimiter.wait()
            sl_resp   = order_manager.place_stop_loss(
                side=exit_side, quantity=position_size,
                trigger_price=sl_price)
            sl_order_id = sl_resp.get("order_id") if sl_resp else None
            if not sl_order_id:
                logger.error("❌ SL placement failed — cancelling limit")
                order_manager.cancel_order(entry_order_id)
                self._placement_locked_until = (
                    current_time + PLACEMENT_LOCK_SECONDS * 1000)
                return
            logger.info(f"✅ SL placed {sl_order_id} @ {sl_price:.2f}")
            time.sleep(0.3)

            # Place single structure-based TP (CoinSwitch-compatible)
            GlobalRateLimiter.wait()
            tp_resp = order_manager.place_take_profit(
                side=exit_side, quantity=position_size,
                trigger_price=tp_price)
            tp_order_id = tp_resp.get("order_id") if tp_resp else None
            if not tp_order_id:
                logger.error("❌ TP placement failed — cancelling entry and SL to avoid naked exposure")
                order_manager.cancel_order(entry_order_id)
                order_manager.cancel_order(sl_order_id)
                self._placement_locked_until = (
                    current_time + PLACEMENT_LOCK_SECONDS * 1000)
                return
            logger.info(
                f"✅ TP placed {tp_order_id} "
                f"@ {tp_price:.2f} qty={position_size}")
            time.sleep(0.3)

            # Commit state
            self.entry_order_id        = entry_order_id
            self.sl_order_id           = sl_order_id
            self.tp_order_id           = tp_order_id
            self._pending_ctx          = ctx
            self.initial_entry_price   = entry_price
            self.initial_sl_price      = sl_price
            self.initial_tp_price      = tp_price
            self.current_sl_price      = sl_price
            self.current_tp_price      = tp_price
            self.entry_pending_start   = current_time
            self.breakeven_moved       = False
            self.profit_locked_pct     = 0.0

            self.active_position = {
                "side": side, "score": score, "reasons": reasons,
                "entry_price": entry_price, "sl": sl_price,
                "tp": tp_price, "quantity": position_size,
                "ctx": ctx,
            }
            self.state = "ENTRY_PENDING"
            self._sm_transition("ENTRY_PENDING", "entry_orders_placed")
            self.total_entries += 1
            self._tce_mark_entry_taken()

            # Notify risk_manager so cooldown timer starts at entry, not close
            if hasattr(risk_manager, "notify_entry_placed"):
                risk_manager.notify_entry_placed()


            # DR alignment note in Telegram
            aligned_cnt, _ = self._ndr.alignment_score(current_price, side)
            dr_tag = f"DR_ALIGNED_{aligned_cnt}/3"

            send_telegram_message(
                f"🎯 *ENTRY PENDING [{side.upper()}]*\n"
                f"Score: {score:.0f}/100\n"
                f"Entry: {entry_price:.2f} | SL: {sl_price:.2f} | "
                f"TP: {tp_price:.2f}\n"
                f"RR: {rr:.2f}:1 | Qty: {position_size}\n"
                f"Mode: Single TP + Dynamic SL trail\n"
                f"Regime: {rs.regime} | {dr_tag}\n"
                f"Preserve: {self._capital_preserve_mode}\n"
                + "\n".join(f"• {r}" for r in reasons[:5]))

        except Exception as e:
            logger.error(f"❌ Execute entry error: {e}", exc_info=True)

    # =========================================================================
    # HANDLE ENTRY PENDING (unchanged logic, tranche-aware)
    # =========================================================================

    def _handle_entry_pending(self, order_manager, risk_manager,
                               current_time: int):
        try:
            if self.entry_pending_start is None:
                self.state = "READY"
                self._sm_transition("SCANNING", "pending_without_start")
                return

            timeout_ms = getattr(config, "ENTRY_PENDING_TIMEOUT_SECONDS",
                                  120) * 1000
            if (current_time - self.entry_pending_start) > timeout_ms:
                logger.warning("⏱️ Entry pending timeout — cancelling")
                if self.entry_order_id:
                    order_manager.cancel_order(self.entry_order_id)
                if self.sl_order_id:
                    order_manager.cancel_order(self.sl_order_id)
                if self.tp_order_id:
                    order_manager.cancel_order(self.tp_order_id)
                self._reset_position_state()
                send_telegram_message("⏱️ Entry pending timeout — cancelled")
                return

            status = order_manager.get_order_status(self.entry_order_id) \
                     if self.entry_order_id else None
            if status and status.get("status") == "FILLED":
                if not self.active_position:
                    logger.error("❌ Fill confirmed but active_position is None — resetting")
                    self._reset_position_state()
                    return
                logger.info(f"Entry FILLED @ {self.initial_entry_price:.2f}")
                self.state                 = "POSITION_ACTIVE"
                self._sm_transition("POSITION_ACTIVE", "entry_filled")
                self.highest_price_reached = self.initial_entry_price
                self.lowest_price_reached  = self.initial_entry_price
                send_telegram_message(
                    f"✅ POSITION ACTIVE {self.active_position['side'].upper()}\n"
                    f"Entry {self.initial_entry_price:.2f}\n"
                    f"SL {self.current_sl_price:.2f}")


        except Exception as e:
            logger.error(f"❌ Handle entry pending error: {e}", exc_info=True)

    # =========================================================================
    # MANAGE ACTIVE POSITION
    # =========================================================================

    def _manage_active_position(self, data_manager, order_manager,
                                  current_time: int):
        try:
            current_price = data_manager.get_last_price()
            if not current_price or not self.active_position:
                return

            side = self.active_position["side"]

            if side == "long":
                if self.highest_price_reached is None or \
                        current_price > self.highest_price_reached:
                    self.highest_price_reached = current_price
            else:
                if self.lowest_price_reached is None or \
                        current_price < self.lowest_price_reached:
                    self.lowest_price_reached = current_price

            # ── SL health / dynamic stop ──────────────────────────────────
            if (current_time - self.last_sl_update) > \
                    getattr(config, "SL_UPDATE_INTERVAL_SECONDS", 30) * 1000:
                self._update_dynamic_stop_loss(
                    current_price, order_manager, current_time)
                self.last_sl_update = current_time

            # ── SL hit detection ──────────────────────────────────────────
            sl_hit = False
            if side == "long"  and self.current_sl_price and \
                    current_price <= self.current_sl_price:
                sl_hit = True
            elif side == "short" and self.current_sl_price and \
                    current_price >= self.current_sl_price:
                sl_hit = True

            if sl_hit:
                logger.info(
                    f"🛑 SL HIT [{side}] @ {current_price:.2f} "
                    f"SL={self.current_sl_price:.2f}")
                pnl = self._calculate_position_pnl(current_price)
                self._close_position(
                    order_manager, self._risk_manager, current_price, "SL_HIT", pnl, current_time)
                return

            # ── Full TP hit (single TP mode) ──────────────────────────────
            tp_hit = False
            if self.current_tp_price:
                if side == "long"  and current_price >= self.current_tp_price:
                    tp_hit = True
                elif side == "short" and current_price <= self.current_tp_price:
                    tp_hit = True

            if tp_hit:
                logger.info(
                    f"🎉 TP HIT [{side}] @ {current_price:.2f} "
                    f"TP={self.current_tp_price:.2f}")
                pnl = self._calculate_position_pnl(current_price)
                self._close_position(
                    order_manager, self._risk_manager, current_price, "TP_HIT", pnl, current_time)

        except Exception as e:
            logger.error(f"❌ Manage position error: {e}", exc_info=True)

    # =========================================================================
    # DYNAMIC STOP LOSS
    # =========================================================================

    def _get_structure_trailing_stop(self, side: str) -> Optional[float]:
        """
        Derive trailing SL from recent market structure (swing points).
        Long: highest recent confirmed lows under market.
        Short: lowest recent confirmed highs above market.
        """
        tick = config.TICK_SIZE
        pad = max(config.SL_BUFFER_TICKS, 1) * tick

        if side == "long":
            lows = [sw.price for sw in list(self.swing_lows)[-12:] if sw.confirmed]
            if not lows:
                return None
            return max(lows) - pad

        highs = [sw.price for sw in list(self.swing_highs)[-12:] if sw.confirmed]
        if not highs:
            return None
        return min(highs) + pad

    def _emergency_flatten_position(self, order_manager, reason: str) -> None:
        """Fail-safe: flatten immediately if SL replacement fails."""
        if not self.active_position:
            return
        side = self.active_position.get("side")
        qty = self.active_position.get("quantity", 0)
        if not side or qty <= 0:
            return
        exit_side = "SHORT" if side == "long" else "LONG"
        try:
            GlobalRateLimiter.wait()
            order_manager.place_market_order(side=exit_side, quantity=qty)
            logger.critical(f"🚨 Emergency flatten executed [{reason}] qty={qty}")
            send_telegram_message(
                f"🚨 *EMERGENCY FLATTEN*\n"
                f"Reason: {reason}\n"
                f"Side: {side.upper()} | Qty: {qty}")
            self._reset_position_state()
        except Exception as ex:
            logger.critical(f"🚨 Emergency flatten FAILED: {ex}", exc_info=True)

    def _move_sl(self, new_sl: float, order_manager, reason: str):
        try:
            if not self.active_position:
                return
            side      = self.active_position["side"]
            exit_side = "SHORT" if side == "long" else "LONG"
            qty_rem   = self.active_position.get("quantity", 0)
            if qty_rem <= 0:
                return

            prior_sl_id = self.sl_order_id
            if self.sl_order_id:
                order_manager.cancel_order(self.sl_order_id)
            GlobalRateLimiter.wait()
            new_sl_r = round(new_sl / config.TICK_SIZE) * config.TICK_SIZE
            sl_resp  = order_manager.place_stop_loss(
                side=exit_side, quantity=qty_rem,
                trigger_price=new_sl_r)
            new_sl_id = sl_resp.get("order_id") if sl_resp else None
            if new_sl_id:
                self.sl_order_id       = new_sl_id
                self.current_sl_price  = new_sl_r
                logger.info(
                    f"🔄 SL moved [{reason}] → {new_sl_r:.2f} "
                    f"id={new_sl_id}")
                return

            logger.error(
                f"❌ SL replace failed after cancel [{reason}] old_id={prior_sl_id} "
                f"new_sl={new_sl_r:.2f} — triggering emergency flatten")
            self._emergency_flatten_position(order_manager, f"SL_REPLACE_FAIL:{reason}")
        except Exception as e:
            logger.error(f"❌ Move SL error: {e}", exc_info=True)
            self._emergency_flatten_position(order_manager, f"SL_REPLACE_EXCEPTION:{reason}")

    def _update_dynamic_stop_loss(self, current_price: float,
                                   order_manager, current_time: int):
        """
        Breakeven, structure-following SL, and ATR profit protection.
        Single-TP mode: SL is actively re-anchored to structure progression.
        """
        if not self.active_position or not self.initial_entry_price:
            return
        side         = self.active_position["side"]
        entry        = self.initial_entry_price
        current_sl   = self.current_sl_price
        if current_sl is None:
            return

        rs      = self.regime_engine.state
        atr_sl  = rs.atr_sl_multiplier * getattr(config, "ATR_VALUE", 50.0)
        tick    = config.TICK_SIZE

        if side == "long":
            # Breakeven at 1× risk
            risk = entry - self.initial_sl_price if self.initial_sl_price else 0
            if risk > 0 and not self.breakeven_moved:
                if current_price >= entry + risk:
                    new_sl = entry + tick
                    if new_sl > current_sl:
                        self._move_sl(new_sl, order_manager, "BREAKEVEN")
                        self.breakeven_moved = True

            # Structure-led trailing + ATR floor
            struct_sl = self._get_structure_trailing_stop("long")
            atr_trail_sl = (self.highest_price_reached or current_price) - atr_sl
            candidates = [x for x in [struct_sl, atr_trail_sl] if x is not None]
            if candidates:
                trail_sl = max(candidates)
                trail_sl = round(trail_sl / tick) * tick
                if trail_sl > current_sl and trail_sl < current_price:
                    self._move_sl(trail_sl, order_manager, "STRUCTURE_ATR_TRAIL")

            if self.highest_price_reached:
                profit_pct = ((self.highest_price_reached - entry)
                              / entry * 100)
                if profit_pct >= 1.5 and self.profit_locked_pct < 0.5:
                    lock_sl = self.highest_price_reached - atr_sl
                    lock_sl = round(lock_sl / tick) * tick
                    if lock_sl > current_sl:
                        self._move_sl(lock_sl, order_manager, "PROFIT_LOCK")
                        self.profit_locked_pct = 0.5
                elif profit_pct >= 2.5 and self.profit_locked_pct < 1.0:
                    lock_sl = self.highest_price_reached - atr_sl * 0.75
                    lock_sl = round(lock_sl / tick) * tick
                    if lock_sl > current_sl:
                        self._move_sl(lock_sl, order_manager, "PROFIT_LOCK_2")
                        self.profit_locked_pct = 1.0
        else:  # short
            risk = (self.initial_sl_price - entry
                    if self.initial_sl_price else 0)
            if risk > 0 and not self.breakeven_moved:
                if current_price <= entry - risk:
                    new_sl = entry - tick
                    if new_sl < current_sl:
                        self._move_sl(new_sl, order_manager, "BREAKEVEN")
                        self.breakeven_moved = True

            struct_sl = self._get_structure_trailing_stop("short")
            atr_trail_sl = (self.lowest_price_reached or current_price) + atr_sl
            candidates = [x for x in [struct_sl, atr_trail_sl] if x is not None]
            if candidates:
                trail_sl = min(candidates)
                trail_sl = round(trail_sl / tick) * tick
                if trail_sl < current_sl and trail_sl > current_price:
                    self._move_sl(trail_sl, order_manager, "STRUCTURE_ATR_TRAIL")

            if self.lowest_price_reached:
                profit_pct = ((entry - self.lowest_price_reached)
                              / entry * 100)
                if profit_pct >= 1.5 and self.profit_locked_pct < 0.5:
                    lock_sl = self.lowest_price_reached + atr_sl
                    lock_sl = round(lock_sl / tick) * tick
                    if lock_sl < current_sl:
                        self._move_sl(lock_sl, order_manager, "PROFIT_LOCK")
                        self.profit_locked_pct = 0.5
                elif profit_pct >= 2.5 and self.profit_locked_pct < 1.0:
                    lock_sl = self.lowest_price_reached + atr_sl * 0.75
                    lock_sl = round(lock_sl / tick) * tick
                    if lock_sl < current_sl:
                        self._move_sl(lock_sl, order_manager, "PROFIT_LOCK_2")
                        self.profit_locked_pct = 1.0

    # =========================================================================
    # CLOSE POSITION
    # =========================================================================

    def _calculate_position_pnl(self, close_price: float) -> float:
        if not self.active_position or not self.initial_entry_price:
            return 0.0
        side = self.active_position["side"]
        qty  = self.active_position.get("quantity", 0)
        if side == "long":
            return (close_price - self.initial_entry_price) * qty
        else:
            return (self.initial_entry_price - close_price) * qty

    def _close_position(self, order_manager, risk_manager, close_price: float,
                         reason: str, pnl: float, current_time: int):
        try:
            self._sm_transition("EXITING", reason)
            side = self.active_position["side"] if self.active_position else "?"
            logger.info(
                f"📤 CLOSE [{side.upper()}] reason={reason} "
                f"price={close_price:.2f} pnl={pnl:.4f}")

            # Cancel exit orders atomically (TP first then SL)
            order_manager.cancel_all_exit_orders(self.sl_order_id, self.tp_order_id)

            # Market close remainder
            if self.active_position:
                qty_rem = self.active_position.get("quantity", 0)
                if qty_rem > 0:
                    exit_side = ("SHORT" if side == "long" else "LONG")
                    GlobalRateLimiter.wait()
                    order_manager.place_market_order(
                        side=exit_side, quantity=qty_rem)

            # ── Stats ─────────────────────────────────────────────────────
            self.total_exits  += 1
            self.total_pnl    += pnl
            self.daily_pnl    += pnl
            won = pnl > 0

            # Regime performance tracking
            regime_now = self.regime_engine.state.regime
            self._perf_tracker.record(
                regime=regime_now, side=side, pnl=pnl, won=won)

            if won:
                self.winning_trades     += 1
                self.consecutive_losses  = 0
            else:
                self.consecutive_losses += 1
                if self._pending_ctx:
                    self._record_failed_setup(
                        self._pending_ctx, side,
                        self.initial_entry_price or close_price,
                        self.initial_sl_price   or close_price)

            self._update_adaptation_state()

            total  = self.total_exits
            wr     = self.winning_trades / total * 100 if total > 0 else 0
            tr_pnl = 0.0

            send_telegram_message(
                f"📤 *POSITION CLOSED [{side.upper()}]*\n"
                f"Reason: {reason} | Price: {close_price:.2f}\n"
                f"PnL: {pnl:+.4f} (TP1+2: {tr_pnl:.4f})\n"
                f"Total PnL: {self.total_pnl:.4f} | "
                f"Daily: {self.daily_pnl:.4f}\n"
                f"W/L: {self.winning_trades}/{self.consecutive_losses} "
                f"WR={wr:.1f}% | Regime: {regime_now}")

            # Sync closed trade to risk_manager for daily limits tracking
            if hasattr(risk_manager, "record_trade") and self.active_position:
                risk_manager.record_trade(
                    side=side.upper(),
                    entry_price=self.initial_entry_price or close_price,
                    exit_price=close_price,
                    quantity=self.active_position.get("quantity", 0),
                    reason=reason,
                )

            self._reset_position_state()

        except Exception as e:
            logger.error(f"❌ Close position error: {e}", exc_info=True)

    def _reset_position_state(self):
        self.state                 = "READY"
        self._sm_transition("COOLDOWN", "position_reset")
        self._sm_transition("SCANNING", "cooldown_complete")
        self.active_position       = None
        self.entry_order_id        = None
        self.sl_order_id           = None
        self.tp_order_id           = None
        self.initial_entry_price   = None
        self.initial_sl_price      = None
        self.initial_tp_price      = None
        self.current_sl_price      = None
        self.current_tp_price      = None
        self.breakeven_moved       = False
        self.profit_locked_pct     = 0.0
        self.highest_price_reached = None
        self.lowest_price_reached  = None
        self.entry_pending_start   = None
        self._pending_ctx          = None

    # =========================================================================
    # ADOPT ORPHANED POSITION (unchanged from v8)
    # =========================================================================

    def _adopt_orphaned_position(self, side: str, entry_price: float,
                                   sl_price: float, tp_price: float,
                                   quantity: float, current_time: int):
        self.active_position = {
            "side": side, "score": 0.0, "reasons": ["ORPHANED"],
            "entry_price": entry_price, "sl": sl_price,
            "tp": tp_price, "quantity": quantity,
            "ctx": TriggerContext(),
        }
        self.initial_entry_price   = entry_price
        self.initial_sl_price      = sl_price
        self.initial_tp_price      = tp_price
        self.current_sl_price      = sl_price
        self.current_tp_price      = tp_price
        self.highest_price_reached = entry_price
        self.lowest_price_reached  = entry_price
        self.state                 = "POSITION_ACTIVE"
        self._sm_transition("POSITION_ACTIVE", "orphan_adopted")
        logger.info(
            f"🔁 Adopted orphaned {side} @ {entry_price:.2f} "
            f"SL={sl_price:.2f} TP={tp_price:.2f}")

    # =========================================================================
    # POTENTIAL TRADE REPORT HELPER
    # =========================================================================

    def _report_potential_trades(self, long_score: float, short_score: float,
                                   long_reasons: List[str],
                                   short_reasons: List[str]):
        self.confluences_detected += 1
        lines = ["📊 *Near-miss Setups*"]
        if long_score >= 75:
            lines.append(f"LONG {long_score:.0f}/100")
            lines += [f"  • {r}" for r in long_reasons[:4]]
        if short_score >= 75:
            lines.append(f"SHORT {short_score:.0f}/100")
            lines += [f"  • {r}" for r in short_reasons[:4]]
        send_telegram_message("\n".join(lines))

    # =========================================================================
    # GET STRATEGY STATS
    # =========================================================================

    def get_strategy_stats(self) -> Dict:
        total  = self.total_exits
        wr     = self.winning_trades / total * 100 if total > 0 else 0.0
        rs     = self.regime_engine.state
        regime_stats = {
            reg: self._perf_tracker.regime_stats(reg)
            for reg in [
                REGIME_TRENDING_BULL, REGIME_TRENDING_BEAR,
                REGIME_RANGING, REGIME_VOLATILE_EXPANSION,
                REGIME_DISTRIBUTION, REGIME_ACCUMULATION,
            ]
        }
        ndr = self._ndr
        return {
            # State
            "state":                self.state,
            "htf_bias":             self.htf_bias,
            "htf_bias_strength":    round(self.htf_bias_strength, 2),
            "htf_components":       self.htf_bias_components,
            "daily_bias":           self.daily_bias,
            "amd_phase":            self.amd_phase,
            "session":              self.current_session,
            "in_killzone":          self.in_killzone,
            # Structures
            "bull_obs":             len(self.order_blocks_bull),
            "bear_obs":             len(self.order_blocks_bear),
            "bull_fvgs":            len(self.fvgs_bull),
            "bear_fvgs":            len(self.fvgs_bear),
            "liq_pools":            len(self.liquidity_pools),
            "swing_highs":          len(self.swing_highs),
            "swing_lows":           len(self.swing_lows),
            "ms_count":             len(self.market_structures),
            # Dealing ranges
            "dr_weekly":            f"{ndr.weekly.low:.0f}–{ndr.weekly.high:.0f}"
                                    if ndr.weekly else "N/A",
            "dr_daily":             f"{ndr.daily.low:.0f}–{ndr.daily.high:.0f}"
                                    if ndr.daily else "N/A",
            "dr_intraday":          f"{ndr.intraday.low:.0f}–{ndr.intraday.high:.0f}"
                                    if ndr.intraday else "N/A",
            # Regime
            "regime":               rs.regime,
            "adx":                  round(rs.adx, 1),
            "atr_ratio":            round(rs.atr_ratio, 2),
            "entry_threshold_mod":  round(rs.entry_threshold_modifier, 2),
            "size_multiplier":      round(rs.size_multiplier, 2),
            # TCE
            "tce_state":            self._active_tce.tce_state
                                    if self._active_tce else "NONE",
            "tce_pullback_prob":    round(self._active_tce.pullback_probability, 2)
                                    if self._active_tce else 0.0,
            # Stats
            "total_entries":        self.total_entries,
            "total_exits":          self.total_exits,
            "winning_trades":       self.winning_trades,
            "consecutive_losses":   self.consecutive_losses,
            "win_rate_pct":         round(wr, 1),
            "daily_pnl":            round(self.daily_pnl, 4),
            "total_pnl":            round(self.total_pnl, 4),
            "confluences_detected": self.confluences_detected,
            # Self-adaptation
            "capital_preserve":     self._capital_preserve_mode,
            "regime_stats":         regime_stats,
            "tp_mode":              "single_tp_dynamic_sl",
        }
