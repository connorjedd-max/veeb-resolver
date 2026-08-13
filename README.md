# Veeb Render Resolver V20.1

Playback reliability hotfix on top of V20.

The V20 logs proved YouTube extraction and the AAC/MP4 pipeline were succeeding,
but the browser could still reject the first cold, chunked fragmented-MP4 response.

V20.1 changes cold playback to cache-first:

1. resolve YouTube with the same V20 fast mweb + Deno path
2. finish the ~1 second AAC media transfer into /tmp
3. serve the browser the completed MP4 with Content-Length + byte ranges

Prefetched and cached tracks are still immediate.

Keep the same Render secret and youtube-cookies.txt secret file.
No new environment variables are required.
