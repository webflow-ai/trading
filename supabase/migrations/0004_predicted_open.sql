-- A single point estimate for where Nifty is likely to open (see
-- scoring.compute_predicted_open), alongside the existing expected_low/
-- expected_high band. Top-level scalar column, same pattern as those two.
alter table morning_briefs
  add column if not exists predicted_open numeric;
