import { drawChart, spark } from './chart.js';

const $ = (sel, root = document) => root.querySelector(sel);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const state = {
  status: null, league: null, bridge: null, unit: localStorage.unit || 'chaos', share: +(localStorage.share || 0.1),
  generation: -1, filters: { q: '', category: '', minValue: 1000, watched: false, kind: 'all', minMargin: 0.03, capital: localStorage.bankroll || '' },
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
      ${data.length > shown ? `<div class="empty"><button class="btn" data-more>Show ${Math.min(200, data.length - shown)} more of ${data.length}</button></div>` : ''}`;
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
    <div class="foot"><kbd>/</kbd> search &nbsp; <kbd>H</kbd> hideout<br>Not affiliated with Grinding Gear Games.</div>`;
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
  const [d, f, a, lg] = await Promise.all([loadDesk(), api(withLeague('/api/flips', { share: state.share, limit: 40 })), api(withLeague('/api/arbitrage', { share: state.share })), api(withLeague('/api/league', { horizon: 7, unit: 'divine' }))]);
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

  const flips = panel('Best flips right now', 'c7', '<a href="#/flips">All flips →</a>');
  flips.append(table([
    { h: 'Item', l: 1, f: r => itemCell(r, `<span class="tag ${r.kind}">${KIND[r.kind]}</span>`) },
    { h: 'Buy order', f: r => order(r.buy_order, true), title: 'Currency you pay, for how many items' },
    { h: 'Sell order', f: r => order(r.sell_order, false), title: 'Items you give, for how much currency' },
    { h: 'Margin', f: r => pct(r.margin) },
    { h: 'Est. / hour', f: r => money(r.profit_per_hour) },
  ], f.flips.slice(0, 8), { onRow: r => openItem(r.id), empty: 'No market is liquid enough for a plan yet.' }));
  g.append(flips);

  const sets = panel('Sets worth completing', 'c5', '<a href="#/arbitrage">All conversions →</a>');
  sets.append(table([
    { h: 'Set', l: 1, f: r => itemCell(r, `${r.need} → ${r.gives} ${esc(r.reward)}`) },
    { h: 'Cost', f: r => money(r.cost) }, { h: 'Worth', f: r => money(r.proceeds) }, { h: 'Margin', f: r => pct(r.margin) },
  ], a.conversions.filter(r => r.margin > 0.02).slice(0, 8), { onRow: r => openItem(r.id), empty: 'No set is currently cheaper than its reward.' }));
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
  main.innerHTML = `<div class="head"><div><h1>Markets</h1><p>Every item that trades on the exchange. Price is the model's fair value; “vs Chaos” and “vs Divine” are what the two markets for the item are actually paying.</p></div></div>
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
  $('#q', main).oninput = e => { f.q = e.target.value; draw(); };
  $('#cat', main).onchange = e => { f.category = e.target.value; draw(); };
  $('#minv', main).onchange = e => { f.minValue = +e.target.value || 0; draw(); };
  $('#wonly', main).onchange = e => { f.watched = e.target.checked; draw(); };
  draw();
}

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
];

async function viewFlips(main) {
  const f = state.filters;
  if (!state.summary) await loadDesk();
  const d = await api(withLeague('/api/flips', { share: state.share, min_margin: f.minMargin }));
  state.bridge = d.summary.bridge; renderTop();
  main.innerHTML = `<div class="head"><div><h1>Flips</h1><p>Buy low in one market, sell high in the same or the other. Ranked by estimated profit per hour.</p></div></div>
    <p class="note">Each plan rests a buy order at a price the market's <b>hourly average</b> has dipped to, and a sell order at a price it has risen to — levels a stated share of the last two days' volume actually traded beyond. “Chaos → Divine” buys from people dumping loot for Chaos and sells to people paying Divines, who consistently pay more. Estimates assume you capture ${plainPct(state.share)} of the market's flow and ignore Gold fees and who is ahead of you in the queue; check the in-game book before sizing up.</p>
    <div class="bar">
      <div class="seg" id="kind">${['all', 'chaos', 'divine', 'cross'].map(k => `<button data-k="${k}" class="${f.kind === k ? 'on' : ''}">${k === 'all' ? 'All plans' : KIND[k]}</button>`).join('')}</div>
      <label class="f">Your share of the flow: <span id="shareL">${plainPct(state.share)}</span><input type="range" id="share" min="0.01" max="0.3" step="0.01" value="${state.share}"></label>
      <label class="f">Min. margin (%)<input type="number" id="mm" value="${f.minMargin * 100}" min="0" step="1"></label>
      <label class="f">My bankroll (c)<input type="number" id="cap" value="${f.capital}" placeholder="e.g. 5000" min="0" step="1000"></label>
      <label class="f">Search<input type="search" id="q" value="${esc(f.q)}" placeholder="Item name"></label>
    </div><div id="plan"></div><section class="panel" id="tbl"></section>`;
  const draw = () => {
    const q = f.q.toLowerCase();
    const rows = d.flips.filter(r => (f.kind === 'all' || r.kind === f.kind) && (!q || r.name.toLowerCase().includes(q)));
    $('#tbl', main).replaceChildren(table(flipCols(), rows, { sort: 6, onRow: r => openItem(r.id), empty: 'No plan clears these filters.' }));
    // A session plan: the flips that earn most per Chaos tied up, one per item, sized to fit the bankroll.
    const bank = +f.capital, host = $('#plan', main);
    host.replaceChildren();
    if (!(bank > 0)) return;
    // Each item gets at most a quarter of the bankroll; plans are taken in order of what that stake earns,
    // and only if the same rule sold and made money over the last week.
    const perItem = bank / 4, taken = new Set(), picks = [];
    let left = bank;
    const sized = rows.filter(r => r.capital > 0 && r.replay && r.replay.return > 0 && r.replay.sales >= 2)
      .map(r => { const stake = Math.min(r.capital, perItem); return { ...r, stake, est: r.profit_per_hour * stake / r.capital }; })
      .sort((a, b) => b.est - a.est);
    for (const r of sized) {
      if (left < bank * 0.02 || picks.length >= 12) break;
      if (taken.has(r.id)) continue;
      const stake = Math.min(r.stake, left);
      taken.add(r.id); left -= stake;
      picks.push({ ...r, stake, est: r.est * stake / r.stake });
    }
    const total = picks.reduce((a, r) => a + r.est, 0);
    const p = panel(`Session plan for ${fmt(bank)} c`, '', `<span class="muted small">about ${money(total)} an hour if every order fills as estimated · ${money(bank - left)} deployed</span>`);
    p.style.marginBottom = '14px';
    p.append(table([
      { h: 'Item', l: 1, f: r => itemCell(r, `<span class="tag ${r.kind}">${KIND[r.kind]}</span>`) },
      { h: 'Buy order', f: r => order(r.buy_order, true) }, { h: 'Sell order', f: r => order(r.sell_order, false) },
      { h: 'Margin', f: r => pct(r.margin) }, { h: 'Stake', f: r => money(r.stake), title: 'Chaos to keep working in this item (at most a quarter of the bankroll)' },
      { h: 'Est. / hour', f: r => money(r.est) },
      { h: 'Last 7 days', f: r => r.replay ? pct(r.replay.return, 0) : '<span class="dim">no fills</span>' },
    ], picks, { onRow: r => openItem(r.id), empty: 'No plan fits this bankroll with the current filters.' }));
    host.append(p);
  };
  $('#kind', main).onclick = e => { const k = e.target.dataset.k; if (k) { f.kind = k; main.querySelectorAll('#kind button').forEach(b => b.classList.toggle('on', b.dataset.k === k)); draw(); } };
  $('#share', main).oninput = e => { $('#shareL', main).textContent = plainPct(+e.target.value); };
  $('#share', main).onchange = e => { state.share = +e.target.value; localStorage.share = state.share; route(); };
  $('#mm', main).onchange = e => { f.minMargin = (+e.target.value || 0) / 100; route(); };
  $('#cap', main).onchange = e => { f.capital = localStorage.bankroll = e.target.value; draw(); };
  $('#q', main).oninput = e => { f.q = e.target.value; draw(); };
  draw();
}

async function viewArbitrage(main) {
  if (!state.summary) await loadDesk();
  const d = await api(withLeague('/api/arbitrage', { share: state.share }));
  state.bridge = d.summary.bridge; renderTop();
  main.innerHTML = `<div class="head"><div><h1>Arbitrage</h1><p>Prices that disagree with each other: full sets against what they turn into, and item-for-item markets against the items' own prices.</p></div></div><div class="grid" id="g"></div>`;
  const sets = panel('Set conversions', 'c12');
  sets.innerHTML += `<div class="body" style="padding-bottom:0"><p class="note">“Margin” values both sides at fair value — what a patient trader gets. “In a hurry” buys the set in the dearer market and dumps the reward in the cheaper one; when that is still positive the conversion pays even without waiting for fills.</p></div>`;
  sets.append(table([
    { h: 'Turn in', l: 1, f: r => itemCell(r, `${r.need} → ${r.gives} ${esc(r.reward)}`), s: r => r.name },
    { h: 'Set cost', f: r => money(r.cost), s: r => r.cost },
    { h: 'Reward worth', f: r => money(r.proceeds), s: r => r.proceeds },
    { h: 'Margin', f: r => pct(r.margin), s: r => r.margin },
    { h: 'In a hurry', f: r => pct(r.margin_now), s: r => r.margin_now },
    { h: 'Sets / hour', f: r => fmt(r.sets_per_hour), s: r => r.sets_per_hour, title: `At ${plainPct(state.share)} of the item's traded volume` },
    { h: 'Est. / hour', f: r => money(r.profit_per_hour), s: r => r.profit_per_hour },
    { h: 'Traded 24h', f: r => money(r.value_24h), s: r => r.value_24h },
  ], d.conversions, { sort: 6, onRow: r => openItem(r.id), empty: 'No convertible set is trading right now.' }));
  const swaps = panel('Item-for-item markets out of line', 'c12');
  swaps.innerHTML += `<div class="body" style="padding-bottom:0"><p class="note">Small side markets where one item trades directly for another. “Edge” is how much more the side market gives than selling one item and buying the other at fair value would. These markets are thin: treat a large edge as a stale order to check, not a certainty.</p></div>`;
  swaps.append(table([
    { h: 'Give', l: 1, f: r => itemCell({ id: r.give, name: r.give_name }), s: r => r.give_name },
    { h: 'Get', l: 1, f: r => itemCell({ id: r.get, name: r.get_name }), s: r => r.get_name },
    { h: 'Market rate', f: r => fmt(r.rate), s: r => r.rate, title: 'Units received per unit given, median of the last day' },
    { h: 'Fair rate', f: r => fmt(r.implied_rate), s: r => r.implied_rate },
    { h: 'Edge', f: r => pct(r.edge), s: r => r.edge },
    { h: 'Traded 24h', f: r => money(r.value_24h), s: r => r.value_24h },
    { h: 'Hours active', f: r => r.hours + ' / 24', s: r => r.hours },
  ], d.swaps, { sort: 5, empty: 'No side market traded steadily in the last day.' }));
  $('#g', main).append(sets, swaps);
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
    { h: 'Rose in', f: r => `${Math.round(r.up_share * r.n)} of ${r.n}`, s: r => r.up_share },
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
      <label class="f">Note<input type="text" id="tn" style="width:160px"></label>
      <button class="btn primary" id="add">Add</button></div></section>
    <div class="grid" id="g"></div>`;
  let side = 'buy';
  $('#side', main).onclick = e => { const s = e.target.dataset.s; if (s) { side = s; main.querySelectorAll('#side button').forEach(b => b.classList.toggle('on', b.dataset.s === s)); } };
  $('#ti', main).onchange = e => { const row = state.market.find(r => r.name === e.target.value); if (row && !$('#tp', main).value) $('#tp', main).value = row.price; };
  $('#add', main).onclick = async () => {
    const row = state.market.find(r => r.name === $('#ti', main).value);
    if (!row) return toast('Pick an item from the list.', true);
    try { await api('/api/trade', { league: state.league, item: row.id, side, units: +$('#tu', main).value, price: +$('#tp', main).value, note: $('#tn', main).value }); route(); }
    catch (err) { toast(err.message, true); }
  };
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
  $('#g', main).append(pos, tr, al);
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
      <span class="grow"></span><button class="btn" id="watchBtn">${row.watched ? '★ Watching' : '☆ Watch'}</button></div>
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

// ----------------------------------------------------------------- hideout
async function viewHideout(main) {
  const [d, f] = await Promise.all([loadDesk(), api(withLeague('/api/flips', { share: state.share, limit: 12 }))]);
  const s = d.summary, watched = d.market.filter(r => r.watched);
  main.innerHTML = `<div class="kpi" style="margin-bottom:10px"><span>${esc(s.league)}${s.league_day == null ? '' : ' · day ' + s.league_day} · ${ago(s.as_of + 3600)}</span><b>Divine ${fmt(s.bridge)} c</b><i>${pct(s.bridge_change_24h)}</i></div>`;
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
  if (e.key === 'Escape') closeDrawer();
  else if (e.key === 'h' || e.key === 'H') openHideout();
  else if (e.key === '/') { e.preventDefault(); if (!$('#q')) location.hash = '#/markets'; setTimeout(() => $('#q')?.focus(), 150); }
});
poll(true);
setInterval(poll, state.hideout ? 60000 : 20000);
