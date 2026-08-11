"""
jobs.py — evening/morning job orchestration for the Nifty Pre-Market
Analysis Engine. Module 5 of the build order (docs/PREMARKET_ENGINE.md).

Triggered externally (Open Decision 1, resolved: external cron -> API
endpoints — see .github/workflows/premarket_jobs.yml and main.py's
POST /api/premarket/jobs/evening and /morning), not an in-process
APScheduler — this repo deploys to Vercel, where nothing runs unless a
request arrives.

Design note: compute_levels()/compute_structure() are deliberately only
called from the morning job, not the evening one, even though the original
brief lists "previous-day OHLC/levels" under the evening job. Both are pure,
cheap reads off Yahoo's daily/15m history that give the same answer at
7:30pm or 8:15am (the market is closed both times, so "the last completed
session" doesn't change overnight) — computing them twice a day would just
be redundant work with no upside, so they're computed once, at the point
they're actually consumed by scoring.
"""

import datetime as dt

import httpx

import market_data
import news_ai
import notify
import nse_client
import positioning
import scoring
import storage
import technicals

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

MACRO_SYMBOLS = {
    "crude": "BZ=F", "wti": "CL=F", "usdinr": "USDINR=X", "dxy": "DX-Y.NYB", "us10y": "^TNX",
}
US_SYMBOLS = {"us_dow": "^DJI", "us_nasdaq": "^IXIC", "us_sp500": "^GSPC"}
ASIA_SYMBOLS = {"asia_nikkei": "^N225", "asia_hangseng": "^HSI", "asia_kospi": "^KS11", "asia_shanghai": "000001.SS"}


async def run_evening_job(today: dt.date | None = None) -> dict:
    """End-of-day positioning pull: participant OI and FII/DII cash (both
    persisted so the morning job can read them back), plus the F&O ban list
    (returned in the result — there's no dedicated table for it; nothing
    downstream consumes it yet, see docs/PREMARKET_ENGINE.md). Each source
    is wrapped independently so one failure doesn't take down the rest.
    """
    today = today or dt.datetime.now(IST).date()
    result: dict = {"trade_date": today.isoformat(), "sources": {}}

    client = nse_client.NSEClient()
    try:
        try:
            df = client.fetch_participant_oi(today)
            await storage.save_participant_oi(df)
            result["sources"]["participant_oi"] = "ok"
        except Exception as e:
            print(f"jobs: evening participant_oi failed: {e}")
            result["sources"]["participant_oi"] = f"failed: {e}"

        try:
            cash = client.fetch_fii_dii_cash()
            await storage.save_fii_dii_cash(cash)
            result["sources"]["fii_dii_cash"] = "ok"
        except Exception as e:
            print(f"jobs: evening fii_dii_cash failed: {e}")
            result["sources"]["fii_dii_cash"] = f"failed: {e}"

        try:
            result["ban_list"] = client.fetch_ban_list(today)
            result["sources"]["ban_list"] = "ok"
        except Exception as e:
            print(f"jobs: evening ban_list failed: {e}")
            result["sources"]["ban_list"] = f"failed: {e}"
    finally:
        client.close()

    return result


async def run_morning_job(is_event_day: bool = False) -> dict:
    """Live pre-market cues + scoring: GIFT Nifty, US close, Asia live,
    macro, previous-day levels/structure, FII positioning, option snapshot
    -> weighted score -> persisted morning_briefs row. Every source is
    fetched independently; a dead one is dropped from the score rather than
    aborting the job (see scoring.compute_score)."""
    today = dt.datetime.now(IST).date()

    async with httpx.AsyncClient(timeout=15) as client:
        gift = await market_data.fetch_gift_nifty(client)
        us_quotes = {name: await market_data.fetch_quote(client, sym) for name, sym in US_SYMBOLS.items()}
        asia_quotes = {name: await market_data.fetch_quote(client, sym) for name, sym in ASIA_SYMBOLS.items()}
        macro_quotes = {name: await market_data.fetch_quote(client, sym) for name, sym in MACRO_SYMBOLS.items()}
        levels = await technicals.compute_levels(client)
        structure = await technicals.compute_structure(client)

    await storage.save_macro_snapshots(
        "morning",
        {**{k: v for k, v in us_quotes.items() if v}, **{k: v for k, v in asia_quotes.items() if v},
         **{k: v for k, v in macro_quotes.items() if v}},
    )

    fii = await positioning.compute_fii_positioning()
    option_snap = await positioning.option_snapshot("NIFTY")
    participants = await positioning.participant_snapshot()
    fii_dii_cash = await positioning.fii_dii_cash_snapshot()

    crude = macro_quotes.get("crude")
    usdinr = macro_quotes.get("usdinr")
    dxy = macro_quotes.get("dxy")
    us10y = macro_quotes.get("us10y")

    inputs = {
        "previous_close": levels.get("previous_close") if levels else None,
        "gift_price": gift.get("price") if gift else None,
        "us_quotes": us_quotes,
        "asia_quotes": asia_quotes,
        "crude_pct_change": crude.get("pct_change") if crude else None,
        "usdinr_pct_change": usdinr.get("pct_change") if usdinr else None,
        "dxy_pct_change": dxy.get("pct_change") if dxy else None,
        # ^TNX from Yahoo is already the yield in percentage points (e.g.
        # 4.25 = 4.25%) — a 1-day pct_change on that series is a change in
        # basis points to a decent approximation for the small daily moves
        # this score cares about.
        "us10y_change_bps": (us10y.get("price") - us10y.get("previous_close")) * 100 if us10y else None,
        "fii_ratio": fii.get("ratio") if fii else None,
        "fii_trend": fii.get("trend") if fii else None,
        "is_event_day": is_event_day,
    }

    score_result = scoring.compute_score(inputs)
    expected_range = scoring.compute_expected_range(option_snap, levels, is_event_day=is_event_day)
    predicted_open = scoring.compute_predicted_open(
        previous_close=inputs["previous_close"], gift_price=inputs["gift_price"], score=score_result["score"],
    )

    try:
        news = await news_ai.get_news_brief()
    except Exception as e:
        print(f"jobs: news_ai.get_news_brief failed: {e}")
        news = {"headlines": [], "news_sentiment": None}

    brief = {
        "trade_date": today.isoformat(),
        "score": score_result["score"],
        "verdict": score_result["verdict"],
        "expected_low": expected_range["low"],
        "expected_high": expected_range["high"],
        "predicted_open": predicted_open["value"] if predicted_open else None,
        "components": {
            "predicted_open_method": predicted_open["method"] if predicted_open else None,
            "participants": participants["participants"],
            "participants_trade_date": participants["trade_date"],
            "fii_dii_cash": fii_dii_cash,
            **score_result["components"],
            "confidence": score_result["confidence"],
            "missing": score_result["missing"],
            "is_event_day": is_event_day,
            "previous_close": inputs["previous_close"],
            "gift": {**(score_result["components"].get("gift") or {}), "price": gift.get("price") if gift else None},
            "levels": levels,
            "structure": structure,
            "option_snapshot": option_snap,
            "range_source": expected_range["source"],
            # Per-symbol breakdowns, kept alongside the already-aggregated
            # "us_asia"/"macro" score components — the score only needs the
            # averages/flags, but the dashboard (Module 7) needs to show
            # Dow/Nasdaq/S&P and Nikkei/HangSeng/Kospi/Shanghai individually.
            "us_quotes": us_quotes,
            "asia_quotes": asia_quotes,
            "macro_quotes": macro_quotes,
        },
        "headlines": news["headlines"],
        "news_sentiment": news["news_sentiment"],
    }

    await storage.save_morning_brief(brief)
    result = {**brief, "disclaimer": scoring.DISCLAIMER}

    try:
        result["telegram_sent"] = await notify.send_brief(result)
    except Exception as e:
        print(f"jobs: notify.send_brief failed: {e}")
        result["telegram_sent"] = False

    return result
