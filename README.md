# Veeb Render Resolver V26

V26 prioritises reliable browser playback over the V25 growing-file handoff.

The YouTube extraction/cache build is still single-flight and prefetch-aware, but
the browser now receives only a completed MP4 with Content-Length and byte-range
support. In the V25 logs the full file commonly completed less than one second
after the progressive handoff, so this removes a large source of buffering and
media-element failures for very little added cold latency.

Keep the same RESOLVER_SECRET and youtube-cookies.txt secret file.
