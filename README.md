# Veeb resolver V39.1

This release targets first-play latency and recovery. Deploy it together with
`VEEB-WORKER-v39.1.txt`. Start with `DEPLOY.txt` for exact placement.

## Playback changes

- One yt-dlp process retains ownership of source acquisition. Direct audio-only
  WebM can feed FFmpeg while the file is still downloading. MP4 and fragmented
  sources retain the completed, seekable-file path.
- The default format selector prefers audio WebM, with the existing audio/source
  alternatives retained. MP3 startup now needs 4 KiB instead of 16 KiB.
- Source startup has a 20-second deadline. Completion still requires successful
  download, an intact MP3 frame stream and a matching duration. Temporary file EOF
  and missing fragments cannot become completed MP3 objects.
- Simultaneous playback and cache collection share the same job. `/completed`
  never launches acquisition. Jobs on Render are temporary; the Worker writes
  completed MP3s into the existing R2 namespace.
- The companion Worker fixes failed-source retry, stale recovery callbacks,
  premature startup completion on the `play` event, and forced service-worker
  reloads. Search checks cache metadata without downloading uncached results.

## What this cannot promise

A new uncached track still needs the upstream source to respond. This package
cannot guarantee YouTube source access, or 1-2 second starts during a source
refusal. Free Render instances spin down after 15 idle minutes and take about a
minute to wake. Use an always-on compute instance for the playback target.
The included `render.yaml` retains the uploaded Free baseline; it is not a claim
that Free meets the target. Review the instance plan before using that blueprint.

The optional desktop agent changes acquisition location. It currently uploads a
complete MP3 and is not the latency solution. Normal deployment uses
`VEEB_SOURCE_MODE=direct`. Optional investigation is documented separately in
`SOURCE-AGENT-OPTIONAL.txt`.

## Deployment contract

Keep one ASGI worker and one Render instance. Acquisition concurrency is one;
there are two active MP3 job slots. A busy source service returns a bounded
failure. Cached R2 playback does not consume those slots.

Existing Worker bindings remain `DB`, `AUDIO_CACHE`, `YOUTUBE_RESOLVER_URL` and
`YOUTUBE_RESOLVER_SECRET`. The resolver's `RESOLVER_SECRET` must still match the
Worker secret. Existing cookie-file settings are preserved.

| Route | Purpose |
| --- | --- |
| `GET /` | Version and liveness |
| `GET /health` | Authenticated configuration and source evidence |
| `GET /stream/{id}` | Start or join progressive/shared playback |
| `GET /resolve/{id}` | Start or join the same MP3 job |
| `GET/HEAD /completed/{id}` | Read completed bytes only |
| `POST /prepare/{id}` | Start or join a requested job |
| `GET /jobs/{id}` | Read job state without acquisition |
| `POST /diagnose/{id}` | Encoder, session and complete-MP3 diagnostic |
| `POST /import-cached-source/{id}` | Convert an existing finite source into MP3 |

The optional agent endpoints and separate agent key are retained for existing
users. No endpoint accepts arbitrary commands or arbitrary source URLs.

## Validation

See `TEST-RESULTS.txt` for commands, results and unverified production conditions.
The Docker build runs all Python tests. `worker-verification` contains the Node
regression suite for the supplied Worker. Its media/browser APIs are test doubles;
real yt-dlp HTTP downloading and FFmpeg encoding are tested separately in Python.

## References checked 9 September 2026

- [Render Free instance limitations](https://render.com/docs/free#spinning-down-on-idle).
- [yt-dlp YouTube guidance](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#youtube).
- [yt-dlp PO-token guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide).
- [The browser play event](https://developer.mozilla.org/en-US/docs/Web/API/HTMLMediaElement/play_event)
  and [playing event](https://developer.mozilla.org/en-US/docs/Web/API/HTMLMediaElement/playing_event).
