// Chart wrappers around lightweight-charts (bundled locally, TradingView OSS).
import { createChart, ColorType } from 'lightweight-charts';
import { useEffect, useRef } from 'react';
import { fmtTmnAxis } from './util';

// All chart timestamps are unix seconds (UTC). lightweight-charts would render
// them as UTC, so we format both the axis ticks and the crosshair in Iran time.
const fmtTehran = (ts, opts) =>
  new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Tehran', ...opts })
    .format(new Date(ts * 1000));

const BASE_OPTS = {
  layout: {
    background: { type: ColorType.Solid, color: 'transparent' },
    textColor: '#8a95a8',
    fontFamily: "'Inter', sans-serif",
    fontSize: 11,
  },
  grid: {
    vertLines: { color: '#1e2a3f55' },
    horzLines: { color: '#1e2a3f55' },
  },
  rightPriceScale: { borderColor: '#1e2a3f' },
  localization: {
    timeFormatter: (t) => fmtTehran(t, { month: 'short', day: 'numeric',
                                         hour: '2-digit', minute: '2-digit' }),
  },
  timeScale: {
    borderColor: '#1e2a3f', timeVisible: true, secondsVisible: false,
    tickMarkFormatter: (t, tickType) => {
      // 0=Year 1=Month 2=Day 3=Time 4=TimeWithSeconds
      if (tickType === 0) return fmtTehran(t, { year: 'numeric' });
      if (tickType === 1) return fmtTehran(t, { month: 'short' });
      if (tickType === 2) return fmtTehran(t, { day: 'numeric', month: 'short' });
      return fmtTehran(t, { hour: '2-digit', minute: '2-digit' });
    },
  },
  crosshair: { mode: 1 },
  autoSize: true,
  // A plain mouse wheel scrolls the PAGE; zoom is Ctrl/⌘ + wheel (see
  // ctrlWheelZoom), drag-to-pan and pinch still work.
  handleScroll: { mouseWheel: false, pressedMouseMove: true,
                  horzTouchDrag: true, vertTouchDrag: false },
  handleScale: { mouseWheel: false, pinch: true, axisPressedMouseMove: true },
};

// TMN prices: "21.373B" / "262,612" on the axis and last-price label — no ".00".
const TMN_FORMAT = { type: 'custom', formatter: fmtTmnAxis, minMove: 1 };

/** Ctrl/⌘ + wheel zooms the time axis; returns the cleanup function. */
function ctrlWheelZoom(el, chart) {
  const onWheel = (e) => {
    if (!(e.ctrlKey || e.metaKey)) return;          // let the page scroll
    e.preventDefault();
    const ts = chart.timeScale();
    const spacing = ts.options().barSpacing;
    const next = Math.min(60, Math.max(0.5, spacing * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
    ts.applyOptions({ barSpacing: next });
  };
  el.addEventListener('wheel', onWheel, { passive: false });
  return () => el.removeEventListener('wheel', onWheel);
}

function useChart(containerRef, build, deps) {
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const chart = createChart(el, BASE_OPTS);
    build(chart);
    chart.timeScale().fitContent();
    const off = ctrlWheelZoom(el, chart);
    return () => { off(); chart.remove(); };
  }, deps); // eslint-disable-line react-hooks/exhaustive-deps
}

/** Insert whitespace points for missing buckets so data gaps render as empty
 *  space instead of a flat line that looks like a frozen price. */
function withGaps(points, resolution) {
  if (!resolution || points.length < 2) return points;
  const out = [];
  let budget = 3000;   // never explode a long outage into millions of points
  for (let i = 0; i < points.length; i++) {
    if (i > 0) {
      const gap = points[i].time - points[i - 1].time;
      if (gap > resolution * 1.5) {
        for (let t = points[i - 1].time + resolution; t < points[i].time && budget > 0;
             t += resolution, budget--) {
          out.push({ time: t });
        }
      }
    }
    out.push(points[i]);
  }
  return out;
}

function inferResolution(candles) {
  const diffs = [];
  for (let i = 1; i < candles.length && diffs.length < 50; i++) {
    const d = Math.floor(candles[i].ts) - Math.floor(candles[i - 1].ts);
    if (d > 0) diffs.push(d);
  }
  if (!diffs.length) return 0;
  diffs.sort((a, b) => a - b);
  return diffs[0];
}

/* --------------------------- indicators ---------------------------- */

function smaSeries(candles, period = 20) {
  const out = [];
  let sum = 0;
  for (let i = 0; i < candles.length; i++) {
    sum += candles[i].close;
    if (i >= period) sum -= candles[i - period].close;
    if (i >= period - 1) out.push({ time: Math.floor(candles[i].ts), value: sum / period });
  }
  return out;
}

function emaSeries(candles, period = 20) {
  const out = [];
  const k = 2 / (period + 1);
  let ema = null;
  for (const c of candles) {
    ema = ema === null ? c.close : c.close * k + ema * (1 - k);
    out.push({ time: Math.floor(c.ts), value: ema });
  }
  return out.slice(period);
}

function vwapSeries(candles) {
  const out = [];
  let pv = 0, vol = 0;
  for (const c of candles) {
    const typical = (c.high + c.low + c.close) / 3;
    pv += typical * (c.volume || 1);
    vol += c.volume || 1;
    out.push({ time: Math.floor(c.ts), value: pv / vol });
  }
  return out;
}

const dedupe = (pts) => {
  const seen = new Set();
  return pts.filter((p) => !seen.has(p.time) && seen.add(p.time));
};

const fmtLegend = (v) =>
  v >= 1e9 ? (v / 1e9).toFixed(3) + 'B' : v >= 1e6 ? (v / 1e6).toFixed(2) + 'M'
  : v >= 1e3 ? (v / 1e3).toFixed(1) + 'K' : v.toFixed(2);

export function CandleChart({ candles, height = 380, overlays = {}, compare = null,
                              resolution, livePrice = null }) {
  const ref = useRef(null);
  const legendRef = useRef(null);
  const seriesRef = useRef(null);
  const lastBarRef = useRef(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const chart = createChart(el, BASE_OPTS);
    const series = chart.addCandlestickSeries({
      upColor: '#16c784', downColor: '#ea3943',
      wickUpColor: '#16c784', wickDownColor: '#ea3943',
      borderVisible: false, priceFormat: TMN_FORMAT,
    });
    const res = resolution || inferResolution(candles || []);
    const data = withGaps(dedupe((candles || []).map((c) => ({
      time: Math.floor(c.ts), open: c.open, high: c.high, low: c.low, close: c.close,
    }))), res);
    series.setData(data);
    seriesRef.current = series;
    lastBarRef.current = [...data].reverse().find((d) => d.close !== undefined) || null;

    const volumes = (candles || []).filter((c) => c.volume > 0);
    if (volumes.length) {
      const vs = chart.addHistogramSeries({
        priceFormat: { type: 'volume' }, priceScaleId: 'vol',
      });
      chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
      vs.setData(dedupe(volumes.map((c) => ({
        time: Math.floor(c.ts), value: c.volume,
        color: c.close >= c.open ? '#16c78455' : '#ea394355',
      }))));
    }

    const addOverlay = (pts, color, title) => {
      const line = chart.addLineSeries({
        color, lineWidth: 1.4, title, priceFormat: TMN_FORMAT,
        priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerVisible: false,
      });
      line.setData(withGaps(dedupe(pts), res));
    };
    if (candles?.length > 5) {
      if (overlays.sma) addOverlay(smaSeries(candles), '#4a9eff', 'MA20');
      if (overlays.ema) addOverlay(emaSeries(candles), '#9c6bff', 'EMA20');
      if (overlays.vwap) addOverlay(vwapSeries(candles), '#ffb020', 'VWAP');
    }
    if (compare?.candles?.length) {
      addOverlay(compare.candles.map((c) => ({ time: Math.floor(c.ts), value: c.close })),
                 '#38c6d9', compare.name);
    }

    // OHLC readout on crosshair hover
    chart.subscribeCrosshairMove((param) => {
      const legend = legendRef.current;
      if (!legend) return;
      const d = param.seriesData?.get(series);
      if (d && d.open !== undefined) {
        const up = d.close >= d.open;
        legend.innerHTML =
          `O <b>${fmtLegend(d.open)}</b> H <b>${fmtLegend(d.high)}</b> ` +
          `L <b>${fmtLegend(d.low)}</b> C <b style="color:${up ? '#16c784' : '#ea3943'}">${fmtLegend(d.close)}</b>`;
      } else {
        legend.innerHTML = '';
      }
    });

    chart.timeScale().fitContent();
    const off = ctrlWheelZoom(el, chart);
    return () => { off(); seriesRef.current = null; chart.remove(); };
  }, [candles, height, overlays.sma, overlays.ema, overlays.vwap, compare, resolution]);

  // Keep the last candle's close on the live price between candle reloads
  // (without rebuilding the chart, so zoom/pan survive).
  useEffect(() => {
    const s = seriesRef.current, bar = lastBarRef.current;
    if (!s || !bar || !livePrice) return;
    const next = { ...bar, close: livePrice,
                   high: Math.max(bar.high, livePrice), low: Math.min(bar.low, livePrice) };
    try { s.update(next); lastBarRef.current = next; } catch { /* stale ref */ }
  }, [livePrice, candles]);
  return (
    <div ref={ref} className="chart-box" style={{ height }}>
      <div ref={legendRef} className="chart-legend" />
    </div>
  );
}

export function MultiLineChart({ seriesList, height = 300, percent = false }) {
  // seriesList: [{name, color, points: [{ts, value}]}]
  const ref = useRef(null);
  useChart(ref, (chart) => {
    for (const s of seriesList || []) {
      const line = chart.addLineSeries({
        color: s.color, lineWidth: 1.6, title: s.name,
        priceFormat: percent
          ? { type: 'custom', formatter: (v) => v.toFixed(3) + '%' }
          : { type: 'price', precision: 0, minMove: 1 },
      });
      const seen = new Set();
      const pts = [];
      for (const p of s.points || []) {
        const t = Math.floor(p.ts);
        if (!seen.has(t) && p.value !== null && p.value !== undefined) {
          seen.add(t);
          pts.push({ time: t, value: p.value });
        }
      }
      line.setData(pts);
    }
  }, [seriesList]);
  return <div ref={ref} className="chart-box" style={{ height }} />;
}

export function AreaChart({ points, color = '#4a9eff', height = 300 }) {
  const ref = useRef(null);
  useChart(ref, (chart) => {
    const area = chart.addAreaSeries({
      lineColor: color, topColor: color + '44', bottomColor: color + '05',
      lineWidth: 2,
    });
    const seen = new Set();
    const data = [];
    for (const p of points || []) {
      const t = Math.floor(p.ts);
      if (!seen.has(t)) { seen.add(t); data.push({ time: t, value: p.value }); }
    }
    area.setData(data);
  }, [points]);
  return <div ref={ref} className="chart-box" style={{ height }} />;
}

/** Order-book depth chart: cumulative bids (green) and asks (red). */
export function DepthChart({ bids, asks, height = 260 }) {
  const ref = useRef(null);
  useEffect(() => {
    const el = ref.current;
    if (!el || !bids?.length || !asks?.length) return;
    const render = () => renderDepth(el, bids, asks, height);
    render();
    const ro = new ResizeObserver(render);   // redraw on container resize
    ro.observe(el);
    return () => ro.disconnect();
  }, [bids, asks, height]);
  return <div ref={ref} className="chart-box" style={{ height }} />;
}

function renderDepth(el, bids, asks, height) {
  {
    const canvas = document.createElement('canvas');
    const dpr = window.devicePixelRatio || 1;
    const w = el.clientWidth || 300, h = height;
    canvas.width = w * dpr; canvas.height = h * dpr;
    canvas.style.width = w + 'px'; canvas.style.height = h + 'px';
    el.innerHTML = ''; el.appendChild(canvas);
    const ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);

    let cum = 0;
    const bidPts = bids.map(([p, q]) => { cum += p * q; return [p, cum]; });
    cum = 0;
    const askPts = asks.map(([p, q]) => { cum += p * q; return [p, cum]; });
    const allP = [...bidPts, ...askPts].map((x) => x[0]);
    const maxC = Math.max(bidPts.at(-1)?.[1] || 0, askPts.at(-1)?.[1] || 0) || 1;
    const minP = Math.min(...allP), maxP = Math.max(...allP);
    const X = (p) => ((p - minP) / (maxP - minP || 1)) * (w - 8) + 4;
    const Y = (c) => h - 18 - (c / maxC) * (h - 30);

    const draw = (pts, color, fill) => {
      if (!pts.length) return;
      ctx.beginPath();
      ctx.moveTo(X(pts[0][0]), h - 18);
      for (const [p, c] of pts) ctx.lineTo(X(p), Y(c));
      ctx.lineTo(X(pts.at(-1)[0]), h - 18);
      ctx.closePath();
      ctx.fillStyle = fill; ctx.fill();
      ctx.beginPath();
      for (let i = 0; i < pts.length; i++) {
        const [p, c] = pts[i];
        i ? ctx.lineTo(X(p), Y(c)) : ctx.moveTo(X(p), Y(c));
      }
      ctx.strokeStyle = color; ctx.lineWidth = 1.6; ctx.stroke();
    };
    draw([...bidPts].sort((a, b) => a[0] - b[0]), '#16c784', '#16c78418');
    draw(askPts, '#ea3943', '#ea394318');

    // mid marker + price labels
    ctx.fillStyle = '#5a6578'; ctx.font = '10px Inter, sans-serif';
    ctx.textAlign = 'left';
    ctx.fillText(fmtP(minP), 4, h - 5);
    ctx.textAlign = 'right';
    ctx.fillText(fmtP(maxP), w - 4, h - 5);
    const mid = (bids[0][0] + asks[0][0]) / 2;
    ctx.textAlign = 'center';
    ctx.fillStyle = '#8a95a8';
    ctx.fillText(fmtP(mid), X(mid), 10);
    ctx.strokeStyle = '#2a3a55'; ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(X(mid), 14); ctx.lineTo(X(mid), h - 18); ctx.stroke();

    function fmtP(v) {
      return v >= 1e9 ? (v / 1e9).toFixed(2) + 'B'
           : v >= 1e6 ? (v / 1e6).toFixed(1) + 'M'
           : v >= 1e3 ? (v / 1e3).toFixed(0) + 'K' : v.toFixed(0);
    }
  }
}
