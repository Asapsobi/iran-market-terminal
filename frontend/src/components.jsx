// Shared UI primitives.
import { useEffect, useRef, useState } from 'react';
import { assetColor, countdown, fmtNum, fmtPct, pctClass } from './util';

/** Number that flashes green/red when its value ticks up/down. */
export function FlashNum({ value, digits = 0, bold }) {
  const ref = useRef(null);
  const prev = useRef(value);
  useEffect(() => {
    if (ref.current && prev.current != null && value != null && value !== prev.current) {
      const cls = value > prev.current ? 'flash-up' : 'flash-down';
      ref.current.classList.remove('flash-up', 'flash-down');
      void ref.current.offsetWidth;      // restart CSS animation
      ref.current.classList.add(cls);
    }
    prev.current = value;
  }, [value]);
  return (
    <span ref={ref} className="num flash-cell" style={bold ? { fontWeight: 700 } : undefined}>
      {fmtNum(value, digits)}
    </span>
  );
}

/** Friendly empty state: icon + title + actionable hint. */
export function EmptyState({ icon, title, hint, style }) {
  return (
    <div className="empty-state" style={style}>
      {icon && <div className="empty-state-icon">{icon}</div>}
      <div style={{ fontWeight: 600, fontSize: 13 }}>{title}</div>
      {hint && <div style={{ fontSize: 12, color: 'var(--text-3)', maxWidth: 380, lineHeight: 1.55 }}>{hint}</div>}
    </div>
  );
}

export function ChangePill({ value, digits = 2 }) {
  const cls = pctClass(value);
  return (
    <span className={`pill ${cls}`}>
      {cls === 'up' ? '▲' : cls === 'down' ? '▼' : ''} {fmtPct(value, digits)}
    </span>
  );
}

export function Badge({ level, children }) {
  return <span className={`badge ${String(level).toLowerCase()}`}>{children ?? level}</span>;
}

export function StatusDot({ status }) {
  return <span className={`dot ${status}`} title={status} />;
}

export function AssetIcon({ symbol, size = 28 }) {
  return (
    <span className="asset-icon"
          style={{ background: assetColor(symbol), width: size, height: size,
                   fontSize: size * 0.38 }}>
      {symbol.slice(0, 4)}
    </span>
  );
}

export function Sparkline({ data, width = 130, height = 36 }) {
  if (!data || data.length < 2) return <span className="empty" style={{ padding: 0 }}>—</span>;
  const min = Math.min(...data), max = Math.max(...data);
  const range = max - min || 1;
  const pts = data.map((v, i) =>
    `${(i / (data.length - 1)) * width},${height - 3 - ((v - min) / range) * (height - 6)}`
  ).join(' ');
  const up = data[data.length - 1] >= data[0];
  const color = up ? 'var(--green)' : 'var(--red)';
  return (
    <svg width={width} height={height} style={{ display: 'block' }}>
      <polyline points={pts} fill="none" stroke={color} strokeWidth="1.6" />
    </svg>
  );
}

export function ScoreBar({ score }) {
  if (score === null || score === undefined) return '—';
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 7 }}>
      <span className="bar-track">
        <span className="bar-fill" style={{ width: `${Math.min(100, score)}%` }} />
      </span>
      <span className="num" style={{ color: 'var(--text-2)' }}>{Math.round(score)}</span>
    </span>
  );
}

export function Seg({ options, value, onChange }) {
  return (
    <div className="seg">
      {options.map((o) => {
        const val = typeof o === 'string' ? o : o.value;
        const label = typeof o === 'string' ? o.toUpperCase() : o.label;
        return (
          <button key={val} className={value === val ? 'active' : ''}
                  onClick={() => onChange(val)}>{label}</button>
        );
      })}
    </div>
  );
}

export function Chips({ options, value, onChange }) {
  return (
    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
      {options.map((o) => (
        <button key={o} className={`chip ${value === o ? 'active' : ''}`}
                onClick={() => onChange(o)}>{o}</button>
      ))}
    </div>
  );
}

export function Countdown({ ts }) {
  const [, force] = useState(0);
  useEffect(() => {
    const id = setInterval(() => force((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, []);
  const cd = countdown(ts);
  return cd ? <span className="countdown">⏱ {cd}</span> : null;
}

export function SortableTh({ label, col, sort, setSort, align, className }) {
  const active = sort.col === col;
  return (
    <th onClick={() => setSort({ col, dir: active && sort.dir === 'desc' ? 'asc' : 'desc' })}
        style={{ textAlign: align }} className={className}>
      {label} {active ? (sort.dir === 'desc' ? '↓' : '↑') : ''}
    </th>
  );
}

export function sortRows(rows, sort, accessors) {
  const acc = accessors[sort.col];
  if (!acc) return rows;
  return [...rows].sort((a, b) => {
    const va = acc(a) ?? -Infinity, vb = acc(b) ?? -Infinity;
    const cmp = typeof va === 'string' ? va.localeCompare(vb) : va - vb;
    return sort.dir === 'desc' ? -cmp : cmp;
  });
}
