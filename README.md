VEEB V37.8: DEPLOYMENT AND LIVE CHECK

This is a tested code repair, not a claim that YouTube has accepted requests from
Render. The supplied logs fail during acquisition before FFmpeg starts. They do
not prove that every current yt-dlp acquisition path or every Render IP is blocked.

1. RESOLVER FIRST

Extract veeb-resolver-v37.8.zip. Open the extracted veeb-resolver-v37.8 folder.
Upload its CONTENTS to the ROOT of your existing resolver GitHub repository.
Dockerfile must sit beside veeb_resolver.py, not inside another nested folder.

Files to replace/add at repository root:
  Dockerfile                 replace
  requirements.txt           replace
  render.yaml                replace
  veeb_resolver.py           replace
  extract_source.py          NEW, required
  media_jobs.py              NEW, required
  tests/test_pipeline.py     NEW, required (keep the tests folder)
  README.md                  replace
  RECOVERY-NOTES.txt         replace

Commit the upload. Wait for Render to finish building and show Live.
The build runs the 12 offline regression tests, imports the actual FastAPI app,
and checks package dependencies. A failing check stops the new deployment.
The public resolver root / should show version v37.8-mp3-stream.

Keep your current RESOLVER_SECRET and existing cookie secret file. No new account,
proxy, secret, queue, paid plan, or R2 binding is required by this patch.
Use one Uvicorn worker, as in the supplied Dockerfile. Multiple workers/instances
would have separate in-memory job registries and could duplicate conversions.

For this baseline, REMOVE a custom YOUTUBE_SOURCE_SELECTOR if one is configured.
The default is bestaudio/best[acodec!=none]/best. Old YOUTUBE_STREAM_FORMAT and
VEEB_HEAVY_PREFETCH settings are no longer used by this version. If you previously
customized YOUTUBE_AUTH_FALLBACK_CLIENT, remove it to restore yt-dlp defaults.

2. CLOUDFLARE WORKER SECOND

Cloudflare -> Workers & Pages -> your Veeb Worker -> Edit code.
Open veeb-worker-v37.8.txt. Copy ALL its contents and replace the ENTIRE Worker
source. Save and deploy. This is not a snippet to append.

Keep these bindings/variables as currently configured:
  AUDIO_CACHE                existing R2 bucket binding
  DB                         existing D1 binding
  YOUTUBE_RESOLVER_URL        your Render resolver HTTPS URL
  YOUTUBE_RESOLVER_SECRET     must equal Render's RESOLVER_SECRET
  ADMIN_EMAIL                your admin email

The updated R2 writer requires the completed-file response from resolver V37.8.
Do not deploy the new Worker against V37.4/V37.5.

3. RUN THE ACTUAL ACQUISITION / MP3 / R2 TEST

Sign in to Veeb with the ADMIN_EMAIL account and open /admin.
Under the existing development warning control, find Playback and cache test.
Leave siRAwwaNc1M in the video-ID box initially. This is the track in your log.
Click TEST PLAYBACK. The result opens as formatted JSON. Allow up to a few minutes
if the service was asleep or jobs were already active.

Successful result:
  workerVersion includes v37.8
  health.version is v37.8-mp3-stream
  playback.ok = true
  playback.mp3Complete = true
  playback.bytes > 0
  r2Stored = true
  r2Read.status = 206
  r2Read.contentType = audio/mpeg
  r2Read.bytes = 1024
  r2Read.startsWithMp3Frame = true

The test intentionally uses the resolver even if R2 has an old copy. A recent
successful local resolver job may be reused. The test then writes/reads the MP3
through R2. A repeated diagnostic is not automatically a fresh YouTube test.

If playback.ok = false, copy the entire JSON result plus the corresponding Render
lines beginning 'v37.8 acquisition attempt failed' or 'v37.8 source-to-mp3 attempt
failed'. Those lines identify which source attempt failed, without signed URLs.

SOURCE_ACCESS_DENIED means a source attempt received a bot/login denial and none
of the attempted paths produced MP3. It does not by itself prove a permanent
Render-wide IP block. Try ONE different known-public track to distinguish an
individual-track issue from a wider acquisition failure. Do not keep cycling
player transports or formats when both fail during source acquisition.

health.ok means the server responds. It does NOT mean YouTube access works.
cookieSessionRecognized only identifies cookie fields, not a valid login.

4. PLAYBACK CHECK

Close and reopen Veeb after deployment. Play the tested song. Its next request
should be served through R2. Then play a different uncached song. If two devices
play the same cold song, Render should show one shared conversion job.
The browser's MSE transport still identifies its transport implementation as
37.3. That is intentional; check the new workerVersion and resolver version for
this release. The service-worker shell cache has been incremented to v376.

The development warning toggle remains at /admin and works as before.

WHAT CHANGED

- Clean anonymous yt-dlp defaults first, mweb+PO provider second, cookies last.
  Anonymous and mweb attempts do not inherit account cookies or hand-written
  visitor/player values. HLS/DASH manifests are no longer excluded.
- A selected source must produce MP3 bytes through FFmpeg. If startup fails,
  the next acquisition path is tried within a bounded startup window.
- Killable extraction processes replace uncancellable yt-dlp threads. There is
  one global extraction slot; cancellation kills/reaps the process group.
- One per-track job owns acquisition and progressive conversion. Independent
  listeners and cache fill read the same temporary MP3 with separate offsets.
- At most 8 retained jobs, 2 concurrent transcodes, 80 MiB per track and 256 MiB
  combined temporary audio. Completed jobs have a 15-minute idle TTL. Failed jobs
  have a short cooldown. A job is capped at 240 seconds of processing time.
- Speculative extraction is deferred. New background cache jobs are deferred
  while another job is active; cache fill can always join its own existing song.
- R2 receives a finite MP3 only after FFmpeg exits successfully. MP3 frame checks,
  byte limits and a duration sanity check reject invalid/truncated output.
- The Worker checks completion metadata and exact stored byte count. Existing
  v2 objects without this verification marker are ignored and can be rebuilt.
  Old MP4s/previous cache objects are not bulk-deleted.
- R2 lookup uses the bucket itself, rather than requiring the SQL status to say
  cached. Normal cache promotion still follows the existing popularity/favourite
  policy. This patch does not permanently save every first-time play.
- Source errors reach browser diagnostics with a specific code.

VALIDATION AND LIMITS

Passed locally: 12 Python regression tests using real FFmpeg with synthetic audio
served over local HTTP, plus 7 Worker checks. These cover shared readers, process
cancellation, decode fallback, finite cache responses, broken/short/oversized
output, MP3 parsing, cache rejection, error forwarding and admin authorization.
Worker source and embedded browser scripts passed JavaScript parsing.

The local Python tests isolate the framework layer because FastAPI/yt-dlp were
not available and dependency downloads were blocked in this workspace. The
Docker build includes a real app import and dependency check, but that Docker
build has not been run here. No live YouTube, Render, browser playback, D1 or R2
end-to-end success is claimed. The admin test is the required live verification.

This patch cannot make YouTube grant source access. If the maintained source
paths still fail from the service, the remaining work is acquisition/access or
a different permitted media source. More MP3/MSE patches will not fix a denial.

Reference guidance consulted:
https://github.com/yt-dlp/yt-dlp/wiki/Extractors
https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide
https://github.com/yt-dlp/yt-dlp/wiki/EJS
https://github.com/Brainicism/bgutil-ytdlp-pot-provider
