VEEB V37.9 RESOLVER

Purpose
-------
V37.8 proved that mweb + GVS POT can resolve a real YouTube audio source (itag 251)
but the subsequent Googlevideo request can still return HTTP 403 before FFmpeg gets
any bytes.

V37.9 changes only the resolver acquisition layer:

1. Adds an explicit visionOS client before Android/mweb.
2. visionOS and Android skip the ordinary YouTube watch webpage. This avoids letting
   the Render bot-check webpage block player-API clients that can operate without it.
3. Keeps Android format 18 fallback and mweb + bgutil 1.3.2 GVS POT.
4. If yt-dlp resolves a signed source but FFmpeg's direct HTTP request fails, V37.9
   retries that SAME client with an yt-dlp-owned source download. yt-dlp therefore
   owns the HTTP request/redirect/request-handler details. FFmpeg then reads the
   downloaded local source file and produces the canonical MP3.
5. Temporary source downloads are removed after conversion.
6. Signed Googlevideo URL/query data is more aggressively redacted from diagnostics.
7. R2 remains MP3-only. The Cloudflare Worker does not need to change for this patch.

Deployment
----------
Extract the zip and upload the extracted folder CONTENTS to the GitHub repository root,
replacing the current resolver files. Include tests/. Do not upload __pycache__.

Wait for Render to finish and show Live. Then use Veeb /admin -> TEST PLAYBACK.

Expected /health markers:
  version: v37.9-mp3-stream
  playerSkipWebpageClients: ["visionos", "android"]
  ytDlpOwnedDownloadFallback: true

Useful success logs:
  v37.9 source selected
  v37.9 mp3 first bytes ready

If a direct signed URL fails but yt-dlp's own downloader succeeds, you should see:
  v37.9 direct source fetch failed; trying yt-dlp owned download
  v37.9 source downloaded by yt-dlp
  v37.9 mp3 first bytes ready

The local test suite contains 14 tests and includes real FFmpeg conversion, shared-job
behaviour, local-source conversion/cleanup, and the direct-403 -> yt-dlp-download fallback.
