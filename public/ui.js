export const $=s=>document.querySelector(s);
export const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
const icons={
  overview:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  market:'<path d="M3 19V5m0 14h18M6 14l4-5 4 3 6-8m-4 0h4v4"/>',
  candles:'<path d="M6 3v4m0 8v5m6-17v8m0 6v4m6-19v3m0 7v6"/><rect x="4" y="7" width="4" height="8" rx=".8"/><rect x="10" y="11" width="4" height="6" rx=".8"/><rect x="16" y="5" width="4" height="7" rx=".8"/>',
  routes:'<circle cx="5" cy="17" r="2.6"/><circle cx="12" cy="5" r="2.6"/><circle cx="20" cy="17" r="2.6"/><path d="m6.5 14.6 4-7.2m3 .2 5 7M8 17h9"/>',
  portfolio:'<path d="M3 8.5V7a2 2 0 0 1 2-2h11l3 3M4 8h15a2 2 0 0 1 2 2v9H5a2 2 0 0 1-2-2v-7a2 2 0 0 1 1-2Z"/><path d="M16 12h5v4h-5a2 2 0 1 1 0-4Z"/>',
  layers:'<path d="m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5m-18 4 9 5 9-5"/>',
  lab:'<path d="M9 3h6m-5 0v7l-6 8a2 2 0 0 0 1.6 3h12.8a2 2 0 0 0 1.6-3l-6-8V3M7 15h10"/>',
  craft:'<path d="m13.5 3 7 7-3.5 3.5-3-3L5 20l-2-2 9-9-2-2 3.5-4Zm1 1-2 2"/>',
  alert:'<path d="M5 16c1-2 1.5-2.5 1.5-7a5.5 5.5 0 1 1 11 0c0 4.5.5 5 1.5 7l1 1H4l1-1Zm5 4a2.2 2.2 0 0 0 4 0"/>',
  db:'<ellipse cx="12" cy="5.5" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
  settings:'<path d="m9.5 3-.5 2-2 .9-1.9-.5-2 3.3L4.7 10v3l-1.6 1.5 2 3.4 2-.5 2 .9.4 2.2h4.6l.6-2.2 2-.9 2 .5 2-3.4L19 13v-3l1.6-1.5-2-3.3-2 .5-2-.9L14 3Z"/><circle cx="12" cy="12" r="3"/>',
  hideout:'<path d="M3 8V4h5m8 0h5v4m0 8v4h-5m-8 0H3v-4"/><path d="m7 13 5-5 5 5m-8-1v5h6v-5"/>',
  terminal:'<rect x="3" y="4" width="18" height="15" rx="2"/><path d="m6.5 8 3 3-3 3m6.5 0h4"/>',
  activity:'<path d="M2 12h5l3-8 4 16 3-9 2 1h3"/>',
  pulse:'<path d="M3 19V6m0 13h18M6 15l4-4 3 2 6-7"/>',
  trend:'<path d="m3 17 6-6 4 3 8-9m-6 0h6v6"/>',
  coins:'<ellipse cx="10" cy="6" rx="7" ry="3"/><path d="M3 6v4c0 1.7 3.1 3 7 3 3 0 5.7-.8 6.6-2M3 10v4c0 1.7 3.1 3 7 3"/><ellipse cx="16" cy="14" rx="5" ry="2.5"/><path d="M11 14v4c0 1.4 2.2 2.5 5 2.5s5-1.1 5-2.5v-4"/>',
  shield:'<path d="m12 3 8 3v5c0 5-4 8-8 10-4-2-8-5-8-10V6l8-3Z"/><path d="m8.5 11.5 2.5 2.5 4.5-5"/>',
  globe:'<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/>',
  star:'<path d="m12 3 2.6 5.7 6.3.8-4.7 4.4 1.3 6.4-5.5-3.1-5.5 3.1 1.3-6.4L3 9.5l6.4-.8L12 3Z"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  arrow:'<path d="M4 12h15m-6-6 6 6-6 6"/>',upRight:'<path d="M6 18 18 6M7 6h11v11"/>',
  downRight:'<path d="m6 6 12 12M7 18h11V7"/>',down:'<path d="m6 9 6 6 6-6"/>',right:'<path d="m9 5 7 7-7 7"/>',
  search:'<circle cx="10.5" cy="10.5" r="6.8"/><path d="m16 16 5 5"/>',
  eye:'<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="2.6"/>',
  list:'<path d="M9 6h11M9 12h11M9 18h11"/><path d="M4 6h.2M4 12h.2M4 18h.2" stroke-width="3.8"/>',
  watch:'<path d="M3 6h15M3 11h9M3 16h7m4-1 2.5 2.5L21 12"/>',
  radar:'<circle cx="12" cy="12" r="8.8"/><circle cx="12" cy="12" r="5"/><path d="m12 12 8-8"/>',
  filter:'<path d="M3 6h18M6 12h12M10 18h4"/>',
  signal:'<path d="M4 18v-3m5 3v-6m5 6V8m5 10V4"/>',
  bolt:'<path d="m13 2-9 12h7l-1 8 10-13h-7l1-7Z"/>',
  expand:'<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/>',
  copy:'<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>',
  menu:'<path d="M3 6h18M3 12h18M3 18h18"/>',close:'<path d="m6 6 12 12M6 18 18 6"/>',
  refresh:'<path d="M20 10a8 8 0 1 0 0 5M20 4v6h-6"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',check:'<path d="m4 12 5 5L20 6"/>',
  upload:'<path d="M12 16V3m-5 5 5-5 5 5M4 15v5h16v-5"/>',download:'<path d="M12 3v13m-5-5 5 5 5-5M4 17v4h16v-4"/>',
  play:'<path d="m7 4 13 8-13 8V4Z"/>',pause:'<path d="M8 4v16m8-16v16"/>',trash:'<path d="M4 6h16M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7"/>',
  flask:'<path d="m10 3 4 0m-3 0v7l-6 9h14l-6-9V3"/>',book:'<path d="M3 5h7l2 2 2-2h7v14h-7l-2 2-2-2H3V5Zm9 2v14"/>',
  monitor:'<rect x="3" y="4" width="18" height="13" rx="1.5"/><path d="M8 21h8m-4-4v4"/>',
  diamond:'<path d="m12 2 8 10-8 10-8-10 8-10Zm-8 10h16"/>',sort:'<path d="m8 4-3 3m3-3v14m8 2 3-3m-3 3V6"/>'
};
export const icon=(name,size=16,cls='')=>`<svg class="ico ${cls}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name]??icons.diamond}</svg>`;
export function num(value,places=2,compact=false) {
  if(value==null||value==='')return '—';
  if(compact&&Math.abs(Number(value))>=1000)return new Intl.NumberFormat('en-US',{notation:'compact',maximumFractionDigits:2}).format(Number(value));
  const match=/^(-?)(\d+)(?:\.(\d+))?$/.exec(String(value)); if(!match)return '—';
  const [,sign,whole,fract='']=match, p=BigInt(places),scale=10n**p;
  let n=BigInt(whole)*scale+BigInt(fract.padEnd(places,'0').slice(0,places)||'0'); if(Number(fract[places]??0)>=5)n++;
  return (n?sign:'')+(n/scale).toLocaleString('en-US')+(places?'.'+String(n%scale).padStart(places,'0'):'');
}
export const signed=(v,d=2)=>(Number(v)>0?'+':'')+num(v,d);
export const pct=b=>b==null?'—':(b>0?'+':'')+(b/100).toFixed(2)+'%';
export const changeTag=b=>`<span class="change ${b==null||b===0?'neutral':b>0?'green':'red'}">${b?icon(b>0?'upRight':'downRight',12):''}${pct(b)}</span>`;
export const unit=n=>`<span class="unit">${n??'c'}</span>`;
export const date=t=>t?new Date(t*1000).toLocaleDateString('en-US',{month:'short',day:'numeric',timeZone:'UTC'}):'—';
export const time=t=>t?new Date(t*1000).toISOString().slice(11,16)+' UTC':'—';
export const age=(t,now)=>{const m=Math.max(0,Math.floor((now-t)/60));return m===0?'Just now':m<60?`${m}m ago`:m<1440?`${Math.floor(m/60)}h ago`:`${Math.floor(m/1440)}d ago`;};
export const source=s=>({demo:'Demo replay',exchange:'Exchange feed',import:'Local import'})[s]??s;
export const assetArt=(a,size=32)=>`<span class="item-art" style="--item:${size}px">${a?.icon?`<img width="${size}" height="${size}" alt="" src="${esc(a.icon)}">`:`<span class="fallback-gem">${icon('diamond',Math.round(size*.71))}</span>`}</span>`;
export const btn=(label,action,ic='',cl='',extra='')=>`<button class="btn ${cl}" data-act="${action}" ${extra}>${ic?icon(ic,15):''}${label}</button>`;
export const ibtn=(label,action,ic,extra='')=>`<button class="icon-btn" data-act="${action}" aria-label="${esc(label)}" title="${esc(label)}" ${extra}>${icon(ic,16)}</button>`;
export const empty=(title,body,ic='layers')=>`<div class="empty">${icon(ic,25)}<strong>${esc(title)}</strong><span>${esc(body)}</span></div>`;
export const panelHead=(title,ic,right='')=>`<div class="panel-head"><h2><span class="heading-icon">${icon(ic,18)}</span>${title}</h2><div class="head-tools">${right}</div></div>`;
export function spark(series,color='lime',fill=false) {
  if (!series?.length) return `<span class="muted">—</span>`;
  const v=series.map(Number),w=104,h=31,lo=Math.min(...v),hi=Math.max(...v);
  const pts=v.map((n,i)=>[1+i*(w-2)/Math.max(1,v.length-1),hi===lo?h/2:h-3-(n-lo)/(hi-lo)*(h-6)]);
  const line=pts.map(p=>p.map(x=>x.toFixed(1)).join(',')).join(' ');
  return `<svg class="spark ${color}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">${fill?`<polygon points="1,${h} ${line} ${w-1},${h}" fill="currentColor" opacity=".08"/>`:''}<polyline points="${line}" fill="none" stroke="currentColor" stroke-width="1.6" vector-effect="non-scaling-stroke" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}
export function chartAxis(data,width) {
  const {bars,interval}=data,seconds=interval*3600,w=Math.max(240,width),left=5,right=62;
  const start=Math.floor(bars[0].t/seconds)*seconds,slot=t=>Math.floor((t-start)/seconds);
  const slots=slot(bars.at(-1).t)+1,step=(w-left-right)/slots,x=n=>left+step*(n+.5);
  return {w,right,start,seconds,slots,step,x,slot,indexAt:px=>{
    const n=Math.floor((px-left)/step);
    return bars.findIndex(b=>slot(b.t)===n);
  }};
}
export function chartSvg(data,width,height,mode='candles') {
  const bars=data.bars;if(!bars.length)return empty('No price history','Import hourly bars or sync the exchange feed.','candles');
  const {w,right,start,seconds,slots,step,x,slot}=chartAxis(data,width),h=height,top=7,bottom=26,volH=26,ph=h-top-bottom-volH-8;
  const lo=Math.min(...bars.map(b=>Number(b.l))),hi=Math.max(...bars.map(b=>Number(b.h)));
  const pad=(hi-lo)*.09||Math.max(hi*.005,.001),y=n=>top+(hi+pad-Number(n))/(hi-lo+pad*2)*ph;
  const cw=Math.max(1,Math.min(15,step*.59)),maxVol=Math.max(...bars.map(b=>Number(b.v)),1);
  const pFmt=n=>Number(n).toLocaleString('en-US',{minimumFractionDigits:hi<10?2:0,maximumFractionDigits:hi<1?3:hi<100?2:1});
  let grid='',plot='',vol='',labels='';
  for(let i=0;i<5;i++){
    const n=hi+pad-(hi-lo+pad*2)*i/4,at=y(n);grid+=`<path d="M0 ${at}H${w-right+3}" class="gridline"/><text x="${w-right+10}" y="${at+3}" class="axislabel">${pFmt(n)}</text>`;
  }
  const nLabels=Math.min(slots,w<520?4:6);
  for(let i=0;i<nLabels;i++){
    const n=Math.round(i*(slots-1)/Math.max(1,nLabels-1)),at=x(n),t=start+n*seconds,dt=new Date(t*1000);
    grid+=`<path d="M${at} ${top}V${h-bottom}" class="gridline vline"/>`;
    labels+=`<text x="${at}" y="${h-5}" text-anchor="${nLabels===1?'middle':i===0?'start':i===nLabels-1?'end':'middle'}" class="axislabel">${slots*seconds<=2*86400?dt.toISOString().slice(11,16):date(t)}</text>`;
  }
  const runs=[];
  for(let i=0;i<bars.length;i++){
    const b=bars[i],n=slot(b.t),at=x(n),cl=Number(b.c)>=Number(b.o)?'bull':'bear',v=Number(b.v)/maxVol*volH;
    if(mode==='candles')plot+=`<g class="${cl}"><path d="M${at} ${y(b.h)}V${y(b.l)}" stroke="currentColor"/><rect x="${at-cw/2}" y="${Math.min(y(b.o),y(b.c))}" width="${cw}" height="${Math.max(1.4,Math.abs(y(b.o)-y(b.c)))}" fill="currentColor" rx=".5"/></g>`;
    if(mode==='line'){
      if(!i||n!==slot(bars[i-1].t)+1)runs.push([]);
      runs.at(-1).push([at,y(b.c)]);
    }
    vol+=`<rect class="${cl}" x="${at-cw/2}" y="${h-bottom-v}" width="${cw}" height="${v}" fill="currentColor" opacity=".38"/>`;
  }
  if(mode==='line')plot=runs.map(run=>run.length===1?`<circle cx="${run[0][0]}" cy="${run[0][1]}" r="2.8" fill="var(--lime)"/>`:`<polygon fill="url(#linewash)" points="${run[0][0]},${top+ph} ${run.join(' ')} ${run.at(-1)[0]},${top+ph}"/><polyline points="${run.join(' ')}" stroke="var(--lime)" stroke-width="2" fill="none" stroke-linejoin="round"/>`).join('');
  const ly=y(bars.at(-1).c);
  return `<svg class="market-chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="${mode==='candles'?'Candlestick':'Line'} price chart with volume, ${bars.length} observations">
    <defs><linearGradient id="linewash" x2="0" y2="1"><stop stop-color="#b0ed78" stop-opacity=".19"/><stop offset="1" stop-color="#b0ed78" stop-opacity="0"/></linearGradient></defs>
    ${grid}${vol}${plot}<path d="M0 ${ly}H${w-right+6}" class="lastline"/><rect x="${w-right+4}" y="${ly-8}" width="${right-5}" height="16" fill="#ace783" rx="2"/><text class="lastlabel" x="${w-right/2+1}" y="${ly+3.2}" text-anchor="middle">${pFmt(bars.at(-1).c)}</text>${labels}<line id="crosshair" y1="${top}" y2="${h-bottom}" visibility="hidden" stroke="#77818a" stroke-dasharray="3 3"/></svg><div class="chart-tip" hidden></div>`;
}
