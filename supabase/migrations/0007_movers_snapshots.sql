-- Top-10 Nifty movers, snapshotted periodically by the frontend (see
-- premarket.jsx's MoversPanel) so the "implied move" verdict from
-- api/index.py's /api/upstox/movers can be checked against what Nifty
-- actually did next -- same hit-rate idea as morning_briefs, but for the
-- much simpler top-10-weighted-contribution signal. One row per snapshot
-- (not upserted) since intraday readings for the same trade_date are
-- expected to change as prices move; accuracy is computed off the latest
-- row per trade_date (see main.py's movers_accuracy endpoint).
create table if not exists public.movers_snapshots (
  id                bigint generated always as identity primary key,
  trade_date        date not null,
  captured_at       timestamptz not null default now(),
  implied_move_pct  numeric,
  verdict           text,
  stocks            jsonb
);
create index if not exists movers_snapshots_trade_date_idx on public.movers_snapshots (trade_date, captured_at desc);

alter table public.movers_snapshots enable row level security;
