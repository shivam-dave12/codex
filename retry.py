"""
RETRY LOGIC - Industry Grade
=============================
Exponential backoff with jitter
Circuit breaker integration
Comprehensive retry strategies

Version: 2.0.0
"""

import time
import random
import logging
from typing import Callable, Any, Optional, Type, Tuple
from functools import wraps

logger = logging.getLogger(__name__)


class RetryConfig:
    """Configuration for retry behavior"""
    
    def __init__(
        self,
        max_attempts: int = 3,
        initial_delay: float = 1.0,
        max_delay: float = 60.0,
        exponential_base: float = 2.0,
        jitter: bool = True,
        jitter_range: float = 0.1
    ):
        self.max_attempts = max_attempts
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.jitter = jitter
        self.jitter_range = jitter_range


def calculate_backoff(
    attempt: int,
    config: RetryConfig
) -> float:
    """
    Calculate exponential backoff delay
    
    Args:
        attempt: Current attempt number (0-indexed)
        config: Retry configuration
        
    Returns:
        Delay in seconds
    """
    # Exponential backoff
    delay = min(
        config.initial_delay * (config.exponential_base ** attempt),
        config.max_delay
    )
    
    # Add jitter to prevent thundering herd
    if config.jitter:
        jitter = delay * config.jitter_range
        delay = delay + random.uniform(-jitter, jitter)
    
    return max(0, delay)


def retry(
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    max_attempts: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    jitter: bool = True,
    on_retry: Optional[Callable] = None,
    circuit_breaker: Optional[Any] = None
):
    """
    Decorator for retrying functions with exponential backoff
    
    Args:
        exceptions: Tuple of exceptions to catch
        max_attempts: Maximum retry attempts
        initial_delay: Initial delay in seconds
        max_delay: Maximum delay in seconds
        exponential_base: Base for exponential backoff
        jitter: Whether to add jitter
        on_retry: Callback function called on retry
        circuit_breaker: Optional circuit breaker
    """
    config = RetryConfig(
        max_attempts=max_attempts,
        initial_delay=initial_delay,
        max_delay=max_delay,
        exponential_base=exponential_base,
        jitter=jitter
    )
    
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            last_exception = None
            
            for attempt in range(max_attempts):
                try:
                    # Use circuit breaker if provided
                    if circuit_breaker:
                        return circuit_breaker.call(func, *args, **kwargs)
                    else:
                        return func(*args, **kwargs)
                
                except exceptions as e:
                    last_exception = e
                    
                    # Don't retry on last attempt
                    if attempt == max_attempts - 1:
                        break
                    
                    # Calculate backoff
                    delay = calculate_backoff(attempt, config)
                    
                    logger.warning(
                        f"Retry {attempt + 1}/{max_attempts} for {func.__name__}: "
                        f"{e.__class__.__name__}: {str(e)} "
                        f"(waiting {delay:.2f}s)"
                    )
                    
                    # Call retry callback if provided
                    if on_retry:
                        try:
                            on_retry(attempt, e, delay)
                        except Exception as callback_error:
                            logger.error(f"Error in retry callback: {callback_error}")
                    
                    # Wait before retry
                    time.sleep(delay)
            
            # All retries exhausted
            logger.error(
                f"All {max_attempts} retry attempts exhausted for {func.__name__}"
            )
            raise last_exception
        
        return wrapper
    return decorator


class RetryStrategy:
    """Advanced retry strategy with state"""
    
    def __init__(self, config: RetryConfig):
        self.config = config
        self.attempts = 0
        self.last_exception: Optional[Exception] = None
    
    def should_retry(self, exception: Exception) -> bool:
        """Check if should retry"""
        self.attempts += 1
        self.last_exception = exception
        return self.attempts < self.config.max_attempts
    
    def get_delay(self) -> float:
        """Get next retry delay"""
        return calculate_backoff(self.attempts - 1, self.config)
    
    def reset(self) -> None:
        """Reset retry state"""
        self.attempts = 0
        self.last_exception = None


if __name__ == "__main__":
    # Test
    logging.basicConfig(level=logging.INFO)
    
    @retry(max_attempts=3, initial_delay=0.5, exceptions=(ValueError,))
    def unreliable_function(should_fail: bool = True):
        if should_fail:
            raise ValueError("Simulated failure")
        return "Success"
    
    print("Testing retry decorator...\n")
    
    try:
        result = unreliable_function(should_fail=False)
        print(f"✓ Success: {result}")
    except Exception as e:
        print(f"✗ Failed: {e}")
    
    print("\nTesting with failures...")
    try:
        result = unreliable_function(should_fail=True)
        print(f"✓ Success: {result}")
    except Exception as e:
        print(f"✗ All retries exhausted: {e}")
