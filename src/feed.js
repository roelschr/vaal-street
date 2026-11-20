import { U,HOUR,check,positive,text,uint } from './fixed.js';
import { describe,hash,itemId,catalog } from './catalog.js';
import { now,worldId } from './store.js';

export function parseExact(raw) {
  // JSON source tokens preserve large integers sent as numbers by the upstream API.
  return JSON.parse(raw,(_k,v,context)=>typeof v==='number'&&!Number.isSafeInteger(v)&&context?.source?context.source:v);
}
const UA=process.env.POE_USER_AGENT??'VaalStreet/1.0 (personal local market terminal)';
const endpoint='https://web.poecdn.com/api/currency-exchange';
export function digest(store,game,cursor,raw,advance=true) {
  const j=parseExact(raw),next=Number(j.next_change_id);
  check(Array.isArray(j.markets)&&Number.isSafeInteger(next)&&next>=cursor&&next%HOUR===0,'Malformed hourly digest or cursor.');
  if (next===cursor) { check(!j.markets.length,'Upstream returned markets without advancing its cursor.'); return { worlds:[],markets:0,tip:true }; }
  const key=`exchange:${game}:${cursor}`;
  if (store.sql('SELECT 1 FROM inbox WHERE k=?').get(key)) {
    if (advance&&cursor>=store.get(`cursor:${game}`,0)) store.set(`cursor:${game}`,next);
    return { worlds:[],markets:0,duplicate:true,next };
  }
  const started=performance.now(), worlds=new Map(), known=new Set();
  return store.tx(()=>{
    for (const m of j.markets) {
      const league=text(m.league,'league'),w=worldId('exchange',game,league);
      if (!worlds.has(w)) { store.ensureWorld(w,game,league,'exchange',null,cursor); worlds.set(w,[]); }
      const bases=m.market_pair??String(m.market_id).split('|');
      check(Array.isArray(bases)&&bases.length===2&&bases[0]!==bases[1],'Invalid exchange pair.');
      const [a,b]=bases.map(base=>{
        text(base,'base item ID',512); const id=itemId(base),k=`${w}/${id}`;
        if (!known.has(k)) { if(!store.exists(w,id))store.ensureAsset(w,describe(id,base)); known.add(k); }
        return id;
      });
      const va=uint(m.volume_traded?.[bases[0]],'Settled volume'),vb=uint(m.volume_traded?.[bases[1]],'Settled volume');
      if (!va||!vb) continue; // Stock-only rows are valid upstream observations, but not price evidence.
      store.pair(w,a,b,cursor,va,vb); worlds.get(w).push({a,b,va,vb});
    }
    for (const [w,ps] of worlds) {
      // Anchor to Chaos; prefer direct markets, then the deepest observed bridge at
      // each distance. Never combine different hours or use stock as available depth.
      const rates=new Map([['chaos',{n:1n,d:1n,v:0n}]]);
      for (let hop=0;hop<3;hop++) {
        const candidates=new Map();
        for (const {a,b,va,vb} of ps) {
          for (const [base,other,volume,baseVol] of [[a,b,vb,va],[b,a,va,vb]]) {
            const r=rates.get(base);
            if (!r||rates.has(other)) continue;
            const prev=candidates.get(other);
            if (!prev||volume>prev.v) candidates.set(other,{n:baseVol*r.n,d:volume*r.d,v:volume});
          }
        }
        for (const [a,r] of candidates) rates.set(a,r);
        if (!candidates.size) break;
      }
      for (const [a,r] of rates) {
        if (a==='chaos') continue;
        const px=r.n*U/r.d;
        if (px>0n) store.bar(w,a,cursor,px,px,px,px,r.v);
      }
      store.event(w,cursor,'data','Exchange digest indexed',`${ps.length} settled pairs · ${rates.size-1} Chaos-referenced markets`,key);
    }
    store.archive(key,raw);
    if (advance&&cursor>=store.get(`cursor:${game}`,0)) store.set(`cursor:${game}`,next); // Same commit as raw history, marks, ratios, and event.
    return { worlds:[...worlds.keys()],markets:j.markets.length,ms:+(performance.now()-started).toFixed(2),next };
  });
}
function retryTime(header) {
  if (!header) return 60; const sec=Number(header);
  return Number.isFinite(sec)?Math.max(1,sec):Math.max(60,Math.ceil((Date.parse(header)-Date.now())/1000)||60);
}
export class Feed {
  constructor(store,changed) { this.store=store; this.changed=changed; this.busy=new Set();this.discoveryBusy=false; }
  status() {
    return { automatic:this.store.get('feed:auto',false), realms:[1,2].map(game=>({game,busy:this.busy.has(game),cursor:this.store.get(`cursor:${game}`,null),...this.store.get(`feed:${game}`,{})})),
      stashAvailable:!!(process.env.POE_STASH_TOKEN&&process.env.POE_STASH_TABS),watched:!!process.env.VAAL_IMPORT_DIR,discovery:this.store.get('league:status',null) };
  }
  async pull(game,manual=false) {
    if (this.busy.has(game)) return;
    const s=this.store,prev=s.get(`feed:${game}`,{}),hour=Math.floor(now()/HOUR)*HOUR;
    if ((prev.retry??0)>now()&&(!manual||prev.limited||prev.tip)) return;
    this.busy.add(game);
    const cursor=s.get(`cursor:${game}`,hour-HOUR);
    s.set(`cursor:${game}`,cursor);
    let delay=60, limited=false;
    try {
      const r=await fetch(`${endpoint}${game===2?'/poe2':''}/${cursor}`,{headers:{'User-Agent':UA},redirect:'error',signal:AbortSignal.timeout(25000)});
      if (!r.ok) {
        delay=Math.max(retryTime(r.headers.get('Retry-After')),r.status===429?120:60); limited=r.status===429||!!r.headers.get('Retry-After');
        throw new Error(`Exchange returned HTTP ${r.status}.`);
      }
      const raw=await r.text(); const result=digest(s,game,cursor,raw);
      const tip=result.tip||result.next>=hour;
      s.set(`feed:${game}`,{last:now(),success:now(),retry:tip?hour+HOUR+1:now()+12,tip,limited:false,ms:result.ms??null,markets:result.markets,error:null});
      for (const w of result.worlds) this.changed(w);
    } catch (e) {
      s.set(`feed:${game}`,{...prev,last:now(),failures:(prev.failures??0)+1,retry:now()+Math.max(delay,Math.min(1800,2**Math.min(10,prev.failures??0)*15)),limited,tip:false,error:e.message});
    } finally { this.busy.delete(game); }
  }
  async discover() {
    if(!process.env.POE_LEAGUES_TOKEN||this.discoveryBusy||this.store.get('league:retry',0)>now())return;
    this.discoveryBusy=true;let retry=3600;
    try{
      const worlds=[];
      for(const [game,realm] of [[1,'pc'],[2,'poe2']])for(const type of ['main','event']){
        for(let offset=0;;offset+=50){
          const r=await fetch(`https://api.pathofexile.com/league?realm=${realm}&type=${type}&limit=50&offset=${offset}`,{headers:{Authorization:`Bearer ${process.env.POE_LEAGUES_TOKEN}`,'User-Agent':UA},signal:AbortSignal.timeout(20000),redirect:'error'});
          if(r.headers.has('Retry-After'))retry=Math.max(retry,retryTime(r.headers.get('Retry-After')));
          check(r.ok,`League discovery returned HTTP ${r.status}.`);
          const {leagues}=await r.json();check(Array.isArray(leagues),'Malformed league discovery page.');
          for(const l of leagues) {
            const name=text(l.id,'league',100),ts=Date.parse(l.startAt)/1000;
            worlds.push({w:worldId('exchange',game,name),game,name,start:Number.isSafeInteger(ts)?ts:null});
          }
          if(leagues.length<50)break;
        }
      }
      this.store.tx(()=>{for(const x of worlds)this.store.ensureWorld(x.w,x.game,x.name,'exchange',x.start,0);});
      this.store.set('league:status',{last:now(),count:worlds.length});retry=21600;
    }catch(e){this.store.set('league:status',{last:now(),error:e.message});}
    finally{this.store.set('league:retry',now()+retry);this.discoveryBusy=false;}
  }
  async sync(manual=false) { await Promise.all([this.pull(1,manual),this.pull(2,manual),this.discover()]); return this.status(); }
  async stash(w) {
    const world=this.store.world(w);
    check(world.game===1&&world.source!=='demo','Account stash reads require a personal PoE 1 workspace.');
    check(this.status().stashAvailable,'Set POE_STASH_TOKEN and POE_STASH_TABS to enable selected-tab reads.');
    check((this.store.get('stash:retry',0))<=now(),'Stash read is cooling down. Try again later.');
    this.store.set('stash:retry',now()+60);
    const tabs=[...new Set(process.env.POE_STASH_TABS.split(',').map(x=>x.trim()).filter(Boolean))];
    const seen=new Map(), names=new Map();
    for (const tab of tabs) {
      const route=tab.split('/').map(x=>encodeURIComponent(text(x,'stash tab ID',80))).join('/');
      const r=await fetch(`https://api.pathofexile.com/stash/${encodeURIComponent(world.league)}/${route}`,{
        headers:{Authorization:`Bearer ${process.env.POE_STASH_TOKEN}`,'User-Agent':UA},signal:AbortSignal.timeout(20000),redirect:'error'});
      if (r.status===429||r.headers.has('Retry-After')) this.store.set('stash:retry',now()+Math.max(120,retryTime(r.headers.get('Retry-After'))));
      check(r.ok,`Stash API returned HTTP ${r.status}.`);
      const j=await r.json(); check(Array.isArray(j.stash?.items),'The selected tab did not return an inventory.');
      for (const it of j.stash.items) {
        const name=text(it.name||it.baseType||it.typeLine,'stash item name');
        const id=Object.keys(catalog).find(k=>catalog[k][0]===name)??`stash-${hash(name).slice(0,20)}`;
        names.set(id,name); seen.set(id,(seen.get(id)??0n)+positive(it.stackSize??1,'Stack size'));
      }
    }
    this.store.tx(()=>{
      for (const [id,name] of names) if (!this.store.exists(w,id)) this.store.ensureAsset(w,{...describe(id),name,kind:'Craft'});
      this.store.stashRows(w,[...seen].map(([asset,qty])=>({asset,qty})),now());
      this.store.event(w,now(),'data','Stash snapshot saved',`${tabs.length} selected account tabs · ${seen.size} asset types`);
    });
    this.changed(w); return {items:seen.size};
  }
}
