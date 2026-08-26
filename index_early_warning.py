"""
index_early_warning.py — probabilistic urgency scores for heavy-weight
Nifty constituents.

This is *not* attribution. Scores are 0-100 heuristics (volume surge, OI
change, VWAP deviation, order-book imbalance) whose weights and
thresholds live in config/index_engine.json so they can be tuned against
index_backtest.py. Treat every alert as a probability signal, never a
prediction.
"""

from __future__ import annotations


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _safe_div(n, d) -> float | None:
    if n is None or d in (None, 0):
        return None
    return n / d


def session_vwap(candles: list[dict]) -> float | None:
    """Typical-price VWAP over the provided session candles (oldest-first)."""
    num = den = 0.0
    for c in candles:
        h, l, cl, vol = c.get("high"), c.get("low"), c.get("close"), c.get("volume") or 0
        if None in (h, l, cl) or vol <= 0:
            continue
        tp = (h + l + cl) / 3.0
        num += tp * vol
        den += vol
    return num / den if den else None


def volume_surge_ratio(candles: list[dict], periods: int) -> float | None:
    """Last bar volume vs mean of the prior `periods` bars. Needs periods+1 bars."""
    vols = [c.get("volume") or 0 for c in candles if (c.get("volume") or 0) > 0]
    if len(vols) < periods + 1:
        return None
    current = vols[-1]
    avg = sum(vols[-(periods + 1):-1]) / periods
    return _safe_div(current, avg)


def oi_change_pct(candles: list[dict], lookback_bars: int) -> float | None:
    """% change in candle OI over the last lookback_bars (5-min bars → 5-15 min)."""
    ois = [c.get("oi") for c in candles if c.get("oi") not in (None, 0)]
    if len(ois) < lookback_bars + 1:
        return None
    prev, cur = ois[-(lookback_bars + 1)], ois[-1]
    return _safe_div((cur - prev) * 100.0, prev)


def vwap_deviation(price: float | None, vwap: float | None) -> float | None:
    """(price - vwap) / vwap as a fraction, not percent."""
    if price is None or not vwap:
        return None
    return (price - vwap) / vwap


def vwap_deviation_roc(prev_dev: float | None, cur_dev: float | None) -> float | None:
    if prev_dev is None or cur_dev is None:
        return None
    return cur_dev - prev_dev


def depth_imbalance(depth: dict | None) -> float | None:
    """(bid_qty - ask_qty) / (bid_qty + ask_qty) across best 5 levels. None if no book."""
    if not depth:
        return None
    buy = sum((lvl.get("quantity") or 0) for lvl in (depth.get("buy") or [])[:5])
    sell = sum((lvl.get("quantity") or 0) for lvl in (depth.get("sell") or [])[:5])
    total = buy + sell
    if total <= 0:
        return None
    return (buy - sell) / total


def tick_imbalance(candles: list[dict]) -> float | None:
    """Fallback when depth is missing: uptick vs downtick share of last 20 closes."""
    closes = [c.get("close") for c in candles if c.get("close") is not None]
    if len(closes) < 8:
        return None
    window = closes[-21:] if len(closes) > 21 else closes
    up = down = 0
    for a, b in zip(window, window[1:]):
        if b > a:
            up += 1
        elif b < a:
            down += 1
    total = up + down
    if not total:
        return None
    return (up - down) / total


def _component_score(value: float | None, full_at: float) -> float | None:
    """Map |value| so 0 → 0 and |full_at| → 100."""
    if value is None or not full_at:
        return None
    return 100.0 * _clamp01(abs(value) / abs(full_at))


def urgency_score(features: dict, weights: dict, full_score: dict) -> dict:
    """Weighted 0-100 score from whatever features are present; missing ones
    are dropped and the remaining weights re-normalized so a skipped OI or
    depth feed doesn't silently zero the whole score."""
    parts = {
        "volume": _component_score(features.get("volume_surge"), full_score["volume"]),
        "oi": _component_score(features.get("oi_change_pct"), full_score["oi"]),
        "vwap": _component_score(features.get("vwap_dev_pct"), full_score["vwap"]),
        "imbalance": _component_score(features.get("imbalance"), full_score["imbalance"]),
    }
    used = {k: parts[k] for k in parts if parts[k] is not None and weights.get(k, 0) > 0}
    wsum = sum(weights.get(k, 0) for k in used)
    score = None
    if used and wsum > 0:
        score = sum(parts[k] * weights[k] for k in used) / wsum
        score = round(min(100.0, max(0.0, score)), 1)

    reasons = []
    if features.get("volume_surge") is not None and (features["volume_surge"] or 0) >= 2.0:
        reasons.append(f"volume {features['volume_surge']:.1f}× average")
    if features.get("oi_change_pct") is not None and abs(features["oi_change_pct"]) >= 3:
        reasons.append(f"OI {features['oi_change_pct']:+.1f}% over lookback")
    if features.get("vwap_dev_pct") is not None and abs(features["vwap_dev_pct"]) >= 0.4:
        reasons.append(f"VWAP {features['vwap_dev_pct']:+.2f}%")
    if features.get("imbalance") is not None and abs(features["imbalance"]) >= 0.25:
        side = "bid" if features["imbalance"] > 0 else "ask"
        reasons.append(f"{side}-heavy book ({features['imbalance']:+.2f})")

    return {"score": score, "components": parts, "reasons": reasons, "weights_used": {k: weights[k] for k in used}}


def features_from_candles(candles: list[dict], ltp: float | None, depth: dict | None,
                          cfg: dict) -> dict:
    ew = cfg["early_warning"]
    vwap = session_vwap(candles)
    vwap_dev = vwap_deviation(ltp, vwap)
    prev_vwap = session_vwap(candles[:-1]) if len(candles) >= 2 else None
    prev_price = candles[-2]["close"] if len(candles) >= 2 else None
    prev_dev = vwap_deviation(prev_price, prev_vwap)
    imbalance = depth_imbalance(depth)
    if imbalance is None:
        imbalance = tick_imbalance(candles)
        imb_source = "ticks" if imbalance is not None else None
    else:
        imb_source = "depth"

    surge = volume_surge_ratio(candles, ew["volume_avg_periods"])
    oi_pct = oi_change_pct(candles, ew["oi_lookback_bars"])
    vwap_dev_pct = None if vwap_dev is None else vwap_dev * 100.0

    return {
        "vwap": None if vwap is None else round(vwap, 4),
        "vwap_dev": None if vwap_dev is None else round(vwap_dev, 6),
        "vwap_dev_pct": None if vwap_dev_pct is None else round(vwap_dev_pct, 4),
        "vwap_dev_roc": vwap_deviation_roc(prev_dev, vwap_dev),
        "volume_surge": None if surge is None else round(surge, 3),
        "oi_change_pct": None if oi_pct is None else round(oi_pct, 3),
        "imbalance": None if imbalance is None else round(imbalance, 4),
        "imbalance_source": imb_source,
    }


def score_constituent(constituent: dict, candles: list[dict], ltp: float | None,
                      depth: dict | None, cfg: dict, index_scale: float | None) -> dict:
    ew = cfg["early_warning"]
    feats = features_from_candles(candles, ltp, depth, cfg)
    full = {
        "volume": ew["volume_surge_full_score"],
        "oi": ew["oi_change_full_score_pct"],
        "vwap": ew["vwap_dev_full_score_pct"],
        "imbalance": ew["imbalance_full_score"],
    }
    scored = urgency_score(feats, ew["score_weights"], full)
    potential = None
    if index_scale and constituent.get("weight_pct"):
        mag = abs(feats.get("vwap_dev") or 0)
        ret = None
        if candles:
            prev = candles[0].get("open") or candles[0].get("close")
            if ltp and prev:
                ret = abs((ltp - prev) / prev)
        move = max(mag, ret or 0, 0.005)  # at least a 0.5% continuation hypothesis
        potential = round(move * (constituent["weight_pct"] / 100.0) * index_scale, 1)

    return {
        "symbol": constituent["symbol"],
        "name": constituent["name"],
        "weight_pct": constituent["weight_pct"],
        "ltp": ltp,
        **feats,
        **scored,
        "potential_index_pts": potential,
    }


def format_alert(row: dict) -> str:
    reasons = row.get("reasons") or ["elevated urgency"]
    return (
        f"{row['symbol']} showing {', '.join(reasons)}. "
        f"Weight {row['weight_pct']}%. "
        f"Potential index impact if move continues: ~{row.get('potential_index_pts') or '?'} pts"
    )


def should_alert(row: dict, cfg: dict, last_alert_at: dict[str, object] | None,
                 now_ts: float | None) -> bool:
    ew = cfg["early_warning"]
    score = row.get("score")
    if score is None or score < ew["alert_score_threshold"]:
        return False
    if row.get("weight_pct", 0) < ew["min_weight_pct_to_alert"]:
        return False
    last = (last_alert_at or {}).get(row["symbol"])
    if last is not None and now_ts is not None:
        if now_ts - float(last) < ew["cooldown_minutes"] * 60:
            return False
    return True
