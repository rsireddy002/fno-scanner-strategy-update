"""
paper_trader.py

Live paper trading engine. No real orders are ever placed -- this only
simulates entries/exits against real live prices, so you can validate the
strategy before risking capital.

ENTRY RULE (first-candle breakout, both directions):

  1. Wait for the first 5-minute candle to close (~9:20 AM IST).
  2. Run the Delta Zone breakout scan ONCE (delta_zone_scanner.run_scan) --
     qualifying filter: only stocks meeting POC/VWAP/support/resistance
     breakout conditions (in EITHER direction) are eligible at all. Each
     qualifying row carries a "Side" of "long" or "short".
  3. From that breakout table, rank by RVOL % descending, take the top 5.
  4. For each of those 5, fetch the first 5-minute candle, then branch on
     the row's Side:

     LONG:
       - GREEN first candle -> enter at candle CLOSE, stop = candle LOW.
       - RED first candle -> VWAP-reclaim override: this stock is only
         here because it's in the (long) breakout table, which already
         confirms LTP > VWAP and above both zones -- that membership IS
         the reclaim confirmation. Entry = current LTP, stop = candle LOW.

     SHORT (exact mirror):
       - RED first candle -> enter at candle CLOSE, stop = candle HIGH.
       - GREEN first candle -> VWAP-breakdown override: membership in the
         (short) breakout table already confirms LTP < VWAP and below
         both zones. Entry = current LTP, stop = candle HIGH.

  5. No fixed profit target. Exit ONLY on stop-loss or EOD square-off.

This runs ONCE per day (not every poll cycle) -- a single Delta Zone scan
plus up to 5 intraday-candle fetches. Lighter on the API than continuous
polling, and matches the "enter on the close of the first candle" timing.

EXIT: every poll cycle, cheap (reuses already-fetched LTP, zero extra API
calls). Only two exit paths: stop-loss hit (direction-aware: for longs
that's LTP falling to/through stop; for shorts it's LTP rising to/through
stop), or EOD square-off.

PERSISTENCE: state lives in memory for the running app session only. Does
NOT survive an app reboot (Streamlit Cloud's filesystem resets). Same-
session log, not a permanent trade journal.
"""

import threading
import time
import datetime
from zoneinfo import ZoneInfo

from delta_zone_scanner import run_scan
from upstox_downloads import download_intraday_candles

IST = ZoneInfo("Asia/Kolkata")

MAX_OPEN_POSITIONS = 5           # take the top 5 RVOL candidates from the breakout table
FIRST_CANDLE_WINDOW_MINUTES = 5
FIRST_CANDLE_CLOSE_TIME = datetime.time(9, 20)  # 9:15 session open + 5 min
EOD_SQUAREOFF_TIME = datetime.time(15, 20)      # IST, matches backtest_one_day.py
POSITION_QTY = 1  # shares per paper position -- signal-quality check, not real sizing
INTRADAY_INTERVAL = "1minute"


class PaperTradeState:
    """Thread-safe container, same pattern as ScannerState in fno_scanner_app.py."""

    def __init__(self):
        self.lock = threading.Lock()
        self.open_positions = {}   # symbol -> position dict
        self.closed_trades = []    # list of closed trade dicts
        self.enabled = False       # start OFF by default -- explicit opt-in
        self.entries_processed_date = None  # date object; ensures the one-shot
                                              # entry logic runs only once per day

    def get(self):
        with self.lock:
            return dict(self.open_positions), list(self.closed_trades), self.enabled

    def set_enabled(self, value: bool):
        with self.lock:
            self.enabled = value

    def _open(self, symbol, side, entry, stop, entry_time):
        with self.lock:
            self.open_positions[symbol] = {
                "symbol": symbol, "side": side, "entry": entry, "stop": stop,
                "entry_time": entry_time, "qty": POSITION_QTY,
                "ltp": entry, "unrealized_pnl": 0.0,
            }

    def _update_ltp(self, symbol, ltp):
        with self.lock:
            pos = self.open_positions.get(symbol)
            if pos:
                pos["ltp"] = ltp
                direction = 1 if pos["side"] == "long" else -1
                pos["unrealized_pnl"] = round((ltp - pos["entry"]) * direction * pos["qty"], 2)

    def _close(self, symbol, exit_price, reason, exit_time):
        with self.lock:
            pos = self.open_positions.pop(symbol, None)
            if not pos:
                return
            direction = 1 if pos["side"] == "long" else -1
            risk_distance = abs(pos["entry"] - pos["stop"])
            r_multiple = ((exit_price - pos["entry"]) * direction) / risk_distance if risk_distance else 0
            pnl = round((exit_price - pos["entry"]) * direction * pos["qty"], 2)
            self.closed_trades.append({
                **pos, "exit": exit_price, "exit_reason": reason, "exit_time": exit_time,
                "r_multiple": round(r_multiple, 2), "pnl": pnl,
            })


def _is_eod(now_ist: datetime.datetime) -> bool:
    return now_ist.time() >= EOD_SQUAREOFF_TIME


def manage_exits(pt_state: PaperTradeState, equity_by_token: dict, universe: dict, now_ist: datetime.datetime):
    """
    Only two exit paths: stop-loss hit, or EOD square-off. No profit target,
    no VWAP-invalidation exit -- per the updated rule set.
    Uses LTP already fetched this poll cycle -- zero extra API calls.
    """
    open_positions, _, _ = pt_state.get()
    eod = _is_eod(now_ist)

    for symbol, pos in open_positions.items():
        keys = universe.get(symbol)
        if not keys:
            continue
        equity_quote = equity_by_token.get(keys["equity_key"])
        if not equity_quote:
            continue

        ltp = equity_quote.get("last_price", 0) or 0
        if not ltp:
            continue

        pt_state._update_ltp(symbol, ltp)

        if eod:
            pt_state._close(symbol, ltp, "eod_squareoff", now_ist.strftime("%H:%M:%S"))
        elif pos["side"] == "long" and ltp <= pos["stop"]:
            pt_state._close(symbol, pos["stop"], "stop", now_ist.strftime("%H:%M:%S"))
        elif pos["side"] == "short" and ltp >= pos["stop"]:
            pt_state._close(symbol, pos["stop"], "stop", now_ist.strftime("%H:%M:%S"))


def _get_first_candle(equity_key: str, access_token: str, window: int = FIRST_CANDLE_WINDOW_MINUTES):
    """
    Fetch today's intraday 1-min candles, return the first `window` candles'
    aggregate: open (of the 1st), close (of the last), low (min across all),
    high (max across all), and whether it's green (close >= open).
    Returns None if not enough candles have printed yet.
    """
    raw = download_intraday_candles(equity_key, INTRADAY_INTERVAL, access_token)
    if not raw or len(raw) < window:
        return None
    rows = sorted(raw, key=lambda r: r[0])[:window]  # [ts, o, h, l, c, v, oi]
    candle_open = float(rows[0][1])
    candle_close = float(rows[-1][4])
    candle_low = min(float(r[3]) for r in rows)
    candle_high = max(float(r[2]) for r in rows)
    is_green = candle_close >= candle_open
    return {"open": candle_open, "close": candle_close, "low": candle_low,
            "high": candle_high, "is_green": is_green}


def run_first_candle_entries(pt_state: PaperTradeState, universe: dict, access_token: str,
                              now_ist: datetime.datetime, progress_callback=None):
    """
    ONE-SHOT per day: once the first 5-min candle has closed, run the Delta
    Zone breakout scan, take the top 5 by RVOL % from that qualifying table,
    and enter LONG or SHORT per each row's Side (see module docstring for
    the full branching rule).

    Guarded so this only actually runs once per calendar day, regardless of
    how many poll cycles call it.
    """
    _, _, enabled = pt_state.get()
    if not enabled:
        return
    if now_ist.time() < FIRST_CANDLE_CLOSE_TIME:
        return
    if pt_state.entries_processed_date == now_ist.date():
        return  # already ran today

    pt_state.entries_processed_date = now_ist.date()  # mark immediately -- don't retry on failure mid-run

    breakout_df = run_scan(universe, access_token, progress_callback=progress_callback)
    if breakout_df is None or breakout_df.empty or "RVOL %" not in breakout_df.columns:
        return

    top5 = breakout_df.dropna(subset=["RVOL %"]).sort_values("RVOL %", ascending=False).head(MAX_OPEN_POSITIONS)

    for _, row in top5.iterrows():
        symbol = row["Symbol"]
        side = row.get("Side")
        if side not in ("long", "short"):
            continue
        keys = universe.get(symbol)
        if not keys:
            continue
        try:
            candle = _get_first_candle(keys["equity_key"], access_token)
        except Exception as e:
            print(f"[paper_trader] {symbol}: first-candle fetch failed ({e}), skipping.")
            continue
        if not candle:
            continue

        if side == "long":
            if candle["is_green"]:
                entry_price, stop_price = candle["close"], candle["low"]
            else:
                # RED first candle, VWAP-reclaim override -- membership in
                # the long breakout table already confirms the reclaim.
                entry_price, stop_price = row["LTP"], candle["low"]
        else:  # side == "short", exact mirror
            if not candle["is_green"]:  # RED = trend-confirming color for shorts
                entry_price, stop_price = candle["close"], candle["high"]
            else:
                # GREEN first candle, VWAP-breakdown override -- membership
                # in the short breakout table already confirms the breakdown.
                entry_price, stop_price = row["LTP"], candle["high"]

        pt_state._open(symbol, side, entry_price, stop_price, now_ist.strftime("%H:%M:%S"))
        time.sleep(0.15)  # same pacing as delta_zone_scanner's run_scan
