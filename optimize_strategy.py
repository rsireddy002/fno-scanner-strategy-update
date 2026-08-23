"""
optimize_strategy.py

Tunes the strategy's two key parameters -- rotation threshold % and
checkpoint interval (minutes) -- by testing a grid of combinations against
past trading days, and reports which combination performed best.

ARCHITECTURE: the expensive part (fetching each day's 1-min candles + daily
history for the whole universe) happens ONCE per date, using
backtest_full_day.fetch_day_contexts(). The parameters being tuned only
affect simulate_day(), which is pure local computation -- so every
parameter combination is tested against the SAME cached data, without
re-fetching. This is what makes a real grid search practical: fetching is
API-bound and slow (~2-3 min/day), but simulating is CPU-only and fast
(a fraction of a second per combination).

Ranks results by TOTAL R, not raw rupee PnL -- R-multiple is risk-
normalized (same position risk regardless of stock price), so it isn't
skewed by a handful of high-priced stocks the way raw PnL can be (this
was a real, confirmed distortion in earlier top-5 backtests on this
project, where two expensive stocks accounted for ~94% of a 10-day PnL
total despite unremarkable R-multiples).

Run:
    python optimize_strategy.py --start 2026-08-10 --end 2026-08-21 --token YOUR_TOKEN

Customize the grid via --thresholds and --intervals (comma-separated):
    python optimize_strategy.py --start 2026-08-10 --end 2026-08-21 --token YOUR_TOKEN \\
        --thresholds 10,15,20,30,50 --intervals 5,10,15

Never hardcode your token -- pass it as an argument.
"""

import argparse
import datetime

from fno_universe import load_fno_universe
from backtest_full_day import fetch_day_contexts, simulate_day

DEFAULT_THRESHOLDS = [10, 15, 20, 30, 50]   # rotation threshold %, relative
DEFAULT_INTERVALS = [5, 10, 15]              # checkpoint interval, minutes


def daterange(start_date: datetime.date, end_date: datetime.date):
    days = (end_date - start_date).days
    for n in range(days + 1):
        yield start_date + datetime.timedelta(days=n)


def optimize(start_str: str, end_str: str, access_token: str,
             thresholds: list = None, intervals: list = None):
    thresholds = thresholds or DEFAULT_THRESHOLDS
    intervals = intervals or DEFAULT_INTERVALS

    start_date = datetime.date.fromisoformat(start_str)
    end_date = datetime.date.fromisoformat(end_str)

    print("Loading F&O universe once (reused across all days and parameter combinations)...")
    universe = load_fno_universe()
    print(f"Universe: {len(universe)} stocks.\n")

    # ---- STEP 1: fetch every trading day's data ONCE (the expensive part) ----
    all_day_contexts = {}  # date_str -> contexts dict
    for d in daterange(start_date, end_date):
        if d.weekday() >= 5:  # skip weekends
            continue
        date_str = d.isoformat()
        print(f"Fetching {date_str} ({d.strftime('%A')})...")
        try:
            contexts = fetch_day_contexts(date_str, access_token, universe)
            if contexts:
                all_day_contexts[date_str] = contexts
            else:
                print(f"  No usable data for {date_str} -- likely a holiday, skipping.")
        except Exception as e:
            print(f"  ERROR fetching {date_str}: {e} -- skipping.")

    if not all_day_contexts:
        print("\nNo usable trading days fetched -- nothing to optimize against.")
        return

    print(f"\n{len(all_day_contexts)} trading day(s) fetched successfully. "
          f"Now testing {len(thresholds)} x {len(intervals)} = {len(thresholds)*len(intervals)} "
          f"parameter combinations against them (fast -- no more API calls needed)...\n")

    # ---- STEP 2: grid-search parameters against the SAME cached data ----
    results = []
    for threshold in thresholds:
        for interval in intervals:
            all_trades = []
            for date_str, contexts in all_day_contexts.items():
                trades = simulate_day(contexts, rotate_threshold_pct=threshold,
                                       candle_check_interval=interval, verbose=False)
                all_trades.extend(trades)

            if not all_trades:
                results.append({
                    "threshold": threshold, "interval": interval, "trades": 0,
                    "total_pnl": 0.0, "total_r": 0.0, "win_rate": None, "avg_r": None,
                })
                continue

            total_pnl = sum(t["pnl"] for t in all_trades)
            total_r = sum(t["r_multiple"] for t in all_trades)
            wins = sum(1 for t in all_trades if t["pnl"] > 0)
            results.append({
                "threshold": threshold, "interval": interval, "trades": len(all_trades),
                "total_pnl": round(total_pnl, 2), "total_r": round(total_r, 2),
                "win_rate": round(100 * wins / len(all_trades), 1),
                "avg_r": round(total_r / len(all_trades), 3),
            })

    # ---- Report, ranked by TOTAL R (risk-normalized, not skewed by stock price) ----
    results.sort(key=lambda r: r["total_r"], reverse=True)

    print(f"{'Rotate%':<10}{'Interval':<10}{'Trades':<9}{'Total PnL':<12}{'Total R':<10}{'Win%':<8}{'Avg R':<8}")
    print("-" * 67)
    for r in results:
        win_str = f"{r['win_rate']}%" if r['win_rate'] is not None else "n/a"
        avg_r_str = f"{r['avg_r']}" if r['avg_r'] is not None else "n/a"
        print(f"{r['threshold']:<10}{r['interval']:<10}{r['trades']:<9}{r['total_pnl']:<12}"
              f"{r['total_r']:<10}{win_str:<8}{avg_r_str:<8}")

    best = results[0]
    print(f"\nBest by Total R: rotate_threshold={best['threshold']}%, "
          f"checkpoint_interval={best['interval']}min "
          f"(Total R={best['total_r']}, {best['trades']} trades across {len(all_day_contexts)} day(s))")
    print(f"\nNote: {len(all_day_contexts)} day(s) is still a small sample -- treat this as a "
          f"starting point for further testing, not a settled conclusion.")

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Grid-search rotation threshold and checkpoint interval against past days.")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD, first date to test")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD, last date to test (inclusive)")
    parser.add_argument("--token", required=True, help="Upstox access token (never hardcode this)")
    parser.add_argument("--thresholds", default=None, help="Comma-separated rotation thresholds to test, e.g. 10,15,20,30,50")
    parser.add_argument("--intervals", default=None, help="Comma-separated checkpoint intervals (minutes) to test, e.g. 5,10,15")
    args = parser.parse_args()

    thresholds = [float(x) for x in args.thresholds.split(",")] if args.thresholds else None
    intervals = [int(x) for x in args.intervals.split(",")] if args.intervals else None

    optimize(args.start, args.end, args.token, thresholds, intervals)
