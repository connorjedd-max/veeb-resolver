# Veeb Render Resolver V36.15. YouTube.js decipher path

Deploy this **entire folder as a Docker service**. Do not copy only `veeb_resolver.py`.

## What changed

V36.15 keeps the existing front-facing endpoints unchanged, but replaces the failing per-track yt-dlp EJS decipher attempt with a persistent loopback-only YouTube.js player helper.

It also fixes four concrete V36.14 defects:

- `intent=1` was still routed into the cheap speculative resolver instead of the real cold resolver.
- the direct path referenced missing helper functions, so it could not finish even after cipher/POT work succeeded.
- the PO-token task could outlive a failed player request and waste the only cold-start slot.
- a rejected experimental media URL could re-enter the same experimental path instead of going directly to the proven fallback.

Cold playback path:

1. Direct authenticated mweb Innertube `/player` returns itag 18.
2. The persistent YouTube.js `Player` deciphers `signatureCipher` and `n` from its cached player program.
3. bgutil generates the video-bound GVS PO token in parallel.
4. The resolver appends the token without re-encoding the signed URL, probes one byte, then proxies playback.
5. Full yt-dlp remains the fallback.

The backend interface is unchanged:

- `POST /prefetch/{videoId}`
- `POST /prefetch/{videoId}?intent=1`
- `GET /stream/{videoId}`
- `GET /health`

`intent=1` now starts the real foreground-grade Innertube resolver instead of the cheap speculative probe that always failed.

## Required files

- `Dockerfile`
- `package.json`
- `requirements.txt`
- `veeb_innertube_helper.mjs`
- `veeb_resolver.py`
- `render.yaml`

## Render

Use Docker runtime and keep the existing environment variables/secrets, especially:

- `RESOLVER_SECRET`
- `YOUTUBE_COOKIE_FILE=/etc/secrets/youtube-cookies.txt` if already configured

Recommended settings:

```text
VEEB_HEAVY_PREFETCH=false
VEEB_V36_DIRECT_HEAD_START=3.5
VEEB_DIRECT_MWEB_POT_TIMEOUT=15
```

The Docker image binds both internal helpers to loopback only:

```text
127.0.0.1:4416  bgutil PO-token provider
127.0.0.1:4417  persistent YouTube.js decipher helper
0.0.0.0:10000  public FastAPI service
```

## Expected startup logs

```text
youtubejs-helper-listening
youtubejs-player-ready
POT server ready on loopback before app startup
v36.15 YouTube.js helper ready
v36.15 bgutil integrity warm
v36.15 resolver stack warm ... "youtubejsReady": true
```

## Expected unseen-track logs

```text
v36.15 mweb player candidate ready ... "formatId": "18"
v36.15 YouTube.js decipher success
v36.15 POT ready
v36.15 direct mweb resolve success
```

The successful resolver path should be:

```text
mweb-innertube-youtubejs-v36.15
```

## Important Render limitation

Render free instances can take 50 seconds or more to wake after inactivity. That platform wake delay happens before the resolver can run. V36.15 warms YouTube.js and bgutil during service startup so the first track after the instance is live does not also pay player extraction and BotGuard initialization.
