# Veeb Render Resolver V36.16.1 - direct repair, stable startup

This build keeps the V36.16 direct mweb/YouTube.js repair, but restores the Docker and Render startup files byte-for-byte from the known-booting V36.15 recovered build.

## Why
The first V36.16 package exited before Uvicorn because the bgutil POT sidecar did not become ready. The direct resolver patch does not require Docker-level VEEB_MWEB_CLIENT_VERSION configuration: `veeb_resolver.py` supplies the MWEB client version in each `/decipher` request, and the helper has the same value as its default.

## Playback changes retained from V36.16
- Node `vm` YouTube.js evaluator retained.
- MWEB media URLs have only `c` and `cver` re-stamped after YouTube.js decipher.
- MWEB client version defaults to `2.20260708.05.00`.
- Cold resolution gives mweb + POT + YouTube.js a 1.5 second head start, then starts yt-dlp fallback.
- Dead generic-direct race is removed from the foreground cold path.
- Prefetch uses the real mweb + POT + YouTube.js path.
- YouTube.js helper exposes `/selftest`.

## Startup/container files
These are byte-identical to the recovered V36.15 package:
- `Dockerfile`
- `render.yaml`
- `package.json`
- `requirements.txt`

No Worker change is required.
