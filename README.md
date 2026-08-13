# Veeb YouTube Resolver V25

V25 is a speed/reliability revision of V24.

The key change is progressive cache handoff. V24 waited for the entire audio file to finish downloading before the browser received anything. The user's logs showed first playable media around 16-19 seconds but the completed cache could arrive 7-10 seconds later. V25 keeps the resolver build independent of the browser, buffers a substantial fragmented-MP4 lead-in (default 192 KiB), then lets playback read from the growing file while the rest continues caching in the background.

This keeps the known-good authenticated mweb + bgutil PO-token path and single-flight CPU policy. Completed files still become normal byte-range cache hits.

Optional environment variable:

- `VEEB_LIVE_PREBUFFER_BYTES` defaults to `196608` (192 KiB). Increase it if a particular browser needs more startup buffer. Lower values can start slightly sooner but are less conservative.

Keep the existing `RESOLVER_SECRET` and `/etc/secrets/youtube-cookies.txt` secret file.
