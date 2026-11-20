import { U,HOUR,DAY,decimal,money } from './fixed.js';
import { describe } from './catalog.js';
import { now } from './store.js';

const poe1={ divine:'186.40', exalted:'14.80', mirror:'156800',ancient:'12.80',annul:'6.25',vaal:'0.64',fusing:'0.18',
  alchemy:'0.14',thread:'7.5',ember:'2.46',viscera:'1.2',astrolabe:'39',patient:'4.2',nurse:'35.7',essence:'2.6' };
const poe2={ divine:'76',exalted:'0.83',mirror:'97000',annul:'3.6',vaal:'0.25',alchemy:'0.18',omen:'4.6',catalyst:'2.2',essence:'1.8' };
function rnd(x,s) { x=Math.imul(x^s,0x45d9f3b); x=Math.imul(x^(x>>>16),0x45d9f3b); return (x^(x>>>16))>>>0; }
function noise(t,period,amplitude,s) {
  const i=Math.floor(t/period), a=rnd(i,s)%amplitude,b=rnd(i+1,s)%amplitude;
  return a+Math.trunc((b-a)*(t%period)/period)-Math.floor(amplitude/2);
}
function shape(t,n,s) {
  return 7100+Math.trunc(t*2900/n)+noise(t,147,1000,s)+noise(t,27,790,s+5)+noise(t,6,340,s+9)+noise(t,1,140,s);
}
function sampleWorld(store,id,game,league,end,days,archive=false) {
  const n=days*24, start=end-(n-1)*HOUR, prices=game===1?poe1:poe2;
  store.ensureWorld(id,game,league,'demo',start,end);
  let idx=0;
  for (const [a,target] of Object.entries(prices)) {
    store.ensureAsset(id,describe(a)); const base=money(target), seed=82+idx++*73+(archive?183:0), last=shape(n-1,n,seed);
    let previous=base*7n/10n;
    for (let i=0;i<n;i++) {
      const t=start+i*HOUR;
      // Reproducible value noise with multi-day, intra-day, and hourly components.
      let q=shape(i,n,seed)-last+10000;
      if (a==='divine' && i>=n-193) {
        const anchors=[-840,-655,-745,-520,-650,-315,-390,-245,-65,-130,-280,-220,-390,-180,-250,-95,0];
        const x=(i-(n-193))/12,k=Math.min(15,Math.floor(x)),f=x-k;
        q=10000+Math.trunc(anchors[k]*(1-f)+anchors[k+1]*f)+noise(i,3,105,seed)-noise(n-1,3,105,seed);
      }
      const close=base*BigInt(q)/10000n;
      const wick=base*BigInt(6+rnd(i,seed+31)%70)/10000n;
      const high=(close>previous?close:previous)+wick,low=(close<previous?close:previous)-wick;
      const volume=(a==='mirror'?1+rnd(i,seed)%7:160+rnd(i,seed)%1880);
      store.bar(id,a,t,previous,high,low>0n?low:1n,close,volume); previous=close;
    }
  }
  const q=(src,dst,pay,get,lots,gold,extra={})=>store.quote(id,{id:`${src}-${dst}${extra.suffix??''}`,src,dst,pay,get,lots,gold,
    seen:end-120,online:true,confirmed:true,...extra});
  if (game===1) {
    for (const [asset,buy,sell,qty] of [['divine',187,185,1],['exalted',151,148,10],['ancient',130,128,10],['annul',64,62,10],
      ['thread',76,75,10],['ember',25,24,10],['fusing',19,18,100],['vaal',65,64,100],['mirror',157500,156000,1]]) {
      q('chaos',asset,buy,qty,1600,35); q(asset,'chaos',qty,sell,1000,45);
    }
    q('divine','exalted',1,13,100,175); q('divine','thread',1,26,65,190); q('exalted','ancient',5,6,68,45);
    q('annul','ember',5,13,140,45);
  } else {
    q('chaos','divine',77,1,500,45); q('divine','chaos',1,75,500,40);
    q('chaos','exalted',5,6,4000,12); q('exalted','chaos',6,5,4000,12);
    q('divine','exalted',1,98,140,90); q('divine','omen',1,18,180,80); q('omen','chaos',5,23,650,45);
  }
  q('divine','chaos',1,999,10,3,{suffix:'-outlier'});
  q('chaos','divine',1,1,90,5,{suffix:'-offline',online:false});
  q('divine','chaos',1,1,12,40,{suffix:'-expired',seen:end-3*HOUR});
  const add=(hours,side,a,qty,price='0',g=0)=>{
    const t=end-hours*HOUR;
    store.sql('INSERT INTO trades(w,id,t,side,asset,qty,price,gold,tax,note) VALUES (?,?,?,?,?,?,?,?,?,?)')
      .run(id,`sample-${side}-${a}-${hours}`,t,side,a,String(qty),String(money(price)),String(g),String(money('12')),'Sample session');
  };
  add(180,'deposit','chaos',12000); add(180,'deposit','gold',1250000);
  add(136,'buy','divine',28,game===1?'145':'59',1400); add(78,'buy','exalted',45,game===1?'12.3':'0.79',225);
  add(36,'buy',game===1?'thread':'omen',36,game===1?'6.85':'4.2',160);
  add(14,'buy','divine',8,game===1?'168':'67',400); add(2,'sell','divine',9,game===1?'185.5':'74',450);
  add(1,'sell','exalted',12,game===1?'14.6':'0.85',65);
  store.event(id,end-1560,'data','Hourly digest indexed',`${Object.keys(prices).length} asset histories refreshed in the sample market.`);
  const m=store.marks(id),rows=store.market(id,store.assets(id),m),mover=rows.filter(x=>x.change!==null).sort((a,b)=>b.change-a.change)[0];
  store.event(id,end-945,'momentum','Momentum detected',`${mover.name} moved +${(mover.change/100).toFixed(2)}% over the last 24 hours.`);
  store.event(id,end-360,'filter','Liquidity filter applied',`${store.book(id,m).rejected.length} quotes excluded from route evaluation.`);
  store.set(`quotes:${id}`,end);
  store.sql('INSERT INTO demand VALUES (?,?,?,?,?,?,?)').run(id,game===1?'astrolabe':'omen','Sample endgame builds','120','89',15,end-1800);
}
export function seed(store) {
  if (store.get('demo-version',null)) {
    if(store.get('demo-version',0)===1)store.tx(()=>{
      for(const w of store.worlds().filter(w=>w.source==='demo'))store.sql('INSERT OR IGNORE INTO demand VALUES (?,?,?,?,?,?,?)').run(w.id,w.game===1?'astrolabe':'omen','Sample endgame builds','120','89',15,w.asof-1800);
      store.set('demo-version',2);
    });
    return;
  }
  const end=Math.floor(now()/HOUR)*HOUR;
  store.tx(()=>{
    sampleWorld(store,'demo-1',1,'Covens of Umbra',end,90);
    sampleWorld(store,'demo-2',2,'Dawn of the Hunt',end,90);
    sampleWorld(store,'demo-1-history',1,'Settlers',end-240*DAY,104,true);
    sampleWorld(store,'demo-2-history',2,'Early Access',end-210*DAY,104,true);
    store.set('demo-version',2);
  });
  for (const w of store.worlds().filter(w=>w.source==='demo')) store.evaluate(w.id,store.scanning(w.id),store.marks(w.id));
}
