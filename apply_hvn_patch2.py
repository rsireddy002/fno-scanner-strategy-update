"""
apply_hvn_patch2.py

Replaces the ATR-based HVN/LVN bin sizing (from apply_hvn_patch.py) with
range-based sizing using a fixed target bin count. ATR-based sizing broke
down on stocks where ATR is small relative to the multi-day range (e.g.
RELIANCE composite profile came back with 13 HVNs crammed into a 21-point
range - noise, not real structure). Targeting a fixed bin COUNT per window
keeps node granularity consistent across instruments regardless of ATR.

Run this ONCE, after apply_hvn_patch.py has already been applied.
"""
import sys

TARGET_FILE = "fno_delta_dashboard.py"

OLD = '''    # HVN/LVN: bin size scaled off ATR so it works sensibly across the whole
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
        composite_hvn_lvn = {"hvns": [], "lvns": []}'''

NEW = '''    # HVN/LVN: bin size derived from a FIXED TARGET BIN COUNT across each
    # window's own price range, not a fixed point value or ATR fraction.
    # ATR-based sizing broke down on stocks whose ATR is small relative to
    # their multi-day range (RELIANCE composite came back with 13 "HVNs"
    # crammed into a 21-point range - noise, not real nodes). A fixed bin
    # COUNT keeps node granularity consistent regardless of instrument price
    # level or ATR quirks.
    TODAY_N_BINS = 30
    COMPOSITE_N_BINS = 50
    MIN_NODE_SEPARATION_BINS = 3

    today_range = today_df["high"].max() - today_df["low"].min()
    today_bin_size = max(today_range / TODAY_N_BINS, 0.01) if today_range > 0 else 0.01

    composite_range = df["high"].max() - df["low"].min()
    composite_bin_size = max(composite_range / COMPOSITE_N_BINS, 0.01) if composite_range > 0 else 0.01

    try:
        today_bins, today_vols = build_volume_profile(today_df, bin_size=today_bin_size)
        today_hvn_lvn = find_hvn_lvn(today_bins, today_vols, min_bin_distance=MIN_NODE_SEPARATION_BINS)
    except Exception:
        today_hvn_lvn = {"hvns": [], "lvns": []}

    try:
        composite_bins, composite_vols = build_volume_profile(df, bin_size=composite_bin_size)
        composite_hvn_lvn = find_hvn_lvn(composite_bins, composite_vols, min_bin_distance=MIN_NODE_SEPARATION_BINS)
    except Exception:
        composite_hvn_lvn = {"hvns": [], "lvns": []}'''


def apply_patch():
    with open(TARGET_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    if NEW in content:
        print("Already patched - skipping.")
        return
    if OLD not in content:
        print("ERROR: old_str not found - file may differ from what this patch expects.")
        sys.exit(1)

    content = content.replace(OLD, NEW, 1)
    with open(TARGET_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    print("Patch applied. Run 'git diff fno_delta_dashboard.py' to review.")


if __name__ == "__main__":
    apply_patch()
