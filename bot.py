# -*- coding: utf-8 -*-
# Swing Anchored VWAP → Telegram (MEXC, EURUSDT) + emit в шину для MT5
import os, time, asyncio, requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import numpy as np, pandas as pd
from dotenv import load_dotenv
from telegram import Bot

from bus import emit  # emit(symbol, 'buy'|'sell', price=None, meta=None)

# ── ENV
load_dotenv(override=True)
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "").strip()
MEXC_SYMBOL   = os.getenv("MEXC_SYMBOL", "EURUSDT").strip()
MEXC_INTERVAL = os.getenv("MEXC_INTERVAL", "10m").strip()
EMIT_SYMBOL   = os.getenv("EMIT_SYMBOL", "EURUSD").strip()  # что будет торговать MT5

LEN_FX     = int(os.getenv("SAVW_LENGTH", "67"))
POLL_DELAY = int(os.getenv("POLL_DELAY", "60"))
TZ_NAME    = os.getenv("TZ", "Europe/Belgrade").strip()
try:
    TZ_LOCAL = ZoneInfo(TZ_NAME)
except Exception:
    TZ_LOCAL = ZoneInfo("Europe/Belgrade")

# ── Telegram
async def _send_async(text: str):
    if not TG_TOKEN or not TG_CHAT:
        print("⚠️ TELEGRAM_TOKEN/CHAT_ID не заданы. Сообщение:", text)
        return
    async with Bot(TG_TOKEN) as bot:
        await bot.send_message(chat_id=TG_CHAT, text=text)

def send_msg(text: str):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass
    asyncio.run(_send_async(text))

# ── helpers
def drop_unclosed_last_bar(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    return df.iloc[:-1] if len(df) >= 1 else df

def vwap_trend_series(df: pd.DataFrame, length: int) -> pd.Series:
    hmax = df["h"].rolling(length, min_periods=1).max()
    lmin = df["l"].rolling(length, min_periods=1).min()
    trend_vals, last = [], None
    for hi, lo, hm, lm in zip(df["h"].values, df["l"].values, hmax.values, lmin.values):
        if np.isclose(hi, hm, rtol=0.0, atol=1e-10):
            last = True
        elif np.isclose(lo, lm, rtol=0.0, atol=1e-10):
            last = False
        trend_vals.append(last)
    return pd.Series(trend_vals, index=df.index, dtype="boolean")

def fmt_time_local(ts_utc: datetime | pd.Timestamp) -> str:
    if isinstance(ts_utc, pd.Timestamp):
        dt = ts_utc.to_pydatetime().replace(tzinfo=timezone.utc)
    else:
        dt = ts_utc.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ_LOCAL).strftime("%Y-%m-%d %H:%M:%S %Z")

def to_py_bool(x):
    import math, pandas as _pd, numpy as _np
    if x is None or x is _pd.NA or (isinstance(x, float) and math.isnan(x)):
        return None
    try:
        return bool(x)
    except Exception:
        return None

# ── MEXC
def load_mexc(symbol: str, interval: str, limit: int = 1000) -> pd.DataFrame:
    url = "https://api.mexc.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    k = r.json()
    if not isinstance(k, list) or not k:
        raise RuntimeError("MEXC пусто")
    row_len = len(k[0])
    if row_len >= 12: cols = ["t","o","h","l","c","v","t2","q","n","tb","tq","ig"]
    elif row_len == 8: cols = ["t","o","h","l","c","v","t2","q"]
    else: cols = list(range(row_len))
    df = pd.DataFrame(k, columns=cols).rename(columns={0:"t",1:"o",2:"h",3:"l",4:"c"})
    for need in ["t","o","h","l","c"]:
        if need not in df.columns: raise RuntimeError(f"Unexpected MEXC format: '{need}' missing")
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df[["o","h","l","c"]] = df[["o","h","l","c"]].astype(float)
    df = df[["t","o","h","l","c"]].set_index("t").sort_index()
    df = drop_unclosed_last_bar(df)
    df["complete"] = True
    return df

# ── Task
class Task:
    def __init__(self, label: str, length: int, poll_delay: int, mexc_symbol: str, mexc_interval: str):
        self.label = label
        self.length = length
        self.poll_delay = poll_delay
        self.mexc_symbol = mexc_symbol
        self.mexc_interval = mexc_interval
        self.last_bar_time = None
        self.last_state: bool | None = None
        self.last_price: float | None = None
        self.last_tick_ts = 0.0

    def _msg_signal(self, lt: datetime | pd.Timestamp, price: float | None, up: bool):
        when_local = fmt_time_local(lt)
        price_str = f"{price:.6f}" if isinstance(price, (int, float)) else "—"
        if up:
            text = f"🟢 Тренд ВВЕРХ (highest({self.length}))\n{self.label} @ {price_str}\n{when_local}"
        else:
            text = f"🔴 Тренд ВНИЗ  (lowest({self.length}))\n{self.label} @ {price_str}\n{when_local}"
        print(text)
        send_msg(text)

    def _emit_trade(self, side: str, price: float | None):
        ev = emit(EMIT_SYMBOL, side, price=price, meta={"src": "mexc_savw", "mexc_symbol": self.mexc_symbol})
        print(f"EMIT ▶ {EMIT_SYMBOL} → {side} @ {price} | {ev}")

    def load_df(self) -> pd.DataFrame:
        df = load_mexc(self.mexc_symbol, self.mexc_interval)
        print(f"[MEXC] {self.label}: ОК ({len(df)} баров)")
        return df

    def tick(self):
        now = time.time()
        if now - self.last_tick_ts < self.poll_delay:
            return
        self.last_tick_ts = now
        try:
            df = self.load_df()
            if df.empty:
                return
            lt = df.index[-1]
            if self.last_bar_time is not None and lt <= self.last_bar_time:
                return
            price = float(df["c"].iloc[-1])
            self.last_price = price
            tr = vwap_trend_series(df, self.length)
            curr = to_py_bool(tr.iloc[-1])
            prev = to_py_bool(tr.iloc[-2] if len(tr) >= 2 else pd.NA)

            if self.last_state is None:
                self.last_state = curr
                self.last_bar_time = lt
                print(f"[{self.label}] init state = {curr}, bar {lt}")
                return

            signal_up = (curr is True) and (prev is not True)
            signal_dn = (curr is False) and (prev is not False)
            if signal_up:
                self._msg_signal(lt, price, True)
                self._emit_trade("buy", price)
            elif signal_dn:
                self._msg_signal(lt, price, False)
                self._emit_trade("sell", price)

            self.last_state = curr
            self.last_bar_time = lt

        except Exception as e:
            print(f"[{self.label}] Ошибка: {e}")

def main():
    task = Task(
        label=f"{MEXC_SYMBOL} ({MEXC_INTERVAL})",
        length=LEN_FX,
        poll_delay=POLL_DELAY,
        mexc_symbol=MEXC_SYMBOL,
        mexc_interval=MEXC_INTERVAL,
    )
    print(f"Бот запущен. Источник: MEXC spot klines. TZ: {TZ_NAME}")
    send_msg(f"🚀 Бот запущен. Источник: MEXC\nЗадача: {MEXC_SYMBOL} ({MEXC_INTERVAL})\nTZ: {TZ_NAME}")
    while True:
        task.tick()
        time.sleep(1)

if __name__ == "__main__":
    main()
