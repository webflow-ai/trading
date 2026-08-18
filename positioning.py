"""
positioning.py — FII long/short positioning analytics and the option-chain
snapshot interface. Module 4 (first half) of the build order
(docs/PREMARKET_ENGINE.md).
"""

import asyncio
import datetime as dt

import pandas as pd

import storage

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

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


def _side_label(ratio: float | None) -> str | None:
    if ratio is None:
        return None
    if ratio >= 55:
        return "net long"
    if ratio <= 45:
        return "net short"
    return "balanced"


def build_positioning_outlook(
    snapshot: dict | None,
    cash: dict | None = None,
    fii: dict | None = None,
) -> dict:
    """Plain-language read of Client/DII/FII/Pro futures OI + FII/DII cash
    for 'what this positioning implies into the next open'. This is prior-
    session / latest-published NSE bias — not live tick order flow.
    """
    snapshot = snapshot or {}
    participants = snapshot.get("participants") or {}
    trade_date = snapshot.get("trade_date")
    fii = fii or {}
    cash = cash or {}

    fii_ratio = fii.get("ratio")
    fii_trend = fii.get("trend")
    if fii_ratio is None and participants.get("FII"):
        fii_ratio = participants["FII"].get("ratio")
        fii_trend = fii_trend or participants["FII"].get("trend")

    why: list[str] = []
    watch: list[str] = []

    if fii_ratio is None and not participants:
        return {
            "headline": "Participant positioning unavailable",
            "what_to_expect": "No NSE participant OI loaded yet — refresh after the evening file is published, or wait for the evening job.",
            "why": [],
            "watch": ["Re-check NSE participant OI after market close when the new CSV is posted."],
            "bias": "neutral",
            "scope": "Prior-session positioning only — confirm with GIFT at the open.",
            "trade_date": trade_date,
        }

    side = _side_label(fii_ratio)
    if fii_ratio is not None and fii_ratio >= 55 and fii_trend == "rising":
        bias = "bullish"
        headline = "FII futures lean bullish into the next session"
        what = (
            "Foreign desks are net long and still adding long/short share — "
            "supportive bias for dips if GIFT/open agrees; less reason to fade strength early."
        )
    elif fii_ratio is not None and fii_ratio >= 55:
        bias = "bullish"
        headline = "FII futures are net long"
        what = (
            "FIIs hold more index-futures long than short — mild supportive bias into the open, "
            "but the trend is not accelerating, so wait for GIFT confirmation."
        )
    elif fii_ratio is not None and fii_ratio <= 45 and fii_trend == "falling":
        bias = "bearish"
        headline = "FII futures lean bearish into the next session"
        what = (
            "Foreign desks are net short and getting shorter — soft bias for bounces to sell "
            "if GIFT/open agrees; treat gap-up opens with more caution."
        )
    elif fii_ratio is not None and fii_ratio <= 45:
        bias = "bearish"
        headline = "FII futures are net short"
        what = (
            "FIIs hold more index-futures short than long — mild soft bias into the open; "
            "confirm with GIFT before leaning hard."
        )
    else:
        bias = "neutral"
        headline = "FII futures look roughly balanced"
        what = (
            "No strong FII futures tilt — positioning alone does not argue gap-up or gap-down; "
            "let GIFT and the first 15–30 minutes decide."
        )

    if fii_ratio is not None:
        why.append(
            f"FII index futures {side or 'mixed'} at {fii_ratio:.1f}% long/short"
            + (f", trend {fii_trend}" if fii_trend else "")
            + (f" (as of {trade_date})" if trade_date else "")
            + "."
        )

    client = participants.get("Client") or {}
    dii = participants.get("DII") or {}
    pro = participants.get("Pro") or {}
    if client.get("ratio") is not None:
        cside = _side_label(client["ratio"])
        why.append(f"Clients are {cside} ({client['ratio']:.1f}% long/short).")
        if fii_ratio is not None and client["ratio"] is not None:
            # Classic tell: heavy retail long while FII short (or vice versa)
            if fii_ratio <= 45 and client["ratio"] >= 55:
                why.append("Classic split: Clients long-heavy while FII short — often a softer open risk if GIFT is weak.")
                watch.append("If GIFT is soft, prefer fade-the-gap longs rather than chasing Client-side optimism.")
            elif fii_ratio >= 55 and client["ratio"] <= 45:
                why.append("Clients short-heavy while FII long — FII bias usually matters more for the index open.")
                watch.append("If GIFT is firm, dips may be bought even if retail looks cautious.")
    if dii.get("ratio") is not None:
        why.append(f"DII futures are {_side_label(dii['ratio'])} ({dii['ratio']:.1f}% long/short).")
    if pro.get("ratio") is not None:
        why.append(f"Pros are {_side_label(pro['ratio'])} ({pro['ratio']:.1f}% long/short).")

    if cash.get("fii_buy") is not None and cash.get("fii_sell") is not None:
        net = cash["fii_buy"] - cash["fii_sell"]
        cash_line = (
            f"FII cash was {'net buyer' if net > 0 else 'net seller' if net < 0 else 'flat'} "
            f"(buy {cash['fii_buy']:,.0f} / sell {cash['fii_sell']:,.0f}"
            + (f", {cash.get('trade_date')}" if cash.get("trade_date") else "")
            + ")."
        )
        why.append(cash_line)
        if net < 0 and bias == "bullish":
            why.append("Note: FII cash selling vs long futures — mixed signal; trust GIFT more than cash alone.")
        elif net > 0 and bias == "bearish":
            why.append("Note: FII cash buying vs short futures — mixed signal; trust GIFT more than cash alone.")
    if cash.get("dii_buy") is not None and cash.get("dii_sell") is not None:
        dnet = cash["dii_buy"] - cash["dii_sell"]
        why.append(
            f"DII cash was {'net buyer' if dnet > 0 else 'net seller' if dnet < 0 else 'flat'} "
            f"(buy {cash['dii_buy']:,.0f} / sell {cash['dii_sell']:,.0f})."
        )

    if bias == "bullish":
        watch.append("Supportive if price holds above prior close / predicted open after 9:15–9:30.")
        watch.append("Invalidation: quick reclaim failure back under prior close with GIFT turning soft.")
    elif bias == "bearish":
        watch.append("Soft bias if price stays below prior close / predicted open after 9:15–9:30.")
        watch.append("Invalidation: strong reclaim of prior close with GIFT firm.")
    else:
        watch.append("No positioning edge — trade the open only after a clear first impulse or opening-range break.")

    return {
        "headline": headline,
        "what_to_expect": what,
        "why": why,
        "watch": watch,
        "bias": bias,
        "scope": (
            "NSE participant OI is end-of-day (latest published CSV), not tick-by-tick. "
            "Use as overnight bias; confirm with live GIFT at the open."
        ),
        "trade_date": trade_date,
    }


async def refresh_positioning_from_nse(*, persist: bool = True) -> dict:
    """Re-fetch participant OI + FII/DII cash from NSE, optionally persist,
    then return a dashboard payload with plain-language outlook.

    NSE participant OI is an end-of-day archive file — during market hours
    this usually still returns the previous session's file (correctly). We
    still hit NSE on each call so the dashboard tracks when a new day is
    published, instead of only trusting the evening cron's last write.
    """
    import nse_client

    now = dt.datetime.now(IST)
    today = now.date()
    sources: dict[str, str] = {}

    def _fetch_from_nse():
        with nse_client.NSEClient() as client:
            oi_df, cash, oi_err, cash_err = None, None, None, None
            try:
                oi_df = client.fetch_participant_oi(today)
            except Exception as e:
                oi_err = str(e)
            try:
                cash = client.fetch_fii_dii_cash()
            except Exception as e:
                cash_err = str(e)
            return oi_df, cash, oi_err, cash_err

    oi_df, cash, oi_err, cash_err = await asyncio.to_thread(_fetch_from_nse)

    if oi_df is not None:
        if persist:
            try:
                await storage.save_participant_oi(oi_df)
                sources["participant_oi"] = "nse"
            except Exception as e:
                sources["participant_oi"] = f"nse_ok_persist_failed: {e}"
        else:
            sources["participant_oi"] = "nse_not_persisted"
    else:
        sources["participant_oi"] = f"failed: {oi_err}" if oi_err else "unavailable"

    if cash is not None:
        if persist:
            try:
                await storage.save_fii_dii_cash(cash)
                sources["fii_dii_cash"] = "nse"
            except Exception as e:
                sources["fii_dii_cash"] = f"nse_ok_persist_failed: {e}"
        else:
            sources["fii_dii_cash"] = "nse_not_persisted"
    else:
        sources["fii_dii_cash"] = f"failed: {cash_err}" if cash_err else "unavailable"

    # Rebuild from storage so trends use multi-day history (not just today's row).
    snapshot = await participant_snapshot()
    cash_snap = await fii_dii_cash_snapshot()
    fii = await compute_fii_positioning()
    fii_rows = await storage.get_fii_trend(days=30)
    outlook = build_positioning_outlook(snapshot, cash_snap, fii)

    nse_ok = sources.get("participant_oi") == "nse" or sources.get("fii_dii_cash") == "nse"
    return {
        "available": bool(snapshot.get("participants")),
        "from_nse": nse_ok,
        "trade_date": snapshot.get("trade_date"),
        "participants": snapshot.get("participants") or {},
        "fii_dii_cash": cash_snap,
        "fii": fii,
        "fii_rows": fii_rows,
        "outlook": outlook,
        "sources": sources,
        "fetched_at": now.isoformat(),
        "note": (
            "Re-fetched from NSE. Participant OI is the latest published end-of-day file "
            "(walks back on holidays/weekends); FII/DII cash is NSE's latest daily report."
        ),
    }
