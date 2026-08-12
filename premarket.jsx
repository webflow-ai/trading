import React, { useState, useEffect, useCallback, useMemo, useRef } from "react";
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

function VerdictCard({ brief, showHistory, onToggleHistory, onRefresh, refreshing }) {
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
  const color = VERDICT_COLOR[brief.verdict] || T.muted;
  const confidence = brief.components?.confidence;
  const missing = brief.components?.missing || [];
  return (
    <div style={{
      gridColumn: "1 / -1", background: T.panel, border: `1px solid ${color}55`, borderRadius: 16, padding: 24,
      display: "flex", alignItems: "center", gap: 24, flexWrap: "wrap", position: "relative",
    }}>
      <CardCornerButtons showHistory={showHistory} onToggleHistory={onToggleHistory} onRefresh={onRefresh} refreshing={refreshing} />
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
      <div style={{ fontFamily: DISP, fontSize: 11, color: T.muted, maxWidth: 220, borderLeft: `1px solid ${T.line}`, paddingLeft: 20, paddingTop: 24, paddingRight: 50 }}>
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

// Live mark-to-market for one open trade, from the option-chain lookup
// built in load() below. Mirrors paper_trading.compute_pnl's sign
// convention (BUY profits above entry, SELL below) but computed client-side
// against a *current*, not final, premium -- unrealized, not stored.
function liveFigures(trade, chainByStrike) {
  const invested = trade.entry_price * trade.lot_size * trade.lots;
  if (trade.status !== "open") {
    return { invested, currentLtp: null, currentValue: null, pnl: trade.pnl, isLive: false };
  }
  const chainRow = chainByStrike[Number(trade.strike)];
  const currentLtp = chainRow ? (trade.option_type === "CE" ? chainRow.ceLtp : chainRow.peLtp) : null;
  if (currentLtp == null) {
    return { invested, currentLtp: null, currentValue: null, pnl: null, isLive: false };
  }
  const direction = trade.action === "BUY" ? 1 : -1;
  const pnl = (currentLtp - trade.entry_price) * direction * trade.lot_size * trade.lots;
  return { invested, currentLtp, currentValue: currentLtp * trade.lot_size * trade.lots, pnl, isLive: true };
}

// Prefers Upstox (real broker LTPs, connected via /api/upstox/login) for
// the second-by-second feed; falls back to the NSE-scrape-backed
// /api/optionchain/today (same one "Fetch live" already used) whenever
// Upstox isn't connected or errors, so the panel still works before/without
// ever connecting Upstox. `source` on the result says which one answered.
async function fetchChain() {
  try {
    const res = await fetch(`${PCR_API_BASE}/upstox/optionchain?symbol=NIFTY`);
    if (res.ok) {
      const json = await res.json();
      if (json.connected && (json.rows || []).length > 0) {
        return { spot: json.spot ?? null, rows: json.rows, source: "upstox" };
      }
    }
  } catch {
    // fall through to the NSE fallback below
  }
  try {
    const res = await fetch(`${PCR_API_BASE}/optionchain/today?symbol=NIFTY&n=50`);
    if (!res.ok) return { spot: null, rows: [], source: null };
    const json = await res.json();
    return { spot: json.spot ?? null, rows: json.rows || [], source: "nse" };
  } catch {
    return { spot: null, rows: [], source: null }; // background tick -- not worth surfacing an error banner for
  }
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
  const [chainByStrike, setChainByStrike] = useState({});
  const [chainSource, setChainSource] = useState(null); // "upstox" | "nse" | null
  const chainFetchInFlight = useRef(false);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [form, setForm] = useState({
    strike: "", optionType: "CE", action: "BUY", lots: 1, lotSize: DEFAULT_LOT_SIZE_FALLBACK, entryPrice: "", notes: "",
  });
  const [fetchingLtp, setFetchingLtp] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [closingDrafts, setClosingDrafts] = useState({});

  const load = useCallback(async () => {
    try {
      const [json, chain] = await Promise.all([getJSON("/paper-trades?days=90"), fetchChain()]);
      setTrades(json.trades || []);
      setSummary(json.summary || null);
      setWeekly(json.weekly || []);
      setChainRows(chain.rows);
      setChainSpot(chain.spot);
      setChainByStrike(chainMapFromRows(chain.rows));
      setChainSource(chain.source);
      setErr("");
    } catch (e) {
      setErr(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const openTrades = trades.filter((t) => t.status === "open");

  // Live option chain + PnL, broker-platform-style: re-pull just the chain
  // (not the whole trades list) every 1s. Upstox (when connected) genuinely
  // updates that fast; the NSE fallback is itself CDN-cached on ~10-15s
  // cycles upstream (see backend.py) so it'll often just re-serve the same
  // numbers between ticks -- harmless, not worth a separate slower interval
  // for the fallback case. The in-flight guard skips a tick if the previous
  // fetch hasn't finished yet, so a slow response can't pile up requests.
  useEffect(() => {
    const id = setInterval(async () => {
      if (chainFetchInFlight.current) return;
      chainFetchInFlight.current = true;
      try {
        const chain = await fetchChain();
        setChainRows(chain.rows);
        setChainSpot(chain.spot);
        setChainByStrike(chainMapFromRows(chain.rows));
        setChainSource(chain.source);
      } finally {
        chainFetchInFlight.current = false;
      }
    }, 1000);
    return () => clearInterval(id);
  }, []);

  const selectStrike = (strike, optionType, ltp) => {
    setForm((f) => ({ ...f, strike: String(strike), optionType, entryPrice: ltp != null ? String(ltp) : f.entryPrice }));
  };

  const unrealizedTotal = openTrades.reduce((sum, t) => {
    const { pnl, isLive } = liveFigures(t, chainByStrike);
    return isLive ? sum + pnl : sum;
  }, 0);
  const investedTotal = openTrades.reduce((sum, t) => sum + liveFigures(t, chainByStrike).invested, 0);
  const currentValueTotal = openTrades.reduce((sum, t) => {
    const { currentValue, isLive } = liveFigures(t, chainByStrike);
    return isLive ? sum + currentValue : sum;
  }, 0);

  const fetchLivePremium = async () => {
    if (!form.strike) return;
    setFetchingLtp(true);
    try {
      const res = await fetch(`${PCR_API_BASE}/optionchain/today?symbol=NIFTY&n=50`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      const row = (json.rows || []).find((r) => Number(r.strike) === Number(form.strike));
      if (!row) {
        setErr(`No live data for strike ${form.strike} — try a strike closer to spot, or type the premium in manually.`);
        return;
      }
      const ltp = form.optionType === "CE" ? row.ceLtp : row.peLtp;
      if (ltp == null) {
        setErr(`Strike ${form.strike} found, but no ${form.optionType} premium in the response.`);
        return;
      }
      setForm((f) => ({ ...f, entryPrice: String(ltp) }));
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
        notes: form.notes || null,
      });
      setForm((f) => ({ ...f, strike: "", entryPrice: "", notes: "" }));
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

  return (
    <Panel
      title="Paper trading journal"
      right={summary && (
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
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
        </div>
      )}
    >
      <form onSubmit={submitTrade} style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "flex-end", marginBottom: 16 }}>
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
                    : "Upstox not connected — falling back to the NSE-derived feed. Visit /api/upstox/login to connect Upstox."}
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
                <th style={tradeThStyle}>Strike</th>
                <th style={tradeThStyle}>Type</th>
                <th style={tradeThStyle}>Action</th>
                <th style={tradeThStyle}>Lots</th>
                <th style={tradeThStyle}>Entry</th>
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
                const { invested, currentLtp, currentValue, pnl, isLive } = liveFigures(t, chainByStrike);
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
                    <td style={tradeTdStyle}>{fmtNum(t.strike, 0)}</td>
                    <td style={tradeTdStyle}>{t.option_type}</td>
                    <td style={tradeTdStyle}>{t.action}</td>
                    <td style={tradeTdStyle}>{t.lots}</td>
                    <td style={tradeTdStyle}>{fmtNum(t.entry_price, 2)}</td>
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
                        <Chip color={T.muted}>closed</Chip>
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
        Simulated trades only — nothing here places a real order. Entry/exit premiums are either typed in or pulled
        from the existing PCR tracker's live option chain (not every strike may be available there); lot size
        defaults to a guess and should be confirmed against NSE's current contract spec before trusting PnL figures.
        Live PnL on open trades refreshes every 5s from that same source — as fast as NSE's own data actually
        updates, not faster.
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
export default function App() {
  const [brief, setBrief] = useState(null);
  const [history, setHistory] = useState(null);
  const [fiiRows, setFiiRows] = useState([]);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState(null);
  const [showHistory, setShowHistory] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

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

  // No auto-refresh (explicitly turned off) — data updates only on load or
  // via the manual refresh button on the verdict card.
  const refreshAll = useCallback(async () => {
    setRefreshing(true);
    await Promise.all([loadBrief(), loadHistoryAndTrend()]);
    setRefreshing(false);
  }, [loadBrief, loadHistoryAndTrend]);

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
          <VerdictCard
            brief={brief} showHistory={showHistory} onToggleHistory={() => setShowHistory((s) => !s)}
            onRefresh={refreshAll} refreshing={refreshing}
          />
          <LiveCuesPanel brief={brief} />
          <MacroPanel brief={brief} />
          <PositioningPanel brief={brief} fiiRows={fiiRows} />
          <ParticipantPanel brief={brief} />
          <EventsNewsPanel brief={brief} />
          <LevelsPanel brief={brief} />
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
