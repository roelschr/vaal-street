// Mirrors GGG's public Currency Exchange history to disk, one gzipped raw digest per hour.
//
//   node scripts/archive.mjs            both realms, from the first published hour to now
//   node scripts/archive.mjs poe1       PoE1 PC only        (also: poe2)
//
// Safe to stop and restart: it resumes from data/raw/<realm>/cursor.json.
// Output: data/raw/<realm>/<YYYY-MM>/<hour-start-unix>.json.gz  (bytes exactly as served)
//
// Read-only, documented endpoint: https://www.pathofexile.com/developer/docs/reference
// Env: POE_USER_AGENT (put a contact in it), ARCHIVE_DELAY_MS (default 2000), VAAL_DATA_DIR.
import { gzipSync } from 'node:zlib';
import { mkdirSync, readFileSync, writeFileSync, renameSync, existsSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';

const HOUR = 3600;
const BASE = process.env.ARCHIVE_BASE_URL ?? 'https://web.poecdn.com/api/currency-exchange';
const USER_AGENT = process.env.POE_USER_AGENT ?? 'vaal-street-archive/0.1 (personal research; local use)';
const DELAY_MS = Number(process.env.ARCHIVE_DELAY_MS ?? 2000);
const ROOT = resolve(process.env.VAAL_DATA_DIR ?? 'data', 'raw');
const REALMS = { poe1: '', poe2: '/poe2' };

function writeAtomic(path, bytes) {
  const tmp = `${path}.part`;
  writeFileSync(tmp, bytes);
  renameSync(tmp, path);
}

function retryAfterSeconds(header) {
  const n = Number(header);
  if (Number.isFinite(n) && n > 0) return n;
  const at = Date.parse(header ?? '');
  return Number.isFinite(at) ? Math.max(1, Math.ceil((at - Date.now()) / 1000)) : null;
}

async function fetchDigest(realm, id) {
  const url = `${BASE}${REALMS[realm]}${id == null ? '' : `/${id}`}`;
  for (let attempt = 0; ; attempt++) {
    let wait;
    try {
      const res = await fetch(url, { headers: { 'User-Agent': USER_AGENT }, signal: AbortSignal.timeout(30000) });
      if (res.ok) {
        const raw = await res.text();
        const next = Number(JSON.parse(raw).next_change_id);
        if (!Number.isSafeInteger(next) || next % HOUR !== 0) throw new Error('unexpected next_change_id');
        return { raw, next };
      }
      // 4xx other than 429 will not fix itself; stop instead of hammering the API.
      if (res.status >= 400 && res.status < 500 && res.status !== 429) {
        throw Object.assign(new Error(`HTTP ${res.status} for ${url}`), { fatal: true });
      }
      wait = retryAfterSeconds(res.headers.get('Retry-After')) ?? Math.min(1800, 30 * 2 ** attempt);
      console.log(`  HTTP ${res.status}; waiting ${wait}s`);
    } catch (err) {
      if (err.fatal || attempt >= 8) throw err;
      wait = Math.min(600, 10 * 2 ** attempt);
      console.log(`  ${err.message}; retrying in ${wait}s`);
    }
    await sleep(wait * 1000);
  }
}

async function archive(realm) {
  const dir = join(ROOT, realm);
  mkdirSync(dir, { recursive: true });
  const cursorPath = join(dir, 'cursor.json');
  let id = existsSync(cursorPath) ? JSON.parse(readFileSync(cursorPath, 'utf8')).next : null;
  let saved = 0;
  console.log(`[${realm}] ${id == null ? 'starting from the first published hour' : `resuming at ${new Date(id * 1000).toISOString()}`}`);
  for (;;) {
    const { raw, next } = await fetchDigest(realm, id);
    if (id != null && next === id) break; // caught up: this hour is not published yet
    const hour = id ?? next - HOUR;
    const month = join(dir, new Date(hour * 1000).toISOString().slice(0, 7));
    mkdirSync(month, { recursive: true });
    writeAtomic(join(month, `${hour}.json.gz`), gzipSync(raw));
    writeAtomic(cursorPath, JSON.stringify({ next }));
    id = next;
    if (++saved % 50 === 0) console.log(`[${realm}] ${saved} hours saved, at ${new Date(hour * 1000).toISOString()}`);
    await sleep(DELAY_MS);
  }
  console.log(`[${realm}] up to date (${saved} new hours). Next hour publishes after ${new Date((id + HOUR) * 1000).toISOString()}.`);
}

const wanted = process.argv.slice(2);
const realms = wanted.length ? wanted : Object.keys(REALMS);
for (const realm of realms) {
  if (!(realm in REALMS)) { console.error(`Unknown realm "${realm}". Use poe1 or poe2.`); process.exit(1); }
  await archive(realm);
}
