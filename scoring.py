"""
scoring.py — weighted pre-market score and expected range. Module 4 (second
half) of the build order (docs/PREMARKET_ENGINE.md).

Every source is optional: a missing component is dropped rather than
counted as 0, and the remaining weights are renormalized to sum to 100 —
a dead source should never silently push the score toward "flat".
"""

GIFT_FAIR_VALUE_PREMIUM = 35.0
GIFT_FULL_WEIGHT_GAP_PCT = 0.5

# The brief only specifies GIFT/macro thresholds explicitly; +/-1% on the
# blended US+Asia read is a chosen default (an index move that size is
# already a strong pre-market cue) — open to retuning once real briefs are
# being compared against actual opens.
US_ASIA_FULL_WEIGHT_PCT = 1.0
ASIA_WEIGHT, US_WEIGHT = 0.6, 0.4

CRUDE_FULL_WEIGHT_PCT = 2.0
USDINR_FULL_WEIGHT_PCT = 0.3
DXY_FULL_WEIGHT_PCT = 0.5
US10Y_FULL_WEIGHT_BPS = 10.0

# +/-10 points off the 50% (long == short) midpoint maps to full weight.
FII_RATIO_FULL_WEIGHT_POINTS = 10.0
FII_TREND_NUDGE = 0.2

EVENT_RANGE_WIDEN_FRACTION = 0.25

# Calibration for the score-anchored fallback in compute_predicted_open: a
# score of +/-100 (every component maxed out in the same direction) implies
# roughly a +/-1.5% pre-market move off the previous close — already a very
# large gap for Nifty in one session. This is a chosen default, same caveat
# as US_ASIA_FULL_WEIGHT_PCT above: untested against real outcomes yet.
PREDICTED_OPEN_MAX_MOVE_PCT = 1.5

WEIGHTS = {"gift": 40, "us_asia": 20, "macro": 15, "fii": 15}

DISCLAIMER = "Automated analysis for information only — not investment advice."


def _clip(x: float) -> float:
    return max(-1.0, min(1.0, x))


def _score_gift(previous_close: float | None, gift_price: float | None) -> dict | None:
    if previous_close is None or gift_price is None:
        return None
    fair_value = previous_close + GIFT_FAIR_VALUE_PREMIUM
    if not fair_value:
        return None
    gap_pct = (gift_price - fair_value) / fair_value * 100
    return {
        "score": round(_clip(gap_pct / GIFT_FULL_WEIGHT_GAP_PCT), 4),
        "gap_pct": round(gap_pct, 4),
        "fair_value": round(fair_value, 2),
    }


def _avg_pct_change(quotes: dict) -> float | None:
    changes = [q["pct_change"] for q in (quotes or {}).values() if q and q.get("pct_change") is not None]
    return sum(changes) / len(changes) if changes else None


def _score_us_asia(us_quotes: dict, asia_quotes: dict) -> dict | None:
    us_avg = _avg_pct_change(us_quotes)
    asia_avg = _avg_pct_change(asia_quotes)
    if us_avg is None and asia_avg is None:
        return None
    if us_avg is not None and asia_avg is not None:
        combined = US_WEIGHT * us_avg + ASIA_WEIGHT * asia_avg
    else:
        combined = asia_avg if asia_avg is not None else us_avg
    return {
        "score": round(_clip(combined / US_ASIA_FULL_WEIGHT_PCT), 4),
        "us_avg_pct": round(us_avg, 4) if us_avg is not None else None,
        "asia_avg_pct": round(asia_avg, 4) if asia_avg is not None else None,
    }


def _score_macro(crude_pct, usdinr_pct, dxy_pct, us10y_bps) -> dict | None:
    # Each of these moving *up* is bearish for Nifty (India is a net oil
    # importer; rupee weakness and a stronger dollar/higher US yields both
    # pull FII flows out of EM equities) — hence the negative sign on all
    # four rather than a mix.
    flags = {}
    if crude_pct is not None:
        flags["crude"] = _clip(-crude_pct / CRUDE_FULL_WEIGHT_PCT)
    if usdinr_pct is not None:
        flags["usdinr"] = _clip(-usdinr_pct / USDINR_FULL_WEIGHT_PCT)
    if dxy_pct is not None:
        flags["dxy"] = _clip(-dxy_pct / DXY_FULL_WEIGHT_PCT)
    if us10y_bps is not None:
        flags["us10y"] = _clip(-us10y_bps / US10Y_FULL_WEIGHT_BPS)
    if not flags:
        return None
    return {"score": round(sum(flags.values()) / len(flags), 4), "flags": {k: round(v, 4) for k, v in flags.items()}}


def _score_fii(fii_ratio: float | None, fii_trend: str | None) -> dict | None:
    if fii_ratio is None:
        return None
    level = _clip((fii_ratio - 50.0) / FII_RATIO_FULL_WEIGHT_POINTS)
    nudge = {"rising": FII_TREND_NUDGE, "falling": -FII_TREND_NUDGE}.get(fii_trend, 0.0)
    return {"score": round(_clip(level + nudge), 4), "ratio": fii_ratio, "trend": fii_trend}


def compute_score(inputs: dict) -> dict:
    """inputs: previous_close, gift_price, us_quotes, asia_quotes,
    crude_pct_change, usdinr_pct_change, dxy_pct_change, us10y_change_bps,
    fii_ratio, fii_trend, is_event_day. Any may be missing/None.

    The event flag is deliberately NOT part of the weighted directional sum
    — an expiry/event day doesn't imply a direction, it only lowers
    confidence and widens the expected range (see compute_expected_range),
    per the brief's own instruction that it should "reduce confidence and
    widen expected range rather than shift direction."
    """
    component_scores = {
        "gift": _score_gift(inputs.get("previous_close"), inputs.get("gift_price")),
        "us_asia": _score_us_asia(inputs.get("us_quotes"), inputs.get("asia_quotes")),
        "macro": _score_macro(
            inputs.get("crude_pct_change"), inputs.get("usdinr_pct_change"),
            inputs.get("dxy_pct_change"), inputs.get("us10y_change_bps"),
        ),
        "fii": _score_fii(inputs.get("fii_ratio"), inputs.get("fii_trend")),
    }
    available = {k: v for k, v in component_scores.items() if v is not None}
    missing = [k for k in component_scores if k not in available]

    if available:
        total_weight = sum(WEIGHTS[k] for k in available)
        score = sum(WEIGHTS[k] * available[k]["score"] for k in available) / total_weight * 100
    else:
        score = 0.0
    score = round(score, 2)

    if score > 25:
        verdict = "Gap-up likely"
    elif score < -25:
        verdict = "Gap-down likely"
    else:
        verdict = "Flat open"

    is_event_day = bool(inputs.get("is_event_day"))
    if is_event_day or len(missing) >= 2:
        confidence = "low"
    elif missing:
        confidence = "medium"
    else:
        confidence = "high"

    return {
        "score": score,
        "verdict": verdict,
        "components": component_scores,
        "weights_used": {k: WEIGHTS[k] for k in available},
        "missing": missing,
        "is_event_day": is_event_day,
        "confidence": confidence,
    }


def compute_expected_range(option_snap: dict | None, levels: dict | None, is_event_day: bool = False) -> dict:
    """[max_put_oi_strike, max_call_oi_strike] from the option snapshot when
    available, else PDL-PDH from technicals.compute_levels(). On an event
    day the range is widened around its midpoint rather than the direction
    changed (see compute_score's docstring)."""
    option_snap = option_snap or {}
    levels = levels or {}
    low, high, source = option_snap.get("max_put_oi_strike"), option_snap.get("max_call_oi_strike"), "option_chain"
    if low is None or high is None:
        low, high, source = levels.get("pdl"), levels.get("pdh"), "pdh_pdl"
    if low is None or high is None:
        return {"low": None, "high": None, "source": None}
    if is_event_day:
        pad = (high - low) * EVENT_RANGE_WIDEN_FRACTION
        low, high = low - pad, high + pad
        source += "_widened_for_event"
    return {"low": round(low, 2), "high": round(high, 2), "source": source}


def compute_predicted_open(previous_close: float | None, gift_price: float | None, score: float | None) -> dict | None:
    """A single point estimate for where Nifty is likely to open — still
    just an estimate (see DISCLAIMER), not the same thing as
    compute_expected_range's low/high band. None when nothing usable is
    available.

    Two methods, tried in order:
      1. gift_anchored — GIFT Nifty already reflects the market's live
         overnight pricing (US/Asia moves, macro, flows) since it's an
         actual traded instrument, not a derived score. The standard
         practitioner heuristic is "Nifty tends to open near GIFT Nifty
         minus its usual premium," so this is used whenever GIFT is
         available rather than re-deriving the same information from the
         weighted score (which would double-count GIFT's own contribution).
      2. score_anchored — fallback for when GIFT is unavailable: previous
         close moved by the weighted score, scaled by
         PREDICTED_OPEN_MAX_MOVE_PCT.
    """
    if gift_price is not None:
        return {"value": round(gift_price - GIFT_FAIR_VALUE_PREMIUM, 2), "method": "gift_anchored"}
    if previous_close is not None and score is not None:
        predicted = previous_close * (1 + (score / 100) * PREDICTED_OPEN_MAX_MOVE_PCT / 100)
        return {"value": round(predicted, 2), "method": "score_anchored"}
    return None
