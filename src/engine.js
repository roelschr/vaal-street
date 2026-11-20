import { U, BP, DAY, bps, ceil, check, decimal, feeValue, max, min, money, positive, small, sqrt, uint } from './fixed.js';

// A settlement is evidence of a price, never of executable depth. Evidence and offers
// must be from the same world. Reference ratios are compared by cross multiplication.
export function screen(quotes, marks, pairs, now, ttl = 3600, tolerance = 1200) {
  const good = [], rejected = [];
  for (const q of quotes) {
    let reason;
    const pay = BigInt(q.pay), get = BigInt(q.get);
    const p = pairs.get(`${q.src}|${q.dst}`);
    const a = marks.get(q.src), b = marks.get(q.dst);
    const recent = x => x && now - x.t <= 24 * 3600 && x.t <= now;
    const ref = recent(p) ? [BigInt(p.va), BigInt(p.vb)] :
      recent(a) && recent(b) ? [BigInt(b.c), BigInt(a.c)] : null;
    if (!q.online) reason = q.id.startsWith('ps-')?'Availability unconfirmed':'Seller offline';
    else if (!q.confirmed) reason = 'Unconfirmed depth';
    else if (now - q.seen > ttl || q.seen > now + 60) reason = 'Stale snapshot';
    else if (!ref) reason = 'No recent settlement';
    else {
      const x = get * ref[0], y = pay * ref[1];
      if ((x > y ? x - y : y - x) * BP > y * BigInt(tolerance)) reason = 'Outside reference band';
    }
    if (reason) rejected.push({ ...q, reason });
    else good.push({ ...q, pay, get, lots: BigInt(q.lots), gold: BigInt(q.gold) });
  }
  return { good, rejected };
}

function sizeRoute(path, bankroll, reserve, tax) {
  // Feasibility is monotone in the first-leg lot count (including zero fills).
  function fill(n, detail = false) {
    const start = n * path[0].pay;
    let amount = start, gold = 0n;
    const steps = [];
    for (const e of path) {
      const lots = amount / e.pay;
      gold += lots * e.gold;
      if (lots > e.lots || gold > reserve) return null;
      const out = lots * e.get;
      if (detail) steps.push({ from: e.src, to: e.dst, paid: lots * e.pay, received: out,
        remainder: amount % e.pay, lots, gold: lots * e.gold, pay: e.pay, get: e.get, quote: e.id });
      amount = out;
    }
    const net = (amount - start) * U - feeValue(gold, tax);
    return { start, out: amount, gold, net, steps };
  }
  let lo = 0n, hi = min(bankroll / path[0].pay, path[0].lots);
  while (lo < hi) {
    const mid = (lo + hi + 1n) / 2n;
    if (fill(mid)) lo = mid; else hi = mid - 1n;
  }
  let best, nBest;
  // At lot boundaries the largest feasible route need not be the most profitable.
  for (let n = lo, end = max(1n, lo - 31n); n >= end; n--) {
    const v = fill(n);
    if (v && v.net > 0n && (!best || v.net > best.net)) { best = v; nBest = n; }
  }
  if (!best) return null;
  const v = fill(nBest, true);
  return { path: [path[0].src, ...path.map(x => x.dst)], start: v.start, output: v.out,
    gross: decimal((v.out - v.start) * U), net: decimal(v.net), netRaw: v.net, gold: v.gold,
    yieldBps: bps(v.net, v.start * U), efficiency: v.gold ? decimal(v.net * U / v.gold) : null, steps: v.steps };
}
export function scan(edges, bankroll, gold, tax, legs = 4, budget = 30000) {
  const start = performance.now(), graph = new Map(), found = new Map();
  for (const e of edges) { if (!graph.has(e.src)) graph.set(e.src, []); graph.get(e.src).push(e); }
  let visited = 0;
  const seen = new Set(['chaos']), path = [];
  function walk(src, num, den) {
    for (const e of graph.get(src) ?? []) {
      if (visited >= budget) return;
      visited++;
      const n = num * e.get, d = den * e.pay;
      path.push(e);
      if (e.dst === 'chaos' && path.length > 1 && n > d) {
        const r = sizeRoute(path, bankroll, gold, tax);
        if (r) { const k = r.path.join('|'); if (!found.has(k) || found.get(k).netRaw < r.netRaw) found.set(k, r); }
      } else if (!seen.has(e.dst) && path.length < legs) {
        seen.add(e.dst); walk(e.dst, n, d); seen.delete(e.dst);
      }
      path.pop();
    }
  }
  if (bankroll > 0n) walk('chaos', 1n, 1n);
  return { routes: [...found.values()].sort((a, b) => a.netRaw > b.netRaw ? -1 : a.netRaw < b.netRaw ? 1 : 0)
    .map(({ netRaw, ...r }) => r), visited, truncated: visited >= budget, ms: +(performance.now() - start).toFixed(3) };
}

// Allocation conserves every micro-chaos: the final unit inherits the residual basis.
export function account(trades, method = 'FIFO') {
  let cash = 0n, gold = 0n, realized = 0n, burned = 0n, turnover = 0n;
  const positions = new Map(), entries = [];
  for (const t of trades) {
    const q = BigInt(t.qty), px = BigInt(t.price), fee = BigInt(t.gold), value = q * px;
    const feeC = feeValue(fee, BigInt(t.tax));
    if (!positions.has(t.asset)) positions.set(t.asset, { qty: 0n, cost: 0n, lots: [], last: t.t });
    const p = positions.get(t.asset);
    let pnl = null;
    if (t.side === 'deposit') {
      if (t.asset === 'gold') gold += q; else cash += q * U;
    } else {
      check(gold >= fee, 'Insufficient Gold in this ledger. Record a Gold deposit first.');
      gold -= fee; burned += fee; p.last = t.t;
      if (t.side === 'buy' || t.side === 'opening') {
        if (t.side === 'buy') { check(cash >= value, 'Insufficient Chaos balance. Record a deposit first.'); cash -= value; turnover += value; }
        p.qty += q; p.cost += value + feeC;
        p.lots.push({ qty: q, cost: value + feeC, t: t.t });
      } else {
        check(t.side === 'sell' && p.qty >= q, 'Cannot sell more units than the ledger holds.');
        let allocated = method === 'WAC' ? p.cost * q / p.qty : 0n, left = q;
        while (left > 0n) {
          const lot = p.lots[0], take = min(left, lot.qty), basis = lot.cost * take / lot.qty;
          if (method === 'FIFO') allocated += basis;
          lot.qty -= take; lot.cost -= basis; left -= take;
          if (lot.qty === 0n) p.lots.shift();
        }
        p.qty -= q; p.cost -= allocated; cash += value; turnover += value;
        pnl = value - allocated - feeC; realized += pnl;
      }
    }
    entries.push({ ...t, price: decimal(px), pnl: pnl === null ? null : decimal(pnl) });
  }
  return { cash, gold, realized, burned, turnover, positions, entries };
}

export function executionModel(opts={}) {
  const participation=BigInt(small(opts.participation??300,1,10000,'Participation')),slip=BigInt(small(opts.slip??12,0,2000,'Slippage'));
  const impact=BigInt(small(opts.impact??100,0,2000,'Depth impact')),decay=BigInt(small(opts.decay??3,0,200,'Decay'));
  const perUnit=uint(opts.fee??'35','Gold per unit'),tax=money(opts.tax??'12');
  return ({open,volume,lag,buy,cash,gold,qty,limit=null})=>{
    const depth=volume*participation/BP,base=slip+decay*BigInt(lag);
    const worst=ceil(open*(BP+base+impact),BP),funded=buy?cash/worst:qty,wanted=limit===null?funded:min(limit,funded);
    let size=min(wanted,depth);if(perUnit)size=min(size,gold/perUnit);if(!size)return null;
    const friction=base+impact*size/max(1n,depth),price=buy?ceil(open*(BP+friction),BP):max(1n,open*max(1n,BP-friction)/BP);
    const spent=perUnit*size;return {size,price,spent,fee:feeValue(spent,tax),value:price*size,partial:size<(limit??wanted)};
  };
}
export function replay(bars, opts = {}, customStrategy) {
  const started = performance.now();
  check(bars.length >= 2, 'At least two historical bars are needed for replay.');
  const initial = money(opts.capital ?? '5000', 'Starting capital'); check(initial > 0n, 'Starting capital must be positive.');
  const initialGold = uint(opts.gold ?? '1000000', 'Starting Gold'), execute=executionModel(opts);
  const window = small(opts.window ?? 18, 2, 720, 'Lookback');
  const band = BigInt(small(opts.band ?? 35, 0, 10000, 'Signal band'));
  const delay = small(opts.delay ?? 1, 1, 72, 'Execution lag');
  const strategy = opts.strategy ?? 'reversion';
  check(customStrategy || ['momentum','reversion','hold'].includes(strategy), 'Unknown strategy.');
  let cash = initial, gold = initialGold, qty = 0n, basis = 0n, pnl = 0n, turnover = 0n, feesPaid = 0n;
  let peak = initial, dd = 0n, lockAt = null, lock = 0, lastEq = initial, partials = 0, sum = 0n, target = 0;
  let ret = 0n, squares = 0n, downside = 0n, fills = 0;
  const signals = [], closes = [], trades = [], curve = [], stride = Math.max(1, Math.ceil(bars.length / 180));
  for (let i = 0; i < bars.length; i++) {
    const b = bars[i], open = BigInt(b.o), close = BigInt(b.c);
    if (i >= delay) {
      // Decide at an earlier close; size against previously observable traded volume.
      const buy = signals[i - delay] === 1;
      const volume = BigInt(bars[i - 1].v);
      const fill=execute({open,volume,lag:Math.max(delay,Math.floor((b.t-bars[i-delay].t)/3600)),buy,cash,gold,qty});
      if (fill) {
        const {size,price,value,spent,fee}=fill;
        gold -= spent; feesPaid += fee; turnover += value; fills++; if (fill.partial) partials++;
        if (buy) { cash -= value; basis += value + fee; qty += size; lockAt ??= b.t; }
        else {
          const cost = basis * size / qty; pnl += value - fee - cost; basis -= cost;
          cash += value; qty -= size;
          if (!qty && lockAt !== null) { lock = Math.max(lock, b.t - lockAt); lockAt = null; }
        }
        trades.push({ t: b.t, side: buy ? 'buy' : 'sell', qty: String(size), price: decimal(price), gold: String(spent) });
      }
    }
    const eq = cash + qty * close - feesPaid;
    peak = max(peak, eq); dd = max(dd, (peak - eq) * BP / peak);
    if (i && lastEq > 0n) {
      const r = (eq - lastEq) * U / lastEq; ret += r; squares += r * r; if (r < 0n) downside += r * r;
    }
    lastEq = eq;
    if (i % stride === 0 || i === bars.length - 1) curve.push({ t: b.t, value: decimal(eq) });
    sum += close; closes.push(close); if (i >= window) sum -= closes[i - window];
    if (customStrategy) {
      target = customStrategy({ i, t: b.t, close, open, volume: BigInt(b.v), cash, qty, gold,
        previousClose: n => Number.isInteger(n) && n >= 0 && n <= i ? closes[i - n] : null }) ? 1 : 0;
    } else if (strategy === 'hold') target = 1;
    else if (i + 1 >= window) {
      const rel = (close * BigInt(window) - sum) * BP / sum;
      if (rel > band) target = strategy === 'momentum' ? 1 : 0;
      if (rel < -band) target = strategy === 'momentum' ? 0 : 1;
    }
    signals.push(target);
  }
  if (lockAt !== null) lock = Math.max(lock, bars.at(-1).t - lockAt);
  const n = BigInt(bars.length - 1), sd = sqrt(max(0n, n * squares - ret * ret)), ds = sqrt(n * downside);
  const burn = initialGold - gold, total = lastEq - initial;
  return { finalEquity: decimal(lastEq), realized: decimal(pnl), profit: decimal(total), returnBps: bps(total, initial),
    drawdownBps: Number(dd), sharpe: sd ? decimal(ret * U / sd) : null, sortino: ds ? decimal(ret * U / ds) : null,
    turnover: decimal(turnover * U / initial), lockHours: decimal(BigInt(lock) * U / 3600n),
    efficiency: burn ? decimal(total * U / burn) : null, goldBurn: String(burn), goldLeft: String(gold),
    unitsHeld: String(qty), fills, partials, bars: bars.length, curve, trades: trades.slice(-50).reverse(),
    ms: +(performance.now() - started).toFixed(3) };
}

export function craft(o) {
  const current = money(o.settle), high = money(o.success), low = money(o.fallback), cost = money(o.cost), base = money(o.base);
  const p = BigInt(small(o.chance, 0, 10000, 'Success probability')), demand = BigInt(small(o.demand, 0, 10000, 'Sale probability'));
  const sellTime = positive(o.sellTime, 'Settlement minutes'), rollTime = positive(o.rollTime, 'Reroll minutes');
  const settleEv = current * demand / BP - base;
  const rollEv = (high * p + low * (BP - p)) * demand / (BP * BP) - base - cost;
  const settleHour = settleEv * 60n / sellTime, rollHour = rollEv * 60n / (rollTime + sellTime);
  return { settle: decimal(settleEv), roll: decimal(rollEv), settleHour: decimal(settleHour),
    rollHour: decimal(rollHour), best: settleHour >= rollHour ? 'Settle' : 'Reroll' };
}
export function transform(o) {
  const cost = positive(o.inputQty, 'Input quantity') * money(o.inputPrice), output = positive(o.outputQty, 'Output quantity') * money(o.outputPrice);
  const fee = feeValue(uint(o.gold, 'Gold cost'), money(o.tax)), profit = output - cost - fee;
  return { cost: decimal(cost), output: decimal(output), net: decimal(profit), yieldBps: bps(profit, cost),
    hour: decimal(profit * 60n / positive(o.minutes, 'Minutes per batch')) };
}
