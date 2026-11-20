import http from 'node:http';
import { readFileSync, mkdirSync, readdirSync, statSync, unlinkSync, createReadStream } from 'node:fs';
import { join, dirname, extname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { tmpdir } from 'node:os';
import { randomUUID } from 'node:crypto';
import { Store, encode, now, HOME_WORLD } from './src/store.js';
import { seed } from './src/demo.js';
import { Feed, parseExact } from './src/feed.js';
import { craft,replay,transform } from './src/engine.js';
import { check,small,text,timestamp } from './src/fixed.js';
import { ListingsFeed } from './src/listings.js';
import { basket } from './src/basket.js';
import { example } from './src/example.js';

const root=dirname(fileURLToPath(import.meta.url)), publicDir=join(root,'public');
const data=resolve(process.env.VAAL_DATA_DIR??join(root,'data')),port=Number(process.env.PORT??3000);
mkdirSync(data,{recursive:true});
const store=new Store(join(data,'terminal.sqlite'));
for(const game of [1,2])store.personalWorld(game,'Standard','exchange');
const clients=new Set();
function send(type,obj) { const line=`event: ${type}\ndata: ${encode(obj)}\n\n`; for (const c of clients) c.write(line); }
function changed(w) {
  const marks=store.marks(w),sc=store.scanning(w,{},marks),events=store.evaluate(w,sc,marks);
  send('update',{w,world:store.world(w),events});
  if (process.env.VAAL_WEBHOOK_URL) for (const e of events) {
    if (store.world(w).source==='demo') continue;
    fetch(process.env.VAAL_WEBHOOK_URL,{method:'POST',headers:{'Content-Type':'application/json'},
      body:encode({content:`Vaal Street · ${e.title}: ${e.body}`,event:e}),redirect:'error',signal:AbortSignal.timeout(6000)})
      .then(r=>{if(!r.ok)console.error(`Alert webhook: HTTP ${r.status}`);}).catch(e=>console.error(`Alert webhook: ${e.message}`));
  }
}
const feed=new Feed(store,changed);
const listings=new ListingsFeed(store,changed);
let ingestState={last:null,error:null},scanningDir=false;
const processed=new Map();
function inbox() {
  const dir=process.env.VAAL_IMPORT_DIR; if (!dir||scanningDir) return; scanningDir=true;
  try {
    for (const name of readdirSync(dir).filter(n=>n.endsWith('.json')).sort()) {
      const path=join(dir,name),st=statSync(path),key=`${st.mtimeMs}:${st.size}`;
      if (processed.get(path)===key) continue;
      try {
        check(st.size<=20*1024*1024,'Import limit is 20 MiB.');
        const raw=readFileSync(path,'utf8'),result=store.importBatch(parseExact(raw),raw);
        processed.set(path,key); ingestState={last:now(),error:null}; if (!result.duplicate) changed(result.w);
      } catch (e) { ingestState={last:now(),error:`${name}: ${e.message}`}; processed.set(path,key); }
    }
  } catch(e) { ingestState={last:now(),error:e.message}; } finally { scanningDir=false; }
}
function dataStatus() { return { ...feed.status(),inbox:ingestState,worlds:store.worlds(),
  archived:store.sql('SELECT count(*) n FROM inbox').get().n,bytes:statSync(join(data,'terminal.sqlite')).size,
  webhook:!!process.env.VAAL_WEBHOOK_URL,listings:listings.status() }; }
async function body(req) {
  check(req.headers['content-type']?.split(';')[0]==='application/json','Send application/json.');
  const chunks=[]; let bytes=0;
  for await (const chunk of req) { bytes+=chunk.length; check(bytes<=20*1024*1024,'Request exceeds 20 MiB.'); chunks.push(chunk); }
  const raw=Buffer.concat(chunks).toString('utf8'),v=parseExact(raw||'{}'); check(v&&typeof v==='object'&&!Array.isArray(v),'Expected a JSON object.'); return {raw,v};
}
const types={'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8','.svg':'image/svg+xml','.png':'image/png','.json':'application/json','.woff2':'font/woff2'};
const server=http.createServer(async(req,res)=>{
  const reply=(v,status=200)=>{ res.writeHead(status,{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store'});res.end(encode(v)); };
  try {
    const host=req.headers.host??'';
    if (!/^(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$/i.test(host)) return reply({error:'Loopback host required.'},403);
    const u=new URL(req.url,`http://${host}`),p=u.pathname,w=u.searchParams.get('w')??HOME_WORLD;
    if (req.headers.origin&&req.headers.origin!==u.origin) return reply({error:'Same-origin request required.'},403);
    res.setHeader('X-Content-Type-Options','nosniff');
    res.setHeader('Referrer-Policy','no-referrer');
    res.setHeader('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'");
    if (req.method==='GET') {
      if (p==='/example.json') { res.setHeader('Content-Disposition','attachment; filename="vaal-practice.json"');return reply(example()); }
      if (p==='/api/worlds') return reply(store.worlds());
      if (p==='/api/desk') return reply(store.desk(w));
      if (p==='/api/chart') return reply({...store.chart(w,u.searchParams.get('a'),u.searchParams.get('range')??'7D'),macro:store.macro(w,u.searchParams.get('a')),depth:store.depth(w,u.searchParams.get('a'))});
      if (p==='/api/data') return reply(dataStatus());
      if (p==='/api/backup') {
        const f=join(tmpdir(),`vaal-${randomUUID()}.sqlite`);
        store.sql('VACUUM INTO ?').run(f);
        res.writeHead(200,{'Content-Type':'application/octet-stream','Content-Disposition':'attachment; filename="vaal-street.sqlite"','Content-Length':statSync(f).size});
        const file=createReadStream(f); file.on('close',()=>{try{unlinkSync(f);}catch{}}); res.on('close',()=>file.destroy());file.on('error',()=>res.destroy()); return file.pipe(res);
      }
      if (p==='/api/events') {
        res.writeHead(200,{'Content-Type':'text/event-stream','Cache-Control':'no-cache','Connection':'keep-alive'}); res.write(': connected\n\n');
        clients.add(res); req.on('close',()=>clients.delete(res)); return;
      }
      if (p.startsWith('/api/')) return reply({error:'Endpoint not found.'},404);
      const f=resolve(publicDir,'.'+decodeURIComponent(p==='/'?'/index.html':p));
      if (!f.startsWith(publicDir+sep)||!types[extname(f)]) return reply({error:'Not found.'},404);
      let buf; try {buf=readFileSync(f);} catch {return reply({error:'File not found.'},404);}
      res.writeHead(200,{'Content-Type':types[extname(f)],'Cache-Control':extname(f)==='.png'?'public, max-age=604800':'no-cache'}); return res.end(buf);
    }
    if (req.method!=='POST') return reply({error:'Method not allowed.'},405);
    const {raw,v}=await body(req);
    if (p==='/api/import') { const result=store.importBatch(v,raw); if(!result.duplicate)changed(result.w); return reply(result); }
    if (p==='/api/demo') {
      const game=small(v.game??1,1,2,'Game');seed(store);return reply({w:`demo-${game}`});
    }
    if (p==='/api/workspace') {
      const game=small(v.game,1,2,'Game'),league=text(v.league,'league',100),start=v.start?timestamp(v.start):null;
      check(!start||start<=now(),'League start cannot be in the future.');
      return reply({w:store.personalWorld(game,league,'import',start)});
    }
    if (p==='/api/sync') {
      if (typeof v.automatic==='boolean') store.set('feed:auto',v.automatic);
      if (v.pull===true) await feed.sync(true);
      return reply(dataStatus());
    }
    if(p==='/api/listings-sync'){
      check(listings.status().available,'Set a service:psapi token in POE_PUBLIC_STASH_TOKEN first.');
      if(typeof v.automatic==='boolean')store.set('psapi:auto',v.automatic);
      if(v.pull)await listings.pull();return reply(dataStatus());
    }
    store.world(w);
    if (p==='/api/trade') { const result=store.addTrade(w,v); changed(w);return reply(result); }
    if (p==='/api/quote') { const result=store.manualQuote(w,v);changed(w);return reply(result); }
    if (p==='/api/settings') { const result=store.settings(w,v);changed(w);return reply(result); }
    if (p==='/api/alerts') { store.alert(w,v);changed(w);return reply({ok:true}); }
    if (p==='/api/scan') return reply(store.scanning(w,v));
    if (p==='/api/backtest') {
      const opts={...v,world:w,tax:store.prefs(w).tax};
      if(v.strategy==='rotation'){
        const ids=[...new Set([v.asset,'divine','exalted','ancient'])],end=v.end??store.world(w).asof;
        return reply(basket(Object.fromEntries(ids.map(a=>[a,store.history(w,a,v.days,v.start,end)])),opts));
      }
      return reply(replay(store.history(w,v.asset,v.days,v.start,v.end),opts));
    }
    if (p==='/api/craft') return reply(craft(v));
    if (p==='/api/transform') return reply(transform({...v,tax:store.prefs(w).tax}));
    if (p==='/api/stash-sync') return reply(await feed.stash(w));
    return reply({error:'Endpoint not found.'},404);
  } catch(e) { if(!res.headersSent)reply({error:e.message},400); else res.end(); }
});
const tick=setInterval(()=>{ if (store.get('feed:auto',false)) feed.sync().catch(console.error);if(store.get('psapi:auto',false))listings.pull().catch(console.error);inbox(); },1000);
const expiry=setInterval(()=>{
  for (const w of store.sql("SELECT DISTINCT w FROM quotes UNION SELECT DISTINCT w FROM rules WHERE enabled=1").all()) {
    if (store.world(w.w).source!=='demo') changed(w.w);
  }
  for (const c of clients) c.write(': heartbeat\n\n');
},15000);
let stashBusy=false;
const stashTimer=setInterval(async()=>{
  const w=process.env.POE_STASH_AUTO_WORLD;if(!w||!feed.status().stashAvailable||stashBusy||store.get('stash:auto-retry',0)>now())return;
  stashBusy=true;store.set('stash:auto-retry',now()+900);
  try{await feed.stash(w);}catch(e){console.error(`Stash refresh: ${e.message}`);}finally{stashBusy=false;}
},15000);
server.on('error',e=>{console.error(`Vaal Street: ${e.message}`);process.exit(1);});
server.listen(port,'127.0.0.1',()=>console.log(`Vaal Street terminal: http://localhost:${server.address().port}\nLocal database: ${join(data,'terminal.sqlite')}`));
function stop(){ clearInterval(tick);clearInterval(expiry);clearInterval(stashTimer);for(const c of clients)c.end();server.close(()=>{store.db.close();process.exit(0);}); }
process.on('SIGINT',stop);process.on('SIGTERM',stop);
