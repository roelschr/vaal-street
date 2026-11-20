import { DatabaseSync } from 'node:sqlite';
import { gzipSync } from 'node:zlib';
import { randomUUID } from 'node:crypto';
import { U, DAY, HOUR, BP, bps, check, decimal, max, min, micros,money, positive, small, text, timestamp, uint } from './fixed.js';
import { account, scan, screen } from './engine.js';
import { hash, describe } from './catalog.js';

export const now = () => Math.floor(Date.now() / 1000);
export const encode = x => JSON.stringify(x, (_, v) => typeof v === 'bigint' ? String(v) : v);
export function idOf(x) { check(typeof x === 'string' && /^[a-z0-9][a-z0-9_-]{0,79}$/.test(x), 'Invalid asset ID. Use a lowercase slug.'); return x; }
export const worldId = (source, game, league) => `${source}-${game}-${hash(league).slice(0,16)}`;
export const HOME_WORLD = worldId('exchange',1,'Standard');
const DEFAULT = { method: 'FIFO', tax: '12', staleHours: 48, maxLegs: 4, ttl: 3600, tolerance: 1200 };
const CATEGORIES = ['Currency','Commodity','Craft','Card'];

export class Store {
  constructor(path) {
    this.db = new DatabaseSync(path);
    this.db.exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;
      CREATE TABLE IF NOT EXISTS worlds(id TEXT PRIMARY KEY, game INTEGER NOT NULL, league TEXT NOT NULL, source TEXT NOT NULL, start INTEGER, asof INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS assets(w TEXT REFERENCES worlds, id TEXT, name TEXT NOT NULL, symbol TEXT, kind TEXT, icon TEXT, PRIMARY KEY(w,id)) WITHOUT ROWID;
      CREATE TABLE IF NOT EXISTS bars(w TEXT, a TEXT, t INTEGER, o TEXT, h TEXT, l TEXT, c TEXT, v TEXT, PRIMARY KEY(w,a,t), FOREIGN KEY(w,a) REFERENCES assets(w,id)) WITHOUT ROWID;
      CREATE TABLE IF NOT EXISTS pairs(w TEXT REFERENCES worlds, a TEXT,b TEXT,t INTEGER,va TEXT,vb TEXT,PRIMARY KEY(w,a,b)) WITHOUT ROWID;
      CREATE TABLE IF NOT EXISTS quotes(w TEXT REFERENCES worlds,id TEXT,src TEXT,dst TEXT,pay TEXT,get TEXT,lots TEXT,gold TEXT,seen INTEGER,online INTEGER,confirmed INTEGER,PRIMARY KEY(w,id)) WITHOUT ROWID;
      CREATE TABLE IF NOT EXISTS public_quotes(stash TEXT,w TEXT,q TEXT,PRIMARY KEY(stash,w,q)) WITHOUT ROWID;
      CREATE UNIQUE INDEX IF NOT EXISTS public_quote_id ON public_quotes(w,q);
      CREATE TABLE IF NOT EXISTS trades(seq INTEGER PRIMARY KEY, w TEXT REFERENCES worlds, id TEXT, t INTEGER, side TEXT, asset TEXT,qty TEXT,price TEXT,gold TEXT,tax TEXT,note TEXT, UNIQUE(w,id));
      CREATE INDEX IF NOT EXISTS ledger_order ON trades(w,t,seq);
      CREATE TABLE IF NOT EXISTS stash(w TEXT REFERENCES worlds,a TEXT,qty TEXT,PRIMARY KEY(w,a)) WITHOUT ROWID;
      CREATE TABLE IF NOT EXISTS demand(w TEXT REFERENCES worlds,a TEXT,build TEXT,listed TEXT,sold TEXT,minutes INTEGER,t INTEGER,PRIMARY KEY(w,a,build)) WITHOUT ROWID;
      CREATE TABLE IF NOT EXISTS rules(w TEXT REFERENCES worlds,id TEXT,kind TEXT,asset TEXT,op TEXT,threshold TEXT,enabled INTEGER,hot INTEGER,PRIMARY KEY(w,id)) WITHOUT ROWID;
      CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY,w TEXT REFERENCES worlds,t INTEGER,kind TEXT,title TEXT,body TEXT,ref TEXT, UNIQUE(w,ref));
      CREATE INDEX IF NOT EXISTS event_order ON events(w,seq DESC);
      CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY,v TEXT NOT NULL) WITHOUT ROWID;
      CREATE TABLE IF NOT EXISTS inbox(k TEXT PRIMARY KEY,received INTEGER,body BLOB NOT NULL) WITHOUT ROWID;
      `);
    if (!this.db.prepare('PRAGMA table_info(trades)').all().some(c=>c.name==='session')) {
      this.db.exec("ALTER TABLE trades ADD COLUMN session TEXT NOT NULL DEFAULT 'Default session'");
    }
    this.db.exec('PRAGMA user_version=2');
    this.statements = new Map();
  }
  sql(s) { if (!this.statements.has(s)) this.statements.set(s, this.db.prepare(s)); return this.statements.get(s); }
  tx(fn) {
    this.db.exec('BEGIN IMMEDIATE');
    try { const out = fn(); this.db.exec('COMMIT'); return out; }
    catch (e) { this.db.exec('ROLLBACK'); throw e; }
  }
  get(k, fallback) { const v = this.sql('SELECT v FROM kv WHERE k=?').get(k); return v ? JSON.parse(v.v) : fallback; }
  set(k, v) { this.sql('INSERT OR REPLACE INTO kv VALUES (?,?)').run(k, encode(v)); }
  worlds() { return this.sql("SELECT * FROM worlds ORDER BY CASE WHEN id=? THEN 0 WHEN source='demo' THEN 2 ELSE 1 END, game, asof DESC, league").all(HOME_WORLD); }
  world(w) { const v = this.sql('SELECT * FROM worlds WHERE id=?').get(w); check(v, 'Unknown league workspace.'); return v; }
  personalWorld(game,league,source='import',start=null) {
    const w=worldId(source,game,league);
    this.tx(()=>{
      if(!this.sql('SELECT 1 FROM worlds WHERE id=?').get(w))this.ensureWorld(w,game,league,source,start,0);
      else if(start!==null)this.sql('UPDATE worlds SET start=? WHERE id=?').run(start,w);
      for(const a of ['divine','exalted','mirror','annul','vaal','alchemy'])if(!this.exists(w,a))this.ensureAsset(w,describe(a));
    });
    return w;
  }
  ensureWorld(w, game, league, source, start, t) {
    this.sql('INSERT INTO worlds VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET asof=MAX(asof,excluded.asof),start=COALESCE(excluded.start,start)').run(w,game,league,source,start ?? null,t);
    this.ensureAsset(w, describe('chaos'));
  }
  ensureAsset(w, a) {
    this.sql('INSERT INTO assets VALUES (?,?,?,?,?,?) ON CONFLICT(w,id) DO UPDATE SET name=excluded.name,symbol=excluded.symbol,kind=excluded.kind,icon=excluded.icon')
      .run(w,a.id,a.name,a.symbol,a.kind,a.icon ?? null);
  }
  exists(w,a) { return !!this.sql('SELECT 1 FROM assets WHERE w=? AND id=?').get(w,a); }
  bar(w,a,t,o,h,l,c,v) {
    this.sql('INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?)').run(w,a,t,String(o),String(h),String(l),String(c),String(v));
  }
  pair(w,a,b,t,va,vb) {
    this.sql('INSERT INTO pairs VALUES (?,?,?,?,?,?) ON CONFLICT(w,a,b) DO UPDATE SET va=excluded.va,vb=excluded.vb,t=excluded.t WHERE excluded.t>=t')
      .run(w,a,b,t,String(va),String(vb));
  }
  quote(w,q) {
    this.sql('INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?,?,?,?)').run(w,q.id,q.src,q.dst,String(q.pay),String(q.get),String(q.lots),String(q.gold),q.seen,+q.online,+q.confirmed);
  }
  event(w,t,kind,title,body,ref = randomUUID()) {
    return this.sql('INSERT OR IGNORE INTO events(w,t,kind,title,body,ref) VALUES (?,?,?,?,?,?)').run(w,t,kind,title,body,ref).changes;
  }
  prefs(w) { return { ...DEFAULT, ...this.get(`prefs:${w}`, {}) }; }
  settings(w,v) {
    this.world(w); const p = this.prefs(w);
    if (v.method !== undefined) { check(['FIFO','WAC'].includes(v.method), 'Unknown cost basis method.'); p.method=v.method; }
    if (v.tax !== undefined) p.tax=decimal(money(v.tax,'Gold valuation'));
    for (const [k,lo,hi] of [['staleHours',1,720],['maxLegs',2,6],['ttl',60,7200],['tolerance',10,5000]]) if (v[k] !== undefined) p[k]=small(v[k],lo,hi,k);
    this.set(`prefs:${w}`,p); return p;
  }
  time(w) { const world = this.world(w); return world.source === 'demo' ? world.asof : now(); }
  assets(w) { return this.sql('SELECT id,name,symbol,kind,icon FROM assets WHERE w=? ORDER BY name').all(w); }
  marks(w, assets = this.assets(w)) {
    const m = new Map(), s = this.sql('SELECT c,t FROM bars WHERE w=? AND a=? ORDER BY t DESC LIMIT 1');
    for (const a of assets) { const r = s.get(w,a.id); if (r) m.set(a.id,r); }
    m.set('chaos',{ c: String(U), t:this.time(w) }); return m;
  }
  market(w, assets, marks) {
    const prev = this.sql('SELECT c FROM bars WHERE w=? AND a=? AND t BETWEEN ? AND ? ORDER BY t DESC LIMIT 1');
    const sample = this.sql('SELECT t,c,v FROM bars WHERE w=? AND a=? AND t>? ORDER BY t');
    const rows = assets.map(a => {
      const mark=marks.get(a.id); if (!mark) return { ...a, price:null, change:null, volume:'0', trend:[], t:null };
      const cmp = prev.get(w,a.id,mark.t-DAY-HOUR,mark.t-DAY);
      const pts = sample.all(w,a.id,mark.t-7*DAY);
      let vol=0n; for (const p of pts) if (p.t>mark.t-DAY) vol+=BigInt(p.v);
      return { ...a,price:decimal(mark.c),change:a.id==='chaos'?0:cmp?bps(BigInt(mark.c)-BigInt(cmp.c),BigInt(cmp.c)):null,
        volume:String(vol),t:mark.t,trend:pts.filter((_,i)=>i%6===0||i===pts.length-1).map(r=>decimal(r.c)) };
    });
    const order=['divine','exalted','mirror','ancient','annul','vaal','fusing','thread','omen','chaos'];
    return rows.sort((a,b)=>(order.includes(a.id)?order.indexOf(a.id):99)-(order.includes(b.id)?order.indexOf(b.id):99)||a.name.localeCompare(b.name));
  }
  trades(w) { return this.sql('SELECT * FROM trades WHERE w=? ORDER BY t,seq').all(w); }
  book(w, marks, prefs=this.prefs(w)) {
    const pairs=new Map();
    for (const p of this.sql('SELECT * FROM pairs WHERE w=? AND t>=?').all(w,this.time(w)-DAY)) {
      pairs.set(`${p.a}|${p.b}`,p); pairs.set(`${p.b}|${p.a}`,{ ...p,va:p.vb,vb:p.va });
    }
    return screen(this.sql('SELECT * FROM quotes WHERE w=? ORDER BY id').all(w),marks,pairs,this.time(w),prefs.ttl,prefs.tolerance);
  }
  scanning(w,opts={}, marks=this.marks(w), prefs=this.prefs(w), acct=account(this.trades(w),prefs.method)) {
    const book=this.book(w,marks,prefs);
    const capital=opts.capital===undefined?acct.cash/U:min(uint(opts.capital,'Bankroll'),acct.cash/U);
    const gold=opts.gold===undefined?acct.gold:min(uint(opts.gold,'Gold budget'),acct.gold);
    return { ...scan(book.good,capital,gold,money(prefs.tax),small(opts.legs??prefs.maxLegs,2,6,'Legs')),
      capital:String(capital),gold:String(gold),accepted:book.good.length,rejected:book.rejected,quotes:book.good };
  }
  manualQuote(w,v) {
    const at=this.time(w),src=idOf(v.from),dst=idOf(v.to),id=text(v.id??randomUUID(),'quote ID',100);
    check(src!==dst&&this.exists(w,src)&&this.exists(w,dst),'Choose two different instruments in this league.');
    const q={id,src,dst,pay:positive(v.pay,'Pay units'),get:positive(v.get,'Receive units'),lots:positive(v.lots,'Available lots'),gold:uint(v.gold,'Gold cost'),seen:at,online:v.online===true,confirmed:v.confirmed===true};
    this.tx(()=>{
      if(v.review){
        const old=this.sql('SELECT * FROM quotes WHERE w=? AND id=?').get(w,id);
        check(old&&old.src===src&&old.dst===dst,'Listing changed or was withdrawn. Refresh the quote first.');
        this.sql('DELETE FROM quotes WHERE w=? AND id=?').run(w,id);
      }
      this.quote(w,q);this.archive(`quote:${w}:${id}:${randomUUID()}`,encode(q));this.event(w,at,'data','Listing snapshot recorded',`${src} → ${dst} · ${q.lots} observed lots`);
    });
    return {id};
  }
  portfolio(w,marks=this.marks(w),prefs=this.prefs(w),acct=account(this.trades(w),prefs.method)) {
    const at=this.time(w), holdings=[], allocation={ Currency:acct.cash, Commodity:0n, Craft:0n, Card:0n };
    let worth=acct.cash, unpriced=0;
    for (const [id,p] of acct.positions) {
      if (!p.qty) continue;
      const a=this.sql('SELECT * FROM assets WHERE w=? AND id=?').get(w,id), m=marks.get(id);
      const value=m?BigInt(m.c)*p.qty:null;
      if (value===null) unpriced++; else { worth+=value; allocation[a.kind]+=value; }
      const age=Math.max(0,Math.floor((at-p.last)/3600)), stale=age>=prefs.staleHours;
      holdings.push({ ...a,qty:String(p.qty),basis:decimal(p.cost),average:decimal(p.cost/p.qty),value:value===null?null:decimal(value),
        unrealized:value===null?null:decimal(value-p.cost),age,stale,markdown:stale&&m?decimal(BigInt(m.c)*(BP-min(2000n,BigInt(Math.floor(age/prefs.staleHours))*200n))/BP):null });
    }
    const stash=this.sql('SELECT a AS id,qty FROM stash WHERE w=?').all(w).map(x=>({...x,value:marks.has(x.id)?decimal(BigInt(marks.get(x.id).c)*BigInt(x.qty)):null}));
    const history=[], trades=this.trades(w);
    const past=this.sql('SELECT c FROM bars WHERE w=? AND a=? AND t<=? ORDER BY t DESC LIMIT 1');
    for (let i=28;i>=0;i--) {
      const t=at-i*6*HOUR, old=account(trades.filter(x=>x.t<=t),prefs.method);
      let v=old.cash; for (const [a,pos] of old.positions) if (pos.qty) v+=pos.qty*BigInt(past.get(w,a,t)?.c??0);
      history.push({t,worth:decimal(v),realized:decimal(old.realized),gold:String(old.gold)});
    }
    const sess=acct.entries.filter(t=>t.side==='buy'||t.side==='sell');
    const hours=sess.length>1&&sess.at(-1).t>sess[0].t?sess.at(-1).t-sess[0].t:null;
    const sessions=new Map();
    for(const e of sess){
      const name=e.session??'Default session', r=sessions.get(name)??{name,first:e.t,last:e.t,pnl:0n,gold:0n,fills:0};
      r.last=e.t;r.fills++;r.gold+=BigInt(e.gold);
      if(e.pnl!==null)r.pnl+=micros(e.pnl);
      sessions.set(name,r);
    }
    const stashValue=stash.reduce((sum,x)=>sum+(x.value?micros(x.value):0n),0n);
    return { cash:decimal(acct.cash),gold:String(acct.gold),realized:decimal(acct.realized),worth:decimal(worth),burned:String(acct.burned),
      turnover:decimal(acct.turnover),pnlHour:hours?decimal(acct.realized*3600n/BigInt(hours)):null,holdings,unpriced,
      allocation:Object.fromEntries(Object.entries(allocation).map(([k,v])=>[k,decimal(v)])),
      entries:acct.entries.slice(-200).reverse(),stash,stashValue:decimal(stashValue),stashAt:this.get(`stash:${w}`,null),history,
      sessions:[...sessions.values()].map(r=>({...r,pnl:decimal(r.pnl),pnlHour:r.last>r.first?decimal(r.pnl*3600n/BigInt(r.last-r.first)):null})).reverse() };
  }
  addTrade(w,v) {
    const id=text(v.id??randomUUID(),'entry ID',100);
    return this.tx(()=>{
      if (this.sql('SELECT 1 FROM trades WHERE w=? AND id=?').get(w,id)) return { id, duplicate:true };
      const t=v.t?timestamp(v.t):this.time(w), side=v.side, a=idOf(v.asset);
      check(t<=this.time(w)+60,'Trade timestamp cannot be in the future.');
      check(['deposit','opening','buy','sell'].includes(side),'Unknown entry type.');
      check(side==='deposit'?['chaos','gold'].includes(a):a!=='chaos'&&this.exists(w,a),'Choose a valid asset for this entry type.');
      const q=positive(v.qty,'Quantity'), px=side==='deposit'?0n:money(v.price,'Unit price');
      if (side==='buy'||side==='sell') check(px>0n,'Trade price must be positive.');
      const g=side==='deposit'?0n:uint(v.gold??0,'Gold spent'), p=this.prefs(w);
      this.sql('INSERT INTO trades(w,id,t,side,asset,qty,price,gold,tax,note,session) VALUES (?,?,?,?,?,?,?,?,?,?,?)').run(w,id,t,side,a,String(q),String(px),String(g),String(money(p.tax)),String(v.note??'').slice(0,240),text(v.session||'Default session','session name',80));
      account(this.trades(w),p.method); // Validate the entire chronological ledger, including backdated entries.
      this.event(w,t,'ledger','Ledger entry recorded',`${side} · ${q} ${a}`); return { id };
    });
  }
  chart(w,a,range='7D') {
    check(this.exists(w,a),'Unknown asset.');
    const days=({'24H':1,'7D':7,'30D':30,'90D':90})[range]; check(days,'Unknown chart range.');
    const {source}=this.world(w);
    // An exchange workspace can also accept user-supplied snapshots. Do not imply
    // that every bar is independently attributable to GGG in that case.
    const localSnapshots=source==='exchange'&&!!this.sql("SELECT 1 FROM events WHERE w=? AND kind='data' AND title='Snapshot imported' LIMIT 1").get(w);
    const last=this.sql('SELECT t FROM bars WHERE w=? AND a=? ORDER BY t DESC LIMIT 1').get(w,a)?.t;
    const meta={source,localSnapshots,requestedHours:days*24,hours:0,first:null,last:last??null};
    if (!last) return { ...meta,bars:[], interval:null };
    const raw=this.sql('SELECT * FROM bars WHERE w=? AND a=? AND t>? ORDER BY t').all(w,a,last-days*DAY);
    const span=(last-raw[0].t)/HOUR+1;
    // On a cold start, a few real hours should not collapse into a single 36h bar.
    const interval=Math.min(({'24H':1,'7D':4,'30D':12,'90D':36})[range],span<=2?1:span<=48?2:span<=168?4:span<=720?12:36)*HOUR;
    const bars=[]; let k=-1,b;
    for (const r of raw) {
      const key=Math.floor(r.t/interval);
      if (key!==k) { b={ t:r.t,o:r.o,h:r.h,l:r.l,c:r.c,v:0n }; bars.push(b); k=key; }
      b.h=max(BigInt(b.h),BigInt(r.h)); b.l=min(BigInt(b.l),BigInt(r.l)); b.c=r.c; b.v+=BigInt(r.v);
    }
    return { ...meta,hours:raw.length,first:raw[0].t,interval:interval/HOUR,bars:bars.map(b=>({t:b.t,o:decimal(b.o),h:decimal(b.h),l:decimal(b.l),c:decimal(b.c),v:String(b.v)})) };
  }
  depth(w,a) {
    const pref=this.prefs(w),tax=money(pref.tax),buy=[],sell=[];
    for(const q of this.book(w,this.marks(w),pref).good){
      const ask=q.src==='chaos'&&q.dst===a,bid=q.dst==='chaos'&&q.src===a;if(!ask&&!bid)continue;
      const size=ask?q.get:q.pay,chaos=ask?q.pay:q.get,fee=q.gold*tax;
      const n=chaos*U*U+(ask?fee:-fee),d=U*size;
      (ask?sell:buy).push({n,d,qty:size*q.lots,id:q.id});
    }
    const levels=(xs,dir)=>{
      xs.sort((a,b)=>a.n*b.d===b.n*a.d?0:a.n*b.d>b.n*a.d?dir:-dir);
      let total=0n;return xs.map(x=>({price:decimal(x.n/x.d),qty:String(x.qty),cumulative:String(total+=x.qty),quote:x.id}));
    };
    const asks=levels(sell,1),bids=levels(buy,-1);
    return {asks,bids,spread:buy.length&&sell.length?decimal((sell[0].n*buy[0].d-buy[0].n*sell[0].d)/(sell[0].d*buy[0].d)):null};
  }
  macro(w,a) {
    const world=this.world(w);
    if (!world.start||world.asof<world.start) return { day:null,curves:[],projection:null };
    const day=Math.floor((world.asof-world.start)/DAY)+1;
    const dayClose=(id,start,d)=>this.sql('SELECT c FROM bars WHERE w=? AND a=? AND t>=? AND t<? ORDER BY t DESC LIMIT 1').get(id,a,start+(d-1)*DAY,start+d*DAY)?.c;
    const current=dayClose(w,world.start,day), curves=[], forward=[];
    for (const r of this.worlds().filter(x=>x.game===world.game&&x.start&&x.start<world.start&&x.start+(day+7)*DAY<=world.asof+DAY&&(x.source==='demo')===(world.source==='demo'))) {
      const at=dayClose(r.id,r.start,day), next=dayClose(r.id,r.start,day+7);
      if (at&&next) { const change=bps(BigInt(next)-BigInt(at),BigInt(at)); forward.push(BigInt(change)); curves.push({ league:r.league,day,price:decimal(at),change }); }
    }
    forward.sort((a,b)=>a<b?-1:a>b?1:0);
    const n=forward.length, med=n?(forward[(n-1)>>1]+forward[n>>1])/2n:null;
    return { day,curves,projection:current&&med!==null?decimal(BigInt(current)*(BP+med)/BP):null };
  }
  history(w,a,days,start,end) {
    const last=this.sql('SELECT MAX(t) AS t FROM bars WHERE w=? AND a=?').get(w,a).t??0;
    const to=end?timestamp(end):last, from=start?timestamp(start):to-small(days??90,1,6000,'Replay days')*DAY+1;
    check(to>=from,'Replay end must follow its start.');
    return this.sql('SELECT t,o,h,l,c,v FROM bars WHERE w=? AND a=? AND t BETWEEN ? AND ? ORDER BY t').all(w,a,from,to);
  }
  alerts(w) { return this.sql('SELECT * FROM rules WHERE w=? ORDER BY id').all(w); }
  alert(w,v) {
    this.world(w);
    if (v.action==='delete') { this.sql('DELETE FROM rules WHERE w=? AND id=?').run(w,v.id); return; }
    if (v.action==='toggle') { this.sql('UPDATE rules SET enabled=1-enabled,hot=0 WHERE w=? AND id=?').run(w,v.id); return; }
    check(v.kind==='route'||(v.kind==='price'&&this.exists(w,v.asset)),'Invalid alert target.');
    check(['above','below'].includes(v.op),'Invalid threshold direction.');
    const threshold=money(v.threshold,'Threshold');
    this.sql('INSERT INTO rules VALUES (?,?,?,?,?,?,1,0)').run(w,randomUUID(),v.kind,v.kind==='route'?'chaos':v.asset,v.op,String(threshold));
  }
  evaluate(w,scanner,marks) {
    const events=[],t=this.time(w);
    this.tx(()=>{
      if(this.world(w).source!=='demo'){
        const filtered=hash(encode((scanner.rejected??[]).map(q=>[q.id,q.reason,q.seen])));
        if(filtered!==this.get(`filtered:${w}`,null)){
          if(scanner.rejected?.length)this.event(w,t,'filter','Liquidity filter applied',`${scanner.rejected.length} observations excluded from route evaluation.`);
          this.set(`filtered:${w}`,filtered);
        }
        let latest=0;for(const [id,m] of marks)if(id!=='chaos')latest=Math.max(latest,m.t);
        if(latest>this.get(`momentum:${w}`,0)){
          const prev=this.sql('SELECT c FROM bars WHERE w=? AND a=? AND t BETWEEN ? AND ? ORDER BY t DESC LIMIT 1');let top=null;
          for(const [a,m] of marks){
            if(a==='chaos'||m.t!==latest||t-latest>DAY)continue;
            const p=prev.get(w,a,latest-DAY-HOUR,latest-DAY);if(!p)continue;
            const change=bps(BigInt(m.c)-BigInt(p.c),BigInt(p.c));
            if(Math.abs(change)>=250&&(!top||Math.abs(change)>Math.abs(top.change)))top={a,change};
          }
          if(top){const a=this.sql('SELECT name FROM assets WHERE w=? AND id=?').get(w,top.a);this.event(w,t,'momentum','Momentum detected',`${a.name} moved ${top.change>0?'+':''}${(top.change/100).toFixed(2)}% over the last 24 hours.`);}
          this.set(`momentum:${w}`,latest);
        }
      }
      for (const a of this.alerts(w).filter(x=>x.enabled)) {
        const mark=marks.get(a.asset);
        const value=a.kind==='route'?(scanner.routes.length?micros(scanner.routes[0].net):null):mark&&mark.t<=t&&t-mark.t<=DAY?BigInt(mark.c):null;
        const hot=value!==null&&(a.op==='above'?value>=BigInt(a.threshold):value<=BigInt(a.threshold));
        if (hot&&!a.hot) {
          const title=a.kind==='route'?'Route target reached':'Price target reached', body=`${a.kind==='route'?'Best route':a.asset} at ${decimal(value)} c · ${a.op} ${decimal(a.threshold)} c`;
          this.event(w,t,'alert',title,body); events.push({w,title,body});
        }
        if (+hot!==a.hot) this.sql('UPDATE rules SET hot=? WHERE w=? AND id=?').run(+hot,w,a.id);
      }
      const prev=this.get(`open:${w}`,[]), paths=scanner.routes.map(r=>r.path.join(' → '));
      const first=scanner.routes.find((r,i)=>!prev.includes(paths[i]));
      if (first) {
        const body=`${first.path.join(' → ')} · +${first.net} c net`;
        this.event(w,t,'route','Arbitrage window opened',body); events.push({w,title:'Arbitrage window opened',body});
      }
      if (JSON.stringify(prev)!==JSON.stringify(paths)) this.set(`open:${w}`,paths);
    });
    return events;
  }
  signals(w,n=60) { return this.sql('SELECT seq,t,kind,title,body FROM events WHERE w=? ORDER BY seq DESC LIMIT ?').all(w,n); }
  desk(w) {
    const world=this.world(w),p=this.prefs(w),as=this.assets(w),m=this.marks(w,as),ac=account(this.trades(w),p.method);
    const portfolio=this.portfolio(w,m,p,ac),scanner=this.scanning(w,{},m,p,ac);
    const assets=this.market(w,as,m),hedges=assets.filter(a=>a.price&&a.change>0&&BigInt(a.volume)>0n&&/mirror|hinekora.?s lock|veiled (exalted )?orb|reflecting mist/i.test(a.name)).sort((a,b)=>b.change-a.change);
    for(const h of portfolio.holdings.filter(h=>h.stale&&h.markdown)){
      const budget=micros(h.markdown)*BigInt(h.qty),hedge=hedges.find(a=>a.id!==h.id&&micros(a.price)<=budget);
      if(hedge)h.redeploy={name:hedge.name,units:String(budget/micros(hedge.price)),change:hedge.change};
    }
    const demand=this.sql('SELECT * FROM demand WHERE w=? AND t BETWEEN ? AND ? ORDER BY t DESC').all(w,this.time(w)-7*DAY,this.time(w)).map(r=>({...r,chance:String(BigInt(r.sold)*BP/BigInt(r.listed))}));
    return {world,prefs:p,assets,portfolio,scanner,demand,signals:this.signals(w),alerts:this.alerts(w).map(a=>({...a,threshold:decimal(a.threshold)})),clock:this.time(w)};
  }
  archive(k,raw) { this.sql('INSERT INTO inbox VALUES (?,?,?)').run(k,now(),gzipSync(raw,{level:1})); }
  stashRows(w,rows,at) {
    check(at>=this.get(`stash:${w}`,0),'Stash snapshot predates the stored inventory.');
    check(Array.isArray(rows),'Stash must be an array.');
    this.sql('DELETE FROM stash WHERE w=?').run(w);
    for (const s of rows) { const a=idOf(s.asset); check(this.exists(w,a),`Unknown stash asset: ${a}`); this.sql('INSERT INTO stash VALUES (?,?,?)').run(w,a,String(uint(s.qty))); }
    this.set(`stash:${w}`,at);
  }
  importBatch(v,raw=encode(v)) {
    const game=small(v.game,1,2,'Game'),league=text(v.league,'league',100), source=v.target==='exchange'?'exchange':'import';
    const w=worldId(source,game,league),key=`file:${hash(raw)}`;
    if (this.sql('SELECT 1 FROM inbox WHERE k=?').get(key)) return { w,duplicate:true };
    return this.tx(()=>{
      const at=timestamp(v.asOf??now()); check(at<=now()+60,'Snapshot is in the future.');
      check(['bars','pairs','quotes','stash','demand'].some(k=>Array.isArray(v[k])),'Include bars, pairs, quotes, stash, or demand in the import.');
      const start=v.leagueStart?timestamp(v.leagueStart):null;check(!start||start<=at,'League start follows the snapshot timestamp.');
      this.ensureWorld(w,game,league,source,start,at);
      for (const a of v.assets??[]) {
        const id=idOf(a.id),d=describe(id); d.name=text(a.name??d.name,'asset name'); d.symbol=text(a.symbol??d.symbol,'symbol',16);
        d.kind=a.kind??d.kind; check(CATEGORIES.includes(d.kind),'Unknown asset category.'); this.ensureAsset(w,d);
      }
      const asset=x=>{ const a=idOf(x); check(this.exists(w,a),`Unknown asset: ${a}. Include it in assets.`); return a; };
      const time=x=>{ const t=timestamp(x); check(t<=at+60,'Observation follows the snapshot timestamp.'); return t; };
      for (const b of v.bars??[]) {
        const [o,h,l,c]=[b.open,b.high,b.low,b.close].map(x=>money(x)),t=time(b.t);
        check(t%HOUR===0,'Bars must be hourly and aligned to UTC.'); check(l>0n&&l<=min(o,c)&&h>=max(o,c),'Invalid OHLC range.');
        this.bar(w,asset(b.asset),t,o,h,l,c,uint(b.volume,'Volume'));
      }
      for (const p of v.pairs??[]) {
        check(p.a!==p.b,'A reference pair needs two different assets.');
        const a=asset(p.a),b=asset(p.b),t=time(p.t),va=positive(p.volumeA,'Volume A'),vb=positive(p.volumeB,'Volume B');this.pair(w,a,b,t,va,vb);
        if(a==='chaos'||b==='chaos'){
          const id=a==='chaos'?b:a,qty=a==='chaos'?vb:va,price=(a==='chaos'?va:vb)*U/qty;
          // Explicit OHLC bars have precedence over a flat mark derived from one pair.
          if(price>0n)this.sql('INSERT OR IGNORE INTO bars VALUES (?,?,?,?,?,?,?,?)').run(w,id,Math.floor(t/HOUR)*HOUR,String(price),String(price),String(price),String(price),String(qty));
        }
      }
      if (v.quotes!==undefined) {
        check(Array.isArray(v.quotes)&&at>=this.get(`quotes:${w}`,0),'Invalid or out-of-order listing snapshot.');
        this.sql('DELETE FROM quotes WHERE w=?').run(w);
        for (const q of v.quotes) {
          check(q.from!==q.to&&typeof q.online==='boolean'&&typeof q.confirmed==='boolean','Quotes need different assets and explicit online/confirmed flags.');
          this.quote(w,{id:text(q.id,'quote ID'),src:asset(q.from),dst:asset(q.to),pay:positive(q.pay,'Pay lot'),get:positive(q.get,'Receive lot'),
            lots:positive(q.lots,'Available lots'),gold:uint(q.gold,'Gold per lot'),seen:time(q.seen??at),online:q.online,confirmed:q.confirmed});
        }
        this.set(`quotes:${w}`,at);
      }
      if (v.stash!==undefined) this.stashRows(w,v.stash,at);
      for(const r of v.demand??[]){
        const listed=positive(r.listed,'Listed sample'),sold=uint(r.sold,'Sold sample');
        check(sold<=listed,'Sold count exceeds the listing cohort.');
        this.sql('INSERT INTO demand VALUES (?,?,?,?,?,?,?) ON CONFLICT(w,a,build) DO UPDATE SET listed=excluded.listed,sold=excluded.sold,minutes=excluded.minutes,t=excluded.t WHERE excluded.t>=t')
          .run(w,asset(r.asset),text(r.build,'build cohort',100),String(listed),String(sold),small(r.minutes,1,10080,'Sale window'),time(r.seen??at));
      }
      this.archive(key,raw); this.event(w,at,'data','Snapshot imported',`${v.bars?.length??0} hourly bars · ${v.quotes?.length??0} listing lots`); return { w, duplicate:false };
    });
  }
}
