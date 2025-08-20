import os, threading, requests
from fastapi import FastAPI, Request
from bot import main as worker_main  # твой бесконечный цикл опроса MEXC

TOKEN  = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT   = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SECRET = os.getenv("TV_SECRET", "").strip()  # опционально, для /tv

app = FastAPI()
_started = False

def start_worker_once():
    global _started
    if _started: return
    threading.Thread(target=worker_main, daemon=True).start()
    _started = True

@app.on_event("startup")
def _startup():
    start_worker_once()

@app.get("/")
def health():
    return {"ok": True}

def send_tg(text: str):
    if TOKEN and CHAT:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      json={"chat_id": CHAT, "text": text})

@app.post("/tv")
async def tv(req: Request):
    data = await req.json()
    if SECRET and data.get("secret") != SECRET:
        return {"status": "bad secret"}
    sym = str(data.get("symbol", ""))
    tf  = str(data.get("interval", ""))
    px  = str(data.get("price", ""))
    act = str(data.get("action", ""))
    t   = str(data.get("time", ""))
    send_tg(f"TV ▶ {sym} {tf} {act} @ {px}\n{t}")
    return {"status": "ok"}
