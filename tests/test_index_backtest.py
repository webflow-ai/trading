import copy

import index_backtest as bt
import index_engine


def _cfg():
    cfg = copy.deepcopy(index_engine.load_file_config())
    cfg["early_warning"]["alert_score_threshold"] = 70
    cfg["early_warning"]["score_weights"] = {"volume": 1.0, "oi": 0, "vwap": 0, "imbalance": 0}
    cfg["early_warning"]["cooldown_minutes"] = 0
    cfg["early_warning"]["volume_avg_periods"] = 4
    cfg["backtest"]["lookahead_minutes"] = 15
    cfg["backtest"]["move_threshold_pts"] = 40
    cfg["backtest"]["exclude_open_times"] = ["09:15", "09:20", "09:25", "09:30", "09:35"]
    return cfg


def _ts(hhmm):
    return f"2026-08-26T{hhmm}:00+05:30"


def _bar(hhmm, close, volume=1000, oi=5000):
    return {
        "t": _ts(hhmm), "open": close, "high": close + 1, "low": close - 1,
        "close": close, "volume": volume, "oi": oi,
    }


def _session_times():
    times = []
    h, m = 9, 15
    while (h, m) <= (11, 0):
        times.append(f"{h:02d}:{m:02d}")
        m += 5
        if m >= 60:
            h += 1
            m = 0
    return times


def test_replay_precision_recall_and_confusion_on_synthetic_spike():
    times = _session_times()
    # Quiet volume except 10:00 which surges 4x; index jumps +50pts by 10:15
    nifty = []
    stock = []
    for t in times:
        close = 24000
        vol = 1000
        if t >= "10:15":
            close = 24050
        if t == "10:00":
            vol = 4000
        nifty.append(_bar(t, close, volume=1000))
        stock.append(_bar(t, 100, volume=vol))

    constituents = [{"symbol": "HDFCBANK", "name": "HDFC Bank", "isin": "x", "weight_pct": 13.0}]
    result = bt.replay(nifty, constituents, {"HDFCBANK": stock}, _cfg(), apply_cooldown=False)
    m = result["metrics"]
    assert m["bars_scored"] > 0
    assert m["tp"] + m["fp"] + m["fn"] + m["tn"] == m["bars_scored"]
    assert "precision_pct" in m
    assert m["matrix"]["predicted_alert"]["actual_move"] == m["tp"]
    # The 10:00 surge should fire; 15 min later index is +50, so at least one TP
    assert m["tp"] >= 1
    assert any(a["symbol"] == "HDFCBANK" and a["hit"] for a in result["alerts"])


def test_sweep_returns_one_row_per_threshold():
    times = _session_times()
    nifty = [_bar(t, 24000) for t in times]
    stock = [_bar(t, 100, volume=1000) for t in times]
    cfg = _cfg()
    cfg["backtest"]["threshold_sweep"] = [60, 80]
    curve = bt.sweep_thresholds(
        nifty,
        [{"symbol": "HDFCBANK", "name": "HDFC Bank", "isin": "x", "weight_pct": 13.0}],
        {"HDFCBANK": stock},
        cfg,
    )
    assert [row["threshold"] for row in curve] == [60, 80]
    assert "precision_pct" in curve[0] and "recall_pct" in curve[0]
