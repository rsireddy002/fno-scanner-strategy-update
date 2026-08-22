"""
backtest_multi_day.py

Runs backtest_full_day.py's exact strategy across MULTIPLE past trading
days, aggregating results into one overall summary. Reuses the universe
fetch once (not re-downloaded per day) and automatically skips weekends.

Market holidays aren't hardcoded (holiday calendars drift year to year) --
if a date returns essentially no data across the universe, that's handled
gracefully as "no market data" and simply skipped, same effect as knowing
it was a holiday without needing to maintain a calendar.

Run:
    python backtest_multi_day.py --start 2026-08-10 --end 2026-08-21 --token YOUR_TOKEN

Never hardcode your token -- pass it as an argument.
"""

import argparse
import datetime

from fno_universe import load_fno_universe
from backtest_full_day import backtest_full_day

MIN_QUALIFYING_TO_COUNT_AS_TRADING_DAY = 3  # if fewer than this qualify,
                                              # treat it as "no real market
                                              # data" (likely a holiday) and
                                              # skip rather than force 5 weak
                                              # trades out of a near-empty scan


def daterange(start_date: datetime.date, end_date: datetime.date):
    days = (end_date - start_date).days
    for n in range(days + 1):
        yield start_date + datetime.timedelta(days=n)


def backtest_multi_day(start_str: str, end_str: str, access_token: str):
    start_date = datetime.date.fromisoformat(start_str)
    end_date = datetime.date.fromisoformat(end_str)

    print(f"Loading F&O universe once (reused across all days)...")
    universe = load_fno_universe()
    print(f"Universe: {len(universe)} stocks.\n")

    all_trades = []
    day_summaries = []
    skipped_days = []

    for d in daterange(start_date, end_date):
        if d.weekday() >= 5:  # Saturday=5, Sunday=6
            continue

        date_str = d.isoformat()
        print(f"\n{'='*70}")
        print(f"  {date_str} ({d.strftime('%A')})")
        print(f"{'='*70}")

        try:
            trades = backtest_full_day(date_str, access_token, universe=universe)
        except Exception as e:
            print(f"  ERROR on {date_str}: {e} -- skipping this day.")
            skipped_days.append((date_str, str(e)))
            continue

        if trades is None or len(trades) == 0:
            print(f"  No trades this day -- likely a holiday or genuinely no qualifying setups. Skipping from aggregate.")
            skipped_days.append((date_str, "no trades"))
            continue

        day_pnl = sum(t["pnl"] for t in trades)
        day_r = sum(t["r_multiple"] for t in trades)
        day_summaries.append({"date": date_str, "trades": len(trades), "pnl": day_pnl, "r": day_r})
        all_trades.extend(trades)

    # ---- Aggregate report ----
    print(f"\n\n{'#'*70}")
    print(f"  MULTI-DAY SUMMARY: {start_str} to {end_str}")
    print(f"{'#'*70}\n")

    if skipped_days:
        print(f"Skipped {len(skipped_days)} day(s) (holidays / no data):")
        for date_str, reason in skipped_days:
            print(f"  {date_str}: {reason}")
        print()

    if not day_summaries:
        print("No trading days produced any trades -- nothing to summarize.")
        return

    print(f"{'Date':<12} {'Trades':<8} {'PnL':<10} {'R':<8}")
    for s in day_summaries:
        print(f"{s['date']:<12} {s['trades']:<8} {s['pnl']:<10.2f} {s['r']:<8.2f}")

    total_pnl = sum(t["pnl"] for t in all_trades)
    total_r = sum(t["r_multiple"] for t in all_trades)
    total_trades = len(all_trades)
    wins = sum(1 for t in all_trades if t["pnl"] > 0)
    long_trades = [t for t in all_trades if t["side"] == "long"]
    short_trades = [t for t in all_trades if t["side"] == "short"]

    print(f"\n{'-'*40}")
    print(f"Trading days included: {len(day_summaries)}")
    print(f"Total trades: {total_trades} ({len(long_trades)} long, {len(short_trades)} short)")
    print(f"Net PnL (1 share/position): {total_pnl:.2f}")
    print(f"Total R: {total_r:.2f}")
    print(f"Win rate: {wins}/{total_trades} ({100*wins/total_trades:.0f}%)" if total_trades else "Win rate: n/a")
    print(f"Avg R per trade: {total_r/total_trades:.2f}" if total_trades else "")
    print(f"Avg PnL per trading day: {total_pnl/len(day_summaries):.2f}")

    return all_trades, day_summaries


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-day backtest of the live long/short strategy.")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD, first date to test")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD, last date to test (inclusive)")
    parser.add_argument("--token", required=True, help="Upstox access token (never hardcode this)")
    args = parser.parse_args()

    backtest_multi_day(args.start, args.end, args.token)
