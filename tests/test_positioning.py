import asyncio
import os

import positioning
from nse_client import _parse_participant_oi_csv
import datetime as dt

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _sample_df():
    with open(os.path.join(FIXTURES, "participant_oi_sample.csv")) as f:
        text = f.read()
    return _parse_participant_oi_csv(text, as_of=dt.date(2026, 8, 10))


def test_compute_fii_ratio_from_participant_oi_frame():
    df = _sample_df()
    # fixture: FII Future Index Long=300000, Short=250000
    assert positioning.compute_fii_ratio(df) == round(300000 / (300000 + 250000) * 100, 2)


def test_compute_fii_ratio_returns_none_when_fii_missing():
    df = _sample_df()
    df = df[df["Client Type"] != "FII"]
    assert positioning.compute_fii_ratio(df) is None


def test_compute_fii_ratio_returns_none_when_both_sides_zero():
    df = _sample_df().copy()
    df.loc[df["Client Type"] == "FII", "Future Index Long"] = 0
    df.loc[df["Client Type"] == "FII", "Future Index Short"] = 0
    assert positioning.compute_fii_ratio(df) is None


def test_fii_ratio_trend_rising():
    rows = [
        {"trade_date": "2026-08-10", "future_index_long": 60, "future_index_short": 40},  # 60%
        {"trade_date": "2026-08-07", "future_index_long": 50, "future_index_short": 50},  # 50%
    ]
    assert positioning.fii_ratio_trend(rows) == "rising"


def test_fii_ratio_trend_falling():
    rows = [
        {"trade_date": "2026-08-10", "future_index_long": 40, "future_index_short": 60},  # 40%
        {"trade_date": "2026-08-07", "future_index_long": 50, "future_index_short": 50},  # 50%
    ]
    assert positioning.fii_ratio_trend(rows) == "falling"


def test_fii_ratio_trend_flat_within_threshold():
    rows = [
        {"trade_date": "2026-08-10", "future_index_long": 51, "future_index_short": 49},  # 51%
        {"trade_date": "2026-08-07", "future_index_long": 50, "future_index_short": 50},  # 50%
    ]
    assert positioning.fii_ratio_trend(rows) == "flat"


def test_fii_ratio_trend_none_with_insufficient_data():
    assert positioning.fii_ratio_trend([]) is None
    assert positioning.fii_ratio_trend([{"trade_date": "2026-08-10", "future_index_long": 50, "future_index_short": 50}]) is None


def test_compute_fii_positioning_combines_latest_ratio_and_trend(monkeypatch):
    rows = [
        {"trade_date": "2026-08-10", "future_index_long": 60, "future_index_short": 40},
        {"trade_date": "2026-08-07", "future_index_long": 50, "future_index_short": 50},
    ]

    async def fake_get_fii_trend(days=5):
        return rows

    monkeypatch.setattr(positioning.storage, "get_fii_trend", fake_get_fii_trend)

    result = asyncio.run(positioning.compute_fii_positioning())
    assert result == {"ratio": 60.0, "trend": "rising"}


def test_compute_fii_positioning_returns_none_with_no_history(monkeypatch):
    async def fake_get_fii_trend(days=5):
        return []

    monkeypatch.setattr(positioning.storage, "get_fii_trend", fake_get_fii_trend)
    assert asyncio.run(positioning.compute_fii_positioning()) is None


def test_option_snapshot_reads_latest_pcr_snapshot_row(monkeypatch):
    async def fake_get_latest(symbol):
        return {"symbol": symbol, "max_call_oi_strike": 24700, "max_put_oi_strike": 24400,
                "pcr_oi": 1.15, "max_pain": 24500}

    monkeypatch.setattr(positioning.storage, "get_latest_pcr_snapshot", fake_get_latest)

    result = asyncio.run(positioning.option_snapshot("NIFTY"))
    assert result == {"max_call_oi_strike": 24700, "max_put_oi_strike": 24400, "pcr": 1.15, "max_pain": 24500}


def test_option_snapshot_defaults_to_all_none_when_no_row(monkeypatch):
    async def fake_get_latest(symbol):
        return None

    monkeypatch.setattr(positioning.storage, "get_latest_pcr_snapshot", fake_get_latest)

    result = asyncio.run(positioning.option_snapshot("NIFTY"))
    assert result == {"max_call_oi_strike": None, "max_put_oi_strike": None, "pcr": None, "max_pain": None}


def test_participant_snapshot_shapes_all_four_participants_with_trend(monkeypatch):
    history = [
        # latest day
        {"trade_date": "2026-08-10", "participant": "FII", "future_index_long": 300000, "future_index_short": 250000},
        {"trade_date": "2026-08-10", "participant": "DII", "future_index_long": 50000, "future_index_short": 20000},
        {"trade_date": "2026-08-10", "participant": "Pro", "future_index_long": 80000, "future_index_short": 90000},
        {"trade_date": "2026-08-10", "participant": "Client", "future_index_long": 123456, "future_index_short": 234567},
        # one earlier day, FII only -> ratio was 50% then, rising to ~54.5% now
        {"trade_date": "2026-08-07", "participant": "FII", "future_index_long": 50000, "future_index_short": 50000},
    ]

    async def fake_get_participant_history(days=5):
        return history

    monkeypatch.setattr(positioning.storage, "get_participant_history", fake_get_participant_history)

    result = asyncio.run(positioning.participant_snapshot())

    assert result["trade_date"] == "2026-08-10"
    assert result["participants"]["FII"] == {
        "long": 300000, "short": 250000, "ratio": round(300000 / 550000 * 100, 2), "trend": "rising",
    }
    assert result["participants"]["DII"]["trend"] is None  # only one day of history for DII
    assert set(result["participants"]) == {"FII", "DII", "Pro", "Client"}


def test_participant_snapshot_empty_when_nothing_persisted(monkeypatch):
    async def fake_get_participant_history(days=5):
        return []

    monkeypatch.setattr(positioning.storage, "get_participant_history", fake_get_participant_history)

    assert asyncio.run(positioning.participant_snapshot()) == {"trade_date": None, "participants": {}}


def test_fii_dii_cash_snapshot_passes_through_latest_row(monkeypatch):
    async def fake_get_latest_fii_dii_cash():
        return {"trade_date": "2026-08-10", "fii_buy": 1000.0, "fii_sell": 800.0, "dii_buy": 500.0, "dii_sell": 600.0}

    monkeypatch.setattr(positioning.storage, "get_latest_fii_dii_cash", fake_get_latest_fii_dii_cash)

    result = asyncio.run(positioning.fii_dii_cash_snapshot())
    assert result == {"trade_date": "2026-08-10", "fii_buy": 1000.0, "fii_sell": 800.0, "dii_buy": 500.0, "dii_sell": 600.0}


def test_fii_dii_cash_snapshot_none_when_nothing_persisted(monkeypatch):
    async def fake_get_latest_fii_dii_cash():
        return None

    monkeypatch.setattr(positioning.storage, "get_latest_fii_dii_cash", fake_get_latest_fii_dii_cash)

    assert asyncio.run(positioning.fii_dii_cash_snapshot()) is None


def test_build_positioning_outlook_bullish_when_fii_long_and_rising():
    outlook = positioning.build_positioning_outlook(
        {"trade_date": "2026-08-10", "participants": {
            "FII": {"ratio": 58.0, "trend": "rising"},
            "Client": {"ratio": 42.0},
        }},
        cash={"fii_buy": 1000, "fii_sell": 800, "dii_buy": 500, "dii_sell": 600},
        fii={"ratio": 58.0, "trend": "rising"},
    )
    assert outlook["bias"] == "bullish"
    assert "bullish" in outlook["headline"].lower() or "net long" in outlook["headline"].lower()
    assert "supportive" in outlook["what_to_expect"].lower() or "long" in outlook["what_to_expect"].lower()
    assert any("FII" in w for w in outlook["why"])
    assert any("Clients" in w for w in outlook["why"])
    assert outlook["scope"]


def test_build_positioning_outlook_bearish_when_fii_short_and_falling():
    outlook = positioning.build_positioning_outlook(
        {"trade_date": "2026-08-10", "participants": {"FII": {"ratio": 40.0, "trend": "falling"}}},
        fii={"ratio": 40.0, "trend": "falling"},
    )
    assert outlook["bias"] == "bearish"
    assert "soft" in outlook["what_to_expect"].lower() or "short" in outlook["what_to_expect"].lower()


def test_build_positioning_outlook_flags_client_fii_split():
    outlook = positioning.build_positioning_outlook(
        {"participants": {
            "FII": {"ratio": 40.0, "trend": "flat"},
            "Client": {"ratio": 60.0},
        }},
        fii={"ratio": 40.0, "trend": "flat"},
    )
    assert any("Classic split" in w or "Clients long-heavy" in w for w in outlook["why"])


def test_build_positioning_outlook_empty():
    outlook = positioning.build_positioning_outlook({"participants": {}})
    assert outlook["bias"] == "neutral"
    assert "unavailable" in outlook["headline"].lower()


def test_refresh_positioning_from_nse_persists_and_builds_outlook(monkeypatch):
    import pandas as pd

    df = _sample_df()

    def fake_fetch():
        return df, {"date": "10-Aug-2026", "fii_buy": 1, "fii_sell": 2, "dii_buy": 3, "dii_sell": 4}, None, None

    # Patch the inner fetch used by refresh — replace NSEClient path via to_thread body
    async def fake_to_thread(fn):
        return fake_fetch()

    saved = {}

    async def fake_save_participant_oi(frame):
        saved["oi"] = True

    async def fake_save_fii_dii_cash(data):
        saved["cash"] = data

    async def fake_participant_snapshot(days=5):
        return {
            "trade_date": "2026-08-10",
            "participants": {
                "FII": {"long": 300000, "short": 250000, "ratio": 54.55, "trend": "rising"},
                "Client": {"long": 1, "short": 2, "ratio": 33.33, "trend": None},
            },
        }

    async def fake_fii_dii_cash_snapshot():
        return {"trade_date": "2026-08-10", "fii_buy": 1, "fii_sell": 2, "dii_buy": 3, "dii_sell": 4}

    async def fake_compute_fii_positioning(days=5):
        return {"ratio": 54.55, "trend": "rising"}

    async def fake_get_fii_trend(days=30):
        return [{"trade_date": "2026-08-10", "future_index_long": 300000, "future_index_short": 250000}]

    monkeypatch.setattr(positioning.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(positioning.storage, "save_participant_oi", fake_save_participant_oi)
    monkeypatch.setattr(positioning.storage, "save_fii_dii_cash", fake_save_fii_dii_cash)
    monkeypatch.setattr(positioning, "participant_snapshot", fake_participant_snapshot)
    monkeypatch.setattr(positioning, "fii_dii_cash_snapshot", fake_fii_dii_cash_snapshot)
    monkeypatch.setattr(positioning, "compute_fii_positioning", fake_compute_fii_positioning)
    monkeypatch.setattr(positioning.storage, "get_fii_trend", fake_get_fii_trend)

    result = asyncio.run(positioning.refresh_positioning_from_nse(persist=True))
    assert result["available"] is True
    assert result["from_nse"] is True
    assert saved.get("oi") is True
    assert saved.get("cash")
    assert result["outlook"]["bias"] in ("bullish", "neutral", "bearish")
    assert result["participants"]["FII"]["ratio"] == 54.55
    assert "fetched_at" in result
