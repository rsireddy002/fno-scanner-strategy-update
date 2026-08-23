"""
fno_live_scanner.py

PHASE 2 of 2 - run this as often as you like during the trading session
(every few minutes, or on demand). Fast: one single batch API call for all
symbols' live price + volume, instead of one call per symbol.

Requires fno_levels_cache.json to exist first - run fno_levels_precompute.py
once earlier in the day to generate it.

Maintains fno_crossed_state.json across runs so it remembers the exact time
a stock FIRST crossed above both delta levels today, even if you rerun this
script later - it won't overwrite an earlier crossing time with a later one.

SETUP: $env:UPSTOX_ACCESS_TOKEN = "your_token_here"
"""

import os
import sys
import json
import time
from datetime import datetime

import requests
import pandas as pd

ACCESS_TOKEN = os.environ.get("UPSTOX_ACCESS_TOKEN", "")
CACHE_PATH = "fno_levels_cache.json"
STATE_PATH = "fno_crossed_state.json"
QUOTES_URL = "https://api.upstox.com/v2/market-quote/quotes"
BATCH_SIZE = 480  # stay under the 500 instrument-key limit per call


def load_cache():
    if not os.path.exists(CACHE_PATH):
        print(f"ERROR: {CACHE_PATH} not found. Run fno_levels_precompute.py first.")
        sys.exit(1)
    with open(CACHE_PATH) as f:
        return json.load(f)


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def fetch_batch_quotes(instrument_keys):
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Authorization": f"Bearer {ACCESS_TOKEN}"}
    all_data = {}
    for i in range(0, len(instrument_keys), BATCH_SIZE):
        chunk = instrument_keys[i:i + BATCH_SIZE]
        params = {"instrument_key": ",".join(chunk)}

        max_retries = 5
        for attempt in range(max_retries):
            resp = requests.get(QUOTES_URL, headers=headers, params=params, timeout=20)
            if resp.status_code == 429:
                wait = 2 ** attempt  # 1, 2, 4, 8, 16 seconds
                print(f"Rate limited (429). Waiting {wait}s before retry {attempt + 1}/{max_retries}...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            payload = resp.json()
            all_data.update(payload.get("data", {}))
            break
        else:
            print(f"Gave up on a batch after {max_retries} retries due to persistent rate limiting.")
    return all_data


def nearest_rvol_baseline(rvol_baseline, current_time_str):
    """Find the baseline entry at or before the current time-of-day."""
    if not rvol_baseline:
        return None
    candidates = [t for t in rvol_baseline.keys() if t <= current_time_str]
    if not candidates:
        return None
    closest = max(candidates)
    return rvol_baseline[closest]


def main():
    if not ACCESS_TOKEN:
        print("ERROR: No Upstox access token found. Set it first:")
        print('  $env:UPSTOX_ACCESS_TOKEN = "your_token_here"')
        sys.exit(1)

    cache = load_cache()
    state = load_state()

    today_str = datetime.now().strftime("%Y-%m-%d")
    now_time_str = datetime.now().strftime("%H:%M")

    if state.get("_date") != today_str:
        state = {"_date": today_str}

    symbols = list(cache.keys())
    instrument_keys = [cache[s]["instrument_key"] for s in symbols]
    key_to_symbol = {cache[s]["instrument_key"]: s for s in symbols}

    print(f"Fetching live quotes for {len(instrument_keys)} symbols in batch...")
    quotes = fetch_batch_quotes(instrument_keys)
    print(f"Got {len(quotes)} quotes back.\n")

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
            "Symbol": symbol,
            "CurrentPrice": current_price,
            "POC": poc,
            "DeltaSupport": support,
            "DeltaResistance": resistance,
            "RVOL%": rvol_pct,
            "Status": status,
        })

    save_state(state)

    result_df = pd.DataFrame(results)

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

    intraday_df = result_df[
        result_df["Status"].str.startswith("JUST CROSSED") | (result_df["Status"] == "ABOVE BOTH (continuing)")
    ].copy()
    intraday_df["S.No"] = range(1, len(intraday_df) + 1)

    print("=" * 100)
    print(f"INTRADAY WATCHLIST @ {now_time_str} - Stocks Above Both Delta Support & Resistance")
    print("=" * 100)
    print(intraday_df.to_string(index=False) if not intraday_df.empty else "None currently.")

    result_df.to_csv("fno_live_full.csv", index=False)
    intraday_df.to_csv("fno_intraday_watchlist.csv", index=False)
    print(f"\nSaved full scan to fno_live_full.csv, watchlist to fno_intraday_watchlist.csv")


if __name__ == "__main__":
    main()
