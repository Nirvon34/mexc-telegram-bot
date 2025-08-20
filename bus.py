from collections import deque
from threading import Lock
from datetime import datetime, timezone

QUEUE = deque(maxlen=500)
_LOCK = Lock()

def emit(symbol: str, signal: str, price=None, meta=None):
    ev = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": (symbol or "").upper().replace(":", "").replace(".", ""),
        "signal": (signal or "").lower(),   # 'buy' / 'sell'
        "price": price,
        "meta": meta or {},
    }
    with _LOCK:
        QUEUE.append(ev)
    return ev

def drain():
    with _LOCK:
        items = list(QUEUE)
        QUEUE.clear()
    return items
