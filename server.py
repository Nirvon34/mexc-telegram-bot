# server.py
import threading
from fastapi import FastAPI
from bot import main as bot_main  # импортируем твою main() из bot.py

app = FastAPI()

@app.get("/")
def health():
    return {"status": "ok"}

@app.on_event("startup")
def _start_background_bot():
    t = threading.Thread(target=bot_main, daemon=True)
    t.start()
