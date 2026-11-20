import { check,positive,text } from './fixed.js';
import { catalog,describe,hash } from './catalog.js';
import { now,worldId } from './store.js';
import { parseExact } from './feed.js';

const aliases=new Map(Object.keys(catalog).map(id=>[id,id]));
for(const [alias,id] of [['exa','exalted'],['ex','exalted'],['div','divine'],['alch','alchemy'],['fuse','fusing'],['ancient-orb','ancient']])aliases.set(alias,id);
const byName=new Map(Object.entries(catalog).map(([id,c])=>[c[0],id]));
function ratio(note){
  const m=/^~(?:b\/o|price)\s+(\d{1,16})(?:\/(\d{1,16})|\.(\d{1,6}))?\s+([a-z0-9-]+)(?:\s|$)/i.exec(note??'');
  if(!m||!aliases.has(m[4]))return null;
  const d=m[2]?BigInt(m[2]):m[3]?10n**BigInt(m[3].length):1n;
  const n=m[3]?BigInt(m[1])*d+BigInt(m[3]):BigInt(m[1]);if(!n||!d)return null;
  let a=n,b=d;while(b){const r=a%b;a=b;b=r;}
  return {src:aliases.get(m[4]),pay:n/a,get:d/a};
}
// A changed stash is a full replacement; unlisting it retracts every previous offer.
// A public stash carries neither online status nor an executable Gold quote.
export function stashPage(s,cursor,raw,at=now()) {
  const j=parseExact(raw),next=text(j.next_change_id,'next stash cursor',512);
  check(Array.isArray(j.stashes),'Malformed public stash page.');
  if(!j.stashes.length&&next===cursor)return {worlds:[],count:0,next};
  const key=`psapi:${hash(`${cursor??'start'}|${next}|${hash(raw)}`)}`;
  if(s.sql('SELECT 1 FROM inbox WHERE k=?').get(key))return {worlds:[],count:0,next};
  const affected=new Set();let count=0;
  return s.tx(()=>{
    for(const tab of j.stashes){
      const tabId=text(tab.id,'public tab ID',160);
      for(const old of s.sql('SELECT w,q FROM public_quotes WHERE stash=?').all(tabId)){
        s.sql('DELETE FROM quotes WHERE w=? AND id=?').run(old.w,old.q);affected.add(old.w);
      }
      s.sql('DELETE FROM public_quotes WHERE stash=?').run(tabId);
      if(!tab.public||!tab.league)continue;
      const league=text(tab.league,'league'),w=worldId('exchange',1,league);
      for(const it of tab.items??[]){
        if(![5,6].includes(it.frameType)&&!['Currency','DivinationCard','Divination Card'].includes(it.frameTypeId))continue;
        const r=ratio(it.note??tab.stash),dst=byName.get(it.baseType??it.typeLine);
        if(!r||!dst||r.src===dst||!it.id)continue;
        const lots=positive(it.stackSize??1,'Observed stack')/r.get;if(!lots)continue;
        if(!affected.has(w)||!s.exists(w,dst))s.ensureWorld(w,1,league,'exchange',null,at);
        affected.add(w);
        for(const a of [r.src,dst])if(!s.exists(w,a))s.ensureAsset(w,describe(a));
        const id='ps-'+hash(`${tabId}:${it.id}`).slice(0,40);
        s.quote(w,{...r,id,dst,lots,gold:0,online:false,confirmed:false,seen:at});
        s.sql('INSERT INTO public_quotes(stash,w,q) VALUES (?,?,?)').run(tabId,w,id);count++;
      }
    }
    s.archive(key,raw);s.set('psapi:cursor',next);
    return {worlds:[...affected],count,next};
  });
}
export class ListingsFeed {
  constructor(s,changed){this.s=s;this.changed=changed;this.busy=false;}
  status(){return {available:!!process.env.POE_PUBLIC_STASH_TOKEN,automatic:this.s.get('psapi:auto',false),busy:this.busy,...this.s.get('psapi:state',{})};}
  async pull(){
    const state=this.status();if(!state.available||this.busy||(state.retry??0)>now())return;
    this.busy=true;
    let delay=60;
    try{
      const cursor=this.s.get('psapi:cursor',null);
      const r=await fetch(`https://api.pathofexile.com/public-stash-tabs${cursor?'?id='+encodeURIComponent(cursor):''}`,{headers:{
        Authorization:`Bearer ${process.env.POE_PUBLIC_STASH_TOKEN}`,'User-Agent':process.env.POE_USER_AGENT??'VaalStreet/1.0 (personal local terminal)'},
        signal:AbortSignal.timeout(30000),redirect:'error'});
      if(r.headers.get('Retry-After')){const h=r.headers.get('Retry-After');delay=Math.max(delay,Number(h)||Math.ceil((Date.parse(h)-Date.now())/1000)||60);}
      check(r.ok,`Public stash API returned HTTP ${r.status}.`);
      const out=stashPage(this.s,cursor,await r.text());
      this.s.set('psapi:state',{last:now(),count:out.count,retry:now()+Math.max(30,delay),error:null});
      for(const w of out.worlds)this.changed(w);
    }catch(e){this.s.set('psapi:state',{last:now(),retry:now()+delay,error:e.message});}finally{this.busy=false;}
  }
}
