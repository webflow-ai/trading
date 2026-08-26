"""
market_data.py — quotes for the pre-market cues (US/Asia/macro/Nifty) and
GIFT Nifty. Module 3 (first half) of the build order
(docs/PREMARKET_ENGINE.md).

Reuses the free, unauthenticated Yahoo Finance chart API already used by
backend.py's fetch_yahoo_candles, instead of adding the `yfinance` package as
a new dependency (see docs/PREMARKET_ENGINE.md, Open Decision 3). Every
fetch function returns None (never raises) on failure so one dead source
never takes the whole pre-market brief down with it.

Verified live 2026-08-11: fetch_gift_nifty() needed three fixes over its
original best guess — niftytrader.in/gift-nifty 301-redirects to
/gift-nifty-live (now fetched directly); the page is Next.js and the actual
data lives in the __NEXT_DATA__ script tag's embedded JSON at
props.pageProps.initialGiftData, not loose "lastPrice"/"netChange" keys
in the HTML; and the live price field there is called last_trade_price,
not lastPrice.
"""

import asyncio
import datetime as dt
import json
import re

import httpx

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

YF_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

GIFT_NIFTY_URL = "https://www.niftytrader.in/gift-nifty-live"

YF_SYMBOLS = {
    "us_dow": "^DJI",
    "us_nasdaq": "^IXIC",
    "us_sp500": "^GSPC",
    "asia_nikkei": "^N225",
    "asia_hangseng": "^HSI",
    "asia_kospi": "^KS11",
    "asia_shanghai": "000001.SS",
    "brent": "BZ=F",
    "wti": "CL=F",
    "usdinr": "USDINR=X",
    "dxy": "DX-Y.NYB",
    "us10y": "^TNX",
    "nifty": "^NSEI",
}


async def fetch_ohlc_candles(client: httpx.AsyncClient, yf_symbol: str, interval: str, rng: str) -> list[dict]:
    """Raw OHLC rows with real datetimes (IST). Same shape as backend.py's
    fetch_yahoo_candles (kept as a separate copy here rather than an import
    from backend.py — see docs/PREMARKET_ENGINE.md, Open Decision 2)."""
    resp = await client.get(
        YF_CHART_URL.format(symbol=yf_symbol),
        params={"interval": interval, "range": rng},
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    result = ((resp.json().get("chart") or {}).get("result") or [None])[0]
    if not result:
        return []
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    opens, highs = quote.get("open", []), quote.get("high", [])
    lows, closes = quote.get("low", []), quote.get("close", [])
    rows = []
    for i, ts in enumerate(timestamps):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        if None in (o, h, l, c):
            continue  # Yahoo leaves gaps for pre/post-market or halts
        rows.append({"dt": dt.datetime.fromtimestamp(ts, IST), "open": o, "high": h, "low": l, "close": c})
    return rows


async def fetch_quote(client: httpx.AsyncClient, yf_symbol: str) -> dict | None:
    """{"price", "previous_close", "pct_change"} for one Yahoo Finance
    symbol, or None if the fetch failed — callers must treat a missing quote
    as 'unavailable', not crash."""
    try:
        resp = await client.get(
            YF_CHART_URL.format(symbol=yf_symbol),
            params={"interval": "1d", "range": "5d"},
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        result = ((resp.json().get("chart") or {}).get("result") or [None])[0]
        if not result:
            return None
        meta = result.get("meta") or {}
        price = meta.get("regularMarketPrice")
        previous_close = meta.get("previousClose") or meta.get("chartPreviousClose")
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        closes = [c for c in (quote.get("close") or []) if c is not None]
        if previous_close is None and len(closes) >= 2:
            previous_close = closes[-2]
        if price is None and closes:
            price = closes[-1]
        if price is None or not previous_close:
            return None
        pct_change = (price - previous_close) / previous_close * 100
        as_of = None
        ts = meta.get("regularMarketTime")
        if isinstance(ts, (int, float)):
            as_of = dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()
        return {
            "price": round(price, 4),
            "previous_close": round(previous_close, 4),
            "pct_change": round(pct_change, 4),
            "market_state": meta.get("marketState") or None,
            "as_of": as_of,
            "source": "yahoo",
        }
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        print(f"market_data: quote fetch failed for {yf_symbol}: {e}")
        return None


async def fetch_quotes(symbols: list[str]) -> dict:
    """symbols: Yahoo Finance ticker strings (e.g. '^DJI', 'BZ=F'). Returns
    {symbol: quote_dict_or_None} with one entry per requested symbol — every
    key is present even on failure so callers can tell 'fetched, flat' apart
    from 'unavailable'."""
    async with httpx.AsyncClient(timeout=15) as client:
        quotes = await asyncio.gather(*[fetch_quote(client, symbol) for symbol in symbols])
        return dict(zip(symbols, quotes))


NEXT_DATA_RE = re.compile(r'__NEXT_DATA__"\s*type="application/json">(.*?)</script>', re.DOTALL)


def _parse_gift_nifty_html(html: str) -> dict | None:
    """Isolated from the network call so it's unit-testable against a saved
    fixture. The page is Next.js — the real data is server-embedded JSON in
    the __NEXT_DATA__ script tag, at props.pageProps.initialGiftData, not
    loose key/value pairs in visible HTML. Returns None if that shape isn't
    found — never raises."""
    match = NEXT_DATA_RE.search(html)
    if not match:
        return None
    try:
        gift = json.loads(match.group(1))["props"]["pageProps"]["initialGiftData"]
        price = gift.get("last_trade_price")
        if price is None:
            return None
        change = gift.get("change_value")
        return {"price": float(price), "change": float(change) if change is not None else None}
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


async def fetch_gift_nifty(client: httpx.AsyncClient | None = None) -> dict | None:
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=15, follow_redirects=True)
    try:
        resp = await client.get(GIFT_NIFTY_URL, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        resp.raise_for_status()
        parsed = _parse_gift_nifty_html(resp.text)
        if parsed is None:
            print("market_data: could not parse GIFT Nifty page — marking unavailable")
        return parsed
    except Exception as e:
        print(f"market_data: GIFT Nifty fetch failed: {e}")
        return None
    finally:
        if owns_client:
            await client.aclose()
