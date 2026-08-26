"""
index_backtest.py — replay historical 5-min candles through the *same*
urgency-score logic used live, then score alerts vs subsequent index moves.

Run this (and retune config weights/thresholds from its sweep) before
trusting any live early-warning alert. Attribution is not backtested here
— that module is deterministic given quotes.
"""

from __future__ import annotations

import datetime as dt

from index_early_warning import score_constituent, should_alert


def _bar_hhmm(ts: str) -> str:
    """Upstox timestamps look like 2026-08-26T09:15:00+05:30 or ...09:15:00+0530."""
    if not ts or len(ts) < 16:
        return ""
    return ts[11:16]


def _parse_ts(ts: str) -> dt.datetime | None:
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _session_key(ts: str) -> str:
    return ts[:10] if ts else ""


def align_series(nifty_candles: list[dict], stock_series: dict[str, list[dict]]) -> list[str]:
    """Timestamps present on the index and at least one stock, oldest-first."""
    nifty_ts = {c["t"] for c in nifty_candles if c.get("t")}
    stock_ts = set()
    for rows in stock_series.values():
        stock_ts.update(c["t"] for c in rows if c.get("t"))
    return sorted(nifty_ts & stock_ts) if stock_ts else sorted(nifty_ts)


def _candles_upto(by_t: dict, timestamps: list[str], i: int) -> list[dict]:
    """Today's session bars through timestamps[i], oldest-first."""
    day = _session_key(timestamps[i])
    out = []
    for t in timestamps[: i + 1]:
        if _session_key(t) != day:
            continue
        row = by_t.get(t)
        if row:
            out.append(row)
    return out


def confusion(tp: int, fp: int, fn: int, tn: int) -> dict:
    precision = round(tp / (tp + fp) * 100, 1) if (tp + fp) else None
    recall = round(tp / (tp + fn) * 100, 1) if (tp + fn) else None
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision_pct": precision,
        "recall_pct": recall,
        "matrix": {"predicted_alert": {"actual_move": tp, "no_move": fp},
                   "no_alert": {"actual_move": fn, "no_move": tn}},
    }


def replay(nifty_candles: list[dict], constituents: list[dict],
           stock_series: dict[str, list[dict]], cfg: dict,
           score_threshold: float | None = None,
           apply_cooldown: bool = True) -> dict:
    """Walk aligned 5-min bars. At each eligible bar, score every constituent
    from session-to-date candles (no future bars), optionally fire alerts,
    then label the forward index move over lookahead_minutes.
    """
    bt = cfg["backtest"]
    lookahead = int(bt["lookahead_minutes"])
    move_pts = float(bt["move_threshold_pts"])
    exclude = set(bt.get("exclude_open_times") or [])
    step = 5  # candle minutes
    forward_bars = max(1, lookahead // step)

    live_cfg = {**cfg, "early_warning": {**cfg["early_warning"]}}
    if score_threshold is not None:
        live_cfg["early_warning"]["alert_score_threshold"] = score_threshold
    if not apply_cooldown:
        live_cfg["early_warning"]["cooldown_minutes"] = 0

    timestamps = align_series(nifty_candles, stock_series)
    nifty_by_t = {c["t"]: c for c in nifty_candles}
    stock_by = {
        s["symbol"]: {c["t"]: c for c in (stock_series.get(s["symbol"]) or [])}
        for s in constituents
    }

    alerts = []
    last_alert_at: dict[str, float] = {}
    tp = fp = fn = tn = 0

    for i, t in enumerate(timestamps):
        if _bar_hhmm(t) in exclude:
            continue
        j = i + forward_bars
        if j >= len(timestamps):
            break
        # Don't cross sessions for the forward label
        if _session_key(timestamps[j]) != _session_key(t):
            continue
        n0 = nifty_by_t.get(t)
        n1 = nifty_by_t.get(timestamps[j])
        if not n0 or not n1 or n0.get("close") is None or n1.get("close") is None:
            continue
        actual_move = n1["close"] - n0["close"]
        actual_big = abs(actual_move) >= move_pts

        fired = []
        parsed = _parse_ts(t)
        now_ts = parsed.timestamp() if parsed else float(i)
        for c in constituents:
            bars = _candles_upto(stock_by[c["symbol"]], timestamps, i)
            if len(bars) < 3:
                continue
            ltp = bars[-1].get("close")
            row = score_constituent(c, bars, ltp, depth=None, cfg=live_cfg,
                                    index_scale=n0.get("close"))
            if should_alert(row, live_cfg, last_alert_at if apply_cooldown else None, now_ts):
                last_alert_at[c["symbol"]] = now_ts
                rec = {
                    "t": t,
                    "symbol": c["symbol"],
                    "score": row["score"],
                    "reasons": row["reasons"],
                    "weight_pct": c["weight_pct"],
                    "potential_index_pts": row.get("potential_index_pts"),
                    "index_move_pts": round(actual_move, 2),
                    "hit": actual_big,
                }
                fired.append(rec)
                alerts.append(rec)

        predicted = bool(fired)
        if predicted and actual_big:
            tp += 1
        elif predicted and not actual_big:
            fp += 1
        elif (not predicted) and actual_big:
            fn += 1
        else:
            tn += 1

    metrics = confusion(tp, fp, fn, tn)
    metrics.update({
        "score_threshold": live_cfg["early_warning"]["alert_score_threshold"],
        "lookahead_minutes": lookahead,
        "move_threshold_pts": move_pts,
        "alert_count": len(alerts),
        "bars_scored": tp + fp + fn + tn,
    })
    return {"metrics": metrics, "alerts": alerts}


def sweep_thresholds(nifty_candles, constituents, stock_series, cfg,
                     thresholds: list[float] | None = None) -> list[dict]:
    thresholds = thresholds or cfg["backtest"]["threshold_sweep"]
    out = []
    for th in thresholds:
        result = replay(nifty_candles, constituents, stock_series, cfg,
                        score_threshold=float(th), apply_cooldown=True)
        m = result["metrics"]
        out.append({
            "threshold": th,
            "precision_pct": m["precision_pct"],
            "recall_pct": m["recall_pct"],
            "tp": m["tp"], "fp": m["fp"], "fn": m["fn"], "tn": m["tn"],
            "alert_count": m["alert_count"],
        })
    return out
