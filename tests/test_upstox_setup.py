import datetime as dt

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


def _mk(times_start_hhmm, ohlc_rows):
    """Build candle dicts with sequential 5-min timestamps starting at the
    given HH:MM (all outside the excluded-opening window unless the test
    wants otherwise)."""
    h, m = map(int, times_start_hhmm.split(":"))
    base = dt.datetime(2026, 8, 14, h, m)
    out = []
    for i, (o, hi, lo, c) in enumerate(ohlc_rows):
        t = (base + dt.timedelta(minutes=5 * i)).strftime("2026-08-14T%H:%M:00+05:30")
        out.append({"t": t, "open": o, "high": hi, "low": lo, "close": c, "volume": 0})
    return out


# A hand-built series: bars 0-8 establish a bullish structure break (swing
# high 110 at bar 5, closed through at bar 8), bar 10 prints a swing low at
# 100, bar 13 wicks below it to 99 but closes back at 103 (sell-side
# liquidity sweep) -> sweep + bullish bias = confluence fire at bar 13,
# entry 103. Bars 14-19 rally to a 115 high (reach +12: hits the 10pt
# target, misses 20). Kept free of FVGs (all ranges overlap) and OBs
# (bodies too uniform for the impulse filter).
SCENARIO_SWEEP = [
    (100, 105, 99, 100),
    (100, 105, 99, 101),
    (100, 105, 95, 100),    # swing low 95
    (100, 105, 99, 101),
    (101, 105, 99, 102),
    (102, 110, 101, 104),   # swing high 110
    (104, 105, 101, 103),
    (103, 105, 101, 104),
    (104, 111.5, 103, 111), # closes through 110 -> bias bullish
    (105, 106, 104, 105),
    (105, 106, 100, 105),   # swing low 100
    (105, 106, 104, 105),
    (105, 106, 104, 105),
    (105, 106, 99, 103),    # sweeps 100, closes back above -> FIRE bullish
    (103, 108, 102, 107),
    (107, 110, 106, 109),
    (109, 112, 108, 111),
    (111, 113, 110, 112),
    (112, 114, 111, 113),
    (113, 115, 112, 114),   # max high 115 -> reach 12 from entry 103
    (114, 115, 113, 114),
    (114, 115, 113, 114),
]


def test_detector_fires_on_sweep_plus_bias_and_measures_outcome():
    candles = _mk("10:00", SCENARIO_SWEEP)
    result = index_module.detect_confluence_setups(candles)

    bullish_fires = [f for f in result["fires"] if f["direction"] == "bullish"]
    assert len(bullish_fires) == 1
    fire = bullish_fires[0]
    assert fire["entry"] == 103
    assert fire["score"] >= 2
    assert any("liquidity swept" in r for r in fire["reasons"])
    assert any("structure bias bullish" in r for r in fire["reasons"])
    assert fire["evaluated"] is True
    assert fire["hit_10"] is True   # rallied 12 pts within the 6-bar horizon
    assert fire["hit_20"] is False

    assert result["stats"]["hit_rate_10_pct"] == 100.0
    assert result["stats"]["hit_rate_20_pct"] == 0.0
    # base rates must be present so the frontend can show earned-vs-random
    assert "baseline_up_10_pct" in result["stats"]


def test_detector_respects_opening_exclusion_window():
    # Same series, but timed so the sweep bar lands at 09:15 and the
    # cooldown-free bars after it are still inside the excluded window --
    # by the time bars are admissible again the sweep is stale, so the
    # rule must not fire at all.
    candles = _mk("08:10", SCENARIO_SWEEP)  # bar 13 = 08:10 + 65min = 09:15
    result = index_module.detect_confluence_setups(candles)
    assert result["fires"] == []


def test_detector_fires_on_fvg_touch_plus_bias():
    rows = [
        (100, 105, 99, 100),
        (100, 105, 99, 101),
        (100, 105, 95, 100),
        (100, 105, 99, 101),
        (101, 105, 99, 102),
        (102, 110, 101, 104),     # swing high 110
        (104, 105, 101, 103),
        (103, 105, 101, 104),
        (104, 111.5, 103, 111),   # bias bullish
        (111, 113, 111, 112),
        (112, 118, 114, 117),
        (117, 120, 117.5, 119),   # bullish FVG: bar9 high 113 < bar11 low 117.5
        (119, 119.4, 116, 117),   # dips into the 113-117.5 gap -> FVG touch + bias = fire
    ]
    candles = _mk("10:00", rows)
    result = index_module.detect_confluence_setups(candles)
    fires = [f for f in result["fires"] if f["direction"] == "bullish"]
    assert len(fires) == 1
    assert any("FVG" in r for r in fires[0]["reasons"])
    # too close to the series end for the 6-bar horizon -> honestly unevaluated
    assert fires[0]["evaluated"] is False
    assert result["live"]["fired"] is True  # the fire IS the latest bar


def test_detector_live_state_reports_partial_confluence_without_firing():
    # Only bars 0-9 of the sweep scenario: bias turns bullish at bar 8 but
    # nothing else aligns -> live shows 1 bullish factor, not fired.
    candles = _mk("10:00", SCENARIO_SWEEP[:10])
    result = index_module.detect_confluence_setups(candles)
    assert result["fires"] == []
    assert result["live"]["fired"] is False
    assert result["live"]["direction"] == "bullish"
    assert result["live"]["score"] == 1


def test_setup_endpoint_not_connected(monkeypatch):
    _reset_upstox_state(monkeypatch)
    r = client.get("/api/upstox/setup")
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is False
    assert "not connected" in body["error"]


def test_setup_endpoint_reports_error_when_no_candles(monkeypatch):
    monkeypatch.setattr(index_module, "upstox_token", {"access_token": "tok123", "obtained_at": "now"})
    empty = FakeUpstoxResponse(200, {"data": {"candles": []}})
    monkeypatch.setattr(index_module.httpx, "AsyncClient",
                        lambda **kw: RoutingFakeUpstoxAsyncClient({"historical-candle": empty}))
    r = client.get("/api/upstox/setup")
    body = r.json()
    assert body["connected"] is True
    assert "not enough" in body["error"]
