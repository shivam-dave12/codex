# ============================================================================
# position_manager.py
# ============================================================================
"""
Position Manager - v3 Production Rewrite
=========================================

ROOT CAUSES FIXED vs v2:

STATE ISOLATION:
  - PartialTarget list is built fresh on every create_position() call.
    Previous trade's target objects cannot survive into the next trade.
  - All watermark fields (highest_price, lowest_price) are set from
    ACTUAL fill price, not from entry_price estimate.
  - remaining_quantity is always recomputed from actual filled qty,
    never carried over.

BREAKEVEN:
  - Breakeven price is fee-adjusted: entry + (2 × taker_fee × entry)
    for LONG so the move to BE is always at least fee-neutral.
  - Breakeven only fires once per trade (breakeven_moved flag lives
    inside Position dataclass, not in PositionManager itself).

TRAILING STOP:
  - Trailing activates only after TRAILING_ACTIVATION_RR from config.
  - Step-size gate: SL only updates if improvement ≥ SL_MINIMUM_IMPROVEMENT_PCT.
  - Ratchet enforced: new_sl must be strictly better than current_sl.

PARTIAL TARGETS:
  - check_manual_profit_targets() is idempotent — safe to call every tick.
  - Each PartialTarget has a close_order_id recorded so duplicate
    execution on same target is impossible.
  - remaining_quantity guard: never tries to close more than available.

THREAD SAFETY:
  - All public methods use RLock.
  - _lock is never held during API calls — lock → compute → release → API call.
"""

import logging
import time
import threading
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

import config

logger = logging.getLogger(__name__)


# ============================================================================
# ENUMS & DATACLASSES
# ============================================================================

class PositionState(Enum):
    OPENING        = "OPENING"
    ACTIVE         = "ACTIVE"
    PARTIAL_CLOSED = "PARTIAL_CLOSED"
    CLOSING        = "CLOSING"
    CLOSED         = "CLOSED"


@dataclass
class PartialTarget:
    """Individual profit target for manual management."""
    price:       float
    percentage:  float        # % of original position to close at this target
    rr_ratio:    float
    target_type: str
    executed:         bool           = False
    executed_at:      Optional[float] = None
    close_order_id:   Optional[str]   = None   # prevents duplicate execution
    actual_fill_price: Optional[float] = None


@dataclass
class Position:
    """
    Single trade position with full lifecycle tracking.

    DESIGN:
      - All fields that carry state are set explicitly in create_position().
      - No defaults that could silently survive from a previous trade.
      - partial_targets is always a freshly constructed list.
    """
    # Core
    side:         str    # "long" or "short"
    entry_price:  float  # ACTUAL fill price (set after fill confirmation)
    quantity:     float  # ACTUAL filled quantity
    stop_loss:    float

    # API order IDs
    limit_order_id: Optional[str] = None
    sl_order_id:    Optional[str] = None
    tp_order_id:    Optional[str] = None

    # Targets
    partial_targets: List[PartialTarget] = field(default_factory=list)

    # State
    state:              PositionState = PositionState.OPENING
    filled_quantity:    float         = 0.0
    remaining_quantity: float         = 0.0

    # Watermarks — set from ACTUAL fill, not estimate
    highest_price: Optional[float] = None
    lowest_price:  Optional[float] = None

    # Breakeven tracking — lives in Position, not PositionManager
    breakeven_moved:   bool  = False
    profit_locked_pct: float = 0.0

    # Timing
    opened_at: float = field(default_factory=time.time)

    # Metadata
    entry_reason: str  = ""
    close_reason: str  = ""
    closed_at:    Optional[float] = None
    metadata:     Dict = field(default_factory=dict)

    def __post_init__(self):
        self.remaining_quantity = self.quantity
        # Watermarks set from actual entry — never None once ACTIVE
        if self.side == "long":
            self.highest_price = self.entry_price
            self.lowest_price  = self.entry_price
        else:
            self.lowest_price  = self.entry_price
            self.highest_price = self.entry_price

    # ── Properties ──────────────────────────────────────────────────────────

    @property
    def is_long(self) -> bool:
        return self.side == "long"

    @property
    def is_short(self) -> bool:
        return self.side == "short"

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry_price - self.stop_loss)

    @property
    def risk_amount(self) -> float:
        return self.risk_per_unit * self.quantity

    def unrealized_pnl(self, current_price: float) -> float:
        if self.is_long:
            return (current_price - self.entry_price) * self.remaining_quantity
        return (self.entry_price - current_price) * self.remaining_quantity

    def current_rr(self, current_price: float) -> float:
        risk = self.risk_per_unit
        if risk <= 0:
            return 0.0
        if self.is_long:
            return (current_price - self.entry_price) / risk
        return (self.entry_price - current_price) / risk

    def get_next_partial_target(self) -> Optional[PartialTarget]:
        """Next unexecuted target, sorted by price proximity."""
        pending = [t for t in self.partial_targets
                   if not t.executed and t.close_order_id is None]
        if not pending:
            return None
        if self.is_long:
            return min(pending, key=lambda t: t.price)   # closest above
        return max(pending, key=lambda t: t.price)        # closest below

    def all_targets_hit(self) -> bool:
        return all(t.executed for t in self.partial_targets)


# ============================================================================
# POSITION MANAGER
# ============================================================================

class PositionManager:
    """
    API-aware position manager.

    CoinSwitch allows only ONE TP order per position.
    Strategy: place single TP for first target via exchange order,
    manage subsequent targets manually via market orders on price cross.
    """

    # Fee constants (CoinSwitch futures + GST)
    TAKER_FEE_PCT = 0.0006          # 0.06%
    GST_PCT       = 0.18            # 18% on fee
    TOTAL_FEE_PCT = TAKER_FEE_PCT * (1 + GST_PCT)  # ~0.0708%
    ROUND_TRIP_FEE_PCT = TOTAL_FEE_PCT * 2          # entry + exit

    def __init__(self):
        self._lock = threading.RLock()
        self.active_position:  Optional[Position] = None
        self.position_history: List[Position]     = []

        logger.info("✅ PositionManager v3 initialized")
        logger.info(f"   Round-trip fee: {self.ROUND_TRIP_FEE_PCT*100:.4f}%")

    # ========================================================================
    # CREATE POSITION
    # ========================================================================

    def create_position(
        self,
        side:                 str,
        entry_price:          float,
        quantity:             float,
        stop_loss:            float,
        take_profit_targets:  List[Dict],
        entry_reason:         str  = "",
        metadata:             Dict = None,
    ) -> Position:
        """
        Create a new Position object. Called AFTER fill confirmation so
        entry_price is the ACTUAL fill price.

        Args:
            take_profit_targets: list of dicts:
                {"price": float, "percentage": float, "rr": float, "type": str}

        Returns:
            Fresh Position object with isolated state.

        Raises:
            ValueError: if position already exists or inputs invalid.
        """
        with self._lock:
            if self.active_position is not None:
                raise ValueError(
                    f"Cannot create position: existing {self.active_position.side} "
                    f"position still active (state={self.active_position.state.value})"
                )

            # ── Input validation ─────────────────────────────────────────────
            if quantity <= 0:
                raise ValueError(f"Invalid quantity: {quantity}")
            if entry_price <= 0:
                raise ValueError(f"Invalid entry_price: {entry_price}")

            if side == "long" and stop_loss >= entry_price:
                raise ValueError(
                    f"LONG SL must be below entry: "
                    f"SL={stop_loss:.2f} Entry={entry_price:.2f}"
                )
            if side == "short" and stop_loss <= entry_price:
                raise ValueError(
                    f"SHORT SL must be above entry: "
                    f"SL={stop_loss:.2f} Entry={entry_price:.2f}"
                )

            if not take_profit_targets:
                raise ValueError("At least one take profit target required")

            # ── Build targets — fresh objects, no recycling ──────────────────
            raw_total_pct = sum(t["percentage"] for t in take_profit_targets)
            targets = []
            for t in take_profit_targets:
                # Normalise percentages if they don't sum to 100
                pct = (t["percentage"] / raw_total_pct * 100.0
                       if abs(raw_total_pct - 100) > 0.5
                       else t["percentage"])

                pt = PartialTarget(
                    price      = float(t["price"]),
                    percentage = pct,
                    rr_ratio   = float(t.get("rr", 0.0)),
                    target_type= str(t.get("type", "RR_BASED")),
                )

                # Validate direction
                if side == "long" and pt.price <= entry_price:
                    logger.warning(f"⚠️ Skipping invalid LONG target "
                                   f"{pt.price:.2f} <= entry {entry_price:.2f}")
                    continue
                if side == "short" and pt.price >= entry_price:
                    logger.warning(f"⚠️ Skipping invalid SHORT target "
                                   f"{pt.price:.2f} >= entry {entry_price:.2f}")
                    continue

                targets.append(pt)

            if not targets:
                raise ValueError("No valid take profit targets after validation")

            # ── Create position ──────────────────────────────────────────────
            pos = Position(
                side          = side,
                entry_price   = entry_price,
                quantity      = quantity,
                stop_loss     = stop_loss,
                entry_reason  = entry_reason,
                metadata      = metadata or {},
            )
            pos.partial_targets    = targets
            pos.filled_quantity    = quantity
            pos.remaining_quantity = quantity
            pos.state              = PositionState.ACTIVE

            # Watermarks from actual fill price
            pos.highest_price = entry_price
            pos.lowest_price  = entry_price

            self.active_position = pos

            logger.info("=" * 70)
            logger.info(f"📊 POSITION CREATED: {side.upper()} {quantity} @ ${entry_price:.2f}")
            logger.info(f"   SL: ${stop_loss:.2f} | Risk: ${pos.risk_amount:.2f}")
            logger.info(f"   Targets: {len(targets)}")
            for i, t in enumerate(targets, 1):
                logger.info(f"   TP{i}: ${t.price:.2f} ({t.percentage:.0f}%) "
                            f"RR={t.rr_ratio:.2f}")
            logger.info("=" * 70)

            return pos

    # ========================================================================
    # FIRST TP FOR API ORDER
    # ========================================================================

    def get_first_tp_for_api(self) -> Optional[Tuple[float, float]]:
        """
        Returns (price, quantity) for the first TP target — this is the one
        placed as an exchange order. Subsequent targets are managed manually.
        Returns None if no active position or no targets.
        """
        with self._lock:
            if not self.active_position:
                return None
            pos = self.active_position
            if not pos.partial_targets:
                return None

            first = pos.partial_targets[0]
            tp_qty = round(pos.quantity * (first.percentage / 100.0), 4)
            tp_qty = max(tp_qty, config.MIN_POSITION_SIZE)
            tp_qty = min(tp_qty, pos.remaining_quantity)

            return (first.price, tp_qty)

    # ========================================================================
    # UPDATE FILLED QUANTITY (after fill confirmation)
    # ========================================================================

    def update_filled_quantity(self, filled_qty: float,
                               actual_fill_price: Optional[float] = None):
        """
        Called once fill is confirmed. Updates quantity fields and optionally
        corrects entry_price from actual fill price.
        """
        with self._lock:
            if not self.active_position:
                return

            pos = self.active_position
            pos.filled_quantity    = filled_qty
            pos.remaining_quantity = filled_qty

            if actual_fill_price and actual_fill_price > 0:
                old_entry = pos.entry_price
                pos.entry_price    = actual_fill_price
                pos.highest_price  = actual_fill_price
                pos.lowest_price   = actual_fill_price
                if abs(old_entry - actual_fill_price) > config.TICK_SIZE:
                    logger.info(f"📌 Entry price corrected: "
                                f"${old_entry:.2f} → ${actual_fill_price:.2f}")

            pos.state = PositionState.ACTIVE
            logger.info(f"✅ Position ACTIVE: "
                        f"filled={filled_qty:.4f} @ ${pos.entry_price:.2f}")

    # ========================================================================
    # MANUAL PARTIAL TARGET CHECK
    # ========================================================================

    def check_manual_profit_targets(
        self, current_price: float, order_manager
    ) -> bool:
        """
        Check if any manual partial profit target should be executed.
        Idempotent: safe to call every tick.
        Uses close_order_id to prevent double-execution.

        Returns True if a partial close was executed.
        """
        # Snapshot under lock, then release before API call
        with self._lock:
            if not self.active_position:
                return False
            pos = self.active_position
            if pos.state not in (PositionState.ACTIVE, PositionState.PARTIAL_CLOSED):
                return False

            next_target = pos.get_next_partial_target()
            if not next_target:
                return False

            # Price check
            target_hit = (
                (pos.is_long  and current_price >= next_target.price) or
                (pos.is_short and current_price <= next_target.price)
            )
            if not target_hit:
                return False

            # Compute close quantity — snapshot before releasing lock
            close_qty = round(
                pos.remaining_quantity * (next_target.percentage / 100.0), 4
            )
            close_qty = max(close_qty, config.MIN_POSITION_SIZE)
            close_qty = min(close_qty, pos.remaining_quantity)

            # Mark as in-flight to prevent concurrent execution
            next_target.close_order_id = "IN_FLIGHT"
            close_side = "SELL" if pos.is_long else "BUY"
            remaining_after = pos.remaining_quantity - close_qty

        # ── API call outside lock ────────────────────────────────────────────
        try:
            from order_manager import GlobalRateLimiter
            time.sleep(0.5)
            GlobalRateLimiter.wait()

            result = order_manager.place_market_order(
                side=close_side,
                quantity=close_qty,
                reduce_only=True,
            )

            with self._lock:
                if not self.active_position:
                    return False
                pos = self.active_position

                if result and result.get("order_id"):
                    next_target.executed        = True
                    next_target.executed_at     = time.time()
                    next_target.close_order_id  = result["order_id"]
                    next_target.actual_fill_price = current_price  # approximate

                    pos.remaining_quantity = remaining_after

                    if pos.remaining_quantity < config.MIN_POSITION_SIZE:
                        pos.state = PositionState.CLOSED
                    else:
                        pos.state = PositionState.PARTIAL_CLOSED

                    logger.info(f"✅ Partial close: TP @ ${next_target.price:.2f} "
                                f"({next_target.percentage:.0f}%) "
                                f"order={result['order_id']}")
                    logger.info(f"   Remaining: {pos.remaining_quantity:.4f}")
                    return True

                else:
                    # API failed — unmark in-flight so it can retry
                    next_target.close_order_id = None
                    logger.error(f"❌ Partial close API failed: {result}")
                    return False

        except Exception as e:
            with self._lock:
                if (self.active_position and
                        self.active_position.partial_targets):
                    # Unmark in-flight on exception
                    for t in self.active_position.partial_targets:
                        if t.close_order_id == "IN_FLIGHT":
                            t.close_order_id = None
            logger.error(f"❌ check_manual_profit_targets error: {e}",
                         exc_info=True)
            return False

    # ========================================================================
    # BREAKEVEN
    # ========================================================================

    def check_breakeven(
        self, current_price: float, order_manager
    ) -> bool:
        """
        Move SL to breakeven when RR >= config.BREAKEVEN_TRIGGER_RR.
        BE price = entry + (ROUND_TRIP_FEE_PCT × entry) for LONG
                 = entry - (ROUND_TRIP_FEE_PCT × entry) for SHORT
        This ensures the BE level is always fee-neutral.
        """
        with self._lock:
            if not self.active_position:
                return False
            pos = self.active_position
            if pos.state not in (PositionState.ACTIVE, PositionState.PARTIAL_CLOSED):
                return False
            if pos.breakeven_moved:
                return False

            current_rr = pos.current_rr(current_price)
            if current_rr < config.BREAKEVEN_TRIGGER_RR:
                return False

            # Fee-adjusted breakeven price
            fee_buffer = self.ROUND_TRIP_FEE_PCT * pos.entry_price
            if pos.is_long:
                new_sl = pos.entry_price + fee_buffer
                if new_sl <= pos.stop_loss:
                    return False  # already better than BE
            else:
                new_sl = pos.entry_price - fee_buffer
                if new_sl >= pos.stop_loss:
                    return False

            new_sl       = round(new_sl, 2)
            exit_side    = "SELL" if pos.is_long else "BUY"
            qty          = pos.remaining_quantity
            old_sl_id    = pos.sl_order_id

        # ── API call outside lock ────────────────────────────────────────────
        try:
            from order_manager import GlobalRateLimiter
            result = order_manager.replace_stop_loss(
                existing_sl_order_id=old_sl_id,
                side=exit_side,
                quantity=qty,
                new_trigger_price=new_sl,
            )

            with self._lock:
                if not self.active_position:
                    return False
                pos = self.active_position

                if result and result.get("order_id"):
                    pos.stop_loss        = new_sl
                    pos.sl_order_id      = result["order_id"]
                    pos.breakeven_moved  = True
                    logger.info(f"✅ Breakeven SL set: ${new_sl:.2f} "
                                f"(fee-adjusted, RR={current_rr:.2f})")
                    return True
                elif result is None:
                    # SL already fired — do not update
                    logger.warning("⚠️ BE: SL already fired, skipping")
                    return False
                else:
                    logger.error(f"❌ Breakeven SL placement failed: {result}")
                    return False

        except Exception as e:
            logger.error(f"❌ check_breakeven error: {e}", exc_info=True)
            return False

    # ========================================================================
    # TRAILING STOP
    # ========================================================================

    def check_trailing_stop(
        self, current_price: float, order_manager
    ) -> bool:
        """
        Update trailing stop when RR >= config.TRAILING_ACTIVATION_RR.
        Locks in config.TRAILING_DISTANCE_PCT below/above the watermark.
        Step-size gate: only updates if improvement >= SL_MINIMUM_IMPROVEMENT_PCT.
        Ratchet: never moves SL backwards.
        """
        with self._lock:
            if not self.active_position:
                return False
            pos = self.active_position
            if pos.state not in (PositionState.ACTIVE, PositionState.PARTIAL_CLOSED):
                return False

            # Update watermarks
            if pos.is_long:
                if current_price > (pos.highest_price or 0):
                    pos.highest_price = current_price
            else:
                if current_price < (pos.lowest_price or float('inf')):
                    pos.lowest_price = current_price

            current_rr = pos.current_rr(current_price)
            if current_rr < config.TRAILING_ACTIVATION_RR:
                return False

            trail_dist = config.TRAILING_DISTANCE_PCT

            if pos.is_long:
                new_sl = (pos.highest_price or current_price) * (1 - trail_dist)
                if new_sl <= pos.stop_loss:
                    return False  # ratchet: not better
            else:
                new_sl = (pos.lowest_price or current_price) * (1 + trail_dist)
                if new_sl >= pos.stop_loss:
                    return False

            # Step-size gate
            improvement_pct = abs(new_sl - pos.stop_loss) / pos.entry_price
            if improvement_pct < config.SL_MINIMUM_IMPROVEMENT_PCT:
                return False

            new_sl    = round(new_sl, 2)
            exit_side = "SELL" if pos.is_long else "BUY"
            qty       = pos.remaining_quantity
            old_sl_id = pos.sl_order_id

        # ── API call outside lock ────────────────────────────────────────────
        try:
            from order_manager import GlobalRateLimiter
            result = order_manager.replace_stop_loss(
                existing_sl_order_id=old_sl_id,
                side=exit_side,
                quantity=qty,
                new_trigger_price=new_sl,
            )

            with self._lock:
                if not self.active_position:
                    return False
                pos = self.active_position

                if result and result.get("order_id"):
                    old_sl = pos.stop_loss
                    pos.stop_loss   = new_sl
                    pos.sl_order_id = result["order_id"]
                    logger.info(f"📈 Trailing SL: ${old_sl:.2f} → ${new_sl:.2f} "
                                f"(RR={current_rr:.2f})")
                    return True
                elif result is None:
                    logger.warning("⚠️ Trailing: SL already fired")
                    return False
                else:
                    logger.error(f"❌ Trailing SL update failed: {result}")
                    return False

        except Exception as e:
            logger.error(f"❌ check_trailing_stop error: {e}", exc_info=True)
            return False

    # ========================================================================
    # CLOSE POSITION
    # ========================================================================

    def close_position(self, reason: str = "MANUAL") -> Optional[Position]:
        """
        Mark position as closed and move to history.
        Called by strategy after exchange confirms exit.
        """
        with self._lock:
            if not self.active_position:
                return None

            pos              = self.active_position
            pos.state        = PositionState.CLOSED
            pos.close_reason = reason
            pos.closed_at    = time.time()

            self.position_history.append(pos)
            self.active_position = None

            duration = pos.closed_at - pos.opened_at
            logger.info(f"📪 Position closed: {reason} "
                        f"(duration={duration/60:.1f}min)")
            return pos

    # ========================================================================
    # QUERIES
    # ========================================================================

    def has_active_position(self) -> bool:
        with self._lock:
            return self.active_position is not None

    def get_position_summary(self) -> Dict:
        with self._lock:
            if not self.active_position:
                return {"status": "NO_POSITION"}

            pos = self.active_position
            executed = sum(1 for t in pos.partial_targets if t.executed)
            total    = len(pos.partial_targets)

            return {
                "status":             pos.state.value,
                "side":               pos.side,
                "entry_price":        pos.entry_price,
                "quantity":           pos.quantity,
                "filled_quantity":    pos.filled_quantity,
                "remaining_quantity": pos.remaining_quantity,
                "stop_loss":          pos.stop_loss,
                "risk_amount":        pos.risk_amount,
                "targets_hit":        f"{executed}/{total}",
                "breakeven_moved":    pos.breakeven_moved,
                "profit_locked_pct":  pos.profit_locked_pct,
                "highest_price":      pos.highest_price,
                "lowest_price":       pos.lowest_price,
                "opened_at":          datetime.fromtimestamp(pos.opened_at).isoformat(),
                "entry_reason":       pos.entry_reason,
            }

    def get_trade_history_summary(self) -> List[Dict]:
        with self._lock:
            return [
                {
                    "side":         p.side,
                    "entry":        p.entry_price,
                    "quantity":     p.quantity,
                    "sl":           p.stop_loss,
                    "close_reason": p.close_reason,
                    "duration_min": round(
                        ((p.closed_at or time.time()) - p.opened_at) / 60, 1
                    ),
                }
                for p in self.position_history
            ]
