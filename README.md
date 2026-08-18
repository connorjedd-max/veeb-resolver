# Veeb Render Resolver V36.16.2 - cver plumbing repair

Built from the known-booting V36.16.1 stable-startup package.

This patch targets the exact evidence from the 6r1l7egqcBI log:

- MWEB /player produced itag 18 in ~145 ms.
- YouTube.js decipher succeeded in ~70 ms.
- The helper response reported client/clientVersion null and cverRestamped false.
- The resulting Google Video probe returned 403.

Changes:

1. Python sends the exact MWEB clientName/clientVersion used for /player to /decipher.
2. The helper accepts those fields and uses environment values only as fallback.
3. c and cver are changed with raw query replacement only. The signed Google Video URL is not rebuilt with URLSearchParams.
4. /decipher returns previous/current client metadata and cverRestamped.
5. /selftest now verifies sig, n transformation, and MWEB cver.
6. Dockerfile/render.yaml/package.json/requirements.txt remain from the stable-startup package.

Expected real-track log:

    youtubejs-mweb-url-restamped ...
    v36.15 YouTube.js decipher success ... "client":"MWEB","clientVersion":"2.20260708.05.00",...,"cverRestamped":true

The decisive next line is either:

    v36.16.2 direct mweb resolve success ...

or:

    v36.16.2 direct head-start failed ... Google Video probe returned HTTP 403

If it still 403s with cverRestamped=true, cver is ruled out and the next target is the GVS request/token/client binding, not the Worker or Android playback layer.
