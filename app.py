from __future__ import annotations

import json
import os
import sqlite3
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


def render_row(record: pd.Series, idx: int) -> dict[str, Any]:
    timestamp = pd.to_datetime(record["snapshot_ts"], errors="coerce")
    if pd.isna(timestamp):
        timestamp = pd.Timestamp.now(tz=IST)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(IST)
    return {
        "#": idx,
        "Date": timestamp.strftime("%d-%m-%Y"),
        "Time": timestamp.strftime("%H:%M:%S"),
        "LTP": safe_float(record["underlying_ltp"]),
        "Day H/L Break": strip_none(record.get("day_hl_break"), "-"),
        "Chng. In Call OI": safe_float(record["ce_oi_change"]),
        "Chng. In Put OI": safe_float(record["pe_oi_change"]),
        "Diff. in OI": safe_float(record["diff_in_oi"]),
        "Strength": safe_float(record["strength_pct"]),
        "Direction of chng.": "↑" if safe_float(record["direction_change"]) > 0 else "↓" if safe_float(record["direction_change"]) < 0 else "→",
        "Chng. In Direction": safe_float(record["chg_in_direction"]),
        "Direction of chng. %": safe_float(record["direction_change_pct"]),
        "Net PCR": safe_float(record["net_pcr"]),
        "Day": timestamp.strftime("%d-%m-%Y"),
    }


def build_history_table(records: pd.DataFrame, interval_minutes: int) -> pd.DataFrame:
    if records.empty:
        return pd.DataFrame(columns=["#", "Date", "Time", "LTP", "Day H/L Break", "Chng. In Call OI", "Chng. In Put OI", "Diff. in OI", "Strength", "Direction of chng.", "Chng. In Direction", "Direction of chng. %", "Net PCR", "Day"])

    frame = records.sort_values("snapshot_ts").reset_index(drop=True)
    bucketed = bucket_rows(frame, interval_minutes)
    view = bucketed if len(bucketed) != len(frame) else frame
    rows = [render_row(row, idx + 1) for idx, (_, row) in enumerate(view.iterrows())]
    return pd.DataFrame(rows)


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


def inject_style() -> None:
    st.markdown(
        """
        <style>
        :root {
            --bg: #f7f2eb;
            --bg-2: #eef3f8;
            --panel: rgba(255, 255, 255, 0.84);
            --panel-strong: rgba(255, 255, 255, 0.95);
            --text: #1f2937;
            --muted: #6b7280;
            --line: rgba(31, 41, 55, 0.12);
            --good: #2f9e5b;
            --good-bg: rgba(47, 158, 91, 0.12);
            --bad: #d44949;
            --bad-bg: rgba(212, 73, 73, 0.12);
            --warn: #b88412;
            --warn-bg: rgba(184, 132, 18, 0.12);
        }
        .stApp {
            background:
                radial-gradient(circle at top left, rgba(188, 76, 55, 0.14), transparent 28%),
                radial-gradient(circle at top right, rgba(47, 158, 91, 0.12), transparent 26%),
                linear-gradient(180deg, var(--bg) 0%, var(--bg-2) 100%);
            color: var(--text);
        }
        .hero {
            padding: 1.1rem 1.2rem;
            border: 1px solid var(--line);
            border-radius: 22px;
            background: var(--panel-strong);
            box-shadow: 0 24px 70px rgba(15, 23, 42, 0.12);
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
            background: rgba(127, 127, 127, 0.06);
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
        .metric-good { background: linear-gradient(180deg, rgba(47, 158, 91, 0.16), var(--panel)); }
        .metric-bad { background: linear-gradient(180deg, rgba(212, 73, 73, 0.16), var(--panel)); }
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
        .good { color: var(--good); }
        .bad { color: var(--bad); }
        .neutral { color: var(--warn); }
        div[data-testid="stSidebar"] {
            background: rgba(248, 250, 252, 0.95) !important;
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
        "run": run,
        "reset_strikes": reset_strikes,
    }


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    inject_style()

    controls = render_sidebar()
    symbol_cfg = SYMBOLS[controls["symbol"]]
    interval_minutes = INTERVALS[controls["interval_label"]]

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

        top_left, top_right = st.columns([1.9, 1.1])
        with top_left:
            st.markdown("### Trending OI")
            selected_text = ", ".join(str(x) for x in selected) if selected else "-"
            st.caption(f"Selected Strike Prices: {selected_text}")
            st.dataframe(view, use_container_width=True, height=520, hide_index=True)
        with top_right:
            st.markdown("### Snapshot")
            st.write(
                {
                    "Symbol": controls["symbol"],
                    "Mode": controls["mode"],
                    "Expiry": controls["expiry_date"],
                    "Interval": controls["interval_label"],
                    "Storage": storage_source(),
                    "Rows": int(len(view)),
                    "Credentials": fyers_credentials_source(),
                }
            )
            st.markdown("### Underlying")
            st.write(
                {
                    "Spot": fmt_num(snapshot.get("underlying_ltp"), 2),
                    "Change": fmt_num(snapshot.get("underlying_change"), 2),
                    "Change %": fmt_pct(snapshot.get("underlying_change_pct"), 2),
                    "CE OI": fmt_num(snapshot.get("total_ce_oi"), 0),
                    "PE OI": fmt_num(snapshot.get("total_pe_oi"), 0),
                }
            )
    else:
        st.warning("No snapshot loaded yet.")


if __name__ == "__main__":
    main()
