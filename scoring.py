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


def _fmt_level(x) -> str | None:
    if x is None:
        return None
    try:
        return f"{float(x):,.0f}"
    except (TypeError, ValueError):
        return None


def _tone(score: float | None) -> str:
    if score is None:
        return "mixed"
    if score > 0.25:
        return "supportive"
    if score < -0.25:
        return "pressuring"
    return "mixed"


def build_tomorrow_outlook(brief: dict) -> dict:
    """Turn the morning brief numbers into a plain-language 'what to expect
    at tomorrow's open' card. This is an open-scenario helper (gap / flat /
    first-hour plan), not a full-day price prediction — see DISCLAIMER.

    Accepts a brief-shaped dict (score, verdict, predicted_open, expected_*,
    components, news_sentiment). Safe with missing pieces: unavailable
    drivers are skipped rather than inventing direction.
    """
    components = brief.get("components") or {}
    verdict = brief.get("verdict") or "Flat open"
    score = brief.get("score")
    predicted = brief.get("predicted_open")
    low, high = brief.get("expected_low"), brief.get("expected_high")
    confidence = components.get("confidence") or "medium"
    prev_close = components.get("previous_close")
    levels = components.get("levels") or {}
    structure = components.get("structure") or {}
    gift = components.get("gift") or {}
    us_asia = components.get("us_asia") or {}
    macro = components.get("macro") or {}
    fii = components.get("fii") or {}
    participants = components.get("participants") or {}
    cash = components.get("fii_dii_cash") or {}
    news = brief.get("news_sentiment")

    pred_s = _fmt_level(predicted)
    prev_s = _fmt_level(prev_close)
    low_s, high_s = _fmt_level(low), _fmt_level(high)
    pdh_s, pdl_s = _fmt_level(levels.get("pdh")), _fmt_level(levels.get("pdl"))

    if verdict == "Gap-up likely":
        headline = "Tomorrow leans gap-up / constructive open"
        open_expectation = (
            f"Expect Nifty to open above prior close"
            + (f" (~{pred_s})" if pred_s else "")
            + ", with early buyers favoured if the open holds."
        )
        first_hour = [
            "If price holds above the predicted open / prior close in the first 15–30 min, dips toward that level are the usual long-side watch.",
            "If the gap fails quickly and slips back under prior close, treat the open bias as cancelled — wait for structure.",
        ]
    elif verdict == "Gap-down likely":
        headline = "Tomorrow leans gap-down / soft open"
        open_expectation = (
            f"Expect Nifty to open below prior close"
            + (f" (~{pred_s})" if pred_s else "")
            + ", with early sellers favoured if the open holds."
        )
        first_hour = [
            "If price stays below the predicted open / prior close in the first 15–30 min, bounces into that zone are the usual short-side watch.",
            "If the gap is bought aggressively and reclaims prior close, treat the soft-open bias as cancelled — wait for structure.",
        ]
    else:
        headline = "Tomorrow leans flat / indecisive open"
        open_expectation = (
            f"Expect Nifty to open near prior close"
            + (f" (~{pred_s})" if pred_s else "")
            + " — no strong overnight edge; wait for the first impulse."
        )
        first_hour = [
            "Avoid chasing the first spike; let 9:15–9:45 IST define direction.",
            "Trade the break/hold of the opening range or a clear reclaim of prior day high/low rather than the open print itself.",
        ]

    why: list[str] = []
    gift_score = gift.get("score")
    if gift.get("gap_pct") is not None:
        why.append(
            f"GIFT Nifty is {_tone(gift_score)} (gap {gift['gap_pct']:+.2f}% vs fair value"
            + (f", last {gift['price']:,.1f}" if gift.get("price") is not None else "")
            + ") — this is the strongest open cue."
        )
    elif gift_score is not None:
        why.append(f"GIFT Nifty read looks {_tone(gift_score)}.")

    if us_asia.get("score") is not None:
        bits = []
        if us_asia.get("us_avg_pct") is not None:
            bits.append(f"US {us_asia['us_avg_pct']:+.2f}%")
        if us_asia.get("asia_avg_pct") is not None:
            bits.append(f"Asia {us_asia['asia_avg_pct']:+.2f}%")
        why.append(
            f"Overnight equities are {_tone(us_asia['score'])}"
            + (f" ({', '.join(bits)})" if bits else "")
            + "."
        )

    if macro.get("score") is not None:
        flags = macro.get("flags") or {}
        hot = [k for k, v in flags.items() if abs(v) >= 0.4]
        why.append(
            f"Macro is {_tone(macro['score'])} for Nifty"
            + (f" — watch {', '.join(hot)}" if hot else "")
            + "."
        )

    if fii.get("ratio") is not None:
        side = "net long" if fii["ratio"] >= 50 else "net short"
        trend = fii.get("trend") or "flat"
        why.append(
            f"FII index futures are {side} ({fii['ratio']:.1f}% long/short, trend {trend}) "
            f"— positioning bias only, not an intraday trigger."
        )

    if participants:
        crowded = []
        for name in ("Client", "Pro", "DII"):
            p = participants.get(name)
            if not p or p.get("ratio") is None:
                continue
            if p["ratio"] >= 55:
                crowded.append(f"{name} long-heavy")
            elif p["ratio"] <= 45:
                crowded.append(f"{name} short-heavy")
        if crowded:
            why.append("Other participants: " + "; ".join(crowded) + ".")

    if cash.get("fii_buy") is not None and cash.get("fii_sell") is not None:
        net = cash["fii_buy"] - cash["fii_sell"]
        why.append(
            f"FII cash was {'net buyer' if net > 0 else 'net seller' if net < 0 else 'flat'} "
            f"in the latest session (context only)."
        )

    if news:
        why.append(f"News tone: {news}")

    bias = structure.get("bias") or structure.get("current_bias")
    if bias:
        why.append(f"Prior-session structure bias: {bias}.")

    key_levels: list[str] = []
    if pred_s and prev_s:
        key_levels.append(f"Predicted open ~{pred_s} (prior close {prev_s})")
    elif pred_s:
        key_levels.append(f"Predicted open ~{pred_s}")
    elif prev_s:
        key_levels.append(f"Prior close {prev_s}")
    if low_s and high_s:
        key_levels.append(f"Expected reaction band {low_s} – {high_s}")
    if pdl_s and pdh_s:
        key_levels.append(f"Prior day low/high {pdl_s} / {pdh_s}")

    if confidence == "high":
        confidence_note = "Inputs are mostly complete — use as the base open plan, still confirm at 9:15."
    elif confidence == "low":
        confidence_note = "Confidence is low (missing data and/or event day) — treat this as a soft sketch, not a plan."
    else:
        confidence_note = "Confidence is medium — some inputs missing; size down conviction until the open confirms."

    if components.get("is_event_day"):
        confidence_note += " Event/expiry day: expect wider swings; the range is already widened."

    if score is not None:
        headline = f"{headline} (score {score:+.0f})"

    return {
        "headline": headline,
        "open_expectation": open_expectation,
        "why": why,
        "key_levels": key_levels,
        "first_hour_plan": first_hour,
        "confidence_note": confidence_note,
        "scope": "Open + first hour only — not a full-day prediction.",
        "disclaimer": DISCLAIMER,
    }
