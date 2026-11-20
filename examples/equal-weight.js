// Rebalance on a six-bar cadence. Returned values are desired whole-unit positions.
export default function equalWeight({i,equity,assets}) {
  if(i%6||equity<=0n)return {};
  const entries=Object.entries(assets);
  return Object.fromEntries(entries.map(([id,a])=>[id,equity*95n/(100n*BigInt(entries.length)*a.close)]));
}
