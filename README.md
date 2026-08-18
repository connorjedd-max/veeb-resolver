# Veeb Render Resolver V36.16.5 - session mirror

This build is based on V36.16.4 and leaves the Render/Docker startup and the YouTube.js evaluator unchanged.

## Why this revision exists

The V36.16.4 Render log finally showed the same playable track through both paths:

- direct YouTube.js path: same googlevideo host, POT length 124, `cver` present, HTTP 403
- yt-dlp winning path: same googlevideo host, POT length 140, no `cver`, HTTP 200/206

For `HJstyRBLqBQ`, the query-key sets were otherwise effectively identical. This makes session binding and URL/header parity the highest-value differences to close.

## Changes

1. Adds a lightweight authenticated YouTube watch-page identity fetch before the direct MWEB `/player` call. It extracts only `DATASYNC_ID` and visitor data. It does not run yt-dlp, fetch player JS, or solve challenges.
2. Uses that Data Sync ID for the reusable bgutil GVS token, matching yt-dlp's authenticated GVS binding model.
3. Uses the Data Sync ID and visitor data in the direct `/player` auth/context too.
4. Removes only the raw `cver` query pair after YouTube.js decipher, because the successful yt-dlp URL for the same track has `c=MWEB` but no `cver`.
5. Uses yt-dlp's process-wide `std_headers` for direct googlevideo probes and direct media proxying, matching the successful fallback header shape (`accept`, `accept-language`, `sec-fetch-mode`, `user-agent`).
6. Adds safe diagnostics for `n`/`sig` lengths and whether `cver` appears in `sparams` or `lsparams`. No signed URL or PO token is logged.

## Expected fast-path log

A successful test should look roughly like:

```
v36.16.5 watch session identity {"hasDataSyncId":true,...}
v36.15 mweb player candidate ready ...
v36.15 YouTube.js decipher success ...
v36.16.5 removed helper cver to mirror yt-dlp ...
v36.15 POT ready {"bindingType":"gvs-session-watch",...}
v36.16.5 session GVS POT cached ...
v36.16.5 direct GVS candidate {"binding":"datasync","urlShape":{"potLength":140,"clientVersion":null,...}}
v36.15 media probe success {"label":"v36.16.5-youtubejs-session-gvs",...}
v36.16.5 direct mweb resolve success ...
```

If the direct probe still returns 403, compare its `urlShape` with the logged yt-dlp winning `urlShape`, especially `potLength`, `nLength`, `sigLength`, `sparamsHasCver`, and query keys.
