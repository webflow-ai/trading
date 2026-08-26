import index_attribution as ia


def test_point_contribution_matches_spec_formula():
    # 1% stock move, 13% weight, index prev close 25000 → 32.5 index points
    pts = ia.point_contribution(ltp=101, prev_close=100, weight_pct=13.0, index_scale=25000)
    assert round(pts, 4) == 32.5


def test_point_contribution_degrades_on_missing_inputs():
    assert ia.point_contribution(None, 100, 13, 25000) is None
    assert ia.point_contribution(101, 0, 13, 25000) is None
    assert ia.point_contribution(101, 100, 13, None) is None


def test_rank_puts_largest_absolute_contribution_first():
    rows = [
        {"symbol": "A", "contribution_pts": 2.0},
        {"symbol": "B", "contribution_pts": -9.0},
        {"symbol": "C", "contribution_pts": None},
        {"symbol": "D", "contribution_pts": 4.0},
    ]
    ranked = ia.rank_contributions(rows)
    assert [r["symbol"] for r in ranked] == ["B", "D", "A", "C"]


def test_reconcile_expected_residual_from_uncovered_weight():
    recon = ia.reconcile(sum_pts=80.0, actual_index_pts=100.0, coverage_pct=80.0, flag_pts=25.0)
    assert recon["unexplained_pts"] == 20.0
    assert recon["coverage_pct"] == 80.0
    assert recon["expected_uncovered_pts"] == 20.0
    # 20 pts unexplained with 20% uncovered is expected, not stale
    assert recon["reconciliation_stale"] is False


def test_reconcile_flags_residual_far_beyond_uncovered_weight():
    recon = ia.reconcile(sum_pts=10.0, actual_index_pts=100.0, coverage_pct=80.0, flag_pts=25.0)
    assert recon["reconciliation_stale"] is True


def test_build_attribution_uses_index_prev_close_as_scale():
    constituents = [
        {"symbol": "HDFCBANK", "name": "HDFC Bank", "isin": "INE1", "weight_pct": 13.0, "ff_mcap_crore": 1},
        {"symbol": "INFY", "name": "Infosys", "isin": "INE2", "weight_pct": 5.0, "ff_mcap_crore": 1},
    ]
    quotes = {
        "INE1": {"ltp": 110, "prev_close": 100},  # +10%
        "INE2": {"ltp": 95, "prev_close": 100},   # -5%
    }
    out = ia.build_attribution(constituents, quotes, index_ltp=25100, index_prev_close=25000)
    hdfc = next(s for s in out["stocks"] if s["symbol"] == "HDFCBANK")
    infy = next(s for s in out["stocks"] if s["symbol"] == "INFY")
    assert hdfc["contribution_pts"] == 325.0  # 0.10 * 0.13 * 25000
    assert infy["contribution_pts"] == -62.5  # -0.05 * 0.05 * 25000
    assert out["stocks"][0]["symbol"] == "HDFCBANK"
    assert out["reconciliation"]["sum_contribution_pts"] == 262.5
    assert out["reconciliation"]["actual_index_pts"] == 100.0
    assert out["reconciliation"]["coverage_pct"] == 18.0
