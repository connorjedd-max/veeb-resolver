# Veeb Render Resolver V7

This is a drop-in replacement for the existing Veeb resolver repo.

V7 keeps the same public API (`/health`, `/resolve/:videoId`, `/stream/:videoId`) and the same Cloudflare Worker configuration. The only change is how yt-dlp resolves YouTube playback internally.

It now uses:
- yt-dlp
- Node 22 / EJS
- `mweb` YouTube client
- `bgutil-ytdlp-pot-provider` 1.3.1
- the provider's local generation script at `/opt/bgutil/server`

No YouTube/Google account cookies are configured or required by this package.

## Update the existing Render service

1. Replace the files in your existing `veeb-resolver` GitHub repo with these files.
2. Commit/push to `main`.
3. Render should auto-deploy. If not, choose **Manual Deploy -> Deploy latest commit**.
4. Keep your existing Render environment variable `RESOLVER_SECRET` exactly as-is.
5. Do not change the Cloudflare `YOUTUBE_RESOLVER_URL` or `YOUTUBE_RESOLVER_SECRET` values.

## Verify

Open:

`https://veeb-resolver.onrender.com/health`

You should see values including:

```json
{
  "ok": true,
  "service": "veeb-youtube-resolver-v7",
  "secretConfigured": true,
  "youtubeClient": "mweb",
  "poTokenProvider": "bgutil",
  "poTokenProviderVersion": "1.3.1",
  "poTokenServerPresent": true
}
```

Then try playback in Veeb. If it fails, copy the new Render log lines beginning with `yt-dlp resolver error:`. V7 intentionally prints yt-dlp's useful diagnostic output on a failed extraction.
