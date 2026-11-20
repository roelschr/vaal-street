// All money is integer micro-chaos; quantities, exchange lots, and Gold are whole units.
export const U = 1_000_000n, BP = 10_000n, HOUR = 3600, DAY = 86400;
export function check(condition, message) { if (!condition) throw new Error(message); }
export function uint(value, name = 'Quantity', max = 10n ** 24n) {
  check(typeof value !== 'number' || Number.isSafeInteger(value), `${name}: use a whole-number string for large values.`);
  check(/^\d{1,25}$/.test(String(value)), `${name} must be a nonnegative whole number.`);
  const n = BigInt(value);
  check(n <= max, `${name} exceeds the supported range.`);
  return n;
}
export function positive(value, name) {
  const n = uint(value, name); check(n > 0n, `${name} must be greater than zero.`); return n;
}
export function money(value, name = 'Price') {
  const s = String(value);
  check(/^\d{1,18}(?:\.\d{1,6})?$/.test(s), `${name} must be nonnegative, with up to six decimal places.`);
  return micros(s);
}
// Read a previously formatted internal result, which can exceed the input price limit.
export function micros(s) {
  const [i,f='']=String(s).replace(/^-/,'').split('.');
  return (BigInt(i)*U+BigInt(f.padEnd(6,'0')))*(String(s).startsWith('-')?-1n:1n);
}
export function decimal(value, scale = U) {
  const n = BigInt(value), a = n < 0n ? -n : n;
  const fraction = String(a % scale).padStart(String(scale).length - 1, '0').replace(/0+$/, '');
  return `${n < 0n ? '-' : ''}${a / scale}${fraction ? '.' + fraction : ''}`;
}
export const min = (a, b) => a < b ? a : b;
export const max = (a, b) => a > b ? a : b;
export const ceil = (a, b) => (a + b - 1n) / b;
export const feeValue = (gold, perMillion) => ceil(gold * perMillion, U);
export function bps(delta,base) {
  if(!base)return null;const b=delta*BP/base;return b>9007199254740991n||b< -9007199254740991n?String(b):Number(b);
}
export function small(v, lo, hi, name) {
  const n = Number(v); check(Number.isInteger(n) && n >= lo && n <= hi, `${name} must be between ${lo} and ${hi}.`); return n;
}
export function text(v, name, size = 160) {
  check(typeof v === 'string' && v.trim().length > 0 && v.length <= size && !/[\x00-\x1f]/.test(v), `Invalid ${name}.`);
  return v.trim();
}
export function timestamp(v) {
  const n = typeof v === 'string' && !/^\d+$/.test(v) ? Date.parse(v) / 1000 : Number(v);
  return small(n, 1, 4102444800, 'Timestamp (Unix seconds)');
}
export function sqrt(n) {
  if (n < 2n) return n;
  let x = n, y = (x + 1n) / 2n;
  while (y < x) { x = y; y = (x + n / x) / 2n; }
  return x;
}
