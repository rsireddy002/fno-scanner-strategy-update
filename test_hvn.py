from hvn_lvn import build_volume_profile, find_hvn_lvn
from fetch_helpers import fetch_candles, resolve_futures_instrument_key
from token_helper import get_token

token = get_token()

instrument_key = resolve_futures_instrument_key("NIFTY", token)
if instrument_key is None:
    raise RuntimeError("Could not resolve NIFTY futures instrument key - check token/API response")

df = fetch_candles(instrument_key, token)
if df.empty:
    raise RuntimeError("fetch_candles returned no data - check token validity and market hours")

today = df["date"].max()
today_df = df[df["date"] == today]

print("Instrument key:", instrument_key)
print("Candles fetched:", len(df), "| Most recent session candles:", len(today_df))
print(f"Most recent session price range: {today_df['low'].min():.2f} - {today_df['high'].max():.2f}")
print(f"Full 18-day price range: {df['low'].min():.2f} - {df['high'].max():.2f}")

print("\n=== Single-day profile, bin_size=10 ===")
price_bins, volumes = build_volume_profile(today_df, bin_size=10.0)
result = find_hvn_lvn(price_bins, volumes)
print("HVNs:", result["hvns"])
print("LVNs:", result["lvns"])

print("\n=== Composite 18-day profile, bin_size=15 ===")
price_bins, volumes = build_volume_profile(df, bin_size=15.0)
result = find_hvn_lvn(price_bins, volumes)
print("HVNs:", result["hvns"])
print("LVNs:", result["lvns"])
