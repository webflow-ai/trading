import asyncio
import datetime as dt

import technicals
from market_data import IST


def mc(i, h, l, c):
    return {
        "dt": dt.datetime(2026, 8, 1, 9, 0, tzinfo=IST) + dt.timedelta(minutes=15 * i),
        "open": c, "high": h, "low": l, "close": c,
    }


def test_compute_levels_uses_the_last_candle_as_previous_day(monkeypatch):
    candles = [
        mc(0, 24350, 24100, 24300),
        mc(1, 24800, 24500, 24650),  # most recently finished session
    ]

    async def fake_fetch(client, symbol, interval, rng):
        return candles

    monkeypatch.setattr(technicals, "fetch_ohlc_candles", fake_fetch)

    result = asyncio.run(technicals.compute_levels())

    assert result["pdh"] == 24800
    assert result["pdl"] == 24500
    assert result["previous_close"] == 24650
    assert result["previous_day_range"] == 300
    assert result["close_position_pct"] == round((24650 - 24500) / 300 * 100, 2)


def test_compute_levels_returns_none_on_empty_candles(monkeypatch):
    async def fake_fetch(client, symbol, interval, rng):
        return []

    monkeypatch.setattr(technicals, "fetch_ohlc_candles", fake_fetch)
    assert asyncio.run(technicals.compute_levels()) is None


def test_compute_levels_returns_none_on_fetch_error(monkeypatch):
    async def fake_fetch(client, symbol, interval, rng):
        raise RuntimeError("boom")

    monkeypatch.setattr(technicals, "fetch_ohlc_candles", fake_fetch)
    assert asyncio.run(technicals.compute_levels()) is None


def test_detect_structure_reports_neutral_with_no_swings():
    assert technicals.detect_structure([]) == {"bias": "neutral", "last_event": None}
    assert technicals.detect_structure([mc(0, 100, 90, 95)] * 3) == {"bias": "neutral", "last_event": None}


def test_detect_structure_bos_bullish_on_break_above_swing_high_from_neutral():
    candles = [
        mc(0, 100, 90, 97),
        mc(1, 105, 95, 101),
        mc(2, 110, 98, 107),  # swing high (110)
        mc(3, 104, 96, 99),
        mc(4, 103, 94, 98),
        mc(5, 108, 97, 111),  # closes above 110 -> BOS_bullish
    ]
    assert technicals.detect_structure(candles) == {"bias": "bullish", "last_event": "BOS_bullish"}


def test_detect_structure_choch_bearish_on_break_below_swing_low_after_bullish_bos():
    candles = [
        mc(0, 100, 90, 97),
        mc(1, 105, 95, 101),
        mc(2, 110, 98, 107),
        mc(3, 104, 96, 99),
        mc(4, 103, 94, 98),
        mc(5, 108, 97, 111),   # BOS_bullish, bias -> bullish
        mc(6, 112, 105, 108),
        mc(7, 109, 100, 103),
        mc(8, 107, 94, 98),
        mc(9, 110, 101, 105),
        mc(10, 108, 102, 104),
        mc(11, 106, 93, 93),   # closes below a swing low while bias is bullish -> CHoCH_bearish
    ]
    assert technicals.detect_structure(candles) == {"bias": "bearish", "last_event": "CHoCH_bearish"}


def test_compute_structure_returns_none_on_empty_candles(monkeypatch):
    async def fake_fetch(client, symbol, interval, rng):
        return []

    monkeypatch.setattr(technicals, "fetch_ohlc_candles", fake_fetch)
    assert asyncio.run(technicals.compute_structure()) is None


def test_compute_structure_delegates_to_detect_structure(monkeypatch):
    candles = [
        mc(0, 100, 90, 97), mc(1, 105, 95, 101), mc(2, 110, 98, 107),
        mc(3, 104, 96, 99), mc(4, 103, 94, 98), mc(5, 108, 97, 111),
    ]

    async def fake_fetch(client, symbol, interval, rng):
        return candles

    monkeypatch.setattr(technicals, "fetch_ohlc_candles", fake_fetch)
    assert asyncio.run(technicals.compute_structure()) == {"bias": "bullish", "last_event": "BOS_bullish"}
