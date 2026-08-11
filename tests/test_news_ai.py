import asyncio
import datetime as dt
import sys
import types

import httpx

import news_ai

RSS_SAMPLE = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>Test Feed</title>
<item><title>Nifty hits fresh record high on strong FII inflows</title><pubDate>Mon, 10 Aug 2026 06:00:00 GMT</pubDate></item>
<item><title>Old stale headline from last week</title><pubDate>Mon, 03 Aug 2026 06:00:00 GMT</pubDate></item>
<item><title></title><pubDate>Mon, 10 Aug 2026 06:00:00 GMT</pubDate></item>
<item><title>Headline with no pubDate</title></item>
</channel></rss>
"""


# ---------------- RSS parsing ----------------

def test_parse_rss_items_extracts_title_and_pubdate():
    items = news_ai._parse_rss_items(RSS_SAMPLE, source="test")
    headlines = [it["headline"] for it in items]
    assert "Nifty hits fresh record high on strong FII inflows" in headlines
    assert "" not in headlines  # empty title skipped
    assert all(it["source"] == "test" for it in items)


def test_parse_rss_items_handles_missing_pubdate():
    items = news_ai._parse_rss_items(RSS_SAMPLE, source="test")
    no_date = next(it for it in items if it["headline"] == "Headline with no pubDate")
    assert no_date["published"] is None


def test_parse_rss_items_returns_empty_on_malformed_xml():
    assert news_ai._parse_rss_items("not xml at all <<<", source="test") == []


def test_within_window_filters_stale_but_keeps_undated():
    now = dt.datetime(2026, 8, 10, 8, 0, tzinfo=dt.timezone.utc)
    items = [
        {"headline": "fresh", "published": now - dt.timedelta(hours=2), "source": "a"},
        {"headline": "stale", "published": now - dt.timedelta(hours=20), "source": "a"},
        {"headline": "undated", "published": None, "source": "a"},
    ]
    result = news_ai._within_window(items, now, hours=12)
    assert {it["headline"] for it in result} == {"fresh", "undated"}


def test_dedupe_is_case_insensitive():
    items = [
        {"headline": "Nifty rallies", "published": None, "source": "a"},
        {"headline": "nifty rallies", "published": None, "source": "b"},
        {"headline": "Different headline", "published": None, "source": "a"},
    ]
    result = news_ai._dedupe(items)
    assert len(result) == 2


def test_fetch_rss_headlines_returns_empty_list_on_http_error():
    class FailingClient:
        async def get(self, url, **kwargs):
            raise httpx.HTTPError("boom")

    result = asyncio.run(news_ai.fetch_rss_headlines(FailingClient(), "http://example.com/rss", "test"))
    assert result == []


def test_fetch_all_headlines_combines_dedupes_and_caps(monkeypatch):
    now = dt.datetime(2026, 8, 10, 8, 0, tzinfo=dt.timezone.utc)

    async def fake_fetch(client, url, source):
        return [
            {"headline": f"{source} headline {i}", "published": now - dt.timedelta(minutes=i), "source": source}
            for i in range(20)
        ]

    monkeypatch.setattr(news_ai, "fetch_rss_headlines", fake_fetch)

    result = asyncio.run(news_ai.fetch_all_headlines(now=now))
    assert len(result) == news_ai.MAX_HEADLINES
    # newest first
    assert result[0]["published"] >= result[-1]["published"]


# ---------------- Gemini classification ----------------

def test_build_prompt_numbers_headlines():
    prompt = news_ai._build_prompt(["First", "Second"])
    assert "1. First" in prompt
    assert "2. Second" in prompt


def test_parse_gemini_response_plain_json():
    text = '{"items": [{"headline": "H", "sentiment": "bullish", "reason": "r"}], "overall_sentiment": "Bullish"}'
    result = news_ai._parse_gemini_response(text)
    assert result["items"][0]["sentiment"] == "bullish"
    assert result["overall_sentiment"] == "Bullish"
    assert result["note"] is None


def test_parse_gemini_response_strips_markdown_fences():
    text = '```json\n{"items": [], "overall_sentiment": "Neutral"}\n```'
    result = news_ai._parse_gemini_response(text)
    assert result["overall_sentiment"] == "Neutral"


def test_classify_headlines_returns_empty_result_for_no_headlines():
    result = asyncio.run(news_ai.classify_headlines([]))
    assert result == {"items": [], "overall_sentiment": "No headlines available.", "note": None}


def test_classify_headlines_falls_back_to_neutral_when_gemini_not_configured(monkeypatch):
    monkeypatch.setattr(news_ai, "GEMINI_API_KEY", None)
    result = asyncio.run(news_ai.classify_headlines(["H1", "H2"]))
    assert result["note"] == "GEMINI_API_KEY not configured"
    assert all(item["sentiment"] == "neutral" for item in result["items"])


def test_classify_headlines_falls_back_to_neutral_when_gemini_call_fails(monkeypatch):
    monkeypatch.setattr(news_ai, "GEMINI_API_KEY", "fake-key")

    class FailingModel:
        def __init__(self, name):
            pass

        def generate_content(self, prompt):
            raise RuntimeError("Gemini unreachable")

    fake_genai = types.SimpleNamespace(configure=lambda api_key: None, GenerativeModel=FailingModel)
    monkeypatch.setitem(sys.modules, "google.generativeai", fake_genai)

    result = asyncio.run(news_ai.classify_headlines(["H1"]))
    assert "Gemini unreachable" in result["note"]
    assert result["items"][0]["sentiment"] == "neutral"


def test_classify_headlines_returns_parsed_result_on_success(monkeypatch):
    monkeypatch.setattr(news_ai, "GEMINI_API_KEY", "fake-key")
    calls = {}

    class FakeModel:
        def __init__(self, name):
            calls["model_name"] = name

        def generate_content(self, prompt):
            calls["prompt"] = prompt

            class R:
                text = ('{"items": [{"headline": "H1", "sentiment": "bullish", "reason": "strong inflows"}], '
                         '"overall_sentiment": "Mildly bullish on FII inflows"}')
            return R()

    fake_genai = types.SimpleNamespace(
        configure=lambda api_key: calls.update({"api_key": api_key}),
        GenerativeModel=FakeModel,
    )
    monkeypatch.setitem(sys.modules, "google.generativeai", fake_genai)

    result = asyncio.run(news_ai.classify_headlines(["H1"]))

    assert result["overall_sentiment"] == "Mildly bullish on FII inflows"
    assert result["items"][0]["sentiment"] == "bullish"
    assert calls["api_key"] == "fake-key"
    assert "H1" in calls["prompt"]


# ---------------- top market-moving selection ----------------

def test_select_top_market_moving_ranks_by_impact_then_directional():
    items = [
        {"headline": "low bearish", "sentiment": "bearish", "impact": "low"},
        {"headline": "high bullish", "sentiment": "bullish", "impact": "high"},
        {"headline": "medium neutral", "sentiment": "neutral", "impact": "medium"},
        {"headline": "high neutral", "sentiment": "neutral", "impact": "high"},
        {"headline": "low neutral", "sentiment": "neutral", "impact": "low"},
    ]
    result = news_ai.select_top_market_moving(items, n=3)
    assert [i["headline"] for i in result] == ["high bullish", "high neutral", "medium neutral"]


def test_select_top_market_moving_caps_at_n():
    items = [{"headline": f"h{i}", "sentiment": "neutral", "impact": "low"} for i in range(10)]
    assert len(news_ai.select_top_market_moving(items, n=4)) == 4


def test_select_top_market_moving_handles_missing_or_unrecognized_impact():
    items = [
        {"headline": "no impact field", "sentiment": "bullish"},
        {"headline": "garbage impact", "sentiment": "bearish", "impact": "urgent!!"},
        {"headline": "recognized", "sentiment": "neutral", "impact": "high"},
    ]
    result = news_ai.select_top_market_moving(items, n=3)
    assert result[0]["headline"] == "recognized"  # only one with a recognized impact rank


def test_get_news_brief_combines_fetch_and_classify(monkeypatch):
    async def fake_fetch_all_headlines(now=None):
        return [{"headline": "H1", "published": None, "source": "test"}]

    async def fake_classify_headlines(headlines):
        assert headlines == ["H1"]
        return {"items": [{"headline": "H1", "sentiment": "neutral", "reason": "r"}],
                "overall_sentiment": "Neutral overall", "note": None}

    monkeypatch.setattr(news_ai, "fetch_all_headlines", fake_fetch_all_headlines)
    monkeypatch.setattr(news_ai, "classify_headlines", fake_classify_headlines)

    result = asyncio.run(news_ai.get_news_brief())
    assert result == {"headlines": [{"headline": "H1", "sentiment": "neutral", "reason": "r"}],
                       "news_sentiment": "Neutral overall"}


def test_get_news_brief_trims_to_top_news_count(monkeypatch):
    classified_items = [
        {"headline": f"h{i}", "sentiment": "bullish", "impact": "high"} for i in range(10)
    ]

    async def fake_fetch_all_headlines(now=None):
        return [{"headline": it["headline"], "published": None, "source": "test"} for it in classified_items]

    async def fake_classify_headlines(headlines):
        return {"items": classified_items, "overall_sentiment": "Bullish", "note": None}

    monkeypatch.setattr(news_ai, "fetch_all_headlines", fake_fetch_all_headlines)
    monkeypatch.setattr(news_ai, "classify_headlines", fake_classify_headlines)

    result = asyncio.run(news_ai.get_news_brief())
    assert len(result["headlines"]) == news_ai.TOP_NEWS_COUNT
