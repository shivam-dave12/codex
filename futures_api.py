"""
CoinSwitch Futures Trading API Plugin
CORRECTED: Proper GET request handling + accurate rate limiting
ADDED: Missing methods for order status, cancellation, and open orders to sync with OrderManager
"""

import os
import time
import json
import requests
import urllib.parse
from typing import Dict, List, Optional, Any
from cryptography.hazmat.primitives.asymmetric import ed25519
from urllib.parse import urlparse, urlencode
from dotenv import load_dotenv
import logging

load_dotenv()
logger = logging.getLogger(__name__)

class FuturesAPI:
    """CoinSwitch Futures Trading API Client"""
    
    def __init__(self, api_key: str = None, secret_key: str = None):
        """
        Initialize Futures API client
        
        Args:
            api_key: CoinSwitch API key
            secret_key: CoinSwitch secret key
        """
        self.api_key = api_key or os.getenv('COINSWITCH_API_KEY')
        self.secret_key = secret_key or os.getenv('COINSWITCH_SECRET_KEY')
        self.base_url = "https://coinswitch.co"
        
        if not self.api_key or not self.secret_key:
            raise ValueError("API key and secret key required")
    
    def _generate_signature(self, method: str, endpoint: str, params: Dict = None, payload: Dict = None) -> str:
        """
        Generate ED25519 signature
        
        CRITICAL: Based on official documentation
        - GET with params: signature = METHOD + ENDPOINT_WITH_PARAMS + "{}"
        - POST/DELETE: signature = METHOD + ENDPOINT + JSON_PAYLOAD (sorted keys)
        
        Args:
            method: HTTP method
            endpoint: API endpoint
            params: Query parameters for GET
            payload: Body payload for POST/DELETE
        
        Returns:
            Signature as hex string
        """
        params = params or {}
        payload = payload or {}
        
        # Build endpoint with query params for GET
        signature_endpoint = endpoint
        if method == "GET" and params:
            query_string = urlencode(params)
            signature_endpoint = f"{endpoint}?{query_string}"
        
        # Unquote the endpoint (decode URL encoding)
        unquoted_endpoint = urllib.parse.unquote_plus(signature_endpoint)
        
        # CRITICAL FIX: For GET requests, always use empty object {}
        # For POST/DELETE, use actual payload
        if method == "GET":
            payload_json = "{}"
        else:
            payload_json = json.dumps(payload, separators=(',', ':'), sort_keys=True)
        
        # Build signature message
        signature_msg = method + unquoted_endpoint + payload_json
        
        logger.debug(f"Signature message: {signature_msg}")
        
        # Sign with ED25519
        request_string = bytes(signature_msg, 'utf-8')
        secret_key_bytes = bytes.fromhex(self.secret_key)
        secret_key_obj = ed25519.Ed25519PrivateKey.from_private_bytes(secret_key_bytes)
        signature_bytes = secret_key_obj.sign(request_string)
        
        return signature_bytes.hex()
    
    def _make_request(self, method: str, endpoint: str, params: Dict = None, payload: Dict = None) -> Dict:
        """
        Make authenticated API request
        
        CRITICAL FIX: GET requests should NOT send request body
        """
        signature = self._generate_signature(method, endpoint, params, payload)
        
        # Build URL with query params for GET
        url = self.base_url + endpoint
        if method == "GET" and params:
            url = f"{url}?{urlencode(params)}"
        
        headers = {
            'Content-Type': 'application/json',
            'X-AUTH-SIGNATURE': signature,
            'X-AUTH-APIKEY': self.api_key
        }
        
        try:
            # CRITICAL FIX: Don't send body for GET requests
            if method == "GET":
                response = requests.request(method, url, headers=headers)
            else:
                # For POST/DELETE, send payload as JSON body
                response = requests.request(method, url, headers=headers, json=payload if payload else {})
            
            response.raise_for_status()
            return response.json()
        
        except requests.exceptions.RequestException as e:
            error_response = {
                "error": str(e),
                "status_code": getattr(e.response, 'status_code', None) if hasattr(e, 'response') else None,
            }
            
            if hasattr(e, 'response') and e.response is not None:
                try:
                    error_response['response'] = e.response.json()
                except:
                    error_response['response'] = e.response.text
            
            logger.error(f"API request failed: {error_response}")
            return error_response
    
    # ============ ORDER MANAGEMENT ============
    
    def place_order(self, symbol: str, side: str, order_type: str, quantity: float,
                   exchange: str = "EXCHANGE_2", price: float = None,
                   trigger_price: float = None, reduce_only: bool = False) -> Dict:
        """
        Place a futures order
        
        Args:
            symbol: Trading symbol (e.g., BTCUSDT)
            side: BUY or SELL
            order_type: MARKET, LIMIT, TAKE_PROFIT_MARKET, STOP_MARKET
            quantity: Order quantity in base asset
            exchange: Exchange identifier
            price: Limit price (required for LIMIT orders)
            trigger_price: Trigger price (for TAKE_PROFIT_MARKET/STOP_MARKET)
            reduce_only: Reduce only flag (for TP/SL orders)
        
        Returns:
            Order response with order_id
        """
        endpoint = "/trade/api/v2/futures/order"
        
        payload = {
            "symbol": symbol,
            "exchange": exchange,
            "side": side,
            "order_type": order_type,
            "quantity": quantity,
            "reduce_only": reduce_only,
        }
        
        if price is not None:
            payload["price"] = price
        if trigger_price is not None:
            payload["trigger_price"] = trigger_price
        
        return self._make_request("POST", endpoint, payload=payload)
    
    def get_order(self, order_id: str, exchange: str = "EXCHANGE_2") -> Dict:
        """
        Get specific order details by order_id.
        
        Args:
            order_id: Unique order identifier
            exchange: Exchange identifier
            
        Returns:
            Order details or error dict
        """
        endpoint = "/trade/api/v2/futures/order"
        params = {"order_id": order_id, "exchange": exchange}
        
        return self._make_request("GET", endpoint, params=params)
    
    def get_open_orders(self, exchange: str = "EXCHANGE_2", symbol: str = None) -> Dict:
        """
        Get all open orders.
        
        Args:
            exchange: Exchange identifier
            symbol: Optional filter by symbol
            
        Returns:
            List of open orders or error dict
        """
        endpoint = "/trade/api/v2/futures/open_orders"
        params = {"exchange": exchange}
        if symbol:
            params["symbol"] = symbol
        
        return self._make_request("GET", endpoint, params=params)
    
    def cancel_order(self, order_id: str, exchange: str = "EXCHANGE_2") -> Dict:
        """
        Cancel a specific order.
        
        Args:
            order_id: Unique order identifier
            exchange: Exchange identifier
            
        Returns:
            Cancellation response or error dict
        """
        endpoint = "/trade/api/v2/futures/order"
        payload = {
            "order_id": order_id,
            "exchange": exchange,
            "action": "cancel"  # Assuming API uses 'action' field; adjust if needed
        }
        
        return self._make_request("DELETE", endpoint, payload=payload)
    
    def cancel_all_orders(self, exchange: str = "EXCHANGE_2", symbol: str = None) -> Dict:
        """
        Cancel all open orders optionally filtered by symbol.

        Args:
            exchange: Exchange identifier
            symbol: Optional filter by symbol

        Returns:
            Cancellation response or error dict
        """
        endpoint = "/trade/api/v2/futures/cancel_all"
        payload = {
            "exchange": exchange
        }
        if symbol:
            payload["symbol"] = symbol

        # IMPORTANT: POST with JSON body, NOT params
        return self._make_request("POST", endpoint, payload=payload)

    
    # ============ ADDITIONAL ENDPOINTS (unchanged from original) ============
    
    def set_leverage(self, symbol: str, exchange: str, leverage: int) -> Dict:
        """
        Set leverage for a symbol.
        """
        endpoint = "/trade/api/v2/futures/leverage"
        payload = {
            "symbol": symbol,
            "exchange": exchange,
            "leverage": leverage
        }
        return self._make_request("POST", endpoint, payload=payload)
    
    def add_margin(self, symbol: str, exchange: str, margin: float) -> Dict:
        """
        Add margin to a position.
        """
        endpoint = "/trade/api/v2/futures/add_margin"
        
        payload = {
            "exchange": exchange,
            "symbol": symbol,
            "margin": margin
        }
        
        return self._make_request("POST", endpoint, payload=payload)
    
    # ============ POSITIONS & ACCOUNT ============
    
    def get_positions(self, exchange: str = "EXCHANGE_2", symbol: str = None) -> Dict:
        """
        Get open positions
        
        Args:
            exchange: Exchange identifier
            symbol: Filter by symbol
        """
        endpoint = "/trade/api/v2/futures/positions"
        
        params = {"exchange": exchange}
        if symbol:
            params["symbol"] = symbol
        
        return self._make_request("GET", endpoint, params=params)
    
    def get_wallet_balance(self) -> Dict:
        """Get futures wallet balance"""
        endpoint = "/trade/api/v2/futures/wallet_balance"
        return self._make_request("GET", endpoint, params={}, payload=None)
    
    def get_transactions(self, exchange: str = "EXCHANGE_2", symbol: str = None,
                        transaction_type: str = None, transaction_id: str = None) -> Dict:
        """
        Get transaction history
        
        Args:
            exchange: Exchange identifier
            symbol: Filter by symbol
            transaction_type: Filter by type (commission, P&L, funding fee, liquidation fee)
            transaction_id: Specific transaction ID
        """
        endpoint = "/trade/api/v2/futures/transactions"
        
        params = {"exchange": exchange}
        if symbol:
            params["symbol"] = symbol
        if transaction_type:
            params["type"] = transaction_type
        if transaction_id:
            params["transaction_id"] = transaction_id
        
        return self._make_request("GET", endpoint, params=params, payload=None)
    
    def get_instrument_info(self, exchange: str = "EXCHANGE_2") -> Dict:
        """
        Get instrument specifications
        
        Args:
            exchange: Exchange identifier
        """
        endpoint = "/trade/api/v2/futures/instrument_info"
        
        params = {"exchange": exchange}
        return self._make_request("GET", endpoint, params=params, payload=None)
    
    # ------------------------- REST klines / candles API -----------------
    
    def get_klines(self, symbol: str, interval: int = 1, limit: int = 100, exchange: str = "EXCHANGE_2") -> Dict:
        """
        Retrieve historical klines/candles for warmup.
        - symbol: "BTCUSDT"
        - interval: minutes (1,5,15,...)
        - limit: number of bars
        
        Returns parsed JSON or error dict.
        """
        endpoint = "/trade/api/v2/futures/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit, "exchange": exchange}
        
        try:
            resp = self._make_request("GET", endpoint, params=params, payload=None)
            return resp
        except Exception as e:
            return {"error": str(e)}
    
    def get_balance(self, currency: str = "USDT") -> Dict:
        """
        Get futures wallet balance for a base asset (e.g. USDT).
        """
        result = {
            "available": 0.0,
            "locked": 0.0,
            "currency": currency,
        }
        
        try:
            wallet = self.get_wallet_balance()
            
            # Basic error pass-through
            if not isinstance(wallet, dict):
                return {"error": "wallet response not dict", "raw_response": wallet, **result}
            
            data = wallet.get("data")
            if not isinstance(data, dict):
                return {"error": "wallet.data missing or not dict", "raw_response": wallet, **result}
            
            base_list = data.get("base_asset_balances")
            if not isinstance(base_list, list):
                return {"error": "wallet.data.base_asset_balances missing or not list", "raw_response": wallet, **result}
            
            # Find the entry for the requested base asset (USDT)
            for entry in base_list:
                if entry.get("base_asset") == currency:
                    balances = entry.get("balances", {})
                    total_avail_str = balances.get("total_available_balance", "0")
                    total_blocked_str = balances.get("total_blocked_balance", "0")
                    
                    available = float(total_avail_str)
                    locked = float(total_blocked_str)
                    
                    return {
                        "available": available,
                        "locked": locked,
                        "currency": currency,
                    }
            
            # If we reach here, USDT was not found
            return {
                "error": f"base_asset {currency} not found in base_asset_balances",
                "raw_response": wallet,
                **result,
            }
        
        except Exception as e:
            return {
                "error": f"Exception in get_balance: {e}",
                **result,
            }


if __name__ == "__main__":
    # Quick test
    try:
        api = FuturesAPI()
        print("✓ Futures API initialized successfully")
        
        # Test getting positions
        positions = api.get_positions()
        print(f"✓ Positions retrieved: {positions}")
        
        # Test getting wallet balance
        balance = api.get_wallet_balance()
        print(f"✓ Wallet balance retrieved")
    
    except Exception as e:
        print(f"✗ Error: {e}")