# What the older resolver explains

The uploaded September 8 ZIP contains a V36.16.6-era implementation, although
its root endpoint still identifies itself as v36.15-youtubejs. File naming and
its historical README are not measurements of which route actually won.

## Why it could start quickly

The old foreground path sent a direct authenticated mweb player request, used a
warm YouTube.js helper for deciphering and cached token/session information,
then passed source bytes through HTTP. Its configured default was itag 18,
an MP4 containing audio and video. Its stream route did not perform MP3 encoding.
Its fallback reused already-loaded yt-dlp engines. Source acquisition, warm
state and passthrough all differ from the current validated-MP3 pipeline.

This explains how a successful route could start in a few seconds. The supplied
archive contains no historical latency series or route-success logs, so it does
not identify which route delivered the reported 2-3 second starts.

## Defects found in the older implementation

1. `YtdlpEnginePool.resolve` returns an engine to its queue in `finally` after
   awaiting `asyncio.to_thread`. Cancelling the async waiter does not stop the
   thread. The cold-race code cancels its losing task, so a later request can
   reuse a still-running YoutubeDL instance. An offline reproduction using the
   actual historical method confirmed two calls executing on the same engine
   concurrently after cancellation. This is a demonstrated code defect and a
   plausible contributor to deterioration, not a proven production timeline.
2. `get_writable_cookie_file` copies the secret only if the runtime file is
   absent. Replacing the secret while the process lives does not refresh that
   copy. Direct requests also cache a cookie-header string independently of the
   cookie jars updated by the reusable yt-dlp engines.
3. A cold track can launch both the custom direct path and yt-dlp fallback after
   a 1.5-second head start. Foreground promotion of a prefetch starts additional
   work without cancelling the original prefetch. These can increase contention
   and request volume when successful fast paths deteriorate.
4. Direct requests use a fixed mweb client version, cached identities, custom
   token binding and edits to signed URL query parameters. The older README
   itself describes unresolved 403 diagnostics. Restoring all of this would
   restore those dependencies as well as its warm state.

V39.1 already replaced the unsafe engine sharing with owned child processes,
tracks secret-file changes and validates complete MP3 bytes before cache use.
Those protections remain in V39.2.

## A V39.1 scheduling regression fixed in V39.2

V39.1 holds the sole extraction semaphore around the entire child download.
Although a WebM can start playing progressively, its remaining download holds
up extraction of a second track. That delay can consume the second track's
20-second startup budget. This is particularly relevant to switching songs
while the old job continues filling the cache.

V39.2 retains one extraction slot, releasing it once the downloader has written
at least 8 KiB into its own source file. A separate download limit remains held
until child cleanup, with two slots by default. Another track can therefore
extract while the first downloads. Cancellation still kills and reaps owned
processes; incomplete downloads still cannot become completed MP3s.

An integration test launches actual child processes, holds the first download
open, and verifies that the second child completes before releasing the first.
Additional tests cover capacity limits, queued cancellation and rejecting a
progress marker without owned bytes.

## Source order fixed in V39.2

The older resolver supplied cookies to its first mweb attempt. V39.1 instead
tries anonymous mweb before authenticated mweb. On a route requiring a session,
that adds a failed request before the potentially successful path.

V39.2 tries authenticated mweb first when a usable cookie file is present and
has not been rejected by the session diagnostic. It keeps anonymous and default
fallbacks. When the diagnostic explicitly reports logged out, it skips the
cookie-based attempts and explains the cookie repair. A changed secret resets
that negative session state. This does not claim that cookie fields prove login.
Health now separates cookie auth fields from actual session recognition.

## What the newly supplied live report establishes

- Worker: `10.10.3-v38.3-r2-first`. The V39.1 browser startup fixes are not deployed
  in this captured report.
- Resolver: `v39.1-mp3-stream`, no successful source acquisition recorded.
- Cookie session: YouTube explicitly reports logged out (`false`), HTTP 200.
  The two expired cookie rows alone do not identify the cause of that result.
- Local encoder self-test succeeds, producing 48,483 valid MP3 bytes.
- All four source attempts fail during extraction, before source selection.
- Resolver health responds in 0.26 seconds; the failed diagnostic takes 10.24
  seconds including its checks. This particular request is not evidence of a
  minute-long hosting wake-up delay. It is also not a successful startup test.

A fresh session and the correct Worker deployment are required next actions.
The code changes remove identifiable delays and hazards. They cannot manufacture
source access or establish a universal 1-2 second playback guarantee.

## Validation boundary

81 resolver tests passed locally, including real FFmpeg and a real yt-dlp HTTP
fixture. The fixture's first MP3 bytes arrived before source EOF. It does not
exercise live YouTube, Render, Cloudflare latency or a real mobile audio element.
Use DEPLOY.txt for exact placement, session repair and the live acceptance check.
