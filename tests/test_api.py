from fastapi.testclient import TestClient

import api.index as index_module

client = TestClient(index_module.app)


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
                    "CE": {"openInterest": 100, "totalTradedVolume": 10, "changeinOpenInterest": 5, "lastPrice": 120},
                    "PE": {"openInterest": 200, "totalTradedVolume": 20, "changeinOpenInterest": -5, "lastPrice": 60},
                },
            ],
        },
    }


def test_health_ok():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_pcr_today_fetches_and_returns_current_reading(monkeypatch):
    monkeypatch.setattr(index_module, "get_nearest_expiry", fake_get_nearest_expiry)
    monkeypatch.setattr(index_module, "fetch_option_chain_for_expiry", fake_fetch_option_chain_for_expiry)

    r = client.get("/api/pcr/today", params={"symbol": "NIFTY"})
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "NIFTY"
    assert body["expiry"] == "11-Aug-2026"
    assert body["pcrOi"] == 2.0  # 200 put OI / 100 call OI
    assert "snapshots" not in body  # no server-side history array anymore — see /api/pcr/history


def test_pcr_today_reuses_cache_within_the_reuse_window(monkeypatch):
    calls = {"n": 0}

    async def counting_fetch(symbol, expiry):
        calls["n"] += 1
        return await fake_fetch_option_chain_for_expiry(symbol, expiry)

    monkeypatch.setattr(index_module, "get_nearest_expiry", fake_get_nearest_expiry)
    monkeypatch.setattr(index_module, "fetch_option_chain_for_expiry", counting_fetch)

    client.get("/api/pcr/today", params={"symbol": "NIFTY"})
    client.get("/api/pcr/today", params={"symbol": "NIFTY"})
    assert calls["n"] == 1  # second call served from the in-memory cache, no new fetch


def test_optionchain_today_returns_rows_and_spot(monkeypatch):
    monkeypatch.setattr(index_module, "get_nearest_expiry", fake_get_nearest_expiry)
    monkeypatch.setattr(index_module, "fetch_option_chain_for_expiry", fake_fetch_option_chain_for_expiry)

    r = client.get("/api/optionchain/today", params={"symbol": "NIFTY", "n": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["spot"] == 24500
    assert len(body["rows"]) == 1
    assert body["rows"][0]["strike"] == 24500
    assert body["rows"][0]["ceOi"] == 100
    assert body["rows"][0]["peOi"] == 200


def test_optionchain_today_upstream_failure_falls_back_gracefully(monkeypatch):
    async def broken_fetch(symbol, expiry):
        raise RuntimeError("NSE unreachable")

    monkeypatch.setattr(index_module, "get_nearest_expiry", fake_get_nearest_expiry)
    monkeypatch.setattr(index_module, "fetch_option_chain_for_expiry", broken_fetch)

    r = client.get("/api/optionchain/today", params={"symbol": "NIFTY"})
    assert r.status_code == 200  # never a 500 — degrades to an empty-but-valid response
    body = r.json()
    assert body["rows"] == []


def test_candles_unknown_symbol_returns_empty_list():
    r = client.get("/api/candles", params={"symbol": "NOTASYMBOL"})
    assert r.status_code == 200
    assert r.json() == {"symbol": "NOTASYMBOL", "candles": []}


def test_optionchain_history_without_redis_returns_empty_not_error():
    r = client.get("/api/optionchain/history", params={"symbol": "NIFTY", "strike": 24500})
    assert r.status_code == 200
    assert r.json()["snapshots"] == []
