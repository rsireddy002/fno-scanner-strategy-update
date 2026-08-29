import requests
from token_helper import get_token

token = get_token()
url = "https://api.upstox.com/v2/instruments/search"
headers = {"Content-Type": "application/json", "Accept": "application/json",
           "Authorization": f"Bearer {token}"}

# Each variant removes/changes one filter at a time from the original,
# so whichever variant suddenly returns results tells us the culprit.
variants = {
    "1_original":            {"query": "NIFTY", "exchanges": "NSE", "segments": "FO", "instrument_types": "FUT", "expiry": "current_month"},
    "2_no_expiry":           {"query": "NIFTY", "exchanges": "NSE", "segments": "FO", "instrument_types": "FUT"},
    "3_no_instrument_type":  {"query": "NIFTY", "exchanges": "NSE", "segments": "FO", "expiry": "current_month"},
    "4_no_segments":         {"query": "NIFTY", "exchanges": "NSE", "instrument_types": "FUT", "expiry": "current_month"},
    "5_query_only":          {"query": "NIFTY"},
    "6_query_exchange_only": {"query": "NIFTY", "exchanges": "NSE"},
}

for label, params in variants.items():
    full_params = {**params, "page_number": 1, "records": 10}
    resp = requests.get(url, headers=headers, params=full_params, timeout=20)
    try:
        payload = resp.json()
        total = payload.get("meta_data", {}).get("page", {}).get("total_records", "?")
        sample = payload.get("data", [])[:2]
    except Exception:
        total = "parse_error"
        sample = resp.text[:200]
    print(f"{label}: params={params}")
    print(f"  HTTP {resp.status_code} | total_records={total}")
    if sample:
        for item in sample:
            print(f"  -> {item.get('trading_symbol')} | type={item.get('instrument_type')} | "
                  f"underlying={item.get('underlying_symbol')} | expiry={item.get('expiry')} | "
                  f"key={item.get('instrument_key')}")
    print()
