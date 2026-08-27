import React, { useState, useEffect, useCallback, useMemo, useRef } from "react";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceDot,
  BarChart, Bar, Cell, AreaChart, Area,
} from "recharts";
import { createChart, CrosshairMode } from "lightweight-charts";

/* ---------- design tokens ----------
   Mirrors main.jsx's palette exactly for visual consistency across the two
   pages of this app. Duplicated rather than imported — this is a zero-build
   setup (index.html/premarket.html transpile their .jsx in-browser via
   Babel standalone, no bundler), so there's no module system to share a
   tokens file between them without adding a build step neither page has. */
const T = {
  ink: "#0A0F1E", panel: "#111C33", panel2: "#0E1830", line: "#22304F",
  fg: "#E6ECF8", muted: "#6B7A99", cyan: "#34E0C8", amber: "#FFC24B",
  put: "#3DDC97", call: "#F2789F",
};
const DISP = "'Space Grotesk', system-ui, sans-serif";
const MONO = "'IBM Plex Mono', ui-monospace, 'SF Mono', monospace";

const VERDICT_COLOR = { "Gap-up likely": T.put, "Gap-down likely": T.call, "Flat open": T.amber };
const VERDICT_EMOJI = { "Gap-up likely": "🟢", "Gap-down likely": "🔴", "Flat open": "🟡" };
const CONFIDENCE_COLOR = { high: T.put, medium: T.amber, low: T.call };
const TREND_COLOR = { rising: T.put, falling: T.call, flat: T.amber };
const SENTIMENT_COLOR = { bullish: T.put, bearish: T.call, neutral: T.amber };

/* Same origin in production (Vercel serves the static page and the API
   under the same host); local dev runs the API on :8000 separately from
   whatever's serving this static file — same convention as main.jsx's
   detectDefaultBackendUrl(), duplicated here for the same zero-build reason
   as the tokens above. */
function detectDefaultBackendUrl() {
  if (typeof window === "undefined") return "http://127.0.0.1:8000";
  const { hostname, protocol, origin } = window.location;
  const isLocalDev = hostname === "localhost" || hostname === "127.0.0.1" ||
    /^192\.168\.\d+\.\d+$/.test(hostname) || /^10\.\d+\.\d+\.\d+$/.test(hostname) ||
    /^172\.(1[6-9]|2\d|3[01])\.\d+\.\d+$/.test(hostname);
  return isLocalDev ? `${protocol}//${hostname}:8000` : origin;
}
const API_BASE = `${detectDefaultBackendUrl().replace(/\/$/, "")}/api/premarket`;
// Same origin, the existing PCR tracker's app (api/index.py, not this
// engine) — reused only to pull a live option premium for the paper
// trading form's "fetch live" button, nothing else.
const PCR_API_BASE = `${detectDefaultBackendUrl().replace(/\/$/, "")}/api`;

function fmtNum(v, digits = 2) {
  return typeof v === "number" && !Number.isNaN(v) ? v.toFixed(digits) : "—";
}
function fmtSigned(v, digits = 2) {
  if (typeof v !== "number" || Number.isNaN(v)) return "—";
  return `${v >= 0 ? "+" : ""}${v.toFixed(digits)}`;
}
function directionColor(v) {
  return typeof v === "number" ? (v >= 0 ? T.put : T.call) : T.muted;
}

async function getJSON(path) {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function postJSON(path, body) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new Error((data && data.detail) || `HTTP ${res.status}`);
  return data;
}

/* ---------- small building blocks ---------- */
function Panel({ title, right, children }) {
  return (
    <div style={{ background: T.panel, border: `1px solid ${T.line}`, borderRadius: 12, padding: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
        <div style={{ fontFamily: DISP, fontSize: 12, fontWeight: 700, color: T.muted, textTransform: "uppercase", letterSpacing: 0.6 }}>
          {title}
        </div>
        {right}
      </div>
      {children}
    </div>
  );
}

function Row({ label, value, color }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "7px 0", borderBottom: `1px solid ${T.line}` }}>
      <span style={{ fontFamily: DISP, fontSize: 13, color: T.muted }}>{label}</span>
      <span style={{ fontFamily: MONO, fontSize: 13, color: color || T.fg, fontWeight: 600 }}>{value}</span>
    </div>
  );
}

function Chip({ children, color }) {
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 6, padding: "3px 10px", borderRadius: 999,
      background: `${color}22`, color, fontFamily: DISP, fontSize: 11, fontWeight: 700,
      border: `1px solid ${color}55`, whiteSpace: "nowrap",
    }}>
      {children}
    </span>
  );
}

function EmptyNote({ children }) {
  return <div style={{ fontFamily: DISP, fontSize: 12, color: T.muted, padding: "8px 0" }}>{children}</div>;
}

/* ---------- plain-language open outlook (mirrors scoring.build_tomorrow_outlook) ---------- */
// Used when an older brief was persisted before the backend started storing
// `outlook` — so the dashboard still explains "what to expect tomorrow"
// from fields already on the brief.
function buildTomorrowOutlookClient(brief) {
  if (!brief || brief.score == null) return null;
  const c = brief.components || {};
  const verdict = brief.verdict || "Flat open";
  const score = brief.score;
  const pred = brief.predicted_open;
  const prev = c.previous_close;
  const levels = c.levels || {};
  const gift = c.gift || {};
  const usAsia = c.us_asia || {};
  const macro = c.macro || {};
  const fii = c.fii || {};
  const fmt = (x) => (x == null || Number.isNaN(Number(x)) ? null : Number(x).toLocaleString("en-IN", { maximumFractionDigits: 0 }));
  const tone = (s) => (s == null ? "mixed" : s > 0.25 ? "supportive" : s < -0.25 ? "pressuring" : "mixed");
  const predS = fmt(pred), prevS = fmt(prev);
  const lowS = fmt(brief.expected_low), highS = fmt(brief.expected_high);
  const pdhS = fmt(levels.pdh), pdlS = fmt(levels.pdl);

  let headline, openExpectation, firstHour;
  if (verdict === "Gap-up likely") {
    headline = "Tomorrow leans gap-up / constructive open";
    openExpectation = `Expect Nifty to open above prior close${predS ? ` (~${predS})` : ""}, with early buyers favoured if the open holds.`;
    firstHour = [
      "If price holds above the predicted open / prior close in the first 15–30 min, dips toward that level are the usual long-side watch.",
      "If the gap fails quickly and slips back under prior close, treat the open bias as cancelled — wait for structure.",
    ];
  } else if (verdict === "Gap-down likely") {
    headline = "Tomorrow leans gap-down / soft open";
    openExpectation = `Expect Nifty to open below prior close${predS ? ` (~${predS})` : ""}, with early sellers favoured if the open holds.`;
    firstHour = [
      "If price stays below the predicted open / prior close in the first 15–30 min, bounces into that zone are the usual short-side watch.",
      "If the gap is bought aggressively and reclaims prior close, treat the soft-open bias as cancelled — wait for structure.",
    ];
  } else {
    headline = "Tomorrow leans flat / indecisive open";
    openExpectation = `Expect Nifty to open near prior close${predS ? ` (~${predS})` : ""} — no strong overnight edge; wait for the first impulse.`;
    firstHour = [
      "Avoid chasing the first spike; let 9:15–9:45 IST define direction.",
      "Trade the break/hold of the opening range or a clear reclaim of prior day high/low rather than the open print itself.",
    ];
  }

  const why = [];
  if (gift.gap_pct != null) {
    why.push(`GIFT Nifty is ${tone(gift.score)} (gap ${gift.gap_pct >= 0 ? "+" : ""}${Number(gift.gap_pct).toFixed(2)}% vs fair value${gift.price != null ? `, last ${Number(gift.price).toLocaleString("en-IN")}` : ""}) — this is the strongest open cue.`);
  }
  if (usAsia.score != null) {
    const bits = [];
    if (usAsia.us_avg_pct != null) bits.push(`US ${usAsia.us_avg_pct >= 0 ? "+" : ""}${Number(usAsia.us_avg_pct).toFixed(2)}%`);
    if (usAsia.asia_avg_pct != null) bits.push(`Asia ${usAsia.asia_avg_pct >= 0 ? "+" : ""}${Number(usAsia.asia_avg_pct).toFixed(2)}%`);
    why.push(`Overnight equities are ${tone(usAsia.score)}${bits.length ? ` (${bits.join(", ")})` : ""}.`);
  }
  if (macro.score != null) why.push(`Macro is ${tone(macro.score)} for Nifty.`);
  if (fii.ratio != null) {
    why.push(`FII index futures are ${fii.ratio >= 50 ? "net long" : "net short"} (${Number(fii.ratio).toFixed(1)}% long/short, trend ${fii.trend || "flat"}) — positioning bias only, not an intraday trigger.`);
  }
  if (brief.news_sentiment) why.push(`News tone: ${brief.news_sentiment}`);

  const keyLevels = [];
  if (predS && prevS) keyLevels.push(`Predicted open ~${predS} (prior close ${prevS})`);
  else if (predS) keyLevels.push(`Predicted open ~${predS}`);
  else if (prevS) keyLevels.push(`Prior close ${prevS}`);
  if (lowS && highS) keyLevels.push(`Expected reaction band ${lowS} – ${highS}`);
  if (pdlS && pdhS) keyLevels.push(`Prior day low/high ${pdlS} / ${pdhS}`);

  const confidence = c.confidence || "medium";
  let confidenceNote = confidence === "high"
    ? "Inputs are mostly complete — use as the base open plan, still confirm at 9:15."
    : confidence === "low"
      ? "Confidence is low (missing data and/or event day) — treat this as a soft sketch, not a plan."
      : "Confidence is medium — some inputs missing; size down conviction until the open confirms.";
  if (c.is_event_day) confidenceNote += " Event/expiry day: expect wider swings; the range is already widened.";

  return {
    headline: `${headline} (score ${score >= 0 ? "+" : ""}${Math.round(score)})`,
    open_expectation: openExpectation,
    why, key_levels: keyLevels, first_hour_plan: firstHour,
    confidence_note: confidenceNote,
    scope: "Open + first hour only — not a full-day prediction.",
  };
}

/* ---------- verdict card ---------- */
function CardIconButton({ onClick, title, active, children }) {
  return (
    <button
      onClick={onClick}
      title={title}
      aria-label={title}
      style={{
        width: 30, height: 30, borderRadius: "50%",
        display: "flex", alignItems: "center", justifyContent: "center", fontSize: 14, lineHeight: 1,
        background: active ? T.cyan : T.panel2, color: active ? T.ink : T.muted,
        border: `1px solid ${active ? T.cyan : T.line}`, cursor: "pointer", flexShrink: 0,
      }}
    >
      {children}
    </button>
  );
}

// No auto-refresh (turned off deliberately) — these two small icon buttons
// on the card itself are the only way data updates: manual refresh, and
// toggling the history sheet/table.
function CardCornerButtons({ showHistory, onToggleHistory, onRefresh, refreshing }) {
  return (
    <div style={{ position: "absolute", top: 14, right: 14, display: "flex", gap: 8 }}>
      <CardIconButton onClick={onRefresh} title="Refresh now">
        {refreshing ? "…" : "↻"}
      </CardIconButton>
      <CardIconButton onClick={onToggleHistory} title={showHistory ? "Hide history" : "Show history"} active={showHistory}>
        🕒
      </CardIconButton>
    </div>
  );
}

function VerdictCard({ brief, liveGift, liveScore, showHistory, onToggleHistory, onRefresh, refreshing }) {
  if (!brief || brief.score == null) {
    return (
      <div style={{ gridColumn: "1 / -1", background: T.panel, border: `1px solid ${T.line}`, borderRadius: 16, padding: 24, position: "relative" }}>
        <CardCornerButtons showHistory={showHistory} onToggleHistory={onToggleHistory} onRefresh={onRefresh} refreshing={refreshing} />
        <div style={{ fontFamily: DISP, fontSize: 15, color: T.muted }}>
          {brief?.note || "No brief yet — the morning job runs at 8:15am IST on trading days."}
        </div>
      </div>
    );
  }
  const score = liveScore?.score ?? brief.score;
  const verdict = liveScore?.verdict ?? brief.verdict;
  const confidence = liveScore?.confidence ?? brief.components?.confidence;
  const missing = liveScore?.missing ?? brief.components?.missing ?? [];
  const color = VERDICT_COLOR[verdict] || T.muted;
  const outlook = liveScore?.outlook || brief.outlook || brief.components?.outlook || buildTomorrowOutlookClient(brief);
  const livePredicted = liveScore?.predicted_open ?? (liveGift?.available ? liveGift.predicted_open : null);
  const predictedOpen = livePredicted ?? brief.predicted_open;
  const predictedLive = livePredicted != null;
  const scoreLive = liveScore?.score != null;
  return (
    <div style={{
      gridColumn: "1 / -1", background: T.panel, border: `1px solid ${color}55`, borderRadius: 16, padding: 24,
      display: "flex", flexDirection: "column", gap: 18, position: "relative",
    }}>
      <CardCornerButtons showHistory={showHistory} onToggleHistory={onToggleHistory} onRefresh={onRefresh} refreshing={refreshing} />
      <div style={{ display: "flex", alignItems: "center", gap: 24, flexWrap: "wrap" }}>
        <div style={{
          width: 108, height: 108, borderRadius: "50%", border: `3px solid ${color}`, display: "flex",
          flexDirection: "column", alignItems: "center", justifyContent: "center", flexShrink: 0,
        }}>
          <div style={{ fontFamily: MONO, fontSize: 26, fontWeight: 700, color }}>{fmtSigned(score, 0)}</div>
          <div style={{ fontFamily: DISP, fontSize: 9, color: T.muted, letterSpacing: 0.6 }}>
            {scoreLive ? "LIVE" : "SCORE"}
          </div>
        </div>
        <div style={{ flex: 1, minWidth: 220 }}>
          <div style={{ fontFamily: DISP, fontSize: 22, fontWeight: 700, color: T.fg }}>
            {VERDICT_EMOJI[verdict] || "⚪"} {verdict}
            {scoreLive && <span style={{ marginLeft: 8 }}><Chip color={T.cyan}>live · 1m</Chip></span>}
          </div>
          {predictedOpen != null && (
            <div style={{ fontFamily: MONO, fontSize: 20, fontWeight: 700, color: T.cyan, marginTop: 8 }}>
              ~{fmtNum(predictedOpen, 0)}
              <span style={{ fontFamily: DISP, fontSize: 11, fontWeight: 400, color: T.muted, marginLeft: 8 }}>
                predicted open ({predictedLive ? "live GIFT" : brief.components?.predicted_open_method === "gift_anchored" ? "from GIFT Nifty" : "from score"})
              </span>
            </div>
          )}
          {(liveScore?.gift?.available || liveGift?.available) && (liveScore?.gift?.price ?? liveGift?.price) != null && (
            <div style={{ fontFamily: MONO, fontSize: 13, color: T.fg, marginTop: 6 }}>
              GIFT{" "}
              <span style={{ color: (liveScore?.gift?.change ?? liveGift?.change) != null ? directionColor(liveScore?.gift?.change ?? liveGift?.change) : T.fg, fontWeight: 700 }}>
                {fmtNum(liveScore?.gift?.price ?? liveGift?.price, 1)}
              </span>
              {(liveScore?.gift?.change ?? liveGift?.change) != null && (
                <span style={{ color: directionColor(liveScore?.gift?.change ?? liveGift?.change), marginLeft: 8 }}>
                  {fmtSigned(liveScore?.gift?.change ?? liveGift?.change, 1)}
                </span>
              )}
              <Chip color={T.cyan}>live</Chip>
            </div>
          )}
          <div style={{ fontFamily: MONO, fontSize: 13, color: T.muted, marginTop: 6 }}>
            Range{" "}
            {brief.expected_low != null && brief.expected_high != null
              ? `${fmtNum(brief.expected_low, 0)} – ${fmtNum(brief.expected_high, 0)}`
              : "unavailable"}
            {scoreLive && brief.score != null && liveScore.score !== brief.score && (
              <span style={{ marginLeft: 10 }}>morning brief was {fmtSigned(brief.score, 0)}</span>
            )}
          </div>
          <div style={{ display: "flex", gap: 8, marginTop: 10, flexWrap: "wrap" }}>
            {confidence && <Chip color={CONFIDENCE_COLOR[confidence] || T.muted}>Confidence: {confidence}</Chip>}
            {missing.length > 0 && <Chip color={T.amber}>Missing: {missing.join(", ")}</Chip>}
            {(liveScore?.components ? liveScore : brief.components)?.is_event_day && <Chip color={T.amber}>Event day — range widened</Chip>}
          </div>
        </div>
        <div style={{ fontFamily: DISP, fontSize: 11, color: T.muted, maxWidth: 220, borderLeft: `1px solid ${T.line}`, paddingLeft: 20, paddingTop: 24, paddingRight: 50 }}>
          {brief.disclaimer || "Automated analysis for information only — not investment advice."}
        </div>
      </div>

      {outlook && (
        <div style={{
          borderTop: `1px solid ${T.line}`, paddingTop: 16, paddingRight: 50,
          display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 16,
        }}>
          <div style={{ gridColumn: "1 / -1" }}>
            <div style={{ fontFamily: DISP, fontSize: 11, letterSpacing: 0.8, color: T.cyan, textTransform: "uppercase", marginBottom: 6 }}>
              What to expect tomorrow
            </div>
            <div style={{ fontFamily: DISP, fontSize: 16, fontWeight: 700, color: T.fg, marginBottom: 6 }}>
              {outlook.headline}
            </div>
            <div style={{ fontFamily: DISP, fontSize: 14, color: T.fg, lineHeight: 1.45 }}>
              {outlook.open_expectation}
            </div>
            {outlook.scope && (
              <div style={{ fontFamily: DISP, fontSize: 11, color: T.muted, marginTop: 6 }}>{outlook.scope}</div>
            )}
          </div>
          {!!(outlook.why || []).length && (
            <div>
              <div style={{ fontFamily: DISP, fontSize: 11, letterSpacing: 0.6, color: T.muted, textTransform: "uppercase", marginBottom: 8 }}>Why</div>
              <ul style={{ margin: 0, paddingLeft: 18, fontFamily: DISP, fontSize: 13, color: T.fg, lineHeight: 1.5 }}>
                {outlook.why.map((w, i) => <li key={i} style={{ marginBottom: 4 }}>{w}</li>)}
              </ul>
            </div>
          )}
          {!!(outlook.key_levels || []).length && (
            <div>
              <div style={{ fontFamily: DISP, fontSize: 11, letterSpacing: 0.6, color: T.muted, textTransform: "uppercase", marginBottom: 8 }}>Key levels</div>
              <ul style={{ margin: 0, paddingLeft: 18, fontFamily: MONO, fontSize: 12, color: T.fg, lineHeight: 1.55 }}>
                {outlook.key_levels.map((w, i) => <li key={i} style={{ marginBottom: 4 }}>{w}</li>)}
              </ul>
            </div>
          )}
          {!!(outlook.first_hour_plan || []).length && (
            <div>
              <div style={{ fontFamily: DISP, fontSize: 11, letterSpacing: 0.6, color: T.muted, textTransform: "uppercase", marginBottom: 8 }}>First hour plan</div>
              <ul style={{ margin: 0, paddingLeft: 18, fontFamily: DISP, fontSize: 13, color: T.fg, lineHeight: 1.5 }}>
                {outlook.first_hour_plan.map((w, i) => <li key={i} style={{ marginBottom: 4 }}>{w}</li>)}
              </ul>
            </div>
          )}
          {outlook.confidence_note && (
            <div style={{ gridColumn: "1 / -1", fontFamily: DISP, fontSize: 12, color: T.amber, lineHeight: 1.4 }}>
              {outlook.confidence_note}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ---------- live cues ---------- */
const US_LABELS = { us_dow: "Dow", us_nasdaq: "Nasdaq", us_sp500: "S&P 500" };
const ASIA_LABELS = { asia_nikkei: "Nikkei", asia_hangseng: "Hang Seng", asia_kospi: "Kospi", asia_shanghai: "Shanghai" };

function QuoteRow({ label, quote }) {
  if (!quote) return <Row label={label} value="unavailable" />;
  return <Row label={label} value={`${fmtSigned(quote.pct_change)}%`} color={directionColor(quote.pct_change)} />;
}

function LiveCuesPanel({ brief, liveGift, liveScore, giftUpdatedAt, giftRefreshing }) {
  const c = brief?.components || {};
  const usQuotes = liveScore?.us_quotes || c.us_quotes;
  const asiaQuotes = liveScore?.asia_quotes || c.asia_quotes;
  // Prefer the live scrape; fall back to the morning-brief snapshot so the
  // panel never goes blank between polls or if the live endpoint is down.
  const giftPrice = liveGift?.price ?? liveScore?.gift?.price ?? c.gift?.price;
  const giftGap = liveGift?.gap_pct ?? liveScore?.gift?.gap_pct ?? c.gift?.gap_pct;
  const giftChange = liveGift?.change ?? liveScore?.gift?.change;
  const live = !!(liveGift?.available || liveScore?.gift?.available) && giftPrice != null;
  return (
    <Panel title="Live cues">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 4, gap: 8 }}>
        <div style={{ fontFamily: DISP, fontSize: 11, color: T.muted, letterSpacing: 0.4 }}>
          GIFT Nifty {live ? <Chip color={T.cyan}>live</Chip> : <Chip color={T.amber}>brief</Chip>}
          {giftRefreshing && <span style={{ marginLeft: 6, color: T.muted }}>…</span>}
        </div>
        {giftUpdatedAt && (
          <div style={{ fontFamily: MONO, fontSize: 10, color: T.muted }}>
            {giftUpdatedAt.toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", second: "2-digit" })} IST
          </div>
        )}
      </div>
      <Row
        label="GIFT Nifty"
        value={giftPrice != null ? fmtNum(giftPrice, 1) : "unavailable"}
        color={giftChange != null ? directionColor(giftChange) : undefined}
      />
      {giftChange != null && (
        <Row label="GIFT change" value={fmtSigned(giftChange, 1)} color={directionColor(giftChange)} />
      )}
      <Row
        label="GIFT gap vs fair value"
        value={giftGap != null ? `${fmtSigned(giftGap)}%` : "—"}
        color={directionColor(giftGap)}
      />
      <div style={{ height: 10 }} />
      {Object.entries(US_LABELS).map(([k, label]) => (
        <QuoteRow key={k} label={label} quote={usQuotes?.[k]} />
      ))}
      <div style={{ height: 10 }} />
      {Object.entries(ASIA_LABELS).map(([k, label]) => (
        <QuoteRow key={k} label={label} quote={asiaQuotes?.[k]} />
      ))}
    </Panel>
  );
}

/* ---------- macro ---------- */
const MACRO_LABELS = { crude: "Crude (Brent)", wti: "WTI", usdinr: "USD/INR", dxy: "DXY", us10y: "US 10Y" };

function MacroPanel({ brief, liveScore }) {
  const c = brief?.components || {};
  const flags = (liveScore?.components?.macro?.flags) || c.macro?.flags || {};
  const quotes = liveScore?.macro_quotes || c.macro_quotes || {};
  const keys = Object.keys(MACRO_LABELS).filter((k) => quotes[k] || flags[k] !== undefined);
  if (!keys.length) return <Panel title="Macro"><EmptyNote>No macro data yet.</EmptyNote></Panel>;
  return (
    <Panel title="Macro">
      {Object.entries(MACRO_LABELS).map(([k, label]) => {
        const q = quotes[k];
        const flagScore = flags[k]; // sign-adjusted for Nifty impact — positive = bullish, negative = bearish
        return (
          <Row
            key={k}
            label={label}
            value={q ? `${fmtSigned(q.pct_change)}%` : "unavailable"}
            color={flagScore !== undefined ? (flagScore >= 0 ? T.put : T.call) : undefined}
          />
        );
      })}
      <EmptyNote>Color reflects each metric's estimated impact on Nifty (green = bullish, red = bearish), not just raw direction.</EmptyNote>
    </Panel>
  );
}

/* ---------- positioning ---------- */
// An auto-fit Y domain makes even a genuinely "flat" move (below the
// server's own TREND_FLAT_THRESHOLD_PCT_POINTS, currently 1pt — see
// positioning.py) render as a dramatic full-height plunge, since Recharts
// stretches whatever range it's given to fill the chart height. That's
// misleading right next to a "Trend: flat" chip. Padding the domain to a
// floor keeps small real-world noise visually flat; a move big enough to
// actually matter still shows a real slope.
const SPARKLINE_MIN_SPAN_PCT_POINTS = 10;

function FiiSparkline({ rows }) {
  const data = useMemo(() => {
    return rows
      .filter((r) => r.future_index_long != null && r.future_index_short != null && (r.future_index_long + r.future_index_short))
      .map((r) => ({
        date: r.trade_date,
        ratio: Math.round((r.future_index_long / (r.future_index_long + r.future_index_short)) * 1000) / 10,
      }))
      .sort((a, b) => (a.date < b.date ? -1 : 1));
  }, [rows]);

  const domain = useMemo(() => {
    if (data.length === 0) return [0, 100];
    const values = data.map((d) => d.ratio);
    const dataMin = Math.min(...values), dataMax = Math.max(...values);
    const center = (dataMin + dataMax) / 2;
    const halfSpan = Math.max((dataMax - dataMin) / 2, SPARKLINE_MIN_SPAN_PCT_POINTS / 2);
    return [Math.max(0, center - halfSpan), Math.min(100, center + halfSpan)];
  }, [data]);

  if (data.length < 2) return <EmptyNote>Not enough participant OI history yet for a trend line.</EmptyNote>;
  const last = data[data.length - 1];

  return (
    <div style={{ height: 90 }}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 6, right: 6, bottom: 0, left: 6 }}>
          <CartesianGrid stroke={T.line} strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="date" hide />
          <YAxis domain={domain} hide />
          <Tooltip
            contentStyle={{ background: T.panel2, border: `1px solid ${T.line}`, borderRadius: 8, fontFamily: MONO, fontSize: 12 }}
            labelStyle={{ color: T.muted }}
            formatter={(v) => [`${v}%`, "FII long/short"]}
          />
          <Line type="monotone" dataKey="ratio" stroke={T.cyan} strokeWidth={2} dot={false} isAnimationActive={false} />
          <ReferenceDot x={last.date} y={last.ratio} r={4} fill={T.cyan} stroke={T.panel} strokeWidth={2} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function PositioningPanel({ brief, fiiRows, livePositioning }) {
  const c = brief?.components || {};
  const fii = livePositioning?.fii || c.fii;
  const opt = c.option_snapshot || {};
  const rows = (livePositioning?.fii_rows && livePositioning.fii_rows.length)
    ? livePositioning.fii_rows
    : (fiiRows || []);
  return (
    <Panel title="Positioning">
      <Row label="FII long/short ratio" value={fii?.ratio != null ? `${fmtNum(fii.ratio, 1)}%` : "unavailable"}
        color={fii?.ratio != null ? directionColor(fii.ratio - 50) : undefined} />
      {fii?.trend && (
        <div style={{ padding: "6px 0" }}>
          <Chip color={TREND_COLOR[fii.trend] || T.muted}>Trend: {fii.trend}</Chip>
        </div>
      )}
      <FiiSparkline rows={rows} />
      <div style={{ height: 10 }} />
      <Row label="PCR" value={opt.pcr != null ? fmtNum(opt.pcr, 2) : "unavailable"} />
      <Row label="Max pain" value={opt.max_pain != null ? fmtNum(opt.max_pain, 0) : "unavailable"} />
      <Row label="Max call OI strike" value={opt.max_call_oi_strike != null ? fmtNum(opt.max_call_oi_strike, 0) : "unavailable"} />
      <Row label="Max put OI strike" value={opt.max_put_oi_strike != null ? fmtNum(opt.max_put_oi_strike, 0) : "unavailable"} />
    </Panel>
  );
}

/* ---------- participant OI (Client/DII/FII/Pro) ---------- */
const PARTICIPANT_ORDER = ["FII", "DII", "Pro", "Client"];
const BIAS_COLOR = { bullish: T.put, bearish: T.call, neutral: T.amber };
// How much each participant type matters for the Nifty open-bias score.
// Only FII feeds scoring.compute_score (WEIGHTS.fii = 15); the rest are context.
const NIFTY_PARTICIPANT_WEIGHT = {
  FII: { pct: 15, label: "in Nifty score", scores: true },
  DII: { pct: 0, label: "context", scores: false },
  Pro: { pct: 0, label: "context", scores: false },
  Client: { pct: 0, label: "context", scores: false },
};

function participantLean(ratio) {
  if (ratio == null) return { text: "—", color: T.muted };
  if (ratio >= 55) return { text: "LONG", color: T.put };
  if (ratio <= 45) return { text: "SHORT", color: T.call };
  return { text: "FLAT", color: T.amber };
}

function cashNetLabel(buy, sell) {
  if (buy == null || sell == null) return null;
  const net = buy - sell;
  if (Math.abs(net) < 1e-9) return { text: "flat", color: T.muted, net: 0 };
  return {
    text: `${net > 0 ? "+" : ""}${fmtNum(net, 0)}`,
    color: directionColor(net),
    net,
  };
}

function simplePositioningBias(participants, fii) {
  const ratio = fii?.ratio ?? participants?.FII?.ratio;
  const trend = fii?.trend ?? participants?.FII?.trend;
  if (ratio == null) return { bias: "neutral", title: "No OI yet", line: "Waiting on NSE participant file." };
  if (ratio >= 55 && trend === "rising") {
    return { bias: "bullish", title: "Bullish for Nifty", line: "FII futures net long & rising." };
  }
  if (ratio >= 55) {
    return { bias: "bullish", title: "Mildly bullish", line: "FII futures net long." };
  }
  if (ratio <= 45 && trend === "falling") {
    return { bias: "bearish", title: "Bearish for Nifty", line: "FII futures net short & falling." };
  }
  if (ratio <= 45) {
    return { bias: "bearish", title: "Mildly bearish", line: "FII futures net short." };
  }
  return { bias: "neutral", title: "Neutral for Nifty", line: "FII futures roughly balanced." };
}

function ParticipantPanel({ brief, livePositioning, liveScore, positioningUpdatedAt, positioningRefreshing }) {
  const c = brief?.components || {};
  const participants = (livePositioning?.participants && Object.keys(livePositioning.participants).length)
    ? livePositioning.participants
    : (c.participants || {});
  const asOf = livePositioning?.trade_date || c.participants_trade_date;
  const cash = livePositioning?.fii_dii_cash || c.fii_dii_cash;
  const fii = livePositioning?.fii || liveScore?.components?.fii || c.fii || participants.FII;
  const haveAny = Object.keys(participants).length > 0;
  const fromNse = !!(livePositioning && livePositioning.from_nse);
  const summary = simplePositioningBias(participants, fii);
  const biasColor = BIAS_COLOR[summary.bias] || T.muted;

  // FII's contribution to the live/open score (−15…+15 pts of the −100…+100 dial).
  const fiiComp = liveScore?.components?.fii || c.fii;
  const fiiScoreUnit = fiiComp?.score; // −1…+1
  const fiiPoints = fiiScoreUnit != null ? Math.round(fiiScoreUnit * 15) : null;

  const fiiCash = cashNetLabel(cash?.fii_buy, cash?.fii_sell);
  const diiCash = cashNetLabel(cash?.dii_buy, cash?.dii_sell);

  return (
    <Panel
      title="Participant OI · Nifty"
      right={
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", justifyContent: "flex-end" }}>
          {fromNse ? <Chip color={T.cyan}>NSE</Chip> : null}
          {positioningRefreshing && <span style={{ fontFamily: MONO, fontSize: 10, color: T.muted }}>…</span>}
          {asOf && <span style={{ fontFamily: MONO, fontSize: 11, color: T.muted }}>{asOf}</span>}
        </div>
      }
    >
      {/* One-glance Nifty read */}
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12,
        marginBottom: 14, padding: "12px 14px", borderRadius: 10,
        background: `${biasColor}14`, border: `1px solid ${biasColor}44`,
      }}>
        <div>
          <div style={{ fontFamily: DISP, fontSize: 16, fontWeight: 700, color: biasColor }}>{summary.title}</div>
          <div style={{ fontFamily: DISP, fontSize: 12, color: T.fg, marginTop: 2 }}>{summary.line}</div>
        </div>
        <div style={{ textAlign: "right", flexShrink: 0 }}>
          <div style={{ fontFamily: MONO, fontSize: 11, color: T.muted }}>FII weight</div>
          <div style={{ fontFamily: MONO, fontSize: 18, fontWeight: 700, color: T.cyan }}>15%</div>
          <div style={{ fontFamily: DISP, fontSize: 10, color: T.muted }}>of Nifty score</div>
        </div>
      </div>

      {/* Score contribution bar */}
      <div style={{ marginBottom: 14 }}>
        <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
          <span style={{ fontFamily: DISP, fontSize: 11, color: T.muted }}>Nifty open score mix</span>
          {fiiPoints != null && (
            <span style={{ fontFamily: MONO, fontSize: 11, color: directionColor(fiiPoints), fontWeight: 700 }}>
              FII now {fiiPoints >= 0 ? "+" : ""}{fiiPoints} pts
            </span>
          )}
        </div>
        <div style={{ display: "flex", height: 10, borderRadius: 6, overflow: "hidden", border: `1px solid ${T.line}` }}>
          <div title="GIFT 40%" style={{ width: "40%", background: `${T.cyan}99` }} />
          <div title="US/Asia 20%" style={{ width: "20%", background: `${T.amber}88` }} />
          <div title="Macro 15%" style={{ width: "15%", background: `${T.muted}66` }} />
          <div title="FII OI 15%" style={{ width: "15%", background: biasColor }} />
        </div>
        <div style={{ display: "flex", gap: 10, marginTop: 6, flexWrap: "wrap", fontFamily: DISP, fontSize: 10, color: T.muted }}>
          <span><span style={{ color: T.cyan }}>■</span> GIFT 40%</span>
          <span><span style={{ color: T.amber }}>■</span> US/Asia 20%</span>
          <span><span style={{ color: T.muted }}>■</span> Macro 15%</span>
          <span><span style={{ color: biasColor }}>■</span> FII OI 15%</span>
        </div>
      </div>

      {!haveAny ? (
        <EmptyNote>No participant OI yet — refreshing from NSE.</EmptyNote>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {PARTICIPANT_ORDER.filter((p) => participants[p]).map((p) => {
            const row = participants[p];
            const lean = participantLean(row.ratio);
            const wt = NIFTY_PARTICIPANT_WEIGHT[p];
            return (
              <div key={p} style={{
                display: "grid",
                gridTemplateColumns: "minmax(48px,auto) minmax(64px,auto) 1fr auto",
                alignItems: "center", gap: 8,
                padding: "8px 10px", borderRadius: 8,
                background: p === "FII" ? `${biasColor}10` : T.panel2,
                border: `1px solid ${p === "FII" ? biasColor + "44" : T.line}`,
              }}>
                <span style={{ fontFamily: DISP, fontSize: 13, fontWeight: 700, color: T.fg }}>{p}</span>
                <span style={{ fontFamily: MONO, fontSize: 12, fontWeight: 700, color: lean.color }}>{lean.text}</span>
                <span style={{ fontFamily: MONO, fontSize: 12, color: T.fg }}>
                  {row.ratio != null ? `${fmtNum(row.ratio, 0)}%` : "—"}
                  {row.trend ? <span style={{ color: TREND_COLOR[row.trend] || T.muted, marginLeft: 8 }}>{row.trend}</span> : null}
                </span>
                <span style={{ fontFamily: MONO, fontSize: 10, color: wt.scores ? T.cyan : T.muted, fontWeight: wt.scores ? 700 : 400 }}>
                  {wt.scores ? `${wt.pct}%` : wt.label}
                </span>
              </div>
            );
          })}
        </div>
      )}

      {(fiiCash || diiCash) && (
        <div style={{ display: "flex", gap: 16, marginTop: 12, flexWrap: "wrap" }}>
          {fiiCash && (
            <div style={{ fontFamily: DISP, fontSize: 12, color: T.muted }}>
              FII cash <span style={{ fontFamily: MONO, fontWeight: 700, color: fiiCash.color }}>{fiiCash.text}</span>
            </div>
          )}
          {diiCash && (
            <div style={{ fontFamily: DISP, fontSize: 12, color: T.muted }}>
              DII cash <span style={{ fontFamily: MONO, fontWeight: 700, color: diiCash.color }}>{diiCash.text}</span>
            </div>
          )}
        </div>
      )}

      {positioningUpdatedAt && (
        <div style={{ fontFamily: MONO, fontSize: 10, color: T.muted, marginTop: 10 }}>
          Updated {positioningUpdatedAt.toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit" })} IST · EOD NSE file
        </div>
      )}
    </Panel>
  );
}

/* ---------- top-10 Nifty movers (live, weighted-contribution predictor) ----------
   Reads api/index.py's /api/upstox/movers — same origin, the PCR tracker
   app, not this engine — every MOVERS_POLL_MS while mounted, same
   cross-app pattern PaperTradingPanel's fetchChain() already uses for the
   live option chain below. Every fresh, connected reading is throttled
   client-side to at most one POST /movers/snapshot (this engine's own
   endpoint) every MOVERS_SNAPSHOT_THROTTLE_MS, building the history
   movers/accuracy scores against — there's no server-side job for this
   the way there is for the morning brief, since the signal is only
   meaningful live during market hours.

   Separately, /api/upstox/movers/history feeds the per-stock intraday area
   charts below — one Upstox request per stock server-side (no batch
   history endpoint exists), so it's polled far less often than the live
   quote endpoint. */
const MOVERS_POLL_MS = 5000;
const MOVERS_SNAPSHOT_THROTTLE_MS = 5 * 60 * 1000;
const MOVERS_HISTORY_POLL_MS = 60000;
// Shared between the sparkline tiles' background poll and the modal's
// interval picker default -- keeping this in one place means the modal's
// "already have this one, no fetch needed" shortcut can't drift out of
// sync with whatever interval the panel is actually polling at (it did,
// briefly: both were hardcoded to "30minute" separately until this).
const MOVERS_HISTORY_DEFAULT_INTERVAL = "5minute";
// Threshold for the *current 5-minute window's* implied move (see
// implied5MinPoints in MoversPanel), not the day-cumulative implied move --
// naturally smaller than a full-day figure, so this default is lower than
// it would be for that. Chosen to sit below the backtest panel's 50pt
// "big event" threshold (this alert is meant to catch ordinary single-bar
// moves, not just the rare large ones already validated at ~85% direction
// accuracy there).
const MOVE_ALERT_DEFAULT_THRESHOLD_PTS = 20;

// Non-blocking toast (not a modal -- it shouldn't interrupt whatever the
// user's doing) shown when MoversPanel's implied5MinPoints -- the top-10
// weighted move within the *current 5-minute window* (not the day-
// cumulative implied_points from api/index.py) -- crosses alertThresholdPts.
// Deliberately scoped to one bar's move, matching what the backtest panel
// actually measures (single 5-min-bar moves), rather than the day-open-to-
// now figure shown elsewhere on this panel. Fires once per *new* direction,
// not on every 5s poll tick the condition happens to still hold -- see
// MoversPanel's lastAlertDirectionRef for the de-dup logic.
function MoveAlertToast({ alert, onDismiss }) {
  if (!alert) return null;
  const color = alert.direction === "up" ? T.put : T.call;
  return (
    <div
      style={{
        position: "fixed", top: 16, right: 16, zIndex: 2000, width: 320,
        background: T.panel, border: `1px solid ${color}`, borderRadius: 10, padding: "14px 16px",
        boxShadow: "0 10px 30px rgba(0,0,0,0.5)",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 10 }}>
        <div>
          <div style={{ fontFamily: DISP, fontSize: 13, fontWeight: 700, color }}>
            {alert.direction === "up" ? "📈" : "📉"} Possible ≥{alert.thresholdPts}pt {alert.direction === "up" ? "rise" : "fall"} — this 5-min window
          </div>
          <div style={{ fontFamily: MONO, fontSize: 13, color: T.fg, marginTop: 6 }}>
            Implied ~{fmtSigned(alert.impliedPts, 0)} pts ({fmtSigned(alert.impliedPct, 3)}%) from top-10 weighted signal, since this bar opened
          </div>
          <div style={{ fontFamily: DISP, fontSize: 10, color: T.muted, marginTop: 8, lineHeight: 1.4 }}>
            Live pattern, not a guarantee — same weighted-contribution formula the backtest panel below scored ~85% directionally accurate on single 5-min-bar moves.
          </div>
        </div>
        <button
          onClick={onDismiss} aria-label="Dismiss"
          style={{ background: "transparent", border: "none", color: T.muted, cursor: "pointer", fontSize: 14, lineHeight: 1, flexShrink: 0 }}
        >
          ✕
        </button>
      </div>
    </div>
  );
}

function StockAreaChart({ symbol, name, points, pctChange, onClick }) {
  const color = directionColor(pctChange);
  const chartData = useMemo(() => (points || []).map((p) => ({
    t: p.t, close: p.close,
    label: new Date(p.t).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit" }),
  })), [points]);

  return (
    <div
      onClick={onClick}
      title={`Click for ${name}'s full chart`}
      style={{ background: T.panel2, border: `1px solid ${T.line}`, borderRadius: 8, padding: "8px 10px", cursor: onClick ? "pointer" : "default" }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 4 }}>
        <span style={{ fontFamily: DISP, fontSize: 11, fontWeight: 700, color: T.fg }}>{symbol}</span>
        <span style={{ fontFamily: MONO, fontSize: 11, color }}>{pctChange != null ? `${fmtSigned(pctChange, 2)}%` : "—"}</span>
      </div>
      {chartData.length < 2 ? (
        <div style={{ height: 60, display: "flex", alignItems: "center", fontFamily: DISP, fontSize: 10, color: T.muted }}>
          No intraday history yet
        </div>
      ) : (
        <div style={{ height: 60 }}>
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={chartData} margin={{ top: 2, right: 2, bottom: 0, left: 2 }}>
              <defs>
                <linearGradient id={`movers-area-${symbol}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={color} stopOpacity={0.35} />
                  <stop offset="100%" stopColor={color} stopOpacity={0} />
                </linearGradient>
              </defs>
              <XAxis dataKey="label" hide />
              <YAxis domain={["dataMin", "dataMax"]} hide />
              <Tooltip
                contentStyle={{ background: T.panel2, border: `1px solid ${T.line}`, borderRadius: 8, fontFamily: MONO, fontSize: 11 }}
                labelStyle={{ color: T.muted }}
                formatter={(v) => [fmtNum(v, 2), name]}
              />
              <Area type="monotone" dataKey="close" stroke={color} strokeWidth={1.5} fill={`url(#movers-area-${symbol})`} isAnimationActive={false} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}

// Matches api/index.py's UPSTOX_INTRADAY_INTERVALS keys exactly -- the
// backend rejects anything else with a 400-shaped {error} response.
const HISTORY_INTERVALS = [
  { value: "5minute", label: "5m" },
  { value: "15minute", label: "15m" },
  { value: "30minute", label: "30m" },
];

// Real candlestick + volume chart via TradingView's own open-source
// "Lightweight Charts" library (Apache-2.0, not the paid/licensed
// Charting Library that powers Upstox Pro Web -- that one requires a
// business application to TradingView plus a private datafeed, neither
// obtainable here; this is the closest honest equivalent: TradingView's
// real charting engine and visual language, driven by this app's own
// real Upstox OHLCV data). Recreates the chart fresh per symbol (cheap,
// avoids carrying stale series state across stocks) and swaps just the
// price series (candlestick vs area) when chartType changes, since
// lightweight-charts models those as distinct series types on the same
// chart instance rather than one interchangeable series.
function LightweightChart({ symbol, points, chartType }) {
  const containerRef = useRef(null);
  const chartRef = useRef(null);
  const seriesRef = useRef(null);
  const volumeSeriesRef = useRef(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      layout: { background: { color: T.panel }, textColor: T.muted, fontFamily: MONO },
      grid: { vertLines: { color: T.line }, horzLines: { color: T.line } },
      crosshair: { mode: CrosshairMode.Normal },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: T.line },
      rightPriceScale: { borderColor: T.line },
      autoSize: true,
    });
    chartRef.current = chart;

    const volumeSeries = chart.addHistogramSeries({
      priceFormat: { type: "volume" }, priceScaleId: "volume", lastValueVisible: false, priceLineVisible: false,
    });
    chart.priceScale("volume").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    volumeSeriesRef.current = volumeSeries;

    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      volumeSeriesRef.current = null;
    };
  }, [symbol]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    if (seriesRef.current) chart.removeSeries(seriesRef.current);
    seriesRef.current = chartType === "candles"
      ? chart.addCandlestickSeries({
          upColor: T.put, downColor: T.call, borderVisible: false, wickUpColor: T.put, wickDownColor: T.call,
        })
      : chart.addAreaSeries({ lineColor: T.cyan, topColor: `${T.cyan}55`, bottomColor: `${T.cyan}00`, lineWidth: 2 });
  }, [chartType]);

  useEffect(() => {
    if (!seriesRef.current) return;
    const sorted = [...(points || [])]
      .map((p) => ({ ...p, time: Math.floor(new Date(p.t).getTime() / 1000) }))
      .sort((a, b) => a.time - b.time);

    seriesRef.current.setData(chartType === "candles"
      ? sorted.map((p) => ({ time: p.time, open: p.open, high: p.high, low: p.low, close: p.close }))
      : sorted.map((p) => ({ time: p.time, value: p.close })));

    volumeSeriesRef.current?.setData(
      sorted.filter((p) => p.volume != null)
        .map((p) => ({ time: p.time, value: p.volume, color: p.close >= p.open ? `${T.put}66` : `${T.call}66` })),
    );
    chartRef.current?.timeScale().fitContent();
  }, [points, chartType]);

  return <div ref={containerRef} style={{ width: "100%", height: "100%" }} />;
}

// Expanded single-stock chart, opened by clicking a tile or table row in
// MoversPanel below. Starts from whatever history/quote data the panel
// already has in state (same MOVERS_HISTORY_DEFAULT_INTERVAL series
// backing the small sparkline, passed in as `points`) so the chart is
// visible instantly with no fetch
// on open. Switching the interval picker below re-fetches just this one
// stock at the new granularity via /api/upstox/stock/history -- lighter
// than the panel's own batch endpoint, which always pulls all 10. Area vs.
// candlestick is purely a client-side rendering choice off whichever
// points are currently loaded, toggled independently of interval.
//
// Note: an actual TradingView widget embed was tried here first and
// reverted -- verified live that their free public "Advanced Chart"
// widget doesn't carry real-time NSE (India) data without the viewer
// being logged into their own TradingView account or a paid data-
// licensing agreement on TradingView's side; it either showed "This
// symbol is only available on TradingView" or silently substituted an
// unrelated US symbol (Apple Inc). LightweightChart above is the
// practical middle ground: TradingView's own real charting engine, real
// Upstox data.
function StockDetailModal({ symbol, name, ltp, pctChange, points, onClose }) {
  const [chartType, setChartType] = useState("area");
  const [interval, setIntervalValue] = useState(MOVERS_HISTORY_DEFAULT_INTERVAL);
  const [ownPoints, setOwnPoints] = useState(points);
  const [loadingPoints, setLoadingPoints] = useState(false);
  const color = directionColor(pctChange);

  const selectInterval = useCallback(async (value) => {
    setIntervalValue(value);
    if (value === MOVERS_HISTORY_DEFAULT_INTERVAL) {
      setOwnPoints(points); // already have this one from the panel -- no fetch needed
      return;
    }
    setLoadingPoints(true);
    try {
      const res = await fetch(`${PCR_API_BASE}/upstox/stock/history?symbol=${symbol}&interval=${value}`);
      const json = await res.json();
      setOwnPoints(json.points || []);
    } catch {
      setOwnPoints([]);
    } finally {
      setLoadingPoints(false);
    }
  }, [symbol, points]);

  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed", inset: 0, background: "rgba(10,15,30,0.75)", zIndex: 1000,
        display: "flex", alignItems: "center", justifyContent: "center", padding: 16,
        paddingBottom: "max(16px, env(safe-area-inset-bottom))",
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: T.panel, border: `1px solid ${T.line}`, borderRadius: 12, padding: 20,
          width: "100%", maxWidth: 900, maxHeight: "85vh", height: "min(85vh, 900px)", display: "flex", flexDirection: "column",
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 12, flexShrink: 0 }}>
          <div>
            <div style={{ fontFamily: DISP, fontSize: 18, fontWeight: 700, color: T.fg }}>{name}</div>
            <div style={{ fontFamily: MONO, fontSize: 12, color: T.muted }}>{symbol}</div>
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            style={{
              width: 30, height: 30, borderRadius: "50%", background: T.panel2, color: T.muted,
              border: `1px solid ${T.line}`, cursor: "pointer", fontSize: 14, lineHeight: 1, flexShrink: 0,
            }}
          >
            ✕
          </button>
        </div>

        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 10, flexWrap: "wrap", gap: 10, flexShrink: 0 }}>
          <div style={{ display: "flex", alignItems: "baseline", gap: 12 }}>
            <div style={{ fontFamily: MONO, fontSize: 26, fontWeight: 700, color: T.fg }}>{ltp != null ? fmtNum(ltp, 2) : "—"}</div>
            <div style={{ fontFamily: MONO, fontSize: 14, color }}>{pctChange != null ? `${fmtSigned(pctChange, 2)}%` : "—"}</div>
          </div>
          <div style={{ display: "flex", gap: 6 }}>
            <button onClick={() => setChartType("area")} style={formButtonStyle(chartType === "area", false)}>Area</button>
            <button onClick={() => setChartType("candles")} style={formButtonStyle(chartType === "candles", false)}>Candles</button>
          </div>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 12, flexShrink: 0 }}>
          <span style={{ fontFamily: DISP, fontSize: 10, color: T.muted, textTransform: "uppercase", letterSpacing: 0.4, marginRight: 2 }}>Interval</span>
          {HISTORY_INTERVALS.map((iv) => (
            <button
              key={iv.value} onClick={() => selectInterval(iv.value)}
              disabled={loadingPoints} style={formButtonStyle(interval === iv.value, loadingPoints)}
            >
              {iv.label}
            </button>
          ))}
          {loadingPoints && <span style={{ fontFamily: DISP, fontSize: 11, color: T.muted, marginLeft: 4 }}>Loading…</span>}
        </div>

        {(ownPoints || []).length < 2 ? (
          <EmptyNote>{loadingPoints ? "Loading…" : "No chart data available for this stock yet."}</EmptyNote>
        ) : (
          <div style={{ flex: 1, minHeight: 0 }}>
            <LightweightChart symbol={symbol} points={ownPoints} chartType={chartType} />
          </div>
        )}

        <div style={{ marginTop: 10, fontFamily: DISP, fontSize: 11, color: T.muted, flexShrink: 0 }}>
          Today's intraday path when the market's live; falls back to the last ~10 days of daily closes outside trading hours.
        </div>
      </div>
    </div>
  );
}

function MoversPanel() {
  const [data, setData] = useState(null); // { connected, stocks, implied_move_pct, implied_points, verdict, error }
  const [history, setHistory] = useState(null); // { connected, series: { SYMBOL: [{t, close}, ...] } }
  const [accuracy, setAccuracy] = useState(null);
  const [loading, setLoading] = useState(true);
  const [selectedSymbol, setSelectedSymbol] = useState(null);
  const [alertThresholdPts, setAlertThresholdPts] = useState(MOVE_ALERT_DEFAULT_THRESHOLD_PTS);
  const [activeAlert, setActiveAlert] = useState(null);
  const fetchInFlight = useRef(false);
  const historyFetchInFlight = useRef(false);
  const lastSnapshotAt = useRef(0);
  // Tracks which direction (if any) the alert already fired for, so the
  // toast reappears only on a *new* crossing (e.g. clear -> up, or
  // up -> down) rather than every 5s poll tick the condition still holds.
  const lastAlertDirectionRef = useRef(null);

  const loadAccuracy = useCallback(async () => {
    try {
      setAccuracy(await getJSON("/movers/accuracy?days=30"));
    } catch {
      // background refresh -- accuracy is a nice-to-have, not worth an error banner
    }
  }, []);

  useEffect(() => { loadAccuracy(); }, [loadAccuracy]);

  useEffect(() => {
    const tick = async () => {
      if (fetchInFlight.current) return;
      fetchInFlight.current = true;
      try {
        const res = await fetch(`${PCR_API_BASE}/upstox/movers`);
        const json = await res.json();
        setData(json);

        const now = Date.now();
        if (json.connected && json.implied_move_pct != null && now - lastSnapshotAt.current > MOVERS_SNAPSHOT_THROTTLE_MS) {
          lastSnapshotAt.current = now;
          postJSON("/movers/snapshot", {
            implied_move_pct: json.implied_move_pct, verdict: json.verdict, stocks: json.stocks,
          }).then(loadAccuracy).catch(() => {});
        }
      } catch {
        // background tick -- not worth surfacing an error banner for
      } finally {
        setLoading(false);
        fetchInFlight.current = false;
      }
    };
    tick();
    const id = setInterval(tick, MOVERS_POLL_MS);
    return () => clearInterval(id);
  }, [loadAccuracy]);

  useEffect(() => {
    const tick = async () => {
      if (historyFetchInFlight.current) return;
      historyFetchInFlight.current = true;
      try {
        const res = await fetch(`${PCR_API_BASE}/upstox/movers/history?interval=${MOVERS_HISTORY_DEFAULT_INTERVAL}`);
        setHistory(await res.json());
      } catch {
        // background tick -- charts just stay empty for this cycle
      } finally {
        historyFetchInFlight.current = false;
      }
    };
    tick();
    const id = setInterval(tick, MOVERS_HISTORY_POLL_MS);
    return () => clearInterval(id);
  }, []);

  // "Right now, in the last 5 minutes" -- NOT the same thing as
  // data.implied_points (which measures cumulative move since the
  // previous day's close, i.e. "how the day has gone so far"). This is
  // the metric that actually matches what the backtest panel validates:
  // each stock's %change over one 5-min bar, weight-summed. The anchor is
  // the *open* of the freshest 5-min candle in `history` (last polled up
  // to MOVERS_HISTORY_POLL_MS ago, and Upstox includes the still-forming
  // candle so that open is a stable "start of this window" price) against
  // the *current* LTP from `data` (refreshed every MOVERS_POLL_MS) -- so
  // this updates every 5s even though the candle data itself only refreshes
  // once a minute.
  const implied5MinPct = useMemo(() => {
    if (!data?.stocks || !history?.series) return null;
    let total = 0;
    let any = false;
    for (const s of data.stocks) {
      const candles = history.series[s.symbol];
      const windowOpen = candles?.[candles.length - 1]?.open;
      if (!windowOpen || s.ltp == null) continue;
      total += (s.ltp - windowOpen) / windowOpen * 100 * s.weight_pct / 100;
      any = true;
    }
    return any ? total : null;
  }, [data, history]);

  const implied5MinPoints = useMemo(() => (
    implied5MinPct != null && data?.nifty_spot ? implied5MinPct / 100 * data.nifty_spot : null
  ), [implied5MinPct, data?.nifty_spot]);

  useEffect(() => {
    if (implied5MinPoints == null) return;
    const direction = implied5MinPoints >= alertThresholdPts ? "up"
      : implied5MinPoints <= -alertThresholdPts ? "down" : null;
    if (direction && direction !== lastAlertDirectionRef.current) {
      lastAlertDirectionRef.current = direction;
      setActiveAlert({ direction, impliedPts: implied5MinPoints, impliedPct: implied5MinPct, thresholdPts: alertThresholdPts });
    } else if (!direction) {
      lastAlertDirectionRef.current = null; // condition cleared -- ready to fire again if it returns
    }
  }, [implied5MinPoints, implied5MinPct, alertThresholdPts]);

  const chartData = useMemo(() => {
    return (data?.stocks || [])
      .filter((s) => s.pct_change != null)
      .map((s) => ({ symbol: s.symbol, pct_change: s.pct_change, contribution_pct: s.contribution_pct }))
      .sort((a, b) => b.pct_change - a.pct_change);
  }, [data]);

  const totalWeight = useMemo(() => (data?.stocks || []).reduce((sum, s) => sum + (s.weight_pct || 0), 0), [data]);

  return (
    <div style={{ gridColumn: "1 / -1" }}>
      <Panel
        title="Top 10 Nifty movers (live)"
        right={
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            {accuracy?.hit_rate_pct != null && (
              <span title="Rolling hit-rate: this signal's predicted direction vs Nifty's actual next-day open, last 30 sessions">
                <Chip color={T.cyan}>Predictor accuracy: {fmtNum(accuracy.hit_rate_pct, 1)}%</Chip>
              </span>
            )}
            <label
              title="Pop up a live alert whenever the current 5-minute window's implied move crosses this many Nifty points, in either direction"
              style={{ fontFamily: DISP, fontSize: 11, color: T.muted, display: "flex", alignItems: "center", gap: 4 }}
            >
              alert ≥ pts / 5m
              <input
                type="number" min="5" max="500" value={alertThresholdPts}
                onChange={(e) => setAlertThresholdPts(Number(e.target.value) || MOVE_ALERT_DEFAULT_THRESHOLD_PTS)}
                style={{ ...formInputStyle, width: 52, padding: "3px 6px" }}
              />
            </label>
            <span
              style={{ fontFamily: DISP, fontSize: 11, fontWeight: 700, color: data?.connected ? T.cyan : T.amber }}
              title={data?.connected ? "Live via your connected Upstox account" : "Upstox not connected — visit /api/upstox/login to see live movers"}
            >
              {data?.connected ? "via Upstox" : "not connected"}
            </span>
          </div>
        }
      >
        {loading ? (
          <EmptyNote>Loading…</EmptyNote>
        ) : !data?.connected ? (
          <EmptyNote>{data?.error || "Upstox not connected — visit /api/upstox/login to see live movers."}</EmptyNote>
        ) : (
          <>
            <div style={{ display: "flex", alignItems: "baseline", gap: 24, marginBottom: 14, flexWrap: "wrap" }}>
              <div>
                <div style={{ fontFamily: MONO, fontSize: 28, fontWeight: 700, color: directionColor(data.implied_move_pct) }}>
                  {fmtSigned(data.implied_move_pct, 2)}%
                  {data.implied_points != null && (
                    <span style={{ fontSize: 16, marginLeft: 8, color: directionColor(data.implied_points) }}>
                      ({fmtSigned(data.implied_points, 0)} pts)
                    </span>
                  )}
                </div>
                <div style={{ fontFamily: DISP, fontSize: 11, color: T.muted }}>implied move since open</div>
              </div>
              <div>
                <div style={{ fontFamily: MONO, fontSize: 22, fontWeight: 700, color: directionColor(implied5MinPoints) }}>
                  {implied5MinPoints != null ? `${fmtSigned(implied5MinPoints, 0)} pts` : "—"}
                  {implied5MinPct != null && (
                    <span style={{ fontSize: 13, marginLeft: 6, color: directionColor(implied5MinPct) }}>
                      ({fmtSigned(implied5MinPct, 3)}%)
                    </span>
                  )}
                </div>
                <div style={{ fontFamily: DISP, fontSize: 11, color: T.muted }} title="This is what the alert popup and the backtest panel both measure — one 5-min bar's move, not the day-cumulative figure to the left">
                  implied move, this 5-min window
                </div>
              </div>
              {data.verdict && (
                <Chip color={VERDICT_COLOR[data.verdict] || T.muted}>{VERDICT_EMOJI[data.verdict]} {data.verdict}</Chip>
              )}
            </div>

            <div style={{ height: 180, marginBottom: 14 }}>
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={chartData} margin={{ top: 6, right: 6, bottom: 0, left: 6 }}>
                  <CartesianGrid stroke={T.line} strokeDasharray="3 3" vertical={false} />
                  <XAxis dataKey="symbol" tick={{ fontFamily: MONO, fontSize: 10, fill: T.muted }} />
                  <YAxis tick={{ fontFamily: MONO, fontSize: 10, fill: T.muted }} tickFormatter={(v) => `${v}%`} />
                  <Tooltip
                    contentStyle={{ background: T.panel2, border: `1px solid ${T.line}`, borderRadius: 8, fontFamily: MONO, fontSize: 12 }}
                    labelStyle={{ color: T.muted }}
                    formatter={(v, name) => [`${fmtSigned(v, 2)}%`, name === "pct_change" ? "change" : "index contribution"]}
                  />
                  <Bar dataKey="pct_change" radius={[3, 3, 0, 0]}>
                    {chartData.map((d) => <Cell key={d.symbol} fill={directionColor(d.pct_change)} />)}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>

            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: MONO, fontSize: 12 }}>
                <thead>
                  <tr style={{ color: T.muted, textAlign: "left" }}>
                    <th style={tradeThStyle}>Stock</th>
                    <th style={tradeThStyle}>Weight</th>
                    <th style={tradeThStyle}>LTP</th>
                    <th style={tradeThStyle}>Change</th>
                    <th style={tradeThStyle}>Contribution</th>
                  </tr>
                </thead>
                <tbody>
                  {(data.stocks || []).map((s) => (
                    <tr
                      key={s.symbol} onClick={() => setSelectedSymbol(s.symbol)}
                      title={`Click for ${s.name}'s full chart`}
                      style={{ borderTop: `1px solid ${T.line}`, cursor: "pointer" }}
                    >
                      <td style={tradeTdStyle}>{s.name}</td>
                      <td style={{ ...tradeTdStyle, color: T.muted }}>{fmtNum(s.weight_pct, 1)}%</td>
                      <td style={tradeTdStyle}>{s.ltp != null ? fmtNum(s.ltp, 2) : "—"}</td>
                      <td style={{ ...tradeTdStyle, color: directionColor(s.pct_change) }}>
                        {s.pct_change != null ? `${fmtSigned(s.pct_change, 2)}%` : "—"}
                      </td>
                      <td style={{ ...tradeTdStyle, color: directionColor(s.contribution_pct) }}>
                        {s.contribution_pct != null ? `${fmtSigned(s.contribution_pct, 3)}pp` : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div style={{ marginTop: 10, fontFamily: DISP, fontSize: 11, color: T.muted }}>
              Implied move = Σ(weight% × %change) across these 10 stocks (~{fmtNum(totalWeight, 0)}% of index weight) — a directional
              signal from the heaviest names, not the actual Nifty change. Automated analysis for information only — not investment advice.
            </div>

            <div style={{ marginTop: 18, fontFamily: DISP, fontSize: 12, fontWeight: 700, color: T.muted, textTransform: "uppercase", letterSpacing: 0.6, marginBottom: 8 }}>
              Price history
            </div>
            <div style={{ fontFamily: DISP, fontSize: 11, color: T.muted, marginBottom: 8 }}>
              Today's intraday path when the market's live; falls back to the last ~10 days of daily closes outside trading hours (weekends/holidays/pre-open).
            </div>
            {!history?.connected ? (
              <EmptyNote>{history?.error || "Loading intraday history…"}</EmptyNote>
            ) : (
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 10 }}>
                {(data.stocks || []).map((s) => (
                  <StockAreaChart
                    key={s.symbol} symbol={s.symbol} name={s.name}
                    points={history.series?.[s.symbol]} pctChange={s.pct_change}
                    onClick={() => setSelectedSymbol(s.symbol)}
                  />
                ))}
              </div>
            )}

            {selectedSymbol && (() => {
              const s = (data.stocks || []).find((row) => row.symbol === selectedSymbol);
              return (
                <StockDetailModal
                  symbol={selectedSymbol} name={s?.name || selectedSymbol}
                  ltp={s?.ltp} pctChange={s?.pct_change}
                  points={history?.series?.[selectedSymbol]}
                  onClose={() => setSelectedSymbol(null)}
                />
              );
            })()}
          </>
        )}
      </Panel>
      <MoveAlertToast alert={activeAlert} onDismiss={() => setActiveAlert(null)} />
    </div>
  );
}

/* ---------- full Nifty 50 board (unweighted market breadth) ----------
   Complements MoversPanel's weighted top-10 predictor with plain price
   action across all ~50 constituents (see api/index.py's NIFTY50_ALL for
   the membership-staleness caveat -- this list is a best-effort snapshot,
   not fetched live, and individual stale/wrong entries just show as a
   null row rather than breaking the board). Slower poll than the top-10
   panel (50 quotes is more payload, and index breadth doesn't need
   1s-class freshness) but the same in-flight-guard pattern. */
const NIFTY50_POLL_MS = 10000;

function Nifty50Panel() {
  const [data, setData] = useState(null); // { connected, stocks, advances, declines, unchanged, error }
  const [loading, setLoading] = useState(true);
  const fetchInFlight = useRef(false);

  useEffect(() => {
    const tick = async () => {
      if (fetchInFlight.current) return;
      fetchInFlight.current = true;
      try {
        const res = await fetch(`${PCR_API_BASE}/upstox/nifty50`);
        setData(await res.json());
      } catch {
        // background tick -- not worth surfacing an error banner for
      } finally {
        setLoading(false);
        fetchInFlight.current = false;
      }
    };
    tick();
    const id = setInterval(tick, NIFTY50_POLL_MS);
    return () => clearInterval(id);
  }, []);

  const total = (data?.advances || 0) + (data?.declines || 0) + (data?.unchanged || 0);

  return (
    <div style={{ gridColumn: "1 / -1" }}>
      <Panel
        title="Nifty 50 (live)"
        right={
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            {total > 0 && (
              <>
                <Chip color={T.put}>{data.advances} ↑</Chip>
                <Chip color={T.call}>{data.declines} ↓</Chip>
              </>
            )}
            <span
              style={{ fontFamily: DISP, fontSize: 11, fontWeight: 700, color: data?.connected ? T.cyan : T.amber }}
              title={data?.connected ? "Live via your connected Upstox account" : "Upstox not connected — visit /api/upstox/login to see the live board"}
            >
              {data?.connected ? "via Upstox" : "not connected"}
            </span>
          </div>
        }
      >
        {loading ? (
          <EmptyNote>Loading…</EmptyNote>
        ) : !data?.connected ? (
          <EmptyNote>{data?.error || "Upstox not connected — visit /api/upstox/login to see the live Nifty 50 board."}</EmptyNote>
        ) : (
          <>
            <div style={{ maxHeight: 420, overflowY: "auto", overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: MONO, fontSize: 12 }}>
                <thead>
                  <tr style={{ color: T.muted, textAlign: "left", position: "sticky", top: 0, background: T.panel }}>
                    <th style={tradeThStyle}>Stock</th>
                    <th style={tradeThStyle}>LTP</th>
                    <th style={tradeThStyle}>Change</th>
                  </tr>
                </thead>
                <tbody>
                  {(data.stocks || []).map((s) => (
                    <tr key={s.symbol} style={{ borderTop: `1px solid ${T.line}` }}>
                      <td style={tradeTdStyle}>{s.name}</td>
                      <td style={tradeTdStyle}>{s.ltp != null ? fmtNum(s.ltp, 2) : "—"}</td>
                      <td style={{ ...tradeTdStyle, color: directionColor(s.pct_change) }}>
                        {s.pct_change != null ? `${fmtSigned(s.pct_change, 2)}%` : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div style={{ marginTop: 10, fontFamily: DISP, fontSize: 11, color: T.muted }}>
              Sorted by %change, biggest gainers first. Membership/ISINs are a best-effort snapshot (see the endpoint's own docstring) —
              a handful of stale or wrong entries show up as missing rows here rather than breaking the board.
            </div>
          </>
        )}
      </Panel>
    </div>
  );
}

/* ---------- predictive signal: movers + max pain + OI change ----------
   Reads api/index.py's /api/upstox/predictive, which combines three
   things: the top-10 weighted implied move (the only one with backtested
   accuracy -- see BacktestPanel below), max pain, and OI-change bias
   (both derived from the live NIFTY option chain). The backend
   deliberately keeps these three as separate predictive_lines rather
   than blending them into one score -- max pain/OI bias are classic
   heuristics, not backtested, so this panel renders them as supporting
   context under the primary line rather than equal-weight signals. */
const PREDICTIVE_POLL_MS = 15000;

function PredictivePanel() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const fetchInFlight = useRef(false);

  useEffect(() => {
    const tick = async () => {
      if (fetchInFlight.current) return;
      fetchInFlight.current = true;
      try {
        const res = await fetch(`${PCR_API_BASE}/upstox/predictive`);
        setData(await res.json());
      } catch {
        // background tick -- not worth surfacing an error banner for
      } finally {
        setLoading(false);
        fetchInFlight.current = false;
      }
    };
    tick();
    const id = setInterval(tick, PREDICTIVE_POLL_MS);
    return () => clearInterval(id);
  }, []);

  return (
    <div style={{ gridColumn: "1 / -1" }}>
      <Panel
        title="Predictive signal (movers + max pain + OI)"
        right={
          <span
            style={{ fontFamily: DISP, fontSize: 11, fontWeight: 700, color: data?.connected ? T.cyan : T.amber }}
            title={data?.connected ? "Live via your connected Upstox account" : "Upstox not connected — visit /api/upstox/login"}
          >
            {data?.connected ? "via Upstox" : "not connected"}
          </span>
        }
      >
        {loading ? (
          <EmptyNote>Loading…</EmptyNote>
        ) : !data?.connected ? (
          <EmptyNote>{data?.error || "Upstox not connected — visit /api/upstox/login to see the predictive signal."}</EmptyNote>
        ) : (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))", gap: 10, marginBottom: 16 }}>
              <Row label="Spot" value={data.spot != null ? fmtNum(data.spot, 2) : "—"} />
              <Row label="Max pain" value={data.max_pain != null ? fmtNum(data.max_pain, 0) : "—"} />
              <Row
                label="Implied move" color={directionColor(data.implied_points)}
                value={data.implied_points != null ? `${fmtSigned(data.implied_points, 0)} pts` : "—"}
              />
              <Row label="Resistance (OI)" value={data.oi_bias?.resistance_strike != null ? fmtNum(data.oi_bias.resistance_strike, 0) : "—"} />
              <Row label="Support (OI)" value={data.oi_bias?.support_strike != null ? fmtNum(data.oi_bias.support_strike, 0) : "—"} />
            </div>

            {data.verdict && (
              <div style={{ marginBottom: 12 }}>
                <Chip color={VERDICT_COLOR[data.verdict] || T.muted}>{VERDICT_EMOJI[data.verdict]} {data.verdict}</Chip>
              </div>
            )}

            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {(data.predictive_lines || []).map((line, i) => (
                <div key={i} style={{ fontFamily: DISP, fontSize: 13, color: i === 0 ? T.fg : T.muted, lineHeight: 1.5 }}>
                  {i === 0 ? "▸ " : "· "}{line}
                </div>
              ))}
            </div>

            <div style={{ marginTop: 14, fontFamily: DISP, fontSize: 11, color: T.muted }}>
              Only the top line (top-10 weighted implied move) has backtested accuracy behind it (~85% directional, see the
              backtest panel below). Max pain and OI-change bias are classic options heuristics shown as context, not
              independently validated signals. Automated analysis for information only — not investment advice.
            </div>
          </>
        )}
      </Panel>
    </div>
  );
}

/* ---------- 1-minute breakout zones ----------
   Reads api/index.py's /api/upstox/breakout: the range Nifty is coiling
   in on 1-min bars, the levels a break has to clear, and -- same honesty
   mechanism as SetupPanel -- the identical rule's measured follow-through
   rate over several days of real bars against the base rate.

   The strike block is FACTUAL chain data at those price levels, not a
   position recommendation. */
const BREAKOUT_POLL_MS = 15000;

function StrikeRow({ label, data }) {
  if (!data) return null;
  return (
    <tr style={{ borderTop: `1px solid ${T.line}` }}>
      <td style={{ ...tradeTdStyle, color: T.muted }}>{label}</td>
      <td style={tradeTdStyle}>{fmtNum(data.strike, 0)}</td>
      <td style={{ ...tradeTdStyle, color: T.put }}>{data.ce_ltp != null ? fmtNum(data.ce_ltp, 2) : "—"}</td>
      <td style={{ ...tradeTdStyle, color: T.call }}>{data.pe_ltp != null ? fmtNum(data.pe_ltp, 2) : "—"}</td>
    </tr>
  );
}

function BreakoutPanel() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const fetchInFlight = useRef(false);

  useEffect(() => {
    const tick = async () => {
      if (fetchInFlight.current) return;
      fetchInFlight.current = true;
      try {
        const res = await fetch(`${PCR_API_BASE}/upstox/breakout`);
        setData(await res.json());
      } catch {
        // background tick -- not worth surfacing an error banner for
      } finally {
        setLoading(false);
        fetchInFlight.current = false;
      }
    };
    tick();
    const id = setInterval(tick, BREAKOUT_POLL_MS);
    return () => clearInterval(id);
  }, []);

  const live = data?.live;
  const stats = data?.stats;
  const brokeUp = live?.status === "broke_out_up";
  const brokeDown = live?.status === "broke_out_down";
  const statusColor = brokeUp ? T.put : brokeDown ? T.call : T.amber;

  return (
    <div style={{ gridColumn: "1 / -1" }}>
      <Panel
        title="Breakout zone (1-min)"
        right={
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            {stats?.hit_rate_10_pct != null && (
              <span title={`Of the ${stats.fires_evaluated} range-breaks this rule fired over the last few days, this many extended a further 10pts within 15 minutes. Base rate for any random bar: up ${stats.baseline_up_10_pct}% / down ${stats.baseline_down_10_pct}%.`}>
                <Chip color={T.cyan}>Measured: {fmtNum(stats.hit_rate_10_pct, 0)}% extend +10pt ({stats.fires_evaluated} breaks)</Chip>
              </span>
            )}
            <span
              style={{ fontFamily: DISP, fontSize: 11, fontWeight: 700, color: data?.connected ? T.cyan : T.amber }}
              title={data?.connected ? "Live via your connected Upstox account" : "Upstox not connected — visit /api/upstox/login"}
            >
              {data?.connected ? "via Upstox" : "not connected"}
            </span>
          </div>
        }
      >
        {loading ? (
          <EmptyNote>Loading…</EmptyNote>
        ) : !data?.connected ? (
          <EmptyNote>{data?.error || "Upstox not connected — visit /api/upstox/login."}</EmptyNote>
        ) : data?.error ? (
          <EmptyNote>{data.error}</EmptyNote>
        ) : (
          <>
            <div style={{
              display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap", marginBottom: 14,
              padding: "12px 14px", borderRadius: 10,
              background: `${statusColor}14`, border: `1px solid ${statusColor}`,
            }}>
              <div style={{ fontFamily: DISP, fontSize: 15, fontWeight: 700, color: statusColor }}>
                {brokeUp ? "📈 BROKE ABOVE the 1-min range"
                  : brokeDown ? "📉 BROKE BELOW the 1-min range"
                  : `⏳ Consolidating in a ${fmtNum(live?.range_width_pts, 0)}pt range`}
              </div>
            </div>

            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 10, marginBottom: 16 }}>
              <Row label="Spot / last close" value={live?.last_close != null ? fmtNum(live.last_close, 2) : "—"} />
              <Row label="Upside break above" value={live?.range_high != null ? fmtNum(live.range_high, 0) : "—"} color={T.put} />
              <Row label="…needs" value={live?.pts_to_upside_break != null ? `+${fmtNum(live.pts_to_upside_break, 0)} pts` : "—"} />
              <Row label="Downside break below" value={live?.range_low != null ? fmtNum(live.range_low, 0) : "—"} color={T.call} />
              <Row label="…needs" value={live?.pts_to_downside_break != null ? `-${fmtNum(live.pts_to_downside_break, 0)} pts` : "—"} />
            </div>

            {(data.notes || []).length > 0 && (
              <div style={{ marginBottom: 16 }}>
                {data.notes.map((n, i) => (
                  <div key={i} style={{ fontFamily: DISP, fontSize: 13, color: i === 0 ? T.fg : T.muted, lineHeight: 1.6 }}>
                    {i === 0 ? "▸ " : "· "}{n}
                  </div>
                ))}
              </div>
            )}

            {data.strikes?.atm && (
              <>
                <div style={{ fontFamily: DISP, fontSize: 12, fontWeight: 700, color: T.muted, textTransform: "uppercase", letterSpacing: 0.6, marginBottom: 6 }}>
                  Strikes at these levels (live chain data — not a recommendation)
                </div>
                <div style={{ overflowX: "auto", marginBottom: 14 }}>
                  <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: MONO, fontSize: 12 }}>
                    <thead>
                      <tr style={{ color: T.muted, textAlign: "left" }}>
                        <th style={tradeThStyle}>Level</th>
                        <th style={tradeThStyle}>Strike</th>
                        <th style={tradeThStyle}>CE LTP</th>
                        <th style={tradeThStyle}>PE LTP</th>
                      </tr>
                    </thead>
                    <tbody>
                      <StrikeRow label="At spot (ATM)" data={data.strikes.atm} />
                      <StrikeRow label="At upside break" data={data.strikes.at_upside_break} />
                      <StrikeRow label="At downside break" data={data.strikes.at_downside_break} />
                    </tbody>
                  </table>
                </div>
              </>
            )}

            {stats && (
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 10, marginBottom: 12 }}>
                <Row label="Breaks measured" value={stats.fires_evaluated} />
                <Row label="Extended +10pt / 15min" value={stats.hit_rate_10_pct != null ? `${stats.hit_rate_10_pct}%` : "—"}
                  color={directionColor((stats.hit_rate_10_pct ?? 0) - Math.max(stats.baseline_up_10_pct ?? 0, stats.baseline_down_10_pct ?? 0))} />
                <Row label="Base rate (any bar)" value={stats.baseline_up_10_pct != null ? `↑${stats.baseline_up_10_pct}% / ↓${stats.baseline_down_10_pct}%` : "—"} />
                <Row label="Extended +20pt / 15min" value={stats.hit_rate_20_pct != null ? `${stats.hit_rate_20_pct}%` : "—"} />
                <Row label="Avg extension" value={stats.avg_extension_pts != null ? `${fmtNum(stats.avg_extension_pts, 1)} pts` : "—"} />
              </div>
            )}

            <div style={{ fontFamily: DISP, fontSize: 11, color: T.muted, lineHeight: 1.5 }}>
              A "break" is a 1-min close beyond the prior {live?.lookback_bars || 30}-minute range. Hit rates are the same rule
              replayed over the last few days of real 1-min bars — earned, not claimed. Compare them to the base rate beside
              them: if they're not meaningfully higher, a break here is no more informative than any random minute.
              Opening candles (09:15–09:39) excluded. Automated analysis for information only — not investment advice.
            </div>
          </>
        )}
      </Panel>
    </div>
  );
}

/* ---------- SMC confluence setup: sweep + FVG + OB + structure ----------
   Reads api/index.py's /api/upstox/setup. The week-long SMC backtest
   found none of these patterns predicts direction ALONE, so the backend
   only "fires" when >=2 candle factors align -- and, critically, it
   replays the exact same rule over the trailing week on every call and
   returns its real measured hit rate vs the base rate of any random bar
   making the same move. This panel shows that earned accuracy right next
   to the live signal, so the signal can never claim more than it has
   actually delivered. */
const SETUP_POLL_MS = 15000;

function SetupPanel() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const fetchInFlight = useRef(false);

  useEffect(() => {
    const tick = async () => {
      if (fetchInFlight.current) return;
      fetchInFlight.current = true;
      try {
        const res = await fetch(`${PCR_API_BASE}/upstox/setup`);
        setData(await res.json());
      } catch {
        // background tick -- not worth surfacing an error banner for
      } finally {
        setLoading(false);
        fetchInFlight.current = false;
      }
    };
    tick();
    const id = setInterval(tick, SETUP_POLL_MS);
    return () => clearInterval(id);
  }, []);

  const live = data?.live;
  const stats = data?.stats;
  const liveColor = live?.direction === "bullish" ? T.put : live?.direction === "bearish" ? T.call : T.muted;

  return (
    <div style={{ gridColumn: "1 / -1" }}>
      <Panel
        title="Move setup (SMC confluence: sweeps · FVG · order blocks · structure)"
        right={
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            {stats?.hit_rate_10_pct != null && (
              <span title={`Of the ${stats.fires_evaluated} setups this exact rule fired over the trailing week, this many reached a 10pt move within 30 minutes. Base rate for any random bar: up ${stats.baseline_up_10_pct}% / down ${stats.baseline_down_10_pct}%.`}>
                <Chip color={T.cyan}>Measured: {fmtNum(stats.hit_rate_10_pct, 0)}% hit +10pt ({stats.fires_evaluated} fires/wk)</Chip>
              </span>
            )}
            <span
              style={{ fontFamily: DISP, fontSize: 11, fontWeight: 700, color: data?.connected ? T.cyan : T.amber }}
              title={data?.connected ? "Live via your connected Upstox account" : "Upstox not connected — visit /api/upstox/login"}
            >
              {data?.connected ? "via Upstox" : "not connected"}
            </span>
          </div>
        }
      >
        {loading ? (
          <EmptyNote>Loading…</EmptyNote>
        ) : !data?.connected ? (
          <EmptyNote>{data?.error || "Upstox not connected — visit /api/upstox/login."}</EmptyNote>
        ) : data?.error ? (
          <EmptyNote>{data.error}</EmptyNote>
        ) : (
          <>
            <div style={{
              display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap", marginBottom: 14,
              padding: "12px 14px", borderRadius: 10,
              background: live?.fired ? `${liveColor}14` : T.panel2,
              border: `1px solid ${live?.fired ? liveColor : T.line}`,
            }}>
              <div style={{ fontFamily: DISP, fontSize: 15, fontWeight: 700, color: live?.fired ? liveColor : T.muted }}>
                {live?.fired
                  ? `${live.direction === "bullish" ? "📈" : "📉"} SETUP FIRING — ${live.direction.toUpperCase()}, watching for a 10–20pt ${live.direction === "bullish" ? "rise" : "fall"} within ~30 min`
                  : live?.score > 0
                    ? `No setup — ${live.score}/${2} ${live.direction} factor${live.score !== 1 ? "s" : ""} present, needs 2 aligned`
                    : "No setup — no factors aligned on the current bar"}
              </div>
            </div>

            {(live?.reasons || []).length > 0 && (
              <div style={{ marginBottom: 14 }}>
                <div style={{ fontFamily: DISP, fontSize: 12, fontWeight: 700, color: T.muted, textTransform: "uppercase", letterSpacing: 0.6, marginBottom: 6 }}>
                  Why {live.fired ? "(counted factors)" : "(factors present so far)"}
                </div>
                {live.reasons.map((r, i) => (
                  <div key={i} style={{ fontFamily: DISP, fontSize: 13, color: T.fg, lineHeight: 1.6 }}>▸ {r}</div>
                ))}
              </div>
            )}

            {(data.context || []).length > 0 && (
              <div style={{ marginBottom: 14 }}>
                <div style={{ fontFamily: DISP, fontSize: 12, fontWeight: 700, color: T.muted, textTransform: "uppercase", letterSpacing: 0.6, marginBottom: 6 }}>
                  Supporting context (not counted in the score — unmeasured)
                </div>
                {data.context.map((r, i) => (
                  <div key={i} style={{ fontFamily: DISP, fontSize: 12, color: T.muted, lineHeight: 1.6 }}>· {r}</div>
                ))}
              </div>
            )}

            {stats && (
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 10, marginBottom: 12 }}>
                <Row label="Fires (trailing week)" value={stats.fires_evaluated} />
                <Row label="Hit +10pt in 30min" value={stats.hit_rate_10_pct != null ? `${stats.hit_rate_10_pct}%` : "—"}
                  color={directionColor((stats.hit_rate_10_pct ?? 0) - Math.max(stats.baseline_up_10_pct ?? 0, stats.baseline_down_10_pct ?? 0))} />
                <Row label="Base rate (any bar, 10pt)" value={stats.baseline_up_10_pct != null ? `↑${stats.baseline_up_10_pct}% / ↓${stats.baseline_down_10_pct}%` : "—"} />
                <Row label="Hit +20pt in 30min" value={stats.hit_rate_20_pct != null ? `${stats.hit_rate_20_pct}%` : "—"} />
                <Row label="Base rate (any bar, 20pt)" value={stats.baseline_up_20_pct != null ? `↑${stats.baseline_up_20_pct}% / ↓${stats.baseline_down_20_pct}%` : "—"} />
              </div>
            )}

            <div style={{ fontFamily: DISP, fontSize: 11, color: T.muted, lineHeight: 1.5 }}>
              The rule fires only when ≥2 of the four candle factors (fresh liquidity sweep, active FVG touch, first order-block
              touch, structure bias) align in one direction. The hit rates above are the same rule replayed over the trailing
              week's real 5-min bars — earned, not claimed. If the measured rate isn't meaningfully above the base rate, this
              rule has no edge right now and its signals should be read accordingly. Opening candles (09:15–09:39) excluded.
              Automated analysis for information only — not investment advice.
            </div>
          </>
        )}
      </Panel>
    </div>
  );
}

/* ---------- backtest: which stocks drive big 5-min Nifty moves ----------
   Reads api/index.py's /api/upstox/movers/backtest -- a genuinely heavy
   call (11 parallel Upstox requests pulling a month of 5-min history), so
   this fires once on mount and otherwise only on the manual "Re-run"
   button, same convention as the main VerdictCard's deliberately-no-
   auto-refresh design -- never on a poll interval. */
function BacktestPanel() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [days, setDays] = useState(30);
  const [thresholdPts, setThresholdPts] = useState(50);

  const runBacktest = useCallback(async () => {
    setLoading(true);
    setErr("");
    try {
      const res = await fetch(`${PCR_API_BASE}/upstox/movers/backtest?days=${days}&threshold_pts=${thresholdPts}`);
      const json = await res.json();
      if (!json.connected) {
        setErr(json.error || "Upstox not connected");
        setData(null);
      } else if (json.error) {
        setErr(json.error);
        setData(null);
      } else {
        setData(json);
      }
    } catch (e) {
      setErr(e.message);
    } finally {
      setLoading(false);
    }
  }, [days, thresholdPts]);

  useEffect(() => { runBacktest(); }, []); // eslint-disable-line -- mount-only, deliberately not re-running when days/thresholdPts change until "Re-run" is clicked

  const topDriverRows = useMemo(() => {
    if (!data?.top_driver_counts) return [];
    return Object.entries(data.top_driver_counts)
      .sort((a, b) => b[1] - a[1])
      .map(([symbol, count]) => ({ symbol, count, pct: (count / data.event_count) * 100 }));
  }, [data]);

  return (
    <div style={{ gridColumn: "1 / -1" }}>
      <Panel
        title="Backtest: what drives big 5-min Nifty moves"
        right={
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <label style={{ fontFamily: DISP, fontSize: 11, color: T.muted, display: "flex", alignItems: "center", gap: 4 }}>
              days
              <input
                type="number" min="5" max="90" value={days}
                onChange={(e) => setDays(Number(e.target.value) || 30)}
                style={{ ...formInputStyle, width: 52, padding: "3px 6px" }}
              />
            </label>
            <label style={{ fontFamily: DISP, fontSize: 11, color: T.muted, display: "flex", alignItems: "center", gap: 4 }}>
              ≥ pts
              <input
                type="number" min="10" max="500" value={thresholdPts}
                onChange={(e) => setThresholdPts(Number(e.target.value) || 50)}
                style={{ ...formInputStyle, width: 56, padding: "3px 6px" }}
              />
            </label>
            <button onClick={runBacktest} disabled={loading} style={formButtonStyle(false, loading)}>
              {loading ? "Running…" : "Re-run"}
            </button>
          </div>
        }
      >
        {loading && !data ? (
          <EmptyNote>Running backtest — pulling a month of 5-min data for 11 instruments, can take several seconds…</EmptyNote>
        ) : err ? (
          <EmptyNote>{err}</EmptyNote>
        ) : !data ? (
          <EmptyNote>No backtest run yet.</EmptyNote>
        ) : (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 10, marginBottom: 16 }}>
              <Row label="Events found" value={data.event_count} />
              <Row label="Bars analyzed" value={`${data.total_bars} (${data.excluded_opening_bars} excl. open)`} />
              <Row label="Direction accuracy (all bars)" value={data.direction_accuracy_all_bars_pct != null ? `${data.direction_accuracy_all_bars_pct}%` : "—"}
                color={directionColor((data.direction_accuracy_all_bars_pct ?? 0) - 50)} />
              <Row label="Direction accuracy (events)" value={data.direction_accuracy_events_pct != null ? `${data.direction_accuracy_events_pct}%` : "—"}
                color={directionColor((data.direction_accuracy_events_pct ?? 0) - 50)} />
              <Row label="RMSE (implied vs actual %)" value={data.rmse_all_bars != null ? fmtNum(data.rmse_all_bars, 4) : "—"} />
            </div>

            {topDriverRows.length > 0 && (
              <>
                <div style={{ fontFamily: DISP, fontSize: 12, fontWeight: 700, color: T.muted, textTransform: "uppercase", letterSpacing: 0.6, marginBottom: 8 }}>
                  Top driver, by event count
                </div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 16 }}>
                  {topDriverRows.map((r) => (
                    <Chip key={r.symbol} color={T.cyan}>{r.symbol} — {r.count} ({fmtNum(r.pct, 0)}%)</Chip>
                  ))}
                </div>
              </>
            )}

            <div style={{ fontFamily: DISP, fontSize: 12, fontWeight: 700, color: T.muted, textTransform: "uppercase", letterSpacing: 0.6, marginBottom: 8 }}>
              Events ({data.from_date} to {data.to_date})
            </div>
            {(data.events || []).length === 0 ? (
              <EmptyNote>No events at this threshold over this window.</EmptyNote>
            ) : (
              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: MONO, fontSize: 12 }}>
                  <thead>
                    <tr style={{ color: T.muted, textAlign: "left" }}>
                      <th style={tradeThStyle}>Time</th>
                      <th style={tradeThStyle}>Nifty move</th>
                      <th style={tradeThStyle}>Implied</th>
                      <th style={tradeThStyle}>Top movers (that bar)</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.events.map((ev) => (
                      <tr key={ev.t} style={{ borderTop: `1px solid ${T.line}` }}>
                        <td style={tradeTdStyle}>
                          {new Date(ev.t).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}
                        </td>
                        <td style={{ ...tradeTdStyle, color: directionColor(ev.nifty_move_pts) }}>
                          {fmtSigned(ev.nifty_move_pts, 1)} pts ({fmtSigned(ev.nifty_move_pct, 2)}%)
                        </td>
                        <td style={{ ...tradeTdStyle, color: directionColor(ev.implied_pct) }}>{fmtSigned(ev.implied_pct, 3)}%</td>
                        <td style={tradeTdStyle}>
                          {ev.top_movers.map((m) => `${m.symbol} ${fmtSigned(m.pct_change, 2)}%`).join(", ")}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            <div style={{ marginTop: 12, fontFamily: DISP, fontSize: 11, color: T.muted }}>
              "Top driver" is whichever of NIFTY_TOP10 moved the most in that same 5-minute bar — correlation within the window,
              not proof of causation. Opening candles (09:15–09:39 IST) are excluded from every event and accuracy figure above.
              Automated analysis for information only — not investment advice.
            </div>
          </>
        )}
      </Panel>
    </div>
  );
}

/* ---------- paper trading journal ----------
   Simulated options trades: opened with an entry premium (typed in, or
   pulled from the live option chain below), closed later with an exit
   premium, PnL computed server-side at close time. Nothing here places a
   real order. The chain itself prefers Upstox (real broker LTPs, 1s
   refresh, requires connecting via /api/upstox/login) and falls back to
   the PCR tracker's own NSE-derived /api/optionchain/today — same origin,
   a different app — when Upstox isn't connected or errors. */
const OPTION_TYPES = ["CE", "PE"];
const TRADE_ACTIONS = ["BUY", "SELL"];
const DEFAULT_LOT_SIZE_FALLBACK = 65; // mirrors paper_trading.DEFAULT_LOT_SIZE — see that module's own caveat about this being a guess, not an authoritative current value

const formLabelStyle = { display: "block", fontFamily: DISP, fontSize: 10, color: T.muted, marginBottom: 4, textTransform: "uppercase", letterSpacing: 0.4 };
const formInputStyle = { background: T.ink, border: `1px solid ${T.line}`, color: T.fg, borderRadius: 6, padding: "6px 8px", fontFamily: MONO, fontSize: 12, width: "100%", boxSizing: "border-box" };
function formButtonStyle(primary, disabled) {
  return {
    background: primary ? T.cyan : T.panel2, color: primary ? T.ink : T.fg,
    border: `1px solid ${primary ? T.cyan : T.line}`, borderRadius: 6, padding: "0 12px", height: 30,
    fontFamily: DISP, fontSize: 12, fontWeight: 600, cursor: disabled ? "default" : "pointer",
    opacity: disabled ? 0.5 : 1, flexShrink: 0,
  };
}
const tradeThStyle = { padding: "6px 8px", fontWeight: 500, whiteSpace: "nowrap" };
const tradeTdStyle = { padding: "6px 8px", color: T.fg, whiteSpace: "nowrap" };

// Live mark-to-market for one open trade. `mapsByExpiry` is
// { [expiry]: { [strike]: row }, _default?: flatMap }. Older trades without
// an expiry fall back to `_default` (the form's currently selected expiry).
function liveFigures(trade, mapsByExpiry) {
  const invested = trade.entry_price * trade.lot_size * trade.lots;
  if (trade.status !== "open") {
    return { invested, currentLtp: null, currentValue: null, pnl: trade.pnl, isLive: false };
  }
  const chainByStrike = (trade.expiry && mapsByExpiry?.[trade.expiry])
    || mapsByExpiry?._default
    || mapsByExpiry
    || {};
  const chainRow = chainByStrike[Number(trade.strike)];
  const currentLtp = chainRow ? (trade.option_type === "CE" ? chainRow.ceLtp : chainRow.peLtp) : null;
  if (currentLtp == null) {
    return { invested, currentLtp: null, currentValue: null, pnl: null, isLive: false };
  }
  const direction = trade.action === "BUY" ? 1 : -1;
  const pnl = (currentLtp - trade.entry_price) * direction * trade.lot_size * trade.lots;
  return { invested, currentLtp, currentValue: currentLtp * trade.lot_size * trade.lots, pnl, isLive: true };
}

// Returns "stop_loss" | "target" | null. For a BUY, stop_loss sits below
// entry (triggers if price falls to/through it) and target sits above
// (triggers if price rises to/through it); for a SELL (writing/shorting)
// it's the mirror image, matching compute_pnl's sign convention elsewhere.
// Checked >= /<= (not ==) since the polled LTP can jump past the exact
// trigger between ticks rather than landing on it precisely.
function checkStopTarget(trade, currentLtp) {
  if (currentLtp == null) return null;
  const isBuy = trade.action === "BUY";
  if (trade.stop_loss != null) {
    const hit = isBuy ? currentLtp <= trade.stop_loss : currentLtp >= trade.stop_loss;
    if (hit) return "stop_loss";
  }
  if (trade.target_price != null) {
    const hit = isBuy ? currentLtp >= trade.target_price : currentLtp <= trade.target_price;
    if (hit) return "target";
  }
  return null;
}

// Prefers Upstox (real broker LTPs, connected via /api/upstox/login) for
// the second-by-second feed; falls back to the NSE-scrape-backed
// /api/optionchain/today whenever Upstox isn't connected or errors.
// Pass `expiry` (NSE `18-Aug-2026` or ISO `2026-08-18`) to pin a contract week.
async function fetchChain(expiry) {
  const expiryQ = expiry ? `&expiry=${encodeURIComponent(expiry)}` : "";
  let upstoxHint = null;
  try {
    const res = await fetch(`${PCR_API_BASE}/upstox/optionchain?symbol=NIFTY${expiryQ}`);
    if (res.ok) {
      const json = await res.json();
      if (json.connected && (json.rows || []).length > 0) {
        return {
          spot: json.spot ?? null, rows: json.rows, source: "upstox",
          expiry: json.expiry || expiry || null, connected: true, upstoxHint: null,
        };
      }
      if (json.connected === false) {
        upstoxHint = json.error || "Upstox not connected";
      }
    }
  } catch {
    // fall through to the NSE fallback below
  }
  try {
    const res = await fetch(`${PCR_API_BASE}/optionchain/today?symbol=NIFTY&n=50${expiryQ}`);
    if (!res.ok) return { spot: null, rows: [], source: null, expiry: expiry || null, connected: false, upstoxHint };
    const json = await res.json();
    return {
      spot: json.spot ?? null, rows: json.rows || [], source: "nse",
      expiry: json.expiry || expiry || null, connected: false, upstoxHint,
    };
  } catch {
    return { spot: null, rows: [], source: null, expiry: expiry || null, connected: false, upstoxHint };
  }
}

function expiryLabel(exp) {
  if (!exp) return "—";
  // Accept ISO or NSE format for display.
  try {
    if (/^\d{4}-\d{2}-\d{2}$/.test(exp)) {
      const d = new Date(exp + "T00:00:00");
      return d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
    }
  } catch { /* keep raw */ }
  return exp;
}

function chainMapFromRows(rows) {
  const map = {};
  rows.forEach((r) => { map[Number(r.strike)] = r; });
  return map;
}

// Ticks its own clock every second (broker-platform-style live elapsed
// time) — separate from the PnL itself, which can only update as fast as
// the underlying premium data does (see the 5s poll below). A visible
// second-by-second clock next to a slower-updating PnL is normal and
// matches what real platforms show.
function LiveElapsed({ since }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  if (!since) return null;
  const elapsedSec = Math.max(0, Math.floor((now - new Date(since).getTime()) / 1000));
  const h = Math.floor(elapsedSec / 3600);
  const m = Math.floor((elapsedSec % 3600) / 60);
  const s = elapsedSec % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return <span style={{ fontFamily: MONO, fontSize: 10, color: T.cyan }}>{h > 0 ? `${h}h ` : ""}{pad(m)}:{pad(s)}</span>;
}

function PaperTradingPanel() {
  const [trades, setTrades] = useState([]);
  const [summary, setSummary] = useState(null);
  const [weekly, setWeekly] = useState([]);
  const [chainRows, setChainRows] = useState([]);
  const [chainSpot, setChainSpot] = useState(null);
  // mapsByExpiry: { [expiry]: { [strike]: row }, _default: current form expiry map }
  const [mapsByExpiry, setMapsByExpiry] = useState({});
  const [chainSource, setChainSource] = useState(null); // "upstox" | "nse" | null
  const [upstoxConnected, setUpstoxConnected] = useState(null); // null | true | false
  const [upstoxHint, setUpstoxHint] = useState(null);
  const [expiries, setExpiries] = useState([]);
  const [selectedExpiry, setSelectedExpiry] = useState("");
  const chainFetchInFlight = useRef(false);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [form, setForm] = useState({
    strike: "", optionType: "CE", action: "BUY", lots: 1, lotSize: DEFAULT_LOT_SIZE_FALLBACK, entryPrice: "",
    stopLoss: "", targetPrice: "", notes: "",
  });
  const tradesRef = useRef([]);
  const selectedExpiryRef = useRef("");
  const autoClosingRef = useRef(new Set());
  const [fetchingLtp, setFetchingLtp] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [closingDrafts, setClosingDrafts] = useState({});

  const refreshUpstoxStatus = useCallback(async () => {
    try {
      const res = await fetch(`${PCR_API_BASE}/upstox/status`);
      if (!res.ok) { setUpstoxConnected(false); return; }
      const json = await res.json();
      setUpstoxConnected(Boolean(json.connected));
    } catch {
      setUpstoxConnected(false);
    }
  }, []);

  const loadExpiries = useCallback(async () => {
    try {
      const res = await fetch(`${PCR_API_BASE}/expiries?symbol=NIFTY`);
      if (!res.ok) return;
      const json = await res.json();
      const list = json.expiries || [];
      setExpiries(list);
      setSelectedExpiry((prev) => (prev && list.includes(prev) ? prev : list[0] || ""));
    } catch {
      /* nice-to-have; chain still loads with backend default */
    }
  }, []);

  // Fetch one or more expiries and rebuild mapsByExpiry. Always refreshes
  // the form's selected expiry (for the clickable chain table) plus any
  // distinct expiries on open trades so multi-week journals mark correctly.
  const refreshChains = useCallback(async (formExpiry, openList) => {
    const needed = [];
    const seen = new Set();
    const push = (exp) => {
      const key = exp || "";
      if (seen.has(key)) return;
      seen.add(key);
      needed.push(exp || null);
    };
    push(formExpiry || null);
    (openList || []).forEach((t) => {
      if (t.status === "open" && t.expiry) push(t.expiry);
    });

    const results = await Promise.all(needed.map((exp) => fetchChain(exp || undefined)));
    const maps = {};
    let formChain = results[0] || { rows: [], spot: null, source: null, upstoxHint: null, connected: false };

    results.forEach((chain, i) => {
      const requested = needed[i];
      const map = chainMapFromRows(chain.rows || []);
      if (requested) maps[requested] = map;
      if (chain.expiry && chain.expiry !== requested) maps[chain.expiry] = map;
      if (i === 0) formChain = chain;
    });
    maps._default = chainMapFromRows(formChain.rows || []);

    setChainRows(formChain.rows || []);
    setChainSpot(formChain.spot ?? null);
    setChainSource(formChain.source);
    setMapsByExpiry(maps);
    if (formChain.upstoxHint) setUpstoxHint(formChain.upstoxHint);
    else if (formChain.source === "upstox") setUpstoxHint(null);
    if (formChain.source === "upstox") setUpstoxConnected(true);
    else if (formChain.connected === false) setUpstoxConnected(false);
    return { formChain, maps };
  }, []);

  const load = useCallback(async () => {
    try {
      const json = await getJSON("/paper-trades?days=90");
      const list = json.trades || [];
      setTrades(list);
      setSummary(json.summary || null);
      setWeekly(json.weekly || []);
      await refreshChains(selectedExpiryRef.current, list);
      setErr("");
    } catch (e) {
      setErr(e.message);
    } finally {
      setLoading(false);
    }
  }, [refreshChains]);

  useEffect(() => { loadExpiries(); refreshUpstoxStatus(); }, [loadExpiries, refreshUpstoxStatus]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { tradesRef.current = trades; }, [trades]);
  useEffect(() => { selectedExpiryRef.current = selectedExpiry; }, [selectedExpiry]);

  // When the user picks a different expiry, reload that chain for the table
  // (and keep open-trade maps). Don't re-fetch the whole trades list.
  useEffect(() => {
    if (!selectedExpiry && expiries.length === 0) return;
    let cancelled = false;
    (async () => {
      if (chainFetchInFlight.current) return;
      chainFetchInFlight.current = true;
      try {
        if (!cancelled) await refreshChains(selectedExpiry, tradesRef.current);
      } finally {
        chainFetchInFlight.current = false;
      }
    })();
    return () => { cancelled = true; };
  }, [selectedExpiry, refreshChains, expiries.length]);

  const openTrades = trades.filter((t) => t.status === "open");

  useEffect(() => {
    const id = setInterval(async () => {
      if (chainFetchInFlight.current) return;
      chainFetchInFlight.current = true;
      try {
        const { maps } = await refreshChains(selectedExpiryRef.current, tradesRef.current);

        for (const t of tradesRef.current) {
          if (t.status !== "open" || autoClosingRef.current.has(t.id)) continue;
          const { currentLtp } = liveFigures(t, maps);
          const reason = checkStopTarget(t, currentLtp);
          if (!reason) continue;
          autoClosingRef.current.add(t.id);
          try {
            await postJSON(`/paper-trades/${t.id}/close`, { exit_price: currentLtp, reason });
            await load();
          } finally {
            autoClosingRef.current.delete(t.id);
          }
        }
      } finally {
        chainFetchInFlight.current = false;
      }
    }, 1000);
    return () => clearInterval(id);
  }, [load, refreshChains]);

  const selectStrike = (strike, optionType, ltp) => {
    setForm((f) => ({ ...f, strike: String(strike), optionType, entryPrice: ltp != null ? String(ltp) : f.entryPrice }));
  };

  const unrealizedTotal = openTrades.reduce((sum, t) => {
    const { pnl, isLive } = liveFigures(t, mapsByExpiry);
    return isLive ? sum + pnl : sum;
  }, 0);
  const investedTotal = openTrades.reduce((sum, t) => sum + liveFigures(t, mapsByExpiry).invested, 0);
  const currentValueTotal = openTrades.reduce((sum, t) => {
    const { currentValue, isLive } = liveFigures(t, mapsByExpiry);
    return isLive ? sum + currentValue : sum;
  }, 0);

  const fetchLivePremium = async () => {
    if (!form.strike) return;
    setFetchingLtp(true);
    try {
      const chain = await fetchChain(selectedExpiry || undefined);
      const row = (chain.rows || []).find((r) => Number(r.strike) === Number(form.strike));
      if (!row) {
        setErr(`No live data for strike ${form.strike}${selectedExpiry ? ` on ${expiryLabel(selectedExpiry)}` : ""} — try a strike closer to spot, or type the premium in manually.`);
        return;
      }
      const ltp = form.optionType === "CE" ? row.ceLtp : row.peLtp;
      if (ltp == null) {
        setErr(`Strike ${form.strike} found, but no ${form.optionType} premium in the response.`);
        return;
      }
      setForm((f) => ({ ...f, entryPrice: String(ltp) }));
      setChainSource(chain.source);
      if (chain.source === "upstox") setUpstoxConnected(true);
      setErr("");
    } catch (e) {
      setErr(`Couldn't fetch live premium: ${e.message}`);
    } finally {
      setFetchingLtp(false);
    }
  };

  const submitTrade = async (e) => {
    e.preventDefault();
    if (!form.strike || !form.entryPrice) {
      setErr("Strike and entry price are required.");
      return;
    }
    setSubmitting(true);
    try {
      await postJSON("/paper-trades", {
        strike: Number(form.strike),
        option_type: form.optionType,
        action: form.action,
        entry_price: Number(form.entryPrice),
        lots: Number(form.lots) || 1,
        lot_size: Number(form.lotSize) || DEFAULT_LOT_SIZE_FALLBACK,
        stop_loss: form.stopLoss ? Number(form.stopLoss) : null,
        target_price: form.targetPrice ? Number(form.targetPrice) : null,
        notes: form.notes || null,
        expiry: selectedExpiry || null,
      });
      setForm((f) => ({ ...f, strike: "", entryPrice: "", stopLoss: "", targetPrice: "", notes: "" }));
      setErr("");
      await load();
    } catch (e) {
      setErr(e.message);
    } finally {
      setSubmitting(false);
    }
  };

  const submitClose = async (tradeId) => {
    const exitPrice = closingDrafts[tradeId];
    if (!exitPrice) return;
    try {
      await postJSON(`/paper-trades/${tradeId}/close`, { exit_price: Number(exitPrice) });
      setClosingDrafts((c) => {
        const next = { ...c };
        delete next[tradeId];
        return next;
      });
      setErr("");
      await load();
    } catch (e) {
      setErr(e.message);
    }
  };

  const connectUpstoxHref = `${PCR_API_BASE}/upstox/login`;

  return (
    <Panel
      title="Paper trading journal"
      right={summary && (
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <Chip color={T.muted}>{summary.open_count} open</Chip>
          {openTrades.length > 0 && (
            <>
              <Chip color={T.muted}>Invested: {fmtNum(investedTotal, 0)}</Chip>
              <Chip color={T.cyan}>Current value: {fmtNum(currentValueTotal, 0)}</Chip>
              <Chip color={directionColor(unrealizedTotal)}>Unrealized: {fmtSigned(unrealizedTotal, 0)}</Chip>
            </>
          )}
          {summary.win_rate_pct != null && (
            <Chip color={summary.win_rate_pct >= 50 ? T.put : T.call}>Win rate: {summary.win_rate_pct}%</Chip>
          )}
          <Chip color={directionColor(summary.total_pnl)}>Realized PnL: {fmtSigned(summary.total_pnl, 0)}</Chip>
          {upstoxConnected ? (
            <Chip color={T.cyan} title="Live premiums via your connected Upstox session">Upstox linked</Chip>
          ) : (
            <a
              href={connectUpstoxHref}
              target="_blank"
              rel="noreferrer"
              style={{ textDecoration: "none" }}
              title="Open Upstox OAuth — after login, live LTPs use your broker feed"
            >
              <Chip color={T.amber}>Connect Upstox</Chip>
            </a>
          )}
        </div>
      )}
    >
      {!upstoxConnected && upstoxConnected !== null && (
        <div style={{
          marginBottom: 12, padding: "8px 12px", background: `${T.amber}14`,
          border: `1px solid ${T.amber}55`, borderRadius: 8, color: T.fg, fontSize: 12,
          display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap", alignItems: "center",
        }}>
          <span>
            {upstoxHint || "Upstox not linked — paper journal is using the NSE-derived feed. Connect for broker LTPs."}
          </span>
          <a href={connectUpstoxHref} target="_blank" rel="noreferrer" style={{ color: T.cyan, fontWeight: 600 }}>
            Link Upstox →
          </a>
        </div>
      )}

      <form onSubmit={submitTrade} style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "flex-end", marginBottom: 16 }}>
        <div style={{ width: 130 }}>
          <label style={formLabelStyle}>Expiry</label>
          <select
            value={selectedExpiry}
            onChange={(e) => setSelectedExpiry(e.target.value)}
            style={formInputStyle}
            title="Which weekly/monthly contract this paper trade is on"
          >
            {expiries.length === 0 && <option value="">Nearest</option>}
            {expiries.map((exp) => (
              <option key={exp} value={exp}>{expiryLabel(exp)}</option>
            ))}
          </select>
        </div>
        <div style={{ width: 100 }}>
          <label style={formLabelStyle}>Strike</label>
          <input type="number" value={form.strike} placeholder="24500"
            onChange={(e) => setForm((f) => ({ ...f, strike: e.target.value }))} style={formInputStyle} />
        </div>
        <div style={{ width: 70 }}>
          <label style={formLabelStyle}>Type</label>
          <select value={form.optionType} onChange={(e) => setForm((f) => ({ ...f, optionType: e.target.value }))} style={formInputStyle}>
            {OPTION_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </div>
        <div style={{ width: 85 }}>
          <label style={formLabelStyle}>Action</label>
          <select value={form.action} onChange={(e) => setForm((f) => ({ ...f, action: e.target.value }))} style={formInputStyle}>
            {TRADE_ACTIONS.map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
        </div>
        <div style={{ width: 65 }}>
          <label style={formLabelStyle}>Lots</label>
          <input type="number" min="1" value={form.lots}
            onChange={(e) => setForm((f) => ({ ...f, lots: e.target.value }))} style={formInputStyle} />
        </div>
        <div style={{ width: 90 }}>
          <label style={formLabelStyle} title="NSE revises this periodically — confirm the current value before trusting PnL.">Lot size</label>
          <input type="number" min="1" value={form.lotSize}
            onChange={(e) => setForm((f) => ({ ...f, lotSize: e.target.value }))} style={formInputStyle} />
        </div>
        <div style={{ width: 110 }}>
          <label style={formLabelStyle}>Entry premium</label>
          <input type="number" step="0.05" value={form.entryPrice} placeholder="120.50"
            onChange={(e) => setForm((f) => ({ ...f, entryPrice: e.target.value }))} style={formInputStyle} />
        </div>
        <button type="button" onClick={fetchLivePremium} disabled={!form.strike || fetchingLtp} style={formButtonStyle(false, !form.strike || fetchingLtp)}>
          {fetchingLtp ? "Fetching…" : "Fetch live"}
        </button>
        <div style={{ width: 100 }}>
          <label style={formLabelStyle} title="Auto-closes the trade if the live premium reaches this (watched while this page is open — not a standing broker order)">Stop-loss</label>
          <input type="number" step="0.05" value={form.stopLoss} placeholder="optional"
            onChange={(e) => setForm((f) => ({ ...f, stopLoss: e.target.value }))} style={formInputStyle} />
        </div>
        <div style={{ width: 100 }}>
          <label style={formLabelStyle} title="Auto-closes the trade if the live premium reaches this (watched while this page is open — not a standing broker order)">Target</label>
          <input type="number" step="0.05" value={form.targetPrice} placeholder="optional"
            onChange={(e) => setForm((f) => ({ ...f, targetPrice: e.target.value }))} style={formInputStyle} />
        </div>
        <div style={{ flex: 1, minWidth: 140 }}>
          <label style={formLabelStyle}>Notes (optional)</label>
          <input type="text" value={form.notes} placeholder="why this trade"
            onChange={(e) => setForm((f) => ({ ...f, notes: e.target.value }))} style={formInputStyle} />
        </div>
        <button type="submit" disabled={submitting} style={formButtonStyle(true, submitting)}>
          {submitting ? "Adding…" : "Add trade"}
        </button>
      </form>

      {chainRows.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 6, flexWrap: "wrap", gap: 6 }}>
            <div style={{ fontFamily: DISP, fontSize: 11, fontWeight: 700, color: T.muted, textTransform: "uppercase", letterSpacing: 0.6 }}>
              Live NIFTY option chain
              {selectedExpiry && (
                <span style={{ color: T.fg, textTransform: "none", fontWeight: 400 }}> · {expiryLabel(selectedExpiry)}</span>
              )}
              {chainSpot != null && (
                <span style={{ color: T.fg, textTransform: "none", fontWeight: 400 }}> · spot {fmtNum(chainSpot, 1)}</span>
              )}
              {chainSource && (
                <span
                  style={{
                    marginLeft: 8, textTransform: "none", fontWeight: 600, fontSize: 10,
                    color: chainSource === "upstox" ? T.cyan : T.amber,
                  }}
                  title={chainSource === "upstox"
                    ? "Live via your connected Upstox account (1s refresh)"
                    : "Upstox not connected — falling back to the NSE-derived feed. Use Connect Upstox above."}
                >
                  via {chainSource === "upstox" ? "Upstox" : "NSE fallback"}
                </span>
              )}
            </div>
            <span style={{ fontFamily: DISP, fontSize: 10, color: T.muted }}>Click a CE/PE premium to fill the form</span>
          </div>
          <div style={{ overflow: "auto", maxHeight: 240, border: `1px solid ${T.line}`, borderRadius: 8 }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: MONO, fontSize: 12 }}>
              <thead>
                <tr style={{ color: T.muted, textAlign: "left" }}>
                  <th style={{ ...tradeThStyle, position: "sticky", top: 0, background: T.panel }}>CE OI</th>
                  <th style={{ ...tradeThStyle, position: "sticky", top: 0, background: T.panel, color: T.call }}>CE LTP</th>
                  <th style={{ ...tradeThStyle, position: "sticky", top: 0, background: T.panel, textAlign: "center" }}>Strike</th>
                  <th style={{ ...tradeThStyle, position: "sticky", top: 0, background: T.panel, color: T.put }}>PE LTP</th>
                  <th style={{ ...tradeThStyle, position: "sticky", top: 0, background: T.panel }}>PE OI</th>
                </tr>
              </thead>
              <tbody>
                {[...chainRows].sort((a, b) => a.strike - b.strike).map((r) => {
                  const isAtm = chainSpot != null && Math.abs(r.strike - chainSpot) <= 25;
                  return (
                    <tr key={r.strike} style={{ borderTop: `1px solid ${T.line}`, background: isAtm ? `${T.cyan}14` : "transparent" }}>
                      <td style={tradeTdStyle}>{fmtNum(r.ceOi, 0)}</td>
                      <td
                        style={{ ...tradeTdStyle, color: T.call, cursor: r.ceLtp != null ? "pointer" : "default", fontWeight: 700 }}
                        onClick={() => r.ceLtp != null && selectStrike(r.strike, "CE", r.ceLtp)}
                        title={r.ceLtp != null ? "Use this strike/premium for a CE trade" : undefined}
                      >
                        {r.ceLtp != null ? fmtNum(r.ceLtp, 2) : "—"}
                      </td>
                      <td style={{ ...tradeTdStyle, textAlign: "center", fontWeight: 700, color: isAtm ? T.cyan : T.fg }}>{fmtNum(r.strike, 0)}</td>
                      <td
                        style={{ ...tradeTdStyle, color: T.put, cursor: r.peLtp != null ? "pointer" : "default", fontWeight: 700 }}
                        onClick={() => r.peLtp != null && selectStrike(r.strike, "PE", r.peLtp)}
                        title={r.peLtp != null ? "Use this strike/premium for a PE trade" : undefined}
                      >
                        {r.peLtp != null ? fmtNum(r.peLtp, 2) : "—"}
                      </td>
                      <td style={tradeTdStyle}>{fmtNum(r.peOi, 0)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {err && (
        <div style={{ marginBottom: 12, padding: "8px 12px", background: `${T.call}18`, border: `1px solid ${T.call}55`, borderRadius: 8, color: T.call, fontSize: 12 }}>
          {err}
        </div>
      )}

      {loading ? (
        <EmptyNote>Loading trades…</EmptyNote>
      ) : trades.length === 0 ? (
        <EmptyNote>No paper trades yet — add one above.</EmptyNote>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: MONO, fontSize: 12 }}>
            <thead>
              <tr style={{ color: T.muted, textAlign: "left" }}>
                <th style={tradeThStyle}>Entry time</th>
                <th style={tradeThStyle}>Expiry</th>
                <th style={tradeThStyle}>Strike</th>
                <th style={tradeThStyle}>Type</th>
                <th style={tradeThStyle}>Action</th>
                <th style={tradeThStyle}>Lots</th>
                <th style={tradeThStyle}>Entry</th>
                <th style={tradeThStyle} title="Auto-close levels, watched while this page is open">SL / Target</th>
                <th style={tradeThStyle}>Invested</th>
                <th style={tradeThStyle}>LTP / Exit</th>
                <th style={tradeThStyle}>Value</th>
                <th style={tradeThStyle} title="Live running PnL while open (not yet real), or the exact amount booked once you've closed the trade">
                  Unrealized / Booked
                </th>
                <th style={tradeThStyle}>Status</th>
              </tr>
            </thead>
            <tbody>
              {trades.map((t) => {
                const { invested, currentLtp, currentValue, pnl, isLive } = liveFigures(t, mapsByExpiry);
                const isOpen = t.status === "open";
                return (
                  <tr key={t.id} style={{ borderTop: `1px solid ${T.line}` }}>
                    <td style={tradeTdStyle}>
                      <div>{t.trade_date}</div>
                      <div style={{ color: T.muted, fontSize: 10 }}>
                        {t.entry_time ? new Date(t.entry_time).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—"} IST
                      </div>
                      {isOpen && <LiveElapsed since={t.entry_time} />}
                    </td>
                    <td style={tradeTdStyle}>{expiryLabel(t.expiry)}</td>
                    <td style={tradeTdStyle}>{fmtNum(t.strike, 0)}</td>
                    <td style={tradeTdStyle}>{t.option_type}</td>
                    <td style={tradeTdStyle}>{t.action}</td>
                    <td style={tradeTdStyle}>{t.lots}</td>
                    <td style={tradeTdStyle}>{fmtNum(t.entry_price, 2)}</td>
                    <td style={tradeTdStyle}>
                      {t.stop_loss != null && <div style={{ color: T.call }}>SL {fmtNum(t.stop_loss, 2)}</div>}
                      {t.target_price != null && <div style={{ color: T.put }}>Tgt {fmtNum(t.target_price, 2)}</div>}
                      {t.stop_loss == null && t.target_price == null && "—"}
                    </td>
                    <td style={tradeTdStyle}>{fmtNum(invested, 0)}</td>
                    <td style={tradeTdStyle}>
                      {isOpen
                        ? (currentLtp != null ? <>{fmtNum(currentLtp, 2)} <span style={{ color: T.cyan, fontSize: 10 }}>live</span></> : "—")
                        : (t.exit_price != null ? fmtNum(t.exit_price, 2) : "—")}
                    </td>
                    <td style={tradeTdStyle}>{currentValue != null ? fmtNum(currentValue, 0) : (isOpen ? "—" : fmtNum(invested + (t.pnl || 0), 0))}</td>
                    <td style={{ ...tradeTdStyle, color: pnl != null ? directionColor(pnl) : T.muted, fontWeight: 700 }}>
                      {pnl != null ? fmtSigned(pnl, 0) : "—"}
                      {isOpen && isLive && <span style={{ color: T.muted, fontWeight: 400, fontSize: 10 }}> unrealized</span>}
                      {!isOpen && pnl != null && <span style={{ color: T.muted, fontWeight: 400, fontSize: 10 }}> booked</span>}
                    </td>
                    <td style={tradeTdStyle}>
                      {isOpen ? (
                        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                          <input type="number" step="0.05" placeholder="exit premium" value={closingDrafts[t.id] || ""}
                            onChange={(e) => setClosingDrafts((c) => ({ ...c, [t.id]: e.target.value }))}
                            style={{ ...formInputStyle, width: 90 }} />
                          <button onClick={() => submitClose(t.id)} disabled={!closingDrafts[t.id]}
                            style={formButtonStyle(false, !closingDrafts[t.id])}>
                            Close
                          </button>
                        </div>
                      ) : (
                        <Chip color={t.exit_reason === "stop_loss" ? T.call : t.exit_reason === "target" ? T.put : T.muted}>
                          {t.exit_reason === "stop_loss" ? "SL hit" : t.exit_reason === "target" ? "target hit" : "closed"}
                        </Chip>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {weekly.length > 0 && (
        <>
          <div style={{ height: 16 }} />
          <div style={{ fontFamily: DISP, fontSize: 11, fontWeight: 700, color: T.muted, textTransform: "uppercase", letterSpacing: 0.6, marginBottom: 8 }}>
            Weekly PnL
          </div>
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: MONO, fontSize: 12 }}>
              <thead>
                <tr style={{ color: T.muted, textAlign: "left" }}>
                  <th style={tradeThStyle}>Week of</th>
                  <th style={tradeThStyle}>Trades</th>
                  <th style={tradeThStyle}>Won</th>
                  <th style={tradeThStyle}>Lost</th>
                  <th style={tradeThStyle}>PnL</th>
                </tr>
              </thead>
              <tbody>
                {weekly.map((w) => (
                  <tr key={w.week_start} style={{ borderTop: `1px solid ${T.line}` }}>
                    <td style={tradeTdStyle}>{w.week_start}</td>
                    <td style={tradeTdStyle}>{w.trades}</td>
                    <td style={{ ...tradeTdStyle, color: T.put }}>{w.wins}</td>
                    <td style={{ ...tradeTdStyle, color: T.call }}>{w.losses}</td>
                    <td style={{ ...tradeTdStyle, color: directionColor(w.total_pnl), fontWeight: 700 }}>{fmtSigned(w.total_pnl, 0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      <div style={{ height: 8 }} />
      <EmptyNote>
        Simulated trades only — nothing here places a real order. Pick an expiry, then entry/exit premiums come from
        the live option chain (Upstox when linked, otherwise NSE fallback). Lot size defaults to a guess — confirm
        against NSE before trusting PnL. Live premiums refresh every 1s. Stop-loss/target only fire while this page
        is open and polling.
      </EmptyNote>
    </Panel>
  );
}

/* ---------- events & news ----------
   news_ai.get_news_brief() only ever hands back TOP_NEWS_COUNT (4)
   headlines — the ones ranked most likely to actually move the market, out
   of up to 25 fetched — so this renders whatever it's given directly
   rather than slicing again client-side. */
const IMPACT_COLOR = { high: T.call, medium: T.amber, low: T.muted };

function EventsNewsPanel({ brief }) {
  const headlines = brief?.headlines || [];
  const sentiment = brief?.news_sentiment;
  return (
    <Panel title="Events & news">
      {sentiment
        ? <div style={{ fontFamily: DISP, fontSize: 13, color: T.fg, marginBottom: 10 }}>{sentiment}</div>
        : <EmptyNote>News sentiment unavailable — classifier not wired up yet.</EmptyNote>}
      {headlines.length === 0 ? (
        <EmptyNote>No market-moving headlines for this brief.</EmptyNote>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {headlines.map((h, i) => (
            <div key={i} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                <Chip color={SENTIMENT_COLOR[h.sentiment] || T.muted}>{h.sentiment || "neutral"}</Chip>
                {h.impact && <Chip color={IMPACT_COLOR[h.impact] || T.muted}>{h.impact} impact</Chip>}
              </div>
              <span style={{ fontFamily: DISP, fontSize: 12, color: T.fg }}>{h.headline || h.title}</span>
              {h.reason && h.reason !== "unavailable" && (
                <span style={{ fontFamily: DISP, fontSize: 11, color: T.muted }}>{h.reason}</span>
              )}
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}

/* ---------- levels ---------- */
function LevelsPanel({ brief }) {
  const c = brief?.components || {};
  const levels = c.levels;
  const structure = c.structure;
  if (!levels && !structure) return <Panel title="Levels"><EmptyNote>No levels/structure data yet.</EmptyNote></Panel>;
  return (
    <Panel title="Levels">
      {levels ? (
        <>
          <Row label="PDH" value={fmtNum(levels.pdh, 0)} />
          <Row label="PDL" value={fmtNum(levels.pdl, 0)} />
          <Row label="Previous close" value={fmtNum(levels.previous_close, 0)} />
          <Row label="Previous day range" value={fmtNum(levels.previous_day_range, 0)} />
          <Row label="Close position in range" value={levels.close_position_pct != null ? `${fmtNum(levels.close_position_pct, 0)}%` : "—"} />
        </>
      ) : <EmptyNote>Levels unavailable.</EmptyNote>}
      <div style={{ height: 10 }} />
      {structure ? (
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <Chip color={structure.bias === "bullish" ? T.put : structure.bias === "bearish" ? T.call : T.amber}>
            Structure: {structure.bias}
          </Chip>
          {structure.last_event && <Chip color={T.cyan}>{structure.last_event.replace("_", " ")}</Chip>}
        </div>
      ) : <EmptyNote>Structure unavailable.</EmptyNote>}
    </Panel>
  );
}

/* ---------- daily journal spreadsheet ----------
   One row per trading day, columns matching the manual pre-market checklist
   this replaces (Chart / Option Chain / PCR / Participant Option Data /
   Participant Futures Data / Participant Stock Data / GIFT Nifty / Verdict
   / Trades). Built entirely from data GET /brief/history already returns —
   no new backend endpoint. Cells that this engine genuinely doesn't compute
   yet (Option Chain, PCR, Participant Option Data) say so plainly rather
   than being left blank or guessed at; "Trades" is a plain support/
   resistance + structure read derived from fields already on the brief,
   never a fabricated options premium target (no options pricing data
   exists anywhere in this engine to base one on). */
const TREND_ARROW = { rising: "↑", falling: "↓", flat: "↔" };

function deriveChartCell(b) {
  const s = b.components?.structure;
  if (!s?.bias) return "No structure data";
  const event = s.last_event ? s.last_event.replace(/_/g, " ") : "no recent break";
  return `NIFTY: ${s.bias} (${event})`;
}
function deriveOptionChainCell(b) {
  const o = b.components?.option_snapshot;
  if (!o || (o.max_call_oi_strike == null && o.max_put_oi_strike == null)) return "No data";
  return `Max Call ${fmtNum(o.max_call_oi_strike, 0)} / Max Put ${fmtNum(o.max_put_oi_strike, 0)}`;
}
function derivePcrCell(b) {
  const pcr = b.components?.option_snapshot?.pcr;
  return pcr != null ? fmtNum(pcr, 2) : "No data";
}
function deriveParticipantFuturesCell(b) {
  const p = b.components?.participants;
  if (!p || !Object.keys(p).length) return "No data";
  return PARTICIPANT_ORDER.filter((k) => p[k]).map((k) => {
    const row = p[k];
    return `${k} ${row.ratio != null ? fmtNum(row.ratio, 1) : "—"}%${TREND_ARROW[row.trend] || ""}`;
  }).join(" · ");
}
function deriveParticipantCashCell(b) {
  const c = b.components?.fii_dii_cash;
  if (!c) return "No data";
  const net = (buy, sell) => (buy != null && sell != null ? buy - sell : null);
  const fmtNet = (v) => (v == null ? "—" : `${v >= 0 ? "+" : ""}₹${fmtNum(Math.abs(v), 0)}cr`);
  return `FII ${fmtNet(net(c.fii_buy, c.fii_sell))} · DII ${fmtNet(net(c.dii_buy, c.dii_sell))}`;
}
function deriveGiftCell(b) {
  const g = b.components?.gift;
  if (!g || g.price == null) return "No data";
  const gap = g.gap_pct != null ? ` (${fmtSigned(g.gap_pct)}%)` : "";
  return `${fmtNum(g.price, 0)}${gap}`;
}
function deriveTradesCell(b) {
  const low = b.expected_low, high = b.expected_high;
  const bias = b.components?.structure?.bias;
  if (low == null || high == null) return "No range available";
  const zone = `${fmtNum(low, 0)}–${fmtNum(high, 0)}`;
  if (bias === "bullish") return `Watch ${zone}. Hold + bullish structure → lean long.`;
  if (bias === "bearish") return `Watch ${zone}. Break below with volume → lean short.`;
  return `Watch ${zone}. No structure edge — wait for a break.`;
}

const JOURNAL_COLUMNS = [
  { key: "chart", label: "Chart", render: deriveChartCell },
  { key: "option_chain", label: "Option Chain", render: deriveOptionChainCell },
  { key: "pcr", label: "PCR", render: derivePcrCell },
  { key: "participant_option", label: "Participant Option Data", render: () => "Not tracked" },
  { key: "participant_futures", label: "Participant Futures Data", render: deriveParticipantFuturesCell },
  { key: "participant_stock", label: "Participant Stock Data", render: deriveParticipantCashCell },
  { key: "gift", label: "GIFT Nifty (Overnight)", render: deriveGiftCell },
  {
    key: "verdict", label: "Verdict",
    render: (b) => (
      <span style={{ color: VERDICT_COLOR[b.verdict] || T.fg, fontWeight: 700 }}>
        {b.verdict} ({fmtSigned(b.score, 0)})
      </span>
    ),
  },
  { key: "trades", label: "Trades", render: deriveTradesCell },
];

// Fixed per-column widths (px) so the grid reads like an actual spreadsheet
// instead of one giant unreadable line per cell — text-heavy columns wrap
// within their column instead of forcing whiteSpace:nowrap to stretch the
// whole table. Date/Verdict stay narrow since their content is always short.
const JOURNAL_COL_WIDTH = {
  chart: 200, option_chain: 150, pcr: 80, participant_option: 130,
  participant_futures: 240, participant_stock: 170, gift: 150,
  verdict: 140, trades: 260,
};

function JournalSheet({ history }) {
  const briefs = history?.briefs || [];
  const cellStyle = { padding: "8px 10px", color: T.fg, verticalAlign: "top", borderLeft: `1px solid ${T.line}` };
  return (
    <Panel title="Pre-market journal (spreadsheet)">
      {briefs.length === 0 ? (
        <EmptyNote>No briefs yet.</EmptyNote>
      ) : (
        <div style={{ overflowX: "auto", border: `1px solid ${T.line}`, borderRadius: 8 }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: MONO, fontSize: 12, tableLayout: "fixed" }}>
            <colgroup>
              <col style={{ width: 100 }} />
              {JOURNAL_COLUMNS.map((col) => <col key={col.key} style={{ width: JOURNAL_COL_WIDTH[col.key] }} />)}
            </colgroup>
            <thead>
              <tr style={{ color: T.muted, textAlign: "left", background: T.panel2 }}>
                <th style={{ padding: "8px 10px", fontWeight: 700 }}>Date</th>
                {JOURNAL_COLUMNS.map((col) => (
                  <th key={col.key} style={{ padding: "8px 10px", fontWeight: 700, borderLeft: `1px solid ${T.line}` }}>
                    {col.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {briefs.map((b, i) => (
                <tr key={b.trade_date} style={{ borderTop: `1px solid ${T.line}`, background: i % 2 ? T.panel2 : "transparent" }}>
                  <td style={{ padding: "8px 10px", color: T.fg, fontWeight: 700, verticalAlign: "top" }}>{b.trade_date}</td>
                  {JOURNAL_COLUMNS.map((col) => (
                    <td key={col.key} style={cellStyle}>{col.render(b)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div style={{ height: 8 }} />
      <EmptyNote>
        "Option Chain"/"PCR" fill in once backend.py's PCR tracker persists max call/put OI strike + max pain to pcr_snapshots (not yet). "Participant Option Data" isn't tracked by this engine at all — only futures and cash positioning are, in the columns beside it. "Trades" is a plain support/resistance + structure read, not an options premium target — this engine has no options pricing data to base one on.
      </EmptyNote>
    </Panel>
  );
}

/* ---------- history table ---------- */
function HistoryTable({ history }) {
  const briefs = history?.briefs || [];
  const hitRate = history?.hit_rate_pct;
  return (
    <Panel
      title="Brief history"
      right={hitRate != null ? <Chip color={hitRate >= 50 ? T.put : T.call}>Hit rate: {fmtNum(hitRate, 0)}%</Chip> : null}
    >
      {briefs.length === 0 ? (
        <EmptyNote>No briefs yet.</EmptyNote>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: MONO, fontSize: 12 }}>
            <thead>
              <tr style={{ color: T.muted, textAlign: "left" }}>
                <th style={{ padding: "6px 8px", fontWeight: 500 }}>Date</th>
                <th style={{ padding: "6px 8px", fontWeight: 500 }}>Verdict</th>
                <th style={{ padding: "6px 8px", fontWeight: 500 }}>Score</th>
                <th style={{ padding: "6px 8px", fontWeight: 500 }}>Range</th>
                <th style={{ padding: "6px 8px", fontWeight: 500 }}>Actual open</th>
                <th style={{ padding: "6px 8px", fontWeight: 500 }}>Result</th>
              </tr>
            </thead>
            <tbody>
              {briefs.map((b) => (
                <tr key={b.trade_date} style={{ borderTop: `1px solid ${T.line}` }}>
                  <td style={{ padding: "6px 8px", color: T.fg }}>{b.trade_date}</td>
                  <td style={{ padding: "6px 8px", color: VERDICT_COLOR[b.verdict] || T.fg }}>{b.verdict}</td>
                  <td style={{ padding: "6px 8px", color: directionColor(b.score) }}>{fmtSigned(b.score, 0)}</td>
                  <td style={{ padding: "6px 8px", color: T.muted }}>
                    {b.expected_low != null && b.expected_high != null ? `${fmtNum(b.expected_low, 0)}–${fmtNum(b.expected_high, 0)}` : "—"}
                  </td>
                  <td style={{ padding: "6px 8px", color: T.fg }}>{b.actual_open != null ? fmtNum(b.actual_open, 0) : "—"}</td>
                  <td style={{ padding: "6px 8px" }}>
                    {b.actual_direction == null
                      ? <span style={{ color: T.muted }}>pending</span>
                      : b.hit
                        ? <span style={{ color: T.put }}>✓ hit</span>
                        : <span style={{ color: T.call }}>✗ miss</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

/* ---------- app ---------- */
const GIFT_POLL_MS = 30_000; // scrape cadence — don't hammer niftytrader.in
// NSE participant OI is end-of-day; poll slower than GIFT but often enough
// to pick up a newly published CSV / cash report without hammering NSE.
const POSITIONING_POLL_MS = 5 * 60_000;
const SCORE_POLL_MS = 60_000; // live open-bias score refresh

export default function App() {
  const [brief, setBrief] = useState(null);
  const [history, setHistory] = useState(null);
  const [fiiRows, setFiiRows] = useState([]);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState(null);
  const [showHistory, setShowHistory] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [liveGift, setLiveGift] = useState(null);
  const [giftUpdatedAt, setGiftUpdatedAt] = useState(null);
  const [giftRefreshing, setGiftRefreshing] = useState(false);
  const [livePositioning, setLivePositioning] = useState(null);
  const [positioningUpdatedAt, setPositioningUpdatedAt] = useState(null);
  const [positioningRefreshing, setPositioningRefreshing] = useState(false);
  const [liveScore, setLiveScore] = useState(null);
  const [scoreUpdatedAt, setScoreUpdatedAt] = useState(null);

  const loadBrief = useCallback(async () => {
    try {
      const json = await getJSON("/brief/today");
      setBrief(json);
      setLastUpdated(new Date());
      setErr("");
    } catch (e) {
      setErr(e.message);
    }
  }, []);

  const loadHistoryAndTrend = useCallback(async () => {
    try {
      const [h, f] = await Promise.all([getJSON("/brief/history?days=30"), getJSON("/positioning/fii-trend?days=30")]);
      setHistory(h);
      setFiiRows(f.rows || []);
    } catch (e) {
      setErr(e.message);
    }
  }, []);

  const loadGift = useCallback(async () => {
    setGiftRefreshing(true);
    try {
      const json = await getJSON("/gift");
      setLiveGift(json);
      setGiftUpdatedAt(new Date());
    } catch (e) {
      console.warn("GIFT live refresh failed:", e.message);
    } finally {
      setGiftRefreshing(false);
    }
  }, []);

  const loadPositioning = useCallback(async () => {
    setPositioningRefreshing(true);
    try {
      const json = await getJSON("/positioning/live");
      setLivePositioning(json);
      setPositioningUpdatedAt(new Date());
      if (json.fii_rows && json.fii_rows.length) setFiiRows(json.fii_rows);
    } catch (e) {
      console.warn("NSE positioning refresh failed:", e.message);
    } finally {
      setPositioningRefreshing(false);
    }
  }, []);

  const loadLiveScore = useCallback(async () => {
    try {
      const json = await getJSON("/score/live");
      setLiveScore(json);
      setScoreUpdatedAt(new Date());
      // Keep GIFT panel in sync when the minute score refresh includes a fresher quote.
      if (json.gift?.available) {
        setLiveGift(json.gift);
        setGiftUpdatedAt(new Date());
      }
    } catch (e) {
      console.warn("Live score refresh failed:", e.message);
    }
  }, []);

  useEffect(() => {
    (async () => {
      setLoading(true);
      await Promise.all([loadBrief(), loadHistoryAndTrend(), loadGift(), loadPositioning(), loadLiveScore()]);
      setLoading(false);
    })();
  }, [loadBrief, loadHistoryAndTrend, loadGift, loadPositioning, loadLiveScore]);

  useEffect(() => {
    let timer = null;
    const tick = () => {
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
      loadGift();
    };
    timer = setInterval(tick, GIFT_POLL_MS);
    const onVis = () => {
      if (document.visibilityState === "visible") loadGift();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [loadGift]);

  useEffect(() => {
    let timer = null;
    const tick = () => {
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
      loadPositioning();
    };
    timer = setInterval(tick, POSITIONING_POLL_MS);
    const onVis = () => {
      if (document.visibilityState === "visible") loadPositioning();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [loadPositioning]);

  useEffect(() => {
    let timer = null;
    const tick = () => {
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
      loadLiveScore();
    };
    timer = setInterval(tick, SCORE_POLL_MS);
    const onVis = () => {
      if (document.visibilityState === "visible") loadLiveScore();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [loadLiveScore]);

  const refreshAll = useCallback(async () => {
    setRefreshing(true);
    await Promise.all([loadBrief(), loadHistoryAndTrend(), loadGift(), loadPositioning(), loadLiveScore()]);
    setRefreshing(false);
  }, [loadBrief, loadHistoryAndTrend, loadGift, loadPositioning, loadLiveScore]);

  return (
    <div style={{ minHeight: "100%", background: T.ink, fontFamily: DISP, overflowX: "clip" }}>
      <style>{`
        * { box-sizing: border-box; }
        @media (max-width: 900px) {
          input, select, textarea { font-size: 16px !important; }
          button { min-height: 40px; }
        }
      `}</style>
      <div style={{ maxWidth: 1100, margin: "0 auto", padding: "16px 12px calc(32px + env(safe-area-inset-bottom))", width: "100%" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 20, flexWrap: "wrap", gap: 10 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
            <img src="./icons/apple-touch-icon.png" alt="C34 Exchange" width={40} height={40}
              style={{ borderRadius: 10, flex: "none", display: "block" }} />
            <div style={{ fontSize: 20, fontWeight: 700, color: T.fg }}>Nifty Pre-Market Brief</div>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            {loading && <span style={{ fontSize: 12, color: T.muted }}>Loading…</span>}
            {scoreUpdatedAt && (
              <span style={{ fontFamily: MONO, fontSize: 11, color: T.cyan }}>
                Score {scoreUpdatedAt.toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", second: "2-digit" })} IST
              </span>
            )}
            {giftUpdatedAt && (
              <span style={{ fontFamily: MONO, fontSize: 11, color: T.cyan }}>
                GIFT {giftUpdatedAt.toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", second: "2-digit" })} IST
              </span>
            )}
            {positioningUpdatedAt && (
              <span style={{ fontFamily: MONO, fontSize: 11, color: T.cyan }}>
                NSE OI {positioningUpdatedAt.toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit" })} IST
              </span>
            )}
            {lastUpdated && (
              <span style={{ fontFamily: MONO, fontSize: 11, color: T.muted }}>
                Brief {lastUpdated.toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit" })} IST
              </span>
            )}
            <a href="./index.html"
              style={{ background: T.panel, color: T.fg, border: `1px solid ${T.line}`, borderRadius: 8, padding: "6px 10px", fontSize: 12, textDecoration: "none", fontFamily: DISP }}>
              ← PCR Session Clock
            </a>
          </div>
        </div>

        {err && (
          <div style={{ marginBottom: 16, padding: "10px 14px", background: `${T.call}18`, border: `1px solid ${T.call}55`, borderRadius: 8, color: T.call, fontSize: 13 }}>
            {err}
          </div>
        )}

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 280px), 1fr))", gap: 16 }}>
          <VerdictCard
            brief={brief} liveGift={liveGift} liveScore={liveScore}
            showHistory={showHistory} onToggleHistory={() => setShowHistory((s) => !s)}
            onRefresh={refreshAll} refreshing={refreshing}
          />
          <LiveCuesPanel
            brief={brief} liveGift={liveGift} liveScore={liveScore}
            giftUpdatedAt={giftUpdatedAt} giftRefreshing={giftRefreshing}
          />
          <MacroPanel brief={brief} liveScore={liveScore} />
          <PositioningPanel brief={brief} fiiRows={fiiRows} livePositioning={livePositioning} />
          <ParticipantPanel
            brief={brief} livePositioning={livePositioning} liveScore={liveScore}
            positioningUpdatedAt={positioningUpdatedAt} positioningRefreshing={positioningRefreshing}
          />
          <EventsNewsPanel brief={brief} />
          <LevelsPanel brief={brief} />
          <MoversPanel />
          <Nifty50Panel />
          <PredictivePanel />
          <BreakoutPanel />
          <SetupPanel />
          <BacktestPanel />
          <div style={{ gridColumn: "1 / -1" }}>
            <PaperTradingPanel />
          </div>
          {showHistory && (
            <>
              <div style={{ gridColumn: "1 / -1" }}>
                <JournalSheet history={history} />
              </div>
              <div style={{ gridColumn: "1 / -1" }}>
                <HistoryTable history={history} />
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
