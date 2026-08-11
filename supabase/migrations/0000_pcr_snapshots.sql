-- The existing PCR tracker's own table (backend.py's maybe_persist docstring)
-- — predates the Nifty Pre-Market Analysis Engine addition but was never
-- actually applied to this Supabase project (SUPABASE_URL/SUPABASE_SERVICE_KEY
-- were never configured before now), so migration 0001's `alter table
-- pcr_snapshots` has nothing to alter without this running first.
create table if not exists pcr_snapshots (
  id          bigint generated always as identity primary key,
  symbol      text,
  trade_date  date,
  t           text,
  expiry      text,
  pcr_oi      numeric,
  pcr_vol     numeric,
  put_oi      bigint,
  call_oi     bigint,
  created_at  timestamptz default now()
);
