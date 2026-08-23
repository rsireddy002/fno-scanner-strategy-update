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
from datetime import datetime, timedelta

import requests
import pandas as pd
import streamlit as st

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
    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=LOOKBACK_CALENDAR_DAYS)).strftime("%Y-%m-%d")
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


def run_precompute(token, progress_callback=None):
    symbols = load_symbol_universe()
    cache = {}
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
            cache[symbol] = levels
            if progress_callback:
                progress_callback(i, len(symbols), symbol, "ok")
        except Exception as e:
            if progress_callback:
                progress_callback(i, len(symbols), symbol, f"error: {e}")
            continue

    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)

    today_str = datetime.now().strftime("%Y-%m-%d")
    state = {"_date": today_str}
    for symbol, levels in cache.items():
        if levels.get("already_above_yesterday"):
            state[symbol] = {"status": "continuing"}
        elif levels.get("crossover_time"):
            state[symbol] = {"status": "crossed", "time": levels["crossover_time"]}
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)

    return cache, state


def fetch_batch_quotes(instrument_keys, token):
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Authorization": f"Bearer {token}"}
    all_data = {}
    for i in range(0, len(instrument_keys), BATCH_SIZE):
        chunk = instrument_keys[i:i + BATCH_SIZE]
        params = {"instrument_key": ",".join(chunk)}
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


def run_live_scan(cache, state, token):
    today_str = datetime.now().strftime("%Y-%m-%d")
    now_time_str = datetime.now().strftime("%H:%M")
    if state.get("_date") != today_str:
        state = {"_date": today_str}

    symbols = list(cache.keys())
    instrument_keys = [cache[s]["instrument_key"] for s in symbols]
    key_to_symbol = {cache[s]["instrument_key"]: s for s in symbols}

    quotes = fetch_batch_quotes(instrument_keys, token)

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

        current_price = q.get("last_price")
        today_volume = q.get("volume")

        rvol_pct = None
        baseline_vol = nearest_rvol_baseline(levels.get("rvol_baseline", {}), now_time_str)
        if baseline_vol and today_volume is not None and baseline_vol > 0:
            rvol_pct = round((today_volume / baseline_vol) * 100, 1)

        if support is None or resistance is None or current_price is None:
            status = "no prior delta zone yet"
        else:
            is_above_both = current_price > support and current_price > resistance
            prior_state = state.get(symbol)

            if is_above_both:
                if already_above_yesterday and prior_state is None:
                    status = "ABOVE BOTH (continuing)"
                    state[symbol] = {"status": "continuing"}
                elif prior_state is not None and prior_state.get("status") in ("crossed", "continuing"):
                    if prior_state.get("status") == "crossed":
                        status = f"JUST CROSSED @ {prior_state['time']}"
                    else:
                        status = "ABOVE BOTH (continuing)"
                else:
                    status = f"JUST CROSSED @ {now_time_str}"
                    state[symbol] = {"status": "crossed", "time": now_time_str}
            else:
                status = "-"
                if symbol in state:
                    del state[symbol]

        results.append({
            "Symbol": symbol, "CurrentPrice": current_price, "POC": poc,
            "DeltaSupport": support, "DeltaResistance": resistance,
            "RVOL%": rvol_pct, "Status": status,
        })

    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)

    result_df = pd.DataFrame(results)
    if result_df.empty:
        return result_df, state, now_time_str

    def sort_key(row):
        status = row["Status"]
        if status.startswith("JUST CROSSED"):
            return (0, status.replace("JUST CROSSED @ ", ""))
        elif status == "ABOVE BOTH (continuing)":
            return (1, "")
        else:
            return (2, "")

    result_df["_sort"] = result_df.apply(sort_key, axis=1)
    result_df = result_df.sort_values("_sort").drop(columns="_sort").reset_index(drop=True)
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
    st.header("Step 2: Live Refresh")
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
        progress_bar.progress(i / total, text=f"[{i}/{total}] {symbol}: {result}")

    with st.spinner("Running precompute (this takes a while - one call per symbol)..."):
        cache, state = run_precompute(token, progress_callback)
    progress_bar.empty()
    st.success(f"Precompute complete: {len(cache)} symbols cached, "
               f"{sum(1 for k in state if k != '_date')} known crossover states seeded.")
    st.session_state["cache"] = cache
    st.session_state["state"] = state

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

st.caption(f"Cache has {len(cache)} symbols. Last precomputed: "
           f"{next(iter(cache.values())).get('computed_date', 'unknown')}")

# Run live scan (on button, or auto-refresh trigger)
should_refresh = refresh_now or auto_refresh

if should_refresh:
    with st.spinner("Fetching live quotes..."):
        result_df, state, now_time_str = run_live_scan(cache, st.session_state["state"], token)
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
    result_df["Status"].str.startswith("JUST CROSSED") | (result_df["Status"] == "ABOVE BOTH (continuing)")
].copy()
intraday_df["S.No"] = range(1, len(intraday_df) + 1)


def highlight_status(row):
    if row["Status"].startswith("JUST CROSSED"):
        return ["background-color: #d4f7d4"] * len(row)
    elif row["Status"] == "ABOVE BOTH (continuing)":
        return ["background-color: #eaf5ff"] * len(row)
    return [""] * len(row)


tab1, tab2 = st.tabs(["Intraday Watchlist", "Full Scan"])

with tab1:
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

with tab2:
    st.dataframe(result_df, use_container_width=True, hide_index=True)
    csv_full = result_df.to_csv(index=False).encode("utf-8")
    st.download_button("Download full scan CSV", csv_full, "fno_live_full.csv", "text/csv")
