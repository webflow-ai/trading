import datetime as dt

from fastapi.testclient import TestClient

import main as main_module
from main import app

client = TestClient(app)


def test_health_ok():
    r = client.get("/api/premarket/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_jobs_endpoints_require_api_key(monkeypatch):
    monkeypatch.setattr(main_module, "JOB_API_KEY", "secret123")
    r = client.post("/api/premarket/jobs/evening")
    assert r.status_code == 401


def test_jobs_endpoints_500_when_server_has_no_key_configured(monkeypatch):
    monkeypatch.setattr(main_module, "JOB_API_KEY", None)
    r = client.post("/api/premarket/jobs/evening", headers={"X-API-Key": "anything"})
    assert r.status_code == 500


def test_evening_job_runs_with_correct_api_key(monkeypatch):
    monkeypatch.setattr(main_module, "JOB_API_KEY", "secret123")

    async def fake_run_evening_job():
        return {"trade_date": "2026-08-10", "sources": {}}

    monkeypatch.setattr(main_module.jobs, "run_evening_job", fake_run_evening_job)

    r = client.post("/api/premarket/jobs/evening", headers={"X-API-Key": "secret123"})
    assert r.status_code == 200
    assert r.json()["trade_date"] == "2026-08-10"


def test_morning_job_passes_through_event_day_flag(monkeypatch):
    monkeypatch.setattr(main_module, "JOB_API_KEY", "secret123")
    captured = {}

    async def fake_run_morning_job(is_event_day=False):
        captured["is_event_day"] = is_event_day
        return {"trade_date": "2026-08-10", "score": 0, "verdict": "Flat open"}

    monkeypatch.setattr(main_module.jobs, "run_morning_job", fake_run_morning_job)

    r = client.post("/api/premarket/jobs/morning", params={"is_event_day": "true"},
                     headers={"X-API-Key": "secret123"})
    assert r.status_code == 200
    assert captured["is_event_day"] is True


def test_brief_today_returns_placeholder_when_nothing_generated_yet(monkeypatch):
    async def fake_get_brief_history(days=1):
        return []

    monkeypatch.setattr(main_module.storage, "get_brief_history", fake_get_brief_history)

    r = client.get("/api/premarket/brief/today")
    assert r.status_code == 200
    assert r.json()["note"] == "no brief generated yet"


def test_brief_today_returns_latest_row(monkeypatch):
    async def fake_get_brief_history(days=1):
        return [{"trade_date": "2026-08-10", "score": 40.0, "verdict": "Gap-up likely"}]

    monkeypatch.setattr(main_module.storage, "get_brief_history", fake_get_brief_history)

    r = client.get("/api/premarket/brief/today")
    assert r.status_code == 200
    assert r.json()["verdict"] == "Gap-up likely"


def test_classify_direction_dead_zone_and_signs():
    assert main_module._classify_direction(100.0, 100.05) == "flat"  # +0.05%, within the dead zone
    assert main_module._classify_direction(100.0, 100.5) == "up"
    assert main_module._classify_direction(100.0, 99.5) == "down"
    assert main_module._classify_direction(None, 100.0) is None


def test_verdict_direction_maps_all_three_verdicts():
    assert main_module._verdict_direction("Gap-up likely") == "up"
    assert main_module._verdict_direction("Gap-down likely") == "down"
    assert main_module._verdict_direction("Flat open") == "flat"
    assert main_module._verdict_direction("something else") is None


def test_brief_history_computes_hit_rate_against_actual_next_day_open(monkeypatch):
    async def fake_get_brief_history(days=30):
        return [
            {"trade_date": "2026-08-06", "verdict": "Gap-up likely", "components": {"previous_close": 24500}},
            {"trade_date": "2026-08-07", "verdict": "Gap-down likely", "components": {"previous_close": 24500}},
        ]

    async def fake_actual_open_after(trade_date, client):
        return {"2026-08-06": 24700.0, "2026-08-07": 24650.0}[trade_date]  # both actually went up

    monkeypatch.setattr(main_module.storage, "get_brief_history", fake_get_brief_history)
    monkeypatch.setattr(main_module, "_actual_open_after", fake_actual_open_after)

    r = client.get("/api/premarket/brief/history", params={"days": 30})
    assert r.status_code == 200
    body = r.json()
    assert body["hit_rate_pct"] == 50.0  # the gap-up call hit, the gap-down call missed
    assert body["briefs"][0]["hit"] is True
    assert body["briefs"][1]["hit"] is False


# ---------------- top-10 movers ----------------

def test_movers_snapshot_endpoint_persists_and_defaults_trade_date(monkeypatch):
    captured = {}

    async def fake_save_movers_snapshot(snapshot):
        captured.update(snapshot)

    monkeypatch.setattr(main_module.storage, "save_movers_snapshot", fake_save_movers_snapshot)

    r = client.post("/api/premarket/movers/snapshot", json={
        "implied_move_pct": 0.22, "verdict": "Gap-up likely", "stocks": [{"symbol": "RELIANCE"}],
    })
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert captured["implied_move_pct"] == 0.22
    assert captured["verdict"] == "Gap-up likely"
    assert captured["trade_date"]  # defaulted to today's IST date, not passed explicitly


def test_movers_accuracy_computes_hit_rate_against_actual_next_day_open(monkeypatch):
    async def fake_get_movers_snapshots(days=30):
        # newest-first, two snapshots for 2026-08-06 (latest should win) plus one for 2026-08-07
        return [
            {"trade_date": "2026-08-07", "verdict": "Gap-down likely", "implied_move_pct": -0.3, "captured_at": "t3"},
            {"trade_date": "2026-08-06", "verdict": "Gap-up likely", "implied_move_pct": 0.4, "captured_at": "t2"},
            {"trade_date": "2026-08-06", "verdict": "Flat open", "implied_move_pct": 0.05, "captured_at": "t1"},
        ]

    async def fake_nifty_close_on(trade_date, client):
        return {"2026-08-06": 24500.0, "2026-08-07": 24500.0}[trade_date]

    async def fake_actual_open_after(trade_date, client):
        return {"2026-08-06": 24700.0, "2026-08-07": 24650.0}[trade_date]  # both actually went up

    monkeypatch.setattr(main_module.storage, "get_movers_snapshots", fake_get_movers_snapshots)
    monkeypatch.setattr(main_module, "_nifty_close_on", fake_nifty_close_on)
    monkeypatch.setattr(main_module, "_actual_open_after", fake_actual_open_after)

    r = client.get("/api/premarket/movers/accuracy", params={"days": 30})
    assert r.status_code == 200
    body = r.json()
    assert len(body["snapshots"]) == 2  # deduped to one row per trade_date
    by_date = {s["trade_date"]: s for s in body["snapshots"]}
    assert by_date["2026-08-06"]["verdict"] == "Gap-up likely"  # the newest-first row for that date, not the older one
    assert by_date["2026-08-06"]["hit"] is True
    assert by_date["2026-08-07"]["hit"] is False  # predicted down, actually went up
    assert body["hit_rate_pct"] == 50.0


def test_fii_trend_endpoint_passes_through_days(monkeypatch):
    async def fake_get_fii_trend(days=30):
        return [{"trade_date": "2026-08-10", "future_index_long": 1, "future_index_short": 1}]

    monkeypatch.setattr(main_module.storage, "get_fii_trend", fake_get_fii_trend)

    r = client.get("/api/premarket/positioning/fii-trend", params={"days": 7})
    assert r.status_code == 200
    assert r.json()["days"] == 7
    assert len(r.json()["rows"]) == 1


# ---------------- paper trades ----------------

def test_create_paper_trade_endpoint_success(monkeypatch):
    captured = {}

    async def fake_open_trade(**kwargs):
        captured.update(kwargs)
        return {"id": 1, "status": "open", **kwargs}

    monkeypatch.setattr(main_module.paper_trading, "open_trade", fake_open_trade)

    r = client.post("/api/premarket/paper-trades", json={
        "strike": 24500, "option_type": "CE", "action": "BUY", "entry_price": 120.5, "lots": 2,
    })
    assert r.status_code == 200
    assert r.json()["id"] == 1
    assert captured["strike"] == 24500
    assert captured["lots"] == 2


def test_create_paper_trade_endpoint_rejects_missing_fields():
    r = client.post("/api/premarket/paper-trades", json={"strike": 24500})
    assert r.status_code == 400


def test_create_paper_trade_endpoint_rejects_bad_option_type():
    r = client.post("/api/premarket/paper-trades", json={
        "strike": 24500, "option_type": "XX", "action": "BUY", "entry_price": 100,
    })
    assert r.status_code == 400


def test_create_paper_trade_endpoint_rejects_bad_action():
    r = client.post("/api/premarket/paper-trades", json={
        "strike": 24500, "option_type": "CE", "action": "HOLD", "entry_price": 100,
    })
    assert r.status_code == 400


def test_create_paper_trade_endpoint_passes_through_stop_loss_and_target(monkeypatch):
    captured = {}

    async def fake_open_trade(**kwargs):
        captured.update(kwargs)
        return {"id": 1, "status": "open", **kwargs}

    monkeypatch.setattr(main_module.paper_trading, "open_trade", fake_open_trade)

    r = client.post("/api/premarket/paper-trades", json={
        "strike": 24500, "option_type": "CE", "action": "BUY", "entry_price": 120.5,
        "stop_loss": 90.0, "target_price": 160.0,
    })
    assert r.status_code == 200
    assert captured["stop_loss"] == 90.0
    assert captured["target_price"] == 160.0


def test_close_paper_trade_endpoint_success(monkeypatch):
    async def fake_close_trade(trade_id, exit_price, reason="manual"):
        assert trade_id == 7
        assert exit_price == 125.0
        assert reason == "manual"
        return {"id": 7, "status": "closed", "pnl": 1875.0}

    monkeypatch.setattr(main_module.paper_trading, "close_trade", fake_close_trade)

    r = client.post("/api/premarket/paper-trades/7/close", json={"exit_price": 125.0})
    assert r.status_code == 200
    assert r.json()["pnl"] == 1875.0


def test_close_paper_trade_endpoint_requires_exit_price():
    r = client.post("/api/premarket/paper-trades/7/close", json={})
    assert r.status_code == 400


def test_close_paper_trade_endpoint_rejects_bad_reason():
    r = client.post("/api/premarket/paper-trades/7/close", json={"exit_price": 100, "reason": "vibes"})
    assert r.status_code == 400


def test_close_paper_trade_endpoint_passes_through_stop_loss_reason(monkeypatch):
    captured = {}

    async def fake_close_trade(trade_id, exit_price, reason="manual"):
        captured["reason"] = reason
        return {"id": trade_id, "status": "closed", "pnl": -500.0, "exit_reason": reason}

    monkeypatch.setattr(main_module.paper_trading, "close_trade", fake_close_trade)

    r = client.post("/api/premarket/paper-trades/7/close", json={"exit_price": 90.0, "reason": "stop_loss"})
    assert r.status_code == 200
    assert captured["reason"] == "stop_loss"


def test_close_paper_trade_endpoint_404_when_not_found(monkeypatch):
    async def fake_close_trade(trade_id, exit_price, reason="manual"):
        return None

    monkeypatch.setattr(main_module.paper_trading, "close_trade", fake_close_trade)

    r = client.post("/api/premarket/paper-trades/999/close", json={"exit_price": 100})
    assert r.status_code == 404


def test_list_paper_trades_endpoint_returns_trades_and_summary(monkeypatch):
    async def fake_get_paper_trades(status=None, days=90):
        return [{"id": 1, "status": "closed", "pnl": 500.0}, {"id": 2, "status": "open"}]

    monkeypatch.setattr(main_module.storage, "get_paper_trades", fake_get_paper_trades)

    r = client.get("/api/premarket/paper-trades")
    assert r.status_code == 200
    body = r.json()
    assert len(body["trades"]) == 2
    assert body["summary"]["total_pnl"] == 500.0
    assert body["summary"]["open_count"] == 1
    assert body["weekly"] == []  # fake trades have no exit_time to group by
