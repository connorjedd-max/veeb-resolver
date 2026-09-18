# Veeb Resolver v40.7 - September YouTube compatibility

## Why

The v40.5, v40.5.1 and v40.6 bundles all pinned yt-dlp nightly 2026.08.30 and all shared the same mweb-first source stack. In mid-September YouTube changed playback behavior for mweb/web_embedded sessions. yt-dlp shipped a web_embedded compatibility fix on 2026-09-16.

## Changes

- update yt-dlp nightly from 2026.08.30.232658 to 2026.09.16.232951
- add an explicit cookie-free web_embedded source route after authenticated mweb
- use an audio-first, low-bandwidth muxed fallback selector for web_embedded
- preserve v40.6 foreground source-acquisition preemption
- preserve mweb POT, cookie handling, FFmpeg, R2 completion and concurrency settings
- include compact per-route failure codes in /stream failures so a future upstream break is visible immediately

## Expected route order

With a usable cookie session:
1. authenticated mweb
2. web_embedded (cookie-free)
3. mweb + PO token (cookie-free)
4. default anonymous
5. authenticated default fallback

Without a usable cookie session:
1. web_embedded (cookie-free)
2. mweb + PO token (cookie-free)
3. default anonymous
