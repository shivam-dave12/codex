"""
CIRCUIT BREAKER PATTERN
========================
Prevents cascading failures by:
- Monitoring failure rates
- Opening circuit on threshold
- Half-open state for recovery
- Automatic reset

Version: 2.0.0
"""

import time
import threading
from typing import Callable, Any, Optional
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states"""
    CLOSED = "CLOSED"  # Normal operation
    OPEN = "OPEN"  # Circuit is open, calls fail fast
    HALF_OPEN = "HALF_OPEN"  # Testing if service recovered


class CircuitBreakerError(Exception):
    """Raised when circuit is open"""
    pass


class CircuitBreaker:
    """
    Circuit breaker for protecting against cascading failures
    """
    
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        timeout_seconds: float = 60.0,
        half_open_max_calls: int = 3
    ):
        """
        Initialize circuit breaker
        
        Args:
            name: Circuit breaker name
            failure_threshold: Number of failures before opening
            timeout_seconds: Time to wait before attempting recovery
            half_open_max_calls: Number of test calls in half-open state
        """
        self.name = name
        self.failure_threshold = failure_threshold
        self.timeout_seconds = timeout_seconds
        self.half_open_max_calls = half_open_max_calls
        
        self._lock = threading.RLock()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: Optional[float] = None
        self._opened_at: Optional[float] = None
        
        logger.info(
            f"CircuitBreaker '{name}' initialized: "
            f"threshold={failure_threshold}, timeout={timeout_seconds}s"
        )
    
    @property
    def state(self) -> CircuitState:
        """Get current state"""
        with self._lock:
            return self._state
    
    def _should_attempt_reset(self) -> bool:
        """Check if should attempt to reset circuit"""
        if self._opened_at is None:
            return False
        return time.time() - self._opened_at >= self.timeout_seconds
    
    def _transition_to_half_open(self) -> None:
        """Transition from OPEN to HALF_OPEN"""
        logger.info(f"[{self.name}] Circuit transitioning to HALF_OPEN (testing recovery)")
        self._state = CircuitState.HALF_OPEN
        self._failure_count = 0
        self._success_count = 0
    
    def _handle_success(self) -> None:
        """Handle successful call"""
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                
                if self._success_count >= self.half_open_max_calls:
                    # Service recovered
                    logger.info(f"[{self.name}] Circuit CLOSED (service recovered)")
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    self._opened_at = None
            
            elif self._state == CircuitState.CLOSED:
                # Reset failure count on success
                self._failure_count = max(0, self._failure_count - 1)
    
    def _handle_failure(self) -> None:
        """Handle failed call"""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()
            
            if self._state == CircuitState.HALF_OPEN:
                # Failed during recovery test - reopen circuit
                logger.warning(f"[{self.name}] Circuit OPENED (recovery failed)")
                self._state = CircuitState.OPEN
                self._opened_at = time.time()
                self._success_count = 0
            
            elif self._state == CircuitState.CLOSED:
                if self._failure_count >= self.failure_threshold:
                    # Threshold exceeded - open circuit
                    logger.error(
                        f"[{self.name}] Circuit OPENED "
                        f"({self._failure_count} failures >= {self.failure_threshold})"
                    )
                    self._state = CircuitState.OPEN
                    self._opened_at = time.time()
    
    def call(self, func: Callable, *args, **kwargs) -> Any:
        """
        Call function through circuit breaker
        
        Args:
            func: Function to call
            *args: Positional arguments
            **kwargs: Keyword arguments
            
        Returns:
            Function result
            
        Raises:
            CircuitBreakerError: If circuit is open
        """
        with self._lock:
            # Check if should attempt reset
            if self._state == CircuitState.OPEN and self._should_attempt_reset():
                self._transition_to_half_open()
            
            # Fail fast if circuit is open
            if self._state == CircuitState.OPEN:
                raise CircuitBreakerError(
                    f"Circuit '{self.name}' is OPEN "
                    f"(will retry in {self.timeout_seconds - (time.time() - self._opened_at):.1f}s)"
                )
        
        # Attempt call
        try:
            result = func(*args, **kwargs)
            self._handle_success()
            return result
        
        except Exception as e:
            self._handle_failure()
            raise
    
    def reset(self) -> None:
        """Manually reset circuit breaker"""
        with self._lock:
            logger.info(f"[{self.name}] Circuit manually reset")
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._opened_at = None
            self._last_failure_time = None
    
    def get_stats(self) -> dict:
        """Get circuit breaker statistics"""
        with self._lock:
            return {
                "name": self.name,
                "state": self._state.value,
                "failure_count": self._failure_count,
                "success_count": self._success_count,
                "last_failure": self._last_failure_time,
                "opened_at": self._opened_at,
            }


# Global circuit breakers
_circuit_breakers: dict = {}
_breakers_lock = threading.RLock()


def get_circuit_breaker(name: str, **kwargs) -> CircuitBreaker:
    """Get or create circuit breaker"""
    with _breakers_lock:
        if name not in _circuit_breakers:
            _circuit_breakers[name] = CircuitBreaker(name, **kwargs)
        return _circuit_breakers[name]


if __name__ == "__main__":
    # Test
    logging.basicConfig(level=logging.INFO)
    
    cb = CircuitBreaker("TEST", failure_threshold=3, timeout_seconds=5.0)
    
    def unreliable_function(should_fail=False):
        if should_fail:
            raise Exception("Simulated failure")
        return "Success"
    
    print("Testing circuit breaker...\n")
    
    # Test successes
    for i in range(2):
        try:
            result = cb.call(unreliable_function, should_fail=False)
            print(f"Call {i+1}: {result}")
        except Exception as e:
            print(f"Call {i+1}: Failed - {e}")
    
    # Test failures
    for i in range(5):
        try:
            result = cb.call(unreliable_function, should_fail=True)
            print(f"Call {i+3}: {result}")
        except CircuitBreakerError as e:
            print(f"Call {i+3}: Circuit open - {e}")
        except Exception as e:
            print(f"Call {i+3}: Failed - {e}")
    
    print(f"\nCircuit state: {cb.state}")
    print("\nTest complete!")
