# Veeb Render Resolver V13

V13 fixes two issues exposed by V12:

1. It actually builds and starts bgutil's local HTTP PO-token server.
2. It prebuffers real yt-dlp audio bytes before returning HTTP 200.

It also forces YouTube audio-only itag 251 (Opus/WebM) so Veeb does not silently
fall back to muxed MP4/video format 18.

## Existing Render settings to keep

- `RESOLVER_SECRET`
- Secret file: `youtube-cookies.txt`

No Cloudflare changes are required.

## Expected /health

- `service`: `veeb-youtube-resolver-v13`
- `poTokenHttpServerReady`: true
- `cookieFilePresent`: true
- `writableCookieFilePresent`: true
- `streamTransport`: `yt-dlp-stdout-prebuffered`
- `streamFormat`: `251`
- `rangeSeeking`: false

## Expected successful playback logs

- `yt-dlp stream start ... "potHttpReady": true`
- `yt-dlp stream prebuffer ready ...`
- HTTP `GET /stream/<id>` 200
- `yt-dlp stream body finished ... "bytesSent": <non-zero>`

Seeking is still intentionally disabled until continuous playback is proven.
