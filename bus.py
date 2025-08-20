# bus.py — локальная шина: отправляет событие в Render /push
import os, requests

from dotenv import load_dotenv
load_dotenv(override=True)

BASE_URL  = os.getenv("BASE_URL", "https://mexc-telegram-bot-1xd7.onrender.com").rstrip("/")
TV_SECRET = os.getenv("TV_SECRET", "").strip()
TIMEOUT   = float(os.getenv("BUS_TIMEOUT", "10"))

def emit(symbol: str, signal: str, price=None, meta=None):
    """
    emit('EURUSD','buy', price=1.2345, meta={...})
    """
    assert signal in ("buy", "sell")
    payload = {"secret": TV_SECRET, "symbol": symbol, "signal": signal}
    if price is not None:
        try:
            payload["price"] = float(price)
        except Exception:
            pass
    if meta:
        payload["meta"] = meta

    try:
        r = requests.post(f"{BASE_URL}/push", json=payload, timeout=TIMEOUT)
        return {"ok": r.ok, "status": r.status_code, **payload}
    except Exception as e:
        return {"ok": False, "error": str(e), **payload}
