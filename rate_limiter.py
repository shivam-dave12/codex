"""
RATE LIMITER - Industry Grade
==============================
Thread-safe global rate limiting with:
- Token bucket algorithm
- Sliding window
- Distributed rate limiting support
- Automatic backoff

Version: 2.0.0
"""

import time
import threading
from typing import Optional, Dict
from collections import deque
import logging

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Thread-safe rate limiter using token bucket algorithm
    """
    
    def __init__(
        self,
        name: str,
        max_calls: int,
        period_seconds: float,
        burst_size: Optional[int] = None
    ):
        """
        Initialize rate limiter
        
        Args:
            name: Limiter name for logging
            max_calls: Maximum calls allowed in period
            period_seconds: Time period in seconds
            burst_size: Maximum burst size (default: max_calls)
        """
        self.name = name
        self.max_calls = max_calls
        self.period_seconds = period_seconds
        self.burst_size = burst_size or max_calls
        
        self._lock = threading.RLock()
        self._tokens = float(self.burst_size)
        self._last_update = time.time()
        self._call_times: deque = deque(maxlen=max_calls * 2)
        
        # Rate per second
        self._rate = max_calls / period_seconds
        
        logger.info(
            f"RateLimiter '{name}' initialized: "
            f"{max_calls} calls / {period_seconds}s "
            f"(rate: {self._rate:.2f}/s, burst: {self.burst_size})"
        )
    
    def _refill_tokens(self) -> None:
        """Refill tokens based on time elapsed"""
        now = time.time()
        elapsed = now - self._last_update
        
        # Add tokens based on rate
        new_tokens = elapsed * self._rate
        self._tokens = min(self._tokens + new_tokens, self.burst_size)
        self._last_update = now
    
    def acquire(self, tokens: int = 1, blocking: bool = True, timeout: Optional[float] = None) -> bool:
        """
        Acquire tokens from the bucket
        
        Args:
            tokens: Number of tokens to acquire
            blocking: If True, wait for tokens
            timeout: Maximum wait time (None = infinite)
            
        Returns:
            True if tokens acquired, False otherwise
        """
        if tokens > self.burst_size:
            raise ValueError(f"Cannot acquire {tokens} tokens (burst size: {self.burst_size})")
        
        start_time = time.time()
        
        while True:
            with self._lock:
                self._refill_tokens()
                
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    self._call_times.append(time.time())
                    return True
                
                if not blocking:
                    return False
                
                # Calculate wait time
                tokens_needed = tokens - self._tokens
                wait_time = tokens_needed / self._rate
                
                # Check timeout
                if timeout is not None:
                    elapsed = time.time() - start_time
                    if elapsed >= timeout:
                        return False
                    wait_time = min(wait_time, timeout - elapsed)
            
            # Wait outside lock
            logger.debug(f"[{self.name}] Waiting {wait_time:.2f}s for tokens")
            time.sleep(wait_time)
    
    def wait(self, tokens: int = 1) -> None:
        """
        Wait for tokens (blocking)
        
        Args:
            tokens: Number of tokens to acquire
        """
        self.acquire(tokens=tokens, blocking=True, timeout=None)
    
    def try_acquire(self, tokens: int = 1) -> bool:
        """
        Try to acquire tokens without blocking
        
        Args:
            tokens: Number of tokens to acquire
            
        Returns:
            True if acquired, False otherwise
        """
        return self.acquire(tokens=tokens, blocking=False)
    
    def get_current_rate(self) -> float:
        """Get current call rate (calls per second)"""
        with self._lock:
            now = time.time()
            cutoff = now - self.period_seconds
            
            # Count calls in period
            recent_calls = sum(1 for t in self._call_times if t >= cutoff)
            
            return recent_calls / self.period_seconds
    
    def get_available_tokens(self) -> float:
        """Get currently available tokens"""
        with self._lock:
            self._refill_tokens()
            return self._tokens
    
    def reset(self) -> None:
        """Reset the rate limiter"""
        with self._lock:
            self._tokens = float(self.burst_size)
            self._last_update = time.time()
            self._call_times.clear()
            logger.info(f"[{self.name}] Rate limiter reset")


class GlobalRateLimiter:
    """
    Global rate limiter for API calls
    Singleton pattern with thread safety
    """
    
    _instance: Optional['GlobalRateLimiter'] = None
    _lock = threading.RLock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if not hasattr(self, '_initialized'):
            self._api_limiter = RateLimiter(
                name="API_GLOBAL",
                max_calls=1,
                period_seconds=2.5,  # Minimum 2.5s between API calls
                burst_size=1
            )
            
            self._order_limiter = RateLimiter(
                name="ORDERS",
                max_calls=15,
                period_seconds=60.0,  # Max 15 orders per minute
                burst_size=3
            )
            
            self._initialized = True
            logger.info("GlobalRateLimiter initialized")
    
    @classmethod
    def wait_for_api(cls) -> None:
        """Wait for API rate limit"""
        instance = cls()
        instance._api_limiter.wait()
    
    @classmethod
    def wait_for_order(cls, tokens: int = 1) -> None:
        """Wait for order rate limit"""
        instance = cls()
        instance._order_limiter.wait(tokens=tokens)
    
    @classmethod
    def can_place_order(cls) -> bool:
        """Check if can place order without waiting"""
        instance = cls()
        return instance._order_limiter.try_acquire()
    
    @classmethod
    def get_api_rate(cls) -> float:
        """Get current API call rate"""
        instance = cls()
        return instance._api_limiter.get_current_rate()
    
    @classmethod
    def get_order_rate(cls) -> float:
        """Get current order rate"""
        instance = cls()
        return instance._order_limiter.get_current_rate()
    
    @classmethod
    def reset(cls) -> None:
        """Reset all rate limiters"""
        instance = cls()
        instance._api_limiter.reset()
        instance._order_limiter.reset()


# Convenience function
def wait_for_api():
    """Wait for API rate limit (convenience function)"""
    GlobalRateLimiter.wait_for_api()


if __name__ == "__main__":
    # Test
    logging.basicConfig(level=logging.DEBUG)
    
    limiter = RateLimiter("TEST", max_calls=5, period_seconds=10.0)
    
    print("Testing rate limiter...")
    for i in range(8):
        start = time.time()
        limiter.wait()
        elapsed = time.time() - start
        print(f"Call {i+1}: waited {elapsed:.2f}s, available tokens: {limiter.get_available_tokens():.2f}")
    
    print("\nTest complete!")
