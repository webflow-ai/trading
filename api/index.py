"""
PCR + option chain API — Vercel Python serverless version
-----------------------------------------------------------
Everything is fetched on-demand per request (with a short in-memory reuse
window that only helps on "warm" invocations — Vercel doesn't guarantee
warm reuse between requests, unlike a normal long-running server).

There is intentionally no background scheduler here: serverless functions
only run in response to a request. PCR history is instead persisted to a
Redis-compatible store (Upstash, via Vercel's KV integration) so any visitor
sees the full day's history, not just what accumulated since they opened
the tab — /api/pcr/today appends the current reading on every fresh fetch,
/api/pcr/history reads it back, and /api/cron/poll (wired to a Vercel Cron
schedule) keeps it building even with nobody actively viewing the page.

⚠️ NSE gotcha (same one from backend.py, now higher-stakes): NSE actively
blocks datacenter/cloud IPs. This code primes cookies + sends browser-like
headers, which is what makes it work from a normal residential machine —
whether that survives Vercel's own IP ranges is untested. If every NSE-backed
route starts failing after deploy, that's almost certainly why; the
Yahoo-Finance-backed /api/candles route doesn't have this problem.
"""

import os
import json
import asyncio
import datetime as dt
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

# On Vercel these come from the dashboard's Environment Variables, not a
# file — load_dotenv() is a harmless no-op there since no .env is deployed.
load_dotenv()

# ---------------- config ----------------
SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY"]
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
CHAIN_TOP_N = int(os.getenv("CHAIN_TOP_N", "10"))
# How long an on-demand cache entry is reused before re-fetching — only
# matters within a single warm invocation; a cold start always fetches fresh.
ON_DEMAND_MAX_AGE = int(os.getenv("ON_DEMAND_MAX_AGE", "3"))

# Yahoo Finance's public chart API — free, unauthenticated, gives real
# historical intraday OHLC candles (NSE itself has no free candle endpoint).
YF_SYMBOL = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", "FINNIFTY": "NIFTY_FIN_SERVICE.NS"}

BASE = "https://www.nseindia.com"
CONTRACT_INFO_URL = BASE + "/api/option-chain-contract-info?symbol={sym}"
OC_URL = BASE + "/api/option-chain-v3?type=Indices&symbol={sym}&expiry={expiry}"
PRIME_URL = BASE + "/option-chain"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": PRIME_URL,
    "Connection": "keep-alive",
}

# ---------------- Upstox (optional real-time data source) ----------------
# Same as before, but note: upstox_token/upstox_config living in a plain
# module-level dict only survives for as long as this invocation stays warm
# — there is no guarantee it'll still be there on the next request. Fine for
# the optional/dormant OAuth scaffolding; not something to rely on here.
UPSTOX_REDIRECT_URI = os.getenv("UPSTOX_REDIRECT_URI", "http://localhost:8000/api/upstox/callback")
upstox_config: dict = {
    "api_key": os.getenv("UPSTOX_API_KEY"),
    "api_secret": os.getenv("UPSTOX_API_SECRET"),
}
upstox_token: dict = {
    "access_token": os.getenv("UPSTOX_ACCESS_TOKEN") or None,
    "obtained_at": dt.datetime.now(IST).isoformat() if os.getenv("UPSTOX_ACCESS_TOKEN") else None,
}

# ---------------- in-memory (best-effort, warm-invocation-only) caches ----------------
chain_store: dict = {}   # chain_store[symbol][expiry] = {"updatedAt", "spot", "rows"}
pcr_cache: dict = {}     # pcr_cache[symbol] = {"updatedAt", "expiry", "pcrOi", "pcrVol", "putOi", "callOi"}
expiry_cache: dict = {}  # expiry_cache[symbol] = {"date", "expiries"}
STRIKE_PERSIST_INTERVAL_SECONDS = 5 * 60  # a real 5-min series, driven by visitor traffic — not cron

_client: httpx.AsyncClient | None = None

# ---------------- persistent PCR history (Upstash Redis via Vercel KV) ----------------
KV_URL = os.getenv("KV_REST_API_URL")
KV_TOKEN = os.getenv("KV_REST_API_TOKEN")
PCR_HISTORY_TTL_SECONDS = 2 * 24 * 3600  # keep ~2 trading days, then let old keys expire


async def redis_cmd(*args):
    """Raw call to Upstash's REST API (a plain HTTP POST with the Redis
    command as a JSON array) — no redis client library needed, and it works
    fine from a serverless function with no persistent connection."""
    if not (KV_URL and KV_TOKEN):
        return None
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(KV_URL, json=list(args), headers={"Authorization": f"Bearer {KV_TOKEN}"})
        r.raise_for_status()
        return r.json().get("result")


async def push_snapshot(key: str, snap: dict):
    if not (KV_URL and KV_TOKEN):
        return
    try:
        await redis_cmd("RPUSH", key, json.dumps(snap))
        await redis_cmd("EXPIRE", key, PCR_HISTORY_TTL_SECONDS)
    except Exception as e:
        print(f"redis push failed for {key}: {e}")


async def get_history(key: str) -> list:
    if not (KV_URL and KV_TOKEN):
        return []
    try:
        raw = await redis_cmd("LRANGE", key, 0, -1)
        return [json.loads(x) for x in (raw or [])]
    except Exception as e:
        print(f"redis history fetch failed for {key}: {e}")
        return []


async def push_pcr_snapshot(symbol: str, day: str, snap: dict):
    await push_snapshot(f"pcr:{symbol}:{day}", snap)


async def get_pcr_history(symbol: str, day: str) -> list:
    return await get_history(f"pcr:{symbol}:{day}")


async def push_strike_pcr_snapshot(symbol: str, strike, day: str, snap: dict):
    await push_snapshot(f"strikepcr:{symbol}:{strike}:{day}", snap)


async def get_strike_pcr_history(symbol: str, strike, day: str) -> list:
    return await get_history(f"strikepcr:{symbol}:{strike}:{day}")


UPSTOX_TOKEN_REDIS_KEY = "upstox:access_token"
# Upstox access tokens expire at ~3:30am IST the day after they're issued
# (not a rolling TTL) -- 20h is a conservative window that comfortably
# covers a token obtained anytime during market hours or the evening.
UPSTOX_TOKEN_TTL_SECONDS = 20 * 3600


async def save_upstox_token(access_token: str):
    """Persists to Redis (not just the in-memory dict) so a token obtained
    via one serverless invocation's /api/upstox/callback is actually visible
    to the *next* invocation's option-chain request -- see the in-memory
    dict's own doc comment above for why that alone isn't reliable here."""
    upstox_token["access_token"] = access_token
    upstox_token["obtained_at"] = dt.datetime.now(IST).isoformat()
    try:
        await redis_cmd("SET", UPSTOX_TOKEN_REDIS_KEY, access_token)
        await redis_cmd("EXPIRE", UPSTOX_TOKEN_REDIS_KEY, UPSTOX_TOKEN_TTL_SECONDS)
    except Exception as e:
        print(f"upstox: failed to persist token to redis: {e}")


async def load_upstox_token() -> str | None:
    if upstox_token["access_token"]:
        return upstox_token["access_token"]
    try:
        token = await redis_cmd("GET", UPSTOX_TOKEN_REDIS_KEY)
        if token:
            upstox_token["access_token"] = token
            return token
    except Exception as e:
        print(f"upstox: failed to load token from redis: {e}")
    return None


async def clear_upstox_token():
    upstox_token["access_token"] = None
    try:
        await redis_cmd("DEL", UPSTOX_TOKEN_REDIS_KEY)
    except Exception as e:
        print(f"upstox: failed to clear token in redis: {e}")


async def claim_strike_persist_slot(symbol: str, day: str, now: dt.datetime) -> bool:
    """True at most once per STRIKE_PERSIST_INTERVAL_SECONDS per symbol/day —
    backed by Redis (not local memory) so the throttle holds across
    serverless cold starts and concurrent instances, not just one process."""
    if not (KV_URL and KV_TOKEN):
        return False
    key = f"strikepersist:{symbol}:{day}"
    try:
        last = await redis_cmd("GET", key)
        if last:
            age = (now - dt.datetime.fromisoformat(last)).total_seconds()
            if age < STRIKE_PERSIST_INTERVAL_SECONDS:
                return False
        await redis_cmd("SET", key, now.isoformat())
        await redis_cmd("EXPIRE", key, PCR_HISTORY_TTL_SECONDS)
        return True
    except Exception as e:
        print(f"strike persist throttle check failed: {e}")
        return False


async def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True)
        await _client.get(PRIME_URL)  # prime cookies
    return _client


async def get_all_expiries(symbol: str) -> list:
    today = dt.datetime.now(IST).date().isoformat()
    cached = expiry_cache.get(symbol)
    if cached and cached["date"] == today:
        return cached["expiries"]
    client = await get_client()
    url = CONTRACT_INFO_URL.format(sym=symbol)
    for attempt in range(2):
        r = await client.get(url)
        if r.status_code in (401, 403):
            await client.get(PRIME_URL)
            continue
        r.raise_for_status()
        expiries = r.json().get("expiryDates") or []
        expiry_cache[symbol] = {"date": today, "expiries": expiries}
        return expiries
    r.raise_for_status()
    return []


async def get_nearest_expiry(symbol: str) -> str:
    expiries = await get_all_expiries(symbol)
    return expiries[0] if expiries else ""


async def fetch_option_chain_for_expiry(symbol: str, expiry: str) -> dict:
    client = await get_client()
    url = OC_URL.format(sym=symbol, expiry=expiry)
    for attempt in range(2):
        r = await client.get(url)
        if r.status_code in (401, 403):
            await client.get(PRIME_URL)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return {}


def compute_pcr(oc: dict, target_expiry: str | None = None) -> dict:
    records = oc.get("records", {})
    expiries = records.get("expiryDates") or []
    expiry = target_expiry or (expiries[0] if expiries else "")
    put_oi = call_oi = put_vol = call_vol = 0
    for row in records.get("data", []):
        if expiry and row.get("expiryDates") != expiry:
            continue
        ce, pe = row.get("CE"), row.get("PE")
        if ce:
            call_oi += ce.get("openInterest", 0) or 0
            call_vol += ce.get("totalTradedVolume", 0) or 0
        if pe:
            put_oi += pe.get("openInterest", 0) or 0
            put_vol += pe.get("totalTradedVolume", 0) or 0
    return {
        "expiry": expiry,
        "pcrOi": round(put_oi / call_oi, 4) if call_oi else None,
        "pcrVol": round(put_vol / call_vol, 4) if call_vol else None,
        "putOi": put_oi, "callOi": call_oi,
    }


def build_chain_rows(oc: dict, top_n: int = CHAIN_TOP_N, target_expiry: str | None = None) -> dict:
    records = oc.get("records", {})
    expiries = records.get("expiryDates") or []
    expiry = target_expiry or (expiries[0] if expiries else "")
    spot = records.get("underlyingValue")
    rows = []
    for row in records.get("data", []):
        if expiry and row.get("expiryDates") != expiry:
            continue
        strike = row.get("strikePrice")
        if strike is None:
            continue
        ce, pe = row.get("CE") or {}, row.get("PE") or {}
        rows.append({
            "strike": strike,
            "ceOi": ce.get("openInterest", 0) or 0,
            "ceOiChg": ce.get("changeinOpenInterest", 0) or 0,
            "ceVol": ce.get("totalTradedVolume", 0) or 0,
            "ceLtp": ce.get("lastPrice", 0) or 0,
            "peOi": pe.get("openInterest", 0) or 0,
            "peOiChg": pe.get("changeinOpenInterest", 0) or 0,
            "peVol": pe.get("totalTradedVolume", 0) or 0,
            "peLtp": pe.get("lastPrice", 0) or 0,
        })
    if spot is not None and rows:
        rows.sort(key=lambda r: abs(r["strike"] - spot))
        rows = rows[:top_n]
    rows.sort(key=lambda r: r["strike"])
    return {"expiry": expiry, "spot": spot, "rows": rows}


async def fetch_yahoo_candles(yf_symbol: str, interval: str, rng: str) -> list:
    async with httpx.AsyncClient(timeout=15) as yf_client:
        r = await yf_client.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}",
            params={"interval": interval, "range": rng},
            headers={"User-Agent": HEADERS["User-Agent"]},
        )
    r.raise_for_status()
    result = ((r.json().get("chart") or {}).get("result") or [None])[0]
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
            continue
        rows.append({"dt": dt.datetime.fromtimestamp(ts, IST), "open": o, "high": h, "low": l, "close": c})
    return rows


def resample_candles(rows: list, bucket_hours: int) -> list:
    buckets: dict = {}
    order = []
    for row in rows:
        d = row["dt"]
        bucket_hour = (d.hour // bucket_hours) * bucket_hours
        key = (d.date(), bucket_hour)
        if key not in buckets:
            buckets[key] = {
                "dt": d.replace(hour=bucket_hour, minute=0, second=0, microsecond=0),
                "open": row["open"], "high": row["high"], "low": row["low"], "close": row["close"],
            }
            order.append(key)
        else:
            b = buckets[key]
            b["high"] = max(b["high"], row["high"])
            b["low"] = min(b["low"], row["low"])
            b["close"] = row["close"]
    return [buckets[k] for k in order]


# ---------------- API ----------------
app = FastAPI(title="PCR Session Clock API")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.post("/api/upstox/configure")
async def upstox_configure(payload: dict):
    upstox_config["api_key"] = payload.get("api_key")
    upstox_config["api_secret"] = payload.get("api_secret")
    return {"ok": True, "configured": bool(upstox_config["api_key"] and upstox_config["api_secret"])}


@app.get("/api/upstox/status")
async def upstox_status():
    token = await load_upstox_token()
    return {
        "configured": bool(upstox_config["api_key"] and upstox_config["api_secret"]),
        "connected": bool(token),
        "obtainedAt": upstox_token["obtained_at"],
    }


@app.get("/api/upstox/login")
async def upstox_login():
    if not upstox_config["api_key"]:
        return {"error": "Call POST /api/upstox/configure with api_key + api_secret first"}
    url = (
        "https://api.upstox.com/v2/login/authorization/dialog"
        f"?client_id={upstox_config['api_key']}&redirect_uri={UPSTOX_REDIRECT_URI}&response_type=code"
    )
    return {"login_url": url}


@app.get("/api/upstox/callback", response_class=HTMLResponse)
async def upstox_callback(code: str = Query(None), error: str = Query(None)):
    if error:
        return f"<h3>Upstox login error: {error}</h3>"
    if not code:
        return "<h3>No authorization code received.</h3>"
    if not (upstox_config["api_key"] and upstox_config["api_secret"]):
        return "<h3>Backend isn't configured — call POST /api/upstox/configure first, then retry login.</h3>"
    try:
        async with httpx.AsyncClient(timeout=15) as up_client:
            resp = await up_client.post(
                "https://api.upstox.com/v2/login/authorization/token",
                data={
                    "code": code,
                    "client_id": upstox_config["api_key"],
                    "client_secret": upstox_config["api_secret"],
                    "redirect_uri": UPSTOX_REDIRECT_URI,
                    "grant_type": "authorization_code",
                },
                headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            )
        if resp.status_code != 200:
            return f"<h3>Token exchange failed: {resp.status_code}</h3><pre>{resp.text}</pre>"
        j = resp.json()
        token = j.get("access_token")
        if not token:
            return f"<h3>Token exchange succeeded but no access_token in response</h3><pre>{resp.text}</pre>"
        await save_upstox_token(token)
        return "<h2>&#9989; Upstox connected. You can close this tab.</h2>"
    except Exception as e:
        return f"<h3>Token exchange failed: {e}</h3>"


UPSTOX_UNDERLYING_KEY = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
}


def _nse_expiry_to_iso(expiry: str) -> str:
    """NSE's expiry strings look like '18-Aug-2026'; Upstox's option-chain
    API wants '2026-08-18'. Falls back to the original string if it's
    already ISO (or anything else unrecognized) so a format-string change
    upstream degrades to an Upstox-side error instead of an exception here."""
    try:
        return dt.datetime.strptime(expiry, "%d-%b-%Y").date().isoformat()
    except ValueError:
        return expiry


@app.get("/api/upstox/optionchain")
async def upstox_optionchain(symbol: str = Query("NIFTY"), expiry: str = Query(None)):
    """Live NIFTY/BANKNIFTY/FINNIFTY option chain sourced directly from
    Upstox's own option-chain API (real broker LTPs from a live market-data
    subscription) rather than the NSE scrape /api/optionchain/today relies
    on -- built for the paper-trading journal's live price feed, which wants
    tighter, more trustworthy refresh than NSE's own CDN caching allows.
    Requires a connected session (see /api/upstox/login); the frontend
    falls back to /api/optionchain/today when this reports not connected.

    NOTE: field names below (call_options/put_options/market_data/oi/ltp)
    follow Upstox's v2 option-chain response shape as documented, but this
    hasn't been exercised against a live response yet (no token available
    while building this) -- if Upstox renames/nests fields differently than
    expected, this will come back with rows present but ltp/oi as null
    rather than raising, so check a real response after connecting.
    """
    symbol = symbol.upper()
    underlying_key = UPSTOX_UNDERLYING_KEY.get(symbol)
    if not underlying_key:
        return {"connected": False, "spot": None, "rows": [], "error": f"unsupported symbol for Upstox: {symbol}"}

    token = await load_upstox_token()
    if not token:
        return {"connected": False, "spot": None, "rows": [], "error": "Upstox not connected — visit /api/upstox/login"}

    if not expiry:
        try:
            expiry = await get_nearest_expiry(symbol)
        except Exception as e:
            return {"connected": True, "spot": None, "rows": [], "error": f"couldn't resolve nearest expiry: {e}"}
    # Callers (the main PCR tracker's OptionChain panel included) pass
    # through whatever /api/expiries gave them, which is NSE's own
    # '18-Aug-2026' format -- normalize unconditionally so an explicitly
    # passed expiry works too, not just the internally-resolved default.
    expiry = _nse_expiry_to_iso(expiry)

    try:
        async with httpx.AsyncClient(timeout=10) as up_client:
            resp = await up_client.get(
                "https://api.upstox.com/v2/option/chain",
                params={"instrument_key": underlying_key, "expiry_date": expiry},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
    except Exception as e:
        return {"connected": True, "spot": None, "rows": [], "error": f"upstox request failed: {e}"}

    if resp.status_code == 401:
        # Token's dead (expired or revoked) -- clear it so /api/upstox/status
        # reflects reality and the next call fails fast with "not connected"
        # instead of repeating a doomed request every second.
        await clear_upstox_token()
        return {"connected": False, "spot": None, "rows": [], "error": "Upstox session expired — visit /api/upstox/login again"}
    if resp.status_code != 200:
        return {"connected": True, "spot": None, "rows": [], "error": f"upstox returned {resp.status_code}: {resp.text[:200]}"}

    try:
        payload = resp.json()
    except Exception as e:
        return {"connected": True, "spot": None, "rows": [], "error": f"upstox returned non-JSON: {e}"}

    def _oi_change(md: dict):
        # Upstox's market_data has no direct "change" field -- oi minus
        # prev_oi is the actual change, confirmed against a real response.
        oi, prev_oi = md.get("oi"), md.get("prev_oi")
        return (oi - prev_oi) if oi is not None and prev_oi is not None else None

    data = payload.get("data") or []
    spot = None
    rows = []
    for item in data:
        if spot is None:
            spot = item.get("underlying_spot_price")
        call_md = (item.get("call_options") or {}).get("market_data") or {}
        put_md = (item.get("put_options") or {}).get("market_data") or {}
        rows.append({
            "strike": item.get("strike_price"),
            "ceOi": call_md.get("oi"),
            "ceOiChg": _oi_change(call_md),
            "ceVol": call_md.get("volume"),
            "ceLtp": call_md.get("ltp"),
            "peOi": put_md.get("oi"),
            "peOiChg": _oi_change(put_md),
            "peVol": put_md.get("volume"),
            "peLtp": put_md.get("ltp"),
        })
    rows.sort(key=lambda r: r["strike"] if r["strike"] is not None else 0)
    return {"connected": True, "symbol": symbol, "expiry": expiry, "spot": spot, "rows": rows}


# ---------------- Top-10 Nifty movers (index-weighted contribution) ----------------
# weight_pct is each stock's approximate share of the Nifty 50 free-float
# market-cap index, per NSE's published index factsheet -- NSE revises these
# quarterly and there's no free live API for them, so this is a static
# snapshot that will drift out of date over time (flagged here rather than
# silently treated as authoritative; re-check against the current factsheet
# periodically). instrument_key uses Upstox's documented NSE_EQ|<ISIN>
# format -- ISINs themselves don't change, but this endpoint (like
# upstox_optionchain above) hasn't been exercised against a live response
# yet, so verify field names once actually connected.
NIFTY_TOP10 = [
    {"symbol": "HDFCBANK", "name": "HDFC Bank", "isin": "INE040A01034", "weight_pct": 13.0},
    {"symbol": "ICICIBANK", "name": "ICICI Bank", "isin": "INE090A01021", "weight_pct": 8.7},
    {"symbol": "RELIANCE", "name": "Reliance Industries", "isin": "INE002A01018", "weight_pct": 8.5},
    {"symbol": "INFY", "name": "Infosys", "isin": "INE009A01021", "weight_pct": 5.7},
    {"symbol": "ITC", "name": "ITC", "isin": "INE154A01025", "weight_pct": 4.3},
    {"symbol": "TCS", "name": "Tata Consultancy Services", "isin": "INE467B01029", "weight_pct": 4.0},
    {"symbol": "LT", "name": "Larsen & Toubro", "isin": "INE018A01030", "weight_pct": 3.9},
    {"symbol": "BHARTIARTL", "name": "Bharti Airtel", "isin": "INE397D01024", "weight_pct": 3.8},
    {"symbol": "AXISBANK", "name": "Axis Bank", "isin": "INE238A01034", "weight_pct": 3.3},
    {"symbol": "KOTAKBANK", "name": "Kotak Mahindra Bank", "isin": "INE237A01036", "weight_pct": 3.0},
]

# Chosen default, same caveat as scoring.py's calibration constants: wider
# than the 0.1% dead zone brief_history uses to classify an *actual* Nifty
# open, since this reads only ~58% of the index's weight (these 10 stocks'
# weight_pct sum) as a proxy for the whole -- a noisier signal deserves a
# wider "flat" band before calling a direction. Untested against real
# outcomes yet; retune once movers/accuracy history exists to compare against.
MOVERS_VERDICT_THRESHOLD_PCT = 0.15


def _movers_verdict(implied_move_pct: float | None) -> str | None:
    if implied_move_pct is None:
        return None
    if implied_move_pct > MOVERS_VERDICT_THRESHOLD_PCT:
        return "Gap-up likely"
    if implied_move_pct < -MOVERS_VERDICT_THRESHOLD_PCT:
        return "Gap-down likely"
    return "Flat open"


def _prev_close_from_quote(q: dict) -> float | None:
    """Upstox's market-quote/quotes `ohlc.close` is documented as *today's*
    running close, which trivially equals last_price for as long as the
    market is open (it only becomes a fixed prior-session figure after
    that session ends) -- confirmed live 2026-08-17 09:39 IST, ohlc.close
    == last_price for every NIFTY_TOP10 stock while trading was live,
    which made every %change compute to exactly 0 during market hours (the
    bug hid over a weekend, when ohlc.close legitimately was Friday's
    final, fixed close).

    Upstox separately provides `net_change` (last_price minus the *real*
    previous close) directly in the same quote object, with no such
    live-session ambiguity, so prev_close is derived from that instead:
    last_price - net_change. Falls back to ohlc.close only if net_change
    is missing -- correct in exactly the case this bug didn't show up in
    (market closed), same graceful-degradation convention as the rest of
    this file's Upstox integrations."""
    ltp = q.get("last_price")
    net_change = q.get("net_change")
    if ltp is not None and net_change is not None:
        return ltp - net_change
    return (q.get("ohlc") or {}).get("close")


@app.get("/api/upstox/movers")
async def upstox_movers():
    """Live top-10-by-weight Nifty constituents, each stock's %change since
    previous close, and an "implied index move" = sum(weight_pct *
    pct_change) across the ten -- a weighted-contribution proxy for how much
    of today's Nifty move these heaviest names explain, not the actual index
    change itself (the other ~40 constituents aren't read here). Verdict
    reuses the same three-way labels as the morning brief's scoring.py
    ("Gap-up likely" / "Flat open" / "Gap-down likely") for one consistent
    vocabulary across the dashboard, but this is a separate, much simpler
    calculation -- it has no GIFT/macro/FII/news inputs, just these 10
    stocks' live price action.

    Requires a connected Upstox session (see /api/upstox/login), same as
    upstox_optionchain -- there's no NSE-scrape fallback for per-stock LTPs
    like there is for the option chain, so this degrades to
    connected:false rather than silently guessing.
    """
    token = await load_upstox_token()
    if not token:
        return {
            "connected": False, "stocks": [], "implied_move_pct": None, "verdict": None,
            "error": "Upstox not connected — visit /api/upstox/login",
        }

    instrument_keys = ",".join(f"NSE_EQ|{s['isin']}" for s in NIFTY_TOP10)
    try:
        async with httpx.AsyncClient(timeout=10) as up_client:
            resp = await up_client.get(
                "https://api.upstox.com/v2/market-quote/quotes",
                params={"instrument_key": instrument_keys},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
    except Exception as e:
        return {
            "connected": True, "stocks": [], "implied_move_pct": None, "verdict": None,
            "error": f"upstox request failed: {e}",
        }

    if resp.status_code == 401:
        await clear_upstox_token()
        return {
            "connected": False, "stocks": [], "implied_move_pct": None, "verdict": None,
            "error": "Upstox session expired — visit /api/upstox/login again",
        }
    if resp.status_code != 200:
        return {
            "connected": True, "stocks": [], "implied_move_pct": None, "verdict": None,
            "error": f"upstox returned {resp.status_code}: {resp.text[:200]}",
        }

    try:
        payload = resp.json()
    except Exception as e:
        return {
            "connected": True, "stocks": [], "implied_move_pct": None, "verdict": None,
            "error": f"upstox returned non-JSON: {e}",
        }

    # Matched by each quote's own instrument_token field rather than the
    # outer dict key -- Upstox's documented key format for this endpoint
    # ("NSE_EQ:SYMBOL" vs the instrument_key we sent) isn't confirmed live,
    # so trusting the key shape would be the same kind of guess
    # upstox_optionchain's docstring already warns against.
    by_key = {}
    for quote in (payload.get("data") or {}).values():
        token_field = quote.get("instrument_token")
        if token_field:
            by_key[token_field] = quote

    stocks = []
    total_contribution = 0.0
    any_live = False
    for s in NIFTY_TOP10:
        q = by_key.get(f"NSE_EQ|{s['isin']}")
        ltp = q.get("last_price") if q else None
        prev_close = _prev_close_from_quote(q) if q else None
        pct_change = (ltp - prev_close) / prev_close * 100 if ltp is not None and prev_close else None
        contribution = (pct_change * s["weight_pct"] / 100) if pct_change is not None else None
        if contribution is not None:
            total_contribution += contribution
            any_live = True
        stocks.append({
            "symbol": s["symbol"], "name": s["name"], "weight_pct": s["weight_pct"],
            "ltp": ltp, "prev_close": prev_close, "pct_change": pct_change, "contribution_pct": contribution,
        })

    implied_move_pct = round(total_contribution, 3) if any_live else None
    return {
        "connected": True, "stocks": stocks,
        "implied_move_pct": implied_move_pct, "verdict": _movers_verdict(implied_move_pct),
    }


# Upstox's v2 intraday-candle API only accepts "1minute"/"30minute" (confirmed
# live -- 5/15 minute both come back UDAPI1076 "Interval accepts one of
# (1minute,30minute)"). v3's intraday/historical-candle endpoints take a
# separate unit+number instead of one combined string and support finer
# granularities -- verified live for minutes/1, minutes/5, minutes/15,
# minutes/30, and days/1 -- so v3 is what's actually called below; this
# dict is just this repo's own "1minute"-style query-param spelling mapped
# to v3's (unit, number) pair.
UPSTOX_INTRADAY_INTERVALS = {"1minute": ("minutes", 1), "5minute": ("minutes", 5), "15minute": ("minutes", 15), "30minute": ("minutes", 30)}
DAILY_FALLBACK_LOOKBACK_DAYS = 10  # calendar days, not trading days -- comfortably covers a weekend/holiday gap


def _parse_candles(payload: dict) -> list[dict]:
    """Upstox candle row: [timestamp, open, high, low, close, volume, oi],
    documented as most-recent-first -- reversed here for a left-to-right
    chart. Full OHLC plus volume is kept (not just close) so the frontend
    can render a real candlestick + volume-histogram chart (lightweight-
    charts, see premarket.jsx's LightweightChart) from the same series.
    volume is optional (None if the row is shorter than expected) --
    everything downstream already treats a missing volume as "no volume
    panel for this point" rather than erroring. Shared by both the
    intraday and daily-fallback fetchers below since both endpoints return
    the same row shape."""
    candles = (payload.get("data") or {}).get("candles") or []
    points = [
        {
            "t": c[0], "open": c[1], "high": c[2], "low": c[3], "close": c[4],
            "volume": c[5] if len(c) > 5 else None,
        }
        for c in candles if isinstance(c, list) and len(c) >= 5
    ]
    points.reverse()
    return points


async def _fetch_stock_history(token: str, isin: str, interval: str) -> list[dict]:
    """One stock's price history, oldest-first, as {t, close} points ready
    for an area chart. Upstox has no batch historical-candle endpoint
    (unlike market-quote/quotes above), so getting all 10 movers' history
    means one request per stock -- callers should run these via
    asyncio.gather rather than sequentially awaiting each one.

    Tries today's intraday candles first; Upstox returns `candles: []`
    (success, not an error) rather than a fallback of its own when the
    market hasn't traded yet today (weekend, holiday, or simply before
    open) -- confirmed live. Rather than surface an empty chart in that
    common case, this falls back to the last ~10 calendar days of daily
    closes via Upstox's historical-candle range endpoint, so there's
    usually still a real chart to show. Not a like-for-like substitute for
    intraday (daily granularity, multi-day span instead of one session) --
    just better than blank.

    Never raises: any failure (network, non-200, unexpected shape) at
    either step just returns an empty list for that one stock, same
    degrade-per-row convention as upstox_movers/upstox_nifty50 above -- one
    stock's history being unavailable shouldn't blank out the other nine.
    """
    instrument_key = f"NSE_EQ|{isin}"
    unit, number = UPSTOX_INTRADAY_INTERVALS[interval]
    try:
        async with httpx.AsyncClient(timeout=10) as up_client:
            resp = await up_client.get(
                f"https://api.upstox.com/v3/historical-candle/intraday/{instrument_key}/{unit}/{number}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        if resp.status_code == 200:
            points = _parse_candles(resp.json())
            if points:
                return points
    except Exception:
        pass

    to_date = dt.date.today().isoformat()
    from_date = (dt.date.today() - dt.timedelta(days=DAILY_FALLBACK_LOOKBACK_DAYS)).isoformat()
    try:
        async with httpx.AsyncClient(timeout=10) as up_client:
            resp = await up_client.get(
                f"https://api.upstox.com/v3/historical-candle/{instrument_key}/days/1/{to_date}/{from_date}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        if resp.status_code != 200:
            return []
        return _parse_candles(resp.json())
    except Exception:
        return []


@app.get("/api/upstox/movers/history")
async def upstox_movers_history(interval: str = Query("30minute")):
    """Price history for each of NIFTY_TOP10, for the movers panel's area
    charts -- a heavier call than /api/upstox/movers (one Upstox request
    per stock, run in parallel via asyncio.gather), so the frontend should
    poll this on a much slower cadence than the live-quote endpoint. See
    _fetch_stock_history's own docstring for the intraday-vs-daily-fallback
    behavior.
    """
    if interval not in UPSTOX_INTRADAY_INTERVALS:
        return {"connected": True, "series": {}, "error": f"interval must be one of {sorted(UPSTOX_INTRADAY_INTERVALS)}"}

    token = await load_upstox_token()
    if not token:
        return {"connected": False, "series": {}, "error": "Upstox not connected — visit /api/upstox/login"}

    results = await asyncio.gather(*(_fetch_stock_history(token, s["isin"], interval) for s in NIFTY_TOP10))
    series = {s["symbol"]: points for s, points in zip(NIFTY_TOP10, results)}
    return {"connected": True, "series": series}


@app.get("/api/upstox/stock/history")
async def upstox_stock_history(symbol: str = Query(...), interval: str = Query("30minute")):
    """One stock's price history at a caller-chosen interval -- lighter
    than /api/upstox/movers/history, which always fetches all 10 stocks at
    once for the sparkline grid. Built for the movers modal's interval
    picker (5m/15m/30m): when the user switches granularity for the one
    stock they have open, there's no reason to re-fetch the other nine.
    Only serves NIFTY_TOP10 symbols today, matching the modal's only
    caller -- extend this if another panel ever needs a single-stock chart.
    """
    symbol = symbol.upper()
    if interval not in UPSTOX_INTRADAY_INTERVALS:
        return {"connected": True, "points": [], "error": f"interval must be one of {sorted(UPSTOX_INTRADAY_INTERVALS)}"}

    stock = next((s for s in NIFTY_TOP10 if s["symbol"] == symbol), None)
    if not stock:
        return {"connected": True, "points": [], "error": f"unknown symbol: {symbol}"}

    token = await load_upstox_token()
    if not token:
        return {"connected": False, "points": [], "error": "Upstox not connected — visit /api/upstox/login"}

    return {"connected": True, "points": await _fetch_stock_history(token, stock["isin"], interval)}


# ---------------- Backtest: which stocks drive big 5-min Nifty moves ----------------
# Ad-hoc analysis promoted to a real endpoint after a one-off script version
# (run manually, not committed) validated the approach: pull a month of real
# 5-min OHLC for the Nifty 50 index + NIFTY_TOP10, find bars where the index
# moved a large amount within that single 5-min candle, and attribute each
# one to whichever of the 10 stocks moved the most in that same window.
BACKTEST_EXCLUDED_OPEN_TIMES = {"09:15", "09:20", "09:25", "09:30", "09:35"}
# The opening 25 minutes (five 5-min bars) are excluded by default -- gap-
# open/price-discovery noise right at market open produces artificially
# large 5-min moves that aren't really "driven" by any one stock's ordinary
# trading; live-verified this cut one month's event count from 14 to 5.


async def _fetch_range_candles(token: str, instrument_key: str, unit: str, number: int, from_date: str, to_date: str) -> list[dict]:
    """Same v3 historical-candle range endpoint _fetch_stock_history's daily
    fallback uses, generalized to any unit/number/date-range -- this is
    what makes a month of 5-min bars possible (the intraday-only endpoint
    used elsewhere in this file is limited to the current trading day).
    Never raises: any failure returns an empty list, same degrade-per-
    instrument convention as the rest of this file's Upstox integrations.
    """
    encoded_key = quote(instrument_key, safe="")
    try:
        async with httpx.AsyncClient(timeout=20) as up_client:
            resp = await up_client.get(
                f"https://api.upstox.com/v3/historical-candle/{encoded_key}/{unit}/{number}/{to_date}/{from_date}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        if resp.status_code != 200:
            return []
        return _parse_candles(resp.json())
    except Exception:
        return []


@app.get("/api/upstox/movers/backtest")
async def upstox_movers_backtest(days: int = Query(30), threshold_pts: float = Query(50.0)):
    """Backtests the same weight% x %change formula /api/upstox/movers uses
    live, against `days` of real history: finds every 5-min Nifty 50 bar
    where the index moved at least threshold_pts within that single bar
    (excluding the opening candles, see BACKTEST_EXCLUDED_OPEN_TIMES), and
    for each one, ranks NIFTY_TOP10 by how much they moved in that same
    window. Also reports direction-match accuracy of the weighted formula
    against the real index move across every bar in the range, not just
    the flagged events.

    This is a genuinely heavy call -- 11 parallel Upstox requests (index +
    10 stocks), each returning ~75 bars/trading day -- so the frontend
    should trigger it on load/manual-refresh only, never on a poll
    interval. "Top mover" attribution is correlation within the same
    5-minute bar, not proof of causation -- the best a bar-level backtest
    like this can honestly claim.
    """
    token = await load_upstox_token()
    if not token:
        return {"connected": False, "error": "Upstox not connected — visit /api/upstox/login"}

    to_date = dt.date.today()
    from_date = to_date - dt.timedelta(days=days)
    weight_by_symbol = {s["symbol"]: s["weight_pct"] for s in NIFTY_TOP10}

    nifty_task = _fetch_range_candles(token, UPSTOX_UNDERLYING_KEY["NIFTY"], "minutes", 5, from_date.isoformat(), to_date.isoformat())
    stock_tasks = [
        _fetch_range_candles(token, f"NSE_EQ|{s['isin']}", "minutes", 5, from_date.isoformat(), to_date.isoformat())
        for s in NIFTY_TOP10
    ]
    nifty_candles, *stock_candle_lists = await asyncio.gather(nifty_task, *stock_tasks)

    if not nifty_candles:
        return {"connected": True, "error": "no Nifty 50 index candles returned for this range", "events": []}

    stock_by_symbol = {
        s["symbol"]: {c["t"]: c for c in candles}
        for s, candles in zip(NIFTY_TOP10, stock_candle_lists)
    }

    events = []
    scored_bars = []  # (implied_pct, nifty_move_pct) for every non-excluded bar with data
    excluded_count = 0
    for bar in nifty_candles:
        t = bar["t"]
        if t[11:16] in BACKTEST_EXCLUDED_OPEN_TIMES:
            excluded_count += 1
            continue
        o, c = bar["open"], bar["close"]
        if not o:
            continue
        move_pts = c - o
        move_pct = (c - o) / o * 100

        contributions = []
        implied_pct = 0.0
        for sym, weight in weight_by_symbol.items():
            srow = stock_by_symbol.get(sym, {}).get(t)
            if not srow or not srow["open"]:
                continue
            spct = (srow["close"] - srow["open"]) / srow["open"] * 100
            contributions.append({"symbol": sym, "pct_change": round(spct, 3)})
            implied_pct += spct * weight / 100

        if not contributions:
            continue
        scored_bars.append((implied_pct, move_pct))

        if abs(move_pts) >= threshold_pts:
            contributions.sort(key=lambda c: abs(c["pct_change"]), reverse=True)
            events.append({
                "t": t, "nifty_move_pts": round(move_pts, 1), "nifty_move_pct": round(move_pct, 3),
                "implied_pct": round(implied_pct, 3), "top_movers": contributions[:3],
            })

    scored_bars = [(i, n) for i, n in scored_bars if n != 0]
    direction_matches = sum(1 for implied, actual in scored_bars if (implied > 0) == (actual > 0))
    direction_accuracy = round(direction_matches / len(scored_bars) * 100, 1) if scored_bars else None
    rmse = (
        round((sum((implied - actual) ** 2 for implied, actual in scored_bars) / len(scored_bars)) ** 0.5, 4)
        if scored_bars else None
    )

    event_direction_matches = sum(1 for ev in events if (ev["implied_pct"] > 0) == (ev["nifty_move_pct"] > 0))
    event_direction_accuracy = round(event_direction_matches / len(events) * 100, 1) if events else None

    top_driver_counts: dict[str, int] = {}
    for ev in events:
        if ev["top_movers"]:
            top_sym = ev["top_movers"][0]["symbol"]
            top_driver_counts[top_sym] = top_driver_counts.get(top_sym, 0) + 1

    return {
        "connected": True,
        "days": days, "threshold_pts": threshold_pts,
        "from_date": from_date.isoformat(), "to_date": to_date.isoformat(),
        "total_bars": len(nifty_candles), "excluded_opening_bars": excluded_count,
        "event_count": len(events), "events": events,
        "top_driver_counts": top_driver_counts,
        "direction_accuracy_all_bars_pct": direction_accuracy,
        "direction_accuracy_events_pct": event_direction_accuracy,
        "rmse_all_bars": rmse,
        "top10_weight_pct": sum(weight_by_symbol.values()),
    }


# ---------------- Full Nifty 50 constituent board (unweighted) ----------------
# Best-effort snapshot of current Nifty 50 membership, symbol/ISIN pairs
# only -- unlike NIFTY_TOP10 above, this deliberately carries no per-stock
# weight_pct. Reliable weight figures are only really known with confidence
# for the heaviest names already in NIFTY_TOP10; assigning precise-looking
# weights to all 50 from memory would be false precision. This board reports
# plain price action (gainers/losers/breadth) instead of a weighted implied
# move.
#
# ⚠️ Membership itself is a bigger risk here than in NIFTY_TOP10: NSE
# reconstitutes the index semi-annually (index reviews add/drop names), and
# this list of *which 50 names* was built without a live fetch of the
# current factsheet -- some entries may be stale (a recently added
# constituent missing, a recently dropped one still listed).
#
# The symbol/ISIN pairs themselves, however, HAVE been verified: cross-
# checked against Upstox's own published NSE instrument master
# (assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz) on
# 2026-08-16, which caught and fixed 7 real errors from the original
# from-memory list -- 5 stale ISINs (KOTAKBANK, BAJFINANCE, BAJAJFINSV,
# DRREDDY, SHRIRAMFIN all had a transposed/wrong final digit or two) and 2
# renamed trading symbols Upstox no longer recognizes under their old name
# (TATAMOTORS demerged into TMCV/TMPV -- the Nifty 50 constituent is the
# passenger-vehicle entity, TMPV, which kept the original ISIN; LTIM's
# trading symbol on Upstox is now LTM). Each entry still degrades
# independently if wrong (a bad/delisted ISIN just returns null data for
# that one row via the by_key lookup below, same as NIFTY_TOP10's own
# convention), so a handful of remaining membership errors won't break the
# rest of the board -- but re-verify against nseindia.com's current Nifty
# 50 factsheet before trusting the list is complete, since that's the one
# thing this check didn't cover.
NIFTY50_EXTRA = [
    {"symbol": "SBIN", "name": "State Bank of India", "isin": "INE062A01020"},
    {"symbol": "HINDUNILVR", "name": "Hindustan Unilever", "isin": "INE030A01027"},
    {"symbol": "BAJFINANCE", "name": "Bajaj Finance", "isin": "INE296A01032"},
    {"symbol": "SUNPHARMA", "name": "Sun Pharmaceutical", "isin": "INE044A01036"},
    {"symbol": "MARUTI", "name": "Maruti Suzuki", "isin": "INE585B01010"},
    {"symbol": "M&M", "name": "Mahindra & Mahindra", "isin": "INE101A01026"},
    {"symbol": "NTPC", "name": "NTPC", "isin": "INE733E01010"},
    {"symbol": "HCLTECH", "name": "HCL Technologies", "isin": "INE860A01027"},
    {"symbol": "TITAN", "name": "Titan Company", "isin": "INE280A01028"},
    {"symbol": "TMPV", "name": "Tata Motors Passenger Vehicles", "isin": "INE155A01022"},
    {"symbol": "ULTRACEMCO", "name": "UltraTech Cement", "isin": "INE481G01011"},
    {"symbol": "POWERGRID", "name": "Power Grid Corp", "isin": "INE752E01010"},
    {"symbol": "ASIANPAINT", "name": "Asian Paints", "isin": "INE021A01026"},
    {"symbol": "BAJAJFINSV", "name": "Bajaj Finserv", "isin": "INE918I01026"},
    {"symbol": "WIPRO", "name": "Wipro", "isin": "INE075A01022"},
    {"symbol": "NESTLEIND", "name": "Nestle India", "isin": "INE239A01024"},
    {"symbol": "ADANIENT", "name": "Adani Enterprises", "isin": "INE423A01024"},
    {"symbol": "ADANIPORTS", "name": "Adani Ports & SEZ", "isin": "INE742F01042"},
    {"symbol": "JSWSTEEL", "name": "JSW Steel", "isin": "INE019A01038"},
    {"symbol": "TATASTEEL", "name": "Tata Steel", "isin": "INE081A01020"},
    {"symbol": "GRASIM", "name": "Grasim Industries", "isin": "INE047A01021"},
    {"symbol": "COALINDIA", "name": "Coal India", "isin": "INE522F01014"},
    {"symbol": "BAJAJ-AUTO", "name": "Bajaj Auto", "isin": "INE917I01010"},
    {"symbol": "TECHM", "name": "Tech Mahindra", "isin": "INE669C01036"},
    {"symbol": "HINDALCO", "name": "Hindalco Industries", "isin": "INE038A01020"},
    {"symbol": "DRREDDY", "name": "Dr Reddy's Laboratories", "isin": "INE089A01031"},
    {"symbol": "CIPLA", "name": "Cipla", "isin": "INE059A01026"},
    {"symbol": "EICHERMOT", "name": "Eicher Motors", "isin": "INE066A01021"},
    {"symbol": "BRITANNIA", "name": "Britannia Industries", "isin": "INE216A01030"},
    {"symbol": "HEROMOTOCO", "name": "Hero MotoCorp", "isin": "INE158A01026"},
    {"symbol": "APOLLOHOSP", "name": "Apollo Hospitals", "isin": "INE437A01024"},
    {"symbol": "DIVISLAB", "name": "Divi's Laboratories", "isin": "INE361B01024"},
    {"symbol": "SBILIFE", "name": "SBI Life Insurance", "isin": "INE123W01016"},
    {"symbol": "HDFCLIFE", "name": "HDFC Life Insurance", "isin": "INE795G01014"},
    {"symbol": "INDUSINDBK", "name": "IndusInd Bank", "isin": "INE095A01012"},
    {"symbol": "SHRIRAMFIN", "name": "Shriram Finance", "isin": "INE721A01047"},
    {"symbol": "TATACONSUM", "name": "Tata Consumer Products", "isin": "INE192A01025"},
    {"symbol": "ONGC", "name": "Oil & Natural Gas Corp", "isin": "INE213A01029"},
    {"symbol": "TRENT", "name": "Trent", "isin": "INE849A01020"},
    {"symbol": "LTM", "name": "LTIMindtree", "isin": "INE214T01019"},
]
# NIFTY_TOP10 entries repeated here (minus weight_pct) so the board is the
# full ~50, not 40 -- this is the single source of truth for "which 50",
# NIFTY_TOP10 stays the source of truth for "how much each of its 10 is
# weighted".
NIFTY50_ALL = [{"symbol": s["symbol"], "name": s["name"], "isin": s["isin"]} for s in NIFTY_TOP10] + NIFTY50_EXTRA


@app.get("/api/upstox/nifty50")
async def upstox_nifty50():
    """Live price action for all ~50 Nifty constituents (see NIFTY50_ALL's
    own caveat above about membership/ISIN staleness) -- a market-breadth
    board (advances/declines, sorted by %change) to complement
    /api/upstox/movers' weighted top-10 predictor, not a replacement for it.
    Same connected-session requirement and degrade-gracefully-per-row
    behavior as upstox_movers above; no NSE fallback exists for per-stock
    LTPs, so this is Upstox-only.
    """
    token = await load_upstox_token()
    if not token:
        return {
            "connected": False, "stocks": [], "advances": None, "declines": None, "unchanged": None,
            "error": "Upstox not connected — visit /api/upstox/login",
        }

    instrument_keys = ",".join(f"NSE_EQ|{s['isin']}" for s in NIFTY50_ALL)
    try:
        async with httpx.AsyncClient(timeout=15) as up_client:
            resp = await up_client.get(
                "https://api.upstox.com/v2/market-quote/quotes",
                params={"instrument_key": instrument_keys},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
    except Exception as e:
        return {
            "connected": True, "stocks": [], "advances": None, "declines": None, "unchanged": None,
            "error": f"upstox request failed: {e}",
        }

    if resp.status_code == 401:
        await clear_upstox_token()
        return {
            "connected": False, "stocks": [], "advances": None, "declines": None, "unchanged": None,
            "error": "Upstox session expired — visit /api/upstox/login again",
        }
    if resp.status_code != 200:
        return {
            "connected": True, "stocks": [], "advances": None, "declines": None, "unchanged": None,
            "error": f"upstox returned {resp.status_code}: {resp.text[:200]}",
        }

    try:
        payload = resp.json()
    except Exception as e:
        return {
            "connected": True, "stocks": [], "advances": None, "declines": None, "unchanged": None,
            "error": f"upstox returned non-JSON: {e}",
        }

    by_key = {}
    for quote in (payload.get("data") or {}).values():
        token_field = quote.get("instrument_token")
        if token_field:
            by_key[token_field] = quote

    stocks = []
    advances = declines = unchanged = 0
    for s in NIFTY50_ALL:
        q = by_key.get(f"NSE_EQ|{s['isin']}")
        ltp = q.get("last_price") if q else None
        prev_close = _prev_close_from_quote(q) if q else None
        pct_change = (ltp - prev_close) / prev_close * 100 if ltp is not None and prev_close else None
        if pct_change is not None:
            if pct_change > 0:
                advances += 1
            elif pct_change < 0:
                declines += 1
            else:
                unchanged += 1
        stocks.append({
            "symbol": s["symbol"], "name": s["name"],
            "ltp": ltp, "prev_close": prev_close, "pct_change": pct_change,
        })
    stocks.sort(key=lambda r: (r["pct_change"] is None, -(r["pct_change"] or 0)))

    return {"connected": True, "stocks": stocks, "advances": advances, "declines": declines, "unchanged": unchanged}


async def fetch_and_record_pcr(symbol: str, now: dt.datetime, persist_strikes: bool = False) -> dict:
    """Fetch a fresh PCR reading, update the in-memory cache, and append it
    to the persistent Redis history (best-effort — a Redis outage shouldn't
    break the live reading, just the history).

    persist_strikes additionally snapshots every individual strike's PCR
    from the same NSE fetch (no extra request), through the same 5-min
    Redis-backed throttle /api/optionchain/today uses — this is what the
    once-daily cron sets, mainly as a gap-filler for days nobody visits;
    real visitor traffic is the primary source of per-strike history now."""
    expiry = await get_nearest_expiry(symbol)
    oc = await fetch_option_chain_for_expiry(symbol, expiry)
    pcr = compute_pcr(oc, target_expiry=expiry)
    cached = {
        "updatedAt": now.isoformat(), "expiry": pcr["expiry"],
        "pcrOi": pcr["pcrOi"], "pcrVol": pcr["pcrVol"],
        "putOi": pcr["putOi"], "callOi": pcr["callOi"],
    }
    pcr_cache[symbol] = cached
    day = now.date().isoformat()
    t_label = now.strftime("%H:%M")
    await push_pcr_snapshot(symbol, day, {
        "t": t_label, "pcrOi": pcr["pcrOi"], "pcrVol": pcr["pcrVol"],
        "putOi": pcr["putOi"], "callOi": pcr["callOi"],
    })

    if persist_strikes and await claim_strike_persist_slot(symbol, day, now):
        try:
            chain = build_chain_rows(oc, target_expiry=expiry)
            for row in chain["rows"]:
                strike_key = int(round(row["strike"]))
                strike_pcr = (row["peOi"] / row["ceOi"]) if row["ceOi"] else None
                await push_strike_pcr_snapshot(symbol, strike_key, day, {
                    "t": t_label, "pcr": strike_pcr, "ceOi": row["ceOi"], "peOi": row["peOi"],
                })
        except Exception as e:
            print(f"per-strike pcr persist failed for {symbol}: {e}")

    return cached


@app.get("/api/pcr/today")
async def pcr_today(symbol: str = Query("NIFTY")):
    """Current PCR reading, refreshed on-demand (or reused briefly on a warm
    invocation). Each fresh fetch also gets appended to Redis — see
    /api/pcr/history for the persisted full-day time series."""
    symbol = symbol.upper()
    now = dt.datetime.now(IST)
    cached = pcr_cache.get(symbol)
    stale = True
    if cached and cached.get("updatedAt"):
        age = (now - dt.datetime.fromisoformat(cached["updatedAt"])).total_seconds()
        stale = age > ON_DEMAND_MAX_AGE

    if stale:
        try:
            cached = await fetch_and_record_pcr(symbol, now)
        except Exception as e:
            print(f"[{now:%H:%M:%S}] {symbol} pcr fetch failed: {e}")
            if cached is None:
                cached = {"updatedAt": None, "expiry": "", "pcrOi": None, "pcrVol": None, "putOi": None, "callOi": None}

    return {"symbol": symbol, **cached}


@app.get("/api/pcr/history")
async def pcr_history(symbol: str = Query("NIFTY")):
    """The current trading day's persisted PCR readings, so any visitor sees
    the full day rather than just what accumulated since they opened the tab."""
    symbol = symbol.upper()
    day = dt.datetime.now(IST).date().isoformat()
    snapshots = await get_pcr_history(symbol, day)
    return {"symbol": symbol, "date": day, "snapshots": snapshots}


@app.get("/api/optionchain/history")
async def optionchain_history(symbol: str = Query("NIFTY"), strike: float = Query(...)):
    """A single strike's PCR (put OI / call OI) over the current trading
    day. Points come from two sources: the once-daily cron poll (guarantees
    at least one point near market open) and any visitor traffic (each
    /api/pcr/today fetch persists too) — only strikes that were within the
    top-N-near-spot window at that moment have data."""
    symbol = symbol.upper()
    day = dt.datetime.now(IST).date().isoformat()
    strike_key = int(round(strike))
    snapshots = await get_strike_pcr_history(symbol, strike_key, day)
    return {"symbol": symbol, "strike": strike_key, "date": day, "snapshots": snapshots}


@app.get("/api/optionchain/history-sheet")
async def optionchain_history_sheet(symbol: str = Query("NIFTY"), strikes: str = Query(...)):
    """Every-5-min PCR history for several strikes at once (comma-separated,
    e.g. the currently-displayed option-chain rows) — powers the
    spreadsheet-style time x strike view instead of one chart per strike."""
    symbol = symbol.upper()
    day = dt.datetime.now(IST).date().isoformat()
    strike_keys = []
    for raw in strikes.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            strike_keys.append(int(round(float(raw))))
        except ValueError:
            continue

    result = {}
    for strike_key in strike_keys:
        result[str(strike_key)] = await get_strike_pcr_history(symbol, strike_key, day)
    return {"symbol": symbol, "date": day, "strikes": result}


@app.get("/api/cron/poll")
async def cron_poll():
    """Vercel Cron target — runs once/day shortly after market open (Hobby
    plan's cron limits don't allow more often; see vercel.json's "crons"
    entry) so there's always at least one data point even if nobody visits
    the page all day. Finer intraday granularity comes from visitor traffic:
    every /api/pcr/today fetch persists a reading too."""
    now = dt.datetime.now(IST)
    results = {}
    for sym in SYMBOLS:
        try:
            await fetch_and_record_pcr(sym, now, persist_strikes=True)
            results[sym] = "ok"
        except Exception as e:
            results[sym] = f"failed: {e}"
    return {"ok": True, "updatedAt": now.isoformat(), "results": results}


@app.get("/api/expiries")
async def expiries(symbol: str = Query("NIFTY")):
    symbol = symbol.upper()
    try:
        exp_list = await get_all_expiries(symbol)
    except Exception as e:
        exp_list = []
        print(f"expiries fetch failed for {symbol}: {e}")
    return {"symbol": symbol, "expiries": exp_list}


@app.get("/api/optionchain/today")
async def optionchain_today(symbol: str = Query("NIFTY"), n: int = Query(CHAIN_TOP_N),
                             expiry: str = Query(None)):
    symbol = symbol.upper()
    now = dt.datetime.now(IST)

    if not expiry:
        expiry = await get_nearest_expiry(symbol)

    cached = chain_store.setdefault(symbol, {}).get(expiry)
    stale = True
    if cached and cached.get("updatedAt"):
        age = (now - dt.datetime.fromisoformat(cached["updatedAt"])).total_seconds()
        stale = age > ON_DEMAND_MAX_AGE

    if stale:
        try:
            oc = await fetch_option_chain_for_expiry(symbol, expiry)
            chain = build_chain_rows(oc, target_expiry=expiry)
            cached = {"updatedAt": now.isoformat(), "spot": chain["spot"], "rows": chain["rows"]}
            chain_store[symbol][expiry] = cached
        except Exception as e:
            print(f"[{now:%H:%M:%S}] {symbol} on-demand chain fetch failed ({expiry}): {e}")
            if cached is None:
                cached = {"updatedAt": None, "spot": None, "rows": []}

    # Per-strike PCR history is driven by real visitor traffic, not cron —
    # this is what actually gives a proper ~5-min series starting from
    # whenever the page is first opened each day. Throttled via Redis (not
    # a plain "if stale" check) so it holds to ~5 min regardless of how
    # often this endpoint gets polled. Only the nearest expiry is recorded,
    # so browsing a later expiry doesn't mix into the same strike's history.
    if cached.get("rows"):
        try:
            nearest = await get_nearest_expiry(symbol)
            if expiry == nearest:
                day = now.date().isoformat()
                if await claim_strike_persist_slot(symbol, day, now):
                    t_label = now.strftime("%H:%M")
                    for row in cached["rows"]:
                        strike_key = int(round(row["strike"]))
                        strike_pcr = (row["peOi"] / row["ceOi"]) if row["ceOi"] else None
                        await push_strike_pcr_snapshot(symbol, strike_key, day, {
                            "t": t_label, "pcr": strike_pcr, "ceOi": row["ceOi"], "peOi": row["peOi"],
                        })
        except Exception as e:
            print(f"per-strike pcr persist failed for {symbol}: {e}")

    return {
        "symbol": symbol,
        "expiry": expiry,
        "spot": cached.get("spot"),
        "updatedAt": cached.get("updatedAt"),
        "rows": (cached.get("rows") or [])[:n],
    }


@app.get("/api/candles")
async def candles(symbol: str = Query("NIFTY"), interval: str = Query("5m"),
                   rng: str = Query("1d", alias="range")):
    """Historical intraday OHLC candles, proxied from Yahoo Finance's free
    public chart API — unaffected by the NSE-blocks-cloud-IPs risk below."""
    symbol = symbol.upper()
    yf_symbol = YF_SYMBOL.get(symbol)
    if not yf_symbol:
        return {"symbol": symbol, "candles": []}
    try:
        if interval == "4h":
            rows = await fetch_yahoo_candles(yf_symbol, "60m", rng)
            rows = resample_candles(rows, bucket_hours=4)
        else:
            rows = await fetch_yahoo_candles(yf_symbol, interval, rng)

        multi_day = rng != "1d"
        out = [{
            "t": row["dt"].strftime("%d-%b %H:%M") if multi_day else row["dt"].strftime("%H:%M"),
            "open": round(row["open"], 2), "high": round(row["high"], 2),
            "low": round(row["low"], 2), "close": round(row["close"], 2),
        } for row in rows]
        return {"symbol": symbol, "candles": out}
    except Exception as e:
        print(f"candles fetch failed for {symbol}: {e}")
        return {"symbol": symbol, "candles": []}


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "chain": {s: sum(len(v.get("rows") or []) for v in chain_store.get(s, {}).values()) for s in SYMBOLS},
        "pcrCached": {s: s in pcr_cache for s in SYMBOLS},
    }
