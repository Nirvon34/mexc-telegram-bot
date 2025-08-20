# app.py — FastAPI: health, /tv, /push, /feed + запуск воркера MEXC
import os, threading, requests
from collections import deque
from datetime import datetime, timezone
from typing import Dict, Any
from fastapi import FastAPI, Request, HTTPException

# ── ENV
from dotenv import load_dotenv
load_dotenv(override=True)
TOKEN  = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT   = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SECRET = os.getenv("TV_SECRET", "").strip()

# ── FastAPI/worker
app = FastAPI()
_started = False
_lock = threading.Lock()

try:
    from bot import main as worker_main  # бесконечный опрос MEXC
except Exception:
    worker_main = None

def start_worker_once():
    global _started
    with _lock:
        if _started:
            return
        if worker_main:
            threading.Thread(target=worker_main, daemon=True, name="mexc-worker").start()
        _started = True
        print("Worker thread started")

@app.on_event("startup")
def _startup():
    start_worker_once()

# ── health (GET/HEAD)
@app.api_route("/", methods=["GET", "HEAD"])
def health():
    return {"ok": True}

def send_tg(text: str):
    if not (TOKEN and CHAT):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT, "text": text},
            timeout=10,
        )
    except Exception as e:
        print("TG send error:", e)

# ── старый хук (оставляем для совместимости)
@app.post("/tv")
async def tv(req: Request):
    data = await req.json()
    if SECRET and data.get("secret") != SECRET:
        raise HTTPException(status_code=401, detail="bad secret")
    sym = str(data.get("symbol", ""))
    tf  = str(data.get("interval", ""))
    px  = str(data.get("price", ""))
    act = str(data.get("action", ""))
    t   = str(data.get("time", ""))
    send_tg(f"TV ▶ {sym} {tf} {act} @ {px}\n{t}")
    return {"status": "ok"}

# ── внутренняя очередь сигналов
QUEUE: deque[Dict[str, Any]] = deque(maxlen=500)
q_lock = threading.Lock()

@app.post("/push")
async def push(req: Request):
    """
    POST /push
    body: {"secret":"...","symbol":"EURUSD","signal":"buy|sell","price":1.2345}
    """
    data = await req.json()
    if SECRET and data.get("secret") != SECRET:
        raise HTTPException(status_code=401, detail="bad secret")

    signal = str(data.get("signal", "")).lower()
    if signal not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="signal must be 'buy' or 'sell'")

    symbol = str(data.get("symbol", "EURUSD")).upper().replace(":", "")
    ev = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "signal": signal,
        "price": data.get("price"),
        "meta": data.get("meta") or {},
    }
    with q_lock:
        QUEUE.append(ev)
    try:
        send_tg(f"SIG ▶ {symbol} → {signal.upper()} @ {ev['price'] if ev['price'] is not None else '—'}")
    except Exception:
        pass
    return {"ok": True, "queued": len(QUEUE)}

@app.get("/feed")
def feed(secret: str = ""):
    """Клиент (tv2mt5.py) забирает и очищает очередь."""
    if SECRET and secret != SECRET:
        raise HTTPException(status_code=401, detail="bad secret")
    with q_lock:
        items = list(QUEUE)
        QUEUE.clear()
    return {"ok": True, "events": items}
