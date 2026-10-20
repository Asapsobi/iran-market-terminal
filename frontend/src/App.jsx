import { useEffect, useState } from 'react';
import { apiGet, apiPost, getToken, setAuthErrorHandler, setToken,
         useLiveStream, usePoll } from './api';
import { Badge, Chips } from './components';
import CommandPalette from './CommandPalette';
import NotificationCenter from './NotificationCenter';
import { ChangePasswordModal, ManageUsersModal } from './UserAdmin';
import { IconBell, IconCheck, IconCommand, IconRows, IconX } from './icons';
import { useLang } from './i18n';
import Login from './Login';
import { eventFlag, fmtCompact, fmtDateTime, fmtNum, fmtPct, humanCountdown,
         premiumColor, timeAgo } from './util';
import Overview from './pages/Overview';
import MarketDetail from './pages/MarketDetail';
import Analytics from './pages/Analytics';
import Desk from './pages/Desk';
import Calendar from './pages/Calendar';
import News from './pages/News';
import Admin from './pages/Admin';
import Help from './pages/Help';
import Intelligence from './pages/Intelligence';

const PAGES = ['overview', 'market', 'analytics', 'desk', 'intel', 'calendar', 'news', 'admin', 'help'];

/** Collapse repeats of the same alert (same rule, same text apart from the
 *  numbers) so the bell counts distinct problems, not every re-fire. */
function groupAlerts(events) {
  const groups = new Map();
  for (const e of events) {
    const key = `${e.rule_id ?? e.rule_type}|${String(e.message || '').replace(/[-+]?[\d.,]+%?/g, '#')}`;
    let g = groups.get(key);
    if (!g) {
      g = { key, latest: e, count: 0, unacked: [] };
      groups.set(key, g);
    }
    g.count += 1;
    if ((e.ts || 0) > (g.latest.ts || 0)) g.latest = e;
    if (!e.acknowledged && e.id) g.unacked.push(e.id);
  }
  return [...groups.values()].sort((a, b) => (b.latest.ts || 0) - (a.latest.ts || 0));
}

/** Ticker countdown with a label: "in 21h 27m", exact time in the tooltip. */
function EventCountdown({ ts, lang, t }) {
  const [, force] = useState(0);
  useEffect(() => {
    const id = setInterval(() => force((n) => n + 1), 30000);
    return () => clearInterval(id);
  }, []);
  const cd = humanCountdown(ts);
  if (!cd) return null;
  return (
    <span className="countdown" title={fmtDateTime(ts, lang, { weekday: 'short' }) + ' · ' + t('iranTime')}>
      {t('in')} {cd}
    </span>
  );
}

/** Auth gate: shows the login screen until a valid session exists. */
export default function App() {
  const [authUser, setAuthUser] = useState(null);   // null=checking, false=logged out

  useEffect(() => {
    setAuthErrorHandler(() => { setToken(''); setAuthUser(false); });
    if (!getToken()) { setAuthUser(false); return; }
    apiGet('/auth/me').then(setAuthUser).catch(() => setAuthUser(false));
  }, []);

  if (authUser === null) {
    return <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center',
                         justifyContent: 'center' }}>
      <div className="skel" style={{ width: 320, height: 120 }} /></div>;
  }
  if (authUser === false) return <Login onLogin={setAuthUser} />;
  return <Terminal authUser={authUser} onUserUpdate={setAuthUser}
                   onLogout={async () => {
                     try { await apiPost('/auth/logout', {}); } catch { /* ignore */ }
                     setToken('');
                     setAuthUser(false);
                   }} />;
}

function Terminal({ authUser, onLogout, onUserUpdate }) {
  const { t, lang, setLang } = useLang();
  const [page, setPage] = useState(() => location.hash.slice(1).split('/')[0] || 'overview');
  const [asset, setAsset] = useState(() => location.hash.split('/')[1] || 'BTC');
  const [meta, setMeta] = useState(null);
  const [overview, setOverview] = useState([]);
  const [marketsSnap, setMarketsSnap] = useState([]);
  const [toasts, setToasts] = useState([]);
  const [wsLive, setWsLive] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [alertEvents, setAlertEvents] = useState([]);
  const [calendar, setCalendar] = useState([]);
  const [compact, setCompact] = useState(() => localStorage.getItem('density') === 'compact');
  const [userMenu, setUserMenu] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const [alertSev, setAlertSev] = useState('ALL');
  const [pwOpen, setPwOpen] = useState(false);
  const [usersOpen, setUsersOpen] = useState(false);

  const refreshMeta = () => apiGet('/meta').then(setMeta).catch(() => {});
  useEffect(() => { refreshMeta(); }, []);
  usePoll('/overview', setOverview, 10000);
  usePoll('/alerts/events?hours=24', setAlertEvents, 15000);
  usePoll('/calendar', setCalendar, 120000);

  useEffect(() => {
    document.body.classList.toggle('compact', compact);
    localStorage.setItem('density', compact ? 'compact' : 'comfortable');
  }, [compact]);

  useEffect(() => {
    const onHash = () => {
      const [p, a] = location.hash.slice(1).split('/');
      if (PAGES.includes(p)) setPage(p);
      if (a) setAsset(a.toUpperCase());
    };
    const onKey = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setPaletteOpen((o) => !o);
      } else if (e.key === 'Escape') {
        setPaletteOpen(false); setDrawerOpen(false);
      }
    };
    window.addEventListener('hashchange', onHash);
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('hashchange', onHash);
      window.removeEventListener('keydown', onKey);
    };
  }, []);

  const go = (p, a) => { location.hash = a ? `${p}/${a}` : p; setPage(p); if (a) setAsset(a); };

  useLiveStream({
    markets: (msg) => { setWsLive(true); if (msg && msg.data) setMarketsSnap(msg.data); },
    alert: (msg) => {
      const item = { ...msg.data, key: Math.random() };
      setToasts((prev) => [...prev.slice(-3), item]);
      setAlertEvents((prev) => [msg.data, ...prev]);
      setTimeout(() => setToasts((prev) => prev.filter((x) => x.key !== item.key)), 9000);
    },
  });

  const btc = overview.find((r) => r.base === 'BTC');
  const usdt = overview.find((r) => r.base === 'USDT');
  const totalVol = overview.reduce((s, r) => s + (r.volume_24h_quote || 0), 0);
  const assets = meta?.assets?.length ? meta.assets : ['BTC', 'ETH', 'USDT'];
  const alertGroups = groupAlerts(alertEvents);
  const unread = alertGroups.filter((g) => g.unacked.length).length;
  const shownGroups = alertGroups.filter((g) =>
    alertSev === 'ALL' || String(g.latest.severity).toLowerCase() === alertSev.toLowerCase());
  const ackIds = async (ids) => {
    await Promise.all(ids.map((id) => apiPost(`/alerts/events/${id}/ack`, {}).catch(() => {})));
    setAlertEvents(await apiGet('/alerts/events?hours=24').catch(() => alertEvents));
  };
  const now = Date.now() / 1000;
  const nextEvent = calendar
    .filter((e) => e.impact === 'High' && e.timestamp > now)
    .sort((a, b) => a.timestamp - b.timestamp)[0];

  const ackAll = async () => {
    await Promise.all(alertEvents.filter((e) => !e.acknowledged)
      .map((e) => apiPost(`/alerts/events/${e.id}/ack`, {}).catch(() => {})));
    setAlertEvents(await apiGet('/alerts/events?hours=24').catch(() => alertEvents));
  };

  // Admin + Help live in the user menu so the header fits on one row.
  const navItems = [
    ['overview', t('overview')], ['analytics', t('analytics')], ['desk', t('desk')],
    ['intel', t('intelligence')], ['calendar', t('calendar')], ['news', t('news')],
  ];
  const isActive = (id) => page === id || (id === 'overview' && page === 'market');

  return (
    <div className="app">
      <header className="topbar">
        <button className="icon-btn nav-burger" aria-label="Menu" aria-expanded={navOpen}
                onClick={() => setNavOpen(!navOpen)}>☰</button>
        {navOpen && (
          <div className="nav-menu" onMouseLeave={() => setNavOpen(false)}>
            {[...navItems, ['admin', t('admin')], ['help', t('help')]].map(([id, label]) => (
              <button key={id} className={`palette-item ${isActive(id) ? 'active' : ''}`}
                      onClick={() => { setNavOpen(false); go(id); }}>{label}</button>
            ))}
          </div>
        )}
        <div className="logo">
          <span className="pulse" />
          <span className="logo-text">Iran Market <span style={{ color: 'var(--accent)' }}>Terminal</span></span>
          {meta?.demo_mode && <span className="badge warning">{t('demoMode')}</span>}
        </div>
        <nav className="nav">
          {navItems.map(([id, label]) => (
            <button key={id} className={isActive(id) ? 'active' : ''}
                    onClick={() => go(id)}>{label}</button>
          ))}
        </nav>
        <div className="topbar-right">
          <button className="icon-btn hide-phone" title="Ctrl+K" aria-label="Command palette"
                  onClick={() => setPaletteOpen(true)}>
            <IconCommand />
          </button>
          <button className={`icon-btn hide-phone ${compact ? 'active' : ''}`} title={t('density')}
                  aria-label={t('density')}
                  style={compact ? { color: 'var(--accent)' } : undefined}
                  onClick={() => setCompact(!compact)}>
            <IconRows />
          </button>
          {/* operational notifications: node disconnects, upcoming events, breaking news */}
          <NotificationCenter markets={marketsSnap} calendar={calendar}
                              venues={(meta?.exchanges || []).map((e) => e.name)} />
          <button className="icon-btn" aria-label={t('alerts')}
                  onClick={() => setDrawerOpen(true)}>
            <IconBell />
            {unread > 0 && <span className="count">{unread > 99 ? '99+' : unread}</span>}
          </button>
          <span className={`dot hide-phone ${wsLive ? 'connected' : 'delayed'}`}
                title={wsLive ? 'WebSocket live' : 'Polling'} />
          <div className="seg">
            <button className={lang === 'en' ? 'active' : ''} onClick={() => setLang('en')}>EN</button>
            <button className={lang === 'fa' ? 'active' : ''} onClick={() => setLang('fa')}>فا</button>
          </div>
          <div style={{ position: 'relative' }}>
            <button className="chip" style={{ fontWeight: 600 }}
                    onClick={() => setUserMenu(!userMenu)}>
              {authUser.username} {authUser.role === 'admin' && '★'}
            </button>
            {userMenu && (
              <div className="card" style={{ position: 'absolute', top: '110%',
                    insetInlineEnd: 0, zIndex: 60, minWidth: 190, padding: 8,
                    boxShadow: '0 10px 34px #000000aa' }}>
                <div style={{ fontSize: 11, color: 'var(--text-3)', padding: '4px 10px 6px' }}>
                  {authUser.username} · {authUser.role === 'admin' ? t('roleAdmin') : t('roleOperator')}
                </div>
                <button className={`palette-item ${page === 'admin' ? 'active' : ''}`}
                        onClick={() => { setUserMenu(false); go('admin'); }}>
                  {t('admin')}
                </button>
                <button className={`palette-item ${page === 'help' ? 'active' : ''}`}
                        onClick={() => { setUserMenu(false); go('help'); }}>
                  {t('help')}
                </button>
                <div style={{ borderTop: '1px solid var(--border)', margin: '4px 0' }} />
                <button className="palette-item"
                        onClick={() => { setUserMenu(false); setPwOpen(true); }}>
                  {t('changePassword')}
                </button>
                {authUser.role === 'admin' && (
                  <button className="palette-item"
                          onClick={() => { setUserMenu(false); setUsersOpen(true); }}>
                    {t('manageUsers')}
                  </button>
                )}
                <button className="palette-item" style={{ color: 'var(--red)' }}
                        onClick={onLogout}>
                  {t('logout')}
                </button>
              </div>
            )}
          </div>
        </div>
      </header>

      <div className="statsbar">
        {btc && <span>BTC <b className="num">{fmtNum(btc.price, 0)}</b>{' '}
          <span className={`pill ${(btc.change_24h ?? 0) >= 0 ? 'up' : 'down'}`}
                style={{ padding: '0 5px', fontSize: 11 }}>{fmtPct(btc.change_24h)}</span></span>}
        {usdt && <span>{t('usdtRate')} <b className="num">{fmtNum(usdt.price, 0)}</b></span>}
        {meta?.usd_reference?.BTC && <span>{t('globalBtc')} <b className="num">${fmtNum(meta.usd_reference.BTC, 0)}</b></span>}
        {btc?.premium_pct != null && <span>{t('premium')} <b style={{ color: premiumColor(btc.premium_pct) }}>{fmtPct(btc.premium_pct)}</b></span>}
        <span>{t('totalVolume')} <b className="num">{fmtCompact(totalVol)}</b></span>
        {btc && <span>{t('liveVenues')} <b>{btc.exchanges_live}/{btc.exchanges_total}</b></span>}
        {nextEvent && (
          <span style={{ cursor: 'pointer' }} onClick={() => go('calendar')}
                title={nextEvent.title}>
            {eventFlag(nextEvent.country, nextEvent.title)}{' '}
            <b>{nextEvent.title.length > 26 ? nextEvent.title.slice(0, 25) + '…' : nextEvent.title}</b>{' '}
            <EventCountdown ts={nextEvent.timestamp} lang={lang} t={t} />
          </span>
        )}
      </div>

      <main className="main">
        {page === 'overview' && <Overview onSelectAsset={(a) => go('market', a)} />}
        {page === 'market' && (
          <MarketDetail asset={asset} assets={assets}
                        onSelectAsset={(a) => go('market', a)} />)}
        {page === 'analytics' && (
          <Analytics asset={asset} assets={assets} meta={meta}
                     onSelectAsset={setAsset} />)}
        {page === 'desk' && <Desk />}
        {page === 'intel' && <Intelligence assets={assets} meta={meta} />}
        {page === 'calendar' && <Calendar />}
        {page === 'news' && <News />}
        {page === 'admin' && <Admin meta={meta} refreshMeta={refreshMeta}
                                    isAdmin={authUser.role === 'admin'} />}
        {page === 'help' && <Help />}
      </main>

      {/* alert drawer */}
      {drawerOpen && (
        <>
          <div className="drawer-backdrop" onClick={() => setDrawerOpen(false)} />
          <aside className="drawer" role="dialog" aria-label={t('alerts')}>
            <div className="drawer-head">
              <IconBell /> {t('alerts')}
              {unread > 0 && <span className="badge critical">{unread}</span>}
              <span style={{ marginInlineStart: 'auto', display: 'flex', gap: 6 }}>
                {unread > 0 && (
                  <button className="btn ghost sm" onClick={ackAll}>
                    <IconCheck size={13} /> {t('ackAll')}
                  </button>)}
                <button className="icon-btn" aria-label="Close"
                        onClick={() => setDrawerOpen(false)}><IconX /></button>
              </span>
            </div>
            <div style={{ padding: '8px 16px', borderBottom: '1px solid var(--border)' }}>
              <Chips options={['ALL', 'critical', 'warning', 'info']} value={alertSev}
                     onChange={setAlertSev} />
            </div>
            <div className="drawer-body">
              {shownGroups.length ? shownGroups.map((g) => {
                const e = g.latest;
                return (
                  <div key={g.key}
                       style={{ display: 'flex', gap: 9, alignItems: 'flex-start',
                                padding: '9px 0', borderBottom: '1px solid var(--border)',
                                opacity: g.unacked.length ? 1 : 0.45 }}>
                    <Badge level={e.severity} />
                    <span style={{ flex: 1, fontSize: 12.5, lineHeight: 1.45 }}>
                      {e.message}
                      <div style={{ fontSize: 10.5, color: 'var(--text-3)', marginTop: 2 }}>
                        {timeAgo(e.ts, lang)}
                        {g.count > 1 && <> · <b>×{g.count}</b> {t('in24h')}</>}
                      </div>
                    </span>
                    {g.unacked.length > 0 && (
                      <button className="btn ghost sm" onClick={() => ackIds(g.unacked)}>
                        {t('acknowledge')}</button>)}
                  </div>
                );
              }) : (
                <div className="empty-state">
                  <div className="empty-state-icon"><IconBell /></div>
                  <div style={{ fontWeight: 600 }}>{t('noAlerts')}</div>
                </div>
              )}
            </div>
          </aside>
        </>
      )}

      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)}
                      assets={assets} go={go}
                      actions={[{
                        icon: <IconRows />,
                        label: compact ? t('densityComfort') : t('densityCompact'),
                        run: () => setCompact(!compact),
                      }]} />

      <div className="toast-wrap">
        {toasts.map((a) => (
          <div key={a.key} className={`toast ${a.severity}`}>
            <b style={{ textTransform: 'uppercase', fontSize: 10.5 }}>{a.severity}</b>
            <div>{a.message}</div>
          </div>
        ))}
      </div>

      {pwOpen && <ChangePasswordModal onClose={() => setPwOpen(false)} />}
      {usersOpen && <ManageUsersModal me={authUser} onClose={() => setUsersOpen(false)} />}
    </div>
  );
}
