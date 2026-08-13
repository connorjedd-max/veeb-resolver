# Veeb Render Resolver V12

V12 keeps the existing Render + yt-dlp + mweb + bgutil + cookies setup, but removes
the custom second-stage HTTP downloader.

Playback flow:

Veeb -> Cloudflare Worker -> Render -> yt-dlp -> stdout -> Render -> Veeb

yt-dlp now owns both extraction and media downloading so its YouTube request state
stays together.

## Keep these existing Render settings

- `RESOLVER_SECRET`
- Secret file: `youtube-cookies.txt`

## Health check

`/health` should report:

- `service`: `veeb-youtube-resolver-v12`
- `cookieFilePresent`: true
- `writableCookieFilePresent`: true
- `streamTransport`: `yt-dlp-stdout`
- `rangeSeeking`: false

## Important

V12 intentionally disables byte-range seeking for the first playback proof.
Play, pause, next and normal continuous playback are the target. Seeking can be
added after stdout playback is proven.

Do not commit `youtube-cookies.txt` or Python `.pyc` files.
