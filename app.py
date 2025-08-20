# app.py
import os, threading, requests
from collections import deque
from datetime import datetime, timezone
from typing import Dict, Any
from fastapi import FastAPI, Request, HTTPException

# ───────────── ENV ─────────────
TOKEN  = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT   = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SECRET = os.getenv("TV_SECRET", "").strip()

# ────────── FastAPI/worker ─────
app = FastAPI()
_started = False
_lock = threading.Lock()

# если есть твой бесконечный воркер — запускаем один раз
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

# health: поддерживаем GET и HEAD
@app.api_route("/", methods=["GET", "HEAD"])
def health():
    return {"ok": True}

# ─────────── helpers ───────────
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

# ───────── шина сигналов ───────
QUEUE: deque[Dict[str, Any]] = deque(maxlen=500)
q_lock = threading.Lock()

def _emit(symbol: str, signal: str, price: float | None = None, meta: Dict[str, Any] | None = None):
    ev = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": (symbol or "EURUSD").upper().replace(":", "").replace(".", ""),
        "signal": signal.lower(),                   # 'buy' / 'sell'
        "price": price,
        "meta": meta or {},
    }
    with q_lock:
        QUEUE.append(ev)
    return ev

def _drain():
    with q_lock:
        items = list(QUEUE)
        QUEUE.clear()
    return items

# ─────────── маршруты ──────────

# Старый вебхук (оставляем как был)
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

# НОВОЕ: ручной пуш сигнала
@app.post("/push")
async def push(req: Request):
    """
    Тело: {"secret":"...","symbol":"EURUSD","signal":"buy|sell","price":1.2345}
    """
    data = await req.json()
    if SECRET and data.get("secret") != SECRET:
        raise HTTPException(status_code=401, detail="bad secret")

    signal = str(data.get("signal", "")).lower()
    if signal not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="signal must be 'buy' or 'sell'")

    symbol = str(data.get("symbol", "EURUSD"))
    price  = data.get("price")
    ev = _emit(symbol, signal, price)
    try:
        send_tg(f"SIG ▶ {ev['symbol']} → {ev['signal'].upper()} @ {price if price is not None else '—'}")
    except Exception:
        pass
    return {"ok": True, "queued": len(QUEUE)}

# НОВОЕ: выдача очереди для моста
@app.get("/feed")
def feed(secret: str = ""):
    if SECRET and secret != SECRET:
        raise HTTPException(status_code=401, detail="bad secret")
    return {"ok": True, "events": _drain()}

