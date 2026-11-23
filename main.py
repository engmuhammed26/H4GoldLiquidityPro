import os
import time
import logging
import requests
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo

# H4GoldLiquidityPro
# Gold liquidity analysis Telegram bot
# Analysis and alerts only - no automatic trade execution

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_IDS = set()
TZ = ZoneInfo("Africa/Kampala")

TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else None
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

last_update_id = 0
last_signal = None
last_scan = 0


def get_candles(symbol, interval, period):
    url = YAHOO_URL.format(symbol)
    response = requests.get(
        url,
        params={"interval": interval, "range": period},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20
    )
    response.raise_for_status()

    result = response.json()["chart"]["result"]
    if not result:
        raise ValueError("No market data returned")

    item = result[0]
    timestamps = item["timestamp"]
    quotes = item["indicators"]["quote"][0]

    df = pd.DataFrame({
        "time": pd.to_datetime(timestamps, unit="s", utc=True),
        "open": quotes["open"],
        "high": quotes["high"],
        "low": quotes["low"],
        "close": quotes["close"]
    })

    df = df.dropna().set_index("time")
    return df


def analyse_gold():
    # 15-minute data and hourly data for H4 analysis
    m15 = get_candles("XAUUSD=X", "15m", "5d")
    h1 = get_candles("XAUUSD=X", "60m", "60d")

    # Build four-hour candles from hourly data
    h4 = h1.resample("4h").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last"
    }).dropna()

    if len(h4) < 5 or len(m15) < 6:
        return None

    # Use completed candles only
    h4 = h4.iloc[:-1]
    m15 = m15.iloc[:-1]

    previous = h4.iloc[-2]
    sweep = h4.iloc[-1]

    direction = None
    reason = ""

    # H4 bullish liquidity sweep
    if (sweep["low"] < previous["low"]
            and sweep["close"] > previous["low"]):
        direction = "LONG"
        reason = "H4 swept previous lows and closed back above them"

    # H4 bearish liquidity sweep
    elif (sweep["high"] > previous["high"]
            and sweep["close"] < previous["high"]):
        direction = "SHORT"
        reason = "H4 swept previous highs and closed back below them"

    if direction is None:
        return None

    # M15 confirmation
    recent = m15.iloc[-4:-1]
    confirmation = m15.iloc[-1]

    if direction == "LONG":
        confirmed = confirmation["close"] > recent["high"].max()
    else:
        confirmed = confirmation["close"] < recent["low"].min()

    if not confirmed:
        return None

    current_time = datetime.now(TZ)

    # London and New York session filter (Uganda local time)
    hour = current_time.hour
    london = 10 <= hour < 19
    new_york = 15 <= hour < 23

    if not (london or new_york):
        return None

    session = []
    if london:
        session.append("London")
    if new_york:
        session.append("New York")

    entry = float(confirmation["close"])
    stop = (
        float(sweep["low"]) if direction == "LONG"
        else float(sweep["high"])
    )

    risk = abs(entry - stop)
    if risk <= 0:
        return None

    target = (
        entry + 2 * risk if direction == "LONG"
        else entry - 2 * risk
    )

    signal_key = (
        direction,
        str(sweep.name),
        str(confirmation.name)
    )

    return {
        "key": signal_key,
        "direction": direction,
        "reason": reason,
        "session": " + ".join(session),
        "entry": entry,
        "stop": stop,
        "target": target,
        "time": current_time.strftime("%Y-%m-%d %H:%M EAT")
    }


def send_message(chat_id, message):
    if not TELEGRAM_URL:
        return

    try:
        response = requests.post(
            TELEGRAM_URL + "/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=20
        )
        response.raise_for_status()
    except Exception as error:
        logging.error("Telegram sending error: %s", error)


def broadcast(message):
    for chat_id in list(CHAT_IDS):
        send_message(chat_id, message)


def handle_updates():
    global last_update_id

    if not TELEGRAM_URL:
        return

    try:
        response = requests.get(
            TELEGRAM_URL + "/getUpdates",
            params={
                "offset": last_update_id + 1,
                "timeout": 10
            },
            timeout=20
        )
        response.raise_for_status()

        for update in response.json().get("result", []):
            last_update_id = update["update_id"]

            message = update.get("message", {})
            chat = message.get("chat", {})
            chat_id = chat.get("id")
            command = message.get("text", "").strip().lower()

            if not chat_id:
                continue

            if command in ["/start", "/subscribe"]:
                CHAT_IDS.add(chat_id)
                send_message(
                    chat_id,
                    "Welcome to H4GoldLiquidityPro!\n\n"
                    "Gold market monitoring is active.\n"
                    "Commands:\n/start - Subscribe\n"
                    "/status - Check bot status"
                )

            elif command == "/status":
                send_message(
                    chat_id,
                    "H4GoldLiquidityPro is running.\n"
                    "Market: XAUUSD\n"
                    "Analysis: H4 liquidity sweep + M15 confirmation\n"
                    "Sessions: London and New York"
                )

    except Exception as error:
        logging.error("Telegram update error: %s", error)


def main():
    global last_signal, last_scan

    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    logging.info("H4GoldLiquidityPro started")

    while True:
        handle_updates()

        if time.time() - last_scan >= 300:
            last_scan = time.time()

            try:
                result = analyse_gold()

                if result and result["key"] != last_signal:
                    last_signal = result["key"]

                    message = (
                        "H4 GOLD LIQUIDITY SIGNAL\n\n"
                        f"Direction: {result['direction']}\n"
                        f"Market: XAUUSD\n"
                        f"Session: {result['session']}\n\n"
                        f"Entry reference: {result['entry']:.2f}\n"
                        f"Invalidation reference: {result['stop']:.2f}\n"
                        f"2R target reference: {result['target']:.2f}\n\n"
                        f"Reason: {result['reason']}\n"
                        f"Time: {result['time']}\n\n"
                        "Confirm spread and execution prices with your broker. "
                        "This is an analysis alert, not a guaranteed trade."
                    )

                    broadcast(message)

            except Exception as error:
                logging.error("Market analysis error: %s", error)

        time.sleep(2)


if __name__ == "__main__":
    main()
  
