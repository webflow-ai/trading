import index_early_warning as ew

CANDLES = [
    {"t": f"2026-08-26T09:{15 + i:02d}:00+05:30", "open": 100, "high": 101, "low": 99,
     "close": 100 + (i % 3) * 0.2, "volume": 1000, "oi": 5000 + i * 10}
    for i in range(25)
]


def test_session_vwap_typical_price():
    bars = [
        {"high": 12, "low": 8, "close": 10, "volume": 100},  # tp 10
        {"high": 20, "low": 10, "close": 15, "volume": 100},  # tp 15
    ]
    assert ew.session_vwap(bars) == 12.5


def test_volume_surge_ratio():
    bars = [{"volume": 10} for _ in range(20)] + [{"volume": 40}]
    assert ew.volume_surge_ratio(bars, 20) == 4.0
    assert ew.volume_surge_ratio(bars[:10], 20) is None


def test_oi_change_pct_over_lookback():
    bars = [{"oi": 100}, {"oi": 110}, {"oi": 120}, {"oi": 150}]
    # lookback 3: 100 → 150 = +50%
    assert round(ew.oi_change_pct(bars, 3), 6) == 50.0


def test_depth_imbalance_best_five():
    depth = {
        "buy": [{"quantity": 80}] * 5,
        "sell": [{"quantity": 20}] * 5,
    }
    # (400-100)/500 = 0.6
    assert abs(ew.depth_imbalance(depth) - 0.6) < 1e-9
    assert ew.depth_imbalance(None) is None
    assert ew.depth_imbalance({"buy": [], "sell": []}) is None


def test_urgency_score_renormalizes_when_oi_missing():
    features = {"volume_surge": 4.0, "oi_change_pct": None, "vwap_dev_pct": 0.0, "imbalance": 0.0}
    weights = {"volume": 0.40, "oi": 0.20, "vwap": 0.25, "imbalance": 0.15}
    full = {"volume": 4.0, "oi": 8.0, "vwap": 1.5, "imbalance": 0.55}
    out = ew.urgency_score(features, weights, full)
    assert out["score"] is not None
    assert "oi" not in out["weights_used"]
    assert "volume" in out["weights_used"]
    # volume at full_score → 100, others 0 → score is volume's share of remaining weights
    remaining = 0.40 + 0.25 + 0.15
    assert out["score"] == round(100 * 0.40 / remaining, 1)


def test_should_alert_respects_threshold_weight_and_cooldown():
    cfg = {
        "early_warning": {
            "alert_score_threshold": 78,
            "min_weight_pct_to_alert": 1.4,
            "cooldown_minutes": 45,
        }
    }
    row = {"symbol": "HDFCBANK", "score": 80, "weight_pct": 13.0}
    assert ew.should_alert(row, cfg, {}, 1000) is True
    assert ew.should_alert({**row, "score": 70}, cfg, {}, 1000) is False
    assert ew.should_alert({**row, "weight_pct": 1.0}, cfg, {}, 1000) is False
    assert ew.should_alert(row, cfg, {"HDFCBANK": 1000}, 1000 + 10 * 60) is False
    assert ew.should_alert(row, cfg, {"HDFCBANK": 1000}, 1000 + 46 * 60) is True


def test_format_alert_includes_disclaimer_fields():
    msg = ew.format_alert({
        "symbol": "RELIANCE", "reasons": ["volume 3.2× average"],
        "weight_pct": 8.5, "potential_index_pts": 22.0,
    })
    assert "RELIANCE showing volume 3.2× average" in msg
    assert "Weight 8.5%" in msg
    assert "~22.0 pts" in msg


def test_score_constituent_volume_spike_lifts_score():
    cfg = {
        "early_warning": {
            "score_weights": {"volume": 1.0, "oi": 0.0, "vwap": 0.0, "imbalance": 0.0},
            "volume_avg_periods": 20,
            "volume_surge_full_score": 4.0,
            "oi_lookback_bars": 3,
            "oi_change_full_score_pct": 8.0,
            "vwap_dev_full_score_pct": 1.5,
            "imbalance_full_score": 0.55,
        }
    }
    quiet = [{**c, "volume": 1000} for c in CANDLES[:-1]] + [{**CANDLES[-1], "volume": 4000, "close": 100}]
    row = ew.score_constituent(
        {"symbol": "HDFCBANK", "name": "HDFC Bank", "weight_pct": 13.0},
        quiet, ltp=100, depth=None, cfg=cfg, index_scale=25000,
    )
    assert row["volume_surge"] == 4.0
    assert row["score"] == 100.0
    assert row["potential_index_pts"] is not None
