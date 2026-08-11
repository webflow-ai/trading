import asyncio
import datetime as dt

import pandas as pd

import jobs


def test_run_evening_job_persists_participant_oi_and_fii_dii_cash(monkeypatch):
    df = pd.DataFrame([{"date": "2026-08-10", "Client Type": "FII",
                         "Future Index Long": 300000, "Future Index Short": 250000}])

    class FakeClient:
        def __init__(self):
            self.closed = False

        def fetch_participant_oi(self, date):
            return df

        def fetch_fii_dii_cash(self):
            return {"date": "10-Aug-2026", "fii_buy": 1.0, "fii_sell": 2.0}

        def fetch_ban_list(self, date):
            return ["RELIANCE"]

        def close(self):
            self.closed = True

    fake_client = FakeClient()
    monkeypatch.setattr(jobs.nse_client, "NSEClient", lambda: fake_client)

    saved = {}

    async def fake_save_participant_oi(passed_df):
        saved["participant_oi"] = passed_df

    async def fake_save_fii_dii_cash(data):
        saved["fii_dii_cash"] = data

    monkeypatch.setattr(jobs.storage, "save_participant_oi", fake_save_participant_oi)
    monkeypatch.setattr(jobs.storage, "save_fii_dii_cash", fake_save_fii_dii_cash)

    result = asyncio.run(jobs.run_evening_job(today=dt.date(2026, 8, 10)))

    assert result["trade_date"] == "2026-08-10"
    assert result["sources"]["participant_oi"] == "ok"
    assert result["sources"]["fii_dii_cash"] == "ok"
    assert result["sources"]["ban_list"] == "ok"
    assert result["ban_list"] == ["RELIANCE"]
    assert saved["participant_oi"] is df
    assert saved["fii_dii_cash"]["fii_buy"] == 1.0
    assert fake_client.closed is True


def test_run_evening_job_survives_one_source_failing(monkeypatch):
    class FakeClient:
        def fetch_participant_oi(self, date):
            raise RuntimeError("NSE unreachable")

        def fetch_fii_dii_cash(self):
            return {"date": "10-Aug-2026", "fii_buy": 1.0, "fii_sell": 2.0}

        def fetch_ban_list(self, date):
            return []

        def close(self):
            pass

    monkeypatch.setattr(jobs.nse_client, "NSEClient", lambda: FakeClient())

    async def fake_save_fii_dii_cash(data):
        pass

    monkeypatch.setattr(jobs.storage, "save_fii_dii_cash", fake_save_fii_dii_cash)

    result = asyncio.run(jobs.run_evening_job(today=dt.date(2026, 8, 10)))

    assert "failed" in result["sources"]["participant_oi"]
    assert result["sources"]["fii_dii_cash"] == "ok"


def test_run_morning_job_assembles_and_persists_a_brief(monkeypatch):
    async def fake_fetch_gift_nifty(client):
        return {"price": 24700.0, "change": 45.0}

    async def fake_fetch_quote(client, symbol):
        return {"price": 100.0, "previous_close": 99.0, "pct_change": 1.0}

    async def fake_compute_levels(client):
        return {"pdh": 24800, "pdl": 24500, "previous_close": 24650,
                "previous_day_range": 300, "close_position_pct": 50.0}

    async def fake_compute_structure(client):
        return {"bias": "bullish", "last_event": "BOS_bullish"}

    async def fake_compute_fii_positioning():
        return {"ratio": 55.0, "trend": "rising"}

    async def fake_option_snapshot(symbol):
        return {"max_call_oi_strike": 24800, "max_put_oi_strike": 24500, "pcr": 1.1, "max_pain": 24650}

    async def fake_participant_snapshot():
        return {"trade_date": "2026-08-10", "participants": {
            "FII": {"long": 300000, "short": 250000, "ratio": 54.55},
            "DII": {"long": 50000, "short": 20000, "ratio": 71.43},
        }}

    async def fake_fii_dii_cash_snapshot():
        return {"trade_date": "2026-08-10", "fii_buy": 1000.0, "fii_sell": 800.0, "dii_buy": 500.0, "dii_sell": 600.0}

    saved_macro = {}
    saved_brief = {}

    async def fake_save_macro_snapshots(session, quotes, captured_at=None):
        saved_macro["session"] = session
        saved_macro["quotes"] = quotes

    async def fake_save_morning_brief(brief):
        saved_brief.update(brief)

    async def fake_get_news_brief():
        return {"headlines": [{"headline": "H1", "sentiment": "bullish", "reason": "r"}], "news_sentiment": "Mildly bullish"}

    monkeypatch.setattr(jobs.market_data, "fetch_gift_nifty", fake_fetch_gift_nifty)
    monkeypatch.setattr(jobs.market_data, "fetch_quote", fake_fetch_quote)
    monkeypatch.setattr(jobs.technicals, "compute_levels", fake_compute_levels)
    monkeypatch.setattr(jobs.technicals, "compute_structure", fake_compute_structure)
    monkeypatch.setattr(jobs.positioning, "compute_fii_positioning", fake_compute_fii_positioning)
    monkeypatch.setattr(jobs.positioning, "option_snapshot", fake_option_snapshot)
    monkeypatch.setattr(jobs.positioning, "participant_snapshot", fake_participant_snapshot)
    monkeypatch.setattr(jobs.positioning, "fii_dii_cash_snapshot", fake_fii_dii_cash_snapshot)
    monkeypatch.setattr(jobs.storage, "save_macro_snapshots", fake_save_macro_snapshots)
    monkeypatch.setattr(jobs.storage, "save_morning_brief", fake_save_morning_brief)
    monkeypatch.setattr(jobs.news_ai, "get_news_brief", fake_get_news_brief)

    result = asyncio.run(jobs.run_morning_job())

    assert result["verdict"] in ("Gap-up likely", "Flat open", "Gap-down likely")
    assert result["expected_low"] == 24500
    assert result["expected_high"] == 24800
    assert result["disclaimer"] == jobs.scoring.DISCLAIMER
    assert saved_brief["trade_date"] == result["trade_date"]
    assert saved_macro["session"] == "morning"
    assert result["components"]["previous_close"] == 24650
    assert result["components"]["gift"]["price"] == 24700.0
    assert result["components"]["levels"]["pdh"] == 24800
    assert result["components"]["structure"]["bias"] == "bullish"
    assert result["telegram_sent"] is False  # TELEGRAM_BOT_TOKEN/CHAT_ID unset in tests -> notify no-ops
    assert result["predicted_open"] == 24665.0  # gift.price (24700.0) - 35pt fair-value premium
    assert result["components"]["predicted_open_method"] == "gift_anchored"
    assert result["components"]["participants"]["FII"]["ratio"] == 54.55
    assert result["components"]["participants_trade_date"] == "2026-08-10"
    assert result["components"]["fii_dii_cash"]["fii_buy"] == 1000.0
    assert set(result["components"]["us_quotes"]) == {"us_dow", "us_nasdaq", "us_sp500"}
    assert set(result["components"]["asia_quotes"]) == {"asia_nikkei", "asia_hangseng", "asia_kospi", "asia_shanghai"}
    assert set(result["components"]["macro_quotes"]) == {"crude", "wti", "usdinr", "dxy", "us10y"}
    assert result["components"]["us_quotes"]["us_dow"]["price"] == 100.0
    assert result["headlines"] == [{"headline": "H1", "sentiment": "bullish", "reason": "r"}]
    assert result["news_sentiment"] == "Mildly bullish"


def test_run_morning_job_degrades_gracefully_when_everything_is_unavailable(monkeypatch):
    async def fake_none(*args, **kwargs):
        return None

    async def fake_save_macro_snapshots(session, quotes, captured_at=None):
        pass

    saved_brief = {}

    async def fake_save_morning_brief(brief):
        saved_brief.update(brief)

    monkeypatch.setattr(jobs.market_data, "fetch_gift_nifty", fake_none)
    monkeypatch.setattr(jobs.market_data, "fetch_quote", fake_none)
    monkeypatch.setattr(jobs.technicals, "compute_levels", fake_none)
    monkeypatch.setattr(jobs.technicals, "compute_structure", fake_none)
    monkeypatch.setattr(jobs.positioning, "compute_fii_positioning", fake_none)
    monkeypatch.setattr(jobs.positioning, "option_snapshot", lambda symbol: fake_none())

    async def fake_empty_participant_snapshot():
        return {"trade_date": None, "participants": {}}

    monkeypatch.setattr(jobs.positioning, "participant_snapshot", fake_empty_participant_snapshot)
    monkeypatch.setattr(jobs.positioning, "fii_dii_cash_snapshot", fake_none)
    monkeypatch.setattr(jobs.storage, "save_macro_snapshots", fake_save_macro_snapshots)
    monkeypatch.setattr(jobs.storage, "save_morning_brief", fake_save_morning_brief)

    async def failing_get_news_brief():
        raise RuntimeError("news source down")

    monkeypatch.setattr(jobs.news_ai, "get_news_brief", failing_get_news_brief)

    result = asyncio.run(jobs.run_morning_job())

    assert result["verdict"] == "Flat open"
    assert result["score"] == 0.0
    assert result["headlines"] == []
    assert result["news_sentiment"] is None
    assert result["expected_low"] is None
    assert result["expected_high"] is None
    assert result["predicted_open"] is None
    assert result["components"]["participants"] == {}
    assert result["components"]["fii_dii_cash"] is None
    assert result["disclaimer"] == jobs.scoring.DISCLAIMER
    assert result["telegram_sent"] is False
