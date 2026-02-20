# telegram_config.py

import os
from dotenv import load_dotenv

# Reuse the existing .env setup
load_dotenv()

# Bot credentials from .env
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Enable/disable Telegram notifications
TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

# Interval for periodic reports (seconds) – default 900s = 15 minutes
try:
    TELEGRAM_REPORT_INTERVAL_SEC = int(
        os.getenv("TELEGRAM_REPORT_INTERVAL_SEC", "900")
    )
except Exception:
    TELEGRAM_REPORT_INTERVAL_SEC = 900
