# Veeb Render Resolver V36.16.3 - authenticated session GVS binding

This build keeps the V36.16.2 YouTube.js Node-VM evaluator and exact MWEB cver plumbing, but fixes the next confirmed mismatch with the successful yt-dlp fallback.

## Why

Render logs showed the handcrafted direct path generating a GVS PO token bound to the video ID, while the successful authenticated yt-dlp MWEB fallback generated its PO token against the account Data Sync binding (`...||`). Current yt-dlp WebPO behavior uses Data Sync ID for authenticated GVS requests unless YouTube explicitly signals the video-ID-binding experiment.

## Changes

- Warm the existing reusable Data-Sync-bound GVS PO token during application startup.
- Use the Data Sync ID in authenticated MWEB `/player` headers.
- Use the session-bound GVS token for the first direct media probe.
- If that session-bound probe gets rejected, make one cheap video-ID-bound GVS probe before falling back to yt-dlp.
- Keep V36.16.2 cver restamping exactly in place.
- No Worker/player/Android transport changes.

Expected successful fast-path logs include:

- `v36.1 Data Sync ID ready`
- `v36.1 session GVS POT cached`
- `sessionGvsReady: true`
- `v36.16.3 session-bound GVS probe won`
- `v36.16.3 direct mweb resolve success`
