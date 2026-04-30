import os
import json
import pytz
import requests
import pandas as pd
import pandas_ta as ta
from datetime import datetime, timedelta

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
DHAN_ACCESS_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN", "")
DHAN_CLIENT_ID    = os.environ.get("DHAN_CLIENT_ID", "")
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")

MARKET_FEED_URL   = "https://api.dhan.co/v2/marketfeed/ltp"
OPTION_CHAIN_URL  = "https://api.dhan.co/v2/optionchain"
INTRADAY_URL      = "https://api.dhan.co/v2/charts/intraday"

STATE_FILE        = "crude_state.json"
STRIKE_STEP       = 100          # Standard for Crude Oil[cite: 2]
ATM_RANGE         = 5
INTERVAL          = "3"
IST               = pytz.timezone("Asia/Kolkata")

def _headers():
    return {"access-token": DHAN_ACCESS_TOKEN, "client-id": DHAN_CLIENT_ID, "Content-Type": "application/json"}

def send_telegram(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"})
    except Exception as e: print(f"Telegram error: {e}")

def get_crude_spot():
    """Fetch Crude Future Price (ATM Reference)[cite: 2]"""
    payload = {"MCX": ["1"]} # '1' is typically the active Crude Future ID[cite: 2]
    try:
        r = requests.post(MARKET_FEED_URL, json=payload, headers=_headers(), timeout=10)
        return float(r.json().get("data", {}).get("MCX", {}).get("1", {}).get("last_price", 0))
    except: return None

def main():
    # 1. Market Hours Check[cite: 1, 2]
    now = datetime.now(IST)
    if now.weekday() >= 5: return
    
    # 2. Get Spot and ATM[cite: 2]
    spot = get_crude_spot()
    if not spot: return
    atm = round(spot / STRIKE_STEP) * STRIKE_STEP
    
    # 3. Load State
    state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
    
    print(f"Crude Oil Scan | Spot: {spot} | ATM: {atm}")
    # (Rest of indicator logic goes here following your Sensex template)

if __name__ == "__main__":
    main()
