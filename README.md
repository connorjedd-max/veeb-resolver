# Veeb YouTube Resolver - Render

This is the private playback resolver used by Veeb. It runs yt-dlp behind a small FastAPI service.

## Render deployment

1. Create a private GitHub repository, for example `veeb-resolver`.
2. Upload the files in this folder to the repository root.
3. In Render choose New > Blueprint and connect the repository. Render will detect `render.yaml`.
4. When prompted for `RESOLVER_SECRET`, enter a long random secret. Keep it private.
5. Deploy.
6. Open `https://YOUR-SERVICE.onrender.com/health` and confirm `ok: true` and `secretConfigured: true`.
7. In the Veeb Cloudflare Worker add:
   - `YOUTUBE_RESOLVER_URL=https://YOUR-SERVICE.onrender.com`
   - `YOUTUBE_RESOLVER_SECRET=<the exact same secret>`
8. Deploy `Veeb-worker-v6-youtube-resolver.txt` in Cloudflare.

## Notes

- Render Free web services spin down after 15 minutes with no inbound traffic and wake on the next request.
- The resolver does not store media. It caches only short-lived resolved metadata.
- The resolver intentionally does not use YouTube account cookies, CAPTCHA automation, or bot-challenge bypasses.
