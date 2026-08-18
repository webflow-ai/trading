import scoring


def _gift_only_inputs(gap_pct: float) -> dict:
    """previous_close=965 -> fair_value=1000, so gift_price can be picked to
    land on an exact gap_pct with no floating-point surprises."""
    fair_value = 1000.0
    gift_price = fair_value * (1 + gap_pct / 100)
    return {"previous_close": 965.0, "gift_price": gift_price}


# ---------------- verdict boundaries ----------------

def test_score_flat_open_at_exact_positive_25_boundary():
    result = scoring.compute_score(_gift_only_inputs(0.125))  # -> score exactly +25.0
    assert result["score"] == 25.0
    assert result["verdict"] == "Flat open"


def test_score_flat_open_at_exact_negative_25_boundary():
    result = scoring.compute_score(_gift_only_inputs(-0.125))  # -> score exactly -25.0
    assert result["score"] == -25.0
    assert result["verdict"] == "Flat open"


def test_score_gap_up_likely_just_past_positive_25_boundary():
    result = scoring.compute_score(_gift_only_inputs(0.2))  # -> score = 40.0
    assert result["score"] == 40.0
    assert result["verdict"] == "Gap-up likely"


def test_score_gap_down_likely_just_past_negative_25_boundary():
    result = scoring.compute_score(_gift_only_inputs(-0.2))  # -> score = -40.0
    assert result["score"] == -40.0
    assert result["verdict"] == "Gap-down likely"


# ---------------- missing components / renormalization ----------------

def test_score_all_components_missing_is_zero_flat_low_confidence():
    result = scoring.compute_score({})
    assert result["score"] == 0.0
    assert result["verdict"] == "Flat open"
    assert set(result["missing"]) == {"gift", "us_asia", "macro", "fii"}
    assert result["weights_used"] == {}
    assert result["confidence"] == "low"


def test_score_renormalizes_to_100_percent_weight_when_only_one_component_present():
    # gap_pct beyond the full-weight threshold clips to +1.0, and with gift
    # the only available component its 40% weight is renormalized to 100%.
    result = scoring.compute_score(_gift_only_inputs(1.0))
    assert result["score"] == 100.0
    assert result["verdict"] == "Gap-up likely"
    assert result["weights_used"] == {"gift": 40}
    assert set(result["missing"]) == {"us_asia", "macro", "fii"}


def test_score_confidence_medium_with_exactly_one_missing_component():
    inputs = {
        **_gift_only_inputs(0.05),
        "us_quotes": {"^DJI": {"pct_change": 0.1}},
        "asia_quotes": {"^N225": {"pct_change": 0.1}},
        "fii_ratio": 52, "fii_trend": "flat",
        # macro left out entirely -> exactly one missing component
    }
    result = scoring.compute_score(inputs)
    assert result["missing"] == ["macro"]
    assert result["confidence"] == "medium"


def test_score_confidence_high_when_nothing_missing_and_not_event_day():
    inputs = {
        **_gift_only_inputs(0.05),
        "us_quotes": {"^DJI": {"pct_change": 0.1}},
        "asia_quotes": {"^N225": {"pct_change": 0.1}},
        "crude_pct_change": 0.1, "usdinr_pct_change": 0.05, "dxy_pct_change": 0.05, "us10y_change_bps": 1,
        "fii_ratio": 52, "fii_trend": "flat",
        "is_event_day": False,
    }
    result = scoring.compute_score(inputs)
    assert result["missing"] == []
    assert result["confidence"] == "high"


def test_score_confidence_low_on_event_day_even_with_full_data():
    inputs = {
        **_gift_only_inputs(0.05),
        "us_quotes": {"^DJI": {"pct_change": 0.1}},
        "asia_quotes": {"^N225": {"pct_change": 0.1}},
        "crude_pct_change": 0.1, "usdinr_pct_change": 0.05, "dxy_pct_change": 0.05, "us10y_change_bps": 1,
        "fii_ratio": 52, "fii_trend": "flat",
        "is_event_day": True,
    }
    result = scoring.compute_score(inputs)
    assert result["missing"] == []
    assert result["confidence"] == "low"
    assert result["is_event_day"] is True


# ---------------- component direction/scaling ----------------

def test_macro_component_all_four_flags_moving_up_is_fully_bearish():
    inputs = {
        "crude_pct_change": 2.0, "usdinr_pct_change": 0.3, "dxy_pct_change": 0.5, "us10y_change_bps": 10,
    }
    result = scoring.compute_score(inputs)
    assert result["components"]["macro"]["score"] == -1.0
    assert result["score"] == -100.0
    assert result["verdict"] == "Gap-down likely"


def test_fii_component_neutral_at_ratio_50_with_flat_trend():
    result = scoring.compute_score({"fii_ratio": 50.0, "fii_trend": "flat"})
    assert result["components"]["fii"]["score"] == 0.0
    assert result["score"] == 0.0


def test_fii_component_rising_trend_nudges_score_positive_even_at_neutral_ratio():
    result = scoring.compute_score({"fii_ratio": 50.0, "fii_trend": "rising"})
    assert result["components"]["fii"]["score"] == 0.2


# ---------------- expected range ----------------

def test_expected_range_uses_option_chain_when_available():
    result = scoring.compute_expected_range(
        {"max_put_oi_strike": 24400, "max_call_oi_strike": 24700}, levels=None,
    )
    assert result == {"low": 24400, "high": 24700, "source": "option_chain"}


def test_expected_range_falls_back_to_pdh_pdl_when_option_chain_unavailable():
    result = scoring.compute_expected_range(
        {"max_put_oi_strike": None, "max_call_oi_strike": None},
        levels={"pdl": 24350, "pdh": 24680},
    )
    assert result == {"low": 24350, "high": 24680, "source": "pdh_pdl"}


def test_expected_range_all_none_when_nothing_available():
    assert scoring.compute_expected_range(None, None) == {"low": None, "high": None, "source": None}


def test_expected_range_widens_symmetrically_on_event_day():
    result = scoring.compute_expected_range(
        {"max_put_oi_strike": 24400, "max_call_oi_strike": 24700}, levels=None, is_event_day=True,
    )
    assert result == {"low": 24325.0, "high": 24775.0, "source": "option_chain_widened_for_event"}


def test_disclaimer_is_the_exact_required_line():
    assert scoring.DISCLAIMER == "Automated analysis for information only — not investment advice."


# ---------------- predicted open ----------------

def test_predicted_open_is_gift_anchored_when_gift_available():
    result = scoring.compute_predicted_open(previous_close=24500.0, gift_price=24560.0, score=10.0)
    assert result == {"value": 24525.0, "method": "gift_anchored"}  # 24560 - 35 premium


def test_predicted_open_ignores_previous_close_when_gift_present():
    # gift-anchored deliberately doesn't re-derive from previous_close/score
    # to avoid double-counting what GIFT already prices in.
    with_close = scoring.compute_predicted_open(previous_close=20000.0, gift_price=24560.0, score=90.0)
    without_close = scoring.compute_predicted_open(previous_close=None, gift_price=24560.0, score=90.0)
    assert with_close == without_close == {"value": 24525.0, "method": "gift_anchored"}


def test_predicted_open_falls_back_to_score_anchored_without_gift():
    # score=100 -> full +1.5% move off previous_close
    result = scoring.compute_predicted_open(previous_close=24000.0, gift_price=None, score=100.0)
    assert result == {"value": round(24000.0 * 1.015, 2), "method": "score_anchored"}


def test_predicted_open_score_anchored_negative_score_moves_down():
    result = scoring.compute_predicted_open(previous_close=24000.0, gift_price=None, score=-50.0)
    assert result["value"] < 24000.0
    assert result["method"] == "score_anchored"


def test_predicted_open_none_when_nothing_available():
    assert scoring.compute_predicted_open(previous_close=None, gift_price=None, score=None) is None
    assert scoring.compute_predicted_open(previous_close=None, gift_price=None, score=10.0) is None


# ---------------- tomorrow outlook (plain-language open scenario) ----------------

def _sample_brief(**overrides) -> dict:
    base = {
        "score": 42.0,
        "verdict": "Gap-up likely",
        "predicted_open": 24650.0,
        "expected_low": 24500.0,
        "expected_high": 24800.0,
        "news_sentiment": "Mildly constructive overnight cues",
        "components": {
            "confidence": "high",
            "previous_close": 24580.0,
            "gift": {"score": 0.8, "gap_pct": 0.35, "fair_value": 24615.0, "price": 24700.0},
            "us_asia": {"score": 0.3, "us_avg_pct": 0.4, "asia_avg_pct": 0.2},
            "macro": {"score": -0.1, "flags": {"crude": -0.2}},
            "fii": {"score": 0.4, "ratio": 56.0, "trend": "rising"},
            "participants": {
                "Client": {"ratio": 42.0},
                "FII": {"ratio": 56.0},
            },
            "levels": {"pdh": 24720.0, "pdl": 24480.0},
            "structure": {"bias": "bullish"},
        },
    }
    base.update(overrides)
    return base


def test_outlook_gap_up_has_clear_open_expectation_and_first_hour_plan():
    outlook = scoring.build_tomorrow_outlook(_sample_brief())
    assert "gap-up" in outlook["headline"].lower()
    assert "above prior close" in outlook["open_expectation"]
    assert "~24,650" in outlook["open_expectation"] or "24650" in outlook["open_expectation"].replace(",", "")
    assert len(outlook["why"]) >= 3
    assert any("GIFT" in w for w in outlook["why"])
    assert any("FII" in w for w in outlook["why"])
    assert any("Predicted open" in k for k in outlook["key_levels"])
    assert len(outlook["first_hour_plan"]) == 2
    assert "Open + first hour" in outlook["scope"]
    assert outlook["disclaimer"] == scoring.DISCLAIMER


def test_outlook_gap_down_and_flat_wording():
    down = scoring.build_tomorrow_outlook(_sample_brief(score=-40.0, verdict="Gap-down likely"))
    assert "gap-down" in down["headline"].lower()
    assert "below prior close" in down["open_expectation"]

    flat = scoring.build_tomorrow_outlook(_sample_brief(score=5.0, verdict="Flat open"))
    assert "flat" in flat["headline"].lower()
    assert "near prior close" in flat["open_expectation"]
    assert any("9:15" in p for p in flat["first_hour_plan"])


def test_outlook_tolerates_empty_brief():
    outlook = scoring.build_tomorrow_outlook({"score": 0, "verdict": "Flat open", "components": {}})
    assert "flat" in outlook["headline"].lower()
    assert outlook["why"] == []
    assert "low" in outlook["confidence_note"].lower() or "medium" in outlook["confidence_note"].lower()


def test_outlook_flags_event_day_in_confidence_note():
    outlook = scoring.build_tomorrow_outlook(_sample_brief(components={
        **_sample_brief()["components"],
        "confidence": "low",
        "is_event_day": True,
    }))
    assert "Event" in outlook["confidence_note"] or "event" in outlook["confidence_note"]
