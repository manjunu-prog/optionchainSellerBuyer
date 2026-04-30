import os
import json
import pytz
import requests
import pandas as pd
from datetime import datetime

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
DHAN_ACCESS_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN", "")
DHAN_CLIENT_ID    = os.environ.get("DHAN_CLIENT_ID", "")
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")

MARKET_FEED_URL   = "https://api.dhan.co/v2/marketfeed/ltp"
INTRADAY_URL      = "https://api.dhan.co/v2/charts/intraday"

STATE_FILE        = "crude_state.json"
STRIKE_STEP       = 100
ATM_RANGE         = 5
INTERVAL          = "3"
IST               = pytz.timezone("Asia/Kolkata")

# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────
def _headers():
    return {
        "access-token": DHAN_ACCESS_TOKEN,
        "client-id": DHAN_CLIENT_ID,
        "Content-Type": "application/json"
    }

def send_telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        )
    except Exception as e:
        print(f"Telegram error: {e}")

# ──────────────────────────────────────────────
# INDICATORS (REPLACEMENT FOR pandas_ta)
# ──────────────────────────────────────────────
def calculate_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

# ──────────────────────────────────────────────
# DATA FUNCTIONS
# ──────────────────────────────────────────────
def get_crude_spot():
    payload = {"MCX": ["1"]}
    try:
        r = requests.post(MARKET_FEED_URL, json=payload, headers=_headers(), timeout=10)
        return float(r.json().get("data", {}).get("MCX", {}).get("1", {}).get("last_price", 0))
    except:
        return None

def get_intraday_data(security_id):
    payload = {
        "securityId": security_id,
        "exchangeSegment": "MCX",
        "instrument": "OPT",
        "interval": INTERVAL
    }

    try:
        r = requests.post(INTRADAY_URL, json=payload, headers=_headers(), timeout=10)
        data = r.json().get("data", [])

        df = pd.DataFrame(data)
        if df.empty:
            return None

        df["close"] = df["close"].astype(float)

        # Indicators
        df["ema20"] = calculate_ema(df["close"], 20)
        df["rsi"] = calculate_rsi(df["close"], 14)

        return df
    except Exception as e:
        print("Data error:", e)
        return None

# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    now = datetime.now(IST)

    # Skip weekends
    if now.weekday() >= 5:
        return

    spot = get_crude_spot()
    if not spot:
        return

    atm = round(spot / STRIKE_STEP) * STRIKE_STEP

    print(f"Crude Scan | Spot: {spot} | ATM: {atm}")

    # Example usage (you can plug your logic here)
    # df = get_intraday_data("SOME_OPTION_ID")
    # if df is not None:
    #     print(df.tail())

if __name__ == "__main__":
    main()
