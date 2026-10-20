// Operational notification center (top-right corner). Distinct from the
// rule-based trading alerts (which toast at the bottom-right): this surfaces
// system/operational warnings —
//   • node (exchange) disconnects — any monitored node, or ALL of them
//   • upcoming high-impact calendar events (a configurable lead time before)
//   • breaking important (high-impact) news
// Which nodes to watch and which categories are on are user-selectable (gear),
// persisted in localStorage. All detection is client-side from data that
// already streams in (markets WebSocket, calendar poll) + a light news poll.
import { useEffect, useMemo, useRef, useState } from 'react';
import { apiGet } from './api';
import { useLang } from './i18n';

const LS_KEY = 'notif_settings';
const DEFAULTS = { venues: true, calendar: true, news: true, leadMin: 30, nodes: [] };
// nodes: [] means "watch all nodes".

function loadSettings() {
  try { return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(LS_KEY) || '{}') }; }
  catch { return { ...DEFAULTS }; }
}

let _uid = 0;

export default function NotificationCenter({ markets = [], calendar = [], venues = [] }) {
  const { t } = useLang();
  const [settings, setSettings] = useState(loadSettings);
  const [items, setItems] = useState([]);
  const [gearOpen, setGearOpen] = useState(false);

  const prevDown = useRef({});            // venue -> was it down last cycle
  const allDownRef = useRef(false);
  const primed = useRef(false);           // skip notifications on the first read
  const notifiedEvents = useRef(new Set());
  const notifiedNews = useRef(new Set());
  const newsBaseline = useRef(null);      // newest ts seen on first news load

  useEffect(() => {
    localStorage.setItem(LS_KEY, JSON.stringify(settings));
  }, [settings]);

  const dismiss = (id) => setItems((prev) => prev.filter((n) => n.id !== id));
  const push = (severity, title, message, sticky) => {
    const id = ++_uid;
    setItems((prev) => [{ id, severity, title, message, ts: Date.now() }, ...prev].slice(0, 6));
    if (!sticky) setTimeout(() => dismiss(id), 12000);
  };

  const allVenues = useMemo(() => {
    const set = new Set(venues.filter(Boolean));
    markets.forEach((m) => { if (m && m.exchange) set.add(m.exchange); });
    return [...set].sort();
  }, [venues, markets]);

  const monitored = (v) => !settings.nodes.length || settings.nodes.includes(v);

  // ---- node (exchange) disconnect detection -----------------------------
  useEffect(() => {
    if (!settings.venues || !markets.length) return;
    const byVenue = {};
    markets.forEach((m) => {
      if (!m || !m.exchange) return;
      const s = byVenue[m.exchange] || (byVenue[m.exchange] = { total: 0, offline: 0 });
      s.total += 1;
      if (m.status === 'offline') s.offline += 1;
    });
    const list = Object.keys(byVenue).filter(monitored);
    const firstRun = !primed.current;
    let downCount = 0, monCount = 0;
    list.forEach((v) => {
      const down = byVenue[v].total > 0 && byVenue[v].offline === byVenue[v].total;
      monCount += 1;
      if (down) downCount += 1;
      const was = prevDown.current[v];
      if (!firstRun) {
        if (down && !was) push('warning', t('venueDown'), v + ' — ' + t('disconnected'), true);
        else if (!down && was) push('info', t('venueUp'), v + ' — ' + t('reconnected'), false);
      }
      prevDown.current[v] = down;
    });
    const allDown = monCount > 0 && downCount === monCount;
    if (!firstRun && allDown && !allDownRef.current)
      push('critical', t('allVenuesDown'), t('allVenuesDownMsg'), true);
    allDownRef.current = allDown;
    primed.current = true;
  }, [markets, settings.venues, settings.nodes]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- upcoming high-impact calendar events -----------------------------
  useEffect(() => {
    if (!settings.calendar) return;
    const check = () => {
      const now = Date.now() / 1000;
      const lead = (settings.leadMin || 30) * 60;
      calendar.forEach((e) => {
        if (!e || e.impact !== 'High') return;
        if (e.timestamp <= now || e.timestamp > now + lead) return;
        const key = (e.title || '') + '@' + e.timestamp;
        if (notifiedEvents.current.has(key)) return;
        notifiedEvents.current.add(key);
        const mins = Math.max(1, Math.round((e.timestamp - now) / 60));
        push('warning', t('upcomingEvent'),
             (e.country ? e.country + ' · ' : '') + e.title + ' — ' + t('in') + ' ' + mins + 'm', true);
      });
    };
    check();
    const id = setInterval(check, 60000);
    return () => clearInterval(id);
  }, [calendar, settings.calendar, settings.leadMin]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- breaking important news ------------------------------------------
  useEffect(() => {
    if (!settings.news) return;
    let alive = true;
    const tick = () => apiGet('/news?min_impact=3').then((rows) => {
      if (!alive || !Array.isArray(rows)) return;
      const sorted = [...rows].sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0));
      if (newsBaseline.current === null) {                 // first load: set baseline, don't flood
        newsBaseline.current = sorted.length ? sorted[0].timestamp : 0;
        return;
      }
      sorted.forEach((n) => {
        const key = n.url || (n.title + '@' + n.timestamp);
        if (n.timestamp > newsBaseline.current && !notifiedNews.current.has(key)) {
          notifiedNews.current.add(key);
          push('info', t('importantNews'), n.title + (n.source ? ' — ' + n.source : ''), false);
        }
      });
      if (sorted.length) newsBaseline.current = Math.max(newsBaseline.current, sorted[0].timestamp);
    }).catch(() => {});
    tick();
    const id = setInterval(tick, 60000);
    return () => { alive = false; clearInterval(id); };
  }, [settings.news]); // eslint-disable-line react-hooks/exhaustive-deps

  const icon = (sev) => (sev === 'critical' ? '⛔' : sev === 'warning' ? '⚠️' : 'ℹ️');

  const toggleNode = (v, on) => {
    let nodes = settings.nodes.length ? [...settings.nodes] : [...allVenues];
    if (on) { if (!nodes.includes(v)) nodes.push(v); }
    else nodes = nodes.filter((x) => x !== v);
    setSettings({ ...settings, nodes: nodes.length === allVenues.length ? [] : nodes });
  };

  // Rendered inside .topbar-right: the gear is a normal flex item (no longer a
  // fixed overlay sitting on top of the user menu); only the toasts are fixed.
  return (
    <>
      <div className="notifc-head">
        <button className="icon-btn notifc-gear" title={t('notifSettings')}
                aria-label={t('notifSettings')}
                onClick={() => setGearOpen((o) => !o)}>⚙</button>
        {gearOpen && (
          <div className="notifc-settings">
            <div className="notifc-set-title">{t('notifSettings')}</div>
            <label className="notifc-row">
              <input type="checkbox" checked={settings.venues}
                     onChange={(e) => setSettings({ ...settings, venues: e.target.checked })} />
              {t('notifVenues')}
            </label>
            <label className="notifc-row">
              <input type="checkbox" checked={settings.calendar}
                     onChange={(e) => setSettings({ ...settings, calendar: e.target.checked })} />
              {t('notifCalendar')}
            </label>
            <label className="notifc-row">
              <input type="checkbox" checked={settings.news}
                     onChange={(e) => setSettings({ ...settings, news: e.target.checked })} />
              {t('notifNews')}
            </label>
            <label className="notifc-row" style={{ justifyContent: 'space-between' }}>
              <span>{t('notifLead')}</span>
              <input className="input" type="number" min="5" max="240" style={{ width: 64 }}
                     value={settings.leadMin}
                     onChange={(e) => setSettings({ ...settings, leadMin: +e.target.value || 30 })} />
            </label>
            <div className="notifc-set-title" style={{ marginTop: 10 }}>{t('notifNodes')}</div>
            <div className="notifc-nodes">
              {allVenues.length === 0 && <span style={{ color: 'var(--text-3)', fontSize: 12 }}>—</span>}
              {allVenues.map((v) => (
                <label key={v} className="notifc-row">
                  <input type="checkbox" checked={monitored(v)}
                         onChange={(e) => toggleNode(v, e.target.checked)} />
                  {v}
                </label>
              ))}
            </div>
          </div>
        )}
      </div>

      <div className="notifc-stack">
        {items.map((n) => (
          <div key={n.id} className={`notifc-item ${n.severity}`}>
            <span style={{ fontSize: 15, flexShrink: 0 }}>{icon(n.severity)}</span>
            <span style={{ minWidth: 0 }}>
              <b style={{ fontSize: 12.5 }}>{n.title}</b>
              <div style={{ fontSize: 12, color: 'var(--text-2)', wordBreak: 'break-word' }}>{n.message}</div>
            </span>
            <button className="notifc-x" onClick={() => dismiss(n.id)} aria-label="dismiss">×</button>
          </div>
        ))}
      </div>
    </>
  );
}
