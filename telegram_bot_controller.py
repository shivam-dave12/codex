"""
Telegram Bot Controller - User-Friendly
Commands: /start, /stop, /status, /help
"""

import logging
import time
import threading
import requests
from typing import Optional
import sys

import telegram_config  # TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

bot_instance = None
bot_thread = None
bot_running = False


class TelegramBotController:
    def __init__(self):
        self.bot_token = telegram_config.TELEGRAM_BOT_TOKEN
        self.chat_id = str(telegram_config.TELEGRAM_CHAT_ID)
        self.last_update_id = 0
        self.running = False

        if not self.bot_token or not self.chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")

        logger.info("TelegramBotController initialized")

    def send_message(self, message: str, parse_mode: str = "Markdown") -> bool:
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            resp = requests.post(url, json=payload, timeout=10)
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Error sending message: {e}")
            return False

    def get_updates(self, timeout: int = 30) -> list:
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
            params = {
                "offset": self.last_update_id + 1,
                "timeout": timeout,
                "allowed_updates": ["message"],
            }
            resp = requests.get(url, params=params, timeout=timeout + 5)
            if resp.status_code != 200:
                return []
            data = resp.json()
            if data.get("ok"):
                return data.get("result", [])
            return []
        except Exception:
            return []

    def clear_old_messages(self):
        try:
            updates = self.get_updates(timeout=1)
            if updates:
                self.last_update_id = updates[-1]["update_id"]
                logger.info(f"Cleared {len(updates)} old messages")
        except Exception as e:
            logger.error(f"Error clearing old messages: {e}")

    def set_my_commands(self):
        """Register Telegram command menu"""
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/setMyCommands"
            commands = [
                {"command": "start", "description": "Start trading bot"},
                {"command": "stop", "description": "Stop trading bot"},
                {"command": "status", "description": "Full bot status + market overview"},
                {"command": "structures", "description": "Deep ICT structure analysis"},
                {"command": "help", "description": "Show commands"},
            ]
            requests.post(url, json={"commands": commands}, timeout=10)
        except Exception as e:
            logger.error(f"Error setting commands: {e}")

    def _help_text(self) -> str:
        return (
            "*Commands*\n"
            "/start - Start bot\n"
            "/stop - Stop bot\n"
            "/status - Full status + market overview\n"
            "/structures - Deep ICT structure analysis\n"
            "/help - This list"
        )

    def _normalize_command(self, text: str) -> str:
        """Accept 'status' or '/status'"""
        t = (text or "").strip().lower()
        if t in ("start", "stop", "status", "structures", "help"):
            return "/" + t
        if t.startswith("/"):
            return t.split()[0]
        return t

    def handle_command(self, raw_text: str) -> Optional[str]:
        global bot_instance, bot_thread, bot_running

        cmd = self._normalize_command(raw_text)

        if cmd in ("/help", "/commands"):
            return self._help_text()

        if cmd == "/start":
            if bot_running and bot_thread and bot_thread.is_alive():
                return "Bot already running."

            logger.info("Starting bot from Telegram...")
            bot_thread = threading.Thread(target=self.run_bot, daemon=True)
            bot_thread.start()
            time.sleep(1.0)

            if bot_thread.is_alive():
                return "✅ Bot started."
            return "❌ Start failed. Check logs."

        if cmd == "/stop":
            if not bot_running or not bot_thread or not bot_thread.is_alive():
                return "Bot not running."

            logger.info("Stopping bot from Telegram...")
            bot_running = False
            if bot_instance:
                bot_instance.stop()
            if bot_thread:
                bot_thread.join(timeout=5.0)
            return "🛑 Bot stopped."

        if cmd == "/status":
            if not bot_running or not bot_instance:
                return "Bot not running. Use /start."

            try:
                from telegram_notifier import format_periodic_report

                bot = bot_instance
                strat = bot.strategy
                dm = bot.data_manager
                rm = bot.risk_manager

                if not strat or not dm or not rm:
                    return "❌ Bot components not ready"

                last_price = dm.get_last_price()
                balance_info = rm.get_available_balance()
                total = getattr(rm, "total_trades", 0)
                winning = getattr(rm, "winning_trades", 0)
                win_rate = (winning / total * 100.0) if total > 0 else 0.0

                report = format_periodic_report(
                    current_price=last_price,
                    balance=balance_info.get("available", 0.0) if balance_info else 0.0,
                    total_trades=total,
                    win_rate=win_rate,
                    daily_pnl=getattr(rm, "daily_pnl", 0.0),
                    total_pnl=getattr(rm, "realized_pnl", 0.0),
                    consecutive_losses=getattr(rm, "consecutive_losses", 0),
                    htf_bias=getattr(strat, "htf_bias", "UNKNOWN"),
                    htf_bias_strength=getattr(strat, "htf_bias_strength", 0.0),
                    daily_bias=getattr(strat, "daily_bias", "NEUTRAL"),
                    session=getattr(strat, "current_session", "REGULAR"),
                    in_killzone=getattr(strat, "in_killzone", False),
                    amd_phase=getattr(strat, "amd_phase", "UNKNOWN"),
                    bot_state=getattr(strat, "state", "UNKNOWN"),
                    position=strat.get_position(),
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
                    volume_delta=dm.get_volume_delta(lookback_seconds=300),
                )

                self.send_message(report, parse_mode="HTML")
                logger.info("✅ Sent /status report")
                return None

            except Exception as e:
                logger.error(f"Status error: {e}", exc_info=True)
                return f"❌ Status error: {e}"

        if cmd == "/structures":
            if not bot_running or not bot_instance:
                return "Bot not running. Use /start."

            try:
                from telegram_notifier import format_structures_report

                bot = bot_instance
                strat = bot.strategy
                dm = bot.data_manager

                if not strat or not dm:
                    return "❌ Bot components not ready"

                last_price = dm.get_last_price()

                report = format_structures_report(
                    current_price=last_price,
                    htf_bias=getattr(strat, "htf_bias", "UNKNOWN"),
                    htf_bias_strength=getattr(strat, "htf_bias_strength", 0.0),
                    daily_bias=getattr(strat, "daily_bias", "NEUTRAL"),
                    session=getattr(strat, "current_session", "REGULAR"),
                    in_killzone=getattr(strat, "in_killzone", False),
                    amd_phase=getattr(strat, "amd_phase", "UNKNOWN"),
                    bullish_obs=list(getattr(strat, "order_blocks_bull", [])),
                    bearish_obs=list(getattr(strat, "order_blocks_bear", [])),
                    bullish_fvgs=list(getattr(strat, "fvgs_bull", [])),
                    bearish_fvgs=list(getattr(strat, "fvgs_bear", [])),
                    liquidity_pools=list(getattr(strat, "liquidity_pools", [])),
                    market_structures=list(getattr(strat, "market_structures", [])),
                    swing_highs=list(getattr(strat, "swing_highs", [])),
                    swing_lows=list(getattr(strat, "swing_lows", [])),
                    volume_delta=dm.get_volume_delta(lookback_seconds=300),
                )

                self.send_message(report, parse_mode="HTML")
                logger.info("✅ Sent /structures report")
                return None

            except Exception as e:
                logger.error(f"Structures error: {e}", exc_info=True)
                return f"❌ Structures error: {e}"


        return "❌ Unknown command.\n\n" + self._help_text()

    def run_bot(self):
        global bot_instance, bot_running
        try:
            bot_running = True
            from main import ICTBot

            bot_instance = ICTBot()
            if not bot_instance.initialize():
                logger.error("Bot initialize failed")
                bot_running = False
                return

            if not bot_instance.start():
                logger.error("Bot start failed")
                bot_running = False
                return

            bot_instance.run()

        except Exception as e:
            logger.error(f"Bot crashed: {e}", exc_info=True)
        finally:
            logger.info("Bot thread finished")
            bot_running = False

    def start(self):
        self.running = True
        self.clear_old_messages()
        self.set_my_commands()
        self.send_message("✅ Controller Ready.\n\n" + self._help_text())

        logger.info("Telegram controller started; waiting for commands...")

        while self.running:
            try:
                updates = self.get_updates(timeout=30)
                for upd in updates:
                    self.last_update_id = upd.get("update_id", self.last_update_id)

                    msg = (upd.get("message") or {})
                    chat_id = str((msg.get("chat") or {}).get("id", ""))
                    text = (msg.get("text") or "").strip()

                    # Auth check
                    if chat_id != self.chat_id:
                        continue

                    if not text:
                        continue

                    logger.info(f"Received: {text}")
                    response = self.handle_command(text)
                    if response:
                        self.send_message(response)

            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Command loop error: {e}", exc_info=True)
                time.sleep(2.0)

        logger.info("Telegram controller stopped")

    def stop(self):
        self.running = False
        global bot_instance, bot_running
        if bot_running and bot_instance:
            bot_instance.stop()



def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler("telegram_controller.log"),
            logging.StreamHandler(),
        ],
    )
    try:
        controller = TelegramBotController()
        controller.start()
    except KeyboardInterrupt:
        logger.info("Shutdown requested")
    except Exception as e:
        logger.error(f"Fatal: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()