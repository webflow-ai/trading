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
