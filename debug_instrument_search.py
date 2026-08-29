import json
import requests
from token_helper import get_token

token = get_token()

url = "https://api.upstox.com/v2/instruments/search"
headers = {"Content-Type": "application/json", "Accept": "application/json",
           "Authorization": f"Bearer {token}"}
params = {
    "query": "NIFTY", "exchanges": "NSE", "segments": "FO",
    "instrument_types": "FUT", "expiry": "current_month",
    "page_number": 1, "records": 30,
}

resp = requests.get(url, headers=headers, params=params, timeout=20)
print("HTTP status:", resp.status_code)
print("Raw response:")
print(json.dumps(resp.json(), indent=2)[:3000])  # first 3000 chars - full payload may be long
