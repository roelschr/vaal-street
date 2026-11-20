import { mkdtempSync,rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Store,now } from '../src/store.js';
import { seed } from '../src/demo.js';
import { replay,scan } from '../src/engine.js';
import { digest } from '../src/feed.js';
import { HOUR,money } from '../src/fixed.js';
const dir=mkdtempSync(join(tmpdir(),'vaal-bench-')),s=new Store(join(dir,'bench.sqlite'));
function timing(label,n,run) {
  run();const samples=[];let r;
  for(let i=0;i<n;i++){const start=performance.now();r=run();samples.push(performance.now()-start);}
  samples.sort((a,b)=>a-b);console.log(`${label}: median ${samples[Math.floor(n/2)].toFixed(2)} ms | p95 ${samples[Math.ceil(.95*n)-1].toFixed(2)} ms | ${n} runs`);return r;
}
try{
  seed(s);console.log(`Vaal Street local benchmarks · ${process.version} / ${process.platform} / ${process.arch}`);
  const d=s.desk('demo-1');
  timing(`Sample graph including SQL screening (${d.scanner.accepted} accepted quotes)`,100,()=>s.scanning('demo-1'));
  const edges=[];
  const push=(src,dst,pay,get)=>edges.push({id:`${src}-${dst}`,src,dst,pay:BigInt(pay),get:BigInt(get),lots:5000n,gold:8n});
  for(let i=0;i<128;i++){
    const a=`asset${i}`,p=i+4;push('chaos',a,p,1);push(a,'chaos',1,p-1);
    for(const gap of [1,3,7])if(i+gap<128)push(a,`asset${i+gap}`,p+gap,p+(i%9===0?2:-1));
  }
  const sc=timing(`Funded graph, 128 assets + Chaos, ${edges.length} quotes, 4 legs`,70,()=>scan(edges,15000n,1250000n,money('12'),4));
  console.log(`  ${sc.visited} edge evaluations · ${sc.routes.length} profitable cycles · truncated: ${sc.truncated}`);
  const r=timing('90-day replay including SQLite history read',50,()=>replay(s.history('demo-1','divine',90),{strategy:'reversion'}));
  console.log(`  ${r.bars} hourly bars · ${r.fills} fills`);
  const t=Math.floor(now()/HOUR)*HOUR,markets=[];
  for(let i=0;i<4000;i++){const a='Metadata/Items/Currency/CurrencyRerollRare',b=`Metadata/Items/Fixture/Currency${i}`;markets.push({league:`Bench ${i%4}`,market_pair:[a,b],volume_traded:{[a]:200+i,[b]:9}});}
  let cursor=t-30*HOUR;
  timing('4,000-pair digest: JSON parse, normalize, gzip archive, FULL SQLite commit',12,()=>{const hour=cursor;cursor+=HOUR;return digest(s,1,hour,JSON.stringify({next_change_id:cursor,markets}));});
  console.log(`Database integrity: ${s.sql('PRAGMA integrity_check').get().integrity_check}. Timings exclude network/publication delay.`);
}finally{s.db.close();rmSync(dir,{recursive:true,force:true});}
