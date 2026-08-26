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

Verified live 2026-08-14: Gemini returned a flat 503 ("This model is
currently experiencing high demand") — transient and Google-side, not a
bug here, but it meant that day's brief fell all the way to the neutral
fallback with no real classification at all. Added classify_headlines's
OpenRouter fallback (openrouter.ai, free tier, no card required) for
exactly this case — see OPENROUTER_API_KEY/_call_openrouter below.
"""

import asyncio
import datetime as dt
import email.utils
import json
import os
import time
import xml.etree.ElementTree as ET

import httpx

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")

# Free-tier fallback for when Gemini errors or times out (e.g. the 503
# "model experiencing high demand" seen live 2026-08-14) -- openrouter.ai,
# no card/purchase required. Usage here is ~2 calls/day (evening + morning
# job), far under even the no-purchase free tier's 50/day cap, so this
# genuinely costs nothing. gpt-oss-20b:free chosen after live-testing a few
# free models: solid instruction-following, and (unlike some Gemini-backed
# free models on OpenRouter, which share Google AI Studio's free quota and
# were rate-limited on the same day Gemini itself was struggling) backed by
# a different upstream provider, so it isn't correlated with Gemini's own
# outages.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-oss-20b:free")
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

RSS_FEEDS = {
    "reuters_business": "https://www.reutersagency.com/feed/?best-topics=business-finance",
    "moneycontrol": "https://www.moneycontrol.com/rss/marketreports.xml",
    "et_markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
}

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

HEADLINE_WINDOW_HOURS = 12
# Verified live 2026-08-11: classify_headlines (one Gemini call, all fetched
# headlines at once) was the actual bottleneck behind the 20s+ response
# times, not the RSS/network calls -- 17.5s of a 21s total, live-measured.
# Generation time scales with output volume, and only TOP_NEWS_COUNT of
# these are ever shown, so asking Gemini to fully classify+reason about 25
# headlines to keep 4 was mostly wasted output. Cut to 12 (still several
# hours of real headline volume across 3 feeds) plus a shorter per-item
# reason below -- the two together are what actually bring this down.
MAX_HEADLINES = 12

# How many headlines actually get shown/stored on the brief, out of the up-
# to-12 fetched — the fetch casts a wide net, but a dashboard user wants the
# handful that could actually move the market, not a full RSS dump.
TOP_NEWS_COUNT = 4
IMPACT_RANK = {"high": 3, "medium": 2, "low": 1}

PROMPT_TEMPLATE = """You are a financial news classifier for Indian equity markets (Nifty 50).
For each headline below, classify its likely near-term impact on the Nifty index as
exactly one of: bullish, bearish, neutral, AND how large that impact is likely to be
(high, medium, low — most headlines are low; reserve "high" for genuinely
market-moving news like a rate decision, a major geopolitical shock, or a shift in
FII flows, not routine single-stock earnings). Give a reason for each in 8 words or fewer.
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
        items.append({
            "headline": title_el.text.strip(),
            "published": published,
            "source": source,
            "link": (item.findtext("link") or "").strip() or None,
        })
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


def _parse_classifier_json(text: str) -> dict:
    """Isolated from the network call so it's unit-testable. Shared by both
    Gemini and the OpenRouter fallback -- same requested JSON shape either
    way (see PROMPT_TEMPLATE), only how `text` gets extracted from the raw
    HTTP response differs between them. Models sometimes wrap JSON in
    ```json fences despite being told not to — stripped defensively."""
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
        "pre_analysis": data.get("pre_analysis") or "",
        "note": None,
    }


async def _call_gemini(headlines: list[str], client: httpx.AsyncClient) -> dict:
    resp = await client.post(
        GEMINI_API_URL.format(model=GEMINI_MODEL),
        params={"key": GEMINI_API_KEY},
        json={"contents": [{"parts": [{"text": _build_prompt(headlines)}]}]},
        # No generationConfig, deliberately, after two failed live
        # attempts at one (both 2026-08-11): maxOutputTokens: 1024 got
        # silently consumed by this model's hidden "thinking" tokens
        # before any visible JSON was produced (truncated, unparseable
        # response); thinkingConfig.thinkingBudget: 0 to turn thinking
        # off outright isn't a supported field on this model/API version
        # at all (flat 400 Bad Request). MAX_HEADLINES above is the only
        # verified-safe lever for cutting this call's latency.
    )
    resp.raise_for_status()
    data = resp.json()
    candidate = (data.get("candidates") or [{}])[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = parts[0]["text"] if parts else ""
    if not text:
        raise ValueError(f"empty Gemini response (finishReason={candidate.get('finishReason')})")
    return _parse_classifier_json(text)


async def _call_openrouter(headlines: list[str], client: httpx.AsyncClient) -> dict:
    """OpenAI-compatible chat-completions endpoint, not Gemini's
    generateContent -- different request/response shape, same prompt.
    gpt-oss-20b:free is a reasoning model: its response separates
    "reasoning" from "content"; only content (the actual answer) matters
    here, live-verified 2026-08-14 not to leak reasoning text into it."""
    resp = await client.post(
        OPENROUTER_API_URL,
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
        json={
            "model": OPENROUTER_MODEL,
            "messages": [{"role": "user", "content": _build_prompt(headlines)}],
        },
    )
    resp.raise_for_status()
    data = resp.json()
    choice = (data.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or ""
    if not text:
        raise ValueError(f"empty OpenRouter response (finish_reason={choice.get('finish_reason')})")
    return _parse_classifier_json(text)


async def classify_headlines(headlines: list[str], client: httpx.AsyncClient | None = None) -> dict:
    """{"items": [{"headline", "sentiment", "reason"}, ...],
    "overall_sentiment": "<one-line>", "note": str|None}. Tries Gemini
    first, falls back to OpenRouter's free tier if Gemini isn't configured
    or its call fails, and only then falls back to an all-neutral result
    (never raises) — per the brief's own instruction to degrade gracefully.

    Raw REST throughout (no SDK for either provider) — see this module's
    docstring for why, re: Gemini specifically."""
    if not headlines:
        return {"items": [], "overall_sentiment": "No headlines available.", "note": None}

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=30)
    errors = []
    try:
        if GEMINI_API_KEY:
            try:
                return await _call_gemini(headlines, client)
            except Exception as e:
                print(f"news_ai: Gemini classification failed: {e}")
                errors.append(f"Gemini call failed: {e}")
        else:
            errors.append("GEMINI_API_KEY not configured")

        if OPENROUTER_API_KEY:
            try:
                result = await _call_openrouter(headlines, client)
                result["note"] = "; ".join(errors + [f"used OpenRouter fallback ({OPENROUTER_MODEL})"])
                return result
            except Exception as e:
                print(f"news_ai: OpenRouter classification failed: {e}")
                errors.append(f"OpenRouter call failed: {e}")
        else:
            errors.append("OPENROUTER_API_KEY not configured")

        return _neutral_fallback(headlines, note="; ".join(errors))
    finally:
        if owns_client:
            await client.aclose()


async def get_news_brief(now: dt.datetime | None = None) -> dict:
    """headlines + classification, ready to drop into the morning brief:
    {"headlines": [...], "news_sentiment": "<one-line>"}. `headlines` is
    trimmed to TOP_NEWS_COUNT — up to MAX_HEADLINES are fetched and
    classified for a fuller read on `overall_sentiment`, but only the
    handful most likely to actually move the market are kept for display."""
    t0 = time.monotonic()
    items = await fetch_all_headlines(now=now)
    t1 = time.monotonic()
    print(f"news_ai: fetch_all_headlines took {t1 - t0:.2f}s ({len(items)} headlines)")
    classified = await classify_headlines([it["headline"] for it in items])
    print(f"news_ai: classify_headlines took {time.monotonic() - t1:.2f}s")
    return {
        "headlines": select_top_market_moving(classified["items"]),
        "news_sentiment": classified["overall_sentiment"],
    }


# ---------------- Live desk (US / crude / Trump / Nifty) ----------------
# Separate from the morning-brief RSS list: topic-specific Google News RSS
# plus the existing India market feeds. Cached a few minutes so the
# Contribution page can poll without a Gemini round-trip every time.

LIVE_NEWS_FEEDS = {
    "nifty": [
        "https://news.google.com/rss/search?q=Nifty+50+OR+Sensex+OR+NSE+India+when:1d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://www.moneycontrol.com/rss/marketreports.xml",
        "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    ],
    "us": [
        "https://news.google.com/rss/search?q=US+stock+market+OR+Nasdaq+OR+S%26P+500+OR+Federal+Reserve+when:1d&hl=en-IN&gl=IN&ceid=IN:en",
    ],
    "trump": [
        "https://news.google.com/rss/search?q=Trump+tariff+OR+Trump+Fed+OR+Trump+India+OR+Trump+tweet+when:1d&hl=en-IN&gl=IN&ceid=IN:en",
    ],
    "crude": [
        "https://news.google.com/rss/search?q=crude+oil+OR+Brent+OR+WTI+when:1d&hl=en-IN&gl=IN&ceid=IN:en",
    ],
}

# Display + fetch fill order. Crude is the leftover / "other" bucket.
TOPIC_PRIORITY = ("nifty", "us", "trump", "crude")
LIVE_PER_TOPIC = {"nifty": 6, "us": 4, "trump": 3, "crude": 3}

TOPIC_KEYWORDS = {
    "nifty": ("nifty", "sensex", "nse ", "bse ", "fii", "dii", "rbi", "gift nifty"),
    "us": ("federal reserve", "powell", "s&p", "nasdaq", "dow ", "wall street", "us stocks", "treasury yield", "fomc"),
    "trump": ("trump", "tariff", "white house", "maga"),
    "crude": ("crude", "brent", "wti", "opec", "oil price", "oil prices"),
}

LIVE_PROMPT_TEMPLATE = """You are a desk analyst for Indian Nifty 50 traders.
Each headline has a suggested topic in this priority: nifty, us, trump, crude (other).
Prefer tagging Nifty 50 / India market news as nifty when the headline is about Indian equities.

For each headline classify Nifty impact (not US-only impact):
- sentiment: bullish|bearish|neutral for Nifty
- impact: high|medium|low (high only if it could actually move the Indian session)
- topic: us|crude|trump|nifty|other
- reason: 8 words or fewer

Then write pre_analysis: ONE short sentence (max 22 words) on impact for the Indian / Nifty session only. No buy/sell advice.

Respond with ONLY valid JSON, no markdown:
{{"items":[{{"headline":"...","sentiment":"bullish|bearish|neutral","impact":"high|medium|low","topic":"us|crude|trump|nifty|other","reason":"..."}}],"pre_analysis":"..."}}

Headlines:
{headlines_block}
"""

LIVE_CACHE_SECONDS = 300  # 5 minutes — matches the dashboard poll
TAPE_CACHE_SECONDS = 20  # quotes are cheap; do not freeze them with news AI
_live_cache: dict = {"at": 0.0, "payload": None}
_tape_cache: dict = {"at": 0.0, "payload": None}

IMPACT_WEIGHT = {"high": 3, "medium": 2, "low": 1}
HEADLINE_INDIA_PCT = {"high": 80, "medium": 45, "low": 20}


def shorten_pre_analysis(text: str, max_chars: int = 140) -> str:
    """Keep the pre-read to one short line for the left rail."""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""
    for sep in (". ", "! ", "? "):
        if sep in cleaned:
            cleaned = cleaned.split(sep, 1)[0].rstrip(".!?") + "."
            break
    if len(cleaned) > max_chars:
        cleaned = cleaned[: max_chars - 1].rsplit(" ", 1)[0] + "…"
    return cleaned


def india_pct_for_item(sentiment: str | None, impact: str | None) -> int:
    mag = HEADLINE_INDIA_PCT.get((impact or "low").lower(), 20)
    s = (sentiment or "neutral").lower()
    if s == "bullish":
        return mag
    if s == "bearish":
        return -mag
    return 0


def summarize_india_impact(sections: dict) -> dict:
    """Net news tilt for India: −100 (bearish) to +100 (bullish)."""
    items = [it for rows in (sections or {}).values() for it in (rows or [])]
    signed = weight = 0
    n_bull = n_bear = 0
    for it in items:
        w = IMPACT_WEIGHT.get(str(it.get("impact", "low")).lower(), 1)
        s = str(it.get("sentiment", "neutral")).lower()
        if s == "bullish":
            signed += w
            n_bull += 1
        elif s == "bearish":
            signed -= w
            n_bear += 1
        weight += w
    tilt = round(100 * signed / weight) if weight else 0
    if tilt >= 25:
        label = "Positive for India"
    elif tilt <= -25:
        label = "Negative for India"
    elif tilt >= 8:
        label = "Mildly positive"
    elif tilt <= -8:
        label = "Mildly negative"
    else:
        label = "Mixed for India"
    return {
        "india_tilt_pct": tilt,
        "label": label,
        "bullish_headlines": n_bull,
        "bearish_headlines": n_bear,
        "headline_count": len(items),
    }


def _topic_cap(topic: str) -> int:
    return int(LIVE_PER_TOPIC.get(topic, 3))


def infer_topic(headline: str, suggested: str | None = None) -> str:
    text = (headline or "").lower()
    for topic, keys in TOPIC_KEYWORDS.items():
        if any(k in text for k in keys):
            return topic
    if suggested in TOPIC_KEYWORDS or suggested in ("us", "crude", "trump", "nifty"):
        return suggested
    return "other"


def _live_prompt(rows: list[dict]) -> str:
    block = "\n".join(
        f"{i + 1}. [{r.get('topic') or 'other'}] {r['headline']}"
        for i, r in enumerate(rows)
    )
    return LIVE_PROMPT_TEMPLATE.format(headlines_block=block)


async def _classify_live(rows: list[dict], client: httpx.AsyncClient) -> dict:
    if not rows:
        return {"items": [], "pre_analysis": "No headlines available.", "note": None}
    prompt = _live_prompt(rows)
    errors = []
    if GEMINI_API_KEY:
        try:
            resp = await client.post(
                GEMINI_API_URL.format(model=GEMINI_MODEL),
                params={"key": GEMINI_API_KEY},
                json={"contents": [{"parts": [{"text": prompt}]}]},
            )
            resp.raise_for_status()
            data = resp.json()
            candidate = (data.get("candidates") or [{}])[0]
            parts = (candidate.get("content") or {}).get("parts") or []
            text = parts[0]["text"] if parts else ""
            parsed = _parse_classifier_json(text)
            return {
                "items": parsed.get("items") or [],
                "pre_analysis": (parsed.get("pre_analysis") or parsed.get("overall_sentiment") or "").strip(),
                "note": None,
            }
        except Exception as e:
            print(f"news_ai: live Gemini failed: {e}")
            errors.append(f"Gemini: {e}")
    else:
        errors.append("GEMINI_API_KEY not configured")

    if OPENROUTER_API_KEY:
        try:
            resp = await client.post(
                OPENROUTER_API_URL,
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                json={"model": OPENROUTER_MODEL, "messages": [{"role": "user", "content": prompt}]},
            )
            resp.raise_for_status()
            data = resp.json()
            choice = (data.get("choices") or [{}])[0]
            text = (choice.get("message") or {}).get("content") or ""
            parsed = _parse_classifier_json(text)
            return {
                "items": parsed.get("items") or [],
                "pre_analysis": (parsed.get("pre_analysis") or parsed.get("overall_sentiment") or "").strip(),
                "note": "; ".join(errors + [f"used OpenRouter ({OPENROUTER_MODEL})"]),
            }
        except Exception as e:
            print(f"news_ai: live OpenRouter failed: {e}")
            errors.append(f"OpenRouter: {e}")
    else:
        errors.append("OPENROUTER_API_KEY not configured")

    return {
        "items": [{"headline": r["headline"], "sentiment": "neutral", "impact": "low", "topic": r.get("topic"), "reason": "unavailable"} for r in rows],
        "pre_analysis": "AI pre-read is unavailable right now. Headlines below are raw feeds — treat them as unfiltered.",
        "note": "; ".join(errors),
    }


def merge_live_classification(raw_rows: list[dict], classified: dict) -> dict:
    by_h = {}
    for it in classified.get("items") or []:
        key = (it.get("headline") or "").strip().lower()
        if key:
            by_h[key] = it
    sections = {t: [] for t in TOPIC_PRIORITY}
    for r in raw_rows:
        hit = by_h.get(r["headline"].strip().lower(), {})
        topic = infer_topic(r["headline"], hit.get("topic") or r.get("topic"))
        if topic not in sections:
            topic = r.get("topic") if r.get("topic") in sections else "crude"
        item = {
            "headline": r["headline"],
            "source": r.get("source"),
            "link": r.get("link"),
            "published": r["published"].isoformat() if isinstance(r.get("published"), dt.datetime) else r.get("published"),
            "topic": topic,
            "sentiment": hit.get("sentiment") or "neutral",
            "impact": hit.get("impact") or "low",
            "reason": hit.get("reason") or "",
            "india_pct": india_pct_for_item(hit.get("sentiment") or "neutral", hit.get("impact") or "low"),
        }
        if len(sections[topic]) < _topic_cap(topic):
            sections[topic].append(item)
    return {
        "sections": sections,
        "pre_analysis": shorten_pre_analysis(classified.get("pre_analysis") or ""),
        "india_impact": summarize_india_impact(sections),
        "note": classified.get("note"),
    }


async def fetch_live_topic_headlines(now: dt.datetime | None = None) -> list[dict]:
    now = now or dt.datetime.now(dt.timezone.utc)
    # Short timeout: one slow Google News feed must not stall the whole desk.
    async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
        tasks = []
        meta = []
        for topic, urls in LIVE_NEWS_FEEDS.items():
            for url in urls:
                tasks.append(fetch_rss_headlines(client, url, topic))
                meta.append(topic)
        groups = await asyncio.gather(*tasks)
    rows = []
    for topic, group in zip(meta, groups):
        for it in group:
            it = {**it, "topic": infer_topic(it["headline"], topic)}
            rows.append(it)
    recent = _within_window(rows, now, hours=24)
    recent.sort(key=lambda it: it["published"] or now, reverse=True)
    # Fill in priority order: Nifty 50, US, Trump, then other (crude).
    buckets = {t: [] for t in TOPIC_PRIORITY}
    for it in _dedupe(recent):
        t = it.get("topic") if it.get("topic") in buckets else "crude"
        it["topic"] = t
        if len(buckets[t]) < _topic_cap(t):
            buckets[t].append(it)
    picked = []
    for t in TOPIC_PRIORITY:
        picked.extend(buckets[t])
    return picked


def _rows_sig(rows: list[dict]) -> tuple:
    return tuple(sorted((r.get("headline") or "").strip().lower() for r in rows))


async def _fetch_tape() -> dict:
    try:
        import market_data
        tape = await market_data.fetch_quotes(["^NSEI", "^GSPC", "^IXIC", "BZ=F"])
        return {
            "nifty": tape.get("^NSEI"),
            "sp500": tape.get("^GSPC"),
            "nasdaq": tape.get("^IXIC"),
            "brent": tape.get("BZ=F"),
        }
    except Exception as e:
        print(f"news_ai: live tape quotes failed: {e}")
        return {}


async def get_live_tape(force: bool = False) -> dict:
    """Yahoo last prices, cached ~20s so the chips can tick without Gemini."""
    now_m = time.monotonic()
    cached = _tape_cache.get("payload")
    if cached and not force and now_m - _tape_cache["at"] < TAPE_CACHE_SECONDS:
        return cached
    tape = await _fetch_tape()
    payload = {
        "tape": tape,
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "refresh_seconds": TAPE_CACHE_SECONDS,
    }
    _tape_cache["at"] = now_m
    _tape_cache["payload"] = payload
    return payload


def _stamp(payload: dict, *, ai_pending: bool, stale: bool = False) -> dict:
    payload["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    payload["refresh_seconds"] = LIVE_CACHE_SECONDS
    payload["ai_pending"] = ai_pending
    payload["stale"] = stale
    return payload


async def _assemble(rows: list[dict], tape: dict, classified: dict | None, *, ai_pending: bool) -> dict:
    classified = classified or {
        "items": [],
        "pre_analysis": "Scoring India impact…",
        "note": None,
    }
    payload = merge_live_classification(rows, classified)
    payload["tape"] = tape or {}
    return _stamp(payload, ai_pending=ai_pending)


async def get_live_news_desk(force: bool = False, lite: bool = False) -> dict:
    """Headlines are cheap (RSS + Yahoo). Gemini is the slow part (~15–25s).

    lite=True: RSS + tape only, reuse last AI scores if the headline set
    matches. The UI should call this first so the rail fills in seconds.

    Full call: returns a fresh cache if young; otherwise reuses last payload
    (stale-while-revalidate) unless force=True, which waits for a full rebuild.
    """
    now_m = time.monotonic()
    cached = _live_cache.get("payload")
    if cached and not force and not lite and now_m - _live_cache["at"] < LIVE_CACHE_SECONDS:
        return cached

    try:
        rows, tape_pack = await asyncio.gather(fetch_live_topic_headlines(), get_live_tape())
        tape = tape_pack.get("tape") or {}
        sig = _rows_sig(rows)
        classified = _live_cache.get("classified") if _live_cache.get("sig") == sig else None

        if lite:
            payload = await _assemble(rows, tape, classified, ai_pending=classified is None)
            if classified is None and cached:
                # Keep previous India % / pre-read until the full pass finishes
                payload["pre_analysis"] = cached.get("pre_analysis") or payload["pre_analysis"]
                payload["india_impact"] = cached.get("india_impact") or payload["india_impact"]
            return payload

        if classified is None:
            async with httpx.AsyncClient(timeout=35) as client:
                classified = await _classify_live(rows, client)

        payload = await _assemble(rows, tape, classified, ai_pending=False)
        _live_cache["at"] = now_m
        _live_cache["payload"] = payload
        _live_cache["classified"] = classified
        _live_cache["sig"] = sig
        return payload
    except Exception as e:
        print(f"news_ai: get_live_news_desk failed: {e}")
        if cached:
            return {**cached, "stale": True, "note": str(e)}
        return _stamp({
            "sections": {t: [] for t in TOPIC_PRIORITY},
            "pre_analysis": "News desk failed to load.",
            "india_impact": summarize_india_impact({}),
            "tape": {},
            "note": str(e),
        }, ai_pending=False)
