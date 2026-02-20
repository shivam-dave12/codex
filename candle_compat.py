"""
COMPATIBILITY LAYER
===================
Wraps Candle dataclass to provide dictionary-like access
This allows old strategy.py to work without changes
"""

class CandleDict:
    """Wrapper that makes Candle act like a dictionary AND supports attribute access"""
    
    # Mapping from attribute names to dict keys
    _ATTR_MAP = {
        'timestamp': 't',
        'open': 'o',
        'high': 'h',
        'low': 'l',
        'close': 'c',
        'volume': 'v',
    }
    
    def __init__(self, candle):
        self._candle = candle
    
    def __getitem__(self, key):
        mapping = {
            't': int(self._candle.timestamp * 1000),  # Convert to ms
            'o': self._candle.open,
            'h': self._candle.high,
            'l': self._candle.low,
            'c': self._candle.close,
            'v': self._candle.volume,
        }
        if key in mapping:
            return mapping[key]
        raise KeyError(f"Invalid candle key: {key}")
    
    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        if name in self._ATTR_MAP:
            return self[self._ATTR_MAP[name]]
        # Forward to underlying Candle for methods like is_bullish(), body_size(), etc.
        return getattr(self._candle, name)
    
    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default
    
    def __repr__(self):
        return f"CandleDict(t={self['t']}, o={self['o']}, h={self['h']}, l={self['l']}, c={self['c']}, v={self['v']})"


def wrap_candles(candles):
    """Convert list of Candle objects to dict-like wrappers"""
    if not candles:
        return []
    return [CandleDict(c) for c in candles]