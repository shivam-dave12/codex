"""
Risk Manager - Industry Grade
Comprehensive risk management with position tracking
"""

import logging
import time
import threading
from typing import Dict, Optional
from datetime import datetime, timedelta
from dataclasses import dataclass
from collections import deque

import config

logger = logging.getLogger(__name__)

@dataclass
class TradeRecord:
    timestamp: float
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    is_win: bool
    reason: str

class RiskManager:
    """Industry-grade risk manager"""

    def __init__(self):
        self._lock = threading.RLock()

        # Performance tracking
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.realized_pnl = 0.0
        self.daily_pnl = 0.0
        self.consecutive_losses = 0

        # Trade history
        self.trade_history: deque[TradeRecord] = deque(maxlen=1000)
        self.daily_trades = []
        self.last_trade_time = 0.0

        # Risk limits
        self.daily_loss_limit = config.MAX_DAILY_LOSS
        self.max_consecutive_losses = config.MAX_CONSECUTIVE_LOSSES
        self.max_daily_trades = config.MAX_DAILY_TRADES

        # Balance tracking
        self.initial_balance = 0.0
        self.current_balance = 0.0
        self.balance_cache_time = 0.0
        self.balance_cache_ttl = config.BALANCE_CACHE_TTL_SEC

        # API access
        from futures_api import FuturesAPI
        self.api = FuturesAPI(
            api_key=config.COINSWITCH_API_KEY,
            secret_key=config.COINSWITCH_SECRET_KEY
        )

        logger.info("✅ RiskManager initialized")

    def get_available_balance(self) -> Optional[Dict]:
        """Get available balance with caching"""
        with self._lock:
            now = time.time()
            
            # Return cached balance if still valid
            if now - self.balance_cache_time < self.balance_cache_ttl:
                return {
                    "available": self.current_balance,
                    "cached": True
                }

            try:
                # Import and use rate limiter
                from order_manager import GlobalRateLimiter
                GlobalRateLimiter.wait()  # This will work after you fix GlobalRateLimiter

                balance_data = self.api.get_balance("USDT")
                
                if "error" in balance_data:
                    logger.error(f"Balance fetch error: {balance_data['error']}")
                    # Return cached balance on error
                    return {
                        "available": self.current_balance, 
                        "cached": True,
                        "error": balance_data['error']
                    }

                # Update balance
                self.current_balance = float(balance_data.get("available", 0.0))
                self.balance_cache_time = now

                # Set initial balance on first fetch
                if self.initial_balance == 0.0:
                    self.initial_balance = self.current_balance
                    logger.info(f"💰 Initial balance set: ${self.initial_balance:.2f}")

                return {
                    "available": self.current_balance,
                    "cached": False
                }
                
            except Exception as e:
                logger.error(f"Error fetching balance: {e}", exc_info=True)
                # Return cached balance on exception
                return {
                    "available": self.current_balance, 
                    "cached": True,
                    "error": str(e)
                }


    def calculate_position_size(
        self,
        entry_price: float,
        stop_loss: float,
        side: str
    ) -> Optional[float]:
        """Calculate position size based on risk"""
        try:
            # Type safety - ensure we have floats
            entry_price = float(entry_price)
            stop_loss = float(stop_loss)
            side = str(side).upper()
            
            # Validate side
            if side not in ["LONG", "SHORT"]:
                logger.error(f"Invalid side: {side}")
                return None
            
            # Get balance
            balance_info = self.get_available_balance()
            if not balance_info:
                logger.error("Failed to get balance")
                return None

            available = balance_info.get("available", 0.0)
            if available <= 0:
                logger.error(f"No available balance: {available}")
                return None

            # Calculate risk per trade
            risk_amount = available * (config.BALANCE_USAGE_PERCENTAGE / 100)
            risk_amount = max(config.MIN_MARGIN_PER_TRADE, 
                            min(risk_amount, config.MAX_MARGIN_PER_TRADE))

            # Calculate price distance
            if side == "LONG":
                price_distance = entry_price - stop_loss
                if stop_loss >= entry_price:
                    logger.error(f"Invalid LONG SL: SL ({stop_loss}) must be < entry ({entry_price})")
                    return None
            else:  # SHORT
                price_distance = stop_loss - entry_price
                if stop_loss <= entry_price:
                    logger.error(f"Invalid SHORT SL: SL ({stop_loss}) must be > entry ({entry_price})")
                    return None

            # Validate distance
            if price_distance <= 0:
                logger.error(
                    f"Invalid price distance: side={side}, entry={entry_price:.2f}, "
                    f"sl={stop_loss:.2f}, distance={price_distance:.2f}"
                )
                return None

            # Calculate risk percentage
            risk_pct = price_distance / entry_price
            
            # Validate risk percentage (0.1% to 10%)
            if risk_pct < 0.001:
                logger.error(f"SL too tight: {risk_pct*100:.3f}% (min 0.1%)")
                return None
            if risk_pct > 0.10:
                logger.warning(f"SL very wide: {risk_pct*100:.2f}% (max 10%)")
                # Allow but warn

            # Position size calculation with leverage
            position_value = risk_amount / risk_pct
            position_size = position_value / entry_price
            position_size = position_size / config.LEVERAGE

            # Apply limits
            position_size = max(config.MIN_POSITION_SIZE,
                            min(position_size, config.MAX_POSITION_SIZE))

            # Round to precision (4 decimals for BTC)
            position_size = round(position_size, 4)
            
            # Final validation
            if position_size < config.MIN_POSITION_SIZE:
                logger.error(
                    f"Position size too small: {position_size} BTC "
                    f"(min: {config.MIN_POSITION_SIZE})"
                )
                return None

            # Calculate notional value for logging
            notional_value = position_size * entry_price * config.LEVERAGE
            
            logger.info(
                f"✅ Position calculated: {position_size} BTC | "
                f"Risk: ${risk_amount:.2f} ({risk_pct*100:.2f}%) | "
                f"Notional: ${notional_value:.2f} | "
                f"SL distance: {price_distance:.2f}"
            )
            
            return position_size

        except ValueError as e:
            logger.error(f"Value error in position calculation: {e}")
            return None
        except Exception as e:
            logger.error(f"Error calculating position size: {e}", exc_info=True)
            return None

    def notify_entry_placed(self) -> None:
        """
        Called by strategy._execute_entry immediately after limit order
        is confirmed placed. Updates last_trade_time so can_trade() cooldown
        works correctly without waiting for the trade to close.
        """
        with self._lock:
            self.last_trade_time = time.time()
            logger.debug("🔔 RiskManager: entry placed — cooldown timer reset")


    def can_trade(self) -> tuple[bool, str]:
        with self._lock:
            now = time.time()

            # ── Min time between trades ───────────────────────────────────────
            time_since_last = now - self.last_trade_time
            if (self.last_trade_time > 0 and
                    time_since_last < config.MIN_TIME_BETWEEN_TRADES * 60):
                remaining = int(
                    config.MIN_TIME_BETWEEN_TRADES * 60 - time_since_last)
                return False, f"Cooldown: {remaining}s remaining"

            # ── Loss cooldown (new) ───────────────────────────────────────────
            cooldown = getattr(config, "TRADE_COOLDOWN_SECONDS", 300)
            if (self.consecutive_losses > 0 and
                    self.last_trade_time > 0 and
                    time_since_last < cooldown):
                remaining = int(cooldown - time_since_last)
                return False, f"Loss cooldown: {remaining}s remaining"

            # ── Reset daily counters if new day ───────────────────────────────
            self._reset_daily_if_needed()

            # ── Daily trade limit ─────────────────────────────────────────────
            if len(self.daily_trades) >= self.max_daily_trades:
                return False, f"Daily trade limit ({self.max_daily_trades})"

            # ── Daily loss limit (USDT) ───────────────────────────────────────
            if self.daily_pnl <= -self.daily_loss_limit:
                return False, f"Daily loss limit hit (${abs(self.daily_pnl):.2f})"

            # ── Daily loss limit (% of balance) — new ────────────────────────
            if self.current_balance > 0:
                daily_loss_pct = abs(self.daily_pnl) / self.current_balance * 100
                max_daily_pct  = getattr(config, "MAX_DAILY_LOSS_PCT", 5.0)
                if self.daily_pnl < 0 and daily_loss_pct >= max_daily_pct:
                    return False, (f"Daily loss % limit hit "
                                f"({daily_loss_pct:.1f}% >= {max_daily_pct}%)")

            # ── Max drawdown check (new) ──────────────────────────────────────
            if self.initial_balance > 0 and self.current_balance > 0:
                drawdown_pct = ((self.initial_balance - self.current_balance)
                                / self.initial_balance * 100)
                max_dd = getattr(config, "MAX_DRAWDOWN_PCT", 15.0)
                if drawdown_pct >= max_dd:
                    return False, (f"Max drawdown hit "
                                f"({drawdown_pct:.1f}% >= {max_dd}%)")

            # ── Consecutive losses ────────────────────────────────────────────
            if self.consecutive_losses >= self.max_consecutive_losses:
                return False, (f"Max consecutive losses "
                            f"({self.consecutive_losses})")

            return True, "OK"


    def record_trade(
        self,
        side: str,
        entry_price: float,
        exit_price: float,
        quantity: float,
        reason: str
    ):
        """Record completed trade"""
        with self._lock:
            # Calculate P&L
            if side == "LONG":
                pnl = (exit_price - entry_price) * quantity
            else:
                pnl = (entry_price - exit_price) * quantity

            is_win = pnl > 0

            # Create record
            trade = TradeRecord(
                timestamp=time.time(),
                side=side,
                entry_price=entry_price,
                exit_price=exit_price,
                quantity=quantity,
                pnl=pnl,
                is_win=is_win,
                reason=reason
            )

            # Update statistics
            self.trade_history.append(trade)
            self.daily_trades.append(trade)
            self.total_trades += 1
            self.realized_pnl += pnl
            self.daily_pnl += pnl
            self.last_trade_time = time.time()

            if is_win:
                self.winning_trades += 1
                self.consecutive_losses = 0
            else:
                self.losing_trades += 1
                self.consecutive_losses += 1

            logger.info(f"📊 Trade recorded: {side} | P&L: ${pnl:+.2f} | Total: {self.total_trades}")

    def _reset_daily_if_needed(self):
        """Reset daily counters if new UTC day"""
        now = datetime.utcnow()
        if not self.daily_trades:
            return

        last_trade_time = datetime.fromtimestamp(self.daily_trades[-1].timestamp)
        if now.date() > last_trade_time.date():
            logger.info("🔄 New day - resetting daily counters")
            self.daily_trades = []
            self.daily_pnl = 0.0

    def get_statistics(self) -> Dict:
        """Get risk statistics"""
        with self._lock:
            win_rate = (self.winning_trades / self.total_trades * 100) if self.total_trades > 0 else 0

            return {
                "total_trades": self.total_trades,
                "winning_trades": self.winning_trades,
                "losing_trades": self.losing_trades,
                "win_rate": win_rate,
                "realized_pnl": self.realized_pnl,
                "daily_pnl": self.daily_pnl,
                "daily_trades": len(self.daily_trades),
                "consecutive_losses": self.consecutive_losses,
                "current_balance": self.current_balance
            }
