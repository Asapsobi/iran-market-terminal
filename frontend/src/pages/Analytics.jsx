// Spread analysis, liquidity analysis, market monitoring.
import { useEffect, useState } from 'react';
import { apiGet, usePoll } from '../api';
import { Badge, EmptyState, Seg } from '../components';
import { IconActivity } from '../icons';
import { MultiLineChart, AreaChart } from '../charts';
import { useLang } from '../i18n';
import { fmtCompact, fmtPct, premiumColor, timeAgo } from '../util';

// one-toman spreads on cheap assets (USDT ≈ 0.0004%) must not read as 0.000%
const fmtSpread = (v) => (v == null ? '—' : v.toFixed(v < 0.01 ? 4 : 3) + '%');

const WINDOWS = [
  { value: 3600, label: '1H' }, { value: 14400, label: '4H' },
  { value: 86400, label: '1D' }, { value: 604800, label: '1W' },
  { value: 2592000, label: '1M' },
];

export default function Analytics({ asset, assets, onSelectAsset, meta }) {
  const { t, lang } = useLang();
  const [window_, setWindow] = useState(86400);
  const [spread, setSpread] = useState(null);
  const [markets, setMarkets] = useState([]);
  const [anomalies, setAnomalies] = useState([]);

  usePoll('/markets', setMarkets, 6000);
  usePoll('/anomalies', setAnomalies, 10000);

  useEffect(() => {
    setSpread(null);
    apiGet(`/analytics/spread/${asset}?window=${window_}`).then(setSpread).catch(() => {});
  }, [asset, window_]);

  const colorOf = (ex) =>
    meta?.exchanges?.find((e) => e.name === ex)?.color || '#8a95a8';

  const spreadSeries = spread
    ? Object.entries(
        (spread.history || []).reduce((acc, p) => {
          (acc[p.exchange] = acc[p.exchange] || []).push({ ts: p.ts, value: p.spread_pct });
          return acc;
        }, {})
      ).map(([name, points]) => ({ name, color: colorOf(name), points }))
    : [];

  const assetMarkets = markets.filter((m) => m.base === asset && m.mid > 0);

  return (
    <div>
      <div style={{ display: 'flex', gap: 12, alignItems: 'center', marginBottom: 14, flexWrap: 'wrap' }}>
        <h2 style={{ fontSize: 18 }}>{t('analytics')}</h2>
        <Seg options={assets} value={asset} onChange={onSelectAsset} />
        <Seg options={WINDOWS} value={window_} onChange={setWindow} />
      </div>

      {/* spread stats */}
      <div className="card" style={{ marginBottom: 14 }}>
        <h3>{t('spreadAnalysis')} — {asset}</h3>
        <div style={{ overflowX: 'auto' }}>
          <table className="tbl">
            <thead>
              <tr>
                <th>{t('exchange')}</th><th>{t('currentSpread')}</th>
                <th>{t('avgSpread')}</th><th>{t('minSpread')}</th>
                <th>{t('maxSpread')}</th><th>{t('spreadVol')} (σ)</th>
              </tr>
            </thead>
            <tbody>
              {spread && Object.entries(spread.stats).map(([ex, s]) => (
                <tr key={ex}>
                  <td><b style={{ color: colorOf(ex) }}>{ex}</b></td>
                  <td className="num">{fmtSpread(s.current)}</td>
                  <td className="num">{fmtSpread(s.avg)}</td>
                  <td className="num" style={{ color: 'var(--green)' }}>{fmtSpread(s.min)}</td>
                  <td className="num" style={{ color: 'var(--red)' }}>{fmtSpread(s.max)}</td>
                  <td className="num">{s.stdev != null ? s.stdev.toFixed(4) : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div style={{ marginTop: 14 }}>
          {spreadSeries.length
            ? <MultiLineChart seriesList={spreadSeries} percent height={260} />
            : <div className="empty">{t('noData')}</div>}
        </div>
      </div>

      <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(380px, 1fr))', marginBottom: 14 }}>
        {/* liquidity */}
        <div className="card">
          <h3>{t('liquidityAnalysis')} — {asset}</h3>
          {assetMarkets.map((m) => {
            const total = m.bid_depth_quote + m.ask_depth_quote;
            const max = Math.max(...assetMarkets.map((x) => x.bid_depth_quote + x.ask_depth_quote), 1);
            return (
              <div key={m.exchange} style={{ marginBottom: 11 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, marginBottom: 3 }}>
                  <b style={{ color: colorOf(m.exchange) }}>{m.exchange}</b>
                  <span className="num" style={{ color: 'var(--text-2)' }}>
                    {fmtCompact(total)} · {t('imbalance')}{' '}
                    <span style={{ color: m.depth_imbalance > 0 ? 'var(--green)' : 'var(--red)' }}>
                      {(m.depth_imbalance * 100).toFixed(0)}%
                    </span>
                  </span>
                </div>
                <div style={{ display: 'flex', height: 10, borderRadius: 5, overflow: 'hidden',
                              width: `${Math.max(8, (total / max) * 100)}%`, minWidth: 40 }}>
                  <div style={{ background: 'var(--green)', width: `${(m.bid_depth_quote / (total || 1)) * 100}%` }} />
                  <div style={{ background: 'var(--red)', flex: 1 }} />
                </div>
              </div>
            );
          })}
          {!assetMarkets.length && <div className="empty">{t('noData')}</div>}
        </div>

        {/* premium history — configurable benchmark methods */}
        <PremiumPanel asset={asset} window_={window_} t={t} />
      </div>

      <div style={{ height: 14 }} />

      {/* monitoring / anomalies */}
      <div className="card">
        <h3>{t('marketMonitoring')} — {t('anomalies')}</h3>
        {anomalies.length ? anomalies.map((a, i) => (
          <div key={i} style={{ display: 'flex', gap: 10, alignItems: 'center',
                                padding: '8px 0', borderBottom: '1px solid var(--border)' }}>
            <Badge level={a.severity} />
            <span style={{ flex: 1 }}>{a.message}</span>
            <span style={{ fontSize: 11, color: 'var(--text-3)' }}>{timeAgo(a.timestamp, lang)}</span>
          </div>
        )) : <EmptyState icon={<IconActivity />} title={t('noAnomalies')} hint={t('anomalyHint')} />}
      </div>
    </div>
  );
}

/* ------------------- premium panel (configurable benchmark) ------------------- */

const WINDOW_TO_RANGE = (w) =>
  w <= 3600 ? '1h' : w <= 14400 ? '4h' : w <= 86400 ? '1d' : w <= 604800 ? '1w' : '1m';

function PremiumPanel({ asset, window_, t }) {
  const [method, setMethod] = useState('composite');
  const [exchange, setExchange] = useState('');
  const [methods, setMethods] = useState({ methods: [], exchanges: [] });
  const [data, setData] = useState(null);

  useEffect(() => {
    apiGet('/premium/methods').then(setMethods).catch(() => {});
  }, []);

  useEffect(() => {
    if (asset === 'USDT') { setData(null); return; }
    if (method === 'exchange' && !exchange) return;
    setData(null);
    const range = WINDOW_TO_RANGE(window_);
    const q = `/premium/${asset}?range=${range}&method=${method}` +
              (method === 'exchange' ? `&exchange=${exchange}` : '');
    const load = () => apiGet(q).then(setData).catch(() => setData({ series: [] }));
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [asset, window_, method, exchange]);

  const methodLabels = {
    composite: t('methodComposite'), best_mid: t('methodBest'),
    vwap: t('methodVwap'), exchange: t('methodExchange'),
  };

  if (asset === 'USDT') {
    return (
      <div className="card">
        <h3>{t('premium')} — {asset}</h3>
        <EmptyState icon={<IconActivity />} title={t('usdtBenchmark')} />
      </div>
    );
  }

  return (
    <div className="card">
      <h3>
        {t('premium')} — {asset} {t('vsGlobal')}
        <span style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          <select className="input" style={{ width: 'auto', padding: '3px 8px', fontSize: 12 }}
                  value={method} onChange={(e) => setMethod(e.target.value)}
                  title={t('benchmark')}>
            {(methods.methods.length ? methods.methods : ['composite'])
              .map((m) => <option key={m} value={m}>{methodLabels[m] || m}</option>)}
          </select>
          {method === 'exchange' && (
            <select className="input" style={{ width: 'auto', padding: '3px 8px', fontSize: 12 }}
                    value={exchange} onChange={(e) => setExchange(e.target.value)}>
              <option value="">—</option>
              {methods.exchanges.map((ex) => <option key={ex} value={ex}>{ex}</option>)}
            </select>
          )}
        </span>
      </h3>

      {data?.stats && (
        <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', fontSize: 12,
                      color: 'var(--text-2)', marginBottom: 10 }}>
          <span>{t('currentSpread')}: <b className="num" style={{ color: premiumColor(data.current) }}>
            {fmtPct(data.current)}</b></span>
          <span>μ <b className="num">{data.stats.avg != null ? data.stats.avg.toFixed(2) + '%' : '—'}</b></span>
          <span style={{ color: 'var(--green)' }}>{t('minSpread')} <b className="num">
            {data.stats.min != null ? data.stats.min.toFixed(2) + '%' : '—'}</b></span>
          <span style={{ color: 'var(--red)' }}>{t('maxSpread')} <b className="num">
            {data.stats.max != null ? data.stats.max.toFixed(2) + '%' : '—'}</b></span>
          {data.usd_reference && <span>USD: <b className="num">${data.usd_reference.toLocaleString()}</b></span>}
        </div>
      )}

      {data === null ? <div className="skel" style={{ height: 220 }} />
        : data.series?.length
          ? <AreaChart points={data.series.map((p) => ({ ts: p.ts, value: p.premium_pct }))}
                       color="#ffb020" height={220} />
          : <EmptyState icon={<IconActivity />} title={t('noData')} hint={t('premiumHint')} />}
    </div>
  );
}
