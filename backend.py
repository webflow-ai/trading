"""
PCR intraday collector + API  (for the "PCR Session Clock" webapp)
------------------------------------------------------------------
- Pulls the NSE option chain every N minutes during market hours (IST)
- Computes PCR (OI) and PCR (Volume) for the nearest expiry
- Stores snapshots for the current day (in-memory, + optional Supabase)
- Serves GET /pcr/today?symbol=NIFTY  to the frontend

Run:
    pip install fastapi uvicorn httpx apscheduler
    uvicorn pcr_backend:app --host 0.0.0.0 --port 8000

Then in the webapp's "⚙ Source" panel, set:  http://localhost:8000
(or your deployed URL).

⚠️  IMPORTANT — the NSE gotcha:
NSE blocks datacenter / cloud IPs and bare HTTP clients. This works from a
normal machine (home / office / an India VPS) because it (1) primes cookies by
hitting the option-chain page first, (2) sends browser-like headers, and
(3) re-primes + retries on 401/403. If you deploy to a cloud box and start
getting 401s, route the outbound calls through a residential/India proxy,
or run the collector on a small always-on box you control and only host the
API in the cloud.
"""

import os
import asyncio
import datetime as dt
from collections import defaultdict

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Secrets (Upstox API key/secret, etc.) live in a local .env file next to this
# script — never hardcoded, never pasted into chat, never committed anywhere.
load_dotenv()

# ---------------- config ----------------
SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY"]
POLL_MINUTES = int(os.getenv("POLL_MINUTES", "3"))
# NSE fronts this endpoint with an Akamai CDN cache that only refreshes the
# underlying data ~every 10-15s (confirmed via `server-timing: cdn-cache`).
# Polling faster than that just re-fetches the same cached snapshot, so this
# is set to match reality rather than hammer NSE for nothing.
CHAIN_POLL_SECONDS = int(os.getenv("CHAIN_POLL_SECONDS", "3"))
CHAIN_TOP_N = int(os.getenv("CHAIN_TOP_N", "10"))
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# Yahoo Finance's public chart API — free, unauthenticated, gives real
# historical intraday OHLC candles (NSE itself doesn't expose this for free).
YF_SYMBOL = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", "FINNIFTY": "NIFTY_FIN_SERVICE.NS"}

BASE = "https://www.nseindia.com"
# NSE retired /api/option-chain-indices; the live frontend now calls this v3
# endpoint with an explicit expiry date, backed by /api/option-chain-contract-info
# for the list of available expiries.
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

# optional persistence
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

# ---------------- Upstox (optional real-time data source) ----------------
# API key/secret are read from a local .env file (UPSTOX_API_KEY /
# UPSTOX_API_SECRET) so they never have to be pasted into chat or committed
# anywhere. POST /upstox/configure remains as a fallback if you'd rather set
# them at runtime instead.
UPSTOX_REDIRECT_URI = os.getenv("UPSTOX_REDIRECT_URI", "http://localhost:8000/upstox/callback")
upstox_config: dict = {
    "api_key": os.getenv("UPSTOX_API_KEY"),
    "api_secret": os.getenv("UPSTOX_API_SECRET"),
}
# A manually-generated token from the Upstox developer dashboard (if set)
# skips the OAuth login/callback flow entirely.
upstox_token: dict = {
    "access_token": os.getenv("UPSTOX_ACCESS_TOKEN") or None,
    "obtained_at": dt.datetime.now(IST).isoformat() if os.getenv("UPSTOX_ACCESS_TOKEN") else None,
}

# ---------------- in-memory store ----------------
# store[symbol] = {"date": "YYYY-MM-DD", "expiry": str, "snapshots": [ {...} ]}
store: dict = defaultdict(lambda: {"date": None, "expiry": "", "snapshots": []})

# chain_store[symbol][expiry] = {"updatedAt": iso str, "spot": float, "rows": [ {...} ]}
# rows are the CHAIN_TOP_N strikes nearest the spot price, ascending by strike.
# The background job keeps only the *nearest* expiry continuously live; any
# other expiry the frontend asks for is fetched on-demand and cached briefly.
chain_store: dict = defaultdict(dict)

_client: httpx.AsyncClient | None = None

# expiry_cache[symbol] = {"date": "YYYY-MM-DD", "expiries": ["11-Aug-2026", "18-Aug-2026", ...]}
# The expiry list rarely changes intraday, so this is refreshed once per
# trading day instead of on every poll (avoids hammering NSE with an extra
# request every few seconds).
expiry_cache: dict = {}
CHAIN_ON_DEMAND_MAX_AGE = CHAIN_POLL_SECONDS  # how long a non-default expiry's cache is reused before re-fetching


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
            # cookies went stale — re-prime and retry once
            await client.get(PRIME_URL)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return {}


async def fetch_option_chain(symbol: str) -> dict:
    expiry = await get_nearest_expiry(symbol)
    return await fetch_option_chain_for_expiry(symbol, expiry)


def compute_pcr(oc: dict) -> dict:
    records = oc.get("records", {})
    expiries = records.get("expiryDates") or []
    expiry = expiries[0] if expiries else ""
    put_oi = call_oi = put_vol = call_vol = 0
    for row in records.get("data", []):
        # NSE's v3 payload keys each row's expiry as "expiryDates" (plural).
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


async def poll_chain_once(force: bool = False):
    """Keeps the nearest expiry's chain continuously live for every symbol.
    Other expiries are fetched on-demand (see /optionchain/today)."""
    now = dt.datetime.now(IST)
    if not force and not market_open(now):
        return
    for sym in SYMBOLS:
        try:
            nearest = await get_nearest_expiry(sym)
            oc = await fetch_option_chain_for_expiry(sym, nearest)
            chain = build_chain_rows(oc, target_expiry=nearest)
            chain_store[sym][nearest] = {
                "updatedAt": now.isoformat(),
                "spot": chain["spot"],
                "rows": chain["rows"],
            }
        except Exception as e:
            print(f"[{now:%H:%M:%S}] {sym} chain fetch failed: {e}")


def market_open(now: dt.datetime) -> bool:
    if now.weekday() >= 5:  # Sat/Sun
        return False
    t = now.time()
    return dt.time(9, 15) <= t <= dt.time(15, 30)


async def poll_once(force: bool = False):
    now = dt.datetime.now(IST)
    if not force and not market_open(now):
        return
    today = now.date().isoformat()
    for sym in SYMBOLS:
        try:
            oc = await fetch_option_chain(sym)
            pcr = compute_pcr(oc)
            bucket = store[sym]
            if bucket["date"] != today:          # new trading day → reset
                bucket["date"], bucket["snapshots"] = today, []
            bucket["expiry"] = pcr["expiry"]
            snap = {
                "t": now.strftime("%H:%M"),
                "pcrOi": pcr["pcrOi"], "pcrVol": pcr["pcrVol"],
                "putOi": pcr["putOi"], "callOi": pcr["callOi"],
            }
            bucket["snapshots"].append(snap)
            await maybe_persist(sym, today, snap, pcr["expiry"])
            print(f"[{now:%H:%M}] {sym} PCR(OI)={pcr['pcrOi']} PCR(Vol)={pcr['pcrVol']}")
        except Exception as e:
            print(f"[{now:%H:%M}] {sym} fetch failed: {e}")
        await asyncio.sleep(1)  # be gentle between symbols


async def maybe_persist(symbol, day, snap, expiry):
    """Optional: upsert into Supabase table `pcr_snapshots` if env is set.
       CREATE TABLE pcr_snapshots (
         id bigint generated always as identity primary key,
         symbol text, trade_date date, t text, expiry text,
         pcr_oi numeric, pcr_vol numeric, put_oi bigint, call_oi bigint,
         created_at timestamptz default now()
       );
    """
    if not (SUPABASE_URL and SUPABASE_KEY):
        return
    try:
        client = await get_client()
        await client.post(
            f"{SUPABASE_URL}/rest/v1/pcr_snapshots",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                     "Content-Type": "application/json"},
            json={"symbol": symbol, "trade_date": day, "t": snap["t"], "expiry": expiry,
                  "pcr_oi": snap["pcrOi"], "pcr_vol": snap["pcrVol"],
                  "put_oi": snap["putOi"], "call_oi": snap["callOi"]},
        )
    except Exception as e:
        print("supabase persist failed:", e)


# ---------------- API ----------------
app = FastAPI(title="PCR Session Clock API")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    sched = AsyncIOScheduler(timezone=IST)
    sched.add_job(poll_once, "interval", minutes=POLL_MINUTES, next_run_time=dt.datetime.now(IST))
    sched.add_job(poll_chain_once, "interval", seconds=CHAIN_POLL_SECONDS, next_run_time=dt.datetime.now(IST))
    sched.start()


@app.post("/upstox/configure")
async def upstox_configure(payload: dict):
    """Store the developer app's API key/secret in memory (not on disk)."""
    upstox_config["api_key"] = payload.get("api_key")
    upstox_config["api_secret"] = payload.get("api_secret")
    return {"ok": True, "configured": bool(upstox_config["api_key"] and upstox_config["api_secret"])}


@app.get("/upstox/status")
async def upstox_status():
    return {
        "configured": bool(upstox_config["api_key"] and upstox_config["api_secret"]),
        "connected": bool(upstox_token["access_token"]),
        "obtainedAt": upstox_token["obtained_at"],
    }


@app.get("/upstox/login")
async def upstox_login():
    if not upstox_config["api_key"]:
        return {"error": "Call POST /upstox/configure with api_key + api_secret first"}
    url = (
        "https://api.upstox.com/v2/login/authorization/dialog"
        f"?client_id={upstox_config['api_key']}&redirect_uri={UPSTOX_REDIRECT_URI}&response_type=code"
    )
    return {"login_url": url}


@app.get("/upstox/callback", response_class=HTMLResponse)
async def upstox_callback(code: str = Query(None), error: str = Query(None)):
    if error:
        return f"<h3>Upstox login error: {error}</h3>"
    if not code:
        return "<h3>No authorization code received.</h3>"
    if not (upstox_config["api_key"] and upstox_config["api_secret"]):
        return "<h3>Backend isn't configured — call POST /upstox/configure first, then retry login.</h3>"
    try:
        # Dedicated client (not the shared NSE-primed one) so no unrelated
        # cookies/headers leak into this request.
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
            print(f"upstox token exchange failed: {resp.status_code} {resp.text}")
            return f"<h3>Token exchange failed: {resp.status_code}</h3><pre>{resp.text}</pre>"
        j = resp.json()
        upstox_token["access_token"] = j.get("access_token")
        upstox_token["obtained_at"] = dt.datetime.now(IST).isoformat()
        return "<h2>&#9989; Upstox connected. You can close this tab.</h2>"
    except Exception as e:
        print(f"upstox token exchange exception: {e}")
        return f"<h3>Token exchange failed: {e}</h3>"


@app.get("/pcr/today")
async def pcr_today(symbol: str = Query("NIFTY")):
    symbol = symbol.upper()
    bucket = store.get(symbol, {"expiry": "", "snapshots": []})
    return {
        "symbol": symbol,
        "expiry": bucket.get("expiry", ""),
        "updatedAt": dt.datetime.now(IST).isoformat(),
        "snapshots": bucket.get("snapshots", []),
    }


@app.get("/expiries")
async def expiries(symbol: str = Query("NIFTY")):
    symbol = symbol.upper()
    try:
        exp_list = await get_all_expiries(symbol)
    except Exception as e:
        exp_list = []
        print(f"expiries fetch failed for {symbol}: {e}")
    return {"symbol": symbol, "expiries": exp_list}


@app.get("/optionchain/today")
async def optionchain_today(symbol: str = Query("NIFTY"), n: int = Query(CHAIN_TOP_N),
                             expiry: str = Query(None)):
    symbol = symbol.upper()
    now = dt.datetime.now(IST)

    if not expiry:
        expiry = await get_nearest_expiry(symbol)

    cached = chain_store[symbol].get(expiry)
    stale = True
    if cached and cached.get("updatedAt"):
        age = (now - dt.datetime.fromisoformat(cached["updatedAt"])).total_seconds()
        stale = age > CHAIN_ON_DEMAND_MAX_AGE

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

    return {
        "symbol": symbol,
        "expiry": expiry,
        "spot": cached.get("spot"),
        "updatedAt": cached.get("updatedAt"),
        "rows": (cached.get("rows") or [])[:n],
    }


async def fetch_yahoo_candles(yf_symbol: str, interval: str, rng: str) -> list:
    """Raw OHLC rows with real datetimes (IST), no formatting yet."""
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
            continue  # Yahoo leaves gaps for pre/post-market or halts
        rows.append({"dt": dt.datetime.fromtimestamp(ts, IST), "open": o, "high": h, "low": l, "close": c})
    return rows


def resample_candles(rows: list, bucket_hours: int) -> list:
    """Yahoo has no native 4h interval, so this merges consecutive hourly
    candles into bucket_hours-wide bars, grouped per calendar day so buckets
    never span across days."""
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


@app.get("/candles")
async def candles(symbol: str = Query("NIFTY"), interval: str = Query("5m"),
                   rng: str = Query("1d", alias="range")):
    """Historical intraday OHLC candles, proxied from Yahoo Finance's free
    public chart API (NSE itself has no free intraday candle endpoint)."""
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


@app.get("/health")
async def health():
    return {
        "ok": True,
        "symbols": {s: len(store[s]["snapshots"]) for s in SYMBOLS},
        "chain": {s: sum(len(v.get("rows") or []) for v in chain_store[s].values()) for s in SYMBOLS},
    }


@app.post("/poll")
async def manual_poll():
    """Trigger one fetch now (handy for testing outside market hours)."""
    await poll_once(force=True)
    await poll_chain_once(force=True)
    return {"ok": True}