-- Options paper-trading journal (post-build addition, see
-- docs/PREMARKET_ENGINE.md). One row per simulated trade: opened with an
-- entry premium (either typed in or pulled from the existing PCR tracker's
-- live option chain), closed later with an exit premium, pnl computed at
-- close time and stored rather than recomputed on every read.
create table if not exists paper_trades (
  id            bigint generated always as identity primary key,
  created_at    timestamptz not null default now(),
  trade_date    date not null,                 -- which pre-market brief this trade is tied to
  symbol        text not null default 'NIFTY',
  strike        numeric not null,
  option_type   text not null check (option_type in ('CE', 'PE')),
  action        text not null check (action in ('BUY', 'SELL')),
  lots          integer not null default 1,
  lot_size      integer not null,               -- captured per-trade since NSE revises this periodically
  entry_price   numeric not null,
  entry_time    timestamptz not null default now(),
  status        text not null default 'open' check (status in ('open', 'closed')),
  exit_price    numeric,
  exit_time     timestamptz,
  pnl           numeric,
  notes         text
);
create index if not exists paper_trades_status_idx on paper_trades (status, created_at desc);

alter table public.paper_trades enable row level security;
