# Veeb Render Resolver V17

V17 keeps the reliable YouTube format-18 path, but removes the video track before
sending the response to Veeb.

Pipeline:

YouTube format 18 -> yt-dlp -> ffmpeg -> AAC-only fragmented MP4 -> Veeb

ffmpeg uses `-c:a copy`, so the AAC audio is not re-encoded. It only strips H.264
video and repackages the AAC stream as fragmented MP4.

This is intended to make mobile Chrome treat Veeb as genuine audio playback,
which works better with Media Session and screen-off/background playback than
feeding a muxed video/mp4 resource into an audio element.

Keep existing Render settings:

- RESOLVER_SECRET
- Secret file: youtube-cookies.txt

Expected /health:

- service: veeb-youtube-resolver-v17
- poTokenHttpServerReady: true
- sourceFormat: 18
- streamContentType: audio/mp4
- streamTransport: yt-dlp-format18-ffmpeg-audio-only
- audioCodec: aac-copy
