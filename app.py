from __future__ import annotations

import json
import io
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

try:
    from streamlit_autorefresh import st_autorefresh
except Exception:  # pragma: no cover
    st_autorefresh = None

from fyers_client import FyersDataClient, fyers_credentials_source


APP_TITLE = "Trending OI"
IST = ZoneInfo("Asia/Kolkata")
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "trending_oi_cache.sqlite3"
TABLE_NAME = "trending_oi_snapshots"
NIFTY50_CONSTITUENTS_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv"
NIFTY50_FALLBACK_SYMBOLS = [
    "ADANIENT",
    "ADANIPORTS",
    "APOLLOHOSP",
    "ASIANPAINT",
    "AXISBANK",
    "BAJAJ-AUTO",
    "BAJFINANCE",
    "BAJAJFINSV",
    "BEL",
    "BHARTIARTL",
    "BPCL",
    "BRITANNIA",
    "CIPLA",
    "COALINDIA",
    "DRREDDY",
    "EICHERMOT",
    "GRASIM",
    "HCLTECH",
    "HDFCBANK",
    "HEROMOTOCO",
    "HINDALCO",
    "HINDUNILVR",
    "ICICIBANK",
    "INDUSINDBK",
    "INFY",
    "ITC",
    "JSWSTEEL",
    "KOTAKBANK",
    "LT",
    "M&M",
    "MARUTI",
    "NESTLEIND",
    "NTPC",
    "ONGC",
    "POWERGRID",
    "RELIANCE",
    "SBILIFE",
    "SBIN",
    "SHRIRAMFIN",
    "SUNPHARMA",
    "TCS",
    "TATACONSUM",
    "TATAMOTORS",
    "TATASTEEL",
    "TECHM",
    "TITAN",
    "ULTRACEMCO",
    "WIPRO",
    "TRENT",
    "ONGC",
]

SYMBOLS = {
    "NIFTY": {"symbol": "NSE:NIFTY50-INDEX", "label": "NIFTY 50", "step": 50},
    "BANKNIFTY": {"symbol": "NSE:NIFTYBANK-INDEX", "label": "BANK NIFTY", "step": 100},
    "FINNIFTY": {"symbol": "NSE:FINNIFTY-INDEX", "label": "FINNIFTY", "step": 50},
    "MIDCPNIFTY": {"symbol": "NSE:MIDCPNIFTY-INDEX", "label": "MIDCPNIFTY", "step": 25},
    "SENSEX": {"symbol": "BSE:SENSEX-INDEX", "label": "SENSEX", "step": 100},
}

INTERVALS = {"1 min": 1, "5 min": 5, "15 min": 15, "30 min": 30}


@dataclass(frozen=True)
class ViewKey:
    symbol: str
    expiry_date: str
    interval_minutes: int
    mode: str

    def as_string(self) -> str:
        return f"{self.symbol}|{self.expiry_date}|{self.interval_minutes}|{self.mode}"


def now_ist() -> datetime:
    return datetime.now(IST)


def today_ist() -> date:
    return now_ist().date()


def next_weekly_expiry() -> date:
    today = today_ist()
    days_ahead = (3 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return today + timedelta(days=days_ahead)


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in ("", None):
            return default
        return float(value)
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in ("", None):
            return default
        return int(float(value))
    except Exception:
        return default


def fmt_num(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except Exception:
        return "-"


def fmt_pct(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f} %"
    except Exception:
        return "-"


def fmt_signed_num(value: Any, digits: int = 0) -> str:
    try:
        num = float(value)
        sign = "+" if num > 0 else ""
        return f"{sign}{num:,.{digits}f}"
    except Exception:
        return "-"


def strip_none(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default


def supabase_secret(key: str, default: str = "") -> str:
    env_value = os.getenv(key, "").strip()
    if env_value:
        return env_value
    try:
        secrets = getattr(st, "secrets", {})
        if key in secrets:
            return str(secrets.get(key, "")).strip()
        nested = secrets.get("supabase", {})
        nested_map = {
            "SUPABASE_URL": "url",
            "SUPABASE_SERVICE_ROLE_KEY": "service_role_key",
            "SUPABASE_KEY": "key",
            "SUPABASE_ANON_KEY": "anon_key",
            "SUPABASE_TABLE": "table",
        }
        nested_key = nested_map.get(key)
        if nested_key and nested_key in nested:
            return str(nested.get(nested_key, "")).strip()
    except Exception:
        pass
    return default


def supabase_config() -> dict[str, str]:
    url = supabase_secret("SUPABASE_URL").rstrip("/")
    key = (
        supabase_secret("SUPABASE_SERVICE_ROLE_KEY")
        or supabase_secret("SUPABASE_KEY")
        or supabase_secret("SUPABASE_ANON_KEY")
    )
    table = supabase_secret("SUPABASE_TABLE", TABLE_NAME) or TABLE_NAME
    if not url or not key:
        return {}
    return {"url": url, "key": key, "table": table}


def storage_source() -> str:
    cfg = supabase_config()
    if cfg:
        return f"Supabase: {cfg['table']}"
    return f"Local SQLite: {DB_PATH.name}"


def supabase_headers(prefer: str | None = None) -> dict[str, str]:
    cfg = supabase_config()
    headers = {
        "apikey": cfg["key"],
        "Authorization": f"Bearer {cfg['key']}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def ensure_sqlite() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trending_oi_snapshots (
            snapshot_key TEXT PRIMARY KEY,
            snapshot_ts TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            snapshot_minute TEXT NOT NULL,
            symbol TEXT NOT NULL,
            symbol_label TEXT NOT NULL,
            expiry_date TEXT NOT NULL,
            mode TEXT NOT NULL,
            interval_minutes INTEGER NOT NULL,
            interval_label TEXT NOT NULL,
            strike_step INTEGER NOT NULL,
            strike_span INTEGER NOT NULL,
            selected_strikes_json TEXT NOT NULL,
            underlying_ltp REAL NOT NULL,
            underlying_change REAL NOT NULL,
            underlying_change_pct REAL NOT NULL,
            session_open REAL NOT NULL,
            session_high REAL NOT NULL,
            session_low REAL NOT NULL,
            day_hl_break TEXT NOT NULL,
            total_ce_oi REAL NOT NULL,
            total_pe_oi REAL NOT NULL,
            ce_oi_change REAL NOT NULL,
            pe_oi_change REAL NOT NULL,
            diff_in_oi REAL NOT NULL,
            strength_pct REAL NOT NULL,
            direction TEXT NOT NULL,
            direction_change REAL NOT NULL,
            direction_change_pct REAL NOT NULL,
            chg_in_direction REAL NOT NULL,
            chg_in_direction_pct REAL NOT NULL,
            net_pcr REAL NOT NULL,
            atm_strike INTEGER NOT NULL,
            option_chain_json TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trending_lookup ON trending_oi_snapshots(symbol, expiry_date, mode, interval_minutes, snapshot_ts)"
    )
    return conn


def parse_json_field(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return []
    try:
        return json.loads(value)
    except Exception:
        return value


def to_jsonable(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    if value is None or isinstance(value, (int, float, str, bool)):
        return value
    return str(value)


def generate_strikes(spot: float, step: int, span: int) -> list[int]:
    center = int(round(spot / step) * step) if spot else step
    half = max(1, span // 2)
    start = center - (half * step)
    return [start + (i * step) for i in range(span)]


def bucket_rows(frame: pd.DataFrame, interval_minutes: int) -> pd.DataFrame:
    if frame.empty or interval_minutes <= 1:
        return frame.copy()
    temp = frame.copy()
    temp["bucket_ts"] = temp["snapshot_ts"].dt.floor(f"{interval_minutes}min")
    grouped = temp.groupby("bucket_ts", as_index=False).tail(1).sort_values("bucket_ts")
    return grouped.drop(columns=["bucket_ts"]).reset_index(drop=True)


def build_strike_frame(options_chain: list[dict[str, Any]], selected_strikes: list[int]) -> pd.DataFrame:
    grouped: dict[int, dict[str, dict[str, Any]]] = {}
    for contract in options_chain:
        strike = safe_int(contract.get("strike_price"))
        option_type = strip_none(contract.get("option_type")).upper()
        if strike > 0 and option_type in {"CE", "PE"}:
            grouped.setdefault(strike, {})[option_type] = contract

    rows: list[dict[str, Any]] = []
    for strike in selected_strikes:
        ce = grouped.get(strike, {}).get("CE", {})
        pe = grouped.get(strike, {}).get("PE", {})
        rows.append(
            {
                "Strike": strike,
                "CE LTP": safe_float(ce.get("ltp")),
                "CE OI": safe_float(ce.get("oi")),
                "CE Volume": safe_float(ce.get("volume")),
                "CE OI Chg %": safe_float(ce.get("oichp")),
                "PE LTP": safe_float(pe.get("ltp")),
                "PE OI": safe_float(pe.get("oi")),
                "PE Volume": safe_float(pe.get("volume")),
                "PE OI Chg %": safe_float(pe.get("oichp")),
                "IV": (safe_float(ce.get("iv")) + safe_float(pe.get("iv"))) / 2 if ce or pe else None,
            }
        )
    return pd.DataFrame(rows)


def fetch_option_chain(client: FyersDataClient, symbol: str, strike_span: int) -> tuple[float, list[dict[str, Any]]]:
    quote_resp = client.fyers.quotes(data={"symbols": symbol})
    if quote_resp.get("s") != "ok":
        raise RuntimeError(quote_resp.get("message", "Unable to fetch underlying quote from FYERS."))
    spot = safe_float(quote_resp["d"][0]["v"].get("lp", 0))

    strikecount = max(12, int(strike_span / 2) + 4)
    chain_resp = client.fyers.optionchain(
        data={"symbol": symbol, "strikecount": strikecount, "timestamp": "", "greeks": "1"}
    )
    if chain_resp.get("s") != "ok":
        raise RuntimeError(chain_resp.get("message", "Unable to fetch FYERS option chain."))
    return spot, chain_resp.get("data", {}).get("optionsChain", [])


def detect_order_blocks(
    candles: pd.DataFrame,
    spot: float,
    lb: int = 5,
    per_side_limit: int = 3,
    display_date: Any | None = None,
    keep_earliest: bool = True,
    reference_label: str = "spot",
) -> pd.DataFrame:
    if candles.empty or len(candles) < (lb * 2) + 2:
        return pd.DataFrame(columns=["Type", "Zone", "Low", "High", "CreatedTS", "Created", "Distance", "Status"])

    frame = candles.sort_values("timestamp").reset_index(drop=True).copy()
    pivot_highs: dict[int, float] = {}
    pivot_lows: dict[int, float] = {}
    for i in range(lb, len(frame) - lb):
        high_window = frame.loc[i - lb : i + lb, "high"]
        low_window = frame.loc[i - lb : i + lb, "low"]
        if frame.at[i, "high"] == high_window.max():
            pivot_highs[i + lb] = float(frame.at[i, "high"])
        if frame.at[i, "low"] == low_window.min():
            pivot_lows[i + lb] = float(frame.at[i, "low"])

    last_swing_high: float | None = None
    last_swing_low: float | None = None
    last_red_idx: int | None = None
    last_green_idx: int | None = None
    order_blocks: list[dict[str, Any]] = []

    for i, row in frame.iterrows():
        if i in pivot_highs:
            last_swing_high = pivot_highs[i]
        if i in pivot_lows:
            last_swing_low = pivot_lows[i]

        previous_close = float(frame.at[i - 1, "close"]) if i > 0 else float(row["close"])
        close = float(row["close"])

        if last_swing_high is not None and previous_close <= last_swing_high < close and last_red_idx is not None:
            candle = frame.loc[last_red_idx]
            order_blocks.append(
                {
                    "type": "Bullish OB",
                    "low": float(candle["low"]),
                    "high": float(candle["high"]),
                    "created": candle["timestamp"],
                }
            )
            last_swing_high = None

        if last_swing_low is not None and previous_close >= last_swing_low > close and last_green_idx is not None:
            candle = frame.loc[last_green_idx]
            order_blocks.append(
                {
                    "type": "Bearish OB",
                    "low": float(candle["low"]),
                    "high": float(candle["high"]),
                    "created": candle["timestamp"],
                }
            )
            last_swing_low = None

        if close > float(row["open"]):
            last_green_idx = i
        elif close < float(row["open"]):
            last_red_idx = i

    rows: list[dict[str, Any]] = []
    for zone in order_blocks:
        created_ts = pd.to_datetime(zone["created"])
        if zone["low"] <= spot <= zone["high"]:
            status = f"{reference_label.title()} inside zone"
            distance = 0.0
        elif spot < zone["low"]:
            status = f"Above {reference_label}"
            distance = zone["low"] - spot
        else:
            status = f"Below {reference_label}"
            distance = spot - zone["high"]
        rows.append(
            {
                "Type": zone["type"],
                "Zone": f"{zone['low']:,.2f} - {zone['high']:,.2f}",
                "Low": zone["low"],
                "High": zone["high"],
                "CreatedTS": created_ts,
                "Created": created_ts.strftime("%d %b %H:%M") if display_date is None else created_ts.strftime("%H:%M"),
                "Distance": distance,
                "Status": status,
            }
        )

    if not rows:
        return pd.DataFrame(columns=["Type", "Zone", "Low", "High", "CreatedTS", "Created", "Distance", "Status"])

    zones = pd.DataFrame(rows)
    if display_date is not None:
        same_day = zones["CreatedTS"].dt.date == display_date
        if same_day.any():
            zones = zones.loc[same_day].copy()

    selected_parts: list[pd.DataFrame] = []
    for _, side_frame in zones.groupby("Type", sort=False):
        if keep_earliest:
            earliest = side_frame.sort_values("CreatedTS", ascending=True).head(1)
            remaining = side_frame.drop(index=earliest.index)
            closest = remaining.sort_values(["Distance", "CreatedTS"], ascending=[True, False]).head(max(per_side_limit - 1, 0))
            selected_parts.append(pd.concat([earliest, closest]))
        else:
            closest = side_frame.sort_values(["Distance", "CreatedTS"], ascending=[True, False]).head(per_side_limit)
            selected_parts.append(closest)

    selected = pd.concat(selected_parts) if selected_parts else zones.head(0)
    return selected.sort_values("CreatedTS", ascending=False).reset_index(drop=True)


def load_nifty50_universe() -> pd.DataFrame:
    try:
        response = requests.get(
            NIFTY50_CONSTITUENTS_URL,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        response.raise_for_status()
        frame = pd.read_csv(io.StringIO(response.text))
    except Exception:
        frame = pd.DataFrame({"company_name": NIFTY50_FALLBACK_SYMBOLS, "symbol": NIFTY50_FALLBACK_SYMBOLS})
    frame = frame.rename(columns=lambda col: str(col).strip())
    symbol_col = "Symbol" if "Symbol" in frame.columns else frame.columns[0]
    name_col = "Company Name" if "Company Name" in frame.columns else frame.columns[0]
    cleaned = frame[[name_col, symbol_col]].copy()
    cleaned.columns = ["company_name", "symbol"]
    cleaned["company_name"] = cleaned["company_name"].astype(str).str.strip()
    cleaned["symbol"] = cleaned["symbol"].astype(str).str.strip()
    cleaned = cleaned[cleaned["symbol"].str.len() > 0]
    cleaned["fyers_symbol"] = cleaned["symbol"].map(lambda sym: f"NSE:{sym}-EQ")
    cleaned = cleaned.drop_duplicates(subset=["symbol"]).reset_index(drop=True)
    return cleaned


def scan_stock_order_block(
    client: FyersDataClient,
    row: pd.Series,
    resolution: str = "3",
    lookback_days: int = 5,
) -> dict[str, Any] | None:
    symbol = str(row["symbol"]).strip()
    fyers_symbol = str(row["fyers_symbol"]).strip()
    company_name = str(row["company_name"]).strip()
    end_date = now_ist().date()
    start_date = end_date - timedelta(days=lookback_days)

    try:
        candles = client.fetch_history(fyers_symbol, resolution, start_date.isoformat(), end_date.isoformat())
    except Exception as exc:
        return {
            "symbol": symbol,
            "company_name": company_name,
            "fyers_symbol": fyers_symbol,
            "error": str(exc),
        }

    if candles.empty:
        return {
            "symbol": symbol,
            "company_name": company_name,
            "fyers_symbol": fyers_symbol,
            "error": "No candles returned.",
        }

    spot = safe_float(candles.iloc[-1]["close"])
    zones = detect_order_blocks(candles, spot, display_date=end_date, reference_label="spot")
    if zones.empty:
        return {
            "symbol": symbol,
            "company_name": company_name,
            "fyers_symbol": fyers_symbol,
            "spot": spot,
            "bullish": None,
            "bearish": None,
            "latest_type": None,
            "latest_time": None,
        }

    bullish = zones.loc[zones["Type"] == "Bullish OB"].sort_values("CreatedTS")
    bearish = zones.loc[zones["Type"] == "Bearish OB"].sort_values("CreatedTS")
    latest_bull = bullish.iloc[-1].to_dict() if not bullish.empty else None
    latest_bear = bearish.iloc[-1].to_dict() if not bearish.empty else None

    latest_type = None
    latest_time = None
    latest_zone = None
    if latest_bull and latest_bear:
        if pd.to_datetime(latest_bull["CreatedTS"]) >= pd.to_datetime(latest_bear["CreatedTS"]):
            latest_type = "Bullish OB"
            latest_time = latest_bull["CreatedTS"]
            latest_zone = latest_bull
        else:
            latest_type = "Bearish OB"
            latest_time = latest_bear["CreatedTS"]
            latest_zone = latest_bear
    elif latest_bull:
        latest_type = "Bullish OB"
        latest_time = latest_bull["CreatedTS"]
        latest_zone = latest_bull
    elif latest_bear:
        latest_type = "Bearish OB"
        latest_time = latest_bear["CreatedTS"]
        latest_zone = latest_bear

    return {
        "symbol": symbol,
        "company_name": company_name,
        "fyers_symbol": fyers_symbol,
        "spot": spot,
        "latest_type": latest_type,
        "latest_time": latest_time,
        "latest_zone": latest_zone,
        "bullish": latest_bull,
        "bearish": latest_bear,
        "candles": len(candles),
    }


def scan_nifty50_order_blocks(
    client: FyersDataClient,
    resolution: str = "3",
    lookback_days: int = 5,
    max_workers: int = 6,
) -> dict[str, list[dict[str, Any]]]:
    universe = load_nifty50_universe()
    bullish_rows: list[dict[str, Any]] = []
    bearish_rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(scan_stock_order_block, client, row, resolution, lookback_days): row
            for _, row in universe.iterrows()
        }
        for future in as_completed(futures):
            result = future.result()
            if not result:
                continue
            if result.get("error"):
                errors.append(result)
                continue

            latest_type = result.get("latest_type")
            latest_zone = result.get("latest_zone") or {}
            if latest_type == "Bullish OB":
                bullish_rows.append(
                    {
                        "Symbol": result["symbol"],
                        "Company": result["company_name"],
                        "Spot": result["spot"],
                        "Created": latest_zone.get("Created"),
                        "Zone": latest_zone.get("Zone", "-"),
                        "Status": latest_zone.get("Status", "-"),
                        "Distance": latest_zone.get("Distance", 0.0),
                        "Candles": result.get("candles", 0),
                    }
                )
            elif latest_type == "Bearish OB":
                bearish_rows.append(
                    {
                        "Symbol": result["symbol"],
                        "Company": result["company_name"],
                        "Spot": result["spot"],
                        "Created": latest_zone.get("Created"),
                        "Zone": latest_zone.get("Zone", "-"),
                        "Status": latest_zone.get("Status", "-"),
                        "Distance": latest_zone.get("Distance", 0.0),
                        "Candles": result.get("candles", 0),
                    }
                )

    bullish_rows = sorted(bullish_rows, key=lambda row: row.get("Created", ""), reverse=True)
    bearish_rows = sorted(bearish_rows, key=lambda row: row.get("Created", ""), reverse=True)
    return {"bullish": bullish_rows, "bearish": bearish_rows, "errors": errors}


def normalize_history_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    temp = frame.copy()
    temp["snapshot_ts"] = pd.to_datetime(temp["snapshot_ts"], errors="coerce")
    for col in [
        "underlying_ltp",
        "underlying_change",
        "underlying_change_pct",
        "total_ce_oi",
        "total_pe_oi",
        "ce_oi_change",
        "pe_oi_change",
        "diff_in_oi",
        "strength_pct",
        "direction_change",
        "direction_change_pct",
        "chg_in_direction",
        "chg_in_direction_pct",
        "net_pcr",
    ]:
        if col in temp.columns:
            temp[col] = pd.to_numeric(temp[col], errors="coerce").fillna(0.0)
    if "selected_strikes_json" in temp.columns:
        temp["selected_strikes_json"] = temp["selected_strikes_json"].apply(parse_json_field)
    if "option_chain_json" in temp.columns:
        temp["option_chain_json"] = temp["option_chain_json"].apply(parse_json_field)
    if "payload_json" in temp.columns:
        temp["payload_json"] = temp["payload_json"].apply(parse_json_field)
    return temp


def load_rows(
    symbol: str,
    expiry_date: str,
    interval_minutes: int,
    mode: str,
    snapshot_date: str | None = None,
    limit: int = 500,
) -> pd.DataFrame:
    cfg = supabase_config()
    if cfg:
        params = {
            "select": "*",
            "symbol": f"eq.{symbol}",
            "expiry_date": f"eq.{expiry_date}",
            "interval_minutes": f"eq.{interval_minutes}",
            "mode": f"eq.{mode}",
            "order": "snapshot_ts.asc",
            "limit": str(limit),
        }
        if snapshot_date:
            params["snapshot_date"] = f"eq.{snapshot_date}"
        response = requests.get(
            f"{cfg['url']}/rest/v1/{cfg['table']}",
            params=params,
            headers=supabase_headers(),
            timeout=20,
        )
        response.raise_for_status()
        return normalize_history_frame(pd.DataFrame(response.json()))

    with ensure_sqlite() as conn:
        query = """
            SELECT *
            FROM trending_oi_snapshots
            WHERE symbol = ?
              AND expiry_date = ?
              AND interval_minutes = ?
              AND mode = ?
        """
        params: list[Any] = [symbol, expiry_date, interval_minutes, mode]
        if snapshot_date:
            query += " AND snapshot_date = ?"
            params.append(snapshot_date)
        query += " ORDER BY snapshot_ts ASC LIMIT ?"
        params.append(limit)
        frame = pd.read_sql_query(query, conn, params=params)
    return normalize_history_frame(frame)


def load_previous_snapshot(
    symbol: str,
    expiry_date: str,
    interval_minutes: int,
    mode: str,
    snapshot_date: str | None = None,
) -> dict[str, Any] | None:
    frame = load_rows(symbol, expiry_date, interval_minutes, mode, snapshot_date=snapshot_date, limit=500)
    if frame.empty:
        return None
    return frame.iloc[-1].to_dict()


def summarize_snapshot(
    symbol_key: str,
    symbol_label: str,
    expiry_date: str,
    interval_minutes: int,
    strike_step: int,
    strike_span: int,
    spot: float,
    frame: pd.DataFrame,
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    snapshot_ts = now_ist()
    snapshot_date = snapshot_ts.date().isoformat()
    snapshot_minute = snapshot_ts.strftime("%Y-%m-%d %H:%M")
    selected_strikes = generate_strikes(spot, strike_step, strike_span)

    total_ce_oi = safe_float(frame["CE OI"].sum()) if not frame.empty else 0.0
    total_pe_oi = safe_float(frame["PE OI"].sum()) if not frame.empty else 0.0

    prev_ltp = safe_float(previous["underlying_ltp"]) if previous else 0.0
    underlying_change = spot - prev_ltp if prev_ltp else 0.0
    underlying_change_pct = (underlying_change / prev_ltp * 100) if prev_ltp else 0.0

    prev_ce_oi = safe_float(previous["total_ce_oi"]) if previous else 0.0
    prev_pe_oi = safe_float(previous["total_pe_oi"]) if previous else 0.0
    ce_oi_change = total_ce_oi - prev_ce_oi
    pe_oi_change = total_pe_oi - prev_pe_oi
    diff_in_oi = pe_oi_change - ce_oi_change
    strength_pct = (diff_in_oi / abs(pe_oi_change) * 100) if pe_oi_change else 0.0
    net_pcr = (total_pe_oi / total_ce_oi) if total_ce_oi else 0.0

    session_open = prev_ltp or spot
    session_high = max(session_open, spot)
    session_low = min(session_open, spot)
    day_hl_break = "D.H.B. ({})".format(fmt_num(spot)) if previous and spot >= safe_float(previous.get("session_high")) else "-"

    interval_label = next((label for label, minutes in INTERVALS.items() if minutes == interval_minutes), f"{interval_minutes} min")
    direction = "UP" if underlying_change > 0 else "DOWN" if underlying_change < 0 else "FLAT"

    payload = {
        "snapshot_key": ViewKey(symbol_key, expiry_date, interval_minutes, "live").as_string() + f"|{snapshot_minute}",
        "snapshot_ts": snapshot_ts.isoformat(),
        "snapshot_date": snapshot_date,
        "snapshot_minute": snapshot_minute,
        "symbol": symbol_key,
        "symbol_label": symbol_label,
        "expiry_date": expiry_date,
        "mode": "live",
        "interval_minutes": interval_minutes,
        "interval_label": interval_label,
        "strike_step": strike_step,
        "strike_span": strike_span,
        "selected_strikes_json": selected_strikes,
        "underlying_ltp": spot,
        "underlying_change": underlying_change,
        "underlying_change_pct": underlying_change_pct,
        "session_open": session_open,
        "session_high": session_high,
        "session_low": session_low,
        "day_hl_break": day_hl_break,
        "total_ce_oi": total_ce_oi,
        "total_pe_oi": total_pe_oi,
        "ce_oi_change": ce_oi_change,
        "pe_oi_change": pe_oi_change,
        "diff_in_oi": diff_in_oi,
        "strength_pct": strength_pct,
        "direction": direction,
        "direction_change": underlying_change,
        "direction_change_pct": underlying_change_pct,
        "chg_in_direction": diff_in_oi * underlying_change * 0.15,
        "chg_in_direction_pct": (underlying_change_pct * 1.0) if diff_in_oi else 0.0,
        "net_pcr": net_pcr,
        "atm_strike": selected_strikes[len(selected_strikes) // 2] if selected_strikes else 0,
        "option_chain_json": frame.to_dict(orient="records"),
        "payload_json": {},
        "created_at": snapshot_ts.isoformat(),
        "updated_at": snapshot_ts.isoformat(),
    }
    payload["payload_json"] = {
        "selected_strikes": selected_strikes,
        "summary": {
            "underlying_ltp": spot,
            "underlying_change": underlying_change,
            "underlying_change_pct": underlying_change_pct,
            "total_ce_oi": total_ce_oi,
            "total_pe_oi": total_pe_oi,
            "ce_oi_change": ce_oi_change,
            "pe_oi_change": pe_oi_change,
            "diff_in_oi": diff_in_oi,
            "strength_pct": strength_pct,
            "direction": direction,
            "net_pcr": net_pcr,
            "day_hl_break": day_hl_break,
        },
    }
    return payload


def store_snapshot(record: dict[str, Any]) -> None:
    cfg = supabase_config()
    payload = record.copy()
    payload["selected_strikes_json"] = to_jsonable(payload["selected_strikes_json"])
    payload["option_chain_json"] = to_jsonable(payload["option_chain_json"])
    payload["payload_json"] = to_jsonable(payload["payload_json"])

    if cfg:
        response = requests.post(
            f"{cfg['url']}/rest/v1/{cfg['table']}",
            params={"on_conflict": "snapshot_key"},
            headers=supabase_headers("resolution=merge-duplicates"),
            json=payload,
            timeout=20,
        )
        response.raise_for_status()
        return

    with ensure_sqlite() as conn:
        conn.execute(
            """
            INSERT INTO trending_oi_snapshots (
                snapshot_key, snapshot_ts, snapshot_date, snapshot_minute, symbol, symbol_label, expiry_date, mode,
                interval_minutes, interval_label, strike_step, strike_span, selected_strikes_json, underlying_ltp,
                underlying_change, underlying_change_pct, session_open, session_high, session_low, day_hl_break,
                total_ce_oi, total_pe_oi, ce_oi_change, pe_oi_change, diff_in_oi, strength_pct, direction,
                direction_change, direction_change_pct, chg_in_direction, chg_in_direction_pct, net_pcr, atm_strike,
                option_chain_json, payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_key) DO UPDATE SET
                snapshot_ts=excluded.snapshot_ts,
                snapshot_date=excluded.snapshot_date,
                snapshot_minute=excluded.snapshot_minute,
                underlying_ltp=excluded.underlying_ltp,
                underlying_change=excluded.underlying_change,
                underlying_change_pct=excluded.underlying_change_pct,
                session_open=excluded.session_open,
                session_high=excluded.session_high,
                session_low=excluded.session_low,
                day_hl_break=excluded.day_hl_break,
                total_ce_oi=excluded.total_ce_oi,
                total_pe_oi=excluded.total_pe_oi,
                ce_oi_change=excluded.ce_oi_change,
                pe_oi_change=excluded.pe_oi_change,
                diff_in_oi=excluded.diff_in_oi,
                strength_pct=excluded.strength_pct,
                direction=excluded.direction,
                direction_change=excluded.direction_change,
                direction_change_pct=excluded.direction_change_pct,
                chg_in_direction=excluded.chg_in_direction,
                chg_in_direction_pct=excluded.chg_in_direction_pct,
                net_pcr=excluded.net_pcr,
                atm_strike=excluded.atm_strike,
                option_chain_json=excluded.option_chain_json,
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
            """,
            (
                payload["snapshot_key"],
                payload["snapshot_ts"],
                payload["snapshot_date"],
                payload["snapshot_minute"],
                payload["symbol"],
                payload["symbol_label"],
                payload["expiry_date"],
                payload["mode"],
                payload["interval_minutes"],
                payload["interval_label"],
                payload["strike_step"],
                payload["strike_span"],
                json.dumps(payload["selected_strikes_json"], ensure_ascii=False),
                payload["underlying_ltp"],
                payload["underlying_change"],
                payload["underlying_change_pct"],
                payload["session_open"],
                payload["session_high"],
                payload["session_low"],
                payload["day_hl_break"],
                payload["total_ce_oi"],
                payload["total_pe_oi"],
                payload["ce_oi_change"],
                payload["pe_oi_change"],
                payload["diff_in_oi"],
                payload["strength_pct"],
                payload["direction"],
                payload["direction_change"],
                payload["direction_change_pct"],
                payload["chg_in_direction"],
                payload["chg_in_direction_pct"],
                payload["net_pcr"],
                payload["atm_strike"],
                json.dumps(payload["option_chain_json"], ensure_ascii=False),
                json.dumps(payload["payload_json"], ensure_ascii=False),
                payload["created_at"],
                payload["updated_at"],
            ),
        )
        conn.commit()


def _row_option_totals(record: pd.Series) -> dict[str, float]:
    chain = parse_json_field(record.get("option_chain_json"))
    if not isinstance(chain, list) or not chain:
        return {"volume": 0.0, "ce_volume": 0.0, "pe_volume": 0.0}

    ce_volume = 0.0
    pe_volume = 0.0
    for item in chain:
        if not isinstance(item, dict):
            continue
        ce_volume += safe_float(item.get("CE Volume"))
        pe_volume += safe_float(item.get("PE Volume"))
    return {"volume": ce_volume + pe_volume, "ce_volume": ce_volume, "pe_volume": pe_volume}


def render_row(record: pd.Series, idx: int, interval_minutes: int) -> dict[str, Any]:
    timestamp = pd.to_datetime(record["snapshot_ts"], errors="coerce")
    if pd.isna(timestamp):
        timestamp = pd.Timestamp.now(tz=IST)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(IST)
    end_ts = timestamp + timedelta(minutes=interval_minutes)
    if idx == 1:
        date_time = f"{timestamp.strftime('%H:%M')}-EOD"
    else:
        date_time = f"{timestamp.strftime('%H:%M')}-{end_ts.strftime('%H:%M')}"

    totals = _row_option_totals(record)
    total_oi = safe_float(record["total_ce_oi"]) + safe_float(record["total_pe_oi"])
    total_chng_in_oi = safe_float(record["ce_oi_change"]) + safe_float(record["pe_oi_change"])
    oi_change = safe_float(record["diff_in_oi"])
    level_break = strip_none(record.get("day_hl_break"), "-")
    if level_break != "-" and record["direction_change"] >= 0:
        level_break = f"{level_break} ↑"
    elif level_break != "-":
        level_break = f"{level_break} ↓"

    return {
        "#": idx,
        "Date Time": date_time,
        "Total OI": total_oi,
        "Total Chng. In OI": total_chng_in_oi,
        "Day High": safe_float(record["session_high"]),
        "Day Low": safe_float(record["session_low"]),
        "Level Break": level_break,
        "Volume": totals["volume"],
        "LTP": safe_float(record["underlying_ltp"]),
        "LTP Change": safe_float(record["underlying_change"]),
        "OI Change": oi_change,
    }


def build_history_table(records: pd.DataFrame, interval_minutes: int) -> pd.DataFrame:
    if records.empty:
        return pd.DataFrame(columns=["#", "Date Time", "Total OI", "Total Chng. In OI", "Day High", "Day Low", "Level Break", "Volume", "LTP", "LTP Change", "OI Change"])

    frame = records.sort_values("snapshot_ts").reset_index(drop=True)
    bucketed = bucket_rows(frame, interval_minutes)
    view = bucketed if len(bucketed) != len(frame) else frame
    view = view.sort_values("snapshot_ts", ascending=False).reset_index(drop=True)
    rows = [render_row(row, idx + 1, interval_minutes) for idx, (_, row) in enumerate(view.iterrows())]
    return pd.DataFrame(rows)


def format_table_display(view: pd.DataFrame) -> pd.DataFrame:
    if view.empty:
        return view
    display = view.copy()
    display["Total OI"] = display["Total OI"].map(lambda x: fmt_num(x, 0))
    display["Total Chng. In OI"] = display["Total Chng. In OI"].map(lambda x: fmt_signed_num(x, 0))
    display["Day High"] = display["Day High"].map(lambda x: fmt_num(x, 2))
    display["Day Low"] = display["Day Low"].map(lambda x: fmt_num(x, 2))
    display["Volume"] = display["Volume"].map(lambda x: fmt_num(x, 0))
    display["LTP"] = display["LTP"].map(lambda x: fmt_num(x, 2))
    display["LTP Change"] = display["LTP Change"].map(lambda x: fmt_signed_num(x, 2))
    display["OI Change"] = display["OI Change"].map(lambda x: fmt_signed_num(x, 0))
    return display


def style_session_table(view: pd.DataFrame) -> pd.io.formats.style.Styler:
    if view.empty:
        return view.style

    volume_cutoff = safe_float(view["Volume"].quantile(0.75)) if "Volume" in view.columns and not view.empty else 0.0

    def row_style(row: pd.Series) -> list[str]:
        styles = [""] * len(row)
        if "Level Break" in row.index:
            val = str(row["Level Break"])
            if "D.H.B." in val:
                styles[row.index.get_loc("Level Break")] = "background-color: #55b65e; color: #ffffff; font-weight: 800; border-radius: 999px;"
            elif "D.L.B." in val:
                styles[row.index.get_loc("Level Break")] = "background-color: #ef4d3f; color: #ffffff; font-weight: 800; border-radius: 999px;"
        if "Volume" in row.index and safe_float(row["Volume"]) >= volume_cutoff and volume_cutoff > 0:
            styles[row.index.get_loc("Volume")] = "background-color: #f6c343; color: #5c3d00; font-weight: 800; border-radius: 8px;"
        for col in ["Total Chng. In OI", "LTP Change", "OI Change"]:
            if col in row.index:
                value = str(row[col])
                if value.startswith("+"):
                    styles[row.index.get_loc(col)] = "color: #2f9e5b; font-weight: 700;"
                elif value.startswith("-"):
                    styles[row.index.get_loc(col)] = "color: #d44949; font-weight: 700;"
        return styles

    styler = view.style.apply(row_style, axis=1)
    styler = styler.format(
        {
            "Total OI": lambda v: fmt_num(v, 0),
            "Total Chng. In OI": lambda v: fmt_signed_num(v, 0),
            "Day High": lambda v: fmt_num(v, 2),
            "Day Low": lambda v: fmt_num(v, 2),
            "Volume": lambda v: fmt_num(v, 0),
            "LTP": lambda v: fmt_num(v, 2),
            "LTP Change": lambda v: fmt_signed_num(v, 2),
            "OI Change": lambda v: fmt_signed_num(v, 0),
        }
    )
    styler = styler.set_table_styles(
        [
            {"selector": "table", "props": [("border-collapse", "collapse"), ("width", "100%")]},
            {"selector": "thead th", "props": [("background-color", "#f5f2eb"), ("font-family", "Space Grotesk, sans-serif"), ("font-weight", "800"), ("color", "#8f8684"), ("border-bottom", "1px solid rgba(31, 41, 55, 0.12)")]},
            {"selector": "tbody td", "props": [("border-bottom", "1px solid rgba(31, 41, 55, 0.08)"), ("padding", "0.85rem 0.7rem"), ("font-size", "0.95rem")]},
        ]
    )
    return styler


def style_ob_scan_table(view: pd.DataFrame, tone: str) -> pd.io.formats.style.Styler:
    if view.empty:
        return view.style

    def row_style(row: pd.Series) -> list[str]:
        base = "background-color: rgba(255,255,255,0.95); color: #5c5251;"
        if tone == "bullish":
            base = "background-color: rgba(85, 182, 94, 0.08); color: #5c5251;"
        elif tone == "bearish":
            base = "background-color: rgba(239, 77, 63, 0.08); color: #5c5251;"
        return [base] * len(row)

    styler = view.style.apply(row_style, axis=1)
    styler = styler.format(
        {
            "Spot": lambda v: fmt_num(v, 2),
            "Distance": lambda v: fmt_num(v, 2),
            "Candles": lambda v: fmt_num(v, 0),
        }
    )
    styler = styler.set_table_styles(
        [
            {"selector": "table", "props": [("border-collapse", "collapse"), ("width", "100%")]},
            {"selector": "thead th", "props": [("background-color", "#f5f2eb"), ("font-family", "Space Grotesk, sans-serif"), ("font-weight", "800"), ("color", "#8f8684"), ("border-bottom", "1px solid rgba(31, 41, 55, 0.12)")]},
            {"selector": "tbody td", "props": [("border-bottom", "1px solid rgba(31, 41, 55, 0.08)"), ("padding", "0.7rem 0.65rem"), ("font-size", "0.92rem")]},
        ]
    )
    return styler


def metric_card(label: str, value: str, sub: str, tone: str = "neutral") -> None:
    st.markdown(
        f"""
        <div class="metric-card metric-{tone}">
          <div class="metric-label">{label}</div>
          <div class="metric-value">{value}</div>
          <div class="metric-sub">{sub}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def mini_stat(label: str, value: str, tone: str = "neutral") -> None:
    st.markdown(
        f"""
        <div class="mini-stat mini-{tone}">
          <span>{label}</span>
          <strong>{value}</strong>
        </div>
        """,
        unsafe_allow_html=True,
    )


def section_card(title: str, body: str) -> None:
    st.markdown(
        f"""
        <div class="section-card">
          <div class="section-card-title">{title}</div>
          <div class="section-card-body">{body}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def inject_style() -> None:
    st.markdown(
        """
        <style>
        :root {
            --bg: #fbfaf8;
            --bg-2: #f4f1ec;
            --panel: rgba(255, 255, 255, 0.94);
            --panel-strong: rgba(255, 255, 255, 0.98);
            --text: #5c5251;
            --muted: #8f8684;
            --line: rgba(104, 88, 84, 0.14);
            --good: #55b65e;
            --good-bg: rgba(85, 182, 94, 0.14);
            --bad: #ef4d3f;
            --bad-bg: rgba(239, 77, 63, 0.14);
            --warn: #d6a421;
            --warn-bg: rgba(214, 164, 33, 0.14);
            --accent: #c63f2f;
        }
        .stApp {
            background:
                radial-gradient(circle at top left, rgba(198, 63, 47, 0.10), transparent 28%),
                radial-gradient(circle at top right, rgba(214, 164, 33, 0.08), transparent 26%),
                linear-gradient(180deg, var(--bg) 0%, var(--bg-2) 100%);
            color: var(--text);
        }
        .hero {
            padding: 1.1rem 1.2rem;
            border: 1px solid var(--line);
            border-radius: 22px;
            background: var(--panel-strong);
            box-shadow: 0 18px 40px rgba(69, 58, 56, 0.08);
            margin-bottom: 0.9rem;
        }
        .hero h1 {
            margin: 0;
            font-size: 2rem;
            letter-spacing: -0.04em;
            color: var(--text);
        }
        .hero p {
            margin: 0.3rem 0 0;
            color: var(--muted);
        }
        .pill {
            display: inline-flex;
            padding: 0.32rem 0.72rem;
            border-radius: 999px;
            border: 1px solid var(--line);
            background: rgba(255, 255, 255, 0.88);
            font-size: 0.82rem;
            font-weight: 700;
        }
        .metric-card {
            border: 1px solid var(--line);
            border-radius: 18px;
            padding: 0.95rem 1rem;
            background: var(--panel);
            min-height: 110px;
        }
        .metric-good { background: linear-gradient(180deg, rgba(85, 182, 94, 0.14), var(--panel)); }
        .metric-bad { background: linear-gradient(180deg, rgba(239, 77, 63, 0.14), var(--panel)); }
        .metric-neutral { background: var(--panel); }
        .metric-label {
            color: var(--muted);
            text-transform: uppercase;
            letter-spacing: 0.12em;
            font-size: 0.74rem;
            margin-bottom: 0.25rem;
        }
        .metric-value {
            font-size: 1.45rem;
            font-weight: 800;
            line-height: 1.15;
            color: var(--text);
        }
        .metric-sub {
            margin-top: 0.3rem;
            color: var(--muted);
            font-size: 0.86rem;
        }
        .mini-stat {
            border: 1px solid var(--line);
            border-radius: 18px;
            padding: 0.85rem 0.95rem;
            background: rgba(255, 255, 255, 0.9);
            box-shadow: 0 12px 26px rgba(69, 58, 56, 0.04);
        }
        .mini-stat span {
            display: block;
            color: var(--muted);
            font-size: 0.72rem;
            text-transform: uppercase;
            letter-spacing: 0.12em;
            margin-bottom: 0.3rem;
        }
        .mini-stat strong {
            display: block;
            font-family: "Space Grotesk", sans-serif;
            font-size: 1.05rem;
            line-height: 1.25;
        }
        .mini-good { border-color: rgba(85, 182, 94, 0.22); background: rgba(85, 182, 94, 0.08); }
        .mini-bad { border-color: rgba(239, 77, 63, 0.22); background: rgba(239, 77, 63, 0.08); }
        .mini-neutral { border-color: rgba(214, 164, 33, 0.22); background: rgba(214, 164, 33, 0.08); }
        .section-card {
            border: 1px solid var(--line);
            border-radius: 20px;
            background: rgba(255, 255, 255, 0.9);
            padding: 1rem 1rem 0.9rem;
            box-shadow: 0 12px 26px rgba(69, 58, 56, 0.04);
        }
        .section-card-title {
            font-family: "Space Grotesk", sans-serif;
            font-weight: 700;
            font-size: 1.02rem;
            margin-bottom: 0.65rem;
            color: var(--text);
        }
        .section-card-body {
            color: var(--text);
            font-size: 0.95rem;
            line-height: 1.55;
            word-break: break-word;
        }
        .good { color: var(--good); }
        .bad { color: var(--bad); }
        .neutral { color: var(--warn); }
        div[data-testid="stSidebar"] {
            background: rgba(252, 250, 247, 0.98) !important;
            border-right: 1px solid var(--line);
        }
        div[data-testid="stSidebar"] *,
        div[data-testid="stSidebar"] label,
        div[data-testid="stSidebar"] p,
        div[data-testid="stSidebar"] span,
        div[data-testid="stSidebar"] div[data-testid="stWidgetLabel"] {
            color: #1f2937 !important;
        }
        .stDataFrame {
            border-radius: 18px;
            overflow: hidden;
            border: 1px solid var(--line);
        }
        .strike-pill {
            display: inline-flex;
            align-items: center;
            gap: 0.4rem;
            flex-wrap: wrap;
        }
        .strike-pill span {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            padding: 0.35rem 0.65rem;
            border-radius: 999px;
            border: 1px solid var(--line);
            background: rgba(255, 255, 255, 0.92);
            margin: 0.18rem 0.25rem 0 0;
            font-size: 0.82rem;
            font-weight: 700;
        }
        div[data-testid="stToolbar"] {
            visibility: hidden;
        }
        div[data-testid="stSidebar"] [role="radiogroup"] {
            gap: 0.5rem;
        }
        div[data-testid="stSidebar"] [role="radio"] {
            border-radius: 999px;
        }
        div[data-testid="stSidebar"] [data-baseweb="select"] > div {
            border-color: var(--line) !important;
            background: rgba(255, 255, 255, 0.95) !important;
        }
        div[data-testid="stSidebar"] input,
        div[data-testid="stSidebar"] textarea {
            background: rgba(255, 255, 255, 0.95) !important;
            border-color: var(--line) !important;
        }
        button[kind="primary"] {
            background: linear-gradient(135deg, var(--accent), #de5645) !important;
            border: 1px solid rgba(198, 63, 47, 0.34) !important;
            color: white !important;
            box-shadow: 0 12px 26px rgba(198, 63, 47, 0.16) !important;
        }
        button[kind="secondary"] {
            background: rgba(255, 255, 255, 0.92) !important;
            border: 1px solid var(--line) !important;
            color: var(--text) !important;
        }
        .stDataFrame thead th {
            color: var(--muted) !important;
        }
        .stDataFrame tbody td {
            color: var(--text) !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_sidebar() -> dict[str, Any]:
    with st.sidebar:
        st.title("Trending OI")
        st.caption("FYERS-backed option flow desk")
        mode = st.radio("Mode", ["live", "historical"], horizontal=True, index=0)
        symbol = st.selectbox("Name", list(SYMBOLS.keys()), index=0)
        selected_date = st.date_input("Date", value=today_ist())
        expiry_date = st.date_input("Expiry Date", value=next_weekly_expiry())
        interval_label = st.selectbox("Time Interval", list(INTERVALS.keys()), index=2)
        strike_span = st.selectbox("Strike Span", [7, 9, 11, 13, 15, 17, 19], index=4)
        refresh_seconds = st.slider("Refresh Seconds", 15, 300, 60, 15)
        auto_refresh = st.checkbox("Auto refresh live data", value=True)
        st.markdown("---")
        scan_nifty50 = st.checkbox("Scan Nifty 50 OBs", value=True)
        scan_lookback_days = st.slider("OB Lookback Days", 2, 8, 5, 1)
        scan_workers = st.slider("OB Scan Workers", 2, 10, 6, 1)
        scan_now = st.button("Run Nifty 50 Scan", use_container_width=True)
        run = st.button("Go", use_container_width=True)
        reset_strikes = st.button("Change Strike Prices", use_container_width=True)
        st.markdown("---")
        st.caption(f"FYERS source: `{fyers_credentials_source()}`")
        st.caption(f"Backup: `{storage_source()}`")
    return {
        "mode": mode,
        "symbol": symbol,
        "selected_date": selected_date.isoformat(),
        "expiry_date": expiry_date.isoformat(),
        "interval_label": interval_label,
        "strike_span": int(strike_span),
        "refresh_seconds": int(refresh_seconds),
        "auto_refresh": auto_refresh,
        "scan_nifty50": scan_nifty50,
        "scan_lookback_days": int(scan_lookback_days),
        "scan_workers": int(scan_workers),
        "scan_now": scan_now,
        "run": run,
        "reset_strikes": reset_strikes,
    }


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    inject_style()

    controls = render_sidebar()
    symbol_cfg = SYMBOLS[controls["symbol"]]
    interval_minutes = INTERVALS[controls["interval_label"]]
    client: FyersDataClient | None = None

    if "selected_strikes" not in st.session_state:
        st.session_state.selected_strikes = []
    if "last_spot" not in st.session_state:
        st.session_state.last_spot = 0.0
    if "last_snapshot" not in st.session_state:
        st.session_state.last_snapshot = None

    if controls["auto_refresh"] and controls["mode"] == "live" and st_autorefresh is not None:
        st_autorefresh(interval=controls["refresh_seconds"] * 1000, key="trending_oi_auto_refresh")

    if controls["reset_strikes"]:
        st.session_state.selected_strikes = []

    st.markdown(
        """
        <div class="hero">
          <div style="display:flex;justify-content:space-between;gap:1rem;flex-wrap:wrap;align-items:end;">
            <div>
              <div style="text-transform:uppercase;letter-spacing:0.18em;font-size:0.72rem;color:#bc4c37;font-weight:800;">FYERS option flow desk</div>
              <h1>Trending OI</h1>
              <p>Live and historical analysis with strike selection, directional OI drift, and Supabase backup.</p>
            </div>
            <div style="display:flex;flex-wrap:wrap;gap:0.65rem;">
              <span class="pill">Mode: {mode}</span>
              <span class="pill">{symbol}</span>
              <span class="pill">{interval} min</span>
            </div>
          </div>
        </div>
        """.format(mode=controls["mode"].title(), symbol=controls["symbol"], interval=interval_minutes),
        unsafe_allow_html=True,
    )

    if controls["mode"] == "live":
        try:
            client = FyersDataClient.from_env()
            spot, options_chain = fetch_option_chain(client, symbol_cfg["symbol"], controls["strike_span"])
            st.session_state.last_spot = spot
            if not st.session_state.selected_strikes:
                st.session_state.selected_strikes = generate_strikes(spot, symbol_cfg["step"], controls["strike_span"])
            frame = build_strike_frame(options_chain, st.session_state.selected_strikes)
            today_key = today_ist().isoformat()
            previous = load_previous_snapshot(controls["symbol"], controls["expiry_date"], interval_minutes, "live", snapshot_date=today_key)
            snapshot = summarize_snapshot(
                symbol_key=controls["symbol"],
                symbol_label=symbol_cfg["label"],
                expiry_date=controls["expiry_date"],
                interval_minutes=interval_minutes,
                strike_step=symbol_cfg["step"],
                strike_span=controls["strike_span"],
                spot=spot,
                frame=frame,
                previous=previous,
            )
            store_snapshot(snapshot)
            st.session_state.last_snapshot = snapshot
            records = load_rows(
                controls["symbol"],
                controls["expiry_date"],
                interval_minutes,
                "live",
                snapshot_date=today_key,
                limit=500,
            )
            view = build_history_table(records, interval_minutes)
        except Exception as exc:
            st.error(str(exc))
            st.stop()
    else:
        try:
            records = load_rows(
                controls["symbol"],
                controls["expiry_date"],
                interval_minutes,
                "live",
                snapshot_date=controls["selected_date"],
                limit=500,
            )
            if records.empty:
                st.info("No cached snapshots yet for the selected date. Switch to Live data and press Go once.")
                view = pd.DataFrame()
                snapshot = None
            else:
                snapshot = records.iloc[-1].to_dict()
                view = build_history_table(records, interval_minutes)
        except Exception as exc:
            st.error(str(exc))
            st.stop()

    if snapshot:
        selected = parse_json_field(snapshot.get("selected_strikes_json")) or st.session_state.selected_strikes
        st.session_state.selected_strikes = selected
        st.session_state.last_spot = safe_float(snapshot.get("underlying_ltp"))
        st.session_state.last_snapshot = snapshot

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            metric_card(
                "Underlying",
                fmt_num(snapshot.get("underlying_ltp"), 2),
                f"Chg {snapshot.get('underlying_change', 0) >= 0 and '+' or ''}{fmt_num(snapshot.get('underlying_change', 0), 2)} | {fmt_num(snapshot.get('underlying_change_pct', 0), 2)}%",
                "good" if safe_float(snapshot.get("underlying_change")) >= 0 else "bad",
            )
        with c2:
            metric_card(
                "Day Range",
                f"{fmt_num(snapshot.get('session_low', 0), 2)} - {fmt_num(snapshot.get('session_high', 0), 2)}",
                strip_none(snapshot.get("day_hl_break"), "-"),
                "neutral",
            )
        with c3:
            metric_card(
                "Net PCR",
                fmt_num(snapshot.get("net_pcr"), 2),
                f"Session open: {fmt_num(snapshot.get('session_open', 0), 2)}",
                "neutral",
            )
        with c4:
            metric_card(
                "OI Strength",
                f"{snapshot.get('strength_pct', 0) >= 0 and '+' or ''}{fmt_num(snapshot.get('strength_pct', 0), 0)}%",
                f"Updated: {snapshot.get('snapshot_minute', '-')}",
                "good" if safe_float(snapshot.get("strength_pct")) >= 0 else "bad",
            )

        summary_left, summary_mid, summary_right = st.columns([1.2, 1.0, 1.0])
        with summary_left:
            mini_stat("Symbol", f"{controls['symbol']} • {symbol_cfg['label']}", "neutral")
        with summary_mid:
            mini_stat("Expiry", controls["expiry_date"], "neutral")
        with summary_right:
            mini_stat("Storage", storage_source(), "neutral")

        st.markdown("### Trending OI")
        strike_html = " ".join(f"<span>{x}</span>" for x in selected) if selected else "<span>None</span>"
        st.markdown(f"<div class='strike-pill'>{strike_html}</div>", unsafe_allow_html=True)

        table_height = 240 if view.empty else min(720, 120 + max(len(view), 1) * 42)
        st.dataframe(
            style_session_table(view),
            use_container_width=True,
            height=table_height,
            hide_index=True,
            column_config={
                "#": st.column_config.NumberColumn("#", width="small"),
                "Date Time": st.column_config.TextColumn("Date Time", width="medium"),
                "Total OI": st.column_config.TextColumn("Total OI", width="small"),
                "Total Chng. In OI": st.column_config.TextColumn("Total Chng. In OI", width="medium"),
                "Day High": st.column_config.TextColumn("Day High", width="small"),
                "Day Low": st.column_config.TextColumn("Day Low", width="small"),
                "Level Break": st.column_config.TextColumn("Level Break", width="large"),
                "Volume": st.column_config.TextColumn("Volume", width="small"),
                "LTP": st.column_config.TextColumn("LTP", width="small"),
                "LTP Change": st.column_config.TextColumn("LTP Change", width="small"),
                "OI Change": st.column_config.TextColumn("OI Change", width="small"),
            },
        )

        with st.expander("Snapshot details", expanded=False):
            info_cols = st.columns(3)
            with info_cols[0]:
                mini_stat("Mode", controls["mode"].title(), "neutral")
                mini_stat("Interval", controls["interval_label"], "neutral")
            with info_cols[1]:
                mini_stat("Rows", str(int(len(view))), "neutral")
                mini_stat("Credentials", fyers_credentials_source(), "neutral")
            with info_cols[2]:
                mini_stat("Spot", fmt_num(snapshot.get("underlying_ltp"), 2), "good" if safe_float(snapshot.get("underlying_change")) >= 0 else "bad")
                mini_stat("Change", f"{fmt_signed_num(snapshot.get('underlying_change'), 2)} ({fmt_pct(snapshot.get('underlying_change_pct'), 2)})", "good" if safe_float(snapshot.get("underlying_change")) >= 0 else "bad")
            st.markdown("#### Underlying Totals")
            t1, t2, t3 = st.columns(3)
            with t1:
                mini_stat("CE OI", fmt_num(snapshot.get("total_ce_oi"), 0), "neutral")
            with t2:
                mini_stat("PE OI", fmt_num(snapshot.get("total_pe_oi"), 0), "neutral")
            with t3:
                mini_stat("Net PCR", fmt_num(snapshot.get("net_pcr"), 2), "neutral")

        if controls["scan_nifty50"]:
            st.markdown("### Nifty 50 OB Scanner")
            scan_meta_left, scan_meta_mid, scan_meta_right = st.columns(3)
            with scan_meta_left:
                mini_stat("Universe", "NIFTY 50", "neutral")
            with scan_meta_mid:
                mini_stat("Candles", "3 min", "neutral")
            with scan_meta_right:
                mini_stat("Mode", "Live scan", "neutral")

            should_scan = controls["scan_now"] or ("nifty50_ob_scan" not in st.session_state)
            if should_scan:
                with st.spinner("Scanning Nifty 50 stocks for bullish and bearish order blocks..."):
                    if client is None:
                        client = FyersDataClient.from_env()
                    st.session_state.nifty50_ob_scan = scan_nifty50_order_blocks(
                        client,
                        resolution="3",
                        lookback_days=controls["scan_lookback_days"],
                        max_workers=controls["scan_workers"],
                    )

            scan_results = st.session_state.get("nifty50_ob_scan", {"bullish": [], "bearish": [], "errors": []})
            bull_df = pd.DataFrame(scan_results.get("bullish", []))
            bear_df = pd.DataFrame(scan_results.get("bearish", []))
            error_count = len(scan_results.get("errors", []))

            counts_cols = st.columns(4)
            with counts_cols[0]:
                metric_card("Bullish OBs", str(len(bull_df)), "Stocks whose latest OB is bullish", "good")
            with counts_cols[1]:
                metric_card("Bearish OBs", str(len(bear_df)), "Stocks whose latest OB is bearish", "bad")
            with counts_cols[2]:
                metric_card("No OB", str(max(0, 50 - len(bull_df) - len(bear_df))), "No active OB found", "neutral")
            with counts_cols[3]:
                metric_card("Scan Errors", str(error_count), "Fetch issues / missing candles", "neutral" if error_count == 0 else "bad")

            left_col, right_col = st.columns(2, gap="large")
            with left_col:
                st.markdown("#### Bullish OBs")
                if bull_df.empty:
                    st.info("No stocks currently show a latest bullish OB.")
                else:
                    st.dataframe(
                        style_ob_scan_table(bull_df.head(15), "bullish"),
                        use_container_width=True,
                        hide_index=True,
                        height=min(650, 140 + len(bull_df.head(15)) * 44),
                    )
            with right_col:
                st.markdown("#### Bearish OBs")
                if bear_df.empty:
                    st.info("No stocks currently show a latest bearish OB.")
                else:
                    st.dataframe(
                        style_ob_scan_table(bear_df.head(15), "bearish"),
                        use_container_width=True,
                        hide_index=True,
                        height=min(650, 140 + len(bear_df.head(15)) * 44),
                    )
    else:
        st.warning("No snapshot loaded yet.")


if __name__ == "__main__":
    main()
