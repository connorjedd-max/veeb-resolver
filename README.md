# Veeb Render Resolver V22

V22 is a foreground-priority latency build.

Changes from V21:
- removes the Android race because current yt-dlp does not support account cookies on Android and the Render IP is bot-challenged
- selected playback hard-preempts unrelated speculative prefetch work
- subprocess cancellation kills the entire yt-dlp + ffmpeg process group
- uses mweb only for foreground playback
- explicitly sets YouTube playback_wait=0 while mweb ad playback context is enabled
- skips HLS and DASH manifest discovery because Veeb only requests progressive format 18
- logs any yt-dlp sleep/wait line so remaining server-imposed waits are visible
- keeps completed local cache + byte ranges

If zero playback wait causes media-origin failures, set YOUTUBE_PLAYBACK_WAIT=6 in Render.
Keep the same RESOLVER_SECRET and youtube-cookies.txt.
