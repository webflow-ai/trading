"""
PCR + option chain API — Vercel Python serverless version
-----------------------------------------------------------
Everything is fetched on-demand per request (with a short in-memory reuse
window that only helps on "warm" invocations — Vercel doesn't guarantee
warm reuse between requests, unlike a normal long-running server).

There is intentionally no background scheduler here: serverless functions
only run in response to a request, so nothing can poll NSE on a timer the
way the original local backend.py did. PCR history is no longer accumulated
server-side either — /api/pcr/today returns just the current reading, and
the frontend builds its own local time series from repeated polls (the same
pattern the option-chain candle chart already uses).

⚠️ NSE gotcha (same one from backend.py, now higher-stakes): NSE actively
blocks datacenter/cloud IPs. This code primes cookies + sends browser-like
headers, which is what makes it work from a normal residential machine —
whether that survives Vercel's own IP ranges is untested. If every NSE-backed
route starts failing after deploy, that's almost certainly why; the
Yahoo-Finance-backed /api/candles route doesn't have this problem.
"""

import os
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

_client: httpx.AsyncClient | None = None


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
    return {
        "configured": bool(upstox_config["api_key"] and upstox_config["api_secret"]),
        "connected": bool(upstox_token["access_token"]),
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
        upstox_token["access_token"] = j.get("access_token")
        upstox_token["obtained_at"] = dt.datetime.now(IST).isoformat()
        return "<h2>&#9989; Upstox connected. You can close this tab.</h2>"
    except Exception as e:
        return f"<h3>Token exchange failed: {e}</h3>"


@app.get("/api/pcr/today")
async def pcr_today(symbol: str = Query("NIFTY")):
    """Current PCR reading only — no server-side history. Fetched fresh (or
    reused briefly on a warm invocation); the frontend accumulates its own
    time series from repeated polls, same as the candle chart already does."""
    symbol = symbol.upper()
    now = dt.datetime.now(IST)
    cached = pcr_cache.get(symbol)
    stale = True
    if cached and cached.get("updatedAt"):
        age = (now - dt.datetime.fromisoformat(cached["updatedAt"])).total_seconds()
        stale = age > ON_DEMAND_MAX_AGE

    if stale:
        try:
            expiry = await get_nearest_expiry(symbol)
            oc = await fetch_option_chain_for_expiry(symbol, expiry)
            pcr = compute_pcr(oc, target_expiry=expiry)
            cached = {
                "updatedAt": now.isoformat(), "expiry": pcr["expiry"],
                "pcrOi": pcr["pcrOi"], "pcrVol": pcr["pcrVol"],
                "putOi": pcr["putOi"], "callOi": pcr["callOi"],
            }
            pcr_cache[symbol] = cached
        except Exception as e:
            print(f"[{now:%H:%M:%S}] {symbol} pcr fetch failed: {e}")
            if cached is None:
                cached = {"updatedAt": None, "expiry": "", "pcrOi": None, "pcrVol": None, "putOi": None, "callOi": None}

    return {"symbol": symbol, **cached}


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
