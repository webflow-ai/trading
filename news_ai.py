"""
news_ai.py — RSS headline pull + Gemini Flash sentiment classification.
Module 8 of the build order (docs/PREMARKET_ENGINE.md), built last by
design: everything else already works without this — a failed fetch or a
failed Gemini call degrades to a neutral fallback rather than breaking the
morning job.

⚠️ RSS_FEEDS is unverified — built without network access to confirm these
feeds are still live. Reuters in particular discontinued most of its public
RSS feeds years ago; the URL below is a best guess and may 404. Check each
site's current /rss or /feeds listing and swap in whatever's actually live
before trusting this. A dead feed just contributes zero headlines (see
fetch_rss_headlines), it won't break anything else.

Verified live 2026-08-11 (real GEMINI_API_KEY, real headlines): the original
GEMINI_MODEL default, "gemini-2.0-flash", is retired (404). Switched to
"gemini-flash-latest" — an alias Google maintains to always point at their
current recommended Flash model, chosen specifically so this doesn't need
fixing again the next time a dated model id gets retired.

⚠️ Also surfaced by that same test: the `google-generativeai` package this
module depends on is now fully deprecated upstream ("All support ... has
ended ... switch to the `google.genai` package" — printed as a
FutureWarning on every import). It still works as of the test above, but a
migration to `google-genai` is a real, not-yet-scheduled follow-up.

Verified live 2026-08-11 (again): reuters_business 301-redirects
www.reutersagency.com -> reutersagency.com, but the redirect target itself
404s when hit directly (query-string/referrer handling on Reuters' side,
not something worth reverse-engineering) — the *redirect chain* works, a
hardcoded guess at its destination doesn't. Left the original www URL and
turned on follow_redirects so httpx does the actual redirect instead of us
guessing it.

Verified live 2026-08-11 (a third time): production round-trips for this
module were taking 20+ seconds, most of it not the RSS/network calls above
but `google-generativeai` itself — a ~50MB dependency tree (grpcio,
google-api-python-client, cryptography, protobuf, ...) that Vercel's Python
runtime reinstalls from scratch on every cold invocation, for what is, at
its core, one JSON-in/JSON-out HTTP call. Replaced with a direct httpx POST
to Gemini's REST API — matches every other integration in this codebase
(Supabase, Telegram, NSE, Yahoo all use raw httpx, no SDKs) and removes the
dependency entirely.
"""

import asyncio
import datetime as dt
import email.utils
import json
import os
import xml.etree.ElementTree as ET

import httpx

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")

RSS_FEEDS = {
    "reuters_business": "https://www.reutersagency.com/feed/?best-topics=business-finance",
    "moneycontrol": "https://www.moneycontrol.com/rss/marketreports.xml",
    "et_markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
}

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

HEADLINE_WINDOW_HOURS = 12
MAX_HEADLINES = 25

# How many headlines actually get shown/stored on the brief, out of the up-
# to-25 fetched — the fetch casts a wide net, but a dashboard user wants the
# handful that could actually move the market, not a full RSS dump.
TOP_NEWS_COUNT = 4
IMPACT_RANK = {"high": 3, "medium": 2, "low": 1}

PROMPT_TEMPLATE = """You are a financial news classifier for Indian equity markets (Nifty 50).
For each headline below, classify its likely near-term impact on the Nifty index as
exactly one of: bullish, bearish, neutral, AND how large that impact is likely to be
(high, medium, low — most headlines are low; reserve "high" for genuinely
market-moving news like a rate decision, a major geopolitical shock, or a shift in
FII flows, not routine single-stock earnings). Give a one-line reason for each.
Then give one overall one-line sentiment summary across all headlines for this morning.

Respond with ONLY valid JSON, no markdown code fences, in exactly this shape:
{{"items": [{{"headline": "...", "sentiment": "bullish|bearish|neutral", "impact": "high|medium|low", "reason": "..."}}], "overall_sentiment": "..."}}

Headlines:
{headlines_block}
"""


# ---------------- RSS ----------------

def _parse_rss_items(xml_text: str, source: str) -> list[dict]:
    """Isolated from the network call so it's testable against a fixture.
    Handles plain RSS 2.0 (<rss><channel><item>...>) — anything that doesn't
    parse as XML, or has no <item> elements (e.g. an Atom feed), just comes
    back empty rather than raising."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items = []
    for item in root.findall(".//item"):
        title_el = item.find("title")
        if title_el is None or not (title_el.text or "").strip():
            continue
        pubdate_el = item.find("pubDate")
        published = None
        if pubdate_el is not None and pubdate_el.text:
            try:
                published = email.utils.parsedate_to_datetime(pubdate_el.text.strip())
                if published.tzinfo is None:
                    published = published.replace(tzinfo=dt.timezone.utc)
            except (TypeError, ValueError):
                published = None
        items.append({"headline": title_el.text.strip(), "published": published, "source": source})
    return items


async def fetch_rss_headlines(client: httpx.AsyncClient, url: str, source: str) -> list[dict]:
    try:
        resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        return _parse_rss_items(resp.text, source)
    except Exception as e:
        print(f"news_ai: RSS fetch failed for {source} ({url}): {e}")
        return []


def _within_window(items: list[dict], now: dt.datetime, hours: int) -> list[dict]:
    cutoff = now - dt.timedelta(hours=hours)
    out = []
    for it in items:
        # No timestamp -> can't tell if it's stale, so it's kept rather than
        # silently dropped (a missing pubDate shouldn't cost a headline).
        if it["published"] is None or it["published"] >= cutoff:
            out.append(it)
    return out


def _dedupe(items: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for it in items:
        key = it["headline"].strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


async def fetch_all_headlines(now: dt.datetime | None = None) -> list[dict]:
    """Recent headlines (last HEADLINE_WINDOW_HOURS) across all RSS_FEEDS,
    deduplicated, newest first, capped at MAX_HEADLINES. One dead feed never
    blocks the others — see fetch_rss_headlines."""
    now = now or dt.datetime.now(dt.timezone.utc)
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        groups = await asyncio.gather(
            *(fetch_rss_headlines(client, url, source) for source, url in RSS_FEEDS.items())
        )
    all_items = [it for group in groups for it in group]
    recent = _within_window(all_items, now, HEADLINE_WINDOW_HOURS)
    recent.sort(key=lambda it: it["published"] or now, reverse=True)
    return _dedupe(recent)[:MAX_HEADLINES]


# ---------------- Gemini classification ----------------

def _neutral_fallback(headlines: list[str], note: str) -> dict:
    return {
        "items": [{"headline": h, "sentiment": "neutral", "impact": "low", "reason": "unavailable"} for h in headlines],
        "overall_sentiment": "Sentiment unavailable.",
        "note": note,
    }


def select_top_market_moving(items: list[dict], n: int = TOP_NEWS_COUNT) -> list[dict]:
    """The n headlines most likely to actually move the market — ranked by
    impact (high > medium > low > unrecognized), then non-neutral sentiment
    over neutral. Isolated from classify_headlines so it's testable without
    a Gemini call, and reusable if the ranking policy ever needs tuning."""
    def rank(item: dict) -> tuple:
        impact_rank = IMPACT_RANK.get(str(item.get("impact", "")).lower(), 0)
        directional = 1 if item.get("sentiment") in ("bullish", "bearish") else 0
        return (impact_rank, directional)

    return sorted(items, key=rank, reverse=True)[:n]


def _build_prompt(headlines: list[str]) -> str:
    block = "\n".join(f"{i + 1}. {h}" for i, h in enumerate(headlines))
    return PROMPT_TEMPLATE.format(headlines_block=block)


def _parse_gemini_response(text: str) -> dict:
    """Isolated from the network call so it's unit-testable. Gemini
    sometimes wraps JSON in ```json fences despite being told not to —
    stripped defensively before parsing."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    data = json.loads(cleaned)
    return {
        "items": data.get("items", []),
        "overall_sentiment": data.get("overall_sentiment", ""),
        "note": None,
    }


async def classify_headlines(headlines: list[str], client: httpx.AsyncClient | None = None) -> dict:
    """{"items": [{"headline", "sentiment", "reason"}, ...],
    "overall_sentiment": "<one-line>", "note": str|None}. Falls back to an
    all-neutral result (never raises) if Gemini isn't configured or the call
    fails, per the brief's own instruction.

    Raw REST (POST .../models/{model}:generateContent?key=...), not the
    google-generativeai SDK — see this module's docstring for why."""
    if not headlines:
        return {"items": [], "overall_sentiment": "No headlines available.", "note": None}
    if not GEMINI_API_KEY:
        print("news_ai: GEMINI_API_KEY not set, returning neutral fallback")
        return _neutral_fallback(headlines, note="GEMINI_API_KEY not configured")
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=30)
    try:
        resp = await client.post(
            GEMINI_API_URL.format(model=GEMINI_MODEL),
            params={"key": GEMINI_API_KEY},
            json={"contents": [{"parts": [{"text": _build_prompt(headlines)}]}]},
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return _parse_gemini_response(text)
    except Exception as e:
        print(f"news_ai: Gemini classification failed: {e}")
        return _neutral_fallback(headlines, note=f"Gemini call failed: {e}")
    finally:
        if owns_client:
            await client.aclose()


async def get_news_brief(now: dt.datetime | None = None) -> dict:
    """headlines + classification, ready to drop into the morning brief:
    {"headlines": [...], "news_sentiment": "<one-line>"}. `headlines` is
    trimmed to TOP_NEWS_COUNT — up to MAX_HEADLINES are fetched and
    classified for a fuller read on `overall_sentiment`, but only the
    handful most likely to actually move the market are kept for display."""
    items = await fetch_all_headlines(now=now)
    classified = await classify_headlines([it["headline"] for it in items])
    return {
        "headlines": select_top_market_moving(classified["items"]),
        "news_sentiment": classified["overall_sentiment"],
    }
