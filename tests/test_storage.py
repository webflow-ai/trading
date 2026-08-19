import asyncio
import datetime as dt

import pandas as pd
import pytest

import storage


class FakeResponse:
    def __init__(self, json_data=None, status_code=200):
        self._json = json_data if json_data is not None else []
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            req = httpx.Request("POST", "https://fake.supabase.co/rest/v1/paper_trades")
            resp = httpx.Response(self.status_code, json=self._json, request=req)
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=req, response=resp,
            )

    def json(self):
        return self._json


class FakeAsyncClient:
    def __init__(self, get_response=None, post_response=None, patch_response=None, post_responses=None):
        self.calls = []
        self._get_response = get_response if get_response is not None else FakeResponse([])
        self._post_response = post_response if post_response is not None else FakeResponse()
        self._post_responses = list(post_responses) if post_responses is not None else None
        self._patch_response = patch_response if patch_response is not None else FakeResponse()

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if self._post_responses is not None:
            if not self._post_responses:
                raise AssertionError("unexpected extra POST")
            return self._post_responses.pop(0)
        return self._post_response

    async def patch(self, url, **kwargs):
        self.calls.append(("PATCH", url, kwargs))
        return self._patch_response

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._get_response


@pytest.fixture(autouse=True)
def configured_supabase(monkeypatch):
    """Every test gets a fake-but-truthy Supabase config by default so
    `configured()` is True and writes/reads actually go through the fake
    client instead of silently no-op'ing."""
    monkeypatch.setattr(storage, "SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setattr(storage, "SUPABASE_KEY", "fake-service-key")
    storage._paper_trades_dropped_cols.clear()


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeAsyncClient()

    async def fake_get_client():
        return client

    monkeypatch.setattr(storage, "get_client", fake_get_client)
    return client


def test_not_configured_skips_the_network_call_entirely(monkeypatch):
    monkeypatch.setattr(storage, "SUPABASE_URL", None)
    monkeypatch.setattr(storage, "SUPABASE_KEY", None)

    async def fail_if_called():
        raise AssertionError("get_client() should not be called when Supabase isn't configured")

    monkeypatch.setattr(storage, "get_client", fail_if_called)

    asyncio.run(storage.save_fii_dii_cash({"date": "10-Aug-2026", "fii_buy": 1, "fii_sell": 2}))
    assert asyncio.run(storage.get_brief_history()) == []


def test_normalize_nse_date_converts_to_iso():
    assert storage._normalize_nse_date("10-Aug-2026") == "2026-08-10"
    assert storage._normalize_nse_date(None) is None


def test_save_participant_oi_builds_one_row_per_participant_and_upserts(fake_client):
    df = pd.DataFrame([
        {"date": "2026-08-10", "Client Type": "FII", "Future Index Long": 300000, "Future Index Short": 250000,
         "Future Stock Long": 1, "Future Stock Short": 2, "Option Index Call Long": 3, "Option Index Put Long": 4,
         "Option Index Call Short": 5, "Option Index Put Short": 6, "Total Long Contracts": 7,
         "Total Short Contracts": 8},
        {"date": "2026-08-10", "Client Type": "DII", "Future Index Long": 50000, "Future Index Short": 20000,
         "Future Stock Long": 0, "Future Stock Short": 0, "Option Index Call Long": 0, "Option Index Put Long": 0,
         "Option Index Call Short": 0, "Option Index Put Short": 0, "Total Long Contracts": 0,
         "Total Short Contracts": 0},
    ])

    asyncio.run(storage.save_participant_oi(df))

    assert len(fake_client.calls) == 1
    method, url, kwargs = fake_client.calls[0]
    assert method == "POST"
    assert url == "https://fake.supabase.co/rest/v1/participant_oi"
    assert kwargs["params"] == {"on_conflict": "trade_date,participant"}
    assert kwargs["headers"]["Prefer"] == "resolution=merge-duplicates,return=minimal"
    rows = kwargs["json"]
    assert len(rows) == 2
    fii_row = next(r for r in rows if r["participant"] == "FII")
    assert fii_row["trade_date"] == "2026-08-10"
    assert fii_row["future_index_long"] == 300000
    assert fii_row["future_index_short"] == 250000


def test_save_fii_dii_cash_normalizes_date_and_upserts_on_trade_date(fake_client):
    asyncio.run(storage.save_fii_dii_cash({
        "date": "10-Aug-2026", "fii_buy": 12345.67, "fii_sell": 11000.5, "dii_buy": 9000.0, "dii_sell": 9500.25,
    }))

    method, url, kwargs = fake_client.calls[0]
    assert url == "https://fake.supabase.co/rest/v1/fii_dii_cash"
    assert kwargs["params"] == {"on_conflict": "trade_date"}
    assert kwargs["json"]["trade_date"] == "2026-08-10"
    assert kwargs["json"]["fii_buy"] == 12345.67


def test_save_macro_snapshots_writes_one_row_per_symbol(fake_client):
    quotes = {
        "^DJI": {"price": 42000.1, "pct_change": 0.5},
        "^GSPC": {"price": 5800.2, "pct_change": -0.2},
    }
    asyncio.run(storage.save_macro_snapshots("morning", quotes, captured_at=dt.datetime(2026, 8, 10, 3, 0, tzinfo=dt.timezone.utc)))

    method, url, kwargs = fake_client.calls[0]
    assert url == "https://fake.supabase.co/rest/v1/macro_snapshots"
    rows = kwargs["json"]
    assert len(rows) == 2
    assert all(r["session"] == "morning" for r in rows)
    dji = next(r for r in rows if r["symbol"] == "^DJI")
    assert dji["price"] == 42000.1
    assert dji["pct_change"] == 0.5


def test_save_macro_snapshots_skips_empty_quotes(fake_client):
    asyncio.run(storage.save_macro_snapshots("evening", {}))
    assert fake_client.calls == []


def test_save_morning_brief_upserts_on_trade_date(fake_client):
    brief = {"trade_date": "2026-08-10", "score": 42, "verdict": "Gap-up likely",
              "expected_low": 24400, "expected_high": 24700, "predicted_open": 24550,
              "components": {"outlook": {"headline": "x"}}, "headlines": [],
              "news_sentiment": "mild", "outlook": {"headline": "x"}, "disclaimer": "nope"}
    asyncio.run(storage.save_morning_brief(brief))

    method, url, kwargs = fake_client.calls[0]
    assert url == "https://fake.supabase.co/rest/v1/morning_briefs"
    assert kwargs["params"] == {"on_conflict": "trade_date"}
    # Response-only keys must not be written as columns.
    assert kwargs["json"] == {
        "trade_date": "2026-08-10", "score": 42, "verdict": "Gap-up likely",
        "expected_low": 24400, "expected_high": 24700, "predicted_open": 24550,
        "components": {"outlook": {"headline": "x"}}, "headlines": [], "news_sentiment": "mild",
    }
    assert "outlook" not in kwargs["json"] or "headline" in kwargs["json"]["components"]["outlook"]
    assert "disclaimer" not in kwargs["json"]


def test_get_brief_history_orders_desc_and_limits(monkeypatch):
    rows = [{"trade_date": "2026-08-10", "score": 42}]
    client = FakeAsyncClient(get_response=FakeResponse(rows))

    async def fake_get_client():
        return client
    monkeypatch.setattr(storage, "get_client", fake_get_client)

    result = asyncio.run(storage.get_brief_history(days=10))

    assert result == rows
    method, url, kwargs = client.calls[0]
    assert url == "https://fake.supabase.co/rest/v1/morning_briefs"
    assert kwargs["params"] == {"order": "trade_date.desc", "limit": "10"}


def test_get_fii_trend_filters_to_fii_participant(monkeypatch):
    client = FakeAsyncClient(get_response=FakeResponse([]))

    async def fake_get_client():
        return client
    monkeypatch.setattr(storage, "get_client", fake_get_client)

    asyncio.run(storage.get_fii_trend(days=5))

    method, url, kwargs = client.calls[0]
    assert url == "https://fake.supabase.co/rest/v1/participant_oi"
    assert kwargs["params"] == {"participant": "eq.FII", "order": "trade_date.desc", "limit": "5"}


def test_get_latest_participant_oi_filters_to_the_newest_trade_date(monkeypatch):
    rows = [
        {"trade_date": "2026-08-10", "participant": "FII", "future_index_long": 1, "future_index_short": 1},
        {"trade_date": "2026-08-10", "participant": "DII", "future_index_long": 2, "future_index_short": 2},
        {"trade_date": "2026-08-07", "participant": "FII", "future_index_long": 3, "future_index_short": 3},
    ]
    client = FakeAsyncClient(get_response=FakeResponse(rows))

    async def fake_get_client():
        return client
    monkeypatch.setattr(storage, "get_client", fake_get_client)

    result = asyncio.run(storage.get_latest_participant_oi())

    assert len(result) == 2
    assert all(r["trade_date"] == "2026-08-10" for r in result)
    method, url, kwargs = client.calls[0]
    assert url == "https://fake.supabase.co/rest/v1/participant_oi"
    assert kwargs["params"] == {"order": "trade_date.desc", "limit": "20"}


def test_get_participant_history_fetches_all_participants_unfiltered(monkeypatch):
    rows = [{"trade_date": "2026-08-10", "participant": "FII"}, {"trade_date": "2026-08-10", "participant": "DII"}]
    client = FakeAsyncClient(get_response=FakeResponse(rows))

    async def fake_get_client():
        return client
    monkeypatch.setattr(storage, "get_client", fake_get_client)

    result = asyncio.run(storage.get_participant_history(days=5))

    assert result == rows
    method, url, kwargs = client.calls[0]
    assert url == "https://fake.supabase.co/rest/v1/participant_oi"
    assert "participant" not in kwargs["params"]  # unfiltered, unlike get_fii_trend
    assert kwargs["params"] == {"order": "trade_date.desc", "limit": "25"}


def test_get_latest_participant_oi_empty_when_no_rows(monkeypatch):
    client = FakeAsyncClient(get_response=FakeResponse([]))

    async def fake_get_client():
        return client
    monkeypatch.setattr(storage, "get_client", fake_get_client)

    assert asyncio.run(storage.get_latest_participant_oi()) == []


def test_get_latest_fii_dii_cash_returns_first_row(monkeypatch):
    rows = [{"trade_date": "2026-08-10", "fii_buy": 100.0}]
    client = FakeAsyncClient(get_response=FakeResponse(rows))

    async def fake_get_client():
        return client
    monkeypatch.setattr(storage, "get_client", fake_get_client)

    result = asyncio.run(storage.get_latest_fii_dii_cash())

    assert result == rows[0]
    method, url, kwargs = client.calls[0]
    assert url == "https://fake.supabase.co/rest/v1/fii_dii_cash"
    assert kwargs["params"] == {"order": "trade_date.desc", "limit": "1"}


def test_get_latest_fii_dii_cash_none_when_no_rows(monkeypatch):
    client = FakeAsyncClient(get_response=FakeResponse([]))

    async def fake_get_client():
        return client
    monkeypatch.setattr(storage, "get_client", fake_get_client)

    assert asyncio.run(storage.get_latest_fii_dii_cash()) is None


def test_get_latest_pcr_snapshot_returns_none_when_no_rows(monkeypatch):
    client = FakeAsyncClient(get_response=FakeResponse([]))

    async def fake_get_client():
        return client
    monkeypatch.setattr(storage, "get_client", fake_get_client)

    assert asyncio.run(storage.get_latest_pcr_snapshot("NIFTY")) is None


def test_get_latest_pcr_snapshot_returns_first_row(monkeypatch):
    rows = [{"symbol": "NIFTY", "max_call_oi_strike": 24700}]
    client = FakeAsyncClient(get_response=FakeResponse(rows))

    async def fake_get_client():
        return client
    monkeypatch.setattr(storage, "get_client", fake_get_client)

    result = asyncio.run(storage.get_latest_pcr_snapshot("NIFTY"))
    assert result == rows[0]


# ---------------- paper trades ----------------

def test_create_paper_trade_posts_and_returns_representation(fake_client):
    fake_client._post_response = FakeResponse([{"id": 7, "strike": 24500, "status": "open"}])

    trade = {"trade_date": "2026-08-12", "strike": 24500, "option_type": "CE", "action": "BUY",
              "lots": 1, "lot_size": 75, "entry_price": 120.5, "status": "open"}
    result = asyncio.run(storage.create_paper_trade(trade))

    assert result == {"id": 7, "strike": 24500, "status": "open"}
    method, url, kwargs = fake_client.calls[0]
    assert method == "POST"
    assert url == "https://fake.supabase.co/rest/v1/paper_trades"
    assert kwargs["headers"]["Prefer"] == "return=representation"
    assert kwargs["json"] == trade


def test_create_paper_trade_omits_none_optional_fields(fake_client):
    fake_client._post_response = FakeResponse([{"id": 7, "strike": 24500, "status": "open"}])

    trade = {
        "trade_date": "2026-08-12", "strike": 24500, "option_type": "CE", "action": "BUY",
        "lots": 1, "lot_size": 75, "entry_price": 120.5, "status": "open",
        "notes": None, "stop_loss": None, "target_price": None, "expiry": None,
    }
    asyncio.run(storage.create_paper_trade(trade))

    sent = fake_client.calls[0][2]["json"]
    assert "notes" not in sent
    assert "expiry" not in sent
    assert sent["strike"] == 24500


def test_create_paper_trade_retries_without_unknown_column(monkeypatch):
    """Live schema may lag migrations (PGRST204) — drop the missing col and
    retry so paper trading still works before 0008 is applied."""
    fail = FakeResponse(
        {"code": "PGRST204", "message": "Could not find the 'expiry' column of 'paper_trades' in the schema cache"},
        status_code=400,
    )
    ok = FakeResponse([{"id": 9, "strike": 24500, "status": "open"}])
    client = FakeAsyncClient(post_responses=[fail, ok])

    async def fake_get_client():
        return client
    monkeypatch.setattr(storage, "get_client", fake_get_client)

    trade = {
        "trade_date": "2026-08-12", "strike": 24500, "option_type": "CE", "action": "BUY",
        "lots": 1, "lot_size": 75, "entry_price": 120.5, "status": "open",
        "expiry": "18-Aug-2026",
    }
    result = asyncio.run(storage.create_paper_trade(trade))

    assert result["id"] == 9
    assert len(client.calls) == 2
    assert client.calls[0][2]["json"]["expiry"] == "18-Aug-2026"
    assert "expiry" not in client.calls[1][2]["json"]
    assert "expiry" in storage._paper_trades_dropped_cols


def test_create_paper_trade_skips_when_not_configured(monkeypatch):
    monkeypatch.setattr(storage, "SUPABASE_URL", None)
    monkeypatch.setattr(storage, "SUPABASE_KEY", None)

    async def fail_if_called():
        raise AssertionError("get_client() should not be called when Supabase isn't configured")
    monkeypatch.setattr(storage, "get_client", fail_if_called)

    trade = {"strike": 24500}
    result = asyncio.run(storage.create_paper_trade(trade))
    assert result == {"strike": 24500, "id": None}


def test_update_paper_trade_patches_by_id_and_returns_row(monkeypatch):
    client = FakeAsyncClient(patch_response=FakeResponse([{"id": 7, "status": "closed", "pnl": 375.0}]))

    async def fake_get_client():
        return client
    monkeypatch.setattr(storage, "get_client", fake_get_client)

    patch = {"status": "closed", "exit_price": 125.5, "pnl": 375.0}
    result = asyncio.run(storage.update_paper_trade(7, patch))

    assert result == {"id": 7, "status": "closed", "pnl": 375.0}
    method, url, kwargs = client.calls[0]
    assert method == "PATCH"
    assert url == "https://fake.supabase.co/rest/v1/paper_trades"
    assert kwargs["params"] == {"id": "eq.7"}
    assert kwargs["json"] == patch


def test_update_paper_trade_returns_none_when_no_row_matched(monkeypatch):
    client = FakeAsyncClient(patch_response=FakeResponse([]))

    async def fake_get_client():
        return client
    monkeypatch.setattr(storage, "get_client", fake_get_client)

    assert asyncio.run(storage.update_paper_trade(999, {"status": "closed"})) is None


def test_get_paper_trades_filters_by_status_when_given(monkeypatch):
    client = FakeAsyncClient(get_response=FakeResponse([{"id": 1, "status": "open"}]))

    async def fake_get_client():
        return client
    monkeypatch.setattr(storage, "get_client", fake_get_client)

    result = asyncio.run(storage.get_paper_trades(status="open", days=30))

    assert result == [{"id": 1, "status": "open"}]
    method, url, kwargs = client.calls[0]
    assert kwargs["params"] == {"order": "created_at.desc", "limit": "150", "status": "eq.open"}


def test_get_paper_trades_omits_status_filter_when_not_given(monkeypatch):
    client = FakeAsyncClient(get_response=FakeResponse([]))

    async def fake_get_client():
        return client
    monkeypatch.setattr(storage, "get_client", fake_get_client)

    asyncio.run(storage.get_paper_trades())

    method, url, kwargs = client.calls[0]
    assert "status" not in kwargs["params"]


def test_get_paper_trade_returns_single_row_by_id(monkeypatch):
    client = FakeAsyncClient(get_response=FakeResponse([{"id": 7, "strike": 24500}]))

    async def fake_get_client():
        return client
    monkeypatch.setattr(storage, "get_client", fake_get_client)

    result = asyncio.run(storage.get_paper_trade(7))

    assert result == {"id": 7, "strike": 24500}
    method, url, kwargs = client.calls[0]
    assert kwargs["params"] == {"id": "eq.7", "limit": "1"}


def test_get_paper_trade_returns_none_when_not_found(monkeypatch):
    client = FakeAsyncClient(get_response=FakeResponse([]))

    async def fake_get_client():
        return client
    monkeypatch.setattr(storage, "get_client", fake_get_client)

    assert asyncio.run(storage.get_paper_trade(999)) is None


# ---------------- top-10 movers accuracy tracking ----------------

def test_save_movers_snapshot_inserts_row(fake_client):
    asyncio.run(storage.save_movers_snapshot({
        "trade_date": "2026-08-14", "implied_move_pct": 0.22, "verdict": "Gap-up likely", "stocks": [],
    }))

    method, url, kwargs = fake_client.calls[0]
    assert method == "POST"
    assert url.endswith("/rest/v1/movers_snapshots")
    assert kwargs["json"]["implied_move_pct"] == 0.22


def test_get_movers_snapshots_orders_newest_first(monkeypatch):
    client = FakeAsyncClient(get_response=FakeResponse([{"trade_date": "2026-08-14"}]))

    async def fake_get_client():
        return client
    monkeypatch.setattr(storage, "get_client", fake_get_client)

    result = asyncio.run(storage.get_movers_snapshots(days=30))

    assert result == [{"trade_date": "2026-08-14"}]
    method, url, kwargs = client.calls[0]
    assert kwargs["params"] == {"order": "captured_at.desc", "limit": "1500"}
