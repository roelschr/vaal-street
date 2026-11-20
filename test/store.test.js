import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync,rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { Store,now,encode } from '../src/store.js';
import { HOUR,U } from '../src/fixed.js';
import { digest } from '../src/feed.js';
import { stashPage } from '../src/listings.js';
import { example } from '../src/example.js';
import { chartAxis } from '../public/ui.js';
const T=Math.floor(now()/HOUR)*HOUR;
const setup=()=>new Store(':memory:');
test('snapshot replays are idempotent; invalid and out-of-order replacements roll back',()=>{
  const s=setup(),v=example(T),{w}=s.importBatch(v);
  assert.equal(s.importBatch(v).duplicate,true);
  const broken={...v,quotes:[v.quotes[0],{...v.quotes[1],gold:-1}]};
  assert.throws(()=>s.importBatch(broken),/Gold per lot/);
  assert.equal(s.sql('SELECT count(*) n FROM quotes WHERE w=?').get(w).n,2);
  assert.equal(s.sql('SELECT count(*) n FROM inbox').get().n,1);
  assert.throws(()=>s.importBatch({game:1,league:v.league,asOf:T-1,quotes:[]}),/out-of-order/);
  assert.equal(s.portfolio(w).worth,'0');assert.equal(s.portfolio(w).stash.length,2);
  assert.equal(s.marks(w).get('exalted').c,String(15n*U));
  s.addTrade(w,{id:'balance',side:'deposit',asset:'chaos',qty:'1000'});
  s.addTrade(w,{id:'balance',side:'deposit',asset:'chaos',qty:'1000'});
  assert.equal(s.portfolio(w).cash,'1000');
  assert.throws(()=>s.addTrade(w,{side:'buy',asset:'divine',qty:'10',price:'184'}),/Insufficient Chaos/);
  assert.equal(s.trades(w).length,1);
  const w2=s.importBatch({...v,game:2}).w;assert.notEqual(w2,w);assert.equal(s.portfolio(w2).cash,'0');s.db.close();
});
const A='Metadata/Items/Currency/CurrencyRerollRare',B='Metadata/Items/Currency/CurrencyModValues';
const market=(league,a,b,va,vb)=>({league,market_pair:[a,b],volume_traded:{[a]:va,[b]:vb}});
test('sparse exchange history reports its origin and coverage and leaves missing chart buckets empty',()=>{
  const s=setup(),t=Math.floor(T/(2*HOUR))*2*HOUR-8*HOUR;
  const hour=(n,va,vb)=>digest(s,1,t+n*HOUR,encode({next_change_id:t+(n+1)*HOUR,markets:[market('Standard',A,B,va,vb)]}),false);
  const [w]=hour(4,240,2).worlds;hour(0,200,2);hour(1,330,3);
  const c=s.chart(w,'divine','7D');
  assert.deepEqual([c.source,c.localSnapshots,c.hours,c.requestedHours,c.interval],['exchange',false,3,168,2]);
  assert.deepEqual(c.bars,[{t,o:'100',h:'110',l:'100',c:'110',v:'5'},{t:t+4*HOUR,o:'120',h:'120',l:'120',c:'120',v:'2'}]);
  const axis=chartAxis(c,400);assert.equal(axis.indexAt(axis.x(1)),-1);assert.equal(axis.indexAt(axis.x(2)),1);
  assert.equal(s.trades(w).length,0);
  s.importBatch({game:1,league:'Standard',target:'exchange',asOf:T,bars:[{asset:'divine',t,open:'99',high:'99',low:'99',close:'99',volume:'1'}]});
  assert.equal(s.chart(w,'divine','7D').localSnapshots,true);s.db.close();
});
test('digests preserve raw integers, stock-only rows, cursor atomicity and independent backfill',()=>{
  const s=setup(),raw=JSON.stringify({next_change_id:T+HOUR,markets:[market('L',A,B,'9007199254740993','3'),market('L',A,'ZeroStock',0,0)]}).replace('"9007199254740993"','9007199254740993');
  const [w]=digest(s,1,T,raw).worlds;assert.equal(s.marks(w).get('divine').c,String(9007199254740993n*U/3n));
  assert.equal(s.scanning(w).quotes.length,0);assert.equal(s.get('cursor:1'),T+HOUR);
  assert.throws(()=>digest(s,1,T+HOUR,encode({next_change_id:T+2*HOUR,markets:[market('L',A,B,200,1),market('bad',A,B,-1,1)]})));
  assert.equal(s.get('cursor:1'),T+HOUR);assert.equal(s.sql('SELECT count(*) n FROM inbox').get().n,1);
  digest(s,1,T-HOUR,encode({next_change_id:T,markets:[market('L',A,B,100,1)]}),false);
  assert.equal(s.get('cursor:1'),T+HOUR);assert.equal(s.marks(w).get('divine').t,T);
  assert.equal(digest(s,1,T,raw).duplicate,true);assert.equal(s.worlds().length,1);s.db.close();
});
test('withdrawn public stashes retract even a manually reconfirmed listing',()=>{
  const s=setup(),tab={id:'tab1',public:true,league:'L',stash:'Sales',items:[{frameType:5,baseType:'Divine Orb',id:'item1',stackSize:10,note:'~price 360/2 chaos'}]};
  const {worlds:[w],count}=stashPage(s,null,encode({next_change_id:'p1',stashes:[tab]}),T);assert.equal(count,1);
  const q=s.sql('SELECT * FROM quotes WHERE w=?').get(w);assert.equal(q.pay,'180');assert.equal(q.lots,'10');assert.equal(q.confirmed,0);
  s.manualQuote(w,{review:true,id:q.id,from:q.src,to:q.dst,pay:q.pay,get:q.get,lots:'8',gold:'0',confirmed:true,online:true});
  stashPage(s,'p1',encode({next_change_id:'p2',stashes:[{id:'tab1',public:false}]}),T);
  assert.equal(s.sql('SELECT count(*) n FROM quotes').get().n,0);assert.equal(s.get('psapi:cursor'),'p2');s.db.close();
});
test('thresholds fire on crossings only, rearm after exits, and survive reevaluation',()=>{
  const s=setup(),{w}=s.importBatch(example(T)),sc={routes:[]};
  s.alert(w,{kind:'price',asset:'divine',op:'above',threshold:'185'});
  const evaluate=c=>s.evaluate(w,sc,new Map([['divine',{c:String(BigInt(c)*U),t:T}]]));
  assert.equal(evaluate(180).length,0);assert.equal(evaluate(186).length,1);assert.equal(evaluate(187).length,0);
  evaluate(183);assert.equal(evaluate(185).length,1);
  assert.equal(s.signals(w).filter(e=>e.kind==='alert').length,2);s.db.close();
});
test('SQLite recovers an interrupted writer without partial cursor advancement or lost committed data',()=>{
  const dir=mkdtempSync(join(tmpdir(),'vaal-recovery-')),path=join(dir,'test.sqlite');
  try{
    const s=new Store(path),{w}=s.importBatch(example(T));s.set('cursor:1',T);
    const child=spawnSync(process.execPath,['--input-type=module','-e',`import {DatabaseSync} from 'node:sqlite';const db=new DatabaseSync(process.argv[1]);db.exec("BEGIN IMMEDIATE; UPDATE kv SET v='1' WHERE k='cursor:1'; DELETE FROM quotes;");process.exit(77);`,path]);
    assert.equal(child.status,77);s.db.close();
    const reopened=new Store(path);assert.equal(reopened.sql('PRAGMA integrity_check').get().integrity_check,'ok');
    assert.equal(reopened.get('cursor:1'),T);assert.equal(reopened.sql('SELECT count(*) n FROM quotes WHERE w=?').get(w).n,2);reopened.db.close();
  }finally{rmSync(dir,{recursive:true,force:true});}
});
