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

// The Python resolver sends the exact client identity/version used for the
// MWEB /player request with every decipher call. Keep environment defaults only
// as a safety net so Render configuration cannot silently disable the repair.
const DEFAULT_MWEB_CLIENT_NAME = process.env.VEEB_MWEB_CLIENT_NAME || 'MWEB';
const DEFAULT_MWEB_CLIENT_VERSION = process.env.VEEB_MWEB_CLIENT_VERSION || '2.20260708.05.00';

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

function rawQueryParam(value, key) {
  const match = String(value || '').match(new RegExp('(?:[?&])' + key + '=([^&#]*)'));
  if (!match) return null;
  try { return decodeURIComponent(match[1]); } catch (_) { return match[1]; }
}

// Google Video URLs are signed. Do not rebuild them with URL/URLSearchParams,
// because normalising unrelated parameters can invalidate the signature.
// Replace only c/cver in the existing query string.
function replaceRawQueryParam(value, key, nextValue) {
  const input = String(value || '');
  const hashIndex = input.indexOf('#');
  const base = hashIndex >= 0 ? input.slice(0, hashIndex) : input;
  const fragment = hashIndex >= 0 ? input.slice(hashIndex) : '';
  const encodedKey = encodeURIComponent(key);
  const encodedValue = encodeURIComponent(String(nextValue));
  const pair = encodedKey + '=' + encodedValue;
  const pattern = new RegExp('([?&])' + encodedKey + '=[^&#]*');
  const nextBase = pattern.test(base)
    ? base.replace(pattern, (_match, prefix) => prefix + pair)
    : base + (base.includes('?') ? '&' : '?') + pair;
  return nextBase + fragment;
}

function restampClientVersion(deciphered, clientName, clientVersion) {
  const original = String(deciphered || '');
  const previousClient = rawQueryParam(original, 'c');
  const previousClientVersion = rawQueryParam(original, 'cver');
  let url = original;
  if (clientName) url = replaceRawQueryParam(url, 'c', clientName);
  if (clientVersion) url = replaceRawQueryParam(url, 'cver', clientVersion);
  return {
    url,
    previousClient,
    previousClientVersion,
    client: rawQueryParam(url, 'c'),
    clientVersion: rawQueryParam(url, 'cver'),
    changed: url !== original,
  };
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
  const clientName = typeof body.clientName === 'string' && body.clientName
    ? body.clientName
    : DEFAULT_MWEB_CLIENT_NAME;
  const clientVersion = typeof body.clientVersion === 'string' && body.clientVersion
    ? body.clientVersion
    : DEFAULT_MWEB_CLIENT_VERSION;

  if (!url && !signatureCipher && !cipher)
    throw new Error('No URL or signature cipher was supplied');

  const started = performance.now();
  let base = await loadPlayer({ playerId: requestedPlayerId });

  try {
    const result = await requestPlayer(base, poToken).decipher(url, signatureCipher, cipher);
    const stamped = restampClientVersion(result, clientName, clientVersion);
    if (stamped.changed) {
      console.log(JSON.stringify({
        event: 'youtubejs-mweb-url-restamped',
        videoId,
        previousClient: stamped.previousClient,
        previousClientVersion: stamped.previousClientVersion,
        client: stamped.client,
        clientVersion: stamped.clientVersion,
      }));
    }
    return {
      url: stamped.url,
      playerId: base.player_id,
      signatureTimestamp: base.signature_timestamp,
      elapsedMs: Math.round(performance.now() - started),
      retried: false,
      client: stamped.client,
      clientVersion: stamped.clientVersion,
      previousClient: stamped.previousClient,
      previousClientVersion: stamped.previousClientVersion,
      cverRestamped: stamped.changed,
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
    const stamped = restampClientVersion(result, clientName, clientVersion);
    if (stamped.changed) {
      console.log(JSON.stringify({
        event: 'youtubejs-mweb-url-restamped',
        videoId,
        previousClient: stamped.previousClient,
        previousClientVersion: stamped.previousClientVersion,
        client: stamped.client,
        clientVersion: stamped.clientVersion,
      }));
    }
    return {
      url: stamped.url,
      playerId: base.player_id,
      signatureTimestamp: base.signature_timestamp,
      elapsedMs: Math.round(performance.now() - started),
      retried: true,
      client: stamped.client,
      clientVersion: stamped.clientVersion,
      previousClient: stamped.previousClient,
      previousClientVersion: stamped.previousClientVersion,
      cverRestamped: stamped.changed,
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
        mwebClientName: DEFAULT_MWEB_CLIENT_NAME,
        mwebClientVersion: DEFAULT_MWEB_CLIENT_VERSION,
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
      const stamped = restampClientVersion(out, DEFAULT_MWEB_CLIENT_NAME, DEFAULT_MWEB_CLIENT_VERSION);
      const params = new URL(stamped.url).searchParams;
      const nTransformed = params.get('n') !== 'SELFTESTN';
      const sigPresent = Boolean(params.get('sig'));
      const cverCorrect = params.get('c') === DEFAULT_MWEB_CLIENT_NAME
        && params.get('cver') === DEFAULT_MWEB_CLIENT_VERSION;
      const ok = nTransformed && sigPresent && cverCorrect;
      sendJson(res, ok ? 200 : 500, {
        ok,
        playerId: base.player_id,
        nTransformed,
        sigPresent,
        cverCorrect,
        mwebClientName: params.get('c'),
        mwebClientVersion: params.get('cver'),
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
