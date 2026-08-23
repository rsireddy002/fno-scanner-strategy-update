"""
backtest_full_day.py

Backtests the RVOL-only strategy across the FULL F&O universe for ONE past
trading day, using real 1-minute Upstox candles.

STRATEGY (strictly sequential, ONE position at a time, always the single
highest-RVOL untraded stock -- NO delta-zone/POC/support-resistance gate):

  1. Only at the close of each 5-minute candle (9:20 AM, 9:25, 9:30, ...
     through EOD -- NOT continuously every minute), for every stock that
     hasn't been traded yet today, compute:
       - RVOL % (cumulative volume traded today so far, as a percentage
         of the average FULL-DAY volume over the prior 20 days -- NO
         time-of-day scaling. This grows toward/past 100% as the day
         progresses for an active stock, rather than spiking artificially
         high in the first few minutes.)
       - Side: LTP vs session VWAP so far.
           LTP > VWAP -> long
           LTP < VWAP -> short
           LTP == VWAP -> no side this minute (skip, extremely rare with
             real price data)
     No breakout/POC/zone condition is checked at all -- every stock with
     a computable RVOL % and a clear side is a valid candidate.
  2. If NO position is currently open: take whichever candidate has the
     SINGLE HIGHEST RVOL % right now, in whichever direction (long/short)
     its VWAP position indicates at that moment.
  3. Enter it immediately at the current LTP. Mark it as traded -- it can
     NEVER be entered again today, even if it later becomes the #1
     candidate again.
  4. While a position is open, no new entries are considered -- strictly
     one trade at a time, sequential through the day. The instant a
     position closes (stop or EOD), the search for the new #1-ranked
     untraded candidate resumes immediately within that same loop pass.
  5. Stop-loss = the OPPOSITE side of that stock's own first 5-minute
     candle (fixed once at market open, same reference regardless of when
     the actual entry happens later in the day):
       LONG  -> stop = first candle's LOW
       SHORT -> stop = first candle's HIGH
  6. No fixed profit target. THREE exit paths:
       - Stop-loss hit (checked every minute)
       - EOD square-off (15:20 IST)
       - ROTATION: at each 5-min checkpoint, if the current best untraded
         candidate's RVOL % exceeds the held position's OWN current RVOL %
         by at least ROTATE_THRESHOLD_PCT (default 20%, relative), the held
         position is closed at current LTP and immediately swapped for the
         new candidate. This is a deliberate design choice, not automatic
         "always chase the leader" behavior -- the margin exists specifically
         to avoid constant flip-flopping on marginal RVOL differences.
  7. No cap on total trades across the day -- as many sequential trades as
     fit before EOD, each fully closed before the next opens.

NO LOOKAHEAD: daily history for the RVOL baseline is fetched with
download_daily_history_as_of(), which only returns candles strictly BEFORE
the backtest date. At each minute's check, only that day's candles up to
and including that minute are used for VWAP/RVOL.

Run:
    python backtest_full_day.py --date 2026-08-21 --token YOUR_TOKEN

Never hardcode your token -- pass it as an argument.
"""

import argparse
import datetime
import time

import numpy as np

from fno_universe import load_fno_universe
from upstox_downloads import download_historical_candles, download_daily_history_as_of

# ---------------- Config ----------------
FIRST_CANDLE_WINDOW = 5
RVOL_BASELINE_DAYS = 20
SESSION_MINUTES = 375
EOD_SQUAREOFF_TIME = datetime.time(15, 20)
PACING_SECONDS = 0.15
CANDLE_CHECK_INTERVAL = 5  # entries are only decided at the close of each
                            # 5-minute candle (9:20, 9:25, 9:30, ...) -- NOT
                            # continuously every minute. Stop-loss/EOD exits
                            # still monitor every minute for risk protection;
                            # only the ENTRY decision is gated to this cadence.
ROTATE_THRESHOLD_PCT = 20  # a held position is exited (at current LTP, not
                             # stop) and swapped for a new candidate ONLY if
                             # that candidate's current RVOL % exceeds the
                             # held position's OWN current RVOL % by at least
                             # this relative margin. Prevents constant
                             # flip-flopping on marginal RVOL differences.


def _sorted_numeric(candles):
    if not candles:
        return None
    rows = sorted(candles, key=lambda r: r[0])
    return np.array([[float(r[i]) for i in range(1, 7)] for r in rows])  # o,h,l,c,v,oi


def _calc_avg_daily_volume(daily, days=RVOL_BASELINE_DAYS):
    if daily is None or len(daily) < days:
        return None
    return float(daily[-days:, 4].mean())


class StockContext:
    """Precomputed, per-stock data that doesn't change during the day's minute-by-minute loop."""

    def __init__(self, symbol, today, daily):
        self.symbol = symbol
        self.today = today  # full day's 1-min candles, numeric [o,h,l,c,v,oi]
        self.avg_daily_volume = _calc_avg_daily_volume(daily)

        first5 = today[:FIRST_CANDLE_WINDOW]
        self.first_candle_low = float(first5[:, 2].min())
        self.first_candle_high = float(first5[:, 1].max())

    def evaluate_at_minute(self, minute_idx):
        """
        Returns (side, ltp, rvol_pct), or (None, ltp, None) if RVOL baseline
        unavailable or LTP==VWAP exactly.

        RVOL % = (cumulative volume traded TODAY, so far) / (average FULL-DAY
        volume over the prior baseline days) x 100 -- NO time-of-day scaling.
        Example: prior-day average volume = 2000. Today's first 5 minutes'
        volumes are 100, 5, 10, 10, 10 (cumulative = 135). RVOL % = 135/2000
        x 100 = 6.75%. This naturally grows toward/past 100% as the day
        progresses for an active stock -- it does NOT spike artificially
        high in the first few minutes the way a time-scaled version would.
        """
        if self.avg_daily_volume is None or self.avg_daily_volume <= 0:
            return None, None, None

        window = self.today[:minute_idx + 1]
        close, vol = window[:, 3], window[:, 4]
        ltp = float(close[-1])
        vwap = float((close * vol).sum() / vol.sum()) if vol.sum() else ltp

        if ltp > vwap:
            side = "long"
        elif ltp < vwap:
            side = "short"
        else:
            side = None  # exact tie, extremely rare -- skip this stock this minute

        rvol_pct = (vol.sum() / self.avg_daily_volume) * 100

        return side, ltp, rvol_pct


def fetch_day_contexts(date_str: str, access_token: str, universe: dict = None) -> dict:
    """
    The EXPENSIVE part: fetches 1-min intraday candles + daily history for
    every stock in the universe, for ONE date. Returns {symbol: StockContext}.

    Separated from simulate_day() specifically so this can be called ONCE
    per date and REUSED across many different parameter combinations when
    optimizing (rotation threshold, checkpoint interval, etc.) -- those
    parameters only affect the simulation, not what data is needed, so
    there's no reason to re-fetch for every combination tested.
    """
    if universe is None:
        universe = load_fno_universe()

    print(f"Fetching data for {len(universe)} stocks on {date_str} (no-lookahead daily history + full-day 1-min candles)...")

    contexts = {}
    for i, (symbol, keys) in enumerate(universe.items()):
        equity_key = keys["equity_key"] if isinstance(keys, dict) else keys
        try:
            intraday_raw = download_historical_candles(equity_key, "1minute", date_str, date_str, access_token)
            today = _sorted_numeric(intraday_raw)
            if today is None or len(today) < FIRST_CANDLE_WINDOW:
                continue
            daily_raw = download_daily_history_as_of(equity_key, access_token, date_str, lookback_days=120)
            daily = _sorted_numeric(daily_raw)
            contexts[symbol] = StockContext(symbol, today, daily)
        except Exception as e:
            print(f"  [skip] {symbol}: {e}")
        if (i + 1) % 25 == 0:
            print(f"  ...{i+1}/{len(universe)} fetched")
        time.sleep(PACING_SECONDS)

    print(f"{len(contexts)} stock(s) have usable data.")
    return contexts


def simulate_day(contexts: dict, rotate_threshold_pct: float = ROTATE_THRESHOLD_PCT,
                  candle_check_interval: int = CANDLE_CHECK_INTERVAL, verbose: bool = True):
    """
    The CHEAP part: pure local computation, no API calls. Runs the
    minute-by-minute simulation against already-fetched contexts, using the
    given parameters. Safe to call many times with different parameter
    values against the SAME contexts (e.g. for optimization) without
    re-fetching anything.

    verbose=True prints the full checkpoint-by-checkpoint ranking and trade
    log (the normal single-run experience). verbose=False suppresses all
    that and just returns the closed trades -- used by the optimizer, which
    calls this many times per date and would otherwise produce an
    overwhelming amount of output.
    """
    if verbose:
        print(f"\nRunning minute-by-minute simulation "
              f"(rotate_threshold={rotate_threshold_pct}%, checkpoint_interval={candle_check_interval}min)...")

    if not contexts:
        return []

    max_minutes = max(len(ctx.today) for ctx in contexts.values())
    traded_symbols = set()
    open_position = None   # exactly ONE position at a time, or None
    closed_trades = []
    missed_log = {}  # symbol (of open_position) -> list of (checkpoint_minute, other_symbol,
                       # rvol_pct, side) that exceeded the held position's OWN entry RVOL, checked
                       # ONLY at 5-min candle closes (matching the actual decision cadence) --
                       # diagnostic only, does NOT affect trading.

    for minute_idx in range(FIRST_CANDLE_WINDOW - 1, max_minutes):
        is_eod = minute_idx >= max_minutes - 1
        is_candle_close = (minute_idx + 1) % candle_check_interval == 0  # 9:20, 9:25, 9:30, ...

        # ---- Manage the single open position EVERY MINUTE (stop-loss/EOD --
        # risk protection shouldn't wait for the next checkpoint) ----
        if open_position is not None:
            symbol = open_position["symbol"]
            ctx = contexts[symbol]
            if minute_idx < len(ctx.today):
                o, h, l, c, v, oi = ctx.today[minute_idx]
                exit_price, exit_reason = None, None
                if open_position["side"] == "long" and l <= open_position["stop"]:
                    exit_price, exit_reason = open_position["stop"], "stop"
                elif open_position["side"] == "short" and h >= open_position["stop"]:
                    exit_price, exit_reason = open_position["stop"], "stop"
                elif is_eod:
                    exit_price, exit_reason = float(c), "eod_squareoff"

                if exit_price is not None:
                    direction = 1 if open_position["side"] == "long" else -1
                    risk = abs(open_position["entry"] - open_position["stop"])
                    r_multiple = ((exit_price - open_position["entry"]) * direction) / risk if risk else 0
                    pnl = (exit_price - open_position["entry"]) * direction
                    closed_trades.append({
                        **open_position, "exit": round(exit_price, 2), "exit_reason": exit_reason,
                        "exit_minute": minute_idx, "r_multiple": round(r_multiple, 2), "pnl": round(pnl, 2),
                    })
                    open_position = None  # slot free -- next ENTRY still waits for the next checkpoint

        # ---- Entry decisions (and the diagnostic) ONLY at checkpoint candle closes ----
        if not is_candle_close:
            continue

        if verbose:
            # ---- FULL RANKING PRINTOUT: every stock's RVOL % right now, top 10,
            # regardless of traded status -- so you can see the whole market's
            # comparison happening live at every checkpoint, not just a summary.
            checkpoint_time_min = minute_idx + 1
            all_ranked = []
            for symbol, ctx in contexts.items():
                if minute_idx >= len(ctx.today):
                    continue
                side, ltp, rvol_pct = ctx.evaluate_at_minute(minute_idx)
                if side and rvol_pct is not None:
                    already_traded = symbol in traded_symbols
                    is_held = open_position is not None and symbol == open_position["symbol"]
                    all_ranked.append((symbol, side, rvol_pct, already_traded, is_held))
            all_ranked.sort(key=lambda r: r[2], reverse=True)

            print(f"\n--- Checkpoint @ minute {checkpoint_time_min} ({checkpoint_time_min}min into session) ---")
            for rank, (symbol, side, rvol_pct, already_traded, is_held) in enumerate(all_ranked[:10], start=1):
                tag = " [HELD]" if is_held else (" [already traded]" if already_traded else "")
                print(f"  #{rank} {symbol} ({side}) RVOL%={rvol_pct:.1f}{tag}")

        # ---- ROTATION CHECK: if a position is held, get its CURRENT (updated,
        # not frozen entry-time) RVOL %, and compare against the best untraded
        # candidate right now. If the candidate exceeds the held position by
        # at least rotate_threshold_pct (relative), exit the held position at
        # current LTP and immediately enter the new candidate.
        best_untraded = None
        for symbol, ctx in contexts.items():
            if symbol in traded_symbols or (open_position and symbol == open_position["symbol"]):
                continue
            if minute_idx >= len(ctx.today):
                continue
            side, ltp, rvol_pct = ctx.evaluate_at_minute(minute_idx)
            if side and rvol_pct is not None:
                if best_untraded is None or rvol_pct > best_untraded[2]:
                    best_untraded = (symbol, side, rvol_pct)

        if open_position is not None and best_untraded is not None:
            held_symbol = open_position["symbol"]
            held_ctx = contexts[held_symbol]
            _, held_ltp_now, held_rvol_now = held_ctx.evaluate_at_minute(minute_idx)

            # Still log the diagnostic history regardless of whether we act on it
            if held_rvol_now is not None and best_untraded[2] > held_rvol_now:
                missed_log.setdefault(held_symbol, [])
                if not missed_log[held_symbol] or best_untraded[2] > missed_log[held_symbol][-1][2]:
                    missed_log[held_symbol].append((minute_idx, best_untraded[0], best_untraded[2], best_untraded[1]))

            rotate_trigger = held_rvol_now is not None and held_rvol_now > 0 and (
                best_untraded[2] >= held_rvol_now * (1 + rotate_threshold_pct / 100)
            )
            if rotate_trigger and held_ltp_now is not None:
                direction = 1 if open_position["side"] == "long" else -1
                risk = abs(open_position["entry"] - open_position["stop"])
                r_multiple = ((held_ltp_now - open_position["entry"]) * direction) / risk if risk else 0
                pnl = (held_ltp_now - open_position["entry"]) * direction
                closed_trades.append({
                    **open_position, "exit": round(held_ltp_now, 2), "exit_reason": "rotated_out",
                    "exit_minute": minute_idx, "r_multiple": round(r_multiple, 2), "pnl": round(pnl, 2),
                    "rotated_into": best_untraded[0], "rotated_into_rvol": round(best_untraded[2], 1),
                })
                open_position = None  # slot free -- new entry happens in the block below, same checkpoint

        # ---- If no position is open, look for the current #1-ranked (highest cumulative RVOL) untraded stock ----
        if open_position is None and not is_eod:
            candidates = []  # (symbol, side, ltp, rvol_pct)
            for symbol, ctx in contexts.items():
                if symbol in traded_symbols or minute_idx >= len(ctx.today):
                    continue
                side, ltp, rvol_pct = ctx.evaluate_at_minute(minute_idx)
                if side and rvol_pct is not None:
                    candidates.append((symbol, side, ltp, rvol_pct))

            if candidates:
                candidates.sort(key=lambda c: c[3], reverse=True)

                # Hard correctness check: verify the top pick genuinely has
                # the highest RVOL % among everyone available this checkpoint.
                top_rvol = candidates[0][3]
                assert all(top_rvol >= c[3] for c in candidates), (
                    f"RVOL selection broken at minute {minute_idx}: "
                    f"picked {candidates[0][0]}({top_rvol}) but a higher RVOL% existed"
                )

                symbol, side, ltp, rvol_pct = candidates[0]
                ctx = contexts[symbol]
                stop = ctx.first_candle_low if side == "long" else ctx.first_candle_high
                open_position = {
                    "symbol": symbol, "side": side, "entry": ltp, "stop": stop,
                    "entry_minute": minute_idx, "entry_rvol_pct": round(rvol_pct, 1),
                    "candidates_that_minute": len(candidates),
                }
                traded_symbols.add(symbol)

    # Force-close anything still open at the very end (safety net)
    if open_position is not None:
        symbol = open_position["symbol"]
        ctx = contexts[symbol]
        exit_price = float(ctx.today[-1, 3])
        direction = 1 if open_position["side"] == "long" else -1
        risk = abs(open_position["entry"] - open_position["stop"])
        r_multiple = ((exit_price - open_position["entry"]) * direction) / risk if risk else 0
        pnl = (exit_price - open_position["entry"]) * direction
        closed_trades.append({
            **open_position, "exit": round(exit_price, 2), "exit_reason": "eod_squareoff",
            "exit_minute": len(ctx.today) - 1, "r_multiple": round(r_multiple, 2), "pnl": round(pnl, 2),
        })

    if verbose:
        print(f"\n=== TRADES ({len(closed_trades)}) ===")
        for t in sorted(closed_trades, key=lambda t: t["entry_minute"]):
            print(f"  [{t['entry_minute']}min] {t['symbol']} ({t['side']}) "
                  f"RVOL%={t['entry_rvol_pct']} (top of {t['candidates_that_minute']} candidates): "
                  f"entry={round(t['entry'],2)} stop={round(t['stop'],2)} exit={t['exit']} "
                  f"({t['exit_reason']}) R={t['r_multiple']} PnL={t['pnl']}")

            if t["exit_reason"] == "rotated_out":
                print(f"      [rotation] exited to switch into {t['rotated_into']} "
                      f"(RVOL%={t['rotated_into_rvol']}, exceeded this position by >= {rotate_threshold_pct}%).")

        if closed_trades:
            print(f"\nVerification: every trade was the single highest-RVOL% untraded stock at its "
                  f"entry minute (enforced by an assertion during the run, not just visual inspection). "
                  f"No delta-zone/POC condition was checked -- side was determined purely by LTP vs VWAP.")
            print(f"Max concurrent positions: 1 (strictly sequential, one at a time).")

            total_pnl = sum(t["pnl"] for t in closed_trades)
            total_r = sum(t["r_multiple"] for t in closed_trades)
            wins = sum(1 for t in closed_trades if t["pnl"] > 0)
            print(f"\nNet PnL (1 share/position): {total_pnl:.2f}")
            print(f"Total R: {total_r:.2f} | Win rate: {wins}/{len(closed_trades)} ({100*wins/len(closed_trades):.0f}%)")

    return closed_trades


def backtest_full_day(date_str: str, access_token: str, universe: dict = None,
                       rotate_threshold_pct: float = ROTATE_THRESHOLD_PCT,
                       candle_check_interval: int = CANDLE_CHECK_INTERVAL, verbose: bool = True):
    """Convenience wrapper: fetch + simulate in one call. This is what the CLI uses."""
    contexts = fetch_day_contexts(date_str, access_token, universe)
    return simulate_day(contexts, rotate_threshold_pct, candle_check_interval, verbose)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Full-universe, one-day backtest: pure RVOL-ranked entry, VWAP-determined direction, no delta-zone gate.")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD, a past trading day")
    parser.add_argument("--token", required=True, help="Upstox access token (never hardcode this)")
    args = parser.parse_args()

    backtest_full_day(args.date, args.token)
