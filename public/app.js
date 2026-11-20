import {$,esc,icon,num,unit,pct,time,date,source,assetArt,btn,ibtn,chartSvg,chartAxis,empty} from './ui.js';
import {pages,renderView,tableRows,field,inp,select,opt,options} from './views.js';

const ls=(k,f)=>{try{return JSON.parse(localStorage.getItem('vaal:'+k))??f;}catch{return f;}};
const save=(k,v)=>localStorage.setItem('vaal:'+k,JSON.stringify(v));
const getPage=()=>Object.hasOwn(pages,location.hash.slice(1))?location.hash.slice(1):'overview';
const params=new URLSearchParams(location.search);
const s={w:null,detached:params.has('compact'),page:getPage(),worlds:[],d:null,asset:'divine',range:ls('range','7D'),mode:ls('chart-mode','candles'),
  pins:[],onlyPins:false,sort:'',query:'',portTab:'Holdings',scanR:null,backR:null,craftR:null,transR:null,online:true,
  selectedGame:1,data:null,chart:null,prevPage:'overview'};
let generation=0,loadId=0,chartId=0,stream,pending,clockTimer,needsRefresh=false;
const main=$('#main'),dlg=$('#modal');
const asset=id=>s.d?.assets.find(x=>x.id===id);
function personalDesk(){
  const saved=ls('world',null),personal=s.worlds.filter(w=>w.source!=='demo');
  return (personal.find(w=>w.id===saved)??personal[0])?.id;
}
async function api(path,payload,query='') {
  const res=await fetch(`/api/${path}?${s.w?'w='+encodeURIComponent(s.w):''}${query}`,payload===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const j=await res.json();if(!res.ok)throw new Error(j.error??`HTTP ${res.status}`);return j;
}
function toast(message,bad=false) {
  const el=document.createElement('div');el.className='toast '+(bad?'bad':'');
  el.innerHTML=icon(bad?'shield':'check',16)+`<span>${esc(message)}</span>`;$('#toasts').append(el);setTimeout(()=>el.remove(),4400);
}
function modal(title,body,eyebrow='LOCAL TERMINAL',wide=false) {
  dlg.className=wide?'wide':'';dlg.innerHTML=`<div class="modal-head"><div><div class="eyebrow"><span class="eyeline"></span>${esc(eyebrow)}</div><h2>${title}</h2></div>${ibtn('Close dialog','close','close')}</div>${body}`;
  if(!dlg.open)dlg.showModal();
  const first=dlg.querySelector('[autofocus]');if(first)first.focus();
}
function errorIn(el,e) {
  let box=el.querySelector('.error');if(!box){box=document.createElement('div');box.className='error';box.setAttribute('role','alert');el.append(box);}box.textContent=e.message;
}
function nav(to) {
  if(to==='hideout'&&s.page!=='hideout')s.prevPage=s.page;
  if(dlg.open)dlg.close();document.body.classList.remove('nav-open');
  if(location.hash==='#'+to){s.page=to;render();}else location.hash=to;
}
function sidebar() {
  const items=g=>Object.entries(pages).filter(([k,v])=>v[4]===g&&k!=='hideout').map(([k,[label,ic]])=>`<button data-act="go" data-page="${k}" title="${label}" aria-label="${label}" class="navitem ${s.page===k?'current':''}" ${s.page===k?'aria-current="page"':''}>${icon(ic,17)}<span>${label}</span>${k==='routes'?`<small class="nav-count">${s.d.scanner.routes.length}</small>`:''}</button>`).join('');
  $('#sidebar').innerHTML=`<button class="brand" data-act="go" data-page="overview" aria-label="Vaal Street overview"><img src="/mark.svg" alt="" width="37" height="37"><span><b>Vaal Street<span class="brand-dot">.</span></b><small>TRADING TERMINAL</small></span></button>
    <nav>${['Workspace','Research','System'].map(g=>`<div class="navgroup"><div class="navlabel">${g}</div>${items(g)}</div>`).join('')}</nav>
    <div class="sidebar-bottom"><button class="hideout-btn" data-act="go" data-page="hideout" aria-label="Hideout mode" title="Hideout mode (H)">${icon('hideout',17)} Hideout mode <kbd>H</kbd></button><button class="local-box" data-act="go" data-page="sources" aria-label="Local workspace data sources"><span class="local-symbol">${icon('terminal',17)}</span><span><b>Local workspace</b><small><i class="dot"></i> Single player</small></span>${icon('right',12)}</button></div>`;
}
function topbar(){
  $('#topbar').innerHTML=`<div class="breadcrumb">${ibtn('Toggle navigation','menu','menu','id="hamburger"')}<span class="mobile-wordmark">Vaal Street<span>.</span></span>${icon('terminal',16)}<span class="crumb-base">Terminal</span><em>/</em><b>${pages[s.page][0]}</b></div>
    <div class="top-actions"><button class="command-button" data-act="search" aria-label="Search terminal">${icon('search',14)}<span>Search terminal</span><kbd>Ctrl K</kbd></button><span class="top-divider"></span><span class="engine-status"><i class="dot ${s.online?'':'offline'}"></i>${s.online?'Engine online':'Reconnecting'}</span><button class="bell-btn" data-act="go" data-page="alerts" aria-label="Open signal center">${icon('alert',17)}${s.d.signals.length?'<i></i>':''}</button><button class="avatar" data-act="settings" aria-label="Personal workspace settings">EX</button></div>`;
}
function footer() {
  $('#foot').innerHTML=`<span class="foot-mark">${icon('terminal',12)} VAAL STREET <em>/</em> v1.0</span><span><i class="source-dot"></i>${source(s.d.world.source)} <em>·</em> ${date(s.d.world.asof)} ${time(s.d.world.asof)}</span><span class="foot-end">${icon('shield',12)} Read-only ingestion</span>`;
}
function render(){
  if(!s.d)return; generation++;s.chart=null;
  document.body.classList.toggle('hideout',s.page==='hideout');sidebar();topbar();footer();
  document.title=`${pages[s.page][2]} · Vaal Street`;
  main.innerHTML=renderView(s);
  if(s.page==='overview'||s.page==='markets')loadChart();
  if(s.page==='sources'&&!s.data)loadData();
}
async function loadData(){
  try{s.data=await api('data');if(s.page==='sources')render();}
  catch(e){main.innerHTML=empty('Could not load data sources',e.message,'db');}
}
async function loadDesk(reset=false) {
  const id=++loadId,w=s.w;
  const [worlds,d]=await Promise.all([api('worlds'),api('desk')]);if(id!==loadId||w!==s.w)return;
  s.worlds=worlds;s.d=d;
  if(reset){
    s.asset=ls(`asset:${w}`,'divine');s.onlyPins=false;s.query='';s.sort='';s.scanR=null;s.scanLegs=null;s.backR=null;s.craftR=null;s.transR=null;s.btParams={};s.transParams={};s.craftParams={};s.data=null;
    s.pins=ls(`pins:${w}`,d.assets.filter(a=>['divine','exalted','mirror'].includes(a.id)).map(a=>a.id));
  }
  if(!asset(s.asset))s.asset=d.assets.find(a=>a.price!==null&&a.id!=='chaos')?.id??d.assets[0].id;
  render();
}
async function changeWorld(w) {
  s.w=w;if(dlg.open)dlg.close();await loadDesk(true);
  if(s.d.world.id!==w||s.w!==w)return;
  if(s.d.world.source!=='demo'&&!s.detached)save('world',w);
  const url=new URL(location.href);
  if(url.searchParams.has('world')){url.searchParams.set('world',w);history.replaceState(null,'',url);}
  toast(`${s.d.world.league} workspace opened`);
}
function drawChart(){
  const plot=$('#plot');if(!plot||!s.chart)return;
  const width=Math.max(270,plot.getBoundingClientRect().width);
  plot.innerHTML=chartSvg(s.chart,width,s.page==='markets'?229:190,s.mode);
  const r=$('#resolution');if(r)r.textContent=s.chart.interval?`${s.chart.interval}H INTERVAL  ·  UTC`:'UTC';
  const c=s.chart,label=$('#chart-origin-label'),detail=$('#chart-origin-detail');
  if(label&&detail){
    const provider=c.source==='demo'?'Synthetic demo':c.source==='import'?'Imported snapshot':c.localSnapshots?'Exchange feed + local snapshots':'GGG Currency Exchange';
    label.textContent=`${provider} · ${c.hours} of ${c.requestedHours} hourly observations`;
    let info=c.source==='demo'?'Prices and volume are generated locally for this replay.':c.source==='import'?'Prices and volume come from snapshots imported into this workspace.':
      'GGG publishes hourly traded quantities. The desk prices an asset from its settled Chaos ratio, or from up to three settled pairs within the same hour. Candles summarize hourly prices; volume counts asset units on the pricing leg.';
    if(c.localSnapshots)info+=' This workspace has also received local imports, which can supply or replace price bars.';
    if(c.hours===1)info+=' Only one hour is available, so there is no price trend yet.';
    if(c.hours)info+=` Stored window: ${new Date(c.first*1000).toISOString().slice(0,16).replace('T',' ')} – ${new Date(c.last*1000).toISOString().slice(0,16).replace('T',' ')} UTC.`;
    detail.textContent=info;
    if(c.source==='exchange'){
      const a=document.createElement('a');a.href='https://www.pathofexile.com/developer/docs/reference#currencyexchange';a.target='_blank';a.rel='noopener noreferrer';a.textContent='Official feed documentation ↗';detail.append(' ',a);
    }
  }
  const depth=$('#depth'),book=s.chart.depth;
  if(depth&&book){
    const group=(levels,name,color)=>`<div class="depth-title">${name}<span>Net c / unit · Units</span></div>${levels.slice(0,5).map(q=>`<div class="depth-level"><i style="width:${Number(BigInt(q.cumulative)*100n/BigInt(levels.at(-1).cumulative))}%;background:var(--${color})"></i><b class="${color}">${num(q.price,3)}</b><span>${num(q.qty,0)}</span></div>`).join('')||'<small class="muted">No accepted lots</small>'}`;
    depth.innerHTML=book.asks.length||book.bids.length?group(book.asks,'Asks','red')+`<div class="kv"><span>Net spread</span><b>${num(book.spread,3)} c</b></div>`+group(book.bids,'Bids','lime')+'<p class="inline-note">Widths show cumulative units. Prices include the configured Gold valuation.</p>':
      empty('No executable depth','Record a verified listing or import a current snapshot. Settlements alone do not supply depth.','layers');
  }
  const macro=$('#macro');if(macro){
    const m=s.chart.macro;
    macro.innerHTML=m.curves.length?`<div class="macro-grid"><div><span class="label">League day</span><strong>${m.day}</strong></div><div><span class="label">7-day scenario</span><strong class="lime">${num(m.projection)} c</strong></div><p>Applying the median same-day forward change from ${m.curves.map(c=>`${esc(c.league)} (${pct(c.change)})`).join(', ')}. ${s.d.world.source==='demo'?'Synthetic history.':'Historical scenario, not a guaranteed return.'}</p></div>`:
      `<div class="inline-note">${icon('clock',15)} ${m.day?'Day '+m.day+' · ':''}Import an earlier league with a start date and matching day + 7 prices for a comparison.</div>`;
  }
}
async function loadChart(){
  const id=++chartId,w=s.w,a=s.asset,gen=generation;
  try{
    const data=await api('chart',undefined,`&a=${encodeURIComponent(a)}&range=${s.range}`);
    if(w!==s.w||a!==s.asset||id!==chartId||gen!==generation)return;
    s.chart=data;drawChart();
  }catch(e){if(id===chartId&&$('#plot'))$('#plot').innerHTML=empty('Chart unavailable',e.message);}
}
function leagueModal(game=s.d.world.game) {
  s.selectedGame=game;
  const rows=s.worlds.filter(w=>w.game===game),demos=rows.filter(w=>w.source==='demo');
  const row=w=>`<button class="picker-row ${s.w===w.id?'selected':''}" data-act="world" data-id="${w.id}"><span class="picker-icon">${icon('layers',17)}</span><span><b>${esc(w.league)}</b><small>${source(w.source)} · ${w.asof?date(w.asof)+' '+time(w.asof):'No market observations yet'}</small></span>${s.w===w.id?icon('check',16):icon('right',13)}</button>`;
  modal('League workspace',`<div class="modal-body"><div class="tabs">${[1,2].map(g=>`<button class="${g===game?'active':''}" data-act="league-game" data-val="${g}">Path of Exile ${g}</button>`).join('')}</div>
    <div class="picker-list">${rows.filter(w=>w.source!=='demo').map(row).join('')}</div>
    <details ${s.d.world.source==='demo'?'open':''}><summary>Demo workspaces · simulated balances and trades</summary><div class="picker-list">${demos.length?demos.map(row).join(''):btn('Open demo','open-demo','lab','',`data-game="${game}"`)}</div></details>
    <div class="action-row"><button class="text-link" data-act="go" data-page="sources">${icon('db',14)} Manage market sources ${icon('arrow',14)}</button></div></div>`,'MARKET CONTEXT');
}
function pickAsset(id,where=false) {
  s.asset=id;save(`asset:${s.w}`,id);if(dlg.open)dlg.close();
  if(where&&s.page!=='markets')nav('markets');else render();
}
function palette(assetOnly=false) {
  const list=(query='')=>{
    const q=query.toLowerCase();
    const p=assetOnly?[]:Object.entries(pages).filter(([,v])=>v[0].toLowerCase().includes(q)).slice(0,7);
    const a=s.d.assets.filter(a=>(a.name+' '+a.symbol).toLowerCase().includes(q)).slice(0,40);
    return `${p.length?'<div class="label">Navigation</div>':''}${p.map(([id,v])=>`<button class="picker-row" data-act="go" data-page="${id}">${icon(v[1],16)}<span>${v[0]}</span>${icon('right',13)}</button>`).join('')}${a.length?'<div class="label">Instruments</div>':''}
      ${a.map(a=>`<button class="picker-row" data-act="${assetOnly?'pick':'select-asset'}" data-id="${a.id}">${assetArt(a,26)}<span>${esc(a.name)}</span><small>${num(a.price)} c</small></button>`).join('')}${!p.length&&!a.length?empty('No matches','Try an asset name, ticker, or page.','search'):''}`;
  };
  modal(assetOnly?'Select instrument':'Search terminal',`<div class="modal-body"><div class="search-box">${icon('search',17)}<input id="command-query" autofocus placeholder="${assetOnly?'Search instruments…':'Find a page, asset, or ticker…'}" aria-label="Search"></div><div id="command-results" class="picker-list command-results">${list()}</div></div>`,'QUICK ACCESS');
  $('#command-query').oninput=e=>$('#command-results').innerHTML=list(e.target.value);
  $('#command-query').onkeydown=e=>{if(e.key==='Enter')$('#command-results button')?.click();};
}
function tradeModal(side='buy') {
  const isDeposit=side==='deposit',a=asset(s.asset)?.id!=='chaos'?s.asset:s.d.assets.find(a=>a.id!=='chaos')?.id;
  modal('Record ledger entry',`<form class="form-body" data-form="trade">
    ${field('Entry type',select('side',[['buy','Purchase'],['sell','Sale'],['deposit','Deposit Chaos / Gold'],['opening','Opening inventory']].map(([k,v])=>opt(k,v,side)).join(''),'data-change="trade-side"'))}
    ${field('Asset',select('asset',isDeposit?opt('chaos','Chaos Orb','chaos')+opt('gold','Gold','chaos'):options(s,a,x=>x.id!=='chaos'),'data-change="trade-asset"'))}
    <div class="field-pair">${field('Quantity',inp('qty','1','autofocus'))}${!isDeposit?field('Unit price (c)',inp('price',asset(a)?.price??'0')):''}</div>
    ${!isDeposit?field('Gold spent',inp('gold','0')):''}${field('Session',inp('session',ls(`session:${s.w}`,'Default session'),'maxlength="80"'))}
    <details><summary>Entry time & note</summary><div class="details-grid">${field('Time (UTC, optional)','<input name="t" type="datetime-local">','Defaults to the workspace clock.')}${field('Note','<input name="note" placeholder="e.g. Evening mapping" maxlength="240">')}</div></details>
    <div class="inline-note">${icon('portfolio',15)} ${num(s.d.portfolio.cash)} c available · ${num(s.d.portfolio.gold,0)} Gold</div>
    <input type="hidden" name="id" value="${crypto.randomUUID()}"><button class="btn primary">${icon('check',15)}Save entry</button></form>`,s.d.world.source==='demo'?'DEMO LEDGER · SIMULATED':'PERSONAL LEDGER');
}
function newWorld(){
  modal('New personal workspace',`<form class="form-body" data-form="new-world">${field('Game',select('game',opt(1,'Path of Exile 1',1)+opt(2,'Path of Exile 2',1)))}${field('League name','<input name="league" required autofocus maxlength="100" placeholder="e.g. Standard">')}${field('League start date (optional)','<input type="date" name="start">')}<button class="btn primary">Open workspace</button></form>`,'LOCAL DATASET');
}
function quoteModal(q){
  const id=q?.dst??(s.asset!=='chaos'?s.asset:s.d.assets.find(x=>x.id!=='chaos')?.id);
  modal(q?'Review listing':'Record observed listing',`<form class="form-body" data-form="quote">${q?`<input type="hidden" name="review" value="yes"><input type="hidden" name="id" value="${esc(q.id)}">`:''}<div class="field-pair">${field('You pay',select('from',options(s,q?.src??'chaos')))}${field('You receive',select('to',options(s,id)))}</div>
    <div class="field-pair">${field('Pay units / lot',inp('pay',q?.pay??Math.max(1,Math.round(Number(asset(id)?.price??1)))))}${field('Receive units / lot',inp('get',q?.get??1))}</div>
    <div class="field-pair">${field('Available whole lots',inp('lots',q?.lots??1))}${field('Gold / lot',inp('gold',q?.id?.startsWith('ps-')?'':q?.gold??0))}</div>
    <label class="checkline"><input type="checkbox" name="online">Seller is online</label><label class="checkline"><input type="checkbox" name="confirmed">I confirmed this price and available depth</label>
    <div class="inline-note">${icon('shield',15)}Listings expire after ${s.d.prefs.ttl/60} minutes and are screened against recent settlements.</div>
    <button class="btn primary">${icon('check',15)}Save observation</button></form>`,'LOCAL ORDER BOOK');
}
function alertModal(kind='price') {
  modal('Create target alert',`<form class="form-body" data-form="alert">${field('Watch',select('kind',opt('price','Instrument price',kind)+opt('route','Best funded route profit',kind),'data-change="alert-kind"'))}
    ${kind==='price'?field('Instrument',select('asset',options(s,s.asset))):''}<div class="field-pair">${field('Trigger when',select('op',opt('above','At or above','above')+opt('below','At or below','above')))}${field('Threshold (c)',inp('threshold',kind==='price'?asset(s.asset)?.price??'1':'10'))}</div>
    <div class="inline-note">${icon('activity',15)}Triggers on entry into the target range; rearms after leaving it.</div><button class="btn primary">${icon('alert',15)}Create alert</button></form>`,'SIGNAL CENTER');
}
function settingsModal(){
  const p=s.d.prefs;
  modal('Workspace settings',`<form class="form-body" data-form="settings">${field('Cost basis',select('method',opt('FIFO','FIFO · first in, first out',p.method)+opt('WAC','Weighted average cost',p.method)))}
    ${field('Chaos value of 1 million Gold',inp('tax',p.tax),'Applied to scanner returns and newly recorded entries.')}
    <div class="field-pair">${field('Idle threshold (hours)',inp('staleHours',p.staleHours))}${field('Max route legs',inp('maxLegs',p.maxLegs))}</div>
    <div class="field-pair">${field('Quote lifetime (sec)',inp('ttl',p.ttl))}${field('Reference band (bps)',inp('tolerance',p.tolerance))}</div>
    <button class="btn primary">${icon('check',14)}Save settings</button></form>`,'PERSONAL WORKSPACE');
}
function routeDetail(i){
  const r=(s.page==='routes'&&s.scanR?s.scanR:s.d.scanner).routes[i];if(!r)return;
  modal('Conversion worksheet',`<div class="modal-body"><div class="worksheet-total"><div><span class="label">Net route return</span><strong>+${num(r.net)} ${unit('c')}</strong></div><span class="change green">${pct(r.yieldBps)}</span></div>
    <div class="worksheet">${r.steps.map((x,i)=>`<div><span class="step-number">${String(i+1).padStart(2,'0')}</span><section><b>${num(x.paid,0)} ${esc(asset(x.from)?.symbol)} → ${num(x.received,0)} ${esc(asset(x.to)?.symbol)}</b><small>${num(x.lots,0)} lots at ${esc(x.pay)} : ${esc(x.get)} · ${num(x.gold,0)} g${x.remainder!=='0'?` · ${x.remainder} ${esc(asset(x.from)?.symbol)} dust`:''}</small></section></div>`).join('')}</div>
    ${[['Starting capital',num(r.start,0)+' c'],['Returned capital',num(r.output,0)+' c'],['Gold burn',num(r.gold,0)+' g'],['Net per 1M Gold',num(r.efficiency)+' c']].map(([k,v])=>`<div class="kv"><span>${k}</span><b>${v}</b></div>`).join('')}<p class="inline-note">${icon('eye',14)} ${source(s.d.world.source)} · Manual execution worksheet.</p></div>`,'ARBITRAGE SCANNER');
}
async function sync(button,automatic) {
  button.disabled=true;const old=button.innerHTML;button.textContent='Connecting…';
  try{s.data=await api('sync',automatic?{automatic:!s.data.automatic}:{pull:true});s.worlds=s.data.worlds;render();
    const err=s.data.realms.find(x=>x.error);toast(automatic?'Auto-sync preference saved':err?err.error:'Exchange feeds checked',!!err&&!automatic);
  }finally{button.disabled=false;button.innerHTML=old;}
}
document.addEventListener('click',async e=>{
  if(e.target.closest('.skip')){e.preventDefault();main.focus();return;}
  const b=e.target.closest('[data-act]');if(!b)return;
  const a=b.dataset.act,id=b.dataset.id,v=b.dataset.val;
  try{
    if(a==='submit')return;
    if(a==='go')return nav(b.dataset.page);
    if(a==='close')return dlg.close();
    if(a==='menu')return document.body.classList.toggle('nav-open');
    if(a==='search'||a==='asset-picker')return palette(a==='asset-picker');
    if(a==='leagues'||a==='league-game')return leagueModal(a==='leagues'?s.d.world.game:Number(v));
    if(a==='world')return await changeWorld(id);
    if(a==='personal-desk')return await changeWorld(personalDesk());
    if(a==='open-demo'){
      b.disabled=true;
      try{
        const r=await api('demo',{game:Number(b.dataset.game??s.d.world.game)});await changeWorld(r.w);
        if(s.page==='sources')nav('portfolio');
      }finally{b.disabled=false;}
      return;
    }
    if(a==='range'){s.range=v;save('range',v);return render();}
    if(a==='chart-mode'){s.mode=v;save('chart-mode',v);return render();}
    if(a==='expand'){if(s.page!=='markets')return nav('markets');return chartFocus();}
    if(a==='pick'||a==='select-asset')return pickAsset(id,a==='select-asset');
    if(a==='pin'){s.pins=s.pins.includes(id)?s.pins.filter(x=>x!==id):[...s.pins,id];save(`pins:${s.w}`,s.pins);$('#asset-rows').innerHTML=tableRows(s,s.page==='markets');return;}
    if(a==='pins'){s.onlyPins=!s.onlyPins;return render();}
    if(a==='sort'){s.sort=v+':'+(s.sort===v+':desc'?'asc':'desc');return render();}
    if(a==='port-tab'){s.portTab=v;return render();}
    if(a==='trade')return tradeModal(Number(s.d.portfolio.cash)>0||s.d.portfolio.holdings.length?'buy':'deposit');
    if(a==='quote')return quoteModal();
    if(a==='review-quote')return quoteModal((s.scanR??s.d.scanner).rejected.find(q=>q.id===id));
    if(a==='new-world')return newWorld();
    if(a==='settings')return settingsModal();
    if(a==='new-alert')return alertModal();
    if(a==='route-detail')return routeDetail(Number(b.dataset.index));
    if(a==='exit-hideout')return nav(s.prevPage==='hideout'?'overview':s.prevPage);
    if(a==='popout'){
      const url=`${location.origin}/?compact=1&world=${encodeURIComponent(s.w)}#hideout`;
      if('documentPictureInPicture' in window){
        const pip=await window.documentPictureInPicture.requestWindow({width:460,height:540}),frame=pip.document.createElement('iframe');
        pip.document.title='Vaal Street · Hideout';pip.document.body.style.cssText='margin:0;background:#0d0f12';frame.style.cssText='border:0;width:100vw;height:100vh;display:block';frame.src=url;pip.document.body.replaceChildren(frame);
      }else if(!window.open(url,'vaal-hideout','popup,width=460,height=540'))throw new Error('Allow the local terminal to open its compact window.');
      return;
    }
    if(a==='toggle-alert'||a==='del-alert'){await api('alerts',{id,action:a==='toggle-alert'?'toggle':'delete'});await loadDesk();return;}
    if(a==='desktop'){if(!('Notification' in window))return toast('Desktop notifications are unavailable in this browser.',true);
      const perm=await Notification.requestPermission();toast(perm==='granted'?'Desktop alerts enabled':'Visual alerts remain active');return render();}
    if(a==='sync'||a==='auto')return await sync(b,a==='auto');
    if(a==='listings-pull'||a==='listings-auto'){b.disabled=true;try{s.data=await api('listings-sync',a==='listings-pull'?{pull:true}:{automatic:!s.data.listings.automatic});s.worlds=s.data.worlds;render();}finally{b.disabled=false;}return;}
    if(a==='stash-sync'){b.disabled=true;try{await api('stash-sync',{});await loadDesk();toast('Selected stash tabs refreshed');}finally{b.disabled=false;}}
  }catch(err){toast(err.message,true);}
});
function chartFocus(){
  if(!s.chart)return;
  modal(esc(asset(s.asset)?.name??'Instrument'),`<div class="large-plot"></div>`,'PRICE TERMINAL',true);
  const el=dlg.querySelector('.large-plot');el.innerHTML=chartSvg(s.chart,el.clientWidth,360,s.mode);
}
document.addEventListener('input',e=>{if(e.target.id==='asset-search'){s.query=e.target.value;$('#asset-rows').innerHTML=tableRows(s,true);}});
document.addEventListener('change',async e=>{
  const el=e.target;
  try{
    if(el.id==='import-file'&&el.files[0]){
      const file=el.files[0];if(file.size>20*1024*1024)throw new Error('Import limit is 20 MiB.');
      const raw=await file.text(),r=await fetch('/api/import',{method:'POST',headers:{'Content-Type':'application/json'},body:raw}),j=await r.json();
      if(!r.ok)throw new Error(j.error);await changeWorld(j.w);toast(j.duplicate?'Snapshot already stored':'Snapshot committed to local storage');return;
    }
    if(el.dataset.change==='trade-side')return tradeModal(el.value);
    if(el.dataset.change==='trade-asset'&&el.form.elements.price)el.form.elements.price.value=asset(el.value)?.price??'0';
    if(el.dataset.change==='alert-kind')return alertModal(el.value);
    if(el.dataset.change==='settle-mark'||el.dataset.change==='material-mark'){
      if(asset(el.value)?.price)el.form.elements[el.dataset.change==='settle-mark'?'settle':'cost'].value=asset(el.value).price;
    }
    if(el.dataset.change==='demand'&&el.value!==''){
      const r=s.d.demand[Number(el.value)];el.form.elements.demand.value=r.chance;el.form.elements.sellTime.value=r.minutes;if(asset(r.a)?.price)el.form.elements.settle.value=asset(r.a).price;
    }
    if(el.dataset.change==='preset'){
      const presets={card:[8,1,asset('patient')?.price??'4.2',asset('nurse')?.price??'35.7',300,4],vendor:[3,1,1,3.4,0,1],bulk:[20,20,2,2.4,500,3],craft:[10,1,1.5,19,300,4]};
      ['inputQty','outputQty','inputPrice','outputPrice','gold','minutes'].forEach((k,i)=>el.form.elements[k].value=presets[el.value][i]);
    }
  }catch(err){toast(err.message,true);if(el.type==='file')el.value='';}
});
document.addEventListener('submit',async e=>{
  const form=e.target;if(!form.dataset.form)return;e.preventDefault();
  const values=Object.fromEntries(new FormData(form)),k=form.dataset.form,submit=form.querySelector('button[type="submit"],button.primary,button:last-child');
  if(submit)submit.disabled=true;form.querySelector('.error')?.remove();
  try{
    if(k==='new-world'){const r=await api('workspace',values);return await changeWorld(r.w);}
    if(['trade','settings','alert','quote'].includes(k)){
      if(k==='trade'){if(values.t)values.t+='Z';save(`session:${s.w}`,values.session);}
      if(k==='quote'){values.online=values.online==='on';values.confirmed=values.confirmed==='on';}
      await api(k==='alert'?'alerts':k,values);s.scanR=null;dlg.close();await loadDesk();toast(({trade:'Ledger entry saved',alert:'Target alert created',quote:'Listing recorded',settings:'Workspace settings saved'})[k]);return;
    }
    if(k==='scan'){s.scanR=await api('scan',values);s.scanLegs=values.legs;}
    if(k==='backtest'){s.backR=await api('backtest',values);s.btParams=values;}
    if(k==='craft'){s.craftR=await api('craft',values);s.craftParams=values;}
    if(k==='transform'){s.transR=await api('transform',values);s.transParams=values;}
    render();
  }catch(err){errorIn(form,err);}finally{if(submit)submit.disabled=false;}
});
dlg.addEventListener('click',e=>{if(e.target===dlg){const r=dlg.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)dlg.close();}});
dlg.addEventListener('close',()=>{if(needsRefresh){needsRefresh=false;loadDesk().catch(e=>toast(e.message,true));}});
document.addEventListener('keydown',e=>{
  if(!s.d)return;
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();return palette();}
  if(e.target.closest('input,textarea,select,[contenteditable]')||dlg.open)return;
  if(e.key.toLowerCase()==='h')nav(s.page==='hideout'?s.prevPage:'hideout');
  if(e.key==='/'){e.preventDefault();palette();}
  if(e.key==='Escape')document.body.classList.remove('nav-open');
});
window.addEventListener('hashchange',()=>{s.page=getPage();if(s.page==='sources')s.data=null;render();window.scrollTo(0,0);});
window.addEventListener('resize',()=>{clearTimeout(clockTimer);clockTimer=setTimeout(drawChart,80);});
document.addEventListener('pointermove',e=>{
  const plot=e.target.closest('#plot'),bars=s.chart?.bars;if(!plot||!bars?.length)return;
  const box=plot.getBoundingClientRect(),axis=chartAxis(s.chart,box.width),index=axis.indexAt((e.clientX-box.left)*axis.w/box.width);
  const b=bars[index],line=plot.querySelector('#crosshair'),tip=plot.querySelector('.chart-tip');if(!line||!tip)return;
  if(!b){tip.hidden=true;line.setAttribute('visibility','hidden');return;}
  const x=axis.x(axis.slot(b.t));line.setAttribute('x1',x);line.setAttribute('x2',x);line.setAttribute('visibility','visible');
  tip.hidden=false;tip.style.left=Math.max(0,Math.min(box.width-166,x+9))+'px';tip.innerHTML=`<b>${date(b.t)} · ${time(b.t)}</b><span>O ${num(b.o)} &nbsp; H ${num(b.h)}</span><span>L ${num(b.l)} &nbsp; C ${num(b.c)}</span><span>Vol ${num(b.v,0)}</span>`;
});
document.addEventListener('pointerout',e=>{const plot=e.target.closest?.('#plot');if(plot&&!plot.contains(e.relatedTarget)){const tip=plot.querySelector('.chart-tip');if(tip)tip.hidden=true;plot.querySelector('#crosshair')?.setAttribute('visibility','hidden');}});
function events(){
  stream=new EventSource('/api/events');
  stream.onopen=()=>{s.online=true;topbar();};stream.onerror=()=>{s.online=false;topbar();};
  stream.addEventListener('update',e=>{
    const j=JSON.parse(e.data);
    if(j.world){const i=s.worlds.findIndex(w=>w.id===j.w);if(i<0)s.worlds.push(j.world);else s.worlds[i]=j.world;}
    if(j.world?.source==='demo'&&j.w!==s.w)return;
    for(const ev of j.events){const title=(j.w!==s.w&&j.world?`PoE ${j.world.game} · ${j.world.league}: `:'')+ev.title;toast(title);if(globalThis.Notification?.permission==='granted')new Notification(title,{body:ev.body,icon:'/mark.svg'});}
    if(j.w!==s.w)return;
    s.scanR=null;clearTimeout(pending);
    // Preserve form edits while a trader is entering a trade or simulation.
    if(dlg.open){needsRefresh=true;return;}
    if(main.contains(document.activeElement)&&document.activeElement.closest('form'))return;
    pending=setTimeout(()=>loadDesk().catch(err=>toast(err.message,true)),180);
  });
}
async function init(){
  try{
    s.worlds=await api('worlds');
    s.w=s.worlds.find(w=>w.id===params.get('world'))?.id??personalDesk();
    if(!s.w)throw new Error('No personal workspace available');
    await loadDesk(true);events();
    if(s.d.world.source!=='demo'&&!s.detached)save('world',s.w);
  }catch(e){main.innerHTML=empty('Terminal unavailable',e.message+'. Start the local server and reload.','terminal');}
}
init();
