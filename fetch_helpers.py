"""
fetch_helpers.py

Standalone copies of resolve_futures_instrument_key() and fetch_candles()
from fno_delta_dashboard.py, with every constant/import they depend on
inlined here. Deliberately has ZERO streamlit dependency and no top-level
app code, so it's safe to import from test scripts without executing (or
crashing inside) the dashboard itself.

If you change these functions in fno_delta_dashboard.py, mirror the change
here too - this is a copy, not a shared source of truth.
"""
from datetime import datetime, timedelta, timezone
import requests
import pandas as pd

IST = timezone(timedelta(hours=5, minutes=30))

UNIT = "minutes"
INTERVAL_VALUE = "5"
LOOKBACK_CALENDAR_DAYS = 18
INSTRUMENT_SEARCH_URL = "https://api.upstox.com/v2/instruments/search"


def now_ist():
    return datetime.now(IST)


def resolve_futures_instrument_key(name, token):
    """Resolve the nearest-expiry futures contract for an index (NIFTY,
    BANKNIFTY) via the Instrument Search API."""
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Authorization": f"Bearer {token}"}
    params = {
        "query": name, "exchanges": "NSE", "segments": "FO",
        "instrument_types": "FUT",
        "page_number": 1, "records": 30,
    }
    resp = requests.get(INSTRUMENT_SEARCH_URL, headers=headers, params=params, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    candidates = [
        inst for inst in payload.get("data", [])
        if inst.get("instrument_type") == "FUT" and inst.get("underlying_symbol", "").upper() == name.upper()
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda x: x["expiry"])
    return candidates[0]["instrument_key"]


def fetch_candles(instrument_key, token):
    to_date = now_ist().strftime("%Y-%m-%d")
    from_date = (now_ist() - timedelta(days=LOOKBACK_CALENDAR_DAYS)).strftime("%Y-%m-%d")
    url = f"https://api.upstox.com/v3/historical-candle/{instrument_key}/{UNIT}/{INTERVAL_VALUE}/{to_date}/{from_date}"
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    candles = payload.get("data", {}).get("candles", [])
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    df["date"] = df["timestamp"].dt.date
    return df
