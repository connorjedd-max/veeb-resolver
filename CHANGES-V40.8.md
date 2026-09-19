# Veeb Resolver v40.8 - mweb format-18 fallback

The v40.7 diagnostics exposed two separate failures on Render:

- authenticated `mweb` frequently reaches YouTube but returns `SOURCE_FORMAT_UNAVAILABLE`
- anonymous `web_embedded` is frequently challenged with `Sign in to confirm you're not a bot`

This matches the September 2026 YouTube/yt-dlp behaviour where some mweb sessions expose only progressive format 18 while datacenter IPs may still be challenged on anonymous clients.

## Changes

- mweb no longer trusts a stale `YOUTUBE_SOURCE_SELECTOR` override. Its route-local selector is now:
  `251/140/18/bestaudio[ext=webm]/bestaudio/best[acodec!=none]/best`
- format 18 is therefore an explicit first-class fallback for every mweb attempt
- fallback order is now authenticated mweb -> cookie-free mweb+POT -> web_embedded -> anonymous
- background R2 source acquisition gets a 24 second resolver-side startup budget instead of 20 seconds, while foreground playback remains at 20 seconds
- `/health` reports the actual mweb selector and separate foreground/background startup deadlines
- yt-dlp stays on the Sep-16 nightly introduced by v40.7

The Render Worker still has its own background request deadline, so the resolver does not extend background startup beyond 24 seconds.
