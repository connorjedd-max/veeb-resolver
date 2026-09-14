# Veeb resolver V40.3 - source acquisition phase telemetry

V40.3 is intentionally built directly on V40.2 and does not change source
selection, authenticated mweb priority, POT policy, progressive WebM ownership,
FFmpeg settings, startup byte thresholds, source/transcode concurrency, or R2
completion behaviour.

Production V40.2 logs showed healthy uncached tracks spending roughly 9.5-10.1
seconds in `sourceAcquireMs`, while FFmpeg startup took only 165-205 ms. This
release splits that source-acquisition number into observable phases before any
latency tuning is attempted.

## New source startup phases

Successful progressive source acquisition now emits:

`v40.3 source acquisition phases {...}`

Possible timing fields include:

- `sourceSlotWaitMs`: parent-side wait for source download/extraction capacity.
- `ytDlpImportMs`: time spent importing yt-dlp in the owned child process.
- `childReadyMs`: child-process time until instrumentation is ready.
- `extractInfoStartMs`: point at which yt-dlp extraction begins.
- `webpageMs`: first yt-dlp webpage phase, when emitted by yt-dlp.
- `playerApiMs`: first player API phase, when emitted.
- `potRequestMs`: first GVS POT request phase, when emitted.
- `gvsTokenReadyMs`: first retrieved GVS PO token phase, when emitted.
- `playerTokenReadyMs`: first retrieved player PO token phase, when emitted.
- `jsChallengeMs`: first JS-challenge phase, when emitted.
- `playerJsMs`: first player-JS phase, when emitted.
- `formatSelectedMs`: format-selection phase, when emitted.
- `downloaderInvokedMs`: source downloader invocation.
- `firstDownloadProgressMs`: first actual yt-dlp download progress hook.
- `source8192Ms`: point where at least 8 KiB of owned source data exists and the
  progressive source is eligible for FFmpeg.

Child phase timings are relative to the child-process run start. The source-slot
wait is a separate parent-side duration. The existing `sourceAcquireMs` remains
the end-to-end parent measurement.

Failed source attempts now also preserve `startupTiming`, so slow failures can
be diagnosed without changing fallback behaviour.

The protected `/job/{jobId}` payload now includes `sourceTiming` for successful
source acquisition.

## Why this is deliberately not an optimisation release

The current measurements prove that FFmpeg is not the main startup bottleneck,
but `sourceAcquireMs` still combines several independent operations. Optimising
before splitting those operations risks reintroducing previously fixed resolver
failures. V40.3 makes the next production sample sufficient to choose the
smallest safe performance change.
