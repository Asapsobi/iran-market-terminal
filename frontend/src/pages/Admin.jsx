// Data management: add exchanges/pairs at runtime, tune settings, diagnostics.
import { useEffect, useRef, useState } from 'react';
import { apiDelete, apiGet, apiPost, usePoll } from '../api';
import { StatusDot } from '../components';
import { IconActivity, IconAlert, IconCheck } from '../icons';
import { useLang } from '../i18n';
import { fmtDateTime, timeAgo } from '../util';

/* ------------------------- system diagnostics ------------------------- */

function DiagnosticsCard({ t, lang }) {
  const [diag, setDiag] = useState(null);
  const [net, setNet] = useState(null);
  const [testing, setTesting] = useState(false);

  usePoll('/diagnostics', setDiag, 15000);

  const runNetTest = async () => {
    setTesting(true); setNet(null);
    try { setNet(await apiPost('/diagnostics/nettest', {})); }
    catch (e) { setNet([{ target: 'nettest', ok: false, detail: e.message }]); }
    finally { setTesting(false); }
  };

  if (!diag) return <div className="card"><div className="skel" style={{ height: 160 }} /></div>;

  const checks = Object.entries(diag.checks || {});
  const failing = checks.filter(([, c]) => !c.ok).length;

  return (
    <div className="card" style={{ gridColumn: '1 / -1' }}>
      <h3>
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
          <IconActivity size={14} /> {t('diagnostics')}
          {failing > 0
            ? <span className="badge critical">{failing}</span>
            : <span className="badge info">OK</span>}
        </span>
        <button className="btn sm" onClick={runNetTest} disabled={testing}>
          {testing ? '…' : t('runNetTest')}
        </button>
      </h3>

      <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginBottom: 10 }} className="num">
        python {diag.system.python} · {diag.system.platform} ·{' '}
        {diag.system.serverless ? 'SERVERLESS' : 'server'} ·{' '}
        {diag.system.demo_mode ? 'DEMO' : 'live'} · log {diag.system.log_level} ·{' '}
        {diag.system.uptime_hint}
      </div>

      <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 330px), 1fr))', gap: 6 }}>
        {checks.map(([name, c]) => (
          <div key={name} style={{ display: 'flex', gap: 8, alignItems: 'flex-start',
                                   padding: '6px 8px', background: 'var(--bg-2)',
                                   borderRadius: 7, fontSize: 12,
                                   border: `1px solid ${c.ok ? 'var(--border)' : '#ea394355'}` }}>
            <span style={{ color: c.ok ? 'var(--green)' : 'var(--red)', flexShrink: 0 }}>
              {c.ok ? <IconCheck size={14} /> : <IconAlert size={14} />}
            </span>
            <span style={{ minWidth: 0 }}>
              <b>{name.replace(/_/g, ' ')}</b>
              <div style={{ color: c.ok ? 'var(--text-3)' : 'var(--red)',
                            fontSize: 11, wordBreak: 'break-word' }}>{c.detail}</div>
            </span>
          </div>
        ))}
      </div>

      {diag.venues?.length > 0 && (
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 10 }}>
          {diag.venues.map((v, i) => (
            <span key={i} className="chip" style={{ cursor: 'default', fontSize: 11 }}>
              <StatusDot status={v.status} /> {v.exchange} {v.base}
              {v.age_sec != null && <span style={{ color: 'var(--text-3)' }}> · {v.age_sec}s</span>}
            </span>
          ))}
        </div>
      )}

      {net && (
        <div className="tbl-scroll" style={{ marginTop: 12 }}>
          <table className="tbl">
            <thead><tr>
              <th>{t('target')}</th><th>{t('status')}</th>
              <th>{t('latency')}</th><th>{t('detail')}</th>
            </tr></thead>
            <tbody>
              {net.map((r, i) => (
                <tr key={i}>
                  <td><b>{r.target}</b></td>
                  <td style={{ color: r.ok ? 'var(--green)' : 'var(--red)', fontWeight: 700 }}>
                    {r.ok ? 'OK' : 'FAILED'}{r.status ? ` (${r.status})` : ''}
                  </td>
                  <td className="num">{r.latency_ms}ms</td>
                  <td style={{ fontSize: 11, color: 'var(--text-2)', wordBreak: 'break-word' }}>
                    {r.detail}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/* ------------------------------ live logs ----------------------------- */

const LEVEL_COLORS = { DEBUG: 'var(--text-3)', INFO: 'var(--text-2)',
                       WARNING: 'var(--amber)', ERROR: 'var(--red)',
                       CRITICAL: 'var(--red)' };

function LogsCard({ t, lang }) {
  const [logs, setLogs] = useState([]);
  const [level, setLevel] = useState('INFO');
  const [search, setSearch] = useState('');
  const boxRef = useRef(null);

  usePoll(`/logs?level=${level}&limit=400&search=${encodeURIComponent(search)}`,
          setLogs, 5000, [level, search]);

  useEffect(() => {
    const el = boxRef.current;
    if (el) el.scrollTop = el.scrollHeight;      // follow the tail
  }, [logs]);

  const download = () => {
    const text = logs.map((r) =>
      `${new Date(r.ts * 1000).toISOString()} ${r.level.padEnd(7)} ${r.logger}: ${r.message}`
    ).join('\n');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([text], { type: 'text/plain' }));
    a.download = `terminal-logs-${Date.now()}.txt`;
    a.click();
  };

  return (
    <div className="card" style={{ gridColumn: '1 / -1' }}>
      <h3>
        {t('liveLogs')}
        <span style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <input className="input" style={{ width: 160, padding: '3px 9px', fontSize: 12 }}
                 placeholder={t('searchLogs')} value={search}
                 onChange={(e) => setSearch(e.target.value)} />
          <select className="input" style={{ width: 'auto', padding: '3px 8px', fontSize: 12 }}
                  value={level} onChange={(e) => setLevel(e.target.value)}>
            {['DEBUG', 'INFO', 'WARNING', 'ERROR'].map((l) =>
              <option key={l} value={l}>{l}</option>)}
          </select>
          <button className="btn ghost sm" onClick={download}>{t('download')}</button>
        </span>
      </h3>
      <div ref={boxRef}
           style={{ maxHeight: 340, overflowY: 'auto', background: 'var(--bg)',
                    borderRadius: 8, border: '1px solid var(--border)',
                    padding: '8px 10px', fontFamily: 'var(--mono)', fontSize: 11,
                    lineHeight: 1.55, direction: 'ltr', textAlign: 'left' }}>
        {logs.map((r, i) => (
          <div key={i} style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
            <span style={{ color: 'var(--text-3)' }}>
              {new Date(r.ts * 1000).toLocaleTimeString('en-GB')}
            </span>{' '}
            <b style={{ color: LEVEL_COLORS[r.level] || 'var(--text)' }}>{r.level}</b>{' '}
            <span style={{ color: 'var(--accent)' }}>{r.logger}</span>{' '}
            {r.message}
          </div>
        ))}
        {!logs.length && (
          <div style={{ color: 'var(--text-3)' }}>
            — no records at level {level}{search ? ` matching "${search}"` : ''} —
          </div>
        )}
      </div>
      <div style={{ fontSize: 10.5, color: 'var(--text-3)', marginTop: 6 }}>
        {t('logHint')}
      </div>
    </div>
  );
}

const SPEC_PLACEHOLDER = `{
  "orderbook_url": "https://api.example.com/depth?symbol={symbol}",
  "symbol_template": "{base}{quote}",
  "quote_name": "IRT",
  "bids_path": "bids",
  "asks_path": "asks",
  "price_scale": 1.0,
  "taker_fee_pct": 0.25
}`;

export default function Admin({ meta, refreshMeta }) {
  const { t, lang } = useLang();
  const [settings, setSettings] = useState(null);
  const [saved, setSaved] = useState(false);
  const [pairs, setPairs] = useState([]);
  const [customEx, setCustomEx] = useState([]);
  const [newPair, setNewPair] = useState('');
  const [newCgId, setNewCgId] = useState('');
  const [exName, setExName] = useState('');
  const [exSpec, setExSpec] = useState('');
  const [err, setErr] = useState('');

  usePoll('/settings', (s) => setSettings((prev) => prev ?? s), 60000);
  usePoll('/admin/pairs', setPairs, 30000);
  usePoll('/admin/exchanges', setCustomEx, 30000);

  const saveSettings = async () => {
    try {
      setSettings(await apiPost('/settings', settings));
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (e) { setErr(e.message); }
  };

  const addPair = async () => {
    if (!newPair.trim()) return;
    try {
      await apiPost('/admin/pairs', {
        base: newPair.trim().toUpperCase(),
        coingecko_id: newCgId.trim() || null,
      });
      setNewPair(''); setNewCgId('');
      setPairs(await apiGet('/admin/pairs'));
      refreshMeta?.();
    } catch (e) { setErr(e.message); }
  };

  const addExchange = async () => {
    setErr('');
    try {
      const spec = JSON.parse(exSpec || '{}');
      await apiPost('/admin/exchanges', { name: exName.trim(), spec });
      setExName(''); setExSpec('');
      setCustomEx(await apiGet('/admin/exchanges'));
      refreshMeta?.();
    } catch (e) { setErr(e.message); }
  };

  const fields = [
    ['market_interval', 'marketInterval'], ['snapshot_interval', 'snapshotInterval'],
    ['news_interval', 'newsInterval'], ['calendar_interval', 'calendarInterval'],
    ['request_timeout', 'requestTimeout'], ['ui_refresh_interval', 'uiRefresh'],
    ['arb_min_edge_pct', 'arbMinEdge'], ['stale_latency_ms', 'staleLatency'],
  ];

  return (
    <div>
      <h2 style={{ fontSize: 18, marginBottom: 4 }}>{t('admin')}</h2>
      <div style={{ fontSize: 11.5, color: 'var(--text-2)', marginBottom: 14,
                    fontFamily: 'var(--mono, monospace)' }}
           title="Build currently running on the server">
        v{meta?.version || '2.2.0'}
        {meta?.build?.git_sha && meta.build.git_sha !== 'dev' ? ` · ${meta.build.git_sha}` : ''}
        {meta?.build?.build_time && meta.build.build_time !== 'unknown'
          ? ` · built ${meta.build.build_time}` : ''}
        {meta?.collector_enabled === false ? ' · ⚠ collector OFF' : ''}
      </div>
      {err && <div className="toast critical" style={{ position: 'static', marginBottom: 12 }}>{err}</div>}

      <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 360px), 1fr))' }}>
        <DiagnosticsCard t={t} lang={lang} />
        <LogsCard t={t} lang={lang} />
        {/* settings */}
        <div className="card">
          <h3>{t('settings')}</h3>
          {settings ? (
            <div style={{ display: 'grid', gap: 10 }}>
              {fields.map(([key, label]) => (
                <label key={key} style={{ display: 'grid', gridTemplateColumns: '1fr 110px',
                                          gap: 8, alignItems: 'center', fontSize: 12.5 }}>
                  <span style={{ color: 'var(--text-2)' }}>{t(label)}</span>
                  <input className="input" type="number" value={settings[key]}
                         onChange={(e) => setSettings({ ...settings, [key]: +e.target.value })} />
                </label>
              ))}
              <button className="btn" onClick={saveSettings}>
                {saved ? '✓ ' + t('saved') : t('save')}
              </button>
            </div>
          ) : <div className="skel" style={{ height: 200 }} />}
        </div>

        {/* pairs */}
        <div className="card">
          <h3>{t('addPair')}</h3>
          <div style={{ display: 'grid', gap: 8, marginBottom: 12 }}>
            <div style={{ display: 'flex', gap: 8 }}>
              <input className="input" placeholder={t('baseAsset')} value={newPair}
                     onChange={(e) => setNewPair(e.target.value)}
                     onKeyDown={(e) => e.key === 'Enter' && addPair()} />
              <button className="btn" onClick={addPair}>{t('add')}</button>
            </div>
            <input className="input" placeholder={t('coingeckoId')} value={newCgId}
                   onChange={(e) => setNewCgId(e.target.value)}
                   onKeyDown={(e) => e.key === 'Enter' && addPair()} />
          </div>
          <h3>{t('customPairs')}</h3>
          {pairs.length ? pairs.map((p) => (
            <div key={p.base + p.quote}
                 style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                          padding: '6px 0', borderBottom: '1px solid var(--border)' }}>
              <b>{p.base}/{p.quote}</b>
              <span style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                <span style={{ fontSize: 11, color: 'var(--text-3)' }}>
                  {fmtDateTime(p.created_ts, lang)}
                </span>
                <button className="btn danger sm" onClick={async () => {
                  await apiDelete(`/admin/pairs/${p.base}`);
                  setPairs(await apiGet('/admin/pairs'));
                  refreshMeta?.();
                }}>×</button>
              </span>
            </div>
          )) : <div className="empty" style={{ padding: '12px 0' }}>—</div>}
          <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 10 }}>
            {t('exchanges')}: {meta?.assets?.join(', ')}
          </div>
        </div>

        {/* custom exchanges */}
        <div className="card" style={{ gridColumn: '1 / -1' }}>
          <h3>{t('addExchange')} — {t('dataManagement')}</h3>
          <div style={{ display: 'grid', gap: 10, gridTemplateColumns: 'minmax(200px, 300px) 1fr',
                        alignItems: 'start' }}>
            <div style={{ display: 'grid', gap: 10 }}>
              <input className="input" placeholder={t('exchangeName')} value={exName}
                     onChange={(e) => setExName(e.target.value)} />
              <button className="btn" onClick={addExchange} disabled={!exName.trim()}>
                {t('add')}
              </button>
              <div style={{ fontSize: 11.5, color: 'var(--text-3)', lineHeight: 1.6 }}>
                Declarative connector: point it at any REST order-book endpoint.
                <code style={{ color: 'var(--accent)' }}> {'{symbol}'}</code>,
                <code style={{ color: 'var(--accent)' }}> {'{base}'}</code>,
                <code style={{ color: 'var(--accent)' }}> {'{quote}'}</code> are substituted.
                Use <code>price_scale: 0.1</code> for Rial-quoted venues.
              </div>
            </div>
            <textarea className="input" placeholder={SPEC_PLACEHOLDER} value={exSpec}
                      onChange={(e) => setExSpec(e.target.value)} spellCheck={false} />
          </div>

          {customEx.length > 0 && (
            <div style={{ marginTop: 16 }}>
              <h3>{t('customExchanges')}</h3>
              {customEx.map((x) => (
                <div key={x.name}
                     style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                              padding: '6px 0', borderBottom: '1px solid var(--border)' }}>
                  <span><b>{x.name}</b>
                    <span style={{ fontSize: 11, color: 'var(--text-3)', marginInlineStart: 8 }}>
                      {x.spec?.orderbook_url}
                    </span>
                  </span>
                  <button className="btn danger sm" onClick={async () => {
                    await apiDelete(`/admin/exchanges/${encodeURIComponent(x.name)}`);
                    setCustomEx(await apiGet('/admin/exchanges'));
                    refreshMeta?.();
                  }}>×</button>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
