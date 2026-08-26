"""
index_attribution.py — deterministic Nifty point-contribution math.

Separate from index_early_warning.py on purpose: this module is an
accounting identity (stock return × free-float weight × index level), not
a forecast. Near-100% reliable given live quotes and current weights;
any residual vs the real index is uncovered constituents + stale ticks,
never "the model was wrong."
"""

from __future__ import annotations


def point_contribution(ltp: float | None, prev_close: float | None,
                       weight_pct: float, index_scale: float | None) -> float | None:
    """Points of index attributed to one constituent since previous close.

    Spec form: (ltp - prev_close) / prev_close × weight% × index_divisor_constant
    with index_divisor_constant = index_scale / 100 when weight_pct is in
    percent units (13.0, not 0.13). Using the index previous close as
    index_scale keeps the day's sum comparable to (index_ltp - index_prev_close).
    """
    if ltp is None or not prev_close or not index_scale or weight_pct is None:
        return None
    ret = (ltp - prev_close) / prev_close
    return ret * (weight_pct / 100.0) * index_scale


def rank_contributions(rows: list[dict]) -> list[dict]:
    """Sort by absolute point contribution, descending. Nulls sink to the end."""
    return sorted(
        rows,
        key=lambda r: (r.get("contribution_pts") is None, -abs(r.get("contribution_pts") or 0)),
    )


def reconcile(sum_pts: float | None, actual_index_pts: float | None,
              coverage_pct: float, flag_pts: float) -> dict:
    """Compare summed constituent points to the live index move.

    Top-20 coverage is ~75-80%, so unexplained_pts is *expected* to be
    non-zero (the leftover names). Staleness is flagged only when the
    residual is larger than both the uncovered-weight bound and flag_pts.
    """
    unexplained = None
    if sum_pts is not None and actual_index_pts is not None:
        unexplained = actual_index_pts - sum_pts

    uncovered = max(0.0, 100.0 - coverage_pct) / 100.0
    expected_abs_residual = None
    if actual_index_pts is not None:
        expected_abs_residual = abs(actual_index_pts) * uncovered

    stale = False
    if unexplained is not None and expected_abs_residual is not None:
        stale = abs(unexplained) > max(flag_pts, expected_abs_residual + flag_pts * 0.4)

    return {
        "sum_contribution_pts": None if sum_pts is None else round(sum_pts, 2),
        "actual_index_pts": None if actual_index_pts is None else round(actual_index_pts, 2),
        "unexplained_pts": None if unexplained is None else round(unexplained, 2),
        "coverage_pct": round(coverage_pct, 2),
        "expected_uncovered_pts": None if expected_abs_residual is None else round(expected_abs_residual, 2),
        "reconciliation_stale": stale,
    }


def build_attribution(constituents: list[dict], quotes_by_isin: dict,
                      index_ltp: float | None, index_prev_close: float | None,
                      stale_isins: set[str] | None = None,
                      tracking_error_flag_pts: float = 25.0) -> dict:
    """quotes_by_isin: {isin: {ltp, prev_close, last_trade_time, volume, oi, depth}}."""
    stale_isins = stale_isins or set()
    index_scale = index_prev_close
    actual_pts = None
    if index_ltp is not None and index_prev_close:
        actual_pts = index_ltp - index_prev_close

    rows = []
    sum_pts = 0.0
    any_pts = False
    covered = 0.0
    missing = 0
    for c in constituents:
        q = quotes_by_isin.get(c["isin"]) or {}
        ltp = q.get("ltp")
        prev = q.get("prev_close")
        pts = point_contribution(ltp, prev, c["weight_pct"], index_scale)
        pct = None
        if ltp is not None and prev:
            pct = (ltp - prev) / prev * 100
        if pts is not None:
            sum_pts += pts
            any_pts = True
            covered += c["weight_pct"]
        else:
            missing += 1
        rows.append({
            "symbol": c["symbol"],
            "name": c["name"],
            "isin": c["isin"],
            "weight_pct": c["weight_pct"],
            "ff_mcap_crore": c.get("ff_mcap_crore"),
            "ltp": ltp,
            "prev_close": prev,
            "pct_change": None if pct is None else round(pct, 4),
            "contribution_pts": None if pts is None else round(pts, 3),
            "volume": q.get("volume"),
            "oi": q.get("oi"),
            "last_trade_time": q.get("last_trade_time"),
            "quote_stale": c["isin"] in stale_isins or ltp is None,
        })

    ranked = rank_contributions(rows)
    recon = reconcile(
        sum_pts if any_pts else None,
        actual_pts,
        covered,
        tracking_error_flag_pts,
    )
    recon["missing_quotes"] = missing
    recon["data_stale"] = bool(missing or stale_isins or recon["reconciliation_stale"])
    return {
        "index_ltp": index_ltp,
        "index_prev_close": index_prev_close,
        "index_scale": index_scale,
        "stocks": ranked,
        "reconciliation": recon,
    }
