import http from 'node:http';
import vm from 'node:vm';
import { Player, UniversalCache, Platform } from 'youtubei.js';

const HOST = process.env.VEEB_YOUTUBEJS_HOST || '127.0.0.1';
const PORT = Number.parseInt(process.env.VEEB_YOUTUBEJS_PORT || '4417', 10);
const PLAYER_TTL_MS = Number.parseInt(process.env.VEEB_YOUTUBEJS_PLAYER_TTL_MS || '900000', 10);
const REQUEST_TIMEOUT_MS = Number.parseInt(process.env.VEEB_YOUTUBEJS_FETCH_TIMEOUT_MS || '8000', 10);
const CACHE_DIR = process.env.VEEB_YOUTUBEJS_CACHE_DIR || '/tmp/veeb-youtubejs-cache';
const DEFAULT_MWEB_CLIENT_NAME = process.env.VEEB_MWEB_CLIENT_NAME || 'MWEB';
const DEFAULT_MWEB_CLIENT_VERSION = process.env.VEEB_MWEB_CLIENT_VERSION || '2.20260708.05.00';

const cache = new UniversalCache(true, CACHE_DIR);

// YouTube.js 17.2.0 intentionally ships without a Node JavaScript evaluator.
// Veeb's working V36.15 runtime supplied one with node:vm so Player.decipher()
// can execute the extracted signature/n transformation program.
function nodeVmEvaluate(data, env = {}) {
  const source = typeof data?.output === 'string' ? data.output : '';
  if (!source)
    throw new Error('YouTube.js evaluator received no player script');

  const sandbox = {
    ...env,
    URL,
    URLSearchParams,
    TextEncoder,
    TextDecoder,
    encodeURIComponent,
    decodeURIComponent,
    atob,
    btoa,
  };

  return vm.runInNewContext(
    `(function () {\n${source}\n})()`,
    sandbox,
    {
      timeout: 1500,
      displayErrors: true,
    },
  );
}

Platform.shim.eval = nodeVmEvaluate;
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

// Do not rebuild Google Video URLs with URLSearchParams. These URLs are signed
// and normalising/re-encoding unrelated query parameters can invalidate them.
// Replace only the client fields that YouTube.js may stamp from its bundled
// client table so the GVS request matches the MWEB /player request that created
// the format in the first place.
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

function restampMwebMediaUrl(value, clientName, clientVersion) {
  const beforeClient = rawQueryParam(value, 'c');
  const beforeVersion = rawQueryParam(value, 'cver');
  let next = String(value || '');
  if (clientName) next = replaceRawQueryParam(next, 'c', clientName);
  if (clientVersion) next = replaceRawQueryParam(next, 'cver', clientVersion);
  return {
    url: next,
    beforeClient,
    beforeVersion,
    client: rawQueryParam(next, 'c'),
    clientVersion: rawQueryParam(next, 'cver'),
    changed: next !== String(value || ''),
  };
}

function buildDecipherResult(result, base, started, retried, clientName, clientVersion, videoId) {
  const stamped = restampMwebMediaUrl(result, clientName, clientVersion);
  if (stamped.changed) {
    console.log(JSON.stringify({
      event: 'youtubejs-mweb-url-restamped',
      videoId,
      previousClient: stamped.beforeClient,
      previousClientVersion: stamped.beforeVersion,
      client: stamped.client,
      clientVersion: stamped.clientVersion,
    }));
  }
  return {
    url: stamped.url,
    playerId: base.player_id,
    signatureTimestamp: base.signature_timestamp,
    elapsedMs: Math.round(performance.now() - started),
    retried,
    client: stamped.client,
    clientVersion: stamped.clientVersion,
    previousClient: stamped.beforeClient,
    previousClientVersion: stamped.beforeVersion,
    cverRestamped: stamped.changed,
  };
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
  const clientName = typeof body.clientName === 'string' && body.clientName ? body.clientName : DEFAULT_MWEB_CLIENT_NAME;
  const clientVersion = typeof body.clientVersion === 'string' && body.clientVersion ? body.clientVersion : DEFAULT_MWEB_CLIENT_VERSION;

  if (!url && !signatureCipher && !cipher)
    throw new Error('No URL or signature cipher was supplied');

  const started = performance.now();
  let base = await loadPlayer({ playerId: requestedPlayerId });
  try {
    const result = await requestPlayer(base, poToken).decipher(url, signatureCipher, cipher);
    return buildDecipherResult(result, base, started, false, clientName, clientVersion, videoId);
  } catch (firstError) {
    console.warn(JSON.stringify({
      event: 'youtubejs-decipher-refresh',
      videoId,
      playerId: base.player_id,
      error: safeError(firstError),
    }));
    base = await loadPlayer({ force: true });
    const result = await requestPlayer(base, poToken).decipher(url, signatureCipher, cipher);
    return buildDecipherResult(result, base, started, true, clientName, clientVersion, videoId);
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
        lastError,
        mwebClientName: DEFAULT_MWEB_CLIENT_NAME,
        mwebClientVersion: DEFAULT_MWEB_CLIENT_VERSION,
      });
      return;
    }

    if (req.method === 'GET' && req.url === '/selftest') {
      const evaluation = nodeVmEvaluate({ output: "const value = 'veeb'; return value.split('').reverse().join('');" });
      if (evaluation !== 'beev') throw new Error('node:vm evaluator self-test failed');
      const stamped = restampMwebMediaUrl(
        'https://example.googlevideo.com/videoplayback?c=MWEB&cver=2.20260205.04.01&n=test',
        DEFAULT_MWEB_CLIENT_NAME,
        DEFAULT_MWEB_CLIENT_VERSION,
      );
      if (stamped.clientVersion !== DEFAULT_MWEB_CLIENT_VERSION)
        throw new Error('MWEB cver restamp self-test failed');
      const readyPlayer = await loadPlayer();
      sendJson(res, 200, {
        ok: true,
        evaluator: 'node:vm',
        cverRestamp: true,
        playerId: readyPlayer.player_id,
        signatureTimestamp: readyPlayer.signature_timestamp,
        mwebClientName: DEFAULT_MWEB_CLIENT_NAME,
        mwebClientVersion: DEFAULT_MWEB_CLIENT_VERSION,
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
  console.log(JSON.stringify({ event: 'youtubejs-helper-listening', host: HOST, port: PORT, evaluator: 'node:vm' }));
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
