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

import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv
from telegram import Bot

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

# ───────────────────── HTTP session ─────────────────────
HEADERS = {
    "User-Agent": "mkt-telegram-bot/1.1 (+https://render.com)",
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
def load_klines_yahoo_fx(symbol: str, interval: str, range_str: str = "30d") -> pd.DataFrame:
    ysym = to_yahoo_fx(symbol)
    i_int = normalize_interval(interval)
    if i_int == "1h":  # у Yahoo часовой – это 60m
        i_int = "60m"

    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + ysym
    params = {"interval": i_int, "range": range_str}

    resp = None
    for attempt in range(5):
        _yahoo_rl(8.0, 3.0)
        r = SESSION_HTTP.get(url, params=params, timeout=20)
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
    o, h, l, c = q.get("open", []), q.get("high", []), q.get("low", []), q.get("close", [])

    rows = []
    for k in range(min(len(ts), len(o), len(h), len(l), len(c))):
        if None in (ts[k], o[k], h[k], l[k], c[k]):
            continue
        rows.append([int(ts[k]) * 1000, float(o[k]), float(h[k]), float(l[k]), float(c[k])])

    df = pd.DataFrame(rows, columns=["t", "o", "h", "l", "c"])
    if df.empty:
        raise RuntimeError("yahoo rows empty")
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.set_index("t").sort_index()
    df = drop_unclosed_last_bar(df)
    df["complete"] = True
    return df

# ─────────────────────────── KLINES: CRYPTO ───────────────────────────
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

# ── MEXC: мапперы
def _mexc_spot_interval(interval: str) -> Optional[str]:
    """
    Интервалы, которые понимает MEXC spot v3.
    1h -> 60m; «нестандартные» (3m,20m,45m,2h,6h,8h,12h) не поддерживаются.
    """
    i = normalize_interval(interval)
    support = {
        "1m": "1m",
        "5m": "5m",
        "15m": "15m",
        "30m": "30m",
        "1h": "60m",  # ВАЖНО
        "4h": "4h",
        "1d": "1d",
        "1w": "1w",
        "1M": "1M",
    }
    return support.get(i, None)

def _mexc_contract_symbol(sym_spot: str) -> str:
    # EURUSDT -> EUR_USDT и т.п.
    s = sym_spot.upper()
    return s.replace("USDT", "_USDT") if "_USDT" not in s else s

def _mexc_contract_interval(interval: str) -> str:
    # Min1/3/5/15/30, Hour1/2/4/6/8/12, Day1, Week1, Month1
    i = normalize_interval(interval)
    m = {
        "1m":"Min1","3m":"Min3","5m":"Min5","15m":"Min15","30m":"Min30",
        "1h":"Hour1","2h":"Hour2","4h":"Hour4","6h":"Hour6","8h":"Hour8","12h":"Hour12",
        "1d":"Day1","1w":"Week1","1M":"Month1"
    }
    return m.get(i, "Min30")

def load_klines_crypto(symbol: str, interval: str, limit: int = 1000) -> pd.DataFrame:
    i_int = normalize_interval(interval)
    sym = symbol.replace("/", "").upper()

    # ───── 1) MEXC spot v3 ─────
    if MEXC_MODE == "spot":
        try:
            i_m = _mexc_spot_interval(i_int)
            if not i_m:
                print(f"[info] MEXC v3 spot: interval '{i_int}' unsupported → skip to Binance")
            else:
                r = SESSION_HTTP.get(
                    "https://api.mexc.com/api/v3/klines",
                    params={"symbol": sym, "interval": i_m, "limit": limit},
                    timeout=20,
                )
                if r.status_code == 200:
                    arr = r.json()
                    if isinstance(arr, list) and arr:
                        print(f"[klines] MEXC v3 spot {sym} ({i_m})")
                        return _df_from_k(arr)
                print(f"[warn] MEXC v3 spot {r.status_code}: {r.text[:300]}")
        except Exception as e:
            print(f"[warn] MEXC v3 spot exception: {e}")

    # ───── 2) MEXC contracts (фьючерсы) ─────
    if MEXC_MODE == "contract":
        try:
            url = "https://contract.mexc.com/api/v1/contract/kline"
            sym_c = _mexc_contract_symbol(sym)
            i_c   = _mexc_contract_interval(i_int)
            r2 = SESSION_HTTP.get(url, params={"symbol": sym_c, "interval": i_c, "limit": limit}, timeout=20)
            if r2.status_code == 200:
                js = r2.json()
                data_arr = js.get("data") or js.get("dataList") or []
                if data_arr:
                    data = [[int(a["t"])*1000, float(a["o"]), float(a["h"]), float(a["l"]), float(a["c"]), float(a.get("v",0)), 0, 0]
                            for a in data_arr]
                    print(f"[klines] MEXC contract {sym_c} {i_c}")
                    return _df_from_k(data)
            print(f"[warn] MEXC contract {r2.status_code}: {r2.text[:300]}")
        except Exception as e:
            print(f"[warn] MEXC contract exception: {e}")

    # ───── 3) Binance v3 (fallback) ─────
    try:
        r3 = SESSION_HTTP.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": sym, "interval": i_int, "limit": limit},
            timeout=20,
        )
        if r3.status_code != 200:
            print(f"[warn] Binance v3 {r3.status_code}: {r3.text[:300]}")
            r3.raise_for_status()
        arr = r3.json()
        if not arr:
            raise RuntimeError("binance empty")
        data = [[a[0], a[1], a[2], a[3], a[4], a[5], a[6], 0] for a in arr]
        print(f"[klines] Binance v3 {sym}")
        return _df_from_k(data)
    except Exception as e:
        raise RuntimeError(f"All klines sources failed for {sym}: {e}")

def load_klines(symbol: str, interval: str) -> pd.DataFrame:
    if is_fx(symbol):
        return load_klines_yahoo_fx(symbol, interval, range_str="30d")
    return load_klines_crypto(symbol, interval)

# ─────────────────────────── ЛОГИКА СИГНАЛА ───────────────────────────
def sawvap_last_signal(df: pd.DataFrame, symbol: str, interval: str, length: int, tol_pips: float):
    if df is None or len(df) < (length + 3):
        return None, {}, None

    roll_high = df["h"].rolling(length, min_periods=length).max()
    roll_low  = df["l"].rolling(length, min_periods=length).min()

    h   = roll_high
    h1  = roll_high.shift(1)
    l   = roll_low
    l1  = roll_low.shift(1)

    hi_prev = df["h"].shift(1)
    hi_curr = df["h"]
    lo_prev = df["l"].shift(1)
    lo_curr = df["l"]

    last_price = float(df["c"].iloc[-1])
    pip = pip_for(symbol, last_price)
    eps = tol_pips * pip

    isSELL = (np.abs(hi_prev - h1) <= eps) & (hi_curr < (h - eps))
    isBUY  = (np.abs(lo_prev - l1) <= eps) & (lo_curr > (l + eps))

    bar_time = df.index[-1]
    sell = bool(isSELL.iloc[-1])
    buy  = bool(isBUY.iloc[-1])

    side = "buy" if buy and not sell else ("sell" if sell and not buy else None)

    meta = {
        "symbol": symbol,
        "interval": interval,
        "price": last_price,
        "length": length,
        "tol_pips": tol_pips,
    }
    return side, meta, bar_time

# ─────────────────────────── Текст сигнала ───────────────────────────
def _fmt_signal_text(side: str, meta: dict, bar_ts) -> str:
    head = "🟢 BUY" if side == "buy" else "🔴 SELL"
    p    = float(meta["price"])
    sym  = meta["symbol"]
    interval = meta["interval"]
    when = fmt_time_local(bar_ts)
    digits = decimals_for(sym, p)
    return (
        f"{head}  #{sym} ({interval}) | {when}\n"
        f"price={p:.{digits}f}  len={meta['length']}  tolPips={meta['tol_pips']}"
    )

# ─────────────────────────── Task ───────────────────────────
class Task:
    def __init__(self, symbol: str, interval: str, poll_delay: int):
        self.symbol = symbol
        self.interval = interval
        self.poll_delay = poll_delay
        self.last_bar_time: Optional[pd.Timestamp] = None
        self.last_tick_ts = 0.0

    def tick(self):
        now = time.time()
        if now - self.last_tick_ts < self.poll_delay:
            return
        self.last_tick_ts = now
        label = f"{self.symbol} ({self.interval})"

        try:
            df = load_klines(self.symbol, self.interval)
            if df is None or df.empty:
                print(f"[{label}] df empty")
                return

            lt = df.index[-1]
            if self.last_bar_time is not None and lt <= self.last_bar_time:
                return

            side, meta, bar_ts = sawvap_last_signal(df, self.symbol, self.interval, LENGTH, TOL_PIPS)

            if side and in_session(datetime.now(timezone.utc)):
                if not _already_sent(self.symbol, self.interval, side, bar_ts):
                    text = _fmt_signal_text(side, meta, bar_ts)
                    print(text)
                    send_msg(text)
                else:
                    print(f"[{label}] duplicate signal skipped | {side} @ {bar_ts}")
            else:
                print(f"[{label}] no signal | bar {lt}")

            self.last_bar_time = lt

        except Exception as e:
            print(f"[{label}] Ошибка: {e}")

# ─────────────────────────── Воркер ───────────────────────────
def run_worker():
    # рассинхрон стартов, чтобы не долбить источники одновременно
    tasks = []
    base = time.time()
    for idx, sym in enumerate(MULTI_SYMBOLS):
        t = Task(symbol=sym, interval=INTERVAL, poll_delay=POLL_DELAY)
        # сдвигаем первый опрос для каждого инструмента на 10s * idx (но не позднее чем через POLL_DELAY-1)
        initial_offset = min(max(POLL_DELAY - 1, 1), 10 * idx)
        t.last_tick_ts = base - (POLL_DELAY - initial_offset)
        tasks.append(t)

    print(f"Бот запущен. TZ: {TZ_NAME} | symbols: {', '.join(MULTI_SYMBOLS)} | interval: {INTERVAL} | mexc_mode={MEXC_MODE}")
    while True:
        for t in tasks:
            t.tick()
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
    start_worker_once()
    return {"ok": True, "service": "sawvap-telegram-bot", "symbols": MULTI_SYMBOLS, "interval": INTERVAL, "mexc_mode": MEXC_MODE}

@app.head("/")
def root_head():
    start_worker_once()
    return Response(status_code=200)

@app.get("/health")
def health():
    return JSONResponse({"ok": True, "ts": int(time.time()), "tz": TZ_NAME})

@app.head("/health")
def health_head():
    return Response(status_code=200)

@app.get("/test_sig")
def test_sig():
    try:
        now_bar = datetime.now(timezone.utc)
        dummy = {"symbol": MULTI_SYMBOLS[0], "interval": INTERVAL, "price": 123.45678, "length": LENGTH, "tol_pips": TOL_PIPS}
        text = _fmt_signal_text("buy", dummy, now_bar)
        send_msg(text)
        return JSONResponse({"ok": True, "sent": True}, status_code=200)
    except Exception as e:
        print(f"/test_sig error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)

# ─────────────────────────── Script mode ───────────────────────────
if __name__ == "__main__":
    run_worker()
