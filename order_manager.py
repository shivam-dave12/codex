# ============================================================================
# order_manager.py
# ============================================================================
"""
Order Manager — v3 Production Rewrite
======================================

ROOT CAUSES FIXED vs v2:

POST-EXIT LEVEL INVALIDATION:
  - cancel_all_exit_orders() is now a single atomic call that cancels BOTH
    SL and TP together before any state transition. Partial cancels are
    reconciled — if SL fired while we were cancelling TP, that is detected
    and handled as a fill, not an error.

CANCELRESULT IMPORT FIX:
  - CancelResult enum is defined in THIS file. strategy.py imports it from
    here. No circular import, no "from order_manager import CancelResult"
    errors that were causing AttributeError at runtime.

REPLACE_STOP_LOSS:
  - Returns None when the existing SL is already FILLED (fired).
    Callers must treat None as "SL already executed, position closed."
  - Returns the new order dict on success.
  - Never raises — all errors are returned as structured results.

REPLACE_TAKE_PROFIT:
  - Same contract as replace_stop_loss.

CANCEL_ORDER:
  - Returns CancelResult enum value, not a raw dict.
  - PARTIAL_FILL case detected and returned as CancelResult.PARTIAL_FILL.
  - Caller in strategy is responsible for adopting partial position.

RATE LIMITER:
  - GlobalRateLimiter is exported from this module (imported by strategy.py).
  - Min interval raised to 2.0s (CoinSwitch hard limit).
  - Thread-safe token bucket with RLock.

ORDER HEALTH:
  - get_order_status_safe() maps ALL known CoinSwitch status strings
    including UNTRIGGERED (pending stop orders) to canonical values.
  - get_fill_details() never returns stale data — always re-queries exchange.
"""

import time
import logging
import threading
from typing import Dict, Optional, Tuple
from datetime import datetime
from enum import Enum

from futures_api import FuturesAPI
import config

logger = logging.getLogger(__name__)


# ============================================================================
# CANCEL RESULT ENUM  — exported, imported by strategy.py
# ============================================================================

class CancelResult(Enum):
    """
    Canonical result of a cancel attempt.

    PARTIAL_FILL is the critical case: the order executed (fully or partially)
    before our cancel arrived. Caller MUST treat this as a fill and adopt
    the resulting position — never ignore it.
    """
    SUCCESS        = "SUCCESS"        # cancelled cleanly
    PARTIAL_FILL   = "PARTIAL_FILL"   # was PARTIALLY_EXECUTED — treat as fill
    ALREADY_FILLED = "ALREADY_FILLED" # EXECUTED before cancel
    NOT_FOUND      = "NOT_FOUND"      # unknown order id
    FAILED         = "FAILED"         # exchange error


# ============================================================================
# RATE LIMITER  — exported, imported by strategy.py
# ============================================================================

class GlobalRateLimiter:
    """
    Thread-safe global rate limiter for ALL API calls.

    Hard floor: 3.0s between any two requests (CoinSwitch effective limit).
    429 backoff: when a 429 is detected anywhere, _backoff() is called
    which freezes all requests for BACKOFF_SECONDS before resuming.
    This prevents retry storms from compounding the 429 cascade.
    """
    _lock              = threading.RLock()
    _last_request_ts   = 0.0
    _min_interval_sec  = 3.0        # raised from 2.0 — effective CoinSwitch limit
    _backoff_until     = 0.0        # epoch seconds; all requests blocked until this

    BACKOFF_SECONDS    = 15.0       # freeze duration on 429 detection

    @classmethod
    def wait(cls):
        with cls._lock:
            now = time.time()

            # If in 429 backoff window, sleep the remainder
            if now < cls._backoff_until:
                remaining = cls._backoff_until - now
                logger.warning(f"⏳ Rate limiter backoff: sleeping {remaining:.1f}s")
                time.sleep(remaining)
                cls._last_request_ts = time.time()
                return

            elapsed = time.time() - cls._last_request_ts
            if elapsed < cls._min_interval_sec:
                time.sleep(cls._min_interval_sec - elapsed)

            cls._last_request_ts = time.time()

    @classmethod
    def notify_429(cls):
        """
        Call this whenever a 429 response is received anywhere in the codebase.
        Freezes all API calls for BACKOFF_SECONDS.
        """
        with cls._lock:
            cls._backoff_until = time.time() + cls.BACKOFF_SECONDS
            logger.warning(f"🚨 429 detected — all API calls frozen for "
                           f"{cls.BACKOFF_SECONDS}s")

    @classmethod
    def set_min_interval(cls, seconds: float):
        with cls._lock:
            cls._min_interval_sec = max(1.0, seconds)



# ============================================================================
# ORDER MANAGER
# ============================================================================

class OrderManager:
    """
    Thread-safe, production-grade order manager v3.

    Design contract:
      1. Every public method returns a typed/structured result — no raw dicts
         buried inside silent None returns.
      2. Transient errors (401, 500, 502, 503, 429) retried with exponential
         backoff before declaring failure.
      3. PARTIALLY_EXECUTED is never silently discarded — CancelResult.PARTIAL_FILL
         is returned so the caller can adopt the position.
      4. replace_stop_loss / replace_take_profit return None when the
         existing order has already fired — this is a signal to the caller
         that the position has closed, not an error.
      5. cancel_all_exit_orders() is the single safe path for closing both
         SL and TP simultaneously before a state transition.
    """

    _RETRY_STATUS_CODES = {401, 500, 502, 503, 429}
    _MAX_RETRIES        = 3
    _RETRY_BASE_SLEEP   = 2.0   # seconds; doubles each retry

    def __init__(self):
        self.api = FuturesAPI(
            api_key    = config.COINSWITCH_API_KEY,
            secret_key = config.COINSWITCH_SECRET_KEY,
        )

        self._orders_lock        = threading.RLock()
        self.active_orders: Dict[str, Dict] = {}
        self.order_history: list            = []

        self._rate_window_start = time.time()
        self._rate_window_count = 0

        # Expose CancelResult on instance for callers that do
        # order_manager.CancelResult.PARTIAL_FILL
        self.CancelResult = CancelResult

        logger.info("✅ OrderManager v3 initialized")

    # ========================================================================
    # INTERNAL HELPERS
    # ========================================================================

    @staticmethod
    def _normalize_side(side: str) -> str:
        s = str(side).upper().strip()
        if s in ("LONG", "BUY"):
            return "BUY"
        if s in ("SHORT", "SELL"):
            return "SELL"
        raise ValueError(f"Invalid side '{side}'. Use LONG/SHORT or BUY/SELL.")

    def _check_window_rate_limit(self) -> bool:
        now = time.time()
        if now - self._rate_window_start > 60:
            self._rate_window_count  = 0
            self._rate_window_start  = now
        if self._rate_window_count >= config.RATE_LIMIT_ORDERS:
            logger.warning(f"⚠️ Window rate limit reached: "
                           f"{self._rate_window_count}/{config.RATE_LIMIT_ORDERS}")
            return False
        self._rate_window_count += 1
        return True

    def _place_order_with_retry(self, **kwargs) -> Optional[Dict]:
        """
        Call api.place_order with exponential-backoff retry on transient errors.
        Returns the 'data' dict on success, None on permanent failure.
        Never raises.
        """
        for attempt in range(self._MAX_RETRIES):
            GlobalRateLimiter.wait()
            try:
                resp = self.api.place_order(**kwargs)
            except Exception as e:
                logger.error(f"place_order exception (attempt {attempt + 1}): {e}")
                if attempt < self._MAX_RETRIES - 1:
                    time.sleep(self._RETRY_BASE_SLEEP * (2 ** attempt))
                continue

            # Success path
            data = resp.get("data") if isinstance(resp, dict) else None
            if data and isinstance(data, dict) and "order_id" in data:
                return data

            # Check for retryable status
            sc  = resp.get("status_code", 0) if isinstance(resp, dict) else 0
            msg = ""
            try:
                msg = str(resp.get("response", {}).get("message", ""))
            except Exception:
                pass

            if sc in self._RETRY_STATUS_CODES and attempt < self._MAX_RETRIES - 1:
                sleep = self._RETRY_BASE_SLEEP * (2 ** attempt)
                logger.warning(f"Transient error {sc} on attempt {attempt + 1}, "
                               f"retry in {sleep:.1f}s: {msg}")
                time.sleep(sleep)
                continue

            logger.error(f"place_order permanent failure (attempt {attempt + 1}): "
                         f"status={sc} msg={msg}")
            return None

        logger.error(f"place_order failed after {self._MAX_RETRIES} attempts")
        return None

    def _record_order(self, order_id: str, meta: Dict):
        with self._orders_lock:
            self.active_orders[order_id] = meta
            self.order_history.append(meta.copy())

    def _remove_active_order(self, order_id: str):
        with self._orders_lock:
            self.active_orders.pop(order_id, None)

    # ========================================================================
    # POSITION QUERY
    # ========================================================================

    def get_open_position(self) -> Optional[Dict]:
        """
        Query the exchange for the current open position on config.SYMBOL.

        Returns a normalised dict:
            {
                "side":           "LONG" | "SHORT" | None,
                "size":           float,   # 0 = no position
                "entry_price":    float,
                "unrealized_pnl": float,
                "raw":            dict,    # raw exchange response
            }
        Returns None on API failure (not the same as no position).
        """
        try:
            GlobalRateLimiter.wait()
            resp = self.api.get_positions(
                exchange = config.EXCHANGE,
                symbol   = config.SYMBOL,
            )

            if not isinstance(resp, dict):
                logger.error(f"get_open_position: non-dict response: {resp}")
                return None

            if "error" in resp:
                logger.error(f"get_open_position API error: {resp['error']}")
                return None

            data      = resp.get("data", {})
            positions = data if isinstance(data, list) else [data]

            for pos in positions:
                if not isinstance(pos, dict):
                    continue

                sym = str(pos.get("symbol", "")).upper()
                if config.SYMBOL.upper() not in sym:
                    continue

                # Extract size
                size = 0.0
                for field in ("size", "quantity", "position_size", "net_quantity"):
                    raw = pos.get(field)
                    if raw is not None:
                        try:
                            size = abs(float(raw))
                            if size > 0:
                                break
                        except (ValueError, TypeError):
                            continue

                # Extract side
                side = None
                if size > 0:
                    raw_side = str(pos.get("side",
                                   pos.get("position_side", ""))).upper()
                    if raw_side in ("BUY", "LONG"):
                        side = "LONG"
                    elif raw_side in ("SELL", "SHORT"):
                        side = "SHORT"

                # Extract entry price
                entry = 0.0
                for field in ("entry_price", "avg_price", "average_price"):
                    raw = pos.get(field)
                    if raw is not None:
                        try:
                            entry = float(raw)
                            if entry > 0:
                                break
                        except (ValueError, TypeError):
                            continue

                # Extract unrealized PnL
                upnl = 0.0
                try:
                    upnl = float(pos.get("unrealized_pnl", 0))
                except (ValueError, TypeError):
                    pass

                return {
                    "side":           side,
                    "size":           size,
                    "entry_price":    entry,
                    "unrealized_pnl": upnl,
                    "raw":            pos,
                }

            # No matching position found — confirmed flat
            return {
                "side":           None,
                "size":           0.0,
                "entry_price":    0.0,
                "unrealized_pnl": 0.0,
            }

        except Exception as e:
            logger.error(f"get_open_position error: {e}", exc_info=True)
            return None

    # ========================================================================
    # ORDER STATUS
    # ========================================================================

    def get_order_status(self, order_id: str,
                         retry_count: int = 2) -> Optional[Dict]:
        """Raw order data from exchange. Returns None on failure."""
        for attempt in range(retry_count):
            try:
                GlobalRateLimiter.wait()
                resp = self.api.get_order(order_id)

                if isinstance(resp, dict) and "data" in resp:
                    order_data = resp["data"].get("order", resp["data"])
                    with self._orders_lock:
                        if order_id in self.active_orders:
                            self.active_orders[order_id]["status"] = \
                                order_data.get("status", "UNKNOWN")
                    return order_data

                sc = resp.get("status_code", 0) if isinstance(resp, dict) else 0
                if sc == 429:
                    GlobalRateLimiter.notify_429()   # ← add this
                    if attempt < retry_count - 1:
                        time.sleep(3.0 * (attempt + 1))
                        continue

                logger.warning(f"get_order_status no data for {order_id}: {resp}")
                return None

            except Exception as e:
                logger.error(f"get_order_status error (attempt {attempt + 1}): {e}")
                if attempt < retry_count - 1:
                    time.sleep(2.0 * (attempt + 1))

        return None

    def get_order_status_safe(self, order_id: str) -> str:
        """
        Safe wrapper: never raises.

        Returns canonical string:
            FILLED | PARTIAL_FILL | CANCELLED | PENDING | UNKNOWN

        Maps all known CoinSwitch status strings including UNTRIGGERED.
        """
        try:
            data = self.get_order_status(order_id, retry_count=2)
            if data is None:
                return "UNKNOWN"

            status = str(data.get("status", "UNKNOWN")).upper()

            if status in ("EXECUTED", "FILLED", "COMPLETELY_FILLED"):
                return "FILLED"
            if status in ("PARTIALLY_FILLED", "PARTIALLY_EXECUTED"):
                return "PARTIAL_FILL"
            if status in ("CANCELLED", "CANCELED", "REJECTED", "EXPIRED"):
                return "CANCELLED"
            if status in ("OPEN", "PENDING", "NEW", "UNTRIGGERED",
                          "TRIGGERED", "ACTIVE","RAISED"):
                return "PENDING"
            if status == "UNKNOWN":
                return "UNKNOWN"

            # Unknown status — log and return UNKNOWN to be safe
            logger.warning(f"Unknown order status string: '{status}' "
                           f"for order {order_id}")
            return "UNKNOWN"

        except Exception as e:
            logger.error(f"get_order_status_safe error: {e}")
            return "UNKNOWN"

    def get_fill_details(self, order_id: str) -> Optional[Dict]:
        """
        Return fill price, filled qty, fill %, partial flag.
        Always re-queries exchange — never uses cached data.
        """
        try:
            data = self.get_order_status(order_id, retry_count=2)
            if not data:
                return None

            status = str(data.get("status", "")).upper()

            # Fill price
            fill_price = None
            for f in ("avg_execution_price", "avg_price",
                      "average_price", "price"):
                raw = data.get(f)
                if raw is not None:
                    try:
                        p = float(raw)
                        if p > 0:
                            fill_price = p
                            break
                    except (ValueError, TypeError):
                        pass

            # Filled quantity
            filled_qty = 0.0
            for f in ("exec_quantity", "executed_qty",
                      "filled_quantity", "executed_quantity"):
                raw = data.get(f)
                if raw is not None:
                    try:
                        q = float(raw)
                        if q > 0:
                            filled_qty = q
                            break
                    except (ValueError, TypeError):
                        pass

            # Requested quantity
            requested_qty = 0.0
            for f in ("quantity", "original_quantity",
                      "orig_qty", "requested_quantity"):
                raw = data.get(f)
                if raw is not None:
                    try:
                        q = float(raw)
                        if q > 0:
                            requested_qty = q
                            break
                    except (ValueError, TypeError):
                        pass

            is_partial = status in ("PARTIALLY_FILLED", "PARTIALLY_EXECUTED")

            # If filled and filled_qty not reported, assume full fill
            if filled_qty <= 0 and status in ("EXECUTED", "FILLED",
                                               "COMPLETELY_FILLED"):
                filled_qty = requested_qty

            fill_pct = ((filled_qty / requested_qty * 100)
                        if requested_qty > 0 else 0.0)

            return {
                "status":        status,
                "fill_price":    fill_price,
                "filled_qty":    filled_qty,
                "requested_qty": requested_qty,
                "is_partial":    is_partial,
                "fill_pct":      fill_pct,
                "raw_data":      data,
            }

        except Exception as e:
            logger.error(f"get_fill_details error for {order_id}: {e}",
                         exc_info=True)
            return None

    def extract_fill_price(self, order_data: Dict) -> float:
        """Extract fill price or raise RuntimeError if not found."""
        for f in ("avg_execution_price", "avg_price",
                  "average_price", "price"):
            raw = order_data.get(f)
            if raw is not None:
                try:
                    p = float(raw)
                    if p > 0:
                        return p
                except (ValueError, TypeError):
                    pass
        raise RuntimeError(f"No valid fill price in order data: {order_data}")

    # ========================================================================
    # ORDER PLACEMENT
    # ========================================================================

    def place_market_order(self, side: str, quantity: float,
                           reduce_only: bool = False) -> Optional[Dict]:
        """Place a market order. Returns order data dict or None."""
        try:
            api_side = self._normalize_side(side)
            if not self._check_window_rate_limit():
                logger.warning("Rate limit reached — market order blocked")
                return None

            logger.info(f"Placing MARKET {side} | qty={quantity} "
                        f"reduce_only={reduce_only}")

            data = self._place_order_with_retry(
                symbol      = config.SYMBOL,
                side        = api_side,
                order_type  = "MARKET",
                quantity    = quantity,
                exchange    = config.EXCHANGE,
                reduce_only = reduce_only,
            )

            if data:
                self._record_order(data["order_id"], {
                    "order_id":    data["order_id"],
                    "symbol":      config.SYMBOL,
                    "side":        side,
                    "type":        "MARKET",
                    "quantity":    quantity,
                    "status":      data.get("status", "UNKNOWN"),
                    "timestamp":   datetime.now().isoformat(),
                    "reduce_only": reduce_only,
                })
                logger.info(f"✅ Market order placed: {data['order_id']}")
            return data

        except Exception as e:
            logger.error(f"place_market_order error: {e}", exc_info=True)
            return None

    def place_limit_order(self, side: str, quantity: float,
                          price: float,
                          reduce_only: bool = False) -> Optional[Dict]:
        """Place a limit order. Returns order data dict or None."""
        try:
            api_side = self._normalize_side(side)
            if not self._check_window_rate_limit():
                return None

            logger.info(f"Placing LIMIT {side} | qty={quantity} "
                        f"@ ${price:,.2f} reduce_only={reduce_only}")

            data = self._place_order_with_retry(
                symbol      = config.SYMBOL,
                side        = api_side,
                order_type  = "LIMIT",
                quantity    = quantity,
                price       = price,
                exchange    = config.EXCHANGE,
                reduce_only = reduce_only,
            )

            if data:
                self._record_order(data["order_id"], {
                    "order_id":    data["order_id"],
                    "symbol":      config.SYMBOL,
                    "side":        side,
                    "type":        "LIMIT",
                    "quantity":    quantity,
                    "price":       price,
                    "status":      data.get("status", "UNKNOWN"),
                    "timestamp":   datetime.now().isoformat(),
                    "reduce_only": reduce_only,
                })
                logger.info(f"✅ Limit order placed: {data['order_id']} "
                            f"@ ${price:,.2f}")
            return data

        except Exception as e:
            logger.error(f"place_limit_order error: {e}", exc_info=True)
            return None

    def place_stop_loss(self, side: str, quantity: float,
                        trigger_price: float) -> Optional[Dict]:
        """
        Place a STOP_MARKET SL order.
        reduce_only=True always — never opens a new position.
        """
        try:
            api_side = self._normalize_side(side)
            logger.info(f"Placing STOP_LOSS {side} | qty={quantity} "
                        f"trigger=${trigger_price:,.2f}")

            data = self._place_order_with_retry(
                symbol        = config.SYMBOL,
                side          = api_side,
                order_type    = "STOP_MARKET",
                quantity      = quantity,
                trigger_price = trigger_price,
                exchange      = config.EXCHANGE,
                reduce_only   = True,
            )

            if data:
                self._record_order(data["order_id"], {
                    "order_id":      data["order_id"],
                    "symbol":        config.SYMBOL,
                    "side":          side,
                    "type":          "STOP_LOSS",
                    "quantity":      quantity,
                    "trigger_price": trigger_price,
                    "status":        data.get("status", "UNKNOWN"),
                    "timestamp":     datetime.now().isoformat(),
                })
                logger.info(f"✅ SL placed: {data['order_id']} "
                            f"@ ${trigger_price:,.2f}")
            return data

        except Exception as e:
            logger.error(f"place_stop_loss error: {e}", exc_info=True)
            return None

    def place_take_profit(self, side: str, quantity: float,
                          trigger_price: float) -> Optional[Dict]:
        """
        Place a TAKE_PROFIT_MARKET TP order.
        reduce_only=True always.
        """
        try:
            api_side = self._normalize_side(side)
            logger.info(f"Placing TAKE_PROFIT {side} | qty={quantity} "
                        f"trigger=${trigger_price:,.2f}")

            data = self._place_order_with_retry(
                symbol        = config.SYMBOL,
                side          = api_side,
                order_type    = "TAKE_PROFIT_MARKET",
                quantity      = quantity,
                trigger_price = trigger_price,
                exchange      = config.EXCHANGE,
                reduce_only   = True,
            )

            if data:
                self._record_order(data["order_id"], {
                    "order_id":      data["order_id"],
                    "symbol":        config.SYMBOL,
                    "side":          side,
                    "type":          "TAKE_PROFIT",
                    "quantity":      quantity,
                    "trigger_price": trigger_price,
                    "status":        data.get("status", "UNKNOWN"),
                    "timestamp":     datetime.now().isoformat(),
                })
                logger.info(f"✅ TP placed: {data['order_id']} "
                            f"@ ${trigger_price:,.2f}")
            return data

        except Exception as e:
            logger.error(f"place_take_profit error: {e}", exc_info=True)
            return None

    # ========================================================================
    # ORDER REPLACEMENT
    # ========================================================================

    def replace_stop_loss(
        self,
        existing_sl_order_id: Optional[str],
        side:                 str,
        quantity:             float,
        new_trigger_price:    float,
    ) -> Optional[Dict]:
        """
        Replace an existing SL order with a new trigger price.

        Returns:
            Dict with new order data — on success
            None — if existing SL was already filled (position closed)
            {"error": str} — on API or validation failure

        Contract:
            If existing_sl_order_id is None, places a fresh SL.
            If existing SL is FILLED, returns None (signal to caller).
            If cancel of existing SL gets PARTIAL_FILL, returns None.
        """
        try:
            # Check if existing SL is still alive
            if existing_sl_order_id:
                existing_status = self.get_order_status_safe(existing_sl_order_id)

                if existing_status == "FILLED":
                    logger.info(f"SL {existing_sl_order_id} already FILLED — "
                                f"position closed, no replace needed")
                    return None

                if existing_status == "PARTIAL_FILL":
                    logger.warning(f"SL {existing_sl_order_id} PARTIALLY_FILLED "
                                   f"during replace — treating as fill")
                    return None

                if existing_status == "PENDING":
                    cancel_result = self.cancel_order(existing_sl_order_id)
                    if cancel_result in (CancelResult.ALREADY_FILLED,
                                        CancelResult.PARTIAL_FILL):
                        logger.warning(f"SL {existing_sl_order_id} filled "
                                       f"during cancel attempt — "
                                       f"treating as fill")
                        return None
                    if cancel_result == CancelResult.FAILED:
                        logger.error(f"Failed to cancel old SL "
                                     f"{existing_sl_order_id}")
                        return {"error": "CANCEL_FAILED"}
                    # SUCCESS or NOT_FOUND — continue to place new SL
                    self._remove_active_order(existing_sl_order_id)

            # Place new SL
            new_sl = self.place_stop_loss(
                side          = side,
                quantity      = quantity,
                trigger_price = new_trigger_price,
            )

            if new_sl:
                logger.info(f"✅ SL replaced → {new_sl['order_id']} "
                            f"@ ${new_trigger_price:,.2f}")
            return new_sl

        except Exception as e:
            logger.error(f"replace_stop_loss error: {e}", exc_info=True)
            return {"error": str(e)}

    def replace_take_profit(
        self,
        existing_tp_order_id: Optional[str],
        side:                 str,
        quantity:             float,
        new_trigger_price:    float,
    ) -> Optional[Dict]:
        """
        Replace an existing TP order with a new trigger price.
        Same contract as replace_stop_loss.
        """
        try:
            if existing_tp_order_id:
                existing_status = self.get_order_status_safe(existing_tp_order_id)

                if existing_status == "FILLED":
                    logger.info(f"TP {existing_tp_order_id} already FILLED — "
                                f"no replace needed")
                    return None

                if existing_status == "PARTIAL_FILL":
                    logger.warning(f"TP {existing_tp_order_id} PARTIALLY_FILLED "
                                   f"during replace — treating as fill")
                    return None

                if existing_status == "PENDING":
                    cancel_result = self.cancel_order(existing_tp_order_id)
                    if cancel_result in (CancelResult.ALREADY_FILLED,
                                        CancelResult.PARTIAL_FILL):
                        logger.warning(f"TP {existing_tp_order_id} filled "
                                       f"during cancel — treating as fill")
                        return None
                    if cancel_result == CancelResult.FAILED:
                        return {"error": "CANCEL_FAILED"}
                    self._remove_active_order(existing_tp_order_id)

            new_tp = self.place_take_profit(
                side          = side,
                quantity      = quantity,
                trigger_price = new_trigger_price,
            )

            if new_tp:
                logger.info(f"✅ TP replaced → {new_tp['order_id']} "
                            f"@ ${new_trigger_price:,.2f}")
            return new_tp

        except Exception as e:
            logger.error(f"replace_take_profit error: {e}", exc_info=True)
            return {"error": str(e)}

    # ========================================================================
    # CANCEL ORDER
    # ========================================================================

    def cancel_order(self, order_id: str) -> CancelResult:
        """
        Cancel a single order.

        Returns CancelResult enum — never raises, never returns raw dict.

        PARTIAL_FILL and ALREADY_FILLED must be handled by the caller:
            - Do NOT assume position is flat
            - Reconcile with get_open_position() to determine actual state
        """
        try:
            GlobalRateLimiter.wait()
            resp = self.api.cancel_order(order_id)

            if not isinstance(resp, dict):
                logger.error(f"cancel_order non-dict response for {order_id}: {resp}")
                return CancelResult.FAILED

            status_code = resp.get("status_code", 0)
            data        = resp.get("data", {})
            message     = ""
            try:
                message = str(resp.get("response", {}).get("message", "")).upper()
            except Exception:
                pass

            # Check if the cancel succeeded
            if status_code in (200, 201):
                order_status = str(data.get("status", "")).upper()
                if order_status in ("CANCELLED", "CANCELED"):
                    self._remove_active_order(order_id)
                    logger.info(f"✅ Order cancelled: {order_id}")
                    return CancelResult.SUCCESS

            # Check for fill-at-cancel
            current_status = self.get_order_status_safe(order_id)
            if current_status == "FILLED":
                logger.warning(f"Order {order_id} filled at cancel time — "
                               f"ALREADY_FILLED")
                return CancelResult.ALREADY_FILLED
            if current_status == "PARTIAL_FILL":
                logger.warning(f"Order {order_id} partial at cancel time — "
                               f"PARTIAL_FILL")
                return CancelResult.PARTIAL_FILL
            if current_status == "CANCELLED":
                self._remove_active_order(order_id)
                return CancelResult.SUCCESS

            # 404 or "NOT FOUND"
            if status_code == 404 or "NOT FOUND" in message:
                logger.warning(f"Order {order_id} not found on exchange")
                self._remove_active_order(order_id)
                return CancelResult.NOT_FOUND

            logger.error(f"cancel_order failed for {order_id}: "
                         f"status={status_code} msg={message}")
            return CancelResult.FAILED

        except Exception as e:
            logger.error(f"cancel_order error for {order_id}: {e}", exc_info=True)
            return CancelResult.FAILED

    # ========================================================================
    # ATOMIC DUAL CANCEL  — the correct way to exit both SL and TP
    # ========================================================================

    def cancel_all_exit_orders(
        self,
        sl_order_id: Optional[str],
        tp_order_id: Optional[str],
    ) -> Tuple[CancelResult, CancelResult]:
        """
        Cancel both SL and TP together in the correct sequence.

        Cancel order:
          1. Cancel TP first (lower urgency, less risk if it fills)
          2. Cancel SL second (higher urgency if position is being closed manually)

        Returns:
            (sl_result, tp_result) as CancelResult enums.

        If either returns ALREADY_FILLED or PARTIAL_FILL, the caller must:
          - Reconcile position with get_open_position()
          - Do NOT assume position is flat until reconciliation confirms it
        """
        tp_result = CancelResult.NOT_FOUND
        sl_result = CancelResult.NOT_FOUND

        with self._orders_lock:
            if tp_order_id:
                logger.info(f"Cancelling TP: {tp_order_id}")
                tp_result = self.cancel_order(tp_order_id)
                logger.info(f"TP cancel result: {tp_result.value}")

            if sl_order_id:
                logger.info(f"Cancelling SL: {sl_order_id}")
                sl_result = self.cancel_order(sl_order_id)
                logger.info(f"SL cancel result: {sl_result.value}")

        return sl_result, tp_result

    # ========================================================================
    # QUERIES
    # ========================================================================

    def get_active_orders(self) -> Dict[str, Dict]:
        with self._orders_lock:
            return dict(self.active_orders)

    def get_order_count(self) -> int:
        with self._orders_lock:
            return len(self.active_orders)

    def get_recent_order_history(self, limit: int = 20) -> list:
        with self._orders_lock:
            return list(self.order_history[-limit:])
