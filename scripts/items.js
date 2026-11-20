// Optional maintenance task. All imagery is bundled; starting the desk never contacts the CDN.
import { mkdirSync, writeFileSync } from 'node:fs';
import { catalog } from '../src/catalog.js';
mkdirSync('public/items',{recursive:true});
await Promise.all(Object.entries(catalog).filter(([,v])=>v[3]).map(async ([id,v])=>{
  const art=({ancient:'AncientOrb',annul:'AnnullOrb'})[id]??v[3];
  const url=`https://web.poecdn.com/image/Art/2DItems/Currency/${art}.png`;
  const r=await fetch(url,{signal:AbortSignal.timeout(15000)});
  if (!r.ok||!r.headers.get('content-type')?.includes('png')) throw new Error(`Image unavailable: ${id} (${r.status})`);
  writeFileSync(`public/items/${id}.png`,Buffer.from(await r.arrayBuffer()));
}));
