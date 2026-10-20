// CoinMarketCap-style market overview with favorites, live price ticks,
// and inline row expansion showing the per-venue mini comparison.
import { useState } from 'react';
import { apiGet, usePoll } from '../api';
import { AssetIcon, ChangePill, FlashNum, ScoreBar, Sparkline, SortableTh,
         StatusDot, sortRows } from '../components';
import { IconChevronDown, IconChevronRight, IconStar } from '../icons';
import { useLang } from '../i18n';
import { fmtCompact, fmtNum, fmtPct, premiumColor } from '../util';

// never render the quote twice ("USOON/TMN /TMN") even if a stored base has it
const baseOnly = (base) => String(base).split('/')[0];

const loadFavs = () => {
  try { return new Set(JSON.parse(localStorage.getItem('favs') || '[]')); }
  catch { return new Set(); }
};

export default function Overview({ onSelectAsset }) {
  const { t } = useLang();
  const [rows, setRows] = useState(null);
  const [search, setSearch] = useState('');
  const [sort, setSort] = useState({ col: 'rank', dir: 'asc' });
  const [favs, setFavs] = useState(loadFavs);
  const [expanded, setExpanded] = useState(null);
  const [detailCache, setDetailCache] = useState({});
  usePoll('/overview', setRows, 6000);

  if (!rows) return <div className="skel" style={{ height: 300 }} />;

  const toggleFav = (base, e) => {
    e.stopPropagation();
    const next = new Set(favs);
    next.has(base) ? next.delete(base) : next.add(base);
    setFavs(next);
    localStorage.setItem('favs', JSON.stringify([...next]));
  };

  const toggleExpand = async (base, e) => {
    e.stopPropagation();
    if (expanded === base) { setExpanded(null); return; }
    setExpanded(base);
    try {
      const d = await apiGet(`/pair/${base}`);
      setDetailCache((p) => ({ ...p, [base]: d }));
    } catch { /* row shows loading skeleton */ }
  };

  const filtered = rows.filter((r) =>
    r.base.toLowerCase().includes(search.toLowerCase()));
  const sorted = sortRows(filtered, sort, {
    rank: (r) => r.rank,
    price: (r) => r.price,
    c1h: (r) => r.change_1h,
    c24h: (r) => r.change_24h,
    c7d: (r) => r.change_7d,
    premium: (r) => r.premium_pct,
    spread: (r) => r.min_spread_pct,
    liq: (r) => r.liquidity_score,
    vol: (r) => r.volume_24h_quote,
  });
  // favorites pinned on top, preserving sort inside each group
  const pinned = [...sorted.filter((r) => favs.has(r.base)),
                  ...sorted.filter((r) => !favs.has(r.base))];

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                    marginBottom: 12, gap: 12, flexWrap: 'wrap' }}>
        <h2 style={{ fontSize: 'var(--fs-lg)' }}>{t('overview')}</h2>
        <input className="input" style={{ maxWidth: 220 }} value={search}
               placeholder={t('searchAssets')} onChange={(e) => setSearch(e.target.value)} />
      </div>
      {/* .fit: columns drop by width (hide-*) so the table fits and the
          sticky header can stick to the page under the topbar */}
      <div className="card fit" style={{ padding: 0 }}>
        <table className="tbl">
          <thead>
            <tr>
              <th style={{ width: 30 }} />
              <SortableTh label={t('rank')} col="rank" sort={sort} setSort={setSort} />
              <th>{t('asset')}</th>
              <SortableTh label={t('price')} col="price" sort={sort} setSort={setSort} />
              <SortableTh label={t('change1h')} col="c1h" sort={sort} setSort={setSort} className="hide-lg" />
              <SortableTh label={t('change24h')} col="c24h" sort={sort} setSort={setSort} />
              <SortableTh label={t('change7d')} col="c7d" sort={sort} setSort={setSort} />
              <SortableTh label={t('premium')} col="premium" sort={sort} setSort={setSort} />
              <SortableTh label={t('spread')} col="spread" sort={sort} setSort={setSort} className="hide-lg" />
              <SortableTh label={t('liquidity')} col="liq" sort={sort} setSort={setSort} className="hide-md" />
              <SortableTh label={t('volume24')} col="vol" sort={sort} setSort={setSort} />
              <th>{t('bestBuy')}</th>
              <th>{t('bestSell')}</th>
              <th className="hide-xl">{t('last7d')}</th>
              <th style={{ width: 30 }} />
            </tr>
          </thead>
          <tbody>
            {pinned.map((r) => {
              const detail = detailCache[r.base];
              return [
                <tr key={r.base} className="clickable" onClick={() => onSelectAsset(r.base)}>
                  <td>
                    <button className="icon-btn" style={{ padding: 3 }}
                            aria-label={`favorite ${r.base}`}
                            onClick={(e) => toggleFav(r.base, e)}>
                      <IconStar size={14} filled={favs.has(r.base)} />
                    </button>
                  </td>
                  <td style={{ color: 'var(--text-3)' }}>{r.rank}</td>
                  <td>
                    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 9 }}>
                      <AssetIcon symbol={baseOnly(r.base)} />
                      <span>
                        <b>{baseOnly(r.base)}</b>
                        <span style={{ color: 'var(--text-3)', fontSize: 11.5 }}> /{r.quote}</span>
                        <div style={{ fontSize: 10.5, color: 'var(--text-3)' }}>
                          {r.exchanges_live}/{r.exchanges_total} {t('exchanges')}
                        </div>
                      </span>
                    </span>
                  </td>
                  <td><FlashNum value={r.price} bold /></td>
                  <td className="hide-lg"><ChangePill value={r.change_1h} /></td>
                  <td><ChangePill value={r.change_24h} /></td>
                  <td><ChangePill value={r.change_7d} /></td>
                  {r.base === 'USDT' ? (
                    <td className="num" title={t('pegTip')} style={{ color: premiumColor(r.peg_dev_pct) }}>
                      {fmtPct(r.peg_dev_pct)}
                      <div style={{ fontSize: 10, color: 'var(--text-3)' }}>{t('vsPeg')}</div>
                    </td>
                  ) : (
                    <td className="num" title={t('premiumTip')} style={{ color: premiumColor(r.premium_pct) }}>
                      {fmtPct(r.premium_pct)}
                    </td>
                  )}
                  <td className="num hide-lg">{r.min_spread_pct != null ? r.min_spread_pct.toFixed(2) + '%' : '—'}</td>
                  <td className="hide-md"><ScoreBar score={r.liquidity_score} /></td>
                  <td className="num">{fmtCompact(r.volume_24h_quote)}</td>
                  <td>
                    {r.best_ask ? (
                      <span>
                        <b style={{ color: 'var(--green)' }}>{r.best_ask.exchange}</b>
                        <div className="num" style={{ fontSize: 11, color: 'var(--text-3)' }}>
                          {fmtNum(r.best_ask.price, 0)}
                        </div>
                      </span>) : '—'}
                  </td>
                  <td>
                    {r.best_bid ? (
                      <span>
                        <b style={{ color: 'var(--red)' }}>{r.best_bid.exchange}</b>
                        <div className="num" style={{ fontSize: 11, color: 'var(--text-3)' }}>
                          {fmtNum(r.best_bid.price, 0)}
                        </div>
                      </span>) : '—'}
                  </td>
                  <td className="hide-xl"><Sparkline data={r.sparkline} width={110} /></td>
                  <td>
                    <button className="icon-btn" style={{ padding: 3 }}
                            aria-label={`expand ${r.base}`}
                            onClick={(e) => toggleExpand(r.base, e)}>
                      {expanded === r.base ? <IconChevronDown size={15} /> : <IconChevronRight size={15} />}
                    </button>
                  </td>
                </tr>,
                expanded === r.base && (
                  <tr key={r.base + '-x'}>
                    <td colSpan={15} style={{ background: 'var(--bg-2)', padding: '10px 16px' }}>
                      {detail ? (
                        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
                          {detail.exchanges.filter((e) => e.mid > 0).map((e) => (
                            <div key={e.exchange}
                                 style={{ background: 'var(--card)', border: '1px solid var(--border)',
                                          borderRadius: 8, padding: '8px 12px', minWidth: 150 }}>
                              <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 4 }}>
                                <StatusDot status={e.status} />
                                <b style={{ color: e.color, fontSize: 12 }}>{e.exchange}</b>
                                <span style={{ marginInlineStart: 'auto', fontSize: 10, color: 'var(--text-3)' }}>
                                  {e.spread_pct.toFixed(2)}%
                                </span>
                              </div>
                              <div className="num" style={{ fontSize: 11.5 }}>
                                <span style={{ color: 'var(--green)' }}>{fmtNum(e.best_bid, 0)}</span>
                                {' / '}
                                <span style={{ color: 'var(--red)' }}>{fmtNum(e.best_ask, 0)}</span>
                              </div>
                            </div>
                          ))}
                        </div>
                      ) : <div className="skel" style={{ height: 52 }} />}
                    </td>
                  </tr>
                ),
              ];
            })}
          </tbody>
        </table>
        {!pinned.length && <div className="empty">{t('noData')}</div>}
      </div>
    </div>
  );
}
