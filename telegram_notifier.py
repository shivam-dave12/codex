"""
Telegram Notifier - Production Grade
=====================================
- Rate-limited notifications via HTTP API
- Comprehensive ICT structure reporting with exact price levels
- Periodic 15-min status updates (account + structures + confluence)
- /structures command: deep ICT analysis
- All messages use HTML parse_mode for reliable formatting

Version: 3.0.0
"""

import logging
import time
import threading
from typing import Optional, Dict, List
from datetime import datetime, timezone
import requests

import telegram_config

logger = logging.getLogger(__name__)

# ============================================================================
# ASYNC QUEUE-BASED SENDER  (never blocks the calling thread)
# ============================================================================

import queue as _queue

_send_queue: _queue.Queue = _queue.Queue(maxsize=100)
_send_lock   = threading.Lock()
_last_send_time = 0.0
_MIN_SEND_INTERVAL = 1.5  # seconds between Telegram API calls


def _telegram_sender_worker() -> None:
    """Background daemon: drains queue with rate limiting."""
    global _last_send_time
    while True:
        try:
            item = _send_queue.get(timeout=2.0)
        except _queue.Empty:
            continue

        message, parse_mode = item
        if not telegram_config.TELEGRAM_ENABLED:
            _send_queue.task_done()
            continue

        with _send_lock:
            now  = time.time()
            wait = _MIN_SEND_INTERVAL - (now - _last_send_time)
            if wait > 0:
                time.sleep(wait)
            try:
                text = (message[:4080] + "\n…[truncated]") \
                       if len(message) > 4096 else message
                url  = (f"https://api.telegram.org/bot"
                        f"{telegram_config.TELEGRAM_BOT_TOKEN}/sendMessage")
                resp = requests.post(url, json={
                    "chat_id":                  telegram_config.TELEGRAM_CHAT_ID,
                    "text":                     text,
                    "parse_mode":               parse_mode,
                    "disable_web_page_preview": True,
                }, timeout=10)
                _last_send_time = time.time()
                if resp.status_code != 200:
                    logger.warning("Telegram send failed: %s - %s",
                                   resp.status_code, resp.text[:200])
            except Exception as e:
                logger.error("Error sending Telegram message: %s", e)
                _last_send_time = time.time()
            finally:
                _send_queue.task_done()


# Start once on module import
_sender_thread = threading.Thread(
    target=_telegram_sender_worker,
    name="telegram-sender",
    daemon=True,
)
_sender_thread.start()


def send_telegram_message(message: str, parse_mode: str = "HTML") -> bool:
    """Enqueue message for async delivery. Non-blocking. Returns False if queue full."""
    if not telegram_config.TELEGRAM_ENABLED:
        return False
    try:
        _send_queue.put_nowait((message, parse_mode))
        return True
    except _queue.Full:
        logger.warning("Telegram send queue full — message dropped")
        return False



# ============================================================================
# LOGGING HANDLER STUB
# ============================================================================

class _TelegramLogHandler(logging.Handler):
    """Forwards log records ≥ level to Telegram, throttled per level."""

    _EMOJI = {
        logging.WARNING:  "⚠️",
        logging.ERROR:    "🔴",
        logging.CRITICAL: "🚨",
    }

    def __init__(self, level: int = logging.WARNING,
                 throttle_seconds: float = 5.0) -> None:
        super().__init__(level)
        self._throttle   = throttle_seconds
        self._last_sent: Dict[int, float] = {}
        self._tlock      = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            with self._tlock:
                now  = time.time()
                last = self._last_sent.get(record.levelno, 0.0)
                if now - last < self._throttle:
                    return
                self._last_sent[record.levelno] = now

            import html as _html
            emoji = self._EMOJI.get(record.levelno, "ℹ️")
            text  = _html.escape(self.format(record))   # ← escape < > & ' "
            msg   = (f"{emoji} <b>{record.levelname}</b> "
                    f"[<code>{record.name}</code>]\n"
                    f"<code>{text[:800]}</code>")

            # Use non-blocking send — never block log calls
            send_telegram_message(msg)
        except Exception:
            self.handleError(record)


def install_global_telegram_log_handler(
    level: int = logging.WARNING,
    throttle_seconds: float = 5.0,
) -> None:
    """Install a real throttled Telegram log handler on the root logger."""
    if not telegram_config.TELEGRAM_ENABLED:
        logger.info("Telegram log handler skipped (TELEGRAM_ENABLED=False)")
        return

    # Avoid double-installing on restart / reimport
    root = logging.getLogger()
    if any(isinstance(h, _TelegramLogHandler) for h in root.handlers):
        logger.info("Telegram log handler already installed — skipping")
        return

    handler = _TelegramLogHandler(level=level, throttle_seconds=throttle_seconds)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S")
    )
    root.addHandler(handler)
    logger.info(
        "✅ Telegram log handler installed — level=%s throttle=%.1fs",
        logging.getLevelName(level), throttle_seconds,
    )



# ============================================================================
# HELPERS
# ============================================================================

def _ts_now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _pnl_emoji(value: float) -> str:
    if value > 0:
        return "🟢"
    elif value < 0:
        return "🔴"
    return "⚪"


def _bias_emoji(bias: str) -> str:
    return {"BULLISH": "🟢", "BEARISH": "🔴"}.get(bias, "⚪")


def _session_emoji(session: str) -> str:
    return {
        "LONDON": "🇬🇧",
        "NEW_YORK": "🇺🇸",
        "ASIAN": "🌏",
        "WEEKEND": "🌙",
    }.get(session, "🕐")


def _safe_dist_pct(price_a: float, price_b: float) -> float:
    """(a - b) / b * 100, safe for zero."""
    if price_b <= 0:
        return 0.0
    return ((price_a - price_b) / price_b) * 100


# ============================================================================
# PERIODIC 15-MIN REPORT (combined account + ICT overview)
# ============================================================================

def format_periodic_report(
    *,
    current_price: float,
    balance: float,
    total_trades: int,
    win_rate: float,
    daily_pnl: float,
    total_pnl: float,
    consecutive_losses: int,
    # Strategy context
    htf_bias: str,
    htf_bias_strength: float,
    daily_bias: str,
    session: str,
    in_killzone: bool,
    amd_phase: str,
    bot_state: str,
    # Position (from strategy.active_position dict or None)
    position: Optional[Dict] = None,
    current_sl: Optional[float] = None,
    current_tp: Optional[float] = None,
    breakeven_moved: bool = False,
    profit_locked_pct: float = 0.0,
    # ICT structures
    bullish_obs: Optional[List] = None,
    bearish_obs: Optional[List] = None,
    bullish_fvgs: Optional[List] = None,
    bearish_fvgs: Optional[List] = None,
    liquidity_pools: Optional[List] = None,
    market_structures: Optional[List] = None,
    swing_highs: Optional[List] = None,
    swing_lows: Optional[List] = None,
    # Volume
    volume_delta: Optional[Dict] = None,
    extra_lines: Optional[List[str]] = None,
) -> str:
    """Build the comprehensive 15-minute status update."""
    bullish_obs = bullish_obs or []
    bearish_obs = bearish_obs or []
    bullish_fvgs = bullish_fvgs or []
    bearish_fvgs = bearish_fvgs or []
    liquidity_pools = liquidity_pools or []
    market_structures = market_structures or []
    swing_highs = swing_highs or []
    swing_lows = swing_lows or []
    volume_delta = volume_delta or {}

    now_ms = int(time.time() * 1000)
    lines: List[str] = []

    # ── HEADER ──
    lines.append("📊 <b>ICT BOT — 15-MIN UPDATE</b>")
    lines.append(f"🕐 {_ts_now_utc()}")
    lines.append(f"💲 BTC: <code>${current_price:,.2f}</code>")
    lines.append("")

    # ── ACCOUNT ──
    lines.append("━━━ 💰 <b>ACCOUNT</b> ━━━")
    lines.append(f"Balance: <code>{balance:.2f}</code> USDT")
    lines.append(f"Daily P&L: {_pnl_emoji(daily_pnl)} <code>{daily_pnl:+.2f}</code> USDT")
    lines.append(f"Total P&L: {_pnl_emoji(total_pnl)} <code>{total_pnl:+.2f}</code> USDT")
    wr_str = f"{win_rate:.1f}%" if total_trades > 0 else "N/A"
    lines.append(f"Trades: <code>{total_trades}</code> | WR: <code>{wr_str}</code>")
    if consecutive_losses > 0:
        lines.append(f"⚠️ Consec. losses: <code>{consecutive_losses}</code>")
    lines.append("")

    # ── POSITION ──
    if position and position.get("entry_price"):
        side = position.get("side", "?")
        entry = position["entry_price"]
        qty = position.get("quantity", 0)
        sl = current_sl if (current_sl is not None and current_sl > 0) \
            else (position.get("sl") or 0.0)
        tp = current_tp if (current_tp is not None and current_tp > 0) \
            else (position.get("tp") or 0.0)


        direction_emoji = "🟢" if side == "long" else "🔴"
        if side == "long":
            pnl_pct = _safe_dist_pct(current_price, entry)
            dist_sl = _safe_dist_pct(current_price, sl)
            dist_tp = _safe_dist_pct(tp, current_price)
        else:
            pnl_pct = _safe_dist_pct(entry, current_price)
            dist_sl = _safe_dist_pct(sl, current_price)
            dist_tp = _safe_dist_pct(current_price, tp)

        risk = abs(entry - sl)
        reward = abs(tp - entry)
        rr = reward / risk if risk > 0 else 0

        lines.append(f"━━━ {direction_emoji} <b>ACTIVE {side.upper()}</b> ━━━")
        lines.append(f"Entry: <code>${entry:,.2f}</code>")
        lines.append(f"Size:  <code>{qty:.4f}</code> BTC")
        lines.append(f"P&L:   {_pnl_emoji(pnl_pct)} <code>{pnl_pct:+.2f}%</code>")
        lines.append(f"SL:    <code>${sl:,.2f}</code> ({dist_sl:.2f}% away)")
        lines.append(f"TP:    <code>${tp:,.2f}</code> ({dist_tp:.2f}% away)")
        lines.append(f"RR:    <code>{rr:.1f}:1</code>")
        flags = []
        if breakeven_moved:
            flags.append("BE ✅")
        if profit_locked_pct > 0:
            flags.append(f"Locked {profit_locked_pct:.0f}%")
        if flags:
            lines.append(f"🔒 {' | '.join(flags)}")

        entry_time = position.get("entry_time", 0)
        if entry_time > 0:
            dur_min = (time.time() * 1000 - entry_time) / 60000
            lines.append(f"⏱️ Duration: <code>{dur_min:.1f}</code> min")
    else:
        lines.append("━━━ 📍 <b>NO POSITION</b> ━━━")
        lines.append(f"State: <code>{bot_state}</code>")
    lines.append("")

    # ── MARKET CONTEXT ──
    lines.append("━━━ 🌐 <b>MARKET CONTEXT</b> ━━━")
    lines.append(
        f"HTF Bias: {_bias_emoji(htf_bias)} <code>{htf_bias}</code>"
        f" (str: <code>{htf_bias_strength:.2f}</code>)"
    )
    lines.append(f"Daily Bias: {_bias_emoji(daily_bias)} <code>{daily_bias}</code>")
    lines.append(f"Session: {_session_emoji(session)} <code>{session}</code>")
    kz = "✅ ACTIVE" if in_killzone else "❌ No"
    lines.append(f"Killzone: {kz}")

    amd_emoji = {"ACCUMULATION": "🟡", "MANIPULATION": "🔴", "DISTRIBUTION": "🟢"}.get(amd_phase, "⚪")
    lines.append(f"AMD Phase: {amd_emoji} <code>{amd_phase}</code>")

    # Volume delta
    delta_pct = volume_delta.get("delta_pct", 0) * 100
    buy_vol = volume_delta.get("buy_volume", 0)
    sell_vol = volume_delta.get("sell_volume", 0)
    if abs(delta_pct) > 15:
        vd_tag = "STRONG " + ("BUYING 🟢🟢" if delta_pct > 0 else "SELLING 🔴🔴")
    elif abs(delta_pct) > 5:
        vd_tag = "BUYING 🟢" if delta_pct > 0 else "SELLING 🔴"
    else:
        vd_tag = "NEUTRAL ⚪"
    lines.append(f"Volume: {vd_tag} (<code>{delta_pct:+.1f}%</code>)")
    lines.append(f"  Buy: <code>{buy_vol:.3f}</code> | Sell: <code>{sell_vol:.3f}</code>")
    lines.append("")

    # ── ICT STRUCTURES SUMMARY ──
    active_bull_obs = [ob for ob in bullish_obs if ob.is_active(now_ms)]
    active_bear_obs = [ob for ob in bearish_obs if ob.is_active(now_ms)]
    active_bull_fvgs = [f for f in bullish_fvgs if f.is_active(now_ms)]
    active_bear_fvgs = [f for f in bearish_fvgs if f.is_active(now_ms)]
    active_liq = [lp for lp in liquidity_pools if not lp.swept]

    lines.append("━━━ 🏗️ <b>ICT STRUCTURES</b> ━━━")
    lines.append(
        f"OBs: 🟢<code>{len(active_bull_obs)}</code> 🔴<code>{len(active_bear_obs)}</code>"
        f"  |  FVGs: 🟢<code>{len(active_bull_fvgs)}</code> 🔴<code>{len(active_bear_fvgs)}</code>"
    )
    lines.append(
        f"Liquidity: <code>{len(active_liq)}</code> pools"
        f"  |  MS breaks: <code>{len(market_structures)}</code>"
    )

    # Nearest support/resistance from OBs
    if active_bull_obs:
        nearest = min(active_bull_obs, key=lambda ob: abs(ob.midpoint - current_price))
        d = _safe_dist_pct(current_price, nearest.midpoint)
        zone_tag = " ⚡IN ZONE" if nearest.in_optimal_zone(current_price) else ""
        lines.append(
            f"🟢 Nearest Bull OB: <code>${nearest.low:,.2f}-${nearest.high:,.2f}</code>"
            f" ({d:+.2f}%){zone_tag}"
        )

    if active_bear_obs:
        nearest = min(active_bear_obs, key=lambda ob: abs(ob.midpoint - current_price))
        d = _safe_dist_pct(nearest.midpoint, current_price)
        zone_tag = " ⚡IN ZONE" if nearest.in_optimal_zone(current_price) else ""
        lines.append(
            f"🔴 Nearest Bear OB: <code>${nearest.low:,.2f}-${nearest.high:,.2f}</code>"
            f" ({d:+.2f}%){zone_tag}"
        )

    # Nearest FVGs
    if active_bull_fvgs:
        nearest = min(active_bull_fvgs, key=lambda f: abs(f.midpoint - current_price))
        ifvg_tag = " [IFVG]" if nearest.is_ifvg else ""
        d = _safe_dist_pct(current_price, nearest.midpoint)
        lines.append(
            f"🟢 Nearest Bull FVG: <code>${nearest.bottom:,.2f}-${nearest.top:,.2f}</code>"
            f" ({d:+.2f}%){ifvg_tag}"
        )

    if active_bear_fvgs:
        nearest = min(active_bear_fvgs, key=lambda f: abs(f.midpoint - current_price))
        ifvg_tag = " [IFVG]" if nearest.is_ifvg else ""
        d = _safe_dist_pct(nearest.midpoint, current_price)
        lines.append(
            f"🔴 Nearest Bear FVG: <code>${nearest.bottom:,.2f}-${nearest.top:,.2f}</code>"
            f" ({d:+.2f}%){ifvg_tag}"
        )

    # Nearest liquidity
    eqh = [lp for lp in active_liq if lp.pool_type == "EQH"]
    eql = [lp for lp in active_liq if lp.pool_type == "EQL"]
    if eqh:
        nearest = min(eqh, key=lambda lp: abs(lp.price - current_price))
        d = _safe_dist_pct(nearest.price, current_price)
        lines.append(f"💧 Nearest EQH: <code>${nearest.price:,.2f}</code> ({d:+.2f}%)")
    if eql:
        nearest = min(eql, key=lambda lp: abs(lp.price - current_price))
        d = _safe_dist_pct(current_price, nearest.price)
        lines.append(f"💧 Nearest EQL: <code>${nearest.price:,.2f}</code> (-{d:.2f}%)")

    # Recent market structure
    if market_structures:
        last_ms = market_structures[-1]
        ms_emoji = "📈" if last_ms.direction == "bullish" else "📉"
        lines.append(
            f"{ms_emoji} Last MS: <code>{last_ms.structure_type}</code>"
            f" {last_ms.direction.upper()} @ <code>${last_ms.price:,.2f}</code>"
        )

    # Key swing levels (nearest support + resistance)
    if swing_highs:
        above = [s for s in swing_highs if s.price > current_price]
        if above:
            nearest_res = min(above, key=lambda s: s.price - current_price)
            d = _safe_dist_pct(nearest_res.price, current_price)
            lines.append(f"🔺 Swing Resistance: <code>${nearest_res.price:,.2f}</code> ({d:+.2f}%)")
    if swing_lows:
        below = [s for s in swing_lows if s.price < current_price]
        if below:
            nearest_sup = max(below, key=lambda s: s.price)
            d = _safe_dist_pct(current_price, nearest_sup.price)
            lines.append(f"🔻 Swing Support: <code>${nearest_sup.price:,.2f}</code> ({d:+.2f}%)")

    lines.append("")
    lines.append("💡 /structures for full ICT breakdown")

    # ── v9 Engine Status ──────────────────────────────────────────────────
    if extra_lines:
        lines.append("")
        lines.append("━━━ ⚙️ <b>v9 ENGINE</b> ━━━")
        for el in extra_lines:
            if el:
                lines.append(el)

    return "\n".join(lines)



# ============================================================================
# /structures COMMAND — DEEP ICT ANALYSIS
# ============================================================================

def format_structures_report(
    *,
    current_price: float,
    htf_bias: str,
    htf_bias_strength: float,
    daily_bias: str,
    session: str,
    in_killzone: bool,
    amd_phase: str,
    # All ICT structures
    bullish_obs: Optional[List] = None,
    bearish_obs: Optional[List] = None,
    bullish_fvgs: Optional[List] = None,
    bearish_fvgs: Optional[List] = None,
    liquidity_pools: Optional[List] = None,
    market_structures: Optional[List] = None,
    swing_highs: Optional[List] = None,
    swing_lows: Optional[List] = None,
    volume_delta: Optional[Dict] = None,
) -> str:
    """Full ICT structure dump for /structures command — exhaustive detail."""
    bullish_obs = bullish_obs or []
    bearish_obs = bearish_obs or []
    bullish_fvgs = bullish_fvgs or []
    bearish_fvgs = bearish_fvgs or []
    liquidity_pools = liquidity_pools or []
    market_structures = market_structures or []
    swing_highs = swing_highs or []
    swing_lows = swing_lows or []
    volume_delta = volume_delta or {}

    now_ms = int(time.time() * 1000)
    lines: List[str] = []

    # ── HEADER ──
    lines.append("🎯 <b>ICT STRUCTURE ANALYSIS</b>")
    lines.append(f"🕐 {_ts_now_utc()}")
    lines.append(f"💲 BTC: <code>${current_price:,.2f}</code>")
    lines.append("")

    # ── BIAS & SESSION ──
    lines.append("━━━ 🌐 <b>BIAS &amp; SESSION</b> ━━━")
    lines.append(
        f"HTF Bias: {_bias_emoji(htf_bias)} <code>{htf_bias}</code>"
        f" (strength: <code>{htf_bias_strength:.2f}</code>)"
    )
    lines.append(f"Daily Bias: {_bias_emoji(daily_bias)} <code>{daily_bias}</code>")
    lines.append(f"Session: {_session_emoji(session)} <code>{session}</code>")
    kz = "✅ ACTIVE" if in_killzone else "❌ No"
    lines.append(f"Killzone: {kz}")
    amd_e = {"ACCUMULATION": "🟡", "MANIPULATION": "🔴", "DISTRIBUTION": "🟢"}.get(amd_phase, "⚪")
    lines.append(f"AMD Phase: {amd_e} <code>{amd_phase}</code>")

    # Volume delta
    delta_pct = volume_delta.get("delta_pct", 0) * 100
    buy_vol = volume_delta.get("buy_volume", 0)
    sell_vol = volume_delta.get("sell_volume", 0)
    lines.append(
        f"Volume Δ: <code>{delta_pct:+.1f}%</code>"
        f"  (B: <code>{buy_vol:.3f}</code> / S: <code>{sell_vol:.3f}</code>)"
    )
    lines.append("")

    # ── ORDER BLOCKS ──
    active_bull_obs = [ob for ob in bullish_obs if ob.is_active(now_ms)]
    active_bear_obs = [ob for ob in bearish_obs if ob.is_active(now_ms)]

    lines.append("━━━ 📦 <b>ORDER BLOCKS</b> ━━━")

    if active_bull_obs:
        sorted_obs = sorted(active_bull_obs, key=lambda ob: ob.midpoint, reverse=True)
        lines.append(f"🟢 <b>Bullish OBs ({len(sorted_obs)}):</b>")
        for i, ob in enumerate(sorted_obs[:5], 1):
            d = _safe_dist_pct(current_price, ob.midpoint)
            in_zone = ob.in_optimal_zone(current_price)
            wick = " 🕯️WR" if ob.has_wick_rejection else ""
            zone_tag = " ⚡<b>IN ZONE</b>" if in_zone else ""
            age_min = (now_ms - ob.timestamp) / 60000
            lines.append(
                f"  {i}. <code>${ob.low:,.2f} – ${ob.high:,.2f}</code>"
                f" | str: <code>{ob.strength:.0f}</code>"
                f" | {d:+.2f}%{wick}{zone_tag}"
                f" | {age_min:.0f}m"
            )
    else:
        lines.append("🟢 Bullish OBs: <code>None</code>")

    if active_bear_obs:
        sorted_obs = sorted(active_bear_obs, key=lambda ob: ob.midpoint)
        lines.append(f"🔴 <b>Bearish OBs ({len(sorted_obs)}):</b>")
        for i, ob in enumerate(sorted_obs[:5], 1):
            d = _safe_dist_pct(ob.midpoint, current_price)
            in_zone = ob.in_optimal_zone(current_price)
            wick = " 🕯️WR" if ob.has_wick_rejection else ""
            zone_tag = " ⚡<b>IN ZONE</b>" if in_zone else ""
            age_min = (now_ms - ob.timestamp) / 60000
            lines.append(
                f"  {i}. <code>${ob.low:,.2f} – ${ob.high:,.2f}</code>"
                f" | str: <code>{ob.strength:.0f}</code>"
                f" | {d:+.2f}%{wick}{zone_tag}"
                f" | {age_min:.0f}m"
            )
    else:
        lines.append("🔴 Bearish OBs: <code>None</code>")
    lines.append("")

    # ── FAIR VALUE GAPS ──
    active_bull_fvgs = [f for f in bullish_fvgs if f.is_active(now_ms)]
    active_bear_fvgs = [f for f in bearish_fvgs if f.is_active(now_ms)]

    lines.append("━━━ 📐 <b>FAIR VALUE GAPS</b> ━━━")

    if active_bull_fvgs:
        sorted_fvgs = sorted(active_bull_fvgs, key=lambda f: f.midpoint, reverse=True)
        lines.append(f"🟢 <b>Bullish FVGs ({len(sorted_fvgs)}):</b>")
        for i, fvg in enumerate(sorted_fvgs[:5], 1):
            d = _safe_dist_pct(current_price, fvg.midpoint)
            ifvg = " ⚡<b>IFVG</b>" if fvg.is_ifvg else ""
            in_gap = " 🎯IN GAP" if fvg.is_price_in_gap(current_price) else ""
            size = fvg.top - fvg.bottom
            age_min = (now_ms - fvg.timestamp) / 60000
            lines.append(
                f"  {i}. <code>${fvg.bottom:,.2f} – ${fvg.top:,.2f}</code>"
                f" | sz: <code>${size:.2f}</code>"
                f" | {d:+.2f}%{ifvg}{in_gap}"
                f" | {age_min:.0f}m"
            )
    else:
        lines.append("🟢 Bullish FVGs: <code>None</code>")

    if active_bear_fvgs:
        sorted_fvgs = sorted(active_bear_fvgs, key=lambda f: f.midpoint)
        lines.append(f"🔴 <b>Bearish FVGs ({len(sorted_fvgs)}):</b>")
        for i, fvg in enumerate(sorted_fvgs[:5], 1):
            d = _safe_dist_pct(fvg.midpoint, current_price)
            ifvg = " ⚡<b>IFVG</b>" if fvg.is_ifvg else ""
            in_gap = " 🎯IN GAP" if fvg.is_price_in_gap(current_price) else ""
            size = fvg.top - fvg.bottom
            age_min = (now_ms - fvg.timestamp) / 60000
            lines.append(
                f"  {i}. <code>${fvg.bottom:,.2f} – ${fvg.top:,.2f}</code>"
                f" | sz: <code>${size:.2f}</code>"
                f" | {d:+.2f}%{ifvg}{in_gap}"
                f" | {age_min:.0f}m"
            )
    else:
        lines.append("🔴 Bearish FVGs: <code>None</code>")
    lines.append("")

    # ── LIQUIDITY POOLS ──
    active_liq = [lp for lp in liquidity_pools if not lp.swept]
    swept_liq = [lp for lp in liquidity_pools if lp.swept]
    eqh = sorted([lp for lp in active_liq if lp.pool_type == "EQH"], key=lambda x: x.price)
    eql = sorted([lp for lp in active_liq if lp.pool_type == "EQL"], key=lambda x: x.price, reverse=True)

    lines.append("━━━ 💧 <b>LIQUIDITY POOLS</b> ━━━")

    if eqh:
        lines.append(f"🟢 <b>Buy-Side / EQH ({len(eqh)}):</b>")
        for i, lp in enumerate(eqh[:4], 1):
            d = _safe_dist_pct(lp.price, current_price)
            lines.append(
                f"  {i}. <code>${lp.price:,.2f}</code>"
                f" | touches: <code>{lp.touch_count}</code>"
                f" | {d:+.2f}% above"
            )
    else:
        lines.append("🟢 Buy-Side / EQH: <code>None</code>")

    if eql:
        lines.append(f"🔴 <b>Sell-Side / EQL ({len(eql)}):</b>")
        for i, lp in enumerate(eql[:4], 1):
            d = _safe_dist_pct(current_price, lp.price)
            lines.append(
                f"  {i}. <code>${lp.price:,.2f}</code>"
                f" | touches: <code>{lp.touch_count}</code>"
                f" | {d:+.2f}% below"
            )
    else:
        lines.append("🔴 Sell-Side / EQL: <code>None</code>")

    if swept_liq:
        lines.append(f"✅ <b>Recently Swept ({len(swept_liq)}):</b>")
        for lp in swept_liq[-3:]:
            wr = " + WR ✅" if lp.wick_rejection else ""
            lines.append(f"  • {lp.pool_type} @ <code>${lp.price:,.2f}</code>{wr}")
    lines.append("")

    # ── MARKET STRUCTURE ──
    lines.append("━━━ 🔄 <b>MARKET STRUCTURE</b> ━━━")
    if market_structures:
        recent = sorted(market_structures, key=lambda ms: ms.timestamp, reverse=True)[:5]
        for ms in recent:
            ms_emoji = "📈" if ms.direction == "bullish" else "📉"
            d = _safe_dist_pct(current_price, ms.price)
            lines.append(
                f"  {ms_emoji} <code>{ms.structure_type}</code>"
                f" {ms.direction.upper()} @ <code>${ms.price:,.2f}</code>"
                f" ({d:+.2f}%)"
            )
    else:
        lines.append("  No structure breaks detected")
    lines.append("")

    # ── KEY SWING LEVELS ──
    lines.append("━━━ 📏 <b>KEY SWING LEVELS</b> ━━━")

    above_res = sorted(
        [s for s in swing_highs if s.price > current_price],
        key=lambda s: s.price,
    )[:3]
    below_sup = sorted(
        [s for s in swing_lows if s.price < current_price],
        key=lambda s: s.price,
        reverse=True,
    )[:3]

    if above_res:
        lines.append("<b>Resistance (above):</b>")
        for i, s in enumerate(above_res, 1):
            d = _safe_dist_pct(s.price, current_price)
            lines.append(f"  R{i}: <code>${s.price:,.2f}</code> ({d:+.2f}%)")
    else:
        lines.append("Resistance: <code>None detected</code>")

    if below_sup:
        lines.append("<b>Support (below):</b>")
        for i, s in enumerate(below_sup, 1):
            d = _safe_dist_pct(current_price, s.price)
            lines.append(f"  S{i}: <code>${s.price:,.2f}</code> ({d:.2f}% below)")
    else:
        lines.append("Support: <code>None detected</code>")

    return "\n".join(lines)


# ============================================================================
# EVENT MESSAGES
# ============================================================================

def format_entry_notification(
    side: str,
    entry_price: float,
    sl_price: float,
    tp_price: float,
    quantity: float,
    score: float,
    reasons: List[str],
) -> str:
    """Rich entry signal notification."""
    d = "🟢" if side.lower() == "long" else "🔴"
    risk = abs(entry_price - sl_price)
    reward = abs(tp_price - entry_price)
    rr = reward / risk if risk > 0 else 0

    if side.lower() == "long":
        risk_pct = (entry_price - sl_price) / entry_price * 100   # entry as base
    else:
        risk_pct = (sl_price - entry_price) / entry_price * 100   # entry as base


    lines = [
        f"{d} <b>{side.upper()} ENTRY PENDING</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Score: <code>{score:.0f}/100</code>",
        f"Entry: <code>${entry_price:,.2f}</code>",
        f"SL:    <code>${sl_price:,.2f}</code> (risk: {risk_pct:.2f}%)",
        f"TP:    <code>${tp_price:,.2f}</code>",
        f"RR:    <code>{rr:.1f}:1</code>",
        f"Size:  <code>{quantity:.4f}</code> BTC",
        "",
        "<b>Confluence:</b>",
    ]
    for r in reasons[:6]:
        lines.append(f"  • {r}")

    return "\n".join(lines)


def format_exit_notification(
    side: str,
    exit_type: str,
    entry_price: float,
    exit_price: float,
    quantity: float,
    pnl: float,
    pnl_pct: float,
) -> str:
    """Rich exit notification."""
    emoji = "✅" if pnl > 0 else "❌"
    lines = [
        f"{emoji} <b>{exit_type} HIT — {side.upper()}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Entry: <code>${entry_price:,.2f}</code>",
        f"Exit:  <code>${exit_price:,.2f}</code>",
        f"P&L:   {_pnl_emoji(pnl)} <code>${pnl:+.2f}</code> (<code>{pnl_pct:+.2f}%</code>)",
    ]
    return "\n".join(lines)


def format_bot_started_message() -> str:
    return (
        "🤖 <b>ICT TRADING BOT STARTED</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✅ WebSocket connected\n"
        "✅ Strategy active\n"
        "✅ Risk management enabled\n\n"
        f"🕐 {_ts_now_utc()}\n\n"
        "💡 /status — current state\n"
        "📊 /structures — ICT analysis"
    )


def format_bot_stopped_message() -> str:
    return (
        "🛑 <b>ICT TRADING BOT STOPPED</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "All orders cancelled\n"
        "WebSocket disconnected\n\n"
        f"🕐 {_ts_now_utc()}"
    )