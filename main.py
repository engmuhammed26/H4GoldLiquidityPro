
import os
import sys
import logging
import requests
import pandas as pd
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# H4GoldLiquidityPro
# XAUUSD H4 liquidity sweep + M15 confirmation
# Analysis alerts only. No automatic trade execution.

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TZ = ZoneInfo("Africa/Kampala")
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/XAUUSD=X"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)


def get_candles(interval, period):
    response = requests.get(
        YAHOO_URL,
        params={"interval": interval, "range": period},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=25
    )
    response.raise_for_status()

    result = response.json()["chart"]["result"]
    if not result:
        raise ValueError("No XAUUSD market data available")

    item = result[0]
    quote = item["indicators"]["quote"][0]

    df = pd.DataFrame({
        "time": pd.to_datetime(
            item["timestamp"], unit="s", utc=True
        ),
        "open": quote["open"],
        "high": quote["high"],
        "low": quote["low"],
        "close": quote["close"]
    })

    return df.dropna().set_index("time").sort_index()


def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secret"
        )

    response = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": message
        },
        timeout=25
    )
    response.raise_for_status()

    data = response.json()
    if not data.get("ok"):
        raise RuntimeError("Telegram rejected the message")


def analyse_gold():
    now = pd.Timestamp.now(tz="UTC")

    m15 = get_candles("15m", "5d")
    h1 = get_candles("60m", "60d")

    # Build four-hour candles from hourly data.
    h4 = h1.resample("4h").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last"
    }).dropna()

    # Keep only fully completed candles.
    h4 = h4[h4.index + pd.Timedelta(hours=4) <= now]
    m15 = m15[m15.index + pd.Timedelta(minutes=15) <= now]

    if len(h4) < 3 or len(m15) < 6:
        raise ValueError("Insufficient completed candles")

    previous = h4.iloc[-2]
    sweep = h4.iloc[-1]

    direction = None
    reason = ""

    # Bullish H4 liquidity sweep.
    if (
        sweep["low"] < previous["low"]
        and sweep["close"] > previous["low"]
    ):
        direction = "LONG"
        reason = "H4 swept previous lows and closed back above them."

    # Bearish H4 liquidity sweep.
    elif (
        sweep["high"] > previous["high"]
        and sweep["close"] < previous["high"]
    ):
        direction = "SHORT"
        reason = "H4 swept previous highs and closed back below them."

    if direction is None:
        return None

    # M15 structure confirmation.
    recent = m15.iloc[-5:-1]
    confirmation = m15.iloc[-1]

    if direction == "LONG":
        confirmed = (
            confirmation["close"] > recent["high"].max()
        )
        stop = float(sweep["low"])
    else:
        confirmed = (
            confirmation["close"] < recent["low"].min()
        )
        stop = float(sweep["high"])

    if not confirmed:
        return None

    entry = float(confirmation["close"])
    risk = abs(entry - stop)

    if risk <= 0:
        return None

    target = (
        entry + 2 * risk
        if direction == "LONG"
        else entry - 2 * risk
    )

    # Uganda local time. Session boundaries are approximate
    # because London and New York observe daylight saving time.
    local_time = datetime.now(TZ)
    hour = local_time.hour

    sessions = []
    if 10 <= hour < 20:
        sessions.append("London")
    if hour >= 15 or hour < 1:
        sessions.append("New York")

    if not sessions:
        return None

    # Only alert shortly after a new M15 candle closes.
    candle_close = confirmation.name + pd.Timedelta(minutes=15)
    age_minutes = (now - candle_close).total_seconds() / 60

    if age_minutes < 0 or age_minutes > 5:
        return None

    return {
        "direction": direction,
        "reason": reason,
        "session": " + ".join(sessions),
        "entry": entry,
        "stop": stop,
        "target": target,
        "candle_time": str(confirmation.name),
        "local_time": local_time.strftime("%Y-%m-%d %H:%M EAT")
    }


def main():
    if not TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
            "to GitHub Actions secrets."
        )

    logging.info("Starting H4GoldLiquidityPro analysis")

    signal = analyse_gold()

    if signal is None:
        logging.info("No confirmed trading setup found.")
        return

    message = (
        "H4 GOLD LIQUIDITY ANALYSIS\n\n"
        f"Direction: {signal['direction']}\n"
        "Market: XAUUSD\n"
        f"Session: {signal['session']}\n\n"
        f"Entry reference: {signal['entry']:.2f}\n"
        f"Invalidation: {signal['stop']:.2f}\n"
        f"2R target reference: {signal['target']:.2f}\n\n"
        f"Reason: {signal['reason']}\n"
        f"M15 candle: {signal['candle_time']}\n"
        f"Uganda time: {signal['local_time']}\n\n"
        "Confirm the setup, broker price and spread manually. "
        "These are analysis references, not guaranteed prices. "
        "No automatic orders are placed."
    )

    send_telegram(message)
    logging.info("Telegram alert sent successfully.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Analysis failed")
        sys.exit(1)
