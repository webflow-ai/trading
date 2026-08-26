import asyncio
import datetime as dt

import httpx

import news_ai


class FakeResponse:
    def __init__(self, json_data=None, raise_exc=None):
        self._json = json_data
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc:
            raise self._raise_exc

    def json(self):
        return self._json


class FakeAsyncClient:
    def __init__(self, response=None, responses=None):
        # `responses`: consumed one-per-call in order, for tests exercising
        # a Gemini-then-OpenRouter sequence with different outcomes each.
        # `response`: same one returned for every call (existing behavior).
        self._response = response
        self._responses = list(responses) if responses is not None else None
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self._responses is not None:
            return self._responses.pop(0)
        return self._response

    async def aclose(self):
        pass

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


def test_parse_classifier_json_plain_json():
    text = '{"items": [{"headline": "H", "sentiment": "bullish", "reason": "r"}], "overall_sentiment": "Bullish"}'
    result = news_ai._parse_classifier_json(text)
    assert result["items"][0]["sentiment"] == "bullish"
    assert result["overall_sentiment"] == "Bullish"
    assert result["note"] is None


def test_parse_classifier_json_strips_markdown_fences():
    text = '```json\n{"items": [], "overall_sentiment": "Neutral"}\n```'
    result = news_ai._parse_classifier_json(text)
    assert result["overall_sentiment"] == "Neutral"


def test_classify_headlines_returns_empty_result_for_no_headlines():
    result = asyncio.run(news_ai.classify_headlines([]))
    assert result == {"items": [], "overall_sentiment": "No headlines available.", "note": None}


def test_classify_headlines_falls_back_to_neutral_when_neither_provider_configured(monkeypatch):
    monkeypatch.setattr(news_ai, "GEMINI_API_KEY", None)
    monkeypatch.setattr(news_ai, "OPENROUTER_API_KEY", None)
    result = asyncio.run(news_ai.classify_headlines(["H1", "H2"]))
    assert result["note"] == "GEMINI_API_KEY not configured; OPENROUTER_API_KEY not configured"
    assert all(item["sentiment"] == "neutral" for item in result["items"])


def test_classify_headlines_falls_back_to_neutral_when_gemini_fails_and_openrouter_not_configured(monkeypatch):
    monkeypatch.setattr(news_ai, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(news_ai, "OPENROUTER_API_KEY", None)
    client = FakeAsyncClient(response=FakeResponse(raise_exc=httpx.HTTPError("Gemini unreachable")))

    result = asyncio.run(news_ai.classify_headlines(["H1"], client=client))

    assert "Gemini unreachable" in result["note"]
    assert "OPENROUTER_API_KEY not configured" in result["note"]
    assert result["items"][0]["sentiment"] == "neutral"
    assert len(client.calls) == 1  # OpenRouter never attempted -- not configured


def test_classify_headlines_falls_back_when_response_has_no_visible_text(monkeypatch):
    # A response cut short before producing any output (e.g. finishReason
    # MAX_TOKENS with the budget spent entirely on hidden thinking tokens)
    # has candidates but no usable content/parts.
    monkeypatch.setattr(news_ai, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(news_ai, "OPENROUTER_API_KEY", None)
    gemini_json = {"candidates": [{"finishReason": "MAX_TOKENS"}]}
    client = FakeAsyncClient(response=FakeResponse(json_data=gemini_json))

    result = asyncio.run(news_ai.classify_headlines(["H1"], client=client))

    assert "MAX_TOKENS" in result["note"]
    assert result["items"][0]["sentiment"] == "neutral"


def test_classify_headlines_falls_back_to_openrouter_when_gemini_fails(monkeypatch):
    monkeypatch.setattr(news_ai, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(news_ai, "OPENROUTER_API_KEY", "fake-or-key")
    openrouter_json = {
        "choices": [{"message": {"content":
            '{"items": [{"headline": "H1", "sentiment": "bearish", "reason": "weak"}], '
            '"overall_sentiment": "Mildly bearish"}'
        }}],
    }
    client = FakeAsyncClient(responses=[
        FakeResponse(raise_exc=httpx.HTTPError("Gemini 503")),
        FakeResponse(json_data=openrouter_json),
    ])

    result = asyncio.run(news_ai.classify_headlines(["H1"], client=client))

    assert result["overall_sentiment"] == "Mildly bearish"
    assert result["items"][0]["sentiment"] == "bearish"
    assert "Gemini 503" in result["note"]
    assert "OpenRouter fallback" in result["note"]
    assert len(client.calls) == 2
    or_url, or_kwargs = client.calls[1]
    assert or_url == news_ai.OPENROUTER_API_URL
    assert or_kwargs["headers"]["Authorization"] == "Bearer fake-or-key"
    assert "H1" in or_kwargs["json"]["messages"][0]["content"]


def test_classify_headlines_uses_openrouter_when_gemini_not_configured(monkeypatch):
    monkeypatch.setattr(news_ai, "GEMINI_API_KEY", None)
    monkeypatch.setattr(news_ai, "OPENROUTER_API_KEY", "fake-or-key")
    openrouter_json = {
        "choices": [{"message": {"content":
            '{"items": [{"headline": "H1", "sentiment": "neutral", "reason": "n/a"}], "overall_sentiment": "Flat"}'
        }}],
    }
    client = FakeAsyncClient(response=FakeResponse(json_data=openrouter_json))

    result = asyncio.run(news_ai.classify_headlines(["H1"], client=client))

    assert result["overall_sentiment"] == "Flat"
    assert "GEMINI_API_KEY not configured" in result["note"]
    assert "OpenRouter fallback" in result["note"]


def test_classify_headlines_falls_back_when_openrouter_response_has_no_visible_text(monkeypatch):
    monkeypatch.setattr(news_ai, "GEMINI_API_KEY", None)
    monkeypatch.setattr(news_ai, "OPENROUTER_API_KEY", "fake-or-key")
    openrouter_json = {"choices": [{"finish_reason": "length", "message": {"content": ""}}]}
    client = FakeAsyncClient(response=FakeResponse(json_data=openrouter_json))

    result = asyncio.run(news_ai.classify_headlines(["H1"], client=client))

    assert "empty OpenRouter response" in result["note"]
    assert result["items"][0]["sentiment"] == "neutral"


def test_classify_headlines_falls_back_to_neutral_when_both_providers_fail(monkeypatch):
    monkeypatch.setattr(news_ai, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(news_ai, "OPENROUTER_API_KEY", "fake-or-key")
    client = FakeAsyncClient(responses=[
        FakeResponse(raise_exc=httpx.HTTPError("Gemini down")),
        FakeResponse(raise_exc=httpx.HTTPError("OpenRouter down")),
    ])

    result = asyncio.run(news_ai.classify_headlines(["H1"], client=client))

    assert "Gemini down" in result["note"]
    assert "OpenRouter down" in result["note"]
    assert result["items"][0]["sentiment"] == "neutral"


def test_classify_headlines_returns_parsed_result_on_success(monkeypatch):
    monkeypatch.setattr(news_ai, "GEMINI_API_KEY", "fake-key")
    gemini_json = {
        "candidates": [{"content": {"parts": [{"text":
            '{"items": [{"headline": "H1", "sentiment": "bullish", "reason": "strong inflows"}], '
            '"overall_sentiment": "Mildly bullish on FII inflows"}'
        }]}}],
    }
    client = FakeAsyncClient(response=FakeResponse(json_data=gemini_json))

    result = asyncio.run(news_ai.classify_headlines(["H1"], client=client))

    assert result["overall_sentiment"] == "Mildly bullish on FII inflows"
    assert result["items"][0]["sentiment"] == "bullish"
    url, kwargs = client.calls[0]
    assert url == news_ai.GEMINI_API_URL.format(model=news_ai.GEMINI_MODEL)
    assert kwargs["params"] == {"key": "fake-key"}
    assert "H1" in kwargs["json"]["contents"][0]["parts"][0]["text"]


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


def test_infer_topic_from_keywords_and_suggested():
    assert news_ai.infer_topic("Trump tariff threat hits exporters") == "trump"
    assert news_ai.infer_topic("Brent crude jumps $3") == "crude"
    assert news_ai.infer_topic("Nasdaq futures slide after Fed") == "us"
    assert news_ai.infer_topic("Nifty 50 closes at record") == "nifty"
    assert news_ai.infer_topic("Random company earnings", suggested="us") == "us"


def test_merge_live_classification_buckets_and_keeps_pre_analysis():
    now = dt.datetime(2026, 8, 26, 10, 0, tzinfo=dt.timezone.utc)
    raw = [
        {"headline": "Brent crude jumps", "source": "crude", "link": "http://x", "published": now, "topic": "crude"},
        {"headline": "Nifty 50 opens higher", "source": "nifty", "link": None, "published": now, "topic": "nifty"},
    ]
    classified = {
        "items": [
            {"headline": "Brent crude jumps", "sentiment": "bearish", "impact": "high", "topic": "crude", "reason": "oil shock"},
            {"headline": "Nifty 50 opens higher", "sentiment": "bullish", "impact": "medium", "topic": "nifty", "reason": "gap up"},
        ],
        "pre_analysis": "Oil is the risk; Nifty opened firm.",
        "note": None,
    }
    out = news_ai.merge_live_classification(raw, classified)
    assert list(out["sections"].keys()) == list(news_ai.TOPIC_PRIORITY)
    assert list(news_ai.TOPIC_PRIORITY) == ["nifty", "us", "trump", "crude"]
    assert out["pre_analysis"].startswith("Oil")
    assert out["sections"]["crude"][0]["sentiment"] == "bearish"
    assert out["sections"]["crude"][0]["india_pct"] == -80
    assert out["india_impact"]["india_tilt_pct"] < 0
    assert out["india_impact"]["label"]
    assert out["sections"]["crude"][0]["impact"] == "high"
    assert out["sections"]["nifty"][0]["headline"] == "Nifty 50 opens higher"


def test_shorten_pre_analysis_keeps_one_short_line():
    long = "First sentence is the only one we want. Second sentence should drop. Third too."
    assert news_ai.shorten_pre_analysis(long) == "First sentence is the only one we want."
    assert len(news_ai.shorten_pre_analysis("x" * 400)) <= 140


def test_summarize_india_impact_net_percentage():
    sections = {
        "us": [{"sentiment": "bearish", "impact": "high"}],
        "nifty": [{"sentiment": "bullish", "impact": "low"}],
    }
    out = news_ai.summarize_india_impact(sections)
    assert out["india_tilt_pct"] < 0
    assert out["bearish_headlines"] == 1
    assert out["bullish_headlines"] == 1
    parsed = news_ai._parse_classifier_json('{"items":[],"pre_analysis":"Watch crude.","overall_sentiment":"x"}')
    assert parsed["pre_analysis"] == "Watch crude."


def test_index_engine_news_endpoint(monkeypatch):
    from fastapi.testclient import TestClient
    import api.index as index_module

    async def fake_desk(force=False, lite=False):
        return {"sections": {"us": [], "crude": [], "trump": [], "nifty": []}, "pre_analysis": "Quiet.", "updated_at": "now", "lite": lite}

    monkeypatch.setattr(news_ai, "get_live_news_desk", fake_desk)
    client = TestClient(index_module.app)
    r = client.get("/api/index-engine/news")
    assert r.status_code == 200
    assert r.json()["pre_analysis"] == "Quiet."
    lite = client.get("/api/index-engine/news", params={"lite": "true"})
    assert lite.json()["lite"] is True


def test_index_engine_tape_endpoint(monkeypatch):
    from fastapi.testclient import TestClient
    import api.index as index_module

    async def fake_tape(force=False):
        return {"tape": {"nifty": {"pct_change": 0.2, "price": 25000}}, "refresh_seconds": 20}

    monkeypatch.setattr(news_ai, "get_live_tape", fake_tape)
    client = TestClient(index_module.app)
    r = client.get("/api/index-engine/tape")
    assert r.status_code == 200
    assert r.json()["tape"]["nifty"]["price"] == 25000
