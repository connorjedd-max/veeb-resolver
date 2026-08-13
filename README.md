# Veeb Render Resolver V16

V16 removes the failed format-251 attempt entirely.

The logs proved format 18 is the working path on this Render setup, so V16 goes
straight to format 18 and avoids wasting ~20-25 seconds trying format 251 first.

## Playback path

Veeb -> Cloudflare Worker -> Render -> yt-dlp format 18 -> stdout -> Veeb

## Keep existing Render settings

- `RESOLVER_SECRET`
- Secret file: `youtube-cookies.txt`

No Cloudflare changes are required.

## Expected /health

- `service`: `veeb-youtube-resolver-v16`
- `poTokenHttpServerReady`: true
- `streamFormat`: `18`
- `streamTransport`: `yt-dlp-stdout-direct-format18`
- `streamStartTimeoutSeconds`: 0

## Expected successful logs

- `yt-dlp stream start ... "format":"18"`
- `Generating POT ...`
- `yt-dlp first media bytes ... "format":"18","bytes":<non-zero>`
- `GET /stream/<id> 200 OK`
- `yt-dlp stream body finished ... "bytesSent":<non-zero>`

Seeking remains disabled until playback startup and track transitions are stable.
