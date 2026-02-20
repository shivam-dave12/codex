"""
ICT Order Block Trading Bot
============================
v9 — Institutional Engine:
  Nested Dealing Ranges · Cascade Gate Entry · Multi-Tranche Exits
  Regime-Aware Sizing · Price-Distance TCE · Self-Adaptation
"""

import logging
import signal
import sys
import threading
import time
from typing import Optional

import config
from data_manager import ICTDataManager
from order_manager import OrderManager
from risk_manager import RiskManager
from strategy import AdvancedICTStrategy
from telegram_notifier import (
    install_global_telegram_log_handler,
    send_telegram_message,
)

logging.basicConfig(
    level=getattr(config, "LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("ict_bot.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

install_global_telegram_log_handler(
    level=logging.WARNING, throttle_seconds=5.0)


class ICTBot:
    def __init__(self) -> None:
        self.running                 = False
        self.last_report_sec         = 0.0
        self.last_health_check_sec   = 0.0

        self.data_manager:  Optional[ICTDataManager]      = None
        self.order_manager: Optional[OrderManager]        = None
        self.risk_manager:  Optional[RiskManager]         = None
        self.strategy:      Optional[AdvancedICTStrategy] = None

        self.trading_enabled      = True
        self.trading_pause_reason = ""

    # =========================================================================
    # INITIALIZE
    # =========================================================================

    def initialize(self) -> bool:
        try:
            logger.info("=" * 80)
            logger.info("🚀 ICT BOT v9 — INSTITUTIONAL ENGINE INITIALIZING")
            logger.info("=" * 80)

            self.data_manager  = ICTDataManager()
            self.order_manager = OrderManager()
            self.risk_manager  = RiskManager()
            self.strategy      = AdvancedICTStrategy(self.order_manager)
            self.data_manager.register_strategy(self.strategy)

            # Feature flags
            checks = [
                ("volume_analyzer",  "Volume Profile Analyzer"),
                ("liquidity_model",  "Advanced Liquidity Model"),
                ("regime_engine",    "Regime Engine (6-mode)"),
                ("_ndr",             "Nested Dealing Ranges"),
                ("_perf_tracker",    "Self-Adaptation Tracker"),
            ]
            for attr, label in checks:
                if getattr(self.strategy, attr, None):
                    logger.info(f"✅ {label}: ENABLED")
                else:
                    logger.warning(f"⚠️  {label}: MISSING")

            logger.info("✅ Bot initialized successfully")
            return True

        except Exception:
            logger.exception("❌ Failed to initialize bot")
            return False

    # =========================================================================
    # START
    # =========================================================================

    def start(self) -> bool:
        try:
            if not all([self.order_manager, self.risk_manager,
                        self.data_manager, self.strategy]):
                logger.error("Bot components not initialized")
                return False

            # ── Leverage ──────────────────────────────────────────────────
            logger.info("Setting leverage to %sx...",
                        getattr(config, "LEVERAGE", "?"))
            resp = self.order_manager.api.set_leverage(
                symbol=config.SYMBOL,
                exchange=config.EXCHANGE,
                leverage=int(config.LEVERAGE),
            )
            if isinstance(resp, dict) and resp.get("error"):
                # ── BUG FIX: was `return False` here — non-fatal, continue ──
                logger.warning(
                    "⚠️ Leverage set warning (may already be %sx): %s",
                    config.LEVERAGE, resp)
                # Do NOT return False — leverage may already be correct

            balance_info = self.risk_manager.get_available_balance()
            if balance_info:
                logger.info(
                    "Initial Balance: %.2f USDT",
                    float(balance_info.get("available", 0.0)))

            logger.info("Starting data streams (WS + REST warmup)...")
            if not self.data_manager.start():
                logger.error("❌ Failed to start data streams")
                return False

            logger.info("Waiting for readiness (warmup + min candles)...")
            ready = self.data_manager.wait_until_ready(
                timeout_sec=float(
                    getattr(config, "READY_TIMEOUT_SEC", 120.0)))
            if not ready:
                logger.error("❌ DataManager not ready within timeout")
                return False

            logger.info("✅ Data ready. Price: $%.2f",
                        self.data_manager.get_last_price())
            self.running = True

            send_telegram_message(
                "🚀 *ICT BOT v9 STARTED*\n\n"
                "✅ WS + REST warmup OK\n"
                "✅ Supervisor active\n"
                "✅ Nested Dealing Ranges (3-tier)\n"
                "✅ Cascade Gate L1→L2→L3\n"
                "✅ Multi-Tranche Exits (50/30/20%)\n"
                "✅ Regime-Aware Sizing\n"
                "✅ Price-Distance TCE Decay\n"
                "✅ Self-Adaptation Feedback Loop")
            logger.info("🚀 BOT RUNNING — v9 INSTITUTIONAL ENGINE")
            return True

        except Exception:
            logger.exception("❌ Error starting bot")
            return False

    # =========================================================================
    # STREAM SUPERVISOR
    # =========================================================================

    def maybe_supervise_streams(self) -> None:
        if not self.data_manager or not self.data_manager.ws:
            return

        now      = time.time()
        interval = float(getattr(config, "HEALTH_CHECK_INTERVAL_SEC", 10.0))
        if now - self.last_health_check_sec < interval:
            return
        self.last_health_check_sec = now

        stale_sec = float(getattr(config, "WS_STALE_SECONDS", 30.0))
        if self.data_manager.ws.is_healthy(timeout_seconds=int(stale_sec)):
            return

        logger.warning("⚠️ WS stale (%ss). Restarting...", stale_sec)
        send_telegram_message(
            f"⚠️ WS STALE ({stale_sec}s)\n🔄 Restarting streams...")

        ok = self.data_manager.restart_streams()
        if not ok:
            logger.error("❌ Stream restart failed. Entries gated until ready.")
            return

        self.data_manager.wait_until_ready(
            timeout_sec=float(getattr(config, "READY_TIMEOUT_SEC", 120.0)))

    # =========================================================================
    # MAIN LOOP
    # =========================================================================

    def run(self) -> None:
        if not all([self.strategy, self.data_manager,
                    self.order_manager, self.risk_manager]):
            logger.error("Bot components not initialized")
            return

        logger.info("📊 Main loop active (250ms tick)")

        while self.running:
            try:
                time.sleep(0.25)

                self.maybe_supervise_streams()

                # ── BUG FIX: was calling self.strategy.get_position()
                #    which didn't exist. Now uses safe getattr. ────────────
                pos = (self.strategy.get_position()
                       if hasattr(self.strategy, "get_position") else None)
                if not self.trading_enabled and not pos:
                    continue

                self.strategy.on_tick(
                    self.data_manager,
                    self.order_manager,
                    self.risk_manager,
                    int(time.time() * 1000),
                )
                self.maybe_send_telegram_report()

            except KeyboardInterrupt:
                logger.info("Keyboard interrupt received")
                break
            except Exception:
                logger.exception("❌ Error in main loop")
                time.sleep(1.0)

        self.running = False

    # =========================================================================
    # PERIODIC TELEGRAM REPORT  (v9 fields added)
    # =========================================================================

    def maybe_send_telegram_report(self) -> None:
        interval = float(
            getattr(config, "TELEGRAM_REPORT_INTERVAL_SEC", 0.0))
        if interval <= 0:
            return

        now = time.time()
        if now - self.last_report_sec < interval:
            return
        self.last_report_sec = now

        if not all([self.strategy, self.data_manager, self.risk_manager]):
            return

        try:
            from telegram_notifier import format_periodic_report

            last_price   = self.data_manager.get_last_price()
            balance_info = self.risk_manager.get_available_balance()
            strat        = self.strategy
            stats        = strat.get_strategy_stats()

            # ── BUG FIX: was stats.get("win_rate") — key is "win_rate_pct" in v9
            win_rate = stats.get("win_rate_pct",
                                 stats.get("win_rate", 0.0))

            # ── v9: regime / DR / TCE / tranche / preserve extras ─────────
            regime_line = (
                f"Regime: {stats.get('regime', 'N/A')} "
                f"ADX={stats.get('adx', 0):.1f} "
                f"ATR×={stats.get('atr_ratio', 0):.2f} "
                f"SizeMult={stats.get('size_multiplier', 1):.2f}"
            )
            dr_line = (
                f"DR W={stats.get('dr_weekly', 'N/A')} "
                f"D={stats.get('dr_daily', 'N/A')} "
                f"I={stats.get('dr_intraday', 'N/A')}"
            )
            tce_line = (
                f"TCE={stats.get('tce_state', 'N/A')} "
                f"prob={stats.get('tce_pullback_prob', 0):.0%}"
            )
            state_line = (
                f"RuntimeState={strat.get_runtime_state() if hasattr(strat, 'get_runtime_state') else getattr(strat, 'state', 'N/A')}"
            )
            tranche_line = (
                f"Tranche={stats.get('tranche_index', 0)}/3 "
                f"Trail={'ON' if stats.get('trail_active') else 'OFF'}"
            )
            preserve_line = (
                "⚠️ CAPITAL PRESERVATION MODE"
                if stats.get("capital_preserve") else ""
            )

            # ── Regime degradation alerts ─────────────────────────────────
            regime_stats = stats.get("regime_stats", {})
            degraded = [
                f"{r}: WR={v['win_rate']:.0%}"
                for r, v in regime_stats.items()
                if v.get("degraded") and v.get("win_rate") is not None
            ]
            degraded_line = (
                "⚠️ Degraded: " + ", ".join(degraded)
                if degraded else ""
            )

            msg = format_periodic_report(
                current_price=last_price,
                balance=(balance_info.get("available", 0.0)
                         if balance_info else 0.0),
                total_trades=stats.get("total_exits", 0),
                win_rate=win_rate,
                daily_pnl=stats.get("daily_pnl", 0.0),
                total_pnl=stats.get("total_pnl", 0.0),
                consecutive_losses=stats.get("consecutive_losses", 0),
                htf_bias=getattr(strat, "htf_bias", "UNKNOWN"),
                htf_bias_strength=getattr(strat, "htf_bias_strength", 0.0),
                daily_bias=getattr(strat, "daily_bias", "NEUTRAL"),
                session=getattr(strat, "current_session", "REGULAR"),
                in_killzone=getattr(strat, "in_killzone", False),
                amd_phase=getattr(strat, "amd_phase", "UNKNOWN"),
                bot_state=getattr(strat, "state", "UNKNOWN"),
                position=strat.get_position()
                          if hasattr(strat, "get_position") else None,
                current_sl=getattr(strat, "current_sl_price", None),
                current_tp=getattr(strat, "current_tp_price", None),
                breakeven_moved=getattr(strat, "breakeven_moved", False),
                profit_locked_pct=getattr(strat, "profit_locked_pct", 0.0),
                bullish_obs=list(getattr(strat, "order_blocks_bull", [])),
                bearish_obs=list(getattr(strat, "order_blocks_bear", [])),
                bullish_fvgs=list(getattr(strat, "fvgs_bull", [])),
                bearish_fvgs=list(getattr(strat, "fvgs_bear", [])),
                liquidity_pools=list(getattr(strat, "liquidity_pools", [])),
                market_structures=list(getattr(strat, "market_structures", [])),
                swing_highs=list(getattr(strat, "swing_highs", [])),
                swing_lows=list(getattr(strat, "swing_lows", [])),
                volume_delta=self.data_manager.get_volume_delta(
                    lookback_seconds=300),
                # v9 extra context appended as footer lines
                extra_lines=list(filter(None, [
                    state_line, regime_line, dr_line, tce_line,
                    tranche_line, preserve_line, degraded_line,
                ])),
            )
            send_telegram_message(msg)

        except Exception:
            logger.exception("❌ Failed to send Telegram report")

    # =========================================================================
    # STOP
    # =========================================================================

    def stop(self) -> None:
        logger.info("Stopping ICT bot...")
        self.running = False

        stop_msg = "🛑 *ICT BOT v9 STOPPED*\nShut down gracefully"
        if self.strategy:
            pos = (self.strategy.get_position()
                   if hasattr(self.strategy, "get_position") else None)
            if pos:
                side  = pos.get("side", "?").upper()
                entry = pos.get("entry_price", 0)
                sl    = getattr(self.strategy, "current_sl_price", 0) or 0
                tp    = getattr(self.strategy, "current_tp_price", 0) or 0
                tr    = getattr(self.strategy, "_tranche", None)
                t_idx = tr.tranche_index if tr else 0
                warn  = (
                    f"\n\n⚠️ ACTIVE POSITION LEFT OPEN\n"
                    f"Side: {side}  Entry: {entry:.2f}\n"
                    f"SL: {sl:.2f}  TP: {tp:.2f}\n"
                    f"Tranche: {t_idx}/3\n"
                    f"Exchange SL/TP orders remain live.")
                logger.critical(
                    "Active position open on shutdown: %s", warn)
                stop_msg += warn

        if self.data_manager:
            self.data_manager.stop()

        send_telegram_message(stop_msg)
        logger.info("ICT bot stopped")


# =============================================================================
# ENTRY POINT
# =============================================================================

def main() -> None:
    bot = ICTBot()

    if threading.current_thread() is threading.main_thread():
        def signal_handler(signum, frame):
            logger.info("Shutdown signal received")
            bot.stop()
            sys.exit(0)
        signal.signal(signal.SIGINT,  signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    if not bot.initialize():
        sys.exit(1)
    if not bot.start():
        sys.exit(1)

    try:
        bot.run()
    except Exception:
        logger.exception("Fatal error in main")
        bot.stop()
        sys.exit(1)


if __name__ == "__main__":
    main()
