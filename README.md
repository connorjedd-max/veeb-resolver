# Veeb Render Resolver V36.14 - Innertube bootstrapless

Cold-path experiment focused only on true first-ever video IDs.

## What changed

- Media discovery remains direct authenticated mweb Innertube `/youtubei/v1/player`.
- The per-video `/watch` page is no longer a normal dependency. It was returning HTTP 429 and preventing cipher startup.
- At app startup the resolver discovers the current global YouTube player build from `/iframe_api` and caches the TV player JS URL.
- A cold song starts the Innertube format-18 request, the video-bound bgutil GVS POT request, and player metadata lookup in parallel.
- The first ciphered itag-18 candidate is deciphered once using the cached TV player JS.
- The old `/watch` bootstrap is emergency-only if global player discovery fails.
- The broken startup session-GVS warm request is disabled. It was producing HTTP 400 and was not needed for the proven video-bound POT path.
- bgutil remains bound to 127.0.0.1:4416 inside the Render container.
- `SOURCE_FORMAT=18` remains unchanged.
- Full yt-dlp remains the safety fallback.

## Expected cold-path logs

At startup:

```
v36.14 global player bootstrap ready {...}
```

On a truly unseen video ID:

```
v36.14 mweb player candidate ready {... "formatId":"18", "urlMode":"cipher" ...}
v36.14 cipher solve started {... "playerUrlSource":"iframe-api" ...}
v36.14 POT ready {...}
v36.14 cipher challenge solve returned {...}
v36.14 cipher solved {...}
v36.14 direct mweb resolve success {...}
```

If `global player bootstrap ready` is absent or the cipher solver fails, the full yt-dlp fallback is still retained.
