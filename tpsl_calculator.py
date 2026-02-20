# ============================================================================
# tpsl_calculator.py
# ============================================================================
"""
TP/SL Calculator - v3 Production Rewrite
==========================================

ROOT CAUSES FIXED vs v2:

NO FALLBACKS:
  - ENABLE_FALLBACK_TP / ENABLE_FALLBACK_SL / ENABLE_ATR_FALLBACK all
    respected from config. When config disables fallbacks, the method
    returns (None, "REJECTED", {}) — caller must handle this cleanly.
  - No silent percentage-based fallback when structure is missing.
    If structure is required and absent, return failure explicitly.

SL VALIDATION:
  - Minimum SL distance enforced from config.MIN_SL_DISTANCE_PCT.
  - Maximum SL distance enforced from config.MAX_SL_DISTANCE_PCT.
  - SL distance that fails max is REJECTED (not widened).

TP VALIDATION:
  - Every TP target checked for minimum RR against config.MIN_RISK_REWARD_RATIO.
  - Targets that fail minimum RR are removed from the list — not the whole
    list, just the invalid target. If no valid targets remain, return failure.
  - Fee cost is deducted from effective RR to ensure real-world profitability.

STRUCTURE DERIVATION:
  - SL and TP derived from the same TriggerContext object passed in from
    strategy._calculate_entry_sl_tp. No mix of levels from different sources.
  - OB-derived SL uses ob.low (long) / ob.high (short) — the exact same
    object reference used in entry evaluation.

TICK ROUNDING:
  - All output prices rounded to config.TICK_SIZE precision.
  - SL is always rounded AWAY from entry (wider, not tighter) to avoid
    exchange rejection on too-close stops.
"""

import logging
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass
from enum import Enum

import config

logger = logging.getLogger(__name__)


# ============================================================================
# ENUMS
# ============================================================================

class TPCalcMethod(Enum):
    ICT_STRUCTURE = "ICT_STRUCTURE"
    RISK_REWARD   = "RISK_REWARD"
    FIBONACCI     = "FIBONACCI"
    ATR_BASED     = "ATR_BASED"


class SLCalcMethod(Enum):
    STRUCTURE_BASED   = "STRUCTURE_BASED"
    LIQUIDITY_SWEEP   = "LIQUIDITY_SWEEP"
    ATR_BASED         = "ATR_BASED"
    PERCENTAGE        = "PERCENTAGE"


@dataclass
class ValidationResult:
    valid:    bool
    errors:   List[str]
    warnings: List[str]

    def __post_init__(self):
        if self.errors   is None: self.errors   = []
        if self.warnings is None: self.warnings = []


# ============================================================================
# CALCULATOR
# ============================================================================

class TPSLCalculator:
    """
    Calculates and validates SL and TP levels.

    All methods return a tuple of (price_or_list, method_used, metadata).
    When a calculation fails and no fallback is configured, returns
    (None, "REJECTED", {"reason": "..."}).
    Caller is responsible for aborting entry on rejection.
    """

    # CoinSwitch fees
    TAKER_FEE_PCT      = 0.0006
    GST_PCT            = 0.18
    TOTAL_FEE_PCT      = TAKER_FEE_PCT * (1 + GST_PCT)
    ROUND_TRIP_FEE_PCT = TOTAL_FEE_PCT * 2   # ~0.1416%

    def __init__(self):
        self.min_rr        = config.MIN_RISK_REWARD_RATIO
        self.target_rr     = config.TARGET_RISK_REWARD_RATIO
        self.max_rr        = config.MAX_RR_RATIO
        self.min_sl_pct    = config.MIN_SL_DISTANCE_PCT
        self.max_sl_pct    = config.MAX_SL_DISTANCE_PCT
        self.tick          = config.TICK_SIZE

        logger.info(f"✅ TPSLCalculator v3 initialized")
        logger.info(f"   Min RR: {self.min_rr} | Target RR: {self.target_rr}")
        logger.info(f"   SL range: {self.min_sl_pct*100:.2f}%–{self.max_sl_pct*100:.2f}%")
        logger.info(f"   Round-trip fee: {self.ROUND_TRIP_FEE_PCT*100:.4f}%")

    # ────────────────────────────────────────────────────────────────────────
    # SL CALCULATION
    # ────────────────────────────────────────────────────────────────────────

    def calculate_stop_loss_long(
        self,
        entry_price:     float,
        swing_low:       Optional[float] = None,
        order_block_low: Optional[float] = None,
        liquidity_pool:  Optional[float] = None,
        atr:             Optional[float] = None,
        method: SLCalcMethod = SLCalcMethod.STRUCTURE_BASED,
    ) -> Tuple[Optional[float], str, Dict]:
        """
        Calculate stop loss for LONG position.

        Returns:
            (stop_loss_price, method_used, metadata)
            stop_loss_price is None if calculation fails and fallbacks disabled.
        """
        logger.info("─" * 60)
        logger.info(f"SL CALC (LONG) | Entry=${entry_price:.2f} | Method={method.value}")
        meta = {"method": method.value, "entry": entry_price}

        buffer = config.SL_BUFFER_TICKS * self.tick

        try:
            stop_loss = None

            # ── Structure-based ──────────────────────────────────────────────
            if method == SLCalcMethod.STRUCTURE_BASED:
                candidates = []
                if swing_low and swing_low < entry_price:
                    candidates.append(("SWING_LOW", swing_low))
                if order_block_low and order_block_low < entry_price:
                    candidates.append(("OB_LOW", order_block_low))

                if candidates:
                    label, base = min(candidates, key=lambda x: x[1])
                    stop_loss = base - buffer
                    meta["base_structure"] = base
                    meta["structure_type"] = label
                    meta["buffer"]         = buffer
                    logger.info(f"   Structure SL: ${stop_loss:.2f} "
                                f"({label}=${base:.2f} - buffer)")
                else:
                    if not config.ENABLE_FALLBACK_SL:
                        logger.warning("⚠️ No structure for LONG SL, fallbacks disabled")
                        return None, "REJECTED", {"reason": "NO_STRUCTURE_NO_FALLBACK"}
                    logger.warning("⚠️ No structure for LONG SL, falling to ATR")
                    method = SLCalcMethod.ATR_BASED

            # ── Liquidity sweep ──────────────────────────────────────────────
            if method == SLCalcMethod.LIQUIDITY_SWEEP:
                if liquidity_pool and liquidity_pool < entry_price:
                    stop_loss = liquidity_pool - buffer
                    meta["liquidity_pool"] = liquidity_pool
                    logger.info(f"   Liquidity Sweep SL: ${stop_loss:.2f}")
                else:
                    if not config.ENABLE_FALLBACK_SL:
                        return None, "REJECTED", {"reason": "NO_LIQ_POOL_NO_FALLBACK"}
                    method = SLCalcMethod.STRUCTURE_BASED
                    return self.calculate_stop_loss_long(
                        entry_price, swing_low, order_block_low, None, atr,
                        SLCalcMethod.STRUCTURE_BASED
                    )

            # ── ATR-based ────────────────────────────────────────────────────
            if method == SLCalcMethod.ATR_BASED:
                if not config.ENABLE_ATR_FALLBACK:
                    return None, "REJECTED", {"reason": "ATR_FALLBACK_DISABLED"}
                if atr and atr > 0:
                    mult      = config.ATR_TRAILING_MULTIPLIER
                    stop_loss = entry_price - (atr * mult)
                    meta["atr"]        = atr
                    meta["multiplier"] = mult
                    logger.info(f"   ATR SL: ${stop_loss:.2f} "
                                f"(ATR={atr:.2f} × {mult})")
                else:
                    if not config.ENABLE_FALLBACK_SL:
                        return None, "REJECTED", {"reason": "NO_ATR_NO_FALLBACK"}
                    method = SLCalcMethod.PERCENTAGE

            # ── Percentage ───────────────────────────────────────────────────
            if method == SLCalcMethod.PERCENTAGE:
                if not config.ENABLE_FALLBACK_SL:
                    return None, "REJECTED", {"reason": "PERCENTAGE_FALLBACK_DISABLED"}
                pct       = self.min_sl_pct * 1.5   # slightly wider than minimum
                stop_loss = entry_price * (1 - pct)
                meta["sl_pct"] = pct
                logger.info(f"   Percentage SL: ${stop_loss:.2f} ({pct*100:.2f}%)")

            if stop_loss is None:
                return None, "REJECTED", {"reason": "NO_VALID_METHOD"}

            # ── Validation ───────────────────────────────────────────────────
            validated_sl = self._validate_and_round_sl(
                entry_price, stop_loss, "long"
            )
            if validated_sl is None:
                return None, "REJECTED", {
                    "reason": "VALIDATION_FAILED",
                    "attempted": stop_loss,
                }

            dist_pct = (entry_price - validated_sl) / entry_price
            logger.info(f"   ✅ LONG SL: ${validated_sl:.2f} "
                        f"(dist={dist_pct*100:.2f}%)")
            meta["final_sl"]  = validated_sl
            meta["dist_pct"]  = dist_pct

            return validated_sl, meta.get("method", method.value), meta

        except Exception as e:
            logger.error(f"❌ calculate_stop_loss_long error: {e}", exc_info=True)
            return None, "ERROR", {"reason": str(e)}

    def calculate_stop_loss_short(
        self,
        entry_price:      float,
        swing_high:       Optional[float] = None,
        order_block_high: Optional[float] = None,
        liquidity_pool:   Optional[float] = None,
        atr:              Optional[float] = None,
        method: SLCalcMethod = SLCalcMethod.STRUCTURE_BASED,
    ) -> Tuple[Optional[float], str, Dict]:
        """
        Calculate stop loss for SHORT position.
        Mirror logic of calculate_stop_loss_long.
        """
        logger.info("─" * 60)
        logger.info(f"SL CALC (SHORT) | Entry=${entry_price:.2f} | Method={method.value}")
        meta   = {"method": method.value, "entry": entry_price}
        buffer = config.SL_BUFFER_TICKS * self.tick

        try:
            stop_loss = None

            if method == SLCalcMethod.STRUCTURE_BASED:
                candidates = []
                if swing_high and swing_high > entry_price:
                    candidates.append(("SWING_HIGH", swing_high))
                if order_block_high and order_block_high > entry_price:
                    candidates.append(("OB_HIGH", order_block_high))

                if candidates:
                    label, base = max(candidates, key=lambda x: x[1])
                    stop_loss = base + buffer
                    meta["base_structure"] = base
                    meta["structure_type"] = label
                    meta["buffer"]         = buffer
                    logger.info(f"   Structure SL: ${stop_loss:.2f} "
                                f"({label}=${base:.2f} + buffer)")
                else:
                    if not config.ENABLE_FALLBACK_SL:
                        return None, "REJECTED", {"reason": "NO_STRUCTURE_NO_FALLBACK"}
                    method = SLCalcMethod.ATR_BASED

            if method == SLCalcMethod.LIQUIDITY_SWEEP:
                if liquidity_pool and liquidity_pool > entry_price:
                    stop_loss = liquidity_pool + buffer
                    meta["liquidity_pool"] = liquidity_pool
                    logger.info(f"   Liquidity Sweep SL: ${stop_loss:.2f}")
                else:
                    if not config.ENABLE_FALLBACK_SL:
                        return None, "REJECTED", {"reason": "NO_LIQ_POOL_NO_FALLBACK"}
                    return self.calculate_stop_loss_short(
                        entry_price, swing_high, order_block_high, None, atr,
                        SLCalcMethod.STRUCTURE_BASED
                    )

            if method == SLCalcMethod.ATR_BASED:
                if not config.ENABLE_ATR_FALLBACK:
                    return None, "REJECTED", {"reason": "ATR_FALLBACK_DISABLED"}
                if atr and atr > 0:
                    mult      = config.ATR_TRAILING_MULTIPLIER
                    stop_loss = entry_price + (atr * mult)
                    meta["atr"]        = atr
                    meta["multiplier"] = mult
                else:
                    if not config.ENABLE_FALLBACK_SL:
                        return None, "REJECTED", {"reason": "NO_ATR_NO_FALLBACK"}
                    method = SLCalcMethod.PERCENTAGE

            if method == SLCalcMethod.PERCENTAGE:
                if not config.ENABLE_FALLBACK_SL:
                    return None, "REJECTED", {"reason": "PERCENTAGE_FALLBACK_DISABLED"}
                pct       = self.min_sl_pct * 1.5
                stop_loss = entry_price * (1 + pct)
                meta["sl_pct"] = pct

            if stop_loss is None:
                return None, "REJECTED", {"reason": "NO_VALID_METHOD"}

            validated_sl = self._validate_and_round_sl(
                entry_price, stop_loss, "short"
            )
            if validated_sl is None:
                return None, "REJECTED", {
                    "reason": "VALIDATION_FAILED",
                    "attempted": stop_loss,
                }

            dist_pct = (validated_sl - entry_price) / entry_price
            logger.info(f"   ✅ SHORT SL: ${validated_sl:.2f} "
                        f"(dist={dist_pct*100:.2f}%)")
            meta["final_sl"] = validated_sl
            meta["dist_pct"] = dist_pct

            return validated_sl, meta.get("method", method.value), meta

        except Exception as e:
            logger.error(f"❌ calculate_stop_loss_short error: {e}", exc_info=True)
            return None, "ERROR", {"reason": str(e)}

    # ────────────────────────────────────────────────────────────────────────
    # TP CALCULATION
    # ────────────────────────────────────────────────────────────────────────

    def calculate_take_profit_targets(
        self,
        entry_price:    float,
        stop_loss:      float,
        side:           str,
        min_rr:         float       = None,
        max_rr:         float       = None,
        targets_count:  int         = 2,
        structures:     List[Dict]  = None,
    ) -> Tuple[List[Dict], str, Dict]:
        """
        Calculate TP targets with mandatory RR validation.
        Fee cost is deducted from effective RR — target must be profitable
        after fees, not just before.

        Returns:
            (targets_list, method_used, metadata)
            targets_list is [] if no valid targets can be computed.

        Each target dict:
            {"price": float, "percentage": float, "rr": float,
             "rr_after_fees": float, "type": str}
        """
        if min_rr is None: min_rr = self.min_rr
        if max_rr is None: max_rr = self.target_rr

        logger.info("─" * 60)
        logger.info(f"TP CALC ({side.upper()}) | Entry=${entry_price:.2f} "
                    f"SL=${stop_loss:.2f} | RR={min_rr}–{max_rr}")

        risk = abs(entry_price - stop_loss)
        if risk <= 0:
            logger.error(f"❌ Invalid risk: {risk}")
            return [], "INVALID_RISK", {}

        meta = {
            "risk":    risk,
            "min_rr":  min_rr,
            "max_rr":  max_rr,
            "fee_pct": self.ROUND_TRIP_FEE_PCT,
        }

        try:
            targets = []

            # ── Method 1: ICT structures ─────────────────────────────────────
            if structures:
                valid_structures = []
                for s in structures:
                    sp = s.get("price", 0)
                    if not sp:
                        continue
                    if side == "long" and sp > entry_price:
                        rr = (sp - entry_price) / risk
                    elif side == "short" and sp < entry_price:
                        rr = (entry_price - sp) / risk
                    else:
                        continue

                    rr_after_fees = rr - (self.ROUND_TRIP_FEE_PCT / risk * entry_price)
                    if rr_after_fees < min_rr:
                        logger.debug(f"   Structure @ {sp:.2f} RR={rr:.2f} "
                                     f"(after fees={rr_after_fees:.2f}) — below min {min_rr}")
                        continue
                    if rr > max_rr * 1.5:
                        continue

                    valid_structures.append({
                        "price":         sp,
                        "rr":            rr,
                        "rr_after_fees": rr_after_fees,
                        "type":          s.get("type", "STRUCTURE"),
                        "priority":      s.get("priority", 50),
                    })

                if valid_structures:
                    valid_structures.sort(key=lambda x: (-x["priority"], x["rr"]))
                    targets = self._build_targets_from_structures(
                        valid_structures, targets_count
                    )
                    meta["method"] = "ICT_STRUCTURE"
                    logger.info(f"   ICT structures: {len(targets)} targets")

            # ── Method 2: RR-based ───────────────────────────────────────────
            if not targets:
                if not config.ENABLE_FALLBACK_TP and structures is not None:
                    # structures were provided but none valid
                    logger.warning("⚠️ No valid ICT TP structures, fallback disabled")
                    return [], "REJECTED", {"reason": "NO_VALID_STRUCTURES_NO_FALLBACK"}

                targets = self._build_rr_targets(
                    entry_price, stop_loss, side, risk,
                    min_rr, max_rr, targets_count
                )
                meta["method"] = "RISK_REWARD"

            # ── Validate all targets ─────────────────────────────────────────
            validated = []
            for t in targets:
                v = self._validate_tp_target(entry_price, t["price"],
                                              stop_loss, side, risk)
                if v.valid:
                    validated.append(t)
                else:
                    logger.warning(f"   TP @ {t['price']:.2f} rejected: {v.errors}")

            if not validated:
                logger.error("❌ No valid TP targets after validation")
                return [], "VALIDATION_FAILED", meta

            # ── Log final targets ────────────────────────────────────────────
            logger.info(f"   ✅ TP Targets ({side.upper()}):")
            for i, t in enumerate(validated, 1):
                logger.info(f"   TP{i}: ${t['price']:.2f} "
                            f"({t['percentage']:.0f}%) "
                            f"RR={t['rr']:.2f} "
                            f"RR_net={t.get('rr_after_fees', t['rr']):.2f}")

            return validated, meta.get("method", "UNKNOWN"), meta

        except Exception as e:
            logger.error(f"❌ calculate_take_profit_targets error: {e}",
                         exc_info=True)
            return [], "ERROR", {"reason": str(e)}

    # ────────────────────────────────────────────────────────────────────────
    # INTERNAL HELPERS
    # ────────────────────────────────────────────────────────────────────────

    def _build_targets_from_structures(
        self, valid_structs: List[Dict], count: int
    ) -> List[Dict]:
        """Distribute percentages across up to `count` structure targets."""
        n = min(count, len(valid_structs))
        if n == 1:
            pcts = [100.0]
        elif n == 2:
            pcts = [70.0, 30.0]
        else:
            pcts = [50.0, 30.0, 20.0]

        targets = []
        for i in range(n):
            s = valid_structs[i] if i < len(valid_structs) else valid_structs[-1]
            targets.append({
                "price":         s["price"],
                "percentage":    pcts[i],
                "rr":            s["rr"],
                "rr_after_fees": s.get("rr_after_fees", s["rr"]),
                "type":          s["type"],
            })
        return targets

    def _build_rr_targets(
        self, entry: float, sl: float, side: str,
        risk: float, min_rr: float, max_rr: float, count: int
    ) -> List[Dict]:
        """Build RR-based targets with fee-adjusted RR logged."""
        if count == 1:
            rr_levels = [(min_rr + max_rr) / 2]
            pcts      = [100.0]
        elif count == 2:
            rr_levels = [min_rr, max_rr]
            pcts      = [70.0, 30.0]
        else:
            mid = (min_rr + max_rr) / 2
            rr_levels = [min_rr, mid, max_rr]
            pcts      = [50.0, 30.0, 20.0]

        targets = []
        for rr, pct in zip(rr_levels, pcts):
            if side == "long":
                price = entry + risk * rr
            else:
                price = entry - risk * rr

            price = round(round(price / self.tick) * self.tick, 2)
            fee_cost_in_rr = (self.ROUND_TRIP_FEE_PCT * entry) / risk
            rr_net = rr - fee_cost_in_rr

            targets.append({
                "price":         price,
                "percentage":    pct,
                "rr":            rr,
                "rr_after_fees": rr_net,
                "type":          "RR_BASED",
            })
        return targets

    def _validate_and_round_sl(
        self, entry: float, sl: float, side: str
    ) -> Optional[float]:
        """
        Validate SL direction and distance.
        Rounds SL AWAY from entry (wider) to avoid exchange rejection.
        Returns None if validation fails.
        """
        # Direction
        if side == "long" and sl >= entry:
            logger.error(f"SL validation failed: LONG sl={sl:.2f} >= entry={entry:.2f}")
            return None
        if side == "short" and sl <= entry:
            logger.error(f"SL validation failed: SHORT sl={sl:.2f} <= entry={entry:.2f}")
            return None

        dist_pct = abs(sl - entry) / entry

        # Too close
        if dist_pct < self.min_sl_pct:
            logger.warning(f"SL too close ({dist_pct*100:.3f}% < {self.min_sl_pct*100:.3f}%)")
            # Widen to minimum
            if side == "long":
                sl = entry * (1 - self.min_sl_pct)
            else:
                sl = entry * (1 + self.min_sl_pct)
            dist_pct = self.min_sl_pct

        # Too wide
        if dist_pct > self.max_sl_pct:
            logger.error(f"SL too wide ({dist_pct*100:.2f}% > {self.max_sl_pct*100:.2f}%) — REJECTED")
            return None

        # Round away from entry
        ticks = sl / self.tick
        if side == "long":
            sl = round(int(ticks) * self.tick, 2)       # floor = further below
        else:
            sl = round((int(ticks) + 1) * self.tick, 2)  # ceil  = further above

        return sl

    def _validate_tp_target(
        self, entry: float, tp: float, sl: float,
        side: str, risk: float
    ) -> ValidationResult:
        """
        Validate a single TP target.
        Checks direction, minimum RR (gross and fee-adjusted).
        """
        result = ValidationResult(valid=True, errors=[], warnings=[])

        if side == "long" and tp <= entry:
            result.valid = False
            result.errors.append(
                f"LONG TP must be above entry: tp={tp:.2f} entry={entry:.2f}"
            )
        if side == "short" and tp >= entry:
            result.valid = False
            result.errors.append(
                f"SHORT TP must be below entry: tp={tp:.2f} entry={entry:.2f}"
            )

        if risk > 0:
            reward = abs(tp - entry)
            rr     = reward / risk

            # Fee-adjusted RR
            fee_cost_rr = (self.ROUND_TRIP_FEE_PCT * entry) / risk
            rr_net      = rr - fee_cost_rr

            if rr < self.min_rr:
                result.valid = False
                result.errors.append(
                    f"RR too low: {rr:.2f} < {self.min_rr} (min)"
                )
            elif rr_net < self.min_rr * 0.90:
                # After fees, effective RR is less than 90% of minimum
                result.valid = False
                result.errors.append(
                    f"Net RR after fees too low: {rr_net:.2f}"
                )
            elif rr > self.max_rr:
                result.warnings.append(
                    f"RR very high: {rr:.2f} > {self.max_rr}"
                )

        return result

    # ────────────────────────────────────────────────────────────────────────
    # CONVENIENCE: validate a complete SL/TP pair
    # ────────────────────────────────────────────────────────────────────────

    def validate_sl_tp_pair(
        self,
        entry:  float,
        sl:     float,
        tp:     float,
        side:   str,
    ) -> ValidationResult:
        """
        Quick validity check for an SL/TP pair before order placement.
        Used by strategy as final gate before _place_entry().
        """
        result = ValidationResult(valid=True, errors=[], warnings=[])

        if side == "long":
            if sl >= entry:
                result.valid = False
                result.errors.append(f"LONG SL {sl:.2f} >= entry {entry:.2f}")
            if tp <= entry:
                result.valid = False
                result.errors.append(f"LONG TP {tp:.2f} <= entry {entry:.2f}")
        else:
            if sl <= entry:
                result.valid = False
                result.errors.append(f"SHORT SL {sl:.2f} <= entry {entry:.2f}")
            if tp >= entry:
                result.valid = False
                result.errors.append(f"SHORT TP {tp:.2f} >= entry {entry:.2f}")

        if not result.errors:
            risk   = abs(entry - sl)
            reward = abs(tp - entry)
            rr     = reward / risk if risk > 0 else 0

            if rr < self.min_rr:
                result.valid = False
                result.errors.append(
                    f"RR {rr:.2f} < minimum {self.min_rr}"
                )

            sl_pct = abs(entry - sl) / entry
            if sl_pct < self.min_sl_pct:
                result.valid = False
                result.errors.append(
                    f"SL distance {sl_pct*100:.3f}% < min {self.min_sl_pct*100:.3f}%"
                )
            if sl_pct > self.max_sl_pct:
                result.valid = False
                result.errors.append(
                    f"SL distance {sl_pct*100:.2f}% > max {self.max_sl_pct*100:.2f}%"
                )

        return result
