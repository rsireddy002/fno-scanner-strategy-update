"""
apply_hvn_patch_dashboard_columns.py

Adds two new columns to run_live_scan()'s result rows:
  - NearestHVNAbove: nearest known HVN price above current_price
  - NearestHVNBelow: nearest known HVN price below current_price

Sourced from the today_hvn/composite_hvn data already sitting in the cache
(added earlier by compute_levels_and_baseline()). Scoped deliberately small -
just the two raw levels, not a richer "LVN path clear" indicator - since
that would need is_above_both/is_below_both (the breakout-direction flags
used elsewhere in this function), which haven't been reviewed yet. These
two columns are useful on their own regardless of that.

Run this ONCE, from inside fno-scanner-strategy-update.
"""
import sys

TARGET_FILE = "fno_delta_dashboard.py"

# --- Edit 1: add a small helper function near the top-level functions ---
HELPER_OLD = '''def get_sector(symbol):'''

HELPER_NEW = '''def _nearest_hvn_levels(levels, price):
    """Returns (nearest_hvn_above, nearest_hvn_below) given a cache entry's
    levels dict and a reference price. Pools today_hvn + composite_hvn
    together, same approach as live_strategy.py's SymbolState helpers."""
    if price is None:
        return None, None
    pool = (levels.get("today_hvn") or []) + (levels.get("composite_hvn") or [])
    above = [n["price"] for n in pool if n["price"] > price]
    below = [n["price"] for n in pool if n["price"] < price]
    return (min(above) if above else None), (max(below) if below else None)


def get_sector(symbol):'''

# --- Edit 2: compute + include the two new columns in the result row ---
ROW_OLD = '''        results.append({
            "Symbol": symbol, "Sector": get_sector(symbol), "CurrentPrice": current_price,
            "EntryPrice": entry_price if entry_price is not None else current_price,
            "POC": poc, "VWAP": vwap,
            "DeltaSupport": support, "DeltaResistance": resistance,
            "NextSupport": next_support, "NextResistance": next_resistance,
            "NextLevelDistance%": next_level_distance_pct,
            "ZoneWidth%": zone_width_pct,'''

ROW_NEW = '''        nearest_hvn_above, nearest_hvn_below = _nearest_hvn_levels(levels, current_price)
        results.append({
            "Symbol": symbol, "Sector": get_sector(symbol), "CurrentPrice": current_price,
            "EntryPrice": entry_price if entry_price is not None else current_price,
            "POC": poc, "VWAP": vwap,
            "DeltaSupport": support, "DeltaResistance": resistance,
            "NextSupport": next_support, "NextResistance": next_resistance,
            "NextLevelDistance%": next_level_distance_pct,
            "ZoneWidth%": zone_width_pct,
            "NearestHVNAbove": nearest_hvn_above,
            "NearestHVNBelow": nearest_hvn_below,'''


def apply_patch():
    with open(TARGET_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    edits = [
        ("helper function", HELPER_OLD, HELPER_NEW),
        ("result row columns", ROW_OLD, ROW_NEW),
    ]

    for label, old, new in edits:
        if new in content:
            print(f"{label}: already patched - skipping.")
            continue
        if old not in content:
            print(f"ERROR: {label} old_str not found - file may differ from what this patch expects.")
            sys.exit(1)
        content = content.replace(old, new, 1)
        print(f"{label}: patch applied.")

    with open(TARGET_FILE, "w", encoding="utf-8") as f:
        f.write(content)

    print("\nDone. Run 'git diff fno_delta_dashboard.py' to review before committing.")


if __name__ == "__main__":
    apply_patch()
