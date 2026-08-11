"""
notify.py — Telegram delivery for the morning brief. Module 6 of the build
order (docs/PREMARKET_ENGINE.md).

Plain httpx POST to the Telegram Bot API, no library, matching the original
brief's instruction. Silently no-ops (with a log line) when
TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID aren't set, same convention as
storage.py's Supabase skip — the engine runs end-to-end without Telegram
configured, it just doesn't notify anyone.
"""

import os

import httpx

import scoring

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

VERDICT_EMOJI = {"Gap-up likely": "🟢", "Gap-down likely": "🔴", "Flat open": "🟡"}


def configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def _describe_component(name: str, detail: dict) -> str:
    if name == "gift":
        gap, fair_value = detail.get("gap_pct"), detail.get("fair_value")
        if gap is None:
            return "GIFT Nifty: unavailable"
        return f"GIFT Nifty gap {gap:+.2f}% vs fair value {fair_value}"
    if name == "us_asia":
        us, asia = detail.get("us_avg_pct"), detail.get("asia_avg_pct")
        parts = [f"US {us:+.2f}%" for _ in [0] if us is not None] + \
                [f"Asia {asia:+.2f}%" for _ in [0] if asia is not None]
        return "US/Asia: " + (", ".join(parts) if parts else "unavailable")
    if name == "macro":
        flags = detail.get("flags") or {}
        if not flags:
            return "Macro: unavailable"
        return "Macro: " + ", ".join(f"{k} {v:+.2f}" for k, v in flags.items())
    if name == "fii":
        return f"FII positioning: {detail.get('ratio')}% long/short, trend {detail.get('trend')}"
    return f"{name}: {detail}"


def _top_signals(components: dict, n: int = 3) -> list[str]:
    """The n components with the largest weighted contribution to the score
    (|weight * score|), most impactful first — components that are missing
    (None, or lack a 'score' key) are excluded rather than treated as 0."""
    ranked = []
    for name in ("gift", "us_asia", "macro", "fii"):
        detail = components.get(name)
        if not detail or detail.get("score") is None:
            continue
        impact = abs(scoring.WEIGHTS.get(name, 0) * detail["score"])
        ranked.append((impact, name, detail))
    ranked.sort(key=lambda r: r[0], reverse=True)
    return [_describe_component(name, detail) for _, name, detail in ranked[:n]]


def format_brief_message(brief: dict) -> str:
    verdict = brief.get("verdict", "Flat open")
    score = brief.get("score") or 0.0
    emoji = VERDICT_EMOJI.get(verdict, "⚪")
    components = brief.get("components") or {}
    signals = _top_signals(components)

    low, high = brief.get("expected_low"), brief.get("expected_high")
    range_line = f"{low} - {high}" if low is not None and high is not None else "unavailable"
    news_line = brief.get("news_sentiment") or "unavailable (news classifier not wired up yet)"
    disclaimer = brief.get("disclaimer") or scoring.DISCLAIMER

    lines = [f"{emoji} {verdict} (score {score:+.1f})", ""]
    if signals:
        lines.append("Top signals:")
        lines.extend(f"{i}. {s}" for i, s in enumerate(signals, start=1))
        lines.append("")
    lines.append(f"Expected range: {range_line}")
    lines.append(f"News sentiment: {news_line}")
    lines.append("")
    lines.append(disclaimer)
    return "\n".join(lines)


async def send_brief(brief: dict, client: httpx.AsyncClient | None = None) -> bool:
    if not configured():
        print("notify: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set, skipping Telegram send")
        return False
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        resp = await client.post(
            TELEGRAM_API_URL.format(token=TELEGRAM_BOT_TOKEN),
            json={"chat_id": TELEGRAM_CHAT_ID, "text": format_brief_message(brief)},
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"notify: Telegram send failed: {e}")
        return False
    finally:
        if owns_client:
            await client.aclose()
