"""
fno_delta_dashboard.py

Streamlit dashboard for the Delta Support/Resistance + POC + RVOL scanner.

Combines both phases into one app:
  - "Run Precompute" button: slow, once-per-day step (loops per symbol
    fetching historical candles) - builds fno_levels_cache.json and seeds
    accurate crossover times into session state.
  - "Refresh Live Data" button (+ optional auto-refresh): fast, single batch
    API call for all symbols' live price/volume.

SETUP:
    pip install streamlit requests pandas streamlit-autorefresh --break-system-packages
    $env:UPSTOX_ACCESS_TOKEN = "your_token_here"
    streamlit run fno_delta_dashboard.py

If UPSTOX_ACCESS_TOKEN isn't set as an env var, the app will ask for it in
the sidebar (kept in-session only, never written to disk).
"""

import os
import json
import time
from datetime import datetime, timedelta, timezone

import requests
import pandas as pd
import streamlit as st

from sector_rotation import render_sector_rotation_tab
from sector_rotation import render_sector_rotation_tab
# Streamlit Cloud servers run in UTC, not IST - use an explicit fixed IST
# offset for all "current time" logic so RVOL matching and crossover
# timestamps are correct regardless of server timezone.
IST = timezone(timedelta(hours=5, minutes=30))

def now_ist():
    return datetime.now(IST)

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except ImportError:
    HAS_AUTOREFRESH = False

# ---------------- Config ----------------
UNIT = "minutes"
INTERVAL_VALUE = "5"
LOOKBACK_CALENDAR_DAYS = 18
REQUEST_DELAY_SECONDS = 0.2

PERC = 1.0
MERGE_THRESHOLD = 0.002
DELTA_LOOKBACK = 100
THRESHOLD = 0.7
SMOOTH_LEN = 5

FO_CSV_LOCAL_PATH = "fo_mktlots.csv"
CACHE_PATH = "fno_levels_cache.json"
STATE_PATH = "fno_crossed_state.json"

# Composite Score tab: uses DAILY candles (separate from the 5-min candles
# above, which only cover 18 days - not enough for a 50/200-day MA).
DAILY_UNIT = "days"
DAILY_INTERVAL_VALUE = "1"
DAILY_LOOKBACK_DAYS = 300
SCORE_CACHE_PATH = "fno_scores_cache.json"
SIGNAL_LOG_PATH = "fno_signal_log.json"
PAPER_TRADE_LOG_PATH = "fno_paper_trades.json"
INSTRUMENT_SEARCH_URL = "https://api.upstox.com/v2/instruments/search"
QUOTES_URL = "https://api.upstox.com/v2/market-quote/quotes"
BATCH_SIZE = 480

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
    "AARTIIND", "ABFRL", "ANGELONE", "APOLLOTYRE", "AUBANK", "BANKINDIA",
    "BSE", "CGPOWER", "CHAMBLFERT", "COFORGE", "COROMANDEL", "DIXON",
    "FORTIS", "GICRE", "GODFRYPHLP", "GRAPHITE", "GSPL", "HFCL",
    "HUDCO", "IIFL", "INDIACEM", "IRB", "ITI", "KALYANKJIL",
    "KEI", "LTF", "MANKIND", "MAXHEALTH", "MGL", "MOTILALOFS",
    "NBCC", "NCC", "NHPC", "PFIZER", "PGEL", "POWERINDIA",
    "PRESTIGE", "RVNL", "SJVN", "SOLARINDS", "SONACOMS", "STARHEALTH",
    "SUPREMEIND", "TIINDIA", "TITAGARH", "VEDL", "ZFCVINDIA",
]


# ---------------- Shared logic (same as the two standalone scripts) ----------------

def load_symbol_universe():
    if os.path.exists(FO_CSV_LOCAL_PATH):
        try:
            df = pd.read_csv(FO_CSV_LOCAL_PATH, skiprows=4, header=None)
            symbols = df[1].astype(str).str.strip().unique().tolist()
            symbols = [s for s in symbols if s and s.upper() not in ("NIFTY", "BANKNIFTY", "FINNIFTY")]
            return symbols
        except Exception:
            pass
    return DEFAULT_FNO_SYMBOLS


INDEX_FUTURES = ["NIFTY", "BANKNIFTY"]

SECTOR_MAP = {
    "NIFTY": "Index", "BANKNIFTY": "Index",
    "HDFCBANK": "Banking", "ICICIBANK": "Banking", "SBIN": "Banking", "AXISBANK": "Banking",
    "KOTAKBANK": "Banking", "INDUSINDBK": "Banking", "BANKBARODA": "Banking", "PNB": "Banking",
    "CANBK": "Banking", "IDFCFIRSTB": "Banking", "FEDERALBNK": "Banking", "BANDHANBNK": "Banking",
    "RBLBANK": "Banking", "AUBANK": "Banking", "UNIONBANK": "Banking", "YESBANK": "Banking",
    "BAJFINANCE": "NBFC/Financial Services", "BAJAJFINSV": "NBFC/Financial Services",
    "HDFCAMC": "NBFC/Financial Services", "SBICARD": "NBFC/Financial Services",
    "CHOLAFIN": "NBFC/Financial Services", "MUTHOOTFIN": "NBFC/Financial Services",
    "MANAPPURAM": "NBFC/Financial Services", "LICHSGFIN": "NBFC/Financial Services",
    "M&MFIN": "NBFC/Financial Services", "L&TFH": "NBFC/Financial Services",
    "PFC": "NBFC/Financial Services", "RECLTD": "NBFC/Financial Services",
    "IIFL": "NBFC/Financial Services", "ABCAPITAL": "NBFC/Financial Services",
    "MFSL": "NBFC/Financial Services", "ANGELONE": "NBFC/Financial Services",
    "MOTILALOFS": "NBFC/Financial Services", "CANFINHOME": "NBFC/Financial Services",
    "IEX": "NBFC/Financial Services", "BSE": "NBFC/Financial Services",
    "IRFC": "NBFC/Financial Services",
    "ICICIGI": "Insurance", "ICICIPRULI": "Insurance", "SBILIFE": "Insurance",
    "HDFCLIFE": "Insurance", "GICRE": "Insurance", "STARHEALTH": "Insurance",
    "TCS": "IT", "INFY": "IT", "HCLTECH": "IT", "WIPRO": "IT", "TECHM": "IT",
    "LTIM": "IT", "MPHASIS": "IT", "PERSISTENT": "IT", "COFORGE": "IT",
    "OFSS": "IT", "BSOFT": "IT", "TATAELXSI": "IT", "LTF": "IT",
    "MARUTI": "Auto", "TATAMOTORS": "Auto", "M&M": "Auto", "BAJAJ-AUTO": "Auto",
    "EICHERMOT": "Auto", "HEROMOTOCO": "Auto", "TVSMOTOR": "Auto", "ASHOKLEY": "Auto",
    "APOLLOTYRE": "Auto", "MOTHERSON": "Auto", "BHARATFORG": "Auto", "SONACOMS": "Auto",
    "ESCORTS": "Auto",
    "SUNPHARMA": "Pharma", "DIVISLAB": "Pharma", "DRREDDY": "Pharma", "CIPLA": "Pharma",
    "AUROPHARMA": "Pharma", "LUPIN": "Pharma", "BIOCON": "Pharma", "ALKEM": "Pharma",
    "TORNTPHARM": "Pharma", "GLENMARK": "Pharma", "LAURUSLABS": "Pharma", "IPCALAB": "Pharma",
    "ZYDUSLIFE": "Pharma", "MANKIND": "Pharma", "PFIZER": "Pharma", "GRANULES": "Pharma",
    "APOLLOHOSP": "Healthcare", "FORTIS": "Healthcare", "MAXHEALTH": "Healthcare",
    "LALPATHLAB": "Healthcare", "METROPOLIS": "Healthcare", "SYNGENE": "Healthcare",
    "HINDUNILVR": "FMCG", "ITC": "FMCG", "NESTLEIND": "FMCG", "BRITANNIA": "FMCG",
    "DABUR": "FMCG", "MARICO": "FMCG", "COLPAL": "FMCG", "GODREJCP": "FMCG",
    "TATACONSUM": "FMCG", "UBL": "FMCG", "VBL": "FMCG", "PATANJALI": "FMCG",
    "MCDOWELL-N": "FMCG", "JUBLFOOD": "FMCG",
    "TATASTEEL": "Metals & Mining", "JSWSTEEL": "Metals & Mining", "HINDALCO": "Metals & Mining",
    "VEDANTA": "Metals & Mining", "VEDL": "Metals & Mining", "SAIL": "Metals & Mining",
    "NMDC": "Metals & Mining", "NATIONALUM": "Metals & Mining", "HINDCOPPER": "Metals & Mining",
    "JINDALSTEL": "Metals & Mining", "COALINDIA": "Metals & Mining",
    "RELIANCE": "Oil & Gas", "ONGC": "Oil & Gas", "BPCL": "Oil & Gas", "IOC": "Oil & Gas",
    "GAIL": "Oil & Gas", "PETRONET": "Oil & Gas", "OIL": "Oil & Gas", "HINDPETRO": "Oil & Gas",
    "IGL": "Oil & Gas", "MGL": "Oil & Gas", "GUJGASLTD": "Oil & Gas", "GSPL": "Oil & Gas",
    "NTPC": "Power", "POWERGRID": "Power", "TATAPOWER": "Power", "ADANIENT": "Power",
    "NHPC": "Power", "SJVN": "Power", "TORNTPOWER": "Power", "CGPOWER": "Power",
    "POWERINDIA": "Power", "SUZLON": "Power", "SOLARINDS": "Power",
    "ULTRACEMCO": "Cement & Construction", "SHREECEM": "Cement & Construction",
    "AMBUJACEM": "Cement & Construction", "DALBHARAT": "Cement & Construction",
    "JKCEMENT": "Cement & Construction", "GRASIM": "Cement & Construction",
    "LT": "Infra & Capital Goods", "SIEMENS": "Infra & Capital Goods", "BEL": "Infra & Capital Goods",
    "HAL": "Infra & Capital Goods", "BHEL": "Infra & Capital Goods", "CUMMINSIND": "Infra & Capital Goods",
    "POLYCAB": "Infra & Capital Goods", "HAVELLS": "Infra & Capital Goods", "VOLTAS": "Infra & Capital Goods",
    "CROMPTON": "Infra & Capital Goods", "NCC": "Infra & Capital Goods", "IRB": "Infra & Capital Goods",
    "RVNL": "Infra & Capital Goods", "TITAGARH": "Infra & Capital Goods", "GMRINFRA": "Infra & Capital Goods",
    "APLAPOLLO": "Infra & Capital Goods", "KEI": "Infra & Capital Goods", "ZFCVINDIA": "Infra & Capital Goods",
    "NBCC": "Infra & Capital Goods", "ITI": "Infra & Capital Goods",
    "BHARTIARTL": "Telecom", "IDEA": "Telecom", "INDUSTOWER": "Telecom", "TATACOMM": "Telecom",
    "HFCL": "Telecom",
    "ASIANPAINT": "Consumer Durables", "TITAN": "Consumer Durables", "PIDILITIND": "Consumer Durables",
    "WHIRLPOOL": "Consumer Durables", "DIXON": "Consumer Durables", "BATAINDIA": "Consumer Durables",
    "PAGEIND": "Consumer Durables", "EXIDEIND": "Consumer Durables",
    "UPL": "Chemicals & Fertilizers", "PIIND": "Chemicals & Fertilizers", "SRF": "Chemicals & Fertilizers",
    "DEEPAKNTR": "Chemicals & Fertilizers", "AARTIIND": "Chemicals & Fertilizers",
    "NAVINFLUOR": "Chemicals & Fertilizers", "ATUL": "Chemicals & Fertilizers",
    "GNFC": "Chemicals & Fertilizers", "CHAMBLFERT": "Chemicals & Fertilizers",
    "TATACHEM": "Chemicals & Fertilizers", "COROMANDEL": "Chemicals & Fertilizers",
    "GRAPHITE": "Chemicals & Fertilizers", "SUPREMEIND": "Chemicals & Fertilizers",
    "DLF": "Realty", "GODREJPROP": "Realty", "OBEROIRLTY": "Realty", "LODHA": "Realty",
    "PRESTIGE": "Realty", "IBULHSGFIN": "Realty", "HUDCO": "Realty",
    "ZEEL": "Media & Entertainment", "SUNTV": "Media & Entertainment", "PVRINOX": "Media & Entertainment",
    "TRENT": "Retail", "DMART": "Retail", "JIOFIN": "Retail", "NAUKRI": "Retail",
    "INDIAMART": "Retail", "NYKAA": "Retail", "POLICYBZR": "Retail", "ZOMATO": "Retail",
    "PAYTM": "Retail",
    "INDIGO": "Aviation & Logistics", "IRCTC": "Aviation & Logistics", "CONCOR": "Aviation & Logistics",
    "DELHIVERY": "Aviation & Logistics",
    "ADANIPORTS": "Diversified/Conglomerate",
}


def get_sector(symbol):
    return SECTOR_MAP.get(symbol, "Other")




def resolve_futures_instrument_key(name, token):
    """Resolve the nearest-expiry futures contract for an index (NIFTY,
    BANKNIFTY) via the Instrument Search API - same approach used for the
    standalone nifty_fut_lipi_extract.py verification script."""
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Authorization": f"Bearer {token}"}
    params = {
        "query": name, "exchanges": "NSE", "segments": "FO",
        "instrument_types": "FUT", "expiry": "current_month",
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


def resolve_equity_instrument_key(symbol, token):
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Authorization": f"Bearer {token}"}
    params = {"query": symbol, "exchanges": "NSE", "segments": "EQ", "page_number": 1, "records": 10}
    resp = requests.get(INSTRUMENT_SEARCH_URL, headers=headers, params=params, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    for inst in payload.get("data", []):
        if inst.get("trading_symbol", "").upper() == symbol.upper() and inst.get("instrument_type") == "EQ":
            return inst["instrument_key"]
    return None


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


def find_swing_points(df, pivot_window=12):
    """Detect swing highs/lows across the FULL lookback period (not just
    today) using a fractal pivot method: a bar's low/high is a swing point
    if it's the lowest/highest within pivot_window bars on both sides.
    pivot_window=12 at 5-min bars = ~1 hour of context each side, filtering
    out minor noise in favor of more meaningful swing levels."""
    df = df.reset_index(drop=True)
    n = len(df)
    swing_lows = []
    swing_highs = []
    for i in range(pivot_window, n - pivot_window):
        window_low = df["low"].iloc[i - pivot_window:i + pivot_window + 1]
        window_high = df["high"].iloc[i - pivot_window:i + pivot_window + 1]
        if df["low"].iloc[i] == window_low.min():
            swing_lows.append(round(df["low"].iloc[i], 2))
        if df["high"].iloc[i] == window_high.max():
            swing_highs.append(round(df["high"].iloc[i], 2))
    return sorted(set(swing_lows)), sorted(set(swing_highs))


def compute_atr(df, period=14):
    """Average True Range on the 5-min bars already being fetched for this
    dashboard - measures typical bar-to-bar volatility, used to size the
    Target distance for a given symbol instead of a fixed % or a nearby
    swing point (both of which can be arbitrarily too tight or too wide
    relative to how much this particular stock actually moves).
    True Range = max(high-low, |high-prev_close|, |low-prev_close|).
    Returns the latest ATR value (in price units, not %), or None if there
    isn't enough history yet."""
    if len(df) < period + 1:
        return None
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr_series = tr.rolling(period).mean()
    latest = atr_series.iloc[-1]
    return None if pd.isna(latest) else round(latest, 2)


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
    last_close_val = round(today_df["close"].iloc[-1], 2) if not today_df.empty else None
    # prevSupport/prevResistance after the loop finishes hold the value that
    # will apply as the STARTING level for the NEXT day - i.e. what will
    # actually be in effect when tomorrow's session begins, as opposed to
    # support_before_today/resistance_before_today which is what was in
    # effect BEFORE today (used to detect today's own crossings).
    next_day_support_val = None if pd.isna(prevSupport) else round(prevSupport, 2)
    next_day_resistance_val = None if pd.isna(prevResistance) else round(prevResistance, 2)
    prior_days_df = df[df["date"] < today]
    prior_days = sorted(prior_days_df["date"].unique())

    yesterday_close = prior_days_df["close"].iloc[-1] if not prior_days_df.empty else float("nan")
    already_above_yesterday = (
        not pd.isna(support_before_today) and not pd.isna(resistance_before_today) and
        not pd.isna(yesterday_close) and
        yesterday_close > support_before_today and yesterday_close > resistance_before_today
    )
    already_below_yesterday = (
        not pd.isna(support_before_today) and not pd.isna(resistance_before_today) and
        not pd.isna(yesterday_close) and
        yesterday_close < support_before_today and yesterday_close < resistance_before_today
    )

    crossover_time_str = None  # bullish: first candle closing above BOTH levels
    crossover_price_val = None
    crossunder_time_str = None  # bearish: first candle closing below BOTH levels
    crossunder_price_val = None
    has_prior_levels = not pd.isna(support_before_today) and not pd.isna(resistance_before_today)

    if has_prior_levels and not already_above_yesterday:
        prev_above = False
        for _, row in today_df.iterrows():
            above_now = row["close"] > support_before_today and row["close"] > resistance_before_today
            if above_now and not prev_above:
                crossover_time_str = row["timestamp"].strftime("%H:%M")
                crossover_price_val = round(row["close"], 2)
                break
            prev_above = above_now

    if has_prior_levels and not already_below_yesterday:
        prev_below = False
        for _, row in today_df.iterrows():
            below_now = row["close"] < support_before_today and row["close"] < resistance_before_today
            if below_now and not prev_below:
                crossunder_time_str = row["timestamp"].strftime("%H:%M")
                crossunder_price_val = round(row["close"], 2)
                break
            prev_below = below_now

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

    swing_lows, swing_highs = find_swing_points(df)
    atr_val = compute_atr(df, period=14)

    # Rolling buffer of recent 5-min closes, used to power the Intraday
    # Composite Score (MA20 + RSI + Volume, all on 5-min bars) so that
    # score can move throughout the session instead of only once a day.
    # 220 bars gives enough margin for RSI(14)+lookback(20) and MA20+slope(5)
    # even after the live scan appends the current tick as an extra bar.
    intraday_closes = df.tail(220)["close"].round(2).tolist()

    return {
        "poc": None if pd.isna(todayPOC) else round(todayPOC, 2),
        "delta_support": None if pd.isna(support_before_today) else round(support_before_today, 2),
        "delta_resistance": None if pd.isna(resistance_before_today) else round(resistance_before_today, 2),
        "last_close": last_close_val,
        "next_day_support": next_day_support_val,
        "next_day_resistance": next_day_resistance_val,
        "yesterday_close": None if pd.isna(yesterday_close) else round(yesterday_close, 2),
        "already_above_yesterday": bool(already_above_yesterday),
        "already_below_yesterday": bool(already_below_yesterday),
        "crossover_time": crossover_time_str,
        "crossover_price": crossover_price_val,
        "crossunder_time": crossunder_time_str,
        "crossunder_price": crossunder_price_val,
        "rvol_baseline": rvol_baseline,
        "swing_lows": swing_lows,
        "swing_highs": swing_highs,
        "computed_date": str(today),
        "intraday_closes": intraday_closes,
        "atr": atr_val,
    }


def run_precompute(token, progress_callback=None):
    cache = {}

    # Index futures first - always included regardless of the equity universe
    for name in INDEX_FUTURES:
        try:
            instrument_key = resolve_futures_instrument_key(name, token)
            if not instrument_key:
                if progress_callback:
                    progress_callback(0, 0, name, "no futures contract found")
                continue
            time.sleep(REQUEST_DELAY_SECONDS)
            df = fetch_candles(instrument_key, token)
            time.sleep(REQUEST_DELAY_SECONDS)
            if df.empty:
                continue
            levels = compute_levels_and_baseline(df)
            if levels is None:
                continue
            levels["instrument_key"] = instrument_key
            levels["is_index"] = True
            cache[name] = levels
            if progress_callback:
                progress_callback(0, 0, name, "ok (index future)")
        except Exception as e:
            if progress_callback:
                progress_callback(0, 0, name, f"error: {e}")
            continue

    symbols = load_symbol_universe()
    for i, symbol in enumerate(symbols, start=1):
        try:
            instrument_key = resolve_equity_instrument_key(symbol, token)
            if not instrument_key:
                if progress_callback:
                    progress_callback(i, len(symbols), symbol, "no instrument key")
                continue
            time.sleep(REQUEST_DELAY_SECONDS)
            df = fetch_candles(instrument_key, token)
            time.sleep(REQUEST_DELAY_SECONDS)
            if df.empty:
                if progress_callback:
                    progress_callback(i, len(symbols), symbol, "no data")
                continue
            levels = compute_levels_and_baseline(df)
            if levels is None:
                continue
            levels["instrument_key"] = instrument_key
            levels["is_index"] = False
            cache[symbol] = levels
            if progress_callback:
                progress_callback(i, len(symbols), symbol, "ok")
        except Exception as e:
            if progress_callback:
                progress_callback(i, len(symbols), symbol, f"error: {e}")
            continue

    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)

    today_str = now_ist().strftime("%Y-%m-%d")
    state = {"_date": today_str}
    for symbol, levels in cache.items():
        # Only seed the STABLE "already above/below" continuation state here.
        # Do NOT seed "crossed_up"/"crossed_down" from historical crossover_time -
        # Upstox's historical candle API lags a full day behind, so that data
        # actually reflects the LAST COMPLETE session, not today. Stamping it
        # with today's date would disguise an old crossing as a fresh one.
        # Genuine fresh crossings TODAY get detected properly by the live scan
        # itself, comparing real-time price against these same levels.
        if levels.get("already_above_yesterday"):
            state[symbol] = {"status": "continuing_up"}
        elif levels.get("already_below_yesterday"):
            state[symbol] = {"status": "continuing_down"}
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)

    return cache, state


# ---------------- Composite Score (MA50 + RSI momentum + Volume) ----------------

def fetch_daily_candles(instrument_key, token, lookback_days=DAILY_LOOKBACK_DAYS):
    """Same shape as fetch_candles(), but daily bars over a long lookback -
    needed for 50/200-day moving averages, which the 18-day 5-min candles
    used elsewhere in this file can't support."""
    to_date = now_ist().strftime("%Y-%m-%d")
    from_date = (now_ist() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    url = f"https://api.upstox.com/v3/historical-candle/{instrument_key}/{DAILY_UNIT}/{DAILY_INTERVAL_VALUE}/{to_date}/{from_date}"
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
    return df


def compute_rsi(close_series, period=14):
    delta = close_series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def ma50_score(df):
    """Trend score in [-1, 1]: 50% distance of price from MA50 (capped),
    30% MA50 slope over the last 5 sessions, 20% MA50-vs-MA200 regime."""
    ma50_series = df["close"].rolling(50).mean()
    ma200_series = df["close"].rolling(200).mean()
    if len(df) < 50 or pd.isna(ma50_series.iloc[-1]):
        return None
    ma50 = ma50_series.iloc[-1]
    ma50_5ago = ma50_series.iloc[-5] if len(ma50_series) >= 5 else ma50
    close = df["close"].iloc[-1]

    dist_score = max(-1, min(1, (close - ma50) / ma50)) if ma50 else 0
    slope = (ma50 - ma50_5ago) / ma50 if ma50 else 0
    slope_score = max(-1, min(1, slope * 5))
    ma200 = ma200_series.iloc[-1]
    regime = 0 if pd.isna(ma200) else (1 if ma50 > ma200 else -1)

    return max(-1, min(1, 0.5 * dist_score + 0.3 * slope_score + 0.2 * regime))


def rsi_score_momentum(df, rsi_period=14, lookback=20):
    """RSI change normalized by its own recent volatility, in [-1, 1]."""
    rsi_series = compute_rsi(df["close"], rsi_period)
    rsi_change = rsi_series.diff()
    if rsi_change.dropna().empty:
        return 0.0
    change = rsi_change.iloc[-1]
    stdev = rsi_change.rolling(lookback).std().iloc[-1]
    if pd.isna(stdev) or stdev == 0 or pd.isna(change):
        return 0.0
    return max(-1, min(1, change / (2 * stdev)))


def vol_score(df, lookback_vol=20):
    """Volume ratio (capped at 1) signed by the direction of the last move."""
    if len(df) < lookback_vol + 1:
        return 0.0
    avg_vol = df["volume"].rolling(lookback_vol).mean().iloc[-1]
    curr_vol = df["volume"].iloc[-1]
    ratio = (curr_vol / avg_vol) if avg_vol else 0
    ratio = min(ratio, 1.0)
    if df["close"].iloc[-1] > df["close"].iloc[-2]:
        trend_sign = 1
    elif df["close"].iloc[-1] < df["close"].iloc[-2]:
        trend_sign = -1
    else:
        trend_sign = 0
    return ratio * trend_sign


# ---------------- Intraday Composite Score (MA20 + RSI + Volume, on 5-min bars) ----------------
#
# Same idea as the daily Composite Score above (trend + momentum + volume,
# each normalized to -1..+1 and summed), but built on 5-min closes so it
# actually moves during the session instead of only changing once a day.
# Reuses compute_rsi() defined above. Fed by the "intraday_closes" buffer
# stored in each symbol's cache entry (see compute_levels_and_baseline),
# with the current live price appended as the latest bar on every refresh.

def ma20_intraday_score(closes_series):
    """Trend score in [-1, 1]: distance of live price from MA20 (5-min bars,
    ~100 min of context), plus MA20 slope over the last 5 bars (~25 min)."""
    if len(closes_series) < 20:
        return None
    ma20_series = closes_series.rolling(20).mean()
    if pd.isna(ma20_series.iloc[-1]):
        return None
    ma20 = ma20_series.iloc[-1]
    ma20_5ago = ma20_series.iloc[-5] if len(ma20_series) >= 5 else ma20
    current = closes_series.iloc[-1]

    dist_score = max(-1, min(1, (current - ma20) / ma20)) if ma20 else 0
    slope = (ma20 - ma20_5ago) / ma20 if ma20 else 0
    # 5-min bars move faster than daily bars, so the slope is scaled up
    # (x10 vs x5 for the daily version) to stay meaningfully sensitive.
    slope_score = max(-1, min(1, slope * 10))
    return max(-1, min(1, 0.6 * dist_score + 0.4 * slope_score))


def rsi_intraday_score(closes_series, rsi_period=14, lookback=20):
    """RSI change normalized by its own recent volatility, in [-1, 1] -
    same math as the daily version, just fed 5-min closes instead."""
    rsi_series = compute_rsi(closes_series, rsi_period)
    rsi_change = rsi_series.diff()
    if rsi_change.dropna().empty:
        return 0.0
    change = rsi_change.iloc[-1]
    stdev = rsi_change.rolling(lookback).std().iloc[-1]
    if pd.isna(stdev) or stdev == 0 or pd.isna(change):
        return 0.0
    return max(-1, min(1, change / (2 * stdev)))


def vol_intraday_score(rvol_pct, closes_series):
    """RVOL-based volume score in [-1, 1], signed by the direction of the
    most recent tick. rvol_pct is today's cumulative volume as a % of the
    time-matched historical baseline (already computed for the live scan) -
    100% = right on pace, 200%+ = well above average for this time of day."""
    if rvol_pct is None or len(closes_series) < 2:
        return 0.0
    ratio = min(rvol_pct / 100.0, 2.0) / 2.0  # 100%->0.5, 200%+->1.0 (capped)
    current, prev = closes_series.iloc[-1], closes_series.iloc[-2]
    if current > prev:
        sign = 1
    elif current < prev:
        sign = -1
    else:
        sign = 0
    return max(-1, min(1, ratio * sign))


def compute_intraday_composite_score(intraday_closes, current_price, rvol_pct):
    """Combine the three intraday sub-scores into a final score, using the
    symbol's cached 5-min close buffer plus the live price as the newest
    bar - so this reflects what's happening RIGHT NOW, not just at the
    start of the day. Returns None if there isn't enough buffered history
    yet (e.g. cache built before this feature existed - re-run Precompute)."""
    if not intraday_closes or current_price is None:
        return None
    closes_series = pd.Series(list(intraday_closes) + [current_price])

    ma_s = ma20_intraday_score(closes_series)
    if ma_s is None:
        return None
    rsi_s = rsi_intraday_score(closes_series)
    vol_s = vol_intraday_score(rvol_pct, closes_series)

    return {
        "ma20_score": round(ma_s, 4),
        "rsi_score": round(rsi_s, 4),
        "vol_score": round(vol_s, 4),
        "final_score": round(ma_s + rsi_s + vol_s, 4),
    }


def compute_composite_score(df):
    if df.empty or len(df) < 60:
        return None
    ma50 = ma50_score(df)
    if ma50 is None:
        return None
    rsi = rsi_score_momentum(df)
    vol = vol_score(df)
    return {
        "ma50_score": round(ma50, 4),
        "rsi_score": round(rsi, 4),
        "vol_score": round(vol, 4),
        "final_score": round(ma50 + rsi + vol, 4),
    }


def run_composite_scan(token, progress_callback=None):
    """Slow, once-a-day scan (like run_precompute) - one daily-candle API
    call per symbol - producing a ranked composite score table."""
    results = []

    for name in INDEX_FUTURES:
        try:
            instrument_key = resolve_futures_instrument_key(name, token)
            if not instrument_key:
                continue
            time.sleep(REQUEST_DELAY_SECONDS)
            df = fetch_daily_candles(instrument_key, token)
            time.sleep(REQUEST_DELAY_SECONDS)
            scores = compute_composite_score(df)
            if scores is None:
                continue
            scores["Symbol"] = name
            scores["IsIndex"] = True
            results.append(scores)
            if progress_callback:
                progress_callback(0, 0, name, "ok (index)")
        except Exception as e:
            if progress_callback:
                progress_callback(0, 0, name, f"error: {e}")
            continue

    symbols = load_symbol_universe()
    for i, symbol in enumerate(symbols, start=1):
        try:
            instrument_key = resolve_equity_instrument_key(symbol, token)
            if not instrument_key:
                if progress_callback:
                    progress_callback(i, len(symbols), symbol, "no instrument key")
                continue
            time.sleep(REQUEST_DELAY_SECONDS)
            df = fetch_daily_candles(instrument_key, token)
            time.sleep(REQUEST_DELAY_SECONDS)
            scores = compute_composite_score(df)
            if scores is None:
                if progress_callback:
                    progress_callback(i, len(symbols), symbol, "not enough daily history")
                continue
            scores["Symbol"] = symbol
            scores["IsIndex"] = False
            results.append(scores)
            if progress_callback:
                progress_callback(i, len(symbols), symbol, "ok")
        except Exception as e:
            if progress_callback:
                progress_callback(i, len(symbols), symbol, f"error: {e}")
            continue

    score_df = pd.DataFrame(results)
    if not score_df.empty:
        score_df = score_df[["Symbol", "IsIndex", "ma50_score", "rsi_score", "vol_score", "final_score"]]
        score_df = score_df.sort_values("final_score", ascending=False).reset_index(drop=True)

    score_df.to_json(SCORE_CACHE_PATH, orient="records", indent=2)
    return score_df


def fetch_batch_quotes(instrument_keys, token):
    headers = {
        "Content-Type": "application/json", "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
    }
    all_data = {}
    for i in range(0, len(instrument_keys), BATCH_SIZE):
        chunk = instrument_keys[i:i + BATCH_SIZE]
        # Cache-busting timestamp param - some infra caches identical GET
        # requests by URL, which would otherwise serve stale quotes on
        # every refresh since the symbol list is the same each time.
        params = {"instrument_key": ",".join(chunk), "_ts": str(int(time.time() * 1000))}
        max_retries = 4
        for attempt in range(max_retries):
            resp = requests.get(QUOTES_URL, headers=headers, params=params, timeout=20)
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            payload = resp.json()
            all_data.update(payload.get("data", {}))
            break
    return all_data


def nearest_rvol_baseline(rvol_baseline, current_time_str):
    if not rvol_baseline:
        return None
    candidates = [t for t in rvol_baseline.keys() if t <= current_time_str]
    if not candidates:
        return None
    return rvol_baseline[max(candidates)]


def load_signal_log():
    today_str = now_ist().strftime("%Y-%m-%d")
    if os.path.exists(SIGNAL_LOG_PATH):
        with open(SIGNAL_LOG_PATH) as f:
            log = json.load(f)
        if log.get("_date") == today_str:
            return log
    return {"_date": today_str, "entries": []}


def save_signal_log(log):
    with open(SIGNAL_LOG_PATH, "w") as f:
        json.dump(log, f, indent=2)


def append_to_signal_log(log, category, df, detail_col):
    """Append any NEW (not-already-logged-today) entries from df to the log,
    deduped by (symbol, category, detail) so a stock that keeps showing the
    same status across multiple refreshes doesn't get logged repeatedly -
    but a genuinely new event (different time/detail) does."""
    existing_keys = {(e["symbol"], e["category"], e["detail"]) for e in log["entries"]}
    for _, row in df.iterrows():
        detail = str(row[detail_col])
        key = (row["Symbol"], category, detail)
        if key in existing_keys:
            continue
        log["entries"].append({
            "logged_at": now_ist().strftime("%H:%M"),
            "symbol": row["Symbol"],
            "category": category,
            "current_price": row.get("CurrentPrice"),
            "vwap": row.get("VWAP"),
            "zone_width_pct": row.get("ZoneWidth%"),
            "detail": detail,
        })
        existing_keys.add(key)
    return log


# ---------------- Paper Trade Log ----------------
#
# Persists every Signals-tab idea to disk the moment it first appears, and
# tracks its outcome (target hit / stop hit / still open) on every
# subsequent refresh - so opening this dashboard from your phone at, say,
# 2 PM still shows everything that fired since market open at 9:15, not
# just whatever's live at that exact moment. Resets automatically at the
# start of each new trading day (same _date-keyed pattern as the Signal
# Log above).
#
# IMPORTANT: this only captures what happens while the app is actually
# running and refreshing - Streamlit doesn't execute code in the
# background on its own. For this to genuinely cover "since market open"
# regardless of when you check your phone, leave a tab open somewhere
# (PC or Streamlit Cloud) with Auto-refresh turned ON for the whole
# session, so a scan actually happens every ~30-60s all day. Opening the
# app fresh at 2 PM with auto-refresh OFF the whole morning will only
# have logged whatever happened to be live during actual refreshes.

def load_paper_trade_log():
    today_str = now_ist().strftime("%Y-%m-%d")
    if os.path.exists(PAPER_TRADE_LOG_PATH):
        with open(PAPER_TRADE_LOG_PATH) as f:
            log = json.load(f)
        if log.get("_date") == today_str:
            return log
    return {"_date": today_str, "trades": []}


def save_paper_trade_log(log):
    with open(PAPER_TRADE_LOG_PATH, "w") as f:
        json.dump(log, f, indent=2)


def log_new_paper_trades(log, signals_df):
    """Append any signal not already logged today, deduped by
    (symbol, action, entry_time) - so the SAME fresh signal seen across
    consecutive refreshes (before its underlying status changes) isn't
    logged twice, but a genuinely new entry later in the day is."""
    existing_keys = {(t["symbol"], t["action"], t["entry_time"]) for t in log["trades"]}
    for _, row in signals_df.iterrows():
        key = (row["Symbol"], row["Action"], row["Time"])
        if key in existing_keys:
            continue
        log["trades"].append({
            "symbol": row["Symbol"],
            "action": row["Action"],
            "entry_time": row["Time"],
            "entry_price": row["Price"],
            "stop_loss": row["StopLoss"],
            "target": row["Target"],
            "reward_risk": row.get("RewardRisk"),
            "target_source": row.get("TargetSource"),
            "confidence": row["Confidence"],
            "rvol_at_entry": row.get("RVOL%"),
            "why": row.get("Why"),
            "status": "OPEN",
            "exit_price": None,
            "exit_time": None,
            "logged_at": now_ist().strftime("%H:%M"),
        })
        existing_keys.add(key)
    return log


def update_paper_trade_statuses(log, result_df):
    """Mark-to-market every still-OPEN paper trade against the latest live
    price: closes it out the moment price actually reaches the recorded
    target or stop, using the CURRENT scan's price - not a live tick
    stream, so an intra-refresh spike through a level between scans won't
    be caught until the next refresh picks it up."""
    price_lookup = result_df.set_index("Symbol")["CurrentPrice"].to_dict()
    now_time_str = now_ist().strftime("%H:%M")
    for t in log["trades"]:
        if t["status"] != "OPEN":
            continue
        ltp = price_lookup.get(t["symbol"])
        if ltp is None or pd.isna(ltp):
            continue
        if t["action"] == "BUY":
            if ltp >= t["target"]:
                t["status"], t["exit_price"], t["exit_time"] = "TARGET HIT", ltp, now_time_str
            elif ltp <= t["stop_loss"]:
                t["status"], t["exit_price"], t["exit_time"] = "STOP HIT", ltp, now_time_str
        else:  # SELL
            if ltp <= t["target"]:
                t["status"], t["exit_price"], t["exit_time"] = "TARGET HIT", ltp, now_time_str
            elif ltp >= t["stop_loss"]:
                t["status"], t["exit_price"], t["exit_time"] = "STOP HIT", ltp, now_time_str
    return log


def build_paper_trade_df(log, result_df):
    """Render the log into a display-ready DataFrame, with live mark-to-
    market P&L% for OPEN trades (using current price) and locked-in P&L%
    for closed ones (using the recorded exit price)."""
    if not log["trades"]:
        return pd.DataFrame()
    price_lookup = result_df.set_index("Symbol")["CurrentPrice"].to_dict()
    rows = []
    for t in log["trades"]:
        if t["status"] == "OPEN":
            mark_price = price_lookup.get(t["symbol"])
        else:
            mark_price = t["exit_price"]
        pnl_pct = None
        if mark_price is not None and pd.notna(mark_price):
            if t["action"] == "BUY":
                pnl_pct = round((mark_price - t["entry_price"]) / t["entry_price"] * 100, 2)
            else:
                pnl_pct = round((t["entry_price"] - mark_price) / t["entry_price"] * 100, 2)
        rows.append({
            "Symbol": t["symbol"], "Action": t["action"], "EntryTime": t["entry_time"],
            "EntryPrice": t["entry_price"], "StopLoss": t["stop_loss"], "Target": t["target"],
            "TargetSource": t.get("target_source"),
            "RewardRisk": t.get("reward_risk"), "Confidence": t["confidence"],
            "RVOL@Entry": t.get("rvol_at_entry"), "Status": t["status"],
            "ExitPrice": t["exit_price"], "ExitTime": t["exit_time"],
            "PnL%": pnl_pct, "Why": t.get("why"),
        })
    df = pd.DataFrame(rows)
    df = df.sort_values("EntryTime").reset_index(drop=True)
    df.insert(0, "S.No", range(1, len(df) + 1))
    return df


def run_live_scan(cache, state, token, max_zone_width_pct=1.5,
                   vwap_above_support_max_pct=0.5, vwap_resistance_room_min_pct=1.0,
                   vwap_resistance_room_max_pct=2.0):
    today_str = now_ist().strftime("%Y-%m-%d")
    now_time_str = now_ist().strftime("%H:%M")
    if state.get("_date") != today_str:
        state = {"_date": today_str}

    symbols = list(cache.keys())
    instrument_keys = [cache[s]["instrument_key"] for s in symbols]
    key_to_symbol = {cache[s]["instrument_key"]: s for s in symbols}

    quotes = fetch_batch_quotes(instrument_keys, token)

    # Diagnostic: print raw quote details for one symbol so the logs show
    # exactly what Upstox is returning right now, including any timestamp
    # field it provides - this tells us definitively whether stale data is
    # coming from Upstox itself or from something in our own processing.
    if "PRESTIGE" in cache:
        prestige_key = cache["PRESTIGE"]["instrument_key"]
        raw_q = quotes.get(prestige_key) or next(
            (v for v in quotes.values() if v.get("instrument_token") == prestige_key), None
        )
        print(f"[DIAGNOSTIC {now_time_str}] PRESTIGE raw quote: {raw_q}")

    results = []
    for quote_key, q in quotes.items():
        instrument_key = q.get("instrument_token")
        symbol = key_to_symbol.get(instrument_key)
        if not symbol:
            continue

        levels = cache[symbol]
        support = levels["delta_support"]
        resistance = levels["delta_resistance"]
        poc = levels["poc"]
        already_above_yesterday = levels["already_above_yesterday"]
        already_below_yesterday = levels.get("already_below_yesterday", False)

        current_price = q.get("last_price")
        today_volume = q.get("volume")
        vwap = q.get("average_price")  # Upstox's day-average price, used as VWAP proxy

        # Day Change % - standard "vs yesterday's close" metric, the same
        # thing every generic screener (Zerodha, TradingView, etc.) shows.
        # Distinct from %Move below, which is specific to this dashboard's
        # own Delta Support/Resistance breakout logic (% distance from the
        # broken zone level, not from yesterday's close) - the two measure
        # different things and will legitimately disagree on any given stock.
        #
        # Uses levels["last_close"], NOT levels["yesterday_close"]. Upstox's
        # historical candle API lags one full session behind (documented
        # elsewhere in this file), so the cache's internal "today" is
        # actually real-world yesterday - meaning last_close (the most
        # recent close in the fetched data) IS real yesterday's close,
        # while yesterday_close is one day further back than that and
        # would overstate today's change by an extra day's move.
        prev_close = levels.get("last_close")
        day_change_pct = None
        if prev_close and current_price is not None and prev_close != 0:
            day_change_pct = round(((current_price - prev_close) / prev_close) * 100, 2)

        rvol_pct = None
        baseline_vol = nearest_rvol_baseline(levels.get("rvol_baseline", {}), now_time_str)
        if baseline_vol and today_volume is not None and baseline_vol > 0:
            rvol_pct = round((today_volume / baseline_vol) * 100, 1)

        # Intraday Composite Score - recomputed fresh on every live refresh
        # (unlike the daily Composite Score tab, which only updates once a
        # day via its own sidebar button). None if the symbol's cache
        # predates this feature - re-run Precompute to populate it.
        intraday_score = compute_intraday_composite_score(
            levels.get("intraday_closes"), current_price, rvol_pct
        )

        if support is None or resistance is None or current_price is None:
            status = "no prior delta zone yet"
            zone_width_pct = None
            next_support = next_resistance = None
            is_above_both = is_below_both = False
            entry_price = None
        else:
            zone_width_pct = round(((resistance - support) / current_price) * 100, 2)
            is_inverted_zone = zone_width_pct < 0
            is_wide_zone = abs(zone_width_pct) > max_zone_width_pct

            swing_lows = levels.get("swing_lows", [])
            swing_highs = levels.get("swing_highs", [])
            lows_below = [s for s in swing_lows if s < current_price]
            highs_above = [r for r in swing_highs if r > current_price]
            next_support = max(lows_below) if lows_below else None
            next_resistance = min(highs_above) if highs_above else None

            is_above_both = current_price > support and current_price > resistance
            is_below_both = current_price < support and current_price < resistance
            prior_state = state.get(symbol)
            prior_status = prior_state.get("status") if prior_state else None
            entry_price = None

            if is_above_both:
                if already_above_yesterday and prior_status is None:
                    status = "ABOVE BOTH (continuing)"
                    state[symbol] = {"status": "continuing_up"}
                elif prior_status in ("crossed_up", "continuing_up"):
                    if prior_status == "crossed_up":
                        status = f"JUST CROSSED UP @ {prior_state['time']}"
                        entry_price = prior_state.get("price")
                    else:
                        status = "ABOVE BOTH (continuing)"
                else:
                    status = f"JUST CROSSED UP @ {now_time_str}"
                    entry_price = current_price
                    state[symbol] = {"status": "crossed_up", "time": now_time_str, "price": current_price}
                if status.startswith("JUST CROSSED"):
                    if is_inverted_zone:
                        status += " (INVERTED ZONE - unreliable)"
                    elif is_wide_zone:
                        status += " (wide zone - caution)"
            elif is_below_both:
                if already_below_yesterday and prior_status is None:
                    status = "BELOW BOTH (continuing)"
                    state[symbol] = {"status": "continuing_down"}
                elif prior_status in ("crossed_down", "continuing_down"):
                    if prior_status == "crossed_down":
                        status = f"JUST CROSSED DOWN @ {prior_state['time']}"
                        entry_price = prior_state.get("price")
                    else:
                        status = "BELOW BOTH (continuing)"
                else:
                    status = f"JUST CROSSED DOWN @ {now_time_str}"
                    entry_price = current_price
                    state[symbol] = {"status": "crossed_down", "time": now_time_str, "price": current_price}
                if status.startswith("JUST CROSSED"):
                    if is_inverted_zone:
                        status += " (INVERTED ZONE - unreliable)"
                    elif is_wide_zone:
                        status += " (wide zone - caution)"
            else:
                status = "-"
                if symbol in state:
                    del state[symbol]

        # VWAP-reclaim setup: VWAP sitting just above Delta Support (tight,
        # well-positioned base), Delta Resistance 1-2% above VWAP (room to
        # run), and price currently above VWAP - a distinct setup from the
        # support/resistance zone crossovers above.
        vwap_setup_detail = None
        if vwap and support and resistance and current_price:
            vwap_above_support_pct = ((vwap - support) / support) * 100
            resistance_room_pct = ((resistance - vwap) / vwap) * 100
            zone_positioned_well = (
                0 <= vwap_above_support_pct <= vwap_above_support_max_pct and
                vwap_resistance_room_min_pct <= resistance_room_pct <= vwap_resistance_room_max_pct
            )
            price_above_vwap = current_price > vwap
            vwap_key = f"{symbol}_vwap"
            was_above_vwap = state.get(vwap_key, {}).get("above", False)

            if zone_positioned_well and price_above_vwap:
                vwap_setup_detail = f"VWAP RECLAIM @ {now_time_str}" if not was_above_vwap else "VWAP RECLAIM (holding)"
                state[vwap_key] = {"above": True}
            else:
                state[vwap_key] = {"above": price_above_vwap}

        # Single-level crossings: distinct, earlier-stage events from the
        # "both levels" crossing logic above. Tracked independently per
        # symbol so a support reclaim or resistance loss is caught even
        # when the stock isn't (yet, or ever) above/below BOTH levels.
        simple_status = "-"
        if support is not None and current_price is not None:
            support_key = f"{symbol}_support_side"
            was_above_support = state.get(support_key, {}).get("above")
            now_above_support = current_price > support
            if was_above_support is False and now_above_support:
                simple_status = "CROSSING SUPPORT FROM BELOW"
            state[support_key] = {"above": now_above_support}

        if resistance is not None and current_price is not None:
            resistance_key = f"{symbol}_resistance_side"
            was_above_resistance = state.get(resistance_key, {}).get("above")
            now_above_resistance = current_price > resistance
            if was_above_resistance is True and not now_above_resistance and simple_status == "-":
                simple_status = "CROSSING RESISTANCE FROM ABOVE"
            state[resistance_key] = {"above": now_above_resistance}

        # Plain VWAP crossings - independent of the zone-conditioned VWAP
        # Reclaim setup above. Just: did price cross above/below VWAP itself.
        if vwap is not None and current_price is not None:
            vwap_side_key = f"{symbol}_vwap_side"
            was_above_vwap_side = state.get(vwap_side_key, {}).get("above")
            now_above_vwap_side = current_price > vwap
            if simple_status == "-":
                if was_above_vwap_side is False and now_above_vwap_side:
                    simple_status = "CROSSED ABOVE VWAP FROM BELOW"
                elif was_above_vwap_side is True and not now_above_vwap_side:
                    simple_status = "CROSSED BELOW VWAP FROM ABOVE"
            state[vwap_side_key] = {"above": now_above_vwap_side}

        # Both-level events take priority over single-level ones when both apply
        if status.startswith("JUST CROSSED UP"):
            simple_status = "CROSSED UP"
        elif status.startswith("JUST CROSSED DOWN"):
            simple_status = "CROSSED BELOW"
        elif status == "ABOVE BOTH (continuing)":
            simple_status = "ABOVE BOTH"
        elif status == "BELOW BOTH (continuing)":
            simple_status = "BELOW BOTH"

        # Distance to the NEXT level in the direction of the current move -
        # room to run before hitting the next real obstacle
        next_level_distance_pct = None
        if is_above_both and next_resistance is not None and current_price:
            next_level_distance_pct = round(((next_resistance - current_price) / current_price) * 100, 2)
        elif is_below_both and next_support is not None and current_price:
            next_level_distance_pct = round(((current_price - next_support) / current_price) * 100, 2)

        # Time the underlying signal actually fired - parsed from the
        # "@ HH:MM" in Status for full both-level crossings, or the current
        # scan time for single-level/VWAP crossings (which are one-scan
        # events by design, so "now" IS when they fired).
        signal_time = None
        if status.startswith("JUST CROSSED"):
            try:
                signal_time = status.split("@ ")[1].split(" ")[0]
            except IndexError:
                signal_time = now_time_str
        elif simple_status in ("CROSSING SUPPORT FROM BELOW", "CROSSING RESISTANCE FROM ABOVE",
                                "CROSSED ABOVE VWAP FROM BELOW", "CROSSED BELOW VWAP FROM ABOVE"):
            signal_time = now_time_str

        results.append({
            "Symbol": symbol, "Sector": get_sector(symbol), "CurrentPrice": current_price,
            "EntryPrice": entry_price if entry_price is not None else current_price,
            "POC": poc, "VWAP": vwap,
            "DeltaSupport": support, "DeltaResistance": resistance,
            "NextSupport": next_support, "NextResistance": next_resistance,
            "NextLevelDistance%": next_level_distance_pct,
            "ZoneWidth%": zone_width_pct,
            "%Move": (
                round(((current_price - resistance) / resistance) * 100, 2) if is_above_both
                else round(((support - current_price) / support) * 100, 2) if is_below_both
                else None
            ),
            "DayChange%": day_change_pct,
            "RVOL%": rvol_pct, "Status": status, "SimpleStatus": simple_status,
            "SignalTime": signal_time,
            "VWAPSetup": vwap_setup_detail,
            "IntradayMA20Score": intraday_score["ma20_score"] if intraday_score else None,
            "IntradayRSIScore": intraday_score["rsi_score"] if intraday_score else None,
            "IntradayVolScore": intraday_score["vol_score"] if intraday_score else None,
            "IntradayFinalScore": intraday_score["final_score"] if intraday_score else None,
            "ATR": levels.get("atr"),
            "IsIndex": levels.get("is_index", False),
        })

    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)

    result_df = pd.DataFrame(results)
    if result_df.empty:
        return result_df, state, now_time_str

    def sort_key(row):
        if row.get("IsIndex"):
            return (-1, row["Symbol"])  # NIFTY/BANKNIFTY always first
        status = row["Status"]
        if status.startswith("JUST CROSSED"):
            time_part = status.split("@ ")[-1]
            return (0, time_part)
        elif status in ("ABOVE BOTH (continuing)", "BELOW BOTH (continuing)"):
            return (1, "")
        else:
            return (2, "")

    result_df["_sort"] = result_df.apply(sort_key, axis=1)
    result_df = result_df.sort_values("_sort").drop(columns=["_sort", "IsIndex"]).reset_index(drop=True)
    result_df.insert(0, "S.No", range(1, len(result_df) + 1))
    return result_df, state, now_time_str


# ---------------- Streamlit UI ----------------

st.set_page_config(page_title="F&O Delta Scanner", layout="wide")
st.title("F&O Delta Support/Resistance + POC + RVOL Scanner")

with st.sidebar:
    st.header("Setup")
    env_token = os.environ.get("UPSTOX_ACCESS_TOKEN", "")
    token = st.text_input("Upstox Access Token", value=env_token, type="password",
                           help="Set UPSTOX_ACCESS_TOKEN env var to skip this, or paste it here (session only).")

    st.divider()
    st.header("Step 1: Precompute (once per day)")
    st.caption("Run any time after ~9:20 AM. Slow (~1-2 min for full universe).")
    run_pre = st.button("Run Precompute", type="primary", use_container_width=True)

    st.divider()
    st.header("Composite Score Scan (MA50 + RSI + Volume)")
    st.caption("Separate from the precompute above - uses DAILY candles (~300 days) to rank "
               "symbols by trend/momentum/volume alignment. Changes slowly - run once a day.")
    run_score_scan = st.button("Run Composite Score Scan", use_container_width=True)

    st.divider()
    st.header("Step 2: Live Refresh")
    max_zone_width_pct = st.slider(
        "Max Zone Width % (flag entries wider than this)", 0.5, 5.0, 1.5, 0.1,
        help="If the gap between Delta Support and Delta Resistance exceeds this % of price, "
             "fresh crossover signals get flagged '(wide zone - caution)' instead of treated as clean entries."
    )
    atr_target_multiple = st.slider(
        "Target = ATR × this multiple (Trade Ideas / Signals)", 0.5, 4.0, 1.5, 0.1,
        help="Target is set at Entry Price ± (14-period ATR on 5-min bars) × this multiple, so it scales "
             "with how much THIS stock actually moves rather than a fixed % or the nearest swing point "
             "(which could be too close on a quiet stock, or too far on a volatile one). "
             "Falls back to the Reward:Risk-based projection below if ATR isn't available yet (e.g. "
             "cache built before this feature existed - re-run Precompute)."
    )
    min_reward_risk_ratio = st.slider(
        "Min Reward:Risk Ratio (safety-net filter)", 0.5, 3.0, 1.0, 0.1,
        help="Even with an ATR-based target, this still acts as a floor: if the ATR-projected target ends up "
             "less than this many times the StopLoss distance, the trade idea is dropped entirely. Also used "
             "directly as the projection multiple on the rare fallback path when ATR isn't available."
    )
    with st.expander("VWAP Reclaim Setup thresholds"):
        vwap_above_support_max_pct = st.slider(
            "Max VWAP above Support (%)", 0.1, 2.0, 0.5, 0.1,
            help="VWAP must sit no more than this % above Delta Support to count as 'just above'."
        )
        vwap_room_range = st.slider(
            "Resistance room above VWAP (%)", 0.5, 5.0, (1.0, 2.0), 0.1,
            help="Delta Resistance must be this far above VWAP - enough room to run, not already at the ceiling."
        )
    refresh_now = st.button("Refresh Live Data Now", use_container_width=True)

    auto_refresh = st.checkbox("Auto-refresh", value=False)
    refresh_interval = st.slider("Refresh every (seconds)", 30, 300, 60, disabled=not auto_refresh)
    if auto_refresh and not HAS_AUTOREFRESH:
        st.warning("Install streamlit-autorefresh for auto-refresh: "
                   "pip install streamlit-autorefresh --break-system-packages")
    if auto_refresh and HAS_AUTOREFRESH:
        st_autorefresh(interval=refresh_interval * 1000, key="auto_refresh_timer")

if not token:
    st.warning("Enter your Upstox access token in the sidebar to begin.")
    st.stop()

# Run precompute
if run_pre:
    progress_bar = st.progress(0, text="Starting precompute...")

    def progress_callback(i, total, symbol, result):
        if total > 0:
            progress_bar.progress(i / total, text=f"[{i}/{total}] {symbol}: {result}")
        else:
            progress_bar.progress(0, text=f"{symbol}: {result}")

    with st.spinner("Running precompute (this takes a while - one call per symbol)..."):
        cache, state = run_precompute(token, progress_callback)
    progress_bar.empty()
    st.success(f"Precompute complete: {len(cache)} symbols cached, "
               f"{sum(1 for k in state if k != '_date')} already-above/below continuation states seeded. "
               f"Fresh crossings will be detected live as they happen today.")
    st.session_state["cache"] = cache
    st.session_state["state"] = state

# Run composite score scan
if run_score_scan:
    score_progress_bar = st.progress(0, text="Starting composite score scan...")

    def score_progress_callback(i, total, symbol, result):
        if total > 0:
            score_progress_bar.progress(i / total, text=f"[{i}/{total}] {symbol}: {result}")
        else:
            score_progress_bar.progress(0, text=f"{symbol}: {result}")

    with st.spinner("Running composite score scan (one daily-candle call per symbol)..."):
        score_df_result = run_composite_scan(token, score_progress_callback)
    score_progress_bar.empty()
    st.success(f"Composite score scan complete: {len(score_df_result)} symbols scored.")
    st.session_state["score_df"] = score_df_result

if "score_df" not in st.session_state:
    if os.path.exists(SCORE_CACHE_PATH):
        st.session_state["score_df"] = pd.read_json(SCORE_CACHE_PATH, orient="records")
    else:
        st.session_state["score_df"] = pd.DataFrame()

score_df = st.session_state["score_df"]

# Load cache/state from disk if not in session
if "cache" not in st.session_state:
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            st.session_state["cache"] = json.load(f)
    else:
        st.session_state["cache"] = {}

if "state" not in st.session_state:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            st.session_state["state"] = json.load(f)
    else:
        st.session_state["state"] = {}

cache = st.session_state["cache"]

if not cache:
    st.info("No cached levels yet. Click 'Run Precompute' in the sidebar to get started.")
    st.stop()

st.caption(f"Cache has {len(cache)} symbols. Delta Support/Resistance levels computed through: "
           f"{next(iter(cache.values())).get('computed_date', 'unknown')} "
           f"(Upstox's historical data lags one full session behind - this is expected, not stale. "
           f"Live prices and fresh crossings below are real-time.)")

# Run live scan (on button, or auto-refresh trigger)
should_refresh = refresh_now or auto_refresh

if should_refresh:
    with st.spinner("Fetching live quotes..."):
        result_df, state, now_time_str = run_live_scan(
            cache, st.session_state["state"], token, max_zone_width_pct,
            vwap_above_support_max_pct, vwap_room_range[0], vwap_room_range[1]
        )
    st.session_state["state"] = state
    st.session_state["result_df"] = result_df
    st.session_state["last_update"] = now_time_str

if "result_df" not in st.session_state:
    st.info("Click 'Refresh Live Data Now' in the sidebar to fetch the latest scan.")
    st.stop()

result_df = st.session_state["result_df"]
last_update = st.session_state.get("last_update", "-")

st.subheader(f"Live Watchlist - last updated {last_update}")

intraday_df = result_df[
    result_df["Status"].str.startswith("JUST CROSSED") |
    result_df["Status"].isin(["ABOVE BOTH (continuing)", "BELOW BOTH (continuing)"]) |
    result_df["Symbol"].isin(["NIFTY", "BANKNIFTY"])
].copy()
intraday_df["S.No"] = range(1, len(intraday_df) + 1)


def highlight_status(row):
    status = row["Status"]
    if pd.isna(status) or not isinstance(status, str):
        return [""] * len(row)
    if "INVERTED ZONE" in status:
        return ["background-color: #5a2a4a; color: white"] * len(row)  # dark magenta - structurally unreliable
    elif "(wide zone" in status:
        return ["background-color: #4a4a2a; color: white"] * len(row)  # dim yellow-gray - caution, don't enter
    elif status.startswith("JUST CROSSED UP"):
        return ["background-color: #d4f7d4; color: black"] * len(row)  # green - fresh bullish
    elif status.startswith("JUST CROSSED DOWN"):
        return ["background-color: #f7d4d4; color: black"] * len(row)  # red - fresh bearish
    elif status == "ABOVE BOTH (continuing)":
        return ["background-color: #eaf5ff; color: black"] * len(row)  # light blue - continuing bullish
    elif status == "BELOW BOTH (continuing)":
        return ["background-color: #fff0e0; color: black"] * len(row)  # light orange - continuing bearish
    return [""] * len(row)


bullish_df = intraday_df[
    intraday_df["Status"].str.contains("UP", na=False) | (intraday_df["Status"] == "ABOVE BOTH (continuing)")
].copy()
bullish_df["_index_pin"] = bullish_df["Symbol"].isin(["NIFTY", "BANKNIFTY"])
bullish_df = bullish_df.sort_values(["_index_pin", "%Move"], ascending=[False, False], na_position="last").drop(columns="_index_pin").reset_index(drop=True)
bullish_df["S.No"] = range(1, len(bullish_df) + 1)

bearish_df = intraday_df[
    intraday_df["Status"].str.contains("DOWN", na=False) | (intraday_df["Status"] == "BELOW BOTH (continuing)")
].copy()
bearish_df["_index_pin"] = bearish_df["Symbol"].isin(["NIFTY", "BANKNIFTY"])
bearish_df = bearish_df.sort_values(["_index_pin", "%Move"], ascending=[False, False], na_position="last").drop(columns="_index_pin").reset_index(drop=True)
bearish_df["S.No"] = range(1, len(bearish_df) + 1)

# Sector Overview: breadth of stocks above VWAP per sector, plus counts of
# tight-zone fresh breakouts and VWAP-reclaim setups per sector
sector_rows = []
for sector, grp in result_df[result_df["Sector"] != "Index"].groupby("Sector"):
    total = len(grp)
    valid_vwap = grp["VWAP"].notna() & grp["CurrentPrice"].notna()
    above_vwap = ((grp["CurrentPrice"] > grp["VWAP"]) & valid_vwap).sum()
    pct_above_vwap = round((above_vwap / total) * 100, 1) if total > 0 else None
    tight_breakouts = grp["Status"].str.startswith("JUST CROSSED", na=False) & ~grp["Status"].str.contains("wide zone|INVERTED", na=False)
    vwap_setups = grp["VWAPSetup"].notna().sum()
    sector_rows.append({
        "Sector": sector, "TotalStocks": total, "AboveVWAP": above_vwap,
        "PctAboveVWAP": pct_above_vwap, "CleanBreakouts": int(tight_breakouts.sum()),
        "VWAPSetups": int(vwap_setups),
    })
sector_df = pd.DataFrame(sector_rows).sort_values("PctAboveVWAP", ascending=False).reset_index(drop=True)

vwap_setup_df = result_df[result_df["VWAPSetup"].notna()].copy()
vwap_setup_df = vwap_setup_df.sort_values("VWAPSetup").reset_index(drop=True)
if not vwap_setup_df.empty:
    vwap_setup_df = vwap_setup_df.drop(columns=["S.No"], errors="ignore")
    vwap_setup_df.insert(0, "S.No", range(1, len(vwap_setup_df) + 1))

# Heatmap data: signed move (positive = bullish, negative = bearish) so the
# treemap can color green/red like a standard market heatmap
heatmap_df = intraday_df[intraday_df["Symbol"] != ""].copy()
heatmap_df = heatmap_df[heatmap_df["Sector"].notna()]


def signed_move(row):
    status = str(row["Status"])
    move = row["%Move"]
    if pd.isna(move):
        move = 0.1  # give continuing-only rows a small nonzero size to stay visible
    if "UP" in status or status == "ABOVE BOTH (continuing)":
        return abs(move)
    elif "DOWN" in status or status == "BELOW BOTH (continuing)":
        return -abs(move)
    return 0


heatmap_df["SignedMove"] = heatmap_df.apply(signed_move, axis=1)
heatmap_df["BoxSize"] = heatmap_df["SignedMove"].abs().clip(lower=0.1)

# Simplified view: just Symbol, CurrentPrice, VWAP, RVOL%, and the 5 status
# categories - CROSSED UP, CROSSED BELOW, ABOVE BOTH, CROSSING SUPPORT FROM
# BELOW, CROSSING RESISTANCE FROM ABOVE. Only active signals, no extra columns.
simple_status_order = {
    "CROSSED UP": 0, "CROSSED BELOW": 1, "ABOVE BOTH": 2, "BELOW BOTH": 3,
    "CROSSING SUPPORT FROM BELOW": 4, "CROSSING RESISTANCE FROM ABOVE": 5,
    "CROSSED ABOVE VWAP FROM BELOW": 6, "CROSSED BELOW VWAP FROM ABOVE": 7,
}
simple_df = result_df[result_df["SimpleStatus"] != "-"].copy()
simple_df["_sort"] = simple_df["SimpleStatus"].map(simple_status_order).fillna(9)
simple_df["_index_pin"] = simple_df["Symbol"].isin(["NIFTY", "BANKNIFTY"])
simple_df = simple_df.sort_values(["_index_pin", "_sort"], ascending=[False, True]).drop(columns=["_sort", "_index_pin"])
simple_df = simple_df[["Symbol", "CurrentPrice", "DayChange%", "VWAP", "RVOL%", "SimpleStatus"]].reset_index(drop=True)
simple_df.insert(0, "S.No", range(1, len(simple_df) + 1))


def highlight_simple_status(row):
    s = row["SimpleStatus"]
    if s == "CROSSED UP":
        return ["background-color: #d4f7d4; color: black"] * len(row)
    elif s == "CROSSED BELOW":
        return ["background-color: #f7d4d4; color: black"] * len(row)
    elif s == "ABOVE BOTH":
        return ["background-color: #eaf5ff; color: black"] * len(row)
    elif s == "BELOW BOTH":
        return ["background-color: #fff0e0; color: black"] * len(row)
    elif s == "CROSSING SUPPORT FROM BELOW":
        return ["background-color: #c8f0d8; color: black"] * len(row)
    elif s == "CROSSING RESISTANCE FROM ABOVE":
        return ["background-color: #f0d8c8; color: black"] * len(row)
    elif s == "CROSSED ABOVE VWAP FROM BELOW":
        return ["background-color: #d8f0e8; color: black"] * len(row)
    elif s == "CROSSED BELOW VWAP FROM ABOVE":
        return ["background-color: #f0e0d8; color: black"] * len(row)
    return [""] * len(row)


# Dedicated NIFTY / BankNifty table - full detail since it's only 2 rows
index_cols = ["Symbol", "CurrentPrice", "DayChange%", "POC", "VWAP", "DeltaSupport", "DeltaResistance",
              "NextSupport", "NextResistance", "ZoneWidth%", "RVOL%", "Status", "SimpleStatus"]
index_df = result_df[result_df["Symbol"].isin(["NIFTY", "BANKNIFTY"])][index_cols].copy().reset_index(drop=True)

# Tomorrow's Watchlist: built from the cache directly (not the live scan),
# using last_close and the TRUE next-day support/resistance (the levels as
# they'll actually stand when tomorrow's session begins, after accounting
# for anything today itself triggered) - not the pre-today levels used for
# detecting today's own crossings. Sorted by proximity: closest to breaking
# out or down first, since those need watching most urgently tomorrow.
tomorrow_rows = []
for symbol, levels in cache.items():
    last_close = levels.get("last_close")
    nd_support = levels.get("next_day_support")
    nd_resistance = levels.get("next_day_resistance")
    if last_close is None or nd_support is None or nd_resistance is None:
        continue
    dist_to_resistance_pct = round(((nd_resistance - last_close) / last_close) * 100, 2)
    dist_to_support_pct = round(((last_close - nd_support) / last_close) * 100, 2)
    nd_zone_width_pct = round(((nd_resistance - nd_support) / last_close) * 100, 2)
    closest_pct = min(abs(dist_to_resistance_pct), abs(dist_to_support_pct))
    closest_side = "Resistance" if abs(dist_to_resistance_pct) <= abs(dist_to_support_pct) else "Support"
    tomorrow_rows.append({
        "Symbol": symbol, "LastClose": last_close,
        "NextDaySupport": nd_support, "NextDayResistance": nd_resistance,
        "ZoneWidth%": nd_zone_width_pct,
        "ClosestTo": closest_side, "DistanceToClosest%": round(closest_pct, 2),
    })

tomorrow_df = pd.DataFrame(tomorrow_rows)
if not tomorrow_df.empty:
    # Merge in LIVE data so this table shows both the plan (static, from
    # last close) AND what's actually happened since - the outcome.
    live_cols = result_df[["Symbol", "CurrentPrice", "Status", "RVOL%"]].copy()
    tomorrow_df = tomorrow_df.merge(live_cols, on="Symbol", how="left")
    tomorrow_df["Status"] = tomorrow_df["Status"].fillna("no live data yet")
    tomorrow_df["%MoveSinceClose"] = (
        (tomorrow_df["CurrentPrice"] - tomorrow_df["LastClose"]) / tomorrow_df["LastClose"] * 100
    ).round(2)

    tomorrow_df["_pin"] = tomorrow_df["Symbol"].isin(["NIFTY", "BANKNIFTY"])
    tomorrow_df = tomorrow_df.sort_values(
        ["_pin", "DistanceToClosest%"], ascending=[False, True]
    ).drop(columns="_pin").reset_index(drop=True)
    tomorrow_df.insert(0, "S.No", range(1, len(tomorrow_df) + 1))

    # Reorder: planning columns first, then the outcome columns
    tomorrow_df = tomorrow_df[[
        "S.No", "Symbol", "LastClose", "NextDaySupport", "NextDayResistance",
        "ZoneWidth%", "ClosestTo", "DistanceToClosest%",
        "CurrentPrice", "%MoveSinceClose", "Status", "RVOL%",
    ]]

# Tight-zone breakouts WITH ROOM: fresh full crossover (both levels), zone
# under 0.5%, AND at least 1% of clear space to the next level in that
# direction - high precision entry, not immediately capped
tight_room_cols = ["Symbol", "CurrentPrice", "VWAP", "ZoneWidth%", "NextLevelDistance%", "RVOL%", "Status"]
tight_breakout_room_df = result_df[
    result_df["Status"].str.startswith("JUST CROSSED", na=False) &
    (result_df["ZoneWidth%"].abs() < 0.5) &
    (result_df["NextLevelDistance%"] >= 1.0)
][tight_room_cols].copy()
tight_breakout_room_df["_pin"] = tight_breakout_room_df["Symbol"].isin(["NIFTY", "BANKNIFTY"])
tight_breakout_room_df = tight_breakout_room_df.sort_values(
    ["_pin", "NextLevelDistance%"], ascending=[False, False]
).drop(columns="_pin").reset_index(drop=True)
tight_breakout_room_df.insert(0, "S.No", range(1, len(tight_breakout_room_df) + 1))

# Wide-zone single-level crossings: price just reclaimed support (from
# below, confirmed by also being above VWAP) or just lost resistance (from
# above, confirmed by also being below VWAP), with a wide zone (>=1%) -
# looser, earlier setups than the tight-zone table above, with VWAP as a
# directional confirmation on top of the level crossing itself.
wide_single_cols = ["Symbol", "CurrentPrice", "VWAP", "ZoneWidth%", "RVOL%", "SimpleStatus"]
wide_single_level_df = result_df[
    (
        ((result_df["SimpleStatus"] == "CROSSING SUPPORT FROM BELOW") & (result_df["CurrentPrice"] > result_df["VWAP"])) |
        ((result_df["SimpleStatus"] == "CROSSING RESISTANCE FROM ABOVE") & (result_df["CurrentPrice"] < result_df["VWAP"]))
    ) &
    (result_df["ZoneWidth%"].abs() >= 1.0)
][wide_single_cols].copy()
wide_single_level_df["_pin"] = wide_single_level_df["Symbol"].isin(["NIFTY", "BANKNIFTY"])
wide_single_level_df = wide_single_level_df.sort_values(
    ["_pin", "ZoneWidth%"], ascending=[False, False]
).drop(columns="_pin").reset_index(drop=True)
wide_single_level_df.insert(0, "S.No", range(1, len(wide_single_level_df) + 1))

# Persistent daily log: since both tables above only show CURRENT state,
# anything that triggers and later reverts (or was only ever a single-scan
# event, as with Wide Zone Single-Level) would otherwise vanish without a
# trace. This accumulates every distinct trigger seen today for verification.
signal_log = load_signal_log()
signal_log = append_to_signal_log(signal_log, "Tight Zone + Room", tight_breakout_room_df, "Status")
signal_log = append_to_signal_log(signal_log, "Wide Zone Single-Level", wide_single_level_df, "SimpleStatus")
save_signal_log(signal_log)

log_df = pd.DataFrame(signal_log["entries"])
if not log_df.empty:
    log_df = log_df.sort_values("logged_at").reset_index(drop=True)
    log_df.insert(0, "S.No", range(1, len(log_df) + 1))
    log_df.columns = ["S.No", "Time", "Symbol", "Category", "CurrentPrice", "VWAP", "ZoneWidth%", "Detail"]

# ONE simple decision sheet: Symbol, Action (BUY/SELL), entry price,
# stop-loss, target, time signal fired. Only fresh events (not "continuing"),
# so the list stays short and each row is an actual actionable idea.
# Every row is validated for directional sanity before being included:
# for a BUY, Target must be genuinely above Price and StopLoss genuinely
# below (and vice versa for SELL) - a broken fallback level (e.g. from an
# inverted zone) that would put Target on the wrong side gets excluded
# rather than shown with a nonsensical number. Target is set from ATR (14-
# period, 5-min bars) x the sidebar's ATR multiple - scaling with how much
# THIS stock actually moves - rather than pulled from the nearest swing
# point (NextSupport/NextResistance), which could sit arbitrarily close to
# price on quiet stocks and produce razor-thin, cost-eating targets even
# when the old Reward:Risk-only check looked fine on paper. If ATR isn't
# cached yet for a symbol (re-run Precompute to populate it), falls back to
# projecting the target from the stop-loss distance x Min Reward:Risk Ratio.
# Either way, the Min Reward:Risk Ratio slider still acts as a final floor -
# an ATR-based target that ends up too small relative to its own stop gets
# dropped rather than shown.
BUY_STATUSES = ["CROSSED UP", "CROSSING SUPPORT FROM BELOW", "CROSSED ABOVE VWAP FROM BELOW"]
SELL_STATUSES = ["CROSSED BELOW", "CROSSING RESISTANCE FROM ABOVE", "CROSSED BELOW VWAP FROM ABOVE"]
MIN_TARGET_DISTANCE_PCT = 0.1
MIN_STOPLOSS_DISTANCE_PCT = 0.1

trade_rows = []
for _, row in result_df.iterrows():
    s = row["SimpleStatus"]
    price = row["EntryPrice"]
    if pd.isna(price):
        continue
    atr = row.get("ATR")
    has_atr = pd.notna(atr) and atr > 0

    if s in BUY_STATUSES:
        stop_loss = row["DeltaSupport"]
        if pd.isna(stop_loss) or stop_loss >= price:
            continue
        risk_pct = (price - stop_loss) / price * 100
        if risk_pct < MIN_STOPLOSS_DISTANCE_PCT:
            continue
        target = (price + atr * atr_target_multiple) if has_atr \
            else price + (price - stop_loss) * min_reward_risk_ratio
        reward_pct = (target - price) / price * 100
        if reward_pct < MIN_TARGET_DISTANCE_PCT:
            continue
        if reward_pct / risk_pct < min_reward_risk_ratio:
            continue
        trade_rows.append({
            "Symbol": row["Symbol"], "Action": "BUY", "Price": price,
            "LTP": row["CurrentPrice"],
            "StopLoss": stop_loss, "Target": round(target, 2),
            "RewardRisk": round(reward_pct / risk_pct, 2),
            "TargetSource": "ATR" if has_atr else "Ratio (ATR unavailable)",
            "RVOL%": row["RVOL%"],
            "Time": row["SignalTime"],
        })
    elif s in SELL_STATUSES:
        stop_loss = row["DeltaResistance"]
        if pd.isna(stop_loss) or stop_loss <= price:
            continue
        risk_pct = (stop_loss - price) / price * 100
        if risk_pct < MIN_STOPLOSS_DISTANCE_PCT:
            continue
        target = (price - atr * atr_target_multiple) if has_atr \
            else price - (stop_loss - price) * min_reward_risk_ratio
        reward_pct = (price - target) / price * 100
        if reward_pct < MIN_TARGET_DISTANCE_PCT:
            continue
        if reward_pct / risk_pct < min_reward_risk_ratio:
            continue
        trade_rows.append({
            "Symbol": row["Symbol"], "Action": "SELL", "Price": price,
            "LTP": row["CurrentPrice"],
            "StopLoss": stop_loss, "Target": round(target, 2),
            "RewardRisk": round(reward_pct / risk_pct, 2),
            "TargetSource": "ATR" if has_atr else "Ratio (ATR unavailable)",
            "RVOL%": row["RVOL%"],
            "Time": row["SignalTime"],
        })

trade_df = pd.DataFrame(trade_rows)
if not trade_df.empty:
    trade_df["_pin"] = trade_df["Symbol"].isin(["NIFTY", "BANKNIFTY"])
    trade_df = trade_df.sort_values(["_pin", "Action"], ascending=[False, True]).drop(columns="_pin").reset_index(drop=True)
    trade_df.insert(0, "S.No", range(1, len(trade_df) + 1))


def highlight_action(row):
    if row["Action"] == "BUY":
        return ["background-color: #d4f7d4; color: black"] * len(row)
    elif row["Action"] == "SELL":
        return ["background-color: #f7d4d4; color: black"] * len(row)
    return [""] * len(row)


# ---------------- Signals: one distilled BUY/SELL sheet ----------------
#
# Everything else in this dashboard is raw data across many tabs. This
# takes the same structural breakout signals as Trade Ideas above, and
# layers on 3 independent confirmations so only the highest-conviction
# ideas surface - the goal is a SHORT list you can act on without reading
# every tab yourself:
#   1. RVOL%      - is real volume actually behind this move?
#   2. Intraday Score - does the 5-min trend/momentum/volume score agree?
#   3. Sector tailwind - is this stock's sector moving the same direction
#                        today (using this dashboard's own Sector column
#                        and DayChange%, not an external source)?
# A signal needs at least 2 of these 3 to appear at all - anything with
# only the bare structural trigger and no confirmation is left out.
signals_df = pd.DataFrame()
if not trade_df.empty:
    extra_cols = result_df[["Symbol", "DayChange%", "IntradayFinalScore", "Sector"]].drop_duplicates("Symbol")
    signals_df = trade_df.drop(columns=["S.No"], errors="ignore").merge(extra_cols, on="Symbol", how="left")

    sector_avg_daychange = result_df.groupby("Sector")["DayChange%"].mean()

    def _score_confirmations(row):
        confirmations = 0
        reasons = []

        rvol = row.get("RVOL%")
        if pd.notna(rvol) and rvol >= 150:
            confirmations += 1
            reasons.append(f"RVOL {rvol:.0f}%")

        iscore = row.get("IntradayFinalScore")
        if pd.notna(iscore):
            if (row["Action"] == "BUY" and iscore > 0) or (row["Action"] == "SELL" and iscore < 0):
                confirmations += 1
                reasons.append(f"Intraday score {iscore:+.2f}")

        day_chg = row.get("DayChange%")
        if pd.notna(day_chg):
            if (row["Action"] == "BUY" and day_chg > 0) or (row["Action"] == "SELL" and day_chg < 0):
                confirmations += 1
                reasons.append(f"Day change {day_chg:+.2f}%")

        sec_avg = sector_avg_daychange.get(row.get("Sector"))
        if pd.notna(sec_avg):
            if (row["Action"] == "BUY" and sec_avg > 0) or (row["Action"] == "SELL" and sec_avg < 0):
                confirmations += 1
                reasons.append(f"{row.get('Sector')} sector +{sec_avg:.2f}%" if sec_avg > 0
                                else f"{row.get('Sector')} sector {sec_avg:.2f}%")

        return confirmations, "; ".join(reasons) if reasons else "Structural signal only - no confirmations"

    scored = signals_df.apply(_score_confirmations, axis=1)
    signals_df["Confirmations"] = scored.apply(lambda x: x[0])
    signals_df["Why"] = scored.apply(lambda x: x[1])

    def _confidence_label(n):
        if n >= 3:
            return "STRONG"
        elif n == 2:
            return "GOOD"
        else:
            return "WEAK"

    signals_df["Confidence"] = signals_df["Confirmations"].map(_confidence_label)

    # Keep only ideas with at least 2 of 3 confirmations - this is the
    # actual filtering step that turns "everything" into "a short list".
    signals_df = signals_df[signals_df["Confirmations"] >= 2].copy()
    signals_df["_pin"] = signals_df["Symbol"].isin(["NIFTY", "BANKNIFTY"])
    signals_df = signals_df.sort_values(
        ["_pin", "Confirmations", "RVOL%"], ascending=[False, False, False]
    ).drop(columns="_pin").reset_index(drop=True)
    signals_df.insert(0, "S.No", range(1, len(signals_df) + 1))
    signals_df = signals_df[["S.No", "Symbol", "Action", "Confidence", "Price", "LTP",
                              "StopLoss", "Target", "TargetSource", "RewardRisk", "RVOL%", "Time", "Why"]]

# Paper Trade Log - persist every Signals-tab idea the moment it first
# appears, and mark-to-market/close out existing OPEN entries against the
# latest scan. Runs every refresh (regardless of which tab you're looking
# at) so the log accumulates across the whole day, not just when you
# happen to be on this tab.
paper_trade_log = load_paper_trade_log()
if not signals_df.empty:
    paper_trade_log = log_new_paper_trades(paper_trade_log, signals_df)
paper_trade_log = update_paper_trade_statuses(paper_trade_log, result_df)
save_paper_trade_log(paper_trade_log)
paper_trade_df = build_paper_trade_df(paper_trade_log, result_df)


def highlight_signal_action(row):
    if row["Action"] == "BUY":
        return ["background-color: #d4f7d4; color: black"] * len(row)
    elif row["Action"] == "SELL":
        return ["background-color: #f7d4d4; color: black"] * len(row)
    return [""] * len(row)


tabSig, tabPT, tabTom, tabScore, tabIS, tabX, tabI, tabT, tabW, tabL, tab0, tab1, tab2, tab3, tab4, tab5, tab6, tab7, tabSR = st.tabs(
    ["🎯 Signals", "📒 Paper Trades", "Tomorrow's Watchlist", "Composite Score", "Intraday Score", "Trade Ideas",
     "NIFTY & BankNifty", "Tight Zone + Room", "Wide Zone Single-Level", "Signal Log (Today)", "Simple View",
     "Sector Movers", "Bullish (Up)", "Bearish (Down)", "Sector Overview", "VWAP Setups", "All Intraday",
     "Full Scan", "Sector Rotation"]
)

with tabSig:
    st.caption(
        "The short list: same structural breakout signals as Trade Ideas, but only shown here if at least "
        "2 of 3 independent checks agree - real volume behind the move (RVOL), the 5-min Intraday Score "
        "pointing the same direction, and the stock's sector moving the same way today. Confidence: "
        "STRONG = all 3 confirmations, GOOD = 2 of 3. If this tab is empty, nothing right now clears that bar - "
        "check Trade Ideas for the fuller, unfiltered list."
    )
    if signals_df.empty:
        st.write("No high-confidence signals right now.")
    else:
        st.dataframe(
            signals_df.style.apply(highlight_signal_action, axis=1),
            use_container_width=True,
            hide_index=True,
        )
        csv_signals = signals_df.to_csv(index=False).encode("utf-8")
        st.download_button("Download signals CSV", csv_signals, "fno_signals.csv", "text/csv")

with tabPT:
    st.caption(
        "Every Signals-tab idea gets logged here the moment it first appears - permanently, for the rest "
        "of the trading day - so opening this on your phone hours later still shows everything that fired "
        "since market open, not just what's live right now. OPEN trades are marked-to-market against the "
        "live price on every refresh; TARGET HIT / STOP HIT lock in once price actually reaches that level. "
        "Resets automatically at the start of each new trading day. "
        "NOTE: this only captures activity while the app is actually refreshing - leave Auto-refresh ON "
        "(sidebar) in a tab all day for this to genuinely cover the full session."
    )
    if paper_trade_df.empty:
        st.write("No paper trades logged yet today.")
    else:
        total = len(paper_trade_df)
        open_ct = (paper_trade_df["Status"] == "OPEN").sum()
        target_ct = (paper_trade_df["Status"] == "TARGET HIT").sum()
        stop_ct = (paper_trade_df["Status"] == "STOP HIT").sum()
        closed_ct = target_ct + stop_ct
        win_rate = round((target_ct / closed_ct) * 100, 1) if closed_ct > 0 else None
        avg_pnl = round(paper_trade_df["PnL%"].mean(), 2) if paper_trade_df["PnL%"].notna().any() else None

        m1, m2, m3, m4, m5, m6 = st.columns(6)
        m1.metric("Total Trades", total)
        m2.metric("Open", int(open_ct))
        m3.metric("Target Hit", int(target_ct))
        m4.metric("Stop Hit", int(stop_ct))
        m5.metric("Win Rate", f"{win_rate}%" if win_rate is not None else "-")
        m6.metric("Avg P&L%", f"{avg_pnl:+.2f}%" if avg_pnl is not None else "-")

        def highlight_paper_status(row):
            status = row["Status"]
            if status == "TARGET HIT":
                return ["background-color: #d4f7d4; color: black"] * len(row)
            elif status == "STOP HIT":
                return ["background-color: #f7d4d4; color: black"] * len(row)
            elif status == "OPEN":
                return ["background-color: #eaf5ff; color: black"] * len(row)
            return [""] * len(row)

        st.dataframe(
            paper_trade_df.style.apply(highlight_paper_status, axis=1),
            use_container_width=True,
            hide_index=True,
        )
        csv_paper = paper_trade_df.to_csv(index=False).encode("utf-8")
        st.download_button("Download paper trade log CSV", csv_paper, "fno_paper_trades.csv", "text/csv")


with tabTom:
    st.caption("First set of columns (LastClose through DistanceToClosest%) = the PLAN, fixed from last night's "
               "Precompute, unchanged all day. CurrentPrice/%MoveSinceClose/Status/RVOL% = what's ACTUALLY happened "
               "since, refreshed live. Sorted by DistanceToClosest% - stocks nearest to breaking out or down are "
               "listed first, since those needed watching most urgently at today's open.")
    if tomorrow_df.empty:
        st.write("No data available - run Precompute first.")
    else:
        st.dataframe(
            tomorrow_df.style.apply(highlight_status, axis=1),
            use_container_width=True,
            hide_index=True,
        )
        csv_tomorrow = tomorrow_df.to_csv(index=False).encode("utf-8")
        st.download_button("Download tomorrow's watchlist CSV", csv_tomorrow, "fno_tomorrows_watchlist.csv", "text/csv")

with tabScore:
    st.caption("Ranks symbols by a composite of trend (MA50 distance/slope + MA200 regime), RSI momentum, "
               "and volume-with-trend, each normalized to -1..+1 and summed into final_score. Uses DAILY "
               "candles, so it moves slowly day to day - run the scan from the sidebar once a day, not on "
               "every live refresh.")
    if score_df.empty:
        st.write("No data available - run the Composite Score Scan from the sidebar first.")
    else:
        st.dataframe(score_df, use_container_width=True, hide_index=True)
        csv_score = score_df.to_csv(index=False).encode("utf-8")
        st.download_button("Download composite scores CSV", csv_score, "fno_composite_scores.csv", "text/csv")

with tabIS:
    st.caption("Same idea as Composite Score (trend + RSI momentum + volume, each normalized to -1..+1 "
               "and summed into IntradayFinalScore), but built on 5-min bars instead of daily ones - so "
               "this updates on every live refresh, not once a day. Symbols cached before this feature was "
               "added will show blank scores until you re-run Precompute.")
    intraday_score_cols = ["Symbol", "Sector", "CurrentPrice", "RVOL%",
                            "IntradayMA20Score", "IntradayRSIScore", "IntradayVolScore", "IntradayFinalScore"]
    intraday_score_df = result_df[intraday_score_cols].copy()
    intraday_score_df = intraday_score_df[intraday_score_df["IntradayFinalScore"].notna()]
    if intraday_score_df.empty:
        st.write("No intraday scores available yet - run Precompute again to populate the new cache field, "
                 "then Refresh Live Data.")
    else:
        intraday_score_df = intraday_score_df.sort_values("IntradayFinalScore", ascending=False).reset_index(drop=True)
        intraday_score_df.insert(0, "S.No", range(1, len(intraday_score_df) + 1))
        st.dataframe(intraday_score_df, use_container_width=True, hide_index=True)
        csv_intraday_score = intraday_score_df.to_csv(index=False).encode("utf-8")
        st.download_button("Download intraday scores CSV", csv_intraday_score,
                            "fno_intraday_scores.csv", "text/csv")

with tabX:
    st.caption("BUY = fresh bullish event (crossed up, support reclaim, or VWAP reclaim). SELL = fresh bearish event. "
               "Price = entry price AT THE MOMENT the signal fired. LTP = current live price, for tracking how far "
               "it's moved since entry. StopLoss = the broken Delta Support/Resistance level. Target is PROJECTED "
               "from the StopLoss distance × the sidebar's Min Reward:Risk Ratio - not pulled from the nearest swing "
               "point, which could sit arbitrarily close to price and produce a razor-thin target on quiet stocks. "
               "RewardRisk = Target distance ÷ StopLoss distance (matches the sidebar ratio by construction). "
               "Time = when the underlying signal actually fired.")
    if trade_df.empty:
        st.write("No fresh trade ideas right now.")
    else:
        st.dataframe(
            trade_df.style.apply(highlight_action, axis=1),
            use_container_width=True,
            hide_index=True,
        )
        csv_trade = trade_df.to_csv(index=False).encode("utf-8")
        st.download_button("Download trade ideas CSV", csv_trade, "fno_trade_ideas.csv", "text/csv")

with tabL:
    st.caption("Every distinct Tight Zone+Room and Wide Zone Single-Level trigger seen today, for verification - "
               "including ones that later reverted or only appeared for a single scan. Resets at the start of each new day.")
    if log_df.empty:
        st.write("No signals logged yet today.")
    else:
        st.dataframe(log_df, use_container_width=True, hide_index=True)
        csv_log = log_df.to_csv(index=False).encode("utf-8")
        st.download_button("Download signal log CSV", csv_log, "fno_signal_log.csv", "text/csv")

with tabT:
    st.caption("Fresh full breakouts (both levels) where the zone is tight (<0.5%) AND there's at least 1% of clear "
               "room to the next level in that direction - high-precision entries that aren't immediately capped.")
    if tight_breakout_room_df.empty:
        st.write("No matching signals currently.")
    else:
        st.dataframe(tight_breakout_room_df, use_container_width=True, hide_index=True)
        csv_tr = tight_breakout_room_df.to_csv(index=False).encode("utf-8")
        st.download_button("Download tight-zone+room CSV", csv_tr, "fno_tight_zone_room.csv", "text/csv")

with tabW:
    st.caption("Price just reclaimed support (from below, confirmed above VWAP) or just lost resistance "
               "(from above, confirmed below VWAP), with a wide zone (>=1%) - a looser, single-level setup "
               "with VWAP as directional confirmation.")
    if wide_single_level_df.empty:
        st.write("No matching signals currently.")
    else:
        st.dataframe(wide_single_level_df, use_container_width=True, hide_index=True)
        csv_ws = wide_single_level_df.to_csv(index=False).encode("utf-8")
        st.download_button("Download wide-zone single-level CSV", csv_ws, "fno_wide_single_level.csv", "text/csv")

with tabI:
    st.caption("NIFTY and BANK NIFTY futures - full detail, always shown regardless of status.")
    if index_df.empty:
        st.write("No index futures data - run Precompute first.")
    else:
        st.dataframe(
            index_df.style.apply(highlight_status, axis=1),
            use_container_width=True,
            hide_index=True,
        )
        csv_index = index_df.to_csv(index=False).encode("utf-8")
        st.download_button("Download NIFTY/BankNifty CSV", csv_index, "fno_index_futures.csv", "text/csv")

with tab0:
    st.caption("Just the essentials: Symbol, CurrentPrice, VWAP, RVOL%, and status. "
               "CROSSED UP/BELOW = fresh full breakout. ABOVE/BELOW BOTH = continuing. "
               "CROSSING SUPPORT/RESISTANCE = single-level event, earlier signal.")
    if simple_df.empty:
        st.write("No active signals currently.")
    else:
        st.dataframe(
            simple_df.style.apply(highlight_simple_status, axis=1),
            use_container_width=True,
            hide_index=True,
        )
        csv_simple = simple_df.to_csv(index=False).encode("utf-8")
        st.download_button("Download simple view CSV", csv_simple, "fno_simple_view.csv", "text/csv")

with tab1:
    st.caption("Stocks grouped by sector, ordered by sector strength (% above VWAP). "
               "Within each sector, sorted by %Move - strongest movers first.")
    if heatmap_df.empty:
        st.write("No active signals to display currently.")
    else:
        sector_order = sector_df.sort_values("PctAboveVWAP", ascending=False)["Sector"].tolist()
        display_cols = ["Symbol", "CurrentPrice", "%Move", "RVOL%", "ZoneWidth%", "Status"]

        for sector in sector_order:
            sector_stocks = heatmap_df[heatmap_df["Sector"] == sector].copy()
            if sector_stocks.empty:
                continue
            sector_stocks = sector_stocks.sort_values("SignedMove", ascending=False)
            sector_breadth_row = sector_df[sector_df["Sector"] == sector]
            pct_above = sector_breadth_row["PctAboveVWAP"].iloc[0] if not sector_breadth_row.empty else None

            header = f"{sector}"
            if pct_above is not None:
                header += f" — {pct_above}% above VWAP"
            st.markdown(f"**{header}**")
            st.dataframe(
                sector_stocks[display_cols].style.apply(highlight_status, axis=1),
                use_container_width=True,
                hide_index=True,
            )

with tab2:
    if bullish_df.empty:
        st.write("No bullish signals currently.")
    else:
        st.caption("Sorted by %Move - how far price has moved above the broken Delta Resistance.")
        st.dataframe(
            bullish_df.style.apply(highlight_status, axis=1),
            use_container_width=True,
            hide_index=True,
        )
        csv_bull = bullish_df.to_csv(index=False).encode("utf-8")
        st.download_button("Download bullish CSV", csv_bull, "fno_bullish_watchlist.csv", "text/csv")

with tab3:
    if bearish_df.empty:
        st.write("No bearish signals currently.")
    else:
        st.caption("Sorted by %Move - how far price has moved below the broken Delta Support.")
        st.dataframe(
            bearish_df.style.apply(highlight_status, axis=1),
            use_container_width=True,
            hide_index=True,
        )
        csv_bear = bearish_df.to_csv(index=False).encode("utf-8")
        st.download_button("Download bearish CSV", csv_bear, "fno_bearish_watchlist.csv", "text/csv")

with tab4:
    st.caption("Breadth per sector: what % of stocks are trading above VWAP right now, plus clean breakout and VWAP-reclaim setup counts. "
               "A sector near 100% above VWAP with several breakouts suggests a genuine sector-wide move, not an isolated stock.")
    if sector_df.empty:
        st.write("No sector data available.")
    else:
        st.dataframe(sector_df, use_container_width=True, hide_index=True)
        csv_sector = sector_df.to_csv(index=False).encode("utf-8")
        st.download_button("Download sector overview CSV", csv_sector, "fno_sector_overview.csv", "text/csv")

with tab5:
    st.caption("VWAP sitting just above Delta Support with 1-2% room to Delta Resistance, and price currently above VWAP - "
               "a distinct setup from the support/resistance zone breakouts.")
    if vwap_setup_df.empty:
        st.write("No VWAP reclaim setups currently.")
    else:
        st.dataframe(vwap_setup_df, use_container_width=True, hide_index=True)
        csv_vwap = vwap_setup_df.to_csv(index=False).encode("utf-8")
        st.download_button("Download VWAP setups CSV", csv_vwap, "fno_vwap_setups.csv", "text/csv")

with tab6:
    if intraday_df.empty:
        st.write("No stocks currently above both delta levels.")
    else:
        st.dataframe(
            intraday_df.style.apply(highlight_status, axis=1),
            use_container_width=True,
            hide_index=True,
        )
        csv = intraday_df.to_csv(index=False).encode("utf-8")
        st.download_button("Download watchlist CSV", csv, "fno_intraday_watchlist.csv", "text/csv")

with tab7:
    st.dataframe(result_df, use_container_width=True, hide_index=True)
    csv_full = result_df.to_csv(index=False).encode("utf-8")
    st.download_button("Download full scan CSV", csv_full, "fno_live_full.csv", "text/csv")

with tabSR:
    render_sector_rotation_tab()
