# Veeb Render Resolver V24

V24 removes the failed Safari foreground race and makes YouTube extraction strictly single-flight on the small Render instance.

Key changes:
- authenticated mweb only
- one yt-dlp/ffmpeg pipeline at a time
- same-track prefetch is reused instead of canceled/restarted
- unrelated speculative work is canceled and its process group is torn down before foreground extraction starts
- shared writable YouTube cookie jar again, now safe because extraction is serialized
- explicit shared yt-dlp cache directory at `/tmp/veeb-yt-dlp-cache`
- `--no-check-formats` is explicit
- keeps format 18 -> ffmpeg AAC-only MP4 cache and byte ranges

Keep the same Render secret and `/etc/secrets/youtube-cookies.txt` secret file.
No new required environment variables.
