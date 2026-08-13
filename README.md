# Veeb Render Resolver V23

V23 targets the remaining cold-play latency and V22 reliability problems.

- Uvicorn does not start until the bgutil POT HTTP server answers `/ping`.
- Speculative prefetch never queues more than one track.
- Strong user intent can replace a weak speculative prefetch.
- Young prefetches are promoted to foreground instead of trapping a click.
- Foreground races a cookie-authenticated `web_safari` HLS no-JS lane against the reliable mweb lane.
- The fast lane uses `player_skip=configs,js` and a pre-merged HLS rendition.
- mweb remains the fallback and starts in parallel, so a failed Safari lane does not add a serial timeout.
- Every concurrent yt-dlp attempt receives its own temporary cookie jar copy.
- Completed playback remains cache-first with Content-Length and byte ranges.

Keep the existing `RESOLVER_SECRET` and `/etc/secrets/youtube-cookies.txt`.
