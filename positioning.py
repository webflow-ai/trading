"""
positioning.py — FII long/short positioning analytics and the option-chain
snapshot interface. Module 4 (first half) of the build order
(docs/PREMARKET_ENGINE.md).
"""

import pandas as pd

import storage

# Ratio moves smaller than this over the trend window count as "flat" —
# without a floor, a 49.4% -> 49.6% wobble would get reported as "rising",
# which is noise, not a positioning shift.
TREND_FLAT_THRESHOLD_PCT_POINTS = 1.0


def compute_fii_ratio(df: pd.DataFrame) -> float | None:
    """df: one day's participant OI, as returned by
    nse_client.NSEClient.fetch_participant_oi (title-case CSV columns).
    FII index-futures long / (long + short), as a percentage. None if FII
    isn't present in the frame or both sides are zero."""
    fii_rows = df[df["Client Type"] == "FII"]
    if fii_rows.empty:
        return None
    row = fii_rows.iloc[0]
    long_, short_ = row["Future Index Long"], row["Future Index Short"]
    if long_ + short_ == 0:
        return None
    return round(float(long_) / float(long_ + short_) * 100, 2)


def _ratio_trend_from_rows(daily_rows: list[dict]) -> str | None:
    """daily_rows: one participant's participant_oi rows (snake_case
    columns, any order). Compares the oldest vs. newest ratio in the window
    to call it rising/falling/flat. None if there isn't enough data to
    compare. Shared by fii_ratio_trend (score input) and participant_snapshot
    (dashboard display, all four participants)."""
    ratios = []
    for row in daily_rows:
        long_, short_ = row.get("future_index_long"), row.get("future_index_short")
        if long_ is None or short_ is None or (long_ + short_) == 0:
            continue
        ratios.append((row.get("trade_date"), long_ / (long_ + short_) * 100))
    if len(ratios) < 2:
        return None
    ratios.sort(key=lambda r: r[0])  # oldest first
    delta = ratios[-1][1] - ratios[0][1]
    if delta > TREND_FLAT_THRESHOLD_PCT_POINTS:
        return "rising"
    if delta < -TREND_FLAT_THRESHOLD_PCT_POINTS:
        return "falling"
    return "flat"


def fii_ratio_trend(daily_rows: list[dict]) -> str | None:
    """daily_rows: FII participant_oi rows as returned by
    storage.get_fii_trend() (snake_case columns, most-recent-first)."""
    return _ratio_trend_from_rows(daily_rows)


async def compute_fii_positioning(days: int = 5) -> dict | None:
    """Current FII long/short ratio plus its N-day trend, read from storage.
    None if there's no participant_oi history yet."""
    rows = await storage.get_fii_trend(days=days)
    if not rows:
        return None
    latest = rows[0]  # storage.get_fii_trend orders trade_date.desc
    long_, short_ = latest.get("future_index_long"), latest.get("future_index_short")
    ratio = None
    if long_ is not None and short_ is not None and (long_ + short_):
        ratio = round(long_ / (long_ + short_) * 100, 2)
    return {"ratio": ratio, "trend": fii_ratio_trend(rows)}


def _participant_ratio(row: dict) -> float | None:
    long_, short_ = row.get("future_index_long"), row.get("future_index_short")
    if long_ is None or short_ is None or not (long_ + short_):
        return None
    return round(long_ / (long_ + short_) * 100, 2)


async def participant_snapshot(days: int = 5) -> dict:
    """Same-day long/short positioning *and* each one's N-day trend, for all
    four participant types (Client, DII, FII, Pro) — the full picture
    behind fii_ratio_trend, which only ever looks at FII for the score.
    {"trade_date": ..., "participants": {name: {long, short, ratio, trend},
    ...}}, or {"trade_date": None, "participants": {}} if nothing's been
    persisted yet."""
    history = await storage.get_participant_history(days=days)
    if not history:
        return {"trade_date": None, "participants": {}}

    latest_date = max(row["trade_date"] for row in history)
    by_participant: dict[str, list[dict]] = {}
    for row in history:
        by_participant.setdefault(row["participant"], []).append(row)

    participants = {}
    for name, rows in by_participant.items():
        latest_row = next((r for r in rows if r["trade_date"] == latest_date), None)
        if latest_row is None:
            continue
        participants[name] = {
            "long": latest_row.get("future_index_long"),
            "short": latest_row.get("future_index_short"),
            "ratio": _participant_ratio(latest_row),
            "trend": _ratio_trend_from_rows(rows),
        }
    return {"trade_date": latest_date, "participants": participants}


async def fii_dii_cash_snapshot() -> dict | None:
    """Latest day's FII/DII cash buy/sell, straight from storage — {trade_date,
    fii_buy, fii_sell, dii_buy, dii_sell} or None if nothing's persisted yet."""
    row = await storage.get_latest_fii_dii_cash()
    if not row:
        return None
    return {
        "trade_date": row.get("trade_date"),
        "fii_buy": row.get("fii_buy"), "fii_sell": row.get("fii_sell"),
        "dii_buy": row.get("dii_buy"), "dii_sell": row.get("dii_sell"),
    }


async def option_snapshot(symbol: str = "NIFTY") -> dict:
    """{max_call_oi_strike, max_put_oi_strike, pcr, max_pain}. Reads the
    newest row of pcr_snapshots (extended with these three columns by
    Module 2's migration). All values come back None until backend.py's PCR
    tracker is also updated to compute and persist
    max_call_oi_strike/max_put_oi_strike/max_pain — it doesn't yet — so this
    is the interface plus a working mock, not a finished wiring."""
    row = await storage.get_latest_pcr_snapshot(symbol)
    if not row:
        return {"max_call_oi_strike": None, "max_put_oi_strike": None, "pcr": None, "max_pain": None}
    return {
        "max_call_oi_strike": row.get("max_call_oi_strike"),
        "max_put_oi_strike": row.get("max_put_oi_strike"),
        "pcr": row.get("pcr_oi"),
        "max_pain": row.get("max_pain"),
    }
