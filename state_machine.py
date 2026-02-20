"""
STATE MACHINE - Industry Grade
===============================
Thread-safe state management with:
- Validated transitions
- Event hooks
- State history
- Recovery mechanisms

Version: 2.0.0
"""

import threading
import logging
from typing import Dict, Set, Optional, Callable, List
from enum import Enum
from datetime import datetime
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class StateTransition:
    """Record of a state transition"""
    from_state: str
    to_state: str
    timestamp: datetime
    reason: Optional[str] = None


class StateMachine:
    """
    Thread-safe state machine with validation
    """
    
    def __init__(
        self,
        name: str,
        initial_state: str,
        valid_transitions: Dict[str, Set[str]],
        on_enter: Optional[Dict[str, Callable]] = None,
        on_exit: Optional[Dict[str, Callable]] = None
    ):
        """
        Initialize state machine
        
        Args:
            name: State machine name
            initial_state: Initial state
            valid_transitions: Dict of state -> set of valid next states
            on_enter: Dict of state -> callback on entering state
            on_exit: Dict of state -> callback on exiting state
        """
        self.name = name
        self._current_state = initial_state
        self._valid_transitions = valid_transitions
        self._on_enter = on_enter or {}
        self._on_exit = on_exit or {}
        
        self._lock = threading.RLock()
        self._history: List[StateTransition] = []
        
        logger.info(f"StateMachine '{name}' initialized in state: {initial_state}")
    
    @property
    def current_state(self) -> str:
        """Get current state (thread-safe)"""
        with self._lock:
            return self._current_state
    
    def can_transition_to(self, new_state: str) -> bool:
        """Check if transition is valid"""
        with self._lock:
            valid_next_states = self._valid_transitions.get(self._current_state, set())
            return new_state in valid_next_states
    
    def transition(self, new_state: str, reason: Optional[str] = None) -> bool:
        """
        Transition to new state
        
        Args:
            new_state: Target state
            reason: Reason for transition
            
        Returns:
            True if transition successful
            
        Raises:
            ValueError: If transition is invalid
        """
        with self._lock:
            if not self.can_transition_to(new_state):
                raise ValueError(
                    f"Invalid transition in '{self.name}': "
                    f"{self._current_state} -> {new_state}"
                )
            
            old_state = self._current_state
            
            # Call exit callback for old state
            if old_state in self._on_exit:
                try:
                    self._on_exit[old_state]()
                except Exception as e:
                    logger.error(f"Error in exit callback for {old_state}: {e}")
            
            # Update state
            self._current_state = new_state
            
            # Record transition
            transition = StateTransition(
                from_state=old_state,
                to_state=new_state,
                timestamp=datetime.utcnow(),
                reason=reason
            )
            self._history.append(transition)
            
            logger.info(
                f"[{self.name}] State transition: {old_state} -> {new_state}"
                + (f" ({reason})" if reason else "")
            )
            
            # Call enter callback for new state
            if new_state in self._on_enter:
                try:
                    self._on_enter[new_state]()
                except Exception as e:
                    logger.error(f"Error in enter callback for {new_state}: {e}")
            
            return True
    
    def force_state(self, new_state: str, reason: str = "forced") -> None:
        """Force state without validation (use with caution)"""
        with self._lock:
            old_state = self._current_state
            self._current_state = new_state
            
            transition = StateTransition(
                from_state=old_state,
                to_state=new_state,
                timestamp=datetime.utcnow(),
                reason=f"FORCED: {reason}"
            )
            self._history.append(transition)
            
            logger.warning(
                f"[{self.name}] FORCED state transition: {old_state} -> {new_state} ({reason})"
            )
    
    def get_history(self, limit: Optional[int] = None) -> List[StateTransition]:
        """Get state transition history"""
        with self._lock:
            if limit:
                return self._history[-limit:]
            return list(self._history)
    
    def reset(self, initial_state: Optional[str] = None) -> None:
        """Reset to initial state"""
        with self._lock:
            if initial_state:
                self._current_state = initial_state
            self._history.clear()
            logger.info(f"[{self.name}] Reset to state: {self._current_state}")


class TradingStateMachine(StateMachine):
    """Pre-configured state machine for trading bot"""
    
    def __init__(self):
        valid_transitions = {
            "INITIALIZING": {"READY", "ERROR"},
            "READY": {"SCANNING", "ERROR"},
            "SCANNING": {"ENTRY_PENDING", "READY", "ERROR"},
            "ENTRY_PENDING": {"POSITION_ACTIVE", "READY", "ERROR"},
            "POSITION_ACTIVE": {"EXITING", "ERROR"},
            "EXITING": {"READY", "ERROR"},
            "ERROR": {"READY", "INITIALIZING"},
        }
        
        super().__init__(
            name="TRADING",
            initial_state="INITIALIZING",
            valid_transitions=valid_transitions
        )


if __name__ == "__main__":
    # Test
    logging.basicConfig(level=logging.INFO)
    
    sm = TradingStateMachine()
    
    print(f"Current state: {sm.current_state}")
    
    sm.transition("READY")
    sm.transition("SCANNING")
    sm.transition("ENTRY_PENDING", reason="High confluence setup detected")
    sm.transition("POSITION_ACTIVE", reason="Entry filled")
    sm.transition("EXITING", reason="Take profit hit")
    sm.transition("READY")
    
    print("\nTransition history:")
    for t in sm.get_history():
        print(f"  {t.from_state} -> {t.to_state} ({t.reason})")
    
    print("\n✅ State machine test complete")
