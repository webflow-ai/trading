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

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        for fragment, response in self._responses.items():
            if fragment in url:
                return response
        raise AssertionError(f"no fake response configured for {url}")


def _reset_upstox_state(monkeypatch):
    monkeypatch.setattr(index_module, "upstox_token", {"access_token": None, "obtained_at": None})


def _mk(start_hhmm, rows):
    """1-min candles from (open, high, low, close) tuples."""
    h, m = map(int, start_hhmm.split(":"))
    base = dt.datetime(2026, 8, 14, h, m)
    return [
        {
            "t": (base + dt.timedelta(minutes=i)).strftime("2026-08-14T%H:%M:00+05:30"),
            "open": o, "high": hi, "low": lo, "close": c, "volume": 0,
        }
        for i, (o, hi, lo, c) in enumerate(rows)
    ]


def _flat(count, level=100.0):
    """Tight range bars: high/low stay within level±1, so any close beyond
    that band is unambiguously a break of the prior window."""
    return [(level, level + 1, level - 1, level)] * count


# ---------------- detect_breakouts ----------------

def test_detects_upside_break_and_measures_follow_through():
    lookback, horizon = 10, 5
    rows = _flat(lookback)                       # bars 0-9: range 99-101
    rows += [(101, 112, 100, 111)]               # bar 10: closes 111 > prior high 101 -> break up
    rows += [(111, 124, 110, 123)]               # follow-through: high reaches 124
    rows += _flat(6, 123.0)
    candles = _mk("10:00", rows)

    result = index_module.detect_breakouts(candles, lookback=lookback, horizon=horizon)
    ups = [f for f in result["fires"] if f["direction"] == "up"]
    assert len(ups) == 1
    fire = ups[0]
    assert fire["level"] == 101
    assert fire["entry"] == 111
    assert fire["evaluated"] is True
    assert fire["extension_pts"] == 13.0          # 124 high - 111 entry
    assert fire["hit_10"] is True
    assert fire["hit_20"] is False
    assert result["stats"]["hit_rate_10_pct"] == 100.0
    assert result["stats"]["hit_rate_20_pct"] == 0.0
    # base rates must be present for the earned-vs-random comparison
    assert "baseline_up_10_pct" in result["stats"]


def test_detects_downside_break():
    lookback, horizon = 10, 5
    rows = _flat(lookback)
    rows += [(99, 100, 88, 89)]                   # closes 89 < prior low 99 -> break down
    rows += [(89, 90, 76, 77)]                    # extends to 76
    rows += _flat(6, 77.0)
    candles = _mk("10:00", rows)

    result = index_module.detect_breakouts(candles, lookback=lookback, horizon=horizon)
    downs = [f for f in result["fires"] if f["direction"] == "down"]
    assert len(downs) == 1
    assert downs[0]["level"] == 99
    assert downs[0]["extension_pts"] == 13.0      # 89 entry - 76 low
    assert downs[0]["hit_10"] is True


def test_no_break_while_price_stays_inside_the_range():
    candles = _mk("10:00", _flat(40))
    result = index_module.detect_breakouts(candles, lookback=10, horizon=5)
    assert result["fires"] == []
    assert result["live"]["status"] == "consolidating"


def test_cooldown_prevents_refiring_on_every_extended_bar():
    lookback, horizon = 10, 3
    rows = _flat(lookback)
    # A sustained ramp: without the cooldown nearly every one of these bars
    # would clear its own trailing window and fire.
    rows += [(100 + i * 5, 103 + i * 5, 99 + i * 5, 102 + i * 5) for i in range(1, 12)]
    candles = _mk("10:00", rows)

    result = index_module.detect_breakouts(candles, lookback=lookback, horizon=horizon)
    assert len(result["fires"]) <= 2, f"cooldown failed, got {len(result['fires'])} fires"


def test_opening_window_bars_never_fire():
    lookback = 10
    rows = _flat(lookback) + [(101, 112, 100, 111)]   # break bar lands at 09:25
    candles = _mk("09:15", rows)
    result = index_module.detect_breakouts(candles, lookback=lookback, horizon=3)
    assert result["fires"] == []


def test_live_state_reports_distance_to_each_break_level():
    candles = _mk("10:00", _flat(20, 100.0))
    live = index_module.detect_breakouts(candles, lookback=10, horizon=3)["live"]
    assert live["status"] == "consolidating"
    assert live["range_high"] == 101
    assert live["range_low"] == 99
    assert live["pts_to_upside_break"] == 1.0     # 101 high - 100 close
    assert live["pts_to_downside_break"] == 1.0   # 100 close - 99 low


def test_is_opening_window_boundary():
    assert index_module._is_opening_window("2026-08-14T09:15:00+05:30") is True
    assert index_module._is_opening_window("2026-08-14T09:39:00+05:30") is True
    assert index_module._is_opening_window("2026-08-14T09:40:00+05:30") is False
    assert index_module._is_opening_window("2026-08-14T14:00:00+05:30") is False


# ---------------- /api/upstox/breakout ----------------

def test_breakout_endpoint_not_connected(monkeypatch):
    _reset_upstox_state(monkeypatch)
    r = client.get("/api/upstox/breakout")
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is False
    assert "not connected" in body["error"]


def test_breakout_endpoint_reports_error_without_enough_candles(monkeypatch):
    monkeypatch.setattr(index_module, "upstox_token", {"access_token": "tok", "obtained_at": "now"})
    empty = FakeUpstoxResponse(200, {"data": {"candles": []}})
    monkeypatch.setattr(index_module.httpx, "AsyncClient",
                        lambda **kw: RoutingFakeUpstoxAsyncClient({"historical-candle": empty}))
    r = client.get("/api/upstox/breakout")
    body = r.json()
    assert body["connected"] is True
    assert "not enough" in body["error"]
