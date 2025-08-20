# bus.py — простая внутренняя очередь событий для моста /push → /feed

from collections import deque
from threading import Lock
from time import time
from typing import Any, Dict, List, Optional

_queue: deque[Dict[str, Any]] = deque()
_lock = Lock()

def emit(symbol: str, signal: str, price: Optional[float] = None, meta: Optional[dict] = None) -> dict:
    """Положить событие в очередь."""
    ev: Dict[str, Any] = {
        "ts": time(),
        "symbol": str(symbol).upper(),
        "signal": str(signal).lower(),
    }
    if price is not None:
        try:
            ev["price"] = float(price)
        except Exception:
            pass
    if meta:
        ev["meta"] = meta

    with _lock:
        _queue.append(ev)
    return ev

def drain() -> List[Dict[str, Any]]:
    """Забрать и очистить все накопленные события."""
    with _lock:
        items = list(_queue)
        _queue.clear()
    return items

