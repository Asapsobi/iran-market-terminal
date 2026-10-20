// Formatting, dates (Gregorian + Jalali via Intl), flags, colors.

export function fmtNum(n, digits) {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  if (digits === undefined) {
    const abs = Math.abs(n);
    digits = abs >= 1000 ? 0 : abs >= 10 ? 2 : abs >= 0.1 ? 4 : 6;
  }
  return n.toLocaleString('en-US', { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}

export function fmtCompact(n) {
  if (n === null || n === undefined || !isFinite(n) || n === 0) return '—';
  const units = [[1e12, 'T'], [1e9, 'B'], [1e6, 'M'], [1e3, 'K']];
  for (const [v, u] of units) {
    if (Math.abs(n) >= v) return (n / v).toFixed(n / v >= 100 ? 0 : 1) + u;
  }
  return n.toFixed(1);
}

export function fmtPct(n, digits = 2) {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  const s = n.toFixed(digits);
  if (Number(s) === 0) return `${(0).toFixed(digits)}%`;   // never "-0.00%"
  return `${n > 0 ? '+' : ''}${s}%`;
}

export function pctClass(n, digits = 2) {
  if (n === null || n === undefined || Number.isNaN(n)) return 'flat';
  if (Number(n.toFixed(digits)) === 0) return 'flat';
  return n > 0 ? 'up' : 'down';
}

/** Premium/discount color: amber = premium, blue = discount, gray = flat. */
export function premiumColor(n) {
  if (n === null || n === undefined || Number(n.toFixed(2)) === 0) return 'var(--text-2)';
  return n > 0 ? 'var(--amber)' : 'var(--accent)';
}

/** TMN price for chart axes/labels: compact above 1B, else whole tomans. */
export function fmtTmnAxis(v) {
  if (v === null || v === undefined || !isFinite(v)) return '';
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(a >= 1e11 ? 2 : 3) + 'B';   // 21.373B
  if (a >= 1e3) return Math.round(v).toLocaleString('en-US');
  return v.toLocaleString('en-US', { maximumFractionDigits: a >= 1 ? 2 : 6 });
}

/** Order-book quantity: enough decimals that small BTC sizes aren't 0.0000. */
export function fmtQty(q, maxQty) {
  if (q === null || q === undefined || !isFinite(q)) return '—';
  const ref = Math.max(Math.abs(maxQty || q), 1e-12);
  const digits = ref >= 1000 ? 0 : ref >= 10 ? 2 : ref >= 1 ? 4 : ref >= 0.01 ? 6 : 8;
  return q.toLocaleString('en-US', { minimumFractionDigits: digits,
                                     maximumFractionDigits: digits });
}

/** Normalize feed impact labels: "High", "high", 3, "High Impact Expected",
 *  and Forex Factory icon codes (red / ora / yel / gra). */
export function impactLevel(raw) {
  const s = String(raw ?? '').trim().toLowerCase();
  if (s.startsWith('high') || s === '3' || s === 'red') return 'high';
  if (s.startsWith('med') || s === '2' || s === 'ora' || s === 'orange') return 'medium';
  if (s.startsWith('holiday') || s.startsWith('non-econ') || s === 'gra' || s === 'gray') return 'holiday';
  return 'low';
}

/** "21h 27m", "3d 4h", "12m" — human countdown for labels. */
export function humanCountdown(ts) {
  const s = ts - Date.now() / 1000;
  if (s <= 0) return null;
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600),
        m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return m > 0 ? `${m}m` : '<1m';
}

// Iran-time date formatting. Persian locale automatically uses the Jalali
// (Persian) calendar via Intl — no conversion library required.
export function fmtDateTime(ts, lang = 'en', opts = {}) {
  if (!ts) return '—';
  const date = new Date(ts > 2e10 ? ts : ts * 1000);
  const locale = lang === 'fa' ? 'fa-IR-u-ca-persian' : 'en-GB';
  return new Intl.DateTimeFormat(locale, {
    timeZone: 'Asia/Tehran',
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    ...opts,
  }).format(date);
}

export function fmtDay(ts, lang = 'en') {
  return fmtDateTime(ts, lang, { weekday: 'long', month: 'long', day: 'numeric',
                                 hour: undefined, minute: undefined });
}

export function timeAgo(ts, lang = 'en') {
  if (!ts) return '—';
  const s = Math.max(0, Date.now() / 1000 - ts);
  const fa = lang === 'fa';
  if (s < 60) return fa ? 'لحظاتی پیش' : 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}${fa ? ' دقیقه پیش' : 'm ago'}`;
  if (s < 86400) return `${Math.floor(s / 3600)}${fa ? ' ساعت پیش' : 'h ago'}`;
  return `${Math.floor(s / 86400)}${fa ? ' روز پیش' : 'd ago'}`;
}

export function countdown(ts) {
  const s = ts - Date.now() / 1000;
  if (s <= 0) return null;
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600),
        m = Math.floor((s % 3600) / 60), sec = Math.floor(s % 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
  return `${m}:${String(sec).padStart(2, '0')}`;
}

const CCY_FLAGS = {
  USD: '🇺🇸', EUR: '🇪🇺', GBP: '🇬🇧', JPY: '🇯🇵', CHF: '🇨🇭', CAD: '🇨🇦',
  AUD: '🇦🇺', NZD: '🇳🇿', CNY: '🇨🇳', IRR: '🇮🇷', TMN: '🇮🇷',
};
const TITLE_FLAGS = [
  ['german', '🇩🇪'], ['french', '🇫🇷'], ['italian', '🇮🇹'], ['spanish', '🇪🇸'],
  ['ecb', '🇪🇺'], ['boj', '🇯🇵'], ['chinese', '🇨🇳'], ['bank of england', '🇬🇧'],
];

export function eventFlag(country, title = '') {
  const t = title.toLowerCase();
  for (const [kw, flag] of TITLE_FLAGS) if (t.includes(kw)) return flag;
  return CCY_FLAGS[country?.toUpperCase()] || '🌐';
}

const ASSET_COLORS = { BTC: '#F7931A', ETH: '#627EEA', USDT: '#26A17B',
                       DOGE: '#C2A633', XRP: '#00AAE4', ADA: '#0033AD',
                       SOL: '#9945FF', TRX: '#EB0029', TON: '#0098EA' };

export function assetColor(sym) {
  if (ASSET_COLORS[sym]) return ASSET_COLORS[sym];
  let h = 0;
  for (const c of sym) h = (h * 31 + c.charCodeAt(0)) % 360;
  return `hsl(${h}, 60%, 50%)`;
}

export const RANGES = ['1h', '4h', '1d', '1w', '1m'];

export const RULE_TYPES = [
  'spread_above', 'arb_net_above', 'deviation_above', 'liquidity_drop',
  'change_above', 'premium_above', 'premium_below', 'calendar_high_impact',
];
