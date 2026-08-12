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
import datetime as dt

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
            expiry = _nse_expiry_to_iso(await get_nearest_expiry(symbol))
        except Exception as e:
            return {"connected": True, "spot": None, "rows": [], "error": f"couldn't resolve nearest expiry: {e}"}

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
            "ceOiChg": call_md.get("oi_change") or call_md.get("oi_day_change"),
            "ceVol": call_md.get("volume"),
            "ceLtp": call_md.get("ltp"),
            "peOi": put_md.get("oi"),
            "peOiChg": put_md.get("oi_change") or put_md.get("oi_day_change"),
            "peVol": put_md.get("volume"),
            "peLtp": put_md.get("ltp"),
        })
    rows.sort(key=lambda r: r["strike"] if r["strike"] is not None else 0)
    return {"connected": True, "symbol": symbol, "expiry": expiry, "spot": spot, "rows": rows}


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
