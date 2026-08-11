-- Supabase's security advisor flagged all 5 tables above as publicly
-- readable/writable via the anon key once created (RLS defaults to off).
-- This repo only ever talks to Supabase with SUPABASE_SERVICE_KEY
-- (server-side, never shipped to the browser — see storage.py), which
-- bypasses RLS entirely, so enabling it with no policies blocks the public
-- anon key without changing how this app behaves.
alter table public.pcr_snapshots enable row level security;
alter table public.participant_oi enable row level security;
alter table public.fii_dii_cash enable row level security;
alter table public.macro_snapshots enable row level security;
alter table public.morning_briefs enable row level security;
