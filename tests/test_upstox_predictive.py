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


class RoutingFakeUpstoxAsyncClient:
    """Picks a response based on which URL fragment is present -- needed
    because upstox_predictive calls both upstox_movers (hits
    market-quote/quotes) and upstox_optionchain (hits option/chain)
    internally, each a distinct Upstox endpoint."""

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


async def fake_get_nearest_expiry(symbol):
    return "18-Aug-2026"


# ---------------- compute_max_pain ----------------

def test_compute_max_pain_picks_strike_minimizing_total_payout():
    # Heavy OI concentrated at 100 on both sides -> payout is minimized
    # (zero from either side) exactly at strike 100.
    rows = [
        {"strike": 90, "ceOi": 10, "peOi": 500},
        {"strike": 100, "ceOi": 800, "peOi": 800},
        {"strike": 110, "ceOi": 500, "peOi": 10},
    ]
    assert index_module.compute_max_pain(rows) == 100


def test_compute_max_pain_returns_none_without_oi_data():
    rows = [{"strike": 100, "ceOi": None, "peOi": None}, {"strike": 110, "ceOi": 0, "peOi": 0}]
    assert index_module.compute_max_pain(rows) is None


def test_compute_max_pain_returns_none_for_empty_rows():
    assert index_module.compute_max_pain([]) is None


# ---------------- compute_oi_bias ----------------

def test_compute_oi_bias_finds_resistance_and_support_strikes():
    rows = [
        {"strike": 100, "ceOiChg": 5000, "peOiChg": 1000},
        {"strike": 110, "ceOiChg": 12000, "peOiChg": 2000},  # heaviest call build -> resistance
        {"strike": 90, "ceOiChg": 1000, "peOiChg": 9000},    # heaviest put build -> support
    ]
    bias = index_module.compute_oi_bias(rows)
    assert bias["resistance_strike"] == 110
    assert bias["resistance_strike_oi_change"] == 12000
    assert bias["support_strike"] == 90
    assert bias["support_strike_oi_change"] == 9000
    assert bias["net_call_oi_change"] == 18000
    assert bias["net_put_oi_change"] == 12000


def test_compute_oi_bias_handles_missing_data():
    bias = index_module.compute_oi_bias([{"strike": 100, "ceOiChg": None, "peOiChg": None}])
    assert bias["resistance_strike"] is None
    assert bias["support_strike"] is None
    assert bias["net_call_oi_change"] is None
    assert bias["net_put_oi_change"] is None


# ---------------- /api/upstox/predictive ----------------

def test_upstox_predictive_not_connected_when_nothing_available(monkeypatch):
    _reset_upstox_state(monkeypatch)

    r = client.get("/api/upstox/predictive")
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is False
    assert body["predictive_lines"] == []


def test_upstox_predictive_combines_movers_and_option_chain_signals(monkeypatch):
    monkeypatch.setattr(index_module, "upstox_token", {"access_token": "tok123", "obtained_at": "now"})
    monkeypatch.setattr(index_module, "get_nearest_expiry", fake_get_nearest_expiry)

    hdfc = index_module.NIFTY_TOP10[0]
    quotes_response = FakeUpstoxResponse(200, {
        "data": {
            "NSE_INDEX:Nifty 50": {
                "instrument_token": index_module.UPSTOX_UNDERLYING_KEY["NIFTY"],
                "last_price": 24000.0,
            },
            "NSE_EQ:HDFCBANK": {
                "instrument_token": f"NSE_EQ|{hdfc['isin']}",
                "last_price": 750.0,
                "ohlc": {"close": 725.0},  # implies a real move
            },
        },
    })
    chain_response = FakeUpstoxResponse(200, {
        "data": [
            {
                "strike_price": 23900,
                "underlying_spot_price": 24000.0,
                "call_options": {"market_data": {"ltp": 10, "oi": 500, "volume": 0, "prev_oi": 400}},
                "put_options": {"market_data": {"ltp": 200, "oi": 9000, "volume": 0, "prev_oi": 1000}},
            },
            {
                "strike_price": 24100,
                "underlying_spot_price": 24000.0,
                "call_options": {"market_data": {"ltp": 200, "oi": 12000, "volume": 0, "prev_oi": 1000}},
                "put_options": {"market_data": {"ltp": 10, "oi": 500, "volume": 0, "prev_oi": 400}},
            },
        ],
    })
    fake_client = RoutingFakeUpstoxAsyncClient({
        "market-quote/quotes": quotes_response,
        "option/chain": chain_response,
    })
    monkeypatch.setattr(index_module.httpx, "AsyncClient", lambda **kw: fake_client)

    r = client.get("/api/upstox/predictive")
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is True
    assert body["spot"] == 24000.0
    assert body["max_pain"] == 23900  # both strikes tie on total payout; first candidate wins the tie
    assert body["oi_bias"]["resistance_strike"] == 24100
    assert body["oi_bias"]["support_strike"] == 23900
    assert body["implied_points"] is not None
    assert len(body["predictive_lines"]) == 3
    assert "Primary signal" in body["predictive_lines"][0]
    assert "Max pain" in body["predictive_lines"][1] or "close to max pain" in body["predictive_lines"][1]
    assert "Option chain" in body["predictive_lines"][2]
