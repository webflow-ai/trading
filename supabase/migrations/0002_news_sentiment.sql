-- Module 8 (news_ai.py) adds a one-line overall sentiment to each morning
-- brief, read at the top level by notify.py/premarket.jsx (not nested in
-- `components`, to match score/verdict/expected_low/expected_high already
-- being top-level scalar columns rather than buried in jsonb).
alter table morning_briefs
  add column if not exists news_sentiment text;
