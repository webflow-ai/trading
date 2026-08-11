# Addition: Nifty Pre-Market Analysis Engine

Status: **all 8 modules built, 130/130 tests passing — not yet deployed.**
This document adapts the original build prompt (verbatim copy at the bottom)
to this repo's existing conventions so a build session can pick up without
re-deriving context.

**Before trusting this in production**, in rough priority order:

1. ~~Run the Supabase migrations~~ **Done** — applied directly against the
   live project (`jwyosiznurqnphqnkpgo`, via the Supabase MCP server) rather
   than just left as files. This also surfaced that `pcr_snapshots` itself
   didn't exist in this project yet (`SUPABASE_URL`/`SUPABASE_SERVICE_KEY`
   had never actually been configured before), so
   `supabase/migrations/0000_pcr_snapshots.sql` was added to create it —
   0001's `alter table pcr_snapshots` would otherwise have had nothing to
   alter. The security advisor also flagged all 5 tables as publicly
   readable/writable via the anon key (RLS defaults off); confirmed with the
   user and applied `0003_enable_rls.sql` — safe here since this repo only
   ever talks to Supabase with the service-role key, which bypasses RLS.
2. **Partially done.** `SUPABASE_URL`/`SUPABASE_SERVICE_KEY` are set locally
   (`.env`) and confirmed working end-to-end (see below). **Still not set in
   Vercel or GitHub Actions** — that's the one remaining env var task,
   needed before the scheduled jobs work in production. See the chat history
   for the exact list/values.
3. **Done — smoke-tested against live NSE from this machine.**
   `fetch_participant_oi`/`fetch_fii_dii_cash` worked exactly as originally
   written. `fetch_ban_list` did not: NSE has no per-date archive for it
   (unlike participant OI) — it's a fixed URL (`.../content/fo/fo_secban.csv`,
   no date suffix) with no CSV header row at all. Fixed in `nse_client.py`;
   `fetch_ban_list()`'s `date` param is now accepted-but-unused for call-site
   consistency. Verified again live after the fix: returns real symbols.
4. **Done — fixed and verified live.** `fetch_gift_nifty()` returned `None`
   at first (correctly marked "missing," predicted-open fell back to
   score-anchored) — root cause was three separate bugs: the URL
   301-redirects (`/gift-nifty` → `/gift-nifty-live`) and the client wasn't
   following redirects; and the page is Next.js, so the real data lives in
   the `__NEXT_DATA__` script tag's embedded JSON at
   `props.pageProps.initialGiftData.last_trade_price`, not loose
   `lastPrice`/`netChange` keys as guessed. All three fixed in
   `market_data.py`, reverified live: `{'price': 24534.0, 'change': -85.0}`,
   confidence correctly moved from "medium" to "high" once GIFT stopped
   being the one missing component.
5. **Done — RSS and Gemini both verified live.** RSS: 25 real, current
   headlines pulled (at least Moneycontrol/ET, or whichever combination, are
   alive — never pinned down which of the three specifically, since it
   didn't block anything). Gemini: once a real `GEMINI_API_KEY` was set,
   classification worked correctly (substantive per-headline reasoning,
   sensible bullish/bearish/impact calls, a coherent overall-sentiment
   line) — but the coded default model id, `gemini-2.0-flash`, was already
   retired (clean 404, confirming the key itself was fine — auth
   succeeded). Switched the default to `gemini-flash-latest`, an alias
   Google maintains to always point at their current recommended Flash
   model, specifically so this doesn't need re-fixing every time a dated
   model id gets retired. **Separately surfaced, not yet acted on:** the
   `google-generativeai` package this depends on is now fully deprecated
   upstream (a `FutureWarning` on every import says "all support ... has
   ended ... switch to `google.genai`") — still works today, but migrating
   to the new package is a real follow-up, not scheduled here.
6. **Done.** Loaded `/premarket.html` for real (Chrome extension, once
   connected) against `uvicorn main:app` + `python -m http.server 5500`,
   first with no data (confirmed every empty-state path renders instead of
   crashing) and then with a real persisted brief (confirmed every panel —
   verdict card, live cues, macro, positioning, participant OI, news,
   levels, history — renders real data correctly, no console errors).

One thing this surfaced that wasn't in the original 6: **none of the new
engine files called `load_dotenv()`** (unlike `backend.py`/`api/index.py`,
which both do) — so a local `.env` full of real keys was silently never
read by `main.py`. Fixed by adding `load_dotenv()` to the top of `main.py`,
before its `import jobs`/`import storage` (env vars are read at each
module's import time, so order matters). Harmless no-op in production
(Vercel injects env vars directly; no `.env` file is deployed).

None of the remaining items block each other — the engine degrades
gracefully end-to-end per source (see Module 3's design note), so it's
safe to fix them one at a time against real traffic.

## Post-build addition: site-only delivery, predicted open, participant OI panel

After the 8 modules above, the user asked for three changes:

- **No Telegram** — already optional (notify.py no-ops without
  `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`), so nothing to build; just don't
  set those two env vars.
- **A single predicted-open price point**, not just the expected_low/high
  band — added `scoring.compute_predicted_open()`. GIFT Nifty is used as the
  primary anchor when available (`gift_price - GIFT_FAIR_VALUE_PREMIUM`) since
  it's the market's own live overnight pricing, already reflecting US/Asia/
  macro/flows — layering the weighted score on top of it would double-count.
  Falls back to `previous_close` moved by the score (calibrated so a
  maxed-out ±100 score implies a ±1.5% move — `PREDICTED_OPEN_MAX_MOVE_PCT`,
  an untested-against-reality default like the other calibration constants)
  only when GIFT is unavailable. New `morning_briefs.predicted_open` column
  (`0004_predicted_open.sql`, applied live). Shown prominently in the
  dashboard's verdict card, still under the same disclaimer — it's an
  estimate, not a guarantee.
- **Full participant OI (Client/DII/FII/Pro) + FII/DII cash on the
  dashboard**, not just the FII ratio that was already there —
  `storage.get_latest_participant_oi()`/`get_latest_fii_dii_cash()`,
  `positioning.participant_snapshot()`/`fii_dii_cash_snapshot()`, wired into
  the morning brief's components and rendered as a new `ParticipantPanel` in
  `premarket.jsx`.

## Live Supabase project state

Migrations `0000`–`0004` have been **applied directly** against the real
project (`jwyosiznurqnphqnkpgo`, via the Supabase MCP server), not just left
as files — this included discovering `pcr_snapshots` itself didn't exist yet
(`0000_pcr_snapshots.sql` was added to create it) and fixing a security
advisory (RLS was disabled on all 5 tables, publicly exposing them via the
anon key; `0003_enable_rls.sql` fixed it — safe since this app only ever
uses the service-role key, which bypasses RLS). No RLS policies were added
(intentionally — nothing but the service-role key should touch these
tables).

Vercel env vars: `SUPABASE_URL`/`SUPABASE_SERVICE_KEY`/`PREMARKET_JOB_API_KEY`
confirmed set and working (verified live, see below). The rest
(`TELEGRAM_*`, `GEMINI_API_KEY`, `CORS_ORIGIN`) not independently confirmed
here — the Vercel MCP server has no env-var read tool by design, so
"working" was only ever established indirectly, by hitting the deployed
endpoints and seeing correct behavior.

## Operational incident: cron never fired, then the morning job was too slow (2026-08-11)

**Symptom reported:** "the evening job is not working, prices/news aren't
updating." Root causes turned out to be two unrelated things, found via the
Vercel MCP server's runtime-log/error tools plus GitHub's public Actions
API (deployment and workflow-run history — no `gh` CLI or token available
in this environment, so raw `curl` against `api.github.com`):

1. **GitHub Actions cron auth.** The very first real scheduled run (evening
   slot) failed at the `curl` step — `vars.PREMARKET_BASE_URL` and
   `secrets.PREMARKET_JOB_API_KEY` both resolved to **empty strings** in the
   workflow log (`-H "X-API-Key: "`, a URL with no host — GitHub masks real
   secret values as `***`, so an empty one just prints blank). Despite being
   told "both are set," something about how they were configured (wrong
   tab, environment-scoped instead of repo-level, a name typo) meant the
   workflow couldn't see them. Fixed by generating a brand new
   `PREMARKET_JOB_API_KEY` and having the user set that exact value in both
   Vercel and GitHub Actions secrets fresh, then verifying directly with
   `curl` against the production endpoint (`401` on a wrong key, `200` on
   the right one) rather than trusting either dashboard's "saved" state.
2. **Morning job latency, ~27s end to end**, enough to intermittently hit
   Vercel's function timeout and return a bare 500 — which is what actually
   broke "prices/news not updating" even after (1) was fixed. Diagnosed by
   adding `time.monotonic()` checkpoints around each phase (still in
   `jobs.py`/`news_ai.py`, left in — cheap, and useful if this regresses)
   and redeploying between each measurement:
   - Quotes/GIFT/levels/structure (~14 independent Yahoo/GIFT calls) were
     awaited one at a time — 1.3s once parallelized via `asyncio.gather`
     (was contributing several seconds).
   - `google-generativeai` (the SDK, not the API itself) turned out to
     reinstall its ~50MB dependency tree from scratch on every cold Vercel
     invocation (`grpcio`, `google-api-python-client`, `cryptography`, ...)
     for what is fundamentally one JSON-in/JSON-out HTTP call. Replaced with
     a raw `httpx` POST to Gemini's REST API, matching every other
     integration in this codebase — dependency removed entirely.
   - The real remainder, ~17.5s, was the Gemini call itself — confirmed via
     the phase timing, not assumed. Fixed by cutting `MAX_HEADLINES` 25→12
     (only `TOP_NEWS_COUNT`=4 are ever shown, so classifying 25 with full
     reasoning was mostly wasted generation) and running the whole
     RSS-fetch-plus-Gemini-classify step as a background `asyncio.create_task`
     kicked off at the very start of the job, overlapping it with the
     quotes/positioning work instead of stacking after it.
   - **Two attempted further fixes both failed live and were reverted**,
     worth remembering before trying either again: `generationConfig.
     maxOutputTokens: 1024` made things *worse* — this model spends part of
     its budget on hidden "thinking" tokens before the visible answer, and
     1024 was consumed by that alone, producing an empty/truncated response
     that failed to parse (silently fell back to neutral every time, fast
     but wrong). `thinkingConfig.thinkingBudget: 0` to disable thinking
     outright is **not a supported field on this model/API version** — flat
     `400 Bad Request`, also silently falling back to neutral. Both
     reverted; `MAX_HEADLINES` + the background-task overlap are the only
     verified-safe latency levers found so far.
   - **End state, live-verified over several consecutive runs:** 27s → 11s,
     `HTTP 200`, real classified headlines (not the neutral fallback), both
     jobs (`POST /api/premarket/jobs/evening` and `/morning`) confirmed
     working end-to-end against production.

Progress against the build order:

- [x] Module 1 — `nse_client.py` (+ `tests/test_nse_client.py`). Resolved: file
      lives at repo root as its own module, IST is the fixed-offset pattern
      already used everywhere else, walk-back logic is separated from the
      network calls so it's unit-testable without hitting NSE. **Not yet
      smoke-tested against live NSE** — run `python nse_client.py` from a
      normal machine before the evening job depends on it; the `fo_secban`
      archive path and the `fiidiiTradeReact` response shape are best-guess
      until verified live.
- [x] Module 2 — `storage.py` + `supabase/migrations/0001_premarket_engine.sql`
      (+ `tests/test_storage.py`). Resolved: raw REST via httpx (Open
      Decision 3), upserts on the natural key per table so re-running a job
      is idempotent, `option_snapshots` was dropped in favor of extending the
      existing `pcr_snapshots` table. `SUPABASE_URL` / `SUPABASE_SERVICE_KEY`
      now documented in `.env.example`. **Migration not yet applied** —
      needs running against the real Supabase project before Module 4 can
      read/write for real.
- [x] Module 3 — `market_data.py` + `technicals.py` (+ `tests/test_market_data.py`,
      `tests/test_technicals.py`). `fetch_quotes`/`fetch_ohlc_candles` reuse the
      unauthenticated Yahoo chart API already used by `backend.py` (Open
      Decision 3: no `yfinance` dependency added). `compute_levels()` /
      `compute_structure()` assume the market is closed at call time (true
      for both the 7:30pm and 8:15am jobs) — the *last* daily/15m candle
      Yahoo returns is always the reference "previous day", not `[-2]`.
      `detect_structure()`'s BOS/CHoCH fractal logic is unit-tested directly
      against hand-built candle sequences, independent of the network layer.
      **`fetch_gift_nifty()` is the one piece that's a real guess** — built
      without network access to inspect niftytrader.in/gift-nifty's actual
      markup, so its regex parser almost certainly needs recalibrating
      against the live page before the 40%-weighted GIFT gap score means
      anything.
- [x] Module 4 — `positioning.py` + `scoring.py` (+ `tests/test_positioning.py`,
      `tests/test_scoring.py`, 28 boundary-case tests). `option_snapshot()`
      reads the extended `pcr_snapshots` row (Module 2) — real values wait on
      backend.py's PCR tracker computing `max_call_oi_strike` /
      `max_put_oi_strike` / `max_pain`, which it doesn't do yet; until then
      it degrades to all-`None`, which `compute_expected_range()` already
      falls back on. One thing not in the original spec that needed a call:
      the brief says the event flag is a *weighted* component (10%) but also
      says it "reduce[s] confidence and widen[s] range rather than shift
      direction" — those two instructions conflict, so `compute_score()`
      excludes it from the directional weighted sum entirely (renormalizing
      the other four weights to 100 among themselves) and only uses it to
      drop confidence to "low" and widen `compute_expected_range()`. Also
      picked (undocumented in the original brief) a ±1% US+Asia
      full-weight threshold — worth retuning once real briefs exist to
      compare against.
- [x] Module 5 — `jobs.py` + `main.py` + `api/premarket.py` (+
      `tests/test_jobs.py`, `tests/test_main.py`). **Open Decision 1
      resolved: external cron -> API endpoints**, staying on Vercel rather
      than moving to Railway/a VPS. `POST /api/premarket/jobs/evening` and
      `/morning` are plain API-key-protected serverless endpoints (no
      in-process APScheduler); `.github/workflows/premarket_jobs.yml`
      triggers them on schedule (needs repo variable `PREMARKET_BASE_URL`
      and secret `PREMARKET_JOB_API_KEY` set in GitHub before it'll do
      anything — see the workflow file's header comment). `vercel.json`
      gained a second Python function (`api/premarket.py`, routed at
      `/api/premarket/*`) alongside the existing `api/index.py`, kept
      separate so a redeploy of one never touches the other. Routes are
      registered with the full `/api/premarket/...` path baked into each
      `@app.get`/`@app.post` — Vercel forwards the whole incoming path
      without stripping a prefix, same as `api/index.py` already does.
      `GET /brief/history` computes hit/miss by re-fetching `^NSEI`'s
      actual next-day open per brief and comparing direction (`up`/`down`/
      `flat`, with a ±0.1% dead zone) against the verdict — this means
      history calls do real Yahoo fetches per row and aren't cheap; fine at
      30 rows, would want batching before scaling `days` up much further.
      Skipped the brief's Pydantic-response-model requirement to match this
      repo's existing dict-based API convention instead (Open Decision-style
      call, documented in main.py's module docstring). `evening` and
      `morning` jobs split cleanly: evening persists `participant_oi` /
      `fii_dii_cash` (+ returns the ban list, unpersisted — no table for it
      in Module 2's migration, and nothing downstream reads it yet);
      `compute_levels()`/`compute_structure()` moved to the morning job only
      (see jobs.py's module docstring) since both give the same answer
      whether computed at 7:30pm or 8:15am, so computing them twice would be
      pure waste.
- [x] Module 6 — `notify.py` (+ `tests/test_notify.py`). Plain httpx POST to
      the Telegram Bot API, no library, same no-op-when-unconfigured
      convention as `storage.py`. `jobs.run_morning_job()` now calls
      `notify.send_brief()` itself right after persisting the brief (per the
      original brief: "Send at 8:30am after morning job completes" — read as
      "immediately following," not a separately scheduled trigger, so there's
      no extra cron entry for this). "Top 3 signals" are the three
      components with the largest `|weight * score|`, i.e. the ones that
      actually moved the needle, not just whichever three happened to be
      present. Added `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` to
      `.env.example`, plus a `news_sentiment` field convention on the brief
      dict for Module 8 to fill in later — `format_brief_message()` already
      reads it if present, falls back to "unavailable" until then.
- [x] Module 7 — `premarket.html` + `premarket.jsx`. **Resolved (Open
      Decision 2, frontend half): no Vite build**, matching `index.html`/
      `main.jsx`'s existing zero-build setup exactly (Babel-standalone
      in-browser transpile, React/Recharts via an esm.sh importmap) rather
      than introducing a bundler this repo has never had. It's a second,
      independent page (`/premarket.html`), not a tab bolted onto the PCR
      tool — cross-linked from both headers. Design tokens and the
      backend-URL auto-detection helper are duplicated from `main.jsx`
      rather than imported, for the same zero-build reason `nse_client.py`
      duplicated the cookie-priming pattern instead of importing from
      `backend.py` (see the table at the top of this doc) — there's no
      module system to share code between two Babel-standalone pages
      without adding a build step neither page has today.
      **`jobs.py`'s brief payload was extended** to persist the raw
      per-symbol `us_quotes`/`asia_quotes`/`macro_quotes` dicts (previously
      only the aggregated `us_asia`/`macro` *scores* were kept) — the
      dashboard's "Live Cues (GIFT/US/Asia)" and "Macro" panels need
      Dow/Nasdaq/S&P and Nikkei/HangSeng/Kospi/Shanghai individually, not
      just an average; `morning_briefs.components` is `jsonb` so this
      needed no migration change. `vercel.json` gained static-build entries
      for both new files (**not yet deployed/smoke-tested** — no Node.js
      available in this environment to run Babel or a dev server, so this
      was built and reviewed by hand: braces/parens verified balanced, but
      it hasn't rendered in an actual browser. Load `/premarket.html`
      locally against a running `uvicorn main:app` before trusting it.)
      Macro panel colors by each metric's *scoring-sign-adjusted* impact
      (e.g. crude up = red, since that's bearish for Nifty per
      `scoring.py`), not raw price direction — flagged in the panel's own
      footnote so it doesn't read as a bug.
- [x] Module 8 — `news_ai.py` (+ `tests/test_news_ai.py`), built last as
      designed — everything else already works without it. RSS parsing uses
      stdlib `xml.etree.ElementTree` (no new parsing dependency); Gemini is
      called via `google-generativeai` (added to `requirements.txt`,
      installed), imported lazily inside `classify_headlines()` so its
      absence can't break anything else importing this module. Falls back to
      an all-neutral result with a `note` whenever `GEMINI_API_KEY` is unset
      or the call fails, per spec. Wired into `jobs.run_morning_job()`
      (wrapped in try/except, same as every other source) — the brief's
      `headlines`/`news_sentiment` fields go from always-empty placeholders
      to the real thing. **This needed a small schema change**:
      `morning_briefs` had no column for `news_sentiment` (Module 2's
      migration only planned for `score`/`verdict`/`expected_low`/
      `expected_high`/`components`/`headlines`), so
      `supabase/migrations/0002_news_sentiment.sql` adds it — Supabase's
      REST API rejects unknown columns in the payload, so without this the
      morning job's persistence would have started failing silently. Two
      things are genuinely unverified, flagged in the module's own
      docstring: `RSS_FEEDS` (Reuters discontinued most public RSS years
      ago; the URL there is a guess) and `GEMINI_MODEL`'s default (a
      best-guess current Flash model id — this repo's knowledge cutoff is
      older than "now," so check it against the live model list).

## How this plugs into the existing PCR tracker

This repo (`pcr`) already implements a slice of what the engine needs:

| Engine module | Already exists here | Notes |
|---|---|---|
| `nse_client.py` cookie priming | `backend.py:114-165` (`get_client`, `fetch_option_chain*`) | Same NSE-blocks-bare-clients problem, same fix (prime cookies via `PRIME_URL`, browser headers, session reuse). New fetchers (`fetch_participant_oi`, `fetch_fii_dii_cash`, `fetch_ban_list`) should reuse this session/header pattern rather than building a second client. |
| Option chain / PCR | `compute_pcr`, `build_chain_rows`, `/pcr/today`, `/optionchain/today` | This **is** the "existing PCR tracker" Module 4 refers to. `option_snapshot()`'s mock should read from the `pcr_snapshots` table this backend already writes, not a new empty `option_snapshots` table — check field names line up (max pain / max OI strike aren't computed yet here, would need adding). |
| Market candles | `fetch_yahoo_candles` (`backend.py:436`) | Raw httpx calls to Yahoo's chart API, not the `yfinance` package. Decide: keep this pattern for `market_data.py`/`technicals.py`, or add the `yfinance` dependency as the original prompt specifies. Recommend staying with raw httpx + Yahoo chart API for consistency and one fewer dependency, unless `yfinance`'s ticker set (`^N225`, `DX-Y.NYB`, etc.) proves easier that way. |
| Supabase persistence | `maybe_persist` (`backend.py:275`) | Uses **raw REST via httpx** (`POST {SUPABASE_URL}/rest/v1/pcr_snapshots`), not `supabase-py`. New tables (`participant_oi`, `fii_dii_cash`, `macro_snapshots`, `morning_briefs`) should follow the same raw-REST pattern for consistency — pulling in `supabase-py` as a new dependency needs an explicit decision, not a default. |
| IST handling | Fixed-offset `dt.timezone(timedelta(hours=5, minutes=30))` (`backend.py:51`) | Original prompt asks for `zoneinfo`. Fixed offset is what's already used everywhere in this file; switching only the new modules to `zoneinfo` would be an inconsistency worth avoiding — pick one and apply repo-wide. |
| Scheduler | `AsyncIOScheduler` job in `startup()` (`backend.py:307-311`), **but** deployed to Vercel via `api/index.py` | See "Open decisions" below — this is the biggest structural conflict with the original prompt. |
| `.env` | `.env.example` only documents Upstox vars today | Needs new rows for `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` (already read in code but undocumented), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GEMINI_API_KEY`. |

## Open decisions to make before building (don't assume)

1. **Hosting/scheduler model.** This repo deploys to Vercel (`vercel.json`), and per recent commit history (`cb7730a Re-add cron at once/day (Hobby-plan-safe)`, `3e02dc3 Diagnostic: remove crons from vercel.json, suspected deploy blocker`) the Hobby plan constrains crons hard — likely to one run/day. The engine needs **two** daily triggers (7:30 PM evening job, 8:15 AM morning job) plus in-process `APScheduler`, which doesn't fit a serverless function at all (no long-running process, no guaranteed warm state between requests). Original prompt assumes Railway or a VPS. Pick one:
   - Move this whole backend off Vercel to an always-on box (matches the original prompt's assumption, keeps `APScheduler` as-is).
   - Keep Vercel for the API/dashboard, add two separate external cron triggers (e.g. GitHub Actions scheduled workflows, or `cron-job.org`) hitting protected `POST /jobs/evening` / `POST /jobs/morning` endpoints instead of relying on in-process `APScheduler`.
   - Upgrade the Vercel plan if it lifts the cron-count/frequency limit enough for two daily crons.
2. **Single-file vs. package layout.** `backend.py` is one ~22K file today. The original prompt's module split (`nse_client.py`, `market_data.py`, `technicals.py`, `positioning.py`, `news_ai.py`, `scoring.py`, `notify.py`, `main.py`) is a real package layout. Confirm whether to split the whole backend into modules now (touching existing PCR code too) or keep the engine additive as new files alongside the existing monolith and only refactor `backend.py` later.
3. **`supabase-py` vs. raw REST**, **`yfinance` vs. existing Yahoo chart calls**, **`zoneinfo` vs. fixed IST offset** — pick one per the table above rather than mixing patterns file-by-file.
4. **GIFT Nifty scraping target.** `niftytrader.in/gift-nifty` markup can change without notice; confirm this is still the acceptable source (vs. an API) before building — this was the one module the original prompt already flagged as "wrap in try/except, don't crash."

## Adapted module list (build order preserved from original)

1. **`nse_client.py`** — extend, don't duplicate, the cookie-priming client in `backend.py`. Add `fetch_participant_oi(date)`, `fetch_fii_dii_cash()`, `fetch_ban_list(date)` with DDMMYYYY URL builder + 5-day holiday walk-back (hardcoded NSE holiday list for 2026). Tests: URL builder, walk-back logic, CSV parser fixture.
2. **Storage** — new Supabase tables per the migration below, written via whichever client pattern was chosen in Open Decision 3.
3. **`market_data.py` + `technicals.py`** — GIFT Nifty scrape, US/Asia/macro quotes, PDH/PDL/structure (BOS/CHoCH) off `^NSEI`.
4. **`positioning.py` + `scoring.py`** — FII long/short ratio + trend; weighted score (GIFT gap 40%, US+Asia 20%, macro 15%, FII positioning 15%, event flag 10%); wire `option_snapshot()` to the existing `pcr_snapshots` table.
5. **Jobs + scheduler + API** — per Open Decision 1; `GET /brief/today`, `GET /brief/history`, `GET /positioning/fii-trend`, `POST /jobs/evening`, `POST /jobs/morning` (API-key protected, same pattern as existing `/upstox/configure`).
6. **`notify.py`** — Telegram brief at 8:30 AM, disclaimer line mandatory.
7. **Dashboard** — decide whether this becomes a second page/section in the existing `main.jsx` single-file frontend or a genuinely separate Vite React app as the original prompt specifies.
8. **`news_ai.py`** — Gemini Flash headline classifier, last, system works without it.

## Supabase migration (unchanged from original prompt, adjust to REST pattern chosen)

```sql
participant_oi(date, participant, segment fields, long, short, computed ratio)
fii_dii_cash(date, fii_buy, fii_sell, dii_buy, dii_sell)
macro_snapshots(timestamp, symbol, price, pct_change, session: 'evening'|'morning')
option_snapshots(date, max_call_oi_strike, max_put_oi_strike, pcr, max_pain)
morning_briefs(date, score, verdict, expected_low, expected_high, components jsonb, headlines jsonb, created_at)
```

Note: `option_snapshots` may be unnecessary as a separate table if `pcr_snapshots`
(already populated by this backend) is extended with `max_call_oi_strike` /
`max_put_oi_strike` / `max_pain` columns instead — avoids two sources of truth
for the same option chain.

---

## Original build prompt (verbatim, for reference)

You are building a **Nifty Pre-Market Analysis Engine** — a full-stack system that collects overnight and end-of-day market data, scores it, and produces a "Morning Brief" verdict (gap-up / flat / gap-down + expected range) before Indian market open at 9:15 AM IST.

### Tech Stack (use exactly this)

- **Backend:** Python 3.11+, FastAPI, APScheduler for jobs, httpx for HTTP, pandas for parsing, yfinance for market data
- **Database:** Supabase (Postgres) via `supabase-py`
- **Frontend:** React (Vite) with a single-page dashboard, Tailwind for styling
- **Notifications:** Telegram Bot API (simple httpx POST, no library needed)
- **Config:** All secrets in `.env` (Supabase URL/key, Telegram bot token/chat ID)

### System Overview

Two scheduled jobs write to Supabase; a FastAPI API serves the data; a React dashboard displays it.

1. **Evening Job (7:30 PM IST, Mon–Fri):** pulls end-of-day positioning data — NSE participant OI, FII/DII cash activity, option chain snapshot, F&O ban list, previous-day OHLC/levels.
2. **Morning Job (8:15 AM IST, Mon–Fri):** pulls live cues — GIFT Nifty, US close, Asian markets live, crude, USD/INR, DXY, US 10Y yield — then computes the score and sends the Telegram brief at 8:30 AM.

### Module 1 — NSE Fetcher (`nse_client.py`) — BUILD THIS FIRST

NSE blocks naive scripts. Implement a session-based client:

- Create an `httpx.Client` with browser-like headers: real Chrome User-Agent, `Accept-Language: en-US,en;q=0.9`, `Referer: https://www.nseindia.com/`
- On init, GET `https://www.nseindia.com` first to collect cookies into the session, THEN make data requests with the same session
- Retry with exponential backoff (3 attempts); if cookies expire (401/403), re-warm the session
- Rate limit: minimum 2 seconds between requests

Functions to implement:

1. `fetch_participant_oi(date: date) -> pd.DataFrame`
   - URL pattern: `https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_DDMMYYYY.csv` (date format is **DDMMYYYY**, e.g. 10082026 = 10 Aug 2026)
   - Parse CSV (note: first row is a header line to skip; columns include Client Type, Future Index Long, Future Index Short, Option Index Call Long, Option Index Put Long, Option Index Call Short, Option Index Put Short, etc.)
   - Return rows for Client, DII, FII, Pro
2. `fetch_fii_dii_cash() -> dict` — FII/DII daily cash buy/sell values (NSE fii-dii API endpoint or fallback to scraping)
3. `fetch_ban_list(date: date) -> list[str]` — F&O securities in ban period (`fo_secban.csv`, same archives pattern)
4. Handle weekends/holidays: if a file 404s, try the previous trading day (walk back up to 5 days). Maintain a hardcoded NSE holiday list for the current year.

### Module 2 — Market Data (`market_data.py`)

Use yfinance. Implement `fetch_quotes(symbols: list[str]) -> dict` returning last price, previous close, % change for:

- US indices: `^DJI`, `^IXIC`, `^GSPC`
- Asia: `^N225`, `^HSI`, `^KS11`, `000001.SS`
- Macro: `BZ=F` (Brent), `CL=F` (WTI), `USDINR=X`, `DX-Y.NYB` (DXY), `^TNX` (US 10Y)
- India: `^NSEI` (Nifty spot, previous day OHLC)

For **GIFT Nifty**: build `fetch_gift_nifty()` that scrapes `https://www.niftytrader.in/gift-nifty` (parse the displayed price and change). Wrap in try/except; if scraping fails, log a warning and mark GIFT data as unavailable rather than crashing — the score function must handle missing GIFT gracefully.

### Module 3 — Levels & Technicals (`technicals.py`)

From `^NSEI` daily + 15-minute data (yfinance):

- `compute_levels()` → PDH, PDL, previous close, previous day range, where close sat in range (%)
- `detect_structure()` → on 15m candles for the last 5 sessions: track swing highs/lows (fractal window of 2), detect most recent BOS (break of structure) and CHoCH (change of character), and report current bias: bullish / bearish / neutral. Keep the logic simple and documented — swing high broken upward after downtrend = CHoCH bullish, etc.

### Module 4 — Positioning Analytics (`positioning.py`)

- `fii_long_short_ratio(df)` → from participant OI: FII index futures long / (long + short), as %. Store daily; compute 5-day trend (rising/falling/flat).
- `option_snapshot()` → placeholder function with a clear interface: `{max_call_oi_strike, max_put_oi_strike, pcr, max_pain}`. I will wire this to my existing PCR tracker (Upstox/Dhan) — just define the interface and a mock that reads from a Supabase table `option_snapshots` if present.

### Module 5 — News Classifier (`news_ai.py`)

- Pull RSS headlines from Reuters (business), Moneycontrol, Economic Times markets (last 12 hours only)
- Send batch of headlines to **Gemini Flash** (`google-generativeai` lib, key from env) with prompt: classify each headline's likely Nifty impact as bullish / bearish / neutral with a one-line reason; also return one overall sentiment line for the morning
- Return structured JSON. If Gemini call fails, return neutral with a note. Cap at 25 headlines.

### Module 6 — Scoring & Brief (`scoring.py`)

Compute a weighted score from -100 (strong gap-down) to +100 (strong gap-up):

- GIFT Nifty gap vs fair value: **40%** (fair value = spot close + 35 pts premium; gap % maps linearly, ±0.5% = ±full weight)
- US close + Asia live average: **20%** (Asia weighted 60/40 over US since it's fresher)
- Macro flags: **15%** (crude move beyond ±2% counts against/for; USDINR beyond ±0.3%; DXY beyond ±0.5%; US 10Y beyond ±10bps — each flag contributes proportionally)
- FII positioning: **15%** (long-short ratio level + 5-day trend direction)
- Event flag: **10%** (expiry day, major scheduled events reduce confidence and widen expected range rather than shift direction)

Output verdict: score > +25 = "Gap-up likely", -25 to +25 = "Flat open", < -25 = "Gap-down likely". Expected range = [max_put_oi_strike, max_call_oi_strike] from option snapshot, else PDL–PDH.

Every brief must include the line: "Automated analysis for information only — not investment advice."

### Module 7 — Storage (Supabase)

Create SQL migration file with tables:

- `participant_oi` (date, participant, segment fields, long, short, computed ratio)
- `fii_dii_cash` (date, fii_buy, fii_sell, dii_buy, dii_sell)
- `macro_snapshots` (timestamp, symbol, price, pct_change, session: 'evening'|'morning')
- `option_snapshots` (date, max_call_oi_strike, max_put_oi_strike, pcr, max_pain)
- `morning_briefs` (date, score, verdict, expected_low, expected_high, components jsonb, headlines jsonb, created_at)

### Module 8 — API (`main.py`)

FastAPI endpoints:

- `GET /brief/today` — latest morning brief with all components
- `GET /brief/history?days=30` — past briefs + what actually happened (join with next-day open from `^NSEI` to show hit/miss — this accuracy tracking is important)
- `GET /positioning/fii-trend?days=30` — FII long-short ratio series
- `POST /jobs/evening` and `POST /jobs/morning` — manual triggers (protected by a simple API key header)
- CORS enabled for the React dev origin

### Module 9 — Telegram (`notify.py`)

`send_brief(brief)` → formatted message: verdict emoji + score, GIFT gap, top 3 signals, expected range, one-line AI news sentiment, and the disclaimer line. Send at 8:30 AM after morning job completes.

### Module 10 — React Dashboard

Single page, dark theme, mobile-friendly:

- Top: verdict card (big score dial, verdict text, expected range)
- Grid of tier panels: Live Cues (GIFT/US/Asia), Macro (crude/INR/DXY/10Y with red/green flags), Positioning (FII ratio sparkline for 30 days, PCR), Events & News (AI-classified headlines with sentiment chips), Levels (PDH/PDL/structure bias)
- History table: last 30 briefs with predicted vs actual open (hit rate % at top)
- Poll `GET /brief/today` every 60s between 8:00–9:30 AM IST, otherwise on load

### Engineering Requirements

- Type hints everywhere, Pydantic models for all API responses
- Structured logging (loguru) — every fetch logs source, latency, success/failure
- Every external fetch wrapped so ONE failing source never kills the job; the brief marks missing components as "unavailable" and renormalizes weights
- `README.md` with setup steps, env vars table, cron/deploy notes (assume Railway or a VPS), and the NSE cookie-handling explanation
- Tests: unit tests for the score function (all boundary cases), the DDMMYYYY URL builder, holiday walk-back logic, and CSV parser with a fixture file
- Timezone handling: everything in IST (`zoneinfo`), never naive datetimes

### Build Order

1. `nse_client.py` + tests (hardest part — cookie handling)
2. Supabase migration + storage layer
3. `market_data.py` + `technicals.py`
4. `positioning.py` + `scoring.py` + tests
5. Jobs + scheduler + `main.py` API
6. `notify.py` Telegram
7. React dashboard
8. `news_ai.py` Gemini classifier (last — system works without it)
