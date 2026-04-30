"""
atm_strike_scanner.py
─────────────────────
Scans SENSEX ATM ±5 CE & PE strikes every 3 minutes.
BUY signal = Price > VWAP AND Price > EMA14 AND Price > EMA28

Uses Dhan API for live option chain + intraday candles.
Sends a consolidated Telegram message showing ALL strikes' status.

Secrets (GitHub Actions / env vars):
  DHAN_ACCESS_TOKEN
  DHAN_CLIENT_ID
  TELEGRAM_TOKEN
  TELEGRAM_CHAT_ID
"""

import os
import json
import pytz
import requests
from datetime import datetime, timedelta

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
DHAN_ACCESS_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN", "")
DHAN_CLIENT_ID    = os.environ.get("DHAN_CLIENT_ID", "")
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")

INTRADAY_URL      = "https://api.dhan.co/v2/charts/intraday"
OPTION_CHAIN_URL  = "https://api.dhan.co/v2/optionchain"
MARKET_FEED_URL   = "https://api.dhan.co/v2/marketfeed/ltp"

STATE_FILE        = "atm_state.json"
STRIKE_STEP       = 100          # SENSEX strikes are in multiples of 100
ATM_RANGE         = 5            # ±5 strikes
INTERVAL          = "3"          # 3-minute candles
EMA_FAST          = 14
EMA_SLOW          = 28
IST               = pytz.timezone("Asia/Kolkata")

# ──────────────────────────────────────────────
# HEADERS
# ──────────────────────────────────────────────
def _headers():
    return {
        "access-token": DHAN_ACCESS_TOKEN,
        "client-id":    DHAN_CLIENT_ID,
        "Content-Type": "application/json"
    }

# ──────────────────────────────────────────────
# TELEGRAM
# ──────────────────────────────────────────────
def send_telegram(msg):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10
        )
        r.raise_for_status()
        print("Telegram sent ✅")
    except Exception as e:
        print(f"Telegram error: {e}")

# ──────────────────────────────────────────────
# MARKET HOURS
# ──────────────────────────────────────────────
def is_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False, f"Weekend ({now.strftime('%A')})"
    open_t  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    if open_t <= now <= close_t:
        return True, now.strftime("%H:%M IST")
    return False, f"Market closed — {now.strftime('%H:%M IST')}"

# ──────────────────────────────────────────────
# GET SENSEX SPOT PRICE (INDEX)
# ──────────────────────────────────────────────
def get_sensex_spot():
    """Fetch SENSEX index LTP via Dhan market feed."""
    payload = {
        "IDX_I": ["13"]       # securityId 13 = SENSEX on IDX_I
    }
    try:
        r    = requests.post(MARKET_FEED_URL, json=payload, headers=_headers(), timeout=10)
        data = r.json()
        ltp  = data.get("data", {}).get("IDX_I", {}).get("13", {}).get("last_price")
        if ltp:
            print(f"SENSEX Spot: {ltp}")
            return float(ltp)
    except Exception as e:
        print(f"Spot fetch error: {e}")

    # Fallback — fetch from intraday last close
    now       = datetime.now(IST)
    from_dt   = now.replace(hour=9, minute=15, second=0, microsecond=0)
    closes    = _fetch_index_candles(from_dt, now)
    if closes:
        return float(closes[-1])
    return None

def _fetch_index_candles(from_dt, to_dt):
    """Fetch SENSEX INDEX 3-min candles."""
    payload = {
        "securityId":      "13",
        "exchangeSegment": "IDX_I",
        "instrument":      "INDEX",
        "interval":        INTERVAL,
        "fromDate":        from_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "toDate":          to_dt.strftime("%Y-%m-%d %H:%M:%S")
    }
    try:
        r    = requests.post(INTRADAY_URL, json=payload, headers=_headers(), timeout=15)
        data = r.json()
        return data.get("close", [])
    except Exception as e:
        print(f"Index candle fetch error: {e}")
        return []

# ──────────────────────────────────────────────
# GET EXPIRY & OPTION SECURITY IDs
# ──────────────────────────────────────────────
def get_nearest_expiry_and_strikes(spot):
    """
    Use Dhan option chain API to get the nearest expiry and
    security IDs for ATM ±5 CE & PE strikes.
    Returns: expiry_str, { strike: {"CE": secId, "PE": secId}, ... }
    """
    atm = round(spot / STRIKE_STEP) * STRIKE_STEP
    strikes_needed = [atm + i * STRIKE_STEP for i in range(-ATM_RANGE, ATM_RANGE + 1)]

    payload = {
        "UnderlyingScrip": 13,          # SENSEX
        "UnderlyingSeg":   "IDX_I",
        "Expiry":          ""           # empty = nearest expiry
    }
    try:
        r    = requests.post(OPTION_CHAIN_URL, json=payload, headers=_headers(), timeout=15)
        data = r.json()
    except Exception as e:
        print(f"Option chain fetch error: {e}")
        return None, {}

    expiry   = data.get("expiryList", [""])[0]
    oc_data  = data.get("data", [])

    strike_map = {}
    for row in oc_data:
        strike = row.get("strikePrice")
        if strike in strikes_needed:
            strike_map[strike] = {
                "CE": str(row.get("callSecurityId", "")),
                "PE": str(row.get("putSecurityId", ""))
            }

    print(f"Expiry: {expiry} | Found {len(strike_map)} strikes in option chain")
    return expiry, strike_map

# ──────────────────────────────────────────────
# FETCH OPTION CANDLES
# ──────────────────────────────────────────────
def get_prev_trading_day(now):
    day = now - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day

def fetch_option_closes(security_id):
    """
    Fetch prev day + today 3-min close prices for an option security.
    Returns (all_closes_list, latest_close) or (None, None)
    """
    now      = datetime.now(IST)
    prev     = get_prev_trading_day(now)

    prev_from = prev.replace(hour=9,  minute=15, second=0, microsecond=0)
    prev_to   = prev.replace(hour=15, minute=30, second=0, microsecond=0)

    today_from = now.replace(hour=9, minute=15, second=0, microsecond=0)

    def _fetch(from_dt, to_dt, seg="FNO"):
        payload = {
            "securityId":      security_id,
            "exchangeSegment": seg,
            "instrument":      "OPTIDX",
            "interval":        INTERVAL,
            "fromDate":        from_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "toDate":          to_dt.strftime("%Y-%m-%d %H:%M:%S")
        }
        try:
            r    = requests.post(INTRADAY_URL, json=payload, headers=_headers(), timeout=15)
            data = r.json()
            return data.get("close", [])
        except Exception as e:
            print(f"Option candle error ({security_id}): {e}")
            return []

    prev_closes  = _fetch(prev_from, prev_to)
    today_closes = _fetch(today_from, now)

    if not today_closes:
        return None, None

    all_closes = list(prev_closes) + list(today_closes)
    return all_closes, float(today_closes[-1])

# ──────────────────────────────────────────────
# FETCH OPTION INTRADAY CANDLES FOR VWAP
# ──────────────────────────────────────────────
def fetch_option_ohlcv(security_id):
    """Fetch today's OHLCV candles for VWAP calculation."""
    now        = datetime.now(IST)
    today_from = now.replace(hour=9, minute=15, second=0, microsecond=0)
    payload = {
        "securityId":      security_id,
        "exchangeSegment": "FNO",
        "instrument":      "OPTIDX",
        "interval":        INTERVAL,
        "fromDate":        today_from.strftime("%Y-%m-%d %H:%M:%S"),
        "toDate":          now.strftime("%Y-%m-%d %H:%M:%S")
    }
    try:
        r    = requests.post(INTRADAY_URL, json=payload, headers=_headers(), timeout=15)
        data = r.json()
        return {
            "open":   data.get("open",   []),
            "high":   data.get("high",   []),
            "low":    data.get("low",    []),
            "close":  data.get("close",  []),
            "volume": data.get("volume", [])
        }
    except Exception as e:
        print(f"OHLCV fetch error ({security_id}): {e}")
        return None

# ──────────────────────────────────────────────
# INDICATORS
# ──────────────────────────────────────────────
def calc_ema(closes, period):
    if len(closes) < period:
        return None
    k   = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 2)

def calc_vwap(ohlcv):
    """Session VWAP = cumsum(hlc3 * volume) / cumsum(volume)"""
    highs   = ohlcv["high"]
    lows    = ohlcv["low"]
    closes  = ohlcv["close"]
    volumes = ohlcv["volume"]

    if not (highs and lows and closes and volumes):
        return None

    cum_tp_vol = 0.0
    cum_vol    = 0.0
    for h, l, c, v in zip(highs, lows, closes, volumes):
        tp          = (h + l + c) / 3
        cum_tp_vol += tp * v
        cum_vol    += v

    if cum_vol == 0:
        return None
    return round(cum_tp_vol / cum_vol, 2)

# ──────────────────────────────────────────────
# BUY SIGNAL LOGIC
# ──────────────────────────────────────────────
def check_buy_signal(all_closes, ohlcv):
    """
    BUY = latest close > VWAP  AND  > EMA14  AND  > EMA28
    Returns (signal: bool, details: dict)
    """
    if not all_closes or len(all_closes) < EMA_SLOW:
        return False, {}

    latest  = float(all_closes[-1])
    ema14   = calc_ema(all_closes, EMA_FAST)
    ema28   = calc_ema(all_closes, EMA_SLOW)
    vwap    = calc_vwap(ohlcv) if ohlcv else None

    details = {
        "close": latest,
        "ema14": ema14,
        "ema28": ema28,
        "vwap":  vwap
    }

    if ema14 is None or ema28 is None or vwap is None:
        return False, details

    signal = latest > vwap and latest > ema14 and latest > ema28
    return signal, details

# ──────────────────────────────────────────────
# STATE
# ──────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ──────────────────────────────────────────────
# FORMAT TELEGRAM MESSAGE
# ──────────────────────────────────────────────
def build_telegram_message(spot, atm, scan_results, ist_time):
    """
    scan_results: list of dicts:
      { strike, type (CE/PE), signal (True/False), close, ema14, ema28, vwap, new_signal }
    """
    ce_buys = [r for r in scan_results if r["type"] == "CE" and r["signal"]]
    pe_buys = [r for r in scan_results if r["type"] == "PE" and r["signal"]]
    new_ce  = [r for r in ce_buys if r.get("new_signal")]
    new_pe  = [r for r in pe_buys if r.get("new_signal")]

    lines = [
        f"📊 *SENSEX ATM ±5 Strike Scanner*",
        f"🕐 {ist_time}  |  Spot: `{spot:.0f}`  |  ATM: `{atm}`",
        f"━━━━━━━━━━━━━━━━━━━━━━",
    ]

    # ── New crossover alerts (prominent) ──────
    if new_ce:
        for r in new_ce:
            lines.append(
                f"🟢 *NEW BUY CE* → Strike `{r['strike']}`\n"
                f"   Price `{r['close']}` > VWAP `{r['vwap']}` | EMA14 `{r['ema14']}` | EMA28 `{r['ema28']}`"
            )
    if new_pe:
        for r in new_pe:
            lines.append(
                f"🔴 *NEW BUY PE* → Strike `{r['strike']}`\n"
                f"   Price `{r['close']}` > VWAP `{r['vwap']}` | EMA14 `{r['ema14']}` | EMA28 `{r['ema28']}`"
            )

    if new_ce or new_pe:
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")

    # ── Full CE overview ───────────────────────
    lines.append("*📈 CE Strikes Overview*")
    ce_results = sorted([r for r in scan_results if r["type"] == "CE"], key=lambda x: x["strike"])
    for r in ce_results:
        icon  = "✅" if r["signal"] else "⬜"
        lines.append(
            f"{icon} `{r['strike']} CE`  Close:`{r['close']}`  "
            f"VWAP:`{r['vwap']}`  E14:`{r['ema14']}`  E28:`{r['ema28']}`"
        )

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")

    # ── Full PE overview ───────────────────────
    lines.append("*📉 PE Strikes Overview*")
    pe_results = sorted([r for r in scan_results if r["type"] == "PE"], key=lambda x: x["strike"])
    for r in pe_results:
        icon  = "✅" if r["signal"] else "⬜"
        lines.append(
            f"{icon} `{r['strike']} PE`  Close:`{r['close']}`  "
            f"VWAP:`{r['vwap']}`  E14:`{r['ema14']}`  E28:`{r['ema28']}`"
        )

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"✅ = Price > VWAP + EMA14 + EMA28 (BUY bias)")

    return "\n".join(lines)

# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    open_status, reason = is_market_open()
    if not open_status:
        print(f"Skipping — {reason}")
        return

    ist_time = datetime.now(IST).strftime("%d-%b %H:%M")
    print(f"\n{'='*50}")
    print(f"Market open — {reason}  |  {ist_time}")
    print(f"{'='*50}")

    # 1. Get SENSEX spot
    spot = get_sensex_spot()
    if not spot:
        print("Could not fetch SENSEX spot. Exiting.")
        return

    atm = round(spot / STRIKE_STEP) * STRIKE_STEP
    print(f"ATM Strike: {atm}")

    # 2. Get option chain — security IDs for ±5 strikes
    expiry, strike_map = get_nearest_expiry_and_strikes(spot)
    if not strike_map:
        print("No strikes found in option chain.")
        return

    # 3. Load previous state
    state = load_state()

    # 4. Scan each strike × CE/PE
    scan_results = []
    for strike in sorted(strike_map.keys()):
        for opt_type in ["CE", "PE"]:
            sec_id = strike_map[strike].get(opt_type, "")
            if not sec_id:
                continue

            label = f"{strike}_{opt_type}"
            print(f"\nChecking {label} (secId: {sec_id})")

            all_closes, latest = fetch_option_closes(sec_id)
            ohlcv              = fetch_option_ohlcv(sec_id)

            if all_closes is None:
                print(f"  No data for {label}")
                continue

            signal, details = check_buy_signal(all_closes, ohlcv)

            # Was it already in BUY state?
            prev_signal  = state.get(label, False)
            new_signal   = signal and not prev_signal     # only True when first turns BUY

            # Update state
            state[label] = signal

            print(
                f"  Signal: {'BUY ✅' if signal else 'NO  ⬜'}  "
                f"| Close:{details.get('close')}  VWAP:{details.get('vwap')}  "
                f"EMA14:{details.get('ema14')}  EMA28:{details.get('ema28')}"
                + ("  ← NEW!" if new_signal else "")
            )

            scan_results.append({
                "strike":     strike,
                "type":       opt_type,
                "signal":     signal,
                "new_signal": new_signal,
                "close":      details.get("close", "N/A"),
                "ema14":      details.get("ema14", "N/A"),
                "ema28":      details.get("ema28", "N/A"),
                "vwap":       details.get("vwap",  "N/A"),
            })

    # 5. Save updated state
    save_state(state)

    # 6. Send Telegram — always send full overview every run
    #    (so you get a clear picture of all strikes each 3 mins)
    if scan_results:
        msg = build_telegram_message(spot, atm, scan_results, ist_time)
        send_telegram(msg)
    else:
        print("No scan results to send.")


if __name__ == "__main__":
    main()
