"""
fno_levels_precompute.py

PHASE 1 of 2 - run this ONCE per day (any time after ~9:20 AM, once today's
first 5-min candle exists).

Fetches historical 5-min candles per symbol (unavoidably one-by-one - there's
no batch historical-candle endpoint), computes:
  - POC (today, verified LIPI logic)
  - Delta Support / Delta Resistance (as they stood BEFORE today)
  - RVOL baseline: average cumulative volume at each 5-min time-of-day,
    across the last 10 trading days
  - Yesterday's close (to know if already trading above both levels)

...and caches everything to fno_levels_cache.json. This is the slow part,
but it only needs to run once - after that, fno_live_scanner.py uses this
cache plus a single fast batch quote call to refresh live status in seconds.

SETUP: $env:UPSTOX_ACCESS_TOKEN = "your_token_here"
"""

import os
import sys
import json
import time
from datetime import datetime, timedelta

import requests
import pandas as pd

ACCESS_TOKEN = os.environ.get("UPSTOX_ACCESS_TOKEN", "")
UNIT = "minutes"
INTERVAL_VALUE = "5"
LOOKBACK_CALENDAR_DAYS = 18
REQUEST_DELAY_SECONDS = 0.25

PERC = 1.0
MERGE_THRESHOLD = 0.002
DELTA_LOOKBACK = 100
THRESHOLD = 0.7
SMOOTH_LEN = 5

FO_CSV_LOCAL_PATH = "fo_mktlots.csv"
CACHE_PATH = "fno_levels_cache.json"
STATE_PATH = "fno_crossed_state.json"

DEFAULT_FNO_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "SBIN", "AXISBANK",
    "KOTAKBANK", "BAJFINANCE", "BHARTIARTL", "ITC", "LT", "HINDUNILVR",
    "MARUTI", "TATAMOTORS", "TATASTEEL", "SUNPHARMA", "TITAN", "ULTRACEMCO",
    "ASIANPAINT", "WIPRO", "NTPC", "POWERGRID", "M&M", "ADANIENT",
    "ADANIPORTS", "BAJAJFINSV", "HCLTECH", "JSWSTEEL", "ONGC", "COALINDIA",
    "TECHM", "GRASIM", "DIVISLAB", "DRREDDY", "CIPLA", "EICHERMOT",
    "HEROMOTOCO", "HINDALCO", "BPCL", "BRITANNIA", "APOLLOHOSP", "SBILIFE",
    "HDFCLIFE", "INDUSINDBK", "BAJAJ-AUTO", "TATACONSUM", "UPL", "SHREECEM",
    "NESTLEIND", "VEDANTA", "GAIL", "PIDILITIND", "DLF", "GODREJCP",
    "SIEMENS", "AMBUJACEM", "BANDHANBNK", "BANKBARODA", "PNB", "CANBK",
    "IDFCFIRSTB", "FEDERALBNK", "AUROPHARMA", "BEL", "BIOCON", "CHOLAFIN",
    "COLPAL", "CONCOR", "CUMMINSIND", "DABUR", "DEEPAKNTR", "ESCORTS",
    "EXIDEIND", "GODREJPROP", "HAVELLS", "HDFCAMC", "ICICIGI", "ICICIPRULI",
    "IEX", "INDIGO", "INDUSTOWER", "IOC", "IRCTC", "JINDALSTEL", "JUBLFOOD",
    "LICHSGFIN", "LTIM", "LUPIN", "MANAPPURAM", "MARICO", "MCDOWELL-N",
    "MFSL", "MOTHERSON", "MPHASIS", "MRF", "MUTHOOTFIN", "NAUKRI",
    "NMDC", "OBEROIRLTY", "OFSS", "PAGEIND", "PEL", "PERSISTENT",
    "PETRONET", "PFC", "PIIND", "POLYCAB", "RECLTD", "SAIL", "SBICARD",
    "SRF", "SYNGENE", "TATACOMM", "TATAPOWER", "TORNTPHARM", "TRENT",
    "TVSMOTOR", "UBL", "VOLTAS", "ZEEL", "ZYDUSLIFE", "CDSL", "IRFC",
    "IDEA", "YESBANK", "SUZLON", "ZOMATO", "DMART", "JIOFIN", "PAYTM",
    "NYKAA", "POLICYBZR", "DELHIVERY", "LODHA", "PATANJALI", "ABCAPITAL",
    "ALKEM", "APLAPOLLO", "ASHOKLEY", "ASTRAL", "ATUL", "BALKRISNIND",
    "BATAINDIA", "BHARATFORG", "BHEL", "BSOFT", "CANFINHOME", "CROMPTON",
    "CUB", "DALBHARAT", "GLENMARK", "GMRINFRA", "GNFC", "GRANULES",
    "GUJGASLTD", "HAL", "HINDCOPPER", "HINDPETRO", "IBULHSGFIN", "IGL",
    "INDHOTEL", "INDIAMART", "IPCALAB", "JKCEMENT", "L&TFH", "LALPATHLAB",
    "LAURUSLABS", "M&MFIN", "METROPOLIS", "NATIONALUM", "NAVINFLUOR",
    "OIL", "PVRINOX", "RAIN", "RBLBANK", "SUNTV", "TATACHEM",
    "TATAELXSI", "TORNTPOWER", "UNIONBANK", "VBL", "WHIRLPOOL",
]

FO_INSTRUMENT_SEARCH_URL = "https://api.upstox.com/v2/instruments/search"


def load_symbol_universe():
    if os.path.exists(FO_CSV_LOCAL_PATH):
        try:
            df = pd.read_csv(FO_CSV_LOCAL_PATH, skiprows=4, header=None)
            symbols = df[1].astype(str).str.strip().unique().tolist()
            symbols = [s for s in symbols if s and s.upper() not in ("NIFTY", "BANKNIFTY", "FINNIFTY")]
            print(f"Loaded {len(symbols)} symbols from local {FO_CSV_LOCAL_PATH}")
            return symbols
        except Exception as e:
            print(f"[warn] Could not parse {FO_CSV_LOCAL_PATH} ({e}); falling back to built-in list.")
    print(f"Using built-in starter list of {len(DEFAULT_FNO_SYMBOLS)} F&O symbols.")
    return DEFAULT_FNO_SYMBOLS


def resolve_equity_instrument_key(symbol):
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Authorization": f"Bearer {ACCESS_TOKEN}"}
    params = {"query": symbol, "exchanges": "NSE", "segments": "EQ", "page_number": 1, "records": 10}
    resp = requests.get(FO_INSTRUMENT_SEARCH_URL, headers=headers, params=params, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    for inst in payload.get("data", []):
        if inst.get("trading_symbol", "").upper() == symbol.upper() and inst.get("instrument_type") == "EQ":
            return inst["instrument_key"]
    return None


def fetch_candles(instrument_key):
    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=LOOKBACK_CALENDAR_DAYS)).strftime("%Y-%m-%d")
    url = f"https://api.upstox.com/v3/historical-candle/{instrument_key}/{UNIT}/{INTERVAL_VALUE}/{to_date}/{from_date}"
    headers = {"Accept": "application/json", "Authorization": f"Bearer {ACCESS_TOKEN}"}
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


def compute_levels_and_baseline(df):
    df = df.copy()
    df["volumeDelta"] = (df["close"] - df["open"]) * df["volume"]
    df["cumDelta"] = df["volumeDelta"].rolling(SMOOTH_LEN, min_periods=1).sum()
    df["maxDelta"] = df["cumDelta"].rolling(DELTA_LOOKBACK, min_periods=1).max()
    df["minDelta"] = df["cumDelta"].rolling(DELTA_LOOKBACK, min_periods=1).min()
    df["rangeDelta"] = df["maxDelta"] - df["minDelta"]
    df["isStrongBuy"] = df["cumDelta"] > (df["minDelta"] + df["rangeDelta"] * THRESHOLD)
    df["isStrongSell"] = df["cumDelta"] < (df["maxDelta"] - df["rangeDelta"] * THRESHOLD)

    todayVAH = todayVAL = todayPOC = float("nan")
    prevVAH = prevVAL = prevPOC = float("nan")
    prevSupport = prevResistance = float("nan")
    support_before_today = resistance_before_today = float("nan")
    last_date = None

    for _, row in df.iterrows():
        d = row["date"]
        if d != last_date:
            support_before_today = prevSupport
            resistance_before_today = prevResistance
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

    if df.empty:
        return None

    today = df["date"].max()
    today_df = df[df["date"] == today].sort_values("timestamp")
    prior_days_df = df[df["date"] < today]
    prior_days = sorted(prior_days_df["date"].unique())

    yesterday_close = prior_days_df["close"].iloc[-1] if not prior_days_df.empty else float("nan")
    already_above_yesterday = (
        not pd.isna(support_before_today) and not pd.isna(resistance_before_today) and
        not pd.isna(yesterday_close) and
        yesterday_close > support_before_today and yesterday_close > resistance_before_today
    )

    # Detect the TRUE crossover time from today's already-fetched intraday
    # data (if any), so live polling doesn't have to guess/default to "now"
    # on its first run of the day.
    crossover_time_str = None
    has_prior_levels = not pd.isna(support_before_today) and not pd.isna(resistance_before_today)
    if has_prior_levels and not already_above_yesterday:
        prev_above = False
        for _, row in today_df.iterrows():
            above_now = row["close"] > support_before_today and row["close"] > resistance_before_today
            if above_now and not prev_above:
                crossover_time_str = row["timestamp"].strftime("%H:%M")
                break
            prev_above = above_now

    # RVOL baseline: average cumulative volume at each 5-min time-of-day,
    # across the last 10 available trading days
    baseline_days = prior_days[-10:]
    rvol_baseline = {}
    if baseline_days:
        for d in baseline_days:
            day_df = df[df["date"] == d].sort_values("timestamp")
            cum_vol = 0
            for _, row in day_df.iterrows():
                cum_vol += row["volume"]
                t_str = row["timestamp"].strftime("%H:%M")
                rvol_baseline.setdefault(t_str, []).append(cum_vol)
        rvol_baseline = {t: sum(v) / len(v) for t, v in rvol_baseline.items()}

    return {
        "poc": None if pd.isna(todayPOC) else round(todayPOC, 2),
        "delta_support": None if pd.isna(support_before_today) else round(support_before_today, 2),
        "delta_resistance": None if pd.isna(resistance_before_today) else round(resistance_before_today, 2),
        "yesterday_close": None if pd.isna(yesterday_close) else round(yesterday_close, 2),
        "already_above_yesterday": bool(already_above_yesterday),
        "crossover_time": crossover_time_str,
        "rvol_baseline": rvol_baseline,
        "computed_date": str(today),
    }


def main():
    if not ACCESS_TOKEN:
        print("ERROR: No Upstox access token found. Set it first:")
        print('  $env:UPSTOX_ACCESS_TOKEN = "your_token_here"')
        sys.exit(1)

    symbols = load_symbol_universe()
    cache = {}

    for i, symbol in enumerate(symbols, start=1):
        try:
            instrument_key = resolve_equity_instrument_key(symbol)
            if not instrument_key:
                print(f"[{i}/{len(symbols)}] {symbol}: instrument key not found, skipping")
                continue
            time.sleep(REQUEST_DELAY_SECONDS)

            df = fetch_candles(instrument_key)
            time.sleep(REQUEST_DELAY_SECONDS)
            if df.empty:
                print(f"[{i}/{len(symbols)}] {symbol}: no candle data, skipping")
                continue

            levels = compute_levels_and_baseline(df)
            if levels is None:
                continue

            levels["instrument_key"] = instrument_key
            cache[symbol] = levels
            print(f"[{i}/{len(symbols)}] {symbol}: cached "
                  f"(POC={levels['poc']}, Support={levels['delta_support']}, Resistance={levels['delta_resistance']})")

        except Exception as e:
            print(f"[{i}/{len(symbols)}] {symbol}: ERROR - {e}")
            continue

    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)

    # Seed the live-scanner's state file with TRUE crossover times/status
    # detected from today's historical intraday data, so fno_live_scanner.py
    # doesn't default everything to "just crossed now" on its first run.
    today_str = datetime.now().strftime("%Y-%m-%d")
    state = {"_date": today_str}
    for symbol, levels in cache.items():
        if levels.get("already_above_yesterday"):
            state[symbol] = {"status": "continuing"}
        elif levels.get("crossover_time"):
            state[symbol] = {"status": "crossed", "time": levels["crossover_time"]}
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)

    print(f"\nCached levels for {len(cache)} symbols to {CACHE_PATH}")
    print(f"Seeded {STATE_PATH} with {len(state) - 1} known crossover states from today's history so far.")
    print("Now run fno_live_scanner.py anytime during the day for fast live updates.")


if __name__ == "__main__":
    main()
