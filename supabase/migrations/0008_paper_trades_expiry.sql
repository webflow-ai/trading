-- Paper trading: store which option expiry the simulated trade is on
-- so the journal can track weekly vs monthly contracts and pull the
-- matching Upstox/NSE chain for live LTP / SL / target.
alter table paper_trades
  add column if not exists expiry text;
