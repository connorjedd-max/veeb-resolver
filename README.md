# Veeb Render Resolver V27

V27 restores fast progressive playback while preserving V26's completed-file cache.

Key changes:

- Prefer YouTube audio-only format 140, with format 18 as fallback.
- Build fragmented MP4 exactly as before.
- Start browser playback after a 512 KiB prebuffer instead of waiting for the whole track.
- Continue building the same file in the background until it becomes the normal Range-capable cache entry.
- Preserve single-flight foreground/preload behaviour and preemption of speculative work.
- Completed files still expose Content-Length and byte-range support.

Keep the same `RESOLVER_SECRET` and `youtube-cookies.txt` secret file.

Recommended Cloudflare Cron Trigger for a Render Free deployment:

    */10 * * * *

The Veeb Worker already contains the `scheduled()` handler that calls `/health`.
