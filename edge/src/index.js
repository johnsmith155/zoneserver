/**
 * ZoneVPN edge — two small jobs on Cloudflare's network.
 *
 * 1. FIELD REPORTS. The collector tests every server from one datacenter in
 *    Iran; users are on mobile networks that filter differently, so "works
 *    from the VPS" and "works on a phone" disagree often enough to decide the
 *    list's quality. The app reports what each connect attempt and background
 *    check actually saw, and the collector folds it into the order it
 *    publishes. Reports arrive through the user's tunnel, so this endpoint
 *    being filtered in Iran does not matter, and the address it sees is the VPN
 *    server's, never the user's.
 *
 *    Only aggregates are kept: per node, per hour, per network type, mobile
 *    operator and origin country — counters and a latency sum. No user
 *    identifier exists anywhere in the request, and nothing per-request is
 *    stored.
 *
 * 2. LIST MIRROR. The collector also writes the signed server list here, and
 *    anyone may read it. The signature is what makes a mirror safe: the app
 *    verifies it, so a mirror can go stale or dark but cannot lie.
 *
 * Routes:
 *   POST /v1/r            app -> field reports (batch)
 *   GET  /v1/s?hours=6    collector -> aggregates      Bearer READ_KEY
 *   GET  /v1/l            anyone -> signed list
 *   PUT  /v1/l            collector -> signed list     Bearer PUBLISH_KEY
 *   GET  /privacy         anyone -> the privacy policy page Play links to
 */

// Generated from the app's legal_documents.dart by edge/build_privacy.py, so
// the page and the in-app policy are the same text. Wrangler imports .html as
// a string.
import PRIVACY_HTML from './privacy.html';

const OUTCOMES = {
  ok: 'ok',   // connect verified: real traffic crossed the tunnel
  hs: 'hs',   // the tunnel never came up
  vf: 'vf',   // came up, carried nothing
  'p+': 'pok', // background check through a throwaway core carried
  'p-': 'pfail', // background check failed
};

const MAX_REPORTS = 60;
const MAX_BODY = 16 * 1024;
const KEEP_HOURS = 72;

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    try {
      if (url.pathname === '/v1/r' && request.method === 'POST') {
        return await ingest(request, env, ctx);
      }
      if (url.pathname === '/v1/s' && request.method === 'GET') {
        if (!(await authorized(request, env.READ_KEY))) return deny();
        return await summary(url, env);
      }
      if (url.pathname === '/v1/l' && request.method === 'GET') {
        return await readList(request, env, ctx);
      }
      if ((url.pathname === '/privacy' || url.pathname === '/privacy/') &&
          (request.method === 'GET' || request.method === 'HEAD')) {
        return privacyPage(request);
      }
      if (url.pathname === '/v1/l' && request.method === 'PUT') {
        if (!(await authorized(request, env.PUBLISH_KEY))) return deny();
        return await writeList(request, env);
      }
    } catch (err) {
      return json({ error: 'internal' }, 500);
    }
    return new Response('not found', { status: 404 });
  },

  // Old buckets are removed here rather than on the write path, so a report
  // never pays for housekeeping.
  async scheduled(event, env) {
    const cutoff = hourBucket(new Date(Date.now() - KEEP_HOURS * 3600 * 1000));
    await env.DB.prepare('DELETE FROM node_hour WHERE bucket < ?').bind(cutoff).run();
  },
};

// ── Field reports ────────────────────────────────────────────────────────────

async function ingest(request, env) {
  const text = await request.text();
  if (text.length > MAX_BODY) return json({ error: 'too large' }, 413);
  let body;
  try {
    body = JSON.parse(text);
  } catch {
    return json({ error: 'bad json' }, 400);
  }

  const net = ['cell', 'wifi', 'other'].includes(body.net) ? body.net : 'other';
  const op = typeof body.op === 'string' && /^\d{5,6}$/.test(body.op) ? body.op : '';
  const cc = typeof body.cc === 'string' && /^[A-Z]{2}$/.test(body.cc) ? body.cc : '';
  const reports = Array.isArray(body.r) ? body.r.slice(0, MAX_REPORTS) : [];
  if (!reports.length) return json({ stored: 0 });

  // Group the batch first: one row write per distinct key, not per report.
  const bucket = hourBucket(new Date());
  const rows = new Map();
  for (const r of reports) {
    if (!r || typeof r.n !== 'string' || !/^[a-z0-9]{3,12}$/.test(r.n)) continue;
    const column = OUTCOMES[r.o];
    if (!column) continue;
    const key = r.n;
    const row = rows.get(key) || { ok: 0, hs: 0, vf: 0, pok: 0, pfail: 0, ms: 0, msn: 0 };
    row[column] += 1;
    const ms = Number.isInteger(r.ms) ? r.ms : -1;
    if ((column === 'ok' || column === 'pok') && ms > 0 && ms < 60000) {
      row.ms += ms;
      row.msn += 1;
    }
    rows.set(key, row);
  }
  if (!rows.size) return json({ stored: 0 });

  const stmt = env.DB.prepare(
    `INSERT INTO node_hour (bucket, node, net, op, cc, ok, hs, vf, pok, pfail, ms_sum, ms_n)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)
     ON CONFLICT (bucket, node, net, op, cc) DO UPDATE SET
       ok = ok + excluded.ok, hs = hs + excluded.hs, vf = vf + excluded.vf,
       pok = pok + excluded.pok, pfail = pfail + excluded.pfail,
       ms_sum = ms_sum + excluded.ms_sum, ms_n = ms_n + excluded.ms_n`);
  const batch = [];
  for (const [node, r] of rows) {
    batch.push(stmt.bind(bucket, node, net, op, cc, r.ok, r.hs, r.vf, r.pok, r.pfail, r.ms, r.msn));
  }
  await env.DB.batch(batch);
  return json({ stored: batch.length });
}

async function summary(url, env) {
  const hours = clamp(parseInt(url.searchParams.get('hours') || '6', 10), 1, KEEP_HOURS);
  const cc = /^[A-Z]{2}$/.test(url.searchParams.get('cc') || '') ? url.searchParams.get('cc') : 'IR';
  const since = hourBucket(new Date(Date.now() - hours * 3600 * 1000));
  const { results } = await env.DB.prepare(
    `SELECT node, net,
            SUM(ok) AS ok, SUM(hs) AS hs, SUM(vf) AS vf,
            SUM(pok) AS pok, SUM(pfail) AS pfail,
            SUM(ms_sum) AS ms_sum, SUM(ms_n) AS ms_n
     FROM node_hour WHERE bucket >= ?1 AND cc = ?2
     GROUP BY node, net`).bind(since, cc).all();
  return json({ since, cc, hours, nodes: results });
}

// ── List mirror ──────────────────────────────────────────────────────────────

async function readList(request, env, ctx) {
  const cache = caches.default;
  const cacheKey = new Request(new URL('/v1/l', request.url).toString(), { method: 'GET' });
  const hit = await cache.match(cacheKey);
  if (hit) return hit;
  const body = await env.LIST.get('list');
  if (!body) return new Response('not yet', { status: 404 });
  const response = new Response(body, {
    headers: {
      'content-type': 'text/plain; charset=utf-8',
      // Short: the collector republishes every few minutes, and a mirror that
      // serves a stale list is only worth having if it is not very stale.
      'cache-control': 'public, max-age=60',
    },
  });
  ctx.waitUntil(cache.put(cacheKey, response.clone()));
  return response;
}

async function writeList(request, env) {
  const body = await request.text();
  if (!body || body.length > 2 * 1024 * 1024) return json({ error: 'bad size' }, 400);
  await env.LIST.put('list', body);
  return json({ stored: body.length });
}

// ── Privacy policy ───────────────────────────────────────────────────────────

function privacyPage(request) {
  return new Response(request.method === 'HEAD' ? null : PRIVACY_HTML, {
    headers: {
      'content-type': 'text/html; charset=utf-8',
      'cache-control': 'public, max-age=3600',
      'x-content-type-options': 'nosniff',
    },
  });
}

// ── Helpers ──────────────────────────────────────────────────────────────────

async function authorized(request, secret) {
  if (!secret) return false;
  const header = request.headers.get('authorization') || '';
  const given = header.startsWith('Bearer ') ? header.slice(7) : '';
  const a = new TextEncoder().encode(given);
  const b = new TextEncoder().encode(secret);
  if (a.byteLength !== b.byteLength) return false;
  return crypto.subtle.timingSafeEqual(a, b);
}

function deny() {
  return json({ error: 'unauthorized' }, 401);
}

function json(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function hourBucket(date) {
  return date.toISOString().slice(0, 13); // YYYY-MM-DDTHH, UTC
}

function clamp(value, lo, hi) {
  return Number.isFinite(value) ? Math.min(hi, Math.max(lo, value)) : lo;
}
