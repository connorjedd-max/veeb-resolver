# Veeb Render Resolver V37 - canonical live MP3

This build keeps the proven YouTube acquisition source format at itag 18, but it no longer exposes or stores that MP4 container as Veeb audio.

## V37 changes

1. Cold resolution is sequential. The direct MWEB + POT + YouTube.js path gets a strict short budget, then it is cancelled before yt-dlp fallback starts. This removes the previous heavy race on small Render instances.
2. `/stream/:videoId` progressively transcodes the resolved source to `audio/mpeg` with FFmpeg. Veeb receives MP3 bytes while the source is still downloading.
3. The resolver proves at least 4 KB of MP3 output before returning a successful live stream. If startup fails, it refreshes the source once.
4. Live MP3 intentionally does not advertise byte-range support. Completed R2 MP3 objects handle real `206` byte ranges.
5. Concurrent MP3 work is capped. By default two total transcodes may run and at most one cache-fill transcode may occupy those slots.

## Keep this setting

Leave `YOUTUBE_STREAM_FORMAT` unset. V37.4 no longer forces format 18. Acquisition now prefers audio-only sources (`140`, then `251`, with yt-dlp `bestaudio[ext=m4a]/bestaudio/best`) and FFmpeg still emits canonical MP3.

## Optional environment variables

- `VEEB_MP3_BITRATE_KBPS=128`
- `VEEB_MP3_MAX_CONCURRENT=2`
- `VEEB_MP3_FIRST_BYTE_TIMEOUT=8`
- `VEEB_MP3_STARTUP_MIN_BYTES=4096`
- `VEEB_V37_DIRECT_BUDGET=3.0`

## Validation

- `python3 -m py_compile veeb_resolver.py`
- `node --check veeb_innertube_helper.mjs`

A cold stream should return `Content-Type: audio/mpeg` and `X-Veeb-Transcode: ffmpeg-mp3-live-v1`.
