# app.py — FastAPI слой: health, /tv, /push, /feed + запуск фонового воркера

import os
import threading
import requests
from fastapi import FastAPI, Request, HTTPException

# Фоновый воркер (MEXC → Telegram → bus.emit)
from bot import main as worker_main

# Внутренняя шина событий для моста в MT5
# ожидаем в bus.py: emit(symbol, signal, price=None, meta=None) и drain()
from bus import emit, drain  # type: ignore

# ────────────────────────── ENV ──────────────────────────
TOKEN  = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT   = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SECRET = os.getenv("TV_SECRET", "").strip()

app = FastAPI()

_started = False
_lock = threading.Lock()


def start_worker_once():
    """Запускаем фонового воркера один раз на весь процесс."""
    global _started
    with _lock:
        if _started:
            return
        threading.Thread(
            target=worker_main,
            daemon=True,
            name="mexc-worker",
        ).start()
        _started = True
        print("Worker thread started")


@app.on_event("startup")
def _startup():
    start_worker_once()


# ───────────────────── Telegram helper ───────────────────
def send_tg(text: str):
    """Отправка простого текста в Telegram (если заданы токен/чат)."""
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


# ───────────────────── Health/Keep-alive ─────────────────
@app.api_route("/", methods=["GET", "HEAD"])
def health_root():
    return {"ok": True}


# ───────────────────── TradingView → bus ─────────────────
@app.post("/tv")
async def tv(req: Request):
    """
    Принимаем сигнал от TradingView (или другого источника).
    Ожидаем JSON: {
      "secret": "...",           # должен совпадать с TV_SECRET
      "symbol": "EURUSD",
      "action": "buy"|"sell",
      "price":  1.2345,          # опц.
      "interval": "M5"           # опц.
    }
    """
    data = await req.json()

    if SECRET and data.get("secret") != SECRET:
        raise HTTPException(status_code=401, detail="bad secret")

    sym = str(data.get("symbol", "EURUSD"))
    act = str(data.get("action", "")).lower()
    px  = data.get("price")
    tf  = str(data.get("interval", "") or data.get("tf", ""))

    # уведомление в TG
    msg = f"SIG ▶ {sym} → {act.upper()} @ {px if px is not None else '—'}"
    if tf:
        msg += f"\nTF: {tf}"
    send_tg(msg)

    # кладём в очередь для моста tv2mt5.py
    queued = 0
    if act in ("buy", "sell"):
        ev = emit(sym, act, price=px, meta={"src": "tv", "tf": tf})
        print("TV->EMIT:", ev)
        queued = 1

    return {"ok": True, "queued": queued}


# ───────────────────── Универсальный push ────────────────
@app.post("/push")
async def push(req: Request):
    """
    Тестовый/универсальный пуш в очередь:
    JSON: { "secret":"...", "symbol":"EURUSD", "signal":"buy"|"sell", "price":1.2345, "meta":{...} }
    """
    data = await req.json()

    if SECRET and data.get("secret") != SECRET:
        raise HTTPException(status_code=401, detail="bad secret")

    sym    = str(data.get("symbol", "EURUSD"))
    signal = str(data.get("signal", "")).lower()
    price  = data.get("price")
    meta   = data.get("meta") or {}

    queued = 0
    if signal in ("buy", "sell"):
        ev = emit(sym, signal, price=price, meta={"src": "push", **meta})
        print("PUSH->EMIT:", ev)
        queued = 1

    return {"ok": True, "queued": queued}


# ───────────────────── Выдача очереди /feed ──────────────
@app.get("/feed")
def feed(secret: str):
    """
    Мост читает здесь пачку событий и очищает очередь.
    GET /feed?secret=XXXX  →  {"ok":true,"events":[...]}
    """
    if SECRET and secret != SECRET:
        raise HTTPException(status_code=401, detail="bad secret")

    events = drain()  # должен вернуть list и очистить очередь
    return {"ok": True, "events": events}
