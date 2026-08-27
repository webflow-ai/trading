import React, { useState, useEffect, useMemo, useCallback, useRef } from "react";
import {
  LineChart, Line, Bar, ComposedChart, XAxis, YAxis, CartesianGrid, Tooltip,
  ReferenceLine, ReferenceArea, ReferenceDot, ResponsiveContainer,
} from "recharts";

/* ---------- design tokens ---------- */
const T = {
  ink: "#0A0F1E",
  panel: "#111C33",
  panel2: "#0E1830",
  line: "#22304F",
  fg: "#E6ECF8",
  muted: "#6B7A99",
  cyan: "#34E0C8",
  amber: "#FFC24B",
  put: "#3DDC97",
  call: "#F2789F",
  obBull: "#3B82F6",
  obBear: "#FB923C",
  fvgBull: "#818CF8",
  fvgBear: "#F472B6",
  pattern: "#C084FC",
};
const DISP = "'Space Grotesk', system-ui, sans-serif";
const MONO = "'IBM Plex Mono', ui-monospace, 'SF Mono', monospace";

function useNarrow(bp = 900) {
  const [narrow, setNarrow] = useState(() => typeof window !== "undefined" && window.innerWidth < bp);
  useEffect(() => {
    const onResize = () => setNarrow(window.innerWidth < bp);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [bp]);
  return narrow;
}

const SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY"];
// TradingView won't embed NSE index/futures data on third-party sites (their
// licensing restriction, confirmed — every symbol shows "only available on
// TradingView" in the embedded widget). This just links out to the real
// chart on their site instead, which isn't restricted.
const TV_PAGE_SYMBOL = { NIFTY: "NSE-NIFTY", BANKNIFTY: "NSE-BANKNIFTY", FINNIFTY: "NSE-CNXFINANCE" };

/* ---------- live spot-price chart (our own data, same pipeline as PCR) ---------- */
/* candlestick body+wick, drawn via a custom Bar shape. y/height from Recharts
   already map the [low, high] range to pixels, so open/close positions are
   found by linear-interpolating within that same band. */
function CandleShape(props) {
  const { x, y, width, height, payload } = props;
  const { open, high, low, close } = payload;
  const isUp = close >= open;
  const color = isUp ? T.put : T.call;
  const cx = x + width / 2;

  if (high === low) {
    return <line x1={x} x2={x + width} y1={y + height / 2} y2={y + height / 2} stroke={T.muted} strokeWidth={1} />;
  }

  const valToY = (v) => y + height * (high - v) / (high - low);
  const bodyTop = valToY(Math.max(open, close));
  const bodyBottom = valToY(Math.min(open, close));
  const bodyHeight = Math.max(1, bodyBottom - bodyTop);
  const bodyWidth = Math.max(2, width * 0.82);

  return (
    <g>
      <line x1={cx} x2={cx} y1={y} y2={y + height} stroke={color} strokeWidth={1} />
      <rect x={cx - bodyWidth / 2} y={bodyTop} width={bodyWidth} height={bodyHeight} fill={color} />
    </g>
  );
}

function CandleTooltip({ active, payload }) {
  if (!active || !payload || !payload.length) return null;
  const p = payload[0].payload;
  return (
    <div style={{ background: T.panel2, border: `1px solid ${T.line}`, borderRadius: 8, padding: "8px 10px", fontFamily: MONO }}>
      <div style={{ fontSize: 11, color: T.muted }}>{p.t} IST</div>
      <div style={{ fontSize: 12, color: T.fg }}>
        O <span style={{ color: T.muted }}>{fmt(p.open)}</span>{"  "}
        H <span style={{ color: T.put }}>{fmt(p.high)}</span>{"  "}
        L <span style={{ color: T.call }}>{fmt(p.low)}</span>{"  "}
        C <span style={{ color: T.cyan, fontWeight: 600 }}>{fmt(p.close)}</span>
      </div>
    </div>
  );
}

const CHART_MODES = {
  "1m": { interval: "1m", range: "1d", bucketMinutes: 1, liveMerge: true },
  "5m": { interval: "5m", range: "1d", bucketMinutes: 5, liveMerge: true },
  "15m": { interval: "15m", range: "5d", bucketMinutes: 15, liveMerge: false },
  "1h": { interval: "60m", range: "1mo", bucketMinutes: 60, liveMerge: false },
  "4h": { interval: "4h", range: "3mo", bucketMinutes: 240, liveMerge: false },
};

/* real IST hour/minute via Intl formatting — avoids Date-reconstruction
   timezone bugs entirely, unlike re-parsing a toLocaleString() output */
function istHM(date) {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(date);
  return {
    h: parseInt(parts.find((p) => p.type === "hour").value, 10),
    m: parseInt(parts.find((p) => p.type === "minute").value, 10),
  };
}

// Buckets align to a plain N-minute grid from midnight IST; since market
// open (09:15) is itself a multiple of both 5 and 15, this lines up exactly
// with Yahoo Finance's own candle boundaries, so live ticks merge cleanly
// into the last historical candle instead of drifting off-grid.
function bucketLabel(date, bucketMinutes) {
  const { h, m } = istHM(date);
  const totalMin = Math.floor((h * 60 + m) / bucketMinutes) * bucketMinutes;
  const bh = String(Math.floor(totalMin / 60)).padStart(2, "0");
  const bm = String(totalMin % 60).padStart(2, "0");
  return `${bh}:${bm}`;
}

function secondsToBucketClose(date, bucketMinutes) {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).formatToParts(date);
  const h = parseInt(parts.find((p) => p.type === "hour").value, 10);
  const m = parseInt(parts.find((p) => p.type === "minute").value, 10);
  const s = parseInt(parts.find((p) => p.type === "second").value, 10);
  const bucketSec = bucketMinutes * 60;
  const intoBucket = (h * 3600 + m * 60 + s) % bucketSec;
  return bucketSec - intoBucket;
}

/* ---------- Smart Money Concepts overlay ----------
   Original implementation of the publicly-documented ICT/SMC methodology
   (swing structure, break of structure / change of character, order blocks,
   fair value gaps) — not a port of any proprietary indicator's source, which
   isn't available to work from. Recomputed from whatever candles are on
   screen, so it automatically re-derives itself for every timeframe. */
function computeSMC(candles) {
  const n = candles.length;
  const LOOKBACK = 2; // bars each side for fractal swing detection
  if (n < LOOKBACK * 2 + 3) return { events: [], orderBlocks: [], fvgs: [] };

  const swingAt = new Array(n).fill(null); // 'high' | 'low' | null
  for (let i = LOOKBACK; i < n - LOOKBACK; i++) {
    let isHigh = true, isLow = true;
    for (let j = i - LOOKBACK; j <= i + LOOKBACK; j++) {
      if (j === i) continue;
      if (candles[j].high >= candles[i].high) isHigh = false;
      if (candles[j].low <= candles[i].low) isLow = false;
    }
    if (isHigh) swingAt[i] = "high";
    else if (isLow) swingAt[i] = "low";
  }

  let trend = null; // "up" | "down"
  let swingHigh = null, swingLow = null; // { index, price, broken }
  const events = [];
  const orderBlocks = [];

  for (let i = 0; i < n; i++) {
    const confirmIdx = i - LOOKBACK;
    if (confirmIdx >= 0 && swingAt[confirmIdx] === "high") swingHigh = { index: confirmIdx, price: candles[confirmIdx].high, broken: false };
    if (confirmIdx >= 0 && swingAt[confirmIdx] === "low") swingLow = { index: confirmIdx, price: candles[confirmIdx].low, broken: false };

    const c = candles[i];
    if (swingHigh && !swingHigh.broken && c.close > swingHigh.price) {
      swingHigh.broken = true;
      events.push({ index: i, fromIndex: swingHigh.index, label: trend === "down" ? "CHoCH" : "BOS", bias: "up", price: swingHigh.price });
      trend = "up";
      for (let k = i; k >= 0; k--) {
        if (candles[k].close < candles[k].open) {
          // box marks only the origin candle itself, not extended forward
          orderBlocks.push({ startIndex: k, endIndex: k, type: "bullish", high: candles[k].high, low: candles[k].low });
          break;
        }
      }
    }
    if (swingLow && !swingLow.broken && c.close < swingLow.price) {
      swingLow.broken = true;
      events.push({ index: i, fromIndex: swingLow.index, label: trend === "up" ? "CHoCH" : "BOS", bias: "down", price: swingLow.price });
      trend = "down";
      for (let k = i; k >= 0; k--) {
        if (candles[k].close > candles[k].open) {
          orderBlocks.push({ startIndex: k, endIndex: k, type: "bearish", high: candles[k].high, low: candles[k].low });
          break;
        }
      }
    }
  }

  const fvgs = [];
  for (let i = 2; i < n; i++) {
    const c1 = candles[i - 2], c3 = candles[i];
    // box spans exactly the 3-candle gap that formed it, not extended to mitigation
    if (c1.high < c3.low) {
      fvgs.push({ startIndex: i - 2, endIndex: i, type: "bullish", top: c3.low, bottom: c1.high });
    } else if (c1.low > c3.high) {
      fvgs.push({ startIndex: i - 2, endIndex: i, type: "bearish", top: c1.low, bottom: c3.high });
    }
  }

  // keep it readable — most recent structure only
  return {
    events: events.slice(-5),
    orderBlocks: orderBlocks.slice(-5),
    fvgs: fvgs.slice(-5),
  };
}

/* plain small text label for BOS/CHoCH and pattern markers — no background box */
function StructureLabel({ viewBox, value, color, dy }) {
  if (!viewBox) return null;
  const { x, y } = viewBox;
  return (
    <text x={x} y={y + dy} textAnchor="middle" fontSize={7} fontFamily={MONO} fontWeight={700} fill={color}>
      {value}
    </text>
  );
}

/* live price tag pinned to the right (price-axis) edge — this one keeps a
   filled background on purpose, unlike StructureLabel above, since it needs
   to stay legible sitting directly over the axis/gridlines and is the one
   thing on the chart meant to grab your eye every tick */
function LivePriceTag({ viewBox, value, color }) {
  if (!viewBox) return null;
  const { x, y, width } = viewBox;
  const text = String(value);
  const w = text.length * 6.2 + 10, h = 16;
  const tagX = x + width;
  return (
    <g>
      <rect x={tagX} y={y - h / 2} width={w} height={h} rx={2} fill={color} />
      <text x={tagX + w / 2} y={y + 4} textAnchor="middle" fontSize={10} fontFamily={MONO} fontWeight={700} fill={T.ink}>
        {text}
      </text>
    </g>
  );
}

/* ---------- classic chart-pattern detection ----------
   Well-established, textbook TA patterns (Double Top/Bottom, Head &
   Shoulders / Inverse, Triangles) — not novel invented shapes. Each is only
   surfaced with a measured-move target, and marked "forming" vs "confirmed"
   based on whether price has actually broken the neckline/trendline, since
   an unconfirmed pattern isn't something anyone should trade off. */
function findSwings(candles, lookback = 2) {
  const n = candles.length;
  const swings = [];
  for (let i = lookback; i < n - lookback; i++) {
    let isHigh = true, isLow = true;
    for (let j = i - lookback; j <= i + lookback; j++) {
      if (j === i) continue;
      if (candles[j].high >= candles[i].high) isHigh = false;
      if (candles[j].low <= candles[i].low) isLow = false;
    }
    if (isHigh) swings.push({ index: i, type: "high", price: candles[i].high });
    else if (isLow) swings.push({ index: i, type: "low", price: candles[i].low });
  }
  return swings;
}
function firstCloseBelowAfter(candles, fromIndex, level) {
  for (let k = fromIndex + 1; k < candles.length; k++) if (candles[k].close < level) return k;
  return null;
}
function firstCloseAboveAfter(candles, fromIndex, level) {
  for (let k = fromIndex + 1; k < candles.length; k++) if (candles[k].close > level) return k;
  return null;
}

function computePatterns(candles) {
  const n = candles.length;
  if (n < 12) return [];
  const swings = findSwings(candles, 2);
  const patterns = [];

  // Double Top (H,L,H) / Double Bottom (L,H,L) — twin extremes within ~0.6%
  for (let i = 0; i < swings.length - 2; i++) {
    const [a, b, c] = swings.slice(i, i + 3);
    if (a.type === "high" && b.type === "low" && c.type === "high") {
      const diff = Math.abs(a.price - c.price) / a.price;
      const depth = (Math.min(a.price, c.price) - b.price) / a.price;
      if (diff < 0.01 && depth > 0.002) {
        const neckline = b.price, height = Math.min(a.price, c.price) - neckline;
        const breakIndex = firstCloseBelowAfter(candles, c.index, neckline);
        patterns.push({ type: "Double Top", bias: "down", points: [a, b, c], startIndex: a.index,
          neckline, target: neckline - height, confirmed: breakIndex != null, breakIndex });
      }
    }
    if (a.type === "low" && b.type === "high" && c.type === "low") {
      const diff = Math.abs(a.price - c.price) / a.price;
      const depth = (b.price - Math.max(a.price, c.price)) / a.price;
      if (diff < 0.01 && depth > 0.002) {
        const neckline = b.price, height = neckline - Math.max(a.price, c.price);
        const breakIndex = firstCloseAboveAfter(candles, c.index, neckline);
        patterns.push({ type: "Double Bottom", bias: "up", points: [a, b, c], startIndex: a.index,
          neckline, target: neckline + height, confirmed: breakIndex != null, breakIndex });
      }
    }
  }

  // Head & Shoulders (H,L,H,L,H) / Inverse (L,H,L,H,L) — head clears both shoulders,
  // shoulders within ~1% of each other
  for (let i = 0; i < swings.length - 4; i++) {
    const [s1, t1, h, t2, s2] = swings.slice(i, i + 5);
    if (s1.type === "high" && t1.type === "low" && h.type === "high" && t2.type === "low" && s2.type === "high") {
      const shoulderDiff = Math.abs(s1.price - s2.price) / s1.price;
      const headProminence = (h.price - Math.max(s1.price, s2.price)) / s1.price;
      if (shoulderDiff < 0.015 && headProminence > 0.0025) {
        const neckline = (t1.price + t2.price) / 2, height = h.price - neckline;
        const breakIndex = firstCloseBelowAfter(candles, s2.index, neckline);
        patterns.push({ type: "Head & Shoulders", bias: "down", points: [s1, t1, h, t2, s2], startIndex: s1.index,
          neckline, target: neckline - height, confirmed: breakIndex != null, breakIndex });
      }
    }
    if (s1.type === "low" && t1.type === "high" && h.type === "low" && t2.type === "high" && s2.type === "low") {
      const shoulderDiff = Math.abs(s1.price - s2.price) / s1.price;
      const headProminence = (Math.min(s1.price, s2.price) - h.price) / s1.price;
      if (shoulderDiff < 0.015 && headProminence > 0.0025) {
        const neckline = (t1.price + t2.price) / 2, height = neckline - h.price;
        const breakIndex = firstCloseAboveAfter(candles, s2.index, neckline);
        patterns.push({ type: "Inverse H&S", bias: "up", points: [s1, t1, h, t2, s2], startIndex: s1.index,
          neckline, target: neckline + height, confirmed: breakIndex != null, breakIndex });
      }
    }
  }

  // Triangles / Wedges — trendline through the last few swing highs vs. last
  // few swing lows. Triangle = one line flat, other angled. Wedge = both
  // lines angled the SAME direction while converging (narrowing range).
  const highs = swings.filter((s) => s.type === "high").slice(-4);
  const lows = swings.filter((s) => s.type === "low").slice(-4);
  if (highs.length >= 3 && lows.length >= 3) {
    const hSlope = (highs[highs.length - 1].price - highs[0].price) / (highs[highs.length - 1].index - highs[0].index);
    const lSlope = (lows[lows.length - 1].price - lows[0].price) / (lows[lows.length - 1].index - lows[0].index);
    const lastClose = candles[n - 1].close;
    const flatTol = lastClose * 0.0003;
    const startIndex = Math.min(highs[0].index, lows[0].index);
    const endIndex = Math.max(highs[highs.length - 1].index, lows[lows.length - 1].index);
    const resAt = (k) => highs[0].price + hSlope * (k - highs[0].index);
    const supAt = (k) => lows[0].price + lSlope * (k - lows[0].index);
    const converging = (resAt(endIndex) - supAt(endIndex)) < (resAt(startIndex) - supAt(startIndex)) * 0.85;

    let shapeType = null;
    if (Math.abs(hSlope) < flatTol && lSlope > flatTol) shapeType = "Ascending Triangle";
    else if (Math.abs(lSlope) < flatTol && hSlope < -flatTol) shapeType = "Descending Triangle";
    else if (hSlope < -flatTol && lSlope > flatTol) shapeType = "Symmetrical Triangle";
    else if (hSlope < -flatTol && lSlope < -flatTol && converging) shapeType = "Falling Wedge";
    else if (hSlope > flatTol && lSlope > flatTol && converging) shapeType = "Rising Wedge";

    if (shapeType) {
      const height = resAt(endIndex) - supAt(endIndex);
      let confirmed = false, bias = "neutral", target = null, breakIndex = null;
      for (let k = endIndex + 1; k < n; k++) {
        if (candles[k].close > resAt(k)) { confirmed = true; bias = "up"; target = candles[k].close + height; breakIndex = k; break; }
        if (candles[k].close < supAt(k)) { confirmed = true; bias = "down"; target = candles[k].close - height; breakIndex = k; break; }
      }
      patterns.push({
        type: shapeType, bias, isTriangle: true, startIndex, endIndex,
        highLine: [highs[0], highs[highs.length - 1]], lowLine: [lows[0], lows[lows.length - 1]],
        confirmed, target, breakIndex,
      });
    }
  }

  // Cup & Handle — rounded recovery (left lip -> bottom -> right lip near
  // the same level) followed by a shallow pullback ("handle"), confirmed by
  // a breakout above the lips. The "rounded, not V-shaped" requirement is
  // approximated by requiring the cup to span a meaningful number of bars.
  outer:
  for (let i = 0; i < swings.length - 3; i++) {
    if (swings[i].type !== "high") continue;
    const leftLip = swings[i];
    for (let j = i + 1; j < swings.length - 1; j++) {
      if (swings[j].type !== "low") continue;
      const bottom = swings[j];
      const cupDepth = (leftLip.price - bottom.price) / leftLip.price;
      if (cupDepth < 0.01) continue;
      for (let k = j + 1; k < swings.length; k++) {
        if (swings[k].type !== "high") continue;
        const rightLip = swings[k];
        const lipDiff = Math.abs(rightLip.price - leftLip.price) / leftLip.price;
        if (lipDiff > 0.02) break; // lips diverging — later highs will drift further, stop this branch
        if (rightLip.index - leftLip.index < 10) break; // too fast to read as a rounded cup
        for (let m = k + 1; m < swings.length; m++) {
          if (swings[m].type !== "low") continue;
          const handle = swings[m];
          const handleDepth = (rightLip.price - handle.price) / rightLip.price;
          const handleBars = handle.index - rightLip.index;
          if (handleDepth < 0.001 || handleDepth > cupDepth * 0.5) break;
          if (handleBars > (rightLip.index - leftLip.index) * 0.5) break;
          const neckline = Math.max(leftLip.price, rightLip.price);
          const breakIndex = firstCloseAboveAfter(candles, handle.index, neckline);
          patterns.push({
            type: "Cup & Handle", bias: "up", points: [leftLip, bottom, rightLip, handle], startIndex: leftLip.index,
            neckline, target: neckline + (neckline - bottom.price), confirmed: breakIndex != null, breakIndex,
          });
          break outer; // one cup at a time keeps the chart readable
        }
        break;
      }
    }
  }

  // Bull Flag — a sharp impulsive rally (the "pole") followed by a tight,
  // shallow consolidation (the "flag") that doesn't give back more than
  // ~60% of the pole, then a continuation breakout above the flag.
  const POLE_WINDOW = 6, POLE_MIN_MOVE = 0.012;
  for (let poleStart = 0; poleStart < n - POLE_WINDOW - 2; poleStart++) {
    let poleLow = candles[poleStart].low, poleLowIdx = poleStart;
    let peakIdx = poleStart, peakPrice = candles[poleStart].high;
    for (let k = poleStart; k <= Math.min(poleStart + POLE_WINDOW, n - 1); k++) {
      if (candles[k].low < poleLow) { poleLow = candles[k].low; poleLowIdx = k; }
      if (candles[k].high > peakPrice) { peakPrice = candles[k].high; peakIdx = k; }
    }
    const poleBars = peakIdx - poleLowIdx;
    if (poleBars < 2 || (peakPrice - poleLow) / poleLow < POLE_MIN_MOVE) continue;
    const poleHeight = peakPrice - poleLow;

    let flagLow = peakPrice, flagHigh = peakPrice, flagEnd = peakIdx;
    const flagMaxBars = poleBars * 4;
    for (let k = peakIdx + 1; k < Math.min(peakIdx + 1 + flagMaxBars, n); k++) {
      if (candles[k].low < peakPrice - poleHeight * 0.6) break; // gave back too much of the pole
      flagLow = Math.min(flagLow, candles[k].low);
      flagHigh = Math.max(flagHigh, candles[k].high);
      flagEnd = k;
    }
    const flagBars = flagEnd - peakIdx, flagRange = flagHigh - flagLow;
    if (flagBars < 2 || flagRange > poleHeight * 0.6) continue;

    const breakoutLevel = flagHigh;
    const breakIndex = firstCloseAboveAfter(candles, flagEnd, breakoutLevel);
    patterns.push({
      type: "Bull Flag", bias: "up", isFlag: true,
      poleStart: poleLowIdx, poleEnd: peakIdx, flagStart: peakIdx, flagEnd,
      top: flagHigh, bottom: flagLow, startIndex: poleLowIdx,
      neckline: breakoutLevel, target: breakoutLevel + poleHeight, confirmed: breakIndex != null, breakIndex,
    });
    poleStart = flagEnd; // skip past this flag instead of re-detecting overlapping variants
  }

  return patterns.slice(-4); // most recent only — stay readable
}

function SpotChart({ backendUrl, symbol }) {
  const [mode, setMode] = useState("5m");
  const [candles, setCandles] = useState([]);
  const [err, setErr] = useState("");
  const [closeIn, setCloseIn] = useState(null);
  const [zoom, setZoom] = useState(1); // multiplier on px-per-candle (time axis)
  const [priceZoom, setPriceZoom] = useState(1); // multiplier on visible price range (Y axis)
  const [showSMC, setShowSMC] = useState(true);
  const [showPatterns, setShowPatterns] = useState(true);
  const cfg = CHART_MODES[mode];
  const smc = useMemo(() => (showSMC ? computeSMC(candles) : { events: [], orderBlocks: [], fvgs: [] }), [candles, showSMC]);
  const patterns = useMemo(() => (showPatterns ? computePatterns(candles) : []), [candles, showPatterns]);
  const scrollRef = React.useRef(null);
  const stuckToEndRef = React.useRef(true); // auto-follow the live edge unless the user scrolls away
  const [viewport, setViewport] = useState({ scrollLeft: 0, clientWidth: 0 });
  const priceDragRef = React.useRef(null);

  const measureViewport = () => {
    const el = scrollRef.current;
    if (!el) return;
    setViewport({ scrollLeft: el.scrollLeft, clientWidth: el.clientWidth });
  };
  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    stuckToEndRef.current = el.scrollWidth - el.clientWidth - el.scrollLeft < 20;
    measureViewport();
  };
  const zoomIn = () => setZoom((z) => Math.min(5, +(z * 1.4).toFixed(2)));
  const zoomOut = () => setZoom((z) => Math.max(0.3, +(z / 1.4).toFixed(2)));
  // No real ceiling — just guarding against literal 0/Infinity, not an intended UX limit
  const priceZoomIn = () => setPriceZoom((z) => Math.min(1000, +(z * 1.25).toFixed(3)));
  const priceZoomOut = () => setPriceZoom((z) => Math.max(0.001, +(z / 1.25).toFixed(3)));
  const priceZoomReset = () => setPriceZoom(1);

  // TradingView-style drag-on-the-price-scale: drag up narrows the visible
  // price range (zoom in), drag down widens it (zoom out).
  const onAxisPointerDown = (e) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    priceDragRef.current = { startY: e.clientY, startZoom: priceZoom };
  };
  const onAxisPointerMove = (e) => {
    if (!priceDragRef.current) return;
    const dy = e.clientY - priceDragRef.current.startY;
    const next = Math.min(1000, Math.max(0.001, priceDragRef.current.startZoom * Math.exp(-dy * 0.01)));
    setPriceZoom(+next.toFixed(2));
  };
  const onAxisPointerUp = () => { priceDragRef.current = null; };

  const loadHistory = useCallback(async () => {
    if (!backendUrl) { setErr("No backend connected"); return; }
    try {
      const base = backendUrl.replace(/\/$/, "");
      const qs = new URLSearchParams({ symbol, interval: cfg.interval, range: cfg.range });
      const res = await fetch(`${base}/api/candles?${qs.toString()}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      setCandles(json.candles || []);
      setErr(json.candles && json.candles.length ? "" : "No historical candles available");
    } catch (e) {
      setErr(e.message);
    }
  }, [backendUrl, symbol, cfg.interval, cfg.range]);

  const mergeLiveTick = useCallback(async () => {
    if (!backendUrl || !cfg.liveMerge) return;
    if (!marketStatus().open) return; // frozen at the close — NSE keeps serving the last
    // snapshot with a *fresh* timestamp even after hours, which would otherwise create
    // phantom flat candles marching forward at the current wall-clock time.
    try {
      const base = backendUrl.replace(/\/$/, "");
      const res = await fetch(`${base}/api/optionchain/today?symbol=${symbol}&n=1`);
      if (!res.ok) return;
      const json = await res.json();
      if (json.spot == null) return;
      const now = json.updatedAt ? new Date(json.updatedAt) : new Date();
      const label = bucketLabel(now, cfg.bucketMinutes);
      const spot = json.spot;
      setCandles((prev) => {
        const last = prev[prev.length - 1];
        if (last && last.t === label) {
          if (last.close === spot) return prev; // no new tick
          const updated = { ...last, high: Math.max(last.high, spot), low: Math.min(last.low, spot), close: spot };
          return [...prev.slice(0, -1), updated];
        }
        if (last && last.t > label) return prev; // stale response, ignore
        return [...prev, { t: label, open: spot, high: spot, low: spot, close: spot }].slice(-150);
      });
    } catch {
      /* live tail merge is best-effort; historical candles still stand */
    }
  }, [backendUrl, symbol, cfg.liveMerge, cfg.bucketMinutes]);

  useEffect(() => {
    stuckToEndRef.current = true; // fresh timeframe/symbol — snap to the live edge
    loadHistory();
  }, [loadHistory]);

  useEffect(() => {
    // Recharts' ResponsiveContainer resizes via ResizeObserver, which fires
    // a frame or two AFTER this effect would normally run — snapping scroll
    // position immediately here can race ahead of the chart's own redraw,
    // showing stale candle positions for a moment. A double rAF waits for
    // that layout pass to actually settle first.
    let raf2;
    const raf1 = requestAnimationFrame(() => {
      raf2 = requestAnimationFrame(() => {
        if (scrollRef.current && stuckToEndRef.current) {
          scrollRef.current.scrollLeft = scrollRef.current.scrollWidth;
        }
        measureViewport();
      });
    });
    return () => {
      cancelAnimationFrame(raf1);
      if (raf2) cancelAnimationFrame(raf2);
    };
  }, [candles, zoom]);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    measureViewport();
    const onResize = () => measureViewport();
    window.addEventListener("resize", onResize);

    // Pinch-to-zoom needs a non-passive native listener (React's synthetic
    // touch handlers are passive by default, so preventDefault there won't
    // stop the browser's own page-zoom gesture) — pan-x still lets a single
    // finger scroll the container natively via CSS touch-action below.
    let pinchStart = null;
    const dist = (touches) => Math.hypot(touches[0].clientX - touches[1].clientX, touches[0].clientY - touches[1].clientY);
    const onTouchStart = (e) => {
      if (e.touches.length === 2) pinchStart = { dist: dist(e.touches), zoom };
    };
    const onTouchMove = (e) => {
      if (e.touches.length === 2 && pinchStart) {
        e.preventDefault();
        const ratio = dist(e.touches) / pinchStart.dist;
        setZoom(Math.min(5, Math.max(0.3, +(pinchStart.zoom * ratio).toFixed(2))));
      }
    };
    const onTouchEnd = (e) => {
      if (e.touches.length < 2) pinchStart = null;
    };
    el.addEventListener("touchstart", onTouchStart, { passive: true });
    el.addEventListener("touchmove", onTouchMove, { passive: false });
    el.addEventListener("touchend", onTouchEnd, { passive: true });

    // Mouse-wheel over the chart zooms the price scale — the container has
    // overflowY:hidden so vertical wheel wouldn't otherwise do anything,
    // making this a free gesture with nothing to conflict with.
    const onWheel = (e) => {
      if (Math.abs(e.deltaY) < 1) return;
      e.preventDefault();
      const factor = e.deltaY < 0 ? 1.1 : 1 / 1.1;
      setPriceZoom((z) => Math.min(1000, Math.max(0.001, +(z * factor).toFixed(3))));
    };
    el.addEventListener("wheel", onWheel, { passive: false });

    return () => {
      window.removeEventListener("resize", onResize);
      el.removeEventListener("touchstart", onTouchStart);
      el.removeEventListener("touchmove", onTouchMove);
      el.removeEventListener("touchend", onTouchEnd);
      el.removeEventListener("wheel", onWheel);
    };
  }, [zoom]);

  useEffect(() => {
    // fine-grained timeframes: live tail from our own NSE spot-price poll.
    // coarse timeframes: too wide for live merging to matter — just re-pull.
    const id = setInterval(cfg.liveMerge ? mergeLiveTick : loadHistory, cfg.liveMerge ? 3000 : 60000);
    return () => clearInterval(id);
  }, [cfg.liveMerge, mergeLiveTick, loadHistory]);

  // Client-side-only countdown to the current candle's close — no network
  // calls, just a 1-second UI tick against the same bucket-alignment logic
  // used to merge live ticks.
  useEffect(() => {
    if (!cfg.liveMerge) { setCloseIn(null); return; }
    const tick = () => {
      if (!marketStatus().open) { setCloseIn(null); return; } // nothing left to count down to once shut
      setCloseIn(secondsToBucketClose(new Date(), cfg.bucketMinutes));
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [cfg.liveMerge, cfg.bucketMinutes]);

  const pxPerCandle = 10 * zoom;
  const chartWidth = Math.max(600, candles.length * pxPerCandle); // fixed px/candle so history stays scrollable, not squeezed
  const lastIdx = candles.length - 1;

  // Price axis autoscales to whatever's actually visible in the scrolled/zoomed
  // viewport — like a real charting tool — instead of the whole history's range.
  const visStart = viewport.clientWidth
    ? Math.max(0, Math.floor(viewport.scrollLeft / pxPerCandle) - 1)
    : 0;
  const visEnd = viewport.clientWidth
    ? Math.min(lastIdx, Math.ceil((viewport.scrollLeft + viewport.clientWidth) / pxPerCandle) + 1)
    : lastIdx;
  const visibleCandles = candles.slice(visStart, visEnd + 1);
  const highs = (visibleCandles.length ? visibleCandles : candles).map((c) => c.high);
  const lows = (visibleCandles.length ? visibleCandles : candles).map((c) => c.low);
  const yPad = highs.length ? (Math.max(...highs) - Math.min(...lows)) * 0.08 || 5 : 5;
  const yMinFit = lows.length ? Math.min(...lows) - yPad : 0;
  const yMaxFit = highs.length ? Math.max(...highs) + yPad : 100;
  // priceZoom scales the fitted range around its own center — > 1 narrows
  // it (zoomed in on price), < 1 widens it (zoomed out).
  const yCenter = (yMinFit + yMaxFit) / 2;
  const yHalfRange = (yMaxFit - yMinFit) / 2 / priceZoom;
  const yMin = yCenter - yHalfRange;
  const yMax = yCenter + yHalfRange;
  const last = candles[candles.length - 1];
  const first = candles[0];
  const delta = last && first ? last.close - first.open : null;
  const tvUrl = `https://www.tradingview.com/symbols/${TV_PAGE_SYMBOL[symbol] || `NSE-${symbol}`}/`;

  return (
    <div style={{ background: T.panel, border: `1px solid ${T.line}`, borderRadius: 14, padding: "12px 8px 8px", marginTop: 12 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "0 8px 10px", flexWrap: "wrap", gap: 6 }}>
        <div style={{ fontFamily: DISP, fontSize: 12, fontWeight: 600 }}>
          Spot Price <span style={{ color: T.muted, fontWeight: 400 }}>· {cfg.bucketMinutes}-min candles</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          {last && (
            <span style={{ fontFamily: MONO, fontSize: 13, color: T.fg, fontWeight: 600 }}>
              {fmt(last.close, 2)}
              {delta != null && (
                <span style={{ fontSize: 11, marginLeft: 6, color: delta >= 0 ? T.put : T.call }}>
                  {delta >= 0 ? "▲" : "▼"} {fmt(Math.abs(delta))}
                </span>
              )}
            </span>
          )}
          {closeIn != null && (
            <span style={{
              fontFamily: MONO, fontSize: 11, color: T.amber, border: `1px solid ${T.amber}44`,
              borderRadius: 999, padding: "3px 9px", whiteSpace: "nowrap",
            }}>
              closes in {Math.floor(closeIn / 60)}:{String(closeIn % 60).padStart(2, "0")}
            </span>
          )}
          {cfg.liveMerge && !marketStatus().open && (
            <span style={{
              fontFamily: MONO, fontSize: 11, color: T.muted, border: `1px solid ${T.line}`,
              borderRadius: 999, padding: "3px 9px", whiteSpace: "nowrap",
            }}>
              ● Market closed — showing last close
            </span>
          )}
          <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
            {Object.keys(CHART_MODES).map((k) => (
              <button key={k} onClick={() => setMode(k)}
                style={{
                  padding: "4px 10px", borderRadius: 6, cursor: "pointer", fontFamily: MONO, fontSize: 11,
                  background: mode === k ? T.cyan : T.panel2, color: mode === k ? T.ink : T.muted,
                  border: `1px solid ${mode === k ? T.cyan : T.line}`,
                }}>
                {k}
              </button>
            ))}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
            <button onClick={zoomOut} title="Zoom out"
              style={{
                width: 30, height: 30, borderRadius: 6, cursor: "pointer", fontFamily: MONO, fontSize: 15,
                background: T.panel2, color: T.muted, border: `1px solid ${T.line}`, lineHeight: 1,
              }}>
              −
            </button>
            <span style={{ fontFamily: MONO, fontSize: 10, color: T.muted, minWidth: 32, textAlign: "center" }}>
              {Math.round(zoom * 100)}%
            </span>
            <button onClick={zoomIn} title="Zoom in"
              style={{
                width: 30, height: 30, borderRadius: 6, cursor: "pointer", fontFamily: MONO, fontSize: 15,
                background: T.panel2, color: T.muted, border: `1px solid ${T.line}`, lineHeight: 1,
              }}>
              +
            </button>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 4 }} title="Price scale zoom (or drag the right edge of the chart, or scroll wheel over it)">
            <button onClick={priceZoomOut} title="Zoom price out"
              style={{
                width: 30, height: 30, borderRadius: 6, cursor: "pointer", fontFamily: MONO, fontSize: 15,
                background: T.panel2, color: T.muted, border: `1px solid ${T.line}`, lineHeight: 1,
              }}>
              ⇕−
            </button>
            <span style={{ fontFamily: MONO, fontSize: 10, color: T.muted, minWidth: 32, textAlign: "center" }}>
              {Math.round(priceZoom * 100)}%
            </span>
            <button onClick={priceZoomIn} title="Zoom price in"
              style={{
                width: 30, height: 30, borderRadius: 6, cursor: "pointer", fontFamily: MONO, fontSize: 15,
                background: T.panel2, color: T.muted, border: `1px solid ${T.line}`, lineHeight: 1,
              }}>
              ⇕+
            </button>
          </div>
          <button onClick={() => setShowSMC((s) => !s)} title="Toggle Smart Money Concepts overlay"
            style={{
              padding: "4px 10px", borderRadius: 6, cursor: "pointer", fontFamily: MONO, fontSize: 11,
              background: showSMC ? T.amber : T.panel2, color: showSMC ? T.ink : T.muted,
              border: `1px solid ${showSMC ? T.amber : T.line}`,
            }}>
            SMC
          </button>
          <button onClick={() => setShowPatterns((s) => !s)} title="Toggle classic chart-pattern detection"
            style={{
              padding: "4px 10px", borderRadius: 6, cursor: "pointer", fontFamily: MONO, fontSize: 11,
              background: showPatterns ? T.pattern : T.panel2, color: showPatterns ? T.ink : T.muted,
              border: `1px solid ${showPatterns ? T.pattern : T.line}`,
            }}>
            Patterns
          </button>
          <a href={tvUrl} target="_blank" rel="noopener noreferrer"
            style={{
              fontFamily: MONO, fontSize: 11, color: T.cyan, textDecoration: "none",
              border: `1px solid ${T.cyan}44`, borderRadius: 999, padding: "3px 9px",
            }}>
            Open on TradingView ↗
          </a>
        </div>
      </div>
      <div style={{ position: "relative" }}>
        <div ref={scrollRef} onScroll={handleScroll} className="spot-scroll" style={{
          height: 280, overflowX: "auto", overflowY: "hidden",
          touchAction: "pan-x", WebkitOverflowScrolling: "touch",
        }}>
          <div style={{ width: chartWidth, height: "100%" }}>
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={candles} margin={{ top: 4, right: 14, bottom: 4, left: -8 }}>
                <CartesianGrid stroke={T.line} strokeDasharray="2 4" vertical={false} />
                <XAxis dataKey="t" tick={{ fill: T.muted, fontSize: 10, fontFamily: MONO }}
                  tickLine={false} axisLine={{ stroke: T.line }} interval="preserveStartEnd" minTickGap={40} />
                <YAxis domain={[yMin, yMax]} orientation="right" tick={{ fill: T.muted, fontSize: 10, fontFamily: MONO }}
                  tickLine={false} axisLine={false} width={54} tickFormatter={(v) => v.toFixed(0)} />
              <Tooltip content={<CandleTooltip />} />
              {smc.orderBlocks.map((ob, i) => {
                const color = ob.type === "bullish" ? T.obBull : T.obBear;
                return (
                  <ReferenceArea key={`ob${i}`}
                    x1={candles[ob.startIndex].t} x2={candles[ob.endIndex].t} y1={ob.low} y2={ob.high}
                    fill={color} fillOpacity={0.16} stroke={color} strokeOpacity={0.45} strokeWidth={1}
                    ifOverflow="extendDomain"
                    label={{ value: "OB", position: "insideTopLeft", fill: color, fontSize: 6, fontFamily: MONO, fontWeight: 700 }} />
                );
              })}
              {smc.fvgs.map((f, i) => {
                const color = f.type === "bullish" ? T.fvgBull : T.fvgBear;
                return (
                  <ReferenceArea key={`fvg${i}`}
                    x1={candles[f.startIndex].t} x2={candles[f.endIndex].t} y1={f.bottom} y2={f.top}
                    fill={color} fillOpacity={0.12} stroke={color} strokeOpacity={0.4} strokeDasharray="2 2"
                    ifOverflow="extendDomain"
                    label={{ value: "FVG", position: "insideTopLeft", fill: color, fontSize: 6, fontFamily: MONO, fontWeight: 700 }} />
                );
              })}
              <Bar dataKey={(c) => [c.low, c.high]} shape={CandleShape} isAnimationActive={false} />
              {cfg.liveMerge && last && (
                <ReferenceLine y={last.close} stroke={delta >= 0 ? T.put : T.call} strokeOpacity={0.6} strokeDasharray="3 3" ifOverflow="extendDomain"
                  label={(props) => <LivePriceTag {...props} value={fmt(last.close)} color={delta >= 0 ? T.put : T.call} />} />
              )}
              {smc.events.map((ev, i) => {
                const color = ev.bias === "up" ? T.put : T.call;
                return (
                  <React.Fragment key={`ev${i}`}>
                    <ReferenceLine
                      segment={[{ x: candles[ev.fromIndex].t, y: ev.price }, { x: candles[ev.index].t, y: ev.price }]}
                      stroke={color} strokeOpacity={0.75} strokeDasharray="3 3" strokeWidth={1} ifOverflow="extendDomain" />
                    <ReferenceDot x={candles[ev.index].t} y={ev.price} r={0} isFront
                      label={(props) => <StructureLabel {...props} value={ev.label} color={color} dy={ev.bias === "up" ? -6 : 11} />} />
                  </React.Fragment>
                );
              })}
              {patterns.map((p, i) => {
                const color = T.pattern;
                const opacity = p.confirmed ? 0.9 : 0.4;
                const labelText = p.type + (p.confirmed ? "" : " (forming)");
                if (p.isTriangle) {
                  const [h1, h2] = p.highLine, [l1, l2] = p.lowLine;
                  return (
                    <React.Fragment key={`pat${i}`}>
                      <ReferenceLine segment={[{ x: candles[h1.index].t, y: h1.price }, { x: candles[h2.index].t, y: h2.price }]}
                        stroke={color} strokeOpacity={opacity} strokeWidth={1.3} ifOverflow="extendDomain" />
                      <ReferenceLine segment={[{ x: candles[l1.index].t, y: l1.price }, { x: candles[l2.index].t, y: l2.price }]}
                        stroke={color} strokeOpacity={opacity} strokeWidth={1.3} ifOverflow="extendDomain" />
                      {p.confirmed && p.breakIndex != null && (
                        <ReferenceLine segment={[{ x: candles[p.breakIndex].t, y: p.target }, { x: candles[lastIdx].t, y: p.target }]}
                          stroke={color} strokeDasharray="2 2" strokeOpacity={0.55} ifOverflow="extendDomain" />
                      )}
                      <ReferenceDot x={candles[p.endIndex].t} y={(h2.price + l2.price) / 2} r={0} isFront
                        label={(props) => <StructureLabel {...props} value={labelText} color={color} dy={-6} />} />
                    </React.Fragment>
                  );
                }
                if (p.isFlag) {
                  return (
                    <React.Fragment key={`pat${i}`}>
                      <ReferenceLine
                        segment={[{ x: candles[p.poleStart].t, y: candles[p.poleStart].low }, { x: candles[p.poleEnd].t, y: candles[p.poleEnd].high }]}
                        stroke={color} strokeOpacity={opacity} strokeWidth={1.5} ifOverflow="extendDomain" />
                      <ReferenceArea
                        x1={candles[p.flagStart].t} x2={candles[p.flagEnd].t} y1={p.bottom} y2={p.top}
                        fill={color} fillOpacity={0.1} stroke={color} strokeOpacity={opacity} strokeDasharray="3 2" ifOverflow="extendDomain" />
                      {p.confirmed && (
                        <ReferenceLine segment={[{ x: candles[p.breakIndex].t, y: p.target }, { x: candles[lastIdx].t, y: p.target }]}
                          stroke={color} strokeDasharray="2 2" strokeOpacity={0.55} ifOverflow="extendDomain" />
                      )}
                      <ReferenceDot x={candles[p.flagEnd].t} y={p.top} r={0} isFront
                        label={(props) => <StructureLabel {...props} value={labelText} color={color} dy={-6} />} />
                    </React.Fragment>
                  );
                }
                const lastPt = p.points[p.points.length - 1];
                return (
                  <React.Fragment key={`pat${i}`}>
                    {p.points.slice(0, -1).map((pt, j) => (
                      <ReferenceLine key={`out${j}`}
                        segment={[{ x: candles[pt.index].t, y: pt.price }, { x: candles[p.points[j + 1].index].t, y: p.points[j + 1].price }]}
                        stroke={color} strokeOpacity={opacity} strokeWidth={1.5} ifOverflow="extendDomain" />
                    ))}
                    {p.points.map((pt, j) => (
                      <ReferenceDot key={`pt${j}`} x={candles[pt.index].t} y={pt.price} r={3}
                        fill={color} fillOpacity={opacity} stroke="none" ifOverflow="extendDomain" />
                    ))}
                    <ReferenceLine segment={[{ x: candles[p.points[0].index].t, y: p.neckline }, { x: candles[lastPt.index].t, y: p.neckline }]}
                      stroke={color} strokeDasharray="4 2" strokeOpacity={opacity} ifOverflow="extendDomain" />
                    {p.confirmed && (
                      <ReferenceLine segment={[{ x: candles[p.breakIndex].t, y: p.target }, { x: candles[lastIdx].t, y: p.target }]}
                        stroke={color} strokeDasharray="2 2" strokeOpacity={0.55} ifOverflow="extendDomain" />
                    )}
                    <ReferenceDot x={candles[lastPt.index].t} y={lastPt.price} r={0} isFront
                      label={(props) => <StructureLabel {...props} value={labelText} color={color} dy={p.bias === "down" ? -6 : 11} />} />
                  </React.Fragment>
                );
              })}
            </ComposedChart>
          </ResponsiveContainer>
          </div>
        </div>
        {/* fixed strip over the price scale — drag vertically to zoom price,
            double-click/tap to reset to auto-fit, mirroring TradingView */}
        <div
          onPointerDown={onAxisPointerDown} onPointerMove={onAxisPointerMove}
          onPointerUp={onAxisPointerUp} onPointerCancel={onAxisPointerUp}
          onDoubleClick={priceZoomReset}
          title="Drag to zoom price · double-click to reset"
          style={{
            position: "absolute", right: 0, top: 0, width: 54, height: 280,
            cursor: "ns-resize", touchAction: "none",
          }}
        />
      </div>
      {!candles.length && !err && (
        <div style={{ fontFamily: MONO, fontSize: 11, color: T.muted, textAlign: "center", padding: 16 }}>Loading historical candles…</div>
      )}
      {!!candles.length && (
        <div style={{ fontFamily: MONO, fontSize: 10, color: T.muted, textAlign: "center", padding: "4px 0 0" }}>
          ← scroll for history · {candles.length} candles
        </div>
      )}
      {showSMC && !!candles.length && (
        <div style={{
          display: "flex", flexWrap: "wrap", gap: "4px 12px", justifyContent: "center",
          fontFamily: MONO, fontSize: 9, color: T.muted, padding: "6px 8px 2px",
        }}>
          <span><span style={{ color: T.obBull }}>■</span> bullish OB</span>
          <span><span style={{ color: T.obBear }}>■</span> bearish OB</span>
          <span><span style={{ color: T.fvgBull }}>▨</span> bullish FVG</span>
          <span><span style={{ color: T.fvgBear }}>▨</span> bearish FVG</span>
          <span><span style={{ color: T.put }}>┈</span>/<span style={{ color: T.call }}>┈</span> BOS / CHoCH at structure breaks</span>
        </div>
      )}
      {showPatterns && !!patterns.length && (
        <div style={{ padding: "6px 8px 2px", display: "flex", flexDirection: "column", gap: 4 }}>
          {patterns.map((p, i) => (
            <div key={i} style={{
              display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8,
              fontFamily: MONO, fontSize: 10, padding: "4px 8px", borderRadius: 6,
              background: T.panel2, border: `1px solid ${T.pattern}33`,
            }}>
              <span style={{ color: T.pattern, fontWeight: 600 }}>
                {p.type}
                <span style={{ color: T.muted, fontWeight: 400 }}> · {p.confirmed ? "confirmed" : "forming"}</span>
              </span>
              <span style={{ color: p.bias === "up" ? T.put : p.bias === "down" ? T.call : T.muted }}>
                {p.bias === "up" ? "▲ bullish" : p.bias === "down" ? "▼ bearish" : "neutral"}
                {p.confirmed && p.target != null && <span style={{ color: T.fg }}> · target {fmt(p.target)}</span>}
              </span>
            </div>
          ))}
        </div>
      )}
      {showPatterns && !!candles.length && !patterns.length && (
        <div style={{ fontFamily: MONO, fontSize: 10, color: T.muted, textAlign: "center", padding: "6px 8px 0" }}>
          No reliable pattern in view right now — price action doesn't currently form a clean double top/bottom, head & shoulders, or triangle at this timeframe.
        </div>
      )}
      {err && <div style={{ fontFamily: MONO, fontSize: 10, color: T.call, padding: "6px 8px 0" }}>{err}</div>}
    </div>
  );
}

/* ---------- helpers ---------- */
function istNow() {
  const s = new Date().toLocaleString("en-US", { timeZone: "Asia/Kolkata" });
  return new Date(s);
}
function marketStatus() {
  const d = istNow();
  const day = d.getDay(); // 0 Sun .. 6 Sat
  const mins = d.getHours() * 60 + d.getMinutes();
  const open = 9 * 60 + 15, close = 15 * 60 + 30;
  if (day === 0 || day === 6) return { open: false, label: "Weekend" };
  if (mins < open) return { open: false, label: "Pre-open" };
  if (mins > close) return { open: false, label: "Closed" };
  return { open: true, label: "Live" };
}
function fmt(v, d = 2) {
  return v == null || isNaN(v) ? "—" : Number(v).toFixed(d);
}
function fmtOi(v) {
  if (v == null || isNaN(v)) return "—";
  if (v >= 1e7) return (v / 1e7).toFixed(2) + " Cr";
  if (v >= 1e5) return (v / 1e5).toFixed(2) + " L";
  if (v >= 1e3) return (v / 1e3).toFixed(1) + " K";
  return String(v);
}

/* ---------- flexible parser for a real backend response ----------
   The serverless backend has no background poller, so /api/pcr/today
   returns just the current reading, not a stored history — this reads a
   single snapshot out of whatever shape comes back (still tolerant of
   either naming convention) instead of an array. */
function normalizeSnapshot(json) {
  const pcrOi = num(json.pcrOi ?? json.pcr_oi ?? json.pcr);
  const pcrVol = num(json.pcrVol ?? json.pcr_vol);
  if (pcrOi == null && pcrVol == null) return null;
  const now = json.updatedAt ? new Date(json.updatedAt) : new Date();
  return {
    t: now.toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false }).slice(0, 5),
    pcrOi, pcrVol,
    putOi: num(json.putOi ?? json.put_oi),
    callOi: num(json.callOi ?? json.call_oi),
  };
}
const num = (x) => (x == null || x === "" ? null : Number(x));

/* ---------- small UI bits ---------- */
function Pill({ children, color, dot }) {
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 6, fontFamily: MONO,
      fontSize: 11, letterSpacing: ".04em", color, padding: "3px 9px",
      border: `1px solid ${color}44`, borderRadius: 999, background: `${color}12`,
    }}>
      {dot && <span style={{ width: 6, height: 6, borderRadius: 99, background: color, animation: "pulse 1.6s infinite" }} />}
      {children}
    </span>
  );
}
function Stat({ label, value, sub, color }) {
  return (
    <div style={{ flex: "1 1 140px", minWidth: 140 }}>
      <div style={{ fontFamily: DISP, fontSize: 10, letterSpacing: ".12em", textTransform: "uppercase", color: T.muted }}>{label}</div>
      <div style={{ fontFamily: MONO, fontSize: 20, fontWeight: 600, color: color || T.fg, marginTop: 2 }}>{value}</div>
      {sub && <div style={{ fontFamily: MONO, fontSize: 11, color: T.muted, marginTop: 1 }}>{sub}</div>}
    </div>
  );
}

/* ---------- chart tooltip ---------- */
function TT({ active, payload, metric }) {
  if (!active || !payload || !payload.length) return null;
  const p = payload[0].payload;
  const val = metric === "oi" ? p.pcrOi : p.pcrVol;
  return (
    <div style={{ background: T.panel2, border: `1px solid ${T.line}`, borderRadius: 8, padding: "8px 10px", fontFamily: MONO }}>
      <div style={{ fontSize: 11, color: T.muted }}>{p.t} IST</div>
      <div style={{ fontSize: 16, color: T.cyan, fontWeight: 600 }}>PCR {fmt(val)}</div>
      {p.putOi != null && <div style={{ fontSize: 10, color: T.put }}>Put OI {fmtOi(p.putOi)}</div>}
      {p.callOi != null && <div style={{ fontSize: 10, color: T.call }}>Call OI {fmtOi(p.callOi)}</div>}
    </div>
  );
}

/* ---------- option chain: flashing cell ---------- */
function FlashCell({ value, render, align = "right" }) {
  const prevRef = React.useRef(value);
  const [flash, setFlash] = useState(null); // "up" | "down" | null

  useEffect(() => {
    const prev = prevRef.current;
    if (prev != null && value != null && value !== prev) {
      setFlash(value > prev ? "up" : "down");
      const timer = setTimeout(() => setFlash(null), 700);
      prevRef.current = value;
      return () => clearTimeout(timer);
    }
    prevRef.current = value;
  }, [value]);

  const bg = flash === "up" ? `${T.put}2E` : flash === "down" ? `${T.call}2E` : "transparent";
  const fg = flash === "up" ? T.put : flash === "down" ? T.call : T.fg;
  return (
    <td style={{
      padding: "6px 8px", textAlign: align, fontFamily: MONO, fontSize: 11, color: fg,
      background: bg, transition: "background 0.65s ease, color 0.65s ease",
    }}>
      {render(value)}
    </td>
  );
}

const thGroup = (color) => ({
  padding: "6px 8px", fontFamily: DISP, fontSize: 10, letterSpacing: ".1em",
  textTransform: "uppercase", color, borderBottom: `1px solid ${T.line}`,
});
const thCol = () => ({
  padding: "4px 8px", fontFamily: MONO, fontSize: 10, color: T.muted,
  borderBottom: `1px solid ${T.line}`, textAlign: "right",
});

/* ---------- expiry label helper ---------- */
function expiryLabel(exp) {
  // "11-Aug-2026" -> "11 Aug"
  const parts = String(exp).split("-");
  return parts.length >= 2 ? `${parts[0]} ${parts[1]}` : exp;
}

/* ---------- per-strike PCR history chart ---------- */
function StrikePcrHistory({ backendUrl, symbol, strike, onClose }) {
  const [snaps, setSnaps] = useState([]);
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    if (!backendUrl || strike == null) return;
    try {
      const base = backendUrl.replace(/\/$/, "");
      const res = await fetch(`${base}/api/optionchain/history?symbol=${symbol}&strike=${strike}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      const rows = (json.snapshots || []).filter((s) => s.pcr != null);
      setSnaps(rows);
      setErr(rows.length ? "" : "No history recorded for this strike yet — the first visit of the day starts it, then a new point every ~5 min");
    } catch (e) {
      setErr(e.message);
    }
  }, [backendUrl, symbol, strike]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    // new points land whenever anyone's page-view persists one, so a light poll is enough
    const id = setInterval(load, 60000);
    return () => clearInterval(id);
  }, [load]);

  const vals = snaps.map((s) => s.pcr);
  const yMin = vals.length ? Math.min(...vals, 0.9) - 0.1 : 0.5;
  const yMax = vals.length ? Math.max(...vals, 1.1) + 0.1 : 1.5;
  const last = snaps[snaps.length - 1];
  const first = snaps[0];
  const delta = last && first ? last.pcr - first.pcr : null;

  return (
    <div style={{ background: T.panel2, border: `1px solid ${T.line}`, borderRadius: 12, padding: "10px 8px", marginTop: 10 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "0 4px 8px", flexWrap: "wrap", gap: 6 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontFamily: DISP, fontSize: 12, fontWeight: 600 }}>Strike {strike} PCR</span>
          <span style={{
            fontFamily: MONO, fontSize: 10, fontWeight: 700, color: T.cyan,
            border: `1px solid ${T.cyan}44`, borderRadius: 999, padding: "2px 8px",
          }}>
            5m
          </span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {last && (
            <span style={{ fontFamily: MONO, fontSize: 13, color: T.fg, fontWeight: 600 }}>
              {last.pcr.toFixed(2)}
              {delta != null && (
                <span style={{ fontSize: 11, marginLeft: 6, color: delta >= 0 ? T.put : T.call }}>
                  {delta >= 0 ? "▲" : "▼"} {Math.abs(delta).toFixed(2)}
                </span>
              )}
            </span>
          )}
          <button onClick={onClose} title="Close"
            style={{ background: "none", border: "none", color: T.muted, cursor: "pointer", fontFamily: MONO, fontSize: 14, lineHeight: 1 }}>
            ✕
          </button>
        </div>
      </div>
      <div style={{ height: 160 }}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={snaps} margin={{ top: 4, right: 10, bottom: 4, left: -12 }}>
            <CartesianGrid stroke={T.line} strokeDasharray="2 4" vertical={false} />
            <ReferenceLine y={1} stroke={T.amber} strokeDasharray="4 4" strokeOpacity={0.6} />
            <XAxis dataKey="t" tick={{ fill: T.muted, fontSize: 9, fontFamily: MONO }}
              tickLine={false} axisLine={{ stroke: T.line }} minTickGap={30} />
            <YAxis domain={[yMin, yMax]} tick={{ fill: T.muted, fontSize: 9, fontFamily: MONO }}
              tickLine={false} axisLine={false} width={36} tickFormatter={(v) => v.toFixed(2)} />
            <Tooltip content={({ active, payload }) => {
              if (!active || !payload || !payload.length) return null;
              const p = payload[0].payload;
              return (
                <div style={{ background: T.panel, border: `1px solid ${T.line}`, borderRadius: 8, padding: "6px 8px", fontFamily: MONO, fontSize: 11 }}>
                  <div style={{ color: T.muted }}>{p.t} IST</div>
                  <div style={{ color: T.cyan, fontWeight: 600 }}>PCR {p.pcr.toFixed(2)}</div>
                </div>
              );
            }} />
            <Line type="monotone" dataKey="pcr" stroke={T.cyan} strokeWidth={2} dot={{ r: 2 }} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
      {err && <div style={{ fontFamily: MONO, fontSize: 10, color: T.call, padding: "6px 4px 0" }}>{err}</div>}
    </div>
  );
}

/* ---------- PCR sheet: time x strike pivot table, 5-min intervals ---------- */
function pcrCellColor(v) {
  return v == null ? T.muted : v > 1.05 ? T.put : v < 0.95 ? T.call : T.amber;
}

function OptionChainSheet({ backendUrl, symbol, strikes, onClose }) {
  const [data, setData] = useState({});
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    if (!backendUrl || !strikes.length) return;
    try {
      const base = backendUrl.replace(/\/$/, "");
      const qs = new URLSearchParams({ symbol, strikes: strikes.join(",") });
      const res = await fetch(`${base}/api/optionchain/history-sheet?${qs.toString()}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      const strikesData = json.strikes || {};
      setData(strikesData);
      const anyPoints = Object.values(strikesData).some((rows) => rows && rows.length);
      setErr(anyPoints ? "" : "No recorded history yet today — the first visit of the day starts it, then a new column every ~5 min");
    } catch (e) {
      setErr(e.message);
    }
  }, [backendUrl, symbol, strikes]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    const id = setInterval(load, 60000); // new columns land ~every 5 min; no benefit polling faster
    return () => clearInterval(id);
  }, [load]);

  const times = useMemo(() => {
    const set = new Set();
    Object.values(data).forEach((rows) => (rows || []).forEach((r) => { if (r.pcr != null) set.add(r.t); }));
    return Array.from(set).sort();
  }, [data]);

  const lookup = useMemo(() => {
    const out = {};
    Object.entries(data).forEach(([strike, rows]) => {
      out[strike] = {};
      (rows || []).forEach((r) => { if (r.pcr != null) out[strike][r.t] = r.pcr; });
    });
    return out;
  }, [data]);

  return (
    <div style={{ background: T.panel, border: `1px solid ${T.line}`, borderRadius: 14, padding: "12px 8px", marginTop: 12 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "0 4px 10px", flexWrap: "wrap", gap: 6 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontFamily: DISP, fontSize: 12, fontWeight: 600 }}>PCR Sheet</span>
          <span style={{
            fontFamily: MONO, fontSize: 10, fontWeight: 700, color: T.cyan,
            border: `1px solid ${T.cyan}44`, borderRadius: 999, padding: "2px 8px",
          }}>
            5m
          </span>
          <span style={{ fontFamily: MONO, fontSize: 10, color: T.muted }}>time × strike</span>
        </div>
        <button onClick={onClose} title="Close"
          style={{ background: "none", border: "none", color: T.muted, cursor: "pointer", fontFamily: MONO, fontSize: 14, lineHeight: 1 }}>
          ✕
        </button>
      </div>
      <div style={{ overflow: "auto", maxHeight: 320 }}>
        <table style={{ borderCollapse: "collapse", fontFamily: MONO, fontSize: 10, width: "100%" }}>
          <thead>
            <tr>
              <th style={{
                position: "sticky", left: 0, top: 0, zIndex: 2, background: T.panel,
                padding: "5px 10px", textAlign: "left", color: T.muted, borderBottom: `1px solid ${T.line}`,
              }}>
                Time
              </th>
              {strikes.map((s) => (
                <th key={s} style={{
                  position: "sticky", top: 0, background: T.panel, padding: "5px 10px",
                  textAlign: "right", color: T.muted, borderBottom: `1px solid ${T.line}`, whiteSpace: "nowrap",
                }}>
                  {s}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {times.map((t) => (
              <tr key={t}>
                <td style={{
                  position: "sticky", left: 0, background: T.panel, padding: "4px 10px",
                  color: T.muted, borderBottom: `1px solid ${T.line}22`, whiteSpace: "nowrap",
                }}>
                  {t}
                </td>
                {strikes.map((s) => {
                  const v = lookup[s]?.[t];
                  return (
                    <td key={s} style={{
                      padding: "4px 10px", textAlign: "right", fontWeight: 600,
                      color: pcrCellColor(v), borderBottom: `1px solid ${T.line}22`,
                    }}>
                      {v == null ? "—" : v.toFixed(2)}
                    </td>
                  );
                })}
              </tr>
            ))}
            {!times.length && (
              <tr><td colSpan={strikes.length + 1} style={{ padding: 16, textAlign: "center", color: T.muted }}>No recorded history yet today</td></tr>
            )}
          </tbody>
        </table>
      </div>
      {err && <div style={{ fontFamily: MONO, fontSize: 10, color: T.call, padding: "8px 4px 0" }}>{err}</div>}
    </div>
  );
}

/* ---------- option chain: panel ---------- */
// Nearest-to-spot N rows from a full chain -- Upstox's option-chain API
// always returns every strike (unlike /api/optionchain/today's own n=
// param), so this reproduces the "top N near spot" trim client-side when
// Upstox is the source, keeping the panel's behavior identical either way.
function nearestRows(rows, spot, n) {
  if (spot == null) return rows.slice(0, n);
  return [...rows]
    .sort((a, b) => Math.abs(a.strike - spot) - Math.abs(b.strike - spot))
    .slice(0, n)
    .sort((a, b) => a.strike - b.strike);
}

function OptionChain({ backendUrl, symbol }) {
  const [chain, setChain] = useState(null);
  const [chainErr, setChainErr] = useState("");
  const [chainSource, setChainSource] = useState(null); // "upstox" | "nse" | null
  const [expiries, setExpiries] = useState([]);
  const [selectedExpiry, setSelectedExpiry] = useState("");
  const [selectedStrike, setSelectedStrike] = useState(null);
  const [showSheet, setShowSheet] = useState(false);

  const loadExpiries = useCallback(async () => {
    if (!backendUrl) return;
    try {
      const base = backendUrl.replace(/\/$/, "");
      const res = await fetch(`${base}/api/expiries?symbol=${symbol}`);
      if (!res.ok) return;
      const json = await res.json();
      const list = json.expiries || [];
      setExpiries(list);
      setSelectedExpiry((prev) => (prev && list.includes(prev) ? prev : list[0] || ""));
    } catch {
      /* expiry list is a nice-to-have; chain still loads with the backend's default */
    }
  }, [backendUrl, symbol]);

  const load = useCallback(async () => {
    if (!backendUrl) { setChainErr("No backend connected"); return; }
    const base = backendUrl.replace(/\/$/, "");

    // Prefer Upstox (real broker LTPs, connected via /api/upstox/login) --
    // falls back to the NSE-scrape-backed /api/optionchain/today whenever
    // Upstox isn't connected or errors, so this panel still works either way.
    try {
      const uq = new URLSearchParams({ symbol });
      if (selectedExpiry) uq.set("expiry", selectedExpiry);
      const ures = await fetch(`${base}/api/upstox/optionchain?${uq.toString()}`);
      if (ures.ok) {
        const ujson = await ures.json();
        if (ujson.connected && (ujson.rows || []).length > 0) {
          setChain({
            symbol: ujson.symbol, expiry: ujson.expiry, spot: ujson.spot,
            rows: nearestRows(ujson.rows, ujson.spot, 10),
          });
          setChainSource("upstox");
          setChainErr("");
          return;
        }
      }
    } catch {
      // fall through to the NSE fallback below
    }

    try {
      const qs = new URLSearchParams({ symbol, n: "10" });
      if (selectedExpiry) qs.set("expiry", selectedExpiry);
      const res = await fetch(`${base}/api/optionchain/today?${qs.toString()}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      setChain(json);
      setChainSource("nse");
      setChainErr(json.rows && json.rows.length ? "" : "Backend has no chain data yet");
    } catch (e) {
      setChainErr(e.message);
    }
  }, [backendUrl, symbol, selectedExpiry]);

  useEffect(() => {
    setChain(null);
    setExpiries([]);
    setSelectedExpiry("");
    setSelectedStrike(null);
    loadExpiries();
  }, [symbol, backendUrl]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, [load]);

  const rows = chain?.rows || [];
  const atmStrike = rows.reduce((best, r) => {
    if (chain?.spot == null) return best;
    if (best == null || Math.abs(r.strike - chain.spot) < Math.abs(best - chain.spot)) return r.strike;
    return best;
  }, null);

  return (
    <div style={{ background: T.panel, border: `1px solid ${T.line}`, borderRadius: 14, padding: "12px 8px 8px", marginTop: 12 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "0 8px 8px", flexWrap: "wrap", gap: 6 }}>
        <div style={{ fontFamily: DISP, fontSize: 12, fontWeight: 600, letterSpacing: ".02em" }}>
          Option Chain <span style={{ color: T.muted, fontWeight: 400 }}>· top 10 near spot</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8, fontFamily: MONO, fontSize: 11 }}>
          {chain?.spot != null && <span style={{ color: T.muted }}>Spot {fmt(chain.spot, 2)}</span>}
          {chainSource && (
            <span
              style={{ color: chainSource === "upstox" ? T.cyan : T.amber, fontWeight: 600 }}
              title={chainSource === "upstox"
                ? "Live via your connected Upstox account"
                : "Upstox not connected — falling back to the NSE-derived feed. Visit /api/upstox/login to connect Upstox."}
            >
              via {chainSource === "upstox" ? "Upstox" : "NSE fallback"}
            </span>
          )}
          <Pill color={rows.length ? T.put : T.call} dot={!!rows.length}>{rows.length ? "Live" : "No data"}</Pill>
          <button onClick={() => setShowSheet((s) => !s)} title="Show every strike's 5-min PCR history as a sheet"
            style={{
              padding: "4px 10px", borderRadius: 999, cursor: "pointer", fontSize: 11,
              background: showSheet ? T.cyan : T.panel2, color: showSheet ? T.ink : T.muted,
              border: `1px solid ${showSheet ? T.cyan : T.line}`,
            }}>
            Sheet
          </button>
        </div>
      </div>

      {expiries.length > 1 && (
        <div style={{ display: "flex", gap: 5, padding: "0 8px 10px", overflowX: "auto" }}>
          {expiries.map((exp, i) => (
            <button key={exp} onClick={() => setSelectedExpiry(exp)}
              style={{
                flex: "0 0 auto", padding: "5px 10px", borderRadius: 7, cursor: "pointer",
                fontFamily: MONO, fontSize: 11, whiteSpace: "nowrap",
                background: selectedExpiry === exp ? T.cyan : T.panel2,
                color: selectedExpiry === exp ? T.ink : T.muted,
                border: `1px solid ${selectedExpiry === exp ? T.cyan : T.line}`,
              }}>
              {expiryLabel(exp)}{i === 0 ? " · this week" : ""}
            </button>
          ))}
        </div>
      )}

      {rows.length > 0 && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "0 8px 10px", flexWrap: "wrap" }}>
          <label style={{ fontFamily: MONO, fontSize: 11, color: T.muted }}>PCR history for strike:</label>
          <select value={selectedStrike ?? ""} onChange={(e) => setSelectedStrike(e.target.value ? Number(e.target.value) : null)}
            style={{
              background: T.panel2, color: T.fg, border: `1px solid ${T.line}`, borderRadius: 6,
              padding: "4px 8px", fontFamily: MONO, fontSize: 11,
            }}>
            <option value="">— select a strike —</option>
            {rows.map((r) => <option key={r.strike} value={r.strike}>{r.strike}</option>)}
          </select>
        </div>
      )}

      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 650 }}>
          <thead>
            <tr>
              <th colSpan={3} style={thGroup(T.call)}>Calls</th>
              <th style={thGroup(T.muted)}>Strike</th>
              <th colSpan={3} style={thGroup(T.put)}>Puts</th>
              <th rowSpan={2} style={{ ...thGroup(T.cyan), verticalAlign: "middle" }}>PCR</th>
            </tr>
            <tr>
              <th style={thCol()}>OI</th><th style={thCol()}>Chg OI</th><th style={thCol()}>Vol</th>
              <th style={{ ...thCol(), textAlign: "center" }}></th>
              <th style={thCol()}>OI</th><th style={thCol()}>Chg OI</th><th style={thCol()}>Vol</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const isAtm = r.strike === atmStrike;
              const strikePcr = r.ceOi ? r.peOi / r.ceOi : null;
              const pcrColor = strikePcr == null ? T.muted : strikePcr > 1.05 ? T.put : strikePcr < 0.95 ? T.call : T.amber;
              return (
                <tr key={r.strike} style={{ background: isAtm ? `${T.amber}14` : "transparent" }}>
                  <FlashCell value={r.ceOi} render={fmtOi} />
                  <FlashCell value={r.ceOiChg} render={(v) => `${v >= 0 ? "+" : ""}${fmtOi(v)}`} />
                  <FlashCell value={r.ceVol} render={fmtOi} />
                  <td onClick={() => setSelectedStrike((s) => (s === r.strike ? null : r.strike))}
                    title="Click to chart this strike's PCR history"
                    style={{
                      padding: "6px 8px", textAlign: "center", fontFamily: MONO, fontSize: 12, fontWeight: 700,
                      color: selectedStrike === r.strike ? T.cyan : isAtm ? T.amber : T.fg, cursor: "pointer",
                    }}>
                    {r.strike}
                    {isAtm && <div style={{ fontSize: 9, fontWeight: 500, color: T.amber, letterSpacing: ".08em" }}>ATM</div>}
                  </td>
                  <FlashCell value={r.peOi} render={fmtOi} />
                  <FlashCell value={r.peOiChg} render={(v) => `${v >= 0 ? "+" : ""}${fmtOi(v)}`} />
                  <FlashCell value={r.peVol} render={fmtOi} />
                  <td style={{ padding: "6px 8px", textAlign: "right", fontFamily: MONO, fontSize: 11, fontWeight: 700, color: pcrColor }}>
                    {strikePcr == null ? "—" : strikePcr.toFixed(2)}
                  </td>
                </tr>
              );
            })}
            {!rows.length && (
              <tr><td colSpan={8} style={{ padding: 16, textAlign: "center", color: T.muted, fontFamily: MONO, fontSize: 11 }}>No chain data yet</td></tr>
            )}
          </tbody>
        </table>
      </div>
      {chainErr && <div style={{ fontFamily: MONO, fontSize: 10, color: T.call, padding: "6px 8px 0" }}>{chainErr}</div>}
      {!chainErr && !selectedStrike && (
        <div style={{ fontFamily: MONO, fontSize: 10, color: T.muted, textAlign: "center", padding: "6px 8px 0" }}>
          click a strike to chart its PCR history
        </div>
      )}
      {selectedStrike != null && (
        <StrikePcrHistory backendUrl={backendUrl} symbol={symbol} strike={selectedStrike} onClose={() => setSelectedStrike(null)} />
      )}
      {showSheet && rows.length > 0 && (
        <OptionChainSheet backendUrl={backendUrl} symbol={symbol} strikes={rows.map((r) => r.strike)} onClose={() => setShowSheet(false)} />
      )}
    </div>
  );
}

// Local dev runs the frontend (port 5500) and backend (port 8000) as two
// separate servers, so localhost/LAN-IP hosts need that explicit port. A
// real deployment (Vercel, custom domain, etc.) serves the API from the
// exact same origin under /api — appending :8000 there would be wrong
// (nothing listens on that port, and it'd even break http vs https).
function detectDefaultBackendUrl() {
  if (typeof window === "undefined") return "http://127.0.0.1:8000";
  const { hostname, protocol, origin } = window.location;
  const isLocalDev = hostname === "localhost" || hostname === "127.0.0.1" ||
    /^192\.168\.\d+\.\d+$/.test(hostname) || /^10\.\d+\.\d+\.\d+$/.test(hostname) ||
    /^172\.(1[6-9]|2\d|3[01])\.\d+\.\d+$/.test(hostname);
  return isLocalDev ? `${protocol}//${hostname}:8000` : origin;
}
const DEFAULT_BACKEND_URL = detectDefaultBackendUrl();

function fmtSigned(v, d = 2) {
  if (v == null || isNaN(v)) return "—";
  const n = Number(v);
  const s = n.toFixed(d);
  return n > 0 ? `+${s}` : s;
}

function Hint({ children }) {
  return <div style={{ fontFamily: DISP, fontSize: 13, color: T.muted, lineHeight: 1.5, marginTop: 6 }}>{children}</div>;
}

function topOiRows(rows, oiKey, volKey, n = 4) {
  const totalVol = rows.reduce((s, r) => s + (Number(r[volKey]) || 0), 0);
  const totalOi = rows.reduce((s, r) => s + (Number(r[oiKey]) || 0), 0);
  return [...rows]
    .filter((r) => r[oiKey] != null && r.strike != null)
    .sort((a, b) => (Number(b[oiKey]) || 0) - (Number(a[oiKey]) || 0))
    .slice(0, n)
    .map((r, i) => {
      const vol = Number(r[volKey]) || 0;
      const oi = Number(r[oiKey]) || 0;
      const isCall = oiKey === "ceOi";
      return {
        rank: i + 1,
        strike: r.strike,
        oi,
        oiChg: isCall ? r.ceOiChg : r.peOiChg,
        vol,
        volPct: totalVol > 0 ? (vol / totalVol) * 100 : null,
        oiPct: totalOi > 0 ? (oi / totalOi) * 100 : null,
        ltp: isCall ? r.ceLtp : r.peLtp,
      };
    });
}

function oiSideMetrics(row, side, totalOi, totalVol) {
  if (!row) return null;
  const isCall = side === "ce";
  const oi = Number(isCall ? row.ceOi : row.peOi) || 0;
  const vol = Number(isCall ? row.ceVol : row.peVol) || 0;
  return {
    strike: row.strike,
    oi,
    oiChg: isCall ? row.ceOiChg : row.peOiChg,
    vol,
    volPct: totalVol > 0 ? (vol / totalVol) * 100 : null,
    oiPct: totalOi > 0 ? (oi / totalOi) * 100 : null,
    ltp: isCall ? row.ceLtp : row.peLtp,
  };
}

async function fetchUpstoxChainLive(backendUrl, symbol = "NIFTY") {
  const base = backendUrl.replace(/\/$/, "");
  const res = await fetch(`${base}/api/upstox/optionchain?symbol=${symbol}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const json = await res.json();
  if (!json.connected) {
    const err = new Error(json.error || "Upstox not connected — visit /api/upstox/login");
    err.code = "upstox_disconnected";
    throw err;
  }
  if (!(json.rows || []).length) {
    throw new Error(json.error || "Upstox returned an empty option chain");
  }
  return {
    rows: json.rows,
    spot: json.spot,
    expiry: json.expiry,
    source: "upstox",
    updatedAt: new Date().toISOString(),
  };
}

function OiRankModal({ backendUrl, onClose }) {
  const [pack, setPack] = useState(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);
  const [loginHint, setLoginHint] = useState("");

  const load = useCallback(async (quiet = false) => {
    if (!backendUrl) return;
    if (!quiet) setLoading(true);
    try {
      const next = await fetchUpstoxChainLive(backendUrl, "NIFTY");
      setPack(next);
      setErr("");
      setLoginHint("");
    } catch (e) {
      setErr(e.message);
      setLoginHint(e.code === "upstox_disconnected" ? `${backendUrl.replace(/\/$/, "")}/api/upstox/login` : "");
      if (!quiet) setPack(null);
    } finally { setLoading(false); }
  }, [backendUrl]);

  useEffect(() => { load(false); }, [load]);
  // Live while the modal is open — same cadence as the PCR option-chain panel.
  useEffect(() => {
    const id = setInterval(() => load(true), 3000);
    return () => clearInterval(id);
  }, [load]);
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const rows = pack?.rows || [];
  const spot = pack?.spot != null ? Number(pack.spot) : null;
  const totalCeOi = rows.reduce((s, r) => s + (Number(r.ceOi) || 0), 0);
  const totalPeOi = rows.reduce((s, r) => s + (Number(r.peOi) || 0), 0);
  const totalCeVol = rows.reduce((s, r) => s + (Number(r.ceVol) || 0), 0);
  const totalPeVol = rows.reduce((s, r) => s + (Number(r.peVol) || 0), 0);
  const callTop = topOiRows(rows, "ceOi", "ceVol", 4);
  const putTop = topOiRows(rows, "peOi", "peVol", 4);
  const atmStrike = rows.reduce((best, r) => {
    if (spot == null || r.strike == null) return best;
    if (best == null || Math.abs(r.strike - spot) < Math.abs(best - spot)) return r.strike;
    return best;
  }, null);
  const atmRow = rows.find((r) => r.strike === atmStrike) || null;
  const atmCall = oiSideMetrics(atmRow, "ce", totalCeOi, totalCeVol);
  const atmPut = oiSideMetrics(atmRow, "pe", totalPeOi, totalPeVol);
  const atmDiff = spot != null && atmStrike != null ? atmStrike - spot : null;
  const atmPcr = atmCall?.oi ? (atmPut?.oi || 0) / atmCall.oi : null;

  const th = (label, align = "left") => (
    <th style={{ padding: "6px 8px", borderBottom: `1px solid ${T.line}`, textAlign: align }}>{label}</th>
  );

  const renderSideCells = (r, color) => (
    <>
      <td style={{ padding: "7px 8px", borderBottom: `1px solid ${T.line}88`, textAlign: "right", color: T.fg }}>{fmt(r?.ltp, 2)}</td>
      <td style={{ padding: "7px 8px", borderBottom: `1px solid ${T.line}88`, textAlign: "right", color }}>{fmtOi(r?.oi)}</td>
      <td style={{ padding: "7px 8px", borderBottom: `1px solid ${T.line}88`, textAlign: "right", color: T.muted }}>
        {r?.oiPct == null ? "—" : `${r.oiPct.toFixed(1)}%`}
      </td>
      <td style={{
        padding: "7px 8px", borderBottom: `1px solid ${T.line}88`, textAlign: "right",
        color: (r?.oiChg || 0) >= 0 ? T.put : T.call,
      }}>
        {r?.oiChg == null ? "—" : `${r.oiChg >= 0 ? "+" : ""}${fmtOi(r.oiChg)}`}
      </td>
      <td style={{ padding: "7px 8px", borderBottom: `1px solid ${T.line}88`, textAlign: "right" }}>{fmtOi(r?.vol)}</td>
      <td style={{ padding: "7px 8px", borderBottom: `1px solid ${T.line}88`, textAlign: "right", fontWeight: 700, color: T.cyan }}>
        {r?.volPct == null ? "—" : `${r.volPct.toFixed(1)}%`}
      </td>
    </>
  );

  const renderTable = (title, color, list, side) => (
    <div style={{ flex: "1 1 320px", minWidth: 0 }}>
      <div style={{ fontFamily: MONO, fontSize: 11, color, letterSpacing: ".06em", marginBottom: 8 }}>{title}</div>
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: MONO, fontSize: 11, minWidth: 460 }}>
          <thead>
            <tr style={{ color: T.muted, textAlign: "left" }}>
              {th("#")}
              {th("Strike")}
              {th("vs Spot", "right")}
              {th("LTP", "right")}
              {th("OI", "right")}
              {th("OI %", "right")}
              {th("Chg OI", "right")}
              {th("Vol", "right")}
              {th("Vol %", "right")}
            </tr>
          </thead>
          <tbody>
            {list.map((r) => {
              const isAtm = atmStrike != null && r.strike === atmStrike;
              const diff = spot != null && r.strike != null ? r.strike - spot : null;
              return (
              <tr key={`${side}-${r.strike}`} style={{ background: isAtm ? `${T.amber}14` : "transparent" }}>
                <td style={{ padding: "7px 8px", borderBottom: `1px solid ${T.line}88`, color: T.muted }}>OI{r.rank}</td>
                <td style={{ padding: "7px 8px", borderBottom: `1px solid ${T.line}88` }}>
                  <div style={{ fontWeight: 700, color: isAtm ? T.amber : T.fg }}>{r.strike}</div>
                  {isAtm && <div style={{ fontSize: 9, fontWeight: 600, color: T.amber, letterSpacing: ".08em" }}>ATM</div>}
                </td>
                <td style={{
                  padding: "7px 8px", borderBottom: `1px solid ${T.line}88`, textAlign: "right", fontWeight: 600,
                  color: diff == null ? T.muted : Math.abs(diff) < 0.5 ? T.amber : diff > 0 ? T.call : T.put,
                }}>
                  {diff == null ? "—" : `${diff >= 0 ? "+" : ""}${diff.toFixed(0)}`}
                </td>
                {renderSideCells(r, color)}
              </tr>
              );
            })}
            {!list.length && (
              <tr><td colSpan={9} style={{ padding: 14, color: T.muted, textAlign: "center", fontFamily: DISP }}>No OI rows yet</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed", inset: 0, background: "rgba(10,15,30,0.78)", zIndex: 2500,
        display: "flex", alignItems: "center", justifyContent: "center",
        padding: 16, paddingBottom: "max(16px, env(safe-area-inset-bottom))",
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: T.panel, border: `1px solid ${T.line}`, borderRadius: 14, padding: 16,
          width: "100%", maxWidth: 1100, maxHeight: "88vh", overflow: "auto",
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", gap: 10, alignItems: "flex-start", flexWrap: "wrap" }}>
          <div>
            <div style={{ fontFamily: MONO, fontSize: 11, color: T.cyan }}>OPEN INTEREST · LIVE UPSTOX</div>
            <div style={{ fontSize: 18, fontWeight: 700, marginTop: 2 }}>Top OI strikes · NIFTY</div>
            <div style={{ fontFamily: DISP, fontSize: 12, color: T.muted, marginTop: 4 }}>
              ATM shows call + put for the nearest strike. OI1–OI4 = highest open interest. Same fields everywhere: LTP, OI, OI %, Chg OI, Vol, Vol %.
              {pack?.expiry ? ` · expiry ${expiryLabel(pack.expiry)}` : ""}
              {pack?.updatedAt
                ? ` · ${new Date(pack.updatedAt).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", second: "2-digit" })} IST`
                : ""}
            </div>
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <Pill color={rows.length ? T.put : T.amber} dot={!!rows.length}>{rows.length ? "Live Upstox" : "Waiting"}</Pill>
            <button onClick={() => load(false)} style={{ background: T.panel2, color: T.cyan, border: `1px solid ${T.line}`, borderRadius: 8, padding: "6px 12px", fontSize: 12, cursor: "pointer", fontFamily: MONO }}>
              {loading ? "…" : "↻"}
            </button>
            <button onClick={onClose} aria-label="Close" style={{ background: "transparent", border: "none", color: T.muted, cursor: "pointer", fontSize: 18, lineHeight: 1 }}>✕</button>
          </div>
        </div>

        {err && (
          <div style={{ fontFamily: DISP, fontSize: 13, color: T.call, marginTop: 10, lineHeight: 1.45 }}>
            {err}
            {loginHint && (
              <>
                {" "}Open <a href={loginHint} style={{ color: T.cyan, overflowWrap: "anywhere" }}>{loginHint}</a> once, then refresh.
              </>
            )}
          </div>
        )}
        {loading && !rows.length ? (
          <div style={{ fontFamily: DISP, fontSize: 13, color: T.muted, marginTop: 16 }}>Loading live Upstox option chain…</div>
        ) : rows.length ? (
          <>
            {atmStrike != null && (
              <div style={{
                marginTop: 16, background: `${T.amber}12`, border: `1px solid ${T.amber}55`,
                borderRadius: 12, padding: 12,
              }}>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 10, flexWrap: "wrap", alignItems: "baseline" }}>
                  <div>
                    <div style={{ fontFamily: MONO, fontSize: 11, color: T.amber, letterSpacing: ".06em" }}>ATM STRIKE</div>
                    <div style={{ fontFamily: MONO, fontSize: 28, fontWeight: 700, color: T.amber, lineHeight: 1.1, marginTop: 2 }}>
                      {atmStrike}
                    </div>
                    <div style={{ fontFamily: DISP, fontSize: 12, color: T.muted, marginTop: 4 }}>
                      Spot {fmt(spot, 1)}
                      {atmDiff != null ? ` · vs spot ${atmDiff >= 0 ? "+" : ""}${atmDiff.toFixed(0)}` : ""}
                      {atmPcr != null ? ` · strike PCR ${fmt(atmPcr, 2)}` : ""}
                    </div>
                  </div>
                  <Pill color={T.amber}>nearest to spot</Pill>
                </div>
                <div style={{ overflowX: "auto", marginTop: 12 }}>
                  <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: MONO, fontSize: 11, minWidth: 720 }}>
                    <thead>
                      <tr style={{ color: T.muted, textAlign: "left" }}>
                        <th style={{ padding: "6px 8px", borderBottom: `1px solid ${T.line}` }}>Side</th>
                        <th style={{ padding: "6px 8px", borderBottom: `1px solid ${T.line}`, textAlign: "right" }}>LTP</th>
                        <th style={{ padding: "6px 8px", borderBottom: `1px solid ${T.line}`, textAlign: "right" }}>OI</th>
                        <th style={{ padding: "6px 8px", borderBottom: `1px solid ${T.line}`, textAlign: "right" }}>OI %</th>
                        <th style={{ padding: "6px 8px", borderBottom: `1px solid ${T.line}`, textAlign: "right" }}>Chg OI</th>
                        <th style={{ padding: "6px 8px", borderBottom: `1px solid ${T.line}`, textAlign: "right" }}>Vol</th>
                        <th style={{ padding: "6px 8px", borderBottom: `1px solid ${T.line}`, textAlign: "right" }}>Vol %</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr>
                        <td style={{ padding: "8px 8px", borderBottom: `1px solid ${T.line}88`, fontWeight: 700, color: T.call }}>Call</td>
                        {renderSideCells(atmCall, T.call)}
                      </tr>
                      <tr>
                        <td style={{ padding: "8px 8px", borderBottom: `1px solid ${T.line}88`, fontWeight: 700, color: T.put }}>Put</td>
                        {renderSideCells(atmPut, T.put)}
                      </tr>
                    </tbody>
                  </table>
                </div>
              </div>
            )}
            <div style={{ display: "flex", gap: 16, marginTop: 16, flexWrap: "wrap", alignItems: "flex-start" }}>
              {renderTable("CALL OI 1–4", T.call, callTop, "ce")}
              {renderTable("PUT OI 1–4", T.put, putTop, "pe")}
            </div>
          </>
        ) : null}
      </div>
    </div>
  );
}

function PcrOiCard({ backendUrl }) {
  const [snap, setSnap] = useState(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [metric, setMetric] = useState("oi");
  const [showOi, setShowOi] = useState(false);
  const [oiLive, setOiLive] = useState(false);

  const load = useCallback(async () => {
    if (!backendUrl) return;
    setLoading(true);
    const base = backendUrl.replace(/\/$/, "");
    let next = null;
    let live = false;
    let loadErr = "";
    try {
      const chain = await fetchUpstoxChainLive(backendUrl, "NIFTY");
      const putOi = chain.rows.reduce((s, r) => s + (Number(r.peOi) || 0), 0);
      const callOi = chain.rows.reduce((s, r) => s + (Number(r.ceOi) || 0), 0);
      const putVol = chain.rows.reduce((s, r) => s + (Number(r.peVol) || 0), 0);
      const callVol = chain.rows.reduce((s, r) => s + (Number(r.ceVol) || 0), 0);
      next = {
        t: new Date().toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false }).slice(0, 5),
        pcrOi: callOi ? putOi / callOi : null,
        pcrVol: callVol ? putVol / callVol : null,
        putOi, callOi,
      };
      live = true;
    } catch (e) {
      loadErr = e.message;
      try {
        const res = await fetch(`${base}/api/pcr/today?symbol=NIFTY`);
        if (res.ok) {
          next = normalizeSnapshot(await res.json());
          if (next) loadErr = `${e.message} · showing last PCR cache`;
        }
      } catch { /* keep upstox error */ }
    }
    setSnap(next);
    setOiLive(live);
    setErr(next ? loadErr : (loadErr || "No PCR reading yet"));
    setLoading(false);
  }, [backendUrl]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!backendUrl) return;
    // Live Upstox OI while this card is on screen (market hours or not —
    // Upstox still returns the last session chain when closed).
    const id = setInterval(load, marketStatus().open ? 5000 : 60_000);
    return () => clearInterval(id);
  }, [backendUrl, load]);

  const pcrVal = metric === "oi" ? snap?.pcrOi : snap?.pcrVol;
  const sentiment = pcrVal == null
    ? { label: "—", color: T.muted }
    : pcrVal > 1.05 ? { label: "Put-heavy", color: T.put }
      : pcrVal < 0.95 ? { label: "Call-heavy", color: T.call }
        : { label: "Balanced", color: T.amber };

  return (
    <>
      <div style={{ background: T.panel, border: `1px solid ${T.line}`, borderRadius: 14, padding: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap", alignItems: "flex-start" }}>
          <div>
            <div style={{ fontFamily: MONO, fontSize: 11, color: T.cyan }}>NIFTY PCR</div>
            <div style={{ fontSize: 16, fontWeight: 700, marginTop: 2 }}>Put-Call Ratio</div>
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <Pill color={oiLive ? T.put : T.amber} dot={oiLive}>{oiLive ? "Live Upstox" : "Not live"}</Pill>
            <button
              onClick={() => setShowOi(true)}
              style={{
                background: T.cyan, color: T.ink, border: "none", borderRadius: 8,
                padding: "8px 14px", fontWeight: 700, cursor: "pointer", fontFamily: MONO, fontSize: 12,
              }}
            >
              OI
            </button>
            <button onClick={load} style={{ background: T.panel2, color: T.cyan, border: `1px solid ${T.line}`, borderRadius: 8, padding: "6px 12px", fontSize: 12, cursor: "pointer", fontFamily: MONO }}>
              {loading ? "…" : "↻"}
            </button>
          </div>
        </div>

        <div style={{ display: "flex", alignItems: "flex-end", gap: 16, flexWrap: "wrap", marginTop: 12 }}>
          <div>
            <div style={{ fontFamily: DISP, fontSize: 10, letterSpacing: ".12em", textTransform: "uppercase", color: T.muted }}>
              Current PCR ({metric === "oi" ? "OI" : "Volume"})
            </div>
            <div className="pcr-hero" style={{ fontFamily: MONO, fontSize: 48, fontWeight: 600, lineHeight: 1, color: T.fg }}>
              {fmt(pcrVal)}
            </div>
          </div>
          <div style={{ paddingBottom: 6 }}>
            <Pill color={sentiment.color}>{sentiment.label}</Pill>
          </div>
        </div>

        <div style={{ display: "flex", gap: 6, marginTop: 12, flexWrap: "wrap" }}>
          {[["oi", "PCR · Open Interest"], ["vol", "PCR · Volume"]].map(([k, lbl]) => (
            <button key={k} onClick={() => setMetric(k)}
              style={{
                padding: "7px 12px", borderRadius: 8, cursor: "pointer", fontFamily: MONO, fontSize: 11,
                background: metric === k ? T.panel2 : "transparent", color: metric === k ? T.cyan : T.muted,
                border: `1px solid ${metric === k ? T.cyan + "66" : T.line}`,
              }}>
              {lbl}
            </button>
          ))}
        </div>

        <div style={{ display: "flex", gap: 16, marginTop: 14, paddingTop: 14, borderTop: `1px solid ${T.line}`, flexWrap: "wrap" }}>
          <Stat label="Put OI" value={fmtOi(snap?.putOi)} color={T.put} />
          <Stat label="Call OI" value={fmtOi(snap?.callOi)} color={T.call} />
          <Stat label="Put/Call" value={snap?.putOi != null && snap?.callOi ? fmtOi(snap.putOi + snap.callOi) : "—"} sub="combined OI" />
        </div>
        {err && <div style={{ fontFamily: DISP, fontSize: 12, color: T.call, marginTop: 8 }}>{err}</div>}
      </div>
      {showOi && <OiRankModal backendUrl={backendUrl} onClose={() => setShowOi(false)} />}
    </>
  );
}

const UNUSUAL_TOAST_MS = 8000;

function alertKey(a) {
  return `${a.fired_at || ""}|${a.symbol || ""}`;
}

function toastKey(a) {
  if (a?.kind === "news") return `news|${a.headline || ""}`;
  return alertKey(a);
}

function fireBrowserNotification(alert) {
  if (typeof Notification === "undefined") return;
  if (Notification.permission !== "granted") return;
  try {
    const isNews = alert.kind === "news";
    const n = new Notification(
      isNews ? `News · ${(alert.topic || "market").toUpperCase()}` : `${alert.symbol} — unusual activity`,
      {
        body: isNews
          ? (alert.headline || "High-impact headline")
          : (alert.message || "A heavy Nifty stock looks unusually busy. Not a prediction."),
        tag: toastKey(alert),
        requireInteraction: false,
      },
    );
    setTimeout(() => { try { n.close(); } catch { /* already closed */ } }, UNUSUAL_TOAST_MS);
  } catch { /* blocked / insecure context */ }
}

function UnusualActivityToasts({ toasts, onDismiss }) {
  if (!toasts.length) return null;
  return (
    <div className="ua-toasts" style={{ position: "fixed", top: 16, right: 16, zIndex: 3000, display: "flex", flexDirection: "column", gap: 8, width: 360, maxWidth: "calc(100vw - 24px)" }}>
      {toasts.map((a) => {
        const isNews = a.kind === "news";
        const color = isNews ? T.amber : T.call;
        const key = a.kind === "news" ? `news|${a.headline}` : alertKey(a);
        return (
        <div
          key={key}
          style={{
            background: T.panel, border: `1px solid ${color}`, borderRadius: 10, padding: "12px 14px",
            boxShadow: "0 12px 32px rgba(0,0,0,0.55)", animation: "pulse 1.6s ease-out 1",
          }}
        >
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 10 }}>
            <div>
              <div style={{ fontFamily: DISP, fontSize: 13, fontWeight: 700, color }}>
                {isNews ? `News · ${(a.topic || "market").toUpperCase()}` : `Unusual activity · ${a.symbol} (score ${a.score != null ? Math.round(a.score) : "—"})`}
              </div>
              <div style={{ fontFamily: DISP, fontSize: 12, color: T.fg, marginTop: 6, lineHeight: 1.4 }}>
                {isNews ? a.headline : a.message}
              </div>
              <div style={{ fontFamily: DISP, fontSize: 11, color: T.muted, marginTop: 6 }}>
                {isNews ? (a.reason || "AI flagged this as high impact for Nifty. Not advice.") : "Heads-up only — not a Nifty prediction. Disappears in a few seconds."}
              </div>
            </div>
            <button
              onClick={() => onDismiss(key)} aria-label="Dismiss"
              style={{ background: "transparent", border: "none", color: T.muted, cursor: "pointer", fontSize: 16, lineHeight: 1, flexShrink: 0 }}
            >
              ✕
            </button>
          </div>
        </div>
        );
      })}
    </div>
  );
}

const NEWS_SECTIONS = [
  { id: "nifty", title: "Nifty 50", blurb: "India index — shown first" },
  { id: "us", title: "USA markets", blurb: "S&P, Nasdaq, Fed" },
  { id: "trump", title: "Trump / policy", blurb: "Tariffs, tweets" },
  { id: "crude", title: "Other news", blurb: "Crude and leftover headlines" },
];

function sentimentColor(s) {
  if (s === "bullish") return T.put;
  if (s === "bearish") return T.call;
  return T.muted;
}

function fmtTapePrice(n) {
  if (n == null || Number.isNaN(Number(n))) return "—";
  const v = Number(n);
  return Math.abs(v) >= 1000 ? v.toLocaleString("en-IN", { maximumFractionDigits: 0 }) : v.toFixed(2);
}

function TapeChip({ label, quote }) {
  const pct = quote?.pct_change;
  const color = pct == null ? T.muted : pct >= 0 ? T.put : T.call;
  const state = (quote?.market_state || "").toUpperCase();
  const live = quote?.live || quote?.source === "upstox" || state === "REGULAR";
  const closed = state === "CLOSED" || state === "POST" || state === "PRE";
  const status = quote == null ? "" : live ? "live" : closed ? "last close" : "delayed";
  return (
    <div style={{ background: T.ink, border: `1px solid ${T.line}`, borderRadius: 8, padding: "6px 8px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 6, alignItems: "center" }}>
        <div style={{ fontFamily: MONO, fontSize: 9, color: T.muted, letterSpacing: ".04em" }}>{label}</div>
        {status && (
          <div style={{ fontFamily: MONO, fontSize: 8, color: live ? T.put : T.muted, letterSpacing: ".04em" }}>{status}</div>
        )}
      </div>
      <div style={{ fontFamily: MONO, fontSize: 13, fontWeight: 700, color: T.fg, marginTop: 2 }}>
        {fmtTapePrice(quote?.price)}
      </div>
      <div style={{ fontFamily: MONO, fontSize: 12, fontWeight: 700, color, marginTop: 1 }}>
        {pct == null ? "—" : `${pct >= 0 ? "+" : ""}${Number(pct).toFixed(2)}%`}
      </div>
    </div>
  );
}

function NewsDesk({ news, loading, err, onRefresh, notifyPerm, onEnableNotify, collapsible, liveTape }) {
  const [open, setOpen] = useState(!collapsible);
  const sections = news?.sections || {};
  const impact = news?.india_impact || {};
  const tilt = impact.india_tilt_pct;
  const tiltColor = tilt == null ? T.muted : tilt > 0 ? T.put : tilt < 0 ? T.call : T.amber;
  const tape = liveTape || news?.tape || {};
  const showBody = !collapsible || open;
  return (
    <div style={{ background: T.panel, border: `1px solid ${T.line}`, borderRadius: 14, padding: 14 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 8 }}>
        <div>
          <div style={{ fontFamily: MONO, fontSize: 11, color: T.amber }}>NEWS DESK</div>
          <div style={{ fontSize: 16, fontWeight: 700, marginTop: 2 }}>Top headlines</div>
          <div style={{ fontFamily: DISP, fontSize: 11, color: T.muted, marginTop: 4 }}>
            {loading ? "Updating…" : "Refreshes every 5 minutes"}
            {news?.ai_pending ? " · scoring India impact…" : ""}
          </div>
        </div>
        <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
          {collapsible && (
            <button onClick={() => setOpen((v) => !v)}
              style={{ background: T.panel2, color: T.fg, border: `1px solid ${T.line}`, borderRadius: 8, padding: "6px 10px", fontSize: 12, cursor: "pointer", fontFamily: MONO }}>
              {open ? "Hide" : "Show"}
            </button>
          )}
          <button onClick={onRefresh} style={{ background: T.panel2, color: T.cyan, border: `1px solid ${T.line}`, borderRadius: 8, padding: "6px 10px", fontSize: 12, cursor: "pointer", fontFamily: MONO }}>
            {loading ? "…" : "↻"}
          </button>
        </div>
      </div>
      {collapsible && !open && (
        <div style={{ display: "flex", alignItems: "baseline", gap: 10, marginTop: 10, flexWrap: "wrap" }}>
          <div style={{ fontFamily: MONO, fontSize: 22, fontWeight: 700, color: tiltColor }}>
            {tilt == null ? "—" : `${tilt > 0 ? "+" : ""}${tilt}%`}
          </div>
          <div style={{ fontFamily: DISP, fontSize: 12, color: T.muted }}>{impact.label || "India tilt"} · tap Show for headlines</div>
        </div>
      )}
      {showBody && (
      <>
      {notifyPerm !== "unsupported" && notifyPerm !== "granted" && (
        <button
          onClick={onEnableNotify}
          style={{
            marginTop: 10, width: "100%", background: T.panel2, color: T.fg, border: `1px solid ${T.amber}66`,
            borderRadius: 8, padding: "8px 10px", fontSize: 12, cursor: "pointer", fontFamily: DISP, textAlign: "left",
          }}
        >
          Allow short browser alerts for high-impact news
        </button>
      )}

      <div style={{ marginTop: 12, background: T.panel2, border: `1px solid ${tiltColor}55`, borderRadius: 10, padding: 12 }}>
        <div style={{ fontFamily: MONO, fontSize: 10, color: T.muted, letterSpacing: ".06em" }}>IMPACT ON INDIA</div>
        <div style={{ display: "flex", alignItems: "baseline", gap: 10, marginTop: 6 }}>
          <div style={{ fontFamily: MONO, fontSize: 28, fontWeight: 700, color: tiltColor, lineHeight: 1 }}>
            {tilt == null ? "—" : `${tilt > 0 ? "+" : ""}${tilt}%`}
          </div>
          <div style={{ fontFamily: DISP, fontSize: 13, fontWeight: 600, color: tiltColor }}>{impact.label || "—"}</div>
        </div>
        <div style={{ fontFamily: DISP, fontSize: 11, color: T.muted, marginTop: 6 }}>
          News tilt for Nifty (−100 bearish → +100 bullish)
          {impact.headline_count ? ` · ${impact.bullish_headlines} up / ${impact.bearish_headlines} down` : ""}
        </div>
        <div style={{ fontFamily: DISP, fontSize: 12, color: T.fg, lineHeight: 1.4, marginTop: 8 }}>
          {loading && !news ? "Reading headlines…" : (news?.pre_analysis || "No pre-read yet.")}
        </div>
        {news?.updated_at && (
          <div style={{ fontFamily: MONO, fontSize: 10, color: T.muted, marginTop: 8 }}>
            {new Date(news.updated_at).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit" })} IST
          </div>
        )}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, marginTop: 10 }}>
        <TapeChip label="NIFTY" quote={tape.nifty} />
        <TapeChip label="S&P" quote={tape.sp500} />
        <TapeChip label="NASDAQ" quote={tape.nasdaq} />
        <TapeChip label="BRENT" quote={tape.brent} />
      </div>

      {err && <div style={{ fontFamily: DISP, fontSize: 12, color: T.call, marginTop: 8 }}>{err}</div>}
      {NEWS_SECTIONS.map((sec) => {
        const items = sections[sec.id] || [];
        return (
          <div key={sec.id} style={{ marginTop: 14 }}>
            <div style={{ fontWeight: 700, fontSize: 13 }}>{sec.title}</div>
            <div style={{ fontFamily: DISP, fontSize: 11, color: T.muted }}>{sec.blurb}</div>
            {items.length === 0 && (
              <div style={{ fontFamily: DISP, fontSize: 12, color: T.muted, marginTop: 6 }}>No fresh headline in this bucket.</div>
            )}
            {items.map((h, i) => {
              const ip = h.india_pct;
              const ic = ip > 0 ? T.put : ip < 0 ? T.call : T.muted;
              return (
              <a
                key={`${sec.id}-${i}`}
                href={h.link || undefined}
                target="_blank"
                rel="noreferrer"
                style={{ display: "block", textDecoration: "none", marginTop: 8, paddingBottom: 8, borderBottom: `1px solid ${T.line}` }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "flex-start", flexWrap: "wrap" }}>
                  <div style={{ fontFamily: DISP, fontSize: 13, color: T.fg, lineHeight: 1.4, minWidth: 0, flex: "1 1 160px" }}>{h.headline}</div>
                  <div style={{
                    flexShrink: 0, fontFamily: MONO, fontSize: 11, fontWeight: 700, color: ic,
                    background: `${ic}18`, border: `1px solid ${ic}44`, borderRadius: 999, padding: "2px 7px",
                  }}>
                    India {ip > 0 ? "+" : ""}{ip ?? 0}%
                  </div>
                </div>
                {h.reason && <div style={{ fontFamily: DISP, fontSize: 11, color: T.muted, marginTop: 4 }}>{h.reason}</div>}
              </a>
              );
            })}
          </div>
        );
      })}
      </>
      )}
    </div>
  );
}

function Th({ children, hint }) {
  return (
    <th style={{ padding: "6px 8px", borderBottom: `1px solid ${T.line}`, fontWeight: 500, verticalAlign: "bottom" }}>
      <div>{children}</div>
      {hint && <div style={{ fontFamily: DISP, fontSize: 10, fontWeight: 400, color: T.muted, marginTop: 3, maxWidth: 110, lineHeight: 1.3 }}>{hint}</div>}
    </th>
  );
}

function SectionCard({ borderColor, children }) {
  return (
    <div className="section-card" style={{
      background: T.panel, border: `1px solid ${borderColor || T.line}`,
      borderRadius: 14, padding: 16, marginTop: 12,
    }}>
      {children}
    </div>
  );
}

function StatGrid({ children }) {
  return (
    <div className="stat-grid" style={{
      display: "grid",
      gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 140px), 1fr))",
      gap: 12,
      marginTop: 14,
    }}>
      {children}
    </div>
  );
}

function ContributionAlertsView({ backendUrl }) {
  const narrow = useNarrow(900);
  const [snap, setSnap] = useState(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [backtest, setBacktest] = useState(null);
  const [btErr, setBtErr] = useState("");
  const [btLoading, setBtLoading] = useState(false);
  const [btDays, setBtDays] = useState(15);
  const [toasts, setToasts] = useState([]);
  const [notifyPerm, setNotifyPerm] = useState(
    typeof Notification === "undefined" ? "unsupported" : Notification.permission,
  );
  const seenAlertKeys = useRef(new Set());
  const toastTimers = useRef({});
  const [news, setNews] = useState(null);
  const [newsErr, setNewsErr] = useState("");
  const [newsLoading, setNewsLoading] = useState(false);
  const [tape, setTape] = useState(null);
  const status = marketStatus();

  const dismissToast = useCallback((key) => {
    setToasts((prev) => prev.filter((a) => toastKey(a) !== key));
    if (toastTimers.current[key]) {
      clearTimeout(toastTimers.current[key]);
      delete toastTimers.current[key];
    }
  }, []);

  const pushToasts = useCallback((incoming) => {
    const fresh = [];
    for (const a of incoming || []) {
      const key = toastKey(a);
      if (!(a.symbol || a.headline) || seenAlertKeys.current.has(key)) continue;
      seenAlertKeys.current.add(key);
      fresh.push(a);
    }
    if (!fresh.length) return;
    setToasts((prev) => [...prev, ...fresh].slice(-4));
    for (const a of fresh) {
      fireBrowserNotification(a);
      const key = toastKey(a);
      toastTimers.current[key] = setTimeout(() => dismissToast(key), UNUSUAL_TOAST_MS);
    }
  }, [dismissToast]);

  useEffect(() => () => {
    Object.values(toastTimers.current).forEach(clearTimeout);
  }, []);

  useEffect(() => {
    pushToasts(snap?.early_warning?.new_alerts);
  }, [snap, pushToasts]);

  const load = useCallback(async () => {
    if (!backendUrl) return;
    setLoading(true);
    try {
      const base = backendUrl.replace(/\/$/, "");
      const res = await fetch(`${base}/api/index-engine/snapshot`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      setSnap(json);
      setErr(json.error || "");
    } catch (e) {
      setErr(e.message);
    } finally { setLoading(false); }
  }, [backendUrl]);

  const loadNews = useCallback(async (force = false) => {
    if (!backendUrl) return;
    const base = backendUrl.replace(/\/$/, "");
    const apply = (json) => {
      setNews(json);
      setNewsErr("");
      if (json.tape) setTape(json.tape);
      const hot = [];
      for (const items of Object.values(json.sections || {})) {
        for (const h of items || []) {
          if (h.impact === "high") hot.push({ kind: "news", ...h });
        }
      }
      pushToasts(hot);
    };
    setNewsLoading(true);
    try {
      // Headlines + tape first (no Gemini). Full scores fill in after.
      const liteRes = await fetch(`${base}/api/index-engine/news?lite=true`);
      if (liteRes.ok) apply(await liteRes.json());
    } catch (e) {
      setNewsErr(e.message);
    }
    try {
      const qs = force ? "?force=true" : "";
      const res = await fetch(`${base}/api/index-engine/news${qs}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      apply(await res.json());
    } catch (e) {
      setNewsErr(e.message);
    } finally { setNewsLoading(false); }
  }, [backendUrl, pushToasts]);

  const loadTape = useCallback(async () => {
    if (!backendUrl) return;
    try {
      const base = backendUrl.replace(/\/$/, "");
      const res = await fetch(`${base}/api/index-engine/tape`);
      if (!res.ok) return;
      const json = await res.json();
      if (json.tape) setTape(json.tape);
    } catch { /* keep last quotes */ }
  }, [backendUrl]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { loadNews(false); }, [loadNews]);
  useEffect(() => { loadTape(); }, [loadTape]);
  useEffect(() => {
    const id = setInterval(() => loadNews(false), 5 * 60_000);
    return () => clearInterval(id);
  }, [loadNews]);
  useEffect(() => {
    const id = setInterval(loadTape, 20_000);
    return () => clearInterval(id);
  }, [loadTape]);

  useEffect(() => {
    if (!backendUrl || !status.open) return;
    const sec = Math.max(5, snap?.config?.poll_seconds || 10);
    const id = setInterval(load, sec * 1000);
    return () => clearInterval(id);
  }, [backendUrl, status.open, load, snap?.config?.poll_seconds]);

  const runBacktest = async () => {
    setBtLoading(true); setBtErr("");
    try {
      const base = backendUrl.replace(/\/$/, "");
      const res = await fetch(`${base}/api/index-engine/backtest?days=${btDays}`);
      const json = await res.json();
      if (json.error) setBtErr(json.error);
      setBacktest(json);
    } catch (e) {
      setBtErr(e.message);
    } finally { setBtLoading(false); }
  };

  const attr = snap?.attribution;
  const recon = attr?.reconciliation || {};
  const movers = useMemo(() => {
    const rows = [...(attr?.stocks || [])];
    rows.sort((a, b) => {
      const aa = a.contribution_pts == null ? -1 : Math.abs(a.contribution_pts);
      const bb = b.contribution_pts == null ? -1 : Math.abs(b.contribution_pts);
      return bb - aa;
    });
    return rows.slice(0, 5);
  }, [attr?.stocks]);
  const ew = snap?.early_warning;
  const alerts = ew?.recent_alerts || ew?.new_alerts || [];
  const scored = useMemo(() => {
    const rows = [...(ew?.stocks || [])];
    rows.sort((a, b) => (b.score ?? -1) - (a.score ?? -1));
    return rows.slice(0, 5);
  }, [ew?.stocks]);
  const sweep = backtest?.threshold_sweep || [];
  const mx = backtest?.metrics?.matrix;
  const threshold = snap?.config?.alert_score_threshold ?? 78;
  const liveTape = useMemo(() => {
    const t = { ...(tape || news?.tape || {}) };
    if (attr?.index_ltp != null && attr?.index_prev_close) {
      const prev = attr.index_prev_close;
      t.nifty = {
        price: attr.index_ltp,
        previous_close: prev,
        pct_change: ((attr.index_ltp - prev) / prev) * 100,
        source: "upstox",
        live: true,
        market_state: status.open ? "REGULAR" : "CLOSED",
      };
    }
    return t;
  }, [tape, news, attr, status.open]);

  const urgencyColor = (s) => {
    if (s == null) return T.muted;
    if (s >= 78) return T.call;
    if (s >= 60) return T.amber;
    return T.cyan;
  };

  const urgencyLabel = (s) => {
    if (s == null) return "no data yet";
    if (s >= 78) return "unusual — would alert";
    if (s >= 60) return "elevated";
    return "quiet";
  };

  return (
    <div style={{ marginTop: 14 }}>
      <UnusualActivityToasts toasts={toasts} onDismiss={dismissToast} />
      <div className="contrib-layout">
        <aside className="news-aside">
          <NewsDesk
            news={news} loading={newsLoading} err={newsErr}
            collapsible={narrow}
            liveTape={liveTape}
            onRefresh={() => loadNews(false)}
            notifyPerm={notifyPerm}
            onEnableNotify={async () => {
              if (typeof Notification === "undefined") return;
              const perm = await Notification.requestPermission();
              setNotifyPerm(perm);
            }}
          />
        </aside>
        <div className="contrib-main">
      <PcrOiCard backendUrl={backendUrl} />

      <SectionCard>
        <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap", alignItems: "flex-start" }}>
          <div style={{ minWidth: 0, flex: "1 1 200px" }}>
            <div style={{ fontFamily: MONO, fontSize: 11, color: T.cyan }}>1 · FACT</div>
            <div style={{ fontSize: 16, fontWeight: 700, marginTop: 2 }}>Top 5 dragging Nifty now</div>
            <Hint>
              {narrow
                ? "Live ranking — the 5 names with the biggest Nifty point impact right now. Green = up, pink = down."
                : "Live ranking of the 5 heaviest names by |Nifty points| right now. The list reshuffles as prices move. Green = pushed Nifty up, pink = pulled it down."}
            </Hint>
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <Pill color={recon.data_stale ? T.amber : T.put}>
              {recon.data_stale ? (narrow ? "stale prices" : "prices look stale or incomplete") : "live prices ok"}
            </Pill>
            <button onClick={load} style={{ background: T.panel2, color: T.cyan, border: `1px solid ${T.line}`, borderRadius: 8, padding: "6px 12px", fontSize: 12, cursor: "pointer", fontFamily: MONO }}>
              {loading ? "…" : "↻"}
            </button>
          </div>
        </div>
        {!snap?.connected && (
          <div style={{ fontFamily: DISP, fontSize: 13, color: T.amber, marginTop: 12, lineHeight: 1.45 }}>
            Live stock prices need Upstox. Open <span style={{ fontFamily: MONO, color: T.cyan, overflowWrap: "anywhere" }}>http://127.0.0.1:8000/api/upstox/login</span> once, then refresh.
            {err ? ` (${err})` : ""}
          </div>
        )}
        <StatGrid>
          <Stat label="Nifty now" value={fmt(attr?.index_ltp, 1)} sub={attr?.index_prev_close != null ? `yesterday close ${fmt(attr.index_prev_close, 1)}` : ""} />
          <Stat label="Nifty today" value={fmtSigned(recon.actual_index_pts, 1)} color={(recon.actual_index_pts || 0) >= 0 ? T.put : T.call} sub="points since yesterday" />
          <Stat label={narrow ? "From top 20" : "From the tracked 20"} value={fmtSigned(recon.sum_contribution_pts, 1)} sub={`list below is the live top 5 · cover ${fmt(recon.coverage_pct, 1)}%`} />
          <Stat label={narrow ? "Other 30" : "The other 30 stocks"} value={fmtSigned(recon.unexplained_pts, 1)} color={recon.reconciliation_stale ? T.amber : T.muted} sub="leftover points we didn't assign" />
        </StatGrid>
        <Hint>
          {narrow
            ? "Leftover is normal — we only track the 20 heaviest names."
            : <>The leftover is normal — we only track the 20 heaviest names (~75% of Nifty), not all 50. Worry only if leftover is huge <i>and</i> the badge says prices look stale.</>}
        </Hint>
        {narrow ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 14 }}>
            {movers.map((s, i) => (
              <div key={s.symbol} style={{
                background: T.panel2, border: `1px solid ${T.line}`, borderRadius: 10, padding: "10px 12px",
                color: s.quote_stale ? T.muted : T.fg,
              }}>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "flex-start" }}>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontFamily: DISP, fontWeight: 700 }}>{i + 1}. {s.symbol}</div>
                    <div style={{ fontFamily: DISP, fontSize: 11, color: T.muted }}>{s.name}</div>
                  </div>
                  <div style={{
                    fontFamily: MONO, fontWeight: 700, fontSize: 16,
                    color: (s.contribution_pts || 0) >= 0 ? T.put : T.call, flexShrink: 0,
                  }}>
                    {fmtSigned(s.contribution_pts, 2)} <span style={{ fontSize: 11, fontWeight: 500, color: T.muted }}>pts</span>
                  </div>
                </div>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8, marginTop: 10 }}>
                  <div>
                    <div style={{ fontFamily: MONO, fontSize: 9, color: T.muted }}>WEIGHT</div>
                    <div style={{ fontFamily: MONO, fontSize: 12 }}>{fmt(s.weight_pct, 1)}%</div>
                  </div>
                  <div>
                    <div style={{ fontFamily: MONO, fontSize: 9, color: T.muted }}>MOVE</div>
                    <div style={{ fontFamily: MONO, fontSize: 12, color: (s.pct_change || 0) >= 0 ? T.put : T.call }}>{fmtSigned(s.pct_change, 2)}%</div>
                  </div>
                  <div>
                    <div style={{ fontFamily: MONO, fontSize: 9, color: T.muted }}>PRICE</div>
                    <div style={{ fontFamily: MONO, fontSize: 12 }}>{fmt(s.ltp, 2)}</div>
                  </div>
                </div>
              </div>
            ))}
            {!movers.length && (
              <div style={{ padding: 16, color: T.muted, textAlign: "center", fontFamily: DISP }}>Connect Upstox to see which stocks moved Nifty.</div>
            )}
          </div>
        ) : (
        <div style={{ overflowX: "auto", marginTop: 14, WebkitOverflowScrolling: "touch" }}>
          <table style={{ width: "100%", minWidth: 480, borderCollapse: "collapse", fontFamily: MONO, fontSize: 11 }}>
            <thead>
              <tr style={{ color: T.muted, textAlign: "left" }}>
                <Th>#</Th>
                <Th hint="Company">Stock</Th>
                <Th hint="How big it is inside Nifty">Weight</Th>
                <Th hint="Stock up/down today">Stock move</Th>
                <Th hint="How many Nifty points this caused">Nifty points</Th>
                <Th hint="Last traded price">Price</Th>
              </tr>
            </thead>
            <tbody>
              {movers.map((s, i) => (
                <tr key={s.symbol} style={{ color: s.quote_stale ? T.muted : T.fg }}>
                  <td style={{ padding: "7px 8px", borderBottom: `1px solid ${T.line}88`, color: T.muted }}>{i + 1}</td>
                  <td style={{ padding: "7px 8px", borderBottom: `1px solid ${T.line}88` }}>
                    <div style={{ fontFamily: DISP, fontWeight: 600 }}>{s.symbol}</div>
                    <div style={{ color: T.muted, fontSize: 10 }}>{s.name}</div>
                  </td>
                  <td style={{ padding: "7px 8px", borderBottom: `1px solid ${T.line}88` }}>{fmt(s.weight_pct, 1)}%</td>
                  <td style={{ padding: "7px 8px", borderBottom: `1px solid ${T.line}88`, color: (s.pct_change || 0) >= 0 ? T.put : T.call }}>{fmtSigned(s.pct_change, 2)}%</td>
                  <td style={{ padding: "7px 8px", borderBottom: `1px solid ${T.line}88`, color: (s.contribution_pts || 0) >= 0 ? T.put : T.call, fontWeight: 600 }}>{fmtSigned(s.contribution_pts, 2)}</td>
                  <td style={{ padding: "7px 8px", borderBottom: `1px solid ${T.line}88` }}>{fmt(s.ltp, 2)}</td>
                </tr>
              ))}
              {!movers.length && (
                <tr><td colSpan={6} style={{ padding: 16, color: T.muted, textAlign: "center", fontFamily: DISP }}>Connect Upstox to see which stocks moved Nifty.</td></tr>
              )}
            </tbody>
          </table>
        </div>
        )}
      </SectionCard>

      <SectionCard borderColor={`${T.amber}55`}>
        <div style={{ display: "flex", justifyContent: "space-between", gap: 8, flexWrap: "wrap", alignItems: "flex-start" }}>
          <div style={{ minWidth: 0, flex: "1 1 200px" }}>
            <div style={{ fontFamily: MONO, fontSize: 11, color: T.amber }}>2 · HEADS-UP{narrow ? "" : "  ·  not a prediction"}</div>
            <div style={{ fontSize: 16, fontWeight: 700, marginTop: 2 }}>Top 5 unusual heavy stocks</div>
            <Hint>
              {narrow
                ? <>Live ranking by busy score. Alert only if score ≥ <b style={{ color: T.fg }}>{threshold}</b>. The 5 names reshuffle as tape changes.</>
                : <>
              Live ranking of the 5 busiest heavy names (0–100). The list reshuffles as volume, VWAP, and pressure change.
              An alert only appears if the score reaches <b style={{ color: T.fg }}>{threshold}</b>. Empty feed = quiet. That is OK.
                </>}
            </Hint>
          </div>
          <Pill color={T.amber}>alert if score ≥ {threshold}</Pill>
        </div>
        {notifyPerm !== "unsupported" && notifyPerm !== "granted" && (
          <button
            onClick={async () => {
              if (typeof Notification === "undefined") return;
              const perm = await Notification.requestPermission();
              setNotifyPerm(perm);
            }}
            style={{
              marginTop: 12, width: narrow ? "100%" : "auto", background: T.panel2, color: T.fg, border: `1px solid ${T.call}66`,
              borderRadius: 8, padding: "8px 12px", fontSize: 12, cursor: "pointer", fontFamily: DISP,
            }}
          >
            Allow browser pop-up alerts (they vanish after a few seconds)
          </button>
        )}
        {notifyPerm === "granted" && (
          <Hint>Browser alerts are on. A pink pop-up will flash for ~8 seconds when a heavy stock looks unusual — it will not sit on this page.</Hint>
        )}

        <div style={{ marginTop: 14 }}>
          <div style={{ fontSize: 14, fontWeight: 700, marginBottom: 4 }}>Recent heads-ups</div>
          <Hint>{narrow ? "Quiet log only — alerts are short pop-ups." : "These are a quiet log only. The pink alert is a short pop-up (and a browser notification if you allowed it), not a card that stays here."}</Hint>
          {alerts.length === 0 && (
            <div style={{ fontFamily: DISP, fontSize: 13, color: T.muted, marginTop: 10, padding: 12, background: T.panel2, borderRadius: 8, border: `1px dashed ${T.line}` }}>
              None so far this session. Quiet is the default.
            </div>
          )}
          {alerts.slice(0, 8).map((a, i) => (
            <div key={`${a.fired_at}-${a.symbol}-${i}`} style={{
              borderBottom: `1px solid ${T.line}`, padding: "8px 0",
            }}>
              <div style={{ display: "flex", justifyContent: "space-between", gap: 8, flexWrap: "wrap" }}>
                <div style={{ fontFamily: DISP, fontSize: 13, color: T.fg }}>{a.symbol} · score {fmt(a.score, 0)}</div>
                <div style={{ fontFamily: MONO, fontSize: 10, color: T.muted }}>{a.fired_at ? new Date(a.fired_at).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit" }) : ""} IST</div>
              </div>
              <div style={{ fontFamily: DISP, fontSize: 12, color: T.muted, marginTop: 4, lineHeight: 1.4 }}>{a.message}</div>
            </div>
          ))}
        </div>

        <div style={{ fontSize: 14, fontWeight: 700, marginTop: 16, marginBottom: 4 }}>Live top 5 busy-ness</div>
        <Hint>{narrow ? "Higher = more unusual. Ranking updates with each poll." : "Higher score = more unusual vs that stock’s own recent tape. Ranking updates every few seconds. Colour: quiet (teal), elevated (amber), would-alert (pink)."}</Hint>
        {narrow ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 8 }}>
            {scored.map((s, i) => (
              <div key={s.symbol} style={{
                background: T.panel2, border: `1px solid ${T.line}`, borderRadius: 10, padding: "10px 12px",
              }}>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "flex-start" }}>
                  <div style={{ fontFamily: DISP, fontWeight: 700 }}>{i + 1}. {s.symbol}</div>
                  <div style={{ textAlign: "right" }}>
                    <div style={{ fontFamily: MONO, fontWeight: 700, fontSize: 18, color: urgencyColor(s.score) }}>{fmt(s.score, 0)}</div>
                    <div style={{ fontFamily: DISP, fontSize: 10, color: T.muted }}>{urgencyLabel(s.score)}</div>
                  </div>
                </div>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginTop: 10 }}>
                  <div>
                    <div style={{ fontFamily: MONO, fontSize: 9, color: T.muted }}>VOLUME</div>
                    <div style={{ fontFamily: MONO, fontSize: 12 }}>{s.volume_surge == null ? "—" : `${fmt(s.volume_surge, 1)}×`}</div>
                  </div>
                  <div>
                    <div style={{ fontFamily: MONO, fontSize: 9, color: T.muted }}>OI</div>
                    <div style={{ fontFamily: MONO, fontSize: 12 }}>{fmtSigned(s.oi_change_pct, 1)}{s.oi_change_pct == null ? "" : "%"}</div>
                  </div>
                  <div>
                    <div style={{ fontFamily: MONO, fontSize: 9, color: T.muted }}>VS VWAP</div>
                    <div style={{ fontFamily: MONO, fontSize: 12 }}>{fmtSigned(s.vwap_dev_pct, 2)}{s.vwap_dev_pct == null ? "" : "%"}</div>
                  </div>
                  <div>
                    <div style={{ fontFamily: MONO, fontSize: 9, color: T.muted }}>BUY/SELL</div>
                    <div style={{ fontFamily: MONO, fontSize: 12 }}>{fmtSigned(s.imbalance, 2)}</div>
                  </div>
                </div>
              </div>
            ))}
            {!scored.length && (
              <div style={{ padding: 16, color: T.muted, textAlign: "center", fontFamily: DISP }}>No scores until Upstox is connected.</div>
            )}
          </div>
        ) : (
        <div style={{ overflowX: "auto", marginTop: 8, WebkitOverflowScrolling: "touch" }}>
          <table style={{ width: "100%", minWidth: 520, borderCollapse: "collapse", fontFamily: MONO, fontSize: 11 }}>
            <thead>
              <tr style={{ color: T.muted, textAlign: "left" }}>
                <Th>#</Th>
                <Th>Stock</Th>
                <Th hint="0 quiet → 100 extreme">Busy score</Th>
                <Th hint="1 = normal volume, 3 = 3× usual">Volume</Th>
                <Th hint="Open interest change; often blank for stocks">OI</Th>
                <Th hint="Price vs today’s volume-average">Vs VWAP</Th>
                <Th hint="+ buyers, − sellers">Buy/sell</Th>
              </tr>
            </thead>
            <tbody>
              {scored.map((s, i) => (
                <tr key={s.symbol}>
                  <td style={{ padding: "6px 8px", borderBottom: `1px solid ${T.line}88`, color: T.muted }}>{i + 1}</td>
                  <td style={{ padding: "6px 8px", borderBottom: `1px solid ${T.line}88` }}>{s.symbol}</td>
                  <td style={{ padding: "6px 8px", borderBottom: `1px solid ${T.line}88`, color: urgencyColor(s.score), fontWeight: 700 }}>
                    {fmt(s.score, 0)}
                    <div style={{ fontFamily: DISP, fontSize: 10, fontWeight: 400, color: T.muted }}>{urgencyLabel(s.score)}</div>
                  </td>
                  <td style={{ padding: "6px 8px", borderBottom: `1px solid ${T.line}88` }}>{s.volume_surge == null ? "—" : `${fmt(s.volume_surge, 1)}×`}</td>
                  <td style={{ padding: "6px 8px", borderBottom: `1px solid ${T.line}88` }}>{fmtSigned(s.oi_change_pct, 1)}{s.oi_change_pct == null ? "" : "%"}</td>
                  <td style={{ padding: "6px 8px", borderBottom: `1px solid ${T.line}88` }}>{fmtSigned(s.vwap_dev_pct, 2)}{s.vwap_dev_pct == null ? "" : "%"}</td>
                  <td style={{ padding: "6px 8px", borderBottom: `1px solid ${T.line}88` }}>{fmtSigned(s.imbalance, 2)}</td>
                </tr>
              ))}
              {!scored.length && (
                <tr><td colSpan={7} style={{ padding: 16, color: T.muted, textAlign: "center", fontFamily: DISP }}>No scores until Upstox is connected.</td></tr>
              )}
            </tbody>
          </table>
        </div>
        )}
      </SectionCard>

      <SectionCard>
        <div style={{ display: "flex", justifyContent: "space-between", gap: 8, flexWrap: "wrap", alignItems: "flex-start" }}>
          <div style={{ minWidth: 0, flex: "1 1 200px" }}>
            <div style={{ fontFamily: MONO, fontSize: 11, color: T.muted }}>3 · REPORT CARD</div>
            <div style={{ fontSize: 16, fontWeight: 700, marginTop: 2 }}>Did the heads-up work on old days?</div>
            <Hint>
              {narrow
                ? "Replays past days: when busy, did Nifty move ~40 pts in 15 min?"
                : "This does not trade. It asks: on past days, when a heavy stock looked this busy, did Nifty move at least about 40 points in the next 15 minutes? Use this before you trust Section 2. You can skip it while learning the two tables above."}
            </Hint>
          </div>
          <div className="report-controls" style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", width: narrow ? "100%" : "auto" }}>
            <span style={{ fontFamily: DISP, fontSize: 12, color: T.muted }}>Look back</span>
            <input type="number" min={5} max={60} value={btDays} onChange={(e) => setBtDays(+e.target.value)}
              style={{ width: 56, background: T.ink, border: `1px solid ${T.line}`, color: T.fg, borderRadius: 8, padding: "6px 8px", fontFamily: MONO, fontSize: 12 }} />
            <span style={{ fontFamily: DISP, fontSize: 12, color: T.muted }}>days</span>
            <button onClick={runBacktest} disabled={btLoading}
              style={{
                background: T.cyan, color: T.ink, border: "none", borderRadius: 8, padding: "8px 14px",
                fontWeight: 600, cursor: "pointer", fontSize: 12, flex: narrow ? "1 1 auto" : "none",
              }}>
              {btLoading ? "Checking…" : "Run check"}
            </button>
          </div>
        </div>
        {btErr && <div style={{ fontFamily: DISP, fontSize: 13, color: T.call, marginTop: 8 }}>{btErr}</div>}
        {backtest?.metrics && (
          <StatGrid>
            <Stat label={narrow ? "Precision" : "When it beeped, Nifty really moved"} value={backtest.metrics.precision_pct != null ? `${backtest.metrics.precision_pct}%` : "—"} color={T.cyan} sub="precision — higher = fewer false alarms" />
            <Stat label={narrow ? "Recall" : "Big Nifty moves we had already flagged"} value={backtest.metrics.recall_pct != null ? `${backtest.metrics.recall_pct}%` : "—"} color={T.amber} sub="recall — higher = fewer missed moves" />
            <Stat label={narrow ? "Alerts" : "Heads-ups in this sample"} value={backtest.metrics.alert_count} sub={`busy-score bar ${backtest.metrics.score_threshold}`} />
          </StatGrid>
        )}
        {mx && (
          <div style={{ marginTop: 12, fontFamily: DISP, fontSize: 13, color: T.muted, lineHeight: 1.55 }}>
            Simple count: alarm + Nifty moved = {mx.predicted_alert?.actual_move};
            alarm + Nifty did nothing = {mx.predicted_alert?.no_move};
            no alarm + Nifty moved anyway = {mx.no_alert?.actual_move};
            quiet + Nifty quiet = {mx.no_alert?.no_move}.
          </div>
        )}
        {sweep.length > 1 && (
          <>
            <Hint>{narrow ? "Harder threshold → fewer false alarms, more misses." : "Chart: if we make the alarm harder to trigger (threshold →), false alarms usually fall (teal) but we miss more real moves (amber)."}</Hint>
            <div style={{ height: narrow ? 200 : 240, marginTop: 8 }}>
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={sweep} margin={{ top: 8, right: 12, bottom: 4, left: -8 }}>
                  <CartesianGrid stroke={T.line} strokeDasharray="2 4" vertical={false} />
                  <XAxis dataKey="threshold" tick={{ fill: T.muted, fontSize: 10, fontFamily: MONO }} axisLine={{ stroke: T.line }} tickLine={false} />
                  <YAxis domain={[0, 100]} tick={{ fill: T.muted, fontSize: 10, fontFamily: MONO }} axisLine={false} tickLine={false} width={36} />
                  <Tooltip contentStyle={{ background: T.panel2, border: `1px solid ${T.line}`, borderRadius: 8, fontFamily: MONO, fontSize: 11 }} />
                  <Line type="monotone" dataKey="precision_pct" name="beeped and Nifty moved %" stroke={T.cyan} strokeWidth={2} dot={{ r: 3 }} isAnimationActive={false} />
                  <Line type="monotone" dataKey="recall_pct" name="big moves we caught %" stroke={T.amber} strokeWidth={2} dot={{ r: 3 }} isAnimationActive={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </>
        )}
      </SectionCard>
        </div>
      </div>
    </div>
  );
}

export default function App() {
  const narrow = useNarrow(900);
  const [view, setView] = useState("pcr");
  const [symbol, setSymbol] = useState("NIFTY");
  const [metric, setMetric] = useState("oi");
  const [data, setData] = useState(null);
  // Client-accumulated PCR history — the serverless backend only ever
  // returns the current reading (no stored snapshots array anymore), so
  // this builds the session's time series the same way the candle chart
  // builds candles from repeated polls. Resets on symbol/backend change.
  const [snapshots, setSnapshots] = useState([]);
  const [backendUrl, setBackendUrl] = useState(DEFAULT_BACKEND_URL);
  const [urlDraft, setUrlDraft] = useState(DEFAULT_BACKEND_URL);
  const [intervalMin, setIntervalMin] = useState(3);
  const [showCfg, setShowCfg] = useState(false);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");

  const status = marketStatus();

  const load = useCallback(async () => {
    if (!backendUrl) { setErr("No backend connected"); return; }
    setLoading(true);
    try {
      const base = backendUrl.replace(/\/$/, "");
      const res = await fetch(`${base}/api/pcr/today?symbol=${symbol}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      setData({ symbol: json.symbol || symbol, expiry: json.expiry || "", updatedAt: json.updatedAt || new Date().toISOString() });
      const snap = normalizeSnapshot(json);
      if (snap) {
        setErr("");
        setSnapshots((prev) => {
          const last = prev[prev.length - 1];
          if (last && last.t === snap.t && last.pcrOi === snap.pcrOi && last.pcrVol === snap.pcrVol) return prev;
          return [...prev, snap].slice(-500);
        });
      } else {
        setErr("Backend has no PCR reading yet");
      }
    } catch (e) {
      setErr(e.message);
    } finally { setLoading(false); }
  }, [backendUrl, symbol]);

  // Seed from the backend's persisted full-day history (so any visitor sees
  // the whole day, not just what's accumulated since they opened the tab).
  // Merges rather than overwrites, in case the live load() below already
  // appended a fresher reading before this resolves.
  useEffect(() => {
    let cancelled = false;
    setSnapshots([]);
    (async () => {
      if (!backendUrl) return;
      try {
        const base = backendUrl.replace(/\/$/, "");
        const res = await fetch(`${base}/api/pcr/history?symbol=${symbol}`);
        if (!res.ok || cancelled) return;
        const json = await res.json();
        const hist = (json.snapshots || []).map((s) => ({
          t: s.t, pcrOi: num(s.pcrOi), pcrVol: num(s.pcrVol), putOi: num(s.putOi), callOi: num(s.callOi),
        })).filter((s) => s.t);
        if (!hist.length || cancelled) return;
        setSnapshots((prev) => {
          const histTimes = new Set(hist.map((h) => h.t));
          return [...hist, ...prev.filter((p) => !histTimes.has(p.t))];
        });
      } catch {
        /* history is a nice-to-have seed; live polling still works without it */
      }
    })();
    return () => { cancelled = true; };
  }, [symbol, backendUrl]);

  useEffect(() => { load(); }, [load]);

  // auto refresh during market hours
  useEffect(() => {
    if (!backendUrl || !status.open) return;
    const id = setInterval(load, Math.max(1, intervalMin) * 60_000);
    return () => clearInterval(id);
  }, [backendUrl, status.open, intervalMin, load]);

  const snaps = snapshots;
  const cur = snaps[snaps.length - 1] || {};
  const first = snaps[0] || {};
  const key = metric === "oi" ? "pcrOi" : "pcrVol";
  const curVal = cur[key];
  const delta = curVal != null && first[key] != null ? curVal - first[key] : null;

  const sentiment = useMemo(() => {
    if (curVal == null) return { label: "—", color: T.muted };
    if (curVal > 1.05) return { label: "Put-heavy", color: T.put };
    if (curVal < 0.95) return { label: "Call-heavy", color: T.call };
    return { label: "Balanced", color: T.amber };
  }, [curVal]);

  const yVals = snaps.map((s) => s[key]).filter((v) => v != null);
  const yMin = yVals.length ? Math.min(...yVals, 0.9) - 0.08 : 0.5;
  const yMax = yVals.length ? Math.max(...yVals, 1.1) + 0.08 : 1.5;
  const tickEvery = Math.max(1, Math.ceil(snaps.length / 7));

  const lastDot = (props) => {
    const { cx, cy, index } = props;
    if (index !== snaps.length - 1 || cx == null) return <g />;
    return (
      <g>
        <circle cx={cx} cy={cy} r={9} fill={T.amber} opacity={0.25}>
          <animate attributeName="r" values="6;13;6" dur="1.8s" repeatCount="indefinite" />
          <animate attributeName="opacity" values="0.35;0;0.35" dur="1.8s" repeatCount="indefinite" />
        </circle>
        <circle cx={cx} cy={cy} r={4} fill={T.amber} stroke={T.ink} strokeWidth={1.5} />
      </g>
    );
  };

  return (
    <div style={{ background: T.ink, minHeight: "100vh", color: T.fg, fontFamily: DISP, padding: "14px 14px calc(18px + env(safe-area-inset-bottom))" }}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap');
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.25} }
        * { box-sizing: border-box; }
        input, button, select { font-family: inherit; }
        img { max-width: 100%; }
        .contrib-layout { display: flex; gap: 14px; align-items: flex-start; }
        .news-aside { flex: 0 0 340px; width: 340px; max-width: 100%; position: sticky; top: 12px; max-height: calc(100vh - 24px); overflow-y: auto; -webkit-overflow-scrolling: touch; }
        .contrib-main { flex: 1 1 560px; min-width: 0; }
        @media (max-width: 900px) {
          .contrib-layout { flex-direction: column; }
          .news-aside { flex: none; width: 100%; position: static; max-height: none; overflow: visible; }
          .contrib-main { flex: none; width: 100%; }
          .section-card { padding: 12px !important; border-radius: 12px !important; }
          .stat-grid { grid-template-columns: 1fr 1fr !important; gap: 10px !important; }
          .report-controls { width: 100%; }
          .pcr-hero { font-size: 36px !important; }
          .spot-scroll { height: 220px !important; }
          .ua-toasts { top: auto !important; bottom: calc(12px + env(safe-area-inset-bottom)); left: 8px; right: 8px; width: auto !important; max-width: none !important; }
          input, select, textarea { font-size: 16px !important; }
          button { min-height: 40px; }
        }
      `}</style>

      <div style={{ maxWidth: view === "contribution" ? 1280 : 760, margin: "0 auto", width: "100%" }}>

        {/* header */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, flexWrap: "wrap" }}>
          <div>
            <div style={{ fontSize: 20, fontWeight: 700, letterSpacing: "-.01em" }}>
              {view === "contribution" ? <>Index <span style={{ color: T.cyan }}>Contribution</span></> : <>PCR <span style={{ color: T.cyan }}>Session Clock</span></>}
            </div>
            <div style={{ fontFamily: MONO, fontSize: 11, color: T.muted, marginTop: 1 }}>
              {view === "contribution" ? "Who moved Nifty today, plus a heads-up if a heavy stock looks unusually busy" : "Intraday Put-Call Ratio · NSE indices"}
            </div>
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <Pill color={status.open ? T.put : T.muted} dot={status.open}>{status.label}</Pill>
            <a href="./premarket.html"
              style={{ background: T.panel, color: T.fg, border: `1px solid ${T.line}`, borderRadius: 8, padding: "6px 10px", fontSize: 12, textDecoration: "none" }}>
              Pre-Market Brief →
            </a>
            <button onClick={() => { setShowCfg((s) => !s); setUrlDraft(backendUrl); }}
              style={{ background: T.panel, color: T.fg, border: `1px solid ${T.line}`, borderRadius: 8, padding: "6px 10px", fontSize: 12, cursor: "pointer" }}>
              ⚙ Source
            </button>
          </div>
        </div>

        {/* config panel */}
        {showCfg && (
          <div style={{ background: T.panel, border: `1px solid ${T.line}`, borderRadius: 12, padding: 12, marginTop: 12 }}>
            <div style={{ fontSize: 12, color: T.muted, marginBottom: 8, fontFamily: MONO }}>
              Backend base URL — expects <span style={{ color: T.cyan }}>GET /pcr/today?symbol=NIFTY</span> and <span style={{ color: T.cyan }}>GET /optionchain/today?symbol=NIFTY</span>.
            </div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <input value={urlDraft} onChange={(e) => setUrlDraft(e.target.value)}
                placeholder="https://your-backend.example.com"
                style={{ flex: 1, minWidth: 180, background: T.ink, border: `1px solid ${T.line}`, color: T.fg, borderRadius: 8, padding: "8px 10px", fontFamily: MONO, fontSize: 12 }} />
              <div style={{ display: "flex", alignItems: "center", gap: 6, fontFamily: MONO, fontSize: 12, color: T.muted }}>
                every
                <input type="number" min={1} value={intervalMin} onChange={(e) => setIntervalMin(+e.target.value)}
                  style={{ width: 52, background: T.ink, border: `1px solid ${T.line}`, color: T.fg, borderRadius: 8, padding: "8px", fontFamily: MONO, fontSize: 12 }} />
                min
              </div>
              <button onClick={() => { setBackendUrl(urlDraft.trim()); setShowCfg(false); }}
                style={{ background: T.cyan, color: T.ink, border: "none", borderRadius: 8, padding: "8px 16px", fontWeight: 600, cursor: "pointer" }}>
                Connect
              </button>
            </div>
          </div>
        )}

        <div style={{ display: "flex", gap: 6, marginTop: 14 }}>
          {[["pcr", narrow ? "PCR" : "PCR Clock"], ["contribution", narrow ? "Alerts" : "Contribution & Alerts"]].map(([id, lbl]) => (
            <button key={id} onClick={() => setView(id)}
              style={{
                flex: 1, padding: "9px 4px", borderRadius: 8, cursor: "pointer", fontFamily: MONO, fontSize: 12, fontWeight: 600,
                background: view === id ? T.cyan : T.panel, color: view === id ? T.ink : T.muted,
                border: `1px solid ${view === id ? T.cyan : T.line}`,
              }}>
              {lbl}
            </button>
          ))}
        </div>

        {view === "contribution" ? (
          <ContributionAlertsView backendUrl={backendUrl} />
        ) : (
        <>

        {/* symbol tabs */}
        <div style={{ display: "flex", gap: 6, marginTop: 14, overflowX: "auto" }}>
          {SYMBOLS.map((s) => (
            <button key={s} onClick={() => setSymbol(s)}
              style={{
                flex: 1, padding: "9px 4px", borderRadius: 8, cursor: "pointer", fontFamily: MONO, fontSize: 12, fontWeight: 600, letterSpacing: ".03em",
                background: symbol === s ? T.cyan : T.panel, color: symbol === s ? T.ink : T.muted,
                border: `1px solid ${symbol === s ? T.cyan : T.line}`,
              }}>
              {s}
            </button>
          ))}
        </div>

        {/* live spot-price chart, plus a link out to the real TradingView chart */}
        <SpotChart backendUrl={backendUrl} symbol={symbol} />

        {/* hero */}
        <div style={{ background: T.panel, border: `1px solid ${T.line}`, borderRadius: 14, padding: 16, marginTop: 12 }}>
          <div style={{ display: "flex", alignItems: "flex-end", gap: 16, flexWrap: "wrap" }}>
            <div>
              <div style={{ fontFamily: DISP, fontSize: 10, letterSpacing: ".12em", textTransform: "uppercase", color: T.muted }}>
                Current PCR ({metric === "oi" ? "OI" : "Volume"})
              </div>
              <div className="pcr-hero" style={{ fontFamily: MONO, fontSize: 48, fontWeight: 600, lineHeight: 1, color: T.fg }}>
                {fmt(curVal)}
              </div>
            </div>
            <div style={{ paddingBottom: 6, display: "flex", flexDirection: "column", gap: 6 }}>
              <Pill color={sentiment.color}>{sentiment.label}</Pill>
              {delta != null && (
                <span style={{ fontFamily: MONO, fontSize: 12, color: delta >= 0 ? T.put : T.call }}>
                  {delta >= 0 ? "▲" : "▼"} {fmt(Math.abs(delta))} since open
                </span>
              )}
            </div>
          </div>

          <div style={{ display: "flex", gap: 16, marginTop: 16, paddingTop: 14, borderTop: `1px solid ${T.line}`, flexWrap: "wrap" }}>
            <Stat label="Put OI" value={fmtOi(cur.putOi)} color={T.put} />
            <Stat label="Call OI" value={fmtOi(cur.callOi)} color={T.call} />
            <Stat label="Snapshots" value={snaps.length} sub={data?.expiry || ""} />
          </div>
        </div>

        {/* metric toggle */}
        <div style={{ display: "flex", gap: 6, marginTop: 12, flexWrap: "wrap" }}>
          {[["oi", "PCR · Open Interest"], ["vol", "PCR · Volume"]].map(([k, lbl]) => (
            <button key={k} onClick={() => setMetric(k)}
              style={{
                padding: "7px 12px", borderRadius: 8, cursor: "pointer", fontFamily: MONO, fontSize: 11,
                background: metric === k ? T.panel2 : "transparent", color: metric === k ? T.cyan : T.muted,
                border: `1px solid ${metric === k ? T.cyan + "66" : T.line}`,
              }}>
              {lbl}
            </button>
          ))}
        </div>

        {/* chart */}
        <div style={{ background: T.panel, border: `1px solid ${T.line}`, borderRadius: 14, padding: "16px 8px 8px", marginTop: 12 }}>
          <div style={{ height: 300 }}>
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={snaps} margin={{ top: 4, right: 14, bottom: 4, left: -8 }}>
                <CartesianGrid stroke={T.line} strokeDasharray="2 4" vertical={false} />
                <ReferenceArea y1={0.9} y2={1.1} fill={T.cyan} fillOpacity={0.05} />
                <ReferenceLine y={1} stroke={T.amber} strokeDasharray="4 4" strokeOpacity={0.6}
                  label={{ value: "1.00 pivot", position: "insideTopRight", fill: T.amber, fontSize: 10, fontFamily: MONO }} />
                <XAxis dataKey="t" tick={{ fill: T.muted, fontSize: 10, fontFamily: MONO }}
                  interval={tickEvery - 1} tickLine={false} axisLine={{ stroke: T.line }} />
                <YAxis domain={[yMin, yMax]} tick={{ fill: T.muted, fontSize: 10, fontFamily: MONO }}
                  tickLine={false} axisLine={false} width={44} tickFormatter={(v) => v.toFixed(2)} />
                <Tooltip content={<TT metric={metric} />} />
                <Line type="monotone" dataKey={key} stroke={T.cyan} strokeWidth={2}
                  dot={lastDot} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>

        {/* option chain add-on */}
        <OptionChain backendUrl={backendUrl} symbol={symbol} />

        {/* footer */}
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 12, flexWrap: "wrap", gap: 8 }}>
          <div style={{ fontFamily: MONO, fontSize: 11, color: data?.updatedAt ? T.muted : T.call }}>
            {data?.updatedAt
              ? `● Live · updated ${new Date(data.updatedAt).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit" })} IST`
              : "○ Not connected to live data"}
            {loading && " · refreshing…"}
          </div>
          <button onClick={load} disabled={loading}
            style={{ background: T.panel, color: T.cyan, border: `1px solid ${T.line}`, borderRadius: 8, padding: "6px 12px", fontSize: 12, cursor: "pointer", fontFamily: MONO }}>
            ↻ Refresh
          </button>
        </div>
        {err && <div style={{ fontFamily: MONO, fontSize: 11, color: T.call, marginTop: 6 }}>{err}</div>}

        <div style={{ fontFamily: MONO, fontSize: 10, color: T.muted, marginTop: 16, lineHeight: 1.6, opacity: .8 }}>
          PCR &gt; 1 = more puts open (often read as hedged / defensive positioning); PCR &lt; 1 = more calls.
          Interpretation is contextual — extremes are frequently read contrarian. Not investment advice.
        </div>
        </>
        )}
      </div>
    </div>
  );
}