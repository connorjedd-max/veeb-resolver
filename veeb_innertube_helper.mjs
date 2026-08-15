import http from 'node:http';
import vm from 'node:vm';
import { Player, UniversalCache, Platform } from 'youtubei.js';

/* ============================================================
   THE FIX (v36.16)

   youtubei.js >= 15 ships NO JavaScript evaluator. Its default
   shim is literally:

     export default function evaluate() {
       throw new Error('To decipher URLs, you must provide your
                        own JavaScript evaluator. ...');
     }

   Player.decipher() calls Platform.shim.eval(data, env) at
   dist/src/core/Player.js:108. Until you replace that shim,
   EVERY decipher call throws, which is exactly the 502 in the
   Render logs. Nothing else in the v36.15 design was wrong.

   data.output is a function BODY that ends in `return process(...)`,
   so evaluation = run it and return the result.
   ============================================================ */

const evalContext = vm.createContext({
  URL,
  URLSearchParams,
  TextDecoder,
  TextEncoder,
  atob,
  btoa,
  Math,
  Date,
  JSON,
  Map,
  Set,
  Array,
  Object,
  String,
  Number,
  RegExp,
  Error,
  parseInt,
  parseFloat,
  decodeURIComponent,
  encodeURIComponent,
  decodeURI,
  encodeURI,
  isNaN,
  // Explicitly absent: process, require, fetch, globalThis, Buffer.
  // The player script has no business touching any of them.
  document: undefined,
  window: undefined,
  location: undefined,
  navigator: undefined,
});

const EVAL_TIMEOUT_MS = Number.parseInt(process.env.VEEB_YOUTUBEJS_EVAL_TIMEOUT_MS || '5000', 10);

Platform.shim.eval = (data) =>
  vm.runInContext(`(function(){${data.output}})()`, evalContext, {
    timeout: EVAL_TIMEOUT_MS,
    displayErrors: true,
  });

/* ========================================================== */

const HOST = process.env.VEEB_YOUTUBEJS_HOST || '127.0.0.1';
const PORT = Number.parseInt(process.env.VEEB_YOUTUBEJS_PORT || '4417', 10);
const PLAYER_TTL_MS = Number.parseInt(process.env.VEEB_YOUTUBEJS_PLAYER_TTL_MS || '900000', 10);
const REQUEST_TIMEOUT_MS = Number.parseInt(process.env.VEEB_YOUTUBEJS_FETCH_TIMEOUT_MS || '8000', 10);
const CACHE_DIR = process.env.VEEB_YOUTUBEJS_CACHE_DIR || '/tmp/veeb-youtubejs-cache';

// Re-stamp cver to match the client version the resolver actually used for
// its /player call. youtubei.js hardcodes its own MWEB version
// (2.20260205.04.01 in 17.2.0) and overwrites cver during decipher. A cver
// that disagrees with the /player request is a known 403 source at GVS.
const MWEB_CVER = process.env.VEEB_MWEB_CLIENT_VERSION || '';

const cache = new UniversalCache(true, CACHE_DIR);
let player = null;
let playerPromise = null;
let playerLoadedAt = 0;
let lastError = null;

function safeError(error) {
  const message = error instanceof Error ? error.message : String(error);
  return message.replace(/[\r\n]+/g, ' ').slice(-1200);
}

async function timedFetch(input, init = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

async function loadPlayer({ force = false, playerId } = {}) {
  const fresh = player && (Date.now() - playerLoadedAt) < PLAYER_TTL_MS;
  const idMatches = !playerId || player?.player_id === playerId;
  if (!force && fresh && idMatches)
    return player;

  if (!force && playerPromise)
    return playerPromise;

  playerPromise = (async () => {
    const started = performance.now();
    const next = await Player.create(cache, timedFetch, undefined, playerId);
    if (!next?.data)
      throw new Error('YouTube.js Player loaded without decipher data');
    player = next;
    playerLoadedAt = Date.now();
    lastError = null;
    console.log(JSON.stringify({
      event: 'youtubejs-player-ready',
      playerId: next.player_id,
      signatureTimestamp: next.signature_timestamp,
      elapsedMs: Math.round(performance.now() - started),
    }));
    return next;
  })();

  try {
    return await playerPromise;
  } catch (error) {
    lastError = safeError(error);
    throw error;
  } finally {
    playerPromise = null;
  }
}

function requestPlayer(base, poToken) {
  const scoped = new Player(base.player_id, base.signature_timestamp, base.data);
  if (typeof poToken === 'string' && poToken)
    scoped.po_token = poToken;
  return scoped;
}

function restampClientVersion(deciphered) {
  if (!MWEB_CVER) return deciphered;
  try {
    const url = new URL(deciphered);
    if (url.searchParams.get('c') === 'MWEB')
      url.searchParams.set('cver', MWEB_CVER);
    return url.toString();
  } catch {
    return deciphered;
  }
}

// Only a stale/rotated player is worth a forced re-download. An evaluator or
// extraction fault will fail identically on retry and just burns 1-2s plus a
// full base.js fetch — which is what the v36.15 logs were doing on every track.
function isStalePlayerError(error) {
  const message = safeError(error).toLowerCase();
  return message.includes('nsig')
    || message.includes('decipher script')
    || message.includes('invalid signature')
    || message.includes('no n/sig decipher function')
    || message.includes('invalid result from player script');
}

async function decipher(body) {
  const videoId = typeof body.videoId === 'string' ? body.videoId : '';
  const url = typeof body.url === 'string' && body.url ? body.url : undefined;
  const signatureCipher = typeof body.signatureCipher === 'string' && body.signatureCipher
    ? body.signatureCipher
    : undefined;
  const cipher = typeof body.cipher === 'string' && body.cipher ? body.cipher : undefined;
  const poToken = typeof body.poToken === 'string' && body.poToken ? body.poToken : undefined;
  const requestedPlayerId = typeof body.playerId === 'string' && body.playerId ? body.playerId : undefined;

  if (!url && !signatureCipher && !cipher)
    throw new Error('No URL or signature cipher was supplied');

  const started = performance.now();
  let base = await loadPlayer({ playerId: requestedPlayerId });

  try {
    const result = await requestPlayer(base, poToken).decipher(url, signatureCipher, cipher);
    return {
      url: restampClientVersion(result),
      playerId: base.player_id,
      signatureTimestamp: base.signature_timestamp,
      elapsedMs: Math.round(performance.now() - started),
      retried: false,
    };
  } catch (firstError) {
    if (!isStalePlayerError(firstError)) {
      console.error(JSON.stringify({
        event: 'youtubejs-decipher-failed',
        videoId,
        playerId: base.player_id,
        retryable: false,
        error: safeError(firstError),
      }));
      throw firstError;
    }

    console.warn(JSON.stringify({
      event: 'youtubejs-decipher-refresh',
      videoId,
      playerId: base.player_id,
      error: safeError(firstError),
    }));

    base = await loadPlayer({ force: true });
    const result = await requestPlayer(base, poToken).decipher(url, signatureCipher, cipher);
    return {
      url: restampClientVersion(result),
      playerId: base.player_id,
      signatureTimestamp: base.signature_timestamp,
      elapsedMs: Math.round(performance.now() - started),
      retried: true,
    };
  }
}

async function readJson(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 1024 * 1024)
      throw new Error('Request body is too large');
    chunks.push(chunk);
  }
  if (!chunks.length)
    return {};
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}

function sendJson(res, status, value) {
  const data = Buffer.from(JSON.stringify(value));
  res.writeHead(status, {
    'content-type': 'application/json',
    'content-length': String(data.length),
    'cache-control': 'no-store',
  });
  res.end(data);
}

const server = http.createServer(async (req, res) => {
  try {
    if (req.method === 'GET' && req.url === '/health') {
      const ready = Boolean(player?.data);
      sendJson(res, ready ? 200 : 503, {
        ok: ready,
        ready,
        playerId: player?.player_id || null,
        signatureTimestamp: player?.signature_timestamp || null,
        evaluator: 'node:vm',
        lastError,
      });
      return;
    }

    // Proves the evaluator is wired up without needing a real video.
    // Returns 200 only if a full sig+n round trip succeeds.
    if (req.method === 'GET' && req.url === '/selftest') {
      const base = await loadPlayer();
      const fake = 'url=' + encodeURIComponent(
        'https://r1---sn-veeb.googlevideo.com/videoplayback?n=SELFTESTN&c=MWEB'
      ) + '&s=SELFTESTSIG&sp=sig';
      const out = await requestPlayer(base).decipher(undefined, fake);
      const params = new URL(out).searchParams;
      const ok = params.get('n') !== 'SELFTESTN' && Boolean(params.get('sig'));
      sendJson(res, ok ? 200 : 500, {
        ok,
        playerId: base.player_id,
        nTransformed: params.get('n') !== 'SELFTESTN',
        sigPresent: Boolean(params.get('sig')),
      });
      return;
    }

    if (req.method === 'POST' && req.url === '/decipher') {
      const body = await readJson(req);
      const result = await decipher(body);
      sendJson(res, 200, result);
      return;
    }

    if (req.method === 'POST' && req.url === '/refresh') {
      const body = await readJson(req);
      const next = await loadPlayer({
        force: true,
        playerId: typeof body.playerId === 'string' ? body.playerId : undefined,
      });
      sendJson(res, 200, {
        ok: true,
        playerId: next.player_id,
        signatureTimestamp: next.signature_timestamp,
      });
      return;
    }

    sendJson(res, 404, { error: 'Not found' });
  } catch (error) {
    const message = safeError(error);
    lastError = message;
    console.error(JSON.stringify({ event: 'youtubejs-helper-error', error: message }));
    sendJson(res, 502, { error: message });
  }
});

server.listen(PORT, HOST, () => {
  console.log(JSON.stringify({
    event: 'youtubejs-helper-listening',
    host: HOST,
    port: PORT,
    evaluator: 'node:vm',
  }));
});

loadPlayer().catch((error) => {
  lastError = safeError(error);
  console.error(JSON.stringify({ event: 'youtubejs-player-warm-failed', error: lastError }));
  setTimeout(() => loadPlayer({ force: true }).catch(() => {}), 2000);
});

const shutdown = () => {
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(1), 3000).unref();
};
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
