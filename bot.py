# -*- coding: utf-8 -*-
"""
FastAPI + Telegram Bot (safe, non-blocking)
- /health  → быстрый healthcheck для Render
- /test_sig → мгновенно 200 OK, отправка в Telegram уходит в фоне (и не роняет сервер)
- /send?text=... → отправить произвольный текст в Telegram (тоже в фоне)

Локально:
  python bot.py
Веб (Render):
  uvicorn bot:app --host 0.0.0.0 --port $PORT
"""

import os
import sys
import json
import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, BackgroundTasks, Query
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

# Библиотека python-telegram-bot (классический Bot API, синхронный)
from telegram import Bot
from telegram.error import TelegramError

# ─────────────────────────── ENV ───────────────────────────
load_dotenv(override=True)

TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "").strip()

PORT     = int(os.getenv("PORT", "8000"))
APP_TZ   = os.getenv("APP_TZ", "UTC").strip()

# ─────────────────────────── UTIL ─────────────────────────
def log(msg: str, **kwargs):
    """Простой JSON-лог в stdout (видно в Render Logs)."""
    payload = {"ts": datetime.now(timezone.utc).isoformat(), "msg": msg}
    if kwargs:
        payload.update(kwargs)
    print(json.dumps(payload, ensure_ascii=False), flush=True)

def parse_chat_id(raw: str) -> Optional[int | str]:
    """TELEGRAM_CHAT_ID может быть числом (user_id) или @channelusername."""
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("@"):
        return s  # каналы/паблики
    try:
        return int(s)
    except ValueError:
        # допускаем строковый id (редко), вернём как есть
        return s

def get_bot() -> Optional[Bot]:
    """Ленивая инициализация Bot; вернёт None, если токен пустой."""
    token = TG_TOKEN
    if not token:
        log("TELEGRAM_TOKEN is empty", level="warn")
        return None
    try:
        return Bot(token=token)
    except Exception as e:
        log("Bot init failed", error=str(e))
        return None

CHAT_ID = parse_chat_id(TG_CHAT)

def send_telegram_safe(text: str) -> dict:
    """
    Безопасная отправка в Telegram c try/except.
    Не бросает ошибок наружу: возвращает словарь со статусом.
    """
    if not TG_TOKEN:
        return {"ok": False, "reason": "TELEGRAM_TOKEN is empty"}
    if CHAT_ID is None:
        return {"ok": False, "reason": "TELEGRAM_CHAT_ID is empty"}

    bot = get_bot()
    if bot is None:
        return {"ok": False, "reason": "Bot init failed"}

    try:
        bot.send_message(
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

def enqueue_send(text: str):
    """
    Запускает отправку в отдельном daemon-потоке.
    Ответ по HTTP не блокируется.
    """
    def _worker():
        res = send_telegram_safe(text)
        log("send_telegram_done", result=res)

    threading.Thread(target=_worker, daemon=True).start()

# ─────────────────────────── FASTAPI ───────────────────────
app = FastAPI(title="Render Telegram Safe Bot")

@app.get("/")
def root():
    return {"ok": True, "service": "Render Telegram Safe Bot", "tz": APP_TZ}

@app.get("/health")
def health():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}

@app.get("/test_sig")
def test_sig(background: BackgroundTasks):
    """
    Тестовая ручка. Возвращает 200 мгновенно, отправка в Telegram уходит в фоне.
    Даже если TELEGRAM_TOKEN/CHAT_ID неверны — сервер не падает.
    """
    text = "✅ <b>test_sig</b> — сервис на связи"
    # Вариант через BackgroundTasks (FastAPI)
    background.add_task(send_telegram_safe, text)
    # ИЛИ через собственный тред:
    # enqueue_send(text)

    return JSONResponse({"ok": True, "queued": True, "endpoint": "test_sig"})

@app.get("/send")
def send(text: str = Query(..., min_length=1, description="Текст сообщения")):
    """
    Универсальная ручка для отправки произвольного текста.
    Ответ — сразу; отправка — в фоне.
    """
    # Можно выбрать любой из механизмов:
    enqueue_send(text)
    return JSONResponse({"ok": True, "queued": True, "len": len(text)})

# ─────────────────────────── MAIN ─────────────────────────
if __name__ == "__main__":
    # Локальный запуск: python bot.py
    import uvicorn
    log("starting_uvicorn", port=PORT)
    uvicorn.run("bot:app", host="0.0.0.0", port=PORT, reload=False)
