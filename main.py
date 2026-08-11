"""
main.py — FastAPI app for the Nifty Pre-Market Analysis Engine. Module 5 of
the build order (docs/PREMARKET_ENGINE.md).

Deployed as its own Vercel serverless function (api/premarket.py re-exports
`app` from here) rather than merged into the existing PCR tracker's
api/index.py, keeping the two apps independent — an outage or redeploy of
one never touches the other. Routes are registered with the full
`/api/premarket/...` path baked in (matching this repo's existing
api/index.py convention) because Vercel forwards the whole incoming path to
the destination function without stripping a prefix.

No Pydantic response models here, unlike the original brief's engineering
requirements — this repo's existing API (api/index.py) returns plain dicts
everywhere, and matching that beats introducing a second convention for one
new file (docs/PREMARKET_ENGINE.md, "match this repo's conventions").
"""

import datetime as dt
import os

from dotenv import load_dotenv

# Must run before importing jobs/storage/etc. below — they each read their
# own env vars (SUPABASE_URL, TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, ...) at
# module import time, same pattern as backend.py/api/index.py. In
# production (Vercel) this is a harmless no-op since env vars are injected
# directly and no .env file is deployed; locally, without this, a .env file
# full of keys would silently never be read.
load_dotenv()

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

import jobs
import market_data
import paper_trading
import storage

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
JOB_API_KEY = os.getenv("PREMARKET_JOB_API_KEY")

app = FastAPI(title="Nifty Pre-Market Analysis Engine API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("CORS_ORIGIN", "http://localhost:5173")],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _require_job_api_key(x_api_key: str | None = Header(default=None)):
    if not JOB_API_KEY:
        raise HTTPException(500, "PREMARKET_JOB_API_KEY not configured on the server")
    if x_api_key != JOB_API_KEY:
        raise HTTPException(401, "invalid or missing X-API-Key")


@app.post("/api/premarket/jobs/evening")
async def trigger_evening_job(_: None = Depends(_require_job_api_key)):
    return await jobs.run_evening_job()


@app.post("/api/premarket/jobs/morning")
async def trigger_morning_job(is_event_day: bool = Query(False), _: None = Depends(_require_job_api_key)):
    return await jobs.run_morning_job(is_event_day=is_event_day)


@app.get("/api/premarket/brief/today")
async def brief_today():
    rows = await storage.get_brief_history(days=1)
    if not rows:
        return {"trade_date": None, "score": None, "verdict": None, "note": "no brief generated yet"}
    return rows[0]


def _classify_direction(previous_close: float | None, price: float | None, dead_zone_pct: float = 0.1) -> str | None:
    """'up'/'down'/'flat' for how `price` compares to `previous_close`, with
    a small dead zone so a +0.02% wobble doesn't count as a directional
    move either way. None if either input is missing."""
    if previous_close is None or price is None or not previous_close:
        return None
    pct = (price - previous_close) / previous_close * 100
    if pct > dead_zone_pct:
        return "up"
    if pct < -dead_zone_pct:
        return "down"
    return "flat"


def _verdict_direction(verdict: str | None) -> str | None:
    return {"Gap-up likely": "up", "Gap-down likely": "down", "Flat open": "flat"}.get(verdict)


async def _actual_open_after(trade_date: str, client) -> float | None:
    """The Nifty open on the first trading day after `trade_date` — i.e.
    what a brief dated `trade_date` was predicting."""
    candles = await market_data.fetch_ohlc_candles(client, "^NSEI", interval="1d", rng="3mo")
    target = dt.date.fromisoformat(trade_date) + dt.timedelta(days=1)
    for c in candles:
        if c["dt"].date() >= target:
            return c["open"]
    return None


@app.get("/api/premarket/brief/history")
async def brief_history(days: int = Query(30)):
    rows = await storage.get_brief_history(days=days)
    out = []
    async with httpx.AsyncClient(timeout=15) as client:
        for row in rows:
            previous_close = (row.get("components") or {}).get("previous_close")
            actual_open = await _actual_open_after(row["trade_date"], client) if row.get("trade_date") else None
            actual_direction = _classify_direction(previous_close, actual_open)
            predicted_direction = _verdict_direction(row.get("verdict"))
            hit = (
                actual_direction is not None and predicted_direction is not None
                and actual_direction == predicted_direction
            )
            out.append({**row, "actual_open": actual_open, "actual_direction": actual_direction, "hit": hit})

    scored = [r for r in out if r["actual_direction"] is not None]
    hit_rate = round(sum(1 for r in scored if r["hit"]) / len(scored) * 100, 1) if scored else None
    return {"briefs": out, "hit_rate_pct": hit_rate}


@app.get("/api/premarket/positioning/fii-trend")
async def positioning_fii_trend(days: int = Query(30)):
    return {"days": days, "rows": await storage.get_fii_trend(days=days)}


@app.post("/api/premarket/paper-trades")
async def create_paper_trade_endpoint(payload: dict):
    required = ["strike", "option_type", "action", "entry_price"]
    missing = [k for k in required if payload.get(k) is None]
    if missing:
        raise HTTPException(400, f"missing fields: {', '.join(missing)}")
    if payload["option_type"] not in ("CE", "PE"):
        raise HTTPException(400, "option_type must be 'CE' or 'PE'")
    if payload["action"] not in ("BUY", "SELL"):
        raise HTTPException(400, "action must be 'BUY' or 'SELL'")
    trade = await paper_trading.open_trade(
        strike=payload["strike"],
        option_type=payload["option_type"],
        action=payload["action"],
        entry_price=payload["entry_price"],
        lots=payload.get("lots", 1),
        lot_size=payload.get("lot_size", paper_trading.DEFAULT_LOT_SIZE),
        symbol=payload.get("symbol", "NIFTY"),
        notes=payload.get("notes"),
    )
    return trade


@app.post("/api/premarket/paper-trades/{trade_id}/close")
async def close_paper_trade_endpoint(trade_id: int, payload: dict):
    exit_price = payload.get("exit_price")
    if exit_price is None:
        raise HTTPException(400, "exit_price required")
    trade = await paper_trading.close_trade(trade_id, exit_price)
    if not trade:
        raise HTTPException(404, "trade not found, or already closed")
    return trade


@app.get("/api/premarket/paper-trades")
async def list_paper_trades_endpoint(status: str | None = Query(None), days: int = Query(90)):
    trades = await storage.get_paper_trades(status=status, days=days)
    return {"trades": trades, "summary": paper_trading.summarize(trades)}


@app.get("/api/premarket/health")
async def health():
    return {"ok": True, "supabase_configured": storage.configured()}
