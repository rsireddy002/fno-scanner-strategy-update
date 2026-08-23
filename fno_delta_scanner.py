"""
fno_delta_scanner.py

Scans the NSE F&O stock universe and reports, per symbol:
  - POC (verified LIPI logic - today's single-bar-based reference level)
  - VWAP (today's standard running intraday VWAP - separate from POC)
  - Delta Support level (sticky, locks on strong-buy days)
  - Delta Resistance level (sticky, locks on strong-sell days)
  - Status: "ABOVE BOTH" if currently trading above both delta levels,
    "JUST CROSSED" with the exact 5-min candle time if today is the day
    price crossed above both levels, or "-" otherwise.

SETUP: same as before - set UPSTOX_ACCESS_TOKEN for the session:
  $env:UPSTOX_ACCESS_TOKEN = "your_token_here"

SYMBOL LIST: Uses a built-in list of common NSE F&O stocks as a starting
point. NSE reviews the F&O list quarterly, so this list can drift out of
date. For an authoritative list, download the official CSV from
https://archives.nseindia.com/content/fo/fo_mktlots.csv via your browser
(NSE blocks scripted downloads), save it as 'fo_mktlots.csv' in this same
folder, and the script will use it automatically if present.
"""

import os
import sys
import time
from datetime import datetime, timedelta

import requests
import pandas as pd

# ---------------- Config ----------------
ACCESS_TOKEN = os.environ.get("UPSTOX_ACCESS_TOKEN", "")
INTERVAL = "5minute"
UNIT = "minutes"
INTERVAL_VALUE = "5"
LOOKBACK_CALENDAR_DAYS = 18
REQUEST_DELAY_SECONDS = 0.25  # be polite to the API between calls

# Verified LIPI inputs (matched to actual chart settings)
PERC = 1.0
MERGE_THRESHOLD = 0.002
DELTA_LOOKBACK = 100
THRESHOLD = 0.7
SMOOTH_LEN = 5

FO_CSV_LOCAL_PATH = "fo_mktlots.csv"

# Starter list of common NSE F&O stocks (edit/extend as needed, or provide
# fo_mktlots.csv for an authoritative list)
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
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {ACCESS_TOKEN}",
    }
    params = {
        "query": symbol,
        "exchanges": "NSE",
        "segments": "EQ",
        "page_number": 1,
        "records": 10,
    }
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


def compute_levels(df):
    """Returns (poc_today, delta_support_today, delta_resistance_today,
    delta_support_before_today, delta_resistance_before_today, vwap_today,
    crossover_time_or_None, last_close, today_df).

    delta_support/resistance_before_today = the level as it stood BEFORE
    today's session started (i.e. carried from the prior day), used for
    crossover testing to avoid comparing price against a level that was
    only just derived from today's own first candle (self-referential)."""
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
            # Snapshot the levels as they stood BEFORE this day's own update
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
    today_df = df[df["date"] == today].copy()

    # Standard running VWAP for today only (separate from POC)
    typical_price = (today_df["high"] + today_df["low"] + today_df["close"]) / 3
    today_df["cum_pv"] = (typical_price * today_df["volume"]).cumsum()
    today_df["cum_vol"] = today_df["volume"].cumsum()
    today_df["vwap"] = today_df["cum_pv"] / today_df["cum_vol"]
    vwap_today = today_df["vwap"].iloc[-1] if not today_df.empty else float("nan")
    last_close = today_df["close"].iloc[-1] if not today_df.empty else float("nan")

    # Determine if the stock was ALREADY above both levels at YESTERDAY's
    # close - if so, today's first candle being "above" is just continuation,
    # not a fresh crossover, and should not be flagged as JUST CROSSED.
    prior_days = df[df["date"] < today]
    yesterday_close = prior_days["close"].iloc[-1] if not prior_days.empty else float("nan")
    has_prior_levels = not pd.isna(support_before_today) and not pd.isna(resistance_before_today)
    already_above_yesterday = (
        has_prior_levels and not pd.isna(yesterday_close) and
        yesterday_close > support_before_today and yesterday_close > resistance_before_today
    )

    # Find first candle today where close crosses above BOTH support and
    # resistance AS THEY STOOD BEFORE TODAY (not same-day-updated values) -
    # only meaningful if it WASN'T already above at yesterday's close
    crossover_time = None
    if has_prior_levels and not already_above_yesterday:
        prev_above = False
        for _, row in today_df.iterrows():
            above_now = (row["close"] > support_before_today) and (row["close"] > resistance_before_today)
            if above_now and not prev_above:
                crossover_time = row["timestamp"]
                break
            prev_above = above_now

    return (todayPOC, prevSupport, prevResistance, support_before_today,
            resistance_before_today, vwap_today, crossover_time, last_close,
            today_df, already_above_yesterday)


def compute_rvol(df, today_df, today):
    """Time-of-day-matched RVOL: today's cumulative volume up to the latest
    bar, divided by the average cumulative volume at that SAME time-of-day
    across the prior trading days available (up to 10), expressed as a
    percentage (100% = exactly average, 200% = double the usual volume)."""
    if today_df.empty:
        return None

    cutoff_time = today_df["timestamp"].iloc[-1].time()
    today_cum_vol = today_df["volume"].sum()

    prior_days = sorted(df[df["date"] < today]["date"].unique())
    baseline_days = prior_days[-10:]  # last up to 10 trading days
    if not baseline_days:
        return None

    baseline_cum_vols = []
    for d in baseline_days:
        day_df = df[df["date"] == d]
        # cumulative volume up to (and including) the same time-of-day cutoff
        matched = day_df[day_df["timestamp"].dt.time <= cutoff_time]
        if matched.empty:
            continue
        baseline_cum_vols.append(matched["volume"].sum())

    if not baseline_cum_vols:
        return None

    baseline_avg = sum(baseline_cum_vols) / len(baseline_cum_vols)
    if baseline_avg == 0:
        return None

    return round((today_cum_vol / baseline_avg) * 100, 1)


def main():
    if not ACCESS_TOKEN:
        print("ERROR: No Upstox access token found. Set it first:")
        print('  $env:UPSTOX_ACCESS_TOKEN = "your_token_here"')
        sys.exit(1)

    symbols = load_symbol_universe()
    results = []

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

            levels = compute_levels(df)
            if levels is None:
                continue
            (poc, support_now, resistance_now, support_before_today,
             resistance_before_today, vwap, crossover_time, last_close,
             today_df, already_above_yesterday) = levels

            today = df["date"].max()
            rvol_pct = compute_rvol(df, today_df, today)

            if pd.isna(support_before_today) or pd.isna(resistance_before_today):
                status = "no prior delta zone yet"
            elif crossover_time is not None:
                status = f"JUST CROSSED @ {crossover_time.strftime('%H:%M')}"
            elif already_above_yesterday and last_close > support_before_today and last_close > resistance_before_today:
                status = "ABOVE BOTH (continuing)"
            else:
                status = "-"

            results.append({
                "Symbol": symbol,
                "CurrentPrice": round(last_close, 2) if not pd.isna(last_close) else None,
                "POC": round(poc, 2) if not pd.isna(poc) else None,
                "VWAP": round(vwap, 2) if not pd.isna(vwap) else None,
                "DeltaSupport": round(support_before_today, 2) if not pd.isna(support_before_today) else None,
                "DeltaResistance": round(resistance_before_today, 2) if not pd.isna(resistance_before_today) else None,
                "RVOL%": rvol_pct,
                "Status": status,
            })
            print(f"[{i}/{len(symbols)}] {symbol}: {status}")

        except Exception as e:
            print(f"[{i}/{len(symbols)}] {symbol}: ERROR - {e}")
            continue

    if not results:
        print("No results produced.")
        return

    result_df = pd.DataFrame(results)

    # Reorder columns for intraday readability
    col_order = ["Symbol", "CurrentPrice", "POC", "VWAP", "DeltaSupport", "DeltaResistance", "RVOL%", "Status"]
    result_df = result_df[col_order]

    # Sort: JUST CROSSED (earliest time first) > ABOVE BOTH (continuing) > everything else
    def sort_key(row):
        status = row["Status"]
        if status.startswith("JUST CROSSED"):
            time_str = status.replace("JUST CROSSED @ ", "")
            return (0, time_str)
        elif status == "ABOVE BOTH (continuing)":
            return (1, "")
        else:
            return (2, "")

    result_df["_sort"] = result_df.apply(sort_key, axis=1)
    result_df = result_df.sort_values("_sort").drop(columns="_sort").reset_index(drop=True)
    result_df.insert(0, "S.No", range(1, len(result_df) + 1))

    # Full table (everything, for reference)
    full_out_path = "fno_delta_scanner_full.csv"
    result_df.to_csv(full_out_path, index=False)

    # Clean intraday table: only actionable rows (crossed or continuing above both)
    intraday_df = result_df[
        result_df["Status"].str.startswith("JUST CROSSED") | (result_df["Status"] == "ABOVE BOTH (continuing)")
    ].copy()
    intraday_df["S.No"] = range(1, len(intraday_df) + 1)

    print("\n" + "=" * 100)
    print("INTRADAY WATCHLIST - Stocks Above Both Delta Support & Resistance (sorted by crossover time)")
    print("=" * 100)
    print(intraday_df.to_string(index=False))

    intraday_out_path = "fno_intraday_watchlist.csv"
    intraday_df.to_csv(intraday_out_path, index=False)

    print(f"\nSaved full scan ({len(result_df)} stocks) to {full_out_path}")
    print(f"Saved intraday watchlist ({len(intraday_df)} stocks) to {intraday_out_path}")
    print(f"\nOpen {intraday_out_path} in Excel for a clean, sortable view during the trading session.")


if __name__ == "__main__":
    main()
