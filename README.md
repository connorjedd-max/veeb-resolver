# Veeb Render Resolver V14

V14 combines the parts that actually worked:

- V12 proved yt-dlp stdout can send real bytes end-to-end.
- V13 proved the local bgutil HTTP PO-token provider is running and generating tokens.
- V14 removes V13's 30-second startup timeout.

## Playback behavior

Render starts yt-dlp and waits for the first real audio bytes.
By default there is NO resolver-side startup timeout.

Once the first bytes arrive, Render returns HTTP 200 and streams the rest.

## Existing Render settings to keep

- `RESOLVER_SECRET`
- Secret file: `youtube-cookies.txt`

No Cloudflare changes are required.

## Expected /health

- `service`: `veeb-youtube-resolver-v14`
- `poTokenHttpServerReady`: true
- `cookieFilePresent`: true
- `writableCookieFilePresent`: true
- `streamTransport`: `yt-dlp-stdout-wait-for-bytes`
- `streamFormat`: `251`
- `streamStartTimeoutSeconds`: 0
- `rangeSeeking`: false

## Expected successful logs

- `yt-dlp stream start ... "potHttpReady": true`
- PO token generation logs
- `yt-dlp first audio bytes ... "bytes": <non-zero>`
- HTTP `GET /stream/<id>` 200
- `yt-dlp stream body finished ... "bytesSent": <non-zero>`

Seeking is still intentionally disabled until continuous playback is stable.
