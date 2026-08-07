import asyncio
import datetime as dt
import json

from fastapi.testclient import TestClient

import api.index as index_module
from api.index import push_pcr_snapshot, get_pcr_history, fetch_and_record_pcr, claim_strike_persist_slot, IST

client = TestClient(index_module.app)


def test_pcr_history_round_trips_through_fake_redis(fake_redis):
    asyncio.run(push_pcr_snapshot("NIFTY", "2026-08-06", {"t": "09:15", "pcrOi": 1.1}))
    asyncio.run(push_pcr_snapshot("NIFTY", "2026-08-06", {"t": "09:20", "pcrOi": 1.2}))

    history = asyncio.run(get_pcr_history("NIFTY", "2026-08-06"))
    assert history == [{"t": "09:15", "pcrOi": 1.1}, {"t": "09:20", "pcrOi": 1.2}]


def test_pcr_history_is_isolated_per_symbol_and_day(fake_redis):
    asyncio.run(push_pcr_snapshot("NIFTY", "2026-08-06", {"t": "09:15", "pcrOi": 1.1}))
    asyncio.run(push_pcr_snapshot("BANKNIFTY", "2026-08-06", {"t": "09:15", "pcrOi": 0.9}))
    asyncio.run(push_pcr_snapshot("NIFTY", "2026-08-07", {"t": "09:15", "pcrOi": 1.5}))

    assert asyncio.run(get_pcr_history("NIFTY", "2026-08-06")) == [{"t": "09:15", "pcrOi": 1.1}]
    assert asyncio.run(get_pcr_history("BANKNIFTY", "2026-08-06")) == [{"t": "09:15", "pcrOi": 0.9}]
    assert asyncio.run(get_pcr_history("NIFTY", "2026-08-07")) == [{"t": "09:15", "pcrOi": 1.5}]


def test_fetch_and_record_pcr_persists_whole_chain_and_per_strike(monkeypatch, fake_redis):
    async def fake_get_nearest_expiry(symbol):
        return "11-Aug-2026"

    async def fake_fetch_option_chain_for_expiry(symbol, expiry):
        return {
            "records": {
                "expiryDates": [expiry],
                "underlyingValue": 24500,
                "data": [
                    {
                        "strikePrice": 24500, "expiryDates": expiry,
                        "CE": {"openInterest": 100, "totalTradedVolume": 10, "changeinOpenInterest": 0, "lastPrice": 120},
                        "PE": {"openInterest": 200, "totalTradedVolume": 20, "changeinOpenInterest": 0, "lastPrice": 60},
                    },
                ],
            },
        }

    monkeypatch.setattr(index_module, "get_nearest_expiry", fake_get_nearest_expiry)
    monkeypatch.setattr(index_module, "fetch_option_chain_for_expiry", fake_fetch_option_chain_for_expiry)

    now = dt.datetime(2026, 8, 6, 9, 20, tzinfo=IST)
    asyncio.run(fetch_and_record_pcr("NIFTY", now, persist_strikes=True))

    assert "pcr:NIFTY:2026-08-06" in fake_redis
    assert "strikepcr:NIFTY:24500:2026-08-06" in fake_redis
    strike_snap = json.loads(fake_redis["strikepcr:NIFTY:24500:2026-08-06"][0])
    assert strike_snap["pcr"] == 2.0  # 200 put OI / 100 call OI
    assert strike_snap["t"] == "09:20"


def test_fetch_and_record_pcr_skips_per_strike_by_default(monkeypatch, fake_redis):
    async def fake_get_nearest_expiry(symbol):
        return "11-Aug-2026"

    async def fake_fetch_option_chain_for_expiry(symbol, expiry):
        return {
            "records": {
                "expiryDates": [expiry], "underlyingValue": 24500,
                "data": [{
                    "strikePrice": 24500, "expiryDates": expiry,
                    "CE": {"openInterest": 100, "totalTradedVolume": 10, "changeinOpenInterest": 0, "lastPrice": 120},
                    "PE": {"openInterest": 200, "totalTradedVolume": 20, "changeinOpenInterest": 0, "lastPrice": 60},
                }],
            },
        }

    monkeypatch.setattr(index_module, "get_nearest_expiry", fake_get_nearest_expiry)
    monkeypatch.setattr(index_module, "fetch_option_chain_for_expiry", fake_fetch_option_chain_for_expiry)

    now = dt.datetime(2026, 8, 6, 9, 20, tzinfo=IST)
    asyncio.run(fetch_and_record_pcr("NIFTY", now))  # persist_strikes left at its default (False)

    assert "pcr:NIFTY:2026-08-06" in fake_redis
    assert "strikepcr:NIFTY:24500:2026-08-06" not in fake_redis


def test_claim_strike_persist_slot_throttles_to_once_per_interval(fake_redis):
    now = dt.datetime(2026, 8, 6, 9, 20, tzinfo=IST)
    soon_after = now + dt.timedelta(minutes=2)
    much_later = now + dt.timedelta(minutes=6)

    assert asyncio.run(claim_strike_persist_slot("NIFTY", "2026-08-06", now)) is True
    assert asyncio.run(claim_strike_persist_slot("NIFTY", "2026-08-06", soon_after)) is False
    assert asyncio.run(claim_strike_persist_slot("NIFTY", "2026-08-06", much_later)) is True


def test_claim_strike_persist_slot_is_isolated_per_symbol(fake_redis):
    now = dt.datetime(2026, 8, 6, 9, 20, tzinfo=IST)
    assert asyncio.run(claim_strike_persist_slot("NIFTY", "2026-08-06", now)) is True
    # a different symbol at the same instant must get its own slot
    assert asyncio.run(claim_strike_persist_slot("BANKNIFTY", "2026-08-06", now)) is True


def test_optionchain_today_persists_per_strike_for_visitor_traffic(monkeypatch, fake_redis):
    async def fake_get_nearest_expiry(symbol):
        return "11-Aug-2026"

    async def fake_fetch_option_chain_for_expiry(symbol, expiry):
        return {
            "records": {
                "expiryDates": [expiry], "underlyingValue": 24500,
                "data": [{
                    "strikePrice": 24500, "expiryDates": expiry,
                    "CE": {"openInterest": 100, "totalTradedVolume": 10, "changeinOpenInterest": 0, "lastPrice": 120},
                    "PE": {"openInterest": 200, "totalTradedVolume": 20, "changeinOpenInterest": 0, "lastPrice": 60},
                }],
            },
        }

    monkeypatch.setattr(index_module, "get_nearest_expiry", fake_get_nearest_expiry)
    monkeypatch.setattr(index_module, "fetch_option_chain_for_expiry", fake_fetch_option_chain_for_expiry)

    r = client.get("/api/optionchain/today", params={"symbol": "NIFTY"})
    assert r.status_code == 200

    day = dt.datetime.now(IST).date().isoformat()
    assert f"strikepcr:NIFTY:24500:{day}" in fake_redis
    strike_snap = json.loads(fake_redis[f"strikepcr:NIFTY:24500:{day}"][0])
    assert strike_snap["pcr"] == 2.0


def test_optionchain_today_skips_persist_for_non_nearest_expiry(monkeypatch, fake_redis):
    async def fake_get_nearest_expiry(symbol):
        return "11-Aug-2026"  # nearest is next week, but the request below asks for a later one

    async def fake_fetch_option_chain_for_expiry(symbol, expiry):
        return {
            "records": {
                "expiryDates": [expiry], "underlyingValue": 24500,
                "data": [{
                    "strikePrice": 24500, "expiryDates": expiry,
                    "CE": {"openInterest": 100, "totalTradedVolume": 10, "changeinOpenInterest": 0, "lastPrice": 120},
                    "PE": {"openInterest": 200, "totalTradedVolume": 20, "changeinOpenInterest": 0, "lastPrice": 60},
                }],
            },
        }

    monkeypatch.setattr(index_module, "get_nearest_expiry", fake_get_nearest_expiry)
    monkeypatch.setattr(index_module, "fetch_option_chain_for_expiry", fake_fetch_option_chain_for_expiry)

    r = client.get("/api/optionchain/today", params={"symbol": "NIFTY", "expiry": "18-Aug-2026"})
    assert r.status_code == 200

    day = dt.datetime.now(IST).date().isoformat()
    assert f"strikepcr:NIFTY:24500:{day}" not in fake_redis
