# Veeb Render Resolver V36.16.4 - player DataSync GVS

This build keeps the V36.16.2 YouTube.js helper and the V36.16.3 fallback architecture, but fixes the session-GVS bootstrap.

## Why V36.16.3 did not test session-bound GVS

Render logged `YouTube session bootstrap returned HTTP 400`, so V36.16.3 never obtained the authenticated Data Sync ID and immediately fell back to the already-known failing video-ID GVS token.

## V36.16.4 changes

- Reads `responseContext.mainAppWebResponseContext.datasyncId` / `dataSyncId` directly from the authenticated MWEB `/player` response, matching yt-dlp's own Data Sync extraction model.
- Removes the separate HTML session-GVS warm from startup. BgUtils integrity and YouTube.js still warm at boot.
- Mints and caches the session GVS POT only after `/player` provides the exact binding. POT minting is normally milliseconds once BgUtils is warm.
- Keeps video-ID POT as a cheap fallback for YouTube's GVS video-binding experiment.
- Separates Google Video media headers from Innertube API headers. GVS probes now use only the MWEB User-Agent + YouTube Referer, not API Origin/auth headers.
- Adds non-secret URL-shape diagnostics for both direct and successful yt-dlp URLs so a remaining 403 can be compared without logging signed URLs or tokens.

## Expected successful fast path

Look for:

- `v36.16.4 Data Sync ID learned from player`
- `v36.15 POT ready ... "bindingType":"gvs-session-player"`
- `v36.16.4 session GVS POT cached`
- `v36.16.4 direct GVS candidate ... "binding":"datasync"`
- `v36.15 media probe success ... v36.16.4-youtubejs-session-gvs`
- `v36.16.4 direct mweb resolve success`

If the session probe still returns 403, the fallback yt-dlp success log now includes `mediaHeaderKeys` and `urlShape`; compare those with the preceding `direct GVS candidate` line.
