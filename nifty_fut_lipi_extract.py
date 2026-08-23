"""
nifty_fut_lipi_extract.py

Same verified LIPI replication logic as cdsl_lipi_extract.py, adapted for
NIFTY Futures (NSE_FO segment). Futures contracts roll over by expiry, so
this script auto-resolves the NEAREST unexpired NIFTY FUT contract instead
of using a fixed instrument key.

SETUP: same as before - UPSTOX_ACCESS_TOKEN env var or session var must be set.
"""

import os
import sys
import json
import gzip
from datetime import datetime, timedelta

import requests
import pandas as pd

# ---------------- Config ----------------
ACCESS_TOKEN = os.environ.get("UPSTOX_ACCESS_TOKEN", "")
SYMBOL_NAME = "NIFTY"
INTERVAL = "5minute"
UNIT = "minutes"
INTERVAL_VALUE = "5"
LOOKBACK_CALENDAR_DAYS = 18

# Verified inputs (matched to the actual GoCharting chart settings)
PERC = 1.0
MERGE_THRESHOLD = 0.002
DELTA_LOOKBACK = 100
THRESHOLD = 0.7
SMOOTH_LEN = 5
ZONE_WIDTH = 0.0005

FO_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE_FO.json.gz"


def resolve_nifty_fut_instrument_key() -> tuple[str, str]:
    """Use Upstox's authenticated Instrument Search API to find the current
    NIFTY futures contract (nearest unexpired month), avoiding the CDN-hosted
    instrument master file which blocks scripted/automated requests (403)."""
    url = "https://api.upstox.com/v2/instruments/search"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {ACCESS_TOKEN}",
    }
    params = {
        "query": "NIFTY",
        "exchanges": "NSE",
        "segments": "FO",
        "instrument_types": "FUT",
        "expiry": "current_month",
        "page_number": 1,
        "records": 30,
    }
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    payload = resp.json()

    candidates = [
        inst for inst in payload.get("data", [])
        if inst.get("instrument_type") == "FUT" and inst.get("underlying_symbol", "").upper() == SYMBOL_NAME
    ]
    if not candidates:
        raise RuntimeError(f"No NIFTY futures contract found. Raw response: {payload}")

    # Sort by expiry, pick the nearest (current month should already be nearest, but be safe)
    candidates.sort(key=lambda x: x["expiry"])
    nearest = candidates[0]
    print(f"Nearest NIFTY FUT contract: {nearest['trading_symbol']} (expiry {nearest['expiry']})")
    return nearest["instrument_key"], nearest["trading_symbol"]


def fetch_candles(instrument_key: str) -> pd.DataFrame:
    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=LOOKBACK_CALENDAR_DAYS)).strftime("%Y-%m-%d")

    url = (
        f"https://api.upstox.com/v3/historical-candle/"
        f"{instrument_key}/{UNIT}/{INTERVAL_VALUE}/{to_date}/{from_date}"
    )
    headers = {"Accept": "application/json"}
    if ACCESS_TOKEN:
        headers["Authorization"] = f"Bearer {ACCESS_TOKEN}"

    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    payload = resp.json()

    candles = payload.get("data", {}).get("candles", [])
    if not candles:
        raise RuntimeError(f"No candle data returned. Raw response: {payload}")

    df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    df["date"] = df["timestamp"].dt.date
    return df


def replicate_lipi(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["volumeDelta"] = (df["close"] - df["open"]) * df["volume"]
    df["cumDelta"] = df["volumeDelta"].rolling(SMOOTH_LEN, min_periods=1).sum()
    df["maxDelta"] = df["cumDelta"].rolling(DELTA_LOOKBACK, min_periods=1).max()
    df["minDelta"] = df["cumDelta"].rolling(DELTA_LOOKBACK, min_periods=1).min()
    df["rangeDelta"] = df["maxDelta"] - df["minDelta"]
    df["isStrongBuy"] = df["cumDelta"] > (df["minDelta"] + df["rangeDelta"] * THRESHOLD)
    df["isStrongSell"] = df["cumDelta"] < (df["maxDelta"] - df["rangeDelta"] * THRESHOLD)

    results = []
    prevVAH = prevVAL = prevPOC = float("nan")
    todayVAH = todayVAL = todayPOC = float("nan")
    prevSupport = prevResistance = float("nan")
    last_date = None

    for _, row in df.iterrows():
        d = row["date"]
        is_new_day = d != last_date
        if is_new_day:
            last_date = d

            prevVAH, prevVAL, prevPOC = todayVAH, todayVAL, todayPOC

            sumPV = row["close"] * row["volume"]
            sumVol = row["volume"]
            dayHigh = row["high"]
            dayLow = row["low"]

            dVWAP = sumPV / sumVol if sumVol else float("nan")
            dRange = dayHigh - dayLow
            halfRange = dRange * (PERC * 0.5)

            todayPOC = dVWAP
            todayVAH = dVWAP + halfRange
            todayVAL = dVWAP - halfRange

            if not pd.isna(prevVAH) and prevVAH != 0 and abs(todayVAH - prevVAH) / prevVAH < MERGE_THRESHOLD:
                todayVAH = (todayVAH + prevVAH) / 2
            if not pd.isna(prevVAL) and prevVAL != 0 and abs(todayVAL - prevVAL) / prevVAL < MERGE_THRESHOLD:
                todayVAL = (todayVAL + prevVAL) / 2
            if not pd.isna(prevPOC) and prevPOC != 0 and abs(todayPOC - prevPOC) / prevPOC < MERGE_THRESHOLD:
                todayPOC = (todayPOC + prevPOC) / 2

            if row["isStrongBuy"]:
                prevSupport = row["low"]
            if row["isStrongSell"]:
                prevResistance = row["high"]

            results.append({
                "date": d,
                "POC": round(todayPOC, 2) if not pd.isna(todayPOC) else None,
                "VAH": round(todayVAH, 2) if not pd.isna(todayVAH) else None,
                "VAL": round(todayVAL, 2) if not pd.isna(todayVAL) else None,
                "DeltaSupport": round(prevSupport, 2) if not pd.isna(prevSupport) else None,
                "DeltaResistance": round(prevResistance, 2) if not pd.isna(prevResistance) else None,
            })

    return pd.DataFrame(results)


def add_breakout_signal(result: pd.DataFrame, df: pd.DataFrame) -> pd.DataFrame:
    """Add each day's closing price and test the rule:
    close > POC AND close > the DELTA RESISTANCE LEVEL AS IT STOOD BEFORE
    THAT DAY (i.e. the prior row's value, not same-day updates) - this
    avoids testing a signal against a level that only became what it is
    because of that same day's own price action."""
    result = result.copy()

    # Last close of each trading day
    daily_close = df.groupby("date")["close"].last().rename("Close")
    result = result.merge(daily_close, left_on="date", right_index=True, how="left")

    # Prior-day delta resistance/support (shifted by 1 row = level in effect
    # BEFORE this day started)
    result["PriorDayDeltaResistance"] = result["DeltaResistance"].shift(1)
    result["PriorDayDeltaSupport"] = result["DeltaSupport"].shift(1)

    result["BreakoutAboveResistance"] = (
        (result["Close"] > result["POC"]) &
        (result["Close"] > result["PriorDayDeltaResistance"])
    )
    result["BreakdownBelowSupport"] = (
        (result["Close"] < result["POC"]) &
        (result["Close"] < result["PriorDayDeltaSupport"])
    )

    return result


def main():
    if not ACCESS_TOKEN:
        print("ERROR: No Upstox access token found. Set UPSTOX_ACCESS_TOKEN env var, e.g.:")
        print('  $env:UPSTOX_ACCESS_TOKEN = "your_token_here"')
        sys.exit(1)

    print("Resolving nearest NIFTY futures contract ...")
    instrument_key, trading_symbol = resolve_nifty_fut_instrument_key()
    print(f"Using instrument key: {instrument_key} ({trading_symbol})")

    print("Fetching 5-minute candles ...")
    df = fetch_candles(instrument_key)
    print(f"Fetched {len(df)} candles spanning {df['date'].min()} to {df['date'].max()}")

    result = replicate_lipi(df)
    result = add_breakout_signal(result, df)
    last_10 = result.tail(10)

    print("\n=== Daily Value Area + Delta Zone levels (last 10 trading days) ===")
    print(last_10[["date", "POC", "VAH", "VAL", "DeltaSupport", "DeltaResistance"]].to_string(index=False))

    print("\n=== Breakout Signal Test: close > POC AND close > prior-day Delta Resistance ===")
    print(last_10[["date", "Close", "POC", "PriorDayDeltaResistance", "BreakoutAboveResistance",
                    "PriorDayDeltaSupport", "BreakdownBelowSupport"]].to_string(index=False))

    out_path = "nifty_fut_lipi_extract_output.csv"
    last_10.to_csv(out_path, index=False)
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
