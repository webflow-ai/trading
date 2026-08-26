"""
index_engine.py — live orchestration for attribution + early-warning.

Keeps the two engines separate in the payload (`attribution` vs
`early_warning`) so the UI never blends "this stock moved the index 12 pts"
with "this stock's urgency score is 81." Config is JSON + optional
in-memory overlay (PATCH /api/index-engine/config) + a few env overrides.
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
import os
import time
from pathlib import Path
from urllib.parse import quote

import httpx

import index_attribution
import index_backtest
import index_early_warning
import storage

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent
CONSTITUENTS_PATH = ROOT / "config" / "nifty_top20.json"
ENGINE_CONFIG_PATH = ROOT / "config" / "index_engine.json"
NIFTY_KEY = "NSE_INDEX|Nifty 50"
UPSTOX_QUOTES = "https://api.upstox.com/v2/market-quote/quotes"
UPSTOX_INTRADAY = "https://api.upstox.com/v3/historical-candle/intraday/{key}/minutes/{n}"
UPSTOX_RANGE = "https://api.upstox.com/v3/historical-candle/{key}/minutes/{n}/{to_date}/{from_date}"

_runtime_overlay: dict = {}
_candle_cache: dict = {"at": 0.0, "series": {}}
_last_persist_at = 0.0
_last_alert_at: dict[str, float] = {}
_recent_alerts: list[dict] = []


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_constituents() -> dict:
    return json.loads(CONSTITUENTS_PATH.read_text(encoding="utf-8"))


def load_file_config() -> dict:
    return json.loads(ENGINE_CONFIG_PATH.read_text(encoding="utf-8"))


def env_overrides() -> dict:
    out: dict = {}
    if os.getenv("INDEX_ENGINE_POLL_SECONDS"):
        out["poll_seconds"] = int(os.getenv("INDEX_ENGINE_POLL_SECONDS"))
    if os.getenv("INDEX_ENGINE_PERSIST_SECONDS"):
        out["persist_seconds"] = int(os.getenv("INDEX_ENGINE_PERSIST_SECONDS"))
    ew = {}
    if os.getenv("INDEX_ENGINE_ALERT_THRESHOLD"):
        ew["alert_score_threshold"] = float(os.getenv("INDEX_ENGINE_ALERT_THRESHOLD"))
    if os.getenv("INDEX_ENGINE_COOLDOWN_MINUTES"):
        ew["cooldown_minutes"] = int(os.getenv("INDEX_ENGINE_COOLDOWN_MINUTES"))
    if ew:
        out["early_warning"] = ew
    bt = {}
    if os.getenv("INDEX_ENGINE_LOOKAHEAD_MINUTES"):
        bt["lookahead_minutes"] = int(os.getenv("INDEX_ENGINE_LOOKAHEAD_MINUTES"))
    if os.getenv("INDEX_ENGINE_MOVE_THRESHOLD_PTS"):
        bt["move_threshold_pts"] = float(os.getenv("INDEX_ENGINE_MOVE_THRESHOLD_PTS"))
    if bt:
        out["backtest"] = bt
    return out


def get_config() -> dict:
    cfg = load_file_config()
    cfg = _deep_merge(cfg, env_overrides())
    cfg = _deep_merge(cfg, _runtime_overlay)
    return cfg


def set_runtime_overlay(overlay: dict) -> dict:
    global _runtime_overlay
    _runtime_overlay = _deep_merge(_runtime_overlay, overlay or {})
    return get_config()


def reset_runtime_overlay() -> None:
    global _runtime_overlay
    _runtime_overlay = {}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def prev_close_from_quote(q: dict) -> float | None:
    """Same live-market gotcha as api/index.py: prefer last_price - net_change."""
    ltp = q.get("last_price")
    net_change = q.get("net_change")
    if ltp is not None and net_change is not None:
        return ltp - net_change
    return (q.get("ohlc") or {}).get("close")


def parse_quotes(payload: dict) -> dict:
    by_key = {}
    for quote in (payload.get("data") or {}).values():
        token_field = quote.get("instrument_token")
        if token_field:
            by_key[token_field] = quote
    return by_key


def normalize_quote(q: dict | None) -> dict:
    if not q:
        return {}
    depth = q.get("depth")
    return {
        "ltp": q.get("last_price"),
        "prev_close": prev_close_from_quote(q),
        "volume": q.get("volume"),
        "oi": q.get("oi") or None,
        "last_trade_time": q.get("last_trade_time"),
        "depth": depth,
        "net_change": q.get("net_change"),
    }


def parse_candles(payload: dict) -> list[dict]:
    candles = (payload.get("data") or {}).get("candles") or []
    points = []
    for c in candles:
        if not isinstance(c, list) or len(c) < 5:
            continue
        points.append({
            "t": c[0], "open": c[1], "high": c[2], "low": c[3], "close": c[4],
            "volume": c[5] if len(c) > 5 else None,
            "oi": c[6] if len(c) > 6 else None,
        })
    points.reverse()
    return points


def quote_is_stale(last_trade_time, now: dt.datetime, stale_seconds: int) -> bool:
    if not last_trade_time:
        return False
    parsed = None
    if isinstance(last_trade_time, (int, float)):
        parsed = dt.datetime.fromtimestamp(last_trade_time / 1000 if last_trade_time > 1e12 else last_trade_time, IST)
    elif isinstance(last_trade_time, str):
        try:
            parsed = dt.datetime.fromisoformat(last_trade_time.replace("Z", "+00:00"))
        except ValueError:
            return False
    if parsed is None:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return (now - parsed).total_seconds() > stale_seconds


async def fetch_quotes(token: str, constituents: list[dict]) -> tuple[dict, dict | None]:
    keys = [NIFTY_KEY] + [f"NSE_EQ|{c['isin']}" for c in constituents]
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(UPSTOX_QUOTES, params={"instrument_key": ",".join(keys)}, headers=_auth(token))
    if resp.status_code != 200:
        return resp, None
    try:
        return resp, parse_quotes(resp.json())
    except Exception:
        return resp, None


async def fetch_intraday(token: str, instrument_key: str, minutes: int = 5) -> list[dict]:
    encoded = quote(instrument_key, safe="")
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            resp = await client.get(
                UPSTOX_INTRADAY.format(key=encoded, n=minutes),
                headers=_auth(token),
            )
        if resp.status_code != 200:
            return []
        return parse_candles(resp.json())
    except Exception:
        return []


async def fetch_range(token: str, instrument_key: str, minutes: int, from_date: str, to_date: str) -> list[dict]:
    encoded = quote(instrument_key, safe="")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                UPSTOX_RANGE.format(key=encoded, n=minutes, to_date=to_date, from_date=from_date),
                headers=_auth(token),
            )
        if resp.status_code != 200:
            return []
        return parse_candles(resp.json())
    except Exception:
        return []


async def candles_for_universe(token: str, constituents: list[dict], cfg: dict) -> dict[str, list[dict]]:
    now = time.monotonic()
    ttl = cfg.get("candle_refresh_seconds", 60)
    if _candle_cache["series"] and now - _candle_cache["at"] < ttl:
        return _candle_cache["series"]

    minutes = 5
    tasks = [fetch_intraday(token, NIFTY_KEY, minutes)]
    tasks += [fetch_intraday(token, f"NSE_EQ|{c['isin']}", minutes) for c in constituents]
    nifty, *stock = await asyncio.gather(*tasks)
    series = {"NIFTY": nifty}
    for c, rows in zip(constituents, stock):
        series[c["symbol"]] = rows
    _candle_cache["at"] = now
    _candle_cache["series"] = series
    return series


def _index_from_quotes(by_key: dict) -> tuple[float | None, float | None]:
    q = normalize_quote(by_key.get(NIFTY_KEY))
    return q.get("ltp"), q.get("prev_close")


async def build_snapshot(token: str, persist: bool = True) -> dict:
    cfg = get_config()
    meta = load_constituents()
    constituents = meta["constituents"]
    now = dt.datetime.now(IST)

    resp, by_key = await fetch_quotes(token, constituents)
    if resp.status_code == 401:
        return {"connected": False, "error": "Upstox session expired — visit /api/upstox/login again", "status_code": 401}
    if resp.status_code != 200:
        return {"connected": True, "error": f"upstox returned {resp.status_code}: {resp.text[:200]}"}
    if not by_key:
        return {"connected": True, "error": "upstox returned no quote data"}

    quotes_by_isin = {}
    stale_isins = set()
    for c in constituents:
        raw = by_key.get(f"NSE_EQ|{c['isin']}")
        nq = normalize_quote(raw)
        quotes_by_isin[c["isin"]] = nq
        if quote_is_stale(nq.get("last_trade_time"), now, cfg.get("stale_quote_seconds", 180)):
            stale_isins.add(c["isin"])

    index_ltp, index_prev = _index_from_quotes(by_key)
    attribution = index_attribution.build_attribution(
        constituents, quotes_by_isin, index_ltp, index_prev,
        stale_isins=stale_isins,
        tracking_error_flag_pts=cfg.get("tracking_error_flag_pts", 25),
    )

    series = await candles_for_universe(token, constituents, cfg)
    scored = []
    new_alerts = []
    now_ts = now.timestamp()
    for c in constituents:
        q = quotes_by_isin.get(c["isin"]) or {}
        row = index_early_warning.score_constituent(
            c, series.get(c["symbol"]) or [], q.get("ltp"), q.get("depth"), cfg, index_prev,
        )
        scored.append(row)
        if index_early_warning.should_alert(row, cfg, _last_alert_at, now_ts):
            _last_alert_at[c["symbol"]] = now_ts
            alert = {
                "fired_at": now.isoformat(),
                "trade_date": now.date().isoformat(),
                "symbol": c["symbol"],
                "name": c["name"],
                "weight_pct": c["weight_pct"],
                "score": row["score"],
                "reasons": row["reasons"],
                "message": index_early_warning.format_alert(row),
                "potential_index_pts": row.get("potential_index_pts"),
                "features": {
                    "volume_surge": row.get("volume_surge"),
                    "oi_change_pct": row.get("oi_change_pct"),
                    "vwap_dev_pct": row.get("vwap_dev_pct"),
                    "imbalance": row.get("imbalance"),
                    "imbalance_source": row.get("imbalance_source"),
                },
                "index_ltp_at_fire": index_ltp,
                "stock_ltp_at_fire": q.get("ltp"),
            }
            new_alerts.append(alert)
            _recent_alerts.insert(0, alert)
            _recent_alerts[:] = _recent_alerts[:80]

    scored.sort(key=lambda r: (-(r.get("score") or -1), r["symbol"]))

    if persist:
        await _maybe_persist(cfg, now, attribution, series, new_alerts, index_ltp)
        await _resolve_open_alerts(now, index_ltp, quotes_by_isin, constituents)

    return {
        "connected": True,
        "captured_at": now.isoformat(),
        "constituents_as_of": meta.get("as_of"),
        "constituents_source": meta.get("source"),
        "config": {
            "poll_seconds": cfg.get("poll_seconds"),
            "persist_seconds": cfg.get("persist_seconds"),
            "alert_score_threshold": cfg["early_warning"]["alert_score_threshold"],
            "score_weights": cfg["early_warning"]["score_weights"],
            "cooldown_minutes": cfg["early_warning"]["cooldown_minutes"],
            "lookahead_minutes": cfg["backtest"]["lookahead_minutes"],
            "move_threshold_pts": cfg["backtest"]["move_threshold_pts"],
        },
        "attribution": attribution,
        "early_warning": {
            "disclaimer": cfg["early_warning"]["disclaimer"],
            "stocks": scored,
            "new_alerts": new_alerts,
            "recent_alerts": _recent_alerts[:40],
        },
    }


async def _maybe_persist(cfg, now, attribution, series, new_alerts, index_ltp) -> None:
    global _last_persist_at
    for alert in new_alerts:
        try:
            await storage.save_index_engine_alert(alert)
        except Exception as e:
            print(f"index_engine: alert persist failed: {e}")

    interval = cfg.get("persist_seconds", 60)
    t = time.monotonic()
    if t - _last_persist_at < interval and not new_alerts:
        return
    _last_persist_at = t
    try:
        ticks = [{
            "captured_at": now.isoformat(),
            "trade_date": now.date().isoformat(),
            "symbol": "NIFTY",
            "ltp": index_ltp,
            "prev_close": attribution.get("index_prev_close"),
            "volume": None,
            "oi": None,
            "contribution_pts": None,
        }]
        for s in attribution.get("stocks") or []:
            ticks.append({
                "captured_at": now.isoformat(),
                "trade_date": now.date().isoformat(),
                "symbol": s["symbol"],
                "ltp": s.get("ltp"),
                "prev_close": s.get("prev_close"),
                "volume": s.get("volume"),
                "oi": s.get("oi"),
                "contribution_pts": s.get("contribution_pts"),
            })
        await storage.save_index_engine_ticks(ticks)
        candle_rows = []
        for symbol, rows in series.items():
            for c in rows[-40:]:
                candle_rows.append({
                    "symbol": symbol,
                    "interval": "5minute",
                    "bar_ts": c["t"],
                    "open": c.get("open"),
                    "high": c.get("high"),
                    "low": c.get("low"),
                    "close": c.get("close"),
                    "volume": c.get("volume"),
                    "oi": c.get("oi"),
                })
        if candle_rows:
            await storage.save_index_engine_candles(candle_rows)
    except Exception as e:
        print(f"index_engine: tick/candle persist failed: {e}")


def _fill_horizon(alert: dict, horizon_min: int, now: dt.datetime, index_ltp, stock_ltp) -> dict:
    field_i = f"index_move_{horizon_min}m"
    field_s = f"stock_move_{horizon_min}m"
    patch = {}
    fired = dt.datetime.fromisoformat(alert["fired_at"])
    if fired.tzinfo is None:
        fired = fired.replace(tzinfo=IST)
    if (now - fired).total_seconds() < horizon_min * 60:
        return patch
    if alert.get(field_i) is None and index_ltp is not None and alert.get("index_ltp_at_fire") is not None:
        patch[field_i] = round(index_ltp - alert["index_ltp_at_fire"], 2)
    if alert.get(field_s) is None and stock_ltp is not None and alert.get("stock_ltp_at_fire") is not None:
        patch[field_s] = round(stock_ltp - alert["stock_ltp_at_fire"], 2)
    return patch


async def _resolve_open_alerts(now, index_ltp, quotes_by_isin, constituents) -> None:
    isin_by_sym = {c["symbol"]: c["isin"] for c in constituents}
    try:
        open_rows = await storage.get_open_index_engine_alerts()
    except Exception as e:
        print(f"index_engine: load open alerts failed: {e}")
        open_rows = []
    # Also resolve in-memory recents so the UI fills in even without Supabase
    for alert in list(_recent_alerts) + list(open_rows):
        isin = isin_by_sym.get(alert.get("symbol"))
        stock_ltp = (quotes_by_isin.get(isin) or {}).get("ltp") if isin else None
        patch = {}
        for h in (5, 15, 30):
            patch.update(_fill_horizon(alert, h, now, index_ltp, stock_ltp))
        if not patch:
            continue
        alert.update(patch)
        aid = alert.get("id")
        if aid:
            try:
                await storage.update_index_engine_alert(aid, patch)
            except Exception as e:
                print(f"index_engine: alert resolve failed: {e}")


async def run_backtest(token: str, days: int | None = None) -> dict:
    cfg = get_config()
    meta = load_constituents()
    constituents = meta["constituents"]
    days = days or cfg["backtest"]["default_days"]
    to_date = dt.datetime.now(IST).date()
    from_date = to_date - dt.timedelta(days=days)

    nifty_task = fetch_range(token, NIFTY_KEY, 5, from_date.isoformat(), to_date.isoformat())
    stock_tasks = [
        fetch_range(token, f"NSE_EQ|{c['isin']}", 5, from_date.isoformat(), to_date.isoformat())
        for c in constituents
    ]
    nifty, *stock_lists = await asyncio.gather(nifty_task, *stock_tasks)
    if not nifty:
        return {"connected": True, "error": "no Nifty 50 candles for this range", "days": days}

    series = {c["symbol"]: rows for c, rows in zip(constituents, stock_lists)}
    primary = index_backtest.replay(nifty, constituents, series, cfg)
    curve = index_backtest.sweep_thresholds(nifty, constituents, series, cfg)
    return {
        "connected": True,
        "days": days,
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "constituents_as_of": meta.get("as_of"),
        "disclaimer": cfg["early_warning"]["disclaimer"],
        "metrics": primary["metrics"],
        "alerts": primary["alerts"][-80:],
        "threshold_sweep": curve,
        "config": {
            "score_weights": cfg["early_warning"]["score_weights"],
            "alert_score_threshold": cfg["early_warning"]["alert_score_threshold"],
            "lookahead_minutes": cfg["backtest"]["lookahead_minutes"],
            "move_threshold_pts": cfg["backtest"]["move_threshold_pts"],
        },
    }
