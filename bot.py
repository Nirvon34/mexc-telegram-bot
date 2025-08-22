# -*- coding: utf-8 -*-
# Regime Switcher (Donchian Trend Breakout + Range Reversion) → Telegram + emit в MT5
# Основано на вашем рабочем шаблоне SAVW: та же структура, та же отправка в TG и emit()

import os
import time
import asyncio
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
from dotenv import load_dotenv
from telegram import Bot

from bus import emit  # emit(symbol, 'buy'|'sell', price=None, meta=None)

# ── ENV
load_dotenv(override=True)
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

MEXC_SYMBOL = os.getenv("MEXC_SYMBOL", "EURUSDT").strip()
MEXC_INTERVAL = os.getenv("MEXC_INTERVAL", "5m").strip()
EMIT_SYMBOL = os.getenv("EMIT_SYMBOL", "EURUSD").strip()  # что будет торговать MT5

# Параметры нашей стратегии (дефолты — под M5)
CHLEN = int(os.getenv("CHLEN", "40"))          # период Дончиана
ADX_LEN = int(os.getenv("ADX_LEN", "14"))
ADX_MIN = float(os.getenv("ADX_MIN", "24"))
ATR_LEN = int(os.getenv("ATR_LEN", "14"))
ATR_MIN_PC = float(os.getenv("ATR_MIN_PC", "0.018"))   # 1.8% от цены
BUF_ATR = float(os.getenv("BUF_ATR", "0.20"))          # буфер пробоя в ATR
DIST_SLOW = float(os.getenv("DIST_SLOW", "0.6"))       # мин. дистанция от EMA200 в ATR

# Опционный блок mean-reversion (если тренда нет)
USE_MR = os.getenv("USE_MR", "1").strip() != "0"
DEV_ATR = float(os.getenv("DEV_ATR", "1.2"))           # отклонение от EMA50 в ATR
RSI_LEN = int(os.getenv("RSI_LEN", "14"))
RSI_LOW = float(os.getenv("RSI_LOW", "35"))
RSI_HIGH = float(os.getenv("RSI_HIGH", "65"))

# Сессия/лимиты (формат: HHMM-HHMM, локальная TZ)
SESSION = os.getenv("SESSION", "0700-1800").strip()
COOLDOWN_BARS = int(os.getenv("COOLDOWN_BARS", "40"))
MAX_TRADES_DAY = int(os.getenv("MAX_TRADES_DAY", "2"))

POLL_DELAY = int(os.getenv("POLL_DELAY", "60"))
TZ_NAME = os.getenv("TZ", "Europe/Belgrade").strip()
try:
    TZ_LOCAL = ZoneInfo(TZ_NAME)
except Exception:
    TZ_LOCAL = ZoneInfo("Europe/Belgrade")

# Нейтральные заголовки, чтобы реже ловить 403 на MEXC
HEADERS = {
    "User-Agent": "mexc-telegram-bot/1.0",
    "Accept": "application/json",
}

# ── Telegram
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

# ── helpers
def drop_unclosed_last_bar(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    return df.iloc[:-1] if len(df) >= 1 else df

def fmt_time_local(ts_utc: datetime | pd.Timestamp) -> str:
    if isinstance(ts_utc, pd.Timestamp):
        dt = ts_utc.to_pydatetime().replace(tzinfo=timezone.utc)
    else:
        dt = ts_utc.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ_LOCAL).strftime("%Y-%m-%d %H:%M:%S %Z")

def in_session(now: datetime) -> bool:
    """Проверка по локальному времени."""
    try:
        s, e = SESSION.split("-")
        sh, sm = int(s[:2]), int(s[2:])
        eh, em = int(e[:2]), int(e[2:])
        local = now.astimezone(TZ_LOCAL)
        t = local.hour * 60 + local.minute
        start = sh * 60 + sm
        end = eh * 60 + em
        return start <= t <= end
    except Exception:
        return True

def rma(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(alpha=1.0 / float(length), adjust=False).mean()

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
    tr = pd.concat([(h - l), (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    return rma(tr, length)

def adx_df(df: pd.DataFrame, length: int) -> pd.Series:
    h, l, c = df["h"], df["l"], df["c"]
    up = h.diff()
    dn = -l.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([(h - l), (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    tr_rma = rma(tr, length)
    pdi = 100.0 * rma(pd.Series(plus_dm, index=df.index), length) / tr_rma.replace(0.0, np.nan)
    mdi = 100.0 * rma(pd.Series(minus_dm, index=df.index), length) / tr_rma.replace(0.0, np.nan)
    dx = 100.0 * (pdi - mdi).abs() / (pdi + mdi).replace(0.0, np.nan)
    return rma(dx.fillna(0.0), length)

def interval_to_htf(interval: str) -> str:
    i = interval.lower()
    if i in ("1m", "3m", "5m", "15m"):
        return "4h"
    if i in ("20m", "30m", "1h"):
        return "1d"
    if i in ("2h", "4h", "6h", "8h", "12h", "1d"):
        return "1w"
    return "1M"

# ── MEXC
def load_mexc(symbol: str, interval: str, limit: int = 1000) -> pd.DataFrame:
    url = "https://api.mexc.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(url, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    k = r.json()
    if not isinstance(k, list) or not k:
        raise RuntimeError("MEXC пусто")
    row_len = len(k[0])
    if row_len >= 12:
        cols = ["t", "o", "h", "l", "c", "v", "t2", "q", "n", "tb", "tq", "ig"]
    elif row_len == 8:
        cols = ["t", "o", "h", "l", "c", "v", "t2", "q"]
    else:
        cols = list(range(row_len))
    df = pd.DataFrame(k, columns=cols).rename(columns={0: "t", 1: "o", 2: "h", 3: "l", 4: "c"})
    for need in ["t", "o", "h", "l", "c"]:
        if need not in df.columns:
            raise RuntimeError(f"Unexpected MEXC format: '{need}' missing")
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df[["o", "h", "l", "c"]] = df[["o", "h", "l", "c"]].astype(float)
    df = df[["t", "o", "h", "l", "c"]].set_index("t").sort_index()
    df = drop_unclosed_last_bar(df)
    df["complete"] = True
    return df

# ── Стратегия
def make_signal(df_ltf: pd.DataFrame, df_htf: pd.DataFrame) -> tuple[str | None, dict]:
    """
    Возвращает ('buy'|'sell'|None, meta) на закрытии текущего LTF-бара.
    Логика = HTF-bias + Donchian breakout (+ MR, если тренда нет).
    """
    # LTF индикаторы
    df = df_ltf.copy()
    df["ema50"] = ema(df["c"], 50)
    df["ema200"] = ema(df["c"], 200)
    df["atr"] = atr_df(df, ATR_LEN)
    df["adx"] = adx_df(df, ADX_LEN)
    df["rsi"] = rsi(df["c"], RSI_LEN)

    c = df["c"].iloc[-1]
    ema50 = df["ema50"].iloc[-1]
    ema200 = df["ema200"].iloc[-1]
    atr = df["atr"].iloc[-1]
    adx = df["adx"].iloc[-1]
    rsi_v = df["rsi"].iloc[-1]

    # Дончиан prev
    don_hi_prev = df["h"].rolling(CHLEN).max().shift(1).iloc[-1]
    don_lo_prev = df["l"].rolling(CHLEN).min().shift(1).iloc[-1]

    # HTF тренд
    D = df_htf.copy()
    D["ema200"] = ema(D["c"], 200)
    htf_up = (D["c"].iloc[-1] > D["ema200"].iloc[-1]) and (D["ema200"].iloc[-1] > D["ema200"].iloc[-2])
    htf_dn = (D["c"].iloc[-1] < D["ema200"].iloc[-1]) and (D["ema200"].iloc[-1] < D["ema200"].iloc[-2])

    # Фильтры
    trend_up = (c > ema50) and (ema50 > ema200)
    trend_dn = (c < ema50) and (ema50 < ema200)
    atr_ok = (atr / c * 100.0) >= (ATR_MIN_PC * 100.0)
    adx_ok = adx >= ADX_MIN
    far_slow = abs(c - ema200) >= (DIST_SLOW * atr)

    long_break = c > (don_hi_prev + BUF_ATR * atr)
    short_break = c < (don_lo_prev - BUF_ATR * atr)

    go_long = htf_up and trend_up and atr_ok and adx_ok and far_slow and long_break
    go_short = htf_dn and trend_dn and atr_ok and adx_ok and far_slow and short_break

    side = None
    regime = "trend"
    if go_long and not go_short:
        side = "buy"
    elif go_short and not go_long:
        side = "sell"
    elif USE_MR and not adx_ok:
        # Mean-reversion на слабом тренде
        regime = "range"
        dev = DEV_ATR * atr
        long_mr = (c < ema50 - dev) and (rsi_v < RSI_LOW)
        short_mr = (c > ema50 + dev) and (rsi_v > RSI_HIGH)
        if long_mr and not short_mr:
            side = "buy"
        elif short_mr and not long_mr:
            side = "sell"

    meta = {
        "symbol": MEXC_SYMBOL,
        "interval": MEXC_INTERVAL,
        "price": float(c),
        "ema50": float(ema50),
        "ema200": float(ema200),
        "atr": float(atr),
        "atr_pc": float(atr / c * 100.0) if c else None,
        "adx": float(adx),
        "rsi": float(rsi_v),
        "htf_up": bool(htf_up),
        "htf_dn": bool(htf_dn),
        "trend_up": bool(trend_up),
        "trend_dn": bool(trend_dn),
        "atr_ok": bool(atr_ok),
        "adx_ok": bool(adx_ok),
        "far_slow": bool(far_slow),
        "don_hi_prev": float(don_hi_prev),
        "don_lo_prev": float(don_lo_prev),
        "regime": regime,
    }
    return side, meta

# ── Task
class Task:
    def __init__(self, label: str, poll_delay: int, mexc_symbol: str, mexc_interval: str):
        self.label = label
        self.poll_delay = poll_delay
        self.mexc_symbol = mexc_symbol
        self.mexc_interval = mexc_interval

        self.last_bar_time = None
        self.last_tick_ts = 0.0
        self.last_signal_side: str | None = None

        self.last_trade_index: int | None = None
        self.trades_today = 0
        self.cur_day = None  # для дневного лимита

    def _emit_trade(self, side: str, price: float | None, meta: dict):
        ev = emit(EMIT_SYMBOL, side, price=price, meta={"src": "mexc_regime", **meta})
        print(f"EMIT ▶ {EMIT_SYMBOL} → {side} @ {price} | {ev}")

    def _ok_limits(self, df_len: int) -> bool:
        # дневной лимит
        now_local = datetime.now(tz=TZ_LOCAL)
        d = (now_local.year, now_local.month, now_local.day)
        if self.cur_day != d:
            self.cur_day = d
            self.trades_today = 0
        if self.trades_today >= MAX_TRADES_DAY:
            return False
        # кулдаун в барах
        if self.last_trade_index is None:
            return True
        return (df_len - self.last_trade_index) >= COOLDOWN_BARS

    def tick(self):
        now = time.time()
        if now - self.last_tick_ts < self.poll_delay:
            return
        self.last_tick_ts = now
        try:
            # LTF
            df = load_mexc(self.mexc_symbol, self.mexc_interval)
            if df.empty:
                return
            lt = df.index[-1]
            if self.last_bar_time is not None and lt <= self.last_bar_time:
                return

            # HTF
            htf = interval_to_htf(self.mexc_interval)
            df_htf = load_mexc(self.mexc_symbol, htf)

            side, meta = make_signal(df, df_htf)
            price = float(meta.get("price", float(df["c"].iloc[-1])))

            # ограничения
            if side and self._ok_limits(len(df)) and in_session(datetime.now(timezone.utc)):
                when_local = fmt_time_local(lt)
                msg = (
                    f"#{EMIT_SYMBOL} {side.upper()} | {when_local}\n"
                    f"price={price:.5f}  adx={meta['adx']:.1f}  atr%={meta['atr_pc']:.2f}%  regime={meta['regime']}\n"
                    f"HTF trend: up={meta['htf_up']} dn={meta['htf_dn']}  "
                    f"Donchian prev: H={meta['don_hi_prev']:.5f} L={meta['don_lo_prev']:.5f}"
                )
                print(msg)
                send_msg(msg)
                self._emit_trade(side, price, meta)
                self.last_signal_side = side
                self.last_trade_index = len(df)
                self.trades_today += 1
            else:
                print(f"[{self.label}] no signal | bar {lt}")

            self.last_bar_time = lt

        except Exception as e:
            print(f"[{self.label}] Ошибка: {e}")

def main():
    task = Task(
        label=f"{MEXC_SYMBOL} ({MEXC_INTERVAL})",
        poll_delay=POLL_DELAY,
        mexc_symbol=MEXC_SYMBOL,
        mexc_interval=MEXC_INTERVAL,
    )
    print(f"Бот запущен. Источник: MEXC spot klines. TZ: {TZ_NAME}")
    send_msg(f"🚀 Бот запущен. Источник: MEXC\nЗадача: {MEXC_SYMBOL} ({MEXC_INTERVAL})\nTZ: {TZ_NAME}")
    while True:
        task.tick()
        time.sleep(1)  # фикс: одинаковый уровень отступа, безопасная частота цикла

if __name__ == "__main__":
    main()
