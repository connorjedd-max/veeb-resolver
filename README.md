# Veeb resolver V40.2

Start with **DEPLOY.txt**. V40.2 is a conservative reliability/observability
release built directly on the V40.1 priority-aware resolver.

The V40.1 acquisition architecture is intentionally preserved: authenticated
mweb priority, progressive WebM-to-MP3, shared jobs, finite MP3 validation,
owned-process cancellation, foreground reservation, background preemption,
source cooldowns and R2 completion rules are unchanged.

The focus of V40.2 is the playback requirement: an uncached track should become
audible reliably within roughly **2-3 seconds**. Before tuning the critical path,
V40.2 makes every startup measurable as queue, source acquisition, encoder
startup and total resolver startup time. It also gives every stream a resolver
job ID and exposes a protected `/job/{jobId}` diagnostic so a mid-stream failure
can be traced to an explicit resolver state.

V40.2 does **not** increase the 4096-byte MP3 startup threshold and does not
change source fallback order. This avoids trading away startup speed or
reintroducing older source-routing failures before production timing data shows
that such a change is justified.

New terminal classifications include `MP3_STARTUP_TIMEOUT`,
`MP3_STARTUP_EMPTY`, `MP3_OUTPUT_STALLED`, `FFMPEG_FAILED`,
`SOURCE_VIDEO_UNAVAILABLE` and `SOURCE_REGION_RESTRICTED`.

The existing V40.1 Worker remains compatible. A later Worker update can consume
the new response headers and `/job/{jobId}` endpoint for end-to-end playback
recovery and diagnostics.

The Docker build installs dependencies and runs the complete test suite before
starting the service.
