import {esc,icon,num,signed,pct,changeTag,unit,assetArt,btn,ibtn,empty,panelHead,spark,date,time,age,source} from './ui.js';

export const pages={
  overview:['Overview','overview','Market overview','A unified view of your portfolio, market activity, and trading signals.','Workspace'],
  markets:['Market explorer','candles','Market explorer','Inspect settled prices, listing depth, and league-cycle comparisons.','Workspace'],
  routes:['Arbitrage scanner','routes','Arbitrage scanner','Explore funded conversion loops after whole-lot rounding and Gold costs.','Workspace'],
  portfolio:['Portfolio','portfolio','Portfolio ledger','Track capital, cost basis, and realized returns in this league.','Workspace'],
  backtest:['Backtest lab','lab','Backtest lab','Replay a strategy against historical prices with execution friction.','Research'],
  craft:['Crafting desk','craft','Crafting desk','Compare settlement, rerolls, and transformations on a common Chaos basis.','Research'],
  alerts:['Signal center','activity','Signal center','Follow market events and set targets for prices or route returns.','System'],
  sources:['Data sources','db','Data sources','Manage the feeds, snapshots, and storage behind your local terminal.','System'],
  hideout:['Hideout mode','hideout','Hideout mode','A compact readout for manual execution.','Workspace']
};
const pick=(s,id)=>s.d.assets.find(a=>a.id===id);
export const opt=(value,label,selected)=>`<option value="${esc(value)}"${String(value)===String(selected)?' selected':''}>${esc(label)}</option>`;
export const options=(s,selected,filter=()=>true)=>s.d.assets.filter(filter).map(a=>opt(a.id,a.name,selected)).join('');
export const field=(label,input,hint='')=>`<label class="field"><span>${label}</span>${input}${hint?`<small>${hint}</small>`:''}</label>`;
export const inp=(name,value,extra='')=>`<input name="${name}" value="${esc(value)}" required inputmode="decimal" autocomplete="off" ${extra}>`;
export const select=(name,items,extra='')=>`<select name="${name}" ${extra}>${items}</select>`;
const choices=(rows,sel)=>rows.map(([a,b])=>opt(a,b,sel)).join('');
const tiny=(label,tone='')=>`<span class="tag ${tone}">${label}</span>`;
const routeSymbols=(s,path)=>path.map(a=>`<span>${esc(pick(s,a)?.symbol??a)}</span>`).join(`<span class="route-arrow">→</span>`);
const sourceChip=w=>`<span class="source-chip ${w.source==='exchange'?'teal':''}"><i></i>${source(w.source)}</span>`;
export function titleBlock(s) {
  const [, ,title,subtitle,group]=pages[s.page],w=s.d.world;
  return `<div class="page-intro"><div><div class="eyebrow"><span class="eyeline"></span>${esc(group)} <span class="slash">//</span> ${s.page==='overview'?'Command center':'Terminal'}</div><h1>${title}</h1><p>${subtitle}</p></div>
    <div class="context-pick"><button class="leaguebtn" data-act="leagues"><span class="game-label">PoE ${w.game}</span><span>${esc(w.league)}</span>${icon('down',13)}</button><span class="context-source"><i class="source-dot"></i>${source(w.source)}${w.start&&w.asof>=w.start?` <em>/</em> Day ${Math.floor((w.asof-w.start)/86400)+1}`:''}</span></div></div>
    ${w.source==='demo'?`<div class="workspace-note demo-note"><span>${icon('lab',16)} <b>Demo portfolio</b> · Balances and trades here are simulated.</span>${btn('Return to personal desk','personal-desk','arrow','small')}</div>`:''}`;
}
function kpi(title,ic,tone,value,sub,graph,go) {
  return `<button class="kpi" data-act="go" data-page="${go}"><span class="kpi-title"><span class="kpi-icon ${tone}">${icon(ic,15)}</span>${title}</span><span class="kpi-value">${value}</span>
    <span class="kpi-desc">${sub}</span><span class="kpi-spark">${graph}</span></button>`;
}
function miniNetwork() {
  return `<svg class="network-art" viewBox="0 0 84 32" fill="none" aria-hidden="true"><g stroke="currentColor" stroke-width="1.2"><path d="m5 24 24-14 22 10 24-15M5 24l46-4M29 10l46-5" opacity=".65"/>${[[5,24],[29,10],[51,20],[75,5]].map(([x,y])=>`<circle cx="${x}" cy="${y}" r="3" fill="#191b22"/>`).join('')}</g></svg>`;
}
function miniGold(hist) {
  const values=hist.map(x=>Number(x.gold)),mi=Math.min(...values),ma=Math.max(...values);
  return `<svg class="gold-art" viewBox="0 0 85 32" aria-hidden="true">${values.filter((_,i)=>i%2===0).map((v,i)=>{const h=ma===mi?14:7+21*(v-mi)/(ma-mi);return `<rect x="${i*5.6}" y="${31-h}" width="3" height="${h}" rx=".6" fill="currentColor" opacity="${.32+i*.035}"/>`;}).join('')}</svg>`;
}
function kpis(s) {
  const d=s.d,p=d.portfolio,r=d.scanner,h=p.history,week=Number(h[0]?.worth),change=week?Math.trunc((Number(p.worth)-week)/week*10000):null;
  return `<div class="kpi-grid">
    ${kpi('Portfolio value','portfolio','lime',`${num(p.worth)} ${unit('c')}`,`${!p.entries.length?'No recorded entries':change===null?'Marked in Chaos':`${changeTag(change)} <span>7d</span>`}`,p.entries.length?spark(h.map(x=>x.worth),'lime',true):'','portfolio')}
    ${kpi('Realized P&L','trend','teal',`<span class="${Number(p.realized)<0?'red':'teal'}">${signed(p.realized)}</span> ${unit('c')}`,`${tiny(s.d.prefs.method,'subtle')} <span>cost basis</span>`,p.entries.length?spark(h.map(x=>x.realized),'teal',true):'','portfolio')}
    ${kpi('Arbitrage routes','routes','purple',`${String(r.routes.length).padStart(2,'0')} <span class="value-word">active</span>`,`<span class="purple">${r.accepted} screened</span> <span>quotes</span>`,r.accepted?miniNetwork():'','routes')}
    ${kpi('Gold reserve','coins','amber',`${num(p.gold,0,true)} ${unit('g')}`,`<span class="amber">${num(p.burned,0,true)} g</span> <span>spent</span>`,p.entries.length?miniGold(h):'','portfolio')}
  </div>`;
}
export function pricePanel(s) {
  const a=pick(s,s.asset)??s.d.assets[0];
  return `<section class="panel chart-panel">${panelHead('Price terminal','candles',`<div class="chart-modes">${['candles','line'].map(m=>`<button data-act="chart-mode" data-val="${m}" class="${s.mode===m?'sel':''}" aria-label="${m==='candles'?'Candlestick':'Line'} chart" aria-pressed="${s.mode===m}">${icon(m==='candles'?'candles':'market',14)}</button>`).join('')}</div>${ibtn('Expand chart','expand','expand')}`)}
  <div class="instrument"><div class="instrument-left">${assetArt(a,40)}<div><button class="instrument-pick" data-act="asset-picker">${esc(a.name)} ${icon('down',13)}</button><span class="pair-label">${esc(a.symbol)} <em>/</em> CHAOS</span></div></div>
    <div class="ranges" aria-label="Chart range">${['24H','7D','30D','90D'].map(x=>`<button class="${x===s.range?'on':''}" data-act="range" data-val="${x}" aria-pressed="${x===s.range}">${x}</button>`).join('')}</div></div>
  <div class="quote-line"><strong>${num(a.price,a.price!==null&&Number(a.price)<1?3:2)}</strong>${unit('c')}${changeTag(a.change)}<small>24h</small></div>
  <div id="plot" class="plot"><div class="loading">Loading price history…</div></div>
  <div class="plot-foot"><span><i class="legend-square"></i>Price <i class="legend-square volume"></i>Volume</span><span id="resolution">UTC</span></div>
  <details class="chart-origin"><summary id="chart-origin-label">Price data source</summary><div id="chart-origin-detail"></div></details></section>`;
}
const signalStyle={route:['routes','lime','Arbitrage'],filter:['shield','amber','Market integrity'],momentum:['trend','purple','Momentum'],
  data:['db','teal','Data feed'],alert:['alert','purple','Price alert'],ledger:['portfolio','teal','Ledger']};
export function signals(s,full=false) {
  const list=s.d.signals.slice(0,full?60:3);
  if(!list.length)return empty('No signals yet','New market events will appear here.','activity');
  return `<div class="signal-list ${full?'full':''}">${list.map(e=>{
    const [ic,tone,label]=signalStyle[e.kind]??signalStyle.data;
    const route=e.kind==='route', parts=e.body.split(' · ');
    const desc=route?`${parts[0].split(' → ').map(id=>esc(pick(s,id)?.symbol??id)).join(' <span class="glyph-arrow">→</span> ')}${parts[1]?`<span class="signal-profit">${esc(parts[1].replace(/(\d+\.\d+)/,x=>num(x)))}</span>`:''}`:esc(e.body);
    return `<article class="signal-row"><div class="signal-icon ${tone}">${icon(ic,15)}</div><div class="signal-content"><div class="signal-meta"><span class="${tone}">${label}</span><time>${full?date(e.t)+' · '+time(e.t):age(e.t,s.d.clock)}</time></div><h3>${esc(e.title)}</h3><p class="${route?'route-desc':''}">${desc}</p></div></article>`;
  }).join('')}</div>`;
}
function feedPanel(s) {
  return `<section class="panel feed-panel">${panelHead('Signal feed','activity',`<span class="count-pill">${s.d.signals.length.toString().padStart(2,'0')}</span>`)}
    ${signals(s)}<button class="panel-foot-link" data-act="go" data-page="alerts">Open signal center ${icon('arrow',14)}</button></section>`;
}
export function tableRows(s,all=false) {
  let rows=s.d.assets.filter(a=>(!s.onlyPins||s.pins.includes(a.id))&&(!all||a.name.toLowerCase().includes(s.query.toLowerCase())||a.symbol.toLowerCase().includes(s.query.toLowerCase())));
  if(s.sort) {
    const [key,dir]=s.sort.split(':');rows=rows.slice().sort((a,b)=>(key==='name'?a.name.localeCompare(b.name):Number(a[key])-Number(b[key]))*(dir==='desc'?-1:1));
  }
  if(!all)rows=rows.slice(0,6);
  if(!rows.length)return `<tr><td colspan="8">${empty('No matching assets',s.onlyPins?'Star assets in the market table to build your watchlist.':'Try another name or ticker.','search')}</td></tr>`;
  return rows.map(a=>`<tr><td class="starcell"><button data-act="pin" data-id="${esc(a.id)}" class="star ${s.pins.includes(a.id)?'pinned':''}" aria-label="${s.pins.includes(a.id)?'Unstar':'Star'} ${esc(a.name)}">${icon('star',14)}</button></td>
    <td><button class="asset-cell" data-act="select-asset" data-id="${esc(a.id)}">${assetArt(a,31)}<span><b>${esc(a.name)}</b><small>${esc(a.symbol)}</small></span></button></td>
    <td><span class="category"><i></i>${esc(a.kind)}</span></td><td class="numeric">${num(a.price,a.price!==null&&Number(a.price)<1?3:2)} ${unit('c')}</td><td>${changeTag(a.change)}</td>
    <td class="trend-cell">${spark(a.trend,Number(a.trend.at(-1))>=Number(a.trend[0])?'lime':'red')}</td><td class="numeric volume-cell">${num(a.volume,0)}</td><td>${ibtn('Inspect '+a.name,'select-asset','upRight',`data-id="${a.id}"`)}</td></tr>`).join('');
}
function marketTable(s,all=false) {
  const hdr=(label,k)=>`<button data-act="sort" data-val="${k}">${label}${s.sort.startsWith(k+':')?icon('sort',10):''}</button>`;
  return `<section class="panel markets-table">${panelHead(all?'Asset directory':'Market watch','watch',`<button class="watch-btn ${s.onlyPins?'on':''}" data-act="pins">${icon('star',13)}Starred</button>${!all?`<span class="head-divider"></span><button class="text-link" data-act="go" data-page="markets">View markets ${icon('upRight',13)}</button>`:''}`)}
    ${all?`<div class="table-search">${icon('search',15)}<input aria-label="Filter assets" id="asset-search" placeholder="Search assets or tickers…" value="${esc(s.query)}"><span>${s.d.assets.length} assets</span></div>`:''}
    <div class="table-scroll"><table class="market-grid"><thead><tr><th></th><th>${hdr('Asset','name')}</th><th>Class</th><th>${hdr('Unit price','price')}</th><th>${hdr('24h change','change')}</th><th>7d trend</th><th>${hdr('24h volume','volume')}</th><th></th></tr></thead><tbody id="asset-rows">${tableRows(s,all)}</tbody></table></div></section>`;
}
function overview(s){return kpis(s)+(s.d.world.source!=='demo'&&!s.d.portfolio.entries.length?`<div class="workspace-note"><span>${icon('portfolio',16)} <b>Your ledger is empty.</b> Choose your league, then record your opening balance.</span>${btn('Record first entry','trade','plus','small')}</div>`:'')+`<div class="overview-grid">${pricePanel(s)}${feedPanel(s)}</div>`+marketTable(s);}
function markets(s) {
  return `<div class="overview-grid markets-split">${pricePanel(s)}<section class="panel">${panelHead('Market depth','layers',ibtn('Add observed listing','quote','plus'))}
    <div class="padded" id="depth">Loading observed depth…</div></section></div>
    <section class="panel macro-panel">${panelHead('League-cycle outlook','pulse',tiny('DAY-ALIGNED'))}<div id="macro" class="padded muted">Loading lifecycle comparison…</div></section>${marketTable(s,true)}`;
}
function routes(s) {
  const r=s.scanR??s.d.scanner;
  return `<section class="panel">${panelHead('Scanner parameters','filter',btn('Record listing','quote','plus'))}<form class="toolbar-form" data-form="scan">
    ${field('Chaos bankroll',inp('capital',r.capital))}${field('Gold budget',inp('gold',r.gold))}${field('Maximum legs',select('legs',choices([2,3,4,5,6].map(n=>[n,`${n} legs`]),s.scanLegs??s.d.prefs.maxLegs)))}
    ${btn('Evaluate graph','submit','play','primary','type="submit"')}</form></section>
    <div class="stat-strip">${metric('Funded routes',String(r.routes.length).padStart(2,'0'),'lime')}${metric('Accepted quotes',r.accepted)}${metric('Filtered quotes',r.rejected.length,'amber')}${metric('Evaluation',`${num(r.ms,2)} ms`,'teal')}</div>
    <section class="panel">${panelHead('Conversion windows','routes',tiny(`${r.visited} EDGES TESTED`))}<div class="route-list">${r.routes.map((x,i)=>`<button class="route-row" data-act="route-detail" data-index="${i}">
    <span class="rank">#${String(i+1).padStart(2,'0')}</span><div><div class="route-chain">${routeSymbols(s,x.path)}</div><small>${x.steps.length} legs <em>·</em> ${num(x.start,0)} c deployed <em>·</em> ${num(x.gold,0)} g</small></div><div class="route-earn"><b>+${num(x.net)} c</b>${changeTag(x.yieldBps)}</div>${icon('right',15)}</button>`).join('')||empty('No funded route clears friction','Check your ledger balance, current listing depth, and scanner limits.','routes')}</div>
    <div class="inline-foot">${icon('clock',14)} ${r.truncated?'Scan budget reached; partial search.':'Simple cycles evaluated within the selected leg limit.'} Intermediate dust is excluded from profit.</div></section>
    <section class="panel">${panelHead('Liquidity review','shield',tiny('FILTER LOG'))}<div class="table-scroll"><table><thead><tr><th>Listing</th><th>Ratio</th><th>Reason excluded</th><th>Snapshot</th><th></th></tr></thead><tbody>${r.rejected.slice(0,200).map(q=>`<tr><td>${esc(q.src)} → ${esc(q.dst)}</td><td class="numeric">${esc(q.pay)} : ${esc(q.get)}</td><td>${tiny(esc(q.reason),'amber')}</td><td>${time(q.seen)}</td><td>${btn('Review','review-quote','','small',`data-id="${esc(q.id)}"`)}</td></tr>`).join('')||`<tr><td colspan="5" class="muted">No excluded listings.</td></tr>`}</tbody></table></div></section>`;
}
export const metric=(title,value,tone='')=>`<div class="metric"><span>${title}</span><strong class="${tone}">${value}</strong></div>`;
function portfolio(s) {
  const p=s.d.portfolio,tab=s.portTab;
  const tabs=['Holdings','Ledger','Sessions','Stash'];
  const rows=tab==='Holdings'?p.holdings.map(x=>`<tr><td><div class="asset-cell">${assetArt(x,28)}<b>${esc(x.name)}</b></div></td><td>${num(x.qty,0)}</td><td>${num(x.average)} c</td><td>${num(x.value)} c</td><td class="${Number(x.unrealized)>=0?'green':'red'}">${signed(x.unrealized)} c</td><td>${x.stale?tiny(`Review · ${x.age}h`,'amber'):`${x.age}h`}</td></tr>`):
    tab==='Ledger'?p.entries.map(t=>`<tr><td>${date(t.t)} <small>${time(t.t)}</small></td><td>${tiny(esc(t.side),t.side==='sell'?'lime':'')}</td><td>${esc(pick(s,t.asset)?.name??'Gold')}</td><td>${num(t.qty,0)}</td><td>${t.side==='deposit'?'—':num(t.price)+' c'}</td><td>${num(t.gold,0)} g</td><td>${t.pnl===null?'—':signed(t.pnl)+' c'}</td></tr>`):
    tab==='Sessions'?p.sessions.map(x=>`<tr><td>${esc(x.name)}</td><td>${x.fills}</td><td>${date(x.first)} · ${time(x.first)}</td><td>${num(x.gold,0)} g</td><td>${signed(x.pnl)} c</td><td>${num(x.pnlHour)} c/h</td></tr>`):p.stash.map(x=>`<tr><td>${esc(pick(s,x.id)?.name??x.id)}</td><td>${num(x.qty,0)}</td><td>${x.value===null?'Unpriced':num(x.value)+' c'}</td></tr>`);
  const heads=tab==='Holdings'?['Asset','Units','Avg. basis','Mark value','Unrealized','Idle']:tab==='Ledger'?['Recorded (UTC)','Entry','Asset','Units','Unit price','Gold','Realized']:tab==='Sessions'?['Session','Fills','First entry','Gold spent','Realized','P&L / hour']:['Observed asset','Units','Mark value'];
  return `<div class="stat-strip">${metric('Portfolio value',num(p.worth)+' c','lime')}${metric('Available Chaos',num(p.cash)+' c')}${metric('Realized P&L',signed(p.realized)+' c','teal')}${metric('Gold reserve',num(p.gold,0,true)+' g','amber')}</div>
  <section class="panel">${panelHead('Inventory & accounting','portfolio',btn('Record entry','trade','plus','primary'))}<div class="tabs border">${tabs.map(t=>`<button data-act="port-tab" data-val="${t}" class="${tab===t?'active':''}">${t}</button>`).join('')}</div>
    ${tab==='Stash'?`<div class="inline-foot">${icon('eye',14)} Observed inventory · ${p.stashAt?date(p.stashAt)+' '+time(p.stashAt):'No snapshot yet'} · Separate from ledger cost basis. ${p.stashAt?'Priced subtotal: '+num(p.stashValue)+' c.':''}</div>`:''}
    <div class="table-scroll"><table><thead><tr>${heads.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.join('')||`<tr><td colspan="${heads.length}">${empty(tab==='Ledger'?'No transactions recorded':tab==='Holdings'&&!p.entries.length?'Your ledger is empty':'No '+tab.toLowerCase()+' yet',tab==='Stash'?'Import a stash snapshot in Data sources.':tab==='Sessions'?'Sessions appear when you record purchases or sales.':'Use Record entry to add a Chaos or Gold deposit, or inventory you already own.','portfolio')}</td></tr>`}</tbody></table></div></section>
  <div class="two-col"><section class="panel">${panelHead('Asset allocation','layers',tiny('MARK VALUE'))}<div class="padded">${Object.entries(p.allocation).map(([k,v],i)=>`<div class="alloc-row"><div><span>${k==='Currency'?'Liquid currency':k==='Commodity'?'Bulk commodities':k==='Craft'?'Illiquid crafts':'Divination cards'}</span><b>${num(v)} c</b></div><div class="track"><i style="width:${Number(p.worth)?Math.min(100,Number(v)/Number(p.worth)*100):0}%;background:var(--${['lime','teal','purple','amber'][i]})"></i></div></div>`).join('')}</div></section>
  <section class="panel">${panelHead('Capital review','clock',ibtn('Accounting settings','settings','settings'))}<div class="padded"><div class="kv"><span>P&L / trading hour</span><b>${num(p.pnlHour)} c</b></div><div class="kv"><span>Traded notional</span><b>${num(p.turnover)} c</b></div>${p.holdings.filter(x=>x.stale).map(x=>`<div class="notice"><span class="amber">${icon('clock',14)}</span><span>${esc(x.name)} idle for ${x.age}h. ${x.markdown?`Consider marking down to <b>${num(x.markdown)} c</b>.`:'A price observation is needed.'} ${x.redeploy?`Proceeds could fund ${num(x.redeploy.units,0)} × ${esc(x.redeploy.name)} (${pct(x.redeploy.change)} in 24h).`:''}</span></div>`).join('')||'<div class="inline-note">No holdings exceed the idle threshold.</div>'}${p.unpriced?`<div class="notice">${p.unpriced} unpriced holdings omitted from marked value.</div>`:''}</div></section></div>`;
}
function backtest(s) {
  const v={asset:s.asset,strategy:'reversion',days:90,capital:'5000',gold:'1000000',window:18,band:35,participation:300,slip:12,impact:100,fee:35,delay:1,decay:3,...s.btParams},r=s.backR;
  return `<div class="tool-grid"><section class="panel">${panelHead('Strategy setup','lab')}<form data-form="backtest" class="form-body">
    ${field('Instrument',select('asset',options(s,v.asset,a=>a.id!=='chaos'&&a.price!==null)))}${field('Strategy',select('strategy',choices([['reversion','Mean reversion'],['momentum','Momentum breakout'],['hold','Buy & hold'],['rotation','Relative strength · currency basket']],v.strategy)))}
    <div class="field-pair">${field('History window',select('days',choices([7,30,90,365,3650].map(n=>[n,n+' days']),v.days)))}${field('Lookback (bars)',inp('window',v.window))}</div>
    <div class="field-pair">${field('Starting Chaos',inp('capital',v.capital))}${field('Starting Gold',inp('gold',v.gold))}</div>
    <details><summary>Execution assumptions</summary><div class="field-pair details-grid">${[['band','Signal band (bps)'],['participation','Participation (bps)'],['slip','Slippage (bps)'],['impact','Max impact (bps)'],['fee','Gold / unit'],['delay','Lag (bars)'],['decay','Decay / bar (bps)']].map(([k,l])=>field(l,inp(k,v[k]))).join('')}</div></details>
    <button class="btn primary" type="submit">${icon('play',15)}Run backtest</button></form></section>
    <section class="panel">${panelHead('Replay results','market',r?tiny(num(r.ms,2)+' MS','lime'):tiny('AWAITING RUN'))}${!r?`<div class="lab-idle">${icon('pulse',42)}<h3>Ready to replay</h3><p>Choose your strategy and bankroll, then run a historical simulation.</p></div>`:
      `<div class="padded"><div class="result-label">Ending equity</div><div class="big-result">${num(r.finalEquity)} ${unit('c')} ${changeTag(r.returnBps)}</div><div class="equity-chart">${spark(r.curve.map(x=>x.value),Number(r.profit)>=0?'lime':'red',true)}</div><div class="equity-axis"><span>${date(r.curve[0].t)}</span><span>${r.bars.toLocaleString()} bars</span><span>${date(r.curve.at(-1).t)}</span></div>
        <div class="result-grid">${[['Total P&L',signed(r.profit)+' c'],['Realized P&L',signed(r.realized)+' c'],['Max drawdown',(r.drawdownBps/100).toFixed(2)+'%'],['Turnover',num(r.turnover)+'×'],['Sharpe / Sortino',num(r.sharpe)+' / '+num(r.sortino)],['Longest lockup',num(r.lockHours,1)+' h'],['Gold burn',num(r.goldBurn,0,true)+' g'],['Net / 1M Gold',num(r.efficiency)+' c']].map(([k,x])=>metric(k,x)).join('')}</div></div>`}</section></div>
    ${r?`<section class="panel">${panelHead('Simulated executions','list',tiny(`${r.fills} FILLS · ${r.partials} PARTIAL`))}<div class="table-scroll"><table><thead><tr><th>Hour (UTC)</th><th>Asset</th><th>Side</th><th>Units</th><th>Fill price</th><th>Gold spent</th></tr></thead><tbody>${r.trades.map(t=>`<tr><td>${date(t.t)} · ${time(t.t)}</td><td>${esc(pick(s,t.asset??v.asset)?.symbol)}</td><td>${tiny(t.side,t.side==='buy'?'teal':'purple')}</td><td>${num(t.qty,0)}</td><td>${num(t.price,4)} c</td><td>${num(t.gold,0)} g</td></tr>`).join('')||'<tr><td colspan="6">No fills under these assumptions.</td></tr>'}</tbody></table></div></section>`:''}`;
}
function crafting(s) {
  const v={settle:'85',base:'40',cost:'12',success:'210',fallback:'25',chance:3000,demand:9200,sellTime:8,rollTime:3,...s.craftParams};
  const t={preset:'card',inputQty:8,outputQty:1,inputPrice:pick(s,'patient')?.price??'4.2',outputPrice:pick(s,'nurse')?.price??'35.7',gold:300,minutes:4,...s.transParams},r=s.craftR,k=s.transR;
  return `<div class="two-col"><section class="panel">${panelHead('Settle or reroll','craft',tiny('EXPECTED VALUE','purple'))}<form class="form-body" data-form="craft"><p class="form-lead">Set outcome prices and the probability of a sale within your chosen settlement window.</p>
    <details><summary>Use market observations</summary><div class="details-grid">${field('Price intermediate from market',select('mark',opt('','Choose a marked asset','')+options(s,'',a=>a.price!==null),'data-change="settle-mark"'))}${field('Price one reroll material',select('material',opt('','Choose a marked material','')+options(s,'',a=>a.price!==null),'data-change="material-mark"'))}${field('Build demand sample',select('cohort',opt('','Select a recent cohort','')+(s.d.demand??[]).map((r,i)=>opt(i,`${r.build} · ${r.sold}/${r.listed} sold`,null)).join(''),'data-change="demand"'),'Observed sell-through within the reported time window.')}</div></details>
    <div class="field-pair">${[['settle','Intermediate sale (c)'],['base','Base / sunk cost (c)'],['success','Success sale (c)'],['fallback','Fallback sale (c)'],['cost','Reroll cost (c)'],['chance','Success chance (bps)'],['demand','Sale probability (bps)'],['sellTime','Settlement (min)'],['rollTime','Extra roll time (min)']].map(([a,b])=>field(b,inp(a,v[a]))).join('')}</div>
    <button class="btn primary">${icon('bolt',14)}Compare decisions</button>${r?`<div class="calc-result"><div class="label">Higher EV per hour</div><h3>${r.best}</h3><div class="kv"><span>Settle · ${num(r.settle)} c EV</span><b>${num(r.settleHour)} c/h</b></div><div class="kv"><span>Reroll · ${num(r.roll)} c EV</span><b>${num(r.rollHour)} c/h</b></div></div>`:''}</form></section>
  <section class="panel">${panelHead('Transformation yield','layers',tiny('WHOLE BATCH'))}<form class="form-body" data-form="transform">${field('Transformation',select('preset',choices([['card','Card set completion'],['vendor','Vendor recipe'],['bulk','Bulk vs. single sale'],['craft','Craft material conversion']],t.preset),'data-change="preset"'))}
    <div class="field-pair">${[['inputQty','Input units'],['outputQty','Output units'],['inputPrice','Cost / input (c)'],['outputPrice','Sale / output (c)'],['gold','Gold per batch'],['minutes','Minutes per batch']].map(([a,b])=>field(b,inp(a,t[a]))).join('')}</div><p class="inline-note">Input and output prices are editable scenario assumptions.</p>
    <button class="btn">${icon('play',14)}Evaluate batch</button>${k?`<div class="calc-result"><div class="label">Net batch profit</div><h3>${signed(k.net)} c</h3><div class="kv"><span>Return on cost</span><b>${pct(k.yieldBps)}</b></div><div class="kv"><span>Throughput</span><b>${num(k.hour)} c/h</b></div></div>`:''}</form></section></div>`;
}
function alerts(s) {
  return `<div class="two-col"><section class="panel">${panelHead('Alert rules','alert',btn('New alert','new-alert','plus','primary'))}<div class="padded">
    ${s.d.alerts.map(a=>`<div class="alert-row"><div><strong>${a.kind==='route'?'Route net return':esc(pick(s,a.asset)?.name)}</strong><small>${esc(a.op)} ${num(a.threshold,2)} c · ${a.hot?'condition met':'waiting'}</small></div>${btn(a.enabled?'On':'Off','toggle-alert','',a.enabled?'small enabled':'small',`data-id="${a.id}"`)}${ibtn('Delete alert','del-alert','trash',`data-id="${a.id}"`)}</div>`).join('')||empty('No target alerts','Set a price or net-profit threshold for this workspace.','alert')}
    <button class="btn" data-act="desktop">${icon('monitor',14)}${globalThis.Notification?.permission==='granted'?'Desktop notifications enabled':'Enable desktop notifications'}</button></div></section>
    <section class="panel">${panelHead('Signal history','activity',tiny(`${s.d.signals.length} EVENTS`))}${signals(s,true)}</section></div>`;
}
function sources(s) {
  const f=s.data;
  if(!f)return `<div class="panel padded">Loading source status…</div>`;
  return `<div class="two-col"><section class="panel">${panelHead('Currency exchange','globe',tiny('READ ONLY','teal'))}<div class="padded"><p class="form-lead">Hourly settled trades from Grinding Gear Games. Each digest covers every league with reported activity.</p>
    ${f.realms.map(r=>`<div class="source-row"><span class="game-label">PoE ${r.game}</span><span><b>${r.error?'Sync interrupted':r.success?'Digest received':'Ready to connect'}</b><small>${r.error?esc(r.error):r.success?`${date(r.success)} · ${time(r.success)} · ${num(r.markets,0)} pairs`:'Public feed · no API key required'}</small></span></div>`).join('')}
    <div class="action-row">${btn('Sync both games','sync','refresh','primary')}${btn(f.automatic?'Auto-sync on':'Auto-sync off','auto','clock',f.automatic?'enabled':'')}</div></div></section>
  <section class="panel">${panelHead('Snapshot intake','upload',tiny('LOCAL JSON'))}<div class="padded"><p class="form-lead">Import hourly bars, verified listing lots, or a stash snapshot. Each game and league has its own durable workspace.</p>
    <label class="dropzone">${icon('upload',23)}<strong>Choose a snapshot file</strong><span>Normalized JSON · up to 20 MiB</span><input id="import-file" type="file" accept=".json,application/json"></label>
    <div class="action-row"><a class="text-link" href="/example.json" download>Download example ${icon('download',13)}</a><span class="dim">Directory watch: ${f.watched?'on':'off'}</span></div>${f.inbox.error?`<div class="error">${esc(f.inbox.error)}</div>`:''}</div></section></div>
  <section class="panel">${panelHead('Personal workspaces','layers',btn('Open demo','open-demo','lab')+btn('New workspace','new-world','plus'))}<div class="table-scroll"><table><thead><tr><th>League</th><th>Game</th><th>Source</th><th>Last observation</th><th></th></tr></thead><tbody>${f.worlds.filter(w=>w.source!=='demo').map(w=>`<tr><td><b>${esc(w.league)}</b></td><td>PoE ${w.game}</td><td>${sourceChip(w)}</td><td>${w.asof?date(w.asof)+' · '+time(w.asof):'No observations yet'}</td><td>${btn(w.id===s.w?'Selected':'Open','world','',w.id===s.w?'small enabled':'small',`data-id="${w.id}"`)}</td></tr>`).join('')}</tbody></table></div></section>
  <div class="two-col"><section class="panel">${panelHead('Public listing stream','eye',tiny('POE 1'))}<div class="padded"><p class="form-lead">Delayed public-stash observations enter liquidity review until you confirm availability and Gold cost.</p>${f.listings?.available?`<div class="action-row">${btn('Read next page','listings-pull','refresh')}${btn(f.listings.automatic?'Auto-read on':'Auto-read off','listings-auto','clock')}</div><p class="inline-note">${esc(f.listings.error??(f.listings.last?'Last page: '+time(f.listings.last):'Ready to read'))}</p>`:'<p class="inline-note">Set POE_PUBLIC_STASH_TOKEN with service:psapi access to connect.</p>'}</div></section>
  <section class="panel">${panelHead('Local storage','db',tiny('SQLITE · WAL'))}<div class="padded"><div class="kv"><span>Database size / archived imports</span><b>${(f.bytes/1048576).toFixed(1)} MiB / ${f.archived}</b></div><div class="action-row"><a class="btn" href="/api/backup" download>${icon('download',14)}Download database backup</a>${f.stashAvailable?btn('Read selected stash tabs','stash-sync','refresh'):''}${btn('Workspace settings','settings','settings')}</div></div></section></div>`;
}
function hideout(s) {
  const r=s.d.scanner.routes[0],p=s.d.portfolio;
  return `<div class="hide-header"><div><img src="/mark.svg" width="32" alt=""><b>Vaal Street</b><span class="tag lime">HIDEOUT</span></div><div>${s.detached?'':ibtn('Pop out compact overlay','popout','upRight')}${btn('Full terminal','exit-hideout','expand')}</div></div>
    <div class="compact-bar"><span class="game-label">PoE ${s.d.world.game}</span><button class="text-link" data-act="leagues">${esc(s.d.world.league)} ${icon('down',12)}</button>${sourceChip(s.d.world)}</div>
    <div class="stat-strip">${metric('Free Chaos',num(p.cash,0)+' c','lime')}${metric('Gold',num(p.gold,0,true)+' g','amber')}</div>
    <section class="panel">${panelHead('Best funded route','routes')}${r?`<button class="compact-route" data-act="route-detail" data-index="0"><div class="route-chain">${routeSymbols(s,r.path)}</div><strong>+${num(r.net)} c</strong><small>${num(r.gold,0)} Gold · ${r.steps.length} manual legs</small></button>`:empty('No open routes','Awaiting funded listing depth.','routes')}</section>
    <section class="panel">${panelHead('Pinned prices','star')}<div class="padded">${s.d.assets.filter(x=>s.pins.includes(x.id)).map(a=>`<div class="kv"><span class="asset-cell">${assetArt(a,23)}${esc(a.name)}</span><b>${num(a.price)} c ${changeTag(a.change)}</b></div>`).join('')||'<span class="muted">Star assets in Market watch to pin prices.</span>'}</div></section>
    <div class="action-row">${btn('Record entry','trade','plus')}${btn('Set target','new-alert','alert')}</div>`;
}
export const renderView=s=>s.page==='hideout'?hideout(s):titleBlock(s)+({overview,markets,routes,portfolio,backtest,craft:crafting,alerts,sources}[s.page])(s);
