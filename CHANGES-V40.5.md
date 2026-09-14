# Veeb resolver v40.5 - authenticated mweb client-config fast path

This release makes one bounded startup optimization on top of v40.4.1.

## Behaviour change

The first authenticated `mweb` foreground route now passes yt-dlp's official
`youtube:player_skip=configs` extractor argument. This skips the separate mweb
client-config request on the primary playback path. The existing cookie-free
`fg-pot` mweb fallback deliberately does **not** skip configs, preserving the
v40.4 extraction path if the fast path is not viable for a particular video.

The existing `use_ad_playback_context=true` behaviour remains unchanged.

## Rollback

Set:

```
VEEB_YTDLP_SKIP_MWEB_CLIENT_CONFIG=false
```

to restore v40.4.1 mweb extraction without changing builds.

## Telemetry

`clientConfigMs` is now recorded when yt-dlp emits a client-config request.
`v40.5 source acquisition phases` also reports `skipClientConfig` for the route
that actually succeeded.

## Not changed

No changes to source ordering, POT provider, cookie ownership, progressive source
publication, source/download semaphores, FFmpeg, MP3 bitrate, startup byte
thresholds, R2 completion, or player behaviour.
