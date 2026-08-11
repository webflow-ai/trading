import asyncio

import notify
import scoring


def _brief(**overrides) -> dict:
    base = {
        "verdict": "Gap-up likely",
        "score": 42.5,
        "components": {
            "gift": {"score": 0.8, "gap_pct": 0.35, "fair_value": 24685.0},
            "us_asia": {"score": 0.3, "us_avg_pct": 0.4, "asia_avg_pct": 0.2},
            "macro": {"score": -0.2, "flags": {"crude": -0.1, "dxy": -0.3}},
            "fii": {"score": 0.5, "ratio": 58.0, "trend": "rising"},
        },
        "expected_low": 24500,
        "expected_high": 24800,
        "disclaimer": scoring.DISCLAIMER,
    }
    base.update(overrides)
    return base


def test_format_brief_message_includes_verdict_emoji_score_range_and_disclaimer():
    message = notify.format_brief_message(_brief())
    assert message.startswith("🟢 Gap-up likely (score +42.5)")
    assert "Expected range: 24500 - 24800" in message
    assert scoring.DISCLAIMER in message


def test_format_brief_message_orders_top_signals_by_weighted_impact():
    # weighted impact: gift 40*0.8=32, fii 15*0.5=7.5, us_asia 20*0.3=6, macro 15*-0.2=-3 (|3|)
    message = notify.format_brief_message(_brief())
    lines = message.splitlines()
    signal_lines = [l for l in lines if l[:1].isdigit()]
    assert len(signal_lines) == 3
    assert signal_lines[0].startswith("1. GIFT Nifty gap")
    assert signal_lines[1].startswith("2. FII positioning")
    assert signal_lines[2].startswith("3. US/Asia")


def test_format_brief_message_skips_missing_components():
    brief = _brief(components={"gift": {"score": 0.8, "gap_pct": 0.35, "fair_value": 24685.0}})
    message = notify.format_brief_message(brief)
    lines = message.splitlines()
    signal_lines = [l for l in lines if l[:1].isdigit()]
    assert signal_lines == ["1. GIFT Nifty gap +0.35% vs fair value 24685.0"]


def test_format_brief_message_handles_no_signals_and_no_range():
    brief = _brief(components={}, expected_low=None, expected_high=None)
    message = notify.format_brief_message(brief)
    assert "Top signals:" not in message
    assert "Expected range: unavailable" in message


def test_format_brief_message_falls_back_when_news_sentiment_absent():
    message = notify.format_brief_message(_brief())
    assert "News sentiment: unavailable" in message


def test_format_brief_message_uses_news_sentiment_when_present():
    message = notify.format_brief_message(_brief(news_sentiment="Mildly bullish on strong US tech earnings"))
    assert "News sentiment: Mildly bullish on strong US tech earnings" in message


def test_send_brief_skips_when_not_configured(monkeypatch):
    monkeypatch.setattr(notify, "TELEGRAM_BOT_TOKEN", None)
    monkeypatch.setattr(notify, "TELEGRAM_CHAT_ID", None)
    assert asyncio.run(notify.send_brief(_brief())) is False


class FakeResponse:
    def __init__(self, raise_exc=None):
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc:
            raise self._raise_exc


class FakeAsyncClient:
    def __init__(self, response=None):
        self._response = response or FakeResponse()
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._response

    async def aclose(self):
        pass


def test_send_brief_posts_correct_chat_id_and_text(monkeypatch):
    monkeypatch.setattr(notify, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(notify, "TELEGRAM_CHAT_ID", "12345")
    client = FakeAsyncClient()

    result = asyncio.run(notify.send_brief(_brief(), client=client))

    assert result is True
    assert len(client.calls) == 1
    url, kwargs = client.calls[0]
    assert url == "https://api.telegram.org/bottest-token/sendMessage"
    assert kwargs["json"]["chat_id"] == "12345"
    assert "Gap-up likely" in kwargs["json"]["text"]


def test_send_brief_returns_false_on_telegram_error(monkeypatch):
    monkeypatch.setattr(notify, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(notify, "TELEGRAM_CHAT_ID", "12345")
    client = FakeAsyncClient(response=FakeResponse(raise_exc=RuntimeError("boom")))

    assert asyncio.run(notify.send_brief(_brief(), client=client)) is False
