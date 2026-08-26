import asyncio

from fastapi.testclient import TestClient

import api.index as index_module
import index_engine
import storage

client = TestClient(index_module.app)


class FakeResponse:
    def __init__(self, json_data=None, status_code=200):
        self._json = json_data if json_data is not None else []
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            req = httpx.Request("POST", "https://fake.supabase.co/rest/v1/x")
            resp = httpx.Response(self.status_code, json=self._json, request=req)
            raise httpx.HTTPStatusError("err", request=req, response=resp)

    def json(self):
        return self._json


class FakeAsyncClient:
    def __init__(self):
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return FakeResponse([{"id": 1}])

    async def patch(self, url, **kwargs):
        self.calls.append(("PATCH", url, kwargs))
        return FakeResponse([{"id": 1}])

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return FakeResponse([])


def test_constituents_endpoint_returns_top20():
    r = client.get("/api/index-engine/constituents")
    assert r.status_code == 200
    body = r.json()
    assert len(body["constituents"]) == 20
    assert body["constituents"][0]["symbol"] == "HDFCBANK"
    assert "weight_pct" in body["constituents"][0]


def test_config_put_overlays_threshold_without_redeploy(monkeypatch):
    index_engine.reset_runtime_overlay()
    r = client.put("/api/index-engine/config", json={"early_warning": {"alert_score_threshold": 88}})
    assert r.status_code == 200
    assert r.json()["early_warning"]["alert_score_threshold"] == 88
    # file default still underneath other keys
    assert r.json()["early_warning"]["cooldown_minutes"] == 45
    index_engine.reset_runtime_overlay()


def test_snapshot_not_connected():
    index_module.upstox_token["access_token"] = None
    r = client.get("/api/index-engine/snapshot")
    assert r.status_code == 200
    assert r.json()["connected"] is False


def test_snapshot_keeps_attribution_and_early_warning_separate(monkeypatch):
    monkeypatch.setattr(index_module, "upstox_token", {"access_token": "tok", "obtained_at": "now"})

    async def fake_build(token, persist=True):
        assert token == "tok"
        return {
            "connected": True,
            "attribution": {"stocks": [{"symbol": "HDFCBANK", "contribution_pts": 12.0}]},
            "early_warning": {"disclaimer": "not a prediction", "stocks": [], "new_alerts": []},
        }

    monkeypatch.setattr(index_module.index_engine, "build_snapshot", fake_build)
    r = client.get("/api/index-engine/snapshot")
    body = r.json()
    assert "attribution" in body and "early_warning" in body
    assert body["early_warning"]["disclaimer"]
    assert body["attribution"]["stocks"][0]["contribution_pts"] == 12.0


def test_backtest_not_connected():
    index_module.upstox_token["access_token"] = None
    r = client.get("/api/index-engine/backtest")
    assert r.json()["connected"] is False


def test_save_index_engine_alert_inserts_and_stamps_id(monkeypatch):
    monkeypatch.setattr(storage, "SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setattr(storage, "SUPABASE_KEY", "fake")
    fake = FakeAsyncClient()

    async def fake_get_client():
        return fake
    monkeypatch.setattr(storage, "get_client", fake_get_client)

    alert = {
        "fired_at": "2026-08-26T10:00:00+05:30", "trade_date": "2026-08-26",
        "symbol": "HDFCBANK", "name": "HDFC Bank", "weight_pct": 13, "score": 81,
        "reasons": ["volume 3× average"], "message": "msg", "potential_index_pts": 20,
        "features": {}, "index_ltp_at_fire": 25000, "stock_ltp_at_fire": 1650,
    }
    saved = asyncio.run(storage.save_index_engine_alert(alert))
    assert saved["id"] == 1
    assert alert["id"] == 1
    method, url, kwargs = fake.calls[0]
    assert method == "POST"
    assert url.endswith("/rest/v1/index_engine_alerts")
    assert kwargs["json"]["symbol"] == "HDFCBANK"
