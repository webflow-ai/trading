"""
storage.py — Supabase persistence layer for the Nifty Pre-Market Analysis
Engine. Module 2 of the build order (docs/PREMARKET_ENGINE.md).

Raw REST over httpx, same pattern as backend.py's maybe_persist — no
supabase-py dependency (see docs/PREMARKET_ENGINE.md, Open Decision 3).
Every write function silently no-ops (with a log line) when SUPABASE_URL /
SUPABASE_SERVICE_KEY aren't set, same as the existing PCR persistence, so the
engine still runs end-to-end without a database configured.

Table schema: supabase/migrations/0001_premarket_engine.sql
"""

import os
import datetime as dt

import httpx
import pandas as pd

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

_client: httpx.AsyncClient | None = None

PARTICIPANT_OI_COLUMNS = {
    "Future Index Long": "future_index_long",
    "Future Index Short": "future_index_short",
    "Future Stock Long": "future_stock_long",
    "Future Stock Short": "future_stock_short",
    "Option Index Call Long": "option_index_call_long",
    "Option Index Put Long": "option_index_put_long",
    "Option Index Call Short": "option_index_call_short",
    "Option Index Put Short": "option_index_put_short",
    "Total Long Contracts": "total_long_contracts",
    "Total Short Contracts": "total_short_contracts",
}


def configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)


async def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=15)
    return _client


def _headers(upsert: bool = False) -> dict:
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if upsert:
        headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
    return headers


async def _upsert(table: str, rows: list[dict] | dict, on_conflict: str) -> None:
    if not configured():
        print(f"storage: SUPABASE_URL/SUPABASE_SERVICE_KEY not set, skipping upsert into {table}")
        return
    client = await get_client()
    resp = await client.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        params={"on_conflict": on_conflict},
        headers=_headers(upsert=True),
        json=rows,
    )
    resp.raise_for_status()


async def _insert(table: str, rows: list[dict] | dict) -> None:
    if not configured():
        print(f"storage: SUPABASE_URL/SUPABASE_SERVICE_KEY not set, skipping insert into {table}")
        return
    client = await get_client()
    resp = await client.post(f"{SUPABASE_URL}/rest/v1/{table}", headers=_headers(), json=rows)
    resp.raise_for_status()


async def _select(table: str, **params) -> list[dict]:
    if not configured():
        return []
    client = await get_client()
    resp = await client.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=_headers(), params=params)
    resp.raise_for_status()
    return resp.json()


def _normalize_nse_date(raw: str | None) -> str | None:
    """NSE's fii-dii endpoint returns dates like '10-Aug-2026'; Postgres
    columns here are plain `date`, which wants ISO (2026-08-10)."""
    if not raw:
        return None
    return dt.datetime.strptime(raw, "%d-%b-%Y").date().isoformat()


async def save_participant_oi(df: pd.DataFrame) -> None:
    """df: the output of nse_client.NSEClient.fetch_participant_oi — one row
    per participant (Client/DII/FII/Pro), with a `date` column already
    stamped in ISO form."""
    rows = []
    for _, r in df.iterrows():
        row = {"trade_date": r["date"], "participant": r["Client Type"]}
        for csv_col, db_col in PARTICIPANT_OI_COLUMNS.items():
            if csv_col in df.columns:
                row[db_col] = int(r[csv_col])
        rows.append(row)
    await _upsert("participant_oi", rows, on_conflict="trade_date,participant")


async def save_fii_dii_cash(data: dict) -> None:
    """data: the output of nse_client.NSEClient.fetch_fii_dii_cash()."""
    row = {
        "trade_date": _normalize_nse_date(data.get("date")),
        "fii_buy": data.get("fii_buy"),
        "fii_sell": data.get("fii_sell"),
        "dii_buy": data.get("dii_buy"),
        "dii_sell": data.get("dii_sell"),
    }
    await _upsert("fii_dii_cash", row, on_conflict="trade_date")


async def save_macro_snapshots(session: str, quotes: dict, captured_at: dt.datetime | None = None) -> None:
    """quotes: {symbol: {"price": ..., "pct_change": ...}, ...} — persisted
    as one row per symbol. `session` is 'evening' or 'morning'."""
    if not quotes:
        return
    captured_at = captured_at or dt.datetime.now(dt.timezone.utc)
    rows = [
        {
            "captured_at": captured_at.isoformat(),
            "session": session,
            "symbol": symbol,
            "price": q.get("price"),
            "pct_change": q.get("pct_change"),
        }
        for symbol, q in quotes.items()
    ]
    await _insert("macro_snapshots", rows)


async def save_morning_brief(brief: dict) -> None:
    """brief: {trade_date, score, verdict, expected_low, expected_high,
    predicted_open, components, headlines, news_sentiment} — one row per
    trading day, upserted so a manual re-run of the morning job overwrites
    rather than duplicates.

    Extra response-only keys (e.g. top-level `outlook`, `disclaimer`) are
    stripped before the write — the plain-language outlook lives inside
    `components` jsonb so older schemas don't need a migration.
    """
    row = {k: brief[k] for k in (
        "trade_date", "score", "verdict", "expected_low", "expected_high",
        "predicted_open", "components", "headlines", "news_sentiment",
    ) if k in brief}
    await _upsert("morning_briefs", row, on_conflict="trade_date")


async def get_brief_history(days: int = 30) -> list[dict]:
    return await _select("morning_briefs", order="trade_date.desc", limit=str(days))


async def get_fii_trend(days: int = 30) -> list[dict]:
    return await _select(
        "participant_oi", participant="eq.FII", order="trade_date.desc", limit=str(days),
    )


async def get_latest_participant_oi() -> list[dict]:
    """All participant rows (Client/DII/FII/Pro) for the most recent
    trade_date present in participant_oi — a same-day snapshot, not a time
    series. Fetches a small batch ordered by trade_date and filters to the
    newest date client-side, since PostgREST has no single-query 'top N per
    group' here and a plain `limit=4` would risk mixing in a stale row if
    fewer than 4 participants were written for the latest day."""
    rows = await _select("participant_oi", order="trade_date.desc", limit="20")
    if not rows:
        return []
    latest_date = rows[0]["trade_date"]
    return [r for r in rows if r["trade_date"] == latest_date]


async def get_participant_history(days: int = 5) -> list[dict]:
    """All participants' rows (not filtered to one, unlike get_fii_trend)
    across roughly the last `days` trading days — used to compute a trend
    per participant, not just FII. `days * 5` gives slack over the exact
    `days * 4` (4 participants/day) in case a day has partial rows."""
    return await _select("participant_oi", order="trade_date.desc", limit=str(days * 5))


async def get_latest_fii_dii_cash() -> dict | None:
    rows = await _select("fii_dii_cash", order="trade_date.desc", limit="1")
    return rows[0] if rows else None


async def get_latest_pcr_snapshot(symbol: str) -> dict | None:
    """Newest row of the existing `pcr_snapshots` table (the one backend.py
    already writes to) — the storage-layer half of Module 4's
    option_snapshot() interface."""
    rows = await _select(
        "pcr_snapshots", symbol=f"eq.{symbol}", order="created_at.desc", limit="1",
    )
    return rows[0] if rows else None


# ---------------- paper trading journal ----------------

# Columns that PostgREST rejected as unknown (PGRST204) — e.g. `expiry`
# before migration 0008 is applied. Remembered for the process lifetime so
# we don't keep re-sending fields the live schema doesn't have yet.
_paper_trades_dropped_cols: set[str] = set()


def _paper_trade_payload(trade: dict) -> dict:
    """Drop Nones (optional columns) and any cols already known-missing."""
    return {
        k: v for k, v in trade.items()
        if v is not None and k not in _paper_trades_dropped_cols
    }


def _pgrst_unknown_column(resp: httpx.Response) -> str | None:
    """Parse PostgREST's PGRST204 'Could not find the X column of Y' body."""
    if resp.status_code != 400:
        return None
    try:
        body = resp.json()
    except Exception:
        return None
    if body.get("code") != "PGRST204":
        return None
    msg = body.get("message") or ""
    # "Could not find the 'expiry' column of 'paper_trades' in the schema cache"
    marker = "Could not find the '"
    if marker not in msg:
        return None
    rest = msg.split(marker, 1)[1]
    col, _, _ = rest.partition("'")
    return col or None


async def create_paper_trade(trade: dict) -> dict:
    """Inserts one paper trade and returns the created row (including its
    `id`), so the caller has something to reference when closing it later.
    Falls back to echoing the input with id=None when Supabase isn't
    configured, same no-op-but-don't-crash convention as everything else
    here.

    If the live schema is behind (e.g. migration 0008's `expiry` column not
    applied yet), drops the unknown column and retries once so the journal
    still works — just without persisting that field until the migration
    lands.
    """
    if not configured():
        print("storage: SUPABASE_URL/SUPABASE_SERVICE_KEY not set, skipping paper trade insert")
        return {**trade, "id": None}
    client = await get_client()
    headers = _headers()
    headers["Prefer"] = "return=representation"
    payload = _paper_trade_payload(trade)
    resp = await client.post(f"{SUPABASE_URL}/rest/v1/paper_trades", headers=headers, json=payload)
    unknown = _pgrst_unknown_column(resp)
    if unknown and unknown in payload:
        _paper_trades_dropped_cols.add(unknown)
        print(f"storage: paper_trades has no '{unknown}' column yet — retrying without it "
              f"(apply supabase/migrations/0008_paper_trades_expiry.sql)")
        payload = _paper_trade_payload(trade)
        resp = await client.post(f"{SUPABASE_URL}/rest/v1/paper_trades", headers=headers, json=payload)
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else {**trade, "id": None}


async def update_paper_trade(trade_id: int, patch: dict) -> dict | None:
    """Partial update — used to close a trade (status/exit_price/exit_time/
    pnl) but generic over whatever fields are passed."""
    if not configured():
        print(f"storage: SUPABASE_URL/SUPABASE_SERVICE_KEY not set, skipping paper trade #{trade_id} update")
        return None
    client = await get_client()
    headers = _headers()
    headers["Prefer"] = "return=representation"
    resp = await client.patch(
        f"{SUPABASE_URL}/rest/v1/paper_trades", params={"id": f"eq.{trade_id}"}, headers=headers, json=patch,
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else None


async def get_paper_trades(status: str | None = None, days: int = 90) -> list[dict]:
    params = {"order": "created_at.desc", "limit": str(max(days * 5, 50))}
    if status:
        params["status"] = f"eq.{status}"
    return await _select("paper_trades", **params)


async def get_paper_trade(trade_id: int) -> dict | None:
    rows = await _select("paper_trades", id=f"eq.{trade_id}", limit="1")
    return rows[0] if rows else None


# ---------------- top-10 movers accuracy tracking ----------------

async def save_movers_snapshot(snapshot: dict) -> None:
    """snapshot: {trade_date, implied_move_pct, verdict, stocks} from
    api/index.py's /api/upstox/movers, taken by the frontend (see
    premarket.jsx's MoversPanel) at most once every few minutes. Inserted,
    not upserted -- intraday readings for the same trade_date are expected
    to change as prices move through the session, and movers_accuracy()
    below only ever looks at the latest row per day."""
    await _insert("movers_snapshots", snapshot)


async def get_movers_snapshots(days: int = 30) -> list[dict]:
    """Newest-first, one row per snapshot taken (not deduped per day) --
    callers that want one-per-day (e.g. accuracy scoring) reduce client-side,
    same as get_participant_history()'s convention above."""
    return await _select("movers_snapshots", order="captured_at.desc", limit=str(days * 50))


# ---------------- index contribution / early-warning engine ----------------

async def _insert_returning(table: str, rows: list[dict] | dict) -> list[dict]:
    if not configured():
        print(f"storage: SUPABASE_URL/SUPABASE_SERVICE_KEY not set, skipping insert into {table}")
        return []
    client = await get_client()
    headers = _headers()
    headers["Prefer"] = "return=representation"
    resp = await client.post(f"{SUPABASE_URL}/rest/v1/{table}", headers=headers, json=rows)
    resp.raise_for_status()
    return resp.json() or []


async def _patch(table: str, match: dict, body: dict) -> list[dict]:
    if not configured():
        print(f"storage: SUPABASE_URL/SUPABASE_SERVICE_KEY not set, skipping patch of {table}")
        return []
    client = await get_client()
    headers = _headers()
    headers["Prefer"] = "return=representation"
    resp = await client.patch(
        f"{SUPABASE_URL}/rest/v1/{table}", params=match, headers=headers, json=body,
    )
    resp.raise_for_status()
    return resp.json() or []


async def save_index_engine_ticks(rows: list[dict]) -> None:
    if not rows:
        return
    await _insert("index_engine_ticks", rows)


async def save_index_engine_candles(rows: list[dict]) -> None:
    if not rows:
        return
    await _upsert("index_engine_candles", rows, on_conflict="symbol,interval,bar_ts")


async def save_index_engine_alert(alert: dict) -> dict:
    """Insert one early-warning alert. Subsequent 5/15/30m moves are filled
    in later by update_index_engine_alert — that follow-up is what makes
    live accuracy measurable."""
    payload = {
        "fired_at": alert.get("fired_at"),
        "trade_date": alert.get("trade_date"),
        "symbol": alert.get("symbol"),
        "name": alert.get("name"),
        "weight_pct": alert.get("weight_pct"),
        "score": alert.get("score"),
        "reasons": alert.get("reasons"),
        "message": alert.get("message"),
        "potential_index_pts": alert.get("potential_index_pts"),
        "features": alert.get("features"),
        "index_ltp_at_fire": alert.get("index_ltp_at_fire"),
        "stock_ltp_at_fire": alert.get("stock_ltp_at_fire"),
    }
    rows = await _insert_returning("index_engine_alerts", payload)
    saved = rows[0] if rows else {**payload, "id": None}
    if saved.get("id") is not None:
        alert["id"] = saved["id"]
    return saved


async def update_index_engine_alert(alert_id: int, patch: dict) -> dict | None:
    rows = await _patch("index_engine_alerts", {"id": f"eq.{alert_id}"}, patch)
    return rows[0] if rows else None


async def get_open_index_engine_alerts(limit: int = 80) -> list[dict]:
    """Alerts still missing the 30-minute subsequent-move fill."""
    return await _select(
        "index_engine_alerts",
        index_move_30m="is.null",
        order="fired_at.desc",
        limit=str(limit),
    )


async def get_index_engine_alerts(days: int = 14) -> list[dict]:
    return await _select(
        "index_engine_alerts",
        order="fired_at.desc",
        limit=str(max(days * 40, 50)),
    )
