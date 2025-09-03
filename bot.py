# -*- coding: utf-8 -*-
# MA(5) x MA(10) → Telegram (messages only)
# Источник свечей: MEXC v3 → MEXC v2 → Binance v3 (fallback)
# Локально: python bot.py
# Веб:     uvicorn bot:app --host 0.0.0.0 --port $PORT

import os
import time
import json
import asyncio
import threading
import pathlib
import tempfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv
from telegram import Bot

from fastapi import FastAPI
from fastapi.responses import JSONResponse

# ─────────────────────────── ENV ───────────────────────────
load_dotenv(override=True)
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "").strip()

MEXC_SYMBOL   = os.getenv("MEXC_SYMBOL", "EURUSDT").strip()
MEXC_INTERVAL = os.getenv("MEXC_INTERVAL", "30m").strip()  # ← из .env

SESSION    = os.getenv("SESSION", "0000-2359").strip()      # ← 24/7 по умолчанию
POLL_DELAY = int(os.getenv("POLL_DELAY", "60"))
TZ_NAME    = os.getenv("TZ", "Europe/Belgrade").strip()
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

SIG_STATE = pathlib.Path(os.getenv("SIG_STATE_FILE", str(STATE_DIR / "mexc_last_signal.json")))

def _already_sent(side: str, bar_ts) -> bool:
    try:
        st = json.loads(SIG_STATE.read_text())
        if (
            st.get("side") == side
            and st.get("bar") == (bar_ts.isoformat() if hasattr(bar_ts, "isoformat") else str(bar_ts))
            and st.get("symbol") == MEXC_SYMBOL
            and st.get("interval") == MEXC_INTERVAL
        ):
            return True
    except Exception:
        pass
    try:
        SIG_STATE.write_text(json.dumps({
            "side": side,
            "bar": bar_ts.isoformat() if hasattr(bar_ts, "isoformat") else str(bar_ts),
            "symbol": MEXC_SYMBOL,
            "interval": MEXC_INTERVAL,
        }))
    except Exception:
        pass
    return False

# ───────────────────── HTTP session (MEXC/Binance) ─────────────────────
HEADERS = {
    "User-Agent": "mexc-telegram-bot/1.0 (+https://render.com)",
    "Accept": "application/json",
    "Origin": "https://www.mexc.com",
    "Referer": "https://www.mexc.com/",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Connection": "keep-alive",
}

def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.6,
        status_forcelist=(403, 429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update(HEADERS)
    return s

SESSION_HTTP = make_session()

# ─────────────────────────── Telegram ───────────────────────────
async def _send_async(text: str):
    if not TG_TOKEN or not TG_CHAT:
        print("⚠️ TELEGRAM_TOKEN/CHAT_ID не заданы. Сообщение:", text)
        return
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
def drop_unclosed_last_bar(df: pd.DataFrame) -> pd.DataFrame:
    return df.iloc[:-1] if df is not None and len(df) >= 1 else df

def fmt_time_local(ts_utc):
    if hasattr(ts_utc, "to_pydatetime"):
        dt = ts_utc.to_pydatetime()
    else:
        dt = ts_utc
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
        # если SESSION битый — не ограничиваем
        return True

def sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(length, min_periods=length).mean()

def normalize_interval(interval: str) -> str:
    valid = {"1m","3m","5m","15m","20m","30m","1h","2h","4h","6h","8h","12h","1d","1w","1M"}
    i = (interval or "").strip()
    return i if i in valid else "30m"

# ─────────────────────────── Текст сигнала ───────────────────────────
def _fmt_signal_text(side: str, meta: dict, bar_ts) -> str:
    head = "🟢 BUY" if side == "buy" else "🔴 SELL"
    p    = float(meta["price"])
    when = fmt_time_local(bar_ts)
    return (
        f"{head}  #{meta['symbol']} ({meta['interval']}) | {when}\n"
        f"price={p:.5f}  ma5={meta['ma5']:.5f}  ma10={meta['ma10']:.5f}  cross={meta.get('cross','')}"
    )

# ─────────────────────────── KLINES ───────────────────────────
def _df_from_k(arr) -> pd.DataFrame:
    row_len = len(arr[0])
    if row_len >= 12: cols = ["t","o","h","l","c","v","t2","q","n","tb","tq","ig"]
    elif row_len == 8: cols = ["t","o","h","l","c","v","t2","q"]
    else: cols = list(range(row_len))
    df = pd.DataFrame(arr, columns=cols).rename(columns={0:"t",1:"o",2:"h",3:"l",4:"c"})
    for need in ["t","o","h","l","c"]:
        if need not in df.columns:
            raise RuntimeError(f"Unexpected klines format: '{need}' missing")
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df[["o","h","l","c"]] = df[["o","h","l","c"]].astype(float)
    df = df[["t","o","h","l","c"]].set_index("t").sort_index()
    df = drop_unclosed_last_bar(df)
    df["complete"] = True
    return df

def load_klines(symbol: str, interval: str, limit: int = 1000) -> pd.DataFrame:
    interval = normalize_interval(interval)

    # 1) MEXC v3
    try:
        r = SESSION_HTTP.get(
            "https://api.mexc.com/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=20,
        )
        if r.status_code == 200:
            arr = r.json()
            if isinstance(arr, list) and arr:
                print("[klines] MEXC v3")
                return _df_from_k(arr)
        raise RuntimeError(f"mexc v3 status {r.status_code}")
    except Exception as e:
        print("v3 failed → v2:", e)

    # 2) MEXC open/api v2
    try:
        url_v2 = "https://www.mexc.com/open/api/v2/market/kline"
        sym_v2 = symbol if "_" in symbol else symbol.replace("USDT", "_USDT")
        i_map = {
            "1m":"Min1","3m":"Min3","5m":"Min5","15m":"Min15","30m":"Min30",
            "1h":"Hour1","2h":"Hour2","4h":"Hour4","6h":"Hour6","8h":"Hour8","12h":"Hour12",
            "1d":"Day1","1w":"Week1","1M":"Month1"
        }
        i_v2 = i_map.get(interval.lower(), "Min30")
        r2 = SESSION_HTTP.get(url_v2, params={"symbol": sym_v2, "interval": i_v2, "limit": limit}, timeout=20)
        if r2.status_code == 200:
            js = r2.json()
            arr = js.get("data", [])
            if arr:
                data = [[int(a["t"])*1000, float(a["o"]), float(a["h"]), float(a["l"]),
                         float(a["c"]), float(a["v"]), 0, 0] for a in arr]
                print("[klines] MEXC v2")
                return _df_from_k(data)
        raise RuntimeError(f"mexc v2 status {r2.status_code}")
    except Exception as e:
        print("v2 failed → binance:", e)

    # 3) Binance v3
    try:
        r3 = SESSION_HTTP.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=20,
        )
        r3.raise_for_status()
        arr = r3.json()
        if not arr:
            raise RuntimeError("binance empty")
        data = [[a[0], a[1], a[2], a[3], a[4], a[5], a[6], 0] for a in arr]
        print("[klines] Binance v3")
        return _df_from_k(data)
    except Exception as e:
        raise RuntimeError(f"All klines sources failed: {e}")

# ─────────────────────────── Стратегия: MA(5) x MA(10) ───────────────────────────
def make_signal(df_ltf: pd.DataFrame):
    if df_ltf is None or len(df_ltf) < 11:
        return None, {}

    df = df_ltf.copy()
    df["ma5"]  = sma(df["c"], 5)
    df["ma10"] = sma(df["c"], 10)

    ma5       = df["ma5"].iloc[-1]
    ma10      = df["ma10"].iloc[-1]
    ma5_prev  = df["ma5"].iloc[-2]
    ma10_prev = df["ma10"].iloc[-2]

    side = None
    cross = None
    if (ma5 > ma10) and (ma5_prev <= ma10_prev):
        side, cross = "buy", "up"
    elif (ma5 < ma10) and (ma5_prev >= ma10_prev):
        side, cross = "sell", "down"

    meta = {
        "symbol": MEXC_SYMBOL,
        "interval": MEXC_INTERVAL,
        "price": float(df["c"].iloc[-1]),
        "ma5": float(ma5) if pd.notna(ma5) else float("nan"),
        "ma10": float(ma10) if pd.notna(ma10) else float("nan"),
        "cross": cross,
    }
    return side, meta

# ─────────────────────────── Task ───────────────────────────
class Task:
    def __init__(self, label: str, poll_delay: int, mexc_symbol: str, mexc_interval: str):
        self.label = label
        self.poll_delay = poll_delay
        self.mexc_symbol = mexc_symbol
        self.mexc_interval = mexc_interval
        self.last_bar_time = None
        self.last_tick_ts = 0.0

    def tick(self):
        now = time.time()
        if now - self.last_tick_ts < self.poll_delay:
            return
        self.last_tick_ts = now
        try:
            df = load_klines(self.mexc_symbol, self.mexc_interval)
            if df.empty:
                return
            lt = df.index[-1]
            if self.last_bar_time is not None and lt <= self.last_bar_time:
                return

            side, meta = make_signal(df)

            if side and in_session(datetime.now(timezone.utc)):
                if not _already_sent(side, lt):
                    text = _fmt_signal_text(side, meta, lt)
                    print(text)
                    send_msg(text)
                else:
                    print(f"[{self.label}] duplicate signal skipped | {side} @ {lt}")
            else:
                print(f"[{self.label}] no signal | bar {lt}")

            self.last_bar_time = lt

        except Exception as e:
            print(f"[{self.label}] Ошибка: {e}")

# ─────────────────────────── Воркер ───────────────────────────
def run_worker():
    task = Task(
        label=f"{MEXC_SYMBOL} ({MEXC_INTERVAL})",
        poll_delay=POLL_DELAY,
        mexc_symbol=MEXC_SYMBOL,
        mexc_interval=MEXC_INTERVAL,
    )
    print(f"Бот запущен. Источник: MEXC spot klines (с fallback). TZ: {TZ_NAME}")
    while True:
        task.tick()
        time.sleep(1)

# ─────────────────────────── FastAPI ───────────────────────────
app = FastAPI()
_worker_started = False
_worker_lock = threading.Lock()

def start_worker_once():
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        t = threading.Thread(target=run_worker, daemon=True)
        t.start()
        _worker_started = True

@app.on_event("startup")
def _startup():
    start_worker_once()

@app.get("/")
def root():
    start_worker_once()  # на всякий случай
    return {"ok": True, "service": "mexc-telegram-bot", "symbol": MEXC_SYMBOL, "interval": MEXC_INTERVAL}

@app.get("/health")
def health():
    return JSONResponse({"ok": True, "ts": int(time.time()), "tz": TZ_NAME})

@app.get("/test_sig")
def test_sig():
    try:
        now_bar = datetime.now(timezone.utc)
        dummy = {"symbol": MEXC_SYMBOL, "interval": MEXC_INTERVAL, "price": 123.45678, "ma5": 123.50, "ma10": 123.40, "cross": "up"}
        text = _fmt_signal_text("buy", dummy, now_bar)
        send_msg(text)
        return JSONResponse({"ok": True, "sent": True}, status_code=200)
    except Exception as e:
        print(f"/test_sig error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)

# ─────────────────────────── Script mode ───────────────────────────
if __name__ == "__main__":
    run_worker()
