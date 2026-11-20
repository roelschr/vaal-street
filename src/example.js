import { HOUR,DAY,U,decimal } from './fixed.js';
export function example(at=Math.floor(Date.now()/1000)) {
  const hour=Math.floor(at/HOUR)*HOUR;
  const bars=[];
  for (let i=0;i<48;i++) {
    const c=178n*U+BigInt(i)*120000n;bars.push({asset:'divine',t:hour-(47-i)*HOUR,open:decimal(c-200000n),high:decimal(c+300000n),low:decimal(c-400000n),close:decimal(c),volume:String(100+i)});
  }
  return {game:1,league:'Import practice (sample)',asOf:at,leagueStart:hour-30*DAY,
    assets:[{id:'divine',name:'Divine Orb',symbol:'DIV',kind:'Currency'},{id:'exalted',name:'Exalted Orb',symbol:'EX',kind:'Currency'}],
    bars,pairs:[{a:'chaos',b:'exalted',t:hour,volumeA:'1500',volumeB:'100'}],
    quotes:[{id:'practice-buy',from:'chaos',to:'divine',pay:'184',get:'1',lots:'20',gold:'75',seen:at,online:true,confirmed:true},
      {id:'practice-sell',from:'divine',to:'chaos',pay:'1',get:'186',lots:'12',gold:'90',seen:at,online:true,confirmed:true}],
    stash:[{asset:'divine',qty:'3'},{asset:'chaos',qty:'280'}],demand:[{asset:'exalted',build:'Practice build cohort',listed:'50',sold:'32',minutes:10,seen:at}]};
}
