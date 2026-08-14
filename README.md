# Veeb Render Resolver V28

V28 changes the playback architecture from download/remux/cache to resolve/proxy.

## Hot path

1. yt-dlp resolves the selected YouTube format to a signed Google Video media URL.
2. The URL, required request headers and its expiry are cached in memory.
3. `/stream/:videoId` forwards the browser's HTTP Range request directly to that media URL.
4. Render streams the upstream bytes straight back to Veeb. There is no ffmpeg process, whole-song download or audio temp file.

## Prefetch

`POST /prefetch/:videoId` resolves and caches only the signed media URL. It does not download the song.

## Required secrets / variables

Keep the same:

- `RESOLVER_SECRET`
- `/etc/secrets/youtube-cookies.txt` if you currently use cookies

The existing bgutil PO-token provider remains installed.

## Optional variables

- `YOUTUBE_STREAM_FORMAT=18`
- `YOUTUBE_CLIENTS=mweb,android_vr,web_embedded`
- `VEEB_RESOLVED_URL_TTL=1800`
- `VEEB_RESOLVED_URL_EXPIRY_MARGIN=120`
- `VEEB_RESOLVE_TIMEOUT=45`

## Important

Deploy the matching V28 Cloudflare Worker as well. V28 forwards Range headers end-to-end and intentionally bypasses Cloudflare's full-object audio cache.
