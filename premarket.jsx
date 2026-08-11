import React, { useState, useEffect, useCallback, useMemo } from "react";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceDot,
} from "recharts";

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

function istHM(date) {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(date);
  return {
    h: parseInt(parts.find((p) => p.type === "hour").value, 10),
    m: parseInt(parts.find((p) => p.type === "minute").value, 10),
  };
}
// The brief only actually changes once/day (the morning job runs once at
// 8:15am), but polling 8:00-9:30 IST covers "brief just landed" and
// "market's about to open, want the freshest read" without polling all day
// for a value that isn't moving.
function isPollingWindow(date) {
  const { h, m } = istHM(date);
  const mins = h * 60 + m;
  return mins >= 8 * 60 && mins <= 9 * 60 + 30;
}

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

/* ---------- verdict card ---------- */
function VerdictCard({ brief }) {
  if (!brief || brief.score == null) {
    return (
      <div style={{ gridColumn: "1 / -1", background: T.panel, border: `1px solid ${T.line}`, borderRadius: 16, padding: 24 }}>
        <div style={{ fontFamily: DISP, fontSize: 15, color: T.muted }}>
          {brief?.note || "No brief yet — the morning job runs at 8:15am IST on trading days."}
        </div>
      </div>
    );
  }
  const color = VERDICT_COLOR[brief.verdict] || T.muted;
  const confidence = brief.components?.confidence;
  const missing = brief.components?.missing || [];
  return (
    <div style={{
      gridColumn: "1 / -1", background: T.panel, border: `1px solid ${color}55`, borderRadius: 16, padding: 24,
      display: "flex", alignItems: "center", gap: 24, flexWrap: "wrap",
    }}>
      <div style={{
        width: 108, height: 108, borderRadius: "50%", border: `3px solid ${color}`, display: "flex",
        flexDirection: "column", alignItems: "center", justifyContent: "center", flexShrink: 0,
      }}>
        <div style={{ fontFamily: MONO, fontSize: 26, fontWeight: 700, color }}>{fmtSigned(brief.score, 0)}</div>
        <div style={{ fontFamily: DISP, fontSize: 9, color: T.muted, letterSpacing: 0.6 }}>SCORE</div>
      </div>
      <div style={{ flex: 1, minWidth: 220 }}>
        <div style={{ fontFamily: DISP, fontSize: 22, fontWeight: 700, color: T.fg }}>
          {VERDICT_EMOJI[brief.verdict] || "⚪"} {brief.verdict}
        </div>
        {brief.predicted_open != null && (
          <div style={{ fontFamily: MONO, fontSize: 20, fontWeight: 700, color: T.cyan, marginTop: 8 }}>
            ~{fmtNum(brief.predicted_open, 0)}
            <span style={{ fontFamily: DISP, fontSize: 11, fontWeight: 400, color: T.muted, marginLeft: 8 }}>
              predicted open ({brief.components?.predicted_open_method === "gift_anchored" ? "from GIFT Nifty" : "from score"})
            </span>
          </div>
        )}
        <div style={{ fontFamily: MONO, fontSize: 13, color: T.muted, marginTop: 6 }}>
          Range{" "}
          {brief.expected_low != null && brief.expected_high != null
            ? `${fmtNum(brief.expected_low, 0)} – ${fmtNum(brief.expected_high, 0)}`
            : "unavailable"}
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 10, flexWrap: "wrap" }}>
          {confidence && <Chip color={CONFIDENCE_COLOR[confidence] || T.muted}>Confidence: {confidence}</Chip>}
          {missing.length > 0 && <Chip color={T.amber}>Missing: {missing.join(", ")}</Chip>}
          {brief.components?.is_event_day && <Chip color={T.amber}>Event day — range widened</Chip>}
        </div>
      </div>
      <div style={{ fontFamily: DISP, fontSize: 11, color: T.muted, maxWidth: 260, borderLeft: `1px solid ${T.line}`, paddingLeft: 20 }}>
        {brief.disclaimer || "Automated analysis for information only — not investment advice."}
      </div>
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

function LiveCuesPanel({ brief }) {
  const c = brief?.components || {};
  const gift = c.gift;
  return (
    <Panel title="Live cues">
      <Row
        label="GIFT Nifty"
        value={gift?.price != null ? fmtNum(gift.price, 1) : "unavailable"}
      />
      <Row
        label="GIFT gap vs fair value"
        value={gift?.gap_pct != null ? `${fmtSigned(gift.gap_pct)}%` : "—"}
        color={directionColor(gift?.gap_pct)}
      />
      <div style={{ height: 10 }} />
      {Object.entries(US_LABELS).map(([k, label]) => (
        <QuoteRow key={k} label={label} quote={c.us_quotes?.[k]} />
      ))}
      <div style={{ height: 10 }} />
      {Object.entries(ASIA_LABELS).map(([k, label]) => (
        <QuoteRow key={k} label={label} quote={c.asia_quotes?.[k]} />
      ))}
    </Panel>
  );
}

/* ---------- macro ---------- */
const MACRO_LABELS = { crude: "Crude (Brent)", wti: "WTI", usdinr: "USD/INR", dxy: "DXY", us10y: "US 10Y" };

function MacroPanel({ brief }) {
  const c = brief?.components || {};
  const flags = c.macro?.flags || {};
  const quotes = c.macro_quotes || {};
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

function PositioningPanel({ brief, fiiRows }) {
  const c = brief?.components || {};
  const fii = c.fii;
  const opt = c.option_snapshot || {};
  return (
    <Panel title="Positioning">
      <Row label="FII long/short ratio" value={fii?.ratio != null ? `${fmtNum(fii.ratio, 1)}%` : "unavailable"} />
      {fii?.trend && (
        <div style={{ padding: "6px 0" }}>
          <Chip color={TREND_COLOR[fii.trend] || T.muted}>Trend: {fii.trend}</Chip>
        </div>
      )}
      <FiiSparkline rows={fiiRows || []} />
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

function ParticipantPanel({ brief }) {
  const c = brief?.components || {};
  const participants = c.participants || {};
  const asOf = c.participants_trade_date;
  const cash = c.fii_dii_cash;
  const haveAny = Object.keys(participants).length > 0;

  return (
    <Panel title="Participant OI (NSE)" right={asOf ? <span style={{ fontFamily: MONO, fontSize: 11, color: T.muted }}>as of {asOf}</span> : null}>
      {!haveAny ? (
        <EmptyNote>No participant OI persisted yet — populated by the evening job.</EmptyNote>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: MONO, fontSize: 12 }}>
            <thead>
              <tr style={{ color: T.muted, textAlign: "left" }}>
                <th style={{ padding: "6px 8px", fontWeight: 500 }}>Participant</th>
                <th style={{ padding: "6px 8px", fontWeight: 500 }}>Long</th>
                <th style={{ padding: "6px 8px", fontWeight: 500 }}>Short</th>
                <th style={{ padding: "6px 8px", fontWeight: 500 }}>Long/short %</th>
                <th style={{ padding: "6px 8px", fontWeight: 500 }}>Trend</th>
              </tr>
            </thead>
            <tbody>
              {PARTICIPANT_ORDER.filter((p) => participants[p]).map((p) => (
                <tr key={p} style={{ borderTop: `1px solid ${T.line}` }}>
                  <td style={{ padding: "6px 8px", color: T.fg }}>{p}</td>
                  <td style={{ padding: "6px 8px", color: T.fg }}>{fmtNum(participants[p].long, 0)}</td>
                  <td style={{ padding: "6px 8px", color: T.fg }}>{fmtNum(participants[p].short, 0)}</td>
                  <td style={{ padding: "6px 8px", color: participants[p].ratio != null ? directionColor(participants[p].ratio - 50) : T.muted }}>
                    {participants[p].ratio != null ? `${fmtNum(participants[p].ratio, 1)}%` : "—"}
                  </td>
                  <td style={{ padding: "6px 8px" }}>
                    {participants[p].trend
                      ? <Chip color={TREND_COLOR[participants[p].trend] || T.muted}>{participants[p].trend}</Chip>
                      : <span style={{ color: T.muted }}>—</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div style={{ height: 10 }} />
      <Row label="FII cash" value={cash?.fii_buy != null ? `buy ${fmtNum(cash.fii_buy, 0)} / sell ${fmtNum(cash.fii_sell, 0)}` : "unavailable"}
        color={cash?.fii_buy != null && cash?.fii_sell != null ? directionColor(cash.fii_buy - cash.fii_sell) : undefined} />
      <Row label="DII cash" value={cash?.dii_buy != null ? `buy ${fmtNum(cash.dii_buy, 0)} / sell ${fmtNum(cash.dii_sell, 0)}` : "unavailable"}
        color={cash?.dii_buy != null && cash?.dii_sell != null ? directionColor(cash.dii_buy - cash.dii_sell) : undefined} />
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
export default function App() {
  const [brief, setBrief] = useState(null);
  const [history, setHistory] = useState(null);
  const [fiiRows, setFiiRows] = useState([]);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState(null);
  const [showHistory, setShowHistory] = useState(false);

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

  useEffect(() => {
    (async () => {
      setLoading(true);
      await Promise.all([loadBrief(), loadHistoryAndTrend()]);
      setLoading(false);
    })();
  }, [loadBrief, loadHistoryAndTrend]);

  // Live cues (GIFT/US/Asia quotes, macro, positioning — everything on the
  // brief) refresh every 60s regardless of time of day, not just the
  // 8:00-9:30 IST window: the brief only actually changes once/day via the
  // morning job, but a visitor checking GIFT Nifty or US markets in the
  // evening still wants the page to pick up a fresher read (a manual job
  // trigger, a delayed cron run, etc.) without a manual reload.
  //
  // loadHistoryAndTrend() (the journal sheet + FII sparkline) was
  // previously only ever called once, on mount — neither ever updated
  // again for the rest of the page's life. Included in the same interval
  // now. Note for later: GET /brief/history does one Yahoo fetch per row
  // (for actual_open), so this gets more expensive as history grows past
  // a handful of rows — fine today, worth a dedicated slower interval if
  // it ever becomes noticeably laggy.
  useEffect(() => {
    const id = setInterval(() => {
      loadBrief();
      loadHistoryAndTrend();
    }, 60_000);
    return () => clearInterval(id);
  }, [loadBrief, loadHistoryAndTrend]);

  const inMorningWindow = isPollingWindow(new Date());

  return (
    <div style={{ minHeight: "100%", background: T.ink, fontFamily: DISP }}>
      <div style={{ maxWidth: 1100, margin: "0 auto", padding: "24px 16px 48px" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 20, flexWrap: "wrap", gap: 10 }}>
          <div style={{ fontSize: 20, fontWeight: 700, color: T.fg }}>Nifty Pre-Market Brief</div>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            {loading && <span style={{ fontSize: 12, color: T.muted }}>Loading…</span>}
            {lastUpdated && (
              <span style={{ fontFamily: MONO, fontSize: 11, color: T.muted }}>
                Updated {lastUpdated.toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit" })} IST
              </span>
            )}
            <Chip color={T.put}>● Auto-refreshing (60s){inMorningWindow ? " · pre-open" : ""}</Chip>
            <button onClick={() => setShowHistory((s) => !s)}
              style={{ background: T.panel, color: T.fg, border: `1px solid ${T.line}`, borderRadius: 8, padding: "6px 10px", fontSize: 12, cursor: "pointer", fontFamily: DISP }}>
              {showHistory ? "Hide history" : "Show history"}
            </button>
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

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 16 }}>
          <VerdictCard brief={brief} />
          <LiveCuesPanel brief={brief} />
          <MacroPanel brief={brief} />
          <PositioningPanel brief={brief} fiiRows={fiiRows} />
          <ParticipantPanel brief={brief} />
          <EventsNewsPanel brief={brief} />
          <LevelsPanel brief={brief} />
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
