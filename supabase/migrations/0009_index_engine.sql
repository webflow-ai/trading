-- Index Contribution & Early-Warning Engine
-- Ticks/candles for later replay; alerts with subsequent-move columns so
-- live precision can be measured (5/15/30 min after fire).

create table if not exists public.index_engine_ticks (
  id                 bigint generated always as identity primary key,
  captured_at        timestamptz not null,
  trade_date         date not null,
  symbol             text not null,
  ltp                numeric,
  prev_close         numeric,
  volume             numeric,
  oi                 numeric,
  contribution_pts   numeric
);
create index if not exists index_engine_ticks_lookup_idx
  on public.index_engine_ticks (trade_date, symbol, captured_at desc);

create table if not exists public.index_engine_candles (
  symbol             text not null,
  interval           text not null,
  bar_ts             timestamptz not null,
  open               numeric,
  high               numeric,
  low                numeric,
  close              numeric,
  volume             numeric,
  oi                 numeric,
  primary key (symbol, interval, bar_ts)
);

create table if not exists public.index_engine_alerts (
  id                   bigint generated always as identity primary key,
  fired_at             timestamptz not null,
  trade_date           date not null,
  symbol               text not null,
  name                 text,
  weight_pct           numeric,
  score                numeric,
  reasons              jsonb,
  message              text,
  potential_index_pts  numeric,
  features             jsonb,
  index_ltp_at_fire    numeric,
  stock_ltp_at_fire    numeric,
  index_move_5m        numeric,
  stock_move_5m        numeric,
  index_move_15m       numeric,
  stock_move_15m       numeric,
  index_move_30m       numeric,
  stock_move_30m       numeric
);
create index if not exists index_engine_alerts_fired_idx
  on public.index_engine_alerts (fired_at desc);
create index if not exists index_engine_alerts_open_idx
  on public.index_engine_alerts (fired_at desc)
  where index_move_30m is null;

alter table public.index_engine_ticks enable row level security;
alter table public.index_engine_candles enable row level security;
alter table public.index_engine_alerts enable row level security;
