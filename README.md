# Veeb Render Resolver V10

V10 is a drop-in replacement for V7. Veeb, the Cloudflare Worker, the Render URL, and `RESOLVER_SECRET` all stay unchanged.

V10 keeps:
- yt-dlp
- Node 22 / EJS
- `mweb` YouTube client
- `bgutil-ytdlp-pot-provider` 1.3.1
- the local bgutil provider script

V10 adds support for one runtime-only YouTube cookie file:

`/etc/secrets/youtube-cookies.txt`

The cookie file is not included in this ZIP and must never be committed to GitHub.

## 1. Replace the existing resolver files

Replace the files in your existing `veeb-resolver` GitHub repo with the files from this package and push to `main`.

Render should redeploy automatically. If it does not, use **Manual Deploy -> Deploy latest commit**.

## 2. Add the cookie file in Render

In the existing Render service, add a Secret File with:

- Filename: `youtube-cookies.txt`
- Contents: your Netscape-format YouTube cookies export

Render mounts secret files under `/etc/secrets/`, so V10 will read:

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
  "service": "veeb-youtube-resolver-v10",
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


## V10 playback change
V10 caps each Google media request to 8 MiB even when the browser sends an open-ended Range such as `bytes=0-`. yt-dlp documents that YouTube throttles requests with an HTTP chunk size above 10 MiB. The browser can request the next range normally. V10 also logs `yt-dlp resolved`, `media fetch`, and `media upstream` lines so silent playback stalls can be diagnosed.


## V10 fix: Render Secret Files are read-only

Render mounts `/etc/secrets/youtube-cookies.txt` read-only. yt-dlp saves its cookie jar on exit, so V9 could resolve far enough to reach cookie handling and then fail with `OSError: [Errno 30] Read-only file system`. V10 copies the secret file once per resolver process to `/tmp/veeb-youtube-cookies.txt`, chmods it to `0600`, and passes that writable runtime copy to yt-dlp. The original Render secret file remains untouched.
