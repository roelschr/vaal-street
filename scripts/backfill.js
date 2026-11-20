import { parseArgs } from 'node:util';
import { resolve } from 'node:path';
import { mkdirSync } from 'node:fs';
import { setTimeout as wait } from 'node:timers/promises';
import { Store,now } from '../src/store.js';
import { check,small,timestamp,HOUR } from '../src/fixed.js';
import { digest } from '../src/feed.js';
const {values:v}=parseArgs({options:{game:{type:'string'},from:{type:'string'},to:{type:'string'},limit:{type:'string'}}});
const game=small(v.game,1,2,'Game'),from=Math.floor(timestamp(v.from)/HOUR)*HOUR,to=Math.floor(timestamp(v.to)/HOUR)*HOUR;
check(to>=from&&to<now(),'Choose an ordered historical UTC range.');
const dir=resolve(process.env.VAAL_DATA_DIR??'data');mkdirSync(dir,{recursive:true});
const s=new Store(resolve(dir,'terminal.sqlite')),key=`backfill:${game}:${from}:${to}`;
const limit=small(v.limit??24,1,100000,'Page limit');
try{
  let cursor=s.get(key,from);
  for(let i=0;cursor<=to&&i<limit;i++){
    const blocked=s.get(`backfill:retry:${game}`,0);check(blocked<=now(),`Rate-limited until ${new Date(blocked*1000).toISOString()}.`);
    const r=await fetch(`https://web.poecdn.com/api/currency-exchange${game===2?'/poe2':''}/${cursor}`,{headers:{'User-Agent':process.env.POE_USER_AGENT??'VaalStreet/1.0 (local history import)'},redirect:'error',signal:AbortSignal.timeout(30000)});
    if(!r.ok){const h=r.headers.get('Retry-After');s.set(`backfill:retry:${game}`,now()+Math.max(60,Number(h)||Math.ceil((Date.parse(h)-Date.now())/1000)||0));throw new Error(`HTTP ${r.status}; progress saved.`);}
    const result=digest(s,game,cursor,await r.text(),false);if(result.tip)break;
    cursor=result.next;s.set(key,cursor);console.log(`${new Date((cursor-HOUR)*1000).toISOString()} · ${result.duplicate?'already archived':result.markets+' rows'}`);
    if(i+1<limit&&cursor<=to)await wait(12000);
  }
  console.log(cursor>to?'Range complete.':`Resume the same command at ${new Date(cursor*1000).toISOString()}.`);
}finally{s.db.close();}
