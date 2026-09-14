# V40.2 change boundary

## What this release changes

- Adds per-job startup telemetry without adding network calls to the playback
  critical path.
- Adds a stable 12-character job ID diagnostic contract.
- Adds a protected job-status endpoint that never exposes the resolved media URL.
- Emits structured startup logs against a configurable 3000 ms SLA.
- Converts previously generic FFmpeg startup/output failures into explicit codes.
- Classifies per-video YouTube unavailability separately from auth/route failure.
- Preserves failure job IDs on pre-start JSON responses when a job exists.

## What this release deliberately does not change

- No larger startup buffer.
- No parallel racing of yt-dlp clients.
- No source-order changes.
- No cookie/POT policy changes.
- No FFmpeg codec/bitrate changes.
- No concurrency changes.
- No automatic alternate-YouTube-ID search.
- No shorter production timeouts by default.

That boundary is intentional. V40.2 is the measurement layer for safe subsequent
latency tuning, not another resolver rewrite.
