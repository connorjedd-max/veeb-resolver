# Veeb Render Resolver V8

V8 is a drop-in replacement for V7. Veeb, the Cloudflare Worker, the Render URL, and `RESOLVER_SECRET` all stay unchanged.

V8 keeps:
- yt-dlp
- Node 22 / EJS
- `mweb` YouTube client
- `bgutil-ytdlp-pot-provider` 1.3.1
- the local bgutil provider script

V8 adds support for one runtime-only YouTube cookie file:

`/etc/secrets/youtube-cookies.txt`

The cookie file is not included in this ZIP and must never be committed to GitHub.

## 1. Replace the existing resolver files

Replace the files in your existing `veeb-resolver` GitHub repo with the files from this package and push to `main`.

Render should redeploy automatically. If it does not, use **Manual Deploy -> Deploy latest commit**.

## 2. Add the cookie file in Render

In the existing Render service, add a Secret File with:

- Filename: `youtube-cookies.txt`
- Contents: your Netscape-format YouTube cookies export

Render mounts secret files under `/etc/secrets/`, so V8 will read:

`/etc/secrets/youtube-cookies.txt`

Do not put the cookies into an environment variable and do not commit them to GitHub.

## 3. Keep existing settings

Keep the existing Render environment variable:

`RESOLVER_SECRET`

Do not change these Cloudflare Worker bindings:

- `YOUTUBE_RESOLVER_URL=https://veeb-resolver.onrender.com`
- `YOUTUBE_RESOLVER_SECRET=<same secret as Render>`

## 4. Verify

Open:

`https://veeb-resolver.onrender.com/health`

Expected key values:

```json
{
  "ok": true,
  "service": "veeb-youtube-resolver-v8",
  "secretConfigured": true,
  "youtubeClient": "mweb",
  "poTokenProvider": "bgutil",
  "poTokenProviderVersion": "1.3.1",
  "poTokenServerPresent": true,
  "cookieFileConfigured": true,
  "cookieFilePresent": true,
  "cookieFilePath": "/etc/secrets/youtube-cookies.txt"
}
```

If `cookieFilePresent` is false, Render has not mounted the secret file under the expected filename.

## Security note

Use a dedicated YouTube/Google account for this resolver, not your primary account. Treat `youtube-cookies.txt` like a password. Anyone with a valid session cookie file may be able to act as that account until the session is revoked or expires.
