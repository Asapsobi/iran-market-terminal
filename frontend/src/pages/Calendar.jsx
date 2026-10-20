// Economic calendar — Forex Factory-style: day groups, table rows, filters,
// live status, surprise scoring, descriptions, historical values.
import { useEffect, useRef, useState } from 'react';
import { apiGet, usePoll } from '../api';
import { Chips, Countdown } from '../components';
import { useLang } from '../i18n';
import { eventFlag, fmtDateTime, fmtDay, fmtPct, impactLevel } from '../util';

/* ---------------- event classification & descriptions ---------------- */

const EVENT_TYPES = [
  { id: 'central_bank', match: ['rate', 'fomc', 'ecb', 'boj', 'boe', 'snb', 'monetary', 'press conference', 'minutes', 'statement'],
    label: { en: 'Central Banks', fa: 'بانک مرکزی' },
    desc: { en: 'Interest-rate decisions and central-bank communication. The single biggest driver of currency and risk-asset moves; crypto often reacts to USD liquidity expectations.',
            fa: 'تصمیمات نرخ بهره و اظهارات بانک مرکزی. مهم‌ترین محرک بازار ارز و دارایی‌های ریسکی؛ کریپتو معمولاً به انتظارات نقدینگی دلار واکنش نشان می‌دهد.' } },
  { id: 'inflation', match: ['cpi', 'ppi', 'inflation', 'price index', 'hicp'],
    label: { en: 'Inflation', fa: 'تورم' },
    desc: { en: 'Consumer/producer price growth. Hotter-than-forecast inflation usually means tighter policy → stronger USD, pressure on risk assets.',
            fa: 'رشد قیمت مصرف‌کننده/تولیدکننده. تورم بالاتر از پیش‌بینی معمولاً یعنی سیاست انقباضی‌تر → دلار قوی‌تر و فشار بر دارایی‌های ریسکی.' } },
  { id: 'employment', match: ['employment', 'unemployment', 'jobless', 'claims', 'payroll', 'non-farm', 'nfp', 'jobs'],
    label: { en: 'Employment', fa: 'اشتغال' },
    desc: { en: 'Labor-market health. Strong jobs = strong economy but potentially tighter policy. For claims/unemployment, LOWER numbers are better.',
            fa: 'سلامت بازار کار. اشتغال قوی = اقتصاد قوی اما احتمال سیاست انقباضی‌تر. برای بیکاری/مدعیان بیمه بیکاری عدد پایین‌تر بهتر است.' } },
  { id: 'growth', match: ['gdp'],
    label: { en: 'Growth', fa: 'رشد اقتصادی' },
    desc: { en: 'Gross Domestic Product — the broadest measure of economic output, released quarterly.',
            fa: 'تولید ناخالص داخلی — جامع‌ترین شاخص تولید اقتصادی، انتشار فصلی.' } },
  { id: 'manufacturing', match: ['pmi', 'manufacturing', 'industrial', 'factory', 'orders', 'production'],
    label: { en: 'Manufacturing', fa: 'تولید و صنعت' },
    desc: { en: 'Factory activity and orders. PMIs above 50 signal expansion; below 50, contraction.',
            fa: 'فعالیت کارخانه‌ها و سفارشات. شاخص PMI بالای ۵۰ یعنی رونق و زیر ۵۰ یعنی رکود.' } },
  { id: 'consumer', match: ['retail', 'consumer', 'sentiment', 'confidence', 'spending'],
    label: { en: 'Consumer', fa: 'مصرف‌کننده' },
    desc: { en: 'Household spending and confidence — the demand side of the economy.',
            fa: 'هزینه‌کرد و اعتماد خانوار — سمت تقاضای اقتصاد.' } },
  { id: 'housing', match: ['housing', 'home', 'building', 'mortgage', 'construction'],
    label: { en: 'Housing', fa: 'مسکن' },
    desc: { en: 'Construction and home sales — rate-sensitive leading indicator.',
            fa: 'ساخت‌وساز و فروش مسکن — شاخص پیشرو و حساس به نرخ بهره.' } },
  { id: 'energy', match: ['oil', 'gas', 'inventories', 'crude'],
    label: { en: 'Energy', fa: 'انرژی' },
    desc: { en: 'Energy supply data. For inventories, LOWER (drawdown) usually supports prices.',
            fa: 'داده‌های عرضه انرژی. برای ذخایر، عدد کمتر (برداشت) معمولاً حامی قیمت است.' } },
  { id: 'trade', match: ['trade', 'export', 'import', 'current account'],
    label: { en: 'Trade', fa: 'تجارت' },
    desc: { en: 'Cross-border goods and services flows.',
            fa: 'جریان کالا و خدمات میان کشورها.' } },
  { id: 'other', match: [],
    label: { en: 'Other', fa: 'سایر' },
    desc: { en: 'General economic release.', fa: 'انتشار اقتصادی عمومی.' } },
];

// indicators where a LOWER actual is the "good"/green outcome
const LOWER_IS_BETTER = ['unemployment', 'jobless', 'claims', 'inventories'];

export function classifyEvent(title) {
  const t = (title || '').toLowerCase();
  return EVENT_TYPES.find((e) => e.match.some((kw) => t.includes(kw))) || EVENT_TYPES.at(-1);
}

function surpriseTone(ev) {
  if (ev.surprise_pct == null) return null;                    // no comparison
  if (Math.abs(ev.surprise_pct) < 1e-9) return 'neutral';
  const lowerBetter = LOWER_IS_BETTER.some((kw) => ev.title.toLowerCase().includes(kw));
  const better = lowerBetter ? ev.surprise_pct < 0 : ev.surprise_pct > 0;
  return better ? 'better' : 'worse';
}

function eventStatus(ev, now) {
  if (ev.actual) return 'released';
  if (ev.timestamp > now) return 'upcoming';
  return now - ev.timestamp < 3600 ? 'live' : 'released';
}

/* ------------------------------- page -------------------------------- */

export default function Calendar() {
  const { t, lang } = useLang();
  const [events, setEvents] = useState(null);
  const [impact, setImpact] = useState('ALL');
  const [currency, setCurrency] = useState('ALL');
  const [etype, setEtype] = useState('ALL');
  const [search, setSearch] = useState('');
  const [collapsed, setCollapsed] = useState({});
  const [expanded, setExpanded] = useState(null);
  const [history, setHistory] = useState({});
  const todayRef = useRef(null);
  const scrolled = useRef(false);

  usePoll('/calendar', setEvents, 30000);   // actuals refresh automatically

  // open on today (or the next day with events), not on past days
  useEffect(() => {
    if (!events || scrolled.current || !todayRef.current) return;
    scrolled.current = true;
    const top = todayRef.current.getBoundingClientRect().top + window.scrollY
      - (parseInt(getComputedStyle(document.documentElement)
                    .getPropertyValue('--topbar-h')) || 52) - 12;
    window.scrollTo({ top: Math.max(0, top) });
  });

  if (!events) return <div className="skel" style={{ height: 300 }} />;

  const now = Date.now() / 1000;
  const currencies = ['ALL', ...new Set(events.map((e) => e.country).filter(Boolean))];

  const filtered = events.filter((e) =>
    (impact === 'ALL' || impactLevel(e.impact) === impact.toLowerCase()) &&
    (currency === 'ALL' || e.country === currency) &&
    (etype === 'ALL' || classifyEvent(e.title).id === etype) &&
    (!search || e.title.toLowerCase().includes(search.toLowerCase())));

  const groups = [];
  for (const ev of filtered) {
    const day = fmtDay(ev.timestamp, lang);
    const g = groups.find((x) => x.day === day);
    g ? g.items.push(ev) : groups.push({ day, ts: ev.timestamp, items: [ev] });
  }

  // scroll anchor: today's group, else the first group that is still ahead
  const todayLabel = fmtDay(now, lang);
  let anchorIdx = groups.findIndex((x) => x.day === todayLabel);
  if (anchorIdx < 0) anchorIdx = groups.findIndex((x) => x.items.some((ev) => ev.timestamp >= now));

  const toggleExpand = async (ev, key) => {
    if (expanded === key) { setExpanded(null); return; }
    setExpanded(key);
    if (!history[key]) {
      try {
        const h = await apiGet(`/calendar/history?title=${encodeURIComponent(ev.title)}&country=${encodeURIComponent(ev.country)}`);
        setHistory((p) => ({ ...p, [key]: h }));
      } catch { setHistory((p) => ({ ...p, [key]: [] })); }
    }
  };

  return (
    <div>
      {/* filters */}
      <div className="filter-bar">
        <h2 style={{ fontSize: 18, marginInlineEnd: 4 }}>{t('calendar')}</h2>
        <input className="input" style={{ maxWidth: 190 }} value={search}
               placeholder={t('searchEvents')} onChange={(e) => setSearch(e.target.value)} />
        <Chips options={['ALL', 'High', 'Medium', 'Low']} value={impact} onChange={setImpact} />
        <select className="input" style={{ minWidth: 150, maxWidth: 190 }} value={currency}
                onChange={(e) => setCurrency(e.target.value)} title={t('currency')}>
          {currencies.map((c) => <option key={c} value={c}>{c === 'ALL' ? t('all') + ' — ' + t('currency') : c}</option>)}
        </select>
        <select className="input" style={{ maxWidth: 160 }} value={etype}
                onChange={(e) => setEtype(e.target.value)} title={t('eventType')}>
          <option value="ALL">{t('all')} — {t('eventType')}</option>
          {EVENT_TYPES.map((et) => <option key={et.id} value={et.id}>{et.label[lang]}</option>)}
        </select>
      </div>

      {/* legend */}
      <div style={{ display: 'flex', gap: 14, fontSize: 11, color: 'var(--text-3)', margin: '8px 2px 14px', flexWrap: 'wrap' }}>
        <span><span className="imp-dot high" /> {t('impact')}: High</span>
        <span><span className="imp-dot medium" /> Medium</span>
        <span><span className="imp-dot low" /> Low</span>
        <span style={{ color: 'var(--green)' }}>▲ {t('better')}</span>
        <span style={{ color: 'var(--red)' }}>▼ {t('worse')}</span>
        <span>● {t('neutral')}</span>
      </div>

      {groups.map((g, gi) => {
        const isCollapsed = collapsed[g.day];
        const isToday = todayLabel === g.day;
        return (
          <div key={g.day} className="card cal-day" ref={gi === anchorIdx ? todayRef : undefined}>
            <button className="cal-day-head" onClick={() =>
              setCollapsed((p) => ({ ...p, [g.day]: !p[g.day] }))}>
              <span className="chev">{isCollapsed ? '▸' : '▾'}</span>
              <b>{g.day}</b>
              {isToday && <span className="badge info">{t('today')}</span>}
              <span style={{ marginInlineStart: 'auto', color: 'var(--text-3)', fontSize: 11.5 }}>
                {g.items.length} {t('event')}
              </span>
            </button>

            {!isCollapsed && (
              <div className="tbl-scroll">
                <table className="tbl cal-tbl">
                  <thead>
                    <tr>
                      <th style={{ width: 66 }}>{t('iranTime')}</th>
                      <th style={{ width: 86 }}>{t('status')}</th>
                      <th style={{ width: 74 }}>{t('currency')}</th>
                      <th>{t('event')}</th>
                      <th style={{ width: 40 }}>{t('impact')}</th>
                      <th style={{ width: 90 }}>{t('actual')}</th>
                      <th style={{ width: 90 }}>{t('forecast')}</th>
                      <th style={{ width: 90 }}>{t('previous')}</th>
                      <th style={{ width: 30 }} />
                    </tr>
                  </thead>
                  <tbody>
                    {g.items.map((ev, i) => {
                      const key = `${ev.title}|${ev.country}|${ev.timestamp}`;
                      const status = eventStatus(ev, now);
                      const tone = surpriseTone(ev);
                      const etypeInfo = classifyEvent(ev.title);
                      const hist = history[key];
                      const avgSurprise = hist?.length
                        ? hist.reduce((s, h) => s + (h.surprise_pct ?? 0), 0) / hist.length : null;
                      return [
                        <tr key={i} className="clickable" onClick={() => toggleExpand(ev, key)}>
                          <td className="num" style={{ color: 'var(--text-2)' }}>
                            {fmtDateTime(ev.timestamp, lang, { month: undefined, day: undefined })}
                          </td>
                          <td>
                            {status === 'live' && <span className="badge critical cal-live">● {t('live')}</span>}
                            {status === 'upcoming' && (
                              <span style={{ fontSize: 11 }}><Countdown ts={ev.timestamp} /></span>)}
                            {status === 'released' && <span className="badge low">{t('released')}</span>}
                          </td>
                          <td><span style={{ fontSize: 15 }}>{eventFlag(ev.country, ev.title)}</span> <b style={{ fontSize: 11.5 }}>{ev.country}</b></td>
                          <td>
                            <b style={{ fontSize: 12.5 }}>{ev.title}</b>
                            <span className="cal-type">{etypeInfo.label[lang]}</span>
                          </td>
                          <td><span className={`imp-dot ${impactLevel(ev.impact)}`} title={ev.impact} /></td>
                          <td className="num" style={{ fontWeight: 700,
                                color: tone === 'better' ? 'var(--green)'
                                     : tone === 'worse' ? 'var(--red)' : 'var(--text)' }}>
                            {ev.actual || '—'}
                            {tone === 'better' && ' ▲'}
                            {tone === 'worse' && ' ▼'}
                            {tone === 'neutral' && ' ●'}
                          </td>
                          <td className="num" style={{ color: 'var(--text-2)' }}>{ev.forecast || '—'}</td>
                          <td className="num" style={{ color: 'var(--text-2)' }}>
                            {ev.previous || '—'}
                            {ev.revised && (
                              <div style={{ fontSize: 10, color: 'var(--amber)' }}>
                                {t('revised')}: {ev.revised}
                              </div>)}
                          </td>
                          <td style={{ color: 'var(--text-3)' }}>{expanded === key ? '▴' : '▾'}</td>
                        </tr>,
                        expanded === key && (
                          <tr key={key + '-x'} className="cal-detail">
                            <td colSpan={9}>
                              <div className="cal-detail-grid">
                                <div>
                                  <div className="cal-detail-h">{t('description')}</div>
                                  <p style={{ fontSize: 12.5, color: 'var(--text-2)', lineHeight: 1.6 }}>
                                    {etypeInfo.desc[lang]}
                                  </p>
                                  {ev.surprise_pct != null && (
                                    <p style={{ fontSize: 12.5, marginTop: 6 }}>
                                      {t('surprise')} ({t('surpriseTip')}):{' '}
                                      <b style={{ color: tone === 'better' ? 'var(--green)' : tone === 'worse' ? 'var(--red)' : 'var(--text)' }}>
                                        {fmtPct(ev.surprise_pct, 2)}
                                      </b>
                                    </p>)}
                                </div>
                                <div>
                                  <div className="cal-detail-h">
                                    {t('pastReleases')}
                                    {avgSurprise != null && (
                                      <span style={{ fontWeight: 400, color: 'var(--text-3)' }}>
                                        {' '}· {t('historicalSurprise')}:{' '}
                                        <b style={{ color: avgSurprise >= 0 ? 'var(--green)' : 'var(--red)' }}>
                                          {fmtPct(avgSurprise, 2)}
                                        </b>
                                      </span>)}
                                  </div>
                                  {hist === undefined && <div className="skel" style={{ height: 40 }} />}
                                  {hist?.length ? hist.slice(0, 6).map((h, j) => (
                                    <div key={j} style={{ display: 'flex', justifyContent: 'space-between',
                                                          fontSize: 11.5, padding: '3px 0', color: 'var(--text-2)' }}>
                                      <span>{fmtDateTime(h.ts, lang, { hour: undefined, minute: undefined })}</span>
                                      <span className="num">
                                        F {h.forecast || '—'} → A {h.actual || '—'}
                                        {h.surprise_pct != null && (
                                          <b style={{ color: h.surprise_pct >= 0 ? 'var(--green)' : 'var(--red)', marginInlineStart: 6 }}>
                                            {fmtPct(h.surprise_pct, 2)}
                                          </b>)}
                                      </span>
                                    </div>
                                  )) : hist && <div style={{ fontSize: 11.5, color: 'var(--text-3)' }}>{t('noData')}</div>}
                                </div>
                              </div>
                            </td>
                          </tr>
                        ),
                      ];
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        );
      })}
      {!filtered.length && <div className="empty">{t('noData')}</div>}
    </div>
  );
}

export { EVENT_TYPES };
