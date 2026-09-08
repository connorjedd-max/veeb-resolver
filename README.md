# Veeb V38.1: cookie-backed mweb and session verification

Targeted repair built from the deployed V37.9 source. Live YouTube access is not
verified by the local tests. No proxy is required to run this version. Keep the
existing V37.6.1 diagnostic Cloudflare Worker.

## What changed

- An explicit authenticated mweb mode is now paired with the cookie-free mweb
  mode. The prior default implementation supplied cookies only to fg-auth,
  which used default clients unless YOUTUBE_AUTH_FALLBACK_CLIENT was overridden.
- Cookie-backed acquisition and source download use one yt-dlp process/jar.
  FFmpeg then reads the owned local source file. This fallback waits for the
  source download; it is not progressive during that download. The anonymous
  direct-HTTP FFmpeg path remains progressive.
- Both mweb modes use identical provider policy (fetch_pot=auto). Optional player
  tokens/ad context are no longer forced, and token-cache tracing is disabled.
- Source secret changes atomically refresh runtime cookies. Each authenticated
  child receives a private copy. Server-rotated cookies are committed back to
  runtime only if its input snapshot is still current. The Render secret file
  is never edited. Cookie-free attempts never receive account cookies.
- Local inspection checks Netscape structure and expiry, treating zero expiry
  as a session cookie. Local fields never establish remote authentication.
- Main errors are preserved separately from bounded diagnostic excerpts. Source
  API challenges and media-download HTTP 403 are reported as different phases.
  Resolver/API errors redact URLs/tokens before truncating. The upstream bgutil
  process can still print its own tokens to Render logs; do not publish raw logs.
- Parent-owned source directories are removed after failed/cancelled downloads.
  Existing shared MP3 jobs, full-file checks and MP3-only output remain.

## Existing /admin TEST PLAYBACK now checks

1. codecSelfTest: local synthetic WAV through this instance's production FFmpeg
   encoder, with real MP3 frame validation. No YouTube call or R2 write.
2. cookieSessionTest: one YouTube subscriptions-page request using the cookie jar.
   An explicit server LOGGED_IN flag gives true/false. No flag, conflicting flags,
   consent/challenge pages, or failed requests yield unknown/null. This proves at
   most page-level recognition, not permission to download a track.
3. Production acquisition and completed MP3 validation. Look for fg-mweb-auth,
   usesCookies=true, and the exact stage/code/error. On complete success the
   unchanged Worker performs its existing R2 store and range-readback checks.

Health is liveness. Its cookie authentication field refers to the last page-level
check and has a timestamp/scope. Token generation alone does not establish that
YouTube will accept a media download. Existing logs cannot prove IP mismatch.

## Deployment

Upload ALL archive contents into the repository ROOT, including source_support.py
and tests/. Dockerfile copies the new module. No pycache is required. Wait for
Render Live, then run the existing admin test once. Expected version:
v38.1-mp3-stream. Do not change the Worker or add a proxy for this test.

Keep RESOLVER_SECRET and the existing youtube-cookies.txt secret file. Never
commit cookies to GitHub or send them in chat.

## Validation

35 offline tests passed locally: real FFmpeg, loopback HTTP input, real FastAPI
ASGI routes, cookie isolation/rotation, cancellation cleanup, shared readers and
partial-file rejection. Source/session responses in the tests are mocks.
Live YouTube, bgutil interoperability and actual R2 writes were NOT tested here.
The exact pinned yt-dlp was unavailable in the local execution environment;
Render's Docker build installs the existing pinned dependencies and runs tests.

Run: python -m unittest discover -s tests -v

## Fresh cookie export

Use the official yt-dlp instructions: open a new private/incognito window, sign
into YouTube, navigate the same tab to https://www.youtube.com/robots.txt, export
youtube.com cookies as Netscape text, then close that private session. Put the
file only in Render's secret-file configuration, named youtube-cookies.txt (or
your configured path). Fresh cookies do not guarantee media access, and account
use with automated downloaders carries restriction risk.

References:
https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies
https://github.com/Brainicism/bgutil-ytdlp-pot-provider
