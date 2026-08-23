"""
cdsl_lipi_extract.py

Replicates the LIPI script "Merged Value Areas with Volume Delta Zones" logic
on CDSL 5-minute candles for the last ~10 trading days, so we can compare
the output against what GoCharting plots and verify the logic matches.

IMPORTANT: This replicates the LIPI logic EXACTLY AS WRITTEN, including the
two issues we flagged earlier:
  1. sumPV/sumVol/dayHigh/dayLow reset to only the FIRST bar of the new day
     (not accumulated across the prior full session) -> POC/VAH/VAL are
     effectively based on the day's opening bar + previous day's range, not
     a true running VWAP.
  2. isStrongBuy/isStrongSell condition polarity is as written in the script
     (strong buy = cumDelta near the LOW end of its range).

If GoCharting's actual plotted values differ from this output, that tells us
whether GoCharting's LIPI runtime has different semantics for `static` vars
than assumed here (e.g. resets differently), which we can then adjust.

SETUP REQUIRED BEFORE RUNNING:
  1. pip install requests pandas
  2. Set your Upstox access token as an environment variable:
       setx UPSTOX_ACCESS_TOKEN "your_token_here"      (Windows, then reopen terminal)
     OR just paste it into ACCESS_TOKEN below temporarily (don't commit it / don't paste it back in chat).
  3. Confirm CDSL's instrument key below is correct (NSE_EQ|INE736A01011 is
     CDSL's ISIN-based instrument key as of last known mapping) — the script
     will also try to auto-resolve it from the instruments master as a check.
"""

import os
import sys
import json
import gzip
import io
from datetime import datetime, timedelta

import requests
import pandas as pd

# ---------------- Config ----------------
ACCESS_TOKEN = os.environ.get("UPSTOX_ACCESS_TOKEN", "")  # or paste token string here temporarily
SYMBOL = "CDSL"
FALLBACK_INSTRUMENT_KEY = "NSE_EQ|INE736A01011"  # CDSL - verified against instrument master below
INTERVAL = "5minute"          # Upstox v3 interval string
UNIT = "minutes"
INTERVAL_VALUE = "5"
LOOKBACK_CALENDAR_DAYS = 18   # buffer so we reliably capture 10 trading days

# LIPI script inputs - matched EXACTLY to the values applied on the live chart
# (confirmed via the Options panel screenshot, NOT the script's coded defaults)
PERC = 1.0
MERGE_THRESHOLD = 0.002
DELTA_LOOKBACK = 100
THRESHOLD = 0.7
SMOOTH_LEN = 5
ZONE_WIDTH = 0.0005

INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"


def resolve_instrument_key(symbol: str) -> str:
    """Download NSE instrument master and match on trading_symbol (not name),
    per the symbol-matching bug we hit before on Aug 19."""
    try:
        resp = requests.get(INSTRUMENTS_URL, timeout=30)
        resp.raise_for_status()
        data = json.loads(gzip.decompress(resp.content))
        for inst in data:
            if inst.get("trading_symbol", "").upper() == symbol.upper() and inst.get("instrument_type") == "EQ":
                return inst["instrument_key"]
    except Exception as e:
        print(f"[warn] Could not auto-resolve instrument key ({e}); using fallback.")
    return FALLBACK_INSTRUMENT_KEY


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

    # Upstox candle format: [timestamp, open, high, low, close, volume, oi]
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

    # LITERAL replication: static vars only change on explicit := assignment.
    # Since sumPV/sumVol/dayHigh/dayLow are only assigned inside `if isNewDay`,
    # they hold ONLY the first bar's values for that day (confirmed via
    # GoCharting docs: "static" = initialize once, persist until explicitly
    # reassigned - no auto-accumulation across bars).
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


def main():
    if not ACCESS_TOKEN:
        print("ERROR: No Upstox access token found. Set UPSTOX_ACCESS_TOKEN env var or paste into script.")
        sys.exit(1)

    print(f"Resolving instrument key for {SYMBOL} ...")
    instrument_key = resolve_instrument_key(SYMBOL)
    print(f"Using instrument key: {instrument_key}")

    print("Fetching 5-minute candles ...")
    df = fetch_candles(instrument_key)
    print(f"Fetched {len(df)} candles spanning {df['date'].min()} to {df['date'].max()}")

    result = replicate_lipi(df)
    last_10 = result.tail(10)

    print("\n=== Daily Value Area + Delta Zone levels (last 10 trading days) ===")
    print(last_10.to_string(index=False))

    out_path = "cdsl_lipi_extract_output.csv"
    last_10.to_csv(out_path, index=False)
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
