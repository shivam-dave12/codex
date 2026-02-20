"""
regime_engine.py — Dynamic Market Regime Classifier  v9
========================================================
v9 additions over v8
--------------------
1. RegimeSnapshot.size_multiplier   — position-size scale per regime
2. RegimeSnapshot.atr_sl_multiplier — alias consumed by _update_dynamic_stop_loss
3. NestedDealingRanges              — 3-tier IPDA DR (Weekly / Daily / Intraday)
4. RegimePerformanceTracker         — per-regime win-rate self-adaptation
"""

from __future__ import annotations
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Regime labels ─────────────────────────────────────────────────────────────
REGIME_TRENDING_BULL      = "TRENDING_BULL"
REGIME_TRENDING_BEAR      = "TRENDING_BEAR"
REGIME_RANGING            = "RANGING"
REGIME_VOLATILE_EXPANSION = "VOLATILE_EXPANSION"
REGIME_DISTRIBUTION       = "DISTRIBUTION"
REGIME_ACCUMULATION       = "ACCUMULATION"

ADX_TREND_THRESHOLD   = 25.0
ADX_RANGE_THRESHOLD   = 20.0
EXPANSION_ATR_RATIO   = 1.8
HYSTERESIS_BARS       = 2

# Self-adaptation (mirrors strategy.py constants)
_ADAPT_MIN_SAMPLES   = 20
_ADAPT_POOR_WIN_RATE = 0.40
_ADAPT_THRESHOLD_ADD = 10.0
_ADAPT_SIZE_DEGRADED = 0.70

# DR alignment multipliers
_DR_ALL_MULT  = 1.00
_DR_TWO_MULT  = 0.75
_DR_ONE_MULT  = 0.50


# ============================================================================
# DEALING RANGE
# ============================================================================

@dataclass
class DealingRange:
    """IPDA Dealing Range — Premium / Discount / Equilibrium zones."""
    high:      float
    low:       float
    formed_ts: int
    source:    str = "INIT"
    timeframe: str = "unknown"   # "weekly" / "daily" / "intraday"

    @property
    def size(self) -> float:
        return max(self.high - self.low, 1e-9)

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2.0

    def zone_pct(self, price: float) -> float:
        """0.0 = range low; 1.0 = range high."""
        return max(min((price - self.low) / self.size, 1.0), 0.0)

    def is_premium(self, price: float) -> bool:
        return self.zone_pct(price) > 0.618

    def is_discount(self, price: float) -> bool:
        return self.zone_pct(price) < 0.382

    def is_equilibrium(self, price: float) -> bool:
        pct = self.zone_pct(price)
        return 0.382 <= pct <= 0.618


# ============================================================================
# NESTED DEALING RANGES (v9)
# ============================================================================

class NestedDealingRanges:
    """
    Three simultaneous IPDA Dealing Ranges updated by strategy.py:
      weekly   — sourced from 1D candles / weekly BOS
      daily    — sourced from 4H candles / daily BOS
      intraday — sourced from 1H candles / intraday BOS

    Alignment scoring drives position-size multiplier:
      3/3 aligned → 1.00×   2/3 → 0.75×   1/3 → 0.50×
    """

    def __init__(self):
        self.weekly:   Optional[DealingRange] = None
        self.daily:    Optional[DealingRange] = None
        self.intraday: Optional[DealingRange] = None

    # ── Builders ──────────────────────────────────────────────────────────────

    def update_weekly(self, candles_1d: List[Dict], ts: int,
                       bos_direction: Optional[str] = None) -> None:
        """Called when weekly BOS fires or on startup."""
        if len(candles_1d) < 4:
            return
        n      = min(len(candles_1d), 20)
        recent = candles_1d[-n:]
        high   = max(float(c['h']) for c in recent)
        low    = min(float(c['l']) for c in recent)
        src    = (f"WEEKLY_BOS_{bos_direction.upper()}"
                  if bos_direction else "WEEKLY_INIT")
        self.weekly = DealingRange(
            high=high, low=low, formed_ts=ts,
            source=src, timeframe="weekly")
        logger.debug(f"DR weekly [{src}]: {low:.0f}–{high:.0f}")

    def update_daily(self, candles_4h: List[Dict], ts: int,
                      bos_direction: Optional[str] = None) -> None:
        """Called when daily BOS fires or on startup."""
        if len(candles_4h) < 4:
            return
        n      = min(len(candles_4h), 20)
        recent = candles_4h[-n:]
        high   = max(float(c['h']) for c in recent)
        low    = min(float(c['l']) for c in recent)
        src    = (f"DAILY_BOS_{bos_direction.upper()}"
                  if bos_direction else "DAILY_INIT")
        self.daily = DealingRange(
            high=high, low=low, formed_ts=ts,
            source=src, timeframe="daily")
        logger.debug(f"DR daily [{src}]: {low:.0f}–{high:.0f}")

    def update_intraday(self, candles_1h: List[Dict], ts: int,
                         bos_direction: Optional[str] = None) -> None:
        """Called when intraday BOS fires or on startup."""
        if len(candles_1h) < 4:
            return
        n      = min(len(candles_1h), 10)
        recent = candles_1h[-n:]
        high   = max(float(c['h']) for c in recent)
        low    = min(float(c['l']) for c in recent)
        src    = (f"INTRADAY_BOS_{bos_direction.upper()}"
                  if bos_direction else "INTRADAY_INIT")
        self.intraday = DealingRange(
            high=high, low=low, formed_ts=ts,
            source=src, timeframe="intraday")
        logger.debug(f"DR intraday [{src}]: {low:.0f}–{high:.0f}")

    # ── Queries ───────────────────────────────────────────────────────────────

    def best_dr(self) -> Optional[DealingRange]:
        """
        Highest-priority valid DR for score computation.
        Priority: intraday > daily > weekly (most current context wins).
        """
        return self.intraday or self.daily or self.weekly

    def hard_opposed(self, price: float, side: str) -> bool:
        """
        L1 hard gate: weekly DR premium/discount blocks contrary entries.
          LONG  blocked when price is in weekly premium (> 61.8%)
          SHORT blocked when price is in weekly discount (< 38.2%)
        Returns False if weekly DR not yet built.
        """
        if self.weekly is None:
            return False
        if side == "long"  and self.weekly.is_premium(price):
            return True
        if side == "short" and self.weekly.is_discount(price):
            return True
        return False

    def alignment_score(self, price: float,
                         side: str) -> Tuple[int, float]:
        """
        Counts how many of the 3 DRs align with entry side:
          long  → needs price in discount zone of each DR
          short → needs price in premium zone of each DR

        Returns (aligned_count, size_multiplier).
        None DRs are skipped (do not count for OR against).
        """
        aligned = 0
        for dr in (self.weekly, self.daily, self.intraday):
            if dr is None:
                continue
            if side == "long"  and dr.is_discount(price):
                aligned += 1
            elif side == "short" and dr.is_premium(price):
                aligned += 1

        if aligned >= 3:
            return 3, _DR_ALL_MULT
        if aligned == 2:
            return 2, _DR_TWO_MULT
        return 1, _DR_ONE_MULT   # 0 or 1 — never fully block, just reduce


# ============================================================================
# REGIME PERFORMANCE TRACKER (v9)
# ============================================================================

@dataclass
class _RegimeStats:
    trades:    int   = 0
    wins:      int   = 0
    total_pnl: float = 0.0
    history:   deque = field(
        default_factory=lambda: deque(maxlen=_ADAPT_MIN_SAMPLES))


class RegimePerformanceTracker:
    """
    Tracks per-regime win rates over a rolling 20-trade window.
    Feeds into strategy self-adaptation:
      threshold_add(regime) → extra confluence points  (0 or +10)
      size_mult(regime)     → position-size scale      (1.0 or 0.70)
    """

    def __init__(self):
        self._stats: Dict[str, _RegimeStats] = {
            r: _RegimeStats()
            for r in [
                REGIME_TRENDING_BULL, REGIME_TRENDING_BEAR,
                REGIME_RANGING, REGIME_VOLATILE_EXPANSION,
                REGIME_DISTRIBUTION, REGIME_ACCUMULATION,
            ]
        }

    def record(self, regime: str, side: str,
               pnl: float, won: bool) -> None:
        st = self._stats.get(regime)
        if st is None:
            return
        st.trades    += 1
        st.total_pnl += pnl
        st.history.append(won)
        if won:
            st.wins += 1

    def _rolling_win_rate(self, regime: str) -> Optional[float]:
        st = self._stats.get(regime)
        if st is None or len(st.history) < _ADAPT_MIN_SAMPLES:
            return None      # not enough samples yet
        return sum(st.history) / len(st.history)

    def threshold_add(self, regime: str) -> float:
        """Extra confluence points when regime is degraded."""
        wr = self._rolling_win_rate(regime)
        if wr is not None and wr < _ADAPT_POOR_WIN_RATE:
            return _ADAPT_THRESHOLD_ADD
        return 0.0

    def size_mult(self, regime: str) -> float:
        """Position-size multiplier: 1.0 normal, 0.70 degraded."""
        wr = self._rolling_win_rate(regime)
        if wr is not None and wr < _ADAPT_POOR_WIN_RATE:
            return _ADAPT_SIZE_DEGRADED
        return 1.0

    def regime_stats(self, regime: str) -> Dict:
        """Dict consumed by get_strategy_stats()."""
        st = self._stats.get(regime)
        if st is None:
            return {"trades": 0, "wins": 0, "win_rate": None,
                    "degraded": False, "total_pnl": 0.0, "samples": 0}
        wr = self._rolling_win_rate(regime)
        return {
            "trades":    st.trades,
            "wins":      st.wins,
            "win_rate":  wr,
            "degraded":  (wr is not None and wr < _ADAPT_POOR_WIN_RATE),
            "total_pnl": round(st.total_pnl, 4),
            "samples":   len(st.history),
        }


# ============================================================================
# REGIME SNAPSHOT
# ============================================================================

@dataclass
class RegimeSnapshot:
    """Immutable snapshot consumed by strategy on every tick."""
    regime:           str   = REGIME_RANGING
    atr_5m:           float = 0.0
    atr_1h:           float = 0.0
    atr_ratio:        float = 1.0
    adx:              float = 0.0
    di_plus:          float = 0.0
    di_minus:         float = 0.0
    expansion_active: bool  = False
    dealing_range:    Optional[DealingRange] = None
    last_update_ts:   int   = 0

    # ── SL / Trailing ─────────────────────────────────────────────────────────

    @property
    def sl_atr_multiplier(self) -> float:
        return {
            REGIME_TRENDING_BULL:      1.5,
            REGIME_TRENDING_BEAR:      1.5,
            REGIME_RANGING:            1.1,
            REGIME_VOLATILE_EXPANSION: 2.2,
            REGIME_DISTRIBUTION:       1.7,
            REGIME_ACCUMULATION:       1.7,
        }.get(self.regime, 1.5)

    @property
    def atr_sl_multiplier(self) -> float:
        """Alias used by strategy._update_dynamic_stop_loss."""
        return self.sl_atr_multiplier

    @property
    def tp_rr_floor(self) -> float:
        return {
            REGIME_TRENDING_BULL:      3.5,
            REGIME_TRENDING_BEAR:      3.5,
            REGIME_RANGING:            2.0,
            REGIME_VOLATILE_EXPANSION: 4.5,
            REGIME_DISTRIBUTION:       2.5,
            REGIME_ACCUMULATION:       2.5,
        }.get(self.regime, 3.0)

    # ── Position sizing (v9) ──────────────────────────────────────────────────

    @property
    def size_multiplier(self) -> float:
        """
        Base position-size scale per regime.
        Self-adaptation via RegimePerformanceTracker further scales this.
        """
        return {
            REGIME_TRENDING_BULL:      1.00,
            REGIME_TRENDING_BEAR:      1.00,
            REGIME_RANGING:            0.75,
            REGIME_VOLATILE_EXPANSION: 0.60,
            REGIME_DISTRIBUTION:       0.80,
            REGIME_ACCUMULATION:       0.80,
        }.get(self.regime, 0.80)

    # ── Entry gate ────────────────────────────────────────────────────────────

    @property
    def entry_threshold_modifier(self) -> float:
        return {
            REGIME_TRENDING_BULL:      0.88,
            REGIME_TRENDING_BEAR:      0.88,
            REGIME_RANGING:            1.15,
            REGIME_VOLATILE_EXPANSION: 1.10,
            REGIME_DISTRIBUTION:       1.05,
            REGIME_ACCUMULATION:       1.05,
        }.get(self.regime, 1.0)

    # ── TCE ───────────────────────────────────────────────────────────────────

    @property
    def tce_max_age_ms(self) -> int:
        base = 4 * 3600 * 1000
        mult = {
            REGIME_TRENDING_BULL:      1.5,
            REGIME_TRENDING_BEAR:      1.5,
            REGIME_RANGING:            0.75,
            REGIME_VOLATILE_EXPANSION: 0.40,
            REGIME_DISTRIBUTION:       1.0,
            REGIME_ACCUMULATION:       1.0,
        }.get(self.regime, 1.0)
        return int(base * mult)

    # ── Score multipliers ─────────────────────────────────────────────────────

    @property
    def ob_score_multiplier(self) -> float:
        return {
            REGIME_TRENDING_BULL:      1.25,
            REGIME_TRENDING_BEAR:      1.25,
            REGIME_RANGING:            0.80,
            REGIME_VOLATILE_EXPANSION: 0.65,
            REGIME_DISTRIBUTION:       0.90,
            REGIME_ACCUMULATION:       0.90,
        }.get(self.regime, 1.0)

    @property
    def fvg_score_multiplier(self) -> float:
        return {
            REGIME_TRENDING_BULL:      0.90,
            REGIME_TRENDING_BEAR:      0.90,
            REGIME_RANGING:            1.20,
            REGIME_VOLATILE_EXPANSION: 1.40,
            REGIME_DISTRIBUTION:       1.10,
            REGIME_ACCUMULATION:       1.10,
        }.get(self.regime, 1.0)

    @property
    def sweep_score_multiplier(self) -> float:
        return {
            REGIME_TRENDING_BULL:      1.0,
            REGIME_TRENDING_BEAR:      1.0,
            REGIME_RANGING:            1.15,
            REGIME_VOLATILE_EXPANSION: 1.60,
            REGIME_DISTRIBUTION:       1.45,
            REGIME_ACCUMULATION:       1.45,
        }.get(self.regime, 1.0)

    @property
    def bypass_tce_on_sweep(self) -> bool:
        return self.regime == REGIME_VOLATILE_EXPANSION

    @property
    def breakeven_trigger_pct(self) -> float:
        return {
            REGIME_TRENDING_BULL:      0.40,
            REGIME_TRENDING_BEAR:      0.40,
            REGIME_RANGING:            0.55,
            REGIME_VOLATILE_EXPANSION: 0.35,
            REGIME_DISTRIBUTION:       0.48,
            REGIME_ACCUMULATION:       0.48,
        }.get(self.regime, 0.50)

    @property
    def trail_tiers(self) -> List[Tuple[float, float]]:
        if self.regime in (REGIME_TRENDING_BULL, REGIME_TRENDING_BEAR):
            return [(0.40, 0.50), (0.60, 0.72), (0.72, 0.88)]
        if self.regime == REGIME_RANGING:
            return [(0.55, 0.45), (0.72, 0.68), (0.82, 0.85)]
        if self.regime == REGIME_VOLATILE_EXPANSION:
            return [(0.30, 0.55), (0.55, 0.75), (0.70, 0.90)]
        return [(0.50, 0.50), (0.67, 0.70), (0.75, 0.85)]


# ============================================================================
# REGIME ENGINE
# ============================================================================

class RegimeEngine:
    """
    Called once per structure-update cycle from AdvancedICTStrategy.

        engine = RegimeEngine()
        engine.update(c5m, c1h, price, ts, recent_bos_direction)
        snap = engine.state   # RegimeSnapshot
    """

    def __init__(self, adx_period: int = 14, atr_period: int = 14,
                 baseline_period: int = 20):
        self._adx_period      = adx_period
        self._atr_period      = atr_period
        self._baseline_period = baseline_period
        self._state           = RegimeSnapshot()
        self._dealing_range:  Optional[DealingRange] = None
        self._prev_regime     = REGIME_RANGING
        self._hysteresis_ctr  = 0

    @property
    def state(self) -> RegimeSnapshot:
        return self._state

    # ── Public ───────────────────────────────────────────────────────────────

    def update(self, c5m: List[Dict], c1h: List[Dict],
               current_price: float, current_time: int,
               recent_bos_direction: Optional[str] = None) -> RegimeSnapshot:
        if len(c5m) < self._adx_period * 2 + 5:
            return self._state
        try:
            atr_5m   = self._wilder_atr(c5m, self._atr_period)
            baseline = self._baseline_atr(c5m, self._baseline_period)
            ratio    = atr_5m / baseline if baseline > 0 else 1.0

            atr_1h = 0.0
            if len(c1h) >= self._atr_period + 1:
                atr_1h = self._wilder_atr(c1h, self._atr_period)

            adx, di_plus, di_minus = self._calculate_adx(
                c5m, self._adx_period)

            if recent_bos_direction:
                self._reset_dealing_range(
                    c5m, current_price, current_time, recent_bos_direction)
            elif self._dealing_range is None:
                self._init_dealing_range(c5m, current_price, current_time)

            raw_regime = self._classify(
                adx, di_plus, di_minus, ratio,
                current_price, self._dealing_range)

            # Hysteresis — require HYSTERESIS_BARS consecutive bars to switch
            if raw_regime != self._prev_regime:
                self._hysteresis_ctr += 1
                if self._hysteresis_ctr >= HYSTERESIS_BARS:
                    self._prev_regime    = raw_regime
                    self._hysteresis_ctr = 0
                else:
                    raw_regime = self._prev_regime
            else:
                self._hysteresis_ctr = 0

            self._state = RegimeSnapshot(
                regime=raw_regime,
                atr_5m=atr_5m,
                atr_1h=atr_1h,
                atr_ratio=ratio,
                adx=adx,
                di_plus=di_plus,
                di_minus=di_minus,
                expansion_active=(ratio >= EXPANSION_ATR_RATIO),
                dealing_range=self._dealing_range,
                last_update_ts=current_time,
            )
            logger.debug(
                f"📊 REGIME={raw_regime} ADX={adx:.1f} "
                f"+DI={di_plus:.1f} -DI={di_minus:.1f} "
                f"ATR_ratio={ratio:.2f} size_mult={self._state.size_multiplier:.2f}")
        except Exception as e:
            logger.error(f"❌ RegimeEngine.update: {e}", exc_info=True)
        return self._state

    # ── Classification ────────────────────────────────────────────────────────

    def _classify(self, adx: float, di_plus: float, di_minus: float,
                  atr_ratio: float, price: float,
                  dr: Optional[DealingRange]) -> str:

        if atr_ratio >= EXPANSION_ATR_RATIO:
            return REGIME_VOLATILE_EXPANSION

        if adx >= ADX_TREND_THRESHOLD:
            if di_plus > di_minus:
                if dr and dr.is_premium(price):
                    return REGIME_DISTRIBUTION
                return REGIME_TRENDING_BULL
            else:
                if dr and dr.is_discount(price):
                    return REGIME_ACCUMULATION
                return REGIME_TRENDING_BEAR

        if adx < ADX_RANGE_THRESHOLD:
            return REGIME_RANGING

        # Transitional ADX 20–25: use dealing range zone
        if dr:
            if dr.is_premium(price):
                return REGIME_DISTRIBUTION
            if dr.is_discount(price):
                return REGIME_ACCUMULATION
        return REGIME_RANGING

    # ── Dealing range ────────────────────────────────────────────────────────

    def _init_dealing_range(self, candles: List[Dict],
                             price: float, ts: int) -> None:
        n      = min(len(candles), 50)
        recent = candles[-n:]
        self._dealing_range = DealingRange(
            high=max(float(c['h']) for c in recent),
            low=min(float(c['l']) for c in recent),
            formed_ts=ts, source="INIT")

    def _reset_dealing_range(self, candles: List[Dict], price: float,
                              ts: int, bos_dir: str) -> None:
        n      = min(len(candles), 20)
        recent = candles[-n:]
        high   = max(float(c['h']) for c in recent)
        low    = min(float(c['l']) for c in recent)
        source = f"BOS_{bos_dir.upper()}"
        self._dealing_range = DealingRange(
            high=high, low=low, formed_ts=ts, source=source)
        logger.info(
            f"📐 DR reset [{source}]: "
            f"{low:.2f}–{high:.2f} mid={((high+low)/2):.2f}")

    # ── Math ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _wilder_atr(candles: List[Dict], period: int) -> float:
        if len(candles) < period + 1:
            return 0.0
        trs: List[float] = []
        for i in range(1, len(candles)):
            h, l = float(candles[i]['h']), float(candles[i]['l'])
            pc   = float(candles[i - 1]['c'])
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        atr = sum(trs[:period]) / period
        for tr in trs[period:]:
            atr = (atr * (period - 1) + tr) / period
        return atr

    @staticmethod
    def _baseline_atr(candles: List[Dict], period: int) -> float:
        if len(candles) < period + 1:
            return 0.0
        trs: List[float] = []
        for i in range(max(1, len(candles) - period), len(candles)):
            h, l = float(candles[i]['h']), float(candles[i]['l'])
            pc   = float(candles[i - 1]['c'])
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        return sum(trs) / len(trs) if trs else 0.0

    @staticmethod
    def _calculate_adx(candles: List[Dict],
                        period: int) -> Tuple[float, float, float]:
        n = len(candles)
        if n < 2 * period:
            return 0.0, 0.0, 0.0

        plus_dms:  List[float] = []
        minus_dms: List[float] = []
        trs:       List[float] = []

        for i in range(1, n):
            h, ph = float(candles[i]['h']), float(candles[i-1]['h'])
            l, pl = float(candles[i]['l']), float(candles[i-1]['l'])
            pc    = float(candles[i-1]['c'])
            up    = h - ph
            dn    = pl - l
            plus_dms.append(up if (up > dn and up > 0) else 0.0)
            minus_dms.append(dn if (dn > up and dn > 0) else 0.0)
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))

        sm_tr  = sum(trs[:period])
        sm_pdm = sum(plus_dms[:period])
        sm_mdm = sum(minus_dms[:period])

        dx_list: List[float] = []
        di_plus_f = di_minus_f = 0.0

        for i in range(period, len(trs)):
            sm_tr  = sm_tr  - sm_tr  / period + trs[i]
            sm_pdm = sm_pdm - sm_pdm / period + plus_dms[i]
            sm_mdm = sm_mdm - sm_mdm / period + minus_dms[i]
            di_plus_f  = (sm_pdm / sm_tr * 100) if sm_tr > 0 else 0.0
            di_minus_f = (sm_mdm / sm_tr * 100) if sm_tr > 0 else 0.0
            denom = di_plus_f + di_minus_f
            dx_list.append(
                abs(di_plus_f - di_minus_f) / denom * 100
                if denom > 0 else 0.0)

        if not dx_list:
            return 0.0, 0.0, 0.0

        adx = sum(dx_list[:period]) / period
        for dx in dx_list[period:]:
            adx = (adx * (period - 1) + dx) / period

        return round(adx, 2), round(di_plus_f, 2), round(di_minus_f, 2)
