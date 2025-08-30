# -*- coding: utf-8 -*-
# Regime Switcher (Donchian Trend Breakout + Range Reversion) → Telegram (messages only)
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
from typing import List, Tuple, Optional
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

# Обратная совместимость: если MEXC_SYMBOLS не задано — используем одиночные переменные
MEXC_SYMBOL_OLD   = os.getenv("MEXC_SYMBOL", "EURUSDT").strip()
MEXC_INTERVAL_OLD = os.getenv("MEXC_INTERVAL", "15m").strip()
MEXC_SYMBOLS_RAW  = os.getenv("MEXC_SYMBOLS", "").strip()   # напр.: "EURUSDT:5m,BTCUSDT:30m"

# Параметры стратегии
CHLEN       = int(os.getenv("CHLEN", "40"))
ADX_LEN     = int(os.getenv("ADX_LEN", "14"))
ADX_MIN     = float(os.getenv("ADX_MIN", "24"))
ATR_LEN     = int(os.getenv("ATR_LEN", "14"))
ATR_MIN_PC  = float(os.getenv("ATR_MIN_PC", "0.018"))   # 1.8% от цены
BUF_ATR     = float(os.getenv("BUF_ATR", "0.20"))
DIST_SLOW   = float(os.getenv("DIST_SLOW", "0.6"))

# Mean-reversion при слабом тренде
USE_MR   = os.getenv("USE_MR", "1").strip() != "0"
DEV_ATR  = float(os.getenv("DEV_ATR", "1.2"))
RSI_LEN  = int(os.getenv("RSI_LEN", "14"))
RSI_LOW  = float(os.getenv("RSI_LOW", "35"))
RSI_HIGH = float(os.getenv("RSI_HIGH", "65"))

# Сессия/лимиты (антиспам сообщений)
SESSION        = os.getenv("SESSION", "0700-1800").strip()
COOLDOWN_BARS  = int(os.getenv("COOLDOWN_BARS", "40"))
MAX_TRADES_DAY = int(os.getenv("MAX_TRADES_DAY", "2"))

POLL_DELAY = int(os.getenv("POLL_DELAY", "60"))
TZ_NAME    = os.getenv("TZ", "Europe/Belgrade").strip()
try:
    TZ_LOCAL = ZoneInfo(TZ_NAME)
except Exception:
    TZ_LOCAL = ZoneInfo("Europe/Belgrade")

# ───────────────────────── helpers/env ─────────────────────────
def normalize_interval(interval: str) -> str:
    valid = {"1m","3m","5m","15m","20m","30m","1h","2h","4h","6h","8h","12h","1d","1w","1M"}
    i = (interval or "").strip()
    return i if i in valid else "15m"

def _parse_symbols() -> List[Tuple[str, str]]:
    """
    Возвращает список (symbol, interval).
      - MEXC_SYMBOLS="EURUSDT,BTCUSDT" + общий MEXC_INTERVAL
      - MEXC_SYMBOLS="EURUSDT:5m,BTCUSDT:15m"
      - fallback: одиночные MEXC_SYMBOL / MEXC_INTERVAL
    """
    if not MEXC_SYMBOLS_RAW:
        return [(MEXC_SYMBOL_OLD, normalize_interval(MEXC_INTERVAL_OLD))]

    out: List[Tuple[str, str]] = []
    common_interval = normalize_interval(os.getenv("MEXC_INTERVAL", MEXC_INTERVAL_OLD))
    for chunk in MEXC_SYMBOLS_RAW.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" in chunk:
            sym, itv = chunk.split(":", 1)
            out.append((sym.strip().upper(), normalize_interval(itv.strip())))
        else:
            out.append((chunk.upper(), common_interval))
    return out or [(MEXC_SYMBOL_OLD, normalize_interval(MEXC_INTERVAL_OLD))]

PAIRS: List[Tuple[str, str]] = _parse_symbols()

# ─────────────────────── Файлы состояния (антидубль по каждой паре) ───────────────────────
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

def _sig_state_file(symbol: str, interval: str) -> pathlib.Path:
    safe_sym = symbol.replace("/", "_")
    safe_itv = interval.replace("/", "_")
    return STATE_DIR / f"sig_{safe_sym}_{safe_itv}.json"

def _already_sent(side: str, bar_ts: pd.Timestamp, symbol: str, interval: str) -> bool:
    """True если такой же сигнал уже слали на этом баре (переживает рестарты) — отдельно по паре/интервалу."""
    f = _sig_state_file(symbol, interval)
    try:
        st = json.loads(f.read_text())
        if (
            st.get("side") == side
            and st.get("bar") == bar_ts.isoformat()
            and st.get("symbol") == symbol
            and st.get("interval") == interval
        ):
            return True
    except Exception:
        pass
    try:
        f.write_text(json.dumps({
            "side": side,
            "bar": bar_ts.isoformat(),
            "symbol": symbol,
            "interval": interval,
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
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass
    asyncio.run(_send_async(text))

# ─────────────────────────── helpers ───────────────────────────
def drop_unclosed_last_bar(df: pd.DataFrame) -> pd.DataFrame:
    return df.iloc[:-1] if df is not None and len(df) >= 1 else df

def fmt_time_local(ts_utc) -> str:
    dt = ts_utc.to_pydatetime().replace(tzinfo=timezone.utc) if isinstance(ts_utc, pd.Timestamp) else ts_utc.replace(tzinfo=timezone.utc)
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

def rma(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(alpha=1.0/float(length), adjust=False).mean()

def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()

def rsi(series: pd.Series, length: int) -> pd.Series:
    d = series.diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    rs = rma(up, length) / rma(dn, length).replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    return out.fillna(50.0)

def atr_df(df: pd.DataFrame, length: int) -> pd.Series:
    c, h, l = df["c"], df["h"], df["l"]
    prev = c.shift(1)
    tr = pd.concat([(h-l), (h-prev).abs(), (l-prev).abs()], axis=1).max(axis=1)
    return rma(tr, length)

def adx_df(df: pd.DataFrame, length: int) -> pd.Series:
    h, l, c = df["h"], df["l"], df["c"]
    up = h.diff()
    dn = -l.diff()
    plus_dm  = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([(h-l), (h-c.shift(1)).abs(), (l-c.shift(1)).abs()], axis=1).max(axis=1)
    tr_rma = rma(tr, length)
    pdi = 100.0 * rma(pd.Series(plus_dm, index=df.index), length) / tr_rma.replace(0.0, np.nan)
    mdi = 100.0 * rma(pd.Series(minus_dm, index=df.index), length) / tr_rma.replace(0.0, np.nan)
    dx = 100.0 * (pdi - mdi).abs() / (pdi + mdi).replace(0.0, np.nan)
    return rma(dx.fillna(0.0), length)

def interval_to_htf(interval: str) -> str:
    i = normalize_interval(interval).lower()
    if i in ("1m","3m","5m","15m"): return "4h"
    if i in ("20m","30m","1h"):     return "1d"
    if i in ("2h","4h","6h","8h","12h","1d"): return "1w"
    return "1M"

def _fmt_signal_text(side: str, meta: dict, bar_ts: pd.Timestamp) -> str:
    head = "🟢 BUY" if side == "buy" else "🔴 SELL"
    p    = float(meta["price"])
    when = fmt_time_local(bar_ts)
    return (
        f"{head}  #{meta['symbol']} ({meta['interval']}) | {when}\n"
        f"price={p:.5f}  adx={meta['adx']:.1f}  atr%={meta['atr_pc']:.2f}%  regime={meta['regime']}\n"
        f"HTF: up={meta['htf_up']} dn={meta['htf_dn']}  "
        f"Donchian: H={meta['don_hi_prev']:.5f}  L={meta['don_lo_prev']:.5f}"
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
                print(f"[klines] MEXC v3 | {symbol} {interval}")
                return _df_from_k(arr)
        raise RuntimeError(f"mexc v3 status {r.status_code}")
    except Exception as e:
        print(f"v3 failed → v2: {e}")

    # 2) MEXC open/api v2
    try:
        url_v2 = "https://www.mexc.com/open/api/v2/market/kline"
        sym_v2 = symbol if "_" in symbol else symbol.replace("USDT", "_USDT")
        i_map = {
            "1m": "Min1", "3m": "Min3", "5m": "Min5", "15m": "Min15", "30m": "Min30",
            "1h": "Hour1", "2h": "Hour2", "4h": "Hour4", "6h": "Hour6", "8h": "Hour8", "12h": "Hour12",
            "1d": "Day1", "1w": "Week1", "1M": "Month1",
        }
        i_v2 = i_map.get(interval.lower(), "Min15")
        r2 = SESSION_HTTP.get(
            url_v2,
            params={"symbol": sym_v2, "interval": i_v2, "limit": limit},
            timeout=20,
        )
        if r2.status_code == 200:
            js = r2.json()
            arr = js.get("data", []) if isinstance(js, dict) else []
            if arr:
                data = [[int(a["t"]) * 1000, float(a["o"]), float(a["h"]), float(a["l"]),
                         float(a["c"]), float(a["v"]), 0, 0] for a in arr]
                print(f"[klines] MEXC v2 | {symbol} {interval}")
                return _df_from_k(data)
        raise RuntimeError(f"mexc v2 status {r2.status_code}")
    except Exception as e:
        print(f"v2 failed → binance: {e}")

    # 3) Binance v3
    try:
        r3 = SESSION_HTTP.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=20,
        )
        r3.raise_for_status()
        arr = r3.json()
        if not isinstance(arr, list) or not arr:
            raise RuntimeError("binance empty")
        data = [[a[0], a[1], a[2], a[3], a[4], a[5], a[6], 0] for a in arr]
        print(f"[klines] Binance v3 | {symbol} {interval}")
        return _df_from_k(data)
    except Exception as e:
        raise RuntimeError(f"All klines sources failed: {e}")

# ─────────────────────────── Стратегия ───────────────────────────
def make_signal(df_ltf: pd.DataFrame, df_htf: pd.DataFrame, symbol: str, interval: str) -> Tuple[Optional[str], dict]:
    df = df_ltf.copy()
    df["ema50"]  = ema(df["c"], 50)
    df["ema200"] = ema(df["c"], 200)
    df["atr"]    = atr_df(df, ATR_LEN)
    df["adx"]    = adx_df(df, ADX_LEN)
    df["rsi"]    = rsi(df["c"], RSI_LEN)

    c      = df["c"].iloc[-1]
    ema50  = df["ema50"].iloc[-1]
    ema200 = df["ema200"].iloc[-1]
    atr    = df["atr"].iloc[-1]
    adx    = df["adx"].iloc[-1]
    rsi_v  = df["rsi"].iloc[-1]

    don_hi_prev = df["h"].rolling(CHLEN).max().shift(1).iloc[-1]
    don_lo_prev = df["l"].rolling(CHLEN).min().shift(1).iloc[-1]

    D = df_htf.copy()
    D["ema200"] = ema(D["c"], 200)
    htf_up = (D["c"].iloc[-1] > D["ema200"].iloc[-1]) and (D["ema200"].iloc[-1] > D["ema200"].iloc[-2])
    htf_dn = (D["c"].iloc[-1] < D["ema200"].iloc[-1]) and (D["ema200"].iloc[-1] < D["ema200"].iloc[-2])

    trend_up = (c > ema50) and (ema50 > ema200)
    trend_dn = (c < ema50) and (ema50 < ema200)
    atr_ok   = (atr / c * 100.0) >= (ATR_MIN_PC * 100.0)
    adx_ok   = adx >= ADX_MIN
    far_slow = abs(c - ema200) >= (DIST_SLOW * atr)

    long_break  = c > (don_hi_prev + BUF_ATR * atr)
    short_break = c < (don_lo_prev - BUF_ATR * atr)

    go_long  = htf_up and trend_up and atr_ok and adx_ok and far_slow and long_break
    go_short = htf_dn and trend_dn and atr_ok and adx_ok and far_slow and short_break

    side: Optional[str] = None
    regime = "trend"
    if go_long and not go_short:
        side = "buy"
    elif go_short and not go_long:
        side = "sell"
    elif USE_MR and not adx_ok:
        regime = "range"
        dev = DEV_ATR * atr
        long_mr  = (c < ema50 - dev) and (rsi_v < RSI_LOW)
        short_mr = (c > ema50 + dev) and (rsi_v > RSI_HIGH)
        if long_mr and not short_mr:
            side = "buy"
        elif short_mr and not long_mr:
            side = "sell"

    meta = {
        "symbol": symbol, "interval": interval, "price": float(c),
        "ema50": float(ema50), "ema200": float(ema200),
        "atr": float(atr), "atr_pc": float(atr/c*100.0) if c else None,
        "adx": float(adx), "rsi": float(rsi_v),
        "htf_up": bool(htf_up), "htf_dn": bool(htf_dn),
        "trend_up": bool(trend_up), "trend_dn": bool(trend_dn),
        "atr_ok": bool(atr_ok), "adx_ok": bool(adx_ok), "far_slow": bool(far_slow),
        "don_hi_prev": float(don_hi_prev), "don_lo_prev": float(don_lo_prev),
        "regime": regime,
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
        self.last_msg_index: Optional[int] = None
        self.msgs_today = 0
        self.cur_day = None

    def _ok_limits(self, df_len: int) -> bool:
        now_local = datetime.now(tz=TZ_LOCAL)
        d = (now_local.year, now_local.month, now_local.day)
        if self.cur_day != d:
            self.cur_day = d
            self.msgs_today = 0
        if self.msgs_today >= MAX_TRADES_DAY:
            return False
        if self.last_msg_index is None:
            return True
        return (df_len - self.last_msg_index) >= COOLDOWN_BARS

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

            htf = interval_to_htf(self.mexc_interval)
            df_htf = load_klines(self.mexc_symbol, htf)

            side, meta = make_signal(df, df_htf, self.mexc_symbol, self.mexc_interval)

            if side and self._ok_limits(len(df)) and in_session(datetime.now(timezone.utc)):
                if not _already_sent(side, lt, self.mexc_symbol, self.mexc_interval):
                    text = _fmt_signal_text(side, meta, lt)
                    print(text)
                    send_msg(text)
                    self.last_msg_index = len(df)
                    self.msgs_today += 1
                else:
                    print(f"[{self.label}] duplicate signal skipped | {side} @ {lt}")
            else:
                print(f"[{self.label}] no signal | bar {lt}")

            self.last_bar_time = lt

        except Exception as e:
            print(f"[{self.label}] Ошибка: {e}")

# ─────────────────────────── Воркеры ───────────────────────────
def _spawn_worker(sym: str, itv: str):
    task = Task(
        label=f"{sym} ({itv})",
        poll_delay=POLL_DELAY,
        mexc_symbol=sym,
        mexc_interval=itv,
    )
    while True:
        task.tick()
        time.sleep(1)

def start_all_workers():
    for sym, itv in PAIRS:
        t = threading.Thread(target=_spawn_worker, args=(sym, itv), daemon=True)
        t.start()
    print(f"Бот запущен. Источник: MEXC spot klines (с fallback). TZ: {TZ_NAME}. Пары: {PAIRS}")

# ─────────────────────────── FastAPI (keep-alive) ───────────────────────────
app = FastAPI()
_workers_started = False
_workers_lock = threading.Lock()

def _start_workers_once():
    global _workers_started
    with _workers_lock:
        if _workers_started:
            return
        start_all_workers()
        _workers_started = True

@app.on_event("startup")
def _startup():
    _start_workers_once()

@app.get("/")
def root():
    pairs = [f"{s} ({itv})" for s, itv in PAIRS]
    return {"ok": True, "service": "mexc-telegram-bot", "pairs": pairs}

@app.get("/health")
def health():
    return JSONResponse({"ok": True, "ts": int(time.time()), "tz": TZ_NAME})

@app.get("/test_sig")
def test_sig():
    sym, itv = PAIRS[0]
    dummy = {
        "symbol": sym, "interval": itv, "price": 123.45678,
        "adx": 25.0, "atr_pc": 1.23, "regime": "trend",
        "htf_up": True, "htf_dn": False,
        "don_hi_prev": 1.0, "don_lo_prev": 0.9
    }
    now_bar = pd.Timestamp.utcnow().tz_localize("UTC")
    text = _fmt_signal_text("buy", dummy, now_bar)
    send_msg(text)
    return {"ok": True, "sent": f"BUY #{sym} ({itv})"}

# ─────────────────────────── Script mode ───────────────────────────
if __name__ == "__main__":
    start_all_workers()
    while True:
        time.sleep(3600)
