"""
paper_trading.py — options paper-trading journal: open a simulated trade
against a real (or manually typed) premium, close it out later, and track
PnL + win rate. Post-build addition, see docs/PREMARKET_ENGINE.md.

⚠️ DEFAULT_LOT_SIZE below is a best-guess default for the UI to prefill,
not an authoritative current value — NSE revises index F&O lot sizes
periodically (contract-value bands), and this repo has no live source for
it. `lot_size` is captured per-trade specifically so a future change here
never retroactively alters an already-closed trade's PnL; always confirm
the real current lot size before trusting the number this defaults to.
"""

import datetime as dt

import storage

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

DEFAULT_LOT_SIZE = 75


def compute_pnl(action: str, entry_price: float, exit_price: float, lot_size: int, lots: int) -> float:
    """BUY profits when exit > entry; SELL (writing/shorting) profits when
    exit < entry — same convention as scoring elsewhere in this codebase,
    just applied to premiums instead of index levels."""
    direction = 1 if action == "BUY" else -1
    return round((exit_price - entry_price) * direction * lot_size * lots, 2)


async def open_trade(
    *, strike: float, option_type: str, action: str, entry_price: float,
    lots: int = 1, lot_size: int = DEFAULT_LOT_SIZE, symbol: str = "NIFTY",
    trade_date: str | None = None, notes: str | None = None,
) -> dict:
    trade_date = trade_date or dt.datetime.now(IST).date().isoformat()
    row = {
        "trade_date": trade_date,
        "symbol": symbol,
        "strike": strike,
        "option_type": option_type,
        "action": action,
        "lots": lots,
        "lot_size": lot_size,
        "entry_price": entry_price,
        "entry_time": dt.datetime.now(IST).isoformat(),
        "status": "open",
        "notes": notes,
    }
    return await storage.create_paper_trade(row)


async def close_trade(trade_id: int, exit_price: float) -> dict | None:
    """None if the trade doesn't exist or is already closed (closing twice
    would silently overwrite a real exit with a second one otherwise)."""
    trade = await storage.get_paper_trade(trade_id)
    if not trade or trade.get("status") != "open":
        return None
    pnl = compute_pnl(
        trade["action"], trade["entry_price"], exit_price,
        trade.get("lot_size") or DEFAULT_LOT_SIZE, trade.get("lots") or 1,
    )
    patch = {
        "status": "closed",
        "exit_price": exit_price,
        "exit_time": dt.datetime.now(IST).isoformat(),
        "pnl": pnl,
    }
    return await storage.update_paper_trade(trade_id, patch)


def summarize(trades: list[dict]) -> dict:
    """Win rate + PnL stats over whatever trades are passed in — pure
    function so it's testable without touching storage, and reusable if the
    caller wants stats over a filtered subset (e.g. one trade_date)."""
    open_trades = [t for t in trades if t.get("status") == "open"]
    closed = [t for t in trades if t.get("status") == "closed" and t.get("pnl") is not None]
    wins = [t for t in closed if t["pnl"] > 0]
    total_pnl = round(sum(t["pnl"] for t in closed), 2) if closed else 0.0
    return {
        "open_count": len(open_trades),
        "closed_count": len(closed),
        "win_count": len(wins),
        "loss_count": len(closed) - len(wins),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "total_pnl": total_pnl,
        "avg_pnl": round(total_pnl / len(closed), 2) if closed else None,
    }


def weekly_pnl(trades: list[dict]) -> list[dict]:
    """Realized PnL grouped by the Mon-Sun IST week each trade was *closed*
    in (not opened — a trade's PnL isn't realized until exit), newest week
    first. Only closed trades with a pnl and exit_time contribute; open
    trades have no realized PnL yet (see the live unrealized figure the
    dashboard computes separately, client-side, off current premiums)."""
    closed = [t for t in trades if t.get("status") == "closed" and t.get("pnl") is not None and t.get("exit_time")]
    buckets: dict[tuple, dict] = {}
    for t in closed:
        exit_dt = dt.datetime.fromisoformat(t["exit_time"].replace("Z", "+00:00")).astimezone(IST)
        key = exit_dt.isocalendar()[:2]  # (iso_year, iso_week)
        week_start = (exit_dt.date() - dt.timedelta(days=exit_dt.weekday())).isoformat()
        bucket = buckets.setdefault(key, {"week_start": week_start, "trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0})
        bucket["trades"] += 1
        bucket["total_pnl"] += t["pnl"]
        bucket["wins" if t["pnl"] > 0 else "losses"] += 1
    weeks = sorted(buckets.values(), key=lambda b: b["week_start"], reverse=True)
    for w in weeks:
        w["total_pnl"] = round(w["total_pnl"], 2)
    return weeks
