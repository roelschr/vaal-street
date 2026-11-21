import { drawChart, spark } from './chart.js';

const $ = (sel, root = document) => root.querySelector(sel);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const state = {
  status: null, league: null, bridge: null, unit: localStorage.unit || 'chaos', share: +(localStorage.share || 0.1),
  generation: -1, filters: { q: '', category: '', minValue: 1000, watched: false, kind: 'all', minMargin: 0.03, capital: localStorage.bankroll || '', goldBudget: localStorage.goldBudget || '', rankBy: 'profit' },
  league_view: { horizon: 7, unit: 'divine' }, lab: null, hideout: new URLSearchParams(location.search).has('hideout'),
};
const PALETTE = ['#8fb8f2', '#b9a3ee', '#e8c66a', '#f0787f', '#5fd3a2', '#d9a05b', '#7fd6e0', '#c9cf7a'];

// ---------------------------------------------------------------- plumbing
async function api(path, body) {
  const opts = body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {};
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({ error: res.statusText }));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
const bankQuery = () => (+state.filters.capital > 0 ? { bankroll: +state.filters.capital } : {});
const withLeague = (path, extra = {}) => {
  const q = new URLSearchParams({ ...(state.league ? { league: state.league } : {}), ...extra });
  return `${path}?${q}`;
};
function toast(message, bad) {
  const el = document.createElement('div');
  el.className = 'toast' + (bad ? ' err' : ''); el.textContent = message;
  $('#toasts').append(el); setTimeout(() => el.remove(), 5000);
}

// --------------------------------------------------------------- formatting
function fmt(v) {
  if (v == null || !isFinite(v)) return '–';
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(2) + 'B';
  if (a >= 1e6) return (v / 1e6).toFixed(2) + 'M';
  if (a >= 1000) return Math.round(v).toLocaleString('en-US');
  if (a >= 100) return v.toFixed(1);
  if (a >= 10) return v.toFixed(2);
  if (a >= 1) return v.toFixed(3);
  return v.toPrecision(3);
}
const tick = unit => v => (Math.abs(v) >= 1e4 ? compact(v) : String(parseFloat(v.toPrecision(3)))) + ' ' + unit;
const compact = v => v == null ? '–' : Math.abs(v) >= 1e9 ? (v / 1e9).toFixed(1) + 'B' : Math.abs(v) >= 1e6 ? (v / 1e6).toFixed(1) + 'M' : Math.abs(v) >= 1e3 ? (v / 1e3).toFixed(1) + 'K' : v.toFixed(0);
// A reference-currency amount in the unit the trader chose.
function money(v, unit = state.unit) {
  if (v == null) return '<span class="dim">–</span>';
  return unit === 'divine' && state.bridge ? `${fmt(v / state.bridge)}<span class="dim"> d</span>` : `${fmt(v)}<span class="dim"> c</span>`;
}
const pct = (v, digits = 1) => v == null ? '<span class="dim">–</span>' : `<span class="${v > 0 ? 'up' : v < 0 ? 'down' : 'muted'}">${v > 0 ? '+' : ''}${(v * 100).toFixed(digits)}%</span>`;
const dur = h => h == null ? '–' : h < 1 ? Math.max(1, Math.round(h * 60)) + ' min' : h < 48 ? h.toFixed(1) + ' h' : Math.round(h / 24) + ' d';
const plainPct = (v, digits = 0) => v == null ? '–' : (v * 100).toFixed(digits) + '%';
function ago(t) {
  const s = Date.now() / 1000 - t;
  return s < 5400 ? Math.max(1, Math.round(s / 60)) + ' min ago' : s < 172800 ? Math.round(s / 3600) + ' h ago' : Math.round(s / 86400) + ' d ago';
}
const icon = id => `<img loading="lazy" alt="" src="/icon?id=${encodeURIComponent(id)}" onerror="this.outerHTML='<span class=ph></span>'">`;
const itemCell = (r, sub) => `<div class="item">${icon(r.id)}<div style="min-width:0"><b>${esc(r.name)}</b>${sub ? `<span class="sub">${sub}</span>` : ''}</div></div>`;
// "29 Chaos for 9" (what you pay for how many) / "64 for 1 Divine" (how many you give for what).
const order = (o, buying) => buying
  ? `${o.currency_units} ${o.currency} <span class="dim">for</span> ${o.item_units}`
  : `${o.item_units} <span class="dim">for</span> ${o.currency_units} ${o.currency}`;
const KIND = { chaos: 'Chaos market', divine: 'Divine market', cross: 'Chaos → Divine' };

// A sortable table. cols: [{h, f(row)->html, s(row)->sortable value, l: left-aligned, title}]
function table(cols, rows, { sort = null, desc = true, onRow = null, limit = 200, empty = 'Nothing to show.' } = {}) {
  const wrap = document.createElement('div'); wrap.className = 'tw';
  let shown = limit;
  const render = () => {
    const data = sort == null ? rows : [...rows].sort((a, b) => {
      const x = cols[sort].s(a), y = cols[sort].s(b);
      if (x == null) return 1; if (y == null) return -1;
      return (typeof x === 'string' ? x.localeCompare(y) : x - y) * (desc ? -1 : 1);
    });
    if (!data.length) { wrap.innerHTML = `<div class="empty">${empty}</div>`; return; }
    wrap.innerHTML = `<table><thead><tr>${cols.map((c, i) => `<th class="${c.l ? 'l ' : ''}${c.s ? 'sort ' : ''}${i === sort ? 'sorted' : ''}" data-i="${i}" title="${esc(c.title || '')}">${c.h}${i === sort ? (desc ? ' ↓' : ' ↑') : ''}</th>`).join('')}</tr></thead>
      <tbody>${data.slice(0, shown).map((r, n) => `<tr class="${onRow ? 'click' : ''}" data-n="${n}">${cols.map(c => `<td class="${c.l ? 'l' : 'num'}">${c.f(r)}</td>`).join('')}</tr>`).join('')}</tbody></table>
      ${data.length > shown ? `<div class="empty"><button class="btn" data-more>Show ${Math.min(200, data.length - shown)} more (${data.length} in all)</button></div>` : ''}`;
    wrap.querySelectorAll('th.sort').forEach(th => th.onclick = () => { const i = +th.dataset.i; if (sort === i) desc = !desc; else { sort = i; desc = true; } render(); });
    if (onRow) wrap.querySelectorAll('tbody tr').forEach(tr => tr.onclick = e => { if (!e.target.closest('button,a,input')) onRow(data[+tr.dataset.n]); });
    const more = wrap.querySelector('[data-more]'); if (more) more.onclick = () => { shown += 200; render(); };
    wrap.dispatchEvent(new CustomEvent('rendered'));
  };
  render();
  return wrap;
}
const panel = (title, cls, right = '') => {
  const el = document.createElement('section'); el.className = 'panel ' + cls;
  el.innerHTML = `<header><h2>${title}</h2><span class="grow"></span>${right}</header>`;
  return el;
};

// --------------------------------------------------------------------- nav
const ICONS = {
  desk: '<path d="M3 12l9-8 9 8M5 10v10h14V10"/>', markets: '<path d="M4 19V5M4 19h16M8 15l3-4 3 2 5-7"/>',
  flips: '<path d="M4 8h13l-3-3M20 16H7l3 3"/>', arbitrage: '<path d="M12 3l8 5v8l-8 5-8-5V8zM12 3v18M4 8l16 8M20 8L4 16"/>',
  league: '<path d="M5 21V4M5 4h12l-2 4 2 4H5"/>', lab: '<path d="M9 3v6l-5 10a1 1 0 001 2h14a1 1 0 001-2L15 9V3M8 3h8M7 15h10"/>',
  ledger: '<path d="M6 3h12v18l-3-2-3 2-3-2-3 2zM9 8h6M9 12h6"/>',
};
const PAGES = [['desk', 'Desk'], ['markets', 'Markets'], ['flips', 'Flips'], ['arbitrage', 'Arbitrage'], ['league', 'League start'], ['lab', 'Backtest lab'], ['ledger', 'Ledger']];
function renderNav(page) {
  $('#nav').innerHTML = `<div class="brand"><img src="/mark.svg" alt="">Vaal Street</div>
    ${PAGES.map(([id, label]) => `<a href="#/${id}" class="${id === page ? 'on' : ''}"><svg viewBox="0 0 24 24">${ICONS[id]}</svg>${label}</a>`).join('')}
    <span class="spacer"></span>
    <div class="foot"><kbd>/</kbd> search &nbsp; <kbd>H</kbd> hideout<br><kbd>C</kbd> check an offer<br>Not affiliated with Grinding Gear Games.</div>`;
}
function renderTop() {
  const s = state.status, sync = s.sync, sm = state.summary;
  const leagues = (s.leagues || []).filter(l => l.kind !== 'variant' || l.league === state.league).slice(0, 40);
  $('#top').innerHTML = `
    <select id="leagueSel" title="League">${leagues.map(l => `<option ${l.league === state.league ? 'selected' : ''}>${esc(l.league)}</option>`).join('')}</select>
    ${sm ? `<div class="stat"><b>${fmt(sm.bridge)} c ${pct(sm.bridge_change_24h)}</b><span>Divine Orb</span></div>
    <div class="stat" title="${sm.league_day == null ? 'The archive does not reach back to this league’s first hour.' : ''}"><b>Day ${sm.league_day ?? '–'}</b><span>of league</span></div>
    <div class="stat"><b>${compact(sm.value_24h / sm.bridge)} d</b><span>traded, 24h</span></div>
    <div class="stat"><b>${ago(sm.as_of + 3600)}</b><span>latest hour</span></div>` : ''}
    <span class="grow"></span>
    <div class="sync" title="${esc(sync.error || '')}"><span class="dot ${sync.error ? 'err' : sync.running ? 'busy' : ''}"></span>${esc(sync.error ? 'Sync failed' : sync.message)}</div>
    <div class="seg" id="unitSeg" title="Show prices in Chaos or Divine"><button class="${state.unit === 'chaos' ? 'on' : ''}" data-u="chaos">Chaos</button><button class="${state.unit === 'divine' ? 'on' : ''}" data-u="divine">Divine</button></div>
    <button class="btn" id="hideoutBtn" title="Compact always-on-top view (H)">Hideout</button>`;
  $('#leagueSel').onchange = async e => { state.league = e.target.value; await api('/api/league', { league: state.league }); state.summary = null; route(); };
  $('#unitSeg').onclick = e => { const u = e.target.dataset.u; if (u) { state.unit = localStorage.unit = u; route(); } };
  $('#hideoutBtn').onclick = openHideout;
}

// -------------------------------------------------------------------- views
async function loadDesk() {
  const d = await api(withLeague('/api/desk'));
  state.summary = d.summary; state.bridge = d.summary.bridge; state.league = d.summary.league; state.market = d.market; state.events = d.events;
  return d;
}

async function viewDesk(main) {
  const [d, f, a, lg] = await Promise.all([loadDesk(), api(withLeague('/api/flips', { share: state.share, limit: 40, ...bankQuery() })), api(withLeague('/api/arbitrage', { share: state.share, ...bankQuery() })), api(withLeague('/api/league', { horizon: 7, unit: 'divine' }))]);
  renderTop();
  const s = d.summary, liquid = d.market.filter(r => r.value_24h > 20000 && r.active > 0.8 && r.change_24h != null);
  const movers = [...liquid].sort((x, y) => y.change_24h - x.change_24h);
  main.innerHTML = `<div class="head"><div><h1>${esc(s.league)} desk</h1><p>What is worth doing this hour, from the exchange's latest published prices.</p></div></div>
    <div class="kpis">
      <div class="kpi"><span>Divine Orb</span><b>${fmt(s.bridge)} c</b><i>${pct(s.bridge_change_24h)}</i></div>
      <div class="kpi"><span>League day</span><b>${s.league_day ?? '–'}</b></div>
      <div class="kpi"><span>Traded in 24h</span><b>${compact(s.value_24h / s.bridge)} d</b></div>
      <div class="kpi"><span>Items trading</span><b>${s.items}</b></div>
      <div class="kpi"><span>Latest hour</span><b>${ago(s.as_of + 3600)}</b></div>
    </div><div class="grid" id="g"></div>`;
  const g = $('#g', main);

  const flips = panel(+state.filters.capital > 0 ? `Best flips for ${fmt(+state.filters.capital)} c` : 'Best flips right now', 'c7', '<a href="#/flips">All flips →</a>');
  flips.append(table([
    { h: 'Item', l: 1, f: r => itemCell(r, `<span class="tag ${r.kind}">${KIND[r.kind]}</span>`) },
    { h: 'Buy order', f: r => order(r.buy_order, true), title: 'Currency you pay, for how many items' },
    { h: 'Sell order', f: r => order(r.sell_order, false), title: 'Items you give, for how much currency' },
    { h: 'Margin', f: r => pct(r.margin) },
    { h: 'Est. / hour', f: r => money(r.profit_per_hour) },
  ], f.flips.slice(0, 8), { onRow: r => openItem(r.id), empty: 'No market is liquid enough for a plan yet.' }));
  g.append(flips);

  const sets = panel(+state.filters.capital > 0 ? `Conversions within ${fmt(+state.filters.capital)} c` : 'Sets worth completing', 'c5', '<a href="#/arbitrage">All conversions →</a>');
  sets.append(table([
    { h: 'Set', l: 1, f: r => itemCell(r, `${r.need} → ${r.gives} ${esc(r.reward)}`) },
    { h: 'Cost', f: r => money(r.cost) }, { h: 'Worth', f: r => money(r.proceeds) }, { h: 'Margin', f: r => pct(r.margin) },
  ], a.conversions.filter(r => r.margin > 0.02 && (r.value_24h || 0) >= 2000 && (r.reward_value_24h ?? Infinity) >= 2000 && (!(+state.filters.capital > 0) || r.cost <= +state.filters.capital)).sort((x, y) => (+state.filters.capital > 0 ? y.margin - x.margin : y.profit_per_hour - x.profit_per_hour)).slice(0, 8), { onRow: r => openItem(r.id), empty: 'No set is currently cheaper than its reward.' }));
  g.append(sets);

  const mv = (title, rows) => {
    const p = panel(title, 'c4');
    p.append(table([{ h: 'Item', l: 1, f: r => itemCell(r) }, { h: 'Price', f: r => money(r.price) }, { h: '24h', f: r => pct(r.change_24h) }], rows, { onRow: r => openItem(r.id) }));
    return p;
  };
  g.append(mv('Rising (24h)', movers.slice(0, 7)), mv('Falling (24h)', movers.slice(-7).reverse()));

  const watched = d.market.filter(r => r.watched);
  const w = panel('Watchlist', 'c4');
  w.append(table([{ h: 'Item', l: 1, f: r => itemCell(r) }, { h: 'Price', f: r => money(r.price) }, { h: '24h', f: r => pct(r.change_24h) }], watched,
    { onRow: r => openItem(r.id), empty: 'Star an item in Markets to pin it here and in Hideout mode.' }));
  g.append(w);

  const pb = lg.playbook.items.filter(r => r.traded_now && (r.value_now || 0) >= 5000);
  if (pb.length && !lg.truncated) {
    const cols = [
      { h: 'Item', l: 1, f: r => itemCell(r) },
      { h: 'Typical move', f: r => pct(r.median, 0), title: 'Median over the next 7 days in past leagues, priced in Divine' },
      { h: 'Rose in', f: r => `${Math.round(r.up_share * r.n)} of ${r.n}` },
    ];
    const hint = `<a href="#/league">Day ${lg.day} playbook →</a>`;
    const up = panel('Past leagues: rose from here', 'c6', hint), down = panel('Past leagues: fell from here', 'c6', hint);
    up.append(table(cols, pb.filter(r => r.median > 0).slice(0, 6), { onRow: r => openItem(r.id) }));
    down.append(table(cols, pb.filter(r => r.median < 0).reverse().slice(0, 6), { onRow: r => openItem(r.id) }));
    g.append(up, down);
  }

  if (d.events.length) {
    const ev = panel('Alerts fired', 'c12');
    ev.innerHTML += `<div class="body">${d.events.slice(0, 8).map(e => `<div><b>${esc(e.title)}</b> <span class="muted">${esc(e.body)} · ${ago(e.t)}</span></div>`).join('')}</div>`;
    g.append(ev);
  }
}

async function viewMarkets(main) {
  const d = await loadDesk(); renderTop();
  const f = state.filters, cats = [...new Set(d.market.map(r => r.category))].sort();
  main.innerHTML = `<div class="head"><div><h1>Markets</h1><p>Every item that trades on the exchange. Price is the model's fair value; “vs Chaos” and “vs Divine” are what the two markets for the item are actually paying.</p></div><span class="grow"></span><button class="btn" id="tcBtn" title="Is an offer in the order window a good deal? (C)">Check an offer</button></div>
    <div class="bar">
      <label class="f">Search<input type="search" id="q" placeholder="Item name" value="${esc(f.q)}"></label>
      <label class="f">Category<select id="cat"><option value="">All</option>${cats.map(c => `<option ${c === f.category ? 'selected' : ''}>${esc(c)}</option>`).join('')}</select></label>
      <label class="f">Min. traded / 24h (c)<input type="number" id="minv" value="${f.minValue}" min="0" step="500"></label>
      <label class="f" style="flex-direction:row;align-items:center;gap:6px;padding-bottom:6px"><input type="checkbox" id="wonly" ${f.watched ? 'checked' : ''}> Watchlist only</label>
      <span class="muted small" id="count" style="padding-bottom:7px"></span>
    </div><section class="panel" id="tbl"></section>`;
  const draw = () => {
    const q = f.q.toLowerCase();
    const rows = d.market.filter(r => (!q || r.name.toLowerCase().includes(q)) && (!f.category || r.category === f.category) && (r.value_24h || 0) >= f.minValue && (!f.watched || r.watched));
    $('#count', main).textContent = `${rows.length} of ${d.market.length} items`;
    const t = table([
      { h: '', l: 1, f: r => `<button class="star ${r.watched ? 'on' : ''}" data-watch="${esc(r.id)}" title="Watch">★</button>` },
      { h: 'Item', l: 1, f: r => itemCell(r, esc(r.category)), s: r => r.name },
      { h: 'Price', f: r => money(r.price), s: r => r.price },
      { h: '24h', f: r => pct(r.change_24h), s: r => r.change_24h },
      { h: '7d', f: r => pct(r.change_7d), s: r => r.change_7d },
      { h: 'vs Chaos', f: r => money(r.chaos), s: r => r.chaos, title: 'Typical price in the item-for-Chaos market' },
      { h: 'vs Divine', f: r => money(r.divine), s: r => r.divine, title: 'Typical price in the item-for-Divine market, at the current Divine price' },
      { h: 'Divine premium', f: r => pct(r.premium), s: r => r.premium, title: 'How much more the Divine market pays than the Chaos market' },
      { h: 'Traded 24h', f: r => money(r.value_24h), s: r => r.value_24h },
      { h: 'Bids vs offers', f: r => r.imbalance == null ? '–' : `<span class="meter" title="${r.imbalance > 0 ? 'More Chaos bidding than items offered' : 'More items offered than Chaos bidding'}"><i style="left:${r.imbalance > 0 ? 50 : 50 + r.imbalance * 50}%;width:${Math.abs(r.imbalance) * 50}%;background:var(--${r.imbalance > 0 ? 'up' : 'down'})"></i></span>`, s: r => r.imbalance, title: 'Resting Chaos bids against resting item offers' },
      { h: '3 days', f: r => spark(r.spark, r.change_24h >= 0 ? '#5fd3a2' : '#f0787f') },
    ], rows, { sort: 8, onRow: r => openItem(r.id), empty: 'No item matches these filters.' });
    t.addEventListener('click', async e => {
      const b = e.target.closest('[data-watch]'); if (!b) return;
      const row = d.market.find(r => r.id === b.dataset.watch); row.watched = !row.watched; b.classList.toggle('on', row.watched);
      await api('/api/watch', { league: state.league, item: row.id, on: row.watched });
    });
    $('#tbl', main).replaceChildren(t);
  };
  $('#tcBtn', main).onclick = () => openCheck();
  $('#q', main).oninput = e => { f.q = e.target.value; draw(); };
  $('#cat', main).onchange = e => { f.category = e.target.value; draw(); };
  $('#minv', main).onchange = e => { f.minValue = +e.target.value || 0; draw(); };
  $('#wonly', main).onchange = e => { f.watched = e.target.checked; draw(); };
  draw();
}

// Where each Gold fee came from, in words: the fee schedule is not published, so it is learned or assumed.
const feeSource = r => (r.gold_sources || []).map(x => x.source === 'your trades' ? `from your trades (${x.trades})` : x.source === 'your assumption' ? 'your assumed fee' : 'unknown').filter((v, i, a) => a.indexOf(v) === i).join(', ');
const goldCell = (r, value) => r.gold == null
  ? `<span class="dim" title="No Gold fee known for ${esc((r.gold_sources || []).filter(x => x.source === 'unknown').map(x => x.id).join(', '))}. Record the Gold of a trade in the Ledger, or enter an assumed fee on the Flips page.">Gold unknown</span>`
  : `${value}<div class="muted small">${feeSource(r)}</div>`;
const flipCols = () => [
  { h: 'Item', l: 1, f: r => itemCell(r, `<span class="tag ${r.kind}">${KIND[r.kind]}</span>`), s: r => r.name },
  { h: 'Buy order', f: r => order(r.buy_order, true), s: r => r.buy, title: 'Currency you pay, for how many items' },
  { h: 'Sell order', f: r => order(r.sell_order, false), s: r => r.sell, title: 'Items you give, for how much currency' },
  { h: 'Margin', f: r => pct(r.margin), s: r => r.margin },
  { h: 'Volume beyond', f: r => plainPct(r.reach), s: r => r.reach, title: 'Share of the last two days’ volume that traded in hours averaging at or beyond each order level' },
  { h: 'Units / hour', f: r => fmt(r.units_per_hour), s: r => r.units_per_hour },
  { h: 'Est. / hour', f: r => money(r.profit_per_hour), s: r => r.profit_per_hour },
  { h: 'Last 7 days', f: r => r.replay ? `${pct(r.replay.return, 0)} <span class="dim">· ${r.replay.sales} sale${r.replay.sales === 1 ? '' : 's'}</span>` : '<span class="dim">no fills</span>', s: r => r.replay?.return, title: 'This plan’s rule replayed over the last 7 days on a fixed stake, re-posting orders every 4 hours: profit as a share of the stake (inventory still held is valued at fair value), and the number of hours with a sale' },
  { h: 'Capital', f: r => money(r.capital), s: r => r.capital, title: 'Roughly six hours of buying held as inventory' },
  { h: 'Bid queue', f: r => dur(r.bid_queue_hours), s: r => r.bid_queue_hours, title: 'Resting bids already in the buy market, in hours of that market’s volume' },
  { h: 'Offer queue', f: r => dur(r.ask_queue_hours), s: r => r.ask_queue_hours, title: 'Items already on offer in the sell market, in hours of that market’s volume' },
  { h: 'Per 1,000 Gold', f: r => goldCell(r, money(r.chaos_per_1000_gold)), s: r => r.chaos_per_1000_gold, title: 'Chaos earned for every 1,000 Gold the orders cost. Each order costs Gold for every item it buys or sells' },
  { h: 'Gold / hour', f: r => goldCell(r, fmt(r.gold_per_hour)), s: r => r.gold_per_hour, title: 'Gold the orders cost at the units per hour this plan expects' },
  { h: 'Minutes', f: r => `${fmt(r.minutes)} <span class="dim">· ${r.orders} orders</span>`, s: r => r.minutes, title: 'Your time to post this plan’s orders once, at the minutes per order you set' },
  { h: 'Per hour of your time', f: r => money(r.profit_per_own_hour), s: r => r.profit_per_own_hour, title: 'Est. / hour × 60 ÷ minutes: if you post one buy and one sell order for each hour the market runs' },
];
const RANK = { profit: 6, gold: 11, own: 14 };

async function viewFlips(main) {
  const f = state.filters;
  if (!state.summary) await loadDesk();
  const d = await api(withLeague('/api/flips', { share: state.share, min_margin: f.minMargin, kind: f.kind, ...bankQuery(), ...(f.goldBudget > 0 ? { gold_budget: +f.goldBudget } : {}) }));
  state.bridge = d.summary.bridge; renderTop();
  const st = d.settings;
  main.innerHTML = `<div class="head"><div><h1>Flips</h1><p>Buy low in one market, sell high in the same or the other. Ranked by estimated profit per hour.</p></div></div>
    <p class="note">Each plan rests a buy order at a price the market's <b>hourly average</b> has dipped to, and a sell order at a price it has risen to — levels a stated share of the last two days' volume actually traded beyond. “Chaos → Divine” buys from people dumping loot for Chaos and sells to people paying Divines, who consistently pay more. Estimates assume you capture ${plainPct(state.share)} of the market's flow and ignore who is ahead of you in the queue; check the in-game book before sizing up.</p>
    <p class="note">Every order costs Gold, and the game does not publish how much. The desk uses the Gold per item from the trades you recorded in the Ledger; for other items it uses the fee you assume below, and otherwise says “Gold unknown”. Minutes assume ${fmt(st.minutes_per_order)} minute${st.minutes_per_order === 1 ? '' : 's'} per order — your guess, change it below.</p>
    <div class="bar">
      <div class="seg" id="kind">${['all', 'chaos', 'divine', 'cross'].map(k => `<button data-k="${k}" class="${f.kind === k ? 'on' : ''}">${k === 'all' ? 'All plans' : KIND[k]}</button>`).join('')}</div>
      <label class="f">Your share of the flow: <span id="shareL">${plainPct(state.share)}</span><input type="range" id="share" min="0.01" max="0.3" step="0.01" value="${state.share}"></label>
      <label class="f">Min. margin (%)<input type="number" id="mm" value="${f.minMargin * 100}" min="0" step="1"></label>
      <label class="f">My bankroll (c)<input type="number" id="cap" value="${f.capital}" placeholder="e.g. 5000" min="0" step="1000"></label>
      <label class="f" title="Gold is paid when you post an order. With a budget, the session plan picks the plans that earn most per Gold until posting their orders once would spend it">Gold to spend this session<input type="number" id="gb" value="${f.goldBudget || ''}" placeholder="optional" min="0" step="1000"></label>
      <label class="f" title="Gold per item bought or sold, for items you have no recorded trade for. Leave empty to say “unknown”">Assumed fee (Gold / item)<input type="number" id="fee" value="${st.assumed_gold_fee ?? ''}" placeholder="unknown" min="0" step="any"></label>
      <label class="f">Minutes per order<input type="number" id="mpo" value="${st.minutes_per_order}" min="0" step="0.5"></label>
      <label class="f">Rank by<select id="rank">${[['profit', 'Est. / hour'], ['gold', 'Chaos per 1,000 Gold'], ['own', 'Per hour of your time']].map(([k, l]) => `<option value="${k}" ${f.rankBy === k ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
      <label class="f">Search<input type="search" id="q" value="${esc(f.q)}" placeholder="Item name"></label>
    </div><div id="plan"></div><section class="panel" id="tbl"></section>`;
  const draw = () => {
    const q = f.q.toLowerCase();
    const rows = d.flips.filter(r => (f.kind === 'all' || r.kind === f.kind) && (!q || r.name.toLowerCase().includes(q)));
    $('#tbl', main).replaceChildren(table(flipCols(), rows, { sort: RANK[f.rankBy] ?? 6, onRow: r => openItem(r.id), empty: 'No plan clears these filters.' }));
    // The session plan is built in Python (desk.session_plan) from the same plans, before the search filter.
    const bank = +f.capital, host = $('#plan', main), sp = d.session;
    host.replaceChildren();
    if (!(bank > 0) || !sp) return;
    const picks = sp.flips, total = sp.expected_per_hour;
    // Kept so the Ledger can start a session against this plan.
    state.sessionPlan = { league: state.league, bankroll: bank, expected_per_hour: total, flips: picks.map(r => ({ id: r.id, name: r.name, kind: r.kind, stake: r.stake, est: r.est })) };
    const p = panel(`Session plan for ${fmt(bank)} c`, '', `<span class="muted small">about ${money(total)} an hour if every order fills as estimated · ${money(sp.deployed)} deployed${sp.gold_budget != null ? ` · ${fmt(sp.gold_used)} of ${fmt(sp.gold_budget)} Gold used` : ''} · about ${fmt(sp.minutes)} min to post</span>`);
    p.style.marginBottom = '14px';
    p.append(table([
      { h: 'Item', l: 1, f: r => itemCell(r, `<span class="tag ${r.kind}">${KIND[r.kind]}</span>`) },
      { h: 'Buy order', f: r => order(r.buy_order, true) }, { h: 'Sell order', f: r => order(r.sell_order, false) },
      { h: 'Margin', f: r => pct(r.margin) }, { h: 'Stake', f: r => money(r.stake), title: 'Chaos to keep working in this item: a quarter of the bankroll, or one whole lot if that is more' },
      { h: 'Smallest lot', f: r => money(r.lot_cost), title: 'What it takes to post the buy order and to hold enough for one sell order' },
      { h: 'Est. / hour', f: r => money(r.est) },
      { h: 'Per 1,000 Gold', f: r => r.chaos_per_1000_gold == null ? '<span class="dim">Gold unknown</span>' : `${money(r.chaos_per_1000_gold)}<div class="muted small">${feeSource(r)}</div>` },
      { h: 'Gold to post', f: r => r.gold_to_post == null ? '–' : fmt(r.gold_to_post), title: 'Gold to post this plan’s buy and sell orders once, at the size of its stake' },
      { h: 'Gold / hour', f: r => r.gold_per_hour == null ? '–' : fmt(r.gold_per_hour), title: 'Gold an hour at the fills this stake expects, if you keep re-posting' },
      { h: 'Minutes', f: r => fmt(r.minutes) },
      { h: 'Last 7 days', f: r => r.replay ? pct(r.replay.return, 0) : '<span class="dim">no fills</span>' },
    ], picks, { onRow: r => openItem(r.id), empty: sp.gold_budget != null ? 'No plan with a known Gold fee fits this bankroll and Gold budget.' : 'No plan fits this bankroll with the current filters.' }));
    if (sp.gold_unknown.length) p.insertAdjacentHTML('beforeend', `<div class="body"><p class="note"><b>Gold unknown</b> — left out of the plan because the desk does not know what their orders cost in Gold: ${sp.gold_unknown.map(r => esc(r.name)).join(', ')}. Record a trade's Gold in the Ledger or enter an assumed fee to include them.</p></div>`);
    host.append(p);
  };
  // The session plan is built on the server for one kind of plan, so a new kind asks again.
  $('#kind', main).onclick = e => { const k = e.target.dataset.k; if (k) { f.kind = k; route(); } };
  $('#gb', main).onchange = e => { f.goldBudget = localStorage.goldBudget = e.target.value; route(); };
  $('#rank', main).onchange = e => { f.rankBy = e.target.value; draw(); };
  const setting = (key, value) => api('/api/settings', { [key]: value }).then(route, err => toast(err.message, true));
  $('#fee', main).onchange = e => setting('assumed_gold_fee', e.target.value);
  $('#mpo', main).onchange = e => setting('minutes_per_order', e.target.value || 1);
  $('#share', main).oninput = e => { $('#shareL', main).textContent = plainPct(+e.target.value); };
  $('#share', main).onchange = e => { state.share = +e.target.value; localStorage.share = state.share; route(); };
  $('#mm', main).onchange = e => { f.minMargin = (+e.target.value || 0) / 100; route(); };
  $('#cap', main).onchange = e => { f.capital = localStorage.bankroll = e.target.value; route(); };
  $('#q', main).oninput = e => { f.q = e.target.value; draw(); };
  draw();
}

const HOW = { set: 'Card set', vendor: 'Vendor 3-for-1', stack: 'Full stack', yours: 'Your recipe' };
// How a round trip moves from one item to the next.
const VIA = {
  swap: ['', 'Swap in the item-for-item market'],
  divine: ['Divine', 'Sell for Divines, or buy with them, in the Divine market'],
  vendor: ['vendor', 'Buy from a vendor at the price you entered'],
};
const viaArrow = how => { const [word, title] = VIA[how] || VIA.swap; return ` <span class="dim" title="${title}">→${word ? `<span class="tag" style="margin-left:3px">${word}</span>` : ''}</span> `; };
// Whether a recipe or round trip worked when you tried it in game, with buttons to say so.
const CHECKED = { confirmed: '<span class="up">Worked</span>', failed: '<span class="down">Did not work</span>', unchecked: '<span class="dim">Not tried</span>' };
const checkCell = r => `<span title="${esc(r.check_note || (r.check === 'unchecked' ? 'Nobody has marked this as tried in game yet' : ''))}">${CHECKED[r.check] || CHECKED.unchecked}${r.check_note ? ' <span class="dim">✎</span>' : ''}</span>
  <button class="btn ghost" data-ck="${esc(r.recipe_key)}" data-st="confirmed" title="It worked in game">✓</button><button class="btn ghost" data-ck="${esc(r.recipe_key)}" data-st="failed" title="It did not work in game">✗</button>`;
// One click handler per panel, because the tables re-render on sort.
const onCheck = host => host.addEventListener('click', async e => {
  const b = e.target.closest('[data-ck]'); if (!b) return;
  const note = prompt(b.dataset.st === 'confirmed' ? 'It worked. A note? (optional)' : 'It did not work. What went wrong? (optional)', '');
  if (note === null) return;
  try { await api('/api/recipe_check', { key: b.dataset.ck, status: b.dataset.st, note }); route(); } catch (err) { toast(err.message, true); }
});
const fixedText = r => `${r.inputs.map(x => `${+x.units} ${esc(x.name)}`).join(' + ')} ${r.kind === 'vendor' ? '<span class="dim">buys</span>' : '<span class="dim">→</span>'} ${+r.gives} ${esc(r.output_name)}`;

async function viewArbitrage(main) {
  if (!state.summary) await loadDesk();
  const f = state.filters, purse = +f.capital > 0 ? +f.capital : 0;
  f.swaps ??= +(localStorage.swaps || 3);
  const d = await api(withLeague('/api/arbitrage', { share: state.share, swaps: f.swaps, ...bankQuery() }));
  state.bridge = d.summary.bridge; renderTop();
  const names = [...new Set(['Chaos Orb', ...state.market.map(r => r.name)])];
  main.innerHTML = `<div class="head"><div><h1>Arbitrage</h1><p>Prices that disagree with each other: things that turn into something dearer, and round trips through item-for-item markets.</p></div></div>
    <div class="bar">
      <label class="f">My bankroll (c)<input type="number" id="cap" value="${f.capital}" placeholder="e.g. 40" min="0" step="10"></label>
      <label class="f" style="flex-direction:row;align-items:center;gap:6px;padding-bottom:6px"><input type="checkbox" id="fit" ${f.fit !== false ? 'checked' : ''} ${purse ? '' : 'disabled'}> Only what my bankroll can start</label>
      <label class="f">Min. traded / 24h (c)<input type="number" id="liq" value="${f.arbLiquidity ?? 2000}" min="0" step="500"></label>
      <div class="f">Swaps per round trip<div class="seg" id="swaps">${[1, 2, 3].map(n => `<button type="button" data-n="${n}" class="${f.swaps === n ? 'on' : ''}" title="Round trips with up to ${n} swap${n === 1 ? '' : 's'}">up to ${n}</button>`).join('')}</div></div>
    </div><div class="grid" id="g"></div><datalist id="arbItems">${names.map(n => `<option value="${esc(n)}">`).join('')}</datalist>`;
  const fits = cost => !purse || f.fit === false || cost <= purse;
  const liquid = f.arbLiquidity ?? 2000;
  const sets = panel('Conversions', 'c12');
  sets.innerHTML += `<div class="body" style="padding-bottom:0"><p class="note">Fixed recipes: a full set of divination cards for its reward, a full stack of shards or splinters for the item they form, and three essences or oils of one tier sold to a vendor for one of the next (shown for up to three tiers at once), plus the recipes you entered below. “Margin” values both sides at fair value — what a patient trader gets. “In a hurry” buys the inputs in the dearer market and sells the result in the cheaper one. Try a recipe once in game before buying in bulk, then mark it with ✓ or ✗.</p></div>`;
  sets.append(table([
    { h: 'Turn in', l: 1, f: r => itemCell(r, `${r.need} → ${r.gives} ${esc(r.reward)}`), s: r => r.name },
    { h: 'How', l: 1, f: r => `<span class="tag">${HOW[r.how] || r.how}${r.steps > 1 ? ` × ${r.steps}` : ''}</span>`, s: r => r.how, title: 'A number after × means the recipe is applied that many tiers in a row' },
    { h: 'Cost', f: r => money(r.cost), s: r => r.cost, title: 'Fair value of everything you need to buy for one conversion' },
    { h: 'Worth', f: r => money(r.proceeds), s: r => r.proceeds },
    { h: 'Margin', f: r => pct(r.margin), s: r => r.margin },
    { h: 'In a hurry', f: r => pct(r.margin_now), s: r => r.margin_now },
    { h: 'Est. / hour', f: r => money(r.profit_per_hour), s: r => r.profit_per_hour, title: `At ${plainPct(state.share)} of the input's traded volume` },
    { h: 'Input traded 24h', f: r => money(r.value_24h), s: r => r.value_24h },
    { h: 'Result traded 24h', f: r => money(r.reward_value_24h), s: r => r.reward_value_24h, title: 'How much of the result changes hands: can you sell it?' },
    { h: 'Gold', f: r => goldCell(r, fmt(r.gold)), s: r => r.gold, title: 'Gold to buy the inputs for one conversion and sell the result' },
    { h: 'Per 1,000 Gold', f: r => goldCell(r, money(r.chaos_per_1000_gold)), s: r => r.chaos_per_1000_gold, title: 'Chaos earned for every 1,000 Gold the orders cost' },
    { h: 'In game', l: 1, f: checkCell, s: r => r.check, title: 'Whether you marked this as working when you tried it in game' },
  ], d.conversions.filter(r => fits(r.cost) && (r.value_24h || 0) >= liquid && (r.reward_value_24h ?? Infinity) >= liquid),
  { sort: purse ? 4 : 6, limit: 15, onRow: r => openItem(r.id), empty: 'No conversion clears these filters.' }));
  onCheck(sets);
  const loops = panel('Round trips through item-for-item markets', 'c12');
  loops.innerHTML += `<div class="body" style="padding-bottom:0"><p class="note">Buy the first item${d.chaos_only ? ' for Chaos' : ''}, move it along the path, and sell the last one${d.chaos_only ? ' for Chaos' : ''}. A plain arrow is a swap in the small markets where items trade directly for each other; <span class="tag">Divine</span> means selling for Divines and buying with them; <span class="tag">vendor</span> means buying from a vendor at a price you entered below. Rates are what those markets typically traded at over the last day. These markets are thin. “Waiting now” is the value of orders already resting on the other side of the thinnest swap — when it is not nothing, the swap can be taken at once; otherwise you would have to post an order and wait. “Est. / day” assumes you take ${plainPct(state.share)} of what went through the thinnest step.</p></div>`;
  loops.append(table([
    { h: 'Path', l: 1, f: r => r.path.map((x, k) => `${k ? viaArrow((r.via || [])[k - 1]) : ''}<span style="white-space:nowrap">${icon(x.id).replace('<img', '<img style="width:18px;height:18px;vertical-align:middle"')} ${esc(x.name)}</span>`).join('') },
    { h: 'First swap', f: r => `${r.first_swap.give} <span class="dim">for</span> ${r.first_swap.get}`, title: 'Units of the first item you give, for units of the second' },
    { h: 'Buy at', f: r => money(r.buy), s: r => r.buy }, { h: 'Sell at', f: r => money(r.sell), s: r => r.sell },
    { h: 'Edge', f: r => r.edge > 3 ? `<span class="up">×${(1 + r.edge).toFixed(1)}</span>` : pct(r.edge, 0), s: r => r.edge },
    { h: 'Est. / day', f: r => money(r.profit_per_day), s: r => r.profit_per_day },
    { h: 'Waiting now', f: r => r.resting_value > 0 ? money(r.resting_value) : '<span class="dim">nothing</span>', s: r => r.resting_value, title: 'Value of orders resting on the other side of the thinnest swap in the latest hour: what you could take immediately' },
    { h: 'Went through 24h', f: r => money(r.value_24h), s: r => r.value_24h, title: 'Value traded in the thinnest step of the path' },
    { h: 'Hours active', f: r => r.hours + ' / 24', s: r => r.hours },
    { h: 'Gold', f: r => goldCell(r, fmt(r.gold)), s: r => r.gold, title: 'Gold for one trip per unit of the first item: buying it, each swap, and selling the last item. The game does not say what a swap costs; the desk assumes the fee of the item you give, as if you sold it' },
    { h: 'Per 1,000 Gold', f: r => goldCell(r, money(r.chaos_per_1000_gold)), s: r => r.chaos_per_1000_gold, title: 'Chaos earned for every 1,000 Gold the trip costs' },
    { h: 'In game', l: 1, f: checkCell, s: r => r.check, title: 'Whether you marked this round trip as working when you tried it in game' },
  ], d.loops.filter(r => fits(r.lot_cost)), { sort: 5, limit: 15, empty: 'No round trip traded steadily in the last day.' }));
  onCheck(loops);

  const mine = panel('Your recipes and vendor prices', 'c12', '<span class="muted small">Kept for every league. The desk prices recipes like the built-in ones and uses vendor prices as steps in round trips.</span>');
  const form = document.createElement('form'); form.className = 'body'; form.autocomplete = 'off';
  let kind = 'recipe';
  const itemInput = (cls, ph) => `<input type="text" class="${cls}" list="arbItems" placeholder="${ph}" style="width:200px">`;
  const inputRow = () => `<div class="bar" style="margin:0 0 6px" data-in><label class="f">Count<input type="number" class="n" min="0" step="any" value="1" style="width:70px"></label><label class="f">Item${itemInput('it', 'Start typing…')}</label></div>`;
  const drawForm = () => {
    form.innerHTML = `<div class="bar" style="margin-bottom:8px"><div class="seg" id="fk"><button type="button" data-k="recipe" class="${kind === 'recipe' ? 'on' : ''}">Recipe</button><button type="button" data-k="vendor" class="${kind === 'vendor' ? 'on' : ''}">Vendor price</button></div>
      <span class="muted small">${kind === 'recipe' ? 'What goes in, and what comes out. Any item the exchange knows.' : 'A vendor sells an item for a count of one currency, for example 1 Some Shard for 3 Chaos Orb.'}</span></div>
      <div class="muted small" style="margin-bottom:4px">${kind === 'recipe' ? 'Goes in' : 'The vendor asks'}</div><div id="ins">${inputRow()}</div>
      ${kind === 'recipe' ? '<button type="button" class="btn ghost" id="more" style="margin-bottom:8px">+ another input</button>' : ''}
      <div class="bar" style="margin:0"><label class="f">${kind === 'recipe' ? 'Comes out' : 'and gives'}<span style="display:flex;gap:6px"><input type="number" id="gv" min="0" step="any" value="1" style="width:70px">${itemInput('', 'Start typing…').replace('class=""', 'id="out"')}</span></label>
      <label class="f">Note<input type="text" id="fn" placeholder="optional" style="width:200px"></label>
      <button class="btn primary">Add</button></div>`;
    $('#fk', form).onclick = e => { const k = e.target.dataset.k; if (k) { kind = k; drawForm(); } };
    const more = $('#more', form); if (more) more.onclick = () => $('#ins', form).insertAdjacentHTML('beforeend', inputRow());
  };
  drawForm();
  form.onsubmit = async e => {
    e.preventDefault();
    const inputs = [...form.querySelectorAll('[data-in]')].map(r => [$('.it', r).value.trim(), +$('.n', r).value]).filter(([item]) => item);
    if (!inputs.length || !$('#out', form).value.trim()) return toast('Name what goes in and what comes out.', true);
    try { await api('/api/recipe', { kind, inputs, output: $('#out', form).value.trim(), gives: +$('#gv', form).value || 1, note: $('#fn', form).value }); toast('Added. It starts as not tried in game.'); route(); }
    catch (err) { toast(err.message, true); }
  };
  const ft = table([
    { h: 'Kind', l: 1, f: r => `<span class="tag">${r.kind === 'vendor' ? 'Vendor price' : 'Recipe'}</span>` },
    { h: 'What', l: 1, f: fixedText },
    { h: 'Note', l: 1, f: r => esc(r.note) },
    { h: 'In game', l: 1, f: checkCell },
    { h: '', f: r => `<button class="btn ghost" data-delfix="${r.id}" title="Delete this entry">✕</button>` },
  ], d.fixed, { empty: 'None yet. The desk cannot read recipes or vendor prices from the game, so add the ones you know.' });
  ft.addEventListener('click', async e => {
    const b = e.target.closest('[data-delfix]'); if (!b) return;
    try { await api('/api/recipe', { delete: +b.dataset.delfix }); route(); } catch (err) { toast(err.message, true); }
  });
  onCheck(ft);
  mine.append(form, ft);
  $('#g', main).append(sets, loops, mine);
  $('#cap', main).onchange = e => { f.capital = localStorage.bankroll = e.target.value; route(); };
  $('#fit', main).onchange = e => { f.fit = e.target.checked; route(); };
  $('#liq', main).onchange = e => { f.arbLiquidity = +e.target.value || 0; route(); };
  $('#swaps', main).onclick = e => { const n = +e.target.dataset.n; if (n) { f.swaps = n; localStorage.swaps = n; route(); } };
}

const unitOf = f => f(1).trim().slice(-1);
function leagueChart(host, curves, current, key, yFormat) {
  const names = Object.keys(curves).sort((a, b) => (a === current) - (b === current));
  let n = 0;
  const series = names.map(name => ({
    label: name, x: curves[name].map(p => p.day), y: curves[name].map(p => p[key]),
    color: name === current ? '#b2ef78' : PALETTE[n++ % PALETTE.length], width: name === current ? 2.4 : 1.3, alpha: name === current ? 1 : .8,
  }));
  drawChart(host, { series, xType: 'day', yFormat, tickFormat: tick(unitOf(yFormat)), log: true });
  return `<div class="legend">${series.slice().reverse().map(s => `<span><i style="background:${s.color}"></i>${esc(s.label)}</span>`).join('')}</div>`;
}

async function viewLeague(main) {
  const v = state.league_view;
  const d = await api(withLeague('/api/league', { horizon: v.horizon, unit: v.unit }));
  if (!state.summary) await loadDesk();
  renderTop();
  const past = d.precedents, unitWord = v.unit === 'divine' ? 'Divine' : 'Chaos';
  const st0 = state.status, shallow = st0.first_hour > st0.archive_start + 86400;
  main.innerHTML = `<div class="head"><div><h1>League start</h1><p>${d.truncated ? `The archive does not reach back to the start of ${esc(d.league)}, so its league days cannot be counted yet.` : `${esc(d.league)} is on day ${d.day}.`} ${past.length ? `Lined up against the same day of ${past.map(esc).join(', ')}.` : 'No earlier league in the archive yet.'}</p></div></div>
    ${shallow ? `<p class="note">History on disk starts ${new Date(st0.first_hour * 1000).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })}. The exchange has published every hour since July 2024; the full archive is about 1.5 GB and takes one to two hours to download, in the background, newest first. <button class="btn" id="bf" ${st0.sync.backfill ? 'disabled' : ''}>${st0.sync.backfill ? 'Downloading…' : 'Download full history'}</button></p>` : ''}
    <div class="grid" id="g"></div>`;
  const g = $('#g', main);
  if ($('#bf', main)) $('#bf', main).onclick = async e => { e.target.disabled = true; e.target.textContent = 'Downloading…'; await api('/api/sync', { backfill: true }); toast('Downloading the full history in the background. Pages refresh as leagues arrive.'); };
  const dv = panel('Divine Orb in Chaos, by league day', 'c12');
  const outlook = d.truncated ? '' : (d.divine_outlook || []).map(o => `<span style="margin-right:18px">next ${o.horizon} days: <b class="num">${pct(o.median, 0)}</b> <span class="dim">(${(o.worst * 100).toFixed(0)}% to ${o.best > 0 ? '+' : ''}${(o.best * 100).toFixed(0)}%, ${o.n} league${o.n === 1 ? '' : 's'})</span></span>`).join('');
  dv.innerHTML += `<div class="body">${outlook ? `<div class="muted" style="margin-bottom:10px">From day ${d.day} in past leagues, a Divine went on to cost — ${outlook}</div>` : ''}<div id="lg"></div><div id="dvc"></div></div>`;
  g.append(dv);
  $('#lg', dv).innerHTML = leagueChart($('#dvc', dv), d.divine, d.league, 'price', x => fmt(x) + ' c');

  const moneyU = x => x == null ? '–' : v.unit === 'divine' ? `${fmt(x)}<span class="dim"> d</span>` : `${fmt(x)}<span class="dim"> c</span>`;
  const controls = `<div class="seg" id="hz">${[3, 7, 14, 30].map(h => `<button data-h="${h}" class="${v.horizon === h ? 'on' : ''}">${h} days</button>`).join('')}</div>
    <div class="seg" id="lu" style="margin-left:8px"><button data-u="divine" class="${v.unit === 'divine' ? 'on' : ''}">in Divine</button><button data-u="chaos" class="${v.unit === 'chaos' ? 'on' : ''}">in Chaos</button></div>`;
  const pbCols = [
    { h: 'Item', l: 1, f: r => itemCell(r, esc(r.category)), s: r => r.name },
    { h: 'Typical move', f: r => pct(r.median, 0), s: r => r.median, title: 'Median across past leagues' },
    { h: 'Each league', l: 1, f: r => r.by_league.map(m => `<span class="chip ${m.move > 0 ? 'up' : 'down'}" title="${esc(m.league)}">${m.move > 0 ? '+' : ''}${(m.move * 100).toFixed(0)}%</span>`).join('') },
    { h: 'Price now', f: r => moneyU(r.price_now), s: r => r.price_now },
  ];
  const traded = d.playbook.items.filter(r => r.traded_now && (r.value_now || 0) >= 2000);
  const up = panel(`Rose over the next ${v.horizon} days`, 'c6', controls), down = panel(`Fell over the next ${v.horizon} days`, 'c6');
  const note = `<div class="body" style="padding-bottom:0"><p class="note">What happened in past leagues to items bought on day ${d.playbook.day}, priced in ${unitWord}. ${past.length < 3 ? `Only ${past.length} earlier league${past.length === 1 ? '' : 's'} to learn from — read these as anecdotes, not odds.` : 'Items that moved the same way in every league rank first.'}</p></div>`;
  up.innerHTML += note; down.innerHTML += note;
  up.append(table(pbCols, traded.filter(r => r.median > 0), { onRow: r => openItem(r.id), limit: 25, empty: 'No earlier league traded these items on this day.' }));
  down.append(table(pbCols, traded.filter(r => r.median < 0).reverse(), { onRow: r => openItem(r.id), limit: 25, empty: 'No earlier league traded these items on this day.' }));
  g.append(up, down);

  const st = d.standing.items.filter(r => (r.value_now || 0) >= 5000);
  const stCols = [
    { h: 'Item', l: 1, f: r => itemCell(r, esc(r.category)), s: r => r.name },
    { h: 'Now', f: r => moneyU(r.price_now), s: r => r.price_now },
    { h: `Typical on day ${d.day}`, f: r => moneyU(r.typical), s: r => r.typical },
    { h: 'vs typical', f: r => pct(r.vs_typical, 0), s: r => r.vs_typical },
    { h: 'Leagues', f: r => r.n, s: r => r.n },
  ];
  const cheap = panel('Cheaper than usual for this day', 'c6'), rich = panel('Dearer than usual for this day', 'c6');
  cheap.append(table(stCols, st.filter(r => r.vs_typical < 0), { onRow: r => openItem(r.id), limit: 20, empty: 'No earlier league to compare with yet.' }));
  rich.append(table(stCols, st.filter(r => r.vs_typical > 0).reverse(), { onRow: r => openItem(r.id), limit: 20, empty: 'No earlier league to compare with yet.' }));
  g.append(cheap, rich);
  $('#hz', main).onclick = e => { const h = +e.target.dataset.h; if (h) { v.horizon = h; route(); } };
  $('#lu', main).onclick = e => { const u = e.target.dataset.u; if (u) { v.unit = u; route(); } };
}

const STRATS = {
  swing: { label: 'Swing maker', text: 'Rest buys where the Chaos market dips, offer the inventory where the sell market rises. This is the Flips board, replayed.', params: { reach: [0.25, 'Share of hours each order level is reached'], top: [40, 'Most-traded items to quote'], sell_leg: ['b', 'Sell into', { b: 'the Divine market', r: 'the Chaos market' }], min_margin: [0.03, 'Skip plans below this margin'], per_item: [0.05, 'Max share of bankroll per item'], stop_days: [2, 'Dump inventory unsold after this many days'] } },
  hold: { label: 'Buy and hold', text: 'Buy an equal-weight basket of the most traded items after the first day and hold. The benchmark.', params: { top: [20, 'Items in the basket'] } },
  reversion: { label: 'Dip buyer (taker)', text: 'Pay the spread to buy prints far below the model, sell when they normalise.', params: { entry_z: [-2, 'Buy below this surprise score'], exit_z: [0, 'Sell above this score'], top: [40, 'Most-traded items'], per_item: [0.05, 'Max share of bankroll per item'] } },
  momentum: { label: 'Momentum (taker)', text: 'Once a day, hold the liquid items that rose most.', params: { lookback: [72, 'Hours of history ranked'], hold: [10, 'Items held'], top: [60, 'Most-traded items considered'] } },
};
async function viewLab(main) {
  if (!state.summary) await loadDesk();
  renderTop();
  const leagues = state.status.leagues.filter(l => l.kind === 'challenge' && l.hours >= 72);
  const lab = state.lab ||= { strategy: 'swing', leagues: [state.league], capital: 10000, participation: 0.1, slippage: 0.03, attention: 4, start_day: 0, days: '', params: {}, results: [] };
  const sdef = STRATS[lab.strategy];
  main.innerHTML = `<div class="head"><div><h1>Backtest lab</h1><p>Replay a strategy hour by hour over past leagues. Orders posted at hour t meet hour t+1's market; nothing sees the future.</p></div></div>
    <p class="note">A resting order fills at its limit only in hours whose average price traded through it, for at most the chosen share of that hour's volume; an aggressive order pays the average plus slippage. Results are sized on a fixed bankroll, in Chaos. No Gold fees and no queue priority are modelled, so treat maker results as a ceiling.</p>
    <div class="grid"><section class="panel c4"><header><h2>Setup</h2></header><div class="body" id="form" style="display:grid;gap:10px">
      <label class="f">Strategy<select id="st">${Object.entries(STRATS).map(([k, s]) => `<option value="${k}" ${k === lab.strategy ? 'selected' : ''}>${s.label}</option>`).join('')}</select></label>
      <div class="muted small">${sdef.text}</div>
      ${Object.entries(sdef.params).map(([k, [def, text, options]]) => options
        ? `<label class="f">${text}<select data-p="${k}">${Object.entries(options).map(([v, l]) => `<option value="${v}" ${(lab.params[k] ?? def) === v ? 'selected' : ''}>${l}</option>`).join('')}</select></label>`
        : `<label class="f">${text}<input type="text" data-p="${k}" value="${esc(lab.params[k] ?? def)}"></label>`).join('')}
      <div class="f">Leagues<div>${leagues.map(l => `<label style="display:inline-flex;gap:5px;margin:2px 10px 2px 0;color:var(--text)"><input type="checkbox" data-l="${esc(l.league)}" ${lab.leagues.includes(l.league) ? 'checked' : ''}>${esc(l.league)}</label>`).join('')}</div></div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
        <label class="f">Bankroll (c)<input type="number" data-o="capital" value="${lab.capital}" style="width:auto"></label>
        <label class="f">Hours between visits<input type="number" data-o="attention" value="${lab.attention}" min="1" style="width:auto"></label>
        <label class="f">Max share of hourly volume<input type="number" data-o="participation" value="${lab.participation}" step="0.01" style="width:auto"></label>
        <label class="f">Slippage when taking<input type="number" data-o="slippage" value="${lab.slippage}" step="0.01" style="width:auto"></label>
        <label class="f">Start on league day<input type="number" data-o="start_day" value="${lab.start_day}" min="0" style="width:auto"></label>
        <label class="f">Run for days<input type="number" data-o="days" value="${lab.days}" placeholder="to the end" style="width:auto"></label>
      </div>
      <button class="btn primary" id="run">Run backtest</button></div></section>
    <section class="panel c8" id="res"><header><h2>Results</h2></header><div class="body" id="out"><div class="empty">Pick a strategy and run it.</div></div></section></div>`;
  $('#st', main).onchange = e => { lab.strategy = e.target.value; lab.params = {}; route(); };
  main.querySelectorAll('[data-p]').forEach(i => i.onchange = () => { lab.params[i.dataset.p] = i.value; });
  main.querySelectorAll('[data-o]').forEach(i => i.onchange = () => { lab[i.dataset.o] = i.value; });
  main.querySelectorAll('[data-l]').forEach(i => i.onchange = () => { lab.leagues = [...main.querySelectorAll('[data-l]:checked')].map(c => c.dataset.l); });
  const show = () => {
    const out = $('#out', main);
    if (!lab.results.length) return;
    out.innerHTML = `<div id="rt"></div><div class="legend" style="margin-top:14px" id="rl"></div><div id="rc"></div><div id="tops" class="small muted" style="margin-top:8px"></div>`;
    const pick = r => {
      const s = r.summary;
      $('#rl', out).innerHTML = `<span><i style="background:#b2ef78"></i>${esc(s.strategy)} on ${esc(s.league)}</span><span><i style="background:#8fb8f2"></i>Same Chaos held as Divine Orbs</span>`;
      drawChart($('#rc', out), { series: [{ label: 'Strategy', color: '#b2ef78', x: r.t, y: r.equity, width: 2 }, { label: 'Hold Divine', color: '#8fb8f2', x: r.t, y: r.divine, width: 1.3 }], yFormat: x => compact(x) + ' c', tickFormat: tick('c'), log: true, height: 280 });
      $('#tops', out).innerHTML = s.top_items.length ? 'Biggest contributors: ' + s.top_items.map(t => `${esc(t.name)} <span class="num ${t.profit >= 0 ? 'up' : 'down'}">${t.profit >= 0 ? '+' : ''}${compact(t.profit)}</span>`).join(' · ') : '';
    };
    $('#rt', out).append(table([
      { h: 'League', l: 1, f: r => esc(r.summary.league), s: r => r.summary.league },
      { h: 'Days', f: r => r.summary.days }, { h: 'Return', f: r => pct(r.summary.return, 0), s: r => r.summary.return },
      { h: 'Hold Divine', f: r => pct(r.summary.divine_return, 0), title: 'The same Chaos converted to Divine Orbs after day one' },
      { h: 'Per day', f: r => fmt(r.summary.per_day) + ' c', s: r => r.summary.per_day },
      { h: 'Worst drawdown', f: r => plainPct(r.summary.max_drawdown, 1) },
      { h: 'Fills', f: r => r.summary.fills.toLocaleString('en-US') }, { h: 'Turnover', f: r => r.summary.turnover + '×', title: 'Traded value over bankroll' },
    ], lab.results, { onRow: pick }));
    pick(lab.results[0]);
  };
  $('#run', main).onclick = async e => {
    if (!lab.leagues.length) return toast('Choose at least one league.', true);
    e.target.disabled = true; e.target.textContent = 'Running…'; lab.results = [];
    const params = {};
    for (const [k, [def]] of Object.entries(sdef.params)) { const raw = lab.params[k] ?? def; params[k] = typeof def === 'number' ? +raw : String(raw); }
    try {
      for (const league of lab.leagues) lab.results.push(await api('/api/backtest', { league, strategy: lab.strategy, params, capital: +lab.capital, participation: +lab.participation, slippage: +lab.slippage, attention: +lab.attention, start_day: +lab.start_day, days: lab.days || null }));
      show();
    } catch (err) { toast(err.message, true); }
    e.target.disabled = false; e.target.textContent = 'Run backtest';
  };
  show();
}

// An empty field means "not recorded", which is not the same as zero.
const optional = v => (v === '' || v == null ? null : +v);

// What you hold, valued now: typed in, or the positions your recorded trades add up to.
async function stashPanel(names) {
  const source = localStorage.stashSource === 'ledger' ? 'ledger' : 'stash';
  const p = panel('Stash', 'c12', `<div class="seg" id="stsrc" title="Value what you typed in, or the positions your recorded trades add up to">${[['stash', 'What I entered'], ['ledger', 'My ledger positions']].map(([k, l]) => `<button type="button" data-s="${k}" class="${k === source ? 'on' : ''}">${l}</button>`).join('')}</div>`);
  let d;
  try { d = await api(withLeague('/api/stash', source === 'ledger' ? { source } : {})); }
  catch (err) { p.insertAdjacentHTML('beforeend', `<div class="empty">${esc(err.message)}</div>`); return p; }
  const t = d.total, body = document.createElement('div'); body.className = 'body';
  body.innerHTML = `<div class="kpis" style="margin-bottom:10px">
      <div class="kpi"><span>Worth at fair value</span><b>${money(t.value)}</b></div>
      <div class="kpi" title="Every line's low added up, and every line's high: assumes all prices are wrong the same way at once, so the true range is narrower"><span>Range</span><b>${money(t.low)} <span class="dim">to</span> ${money(t.high)}</b></div>
      <div class="kpi" title="Each line sold in whichever market pays more for it, at that market's own price"><span>Sold in the better market</span><b>${money(t.best)}</b></div>
    </div>
    ${source === 'stash' ? `<form class="bar" style="margin:0 0 4px" autocomplete="off">
      <label class="f">Item<input type="text" name="what" list="items" placeholder="Start typing…" style="width:240px"></label>
      <label class="f">Units you hold<input type="number" name="units" min="0" step="any" title="0 removes the line"></label>
      <button class="btn primary">Set</button><button type="button" class="btn ghost" data-clear>Clear the stash</button>
      <span class="muted small" style="padding-bottom:7px">Setting 0 units removes a line. Kept on this machine, per league.</span></form>`
    : '<p class="note">Valued from the units your recorded trades leave you holding. Switch to “What I entered” to type in a stash instead.</p>'}
    ${d.unpriced ? `<p class="note">${d.unpriced} item${d.unpriced === 1 ? ' has' : 's have'} no price in this league and ${d.unpriced === 1 ? 'is' : 'are'} not counted.</p>` : ''}`;
  const better = r => {
    if (!r.best) return '<span class="dim">neither trades steadily</span>';
    const word = r.best === 'divine' ? 'Divine' : 'Chaos';
    return r.best_by == null ? `<span class="tag ${r.best}">${word}</span> <span class="dim">the only market trading steadily</span>` : `<span class="tag ${r.best}">${word}</span> pays ${plainPct(r.best_by, 1)} more`;
  };
  const tbl = table([
    { h: 'Item', l: 1, f: r => itemCell(r, r.note ? esc(r.note) : ''), s: r => r.name },
    { h: 'Units', f: r => fmt(r.units), s: r => r.units },
    { h: 'Worth', f: r => money(r.value), s: r => r.value, title: 'Units at fair value' },
    { h: 'Low – high', f: r => r.value == null ? '–' : `${money(r.low)} <span class="dim">–</span> ${money(r.high)}`, s: r => r.high, title: 'Units at the low and high end of where fair value plausibly sits' },
    { h: 'Chaos market', f: r => money(r.chaos?.value), s: r => r.chaos?.value, title: 'Units at the Chaos market’s own price' },
    { h: 'Divine market', f: r => money(r.divine?.value), s: r => r.divine?.value, title: 'Units at the Divine market’s own price, converted at the current Divine price' },
    { h: 'Better market', l: 1, f: r => r.value == null ? '' : better(r), s: r => r.best_by },
    { h: 'Selling it all', l: 1, f: r => r.value == null || !r.best ? '' : r.thin ? `<span class="gold" title="More than three hours of that market's volume: selling it all will push the price">thin market${r.hours != null ? ` · ${dur(r.hours)} of volume` : ''}</span>` : `<span class="muted">${dur(r.hours)} of volume</span>`, s: r => r.hours, title: 'How many hours of the better market’s volume this holding is' },
    ...(source === 'stash' ? [{ h: '', f: r => `<button class="btn ghost" data-edit="${esc(r.name)}" data-units="${r.units}" title="Change the units">Edit</button><button class="btn ghost" data-rm="${esc(r.name)}" title="Remove this line">✕</button>` }] : []),
  ], d.rows, { sort: 2, onRow: r => openItem(r.id), empty: source === 'stash' ? 'Enter what you hold to see what it is worth and where to sell it.' : 'Your recorded trades leave you holding nothing in this league.' });
  p.append(body, tbl);
  const set = async (item, units) => { try { await api('/api/stash', { league: state.league, item, units }); route(); } catch (err) { toast(err.message, true); } };
  $('#stsrc', p).onclick = e => { const s = e.target.dataset.s; if (s) { localStorage.stashSource = s; route(); } };
  const form = $('form', body);
  if (form) {
    form.onsubmit = e => { e.preventDefault(); const item = form.elements.what.value.trim(); if (!item) return toast('Pick an item from the list.', true); set(item, +form.elements.units.value || 0); };
    $('[data-clear]', form).onclick = async () => { if (!confirm('Remove every line of the stash you entered?')) return; try { await api('/api/stash', { league: state.league, clear: true }); route(); } catch (err) { toast(err.message, true); } };
  }
  tbl.addEventListener('click', e => {
    const rm = e.target.closest('[data-rm]'), ed = e.target.closest('[data-edit]');
    if (rm) set(rm.dataset.rm, 0);
    if (ed && form) { form.elements.what.value = ed.dataset.edit; form.elements.units.value = ed.dataset.units; form.elements.units.focus(); form.elements.units.select(); }
  });
  return p;
}

async function viewLedger(main) {
  if (!state.summary) await loadDesk();
  const d = await api(withLeague('/api/ledger'));
  state.bridge = d.bridge; renderTop();
  const t = d.totals, names = state.market.map(r => r.name);
  main.innerHTML = `<div class="head"><div><h1>Ledger</h1><p>Record what you actually bought and sold; positions are marked at fair value. Stored only on this machine.</p></div></div>
    <div class="kpis">
      <div class="kpi"><span>Inventory value</span><b>${money(t.value)}</b></div>
      <div class="kpi"><span>Cost of inventory</span><b>${money(t.cost)}</b></div>
      <div class="kpi"><span>Unrealised</span><b class="${t.unrealized >= 0 ? 'up' : 'down'}">${money(t.unrealized)}</b></div>
      <div class="kpi"><span>Realised</span><b class="${t.realized >= 0 ? 'up' : 'down'}">${money(t.realized)}</b></div>
    </div>
    <section class="panel" style="margin-bottom:14px"><header><h2>Record a trade</h2></header><div class="body bar" style="margin:0">
      <label class="f">Item<input type="text" id="ti" list="items" placeholder="Start typing…" style="width:240px"><datalist id="items">${names.map(n => `<option value="${esc(n)}">`).join('')}</datalist></label>
      <div class="seg" id="side"><button data-s="buy" class="on">Bought</button><button data-s="sell">Sold</button></div>
      <label class="f">Units<input type="number" id="tu" min="0" step="any"></label>
      <label class="f">Price each (c)<input type="number" id="tp" min="0" step="any"></label>
      <label class="f">Gold paid<input type="number" id="tg" min="0" step="any" placeholder="optional" title="The Gold this trade cost. The desk learns each item's fee from these."></label>
      <label class="f">Note<input type="text" id="tn" style="width:160px"></label>
      <button class="btn primary" id="add">Add</button></div></section>
    <section class="panel" style="margin-bottom:14px"><header><h2>Record an order you posted</h2></header><div class="body bar" style="margin:0">
      <label class="f">Item<input type="text" id="oi" list="items" placeholder="Start typing…" style="width:240px"></label>
      <div class="seg" id="oside"><button data-s="buy" class="on">Buying</button><button data-s="sell">Selling</button></div>
      <label class="f">Items<input type="number" id="oiu" min="1" step="1" title="The ratio as posted: this many items…"></label>
      <label class="f">for<input type="number" id="ocu" min="1" step="1" title="…for this many Chaos or Divine"></label>
      <div class="seg" id="omkt"><button data-m="chaos" class="on">Chaos</button><button data-m="divine">Divine</button></div>
      <label class="f">Total items<input type="number" id="ou" min="0" step="any"></label>
      <label class="f">Gold paid<input type="number" id="og" min="0" step="any" placeholder="optional"></label>
      <button class="btn primary" id="oadd">Add</button></div></section>
    <div class="grid" id="g"></div>`;
  let side = 'buy';
  $('#side', main).onclick = e => { const s = e.target.dataset.s; if (s) { side = s; main.querySelectorAll('#side button').forEach(b => b.classList.toggle('on', b.dataset.s === s)); } };
  $('#ti', main).onchange = e => { const row = state.market.find(r => r.name === e.target.value); if (row && !$('#tp', main).value) $('#tp', main).value = row.price; };
  $('#add', main).onclick = async () => {
    const row = state.market.find(r => r.name === $('#ti', main).value);
    if (!row) return toast('Pick an item from the list.', true);
    try { await api('/api/trade', { league: state.league, item: row.id, side, units: +$('#tu', main).value, price: +$('#tp', main).value, gold: optional($('#tg', main).value), note: $('#tn', main).value }); route(); }
    catch (err) { toast(err.message, true); }
  };
  let oside = 'buy', omkt = 'chaos';
  $('#oside', main).onclick = e => { const s = e.target.dataset.s; if (s) { oside = s; main.querySelectorAll('#oside button').forEach(b => b.classList.toggle('on', b.dataset.s === s)); } };
  $('#omkt', main).onclick = e => { const m = e.target.dataset.m; if (m) { omkt = m; main.querySelectorAll('#omkt button').forEach(b => b.classList.toggle('on', b.dataset.m === m)); } };
  $('#oadd', main).onclick = async () => {
    const row = state.market.find(r => r.name === $('#oi', main).value);
    if (!row) return toast('Pick an item from the list.', true);
    try { await api('/api/order', { league: state.league, item: row.id, side: oside, market: omkt, item_units: +$('#oiu', main).value, currency_units: +$('#ocu', main).value, units: +$('#ou', main).value, gold: optional($('#og', main).value) }); route(); }
    catch (err) { toast(err.message, true); }
  };
  const post = async (path, body) => { try { await api(path, { league: state.league, ...body }); route(); } catch (err) { toast(err.message, true); } };
  const ADVICE = { keep: 'Keep', 're-price': '<span class="gold">Re-price</span>', cancel: '<span class="down">Cancel</span>', collect: '<span class="up">Collect</span>' };
  const ord = panel('Orders', 'c12', '<span class="muted small">Advice compares each order with its own market\'s price now and with where the flip plan would post it.</span>');
  const ot = table([
    { h: 'Item', l: 1, f: r => esc(r.name) }, { h: 'Side', l: 1, f: r => r.side === 'buy' ? 'Buying' : 'Selling' },
    { h: 'Ratio', l: 1, f: r => `${r.item_units} for ${r.currency_units} ${r.market === 'divine' ? 'Divine' : 'Chaos'}` },
    { h: 'Each', f: r => money(r.price) }, { h: 'Filled', f: r => `${fmt(r.filled)} / ${fmt(r.units)}` },
    { h: 'Gold', f: r => r.gold == null ? '–' : fmt(r.gold) },
    { h: 'Advice', l: 1, f: r => `${ADVICE[r.advice]} <span class="muted small">${esc(r.reason)}${r.suggest ? ` Try ${r.suggest.item_units} for ${r.suggest.currency_units}.` : ''}</span>` },
    { h: '', f: r => r.status === 'filled'
      ? `<button class="btn ghost" data-close="${r.id}" title="You took the proceeds from the exchange">Collected</button>`
      : `<button class="btn ghost" data-fill="${r.id}" data-left="${r.units - r.filled}" title="Record a fill; the matching trade is added">Filled…</button><button class="btn ghost" data-cancel="${r.id}" title="You cancelled it in game">Cancel</button>` },
  ], d.orders, { empty: 'Record the orders you post, and the desk says which to keep, re-price, cancel or collect.' });
  ot.onclick = e => {
    const b = e.target.closest('button'); if (!b) return;
    if (b.dataset.close) post('/api/order', { id: +b.dataset.close, status: 'closed' });
    if (b.dataset.cancel) post('/api/order', { id: +b.dataset.cancel, status: 'cancelled' });
    if (b.dataset.fill) { const n = prompt('How many items filled?', b.dataset.left); if (n) post('/api/order', { fill: +b.dataset.fill, units: +n }); }
  };
  ord.append(ot);
  const running = d.sessions.find(s => s.running), plan = state.sessionPlan && state.sessionPlan.league === d.league ? state.sessionPlan : null;
  const ses = panel('Sessions', 'c12', running
    ? `<label class="f" style="flex-direction:row;align-items:center;gap:6px">Gold earned<input type="number" id="sge" min="0" step="any" placeholder="optional" style="width:110px"></label><button class="btn primary" id="sstop">Stop session</button>`
    : `<span class="muted small">${plan ? `Starts against the Flips session plan: about ${money(plan.expected_per_hour)} an hour` : 'Open Flips with a bankroll first to compare against its plan'}</span><button class="btn primary" id="sstart">Start session</button>`);
  ses.append(table([
    { h: 'Started', l: 1, f: r => new Date(r.started * 1000).toLocaleString() + (r.running ? ' <span class="gold">running</span>' : '') },
    { h: 'Hours', f: r => fmt(r.hours) }, { h: 'Profit', f: r => `<span class="${r.profit >= 0 ? 'up' : 'down'}">${money(r.profit)}</span>`, title: 'Profit realised by sales in the session, against what you paid' },
    { h: 'Per hour', f: r => money(r.per_hour) }, { h: 'Plan said', f: r => r.expected_per_hour == null ? '–' : money(r.expected_per_hour) + '/h' },
    { h: 'Gold spent', f: r => fmt(r.gold_spent) }, { h: 'Gold earned', f: r => r.gold_earned == null ? '–' : fmt(r.gold_earned) },
    { h: '', f: r => `<button class="btn ghost" data-del="${r.id}" title="Delete this session">✕</button>` },
  ], d.sessions, { empty: 'Start a session when you sit down to trade; it reports what you made per hour.' }));
  ses.onclick = e => { const b = e.target.closest('[data-del]'); if (b) post('/api/session', { delete: +b.dataset.del }); };
  const start = $('#sstart', ses), stop = $('#sstop', ses);
  if (start) start.onclick = () => post('/api/session', plan ? { expected_per_hour: plan.expected_per_hour, plan } : {});
  if (stop) stop.onclick = () => post('/api/session', { stop: running.id, gold_earned: optional($('#sge', ses).value) });
  const fee = panel('Gold fees you paid', 'c12', '<span class="muted small">Learned from the Gold you record; the game does not publish its fees.</span>');
  fee.append(table([
    { h: 'Item', l: 1, f: r => esc(r.name) }, { h: 'Gold per item', f: r => fmt(r.gold_per_unit), title: 'The middle value over your records' },
    { h: 'From', f: r => `${r.trades} record${r.trades === 1 ? '' : 's'}` },
  ], d.gold_fees, { empty: 'Unknown until you record the Gold a trade or order cost.' }));
  const rc = panel('Recipe checks', 'c12', '<span class="muted small">Did a conversion or round trip work when you tried it in game? Kept for every league.</span>');
  const rform = document.createElement('div'); rform.className = 'body bar'; rform.style.margin = '0';
  rform.innerHTML = `<label class="f">Recipe<select id="rk" style="max-width:420px">${d.recipes.map(r => `<option value="${esc(r.key)}">${esc(r.label)}${r.check ? ` (${r.check.status})` : ''}</option>`).join('')}</select></label>
    <label class="f">Note<input type="text" id="rn" style="width:200px"></label>
    <button class="btn" data-st="confirmed">Worked</button><button class="btn" data-st="failed">Did not work</button>`;
  rform.onclick = e => { const st = e.target.dataset.st; if (st && $('#rk', rform).value) post('/api/recipe_check', { key: $('#rk', rform).value, status: st, note: $('#rn', rform).value }); };
  const rt = table([
    { h: 'Recipe', l: 1, f: r => esc(r.label) }, { h: 'Result', l: 1, f: r => r.status === 'confirmed' ? '<span class="up">Worked</span>' : '<span class="down">Did not work</span>' },
    { h: 'Note', l: 1, f: r => esc(r.note) }, { h: 'Checked', l: 1, f: r => new Date(r.t * 1000).toLocaleDateString() },
    { h: '', f: r => `<button class="btn ghost" data-del="${r.id}" title="Delete this check">✕</button>` },
  ], d.recipe_checks, { empty: 'No recipe checked yet.' });
  rt.onclick = e => { const b = e.target.closest('[data-del]'); if (b) post('/api/recipe_check', { delete: +b.dataset.del }); };
  rc.append(rform, rt);
  const pos = panel('Positions', 'c12');
  pos.append(table([
    { h: 'Item', l: 1, f: r => itemCell(r), s: r => r.name }, { h: 'Units', f: r => fmt(r.units), s: r => r.units },
    { h: 'Avg. cost', f: r => money(r.average), s: r => r.average }, { h: 'Fair value', f: r => money(r.mark), s: r => r.mark },
    { h: 'Value', f: r => money(r.value), s: r => r.value }, { h: 'Unrealised', f: r => r.unrealized == null ? '–' : `<span class="${r.unrealized >= 0 ? 'up' : 'down'}">${money(r.unrealized)}</span>`, s: r => r.unrealized },
    { h: 'Realised', f: r => `<span class="${r.realized >= 0 ? 'up' : 'down'}">${money(r.realized)}</span>`, s: r => r.realized },
  ], d.positions.filter(p => p.units > 0 || p.realized), { sort: 4, onRow: r => openItem(r.id), empty: 'No trades recorded in this league yet.' }));
  const tr = panel('Trades', 'c12');
  const tt = table([
    { h: 'When', l: 1, f: r => new Date(r.t * 1000).toLocaleString() }, { h: 'Item', l: 1, f: r => esc(r.name) },
    { h: 'Side', l: 1, f: r => r.side === 'buy' ? 'Bought' : 'Sold' }, { h: 'Units', f: r => fmt(r.units) }, { h: 'Price', f: r => money(r.price) },
    { h: 'Gold', f: r => r.gold == null ? '–' : fmt(r.gold) },
    { h: 'Note', l: 1, f: r => esc(r.note) }, { h: '', f: r => `<button class="btn ghost" data-del="${r.id}" title="Delete this trade">✕</button>` },
  ], d.trades, { empty: 'Nothing recorded.' });
  tt.onclick = async e => { const b = e.target.closest('[data-del]'); if (b) { await api('/api/trade', { delete: +b.dataset.del }); route(); } };
  tr.append(tt);
  const al = panel('Alerts', 'c12');
  const at = table([
    { h: 'Item', l: 1, f: r => esc((state.market.find(m => m.id === r.item) || {}).name || r.item) },
    { h: 'Rule', l: 1, f: r => `${esc(d.alert_fields[r.field])} ${r.op} ${r.threshold}` },
    { h: 'State', l: 1, f: r => r.hot ? '<span class="gold">Triggered</span>' : 'Armed' },
    { h: '', f: r => `<button class="btn ghost" data-del="${r.id}" title="Delete this alert">✕</button>` },
  ], d.alerts, { empty: 'Open an item to add a price alert.' });
  at.onclick = async e => { const b = e.target.closest('[data-del]'); if (b) { await api('/api/alert', { delete: +b.dataset.del }); route(); } };
  al.append(at);
  const stash = await stashPanel(names);
  $('#g', main).append(ses, ord, pos, stash, tr, fee, rc, al);
}

// ------------------------------------------------------------------ drawer
async function openItem(id, hours = 168) {
  const drawer = $('#drawer');
  drawer.hidden = false; drawer.innerHTML = '<div class="empty">Loading…</div>';
  let d, curve;
  try { [d, curve] = await Promise.all([api(withLeague('/api/item', { id, hours, share: state.share })), api(`/api/curve?id=${encodeURIComponent(id)}`)]); }
  catch (err) { drawer.innerHTML = `<div class="empty">${esc(err.message)}</div>`; return; }
  const s = d.series, row = (state.market || []).find(r => r.id === s.id) || {};
  const div = state.unit === 'divine', conv = (arr) => arr.map((v, i) => v == null ? null : div ? v / s.bridge[i] : v);
  const yf = x => fmt(x) + (div ? ' d' : ' c');
  drawer.innerHTML = `<button class="btn ghost close" title="Close (Esc)">✕</button>
    <div class="item" style="margin-bottom:4px">${icon(s.id)}<h1>${esc(s.name)}</h1></div>
    <div class="muted" style="margin-bottom:12px">${esc(s.category)}${d.meta.set ? ` · ${d.meta.set.need} turn into ${d.meta.set.gives} ${esc(d.meta.set.reward)}` : ''}</div>
    <div class="kpis">
      <div class="kpi"><span>Fair value</span><b>${money(row.price)}</b><i>${pct(row.change_24h)}</i></div>
      <div class="kpi"><span>vs Chaos</span><b>${money(row.chaos)}</b></div>
      <div class="kpi"><span>vs Divine</span><b>${money(row.divine)}</b><i>${pct(row.premium)}</i></div>
      <div class="kpi"><span>Traded 24h</span><b>${money(row.value_24h)}</b></div>
    </div>
    ${row.move != null ? `<p class="note">The model reads this item as moving about ±${(row.move * 100).toFixed(1)}% an hour for real, with prints bouncing ±${(row.noise * 100).toFixed(1)}% around that. ${row.z != null && Math.abs(row.z) >= 2 ? `The latest print was ${Math.abs(row.z).toFixed(1)} deviations ${row.z > 0 ? 'above' : 'below'} what the model expected.` : ''}</p>` : ''}
    <div class="bar"><div class="seg" id="rg">${[[48, '2 days'], [168, '7 days'], [720, '30 days']].map(([h, l]) => `<button data-h="${h}" class="${h === hours ? 'on' : ''}">${l}</button>`).join('')}</div>
      <span class="grow"></span><button class="btn" id="tcItem" title="Is an offer for this item a good deal? (C)">Check an offer</button><button class="btn" id="watchBtn">${row.watched ? '★ Watching' : '☆ Watch'}</button></div>
    <div class="legend"><span><i style="background:#b2ef78"></i>Fair value</span><span><i style="background:#d9a05b"></i>Chaos market, hourly average</span><span><i style="background:#8fb8f2"></i>Divine market, hourly average</span><span><i style="background:#ffffff30"></i>Value traded</span></div>
    <div id="pc"></div>
    <h2 style="margin:18px 0 8px">Flip plans</h2><div class="orders" id="plans"></div>
    ${d.conversions.length ? `<h2 style="margin:18px 0 8px">Sets</h2><div class="orders">${d.conversions.map(c => `<div class="order"><b>${c.need} ${esc(c.name)}</b> → ${c.gives} ${esc(c.reward)}<div class="big">${pct(c.margin)}</div><span class="muted small">Costs ${money(c.cost)}, worth ${money(c.proceeds)}</span></div>`).join('')}</div>` : ''}
    <h2 style="margin:18px 0 8px">Same league day, past leagues <span class="muted" style="font-weight:400">(${div ? 'in Divine' : 'in Chaos'})</span></h2><div id="lgl"></div><div id="lc"></div>
    <h2 style="margin:18px 0 8px">Alert me when</h2>
    <div class="bar"><select id="af">${Object.entries({ price: 'Fair value', chaos: 'Chaos-market price', divine: 'Divine-market price', premium: 'Divine premium', change_24h: '24h change' }).map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}</select>
      <select id="ao"><option value="above">rises above</option><option value="below">falls below</option></select>
      <input type="number" id="at" step="any" placeholder="${row.price ?? ''}"><button class="btn" id="aa">Add alert</button><span class="muted small">Prices in Chaos; premium and change as fractions (0.1 = 10%).</span></div>`;
  drawChart($('#pc', drawer), {
    series: [
      { label: 'Fair value', color: '#b2ef78', x: s.t, y: conv(s.fair), width: 2, bridge: true },
      { label: 'Chaos market', color: '#d9a05b', x: s.t, y: conv(s.chaos), dots: true },
      { label: 'Divine market', color: '#8fb8f2', x: s.t, y: conv(s.divine), dots: true },
    ], bars: { x: s.t, y: s.value }, yFormat: yf, tickFormat: tick(div ? 'd' : 'c'), height: 300,
  });
  $('#plans', drawer).innerHTML = d.flips.length ? d.flips.map(p => `<div class="order"><span class="tag ${p.kind}">${KIND[p.kind]}</span>
      <div class="big">${pct(p.margin)} <span class="muted small">≈ ${money(p.profit_per_hour)} / hour</span></div>
      <div class="num small">Buy: ${order(p.buy_order, true)}<br>Sell: ${order(p.sell_order, false)}</div>
      <div class="muted small" style="margin-top:5px">${plainPct(p.reach)} of recent volume traded beyond these levels · ${fmt(p.units_per_hour)} units/hour<br>${p.replay ? `Replayed over the last 7 days: ${pct(p.replay.return, 0)} on the stake, ${p.replay.sales} sale${p.replay.sales === 1 ? '' : 's'}` : 'Replayed over the last 7 days: no fills'}</div></div>`).join('')
    : '<div class="muted">Not enough steady two-sided trading to plan a flip.</div>';
  const cur = Object.keys(curve.curve);
  if (cur.length) $('#lgl', drawer).innerHTML = leagueChart($('#lc', drawer), curve.curve, state.league, div ? 'price_div' : 'price', yf);
  else $('#lc', drawer).innerHTML = '<div class="muted">No league history for this item.</div>';
  $('.close', drawer).onclick = closeDrawer;
  $('#tcItem', drawer).onclick = () => openCheck(s.name);
  $('#rg', drawer).onclick = e => { const h = +e.target.dataset.h; if (h) openItem(id, h); };
  $('#watchBtn', drawer).onclick = async () => { row.watched = !row.watched; await api('/api/watch', { league: state.league, item: s.id, on: row.watched }); $('#watchBtn', drawer).textContent = row.watched ? '★ Watching' : '☆ Watch'; };
  $('#aa', drawer).onclick = async () => {
    const threshold = $('#at', drawer).value; if (threshold === '') return toast('Enter a threshold.', true);
    try {
      await api('/api/alert', { league: state.league, item: s.id, field: $('#af', drawer).value, op: $('#ao', drawer).value, threshold: +threshold });
      toast('Alert added. It fires when a new hour crosses the threshold.');
      if (window.Notification?.permission === 'default') Notification.requestPermission();
    }
    catch (err) { toast(err.message, true); }
  };
}
function closeDrawer() { $('#drawer').hidden = true; $('#drawer').innerHTML = ''; }

// --------------------------------------------------------------- trade check
// A small panel outside #main, so a refresh of the page under it never wipes what you typed.
const VERDICT = { go: ['up', 'Go'], caution: ['gold', 'Caution'], 'no-go': ['down', 'No-go'] };
function openCheck(item) {
  let host = $('#tc');
  if (!host) { host = document.createElement('aside'); host.id = 'tc'; document.body.append(host); }
  const mkt = localStorage.tcMarket || 'chaos', side = localStorage.tcSide || 'buy';
  const names = (state.market || []).map(r => r.name);
  host.hidden = false;
  host.innerHTML = `<form autocomplete="off">
    <header><h2>Check an offer</h2><span class="grow"></span><span class="muted small"><kbd>Enter</kbd> check · <kbd>Esc</kbd> close</span><button type="button" class="btn ghost" data-x title="Close (Esc)">✕</button></header>
    <label class="f">Item<input type="text" name="what" list="tcItems" placeholder="Start typing…" value="${esc(item || '')}"><datalist id="tcItems">${names.map(n => `<option value="${esc(n)}">`).join('')}</datalist></label>
    <div class="row">
      <div class="seg" data-g="side" title="Would you buy the item or sell it?">${[['buy', 'I buy'], ['sell', 'I sell']].map(([k, l]) => `<button type="button" data-v="${k}" class="${side === k ? 'on' : ''}">${l}</button>`).join('')}</div>
      <div class="seg" data-g="market" title="Which market the offer is in">${[['chaos', 'Chaos market'], ['divine', 'Divine market']].map(([k, l]) => `<button type="button" data-v="${k}" class="${mkt === k ? 'on' : ''}">${l}</button>`).join('')}</div>
    </div>
    <div class="row"><label class="f">Items<input type="number" name="iu" min="1" step="1" placeholder="64"></label><span class="muted" style="padding-bottom:6px">for</span>
      <label class="f"><span data-cur>${mkt === 'divine' ? 'Divine' : 'Chaos'}</span><input type="number" name="cu" min="1" step="1" placeholder="1"></label>
      <label class="f">Stock (optional)<input type="number" name="stock" min="0" step="1" placeholder="on offer"></label></div>
    <button class="btn primary">Check</button>
    <div data-out></div></form>`;
  const form = $('form', host), out = $('[data-out]', host);
  const pick = g => $(`[data-g="${g}"] .on`, form).dataset.v;
  form.onclick = e => {
    if (e.target.closest('[data-x]')) return closeCheck();
    const b = e.target.closest('[data-g] button'); if (!b) return;
    b.parentNode.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
    localStorage[b.parentNode.dataset.g === 'side' ? 'tcSide' : 'tcMarket'] = b.dataset.v;
    $('[data-cur]', form).textContent = pick('market') === 'divine' ? 'Divine' : 'Chaos';
  };
  form.onkeydown = e => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeCheck(); } };
  form.onsubmit = async e => {
    e.preventDefault();
    const v = name => form.elements[name].value.trim();
    if (!v('what') || !(+v('iu') > 0) || !(+v('cu') > 0)) { out.innerHTML = '<p class="muted">Give the item and both sides of the ratio, items first.</p>'; return; }
    out.innerHTML = '<p class="muted">Checking…</p>';
    try {
      const r = await api('/api/check', { league: state.league, item: v('what'), market: pick('market'), side: pick('side'), item_units: Math.round(+v('iu')), currency_units: Math.round(+v('cu')), stock: v('stock') || null });
      const [cls, word] = VERDICT[r.verdict] || ['muted', r.verdict];
      out.innerHTML = `<div class="verdict ${cls}">${word} <span class="muted small">${esc(r.name)} at ${fmt(r.price)} c each${r.trusted ? '' : ' · this market barely trades'}</span></div>
        <ul>${r.reasons.map(x => `<li>${esc(x)}</li>`).join('')}</ul>`;
    } catch (err) { out.innerHTML = `<p class="down">${esc(err.message)}</p>`; }
  };
  (item ? form.elements.iu : form.elements.what).focus();
}
function closeCheck() { const host = $('#tc'); if (host) host.hidden = true; }

// ----------------------------------------------------------------- hideout
async function viewHideout(main) {
  const [d, f] = await Promise.all([loadDesk(), api(withLeague('/api/flips', { share: state.share, limit: 12, ...bankQuery() }))]);
  const s = d.summary, watched = d.market.filter(r => r.watched);
  main.innerHTML = `<div class="kpi" style="margin-bottom:10px"><span>${esc(s.league)}${s.league_day == null ? '' : ' · day ' + s.league_day} · ${ago(s.as_of + 3600)}</span><b>Divine ${fmt(s.bridge)} c</b><i>${pct(s.bridge_change_24h)}</i></div>
    <button class="btn" id="tcBtn" style="width:100%;margin-bottom:10px" title="Is an offer in the order window a good deal?">Check an offer <kbd>C</kbd></button>`;
  $('#tcBtn', main).onclick = () => openCheck();
  const fp = panel('Flips', '');
  fp.append(table([
    { h: 'Item', l: 1, f: r => `<b>${esc(r.name)}</b><span class="sub">Buy ${order(r.buy_order, true)}</span><span class="sub">Sell ${order(r.sell_order, false)}</span>` },
    { h: 'Margin', f: r => pct(r.margin, 0) }, { h: '/ hour', f: r => money(r.profit_per_hour) },
  ], f.flips.slice(0, 10)));
  const wp = panel('Watchlist', '');
  wp.append(table([{ h: 'Item', l: 1, f: r => esc(r.name) }, { h: 'Price', f: r => money(r.price) }, { h: '24h', f: r => pct(r.change_24h, 0) }], watched, { empty: 'Star items in Markets.' }));
  main.append(fp, wp);
  if (d.events.length) { const ev = panel('Alerts', ''); ev.innerHTML += `<div class="body small">${d.events.slice(0, 5).map(e => `<div><b>${esc(e.title)}</b> <span class="muted">${ago(e.t)}</span></div>`).join('')}</div>`; main.append(ev); }
}
async function openHideout() {
  const url = '/?hideout=1';
  if ('documentPictureInPicture' in window) {
    try {
      const pip = await window.documentPictureInPicture.requestWindow({ width: 400, height: 640 });
      pip.document.body.style.margin = '0';
      pip.document.body.innerHTML = `<iframe src="${url}" style="border:0;width:100vw;height:100vh;display:block"></iframe>`;
      return;
    } catch { /* fall through to a normal window */ }
  }
  window.open(url, 'vaal-hideout', 'width=400,height=640');
}

// ------------------------------------------------------------------- router
const VIEWS = { desk: viewDesk, markets: viewMarkets, flips: viewFlips, arbitrage: viewArbitrage, league: viewLeague, lab: viewLab, ledger: viewLedger };
let routing = 0;
async function route() {
  const main = $('#main'), turn = ++routing;
  if (!state.status?.ready) {
    const s = state.status;
    main.innerHTML = `<div class="empty"><h1 style="margin-bottom:8px">Fetching market history</h1>${esc(s?.sync?.error || s?.sync?.message || 'Starting…')}<br><span class="dim">The first sync downloads the last two weeks of exchange hours. This takes a few minutes.</span></div>`;
    return;
  }
  if (state.hideout) { document.body.classList.add('hideout'); try { await viewHideout(main); } catch (err) { main.innerHTML = `<div class="empty">${esc(err.message)}</div>`; } return; }
  const page = (location.hash.replace('#/', '') || 'desk').split('?')[0];
  const view = VIEWS[page] || viewDesk;
  renderNav(VIEWS[page] ? page : 'desk');
  const scroll = main.scrollTop;
  try { await view(main); if (turn === routing) main.scrollTop = scroll; }
  catch (err) { if (turn === routing) main.innerHTML = `<div class="empty">${esc(err.message)}</div>`; }
}
async function poll(first) {
  try {
    const s = await api('/api/status');
    const changed = s.generation !== state.generation || s.ready !== state.status?.ready;
    state.status = s; state.league ||= s.league; state.generation = s.generation;
    announce(s.last_event);
    if (first || changed) await route(); else if (!state.hideout && state.summary) renderTop();
    else if (!s.ready) route();
  } catch (err) { if (first) $('#main').innerHTML = `<div class="empty">${esc(err.message)}</div>`; }
}
// A fired alert reaches you even when the desk is in a background tab.
function announce(event) {
  if (!event) return;
  const seen = +(localStorage.lastEvent || 0);
  localStorage.lastEvent = Math.max(seen, event.id);
  if (!seen || event.id <= seen) return;
  toast(`${event.title} — ${event.body}`);
  if (window.Notification?.permission === 'granted') new Notification(event.title, { body: event.body, icon: '/mark.svg' });
}
window.addEventListener('hashchange', () => { closeDrawer(); $('#main').scrollTop = 0; route(); });
window.addEventListener('keydown', e => {
  if (e.target.matches('input,select,textarea')) { if (e.key === 'Escape') e.target.blur(); return; }
  if (e.key === 'Escape') { if ($('#tc') && !$('#tc').hidden) closeCheck(); else closeDrawer(); }
  else if (e.ctrlKey || e.metaKey || e.altKey) return;
  else if (e.key === 'h' || e.key === 'H') openHideout();
  else if (e.key === 'c' || e.key === 'C') { e.preventDefault(); openCheck(); }
  else if (e.key === '/') { e.preventDefault(); if (!$('#q')) location.hash = '#/markets'; setTimeout(() => $('#q')?.focus(), 150); }
});
poll(true);
setInterval(poll, state.hideout ? 60000 : 20000);
