"""
Enhanced Data Manager with Full OHLCV Candle Support + REST Warmup (Production Grade)

Pattern: Connect → Subscribe → REST Warmup → Ready
Based on proven Z-Score data manager architecture
"""

import time
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from collections import deque
from typing import Optional, Dict, List

import numpy as np

import config
from candle_compat import wrap_candles
from futures_websocket import FuturesWebSocket
from futures_api import FuturesAPI

logger = logging.getLogger(__name__)


# =====================================================================
# Candle model
# =====================================================================
@dataclass
class Candle:
    timestamp: float  # seconds
    open: float
    high: float
    low: float
    close: float
    volume: float

    def is_bullish(self) -> bool:
        return self.close > self.open

    def is_bearish(self) -> bool:
        return self.close < self.open

    def body_size(self) -> float:
        return abs(self.close - self.open)

    def total_range(self) -> float:
        return self.high - self.low

    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    def body_percentage(self) -> float:
        r = self.total_range()
        return (self.body_size() / r) if r > 0 else 0.0


# =====================================================================
# Stats
# =====================================================================
class StreamStats:
    def __init__(self) -> None:
        self._last_update: Optional[datetime] = None
        self._orderbook_count = 0
        self._trades_count = 0
        self._candles_count = 0
        self._lock = threading.RLock()

    def record_orderbook(self) -> None:
        with self._lock:
            self._orderbook_count += 1
            self._last_update = datetime.utcnow()

    def record_trade(self) -> None:
        with self._lock:
            self._trades_count += 1
            self._last_update = datetime.utcnow()

    def record_candle(self) -> None:
        with self._lock:
            self._candles_count += 1
            self._last_update = datetime.utcnow()

    def get_last_update(self) -> Optional[datetime]:
        with self._lock:
            return self._last_update


# =====================================================================
# Data Manager
# =====================================================================
class ICTDataManager:
    """
    Enhanced Data Manager for ICT Strategy
    - Multi-timeframe OHLCV candle storage
    - Orderbook + trades
    - REST warmup + readiness gating
    """

    def __init__(self) -> None:
        self.api = FuturesAPI(
            api_key=getattr(config, "COINSWITCH_API_KEY", None),
            secret_key=getattr(config, "COINSWITCH_SECRET_KEY", None),
        )
        self.ws: Optional[FuturesWebSocket] = None
        self.stats = StreamStats()

        # Candles
        self._candles_1m: deque[Candle] = deque(maxlen=2000)
        self._candles_5m: deque[Candle] = deque(maxlen=1200)
        self._candles_15m: deque[Candle] = deque(maxlen=800)
        self._candles_1h: deque[Candle] = deque(maxlen=500) 
        self._candles_4h: deque[Candle] = deque(maxlen=400)
        self._candles_1d: deque[Candle] = deque(maxlen=100)

        # Price / microstructure
        self._last_price: float = 0.0
        self._orderbook: Dict = {"bids": [], "asks": []}
        self._recent_trades: deque[Dict] = deque(maxlen=500)

        # Thread safety
        self._lock = threading.RLock()

        # Indicator cache
        self._ema_cache: Dict[str, float] = {}

        # Readiness
        self._strategy_ref = None   # set via register_strategy()
        self.is_ready: bool = False
        self.is_streaming: bool = False

        logger.info("ICTDataManager initialized (REST warmup + strict WS routing)")

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------
    def start(self) -> bool:
        """Connect WebSocket, subscribe to streams, then warm up from REST klines."""
        try:
            self.is_ready = False
            self.is_streaming = False
            
            logger.info("Starting WebSocket streams...")
            
            # Initialize WebSocket
            self.ws = FuturesWebSocket()
            
            # Connect FIRST
            logger.info("Connecting to CoinSwitch Futures WebSocket...")
            if not self.ws.connect(timeout=30):
                logger.error("❌ Failed to connect WebSocket")
                return False
            
            # Subscribe to orderbook and trades (callbacks will be added by subscribe methods)
            logger.info(f"Subscribing ORDERBOOK: {config.SYMBOL}")
            self.ws.subscribe_orderbook(config.SYMBOL, callback=self._on_orderbook_update)
            
            logger.info(f"Subscribing TRADES: {config.SYMBOL}")
            self.ws.subscribe_trades(config.SYMBOL, callback=self._on_trades_update)
            
            # Subscribe candlesticks with dedicated callbacks
            logger.info(f"Subscribing CANDLESTICKS 1m: {config.SYMBOL}_1")
            self.ws.subscribe_candlestick(
                pair=config.SYMBOL,
                interval=1,
                callback=self._on_candlestick_1m
            )
            
            logger.info(f"Subscribing CANDLESTICKS 5m: {config.SYMBOL}_5")
            self.ws.subscribe_candlestick(
                pair=config.SYMBOL,
                interval=5,
                callback=self._on_candlestick_5m
            )
            
            logger.info(f"Subscribing CANDLESTICKS 15m: {config.SYMBOL}_15")
            self.ws.subscribe_candlestick(
                pair=config.SYMBOL,
                interval=15,
                callback=self._on_candlestick_15m
            )

            logger.info(f"Subscribing CANDLESTICKS 1h: {config.SYMBOL}_60")
            self.ws.subscribe_candlestick(
                pair=config.SYMBOL,
                interval=60,
                callback=self._on_candlestick_1h
            )

            logger.info(f"Subscribing CANDLESTICKS 4h: {config.SYMBOL}_240")
            self.ws.subscribe_candlestick(
                pair=config.SYMBOL,
                interval=240,
                callback=self._on_candlestick_4h
            )

            logger.info(f"Subscribing CANDLESTICKS 1d: {config.SYMBOL}_1440")
            self.ws.subscribe_candlestick(
                pair=config.SYMBOL,
                interval=1440,
                callback=self._on_candlestick_1d
            )
            
            self.is_streaming = True
            logger.info("✓ WebSocket streams started successfully")
            logger.info("  - Order Book: Active")
            logger.info("  - Trades: Active")
            logger.info("  - Candles: 1m, 5m, 15m, 1h, 4h, 1d")  # ← UPDATE
            
            # ✅ CRITICAL: REST warmup so cold start has data immediately
            logger.info("Warming up candles from REST API...")
            self._warmup_from_klines_1m()
            self._warmup_from_klines_5m()
            self._warmup_from_klines_15m()
            self._warmup_from_klines_1h()  
            self._warmup_from_klines_4h()
            self._warmup_from_klines_1d()  
            
            # Mark ready if minimum candles exist
            self.is_ready = self._check_minimum_data()
            logger.info(
                f"DataManager ready={self.is_ready} "
                f"(1m={len(self._candles_1m)} 5m={len(self._candles_5m)} "
                f"15m={len(self._candles_15m)} 1h={len(self._candles_1h)} " 
                f"4h={len(self._candles_4h)} 1d={len(self._candles_1d)})"   
            )
            
            return True
            
        except Exception as e:
            logger.error(f"Error starting ICTDataManager: {e}", exc_info=True)
            self.is_ready = False
            self.is_streaming = False
            return False

    def register_strategy(self, strategy) -> None:
        """
        Register strategy instance so trade stream can feed
        VolumeProfileAnalyzer and AdvancedLiquidityModel in real time.
        Must be called AFTER both data_manager.start() and strategy.__init__().
        """
        self._strategy_ref = strategy
        logger.info("✅ Strategy reference registered in DataManager "
                    "(trade stream → VolumeProfileAnalyzer active)")

    def stop(self) -> None:
        try:
            self.is_ready = False
            self.is_streaming = False
            if self.ws:
                self.ws.disconnect()
            logger.info("✓ ICTDataManager stopped")
        except Exception as e:
            logger.error(f"Error stopping ICTDataManager: {e}")

    def restart_streams(self) -> bool:
        """Restart WebSocket with state preservation"""
        try:
            logger.warning("Restarting streams (will re-warmup)")
            self.stop()
            time.sleep(2.0)
            return self.start()
        except Exception as e:
            logger.error(f"Error restarting ICTDataManager: {e}", exc_info=True)
            return False

    # -----------------------------------------------------------------
    # Warmup helpers
    # -----------------------------------------------------------------
    def _warmup_from_klines_1m(self, limit: int = 100) -> None:
        """Warm up 1m candles from REST"""
        try:
            end_ms = int(time.time() * 1000)
            start_ms = end_ms - limit * 60 * 1000
            
            params = {
                "symbol": config.SYMBOL,
                "exchange": config.EXCHANGE,
                "interval": "1",
                "start_time": start_ms,
                "end_time": end_ms,
                "limit": limit,
            }
            
            logger.info(f"Warmup 1m: fetching {limit} candles")
            resp = self.api._make_request(
                method="GET",
                endpoint="/trade/api/v2/futures/klines",
                params=params,
                payload={},
            )
            
            if not isinstance(resp, dict):
                logger.warning(f"Warmup 1m: unexpected response type {type(resp)}")
                return
            
            data = resp.get("data", [])
            if not data:
                logger.warning("Warmup 1m: no data returned")
                return
            
            seeded = 0
            for k in sorted(data, key=lambda x: int(x.get("close_time") or x.get("start_time") or 0)):
                try:
                    candle = Candle(
                        timestamp=float(k.get("close_time") or k.get("start_time") or 0) / 1000.0,
                        open=float(k.get("o") or k.get("open") or 0),
                        high=float(k.get("h") or k.get("high") or 0),
                        low=float(k.get("l") or k.get("low") or 0),
                        close=float(k.get("c") or k.get("close") or 0),
                        volume=float(k.get("v") or k.get("volume") or 0),
                    )
                    if candle.close > 0:
                        self._candles_1m.append(candle)
                        self._last_price = candle.close
                        seeded += 1
                except Exception:
                    continue
            
            if seeded > 0:
                logger.info(f"Warmup 1m complete: {seeded} candles, last_price={self._last_price:.2f}")
            else:
                logger.warning("Warmup 1m: no valid candles parsed")
                
        except Exception as e:
            logger.error(f"Error in 1m warmup: {e}", exc_info=True)

    def _warmup_from_klines_5m(self, limit: int = 100) -> None:
        """Warm up 5m candles from REST"""
        try:
            end_ms = int(time.time() * 1000)
            start_ms = end_ms - limit * 5 * 60 * 1000
            
            params = {
                "symbol": config.SYMBOL,
                "exchange": config.EXCHANGE,
                "interval": "5",
                "start_time": start_ms,
                "end_time": end_ms,
                "limit": limit,
            }
            
            logger.info(f"Warmup 5m: fetching {limit} candles")
            resp = self.api._make_request(
                method="GET",
                endpoint="/trade/api/v2/futures/klines",
                params=params,
                payload={},
            )
            
            if not isinstance(resp, dict):
                return
            
            data = resp.get("data", [])
            if not data:
                logger.warning("Warmup 5m: no data returned")
                return
            
            seeded = 0
            for k in sorted(data, key=lambda x: int(x.get("close_time") or x.get("start_time") or 0)):
                try:
                    candle = Candle(
                        timestamp=float(k.get("close_time") or k.get("start_time") or 0) / 1000.0,
                        open=float(k.get("o") or k.get("open") or 0),
                        high=float(k.get("h") or k.get("high") or 0),
                        low=float(k.get("l") or k.get("low") or 0),
                        close=float(k.get("c") or k.get("close") or 0),
                        volume=float(k.get("v") or k.get("volume") or 0),
                    )
                    if candle.close > 0:
                        self._candles_5m.append(candle)
                        seeded += 1
                except Exception:
                    continue
            
            if seeded > 0:
                logger.info(f"Warmup 5m complete: {seeded} candles")
            else:
                logger.warning("Warmup 5m: no valid candles parsed")
                
        except Exception as e:
            logger.error(f"Error in 5m warmup: {e}", exc_info=True)

    def _warmup_from_klines_15m(self, limit: int = 100) -> None:
        """Warm up 15m candles from REST"""
        try:
            end_ms = int(time.time() * 1000)
            start_ms = end_ms - limit * 15 * 60 * 1000
            
            params = {
                "symbol": config.SYMBOL,
                "exchange": config.EXCHANGE,
                "interval": "15",
                "start_time": start_ms,
                "end_time": end_ms,
                "limit": limit,
            }
            
            logger.info(f"Warmup 15m: fetching {limit} candles")
            resp = self.api._make_request(
                method="GET",
                endpoint="/trade/api/v2/futures/klines",
                params=params,
                payload={},
            )
            
            if not isinstance(resp, dict):
                return
            
            data = resp.get("data", [])
            if not data:
                logger.warning("Warmup 15m: no data returned")
                return
            
            seeded = 0
            for k in sorted(data, key=lambda x: int(x.get("close_time") or x.get("start_time") or 0)):
                try:
                    candle = Candle(
                        timestamp=float(k.get("close_time") or k.get("start_time") or 0) / 1000.0,
                        open=float(k.get("o") or k.get("open") or 0),
                        high=float(k.get("h") or k.get("high") or 0),
                        low=float(k.get("l") or k.get("low") or 0),
                        close=float(k.get("c") or k.get("close") or 0),
                        volume=float(k.get("v") or k.get("volume") or 0),
                    )
                    if candle.close > 0:
                        self._candles_15m.append(candle)
                        seeded += 1
                except Exception:
                    continue
            
            if seeded > 0:
                logger.info(f"Warmup 15m complete: {seeded} candles")
            else:
                logger.warning("Warmup 15m: no valid candles parsed")
                
        except Exception as e:
            logger.error(f"Error in 15m warmup: {e}", exc_info=True)

    def _warmup_from_klines_1h(self, limit: int = 100) -> None:
        """Warm up 1h candles from REST"""
        try:
            end_ms = int(time.time() * 1000)
            start_ms = end_ms - limit * 60 * 60 * 1000
            
            params = {
                "symbol": config.SYMBOL,
                "exchange": config.EXCHANGE,
                "interval": "60",
                "start_time": start_ms,
                "end_time": end_ms,
                "limit": limit,
            }
            
            logger.info(f"Warmup 1h: fetching {limit} candles")
            resp = self.api._make_request(
                method="GET",
                endpoint="/trade/api/v2/futures/klines",
                params=params,
                payload={},
            )
            
            if not isinstance(resp, dict):
                return
            
            data = resp.get("data", [])
            if not data:
                logger.warning("Warmup 1h: no data returned")
                return
            
            seeded = 0
            for k in sorted(data, key=lambda x: int(x.get("close_time") or x.get("start_time") or 0)):
                try:
                    candle = Candle(
                        timestamp=float(k.get("close_time") or k.get("start_time") or 0) / 1000.0,
                        open=float(k.get("o") or k.get("open") or 0),
                        high=float(k.get("h") or k.get("high") or 0),
                        low=float(k.get("l") or k.get("low") or 0),
                        close=float(k.get("c") or k.get("close") or 0),
                        volume=float(k.get("v") or k.get("volume") or 0),
                    )
                    if candle.close > 0:
                        self._candles_1h.append(candle)
                        seeded += 1
                except Exception:
                    continue
            
            if seeded > 0:
                logger.info(f"Warmup 1h complete: {seeded} candles")
            else:
                logger.warning("Warmup 1h: no valid candles parsed")
                
        except Exception as e:
            logger.error(f"Error in 1h warmup: {e}", exc_info=True)


    def _warmup_from_klines_4h(self, limit: int = 50) -> None:
        """Warm up 4h candles from REST"""
        try:
            end_ms = int(time.time() * 1000)
            start_ms = end_ms - limit * 240 * 60 * 1000
            
            params = {
                "symbol": config.SYMBOL,
                "exchange": config.EXCHANGE,
                "interval": "240",
                "start_time": start_ms,
                "end_time": end_ms,
                "limit": limit,
            }
            
            logger.info(f"Warmup 4h: fetching {limit} candles")
            resp = self.api._make_request(
                method="GET",
                endpoint="/trade/api/v2/futures/klines",
                params=params,
                payload={},
            )
            
            if not isinstance(resp, dict):
                return
            
            data = resp.get("data", [])
            if not data:
                logger.warning("Warmup 4h: no data returned")
                return
            
            seeded = 0
            for k in sorted(data, key=lambda x: int(x.get("close_time") or x.get("start_time") or 0)):
                try:
                    candle = Candle(
                        timestamp=float(k.get("close_time") or k.get("start_time") or 0) / 1000.0,
                        open=float(k.get("o") or k.get("open") or 0),
                        high=float(k.get("h") or k.get("high") or 0),
                        low=float(k.get("l") or k.get("low") or 0),
                        close=float(k.get("c") or k.get("close") or 0),
                        volume=float(k.get("v") or k.get("volume") or 0),
                    )
                    if candle.close > 0:
                        self._candles_4h.append(candle)
                        seeded += 1
                except Exception:
                    continue
            
            if seeded > 0:
                logger.info(f"Warmup 4h complete: {seeded} candles")
            else:
                logger.warning("Warmup 4h: no valid candles parsed")
                
        except Exception as e:
            logger.error(f"Error in 4h warmup: {e}", exc_info=True)

    def _warmup_from_klines_1d(self, limit: int = 30) -> None:
        """Warm up 1d candles from REST"""
        try:
            end_ms = int(time.time() * 1000)
            start_ms = end_ms - limit * 24 * 60 * 60 * 1000
            
            params = {
                "symbol": config.SYMBOL,
                "exchange": config.EXCHANGE,
                "interval": "1440",
                "start_time": start_ms,
                "end_time": end_ms,
                "limit": limit,
            }
            
            logger.info(f"Warmup 1d: fetching {limit} candles")
            resp = self.api._make_request(
                method="GET",
                endpoint="/trade/api/v2/futures/klines",
                params=params,
                payload={},
            )
            
            if not isinstance(resp, dict):
                return
            
            data = resp.get("data", [])
            if not data:
                logger.warning("Warmup 1d: no data returned")
                return
            
            seeded = 0
            for k in sorted(data, key=lambda x: int(x.get("close_time") or x.get("start_time") or 0)):
                try:
                    candle = Candle(
                        timestamp=float(k.get("close_time") or k.get("start_time") or 0) / 1000.0,
                        open=float(k.get("o") or k.get("open") or 0),
                        high=float(k.get("h") or k.get("high") or 0),
                        low=float(k.get("l") or k.get("low") or 0),
                        close=float(k.get("c") or k.get("close") or 0),
                        volume=float(k.get("v") or k.get("volume") or 0),
                    )
                    if candle.close > 0:
                        self._candles_1d.append(candle)
                        seeded += 1
                except Exception:
                    continue
            
            if seeded > 0:
                logger.info(f"Warmup 1d complete: {seeded} candles")
            else:
                logger.warning("Warmup 1d: no valid candles parsed")
                
        except Exception as e:
            logger.error(f"Error in 1d warmup: {e}", exc_info=True)

    def _check_minimum_data(self) -> bool:
        """Check if we have minimum candles to start trading"""
        min_1m = 50
        min_5m = 50
        min_15m = 30
        min_1h = 20 
        min_4h = 10
        min_1d = 7 
        
        has_min = (
            len(self._candles_1m) >= min_1m and
            len(self._candles_5m) >= min_5m and
            len(self._candles_15m) >= min_15m and
            len(self._candles_1h) >= min_1h and
            len(self._candles_4h) >= min_4h and
            len(self._candles_1d) >= min_1d 
        )
        return has_min

    def wait_until_ready(self, timeout_sec: float = 120.0) -> bool:
        """Block until data manager is ready or timeout"""
        start = time.time()
        while not self.is_ready and (time.time() - start) < timeout_sec:
            time.sleep(1.0)
            # Re-check in case WS filled candles
            if not self.is_ready:
                self.is_ready = self._check_minimum_data()
        
        return self.is_ready

    # -----------------------------------------------------------------
    # WebSocket callbacks
    # -----------------------------------------------------------------
    def _on_orderbook_update(self, data: Dict) -> None:
        try:
            with self._lock:
                self._orderbook = {
                    "bids": data.get("b", data.get("bids", [])),
                    "asks": data.get("a", data.get("asks", [])),
                }
                
                if self._orderbook["bids"] and self._orderbook["asks"]:
                    best_bid = float(self._orderbook["bids"][0][0])
                    best_ask = float(self._orderbook["asks"][0][0])
                    self._last_price = (best_bid + best_ask) / 2.0
                
                self.stats.record_orderbook()
        except Exception as e:
            logger.debug(f"Error processing orderbook: {e}")

    def _on_trades_update(self, data: Dict) -> None:
        try:
            with self._lock:
                price = float(data.get("p", 0))
                qty   = float(data.get("q", 0))
                is_buyer_maker = data.get("m", False)
                side  = "sell" if is_buyer_maker else "buy"

                if price > 0:
                    self._last_price = price
                    self._recent_trades.append({
                        "price":     price,
                        "quantity":  qty,
                        "side":      side,
                        "timestamp": time.time(),
                    })

                    # ── Feed VolumeProfileAnalyzer ────────────────────────
                    if self._strategy_ref is not None:
                        try:
                            va = getattr(self._strategy_ref, "volume_analyzer", None)
                            if va is not None:
                                va.on_candle({
                                    'o': price, 'h': price, 'l': price, 'c': price,
                                    'v': qty,
                                    't': int(time.time() * 1000),
                                })
                        except Exception:
                            pass
                    # ─────────────────────────────────────────────────────

                self.stats.record_trade()
        except Exception as e:
            logger.debug(f"Error processing trade: {e}")


    def _on_candlestick_1m(self, data: Dict) -> None:
        """Process 1m candlestick"""
        try:
            with self._lock:
                candle = Candle(
                    timestamp=float(data.get('t', 0)) / 1000.0,
                    open=float(data.get('o', 0)),
                    high=float(data.get('h', 0)),
                    low=float(data.get('l', 0)),
                    close=float(data.get('c', 0)),
                    volume=float(data.get('v', 0)),
                )

                if candle.close <= 0:
                    return

                self._last_price = candle.close          # ← FIXED
                is_closed = data.get('x', False)

                if is_closed or not self._candles_1m:
                    self._candles_1m.append(candle)
                    logger.info(f"✅ 1m CLOSED @ ${candle.close:.2f} ({len(self._candles_1m)})")
                else:
                    self._candles_1m[-1] = candle

                self.stats.record_candle()
                if is_closed:
                    self._ema_cache.clear()              # ← FIXED
        except Exception as e:
            logger.error(f"1m error: {e}")


    def _on_candlestick_5m(self, data: Dict) -> None:
        """Process 5m candlestick"""
        try:
            with self._lock:
                candle = Candle(
                    timestamp=float(data.get('t', 0)) / 1000.0,
                    open=float(data.get('o', 0)),
                    high=float(data.get('h', 0)),
                    low=float(data.get('l', 0)),
                    close=float(data.get('c', 0)),
                    volume=float(data.get('v', 0)),
                )

                if candle.close <= 0:
                    return

                self._last_price = candle.close          # ← FIXED
                is_closed = data.get('x', False)

                if is_closed or not self._candles_5m:
                    self._candles_5m.append(candle)
                    logger.info(f"✅ 5m CLOSED @ ${candle.close:.2f} ({len(self._candles_5m)})")
                else:
                    self._candles_5m[-1] = candle

                self.stats.record_candle()
                if is_closed:
                    self._ema_cache.clear()              # ← FIXED
        except Exception as e:
            logger.error(f"5m error: {e}")


    def _on_candlestick_15m(self, data: Dict) -> None:
        """Process 15m candlestick"""
        try:
            with self._lock:
                candle = Candle(
                    timestamp=float(data.get('t', 0)) / 1000.0,
                    open=float(data.get('o', 0)),
                    high=float(data.get('h', 0)),
                    low=float(data.get('l', 0)),
                    close=float(data.get('c', 0)),
                    volume=float(data.get('v', 0)),
                )

                if candle.close <= 0:
                    return

                self._last_price = candle.close          # ← FIXED
                is_closed = data.get('x', False)

                if is_closed or not self._candles_15m:
                    self._candles_15m.append(candle)
                    logger.info(f"✅ 15m CLOSED @ ${candle.close:.2f} ({len(self._candles_15m)})")
                else:
                    self._candles_15m[-1] = candle

                self.stats.record_candle()
                if is_closed:
                    self._ema_cache.clear()              # ← FIXED
        except Exception as e:
            logger.error(f"15m error: {e}")

    def _on_candlestick_1h(self, data: Dict) -> None:
        """Process 1h candlestick"""
        try:
            with self._lock:
                candle = Candle(
                    timestamp=float(data.get('t', 0)) / 1000.0,
                    open=float(data.get('o', 0)),
                    high=float(data.get('h', 0)),
                    low=float(data.get('l', 0)),
                    close=float(data.get('c', 0)),
                    volume=float(data.get('v', 0)),
                )

                if candle.close <= 0:
                    return

                self._last_price = candle.close
                is_closed = data.get('x', False)

                if is_closed or not self._candles_1h:
                    self._candles_1h.append(candle)
                    logger.info(f"✅ 1h CLOSED @ ${candle.close:.2f} ({len(self._candles_1h)})")
                else:
                    self._candles_1h[-1] = candle

                self.stats.record_candle()
                if is_closed:
                    self._ema_cache.clear()
        except Exception as e:
            logger.error(f"1h error: {e}")


    def _on_candlestick_4h(self, data: Dict) -> None:
        """Process 4h candlestick"""
        try:
            with self._lock:
                candle = Candle(
                    timestamp=float(data.get('t', 0)) / 1000.0,
                    open=float(data.get('o', 0)),
                    high=float(data.get('h', 0)),
                    low=float(data.get('l', 0)),
                    close=float(data.get('c', 0)),
                    volume=float(data.get('v', 0)),
                )

                if candle.close <= 0:
                    return

                self._last_price = candle.close          # ← FIXED
                is_closed = data.get('x', False)

                if is_closed or not self._candles_4h:
                    self._candles_4h.append(candle)
                    logger.info(f"✅ 4h CLOSED @ ${candle.close:.2f} ({len(self._candles_4h)})")
                else:
                    self._candles_4h[-1] = candle

                self.stats.record_candle()
                if is_closed:
                    self._ema_cache.clear()              # ← FIXED
        except Exception as e:
            logger.error(f"4h error: {e}")

    def _on_candlestick_1d(self, data: Dict) -> None:
        """Process 1d candlestick"""
        try:
            with self._lock:
                candle = Candle(
                    timestamp=float(data.get('t', 0)) / 1000.0,
                    open=float(data.get('o', 0)),
                    high=float(data.get('h', 0)),
                    low=float(data.get('l', 0)),
                    close=float(data.get('c', 0)),
                    volume=float(data.get('v', 0)),
                )

                if candle.close <= 0:
                    return

                self._last_price = candle.close
                is_closed = data.get('x', False)

                if is_closed or not self._candles_1d:
                    self._candles_1d.append(candle)
                    logger.info(f"✅ 1d CLOSED @ ${candle.close:.2f} ({len(self._candles_1d)})")
                else:
                    self._candles_1d[-1] = candle

                self.stats.record_candle()
                if is_closed:
                    self._ema_cache.clear()
        except Exception as e:
            logger.error(f"1d error: {e}")

    # -----------------------------------------------------------------
    # Public data access
    # -----------------------------------------------------------------
    def get_last_price(self) -> float:
        with self._lock:
            return self._last_price

    def get_recent_candles(self, timeframe: str = "1m", limit: int = 50) -> List[Candle]:
        with self._lock:
            if timeframe == "1m":
                candles = list(self._candles_1m)
            elif timeframe == "5m":
                candles = list(self._candles_5m)
            elif timeframe == "15m":
                candles = list(self._candles_15m)
            elif timeframe == "1h":  # ← ADD
                candles = list(self._candles_1h)
            elif timeframe == "4h":
                candles = list(self._candles_4h)
            elif timeframe == "1d":  # ← ADD
                candles = list(self._candles_1d)
            else:
                logger.warning(f"Unknown timeframe: {timeframe}, defaulting to 1m")
                candles = list(self._candles_1m)
            
            result = candles[-limit:] if candles else []
        return wrap_candles(result)


    def get_orderbook_snapshot(self) -> Dict:
        with self._lock:
            return {
                "bids": self._orderbook.get("bids", [])[:20],
                "asks": self._orderbook.get("asks", [])[:20],
            }

    def get_recent_trades(self, limit: int = 50) -> List[Dict]:
        with self._lock:
            return list(self._recent_trades)[-limit:]

    # -----------------------------------------------------------------
    # Technical indicators
    # -----------------------------------------------------------------
    def get_ema(self, period: int = 20, timeframe: str = "5m") -> Optional[float]:
        cache_key = f"{timeframe}_{period}"
        with self._lock:
            if cache_key in self._ema_cache:
                return self._ema_cache[cache_key]
            
            candles = self.get_recent_candles(timeframe, limit=period * 2)
            if len(candles) < period:
                return None
            
            closes = np.array([c.close for c in candles])
            multiplier = 2.0 / (period + 1)
            ema = closes[0]
            for close in closes[1:]:
                ema = (close * multiplier) + (ema * (1 - multiplier))
            
            self._ema_cache[cache_key] = ema
            return ema

    def get_atr(self, period: int = 14, timeframe: str = "5m") -> Optional[float]:
        candles = self.get_recent_candles(timeframe, limit=period + 1)
        if len(candles) < period + 1:
            return None
        
        trs = []
        for i in range(1, len(candles)):
            high_low = candles[i].high - candles[i].low
            high_close = abs(candles[i].high - candles[i-1].close)
            low_close = abs(candles[i].low - candles[i-1].close)
            tr = max(high_low, high_close, low_close)
            trs.append(tr)
        
        return np.mean(trs) if trs else None

    def get_atr_percent(self, period: int = 14, timeframe: str = "5m") -> Optional[float]:
        atr = self.get_atr(period, timeframe)
        current_price = self.get_last_price()
        if atr and current_price > 0:
            return atr / current_price
        return None

    def get_volume_delta(self, lookback_seconds: float = 60.0) -> Dict:
        with self._lock:
            now = time.time()
            cutoff = now - lookback_seconds
            buy_vol = 0.0
            sell_vol = 0.0
            
            for trade in self._recent_trades:
                if trade["timestamp"] >= cutoff:
                    qty = trade["quantity"]
                    if trade["side"] == "buy":
                        buy_vol += qty
                    else:
                        sell_vol += qty
            
            total_vol = buy_vol + sell_vol
            delta = buy_vol - sell_vol
            delta_pct = delta / total_vol if total_vol > 0 else 0.0
            
            return {
                "buy_volume": buy_vol,
                "sell_volume": sell_vol,
                "delta": delta,
                "delta_pct": delta_pct,
            }

    def get_orderbook_imbalance(self, depth_levels: int = 10) -> Dict:
        with self._lock:
            bids = self._orderbook.get("bids", [])[:depth_levels]
            asks = self._orderbook.get("asks", [])[:depth_levels]
            
            bid_vol = sum(float(b[1]) for b in bids)
            ask_vol = sum(float(a[1]) for a in asks)
            total_vol = bid_vol + ask_vol
            imbalance = (bid_vol - ask_vol) / total_vol if total_vol > 0 else 0.0
            
            return {
                "bid_volume": bid_vol,
                "ask_volume": ask_vol,
                "imbalance": imbalance,
            }

    def get_htf_trend(self, ema_period: int = 34, timeframe: str = "15m") -> Optional[str]:
        ema = self.get_ema(ema_period, timeframe)
        current_price = self.get_last_price()
        
        if ema and current_price > 0:
            if current_price > ema * 1.001:
                return "BULLISH"
            elif current_price < ema * 0.999:
                return "BEARISH"
        return None

    def get_candles(self, timeframe: str, limit: int = 500) -> List[Dict]:
        """
        Strategy-facing candle accessor.
        Returns list of dicts: {'o', 'h', 'l', 'c', 'v', 't'(ms)}
        — the exact format consumed by strategy.py, regime_engine.py,
        and all structure detection methods.
        """
        raw = self.get_recent_candles(timeframe, limit=limit)
        result: List[Dict] = []
        for c in raw:
            if hasattr(c, 'open'):          # Candle dataclass
                result.append({
                    'o': c.open,
                    'h': c.high,
                    'l': c.low,
                    'c': c.close,
                    'v': c.volume,
                    't': int(c.timestamp * 1000),   # seconds → ms
                })
            elif isinstance(c, dict):       # already wrapped by candle_compat
                # Normalise key names in case wrap_candles uses long names
                result.append({
                    'o': float(c.get('o') or c.get('open',  0)),
                    'h': float(c.get('h') or c.get('high',  0)),
                    'l': float(c.get('l') or c.get('low',   0)),
                    'c': float(c.get('c') or c.get('close', 0)),
                    'v': float(c.get('v') or c.get('volume',0)),
                    't': int(  c.get('t') or c.get('timestamp', 0)),
                })
        return result


# Backward compatibility
ZScoreDataManager = ICTDataManager
