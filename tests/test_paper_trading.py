import asyncio

import paper_trading


# ---------------- compute_pnl ----------------

def test_compute_pnl_buy_profits_when_exit_above_entry():
    assert paper_trading.compute_pnl("BUY", entry_price=100, exit_price=125, lot_size=75, lots=1) == 1875.0


def test_compute_pnl_buy_loses_when_exit_below_entry():
    assert paper_trading.compute_pnl("BUY", entry_price=100, exit_price=80, lot_size=75, lots=1) == -1500.0


def test_compute_pnl_sell_profits_when_exit_below_entry():
    assert paper_trading.compute_pnl("SELL", entry_price=100, exit_price=80, lot_size=75, lots=1) == 1500.0


def test_compute_pnl_sell_loses_when_exit_above_entry():
    assert paper_trading.compute_pnl("SELL", entry_price=100, exit_price=125, lot_size=75, lots=1) == -1875.0


def test_compute_pnl_scales_with_lots():
    assert paper_trading.compute_pnl("BUY", entry_price=100, exit_price=110, lot_size=75, lots=3) == 2250.0


# ---------------- open_trade / close_trade ----------------

def test_open_trade_builds_correct_row_and_persists(monkeypatch):
    captured = {}

    async def fake_create_paper_trade(row):
        captured.update(row)
        return {**row, "id": 1}

    monkeypatch.setattr(paper_trading.storage, "create_paper_trade", fake_create_paper_trade)

    result = asyncio.run(paper_trading.open_trade(
        strike=24500, option_type="CE", action="BUY", entry_price=120.5, lots=2, lot_size=75,
        trade_date="2026-08-12", notes="test entry",
    ))

    assert result["id"] == 1
    assert captured["trade_date"] == "2026-08-12"
    assert captured["symbol"] == "NIFTY"
    assert captured["strike"] == 24500
    assert captured["option_type"] == "CE"
    assert captured["action"] == "BUY"
    assert captured["lots"] == 2
    assert captured["lot_size"] == 75
    assert captured["entry_price"] == 120.5
    assert captured["status"] == "open"
    assert captured["notes"] == "test entry"
    assert captured["entry_time"] is not None
    assert captured["stop_loss"] is None
    assert captured["target_price"] is None


def test_open_trade_defaults_trade_date_to_today_ist(monkeypatch):
    captured = {}

    async def fake_create_paper_trade(row):
        captured.update(row)
        return row

    monkeypatch.setattr(paper_trading.storage, "create_paper_trade", fake_create_paper_trade)

    asyncio.run(paper_trading.open_trade(strike=24500, option_type="PE", action="SELL", entry_price=90))

    assert captured["trade_date"] is not None
    assert captured["lot_size"] == paper_trading.DEFAULT_LOT_SIZE
    assert captured["lots"] == 1


def test_open_trade_carries_stop_loss_and_target_price(monkeypatch):
    captured = {}

    async def fake_create_paper_trade(row):
        captured.update(row)
        return row

    monkeypatch.setattr(paper_trading.storage, "create_paper_trade", fake_create_paper_trade)

    asyncio.run(paper_trading.open_trade(
        strike=24500, option_type="CE", action="BUY", entry_price=120.5,
        stop_loss=90.0, target_price=160.0,
    ))

    assert captured["stop_loss"] == 90.0
    assert captured["target_price"] == 160.0


def test_open_trade_carries_expiry(monkeypatch):
    captured = {}

    async def fake_create_paper_trade(row):
        captured.update(row)
        return row

    monkeypatch.setattr(paper_trading.storage, "create_paper_trade", fake_create_paper_trade)

    asyncio.run(paper_trading.open_trade(
        strike=24500, option_type="CE", action="BUY", entry_price=120.5,
        expiry="18-Aug-2026",
    ))

    assert captured["expiry"] == "18-Aug-2026"


def test_close_trade_computes_pnl_and_patches(monkeypatch):
    async def fake_get_paper_trade(trade_id):
        assert trade_id == 7
        return {"id": 7, "action": "BUY", "entry_price": 100.0, "lot_size": 75, "lots": 1, "status": "open"}

    captured_patch = {}

    async def fake_update_paper_trade(trade_id, patch):
        captured_patch.update(patch)
        return {"id": trade_id, **patch}

    monkeypatch.setattr(paper_trading.storage, "get_paper_trade", fake_get_paper_trade)
    monkeypatch.setattr(paper_trading.storage, "update_paper_trade", fake_update_paper_trade)

    result = asyncio.run(paper_trading.close_trade(7, exit_price=125.0))

    assert result["pnl"] == 1875.0
    assert captured_patch["status"] == "closed"
    assert captured_patch["exit_price"] == 125.0
    assert captured_patch["pnl"] == 1875.0
    assert captured_patch["exit_time"] is not None
    assert captured_patch["exit_reason"] == "manual"


def test_close_trade_records_stop_loss_reason(monkeypatch):
    async def fake_get_paper_trade(trade_id):
        return {"id": 7, "action": "BUY", "entry_price": 100.0, "lot_size": 75, "lots": 1, "status": "open"}

    captured_patch = {}

    async def fake_update_paper_trade(trade_id, patch):
        captured_patch.update(patch)
        return {"id": trade_id, **patch}

    monkeypatch.setattr(paper_trading.storage, "get_paper_trade", fake_get_paper_trade)
    monkeypatch.setattr(paper_trading.storage, "update_paper_trade", fake_update_paper_trade)

    asyncio.run(paper_trading.close_trade(7, exit_price=90.0, reason="stop_loss"))

    assert captured_patch["exit_reason"] == "stop_loss"


def test_close_trade_returns_none_when_trade_not_found(monkeypatch):
    async def fake_get_paper_trade(trade_id):
        return None

    monkeypatch.setattr(paper_trading.storage, "get_paper_trade", fake_get_paper_trade)

    assert asyncio.run(paper_trading.close_trade(999, exit_price=100.0)) is None


def test_close_trade_returns_none_when_already_closed(monkeypatch):
    async def fake_get_paper_trade(trade_id):
        return {"id": 7, "status": "closed", "action": "BUY", "entry_price": 100.0, "lot_size": 75, "lots": 1}

    monkeypatch.setattr(paper_trading.storage, "get_paper_trade", fake_get_paper_trade)

    assert asyncio.run(paper_trading.close_trade(7, exit_price=125.0)) is None


# ---------------- summarize ----------------

def test_summarize_empty_list():
    result = paper_trading.summarize([])
    assert result == {
        "open_count": 0, "closed_count": 0, "win_count": 0, "loss_count": 0,
        "win_rate_pct": None, "total_pnl": 0.0, "avg_pnl": None,
    }


def test_summarize_counts_open_and_closed_separately():
    trades = [
        {"status": "open"},
        {"status": "open"},
        {"status": "closed", "pnl": 100.0},
    ]
    result = paper_trading.summarize(trades)
    assert result["open_count"] == 2
    assert result["closed_count"] == 1


def test_summarize_computes_win_rate_and_total_pnl():
    trades = [
        {"status": "closed", "pnl": 500.0},
        {"status": "closed", "pnl": -200.0},
        {"status": "closed", "pnl": 300.0},
    ]
    result = paper_trading.summarize(trades)
    assert result["win_count"] == 2
    assert result["loss_count"] == 1
    assert result["win_rate_pct"] == round(2 / 3 * 100, 1)
    assert result["total_pnl"] == 600.0
    assert result["avg_pnl"] == 200.0


def test_summarize_ignores_closed_trades_missing_pnl():
    trades = [{"status": "closed", "pnl": None}, {"status": "closed", "pnl": 100.0}]
    result = paper_trading.summarize(trades)
    assert result["closed_count"] == 1
    assert result["total_pnl"] == 100.0


# ---------------- weekly_pnl ----------------
# 2026-08-03 and 2026-08-10 are both Mondays (confirmed against the real
# calendar elsewhere in this test suite) -- one week apart, used as the two
# week-start anchors below.

def test_weekly_pnl_groups_by_week_of_exit_time_newest_first():
    trades = [
        {"status": "closed", "pnl": 100.0, "exit_time": "2026-08-05T10:00:00+00:00"},  # Wed, week of 08-03
        {"status": "closed", "pnl": -50.0, "exit_time": "2026-08-06T10:00:00+00:00"},  # Thu, week of 08-03
        {"status": "closed", "pnl": 200.0, "exit_time": "2026-08-11T10:00:00+00:00"},  # Tue, week of 08-10
    ]
    weeks = paper_trading.weekly_pnl(trades)

    assert [w["week_start"] for w in weeks] == ["2026-08-10", "2026-08-03"]
    assert weeks[0] == {"week_start": "2026-08-10", "trades": 1, "wins": 1, "losses": 0, "total_pnl": 200.0}
    assert weeks[1] == {"week_start": "2026-08-03", "trades": 2, "wins": 1, "losses": 1, "total_pnl": 50.0}


def test_weekly_pnl_ignores_open_trades_and_missing_pnl():
    trades = [
        {"status": "open", "exit_time": None},
        {"status": "closed", "pnl": None, "exit_time": "2026-08-05T10:00:00+00:00"},
        {"status": "closed", "pnl": 100.0, "exit_time": None},
    ]
    assert paper_trading.weekly_pnl(trades) == []


def test_weekly_pnl_empty_list():
    assert paper_trading.weekly_pnl([]) == []
