import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { existsSync } from 'node:fs';
import { parseArgs } from 'node:util';
import { Store,encode,HOME_WORLD } from '../src/store.js';
import { seed } from '../src/demo.js';
import { replay } from '../src/engine.js';
import { basket } from '../src/basket.js';
const {values:o}=parseArgs({options:Object.fromEntries(['world','worlds','asset','assets','days','from','to','strategy','module','capital','gold','window','band','delay','decay','slip','impact','participation','fee','tax','db'].map(k=>[k,{type:'string'}]))});
const file=resolve(o.db??`${process.env.VAAL_DATA_DIR??'data'}/terminal.sqlite`);
if(!existsSync(file))throw new Error('Start Vaal Street once to initialize the local database.');
const store=new Store(file);
try{
  const ws=(o.worlds??o.world??HOME_WORLD).split(','),ids=(o.assets??o.asset??'divine').split(',');
  if(ws.some(w=>/^demo-[12](-history)?$/.test(w)))seed(store);
  for(const w of ws){
    const world=store.world(w),a=ids[0],opts={tax:store.prefs(w).tax,...o,world:w};
    // Isolate module-level strategy state between leagues as well as their bankrolls.
    const url=o.module?pathToFileURL(resolve(o.module)):null;if(url)url.searchParams.set('world',w);
    const strategy=url?(await import(url.href)).default:undefined;
    if(o.module&&typeof strategy!=='function')throw new Error('A strategy module must export a default decision function.');
    const r=o.assets?basket(Object.fromEntries(ids.map(id=>[id,store.history(w,id,o.days??90,o.from,o.to??world.asof)])),opts,strategy):
      replay(store.history(w,a,o.days??90,o.from,o.to),opts,strategy);
    console.log(encode({world:w,assets:ids,...r}));
  }
}finally{store.db.close();}
