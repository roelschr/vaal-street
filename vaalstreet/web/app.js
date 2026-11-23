import { drawChart, spark, css } from './chart.js';

const $ = (sel, root = document) => root.querySelector(sel);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const state = {
  status: null, league: null, bridge: null, unit: localStorage.unit || 'chaos', share: +(localStorage.share || 0.1),
  generation: -1, filters: { q: '', category: '', minValue: 1000, watched: false, kind: 'all', minMargin: 0.03, capital: localStorage.bankroll || '', goldBudget: localStorage.goldBudget || '', rankBy: 'profit' },
  league_view: { horizon: 7, unit: 'divine' }, lab: null, hideout: new URLSearchParams(location.search).has('hideout'),
};
// Series colours for charts that compare many lines; the tokens live in style.css.
const palette = () => ['--s1', '--s2', '--s3', '--s4', '--s5', '--s6', '--s7', '--s8'].map(css);

// ---------------------------------------------------------------- plumbing
async function api(path, body) {
  const opts = body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {};
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({ error: res.statusText }));
  if (!res.ok) throw new Error(data.error || res.statusText);
  practiceFrom(data);
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
// A big Chaos amount, short ("8.4M c" or "57K d"), in the unit the trader chose.
const compactMoney = (v, bridge = state.bridge) => state.unit === 'divine' && bridge ? `${compact(v / bridge)}${CUR.d}` : `${compact(v)}${CUR.c}`;
// A reference-currency amount in the unit the trader chose.
function money(v, unit = state.unit) {
  if (v == null) return '<span class="dim">–</span>';
  return unit === 'divine' && state.bridge ? `${fmt(v / state.bridge)}${CUR.d}` : `${fmt(v)}${CUR.c}`;
}
// What a replay made per hour, with the stake it was measured on: "9.8 c / h on 1,000 c". A plan's
// "Est. / hour" is at whatever capital its markets absorb, so the stake has to be said.
const replayRate = rp => rp.per_hour == null || rp.stake == null ? '' : `${money(rp.per_hour)} / h on ${Math.round(rp.stake).toLocaleString('en-US')}${CUR.c}`;
const pct = (v, digits = 1) => v == null ? '<span class="dim">–</span>' : `<span class="${v > 0 ? 'up' : v < 0 ? 'down' : 'muted'}">${v > 0 ? '+' : ''}${(v * 100).toFixed(digits)}%</span>`;
const dur = h => h == null ? '–' : h < 1 ? Math.max(1, Math.round(h * 60)) + ' min' : h < 48 ? h.toFixed(1) + ' h' : Math.round(h / 24) + ' d';
// Minutes as people say them: "2 min", "1.5 min", never "2.000".
const mins = v => v == null || !isFinite(v) ? '–' : `${+(+v).toFixed(v >= 10 ? 0 : 1)} min`;
const plainPct = (v, digits = 0) => v == null ? '–' : (v * 100).toFixed(digits) + '%';
function ago(t) {
  const s = Date.now() / 1000 - t;
  return s < 5400 ? Math.max(1, Math.round(s / 60)) + ' min ago' : s < 172800 ? Math.round(s / 3600) + ' h ago' : Math.round(s / 86400) + ' d ago';
}
const icon = id => `<img loading="lazy" alt="" src="/icon?id=${encodeURIComponent(id)}" onerror="this.outerHTML='<span class=ph></span>'">`;
const itemCell = (r, sub) => `<div class="item">${icon(r.id)}<div style="min-width:0"><b>${esc(r.name)}</b>${sub ? `<span class="sub">${sub}</span>` : ''}</div></div>`;
// "29 Chaos for 9" (what you pay for how many) / "64 for 1 Divine" (how many you give for what).
const order = (o, buying) => buying
  ? `${o.currency_units} ${curWord(o.currency)} <span class="dim">for</span> ${o.item_units}`
  : `${o.item_units} <span class="dim">for</span> ${o.currency_units} ${curWord(o.currency)}`;
// Small icons for the buttons used most: weigh an offer, and the hideout window.
const BI = {
  check: '<svg class="bi" viewBox="0 0 24 24"><path d="M12 4v16M7 20h10M4 8h16M6.5 8l-3 6.5a3.2 3.2 0 006 0L6.5 8zM17.5 8l-3 6.5a3.2 3.2 0 006 0L17.5 8z"/></svg>',
  hideout: '<svg class="bi" viewBox="0 0 24 24"><path d="M4 5h16v14H4zM4 9h16M13 13h4v3h-4z"/></svg>',
};
const KIND = { chaos: 'Chaos market', divine: 'Divine market', cross: 'Chaos → Divine' };
// Currency icons in place of unit words. The letter or word stays in the page, so copying a price keeps
// its unit and the page still reads right if the icons cannot be fetched: style.css shows the icons
// only once <html> carries "icons", which it gets after both images have loaded (see showIcons).
const CUR = { c: '<span class="cur c" title="Chaos Orb"> c</span>', d: '<span class="cur d" title="Divine Orb"> d</span>' };
const curWord = name => name === 'Divine' ? `<span class="cur d word" title="Divine Orb">Divine</span>` : name === 'Chaos' ? `<span class="cur c word" title="Chaos Orb">Chaos</span>` : esc(name);
// A market as a badge: the Chaos market, the Divine market, or buying in one and selling in the other.
const marketTag = kind => {
  const ic = k => `<i class="ci ${k}"></i>`;
  const icons = kind === 'cross' ? `${ic('c')}<i class="ia">→</i>${ic('d')}` : kind === 'divine' ? ic('d') : kind === 'chaos' ? ic('c') : '';
  return `<span class="tag mk ${kind}" title="${esc(KIND[kind] || kind)}">${icons}<span class="kt">${esc(KIND[kind] || kind)}</span></span>`;
};
// Tried at start and again after each new hour (see poll): on a first run the item list that names
// the icons arrives with the first sync, and the letters should not stay until a reload.
let iconsTrying = false;
function showIcons() {
  const root = document.documentElement;
  if (iconsTrying || root.classList.contains('icons')) return;
  iconsTrying = true;
  const urls = ['Metadata/Items/Currency/CurrencyRerollRare', 'Metadata/Items/Currency/CurrencyModValues'].map(id => `/icon?id=${encodeURIComponent(id)}`);
  Promise.all(urls.map(u => new Promise((ok, no) => { const im = new Image(); im.onload = ok; im.onerror = no; im.src = u; })))
    .then(() => root.classList.add('icons'), () => { /* letters stay */ })
    .finally(() => { iconsTrying = false; });
}

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
  ledger: '<path d="M6 3h12v18l-3-2-3 2-3-2-3 2zM9 8h6M9 12h6"/>', ladder: '<path d="M7 3v18M17 3v18M7 7h10M7 12h10M7 17h10"/>',
};
const PAGES = [['desk', 'Desk'], ['markets', 'Markets'], ['flips', 'Flips'], ['arbitrage', 'Arbitrage'], ['ladder', 'Ladder'], ['league', 'League start'], ['lab', 'Backtest lab'], ['ledger', 'Ledger']];
// poe.ninja, quietly: on or off, and when its prices were last fetched.
function ninjaLine() {
  const n = state.status?.ninja;
  if (!n) return '';
  const what = state.practice ? 'not used in practice (its prices are today’s)' : !n.on ? 'off' : n.last_run ? `${n.from_cache ? 'last fetched' : 'fetched'} ${ago(n.last_run)}${n.errors?.length ? ' <span class="gold" title="' + esc(n.errors.join('; ')) + '">(last try failed)</span>' : ''}` : 'on, not fetched yet';
  return `<span title="Prices for card rewards the exchange does not trade">poe.ninja: ${what}</span><br>`;
}
// The menu can fold into a rail of icons, which leaves the tables more room. Labels and the footer
// stay a hover away, and the choice is remembered on this machine.
const railOn = () => { try { return localStorage.navRail === '1'; } catch { return false; } };
const FOLD = '<svg viewBox="0 0 24 24"><path d="M15 6l-6 6 6 6"/></svg>', INFO = '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/></svg>';
function renderNav(page) {
  const rail = railOn();
  $('#nav').classList.toggle('rail', rail);
  $('#nav').innerHTML = `<div class="brand"><img src="/mark.svg" alt=""><span>Vaal Street</span></div>
    ${PAGES.map(([id, label]) => `<a href="#/${id}" class="${id === page ? 'on' : ''}" aria-label="${label}"><svg viewBox="0 0 24 24">${ICONS[id]}</svg><span class="lb">${label}</span></a>`).join('')}
    <span class="spacer"></span>
    <button class="railbtn" id="railBtn" title="${rail ? 'Show the menu labels' : 'Fold the menu into icons'}" aria-label="${rail ? 'Show the menu labels' : 'Fold the menu into icons'}">${FOLD}<span class="lb">Fold the menu</span></button>
    <div class="footwrap"><button class="info" aria-label="Shortcuts and credits" title="Shortcuts and credits">${INFO}</button>
    <div class="foot"><kbd>/</kbd> search &nbsp; <kbd>H</kbd> hideout<br><kbd>C</kbd> check an offer<br><kbd>?</kbd> <a href="#" id="tourLink">tour of the desk</a><br>${ninjaLine()}Not affiliated with Grinding Gear Games.</div></div>`;
  $('#tourLink').onclick = e => { e.preventDefault(); openTour(); };
  $('#railBtn').onclick = () => { try { localStorage.navRail = rail ? '0' : '1'; } catch { /* stays as it is */ } renderNav(page); };
}
// The Divine price flashes green or red when a new hour moves it.
let lastBridge = { key: null, v: null };
function bridgeFlash(sm) {
  const key = `${state.league}|${state.practice ? 'practice' : 'live'}`, prev = lastBridge;
  lastBridge = { key, v: sm?.bridge ?? null };
  if (!sm || prev.key !== key || prev.v == null || sm.bridge == null || sm.bridge === prev.v) return '';
  return sm.bridge > prev.v ? 'flash-up' : 'flash-down';
}
function renderTop() {
  const s = state.status, sync = s.sync, sm = state.summary, flash = bridgeFlash(sm);
  const leagues = (s.leagues || []).filter(l => l.kind !== 'variant' || l.league === state.league).slice(0, 40);
  $('#top').innerHTML = `
    <select id="leagueSel" title="${state.practice ? 'Stop the practice to change league' : 'League'}" ${state.practice ? 'disabled' : ''}>${leagues.map(l => `<option value="${esc(l.league)}" ${l.league === state.league ? 'selected' : ''}>${esc(l.league)}${l.family ? ` · ${esc(familyWord(l.family))}` : ''}</option>`).join('')}</select>
    ${sm ? `<div class="stat orb"><i class="ci d big"></i><div><b${flash ? ` class="${flash}"` : ''}>${fmt(sm.bridge)}${CUR.c} ${pct(sm.bridge_change_24h)}</b><span>Divine Orb</span></div></div>
    <div class="stat" title="${sm.league_day == null ? 'The archive does not reach back to this league’s first hour.' : ''}"><b>Day ${sm.league_day ?? '–'}</b><span>of league</span></div>
    <div class="stat"><b>${compactMoney(sm.value_24h, sm.bridge)}</b><span>traded, 24h</span></div>
    ${state.practice ? `<div class="stat"><b class="violet">${esc(practiceTime(state.practice))}</b><span>practice hour</span></div>` : `<div class="stat"><b>${ago(sm.as_of + 3600)}</b><span>latest hour</span></div>`}` : ''}
    <span class="grow"></span>
    <div class="sync" title="${esc(sync.error || '')}"><span class="dot ${sync.error ? 'err' : sync.running ? 'busy' : ''}"></span>${esc(sync.error ? 'Sync failed' : sync.message)}</div>
    <div class="seg" id="unitSeg" title="Show prices in Chaos or Divine"><button class="${state.unit === 'chaos' ? 'on' : ''}" data-u="chaos" title="Prices in Chaos"><i class="ci c"></i><span class="kt">Chaos</span></button><button class="${state.unit === 'divine' ? 'on' : ''}" data-u="divine" title="Prices in Divine"><i class="ci d"></i><span class="kt">Divine</span></button></div>
    <button class="btn" id="hideoutBtn" title="Compact always-on-top view (H)">${BI.hideout}Hideout</button>`;
  $('#leagueSel').onchange = async e => { state.league = e.target.value; await api('/api/league', { league: state.league }); state.summary = null; route(); };
  $('#unitSeg').onclick = e => { const u = e.target.closest('[data-u]')?.dataset.u; if (u) { state.unit = localStorage.unit = u; route(); } };
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
      <div class="kpi"><span>Divine Orb</span><b><i class="ci d big"></i>${fmt(s.bridge)}${CUR.c}</b><i>${pct(s.bridge_change_24h)}</i></div>
      <div class="kpi"><span>League day</span><b>${s.league_day ?? '–'}</b></div>
      <div class="kpi"><span>Traded in 24h</span><b>${compactMoney(s.value_24h, s.bridge)}</b></div>
      <div class="kpi"><span>Items trading</span><b>${s.items}</b></div>
      <div class="kpi"><span>Latest hour</span><b>${ago(s.as_of + 3600)}</b></div>
    </div><div class="grid" id="g"></div>`;
  const g = $('#g', main);

  const flips = panel(+state.filters.capital > 0 ? `Best flips for ${fmt(+state.filters.capital)}${CUR.c}` : 'Best flips right now', 'c7', '<a href="#/flips">All flips →</a>');
  flips.append(table([
    { h: 'Item', l: 1, f: r => itemCell(r, `${marketTag(r.kind)}${flipWarn(r)}`) },
    { h: 'Buy order', f: r => order(r.buy_order, true), title: 'Currency you pay, for how many items' },
    { h: 'Sell order', f: r => order(r.sell_order, false), title: 'Items you give, for how much currency' },
    { h: 'Margin', f: r => pct(r.margin) },
    { h: 'Est. / hour', f: r => money(r.profit_per_hour) },
  ], f.flips.slice(0, 8), { onRow: r => openItem(r.id), empty: 'No market is liquid enough for a plan yet.' }));
  g.append(flips);

  const sets = panel(+state.filters.capital > 0 ? `Conversions within ${fmt(+state.filters.capital)}${CUR.c}` : 'Sets worth completing', 'c5', '<a href="#/arbitrage">All conversions →</a>');
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
  main.innerHTML = `<div class="head"><div><h1>Markets</h1><p>Every item that trades on the exchange. Price is the model's fair value; “vs Chaos” and “vs Divine” are what the two markets for the item are actually paying.</p></div><span class="grow"></span><button class="btn" id="tcBtn" title="Is an offer in the order window a good deal? (C)">${BI.check}Check an offer</button></div>
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
      { h: 'Price', f: r => `${money(r.price)}${fairRange(r)}`, s: r => r.price, title: 'Fair value, and how sure the model is of it (±). How sure the model is about the value now. Not a forecast: single trades often land outside.' },
      { h: '24h', f: r => pct(r.change_24h), s: r => r.change_24h },
      { h: '7d', f: r => pct(r.change_7d), s: r => r.change_7d },
      { h: 'vs Chaos', f: r => money(r.chaos), s: r => r.chaos, title: 'Typical price in the item-for-Chaos market' },
      { h: 'vs Divine', f: r => money(r.divine), s: r => r.divine, title: 'Typical price in the item-for-Divine market, at the current Divine price' },
      { h: 'Divine premium', f: r => pct(r.premium), s: r => r.premium, title: 'How much more the Divine market pays than the Chaos market' },
      { h: 'Traded 24h', f: r => money(r.value_24h), s: r => r.value_24h },
      { h: 'Bids vs offers', f: r => r.imbalance == null ? '–' : `<span class="meter" title="${r.imbalance > 0 ? 'More Chaos bidding than items offered' : 'More items offered than Chaos bidding'}"><i style="left:${r.imbalance > 0 ? 50 : 50 + r.imbalance * 50}%;width:${Math.abs(r.imbalance) * 50}%;background:var(--${r.imbalance > 0 ? 'up' : 'down'})"></i></span>`, s: r => r.imbalance, title: 'Resting Chaos bids against resting item offers' },
      { h: '3 days', f: r => spark(r.spark, r.change_24h >= 0 ? 'var(--up)' : 'var(--down)') },
    ], rows, { sort: 8, onRow: r => openItem(r.id), empty: 'No item matches these filters.' });
    t.addEventListener('click', async e => {
      const b = e.target.closest('[data-watch]'); if (!b) return;
      const row = d.market.find(r => r.id === b.dataset.watch); row.watched = !row.watched; b.classList.toggle('on', row.watched); if (row.watched) { b.classList.add('pop'); b.onanimationend = () => b.classList.remove('pop'); }
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

// "±0.3%" after a fair value: how sure the model is of it (one standard deviation), not where trades will land.
const halfRange = r => r.fair_low == null || r.fair_high == null || !r.price ? null : (r.fair_high - r.fair_low) / 2 / r.price;
const plusMinus = h => `±${(h * 100).toFixed(h < 0.01 ? 2 : 1)}%`;
function fairRange(r) {
  if (r.fair_low == null || r.fair_high == null || !r.price) return '';
  const half = (r.fair_high - r.fair_low) / 2 / r.price, unit = state.unit === 'divine' && state.bridge ? state.bridge : 1;
  return `<span class="dim small" title="Model sure within ${fmt(r.fair_low / unit)} to ${fmt(r.fair_high / unit)} ${unit === 1 ? 'Chaos' : 'Divine'}. How sure the model is about the value now. Not a forecast: single trades often land outside."> ${plusMinus(half)}</span>`;
}
// Where each Gold fee came from, in words: the fee schedule is not published, so it is learned or assumed.
const feeSource = r => (r.gold_sources || []).map(x => x.source === 'your trades' ? `from your trades (${x.trades})` : x.source === 'your assumption' ? 'your assumed fee' : 'unknown').filter((v, i, a) => a.indexOf(v) === i).join(', ');
const goldCell = (r, value) => r.gold == null
  ? `<span class="dim" title="No Gold fee known for ${esc((r.gold_sources || []).filter(x => x.source === 'unknown').map(x => x.id).join(', '))}. Record the Gold of a trade in the Ledger, or enter an assumed fee on the Flips page.">Gold unknown</span>`
  : `${value}<div class="muted small">${feeSource(r)}</div>`;
const flipCols = () => [
  { h: 'Item', l: 1, f: r => itemCell(r, `${marketTag(r.kind)}${flipWarn(r)}`), s: r => r.name },
  { h: 'Buy order', f: r => order(r.buy_order, true), s: r => r.buy, title: 'Currency you pay, for how many items' },
  { h: 'Sell order', f: r => order(r.sell_order, false), s: r => r.sell, title: 'Items you give, for how much currency' },
  { h: 'Margin', f: r => pct(r.margin), s: r => r.margin },
  { h: 'Volume beyond', f: r => plainPct(r.reach), s: r => r.reach, title: 'Share of the last two days’ volume that traded in hours averaging at or beyond each order level' },
  { h: 'Units / hour', f: r => `<span title="${esc(shareTitle(r))}">${fmt(r.units_per_hour)}${r.share_source?.startsWith('your fills') ? ' <span class="dim" title="Measured from your orders">✓</span>' : ''}</span>`, s: r => r.units_per_hour },
  { h: 'One lot every', f: r => `<span class="${r.slow ? 'gold' : ''}" title="${esc(lotTitle(r))}">${dur(r.hours_per_lot)}</span>`, s: r => r.hours_per_lot, title: 'Orders fill in whole lots. How long your share of the trade takes to fill the smallest lot these two markets allow' },
  { h: 'Est. / hour', f: r => money(r.profit_per_hour), s: r => r.profit_per_hour },
  { h: 'Last 7 days', f: r => replayCell(r.replay), s: r => r.replay?.vs_divine ?? r.replay?.return, title: replayTitle },
  { h: 'Capital', f: r => money(r.capital), s: r => r.capital, title: 'Roughly six hours of buying held as inventory' },
  { h: 'Bid queue', f: r => dur(r.bid_queue_hours), s: r => r.bid_queue_hours, title: 'Resting bids already in the buy market, in hours of that market’s volume' },
  { h: 'Offer queue', f: r => dur(r.ask_queue_hours), s: r => r.ask_queue_hours, title: 'Items already on offer in the sell market, in hours of that market’s volume' },
  { h: 'Per 1,000 Gold', f: r => goldCell(r, money(r.chaos_per_1000_gold)), s: r => r.chaos_per_1000_gold, title: 'Chaos earned for every 1,000 Gold the orders cost. Each order costs Gold for every item it buys or sells' },
  { h: 'Gold / hour', f: r => goldCell(r, fmt(r.gold_per_hour)), s: r => r.gold_per_hour, title: 'Gold the orders cost at the units per hour this plan expects' },
  { h: 'Minutes', f: r => `${mins(r.minutes)} <span class="dim">· ${r.orders} order${r.orders === 1 ? '' : 's'}</span>`, s: r => r.minutes, title: 'Your time to post this plan’s orders once, at the minutes per order you set' },
  { h: 'Per hour of your time', f: r => money(r.profit_per_own_hour), s: r => r.profit_per_own_hour, title: 'Est. / hour × 60 ÷ minutes: if you post one buy and one sell order for each hour the market runs' },
];
// Where a plan's share of the flow comes from: your recorded orders, or the share you set.
const shareTitle = r => r.share == null ? '' : `Assumes you get ${plainPct(r.share, 1)} of the flow: ${/^default \d+%$/.test(r.share_source || '') ? 'the share you set on this page' : r.share_source}`;
// A flip's last week, against holding Divines first: at a league start the Divine rises fast, so a
// gain in Chaos can be a loss next to it.
function replayCell(rp, sales = true) {
  if (!rp) return '<span class="dim">no fills</span>';
  const vs = rp.vs_divine == null ? '' : `${pct(rp.vs_divine, 0)} <span class="dim">vs Divines</span>`;
  const sold = rp.realized != null && rp.return != null && Math.abs(rp.realized - rp.return) > 0.005 ? ` · ${pct(rp.realized, 0)} sold` : '';
  const rate = replayRate(rp);
  return `${vs || pct(rp.return, 0)}<div class="muted small">${pct(rp.return, 0)} in Chaos${rate ? `, ${rate}` : ''}${sold} · Divines ${pct(rp.divines, 0)}${sales ? ` · ${rp.sales} sale${rp.sales === 1 ? '' : 's'}` : ''}</div>`;
}
const replayTitle = 'This plan’s rule replayed over the last 7 days on a 1,000 Chaos stake, re-posting every 4 hours. First: its result against simply holding Divines over the same hours, the one that counts. Below: its return in Chaos (inventory still held valued at fair value) and what that is per hour on the stake, the part already sold, and what Divines made. “Est. / hour” assumes as much capital as the markets absorb, so for a deep market it is far above the rate on 1,000 Chaos';
// A flip whose Divine market pays far more than its Chaos market: the order fills rarely.
// A flip whose one lot takes longer than a sitting to fill: slow, and it says how thin its market is.
const flipWarn = r => (r.warning ? ` <span class="gold" title="${esc(r.warning)}">⚠ rare fills</span>` : '')
  + (r.slow ? ` <span class="gold" title="${esc(r.slow)}">⚠ slow</span>` : '');
// The smallest lot a plan's markets allow: one item, or one Chaos or Divine worth of a cheaper one.
const lotTitle = r => r.lot_units == null ? '' : `The smallest lot is about ${Math.ceil(r.lot_units - 1e-9).toLocaleString('en-US')} item${r.lot_units > 1 ? 's' : ''}. ${r.slow || ''}`.trim();
const RANK = { profit: 7, gold: 12, own: 15 };

async function viewFlips(main) {
  const f = state.filters;
  if (!state.summary) await loadDesk();
  const d = await api(withLeague('/api/flips', { share: state.share, min_margin: f.minMargin, kind: f.kind, ...bankQuery(), ...(f.goldBudget > 0 ? { gold_budget: +f.goldBudget } : {}) }));
  state.bridge = d.summary.bridge; renderTop();
  const st = d.settings;
  main.innerHTML = `<div class="head"><div><h1>Flips</h1><p>Buy low in one market, sell high in the same or the other. Ranked by estimated profit per hour.</p></div></div>
    <p class="note">Each plan rests a buy order at a price the market's <b>hourly average</b> has dipped to, and a sell order at a price it has risen to — levels a stated share of the last two days' volume actually traded beyond. “Chaos → Divine” buys from people dumping loot for Chaos and sells to people paying Divines, who consistently pay more. Estimates assume you capture ${plainPct(state.share)} of the market's flow and ignore who is ahead of you in the queue. The “Last 7 days” replay does wait in line: its orders fill only after the stock already on offer has traded. Check the in-game book before sizing up.</p>
    <p class="note">Every order costs Gold, and the game does not publish how much. The desk uses the Gold per item from the trades you recorded in the Ledger; for other items it uses the fee you assume below, and otherwise says “Gold unknown”. Minutes assume ${+(+st.minutes_per_order).toFixed(2)} minute${st.minutes_per_order === 1 ? '' : 's'} per order — your guess, change it below.</p>
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
    $('#tbl', main).replaceChildren(table(flipCols(), rows, { sort: RANK[f.rankBy] ?? RANK.profit, onRow: r => openItem(r.id), empty: 'No plan clears these filters.' }));
    // The session plan is built in Python (desk.session_plan) from the same plans, before the search filter.
    const bank = +f.capital, host = $('#plan', main), sp = d.session;
    host.replaceChildren();
    if (!(bank > 0) || !sp) return;
    const picks = sp.flips, total = sp.expected_per_hour;
    // Kept so the Ledger can start a session against this plan.
    state.sessionPlan = { league: state.league, bankroll: bank, expected_per_hour: total, flips: picks.map(r => ({ id: r.id, name: r.name, kind: r.kind, stake: r.stake, est: r.est })) };
    const p = panel(`Session plan for ${fmt(bank)}${CUR.c}`, '', `<span class="muted small">last week: <b>${money(sp.replayed_vs_divine_per_hour)}</b> an hour against holding Divines, ${money(sp.replayed_per_hour)} an hour in Chaos · promised ${money(total)} an hour if every order fills as estimated · ${money(sp.deployed)} deployed${sp.gold_budget != null ? ` · ${fmt(sp.gold_used)} of ${fmt(sp.gold_budget)} Gold used` : ''} · about ${mins(sp.minutes)} to post</span>`);
    p.style.marginBottom = '14px';
    p.append(table([
      { h: 'Item', l: 1, f: r => itemCell(r, `${marketTag(r.kind)}${flipWarn(r)}`) },
      { h: 'Buy order', f: r => order(r.buy_order, true) }, { h: 'Sell order', f: r => order(r.sell_order, false) },
      { h: 'Margin', f: r => pct(r.margin) }, { h: 'Stake', f: r => `${money(r.stake)}${r.lot_share != null ? `<div class="gold small">one lot is ${plainPct(r.lot_share)} of your purse</div>` : ''}`, title: 'Chaos to keep working in this item: a quarter of the bankroll, or one whole lot if that is more' },
      { h: 'Smallest lot', f: r => money(r.lot_cost), title: 'What it takes to post the buy order and to hold enough for one sell order' },
      { h: 'Last week vs Divines', f: r => `${money(r.replayed_vs_divine_per_hour)}<span class="dim"> / h</span>`, title: 'What this plan made an hour over the last week at this stake, minus what the stake would have made held as Divines' },
      { h: 'Last week', f: r => `${money(r.replayed_per_hour)}<span class="dim"> / h</span>`, title: 'What this plan made an hour over the last week at this stake, in Chaos' },
      { h: 'Promised', f: r => `${money(r.est)}<span class="dim"> / h</span>`, title: 'What the plan expects if every order fills as estimated; it runs well ahead of the replay' },
      { h: 'Per 1,000 Gold', f: r => r.chaos_per_1000_gold == null ? '<span class="dim">Gold unknown</span>' : `${money(r.chaos_per_1000_gold)}<div class="muted small">${feeSource(r)}</div>` },
      { h: 'Gold to post', f: r => r.gold_to_post == null ? '–' : fmt(r.gold_to_post), title: 'Gold to post this plan’s buy and sell orders once, at the size of its stake' },
      { h: 'Gold / hour', f: r => r.gold_per_hour == null ? '–' : fmt(r.gold_per_hour), title: 'Gold an hour at the fills this stake expects, if you keep re-posting' },
      { h: 'Minutes', f: r => mins(r.minutes) },
      { h: 'Last 7 days', f: r => replayCell(r.replay, false), title: replayTitle },
    ], picks, { onRow: r => openItem(r.id), empty: sp.trailing_divines ? `Nothing beat holding Divines (${pct(sp.divines, 0)} over the last week): ${sp.trailing_divines} plan${sp.trailing_divines === 1 ? '' : 's'} made Chaos but less than the Divine rose.` : sp.gold_budget != null ? 'No plan with a known Gold fee fits this bankroll and Gold budget.' : 'No plan fits this bankroll with the current filters.' }));
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
      <div class="f ctl">Swaps per round trip<div class="seg" id="swaps">${[1, 2, 3].map(n => `<button type="button" data-n="${n}" class="${f.swaps === n ? 'on' : ''}" title="Round trips with up to ${n} swap${n === 1 ? '' : 's'}">up to ${n}</button>`).join('')}</div></div>
    </div><div class="grid" id="g"></div><datalist id="arbItems">${names.map(n => `<option value="${esc(n)}">`).join('')}</datalist>`;
  const fits = cost => !purse || f.fit === false || cost <= purse;
  const liquid = f.arbLiquidity ?? 2000;
  const sets = panel('Conversions', 'c12');
  sets.innerHTML += `<div class="body" style="padding-bottom:0"><p class="note">Fixed recipes: a full set of divination cards for its reward, a full stack of shards or splinters for the item they form, and three essences or oils of one tier sold to a vendor for one of the next (shown for up to three tiers at once), plus the recipes you entered below. “Margin” values both sides at fair value — what a patient trader gets. “In a hurry” buys the inputs in the dearer market and sells the result in the cheaper one. Try a recipe once in game before buying in bulk, then mark it with ✓ or ✗.</p></div>`;
  sets.append(table([
    { h: 'Turn in', l: 1, f: r => itemCell(r, `${r.inputs?.length > 1 ? r.inputs.map(x => `${+x.units} ${esc(x.name)}`).join(' + ') : r.need} → ${r.gives} ${esc(r.reward)}`), s: r => r.name },
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
  // Card sets whose reward the exchange does not trade: the reward's price is a poe.ninja listing, kept apart.
  const nj = panel('Card sets for items not on the exchange', 'c12', `<span class="muted small">Reward ${esc(d.ninja_credit || 'prices from poe.ninja')} (stash listings, not the exchange)</span>`);
  nj.insertAdjacentHTML('beforeend', `<div class="body" style="padding-bottom:0"><p class="note">These cards turn into a unique, a gem or a base type. The cards are priced from the exchange; the reward from poe.ninja’s listings, which are weaker evidence than trades, so there is no “in a hurry” figure and these prices never enter fair value, round trips or alerts. Check what the reward really sells for before buying a set.</p></div>`);
  nj.append(table([
    { h: 'Turn in', l: 1, f: r => itemCell(r, `${r.need} → ${r.gives} ${esc(r.reward)}`), s: r => r.name },
    { h: 'Cost', f: r => money(r.cost), s: r => r.cost, title: 'Fair value of the cards for one set, from the exchange' },
    { h: 'Reward listed at', f: r => money(r.proceeds), s: r => r.proceeds, title: 'poe.ninja’s listing price for the reward' },
    { h: 'Margin', f: r => pct(r.margin), s: r => r.margin },
    { h: 'Est. / hour', f: r => money(r.profit_per_hour), s: r => r.profit_per_hour, title: `At ${plainPct(state.share)} of the cards' traded volume` },
    { h: 'Listings', f: r => r.reward_listings ?? '–', s: r => r.reward_listings, title: 'How many listings back the reward’s price' },
    { h: 'Price age', l: 1, f: r => `${r.reward_age_minutes == null ? '–' : dur(r.reward_age_minutes / 60)}${r.reward_stale ? ' <span class="gold" title="poe.ninja could not be reached; this is the last answer">stale</span>' : ''}`, s: r => r.reward_age_minutes },
    { h: 'Note', l: 1, f: r => r.reward_variant ? `<span class="muted small">${esc(r.reward_variant)}</span>` : '' },
    { h: 'In game', l: 1, f: checkCell, s: r => r.check },
  ], (d.ninja_conversions || []).filter(r => fits(r.cost)), { sort: 4, limit: 15, onRow: r => openItem(r.id), empty: state.status?.ninja?.on === false ? 'poe.ninja prices are turned off (python -m vaalstreet ninja on turns them back on).' : 'No card set for an item off the exchange is priced yet. The desk asks poe.ninja at most once an hour, from the background sync.' }));
  onCheck(nj);
  const loops = panel('Round trips through item-for-item markets', 'c12');
  loops.innerHTML += `<div class="body" style="padding-bottom:0"><p class="note">Buy the first item${d.chaos_only ? ' for Chaos' : ''}, move it along the path, and sell the last one${d.chaos_only ? ' for Chaos' : ''}. A plain arrow is a swap in the small markets where items trade directly for each other; <span class="tag">Divine</span> means selling for Divines and buying with them; <span class="tag">vendor</span> means buying from a vendor at a price you entered below. Rates are what those markets typically traded at over the last day. These markets are thin. “Waiting now” is the value of orders already resting on the other side of the thinnest swap — when it is not nothing, the swap can be taken at once; otherwise you would have to post an order and wait. “Est. / day” assumes you take ${plainPct(state.share)} of what went through the thinnest step.</p></div>`;
  loops.append(table([
    { h: 'Path', l: 1, f: r => r.path.map((x, k) => `${k ? viaArrow((r.via || [])[k - 1]) : ''}<span style="white-space:nowrap">${icon(x.id).replace('<img', '<img style="width:18px;height:18px;vertical-align:middle"')} ${esc(x.name)}</span>`).join('') },
    { h: 'First swap', f: r => `${r.first_swap.give} <span class="dim">for</span> ${r.first_swap.get}`, title: 'Units of the first item you give, for units of the second' },
    { h: 'Buy at', f: r => money(r.buy), s: r => r.buy }, { h: 'Sell at', f: r => money(r.sell), s: r => r.sell },
    { h: 'Edge', f: r => r.edge > 3 ? `<span class="up">×${(1 + r.edge).toFixed(1)}</span>` : pct(r.edge, 0), s: r => r.edge },
    { h: 'Est. / hour', f: r => money(r.profit_per_hour), s: r => r.profit_per_hour, title: `At ${plainPct(state.share)} of what went through the thinnest step of the path in the last day` },
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
  $('#g', main).append(sets, nj, loops, mine);
  $('#cap', main).onchange = e => { f.capital = localStorage.bankroll = e.target.value; route(); };
  $('#fit', main).onchange = e => { f.fit = e.target.checked; route(); };
  $('#liq', main).onchange = e => { f.arbLiquidity = +e.target.value || 0; route(); };
  $('#swaps', main).onclick = e => { const n = +e.target.dataset.n; if (n) { f.swaps = n; localStorage.swaps = n; route(); } };
}

const unitOf = f => f(1).trim().slice(-1);
function leagueChart(host, curves, current, key, yFormat) {
  const names = Object.keys(curves).sort((a, b) => (a === current) - (b === current));
  let n = 0; const others = palette(), fair = css('--fair');
  const series = names.map(name => ({
    label: name, x: curves[name].map(p => p.day), y: curves[name].map(p => p[key]),
    color: name === current ? fair : others[n++ % others.length], width: name === current ? 2.4 : 1.3, alpha: name === current ? 1 : .8,
  }));
  drawChart(host, { series, xType: 'day', yFormat, tickFormat: tick(unitOf(yFormat)), log: true });
  return `<div class="legend">${series.slice().reverse().map(s => `<span><i style="background:${s.color}"></i>${esc(s.label)}</span>`).join('')}</div>`;
}

// A league family in words: "challenge league", "event", "Standard (Hardcore)".
function familyWord(f) {
  if (!f) return '';
  const [base, ...rest] = f.split('_');
  const head = { challenge: 'challenge league', event: 'event', private: 'private league', standard: 'Standard' }[base] || base;
  return rest.length ? `${head} (${rest.map(x => x[0].toUpperCase() + x.slice(1)).join(' ')})` : head;
}
const times = x => x == null ? '–' : `${+(+x).toFixed(x >= 10 ? 0 : 1)}×`;
const MOVE = { rise: '<span class="up">rose</span>', fall: '<span class="down">fell</span>', flat: '<span class="muted">flat</span>' };
const GAP = {
  structural: ['gold', 'Steady', 'Held on most days with the same sign, at least 3%: a real difference between the leagues'],
  noise: ['muted', 'Noise', 'Flips sign or closes from day to day: it will not hold'],
  none: ['dim', 'None', 'Within 3%: no real gap'],
};
// The League start sections added for launch day, Divine timing, meta shifts and other leagues.
async function leagueExtras(g, d) {
  const fam = familyWord(d.family);
  const L = d.launch;
  if (L) {
    const lp = panel(`Launch day: the first ${L.hours} hours of past ${esc(fam)}s`, 'c12', `<span class="muted small">Moves against each league’s first-day value, in ${L.unit === 'divine' ? 'Divine' : 'Chaos'}</span>`);
    lp.insertAdjacentHTML('beforeend', `<div class="body" style="padding-bottom:0"><p class="note">For each item: the hour its market first traded (“wakes up”), what it did first (rose or fell 5% from its first three traded hours, or stayed flat), and where it stood at hours ${L.checks.join(', ')} against its first-day value. Medians over ${L.precedents.length ? esc(L.precedents.join(', ')) : 'no past league'}. ${L.shortfall ? `<b>${esc(L.shortfall)}</b>` : ''}</p></div>`);
    lp.append(table([
      { h: 'Item', l: 1, f: r => itemCell(r, esc(r.category)), s: r => r.name },
      { h: 'Wakes up', f: r => r.wake == null ? '–' : `hour ${r.wake}`, s: r => r.wake, title: 'Median hour after launch when its market first traded' },
      { h: 'First move', l: 1, f: r => MOVE[r.first] || esc(r.first), s: r => r.first },
      ...L.checks.map(h => ({ h: `At ${h} h`, f: r => pct(r.moves?.[h], 0), s: r => r.moves?.[h], title: `Against its first-day value, ${h} hours after launch` })),
      { h: 'Leagues agree', f: r => r.n ? `${Math.round(r.agree * r.n)} of ${r.n}` : '–', s: r => r.agree, title: 'How many past leagues made the same first move' },
    ], L.items || [], { sort: 2 + L.checks.length, limit: 30, onRow: r => openItem(r.id), empty: 'No past league’s first hours are on disk.' }));
    g.append(lp);
  }
  const dt = d.divine_timing;
  if (dt) {
    const tp = panel('When to buy Divines', 'c6');
    tp.insertAdjacentHTML('beforeend', `<div class="body"><p style="margin:0 0 10px">${esc(dt.advice || `No past ${fam} to learn from yet.`)}</p><div class="legend"><span><i style="background:var(--divine)"></i>How much dearer than its cheapest day the Divine was, median over past leagues</span></div><div id="dtc"></div></div>`);
    g.append(tp);
    if (dt.curve?.length) drawChart($('#dtc', tp), { series: [{ label: 'Dearer than its cheapest day', color: css('--divine'), x: dt.curve.map(c => c.day), y: dt.curve.map(c => c.dearer), width: 2 }], xType: 'day', yFormat: x => `+${(x * 100).toFixed(0)}%`, height: 200 });
  }
  const m = d.meta;
  if (m) {
    const mp = panel('Meta shifts: demand growing faster than ever before', 'c6');
    mp.insertAdjacentHTML('beforeend', `<div class="body" style="padding-bottom:0"><p class="note">Units traded in the last 24 hours against league day 1, next to the same hours in past ${esc(fam)}s. Grown at least 1.5× and faster than in any past league: players may be using it for something new. ${m.note ? esc(m.note) : ''}</p></div>`);
    mp.append(table([
      { h: 'Item', l: 1, f: r => itemCell(r, esc(r.category)), s: r => r.name },
      { h: 'Demand grew', f: r => times(r.growth), s: r => r.growth },
      { h: 'Past leagues', f: r => `${times(r.past_low)} <span class="dim">/</span> ${times(r.past_median)} <span class="dim">/</span> ${times(r.past_high)}`, title: 'Lowest / median / highest growth over the same hours in past leagues' },
      { h: 'Leagues', f: r => r.n, s: r => r.n },
    ], m.items || [], { sort: 1, onRow: r => openItem(r.id), empty: 'No item’s demand is growing faster than in past leagues.' }));
    g.append(mp);
  }
  const cp = panel('Other leagues’ prices', 'c12', '<span class="muted small">Same item, same hours, in each league’s own Divines; a gap is not a trade, since items cannot move between leagues</span>');
  g.append(cp);
  try {
    const x = await api(withLeague('/api/cross_league'));
    const rows = [...(x.divine || []), ...(x.items || [])];
    cp.insertAdjacentHTML('beforeend', `<div class="body" style="padding-bottom:0"><p class="note">Over the last ${x.days} days, compared with ${x.others.map(esc).join(', ') || 'no other league'}. “Steady” gaps held on most days with the same sign: trust them when you read prices from that league. “Noise” gaps flip or close.</p></div>`);
    cp.append(table([
      { h: 'Item', l: 1, f: r => itemCell(r, marketTag(r.market)), s: r => r.name },
      { h: 'Other league', l: 1, f: r => `${esc(r.other)}<div class="muted small">${esc(familyWord(r.family))}</div>`, s: r => r.other },
      { h: 'There it costs', f: r => pct(r.gap, 0), s: r => r.gap, title: 'How much more (or less) it costs in the other league, in that league’s Divines, typical day' },
      { h: 'Gap', l: 1, f: r => { const [c, w, t] = GAP[r.verdict] || ['dim', esc(r.verdict), '']; return `<span class="${c}" title="${t}">${w}</span>`; }, s: r => ({ structural: 2, noise: 1 })[r.verdict] || 0 },
      { h: 'Same sign', f: r => plainPct(r.same_sign), title: 'Share of days the gap kept the same direction' },
      { h: 'Days', f: r => r.days },
    ], rows, { sort: 3, limit: 40, onRow: r => openItem(r.id), empty: 'No other league traded these items in the same hours.' }));
  } catch (err) { cp.insertAdjacentHTML('beforeend', `<div class="empty">${esc(err.message)}</div>`); }
}

async function viewLeague(main) {
  const v = state.league_view;
  const d = await api(withLeague('/api/league', { horizon: v.horizon, unit: v.unit }));
  if (!state.summary) await loadDesk();
  renderTop();
  const past = d.precedents, unitWord = v.unit === 'divine' ? 'Divine' : 'Chaos';
  const st0 = state.status, shallow = st0.first_hour > st0.archive_start + 86400;
  main.innerHTML = `<div class="head"><div><h1>League start</h1><p>${d.truncated ? `The archive does not reach back to the start of ${esc(d.league)}, so its league days cannot be counted yet.` : `${esc(d.league)} is on day ${d.day}.`} It is a ${esc(familyWord(d.family))}, so it is lined up only against past ${esc(familyWord(d.family))}s that have ended${past.length ? `: ${past.map(esc).join(', ')}.` : ', and there is none in the archive yet.'}</p></div></div>
    ${shallow ? `<p class="note">History on disk starts ${new Date(st0.first_hour * 1000).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })}. The exchange has published every hour since July 2024; the full archive is about 1.5 GB and takes one to two hours to download, in the background, newest first. <button class="btn" id="bf" ${st0.sync.backfill ? 'disabled' : ''}>${st0.sync.backfill ? 'Downloading…' : 'Download full history'}</button></p>` : ''}
    <div class="grid" id="g"></div>`;
  const g = $('#g', main);
  if ($('#bf', main)) $('#bf', main).onclick = async e => { e.target.disabled = true; e.target.textContent = 'Downloading…'; await api('/api/sync', { backfill: true }); toast('Downloading the full history in the background. Pages refresh as leagues arrive.'); };
  const dv = panel('Divine Orb in Chaos, by league day', 'c12', `<div class="seg" id="dvf" title="Which leagues to draw"><button data-f="family" class="${v.allLeagues ? '' : 'on'}">Same family</button><button data-f="all" class="${v.allLeagues ? 'on' : ''}">All leagues</button></div>`);
  const outlook = d.truncated ? '' : (d.divine_outlook || []).map(o => `<span style="margin-right:18px">next ${o.horizon} days: <b class="num">${pct(o.median, 0)}</b> <span class="dim">(${(o.worst * 100).toFixed(0)}% to ${o.best > 0 ? '+' : ''}${(o.best * 100).toFixed(0)}%, ${o.n} league${o.n === 1 ? '' : 's'})</span></span>`).join('');
  dv.innerHTML += `<div class="body">${outlook ? `<div class="muted" style="margin-bottom:10px">From day ${d.day} in past leagues, a Divine went on to cost — ${outlook}</div>` : ''}<div id="lg"></div><div id="dvc"></div></div>`;
  g.append(dv);
  const keep = new Set([d.league, ...past]);
  const curves = v.allLeagues ? d.divine : Object.fromEntries(Object.entries(d.divine).filter(([name]) => keep.has(name)));
  $('#lg', dv).innerHTML = leagueChart($('#dvc', dv), curves, d.league, 'price', x => fmt(x) + ' c');
  $('#dvf', dv).onclick = e => { const f = e.target.dataset.f; if (f) { v.allLeagues = f === 'all'; route(); } };

  const moneyU = x => x == null ? '–' : v.unit === 'divine' ? `${fmt(x)}${CUR.d}` : `${fmt(x)}${CUR.c}`;
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
  g.prepend(rehearsePanel());
  await leagueExtras(g, d);
}

const STRATS = {
  swing: { label: 'Swing maker', text: 'Rest buys where the Chaos market dips, offer the inventory where the sell market rises. This is the Flips board, replayed.', params: { reach: [0.25, 'Share of hours each order level is reached'], top: [40, 'Most-traded items to quote'], sell_leg: ['b', 'Sell into', { b: 'the Divine market', r: 'the Chaos market' }], min_margin: [0.03, 'Skip plans below this margin'], per_item: [0.05, 'Max share of bankroll per item'], stop_days: [2, 'Dump inventory unsold after this many days'] } },
  hold: { label: 'Buy and hold', text: 'Buy an equal-weight basket of the most traded items after the first day and hold. The benchmark.', params: { top: [20, 'Items in the basket'] } },
  reversion: { label: 'Dip buyer (taker)', text: 'Pay the spread to buy prints far below the model, sell when they normalise.', params: { entry_z: [-2, 'Buy below this surprise score'], exit_z: [0, 'Sell above this score'], top: [40, 'Most-traded items'], per_item: [0.05, 'Max share of bankroll per item'] } },
  momentum: { label: 'Momentum (taker)', text: 'Once a day, hold the liquid items that rose most.', params: { lookback: [72, 'Hours of history ranked'], hold: [10, 'Items held'], top: [60, 'Most-traded items considered'] } },
};
const FILL_MODELS = {
  queue: ['Wait in line', 'Your order waits behind the stock already on offer on your side, then takes at most the share of each hour’s volume set below.'],
  share: ['Share of volume', 'A resting order takes the share of each hour’s volume set below from the start, as if no one were ahead of you.'],
};
const paramsOf = m => {
  const out = {};
  for (const [k, [def]] of Object.entries(STRATS[m.strategy].params)) { const raw = m.params[k] ?? def; out[k] = typeof def === 'number' ? +raw : String(raw); }
  return out;
};
function download(name, data) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 1) + '\n'], { type: 'application/json' }));
  const a = document.createElement('a'); a.href = url; a.download = name; document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
const LAB_MODES = `<div class="seg" id="labMode"><button data-mode="run">Run a backtest</button><button data-mode="search">Search settings</button></div>`;
const SCORE_WORDS = { divine: 'Return over holding Divines', return: 'Return in Chaos', drawdown: 'Return over the worst drop' };
// "0.2, 0.3" -> [0.2, 0.3]; words stay words.
const gridValues = text => text.split(',').map(x => x.trim()).filter(Boolean).map(x => (x !== '' && isFinite(+x) ? +x : x));
const settingText = (s, strategy) => Object.entries(s || {}).map(([k, v]) => { const p = STRATS[strategy]?.params[k]; return `${esc(p ? p[1].toLowerCase() : STRATS_PARAM_WORDS[k] || k)}: ${esc(p?.[2]?.[v] ?? v)}`; }).join(', ') || 'the defaults';
const STRATS_PARAM_WORDS = { attention: 'hours between visits' };

// Strategy search: try a small grid, judged on leagues the settings were not chosen on.
async function viewSearch(main, lab) {
  const s = lab.search ||= { strategy: 'swing', grid: {}, attention: '', leagues: [], score: 'divine', capital: 10000, result: null };
  const leagues = state.status.leagues.filter(l => l.family === 'challenge' && l.hours >= 72);
  const sdef = STRATS[s.strategy];
  main.innerHTML = `<div class="head"><div><h1>Backtest lab</h1><p>Search a few settings of a built-in strategy, judged the way you would have used them: each league is scored with the setting that did best on leagues that had ended before it began.</p></div><span class="grow"></span>${LAB_MODES}</div>
    <p class="note">The desk tries at most 12 settings and 48 backtests per search, because it waits for the answer; use <code>search</code> on the command line for bigger grids. The more settings you try, the better the best one looks by luck, which is why only results on later leagues count.</p>
    <div class="grid"><section class="panel c4"><header><h2>Search</h2></header><form class="body" id="sform" style="display:grid;gap:10px" autocomplete="off">
      <label class="f">Strategy<select name="strategy">${Object.entries(STRATS).map(([k, x]) => `<option value="${k}" ${k === s.strategy ? 'selected' : ''}>${x.label}</option>`).join('')}</select></label>
      <div class="muted small">${sdef.text} Give each setting one value or several, separated by commas; leave it empty to keep its default.</div>
      ${Object.entries(sdef.params).map(([k, [def, text, options]]) => options
        ? `<div class="f ctl">${text} <span class="dim">(none ticked: ${esc(options[def])})</span><div>${Object.entries(options).map(([v, l]) => `<label style="display:inline-flex;gap:5px;margin:2px 10px 2px 0;color:var(--text)"><input type="checkbox" data-go="${k}" value="${esc(v)}" ${(s.grid[k] || []).includes(v) ? 'checked' : ''}>${esc(l)}</label>`).join('')}</div></div>`
        : `<label class="f">${text} <span class="dim">(default ${esc(def)})</span><input type="text" data-g="${k}" value="${esc((s.grid[k] || []).join(', '))}" placeholder="e.g. ${esc(def)}"></label>`).join('')}
      <label class="f">Hours between visits <span class="dim">(a run option, default 1)</span><input type="text" data-g="attention" value="${esc((s.grid.attention || []).join(', '))}" placeholder="e.g. 1, 4"></label>
      <div class="f ctl">Leagues <span class="dim">(none ticked: every challenge league)</span><div>${leagues.map(l => `<label style="display:inline-flex;gap:5px;margin:2px 10px 2px 0;color:var(--text)"><input type="checkbox" data-l="${esc(l.league)}" ${s.leagues.includes(l.league) ? 'checked' : ''}>${esc(l.league)}</label>`).join('')}</div></div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
        <label class="f">Score<select name="score">${Object.entries(SCORE_WORDS).map(([k, l]) => `<option value="${k}" ${k === s.score ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
        <label class="f">Bankroll (c)<input type="number" name="capital" value="${s.capital}" style="width:auto"></label>
      </div>
      <div id="serr" class="down small" role="alert"></div>
      <button class="btn primary">Search</button></form></section>
    <section class="panel c8"><header><h2>Results on later leagues</h2><span class="grow"></span><button class="btn" id="sexp" ${s.result ? '' : 'hidden'} title="Save the setting it would pick next, with this table, as a strategy file. Nothing is uploaded.">Export</button></header><div class="body" id="sout"><div class="empty">Enter a few values and search.</div></div></section></div>`;
  $('#labMode', main).querySelector(`[data-mode=search]`).classList.add('on');
  $('#labMode', main).onclick = e => { const m = e.target.dataset.mode; if (m) { lab.mode = m; route(); } };
  const form = $('#sform', main);
  const read = () => {
    s.strategy = form.elements.strategy.value; s.score = form.elements.score.value; s.capital = form.elements.capital.value;
    s.grid = {}; form.querySelectorAll('[data-g]').forEach(i => { const v = gridValues(i.value); if (v.length) s.grid[i.dataset.g] = v; });
    form.querySelectorAll('[data-go]:checked').forEach(c => (s.grid[c.dataset.go] ||= []).push(c.value));
    s.leagues = [...form.querySelectorAll('[data-l]:checked')].map(c => c.dataset.l);
  };
  form.elements.strategy.onchange = () => { s.strategy = form.elements.strategy.value; s.grid = {}; route(); };
  const show = () => {
    const r = s.result, out = $('#sout', main);
    if (!r) return;
    const p = r.pooled;
    out.innerHTML = `<div class="kpi" style="margin-bottom:12px"><span>Settings tried</span><b>${r.settings_tried}</b><i class="muted">${r.backtests} backtests in ${+(+r.seconds).toFixed(1)} s · score: ${esc(SCORE_WORDS[r.score] || r.score)}</i></div>
      <div id="stbl"></div>
      <p class="muted" style="margin:10px 0 0">Average over ${p.folds} later league${p.folds === 1 ? '' : 's'}: <b class="num">${pct(p.score)}</b> with the chosen settings, against <b class="num">${pct(p.default_score)}</b> with the defaults; better than the defaults in ${p.beat_default} of ${p.folds}. ${r.stability.of ? `The choice changed ${r.stability.changes} time${r.stability.changes === 1 ? '' : 's'} in ${r.stability.of} steps.` : ''}</p>
      <p class="muted" style="margin:6px 0 0">For the next league it would pick ${settingText(r.next.setting, r.strategy)}, chosen on ${r.next.tuned_on.map(esc).join(', ')}.</p>`;
    $('#stbl', out).append(table([
      { h: 'League', l: 1, f: f => esc(f.league) },
      { h: 'Chosen on', l: 1, f: f => `<span class="muted">${f.tuned_on.map(esc).join(', ')}</span>` },
      { h: 'Setting', l: 1, f: f => settingText(f.setting, r.strategy) },
      { h: 'Score there', f: f => pct(f.score), title: 'The chosen setting on a league it was not chosen on' },
      { h: 'Defaults', f: f => pct(f.default_score), title: 'The strategy’s default settings on the same league' },
      { h: 'Holding Divines', f: f => pct(f.divine_return), title: 'What the same Chaos made held as Divine Orbs' },
      { h: 'Return', f: f => pct(f.return), title: 'The chosen setting’s return in Chaos' },
      { h: 'Fit on earlier leagues', f: f => `<span class="dim">${pct(f.fit_on_earlier).replace(/class="(up|down|muted)"/, 'class="dim"')}</span>`, title: 'The best score on the leagues it was chosen on: the best of many tries, so it flatters' },
    ], r.folds));
  };
  form.onsubmit = async e => {
    e.preventDefault(); read();
    const err = $('#serr', form), btn = form.querySelector('button.primary');
    err.textContent = ''; btn.disabled = true; btn.textContent = 'Searching…';
    try {
      s.result = await api('/api/search', { strategy: s.strategy, grid: s.grid, leagues: s.leagues.length ? s.leagues : null, score: s.score, capital: +s.capital });
      $('#sexp', main).hidden = false; show();
    } catch (x) { err.textContent = x.message; }
    btn.disabled = false; btn.textContent = 'Search';
  };
  $('#sexp', main).onclick = async () => {
    try {
      const doc = await api('/api/strategy_export', { strategy: s.result.strategy, walk_forward: s.result });
      download(`${s.result.strategy}-search.json`, doc);
      toast('Saved the setting it would pick next, with its results, to your downloads. Nothing was uploaded.');
    } catch (x) { toast(x.message, true); }
  };
  show();
}

async function viewLab(main) {
  if (!state.summary) await loadDesk();
  renderTop();
  const leagues = state.status.leagues.filter(l => l.kind === 'challenge' && l.hours >= 72);
  const lab = state.lab ||= { members: [{ strategy: 'swing', params: {} }], leagues: [state.league], capital: 10000, participation: 0.1, slippage: 0.03, attention: 4, start_day: 0, days: '', whole_lots: false, gold_per_unit: '', gold_budget: '', schedule: '', fill_model: 'queue', results: [], ran: null };
  if (lab.mode === 'search') return viewSearch(main, lab);
  const several = lab.members.length > 1;
  const member = (m, n) => {
    const sdef = STRATS[m.strategy];
    return `<div class="member" data-m="${n}">
      <div style="display:flex;gap:8px;align-items:flex-end"><label class="f" style="flex:1">${several ? `Strategy ${n + 1}` : 'Strategy'}<select data-ms="${n}">${Object.entries(STRATS).map(([k, s]) => `<option value="${k}" ${k === m.strategy ? 'selected' : ''}>${s.label}</option>`).join('')}</select></label>
      ${several ? `<button type="button" class="btn ghost" data-mrm="${n}" title="Take this strategy out of the portfolio">✕</button>` : ''}</div>
      <div class="muted small">${sdef.text}</div>
      ${Object.entries(sdef.params).map(([k, [def, text, options]]) => options
        ? `<label class="f">${text}<select data-p="${k}">${Object.entries(options).map(([v, l]) => `<option value="${v}" ${(m.params[k] ?? def) === v ? 'selected' : ''}>${l}</option>`).join('')}</select></label>`
        : `<label class="f">${text}<input type="text" data-p="${k}" value="${esc(m.params[k] ?? def)}"></label>`).join('')}</div>`;
  };
  main.innerHTML = `<div class="head"><div><h1>Backtest lab</h1><p>Replay a strategy hour by hour over past leagues. Orders posted at hour t meet hour t+1's market; nothing sees the future.</p></div><span class="grow"></span>${LAB_MODES}</div>
    <p class="note">A resting order fills at its limit only in hours whose average price traded through it; an aggressive order pays the average plus slippage. Results are in Chaos, on a fixed bankroll. Gold is charged only at the fee per unit you enter, because the real fee for each item is not known, so treat maker results as a ceiling.</p>
    <div class="grid"><section class="panel c4"><header><h2>Setup</h2></header><div class="body" id="form" style="display:grid;gap:10px">
      ${lab.members.map(member).join('')}
      <button type="button" class="btn ghost" id="madd" style="justify-self:start" title="Run several strategies together on one bankroll">+ Add a strategy to run alongside</button>
      ${several ? '<div class="muted small">Together they share one bankroll, one Gold budget and each hour’s volume. Strategy 1 fills first every hour, the next ones from what is left.</div>' : ''}
      <div class="f ctl">Leagues<div>${leagues.map(l => `<label style="display:inline-flex;gap:5px;margin:2px 10px 2px 0;color:var(--text)"><input type="checkbox" data-l="${esc(l.league)}" ${lab.leagues.includes(l.league) ? 'checked' : ''}>${esc(l.league)}</label>`).join('')}</div></div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
        <label class="f">Bankroll (c)<input type="number" data-o="capital" value="${lab.capital}" style="width:auto"></label>
        <label class="f">Hours between visits<input type="number" data-o="attention" value="${lab.attention}" min="1" style="width:auto"></label>
        <label class="f">Max share of hourly volume<input type="number" data-o="participation" value="${lab.participation}" step="0.01" style="width:auto"></label>
        <label class="f">Slippage when taking<input type="number" data-o="slippage" value="${lab.slippage}" step="0.01" style="width:auto"></label>
        <label class="f">Start on league day<input type="number" data-o="start_day" value="${lab.start_day}" min="0" style="width:auto"></label>
        <label class="f">Run for days<input type="number" data-o="days" value="${lab.days}" placeholder="to the end" style="width:auto"></label>
        <label class="f" title="Charged for every unit you post, including re-posts">Gold per unit<input type="number" data-o="gold_per_unit" value="${lab.gold_per_unit}" min="0" step="any" placeholder="none" style="width:auto"></label>
        <label class="f" title="Once spent, no new orders go up; standing orders can still fill">Gold budget<input type="number" data-o="gold_budget" value="${lab.gold_budget}" min="0" step="1000" placeholder="no limit" style="width:auto"></label>
      </div>
      <label class="f">When you play (UTC, end hour not included)<input type="text" data-o="schedule" value="${esc(lab.schedule)}" placeholder="e.g. mon-fri 19-22, sat-sun 10-18"></label>
      <div class="f ctl">How resting orders fill<div class="seg" id="fm">${Object.entries(FILL_MODELS).map(([k, [l]]) => `<button type="button" data-fm="${k}" class="${lab.fill_model === k ? 'on' : ''}">${l}</button>`).join('')}</div><span class="muted small" style="color:var(--muted)">${FILL_MODELS[lab.fill_model][1]}</span></div>
      <label class="f" style="flex-direction:row;align-items:center;gap:6px;color:var(--text)"><input type="checkbox" data-o="whole_lots" ${lab.whole_lots ? 'checked' : ''}> Whole lots: trade whole-number ratios your cash can pay for, like a real purse</label>
      <div id="err" class="down small" role="alert"></div>
      <button class="btn primary" id="run">Run backtest</button></div></section>
    <section class="panel c8" id="res"><header><h2>Results</h2><span class="grow"></span><button class="btn" id="exp" hidden title="Save this strategy, its settings and its results as a file you can share. Nothing is uploaded.">Export</button></header><div class="body" id="out"><div class="empty">Pick a strategy and run it.</div></div></section></div>`;
  $('#labMode', main).querySelector('[data-mode=run]').classList.add('on');
  $('#labMode', main).onclick = e => { const m = e.target.dataset.mode; if (m) { lab.mode = m; route(); } };
  const form = $('#form', main);
  form.querySelectorAll('[data-m]').forEach(box => {
    const m = lab.members[+box.dataset.m];
    box.querySelectorAll('[data-p]').forEach(i => i.onchange = () => { m.params[i.dataset.p] = i.value; });
  });
  form.querySelectorAll('[data-ms]').forEach(s => s.onchange = () => { lab.members[+s.dataset.ms] = { strategy: s.value, params: {} }; route(); });
  form.querySelectorAll('[data-mrm]').forEach(b => b.onclick = () => { lab.members.splice(+b.dataset.mrm, 1); route(); });
  $('#madd', form).onclick = () => { lab.members.push({ strategy: 'hold', params: {} }); route(); };
  $('#fm', form).onclick = e => { const k = e.target.dataset.fm; if (k) { lab.fill_model = k; route(); } };
  form.querySelectorAll('[data-o]').forEach(i => i.onchange = () => { lab[i.dataset.o] = i.type === 'checkbox' ? i.checked : i.value; });
  form.querySelectorAll('[data-l]').forEach(i => i.onchange = () => { lab.leagues = [...form.querySelectorAll('[data-l]:checked')].map(c => c.dataset.l); });
  const options = () => ({ capital: +lab.capital, participation: +lab.participation, slippage: +lab.slippage, attention: +lab.attention, start_day: +lab.start_day, days: lab.days || null,
    whole_lots: !!lab.whole_lots, gold_per_unit: +lab.gold_per_unit || 0, gold_budget: lab.gold_budget === '' ? null : +lab.gold_budget, schedule: lab.schedule.trim() || null, fill_model: lab.fill_model });
  const show = () => {
    const out = $('#out', main);
    $('#exp', main).hidden = !(lab.results.length && lab.ran && lab.ran.members.length === 1);
    if (!lab.results.length) return;
    out.innerHTML = `<div id="rt"></div><div class="legend" style="margin-top:14px" id="rl"></div><div id="rc"></div><div id="tops" class="small muted" style="margin-top:8px"></div><div id="mix" style="margin-top:12px"></div>`;
    const pick = r => {
      const s = r.summary;
      $('#rl', out).innerHTML = `<span><i style="background:var(--fair)"></i>${esc(s.strategy.split(' + ').map(k => STRATS[k]?.label || k).join(' + '))} on ${esc(s.league)}</span><span><i style="background:var(--divine)"></i>Same Chaos held as Divine Orbs</span>`;
      drawChart($('#rc', out), { series: [{ label: 'Strategy', color: css('--fair'), x: r.t, y: r.equity, width: 2 }, { label: 'Hold Divine', color: css('--divine'), x: r.t, y: r.divine, width: 1.3 }], yFormat: x => compact(x) + ' c', tickFormat: tick('c'), log: true, height: 280 });
      $('#tops', out).innerHTML = s.top_items.length ? 'Biggest contributors: ' + s.top_items.map(t => `${esc(t.name)} <span class="num ${t.profit >= 0 ? 'up' : 'down'}">${t.profit >= 0 ? '+' : ''}${compact(t.profit)}</span>`).join(' · ') : '';
      const mix = $('#mix', out); mix.replaceChildren();
      if (s.strategies?.length) {
        mix.insertAdjacentHTML('beforeend', `<h2 style="margin-bottom:6px">Each strategy on ${esc(s.league)}</h2>`);
        mix.append(table([
          { h: 'Strategy', l: 1, f: m => esc(STRATS[m.strategy]?.label || m.strategy) },
          { h: 'Profit', f: m => `<span class="${m.profit >= 0 ? 'up' : 'down'}">${fmt(m.profit)}${CUR.c}</span>` },
          { h: 'Share of profit', f: m => plainPct(m.profit_share, 0) },
          { h: 'Gold spent', f: m => fmt(Math.round(m.gold_spent)) },
          { h: 'Most tied up', f: m => fmt(m.peak_capital) + ' c', title: 'The most Chaos this strategy had in items at once' },
          { h: 'Average tied up', f: m => fmt(m.average_capital) + ' c' },
          { h: 'Worst drop', f: m => fmt(m.peak_risk) + ' c', title: 'What its holdings could lose on a bad day (1 in 20), at the worst hour' },
          { h: 'Fills', f: m => m.fills.toLocaleString('en-US') },
        ], s.strategies));
      }
    };
    $('#rt', out).replaceChildren(table([
      { h: 'League', l: 1, f: r => esc(r.summary.league), s: r => r.summary.league },
      { h: 'Days', f: r => r.summary.days }, { h: 'Return', f: r => pct(r.summary.return, 0), s: r => r.summary.return },
      { h: 'Against Divines', f: r => pct(r.summary.vs_divine, 0), s: r => r.summary.vs_divine, title: 'The return measured in Divines: what the strategy made beyond simply holding Divine Orbs. The one to judge it by' },
      { h: 'Hold Divine', f: r => pct(r.summary.divine_return, 0), title: 'The same Chaos converted to Divine Orbs after day one' },
      { h: 'Per hour', f: r => fmt(r.summary.per_hour) + ' c', s: r => r.summary.per_hour, title: 'Chaos made per hour of the run, on the bankroll you set. Always in that league’s Chaos' },
      { h: 'Per hour vs Divines', f: r => r.summary.vs_divine_per_hour == null ? '–' : fmt(r.summary.vs_divine_per_hour) + ' c', s: r => r.summary.vs_divine_per_hour, title: 'Chaos made per hour beyond simply holding Divines' },
      { h: 'Per day', f: r => fmt(r.summary.per_day) + ' c', s: r => r.summary.per_day },
      { h: 'Worst drawdown', f: r => plainPct(r.summary.max_drawdown, 1) },
      { h: 'Fills', f: r => r.summary.fills.toLocaleString('en-US') }, { h: 'Turnover', f: r => r.summary.turnover + '×', title: 'Traded value over bankroll' },
      { h: 'Gold spent', f: r => fmt(Math.round(r.summary.gold_spent)), s: r => r.summary.gold_spent },
      { h: 'Per 1,000 Gold', f: r => r.summary.chaos_per_1000_gold == null ? '–' : fmt(r.summary.chaos_per_1000_gold) + ' c', s: r => r.summary.chaos_per_1000_gold, title: 'Chaos earned for every 1,000 Gold spent' },
      { h: 'Most held', f: r => fmt(r.summary.peak_holdings) + ' c', s: r => r.summary.peak_holdings, title: 'The most Chaos you had in items at any hour, at fair value' },
      { h: 'Worst drop', f: r => fmt(r.summary.peak_risk) + ' c', s: r => r.summary.peak_risk, title: 'What your holdings could lose on a bad day for these items (about 1 day in 20), at the riskiest hour' },
      { h: 'Avg drop', f: r => fmt(r.summary.average_risk) + ' c', s: r => r.summary.average_risk, title: 'The same bad-day loss, averaged over the run' },
    ], lab.results, { onRow: pick }));
    pick(lab.results[0]);
  };
  $('#exp', main).onclick = async () => {
    const ran = lab.ran, m = ran.members[0];
    try {
      const doc = await api('/api/strategy_export', { strategy: m.strategy, params: m.params, options: ran.options, results: lab.results.map(r => r.summary) });
      download(`${m.strategy}-${lab.results.map(r => r.summary.league).join('-').replace(/[^A-Za-z0-9-]+/g, '_')}.json`, doc);
      toast('Saved the strategy file to your downloads. Nothing was uploaded.');
    } catch (err) { toast(err.message, true); }
  };
  $('#run', main).onclick = async e => {
    const err = $('#err', form); err.textContent = '';
    if (!lab.leagues.length) { err.textContent = 'Choose at least one league.'; return; }
    e.target.disabled = true; e.target.textContent = 'Running…'; lab.results = [];
    const members = lab.members.map(m => ({ strategy: m.strategy, params: paramsOf(m) })), opts = options();
    try {
      for (const league of lab.leagues) {
        lab.results.push(await api('/api/backtest', members.length > 1 ? { league, strategies: members, ...opts } : { league, ...members[0], ...opts }));
      }
      lab.ran = { members, options: opts };
      show();
    } catch (x) { err.textContent = x.message; lab.ran = null; show(); }
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
    return r.best_by == null ? `${marketTag(r.best)} <span class="dim">the only market trading steadily</span>` : `${marketTag(r.best)} pays ${plainPct(r.best_by, 1)} more`;
  };
  const tbl = table([
    { h: 'Item', l: 1, f: r => itemCell(r, r.note ? esc(r.note) : ''), s: r => r.name },
    { h: 'Units', f: r => fmt(r.units), s: r => r.units },
    { h: 'Worth', f: r => money(r.value), s: r => r.value, title: 'Units at fair value' },
    { h: 'Low – high', f: r => r.value == null ? '–' : `${money(r.low)} <span class="dim">–</span> ${money(r.high)}`, s: r => r.high, title: 'Units at the low and high end of how sure the model is of the value now. Not what a sale will fetch' },
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

// Every kind of alert the desk can watch, with its form fields in plain words.
const ALERT_FORMS = {
  price: { hint: 'Rings when a value of one item crosses your line.', fields: [
    ['what', 'Item', 'item', true], ['field', 'Watch', { price: 'Fair value', chaos: 'Chaos-market price', divine: 'Divine-market price', premium: 'Divine premium', change_24h: '24h change' }],
    ['op', 'When it', { above: 'rises above', below: 'falls below' }], ['threshold', 'Line (Chaos; premium and change as 0.1 = 10%)', 'number']] },
  plan: { hint: 'Rings when a flip plan reaches your margin and profit per hour.', fields: [
    ['what', 'Item (empty: any item)', 'item'], ['min_margin', 'Margin at least (%)', 'number', 2], ['min_profit', 'Profit per hour at least (c)', 'number', 0]] },
  trip: { hint: 'Rings when a round trip reaches your edge and orders worth taking are waiting at its thinnest swap.', fields: [
    ['what', 'Starting item (empty: any)', 'item'], ['min_edge', 'Edge at least (%)', 'number', 3], ['min_resting', 'Orders waiting worth at least (c)', 'number', 0]] },
  order: { hint: 'Rings when one of the orders you recorded should be re-priced, cancelled or collected. Nothing to set.', fields: [] },
  precedent: { hint: 'Rings when an item stands out against the same day of past leagues.', fields: [
    ['what', 'Item', 'item', true], ['mode', 'Look at', { standing: 'its price against past leagues today', playbook: 'what past leagues did next' }]],
    standing: [['direction', 'When it is', { cheap: 'cheaper', dear: 'dearer' }], ['min_gap', 'than usual by at least (%)', 'number', 20]],
    playbook: [['direction', 'When past leagues moved it', { up: 'up', down: 'down' }], ['min_move', 'by a median of at least (%)', 'number', 20], ['horizon', 'over the next days', 'number', 7]] },
};
function alertsPanel(d, names) {
  const al = panel('Alerts', 'c12', '<span class="muted small">Checked once an hour when new prices arrive. Each rings once when its condition starts, then waits for it to clear.</span>');
  const form = document.createElement('form'); form.className = 'body'; form.autocomplete = 'off';
  let kind = localStorage.alertKind && ALERT_FORMS[localStorage.alertKind] ? localStorage.alertKind : 'price', mode = 'standing';
  const field = ([key, label, type, def]) => type === 'item'
    ? `<label class="f">${label}<input type="text" name="${key}" list="items" placeholder="Start typing…" style="width:220px"></label>`
    : type === 'number' ? `<label class="f">${label}<input type="number" name="${key}" step="any" min="0" value="${def ?? ''}"></label>`
    : `<label class="f">${label}<select name="${key}">${Object.entries(type).map(([v, l]) => `<option value="${v}" ${key === 'mode' && v === mode ? 'selected' : ''}>${l}</option>`).join('')}</select></label>`;
  const draw = () => {
    const spec = ALERT_FORMS[kind], fields = [...spec.fields, ...(kind === 'precedent' ? spec[mode] : [])];
    form.innerHTML = `<div class="bar" style="margin-bottom:8px"><div class="seg" id="ak">${Object.entries(d.alert_kinds || {}).filter(([k]) => ALERT_FORMS[k]).map(([k, l]) => `<button type="button" data-k="${k}" class="${k === kind ? 'on' : ''}">${esc(l)}</button>`).join('')}</div><span class="muted small">${spec.hint}</span></div>
      <div class="bar" style="margin:0">${fields.map(field).join('')}<button class="btn primary">Add alert</button></div>`;
    $('#ak', form).onclick = e => { const k = e.target.dataset.k; if (k) { kind = localStorage.alertKind = k; draw(); } };
    const m = form.elements.mode; if (m) m.onchange = () => { mode = m.value; const item = form.elements.what.value; draw(); form.elements.what.value = item; };
  };
  draw();
  form.onsubmit = async e => {
    e.preventDefault();
    const val = k => form.elements[k]?.value.trim() ?? '';
    let item = '';
    if (form.elements.what && val('what')) {
      const row = state.market.find(r => r.name.toLowerCase() === val('what').toLowerCase());
      if (!row) return toast('Pick an item from the list.', true);
      item = row.id;
    }
    let body;
    if (kind === 'price') {
      if (!item || val('threshold') === '') return toast('A price alert needs an item and a line.', true);
      body = { kind, item, field: val('field'), op: val('op'), threshold: +val('threshold') };
    } else {
      const params = item ? { item } : {};
      for (const k of ['min_margin', 'min_profit', 'min_edge', 'min_resting', 'mode', 'direction', 'min_gap', 'min_move', 'horizon']) if (form.elements[k] && val(k) !== '') params[k] = val(k);
      body = { kind, params };
    }
    try {
      await api('/api/alert', { league: state.league, ...body });
      toast('Alert added. It is checked when the next hour’s prices arrive.');
      if (window.Notification?.permission === 'default') Notification.requestPermission();
      route();
    } catch (err) { toast(err.message, true); }
  };
  const at = table([
    { h: 'Kind', l: 1, f: r => `<span class="tag">${esc((d.alert_kinds || {})[r.kind || 'price'] || r.kind)}</span>` },
    { h: 'Rule', l: 1, f: r => esc(r.description || `${(d.alert_fields || {})[r.field] || r.field} ${r.op} ${r.threshold}`) },
    { h: 'Now', l: 1, f: r => !r.active ? '<span class="dim">Off</span>' : r.hot ? '<span class="gold">Holding now</span>' : 'Waiting', title: '“Holding now” means it has rung and its condition still holds; it rings again only after it clears' },
    { h: '', f: r => `<button class="btn ghost" data-del="${r.id}" title="Delete this alert">✕</button>` },
  ], d.alerts, { empty: 'No alerts yet. Add one above, or from an item’s window.' });
  at.onclick = async e => { const b = e.target.closest('[data-del]'); if (b) { await api('/api/alert', { delete: +b.dataset.del }); route(); } };
  al.append(form, at);
  return al;
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
    <section class="panel" id="tform" style="margin-bottom:14px"><header><h2 id="thead">Record a trade</h2><span class="muted small" id="tlock" hidden>Filled from an order: its item, side and units follow the order, and the order holds the Gold.</span></header><div class="body bar" style="margin:0">
      <label class="f">Item<input type="text" id="ti" list="items" placeholder="Start typing…" style="width:240px"><datalist id="items">${names.map(n => `<option value="${esc(n)}">`).join('')}</datalist></label>
      <div class="seg" id="side"><button data-s="buy" class="on">Bought</button><button data-s="sell">Sold</button></div>
      <label class="f">Units<input type="number" id="tu" min="0" step="any"></label>
      <label class="f">Price each (c)<input type="number" id="tp" min="0" step="any"></label>
      <label class="f">Gold paid<input type="number" id="tg" min="0" step="any" placeholder="optional" title="The Gold this trade cost. The desk learns each item's fee from these."></label>
      <label class="f">Note<input type="text" id="tn" style="width:160px"></label>
      <button class="btn primary" id="add">Add</button><button class="btn" id="tcancel" hidden>Cancel</button></div></section>
    <section class="panel" style="margin-bottom:14px"><header><h2>Record items you made</h2></header><form class="body" id="mform" autocomplete="off">
      <p class="muted small" style="margin:0 0 8px">Merged items into another one, such as 3 essences into 1 of the next tier, or a card set into its reward? Record it here. What you paid for the items you used becomes the cost of the item you made, and you can then record its sale.</p>
      <div class="muted small" style="margin-bottom:4px">You used</div><div id="mins"></div>
      <button type="button" class="btn ghost" id="mmore" style="margin-bottom:8px">+ another item</button>
      <div class="bar" style="margin:0"><label class="f">You got<span style="display:flex;gap:6px"><input type="number" id="mg" min="0" step="any" value="1" style="width:70px"><input type="text" id="mo" list="items" placeholder="Start typing…" style="width:240px"></span></label>
      <label class="f">Note<input type="text" id="mn" placeholder="optional" style="width:160px"></label>
      <button class="btn primary">Add</button></div></form></section>
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
  let side = 'buy', editing = null;  // editing: the trade loaded into the form by its ✎ button
  const setSide = s => { side = s; main.querySelectorAll('#side button').forEach(b => b.classList.toggle('on', b.dataset.s === s)); };
  $('#side', main).onclick = e => { const s = e.target.dataset.s; if (s) setSide(s); };
  $('#ti', main).onchange = e => { const row = state.market.find(r => r.name === e.target.value); if (row && !$('#tp', main).value) $('#tp', main).value = row.price; };
  const editTrade = tr => {
    editing = tr;
    $('#thead', main).textContent = `Edit the trade from ${new Date(tr.t * 1000).toLocaleString()}`;
    $('#ti', main).value = tr.name; setSide(tr.side);
    $('#tu', main).value = tr.units; $('#tp', main).value = tr.price; $('#tg', main).value = tr.gold ?? ''; $('#tn', main).value = tr.note || '';
    const fromOrder = tr.order_id != null;
    [$('#ti', main), $('#tu', main), $('#tg', main), ...main.querySelectorAll('#side button')].forEach(el => { el.disabled = fromOrder; });
    $('#tlock', main).hidden = !fromOrder;
    $('#add', main).textContent = 'Save changes'; $('#tcancel', main).hidden = false;
    $('#tform', main).scrollIntoView({ block: 'start', behavior: 'smooth' });
    $(fromOrder ? '#tp' : '#tu', main).focus();
  };
  $('#tcancel', main).onclick = () => route();
  $('#add', main).onclick = async () => {
    const name = $('#ti', main).value;
    // An edited trade keeps its item without a lookup, so an item that no longer trades can still be corrected.
    const item = editing && name === editing.name ? editing.item : state.market.find(r => r.name === name)?.id;
    if (!item) return toast('Pick an item from the list.', true);
    const fields = { price: +$('#tp', main).value, note: $('#tn', main).value };
    if (!editing || editing.order_id == null) Object.assign(fields, { item, side, units: +$('#tu', main).value, gold: optional($('#tg', main).value) });
    try { await api('/api/trade', { league: state.league, ...(editing ? { edit: editing.id } : {}), ...fields }); if (editing) toast('Trade updated.'); route(); }
    catch (err) { toast(err.message, true); }
  };
  const usedRow = () => `<div class="bar" style="margin:0 0 6px" data-in><label class="f">Count<input type="number" class="n" min="0" step="any" style="width:70px"></label><label class="f">Item<input type="text" class="it" list="items" placeholder="Start typing…" style="width:240px"></label></div>`;
  $('#mins', main).innerHTML = usedRow();
  $('#mmore', main).onclick = () => $('#mins', main).insertAdjacentHTML('beforeend', usedRow());
  $('#mform', main).onsubmit = async e => {
    e.preventDefault();
    // The market's own id where the name is in the list, as the trade form sends it; the server looks up any other name.
    const ref = name => state.market.find(r => r.name === name)?.id || name;
    const form = e.target, inputs = [...form.querySelectorAll('[data-in]')].map(r => [ref($('.it', r).value.trim()), +$('.n', r).value]).filter(([item]) => item);
    if (!inputs.length || !$('#mo', form).value.trim()) return toast('Name what you used and what you got.', true);
    try { await api('/api/made', { league: state.league, inputs, output: ref($('#mo', form).value.trim()), gives: +$('#mg', form).value, note: $('#mn', form).value }); route(); }
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
    { h: 'Ratio', l: 1, f: r => `${r.item_units} for ${r.currency_units} ${curWord(r.market === 'divine' ? 'Divine' : 'Chaos')}` },
    { h: 'Each', f: r => money(r.price) }, { h: 'Filled', f: r => `${fmt(r.filled)} / ${fmt(r.units)}` },
    { h: 'Gold', f: r => r.gold == null ? '–' : fmt(r.gold) },
    { h: 'Advice', l: 1, f: r => `${ADVICE[r.advice]} <span class="muted small">${esc(r.reason)}${r.suggest ? ` Try ${r.suggest.item_units} for ${r.suggest.currency_units}.` : ''}</span>${r.repost_gold != null ? `<div class="gold small" title="Gold is not refunded on cancel, so re-posting pays the fee again">re-posting costs about ${Math.round(r.repost_gold).toLocaleString('en-US')} Gold</div>` : ''}` },
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
    { h: 'Note', l: 1, f: r => esc(r.note) },
    { h: '', f: r => `<button class="btn ghost" data-edit="${r.id}" title="Edit this trade">✎</button><button class="btn ghost" data-del="${r.id}" title="Delete this trade">✕</button>` },
  ], d.trades, { empty: 'Nothing recorded.' });
  tt.onclick = async e => {
    const ed = e.target.closest('[data-edit]');
    if (ed) return editTrade(d.trades.find(r => r.id === +ed.dataset.edit));
    const b = e.target.closest('[data-del]');
    if (!b) return;
    try { await api('/api/trade', { delete: +b.dataset.del }); route(); } catch (err) { toast(err.message, true); }
  };
  tr.append(tt);
  const mk = panel('Items you made', 'c12', '<span class="muted small">Merges and turn-ins you recorded. The item made carries what the items used cost you.</span>');
  const mt = table([
    { h: 'When', l: 1, f: r => new Date(r.t * 1000).toLocaleString() },
    { h: 'Used', l: 1, f: r => r.inputs.map(x => `${+x.units} ${esc(x.name)}`).join(' + ') },
    { h: 'Got', l: 1, f: r => `${+r.gives} ${esc(r.name)}` },
    { h: 'Note', l: 1, f: r => esc(r.note) },
    { h: '', f: r => `<button class="btn ghost" data-del="${r.id}" title="Delete this entry">✕</button>` },
  ], d.made, { empty: 'Nothing recorded. Use "Record items you made" above when you merge or turn in items.' });
  mt.onclick = e => { const b = e.target.closest('[data-del]'); if (b) post('/api/made', { delete: +b.dataset.del }); };
  mk.append(mt);
  const al = alertsPanel(d, names);
  const cal = panel('How much of the flow your orders got', 'c12', '<span class="muted small">From the orders you recorded and their fills, against the volume that traded through your price. Flips use these shares where they exist.</span>');
  cal.append(table([
    { h: 'Item', l: 1, f: r => esc(r.name) }, { h: 'Market', l: 1, f: r => marketTag(r.market) },
    { h: 'Orders', f: r => `${r.orders}${r.complete ? ` <span class="dim">· ${r.complete} filled</span>` : ''}` },
    { h: 'Measured', f: r => plainPct(r.measured, 1), title: 'Units your orders filled, over the units that traded through your price while they stood' },
    { h: 'Share used', f: r => plainPct(r.share, 1), title: 'Blended with the default while you have few orders, so one lucky fill does not decide it' },
    { h: 'What it means', l: 1, f: r => esc(r.reading) },
  ], d.fill_calibration || [], { empty: 'Record orders you post and their fills; the desk then measures how much of the flow you really get.' }));
  const stash = await stashPanel(names);
  $('#g', main).append(ses, ord, cal, pos, stash, tr, mk, fee, rc, al);
}

// ------------------------------------------------------------------ ladder
async function viewLadder(main) {
  if (!state.summary) await loadDesk();
  const v = state.ladder ||= { purse: localStorage.ladderPurse || 25, hours: localStorage.ladderHours || 6, target: '' };
  const d = await api(withLeague('/api/ladder', { bankroll: v.purse, hours: v.hours, share: state.share, ...(v.target ? { target: v.target } : {}) }));
  state.bridge = d.summary.bridge; renderTop();
  main.innerHTML = `<div class="head"><div><h1>Ladder</h1><p>Growing a small purse: what each purse size can run, what opens up at each step, and when to switch plans as the purse grows.</p></div></div>
    <div class="bar">
      <label class="f">Starting purse (c)<input type="number" id="lp" value="${esc(v.purse)}" min="1" step="5"></label>
      <label class="f">Hours you will play<input type="number" id="lh" value="${esc(v.hours)}" min="1" step="1"></label>
      <label class="f" title="Instead of a number of hours, play until the purse reaches this many Chaos">Or until the purse reaches (c)<input type="number" id="lt" value="${esc(v.target)}" min="0" step="50" placeholder="optional"></label>
    </div>
    <p class="note"><b>${esc(d.note)}</b></p>
    <div class="grid" id="g"></div>`;
  const g = $('#g', main);
  const plan = panel(`From ${+d.start.toFixed(1)}${CUR.c}${d.target ? ` to ${+d.target.toFixed(1)}${CUR.c}` : ` over ${+d.hours.toFixed(1)} hours`}`, 'c12');
  plan.insertAdjacentHTML('beforeend', `<div class="body" style="padding-bottom:0">${d.schedule_text.map(t => `<p style="margin:0 0 6px">${esc(t)}</p>`).join('') || '<p class="muted">Nothing this purse can run grows it.</p>'}</div>`);
  plan.append(table([
    { h: 'Hours', l: 1, f: r => r.to_hour == null ? `from ${+r.from_hour.toFixed(1)} h` : `${+r.from_hour.toFixed(1)}–${+r.to_hour.toFixed(1)} h` },
    { h: 'Purse', f: r => money(r.purse) },
    { h: 'Run', l: 1, f: r => r.plans.map(p => `${esc(p.label)} <span class="dim">stake ${fmt(p.stake)}${CUR.c}</span>`).join('<br>') || '<span class="dim">nothing</span>' },
    { h: 'Per hour', f: r => money(r.rate), title: 'The lower of what the plans promise now and what they made over the last week' },
    { h: 'You end near', f: r => r.end_purse == null ? '–' : money(r.end_purse) },
    { h: 'Why it stops', l: 1, f: r => ({ 'session ends': 'your session ends', 'target reached': 'you reach your target', 'next rung': `the purse reaches ${fmt(r.switch_at)}${CUR.c}: switch to the next step’s plan`, 'nothing earns at this purse': 'nothing earns at this purse', 'top of the ladder': 'the top step: keep going' })[r.reason] || esc(r.reason) },
    { h: 'Next step', f: r => r.next_rung == null ? '–' : `${money(r.next_rung)}<div class="muted small">${r.hours_to_next == null ? '' : dur(r.hours_to_next) + ' away'}</div>` },
  ], d.schedule, { empty: 'Nothing this purse can run grows it.' }));
  const rungs = panel('What each purse size can run', 'c12', '<span class="muted small">Flips, conversions and round trips that purse can start, and what opens up that the step below could not run.</span>');
  rungs.append(table([
    { h: 'Purse', l: 1, f: r => `<b>${esc(r.label)}</b><div class="muted small">${fmt(r.chaos)}${CUR.c}</div>` },
    { h: 'Can run', f: r => `${r.can_run}<div class="muted small">${esc(r.counts_text || '')}</div>` },
    { h: 'What opens up here', l: 1, f: r => r.opens?.length ? r.opens.slice(0, 6).map(o => `${esc(o.label)} <span class="dim">· one lot ${fmt(o.lot_cost)}${CUR.c}</span>`).join('<br>') + (r.opens.length > 6 ? `<br><span class="dim">and ${r.opens.length - 6} more</span>` : '') : '<span class="dim">nothing new</span>' },
    { h: 'Session plan', l: 1, f: r => r.session?.picks?.length ? r.session.picks.map(p => `${esc(p.label)}${p.replay_at_stake ? ` <span class="dim">· last week at this stake ${p.replay_at_stake.vs_divine != null ? `${pct(p.replay_at_stake.vs_divine, 0)} vs Divines` : pct(p.replay_at_stake.return, 0)}</span>` : ''}`).join('<br>') : `<span class="dim">${r.session?.divines != null ? `nothing beat holding Divines (${pct(r.session.divines, 0)})` : 'no flip fits'}</span>` },
    { h: 'Last week vs Divines / hour', f: r => money(r.session?.replayed_vs_divine_per_hour), title: 'What the same plans made an hour over the last week, minus what the purse would have made held as Divines' },
    { h: 'Last week / hour', f: r => money(r.session?.replayed_per_hour), title: 'What the same plans made over the last week in Chaos, replayed on recent prices: a rough guide, small lots round worse' },
    { h: 'Promised / hour', f: r => money(r.session?.expected_per_hour), title: 'What the session plan’s flips promise at today’s prices' },
    { h: 'To next step', f: r => r.next == null ? '–' : r.hours_to_next == null ? '<span class="dim">does not grow</span>' : dur(r.hours_to_next) },
  ], d.ladder));
  g.append(plan, rungs);
  const set = (key, store) => e => { v[key] = e.target.value; if (store) localStorage[store] = e.target.value; route(); };
  $('#lp', main).onchange = set('purse', 'ladderPurse');
  $('#lh', main).onchange = set('hours', 'ladderHours');
  $('#lt', main).onchange = set('target');
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
      <div class="kpi"><span>Fair value</span><b>${money(row.price)}</b><i>${pct(row.change_24h)}</i>${halfRange(row) != null ? `<div class="muted small" title="How sure the model is about the value now. Not a forecast: single trades often land outside. Range ${fmt(row.fair_low)} to ${fmt(row.fair_high)} Chaos.">model sure within ${plusMinus(halfRange(row))}</div>` : ''}</div>
      <div class="kpi"><span>vs Chaos</span><b>${money(row.chaos)}</b></div>
      <div class="kpi"><span>vs Divine</span><b>${money(row.divine)}</b><i>${pct(row.premium)}</i></div>
      <div class="kpi"><span>Traded 24h</span><b>${money(row.value_24h)}</b></div>
    </div>
    ${row.move != null ? `<p class="note">The model reads this item as moving about ±${(row.move * 100).toFixed(1)}% an hour for real, with prints bouncing ±${(row.noise * 100).toFixed(1)}% around that. ${row.z != null && Math.abs(row.z) >= 2 ? `The latest print was ${Math.abs(row.z).toFixed(1)} deviations ${row.z > 0 ? 'above' : 'below'} what the model expected.` : ''}</p>` : ''}
    <div class="bar"><div class="seg" id="rg">${[[48, '2 days'], [168, '7 days'], [720, '30 days']].map(([h, l]) => `<button data-h="${h}" class="${h === hours ? 'on' : ''}">${l}</button>`).join('')}</div>
      <span class="grow"></span><button class="btn" id="tcItem" title="Is an offer for this item a good deal? (C)">${BI.check}Check an offer</button><button class="btn" id="watchBtn">${row.watched ? '★ Watching' : '☆ Watch'}</button></div>
    <div class="legend"><span><i style="background:var(--fair)"></i>Fair value</span><span title="How sure the model is about the value now. Not a forecast: single trades often land outside."><i style="background:color-mix(in srgb,var(--fair) 25%,transparent);height:8px"></i>How sure the model is</span><span><i style="background:var(--chaos)"></i>Chaos market, hourly average</span><span><i style="background:var(--divine)"></i>Divine market, hourly average</span><span><i style="background:color-mix(in srgb,var(--vol) 19%,transparent)"></i>Value traded</span></div>
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
      { label: 'Fair value', color: css('--fair'), x: s.t, y: conv(s.fair), width: 2, bridge: true },
      { label: 'Chaos market', color: css('--chaos'), x: s.t, y: conv(s.chaos), dots: true },
      { label: 'Divine market', color: css('--divine'), x: s.t, y: conv(s.divine), dots: true },
    ], band: { label: 'Model sure within', x: s.t, lo: conv(s.fair_low), hi: conv(s.fair_high), color: css('--fair') + '26' },
    bars: { x: s.t, y: s.value }, yFormat: yf, tickFormat: tick(div ? 'd' : 'c'), height: 300,
  });
  $('#plans', drawer).innerHTML = d.flips.length ? d.flips.map(p => `<div class="order">${marketTag(p.kind)}
      <div class="big">≈ ${money(p.profit_per_hour)} <span class="muted small">/ hour · ${pct(p.margin)} margin</span></div>
      <div class="num small">Buy: ${order(p.buy_order, true)}<br>Sell: ${order(p.sell_order, false)}</div>
      <div class="muted small" style="margin-top:5px">${plainPct(p.reach)} of recent volume traded beyond these levels · ${fmt(p.units_per_hour)} units/hour · one lot every ${dur(p.hours_per_lot)}<br>${p.replay ? `Replayed over the last 7 days: ${p.replay.vs_divine != null ? `${pct(p.replay.vs_divine, 0)} against holding Divines, ` : ''}${pct(p.replay.return, 0)} in Chaos${replayRate(p.replay) ? ` (${replayRate(p.replay)})` : ''}, ${p.replay.sales} sale${p.replay.sales === 1 ? '' : 's'}` : 'Replayed over the last 7 days: no fills'}${p.warning ? `<br><span class="gold">⚠ ${esc(p.warning)}</span>` : ''}${p.slow ? `<br><span class="gold">⚠ ${esc(p.slow)}</span>` : ''}</div></div>`).join('')
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
      out.innerHTML = `<div class="verdict ${cls}">${word} <span class="muted small">${esc(r.name)} at ${fmt(r.price)}${CUR.c} each${r.trusted ? '' : ' · this market barely trades'}</span></div>
        <ul>${r.reasons.map(x => `<li>${esc(x)}</li>`).join('')}</ul>`;
    } catch (err) { out.innerHTML = `<p class="down">${esc(err.message)}</p>`; }
  };
  (item ? form.elements.iu : form.elements.what).focus();
}
function closeCheck() { const host = $('#tc'); if (host) host.hidden = true; }

// -------------------------------------------------------------------- tour
// A short first-visit walk through the pages. Each step opens the page it talks about, so the
// panel sits in a corner instead of covering the desk. Seen once, it stays away until `?` asks.
const TOUR = [
  { title: 'Welcome to Vaal Street', body: `<p>A trading desk for the Currency Exchange, built from the hourly history GGG publishes. Everything runs on this machine.</p>
    <p>Two things to know before you trade:</p><ul>
    <li>The desk is one to two hours behind the game: an hour’s prices come out only after it ends. Check the live ratio in game before you commit.</li>
    <li>Flips are <b>resting orders</b>: you post a buy below the market and an offer above it, then wait for other players. A round usually takes hours, not minutes.</li></ul>` },
  { page: 'desk', title: 'Desk', body: `<p>The hour at a glance: the Divine price, the best flips, sets worth completing, what moved, your watchlist and alerts that fired.</p>
    <p>The top bar shows the league, its day, and how old the latest hour is. Switch prices between Chaos and Divine there. Every price shows its orb: a copper ring is Chaos, a blue ring is Divine.</p>` },
  { page: 'markets', title: 'Markets', body: `<p>Every traded item with its fair value and how sure the model is (±%). Most items have a Chaos market and a Divine market, and they pay different prices.</p>
    <p>Click an item for its chart and its flip plans. Star it to put it on your watchlist and in the hideout window. Press <kbd>/</kbd> to search.</p>` },
  { page: 'flips', title: 'Flips', body: `<p>Ranked plans with the exact orders to post. Enter your purse to get a session plan.</p>
    <ul><li>Judge a plan by its <b>last 7 days against holding Divines</b>, not by its promised profit per hour, which is a ceiling.</li>
    <li>Gold is often the real limit: rank by Chaos per 1,000 Gold, or set a Gold budget.</li></ul>` },
  { page: 'arbitrage', title: 'Arbitrage', body: `<p>Things that turn into something dearer: card sets, shards and splinters, three-for-one essence and oil upgrades, and round trips through item-for-item markets.</p>
    <p>Mark ✓ or ✗ once you tried one in game, and add your own recipes and vendor prices at the bottom.</p>` },
  { page: 'ledger', title: 'Ledger', body: `<p>Record the trades you make and the orders you post, with the Gold they cost. This is what makes the desk yours:</p>
    <ul><li>It learns each item’s Gold fee from your records.</li><li>It learns how much of the market your orders really get, and flips get more realistic.</li>
    <li>It tells you when an order should be re-priced, cancelled or collected.</li></ul><p>Fix a mistake with ✎, set alerts, and start a session to see what you made per hour.</p>` },
  { page: 'league', title: 'Planning and practice', body: `<ul><li><b>Ladder</b>: what a small purse can run at each size, and when to switch plans.</li>
    <li><b>League start</b> (this page): what past leagues did from today’s league day, and when Divines were cheapest. You can also <b>rehearse a past league</b> here, hour by hour, without touching your real ledger.</li>
    <li><b>Backtest lab</b>: replay a strategy over past leagues and compare it with holding Divines.</li></ul>` },
  { title: 'While you play', body: `<ul><li><kbd>H</kbd> opens the hideout window: top flips, watchlist and alerts in a small window that stays on top in Chrome and Edge. Run the game in windowed or borderless mode to see it over the game.</li>
    <li><kbd>C</kbd> checks an offer from the in-game window: type the item and the ratio, and get go, caution or no-go.</li>
    <li><kbd>?</kbd> brings this tour back.</li></ul>` },
];
let tourStep = 0;
const tourSeen = () => { try { return !!localStorage.tourSeen; } catch { return true; } };  // no storage: never nag
function openTour(step = 0) {
  let host = $('#tour');
  if (!host) { host = document.createElement('aside'); host.id = 'tour'; host.setAttribute('aria-label', 'Tour of the desk'); document.body.append(host); }
  tourStep = Math.max(0, Math.min(step, TOUR.length - 1));
  const s = TOUR[tourStep], last = tourStep === TOUR.length - 1;
  if (s.page && !location.hash.startsWith(`#/${s.page}`)) location.hash = `#/${s.page}`;
  host.hidden = false;
  host.innerHTML = `<header><h2>${esc(s.title)}</h2><span class="grow"></span><span class="muted small">${tourStep + 1} of ${TOUR.length}</span><button type="button" class="btn ghost" data-x title="Close (Esc)">✕</button></header>
    <div class="body">${s.body}</div>
    <footer>${tourStep ? '<button type="button" class="btn" data-back>Back</button>' : '<button type="button" class="btn ghost" data-x>Skip</button>'}<span class="grow"></span>
      <button type="button" class="btn primary" data-next>${last ? 'Done' : 'Next'}</button></footer>`;
  host.onclick = e => {
    if (e.target.closest('[data-x]')) closeTour();
    else if (e.target.closest('[data-back]')) openTour(tourStep - 1);
    else if (e.target.closest('[data-next]')) last ? closeTour() : openTour(tourStep + 1);
  };
  $('[data-next]', host).focus({ preventScroll: true });
}
function closeTour() {
  const host = $('#tour'); if (host) host.hidden = true;
  try { localStorage.tourSeen = '1'; } catch { /* the tour comes back next visit; harmless */ }
}
const tourOpen = () => $('#tour') && !$('#tour').hidden;

// --------------------------------------------------------------- rehearsal
// While a past league is replayed, every API answer carries `rehearsal`; the banner follows it, so
// practice can never pass for the live market.
// "Old day 3, 14:00": the hour within the league day, so the clock never runs backwards inside a day.
const practiceTime = b => `${b.league} day ${b.league_day}, ${String(b.league_hour % 24).padStart(2, '0')}:00`;
const practiceDate = b => new Date(b.hour * 1000).toLocaleString(undefined, { timeZone: 'UTC', weekday: 'short', day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }) + ' UTC';
const SPEEDS = [[0, 'by hand'], [1, '1 game hour a minute'], [2, '2 game hours a minute'], [6, '6 game hours a minute'], [24, '24 game hours a minute']];
// A form field in use: focused, or holding text the player typed and has not sent.
function typing() {
  const a = document.activeElement;
  if (a && a.matches('input,select,textarea') && !a.closest('#practice')) return true;
  return [...document.querySelectorAll('#main input, #main textarea, #drawer input, #tc input')]
    .some(i => !['checkbox', 'radio', 'range'].includes(i.type) && i.value !== i.defaultValue);
}
let practiceStale = false;
let practiceSeen = null, practiceResult = null, practiceTimer = null, practiceTimerSpeed = 0;
function practiceFrom(data) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return;
  const b = data.rehearsal || (data.running === true && data.hour ? data : null);
  const was = state.practice;
  state.practice = b || null;
  if (b && !was) { state.league = b.league; state.summary = null; }
  // Back to live: forget the practice league and let the server's live default fill it in again.
  if (!b && was) { state.league = null; state.summary = null; }
  drawPractice();
  // A new practice hour means new prices on every page.
  if (b && practiceSeen != null && b.hour !== practiceSeen) {
    practiceSeen = b.hour; refreshPracticeResult();
    // Never wipe what the player is typing: offer a refresh instead.
    if (typing()) { practiceStale = true; drawPractice(); } else setTimeout(route, 0);
  }
  practiceSeen = b ? b.hour : null;
  // With the clock running by itself, look in every 15 s so the pages follow it.
  const want = b && b.speed > 0 && !b.done ? b.speed : 0;
  if (want !== practiceTimerSpeed) {
    clearInterval(practiceTimer); practiceTimerSpeed = want;
    if (want) practiceTimer = setInterval(() => api('/api/rehearsal').catch(() => {}), 15000);
  }
}
async function refreshPracticeResult() {
  try { const r = await fetch('/api/rehearsal').then(x => x.json()); practiceResult = r.result || null; drawPractice(); } catch { /* next step tries again */ }
}
function resultLine(r) {
  if (!r) return '';
  return `Practice profit <b class="${r.profit >= 0 ? 'up' : 'down'}">${fmt(r.profit)} c</b> over ${r.hours} h · holding Divines with ${fmt(r.bankroll)} c: <b>${fmt(r.hold_divines)} c</b> · you are <b class="${r.vs_divines >= 0 ? 'up' : 'down'}">${r.vs_divines >= 0 ? '+' : ''}${fmt(r.vs_divines)} c</b> against it`;
}
function drawPractice() {
  let bar = $('#practice');
  const b = state.practice;
  document.body.classList.toggle('practice', !!b);
  if (!b) { if (bar) bar.remove(); return; }
  if (!bar) { bar = document.createElement('div'); bar.id = 'practice'; bar.setAttribute('role', 'status'); document.body.prepend(bar); }
  bar.innerHTML = `<div class="pline"><b class="ptag">PRACTICE</b><span class="ptime" title="Hours since the league's first trade">${esc(practiceTime(b))}</span><span class="muted">${esc(practiceDate(b))}</span>
      <span class="muted">Not the live market.</span>${practiceStale ? '<button class="btn" id="pfresh" title="A new practice hour arrived while you were typing">New practice hour · refresh</button>' : ''}${b.done ? '<b class="gold">End of the data for this league.</b>' : ''}
      <span class="grow"></span>
      <span class="pbtns">${[1, 6, 24].map(h => `<button class="btn" data-step="${h}" ${b.done ? 'disabled' : ''}>+${h} h</button>`).join('')}</span>
      <select id="pspeed" title="How fast the practice clock runs">${SPEEDS.map(([v, l]) => `<option value="${v}" ${+b.speed === v ? 'selected' : ''}>${l}</option>`).join('')}${SPEEDS.some(([v]) => v === +b.speed) ? '' : `<option selected value="${b.speed}">${b.speed} game hours a minute</option>`}</select>
      <button class="btn" id="pstop">Stop</button></div>
    <div class="pline small"><span>${resultLine(practiceResult) || 'Practice profit appears after your first trade.'}</span><span class="grow"></span><span class="muted">Practice trades, orders and alerts go to a separate practice file, never your real ledger.</span></div>`;
  bar.onclick = async e => {
    const step = e.target.closest('[data-step]');
    if (step) { step.disabled = true; try { practiceFrom(await api('/api/rehearsal/step', { hours: +step.dataset.step })); } catch (err) { toast(err.message, true); } }
    if (e.target.id === 'pstop') stopPractice();
    if (e.target.id === 'pfresh') { practiceStale = false; drawPractice(); route(); }
  };
  $('#pspeed', bar).onchange = async e => { try { practiceFrom(await api('/api/rehearsal/step', { speed: +e.target.value })); } catch (err) { toast(err.message, true); } };
}
async function stopPractice() {
  try {
    const r = await api('/api/rehearsal', { stop: true });
    practiceFrom(r);
    practiceResult = null; showPracticeEnd(r.result); route();
  } catch (err) { toast(err.message, true); }
}
function showPracticeEnd(r) {
  if (!r) return;
  const box = document.createElement('div'); box.className = 'panel pend';
  box.innerHTML = `<header><h2>Practice over: ${esc(r.league)}, ${r.hours} hours</h2><span class="grow"></span><button class="btn ghost" data-x>✕</button></header>
    <div class="body"><div class="kpis" style="margin-bottom:8px">
      <div class="kpi"><span>Practice profit</span><b class="${r.profit >= 0 ? 'up' : 'down'}">${fmt(r.profit)}${CUR.c}</b><i class="muted">${pct(r.return)}</i></div>
      <div class="kpi"><span>Holding Divines instead</span><b>${fmt(r.hold_divines)}${CUR.c}</b><i class="muted">${fmt(r.divine_then)} → ${fmt(r.divine_now)}${CUR.c}</i></div>
      <div class="kpi"><span>Against holding Divines</span><b class="${r.vs_divines >= 0 ? 'up' : 'down'}">${r.vs_divines >= 0 ? '+' : ''}${fmt(r.vs_divines)}${CUR.c}</b></div></div>
      <p class="muted small" style="margin:0">${r.trades} practice trade${r.trades === 1 ? '' : 's'}; ${fmt(r.realized)}${CUR.c} realised, ${fmt(r.unrealized)}${CUR.c} in open positions at fair value. Holding Divines assumes a bankroll of ${fmt(r.bankroll)}${CUR.c}. ${r.unpriced?.length ? `${r.unpriced.length} position${r.unpriced.length === 1 ? ' has' : 's have'} no price and ${r.unpriced.length === 1 ? 'is' : 'are'} counted at cost. ` : ''}Your real ledger was not touched. You are back on the live market.</p></div>`;
  box.querySelector('[data-x]').onclick = () => box.remove();
  document.body.append(box);
}
// The entry point, on League start.
function rehearsePanel() {
  const p = panel('Rehearse a past league', 'c12', '<span class="muted small">Practise a league start: the desk plays a past league forward as if it were live, using only prices published by then.</span>');
  const leagues = (state.status.leagues || []).filter(l => l.hours >= 24);
  const form = document.createElement('form'); form.className = 'body bar'; form.style.margin = '0'; form.autocomplete = 'off';
  form.innerHTML = `<label class="f">League<select name="league">${leagues.map(l => `<option value="${esc(l.league)}">${esc(l.league)}${l.family ? ` · ${esc(familyWord(l.family))}` : ''}</option>`).join('')}</select></label>
    <label class="f">Start on league day<input type="number" name="start_day" value="0" min="0" step="1"></label>
    <label class="f">Clock<select name="speed">${SPEEDS.map(([v, l]) => `<option value="${v}">${l}</option>`).join('')}</select></label>
    <label class="f" title="Only used to size the comparison with holding Divines">Compare with holding Divines worth (c)<input type="number" name="bankroll" value="1000" min="1" step="any"></label>
    <button class="btn primary">Start practice</button>
    <span class="muted small" style="padding-bottom:7px">Practice trades go to a separate file, emptied when the next practice starts; your real ledger is never touched.</span>`;
  form.onsubmit = async e => {
    e.preventDefault();
    const v = k => form.elements[k].value;
    try {
      const r = await api('/api/rehearsal', { league: v('league'), start_day: +v('start_day') || 0, speed: +v('speed'), bankroll: +v('bankroll') || 1000 });
      if (r.previous) showPracticeEnd(r.previous);
      practiceResult = r.result; practiceFrom(r);
      toast(`Practice started: ${practiceTime(r)}.`);
      if (location.hash !== '#/desk') location.hash = '#/desk'; else route();
    } catch (err) { toast(err.message, true); }
  };
  p.append(form);
  return p;
}

// ----------------------------------------------------------------- hideout
// Fired alerts in the hideout window: a browser notification and/or the title read aloud, both opt-in.
// Only events after the last one this window has seen; nothing is ever sent to the game.
const ringing = { since: null, busy: false };
const ringOn = key => localStorage[key] === '1';
async function pollEvents() {
  if (ringing.busy) return;
  ringing.busy = true;
  try {
    const first = ringing.since == null;
    const d = await api(withLeague('/api/events', { since: ringing.since ?? +(localStorage.hoLastEvent || 0) }));
    // On opening, catch up silently unless this window has heard events before: old alerts are not news.
    const fresh = first && !localStorage.hoLastEvent ? [] : d.events;
    ringing.since = d.last; localStorage.hoLastEvent = d.last;
    for (const e of fresh.slice(-3)) {
      if (ringOn('hoNotify') && window.Notification?.permission === 'granted') new Notification(e.title, { body: e.body, icon: '/mark.svg', tag: 'vaal-' + e.id });
      if (ringOn('hoSpeak') && window.speechSynthesis) speechSynthesis.speak(Object.assign(new SpeechSynthesisUtterance(e.title), { rate: 1.05 }));
    }
  } catch { /* the next poll tries again */ }
  ringing.busy = false;
}
function ringControls() {
  const box = document.createElement('div'); box.className = 'panel'; box.style.padding = '8px 12px';
  const notifyState = !window.Notification ? 'unsupported' : Notification.permission;
  box.innerHTML = `<div class="muted small" style="margin-bottom:4px">When an alert fires</div>
    <label style="display:flex;gap:7px;align-items:center"><input type="checkbox" data-ring="hoNotify" ${ringOn('hoNotify') && notifyState === 'granted' ? 'checked' : ''} ${notifyState === 'unsupported' || notifyState === 'denied' ? 'disabled' : ''}> Show a notification${notifyState === 'denied' ? ' <span class="dim small">(blocked in the browser’s site settings)</span>' : notifyState === 'unsupported' ? ' <span class="dim small">(this browser cannot)</span>' : ''}</label>
    <label style="display:flex;gap:7px;align-items:center"><input type="checkbox" data-ring="hoSpeak" ${ringOn('hoSpeak') ? 'checked' : ''} ${window.speechSynthesis ? '' : 'disabled'}> Read its title aloud</label>`;
  box.onchange = async e => {
    const key = e.target.dataset.ring; if (!key) return;
    let on = e.target.checked;
    // The permission prompt only ever appears here, after your click.
    if (key === 'hoNotify' && on && Notification.permission !== 'granted') on = (await Notification.requestPermission()) === 'granted';
    e.target.checked = on; localStorage[key] = on ? '1' : '0';
    if (key === 'hoSpeak' && on) speechSynthesis.speak(new SpeechSynthesisUtterance('Alerts will be read aloud.'));
  };
  return box;
}
async function viewHideout(main) {
  const [d, f] = await Promise.all([loadDesk(), api(withLeague('/api/flips', { share: state.share, limit: 12, ...bankQuery() }))]);
  const s = d.summary, watched = d.market.filter(r => r.watched);
  main.innerHTML = `<div class="kpi" style="margin-bottom:10px"><span>${esc(s.league)}${s.league_day == null ? '' : ' · day ' + s.league_day} · ${state.practice ? 'practice' : ago(s.as_of + 3600)}</span><b>Divine ${fmt(s.bridge)}${CUR.c}</b><i>${pct(s.bridge_change_24h)}</i></div>
    <button class="btn" id="tcBtn" style="width:100%;margin-bottom:10px" title="Is an offer in the order window a good deal?">${BI.check}Check an offer <kbd>C</kbd></button>`;
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
  main.append(ringControls());
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
const VIEWS = { desk: viewDesk, markets: viewMarkets, flips: viewFlips, arbitrage: viewArbitrage, ladder: viewLadder, league: viewLeague, lab: viewLab, ledger: viewLedger };
let routing = 0;
async function route() {
  if (practiceStale) { practiceStale = false; drawPractice(); }
  const main = $('#main'), turn = ++routing;
  if (!state.status?.ready) {
    main.classList.remove('enter');  // this message is redrawn on every poll; it should not fade in each time
    const s = state.status;
    main.innerHTML = `<div class="empty"><h1 style="margin-bottom:8px">Fetching market history</h1>${esc(s?.sync?.error || s?.sync?.message || 'Starting…')}<br><span class="dim">The first sync downloads the last two weeks of exchange hours. This takes a few minutes.</span></div>`;
    return;
  }
  if (state.hideout) { main.classList.remove('enter'); document.body.classList.add('hideout'); try { await viewHideout(main); } catch (err) { main.innerHTML = `<div class="empty">${esc(err.message)}</div>`; } return; }
  if (!tourSeen() && !$('#tour')) openTour();  // first visit, once the desk has data to show
  const page = (location.hash.replace('#/', '') || 'desk').split('?')[0];
  const view = VIEWS[page] || viewDesk;
  renderNav(VIEWS[page] ? page : 'desk');
  const scroll = main.scrollTop;
  try { await view(main); if (turn === routing) main.scrollTop = scroll; }
  catch (err) { if (turn === routing) main.innerHTML = `<div class="empty">${esc(err.message)}</div>`; }
  // Elements made while "enter" is on play their entrance; drop it once this page has had time to.
  if (turn === routing && main.classList.contains('enter')) setTimeout(() => { if (turn === routing) main.classList.remove('enter'); }, 700);
}
async function poll(first) {
  try {
    const s = await api('/api/status');
    const changed = s.generation !== state.generation || s.ready !== state.status?.ready;
    state.status = s; state.league ||= s.league; state.generation = s.generation;
    announce(s.last_event);
    if (changed && state.hideout) pollEvents();  // right after the hourly refresh
    if (changed) showIcons();
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
  // The hideout window rings for itself, only if you turned that on there.
  if (!state.hideout && window.Notification?.permission === 'granted') new Notification(event.title, { body: event.body, icon: '/mark.svg' });
}
// A new page fades in; refreshes and actions re-render in place without replaying it (see route).
window.addEventListener('hashchange', () => { closeDrawer(); $('#main').scrollTop = 0; if (!state.hideout) $('#main').classList.add('enter'); route(); });
window.addEventListener('keydown', e => {
  if (e.target.matches('input,select,textarea')) { if (e.key === 'Escape') e.target.blur(); return; }
  if (e.key === 'Escape') { if ($('#tc') && !$('#tc').hidden) closeCheck(); else if (tourOpen()) closeTour(); else closeDrawer(); }
  else if (e.ctrlKey || e.metaKey || e.altKey) return;
  else if (e.key === '?') { e.preventDefault(); if (!state.hideout) openTour(); }
  else if (e.key === 'h' || e.key === 'H') openHideout();
  else if (e.key === 'c' || e.key === 'C') { e.preventDefault(); openCheck(); }
  else if (e.key === '/') { e.preventDefault(); if (!$('#q')) location.hash = '#/markets'; setTimeout(() => $('#q')?.focus(), 150); }
});
showIcons();
if (!state.hideout) $('#main').classList.add('enter');
poll(true);
setInterval(poll, state.hideout ? 60000 : 20000);
if (state.hideout) { pollEvents(); setInterval(pollEvents, 60000); }
