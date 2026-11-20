import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtempSync,rmSync,writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { DatabaseSync } from 'node:sqlite';
test('local HTTP workflows: empty personal workspace, exact trade, price alerts, replay and restorable backup',async()=>{
  const dir=mkdtempSync(join(tmpdir(),'vaal-http-'));
  const child=spawn(process.execPath,['server.js'],{env:{...process.env,PORT:'0',VAAL_DATA_DIR:dir,VAAL_IMPORT_DIR:'',VAAL_WEBHOOK_URL:'',POE_STASH_AUTO_WORLD:''},stdio:['ignore','pipe','pipe']});
  try{
    const url=await new Promise((res,rej)=>{let output='';child.stdout.on('data',b=>{output+=b;const m=/http:\/\/localhost:(\d+)/.exec(output);if(m)res(`http://127.0.0.1:${m[1]}`);});child.on('error',rej);child.on('exit',code=>rej(new Error(`Server exited ${code}`)));});
    const get=async path=>{const r=await fetch(url+path);assert.equal(r.status,200);return r.json();};
    const post=async(path,v,status=200)=>{const r=await fetch(url+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(v)});const j=await r.json();assert.equal(r.status,status,JSON.stringify(j));return j;};
    const worlds=await get('/api/worlds');assert.equal(worlds.filter(w=>w.source==='demo').length,0);
    const home=await get('/api/desk');assert.equal(home.world.id,worlds[0].id);assert.equal(home.world.source,'exchange');
    assert.deepEqual(home.portfolio.entries,[]);assert.deepEqual(home.portfolio.holdings,[]);assert.deepEqual(home.scanner.routes,[]);
    for(const k of ['cash','gold','worth','realized'])assert.equal(home.portfolio[k],'0');
    await post('/api/trade',{id:'my-opening-balance',side:'deposit',asset:'chaos',qty:'11'});
    const {w:demo}=await post('/api/demo',{game:1});
    const sample=await get('/api/desk?w='+demo);assert.equal(sample.world.source,'demo');assert.equal(sample.scanner.routes.length,4);
    assert.ok(sample.portfolio.entries.length>0);await post('/api/demo',{game:2});
    assert.equal((await get('/api/desk?w='+demo)).portfolio.entries.length,sample.portfolio.entries.length);
    const personal=(await get('/api/desk')).portfolio;assert.equal(personal.cash,'11');assert.deepEqual(personal.entries.map(e=>e.id),['my-opening-balance']);
    const {w}=await post('/api/workspace',{game:1,league:'My local league'});assert.equal((await get('/api/desk?w='+w)).portfolio.worth,'0');
    const example=await get('/example.json');const id=(await post('/api/import',{...example,league:'My local league'})).w;assert.equal(id,w);
    const trade=v=>post('/api/trade?w='+w,v);
    await trade({id:'p1',side:'deposit',asset:'chaos',qty:'1000'});await trade({side:'deposit',asset:'gold',qty:'10000'});
    await trade({side:'buy',asset:'divine',qty:'2',price:'180.000001',gold:'200',session:'Mapping'});
    await trade({side:'sell',asset:'divine',qty:'1',price:'185.000002',gold:'114',session:'Mapping'});
    const d=await get('/api/desk?w='+w);assert.equal(d.portfolio.cash,'825');assert.equal(d.portfolio.realized,'4.997433');assert.equal(d.portfolio.sessions[0].name,'Mapping');
    assert.equal(d.scanner.routes.length,1);const ch=await get(`/api/chart?w=${w}&a=divine&range=24H`);assert.equal(ch.bars.length,24);assert.equal(ch.depth.asks[0].qty,'20');
    await post('/api/alerts?w='+w,{kind:'route',op:'above',threshold:'1'});assert.equal((await get('/api/desk?w='+w)).alerts[0].hot,1);
    const replay=await post('/api/backtest?w='+demo,{asset:'divine',days:90});assert.equal(replay.bars,2160);assert.ok(replay.fills>0);
    const backup=await fetch(url+'/api/backup');assert.equal(backup.status,200);
    const path=join(dir,'snapshot.sqlite');writeFileSync(path,Buffer.from(await backup.arrayBuffer()));
    const db=new DatabaseSync(path,{readOnly:true});assert.equal(db.prepare('PRAGMA integrity_check').get().integrity_check,'ok');assert.equal(db.prepare('SELECT count(*) n FROM trades WHERE w=?').get(w).n,4);db.close();
    const foreign=await fetch(url+'/api/trade',{method:'POST',headers:{Origin:'http://other.invalid','Content-Type':'application/json'},body:'{}'});assert.equal(foreign.status,403);
  }finally{
    if(child.exitCode===null&&child.signalCode===null){const closed=once(child,'exit');child.kill();await closed;}
    rmSync(dir,{recursive:true,force:true,maxRetries:3});
  }
});
