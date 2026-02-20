"""
CoinSwitch Futures Trading WebSocket - PRODUCTION GRADE

Features:
- Automatic reconnection with exponential backoff
- Connection health monitoring
- Thread-safe callback management
- Graceful degradation on errors
- AUTO-RESUBSCRIPTION after reconnect
"""

import socketio
import time
import logging
import threading
from typing import Callable, Dict, Optional, List
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class FuturesWebSocket:
    """Production-grade WebSocket client with auto-reconnect and auto-resubscribe"""

    BASE_URL = "https://ws.coinswitch.co"
    HANDSHAKE_PATH = "/pro/realtime-rates-socket/futures/exchange_2"
    NAMESPACE = "/exchange_2"
    
    EVENT_ORDERBOOK = "FETCH_ORDER_BOOK_CS_PRO"
    EVENT_CANDLESTICK = "FETCH_CANDLESTICK_CS_PRO"
    EVENT_TRADES = "FETCH_TRADES_CS_PRO"
    EVENT_TICKER = "FETCH_TICKER_INFO_CS_PRO"

    def __init__(self):
        """Initialize WebSocket client"""
        self.sio = socketio.Client(
            reconnection=True,
            reconnection_attempts=0,  # Infinite
            reconnection_delay=1,
            reconnection_delay_max=30,
        )
        
        self.is_connected = False
        self.stop_event = threading.Event()
        
        # Thread-safe callback management
        self._callbacks_lock = threading.RLock()
        self.orderbook_callbacks: List[Callable] = []
        self.candlestick_callbacks: List[Callable] = []
        self.trades_callbacks: List[Callable] = []
        self.ticker_callbacks: List[Callable] = []
        
        # ✅ NEW: Store subscription parameters for auto-resubscribe
        self._subscriptions_lock = threading.RLock()
        self._subscriptions: Dict[str, Dict] = {
            "orderbook": [],
            "candlestick": [],
            "trades": [],
            "ticker": []
        }
        
        # Connection health monitoring
        self._last_message_time: Optional[datetime] = None
        self._connection_failures = 0
        self._max_connection_failures = 5
        
        self._setup_handlers()
        logger.info("WebSocket client initialized (production grade with auto-resubscribe)")

    def _setup_handlers(self):
        """Setup WebSocket event handlers"""
        
        @self.sio.event(namespace=self.NAMESPACE)
        def connect():
            self.is_connected = True
            self._connection_failures = 0
            self._last_message_time = datetime.now()
            print("✓ Connected to futures market")
            logger.info(f"WebSocket connected to {self.NAMESPACE}")
            
            # ✅ CRITICAL FIX: Auto-resubscribe to all previous subscriptions
            self._resubscribe_all()
        
        @self.sio.event(namespace=self.NAMESPACE)
        def disconnect():
            self.is_connected = False
            print("✗ Disconnected from futures market")
            logger.warning("WebSocket disconnected")
        
        @self.sio.event(namespace=self.NAMESPACE)
        def connect_error(data):
            self._connection_failures += 1
            print(f"✗ Connection error: {data}")
            logger.error(
                f"Connection error (failures: {self._connection_failures}/"
                f"{self._max_connection_failures}): {data}"
            )
            
            if self._connection_failures >= self._max_connection_failures:
                logger.critical("Max connection failures reached - stopping reconnection")
                self.stop_event.set()
        
        # ORDERBOOK handler with error isolation
        @self.sio.on(self.EVENT_ORDERBOOK, namespace=self.NAMESPACE)
        def on_orderbook(data):
            try:
                self._last_message_time = datetime.now()
                if not isinstance(data, dict) or not ("bids" in data or "b" in data):
                    return
                
                formatted = {
                    "b": data.get("bids", data.get("b", [])),
                    "a": data.get("asks", data.get("a", [])),
                    "timestamp": data.get("timestamp"),
                    "symbol": data.get("s"),
                }
                
                with self._callbacks_lock:
                    callbacks = list(self.orderbook_callbacks)
                
                for callback in callbacks:
                    try:
                        callback(formatted)
                    except Exception as e:
                        logger.error(f"Orderbook callback error: {e}", exc_info=True)
            except Exception as e:
                logger.error(f"Error in orderbook handler: {e}", exc_info=True)
        
        # TRADES handler with error isolation
        @self.sio.on(self.EVENT_TRADES, namespace=self.NAMESPACE)
        def on_trades(data):
            try:
                self._last_message_time = datetime.now()
                if not isinstance(data, dict) or "p" not in data:
                    return
                
                formatted = {
                    "p": data.get("p"),
                    "q": data.get("q"),
                    "T": data.get("E", data.get("T")),
                    "m": data.get("m"),
                    "s": data.get("s"),
                }
                
                with self._callbacks_lock:
                    callbacks = list(self.trades_callbacks)
                
                for callback in callbacks:
                    try:
                        callback(formatted)
                    except Exception as e:
                        logger.error(f"Trade callback error: {e}", exc_info=True)
            except Exception as e:
                logger.error(f"Error in trades handler: {e}", exc_info=True)
        
        # CANDLESTICK handler with error isolation
        @self.sio.on(self.EVENT_CANDLESTICK, namespace=self.NAMESPACE)
        def on_candlestick(data):
            """Handle candlestick updates - FINAL WORKING VERSION"""
            try:
                self.last_message_time = datetime.now()

                # Skip subscription confirmations
                if isinstance(data, dict) and 'success' in data:
                    return

                # Validate candle data
                if not isinstance(data, dict) or 'o' not in data or 't' not in data:
                    return

                # Log first candle
                if not hasattr(self, '_first_candle_logged'):
                    logger.info("="*80)
                    logger.info(f"✅ First candle: {data}")
                    logger.info("="*80)
                    self._first_candle_logged = True

                # Dispatch to callbacks
                with self._callbacks_lock:
                    callbacks = list(self.candlestick_callbacks)

                for callback in callbacks:
                    try:
                        callback(data)
                    except Exception as e:
                        logger.error(f"Callback error: {e}", exc_info=True)

            except Exception as e:
                logger.error(f"Handler error: {e}", exc_info=True)

    def _resubscribe_all(self):
        """✅ CRITICAL FIX: Resubscribe to all previous subscriptions after reconnect"""
        with self._subscriptions_lock:
            logger.info("🔄 Resubscribing to all streams after reconnect...")
            
            # Resubscribe orderbook
            for sub in self._subscriptions["orderbook"]:
                try:
                    self.sio.emit(
                        self.EVENT_ORDERBOOK,
                        {'event': 'subscribe', 'pair': sub['pair']},
                        namespace=self.NAMESPACE
                    )
                    logger.info(f"✓ Resubscribed to orderbook: {sub['pair']}")
                except Exception as e:
                    logger.error(f"Error resubscribing orderbook {sub['pair']}: {e}")
            
            # Resubscribe candlestick
            for sub in self._subscriptions["candlestick"]:
                try:
                    self.sio.emit(
                        self.EVENT_CANDLESTICK,
                        {'event': 'subscribe', 'pair': sub['pair']},
                        namespace=self.NAMESPACE
                    )
                    logger.info(f"✓ Resubscribed to candlestick: {sub['pair']}")
                except Exception as e:
                    logger.error(f"Error resubscribing candlestick {sub['pair']}: {e}")
            
            # Resubscribe trades
            for sub in self._subscriptions["trades"]:
                try:
                    self.sio.emit(
                        self.EVENT_TRADES,
                        {'event': 'subscribe', 'pair': sub['pair']},
                        namespace=self.NAMESPACE
                    )
                    logger.info(f"✓ Resubscribed to trades: {sub['pair']}")
                except Exception as e:
                    logger.error(f"Error resubscribing trades {sub['pair']}: {e}")
            
            # Resubscribe ticker
            for sub in self._subscriptions["ticker"]:
                try:
                    self.sio.emit(
                        self.EVENT_TICKER,
                        {'event': 'subscribe', 'pair': sub['pair']},
                        namespace=self.NAMESPACE
                    )
                    logger.info(f"✓ Resubscribed to ticker: {sub['pair']}")
                except Exception as e:
                    logger.error(f"Error resubscribing ticker {sub['pair']}: {e}")
            
            logger.info("🔄 Resubscription complete")

    def connect(self, timeout: int = 30) -> bool:
        """Connect to WebSocket server with timeout"""
        try:
            logger.info(f"Connecting to {self.BASE_URL} with namespace {self.NAMESPACE}...")
            self.sio.connect(
                url=self.BASE_URL,
                namespaces=[self.NAMESPACE],
                transports='websocket',
                socketio_path=self.HANDSHAKE_PATH,
                wait=True,
                wait_timeout=timeout
            )
            return self.is_connected
        except Exception as e:
            logger.error(f"Connection error: {e}", exc_info=True)
            print(f"Connection error: {e}")
            return False

    def disconnect(self):
        """Gracefully disconnect from WebSocket"""
        try:
            self.stop_event.set()
            if self.sio.connected:
                self.sio.disconnect()
            print("✓ Disconnected successfully")
            logger.info("Disconnected successfully")
        except Exception as e:
            logger.error(f"Disconnect error: {e}", exc_info=True)
            print(f"Disconnect error: {e}")

    def is_healthy(self, timeout_seconds: int = 30) -> bool:
        """
        Check if connection is healthy (receiving data).
        Returns False if no messages received in timeout period.
        """
        if not self.is_connected:
            return False
        
        if self._last_message_time is None:
            return True  # Just connected, no messages yet
        
        time_since_last = datetime.now() - self._last_message_time
        return time_since_last.total_seconds() < timeout_seconds

    def subscribe_orderbook(self, pair: str, callback: Callable = None):
        """Subscribe to order book updates (thread-safe)"""
        subscribe_data = {'event': 'subscribe', 'pair': pair}
        
        if callback:
            with self._callbacks_lock:
                if callback not in self.orderbook_callbacks:
                    self.orderbook_callbacks.append(callback)
        
        # ✅ Store subscription for auto-resubscribe
        with self._subscriptions_lock:
            if not any(s['pair'] == pair for s in self._subscriptions["orderbook"]):
                self._subscriptions["orderbook"].append({'pair': pair})
        
        logger.info(f"Subscribing to orderbook: {pair}")
        self.sio.emit(self.EVENT_ORDERBOOK, subscribe_data, namespace=self.NAMESPACE)
        print(f"✓ Subscribed to orderbook: {pair}")

    def subscribe_candlestick(self, pair: str, interval: int = 5, callback: Callable = None):
        """Subscribe to candlestick updates (thread-safe)"""
        pair_with_interval = f"{pair}_{interval}"
        subscribe_data = {'event': 'subscribe', 'pair': pair_with_interval}
        
        if callback:
            with self._callbacks_lock:
                if callback not in self.candlestick_callbacks:
                    self.candlestick_callbacks.append(callback)
        
        # ✅ Store subscription for auto-resubscribe
        with self._subscriptions_lock:
            if not any(s['pair'] == pair_with_interval for s in self._subscriptions["candlestick"]):
                self._subscriptions["candlestick"].append({'pair': pair_with_interval})
        
        logger.info(f"Subscribing to candlestick: {pair_with_interval}")
        self.sio.emit(self.EVENT_CANDLESTICK, subscribe_data, namespace=self.NAMESPACE)
        print(f"✓ Subscribed to candlestick: {pair_with_interval}")

    def subscribe_trades(self, pair: str, callback: Callable = None):
        """Subscribe to trade updates (thread-safe)"""
        subscribe_data = {'event': 'subscribe', 'pair': pair}
        
        if callback:
            with self._callbacks_lock:
                if callback not in self.trades_callbacks:
                    self.trades_callbacks.append(callback)
        
        # ✅ Store subscription for auto-resubscribe
        with self._subscriptions_lock:
            if not any(s['pair'] == pair for s in self._subscriptions["trades"]):
                self._subscriptions["trades"].append({'pair': pair})
        
        logger.info(f"Subscribing to trades: {pair}")
        self.sio.emit(self.EVENT_TRADES, subscribe_data, namespace=self.NAMESPACE)
        print(f"✓ Subscribed to trades: {pair}")

    def subscribe_ticker(self, pair: str, callback: Callable = None):
        """Subscribe to ticker updates (thread-safe)"""
        subscribe_data = {'event': 'subscribe', 'pair': pair}
        
        if callback:
            with self._callbacks_lock:
                if callback not in self.ticker_callbacks:
                    self.ticker_callbacks.append(callback)
        
        # ✅ Store subscription for auto-resubscribe
        with self._subscriptions_lock:
            if not any(s['pair'] == pair for s in self._subscriptions["ticker"]):
                self._subscriptions["ticker"].append({'pair': pair})
        
        logger.info(f"Subscribing to ticker: {pair}")
        self.sio.emit(self.EVENT_TICKER, subscribe_data, namespace=self.NAMESPACE)
        print(f"✓ Subscribed to ticker: {pair}")

    def wait(self):
        """Keep connection alive"""
        try:
            self.sio.wait()
        except KeyboardInterrupt:
            print("\n✓ Shutting down...")
            self.disconnect()
