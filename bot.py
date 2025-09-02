# -*- coding: utf-8 -*-
"""
FastAPI + python-telegram-bot (v20/v21, async)
- /health       → быстрый healthcheck (GET/HEAD)
- /test_sig     → мгновенно 200 OK; отправка в Telegram уходит в фоне
- /send?text=…  → отправка произвольного текста (в фоне)
Запуск локально:  python bot.py
На Render:        uvicorn bot:app --host 0.0.0.0 --port $PORT
"""

import os
import json
import asyncio
from datetime import datetime, timezone
from typing import Optional, Union

from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks, Query
from fastapi.responses import JSONResponse, Response

from telegram import Bot
from telegram.error import TelegramError

# ─────────────────────────── ENV ───────────────────────────
load_dotenv(override=True)
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "").strip()
PORT     = int(os.getenv("PORT", "8000"))

# ─────────────────────────── UTILS ─────────────────────────
def log(msg: str, **kwargs):
    print(json.dumps(
        {"ts": datetime.now(timezone.utc).isoformat(), "msg": msg, **kwargs},
        ensure_ascii=False
    ), flush=True)

def parse_chat_id(raw: str) -> Optional[Union[int, str]]:
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("@"):
        return s
    try:
        return int(s)
    except ValueError:
        return s

CHAT_ID: Optional[Union[int, str]] = parse_chat_id(TG_CHAT)
BOT: Optional[Bot] = Bot(TG_TOKEN) if TG_TOKEN else None  # PTB 20/21 async Bot

async def send_telegram_async(text: str) -> dict:
    """Безопасная отправка (async). Ничего наружу не пробрасывает."""
    if not TG_TOKEN:
        return {"ok": False, "reason": "TELEGRAM_TOKEN is empty"}
    if CHAT_ID is None:
        return {"ok": False, "reason": "TELEGRAM_CHAT_ID is empty"}
    if BOT is None:
        return {"ok": False, "reason": "Bot init failed"}

    try:
        await BOT.send_message(
            chat_id=CHAT_ID,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return {"ok": True}
    except TelegramError as te:
        log("telegram_error", error=str(te))
        return {"ok": False, "reason": f"telegram_error: {te}"}
    except Exception as e:
        log("unexpected_error", error=str(e))
        return {"ok": False, "reason": f"unexpected_error: {e}"}

def fire_and_forget(coro):
    """Планирует корутину, не блокируя HTTP-ответ."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(coro)
    except RuntimeError:
        # если нет запущенного loop (напр., при запуске из CLI)
        asyncio.run(coro)

# ─────────────────────────── FASTAPI ───────────────────────
app = FastAPI(title="Render Telegram Safe Bot (async)")

@app.get("/")
async def root():
    return {"ok": True, "service": "Render Telegram Safe Bot (async)"}

@app.head("/")
async def root_head():
    return Response(status_code=200)

@app.get("/health")
async def health():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}

@app.head("/health")
async def health_head():
    return Response(status_code=200)

@app.get("/test_sig")
async def test_sig(background: BackgroundTasks):
    """
    Возвращает 200 сразу; отправка в Telegram идёт в фоне и не уронит сервер.
    """
    text = "✅ <b>test_sig</b> — сервис на связи"
    # Вариант 1: планируем корутину через BackgroundTasks (FastAPI сам её await-нит)
    background.add_task(send_telegram_async, text)
    # Вариант 2: вручную, без BackgroundTasks (раскомментируйте при желании):
    # fire_and_forget(send_telegram_async(text))
    return JSONResponse({"ok": True, "queued": True, "endpoint": "test_sig"})

@app.head("/test_sig")
async def test_sig_head():
    return Response(status_code=200)

@app.get("/send")
async def send(text: str = Query(..., min_length=1, description="Текст сообщения")):
    # Мгновенный ответ + отложенная отправка
    fire_and_forget(send_telegram_async(text))
    return JSONResponse({"ok": True, "queued": True, "len": len(text)})

# ─────────────────────────── MAIN ─────────────────────────
if __name__ == "__main__":
    import uvicorn
    log("starting_uvicorn", port=PORT)
    uvicorn.run("bot:app", host="0.0.0.0", port=PORT, reload=False)
