// Dealing-desk dashboard: arbitrage scanner, movers, spreads, alerts, rules.
import { useState } from 'react';
import { apiDelete, apiGet, apiPatch, apiPost, usePoll } from '../api';
import { Badge, ChangePill, EmptyState, ScoreBar } from '../components';
import { IconBell, IconZap } from '../icons';
import { useLang } from '../i18n';
import { fmtCompact, fmtNum, fmtPct, fmtQty, premiumColor, RULE_TYPES, timeAgo } from '../util';

export default function Desk() {
  const { t, lang } = useLang();
  const [arb, setArb] = useState([]);
  const [overview, setOverview] = useState([]);
  const [markets, setMarkets] = useState([]);
  const [liquidity, setLiquidity] = useState([]);
  const [events, setEvents] = useState([]);
  const [rules, setRules] = useState([]);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ name: '', rule_type: 'spread_above',
                                     base: '', exchange: '', threshold: 1,
                                     window_sec: 3600, cooldown_sec: 900 });

  usePoll('/arbitrage', setArb, 6000);
  usePoll('/overview', setOverview, 8000);
  usePoll('/markets', setMarkets, 8000);
  usePoll('/liquidity', setLiquidity, 8000);
  usePoll('/alerts/events?hours=24', setEvents, 8000);
  usePoll('/alerts/rules', setRules, 20000);

  const movers = [...overview].sort((a, b) =>
    Math.abs(b.change_1h ?? 0) - Math.abs(a.change_1h ?? 0));
  const widest = markets.filter((m) => m.mid > 0)
    .sort((a, b) => b.spread_pct - a.spread_pct).slice(0, 8);
  const flagged = liquidity.filter((r) => r.warnings.length);
  const liqBoard = (flagged.length ? flagged : liquidity).slice(0, 10);

  const submitRule = async () => {
    try {
      await apiPost('/alerts/rules', {
        ...form,
        base: form.base || null, exchange: form.exchange || null,
        threshold: +form.threshold, window_sec: +form.window_sec,
        cooldown_sec: +form.cooldown_sec,
      });
      setShowForm(false);
      setRules(await apiGet('/alerts/rules'));
    } catch (e) { alert(e.message); }
  };

  return (
    <div>
      <h2 style={{ fontSize: 18, marginBottom: 14 }}>{t('desk')}</h2>
      <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(420px, 1fr))' }}>

        {/* arbitrage scanner */}
        <div className="card" style={{ padding: 0 }}>
          <h3 style={{ padding: '14px 16px 0' }}>{t('topArbitrage')}</h3>
          <div className="tbl-scroll">
          <table className="tbl">
            <thead><tr>
              <th>{t('asset')}</th><th>{t('buyAt')} → {t('sellAt')}</th>
              <th>{t('net')}</th><th>{t('profit')}</th>
            </tr></thead>
            <tbody>
              {arb.filter((o) => o.net_pct > 0).slice(0, 8).map((o, i) => (
                <tr key={i}>
                  <td><b>{o.base}</b></td>
                  <td>
                    <span style={{ color: 'var(--green)' }}>{o.buy_exchange}</span>
                    {' → '}
                    <span style={{ color: 'var(--red)' }}>{o.sell_exchange}</span>
                  </td>
                  <td className="num" style={{ color: 'var(--green)', fontWeight: 700 }}>
                    {o.net_pct.toFixed(3)}%
                  </td>
                  <td className="num">
                    {fmtCompact(o.est_profit_quote)}
                    <div style={{ fontSize: 10.5, color: 'var(--text-3)' }}>
                      {t('size')}: {fmtQty(o.max_size_base, o.max_size_base)} {o.base}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
          {!arb.filter((o) => o.net_pct > 0).length &&
            <EmptyState icon={<IconZap />} title={t('noData')} hint={t('arbHint')} />}
        </div>

        {/* movers */}
        <div className="card" style={{ padding: 0 }}>
          <h3 style={{ padding: '14px 16px 0' }}>{t('largestMovers')}</h3>
          <table className="tbl">
            <thead><tr>
              <th>{t('asset')}</th><th>{t('price')}</th>
              <th>{t('change1h')}</th><th>{t('change24h')}</th><th>{t('premium')}</th>
            </tr></thead>
            <tbody>
              {movers.map((r) => (
                <tr key={r.base}>
                  <td><b>{r.base}</b></td>
                  <td className="num">{fmtNum(r.price, 0)}</td>
                  <td><ChangePill value={r.change_1h} /></td>
                  <td><ChangePill value={r.change_24h} /></td>
                  <td className="num" style={{ color: premiumColor(r.premium_pct) }}>
                    {fmtPct(r.premium_pct)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* widest spreads */}
        <div className="card" style={{ padding: 0 }}>
          <h3 style={{ padding: '14px 16px 0' }}>{t('widestSpreads')}</h3>
          <table className="tbl">
            <thead><tr>
              <th>{t('exchange')}</th><th>{t('asset')}</th>
              <th>{t('spread')}</th><th>{t('depth')}</th>
            </tr></thead>
            <tbody>
              {widest.map((m, i) => (
                <tr key={i}>
                  <td><b>{m.exchange}</b></td>
                  <td>{m.base}</td>
                  <td className="num" style={{ color: m.spread_pct > 1 ? 'var(--red)' : 'var(--text)' }}>
                    {m.spread_pct.toFixed(3)}%
                  </td>
                  <td className="num">{fmtCompact(m.bid_depth_quote + m.ask_depth_quote)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* liquidity warnings — real order-book analytics per venue */}
        <div className="card" style={{ padding: 0 }}>
          <h3 style={{ padding: '14px 16px 0' }}>
            {t('liquidityWarnings')}
            {flagged.length > 0 && <span className="badge warning">{flagged.length}</span>}
          </h3>
          <table className="tbl">
            <thead><tr>
              <th>{t('exchange')}</th><th>{t('asset')}</th>
              <th>{t('liquidityScore')}</th><th>{t('spread')}</th>
              <th>{t('depth')}</th><th>{t('orderImpact')}</th><th>⚠</th>
            </tr></thead>
            <tbody>
              {liqBoard.map((r, i) => {
                const spreadHot = r.spread_avg_1h && r.spread_pct >= 2 * r.spread_avg_1h;
                return (
                  <tr key={i} style={{ opacity: r.warnings.length ? 1 : 0.65 }}>
                    <td><b>{r.exchange}</b></td>
                    <td>{r.base}</td>
                    <td><ScoreBar score={r.liquidity_score} /></td>
                    <td className="num" style={{ color: spreadHot ? 'var(--red)' : 'var(--text)' }}>
                      {r.spread_pct.toFixed(2)}%
                      {r.spread_avg_1h != null && (
                        <span style={{ fontSize: 10, color: 'var(--text-3)' }}> /μ{r.spread_avg_1h.toFixed(2)}</span>)}
                    </td>
                    <td className="num">
                      {fmtCompact(r.bid_depth_quote + r.ask_depth_quote)}
                      {r.depth_drop_pct != null && r.depth_drop_pct >= 30 && (
                        <span style={{ color: 'var(--red)', fontSize: 10 }}> ▼{r.depth_drop_pct.toFixed(0)}%</span>)}
                    </td>
                    <td className="num" style={{
                      color: r.impact_pct == null ? 'var(--red)'
                           : r.impact_pct >= 0.5 ? 'var(--amber)' : 'var(--green)' }}>
                      {r.impact_pct == null ? t('bookTooThin') : r.impact_pct.toFixed(2) + '%'}
                    </td>
                    <td>{r.warnings.length > 0 &&
                      <span className="badge critical">{r.warnings.length}</span>}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {!liqBoard.length && <div className="empty">{t('noData')}</div>}
          <div style={{ padding: '8px 16px 12px', fontSize: 10.5, color: 'var(--text-3)' }}>
            {t('impactNote')}
          </div>
        </div>

        {/* alert feed */}
        <div className="card">
          <h3>{t('alertFeed')}</h3>
          <div style={{ maxHeight: 320, overflowY: 'auto' }}>
            {events.length ? events.map((e) => (
              <div key={e.id} style={{ display: 'flex', gap: 9, alignItems: 'center',
                                       padding: '7px 0', borderBottom: '1px solid var(--border)',
                                       opacity: e.acknowledged ? 0.45 : 1 }}>
                <Badge level={e.severity} />
                <span style={{ flex: 1, fontSize: 12.5 }}>{e.message}</span>
                <span style={{ fontSize: 10.5, color: 'var(--text-3)', whiteSpace: 'nowrap' }}>
                  {timeAgo(e.ts, lang)}
                </span>
                {!e.acknowledged && (
                  <button className="btn ghost sm" onClick={async () => {
                    await apiPost(`/alerts/events/${e.id}/ack`, {});
                    setEvents(await apiGet('/alerts/events?hours=24'));
                  }}>{t('acknowledge')}</button>
                )}
              </div>
            )) : <EmptyState icon={<IconBell />} title={t('noAlerts')} />}
          </div>
        </div>

        {/* alert rules */}
        <div className="card">
          <h3>
            {t('alertRules')}
            <button className="btn sm" onClick={() => setShowForm(!showForm)}>
              {showForm ? '×' : '+ ' + t('addRule')}
            </button>
          </h3>
          {showForm && (
            <div style={{ display: 'grid', gap: 8, marginBottom: 14,
                          gridTemplateColumns: '1fr 1fr' }}>
              <input className="input" placeholder={t('ruleName')} value={form.name}
                     onChange={(e) => setForm({ ...form, name: e.target.value })} />
              <select className="input" value={form.rule_type}
                      onChange={(e) => setForm({ ...form, rule_type: e.target.value })}>
                {RULE_TYPES.map((rt) => <option key={rt} value={rt}>{rt}</option>)}
              </select>
              <input className="input" placeholder="Base (BTC, empty=all)" value={form.base}
                     onChange={(e) => setForm({ ...form, base: e.target.value })} />
              <input className="input" placeholder="Exchange (empty=all)" value={form.exchange}
                     onChange={(e) => setForm({ ...form, exchange: e.target.value })} />
              <input className="input" type="number" step="0.1" placeholder={t('threshold')}
                     value={form.threshold}
                     onChange={(e) => setForm({ ...form, threshold: e.target.value })} />
              <input className="input" type="number" placeholder={t('window') + ' (s)'}
                     value={form.window_sec}
                     onChange={(e) => setForm({ ...form, window_sec: e.target.value })} />
              <button className="btn" style={{ gridColumn: 'span 2' }} onClick={submitRule}>
                {t('add')}
              </button>
            </div>
          )}
          {rules.map((r) => (
            <div key={r.id} style={{ display: 'flex', gap: 9, alignItems: 'center',
                                     padding: '7px 0', borderBottom: '1px solid var(--border)' }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: 6, flex: 1, cursor: 'pointer' }}>
                <input type="checkbox" checked={!!r.enabled} onChange={async () => {
                  await apiPatch(`/alerts/rules/${r.id}?enabled=${!r.enabled}`);
                  setRules(await apiGet('/alerts/rules'));
                }} />
                <span style={{ fontSize: 12.5 }}>
                  <b>{r.name}</b>
                  <span style={{ color: 'var(--text-3)' }}> · {r.rule_type} &gt; {r.threshold}</span>
                </span>
              </label>
              <button className="btn danger sm" onClick={async () => {
                await apiDelete(`/alerts/rules/${r.id}`);
                setRules(await apiGet('/alerts/rules'));
              }}>×</button>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
