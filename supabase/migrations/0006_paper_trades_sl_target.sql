-- Optional stop-loss / target premium on a paper trade, plus how a closed
-- trade actually ended (manual close vs auto-triggered) — see paper_trading.py
-- and premarket.jsx's PaperTradingPanel for the client-side polling loop
-- that watches live LTP against these and auto-closes on a hit.
alter table public.paper_trades
  add column if not exists stop_loss numeric,
  add column if not exists target_price numeric,
  add column if not exists exit_reason text check (exit_reason in ('manual', 'stop_loss', 'target'));
