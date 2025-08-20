import os, threading, requests
from fastapi import FastAPI, Request, HTTPException
from bot import main as worker_main  # бесконечный опрос MEXC

TOKEN  = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT   = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SECRET = os.getenv("TV_SECRET", "").strip()

app = FastAPI()
_started = False
_lock = threading.Lock()

def start_worker_once():
    global _started
    with _lock:
        if _started:
            return
        threading.Thread(target=worker_main, daemon=True, name="mexc-worker").start()
        _started = True
        print("Worker thread started")

@app.on_event("startup")
def _startup():
    start_worker_once()

# health: поддерживаем GET и HEAD, чтобы не было 405
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
