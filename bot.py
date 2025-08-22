# -*- coding: utf-8 -*-
# Donchian Bias Breakout → Telegram + emit в шину для MT5 (bus.emit)
# Забирает свечи с MEXC, считает EMA/ATR/ADX/Дончиан, выдаёт сигнал на ЗАКРЫТИИ бара.

import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv
from telegram import Bot

# внутренняя шина событий; должна быть реализована в bus.py:
#   emit(symbol, 'buy'|'sell', price=None, meta=None)
from bus import emit  # type: ignore

# ─────────── ENV ───────────
load_dotenv(override=True)

TG_TOKEN      = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT       = os.getenv("TELEGRAM_CHAT_ID", "").strip()
MEXC_SYMBOL   = os.getenv("MEXC_SYMBOL", "EURUSDT").strip()
MEXC_INTERVAL = os.getenv("MEXC_INTERVAL", "5m").strip()
POLL_DELAY    = float(os.getenv("POLL_DELAY", "60"))  # сек
TZ_NAME       = os.getenv("TZ", "Europe/Moscow")
EMIT_SYMBOL   = os.getenv("EMIT_SYMBOL", "EURUSD").strip()  # тикер для MT5

# Параметры стратегии (можно задавать в Environment)
CHLEN       = int(os.getenv("CHLEN", "40"))
ADX_LEN     = int(os.getenv("ADX_LEN", "14"))
ADX_MIN     = float(os.getenv("ADX_MIN", "24"))
ATR_LEN     = int(os.getenv("ATR_LEN", "14"))
ATR_MIN_PC  = float(os.getenv("ATR_MIN_PC", "0.018"))  # 1.8% от цены
BUF_ATR     = float(os.getenv("BUF_ATR", "0.20"))      # буфер пробоя в ATR
DIST_SLOW   = float(os.getenv("DIST_SLOW", "0.6"))     # мин. дистанция от EMA200 в ATR

# MM (информационно — исполняется в tv2mt5, тут только в meta)
TP1_R       = float(os.getenv("TP1_R", "1.0"))
TP1_SHARE   = float(os.getenv("TP1_SHARE", "0.40"))
TP_R        = float(os.getenv("TP_R", "2.6"))
SL_ATR      = float(os.getenv("SL_ATR", "1.2"))
BE_R        = float(os.getenv("BE_R", "0.8"))

# Telegram
bot = Bot(TG_TOKEN) if TG_TOKEN and TG_CHAT else None
TZ  = ZoneInfo(TZ_NAME)

# Заголовки для обхода 403 на MEXC
HEADERS = {
    "User-Agent": "mexc-telegram-bot/1.0 (+https://render.com)",
    "Accept": "application/json",
}

# ─────────── Утилиты ───────────

def send_tg(text: str):
    if not bot:
        return
    try:
        bot.send_message(chat_id=TG_CHAT, text=text, disable_web_page_preview=True)
    except Exception as e:
        print("TG send error:", e)


def mexc_klines(symbol: str, interval: str, limit: int = 600) -> pd.DataFrame:
    """
    Пробуем публичный v3; если 403/ошибка — фолбэк на open/api v2.
    Возвращает DF: open_time(ms), open, high, low, close, volume, close_time
    """
    cols = ["open_time", "open", "high", "low", "close", "volume", "close_time"]

    # 1) Основной v3
    url_v3 = "https://api.mexc.com/api/v3/klines"
    try:
        r = requests.get(
            url_v3,
            params={"symbol": symbol, "interval": interval, "limit": limit},
            headers=HEADERS,
            timeout=15,
        )
        if r.status_code == 403:
            raise requests.HTTPError(f"403 Forbidden for {url_v3}")
        r.raise_for_status()
        arr = r.json()
        data = [[a[0], float(a[1]), float(a[2]), float(a[3]), float(a[4]), float(a[5]), a[6]] for a in arr]
        return pd.DataFrame(data, columns=cols)
    except Exception as e:
        print("v3 klines failed → fallback v2:", e)

    # 2) Фолбэк v2
    url_v2 = "https://www.mexc.com/open/api/v2/market/kline"
    sym_v2 = symbol if "_" in symbol else symbol.replace("USDT", "_USDT")
    i_map = {
        "1m": "Min1", "3m": "Min3", "5m": "Min5", "15m": "Min15", "30m": "Min30",
        "1h": "Hour1", "2h": "Hour2", "4h": "Hour4", "6h": "Hour6", "8h": "Hour8", "12h": "Hour12",
        "1d": "Day1", "1w": "Week1", "1M": "Month1"
    }
    i_v2 = i_map.get(interval.lower(), "Min5")
    r2 = requests.get(
        url_v2,
        params={"symbol": sym_v2, "interval": i_v2, "limit": limit},
        headers=HEADERS,
        timeout=15,
    )
    r2.raise_for_status()
    js = r2.json()
    arr = js.get("data", [])
    data = [[int(a["t"]) * 1000, float(a["o"]), float(a["h"]), float(a["l"]), float(a["c"]), float(a["v"]), None] for a in arr]
    return pd.DataFrame(data, columns=cols)


def interval_to_htf(interval: str) -> str:
    """
    Авто-HTF:
      M1..M15 → 4h;  M20/M30/1h → 1d;  H2..H12/D1 → 1w; иначе → 1M
    """
    i = interval.lower()
    if i in ("1m", "3m", "5m", "15m"):
        return "4h"
    if i in ("20m", "30m", "1h"):
        return "1d"
    if i in ("2h", "4h", "6h", "8h", "12h", "1d"):
        return "1w"
    return "1M"


def rma(s: pd.Series, length: int) -> pd.Series:
    return s.ewm(alpha=1.0 / float(length), adjust=False).mean()


def ema(s: pd.Series, length: int) -> pd.Series:
    return s.ewm(span=length, adjust=False).mean()


def atr_df(df: pd.DataFrame, length: int) -> pd.Series:
    c, h, l = df["close"], df["high"], df["low"]
    prev = c.shift(1)
    tr = pd.concat([(h - l), (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    return rma(tr, length)


def adx_df(df: pd.DataFrame, length: int) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    up = h.diff()
    dn = -l.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([(h - l), (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    tr_rma = rma(tr, length)
    pdi = 100.0 * rma(pd.Series(plus_dm, index=df.index), length) / tr_rma.replace(0, np.nan)
    mdi = 100.0 * rma(pd.Series(minus_dm, index=df.index), length) / tr_rma.replace(0, np.nan)
    dx = 100.0 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return rma(dx.fillna(0.0), length)


def make_signal() -> tuple[str | None, dict]:
    """
    ('buy'|'sell'|None, meta)
    Логика = Pine: HTF-bias + Donchian breakout с буфером + ADX/ATR + тренд LTF.
    """
    # LTF
    d = mexc_klines(MEXC_SYMBOL, MEXC_INTERVAL, limit=600)
    # HTF
    htf = interval_to_htf(MEXC_INTERVAL)
    D = mexc_klines(MEXC_SYMBOL, htf, limit=400)

    # Индикаторы LTF
    d["ema50"] = ema(d["close"], 50)
    d["ema200"] = ema(d["close"], 200)
    d["atr"] = atr_df(d, ATR_LEN)
    d["adx"] = adx_df(d, ADX_LEN)

    # HTF тренд
    D["ema200"] = ema(D["close"], 200)
    htf_up = (D["close"].iloc[-1] > D["ema200"].iloc[-1]) and (D["ema200"].iloc[-1] > D["ema200"].iloc[-2])
    htf_dn = (D["close"].iloc[-1] < D["ema200"].iloc[-1]) and (D["ema200"].iloc[-1] < D["ema200"].iloc[-2])

    # Дончиан prev
    don_hi_prev = d["high"].rolling(CHLEN).max().shift(1).iloc[-1]
    don_lo_prev = d["low"].rolling(CHLEN).min().shift(1).iloc[-1]

    # Фильтры
    c = d["close"].iloc[-1]
    ema50 = d["ema50"].iloc[-1]
    ema200 = d["ema200"].iloc[-1]
    atr = d["atr"].iloc[-1]
    adx = d["adx"].iloc[-1]

    trend_up = (c > ema50) and (ema50 > ema200)
    trend_dn = (c < ema50) and (ema50 < ema200)
    atr_ok = (atr / c * 100.0) >= (ATR_MIN_PC * 100.0)
    adx_ok = adx >= ADX_MIN
    far_slow = abs(c - ema200) >= (DIST_SLOW * atr)

    long_break = c > (don_hi_prev + BUF_ATR * atr)
    short_break = c < (don_lo_prev - BUF_ATR * atr)

    go_long = htf_up and trend_up and atr_ok and adx_ok and far_slow and long_break
    go_short = htf_dn and trend_dn and atr_ok and adx_ok and far_slow and short_break

    meta = {
        "symbol": MEXC_SYMBOL,
        "interval": MEXC_INTERVAL,
        "htf": htf,
        "price": float(c),
        "don_hi_prev": float(don_hi_prev),
        "don_lo_prev": float(don_lo_prev),
        "ema50": float(ema50),
        "ema200": float(ema200),
        "atr": float(atr),
        "adx": float(adx),
        "htf_up": htf_up,
        "htf_dn": htf_dn,
        "trend_up": trend_up,
        "trend_dn": trend_dn,
        "atr_ok": atr_ok,
        "adx_ok": adx_ok,
        "far_slow": far_slow,
        "long_break": long_break,
        "short_break": short_break,
        "mm": {"SL_ATR": SL_ATR, "TP1_R": TP1_R, "TP1_SHARE": TP1_SHARE, "TP_R": TP_R, "BE_R": BE_R},
    }

    if go_long and not go_short:
        return "buy", meta
    if go_short and not go_long:
        return "sell", meta
    return None, meta


def fmt_dt(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S")


# ─────────── Main loop ───────────
def main():
    print(f"Start Donchian bot | {MEXC_SYMBOL} {MEXC_INTERVAL} | emit→ {EMIT_SYMBOL}")
    last_bar_open = None
    last_side_sent = None

    while True:
        try:
            # Берём 2 последних бара, чтобы отследить смену open_time (закрытие предыдущего)
            df_last = mexc_klines(MEXC_SYMBOL, MEXC_INTERVAL, limit=2)
            cur_open = int(df_last["open_time"].iloc[-1])

            if last_bar_open is None:
                last_bar_open = cur_open

            if cur_open != last_bar_open:
                # Закрыт бар → считаем сигнал
                side, meta = make_signal()
                last_bar_open = cur_open

                if side and side != last_side_sent:
                    price = float(meta["price"])
                    when = fmt_dt(cur_open)
                    msg = (
                        f"#{EMIT_SYMBOL} {side.upper()} | {when}\n"
                        f"price={price:.5f} | adx={meta['adx']:.1f} atr%={(meta['atr']/price*100):.2f}%\n"
                        f"HTF={meta['htf']} trend: up={meta['htf_up']} dn={meta['htf_dn']}\n"
                        f"Donchian prev: H={meta['don_hi_prev']:.5f} L={meta['don_lo_prev']:.5f}"
                    )
                    print(msg)
                    send_tg(msg)

                    # событие в очередь для /feed
                    emit(EMIT_SYMBOL, side, price=price, meta=meta)
                    last_side_sent = side
                else:
                    print(f"No signal | {fmt_dt(cur_open)}")

        except Exception as e:
            print("loop error:", e)

        time.sleep(POLL_DELAY)


if __name__ == "__main__":
    main()
