from fastapi.testclient import TestClient

import api.index as index_module

client = TestClient(index_module.app)


class FakeUpstoxResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json


class FakeUpstoxAsyncClient:
    """Same minimal async-context-manager stand-in as test_upstox_optionchain.py's."""

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


class RoutingFakeUpstoxAsyncClient:
    """Like FakeUpstoxAsyncClient, but picks a response based on which URL
    was requested -- needed to test the intraday-vs-daily-fallback path,
    where the same stock's history fetch hits two different Upstox
    endpoints in sequence."""

    def __init__(self, response_by_url_fragment):
        self._responses = response_by_url_fragment
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        for fragment, response in self._responses.items():
            if fragment in url:
                return response
        raise AssertionError(f"no fake response configured for {url}")


def _reset_upstox_state(monkeypatch):
    monkeypatch.setattr(index_module, "upstox_token", {"access_token": None, "obtained_at": None})


def test_upstox_movers_not_connected(monkeypatch):
    _reset_upstox_state(monkeypatch)

    r = client.get("/api/upstox/movers")
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is False
    assert body["stocks"] == []
    assert body["implied_move_pct"] is None
    assert "not connected" in body["error"]


def test_upstox_movers_computes_weighted_contribution_when_connected(monkeypatch):
    monkeypatch.setattr(index_module, "upstox_token", {"access_token": "tok123", "obtained_at": "now"})

    # Only the first two of NIFTY_TOP10 have data in this fake response; the
    # rest should degrade to null fields rather than crashing or being
    # dropped from the stocks list.
    hdfc = index_module.NIFTY_TOP10[0]
    icici = index_module.NIFTY_TOP10[1]
    fake_response = FakeUpstoxResponse(200, {
        "data": {
            "NSE_EQ:HDFCBANK": {
                "instrument_token": f"NSE_EQ|{hdfc['isin']}",
                "last_price": 1650.0,
                "ohlc": {"close": 1600.0},
            },
            "NSE_EQ:ICICIBANK": {
                "instrument_token": f"NSE_EQ|{icici['isin']}",
                "last_price": 1200.0,
                "ohlc": {"close": 1210.0},  # down move
            },
        },
    })
    fake_client = FakeUpstoxAsyncClient(fake_response)
    monkeypatch.setattr(index_module.httpx, "AsyncClient", lambda **kw: fake_client)

    r = client.get("/api/upstox/movers")
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is True
    assert len(body["stocks"]) == len(index_module.NIFTY_TOP10)

    hdfc_row = next(s for s in body["stocks"] if s["symbol"] == "HDFCBANK")
    assert hdfc_row["ltp"] == 1650.0
    assert round(hdfc_row["pct_change"], 4) == round((1650.0 - 1600.0) / 1600.0 * 100, 4)

    lt_row = next(s for s in body["stocks"] if s["symbol"] == "LT")  # no data for this one
    assert lt_row["ltp"] is None
    assert lt_row["pct_change"] is None
    assert lt_row["contribution_pct"] is None

    expected_hdfc_contrib = hdfc_row["pct_change"] * hdfc["weight_pct"] / 100
    expected_icici_contrib = next(s for s in body["stocks"] if s["symbol"] == "ICICIBANK")["pct_change"] * icici["weight_pct"] / 100
    assert body["implied_move_pct"] == round(expected_hdfc_contrib + expected_icici_contrib, 3)
    # confirms the Bearer token from upstox_token actually made it onto the request
    assert fake_client.calls[0][1]["headers"]["Authorization"] == "Bearer tok123"
    # no NSE_INDEX|Nifty 50 entry in this fixture -- implied_points needs a
    # live Nifty LTP to convert %, so it degrades to None rather than guessing
    assert body["nifty_spot"] is None
    assert body["implied_points"] is None


def test_upstox_movers_converts_implied_pct_to_points_using_live_nifty_ltp(monkeypatch):
    monkeypatch.setattr(index_module, "upstox_token", {"access_token": "tok123", "obtained_at": "now"})

    hdfc = index_module.NIFTY_TOP10[0]
    fake_response = FakeUpstoxResponse(200, {
        "data": {
            "NSE_INDEX:Nifty 50": {
                "instrument_token": index_module.UPSTOX_UNDERLYING_KEY["NIFTY"],
                "last_price": 24000.0,
            },
            "NSE_EQ:HDFCBANK": {
                "instrument_token": f"NSE_EQ|{hdfc['isin']}",
                "last_price": 1650.0,
                "ohlc": {"close": 1600.0},  # +3.125% move
            },
        },
    })
    monkeypatch.setattr(index_module.httpx, "AsyncClient", lambda **kw: FakeUpstoxAsyncClient(fake_response))

    r = client.get("/api/upstox/movers")
    body = r.json()
    assert body["nifty_spot"] == 24000.0
    # implied_move_pct = 3.125 * hdfc_weight/100 (only stock with data); implied_points = that % of 24000
    expected_points = round(body["implied_move_pct"] / 100 * 24000.0, 1)
    assert body["implied_points"] == expected_points
    assert body["implied_points"] > 0  # sanity: a real, non-trivial number, not None/0


def test_upstox_movers_uses_net_change_not_ohlc_close_during_live_market(monkeypatch):
    """Regression test for a real bug caught live 2026-08-17: during market
    hours, Upstox's ohlc.close mirrors last_price (it's today's *running*
    close, not the prior session's fixed one), which made every stock's
    pct_change compute to exactly 0 while the market was open. Confirmed
    live against a real quote (HDFCBANK: last_price=725.8, ohlc.close=725.8,
    net_change=-1.2 -> real prev_close=727.0) -- this fixture matches that
    shape exactly."""
    monkeypatch.setattr(index_module, "upstox_token", {"access_token": "tok123", "obtained_at": "now"})

    hdfc = index_module.NIFTY_TOP10[0]
    fake_response = FakeUpstoxResponse(200, {
        "data": {
            "NSE_EQ:HDFCBANK": {
                "instrument_token": f"NSE_EQ|{hdfc['isin']}",
                "last_price": 725.8,
                "ohlc": {"close": 725.8},  # deceptively equals last_price -- the bug
                "net_change": -1.2,
            },
        },
    })
    monkeypatch.setattr(index_module.httpx, "AsyncClient", lambda **kw: FakeUpstoxAsyncClient(fake_response))

    r = client.get("/api/upstox/movers")
    body = r.json()
    hdfc_row = next(s for s in body["stocks"] if s["symbol"] == "HDFCBANK")
    assert hdfc_row["prev_close"] == 727.0  # 725.8 - (-1.2), not the misleading ohlc.close
    assert round(hdfc_row["pct_change"], 4) == round(-1.2 / 727.0 * 100, 4)
    assert hdfc_row["pct_change"] != 0  # the actual bug: this used to always read 0 live


def test_prev_close_from_quote_falls_back_to_ohlc_close_without_net_change():
    # Market-closed case (e.g. weekend) -- no net_change field, ohlc.close
    # is then the real, fixed prior-session close and is safe to use.
    assert index_module._prev_close_from_quote({"last_price": 100.0, "ohlc": {"close": 98.5}}) == 98.5


def test_upstox_movers_401_clears_token_and_reports_not_connected(monkeypatch):
    monkeypatch.setattr(index_module, "upstox_token", {"access_token": "stale-token", "obtained_at": "now"})

    fake_response = FakeUpstoxResponse(401, {}, "unauthorized")
    monkeypatch.setattr(index_module.httpx, "AsyncClient", lambda **kw: FakeUpstoxAsyncClient(fake_response))

    r = client.get("/api/upstox/movers")
    body = r.json()
    assert body["connected"] is False
    assert "expired" in body["error"]
    assert index_module.upstox_token["access_token"] is None


def test_upstox_movers_non_200_reports_error_without_crashing(monkeypatch):
    monkeypatch.setattr(index_module, "upstox_token", {"access_token": "tok123", "obtained_at": "now"})

    fake_response = FakeUpstoxResponse(500, {}, "internal error")
    monkeypatch.setattr(index_module.httpx, "AsyncClient", lambda **kw: FakeUpstoxAsyncClient(fake_response))

    r = client.get("/api/upstox/movers")
    assert r.status_code == 200  # never a 500 to the caller — degrades to an error field
    body = r.json()
    assert body["connected"] is True
    assert body["stocks"] == []
    assert "500" in body["error"]


def test_movers_verdict_thresholds():
    assert index_module._movers_verdict(None) is None
    assert index_module._movers_verdict(0.2) == "Gap-up likely"
    assert index_module._movers_verdict(-0.2) == "Gap-down likely"
    assert index_module._movers_verdict(0.05) == "Flat open"
    assert index_module._movers_verdict(-0.05) == "Flat open"


# ---------------- full Nifty 50 board ----------------

def test_nifty50_all_has_fifty_unique_symbols():
    symbols = [s["symbol"] for s in index_module.NIFTY50_ALL]
    assert len(symbols) == 50
    assert len(set(symbols)) == 50  # no duplicates between NIFTY_TOP10 and NIFTY50_EXTRA


def test_upstox_nifty50_not_connected(monkeypatch):
    _reset_upstox_state(monkeypatch)

    r = client.get("/api/upstox/nifty50")
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is False
    assert body["stocks"] == []
    assert body["advances"] is None
    assert "not connected" in body["error"]


def test_upstox_nifty50_computes_breadth_and_sorts_by_change(monkeypatch):
    monkeypatch.setattr(index_module, "upstox_token", {"access_token": "tok123", "obtained_at": "now"})

    up, down = index_module.NIFTY50_ALL[0], index_module.NIFTY50_ALL[1]
    fake_response = FakeUpstoxResponse(200, {
        "data": {
            "a": {"instrument_token": f"NSE_EQ|{up['isin']}", "last_price": 110.0, "ohlc": {"close": 100.0}},
            "b": {"instrument_token": f"NSE_EQ|{down['isin']}", "last_price": 90.0, "ohlc": {"close": 100.0}},
        },
    })
    fake_client = FakeUpstoxAsyncClient(fake_response)
    monkeypatch.setattr(index_module.httpx, "AsyncClient", lambda **kw: fake_client)

    r = client.get("/api/upstox/nifty50")
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is True
    assert len(body["stocks"]) == 50
    assert body["advances"] == 1
    assert body["declines"] == 1
    # unresolved rows (48 of them here) are neither an advance nor a decline
    assert body["unchanged"] == 0
    # sorted biggest gainer first; null-change rows sort last
    assert body["stocks"][0]["symbol"] == up["symbol"]
    assert body["stocks"][-1]["pct_change"] is None


# ---------------- movers intraday history (area charts) ----------------

def test_upstox_movers_history_not_connected(monkeypatch):
    _reset_upstox_state(monkeypatch)

    r = client.get("/api/upstox/movers/history")
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is False
    assert body["series"] == {}
    assert "not connected" in body["error"]


def test_upstox_movers_history_rejects_bad_interval(monkeypatch):
    monkeypatch.setattr(index_module, "upstox_token", {"access_token": "tok123", "obtained_at": "now"})

    r = client.get("/api/upstox/movers/history", params={"interval": "3minute"})
    body = r.json()
    assert body["series"] == {}
    assert "interval must be one of" in body["error"]


def test_upstox_intraday_intervals_map_to_v3_unit_and_number():
    assert index_module.UPSTOX_INTRADAY_INTERVALS["5minute"] == ("minutes", 5)
    assert index_module.UPSTOX_INTRADAY_INTERVALS["15minute"] == ("minutes", 15)


def test_upstox_movers_history_parses_and_reverses_candles(monkeypatch):
    monkeypatch.setattr(index_module, "upstox_token", {"access_token": "tok123", "obtained_at": "now"})

    # Upstox documents candles as most-recent-first; the endpoint should
    # reverse them to chronological (oldest-first) order for a left-to-right chart.
    fake_response = FakeUpstoxResponse(200, {
        "data": {
            "candles": [
                ["2026-08-14T10:00:00+05:30", 100, 105, 99, 103.5, 1000, 0],
                ["2026-08-14T09:30:00+05:30", 98, 101, 97, 100.0, 900, 0],
            ],
        },
    })
    monkeypatch.setattr(index_module.httpx, "AsyncClient", lambda **kw: FakeUpstoxAsyncClient(fake_response))

    r = client.get("/api/upstox/movers/history", params={"interval": "30minute"})
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is True
    assert set(body["series"].keys()) == {s["symbol"] for s in index_module.NIFTY_TOP10}
    points = body["series"]["HDFCBANK"]
    assert points == [
        {"t": "2026-08-14T09:30:00+05:30", "open": 98, "high": 101, "low": 97, "close": 100.0, "volume": 900},
        {"t": "2026-08-14T10:00:00+05:30", "open": 100, "high": 105, "low": 99, "close": 103.5, "volume": 1000},
    ]


def test_upstox_movers_history_falls_back_to_daily_when_intraday_empty(monkeypatch):
    monkeypatch.setattr(index_module, "upstox_token", {"access_token": "tok123", "obtained_at": "now"})

    # Market closed today (weekend/holiday) -- Upstox returns a *successful*
    # empty candle list for the intraday endpoint, not an error.
    intraday_empty = FakeUpstoxResponse(200, {"data": {"candles": []}})
    daily_with_data = FakeUpstoxResponse(200, {
        "data": {"candles": [
            ["2026-08-14T00:00:00+05:30", 725.0, 729.6, 723.5, 727.0, 20364131, 0],
            ["2026-08-13T00:00:00+05:30", 726.9, 729.0, 724.5, 725.0, 22129893, 0],
        ]},
    })
    fake_client = RoutingFakeUpstoxAsyncClient({
        "historical-candle/intraday": intraday_empty,
        "historical-candle/NSE_EQ": daily_with_data,
    })
    monkeypatch.setattr(index_module.httpx, "AsyncClient", lambda **kw: fake_client)

    r = client.get("/api/upstox/movers/history")
    assert r.status_code == 200
    body = r.json()
    points = body["series"]["HDFCBANK"]
    # reversed to oldest-first, same as the intraday path
    assert points == [
        {"t": "2026-08-13T00:00:00+05:30", "open": 726.9, "high": 729.0, "low": 724.5, "close": 725.0, "volume": 22129893},
        {"t": "2026-08-14T00:00:00+05:30", "open": 725.0, "high": 729.6, "low": 723.5, "close": 727.0, "volume": 20364131},
    ]
    # confirms both the intraday attempt AND the daily fallback actually fired
    urls = [call[0] for call in fake_client.calls]
    assert any("historical-candle/intraday" in u for u in urls)
    assert any("historical-candle/NSE_EQ" in u and "intraday" not in u for u in urls)


def test_upstox_movers_history_degrades_per_stock_on_error(monkeypatch):
    monkeypatch.setattr(index_module, "upstox_token", {"access_token": "tok123", "obtained_at": "now"})

    fake_response = FakeUpstoxResponse(500, {}, "internal error")
    monkeypatch.setattr(index_module.httpx, "AsyncClient", lambda **kw: FakeUpstoxAsyncClient(fake_response))

    r = client.get("/api/upstox/movers/history")
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is True
    assert all(points == [] for points in body["series"].values())  # never a 500 to the caller


def test_upstox_nifty50_401_clears_token(monkeypatch):
    monkeypatch.setattr(index_module, "upstox_token", {"access_token": "stale-token", "obtained_at": "now"})

    fake_response = FakeUpstoxResponse(401, {}, "unauthorized")
    monkeypatch.setattr(index_module.httpx, "AsyncClient", lambda **kw: FakeUpstoxAsyncClient(fake_response))

    r = client.get("/api/upstox/nifty50")
    body = r.json()
    assert body["connected"] is False
    assert index_module.upstox_token["access_token"] is None


# ---------------- single-stock history (modal interval picker) ----------------

def test_upstox_stock_history_not_connected(monkeypatch):
    _reset_upstox_state(monkeypatch)

    r = client.get("/api/upstox/stock/history", params={"symbol": "RELIANCE"})
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is False
    assert body["points"] == []
    assert "not connected" in body["error"]


def test_upstox_stock_history_rejects_unknown_symbol(monkeypatch):
    monkeypatch.setattr(index_module, "upstox_token", {"access_token": "tok123", "obtained_at": "now"})

    r = client.get("/api/upstox/stock/history", params={"symbol": "NOTASTOCK"})
    body = r.json()
    assert body["points"] == []
    assert "unknown symbol" in body["error"]


def test_upstox_stock_history_rejects_bad_interval(monkeypatch):
    monkeypatch.setattr(index_module, "upstox_token", {"access_token": "tok123", "obtained_at": "now"})

    r = client.get("/api/upstox/stock/history", params={"symbol": "RELIANCE", "interval": "3minute"})
    body = r.json()
    assert body["points"] == []
    assert "interval must be one of" in body["error"]


def test_upstox_stock_history_returns_points_for_known_symbol(monkeypatch):
    monkeypatch.setattr(index_module, "upstox_token", {"access_token": "tok123", "obtained_at": "now"})

    fake_response = FakeUpstoxResponse(200, {
        "data": {"candles": [["2026-08-14T09:15:00+05:30", 1300, 1305, 1298, 1302.5, 500, 0]]},
    })
    monkeypatch.setattr(index_module.httpx, "AsyncClient", lambda **kw: FakeUpstoxAsyncClient(fake_response))

    # lowercase in the query param, on purpose -- confirms it's normalized before the NIFTY_TOP10 lookup
    r = client.get("/api/upstox/stock/history", params={"symbol": "reliance", "interval": "5minute"})
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is True
    assert body["points"] == [{"t": "2026-08-14T09:15:00+05:30", "open": 1300, "high": 1305, "low": 1298, "close": 1302.5, "volume": 500}]


# ---------------- backtest: which stocks drive big 5-min Nifty moves ----------------

def test_upstox_movers_backtest_not_connected(monkeypatch):
    _reset_upstox_state(monkeypatch)

    r = client.get("/api/upstox/movers/backtest")
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is False
    assert "not connected" in body["error"]


def test_upstox_movers_backtest_excludes_opening_bars_and_attributes_top_driver(monkeypatch):
    monkeypatch.setattr(index_module, "upstox_token", {"access_token": "tok123", "obtained_at": "now"})

    # Nifty: one bar at the excluded 09:15 open (huge move, should NOT
    # become an event) and one real event at 10:00 (+60 pts, above the
    # default 50pt threshold).
    nifty_response = FakeUpstoxResponse(200, {
        "data": {"candles": [
            ["2026-08-14T10:00:00+05:30", 24200.0, 24265.0, 24195.0, 24260.0, 0, 0],
            ["2026-08-14T09:15:00+05:30", 24000.0, 24500.0, 23900.0, 24400.0, 0, 0],
        ]},
    })
    # HDFCBANK moves +2% in the 10:00 bar (the biggest of the ten by far);
    # every other stock moves a token +0.01% so HDFCBANK is unambiguously
    # the top driver. No 09:15 row needed -- that bar is excluded before
    # stock data is even consulted.
    hdfc_response = FakeUpstoxResponse(200, {
        "data": {"candles": [["2026-08-14T10:00:00+05:30", 700.0, 715.0, 699.0, 714.0, 1000, 0]]},
    })
    tiny_move_response = FakeUpstoxResponse(200, {
        "data": {"candles": [["2026-08-14T10:00:00+05:30", 1000.0, 1000.2, 999.9, 1000.1, 1000, 0]]},
    })

    routes = {"NSE_INDEX": nifty_response, "INE040A01034": hdfc_response}  # HDFC Bank's ISIN
    for s in index_module.NIFTY_TOP10:
        if s["symbol"] != "HDFCBANK":
            routes[s["isin"]] = tiny_move_response
    fake_client = RoutingFakeUpstoxAsyncClient(routes)
    monkeypatch.setattr(index_module.httpx, "AsyncClient", lambda **kw: fake_client)

    r = client.get("/api/upstox/movers/backtest", params={"days": 30, "threshold_pts": 50})
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is True
    assert body["excluded_opening_bars"] == 1
    assert body["event_count"] == 1
    event = body["events"][0]
    assert event["t"] == "2026-08-14T10:00:00+05:30"
    assert event["nifty_move_pts"] == 60.0
    assert event["top_movers"][0]["symbol"] == "HDFCBANK"
    assert body["top_driver_counts"] == {"HDFCBANK": 1}
    # the 09:15 bar contributed nothing to the accuracy scoring either
    assert body["direction_accuracy_events_pct"] == 100.0


def test_upstox_movers_backtest_no_nifty_data_reports_error(monkeypatch):
    monkeypatch.setattr(index_module, "upstox_token", {"access_token": "tok123", "obtained_at": "now"})

    empty_response = FakeUpstoxResponse(200, {"data": {"candles": []}})
    monkeypatch.setattr(index_module.httpx, "AsyncClient", lambda **kw: FakeUpstoxAsyncClient(empty_response))

    r = client.get("/api/upstox/movers/backtest")
    body = r.json()
    assert body["connected"] is True
    assert body["events"] == []
    assert "error" in body
