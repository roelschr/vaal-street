import { U,BP,bps,check,decimal,max,money,small,sqrt,uint } from './fixed.js';
import { executionModel } from './engine.js';

// Each invocation is one league and one shared bankroll. Decisions return whole-unit
// target positions by instrument; a strategy sees only the completed prefix of each series.
export function basket(series,opts={},decide=relativeStrength) {
  const start=performance.now(),initial=money(opts.capital??'5000'),initialGold=uint(opts.gold??'1000000');
  check(initial>0n,'Starting capital must be positive.');
  const execute=executionModel(opts),delay=small(opts.delay??1,1,72,'Execution lag'),window=small(opts.window??18,2,720,'Lookback');
  const band=BigInt(small(opts.band??35,0,10000,'Signal band'));
  const byTime=new Map(),positions=new Map(),pending=new Map(),curve=[],trades=[];
  for(const [id,bars] of Object.entries(series)){
    positions.set(id,{qty:0n,cost:0n,mark:0n,last:0,history:[],volume:0n,lock:null});
    for(const b of bars){if(!byTime.has(b.t))byTime.set(b.t,[]);byTime.get(b.t).push([id,b]);}
  }
  const times=[...byTime.keys()].sort((a,b)=>a-b);check(times.length>=2,'At least two historical bars are needed for replay.');
  let cash=initial,gold=initialGold,fees=0n,realized=0n,turnover=0n,peak=initial,dd=0n,prev=initial,sum=0n,sq=0n,down=0n,lock=0,fills=0,partials=0;
  const stride=Math.max(1,Math.ceil(times.length/180));
  for(const [i,t] of times.entries()){
    const current=byTime.get(t),due=[];
    for(const [id,b] of current){
      const queue=pending.get(id)??[];let target;
      while(queue.length&&queue[0].t+delay*3600<=t)target=queue.shift();
      if(!target)continue;const p=positions.get(id),wanted=target.qty-p.qty;if(!wanted)continue;
      due.push({id,b,p,buy:wanted>0n,limit:wanted>0n?wanted:-wanted,lag:Math.max(delay,(t-target.t)/3600)});
    }
    // Free cash before purchases; never borrow proceeds from another league or future bar.
    due.sort((a,b)=>Number(a.buy)-Number(b.buy)||a.id.localeCompare(b.id));
    for(const o of due){
      const {id,b,p,buy,limit}=o,f=execute({open:BigInt(b.o),volume:p.volume,lag:Math.trunc(o.lag),buy,limit,cash,gold,qty:p.qty});if(!f)continue;
      fills++;if(f.partial)partials++;fees+=f.fee;gold-=f.spent;turnover+=f.value;
      if(buy){cash-=f.value;p.cost+=f.value+f.fee;p.qty+=f.size;p.lock??=t;}
      else {const cost=p.cost*f.size/p.qty;cash+=f.value;realized+=f.value-f.fee-cost;p.cost-=cost;p.qty-=f.size;if(!p.qty){lock=Math.max(lock,t-p.lock);p.lock=null;}}
      trades.push({t,asset:id,side:buy?'buy':'sell',qty:String(f.size),price:decimal(f.price),gold:String(f.spent)});
    }
    for(const [id,b] of current){const p=positions.get(id);p.mark=BigInt(b.c);p.history.push(p.mark);p.last=t;p.volume=BigInt(b.v);}
    let equity=cash-fees;const assets=Object.create(null);
    for(const [id,p] of positions)if(p.last){
      equity+=p.qty*p.mark;const end=p.history.length-1;
      assets[id]={qty:p.qty,close:p.mark,volume:p.volume,t:p.last,previousClose:n=>Number.isInteger(n)&&n>=0&&n<=end?p.history[end-n]:null};
    }
    peak=max(peak,equity);dd=max(dd,(peak-equity)*BP/peak);
    if(i&&prev>0n){const r=(equity-prev)*U/prev;sum+=r;sq+=r*r;if(r<0n)down+=r*r;}prev=equity;
    if(i%stride===0||i===times.length-1)curve.push({t,value:decimal(equity)});
    const targets=decide({world:opts.world,i,t,cash,gold,equity,assets,window,band})??{};
    for(const [id,target] of Object.entries(targets)){
      check(Object.hasOwn(assets,id),'Strategy requested an unobserved instrument.');
      const qty=uint(target,'Target units');if(!pending.has(id))pending.set(id,[]);pending.get(id).push({t,qty});
    }
  }
  for(const p of positions.values())if(p.lock!==null)lock=Math.max(lock,times.at(-1)-p.lock);
  const n=BigInt(times.length-1),sd=sqrt(max(0n,n*sq-sum*sum)),ds=sqrt(n*down),burn=initialGold-gold,total=prev-initial;
  return {finalEquity:decimal(prev),profit:decimal(total),realized:decimal(realized),returnBps:bps(total,initial),drawdownBps:Number(dd),
    sharpe:sd?decimal(sum*U/sd):null,sortino:ds?decimal(sum*U/ds):null,turnover:decimal(turnover*U/initial),
    efficiency:burn?decimal(total*U/burn):null,goldBurn:String(burn),goldLeft:String(gold),lockHours:decimal(BigInt(lock)*U/3600n),
    positions:Object.fromEntries([...positions].map(([id,p])=>[id,String(p.qty)])),fills,partials,bars:times.length,curve,trades:trades.slice(-50).reverse(),ms:+(performance.now()-start).toFixed(3)};
}
export function relativeStrength({assets,equity,window,band,t}){
  let best=null,strength=band;const targets=Object.create(null);
  for(const [id,a] of Object.entries(assets)){
    targets[id]=0n;const prev=a.previousClose(window);if(!prev||a.t!==t)continue;
    const move=(a.close-prev)*BP/prev;if(move>strength){best=id;strength=move;}
  }
  if(best)targets[best]=max(0n,equity)*98n/(100n*assets[best].close);
  return targets;
}
