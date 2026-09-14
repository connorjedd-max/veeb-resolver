# Veeb resolver V40.4

## Purpose

V40.3 proved that healthy cold playback spends roughly 9.5-10.1 seconds in
source acquisition while FFmpeg startup is only about 0.17-0.23 seconds.
The 8 KiB progressive publication threshold is not the bottleneck because the
first yt-dlp progress callback and the 8 KiB marker arrive effectively together.

V40.4 makes one intentionally narrow performance change: for the `mweb`
YouTube client it enables yt-dlp's official `use_ad_playback_context=true`
extractor argument. yt-dlp documents this option as a way to skip preroll ads
and eliminate the mandatory wait period before download.

## Safety / sustainability

- No source-client ordering change.
- No cookie or PO-token provider change.
- No direct-media-URL shortcut.
- No progressive source ownership change.
- No source-slot or concurrency change.
- No FFmpeg option change.
- No MP3 startup threshold change.
- No R2/completion semantics change.

The feature has a single rollback switch:

`VEEB_YTDLP_USE_AD_PLAYBACK_CONTEXT=false`

The default in V40.4 is `true`, and `render.yaml` declares the same value.

## Telemetry

`v40.4 source acquisition phases` now includes:

- `adPlaybackContext`: whether the feature is enabled.
- `adDetectedMs`: present only when yt-dlp logs that it detected a preroll ad.

The existing source phase timings remain unchanged.

## What success looks like

Compare the same three fields from uncached healthy songs:

- `sourceAcquireMs`
- `encoderStartupMs`
- `totalStartupMs`

V40.3 baseline was about 9.5-10.1 seconds for `sourceAcquireMs`. If the
mandatory preroll wait is a major contributor, V40.4 should materially reduce
that number without changing FFmpeg startup.

## Premium-cookie caveat

yt-dlp documents that `use_ad_playback_context` should not be used when the
caller specifically needs YouTube Premium formats, because those premium
formats may be lost. Veeb's resolver selects ordinary audio/WebM for a 128 kbps
MP3 transcode rather than depending on Premium-only formats. If this account
nevertheless shows format regressions, set the rollback environment variable to
`false` immediately.
