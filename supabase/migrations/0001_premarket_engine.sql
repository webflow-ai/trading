-- Nifty Pre-Market Analysis Engine — storage layer
-- Module 2 of the build order (see docs/PREMARKET_ENGINE.md).
--
-- Deliberately no `option_snapshots` table here: rather than a second source
-- of truth for the option chain, the engine's option_snapshot() interface
-- (Module 4) reads the newest row of the `pcr_snapshots` table this repo's
-- existing PCR tracker already writes to, extended with the three columns
-- it doesn't yet have.

create table if not exists participant_oi (
  id                        bigint generated always as identity primary key,
  trade_date                date not null,
  participant                text not null,               -- Client | DII | FII | Pro
  future_index_long          bigint,
  future_index_short         bigint,
  future_stock_long          bigint,
  future_stock_short         bigint,
  option_index_call_long     bigint,
  option_index_put_long      bigint,
  option_index_call_short    bigint,
  option_index_put_short     bigint,
  total_long_contracts       bigint,
  total_short_contracts      bigint,
  created_at                 timestamptz not null default now(),
  unique (trade_date, participant)
);

create table if not exists fii_dii_cash (
  id          bigint generated always as identity primary key,
  trade_date  date not null unique,
  fii_buy     numeric,
  fii_sell    numeric,
  dii_buy     numeric,
  dii_sell    numeric,
  created_at  timestamptz not null default now()
);

create table if not exists macro_snapshots (
  id           bigint generated always as identity primary key,
  captured_at  timestamptz not null default now(),
  session      text not null check (session in ('evening', 'morning')),
  symbol       text not null,
  price        numeric,
  pct_change   numeric
);
create index if not exists macro_snapshots_symbol_captured_idx
  on macro_snapshots (symbol, captured_at desc);

create table if not exists morning_briefs (
  id             bigint generated always as identity primary key,
  trade_date     date not null unique,
  score          numeric not null,
  verdict        text not null,
  expected_low   numeric,
  expected_high  numeric,
  components     jsonb,
  headlines      jsonb,
  created_at     timestamptz not null default now()
);

-- Additive-only: pcr_snapshots already exists (see backend.py's
-- maybe_persist docstring) and is written by the live PCR tracker in
-- production. These columns are nullable so existing writers that don't
-- know about them keep working unchanged.
alter table pcr_snapshots
  add column if not exists max_call_oi_strike numeric,
  add column if not exists max_put_oi_strike  numeric,
  add column if not exists max_pain           numeric;
