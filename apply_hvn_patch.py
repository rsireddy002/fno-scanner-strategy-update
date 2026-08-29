"""
apply_hvn_patch.py

Applies two edits to fno_delta_dashboard.py:
  1. Adds the hvn_lvn import near the top
  2. Adds today_hvn/today_lvn (single-session) and composite_hvn/composite_lvn
     (18-day) keys to compute_levels_and_baseline()'s return dict

Bin size is scaled off atr_val (already computed per-symbol in that function)
rather than a fixed point value, since a fixed bin size can't work sensibly
across a universe spanning ~50-rupee stocks and ~2800-rupee stocks. Falls
back to a range-based estimate if ATR is unavailable, and wraps both profile
builds in try/except so one illiquid/low-data symbol can't crash the whole
precompute loop - it just gets empty hvn/lvn lists for that run.

Run this ONCE. It edits the file in place. Check `git diff` afterward to
confirm the changes look right before committing.
"""
import sys

TARGET_FILE = "fno_delta_dashboard.py"

IMPORT_OLD = '''import streamlit as st

from sector_rotation import render_sector_rotation_tab
from sector_rotation import render_sector_rotation_tab'''

IMPORT_NEW = '''import streamlit as st

from sector_rotation import render_sector_rotation_tab
from sector_rotation import render_sector_rotation_tab
from hvn_lvn import build_volume_profile, find_hvn_lvn'''

RETURN_OLD = '''    return {
        "poc": None if pd.isna(todayPOC) else round(todayPOC, 2),
        "delta_support": None if pd.isna(support_before_today) else round(support_before_today, 2),
        "delta_resistance": None if pd.isna(resistance_before_today) else round(resistance_before_today, 2),
        "last_close": last_close_val,
        "next_day_support": next_day_support_val,
        "next_day_resistance": next_day_resistance_val,
        "yesterday_close": None if pd.isna(yesterday_close) else round(yesterday_close, 2),
        "already_above_yesterday": bool(already_above_yesterday),
        "already_below_yesterday": bool(already_below_yesterday),
        "crossover_time": crossover_time_str,
        "crossover_price": crossover_price_val,
        "crossunder_time": crossunder_time_str,
        "crossunder_price": crossunder_price_val,
        "rvol_baseline": rvol_baseline,
        "swing_lows": swing_lows,
        "swing_highs": swing_highs,
        "computed_date": str(today),
        "intraday_closes": intraday_closes,
        "atr": atr_val,
    }'''

RETURN_NEW = '''    # HVN/LVN: bin size scaled off ATR so it works sensibly across the whole
    # universe (cheap stocks vs. NIFTY/BANKNIFTY futures) rather than a fixed
    # point value. Falls back to a range-based estimate if ATR is unavailable.
    atr_for_bins = atr_val if (atr_val and not pd.isna(atr_val)) else None
    if not atr_for_bins or atr_for_bins <= 0:
        fallback_range = today_df["high"].max() - today_df["low"].min()
        atr_for_bins = fallback_range / 10 if fallback_range > 0 else 1.0

    today_bin_size = max(atr_for_bins / 8, 0.05)
    composite_bin_size = max(atr_for_bins / 4, 0.1)

    try:
        today_bins, today_vols = build_volume_profile(today_df, bin_size=today_bin_size)
        today_hvn_lvn = find_hvn_lvn(today_bins, today_vols)
    except Exception:
        today_hvn_lvn = {"hvns": [], "lvns": []}

    try:
        composite_bins, composite_vols = build_volume_profile(df, bin_size=composite_bin_size)
        composite_hvn_lvn = find_hvn_lvn(composite_bins, composite_vols)
    except Exception:
        composite_hvn_lvn = {"hvns": [], "lvns": []}

    return {
        "poc": None if pd.isna(todayPOC) else round(todayPOC, 2),
        "delta_support": None if pd.isna(support_before_today) else round(support_before_today, 2),
        "delta_resistance": None if pd.isna(resistance_before_today) else round(resistance_before_today, 2),
        "last_close": last_close_val,
        "next_day_support": next_day_support_val,
        "next_day_resistance": next_day_resistance_val,
        "yesterday_close": None if pd.isna(yesterday_close) else round(yesterday_close, 2),
        "already_above_yesterday": bool(already_above_yesterday),
        "already_below_yesterday": bool(already_below_yesterday),
        "crossover_time": crossover_time_str,
        "crossover_price": crossover_price_val,
        "crossunder_time": crossunder_time_str,
        "crossunder_price": crossunder_price_val,
        "rvol_baseline": rvol_baseline,
        "swing_lows": swing_lows,
        "swing_highs": swing_highs,
        "computed_date": str(today),
        "intraday_closes": intraday_closes,
        "atr": atr_val,
        "today_hvn": today_hvn_lvn["hvns"],
        "today_lvn": today_hvn_lvn["lvns"],
        "composite_hvn": composite_hvn_lvn["hvns"],
        "composite_lvn": composite_hvn_lvn["lvns"],
    }'''


def apply_patch():
    with open(TARGET_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    if IMPORT_NEW in content:
        print("Import already patched - skipping.")
    elif IMPORT_OLD in content:
        content = content.replace(IMPORT_OLD, IMPORT_NEW, 1)
        print("Import patch applied.")
    else:
        print("ERROR: import old_str not found - file may have changed since this patch was written.")
        sys.exit(1)

    if RETURN_NEW in content:
        print("Return-dict already patched - skipping.")
    elif RETURN_OLD in content:
        content = content.replace(RETURN_OLD, RETURN_NEW, 1)
        print("Return-dict patch applied.")
    else:
        print("ERROR: return-dict old_str not found - file may have changed since this patch was written.")
        sys.exit(1)

    with open(TARGET_FILE, "w", encoding="utf-8") as f:
        f.write(content)

    print("\nDone. Run 'git diff fno_delta_dashboard.py' to review the changes before committing.")


if __name__ == "__main__":
    apply_patch()
