import { createHash } from 'node:crypto';

// Display metadata only. Unknown GGG base item IDs remain distinct, including new league items.
export const catalog = {
  chaos: ['Chaos Orb','CHAOS','Currency','CurrencyRerollRare'],
  divine: ['Divine Orb','DIV','Currency','CurrencyModValues'],
  exalted: ['Exalted Orb','EX','Currency','CurrencyAddModToRare'],
  ancient: ['Ancient Orb','ANC','Currency','CurrencyRerollUnique'],
  annul: ['Orb of Annulment','ANN','Currency','CurrencyRemoveMod'],
  vaal: ['Vaal Orb','VAAL','Currency','CurrencyVaal'],
  fusing: ['Orb of Fusing','FUSE','Currency','CurrencyRerollSocketLinks'],
  mirror: ['Mirror of Kalandra','MIR','Currency','CurrencyDuplicate'],
  alchemy: ['Orb of Alchemy','ALCH','Currency','CurrencyUpgradeToRare'],
  thread: ['Coven Thread','THR','Commodity',null],
  ember: ['Umbra Ember','EMB','Commodity',null],
  viscera: ['Viscera','VISC','Commodity',null],
  astrolabe: ['Astrolabe','AST','Craft',null],
  omen: ['Omen of Amelioration','OMEN','Commodity',null],
  catalyst: ['Prismatic Catalyst','CAT','Commodity',null],
  doctor: ['The Doctor','DOC','Card',null],
  patient: ['The Patient','PAT','Card',null],
  nurse: ['The Nurse','NUR','Card',null],
  essence: ['Essence of Greed','ESS','Commodity',null]
};
export const hash = s => createHash('sha256').update(s).digest('hex');
const byBase = new Map(Object.entries(catalog).filter(([,v]) => v[3]).map(([k,v]) => [v[3], k]));
const extra={CurrencyHinekorasLock:["Hinekora's Lock",'LOCK'],CurrencyRerollRareVeiled:['Veiled Exalted Orb','VEX'],CurrencyRerollRareVeiledChaos:['Veiled Chaos Orb','VCO']};
export function itemId(base) {
  const tail = base.split('/').at(-1);
  return (base===tail||base===`Metadata/Items/Currency/${tail}`?byBase.get(tail):null) ?? (Object.hasOwn(catalog, base) ? base : `item-${hash(base).slice(0,20)}`);
}
export function describe(id, base = id) {
  const c = Object.hasOwn(catalog, id) ? catalog[id] : null;
  const tail=base.split('/').at(-1),alias=Object.hasOwn(extra,tail)?extra[tail]:null;
  const name = alias?.[0]??tail.replace(/([a-z0-9])([A-Z])/g, '$1 $2').replace(/^(Thread|Astrolabe) (.+)$/,'$2 $1').slice(0,150);
  return { id, name: c?.[0] ?? name, symbol: c?.[1] ?? alias?.[1]??name.slice(0,8).toUpperCase(), kind: c?.[2] ?? 'Commodity',
    icon: c?.[3] ? `/items/${id}.png` : null };
}
