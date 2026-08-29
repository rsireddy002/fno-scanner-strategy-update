"""
token_helper.py

Loads the Upstox access token from, in order:
  1. The UPSTOX_ACCESS_TOKEN environment variable (if set in this session)
  2. A local file named upstox_token.txt sitting next to this script

This avoids the recurring problem of $env:UPSTOX_ACCESS_TOKEN only lasting
for one terminal session/tab - paste the token into upstox_token.txt once
and every script here can find it regardless of which terminal you're in.

SECURITY NOTE: upstox_token.txt contains a live credential. Make sure it's
listed in .gitignore so it never gets committed/pushed to GitHub. It should
NOT go in the fno-scanner-strategy-update -> nifty-banknifty-fno-scanner
copy step either - each machine should have its own local file.
"""
import os

_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "upstox_token.txt")


def get_token():
    token = os.environ.get("UPSTOX_ACCESS_TOKEN")
    if token:
        return token.strip()

    if os.path.exists(_TOKEN_FILE):
        with open(_TOKEN_FILE, "r") as f:
            token = f.read().strip()
        if token and token != "PASTE_YOUR_TOKEN_HERE":
            return token

    raise RuntimeError(
        f"No Upstox token found. Either set $env:UPSTOX_ACCESS_TOKEN, "
        f"or paste your token into {_TOKEN_FILE} (replacing the placeholder line)."
    )
