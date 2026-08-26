"""
sector_rotation.py

Sector Rotation Heatmap module built on nselib.indices.live_index_performances().

Confirmed schema (nselib 2.5.1, checked live):
    columns: key, index, indexSymbol, last, variation, percentChange, open,
             high, low, previousClose, yearHigh, yearLow, indicativeClose,
             pe, pb, dy, declines, advances, unchanged, perChange365d,
             perChange30d, date365dAgo, date30dAgo, previousDay, oneWeekAgo,
             oneMonthAgoVal, oneWeekAgoVal, oneYearAgoVal, previousDayVal

    key values: 'INDICES ELIGIBLE IN DERIVATIVES', 'BROAD MARKET INDICES',
                'SECTORAL INDICES', 'STRATEGY INDICES', 'THEMATIC INDICES',
                'FIXED INCOME INDICES'

Note: NIFTY BANK and NIFTY 50 live under 'INDICES ELIGIBLE IN DERIVATIVES',
NOT 'SECTORAL INDICES' -- so they're pulled in explicitly below, since
Bank Nifty in particular is central to Yarapu's trading.

Usage
-----
Drop this file next to fno_delta_dashboard.py and import it:

    from sector_rotation import render_sector_rotation_tab

Then inside your Streamlit app, add a new tab:

    tabs = st.tabs([..., "Sector Rotation"])
    with tabs[-1]:
        render_sector_rotation_tab()
"""

import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from nselib import indices

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# Indices to pull in explicitly from 'INDICES ELIGIBLE IN DERIVATIVES' even
# though they're not tagged 'SECTORAL INDICES' -- these are the benchmarks
# Yarapu actually trades.
PINNED_BENCHMARK_INDICES = ["NIFTY 50", "NIFTY BANK"]

# Sectoral indices we don't care about for a rotation view (broad/duplicate/
# midsmall variants that clutter the heatmap without adding signal).
EXCLUDE_FROM_SECTORAL = {
    "NIFTY MIDSMALL HEALTHCARE",
    "NIFTY FINANCIAL SERVICES EX-BANK",
    "NIFTY MIDSMALL FINANCIAL SERVICES",
    "NIFTY MIDSMALL IT & TELECOM",
    "NIFTY500 HEALTHCARE",
    "NIFTY REITS & REALTY",
}


@st.cache_data(ttl=45, show_spinner=False)
def fetch_sector_performance() -> Optional[pd.DataFrame]:
    """
    Fetch live index performance and return a clean DataFrame of:
    NIFTY, NIFTY BANK, + core sectoral indices, ranked by % change.

    Returns None on failure so the caller can show a graceful message
    instead of crashing the whole dashboard render.
    """
    try:
        df = indices.live_index_performances()
    except Exception as e:
        logger.warning(f"live_index_performances() failed: {e}")
        return None

    if df is None or df.empty:
        return None

    required = {"key", "index", "percentChange"}
    if not required.issubset(df.columns):
        logger.warning(f"Unexpected columns from live_index_performances(): {list(df.columns)}")
        return None

    sectoral = df[df["key"] == "SECTORAL INDICES"].copy()
    sectoral = sectoral[~sectoral["index"].isin(EXCLUDE_FROM_SECTORAL)]

    benchmarks = df[
        (df["key"] == "INDICES ELIGIBLE IN DERIVATIVES")
        & (df["index"].isin(PINNED_BENCHMARK_INDICES))
    ].copy()

    combined = pd.concat([benchmarks, sectoral], ignore_index=True)
    if combined.empty:
        return None

    combined["percentChange"] = pd.to_numeric(combined["percentChange"], errors="coerce")
    combined["is_benchmark"] = combined["index"].isin(PINNED_BENCHMARK_INDICES)

    keep_cols = ["index", "percentChange", "last", "open", "high", "low",
                 "previousClose", "is_benchmark"]
    keep_cols = [c for c in keep_cols if c in combined.columns]
    combined = combined[keep_cols].rename(
        columns={"index": "Index", "percentChange": "% Change"}
    )

    # Benchmarks pinned at top, then ranked by % change within each group.
    combined = combined.sort_values(
        ["is_benchmark", "% Change"], ascending=[False, False]
    ).reset_index(drop=True)

    return combined


def _color_for_pct(pct: float) -> str:
    """Green->red gradient scaled to a +/-2% typical intraday sector move."""
    if pd.isna(pct):
        return "#888888"
    capped = max(-2.0, min(2.0, pct))
    if capped >= 0:
        intensity = capped / 2.0
        r = int(255 - 155 * intensity)
        g = 200
        b = int(255 - 155 * intensity)
    else:
        intensity = abs(capped) / 2.0
        r = 220
        g = int(200 - 140 * intensity)
        b = int(200 - 140 * intensity)
    return f"#{r:02x}{g:02x}{b:02x}"


# Maps each display name in our heatmap/benchmarks to the (category, name)
# pair nselib.indices.constituent_stock_list() expects. NSE's constituent-
# list naming ("Nifty Auto") differs from the allIndices display naming
# ("NIFTY AUTO"), so this bridges the two.
SECTOR_TO_CONSTITUENT_LOOKUP = {
    "NIFTY 50": ("BroadMarketIndices", "Nifty 50"),
    "NIFTY BANK": ("SectoralIndices", "Nifty Bank"),
    "NIFTY AUTO": ("SectoralIndices", "Nifty Auto"),
    "NIFTY FINANCIAL SERVICES 25/50": ("SectoralIndices", "Nifty Financial Services"),
    "NIFTY FMCG": ("SectoralIndices", "Nifty FMCG"),
    "NIFTY IT": ("SectoralIndices", "Nifty IT"),
    "NIFTY MEDIA": ("SectoralIndices", "Nifty Media"),
    "NIFTY METAL": ("SectoralIndices", "Nifty Metal"),
    "NIFTY PHARMA": ("SectoralIndices", "Nifty Pharma"),
    "NIFTY PSU BANK": ("SectoralIndices", "Nifty PSU Bank"),
    "NIFTY PRIVATE BANK": ("SectoralIndices", "Nifty Private Bank"),
    "NIFTY REALTY": ("SectoralIndices", "Nifty Realty"),
    "NIFTY HEALTHCARE INDEX": ("SectoralIndices", "Nifty Healthcare"),
    "NIFTY CONSUMER DURABLES": ("SectoralIndices", "Nifty Consumer Durables"),
    "NIFTY OIL & GAS": ("SectoralIndices", "Nifty Oil and Gas"),
    "NIFTY CHEMICALS": ("SectoralIndices", "Nifty Chemicals"),
    # Note: NIFTY CEMENT has no confirmed constituent-list entry in nselib's
    # index config (checked NiftySectoralIndices/NiftyThematicIndices) - left
    # out of the drilldown rather than guessing at a wrong mapping.
}


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_sector_constituents(category: str, name: str) -> Optional[pd.DataFrame]:
    """
    Constituent lists barely change intraday, so this is cached for an hour
    (vs 45s for the live % change data) to avoid hitting NSE repeatedly for
    something that's effectively static during the trading day.
    """
    try:
        df = indices.constituent_stock_list(index_category=category, index_name=name)
    except Exception as e:
        logger.warning(f"constituent_stock_list({category}, {name}) failed: {e}")
        return None
    if df is None or df.empty:
        return None
    symbol_col = None
    for candidate in ("Symbol", "SYMBOL", "symbol"):
        if candidate in df.columns:
            symbol_col = candidate
            break
    if symbol_col is None:
        return None
    return df.rename(columns={symbol_col: "Symbol"})


def _render_constituent_drilldown(sector_options: list):
    """
    Lets the user pick a sector and see its constituent stocks joined
    against the live F&O scan already running in the main dashboard
    (st.session_state['result_df']) - so this costs zero extra Upstox
    calls, it just re-slices data you're already fetching for the
    Live Watchlist / Simple View tabs.
    """
    st.divider()
    st.markdown("**Drill into a sector's stocks**")

    available = [s for s in sector_options if s in SECTOR_TO_CONSTITUENT_LOOKUP]
    if not available:
        return

    chosen = st.selectbox("Sector", available, key="sector_rotation_drilldown_choice")
    category, nse_name = SECTOR_TO_CONSTITUENT_LOOKUP[chosen]

    constituents_df = fetch_sector_constituents(category, nse_name)
    if constituents_df is None or constituents_df.empty:
        st.info(f"Couldn't fetch the constituent list for {chosen} right now.")
        return

    live_df = st.session_state.get("result_df")
    if live_df is None or live_df.empty:
        st.info(
            f"{chosen} has {len(constituents_df)} constituents, but no live scan data is loaded yet "
            "- click 'Refresh Live Data Now' in the sidebar first to see live prices here."
        )
        st.dataframe(constituents_df[["Symbol"]], use_container_width=True, hide_index=True)
        return

    live_cols = [c for c in ["Symbol", "CurrentPrice", "DayChange%", "%Move", "RVOL%", "Status"] if c in live_df.columns]
    merged = constituents_df[["Symbol"]].merge(live_df[live_cols], on="Symbol", how="left")
    matched = merged["CurrentPrice"].notna().sum()

    st.caption(
        f"{chosen}: {len(constituents_df)} constituents, {matched} matched against your live F&O scan "
        f"(only symbols in your tracked universe show live data). "
        f"DayChange% = vs yesterday's close (standard screener metric). "
        f"%Move = distance from this dashboard's own Delta Support/Resistance breakout level - "
        f"the two measure different things and can disagree."
    )

    if "DayChange%" in merged.columns:
        merged = merged.sort_values("DayChange%", ascending=False, na_position="last")
    merged = merged.reset_index(drop=True)
    merged.insert(0, "S.No", range(1, len(merged) + 1))
    st.dataframe(merged, use_container_width=True, hide_index=True)


def render_sector_rotation_tab():
    """Streamlit renderer: sector rotation heatmap + ranked table."""
    st.subheader("Sector Rotation")
    now_ist = datetime.now(IST).strftime("%H:%M:%S IST")
    st.caption(f"Live NSE sectoral index performance • last refreshed {now_ist}")

    df = fetch_sector_performance()

    if df is None or df.empty:
        st.warning(
            "Couldn't fetch live sector data right now. "
            "This can happen if NSE rate-limits the request — try refreshing in a bit."
        )
        return

    # --- Benchmark row (NIFTY / NIFTY BANK) shown as big metrics up top ---
    benchmarks = df[df["is_benchmark"]]
    if not benchmarks.empty:
        cols = st.columns(len(benchmarks))
        for col, (_, r) in zip(cols, benchmarks.iterrows()):
            col.metric(r["Index"], f"{r.get('last', '—')}", f"{r['% Change']:.2f}%")
        st.divider()

    # --- Sector heatmap tiles ---
    sector_only = df[~df["is_benchmark"]].reset_index(drop=True)
    cols_per_row = 4
    rows = [sector_only.iloc[i:i + cols_per_row] for i in range(0, len(sector_only), cols_per_row)]
    for row_chunk in rows:
        cols = st.columns(len(row_chunk))
        for col, (_, r) in zip(cols, row_chunk.iterrows()):
            pct = r["% Change"]
            color = _color_for_pct(pct)
            arrow = "▲" if pct >= 0 else "▼"
            col.markdown(
                f"""
                <div style="background-color:{color};border-radius:8px;padding:10px;
                            text-align:center;margin-bottom:8px;">
                    <div style="font-size:12px;font-weight:600;color:#111;">{r['Index']}</div>
                    <div style="font-size:18px;font-weight:700;color:#111;">
                        {arrow} {pct:.2f}%
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    with st.expander("Ranked table"):
        st.dataframe(df.drop(columns=["is_benchmark"]), use_container_width=True, hide_index=True)

    # Leadership callouts, mirroring your RVOL-leader convention
    if len(sector_only) >= 2:
        leader = sector_only.iloc[0]
        laggard = sector_only.iloc[-1]
        c1, c2 = st.columns(2)
        c1.metric("Leading Sector", leader["Index"], f"{leader['% Change']:.2f}%")
        c2.metric("Lagging Sector", laggard["Index"], f"{laggard['% Change']:.2f}%")

    _render_constituent_drilldown(df["Index"].tolist())


if __name__ == "__main__":
    # Standalone smoke test (run: streamlit run sector_rotation.py)
    st.set_page_config(page_title="Sector Rotation", layout="wide")
    render_sector_rotation_tab()
