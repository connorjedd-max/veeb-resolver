# Veeb Resolver v40.6 - Foreground Source Priority

## Why this release exists

v40.5.1 production telemetry proved that the existing reserved playback lane did
not fully protect playback startup. The resolver reserves download/transcode
capacity for foreground work, but source extraction itself is intentionally
serialized through a single extraction semaphore.

A background warm job could therefore hold that one extraction lane for several
seconds while a real playback job had otherwise-free foreground capacity.
Production examples showed foreground `sourceSlotWaitMs` of 3450 ms and a
background warm wait of 5534 ms.

## Behavioral change

Foreground playback now preempts background jobs only while those jobs are still
inside source acquisition. This includes waiting for or owning the serialized
source-extraction lane.

Once a background job has published real source bytes and ProgressiveSource has
started, it is no longer considered a source-acquisition blocker and is not
cancelled by this rule. Its source download, FFmpeg transcode and R2 completion
continue normally.

This keeps extraction concurrency at 1. It does not introduce speculative or
parallel YouTube extraction.

## Same-track promotion

If playback requests a track already being warmed, that exact job is promoted to
foreground ownership and is never preempted. Other background source-acquisition
blockers are cancelled so the promoted job can reach the serialized extraction
lane promptly.

## What is intentionally unchanged

- authenticated mweb remains the first eligible source route
- `use_ad_playback_context` remains enabled by default
- client-config skipping remains disabled by default
- Deno remains the recommended JSC runtime after the Node A/B showed no benefit
- extraction concurrency remains 1
- source publication still requires 8 KiB of owned source bytes
- MP3 startup threshold remains 4096 bytes
- MP3 remains 128 kbps by default
- FFmpeg arguments and stall timeout are unchanged
- download/transcode capacity remains 3 total / 2 background by default
- R2 completion and MP3 validation are unchanged

## New diagnostics

`/health` job stats now include:

- `backgroundSourcePreemptions`

Health also reports:

- `foregroundPreemptsBackgroundSourceAcquisition: true`

The main acceptance metric is existing `sourceSlotWaitMs`. On an uncached
foreground playback started while background warming is active, it should now be
near zero apart from cancellation cleanup/scheduling overhead.

## Rollback

Redeploy v40.5.1. No data migration or Worker change is involved.
