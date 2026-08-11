"""
technicals.py — pre-market levels (PDH/PDL/previous close) and a simple
BOS/CHoCH market-structure detector for Nifty. Module 3 (second half) of the
build order (docs/PREMARKET_ENGINE.md).

Both compute_levels() and compute_structure() are meant to be called only
from the evening/morning jobs, i.e. while the market is closed — at those
times Yahoo's most recent daily/15m candle is always a *finished* session,
so "the last candle" and "the previous trading day" are the same thing. If
called while the market is open, the figures will reflect the still-forming
session instead of the prior close.
"""

import datetime as dt

import httpx

from market_data import fetch_ohlc_candles

NIFTY_YF_SYMBOL = "^NSEI"


async def compute_levels(client: httpx.AsyncClient | None = None) -> dict | None:
    """PDH, PDL, previous close, previous day range, and where the close sat
    in that range (0% = at the low, 100% = at the high). Returns None (never
    raises) on any fetch failure."""
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        candles = await fetch_ohlc_candles(client, NIFTY_YF_SYMBOL, interval="1d", rng="5d")
        if not candles:
            return None
        prev = candles[-1]
        pdh, pdl, pdc = prev["high"], prev["low"], prev["close"]
        day_range = pdh - pdl
        close_position_pct = round((pdc - pdl) / day_range * 100, 2) if day_range else None
        return {
            "pdh": pdh,
            "pdl": pdl,
            "previous_close": pdc,
            "previous_day_range": round(day_range, 2),
            "close_position_pct": close_position_pct,
        }
    except Exception as e:
        print(f"technicals: compute_levels failed: {e}")
        return None
    finally:
        if owns_client:
            await client.aclose()


def _find_fractal_swings(candles: list[dict], window: int = 2) -> list[dict]:
    """A candle is a swing high if its high is the most extreme within
    `window` candles on both sides (and symmetrically for swing lows) — the
    standard "fractal" definition. Returns swing points in chronological
    order as {"dt", "type": "high"|"low", "price"}."""
    swings = []
    n = len(candles)
    for i in range(window, n - window):
        c = candles[i]
        highs_around = [candles[j]["high"] for j in range(i - window, i + window + 1) if j != i]
        lows_around = [candles[j]["low"] for j in range(i - window, i + window + 1) if j != i]
        if c["high"] > max(highs_around):
            swings.append({"dt": c["dt"], "type": "high", "price": c["high"]})
        elif c["low"] < min(lows_around):
            swings.append({"dt": c["dt"], "type": "low", "price": c["low"]})
    return swings


def detect_structure(candles: list[dict], window: int = 2) -> dict:
    """Simple BOS/CHoCH detector.

    Bias starts neutral. Walking forward candle by candle, each close is
    checked against the most recently confirmed swing high/low:
      - close breaks above the last swing high while bias is bullish/neutral
        -> BOS_bullish (trend continues, or starts, upward), bias -> bullish
      - close breaks above the last swing high while bias is bearish
        -> CHoCH_bullish (reversal), bias -> bullish
      - symmetric on the downside against the last swing low
    A consumed swing level doesn't fire again, so only genuinely new breaks
    register, not every candle after an old one. Only the most recent event
    is reported.
    """
    swings = _find_fractal_swings(candles, window=window)
    if not swings:
        return {"bias": "neutral", "last_event": None}

    bias = "neutral"
    last_event = None
    last_swing_high = None
    last_swing_low = None

    swing_iter = iter(swings)
    next_swing = next(swing_iter, None)

    for c in candles:
        while next_swing is not None and next_swing["dt"] <= c["dt"]:
            if next_swing["type"] == "high":
                last_swing_high = next_swing["price"]
            else:
                last_swing_low = next_swing["price"]
            next_swing = next(swing_iter, None)

        if last_swing_high is not None and c["close"] > last_swing_high:
            last_event = "BOS_bullish" if bias in ("bullish", "neutral") else "CHoCH_bullish"
            bias = "bullish"
            last_swing_high = None
        elif last_swing_low is not None and c["close"] < last_swing_low:
            last_event = "BOS_bearish" if bias in ("bearish", "neutral") else "CHoCH_bearish"
            bias = "bearish"
            last_swing_low = None

    return {"bias": bias, "last_event": last_event}


async def compute_structure(client: httpx.AsyncClient | None = None) -> dict | None:
    """detect_structure() over the last 5 sessions of 15m candles. Returns
    None (never raises) on any fetch failure."""
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        candles = await fetch_ohlc_candles(client, NIFTY_YF_SYMBOL, interval="15m", rng="5d")
        if not candles:
            return None
        return detect_structure(candles)
    except Exception as e:
        print(f"technicals: compute_structure failed: {e}")
        return None
    finally:
        if owns_client:
            await client.aclose()
