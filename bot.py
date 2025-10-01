# -*- coding: utf-8 -*-
# SA_VWAP 1h-style (window High/Low breakout with tolerance) → Telegram (messages only)
# FX: Yahoo Finance (EURUSD=X, GBPJPY=X …) · CRYPTO: MEXC v3 (spot/contract) → Binance v3

import os
import time
import json
import random
import asyncio
import threading
import pathlib
import tempfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

# ─────────────────────────── ENV ───────────────────────────
load_dotenv(override=True)
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Только одна пара (можно расширить списком)
MULTI_SYMBOLS = ["EURUSDT"]

# Интервал
INTERVAL   = (os.getenv("INTERVAL", "") or os.getenv("MEXC_INTERVAL", "1h")).strip()
SESSION    = os.getenv("SESSION", "0000-2359").strip()  # 24/7
POLL_DELAY = int(os.getenv("POLL_DELAY", "60"))         # сек между попытками опроса
TZ_NAME    = os.getenv("TZ", "Europe/Belgrade").strip()
MEXC_MODE  = os.getenv("MEXC_MODE", "spot").strip().lower()  # spot | contract

# Параметры логики
LENGTH    = int(os.getenv("LENGTH", "180"))
TOL_PIPS  = float(os.getenv("TOL_PIPS", "1.0"))

# Необязательно: задержка “готовности” при старте (сек) — обычно 0
READINESS_DELAY = int(os.getenv("READINESS_DELAY", "0"))

try:
    TZ_LOCAL = ZoneInfo(TZ_NAME)
except Exception:
    TZ_LOCAL = ZoneInfo("Europe/Belgrade")

# ─────────────────────── Файлы состояния ───────────────────────
def _ensure_dir(p: pathlib.Path) -> pathlib.Path:
    try:
        p.mkdir(parents=True, exist_ok=True)
        (p / ".wtest").write_text("ok", encoding="utf-8")
        (p / ".wtest").unlink()
        return p
    except Exception:
        tmp = pathlib.Path(tempfile.gettempdir()) / "mexc_state"
        tmp.mkdir(parents=True, exist_ok=True)
        return tmp

DEFAULT_STATE_DIR = r"C:\tv2mt5\state" if os.name == "nt" else "/data"
STATE_DIR = pathlib.Path(os.getenv("STATE_DIR", DEFAULT_STATE_DIR))
STATE_DIR = _ensure_dir(STATE_DIR)

def _sig_path(symbol: str, interval: str) -> pathlib.Path:
    safe = symbol.replace("/", "_")
    return STATE_DIR / f"last_signal_{safe}_{interval}.json"

def _already_sent(symbol: str, interval: str, side: str, bar_ts) -> bool:
    path = _sig_path(symbol, interval)
    try:
        st = json.loads(path.read_text())
        if (
            st.get("side") == side
            and st.get("bar") == (bar_ts.isoformat() if hasattr(bar_ts, "isoformat") else str(bar_ts))
            and st.get("symbol") == symbol
            and st.get("interval") == interval
        ):
            return True
    except Exception:
        pass
    try:
        path.write_text(json.dumps({
            "side": side,
            "bar": bar_ts.isoformat() if hasattr(bar_ts, "isoformat") else str(bar_ts),
            "symbol": symbol,
            "interval": interval,
        }))
    except Exception:
        pass
    return False

# ───────────────────── ЛЕНИВЫЕ ИМПОРТЫ (ускорение cold start) ─────────────────────
# Эти переменные будут заполнены при старте воркера.
np = None
pd = None
requests = None
Retry = None
HTTPAdapter = None

def _lazy_heavy_imports():
    """Импортируем тяжёлые библиотеки, когда действительно нужно (в воркере)."""
    global np, pd, requests, Retry, HTTPAdapter
    if np is not None:
        return
    import numpy as _np
    import pandas as _pd
    import requests as _requests
    from requests.adapters import HTTPAdapter as _HTTPAdapter
    from urllib3.util.retry import Retry as _Retry
    np, pd, requests, HTTPAdapter, Retry = _np, _pd, _requests, _HTTPAdapter, _Retry

# ───────────────────── HTTP session (после ленивых импортов) ─────────────────────
SESSION_HTTP = None

def _make_session():
    global SESSION_HTTP
    if SESSION_HTTP is not None:
        return SESSION_HTTP
    retry = Retry(
        total=3,
        backoff_factor=0.6,
        status_forcelist=(403, 429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({
        "User-Agent": "mkt-telegram-bot/1.1 (+https://render.com)",
        "Accept": "application/json",
        "Origin": "https://www.mexc.com",
        "Referer": "https://www.mexc.com/",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
    })
    SESSION_HTTP = s
    return s

# ── rate-limit для Yahoo
_YAHOO_LAST = 0.0
def _yahoo_rl(min_gap=8.0, jitter=3.0):
    global _YAHOO_LAST
    now = time.time()
    remain = min_gap - (now - _YAHOO_LAST)
    if remain > 0:
        time.sleep(remain + random.random() * jitter)
    _YAHOO_LAST = time.time()

# ─────────────────────────── Telegram ───────────────────────────
async def _send_async(text: str):
    if not TG_TOKEN or not TG_CHAT:
        print("⚠️ TELEGRAM_TOKEN/CHAT_ID не заданы. Сообщение:", text)
        return
    # ленивый импорт telegram
    from telegram import Bot
    async with Bot(TG_TOKEN) as bot:
        await bot.send_message(chat_id=TG_CHAT, text=text, disable_web_page_preview=True)

def send_msg(text: str):
    def _runner():
        try:
            asyncio.run(_send_async(text))
        except Exception as e:
            print(f"[telegram] send error: {e}")
    threading.Thread(target=_runner, daemon=True).start()

# ─────────────────────────── helpers ───────────────────────────
def drop_unclosed_last_bar(df):
    return df.iloc[:-1] if df is not None and len(df) >= 1 else df

def fmt_time_local(ts_utc):
    dt = ts_utc.to_pydatetime() if hasattr(ts_utc, "to_pydatetime") else ts_utc
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ_LOCAL).strftime("%Y-%m-%d %H:%M:%S %Z")

def in_session(now: datetime) -> bool:
    try:
        s, e = SESSION.split("-")
        sh, sm = int(s[:2]), int(s[2:])
        eh, em = int(e[:2]), int(e[2:])
        local = now.astimezone(TZ_LOCAL)
        t = local.hour*60 + local.minute
        return (sh*60 + sm) <= t <= (eh*60 + em)
    except Exception:
        return True

def normalize_interval(interval: str) -> str:
    valid = {"1m","3m","5m","15m","20m","30m","45m","1h","2h","4h","6h","8h","12h","1d","1w","1M"}
    i = (interval or "").strip()
    return i if i in valid else "1h"

def is_fx(sym: str) -> bool:
    s = sym.replace("/", "")
    return len(s) == 6 and s.isalpha()

def to_yahoo_fx(sym: str) -> str:
    return sym.replace("/", "").upper() + "=X"

def decimals_for(symbol: str, price: float) -> int:
    s = symbol.replace("/", "").upper()
    if len(s) == 6 and s.endswith("JPY"):
        return 3
    if len(s) == 6:
        return 5
    if price >= 100:
        return 2
    if price >= 1:
        return 4
    return 6

def pip_for(symbol: str, price: float) -> float:
    s = symbol.replace("/", "").upper()
    if len(s) == 6 and s.endswith("JPY"):
        return 0.01
    if len(s) == 6:
        return 0.0001
    if price >= 100:
        return 0.1
    if price >= 1:
        return 0.01
    return 0.0001

# ─────────────────────────── KLINES: FX (Yahoo) ───────────────────────────
def load_klines_yahoo_fx(symbol: str, interval: str, range_str: str = "30d"):
    _lazy_heavy_imports()
    s = _make_session()
    ysym = to_yahoo_fx(symbol)
    i_int = normalize_interval(interval)
    if i_int == "1h":  # у Yahoo часовой – это 60m
        i_int = "60m"

    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + ysym
    params = {"interval": i_int, "range": range_str}

    resp = None
    for attempt in range(5):
        _yahoo_rl(8.0, 3.0)
        r = s.get(url, params=params, timeout=20)
        code = r.status_code
        if code == 429:
            sleep = min(60, 3.0 * (2 ** attempt))
            print(f"[yahoo] 429 rate-limited → sleep {sleep:.1f}s (attempt {attempt+1}/5)")
            time.sleep(sleep)
            continue
        if code in (500, 502, 503, 504, 403):
            sleep = min(30, 2.0 * (2 ** attempt))
            print(f"[yahoo] {code} → retry after {sleep:.1f}s (attempt {attempt+1}/5)")
            time.sleep(sleep)
            continue
        r.raise_for_status()
        resp = r
        break
    if resp is None:
        raise RuntimeError(f"yahoo rate-limited after retries for {symbol} {i_int}")

    js = resp.json()
    res = js.get("chart", {}).get("result", [])
    if not res:
        raise RuntimeError("yahoo rows empty")
    result = res[0]
    ts = result.get("timestamp", [])
    ind = result.get("indicators", {})
    q = (ind.get("quote") or [{}])[0]
    o, h, l, c = q.get("open", []), q.get("high", []), q.get("low", []),
