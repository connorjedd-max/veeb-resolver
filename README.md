# Veeb Render Resolver V21

V21 targets the interaction lag visible in the V20.1 logs.

## What changed

- Foreground playback no longer waits for cancellation of unrelated speculative prefetch jobs.
- A tapped cold track bypasses the speculative prefetch semaphore and starts immediately.
- If the same track was only queued, it is promoted to foreground instead of waiting behind another track.
- Foreground cold playback races the public `android` client against the known-good authenticated `mweb` client.
- The race is parallel, not serial, so a failed Android attempt does not add a V19-style timeout penalty.
- Android is useful as a fast lane because current yt-dlp marks it as not requiring the JS player.
- A race candidate must produce 128 KiB before it can win, preventing a client from winning with only an ffmpeg MP4 header and then failing.
- Completed audio is still served from the normal V20.1 seekable cache with Range support.

## Keep existing configuration

Keep the same `RESOLVER_SECRET` and Render Secret File `youtube-cookies.txt`.
No new required environment variables.

Optional tuning:

- `YOUTUBE_FAST_CLIENT=android`
- `VEEB_FOREGROUND_READY_BYTES=131072`
- `VEEB_PREFETCH_CONCURRENCY=1`

## Healthy deployment

`/health` should report:

- `service`: `veeb-youtube-resolver-v21`
- `foregroundFastClient`: `android`
- `foregroundReliableClient`: `mweb`
- `foregroundRace`: `true`
- `streamTransport`: `foreground-race-android-vs-mweb-cache-first`

The most useful log is `foreground race winner`. If Android works from the current Render egress, it should beat mweb without a JS-player challenge. If it does not work, mweb was already running in parallel and remains the fallback.
