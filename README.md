# Veeb Render Resolver V15

V15 is based on what the logs proved:

- Format 251 (Opus/WebM audio-only) is consistently returning HTTP 403 from YouTube's media server.
- V12 successfully pushed 6.3 MB through the full pipeline on at least one request.
- V12 was allowed to fall back to format 18 (MP4), so V15 reproduces that behavior deliberately.

## Strategy

1. Try format 251 first.
2. If yt-dlp produces no bytes / returns 403, retry the same video with format 18.
3. Stream whichever format actually produces bytes.

This keeps the working bgutil HTTP PO-token server and cookie setup.

## Existing Render settings to keep

- RESOLVER_SECRET
- Secret file: youtube-cookies.txt

No Cloudflare changes required.

## Expected health

- service: veeb-youtube-resolver-v15
- poTokenHttpServerReady: true
- primaryFormat: 251
- fallbackFormat: 18

## Success logs

You may see:

yt-dlp format failed ... "format":"251" ... 403
yt-dlp stream attempt ... "format":"18"
yt-dlp first audio bytes ... "format":"18","bytes":<non-zero>
GET /stream/<id> 200
yt-dlp stream body finished ... "bytesSent":<non-zero>

Format 18 is muxed MP4, but Veeb still never shows the YouTube video UI. This is a playback compatibility fallback.
