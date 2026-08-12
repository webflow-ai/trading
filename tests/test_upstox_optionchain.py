from fastapi.testclient import TestClient

import api.index as index_module

client = TestClient(index_module.app)


async def fake_get_nearest_expiry(symbol):
    return "18-Aug-2026"


class FakeUpstoxResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json


class FakeUpstoxAsyncClient:
    """Minimal async-context-manager stand-in for httpx.AsyncClient, since
    upstox_optionchain instantiates its own client inline (matching the
    existing upstox_callback style) rather than taking one as a parameter."""

    def __init__(self, response):
        self._response = response
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._response


def _reset_upstox_state(monkeypatch):
    monkeypatch.setattr(index_module, "upstox_token", {"access_token": None, "obtained_at": None})


def test_upstox_optionchain_not_connected(monkeypatch):
    _reset_upstox_state(monkeypatch)

    r = client.get("/api/upstox/optionchain", params={"symbol": "NIFTY"})
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is False
    assert body["rows"] == []
    assert "not connected" in body["error"]


def test_upstox_optionchain_unsupported_symbol(monkeypatch):
    _reset_upstox_state(monkeypatch)

    r = client.get("/api/upstox/optionchain", params={"symbol": "SENSEX"})
    body = r.json()
    assert body["connected"] is False
    assert "unsupported symbol" in body["error"]


def test_upstox_optionchain_parses_rows_when_connected(monkeypatch):
    monkeypatch.setattr(index_module, "upstox_token", {"access_token": "tok123", "obtained_at": "now"})
    monkeypatch.setattr(index_module, "get_nearest_expiry", fake_get_nearest_expiry)

    fake_response = FakeUpstoxResponse(200, {
        "data": [
            {
                "strike_price": 24500,
                "underlying_spot_price": 24471.7,
                "call_options": {"market_data": {"ltp": 71.55, "oi": 97312, "volume": 500, "oi_change": 10}},
                "put_options": {"market_data": {"ltp": 28.25, "oi": 167612, "volume": 300, "oi_change": -5}},
            },
        ],
    })
    fake_client = FakeUpstoxAsyncClient(fake_response)
    monkeypatch.setattr(index_module.httpx, "AsyncClient", lambda **kw: fake_client)

    r = client.get("/api/upstox/optionchain", params={"symbol": "NIFTY"})
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is True
    assert body["expiry"] == "2026-08-18"
    assert body["spot"] == 24471.7
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["strike"] == 24500
    assert row["ceLtp"] == 71.55
    assert row["ceOi"] == 97312
    assert row["peLtp"] == 28.25
    assert row["peOi"] == 167612
    # confirms the Bearer token from upstox_token actually made it onto the request
    assert fake_client.calls[0][1]["headers"]["Authorization"] == "Bearer tok123"


def test_upstox_optionchain_401_clears_token_and_reports_not_connected(monkeypatch):
    monkeypatch.setattr(index_module, "upstox_token", {"access_token": "stale-token", "obtained_at": "now"})
    monkeypatch.setattr(index_module, "get_nearest_expiry", fake_get_nearest_expiry)

    fake_response = FakeUpstoxResponse(401, {}, "unauthorized")
    monkeypatch.setattr(index_module.httpx, "AsyncClient", lambda **kw: FakeUpstoxAsyncClient(fake_response))

    r = client.get("/api/upstox/optionchain", params={"symbol": "NIFTY"})
    body = r.json()
    assert body["connected"] is False
    assert "expired" in body["error"]
    assert index_module.upstox_token["access_token"] is None


def test_upstox_optionchain_non_200_reports_error_without_crashing(monkeypatch):
    monkeypatch.setattr(index_module, "upstox_token", {"access_token": "tok123", "obtained_at": "now"})
    monkeypatch.setattr(index_module, "get_nearest_expiry", fake_get_nearest_expiry)

    fake_response = FakeUpstoxResponse(500, {}, "internal error")
    monkeypatch.setattr(index_module.httpx, "AsyncClient", lambda **kw: FakeUpstoxAsyncClient(fake_response))

    r = client.get("/api/upstox/optionchain", params={"symbol": "NIFTY"})
    assert r.status_code == 200  # never a 500 to the caller — degrades to an error field
    body = r.json()
    assert body["connected"] is True
    assert body["rows"] == []
    assert "500" in body["error"]


def test_nse_expiry_to_iso_converts_format():
    assert index_module._nse_expiry_to_iso("18-Aug-2026") == "2026-08-18"


def test_nse_expiry_to_iso_passes_through_unrecognized_format():
    assert index_module._nse_expiry_to_iso("2026-08-18") == "2026-08-18"
