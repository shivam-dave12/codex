"""
config.py — Single source of truth for all bot parameters.
Naming: UPPER_SNAKE_CASE throughout. Every file references this module.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# CREDENTIALS
# ─────────────────────────────────────────────
COINSWITCH_API_KEY    = os.getenv("COINSWITCH_API_KEY")
COINSWITCH_SECRET_KEY = os.getenv("COINSWITCH_SECRET_KEY")
TELEGRAM_BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID      = os.getenv("TELEGRAM_CHAT_ID")

if not COINSWITCH_API_KEY or not COINSWITCH_SECRET_KEY:
    raise ValueError("Missing API credentials in .env")

# ─────────────────────────────────────────────
# EXCHANGE / SYMBOL
# ─────────────────────────────────────────────
SYMBOL   = "BTCUSDT"
EXCHANGE = "EXCHANGE_2"
LEVERAGE = 25

# ─────────────────────────────────────────────
# POSITION SIZING
# ─────────────────────────────────────────────
BALANCE_USAGE_PERCENTAGE = 30       # % of balance available for margin
MIN_MARGIN_PER_TRADE     = 4        # USDT minimum margin per trade
MAX_MARGIN_PER_TRADE     = 10_000   # USDT maximum margin per trade
MIN_POSITION_SIZE        = 0.001    # BTC minimum
MAX_POSITION_SIZE        = 1.0      # BTC maximum

# ─────────────────────────────────────────────
# RISK MANAGEMENT
# (risk_manager.py reads all of these)
# ─────────────────────────────────────────────
RISK_PER_TRADE          = 0.30      # % of balance risked per trade (0.30 = 0.30%)
MAX_DAILY_LOSS          = 400       # USDT — daily loss hard stop
MAX_DAILY_LOSS_PCT      = 5.0       # % of balance — alternative daily loss limit
MAX_DRAWDOWN_PCT        = 15.0      # % max drawdown before trading halted
MAX_CONSECUTIVE_LOSSES  = 2         # halt after this many consecutive losses
MAX_DAILY_TRADES        = 8         # max trades per calendar day
ONE_POSITION_AT_A_TIME  = True
MIN_TIME_BETWEEN_TRADES = 5         # minutes minimum between trades
TRADE_COOLDOWN_SECONDS  = 300       # seconds cooldown after a loss

# ─────────────────────────────────────────────
# RISK / REWARD
# ─────────────────────────────────────────────
MIN_RISK_REWARD_RATIO    = 3.0
TARGET_RISK_REWARD_RATIO = 4.0
MAX_RR_RATIO             = 10.0

# ─────────────────────────────────────────────
# ENTRY THRESHOLDS (confluence score /100)
# ─────────────────────────────────────────────
ENTRY_THRESHOLD_KILLZONE = 70
ENTRY_THRESHOLD_REGULAR  = 75
ENTRY_THRESHOLD_WEEKEND  = 80

# ─────────────────────────────────────────────
# DATA / READINESS
# ─────────────────────────────────────────────
READY_TIMEOUT_SEC    = 120.0
MIN_CANDLES_1M       = 100
MIN_CANDLES_5M       = 100
MIN_CANDLES_15M      = 100
MIN_CANDLES_4H       = 40
MIN_CANDLES_1H       = 20
MIN_CANDLES_1D       = 7

LOOKBACK_CANDLES_1M  = 100
LOOKBACK_CANDLES_5M  = 100
LOOKBACK_CANDLES_15M = 100
LOOKBACK_CANDLES_4H  = 50

CANDLE_TIMEFRAMES    = ["1m", "5m", "15m", "4h"]
PRIMARY_TIMEFRAME    = "5m"
HTF_TIMEFRAME        = "4h"

# ─────────────────────────────────────────────
# ORDER BLOCKS
# ─────────────────────────────────────────────
OB_MIN_STRENGTH              = 30       # 0–100 minimum quality score
OB_IMPULSE_BODY_PCT          = 50       # impulse candle body ≥ 50% of its range
OB_IMPULSE_SIZE_MULTIPLIER   = 1.15     # impulse range ≥ 1.15× OB candle range
OB_MAX_AGE_MINUTES           = 480      # 8 hours
OB_WICK_REJECTION_MIN        = 0.20     # wick ≥ 20% of candle range
OB_OPTIMAL_ENTRY_MIN         = 0.50     # OTE zone 50–79% retracement
OB_OPTIMAL_ENTRY_MAX         = 0.79
MAX_ORDER_BLOCKS             = 15

# ─────────────────────────────────────────────
# FAIR VALUE GAPS
# ─────────────────────────────────────────────
FVG_MIN_SIZE_PCT        = 0.015     # gap ≥ 0.015% of price
FVG_MAX_AGE_MINUTES     = 480
IFVG_MIN_OVERLAP_PCT    = 20        # c2 must fill ≥ 20% of gap for IFVG
MAX_FVGS                = 20

# ─────────────────────────────────────────────
# LIQUIDITY POOLS
# ─────────────────────────────────────────────
LIQ_MIN_TOUCHES          = 2
LIQ_TOUCH_TOLERANCE_PCT  = 0.08     # swings within 0.08% = same level
LIQ_MAX_DISTANCE_PCT     = 3.0      # pools within 3% of current price
LIQ_SWEEP_BUFFER_TICKS   = 8
SWEEP_WICK_REQUIREMENT   = True
SWEEP_CONFIRMATION_TICKS = 12
MAX_LIQUIDITY_ZONES      = 25
SWEEP_MAX_AGE_MINUTES       = 120   # pool-based sweep lookback (2 hours)
SWING_SWEEP_MAX_AGE_MINUTES = 360   # swing-level sweep lookback (6 hours)
SWING_SWEEP_MAX_AGE_MIN = SWING_SWEEP_MAX_AGE_MINUTES


# ─────────────────────────────────────────────
# SWING / STRUCTURE
# ─────────────────────────────────────────────
STRUCTURE_LOOKBACK_CANDLES   = 30
STRUCTURE_MIN_SWING_SIZE_PCT = 0.10
SWING_LOOKBACK_LEFT          = 3
SWING_LOOKBACK_RIGHT         = 2

# ─────────────────────────────────────────────
# CONFLUENCE SCORING — raw component weights
# ─────────────────────────────────────────────
SCORE_HTF_BIAS              = 30
SCORE_OB_OPTIMAL            = 35
SCORE_OB_PROXIMITY          = 25
SCORE_FVG                   = 25
SCORE_IFVG                  = 35
SCORE_LIQUIDITY_SWEEP       = 30
SCORE_SWEEP = SCORE_LIQUIDITY_SWEEP
SCORE_STRUCTURE             = 25
SCORE_KILLZONE              = 20
SCORE_DAILY_BIAS            = 15

VOLUME_DELTA_STRONG_THRESHOLD   = 0.10
VOLUME_DELTA_MODERATE_THRESHOLD = 0.06

PENALTY_STRUCTURE_AGAINST  = -25
PENALTY_HTF_AGAINST        = -25
PENALTY_DAILY_BIAS_AGAINST = -20
PENALTY_VOLUME_AGAINST     = -15

# Aliases used by strategy.py scoring engine
SCORE_HTF_BIAS_ALIGNED   = SCORE_HTF_BIAS
SCORE_HTF_BIAS_STRONG    = 40
SCORE_ORDER_BLOCK_OPTIMAL    = SCORE_OB_OPTIMAL
SCORE_ORDER_BLOCK_PROXIMITY  = SCORE_OB_PROXIMITY
SCORE_FVG_CONFLUENCE     = SCORE_FVG
SCORE_FVG_PRESENT        = SCORE_FVG
SCORE_IFVG_BONUS         = SCORE_IFVG
SCORE_LIQUIDITY          = SCORE_LIQUIDITY_SWEEP
SCORE_STRUCTURE_ALIGNED  = SCORE_STRUCTURE
SCORE_PO3_KILLZONE       = SCORE_KILLZONE
SCORE_PO3                = SCORE_KILLZONE
SCORE_VOLUME_DELTA_STRONG   = 20
SCORE_VOLUME_DELTA_MODERATE = 10
SCORE_VOLUME_DELTA_WEAK     = 5

# ─────────────────────────────────────────────
# HTF BIAS ENGINE
# ─────────────────────────────────────────────
HTF_TREND_EMA          = 34
HTF_EMA_MIN_DISTANCE   = 0.5        # price must be ≥ 0.5% from EMA34

# ─────────────────────────────────────────────
# BREAKEVEN & PROFIT PROTECTION
# ─────────────────────────────────────────────
ENABLE_BREAKEVEN         = True
BREAKEVEN_TRIGGER_RR     = 1.0
BREAKEVEN_BUFFER_PCT     = 0.0025

ENABLE_PROFIT_PROTECTION = True
PROFIT_PROTECTION_ZONES  = [
    {"trigger_rr": 1.5, "protect_pct": 0.50, "buffer_pct": 0.004},
    {"trigger_rr": 2.0, "protect_pct": 0.70, "buffer_pct": 0.005},
    {"trigger_rr": 3.0, "protect_pct": 0.85, "buffer_pct": 0.006},
]

# config.py additions for v8
TCE_MOMENTUM_THRESHOLD   = 0.25   # pullback probability below which momentum mode activates
TCE_MAX_AGE_HOURS        = 4      # how long a TCE cycle stays open before auto-expiry

# ─────────────────────────────────────────────
# TRAILING STOP
# ─────────────────────────────────────────────
ENABLE_TRAILING_STOP    = True
TRAILING_ACTIVATION_RR  = 2.5
TRAILING_DISTANCE_PCT   = 0.018
TRAILING_STEP_PCT       = 0.006

ENABLE_ATR_TRAILING        = True
ATR_TRAILING_PERIOD        = 14
ATR_TRAILING_MULTIPLIER    = 1.5
ATR_TRAILING_MIN_RR        = 1.5

ENABLE_DYNAMIC_SL          = True
ENABLE_DYNAMIC_SL_TIGHTENING = True

SL_RATCHET_ENABLED         = True
SL_MINIMUM_IMPROVEMENT_PCT = 0.001

ENABLE_STRUCTURE_SL         = True
STRUCTURE_SL_CHECK_INTERVAL = 15
STRUCTURE_SL_USE_NEW_OBS    = True
STRUCTURE_SL_BUFFER_PCT     = 0.002

# ─────────────────────────────────────────────
# DYNAMIC TP
# ─────────────────────────────────────────────
ENABLE_DYNAMIC_TP                   = True
DYNAMIC_TP_STRUCTURE_CHECK_INTERVAL = 10
DYNAMIC_TP_STRUCTURE_BUFFER_PCT     = 0.0008
DYNAMIC_TP_MIN_STRUCTURE_AGE_SEC    = 30

ENABLE_TRAILING_TP          = True
TRAILING_TP_ACTIVATION_PCT  = 0.80
TRAILING_TP_LOCK_PCT        = 0.70
TRAILING_TP_STEP_SIZE_PCT   = 0.003

ENABLE_NEAR_TP_CLOSE        = True
NEAR_TP_THRESHOLD_PCT       = 0.97
NEAR_TP_AGGRESSIVE_PCT      = 0.99
NEAR_TP_REVERSAL_SIGNALS    = True
NEAR_TP_SINGLE_REJECTION    = True

ENABLE_TIME_DECAY_TP            = True
TIME_DECAY_START_MINUTES        = 60
TIME_DECAY_AGGRESSIVE_MINUTES   = 120
TIME_DECAY_TIGHTEN_PCT_PER_HOUR = 0.15

ENABLE_HTF_SHIFT_TP             = True
HTF_SHIFT_TIGHTEN_PCT           = 0.50
HTF_SHIFT_IMMEDIATE_CLOSE_IF_IN_PROFIT = False

# ─────────────────────────────────────────────
# MOMENTUM EXHAUSTION
# ─────────────────────────────────────────────
ENABLE_MOMENTUM_EXHAUSTION        = True
MOMENTUM_CANDLE_COUNT             = 5
MOMENTUM_BODY_SHRINK_THRESHOLD    = 0.4
MOMENTUM_VOLUME_DECLINE_THRESHOLD = 0.5
MOMENTUM_CONSECUTIVE_SHRINKS      = 3

# ─────────────────────────────────────────────
# WICK REJECTION
# ─────────────────────────────────────────────
ENABLE_WICK_REJECTION   = True
WICK_REJECTION_RATIO    = 0.6
WICK_REJECTION_COUNT    = 2
WICK_REJECTION_LOOKBACK = 5

# ─────────────────────────────────────────────
# ORDER EXECUTION
# ─────────────────────────────────────────────
TICK_SIZE               = 0.1
SL_BUFFER_TICKS         = 5
LIMIT_ORDER_OFFSET_TICKS = 8
ORDER_TIMEOUT_SECONDS   = 45
MAX_ORDER_RETRIES       = 2

MIN_SL_DISTANCE_PCT     = 0.002
MAX_SL_DISTANCE_PCT     = 0.05

PARTIAL_FILL_MIN_QTY_PCT  = 10.0
PARTIAL_FILL_REPLACE_SLTP = True
PARTIAL_FILL_LOG_LEVEL    = "WARNING"

# ─────────────────────────────────────────────
# RATE LIMITING
# ─────────────────────────────────────────────
GLOBAL_API_MIN_INTERVAL = 3.0       # seconds between any two API calls
RATE_LIMIT_ORDERS       = 15        # max orders per 60s window
REQUEST_TIMEOUT         = 30        # HTTP timeout seconds

# ─────────────────────────────────────────────
# SESSIONS / KILLZONES
# ─────────────────────────────────────────────
ENABLE_PO3_FILTER        = True
PO3_LONDON_KILLZONE_START = 2
PO3_LONDON_KILLZONE_END   = 5
PO3_NY_KILLZONE_START     = 8
PO3_NY_KILLZONE_END       = 11

# ─────────────────────────────────────────────
# HEALTH CHECK / SUPERVISOR
# ─────────────────────────────────────────────
WS_STALE_SECONDS          = 35.0
HEALTH_CHECK_INTERVAL_SEC = 12.0
BALANCE_CACHE_TTL_SEC     = 35.0
POSITION_UPDATE_INTERVAL_SEC = 5.0
SLTP_UPDATE_INTERVAL_SEC     = 15.0
MIN_SL_MOVEMENT_TICKS        = 8
STRUCTURE_UPDATE_INTERVAL_SECONDS  = 30   # re-detect OB/FVG/swing every 30s
ENTRY_EVALUATION_INTERVAL_SECONDS  = 5    # evaluate entry conditions every 5s

# ─────────────────────────────────────────────
# LOGGING / REPORTING
# ─────────────────────────────────────────────
LOG_LEVEL                  = "INFO"
TELEGRAM_REPORT_INTERVAL_SEC = 900.0

# ─────────────────────────────────────────────
# FEATURE FLAGS
# ─────────────────────────────────────────────
ENABLE_PARTIAL_EXITS  = False
ENABLE_FALLBACK_TP    = False
ENABLE_FALLBACK_SL    = False
ENABLE_ATR_FALLBACK   = False
ENABLE_FIB_FALLBACK   = False

# ─────────────────────────────────────────────
# v9 NEW CONSTANTS
# ─────────────────────────────────────────────

# Regime / SL management
ATR_VALUE                      = 50.0    # fallback ATR in price units (BTC ≈ $50)
SL_UPDATE_INTERVAL_SECONDS     = 30      # how often dynamic SL is re-evaluated
ENTRY_PENDING_TIMEOUT_SECONDS  = 120     # cancel limit if unfilled after this

# OB detection
OB_MIN_IMPULSE_PCT             = 0.3     # impulse candle must move ≥ 0.3% to form OB

# Structure maintenance
STRUCTURE_CLEANUP_DISTANCE_PCT = 5.0     # invalidate OB/FVG if > 5% from price
