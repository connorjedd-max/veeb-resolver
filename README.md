# Veeb Render Resolver V36.16.6 - STS / player pin parity test

This build is deliberately conservative. It keeps the complete V36.16.5 resolver,
yt-dlp fallback, bgutil, cookies, itag 18, Render startup and YouTube.js helper.
It does **not** delete the fallback ladder.

## What changed

1. The direct MWEB `/player` request now includes the `signatureTimestamp` (STS)
   reported by the exact YouTube.js player helper that will decipher the URL.
2. The same helper `playerId` is explicitly sent back to `/decipher`, pinning both
   sides to the same player build.
3. The resolver verifies that the helper actually returned the requested player ID
   and STS. A mismatch becomes an explicit diagnostic instead of a mysterious GVS 403.
4. `adPlaybackContext: {pyv: true}` was removed from the direct MWEB request.
5. Google Video probe/proxy headers now use the same MWEB iPad/Safari User-Agent as
   the `/player` request, rather than yt-dlp's unrelated desktop process UA.
6. Startup logs now include `youtubejsSignatureTimestamp`.

## Why

V36.16.5 requested a cipher without STS and later deciphered it using an independently
loaded YouTube.js player. Current yt-dlp pins the player API request to the active
player's STS. If those player builds disagree, a URL can look structurally correct and
still be rejected by GVS with HTTP 403.

## Expected log on a useful test track

Look for the same values on both lines:

```
v36.15 mweb player candidate ready {...
  "playerId":"...",
  "signatureTimestamp":2067x
}

v36.15 YouTube.js decipher success {...
  "playerId":"...",
  "signatureTimestamp":2067x,
  "requestedPlayerId":"...",
  "requestedSignatureTimestamp":2067x
}
```

Then the decisive line is either:

```
v36.15 media probe success
v36.16.6 direct mweb resolve success
```

or another direct GVS 403.

If it still returns 403 with the player ID and STS matching, keep this build and inspect
the remaining GVS POT/session-binding difference. Do not remove yt-dlp yet.

## Validation

- `python3 -m py_compile veeb_resolver.py`
- `node --check veeb_innertube_helper.mjs`
