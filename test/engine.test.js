import test from 'node:test';
import assert from 'node:assert/strict';
import { account,craft,replay,scan,screen,transform } from '../src/engine.js';
import { money,decimal,feeValue,U } from '../src/fixed.js';
import { basket } from '../src/basket.js';

test('micro-Chaos and large whole units round-trip without binary floating point',()=>{
  assert.equal(decimal(money('9007199254740993.000001')),'9007199254740993.000001');
  assert.equal(feeValue(1n,1n),1n);
  assert.throws(()=>money('1.0000001'),/six decimal/);
});
const edge=(src,dst,pay,get,lots,gold=0)=>({id:`${src}:${dst}`,src,dst,pay:BigInt(pay),get:BigInt(get),lots:BigInt(lots),gold:BigInt(gold)});
test('whole-lot route respects the downstream bottleneck, bankroll and per-lot Gold',()=>{
  const es=[edge('chaos','a',7,2,500,3),edge('a','b',3,10,50,2),edge('b','chaos',10,15,3,1)];
  const {routes}=scan(es,1000n,600n,money('10'),3);
  assert.equal(routes.length,1);const r=routes[0];
  assert.equal(r.start,35n);assert.equal(r.output,45n);assert.equal(r.gold,24n);
  assert.equal(r.steps[1].remainder,1n);assert.equal(r.net,'9.99976');
  assert.equal(r.efficiency,decimal(money('9.99976')*U/24n));
  assert.deepEqual(scan(es,6n,600n,0n,3).routes,[]);
  assert.deepEqual(scan(es,1000n,0n,0n,3).routes,[]);
  assert.equal(scan(es,1000n,600n,money('500000'),3).routes[0].start,21n);
  assert.deepEqual(scan(es,1000n,600n,money('650000'),3).routes,[]);
  assert.deepEqual(scan(es,1000n,600n,0n,2).routes,[]);
});
test('an apparently profitable rational cycle can lose money after stack flooring',()=>{
  const es=[edge('chaos','a',10,3,1),edge('a','chaos',2,7,50)];
  assert.equal(scan(es,10n,100n,0n).routes.length,0);
});
test('unverified, stale, and off-reference listings never become depth',()=>{
  const q={...edge('chaos','divine',100,1,10),online:1,confirmed:1,seen:100000};
  const m=new Map([['chaos',{t:100000,c:U}],['divine',{t:100000,c:100n*U}]]);
  const arr=[q,{...q,confirmed:0},{...q,online:0},{...q,seen:500},{...q,pay:5n}];
  const r=screen(arr,m,new Map(),100000);assert.equal(r.good.length,1);assert.deepEqual(r.rejected.map(q=>q.reason),['Unconfirmed depth','Seller offline','Stale snapshot','Outside reference band']);
  assert.equal(screen([q],new Map(),new Map(),100000).good.length,0);
  assert.equal(screen([q],new Map(),new Map([['chaos|divine',{t:100000,va:'300',vb:'3'}]]),100000).good.length,1);
});
const t=(side,qty,price,asset='x',gold=0)=>({side,qty:String(qty),price:String(money(price)),asset,gold:String(gold),tax:String(money('12')),t:1});
test('FIFO and weighted average distribute residual cost exactly over partial sales',()=>{
  const trades=[t('deposit',100,'0','chaos'),t('deposit',100,'0','gold'),t('buy',2,'1.000001','x',1),t('buy',3,'2.000002','x',2),t('sell',1,'3','x',1)];
  const a=account(trades),b=account(trades,'WAC');
  assert.equal(decimal(a.realized),'1.999981');assert.notEqual(a.realized,b.realized);
  trades.push(t('sell',4,'3','x',1));
  for(const method of ['FIFO','WAC']){
    const r=account(trades,method);assert.equal(r.positions.get('x').cost,0n);assert.equal(r.positions.get('x').qty,0n);
    assert.equal(r.realized,money('15')-money('8.000008')-feeValue(5n,money('12')));
  }
  assert.throws(()=>account([...trades,t('sell',1,'3')]),/more units/);
});
const bar=(i,price,vol=100)=>({t:3600*(i+1),o:money(price),h:money(price),l:money(price),c:money(price),v:BigInt(vol)});
const fr={strategy:'hold',capital:'100',gold:'20',fee:'1',tax:'0.000001',slip:0,impact:0,decay:0,participation:10000};
test('decisions fill only on subsequent opens, with lag, prior depth, and Gold limits',()=>{
  const bars=[bar(0,'1',2),bar(1,'10',1000),bar(2,'12')];
  const r=replay(bars,fr,({i})=>i===0);
  assert.equal(r.fills,2);assert.equal(r.partials,1);assert.equal(r.trades.at(-1).qty,'2');assert.equal(r.trades.at(-1).price,'10');
  assert.equal(r.profit,'3.999998');assert.equal(r.realized,r.profit);assert.equal(r.goldBurn,'4');
  const capped=replay(bars,{...fr,gold:'1'},()=>true);assert.equal(capped.unitsHeld,'1');assert.equal(capped.goldLeft,'0');
  const delayed=replay(bars,{...fr,delay:2},()=>true);assert.equal(delayed.trades[0].t,bars[2].t);
  const gap=replay([bar(0,'1',0),bar(1,'9',5)],fr);assert.equal(gap.fills,0);
});
test('crafting and batch comparison use sale probability, time, rounding and Gold',()=>{
  const r=craft({settle:'85',base:'40',cost:'12',success:'210',fallback:'25',chance:3000,demand:9200,sellTime:8,rollTime:3});
  assert.deepEqual(r,{settle:'38.2',roll:'22.06',settleHour:'286.5',rollHour:'120.327272',best:'Settle'});
  assert.equal(transform({inputQty:'8',inputPrice:'4.2',outputQty:'1',outputPrice:'35.7',gold:'300',tax:'12',minutes:'4'}).net,'2.0964');
});
test('basket replay shares cash and depth between assets without filling on the decision bar',()=>{
  const bars=[bar(0,'10',5),bar(1,'10',20),bar(2,'10',100),bar(3,'11',100)];
  const r=basket({a:bars,b:bars},{...fr,tax:0,fee:0},({i,assets})=>{
    assert.equal(assets.a.previousClose(i+1),null);
    return i===0?{a:20,b:20}:i===1?{a:0,b:10}:{};
  });
  assert.equal(r.trades.filter(x=>x.t===bars[0].t).length,0);
  assert.equal(r.trades.filter(x=>x.t===bars[1].t).length,2);
  assert.deepEqual(r.positions,{a:'0',b:'10'});assert.equal(r.finalEquity,'110');assert.equal(r.realized,'0');
});
