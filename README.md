# Veeb Render Resolver V19

V19 targets the remaining cold-play delay.

## Cold-start strategy

V18 forced every cold request through `mweb`, which requires the GVS PO-token path. V19 tries the logged-in `tv_downgraded` client first, then falls back to the known-good `mweb` path.

Primary path:

`tv_downgraded + cookies + format 18`

Fallback path:

`mweb + cookies + fetch_pot=auto + use_ad_playback_context=true + format 18`

The fallback still uses the existing bgutil HTTP PO-token server.

## Why this should be faster

- `tv_downgraded` supports account cookies and is one of yt-dlp's current default clients when logged-in cookies are supplied.
- `fetch_pot=auto` stops yt-dlp from requesting PO tokens for contexts that do not require them.
- `use_ad_playback_context=true` tells yt-dlp to skip the mandatory preroll waiting behavior for mweb.
- V18 prefetch and local audio cache remain intact.
- New startup phase timing logs show where every remaining second is being spent.

## Premium account note

The dedicated Veeb YouTube account is assumed to be a normal free account.

If the cookie file ever comes from a YouTube Premium account, set this Render environment variable:

`YOUTUBE_PREMIUM_ACCOUNT=true`

That disables `use_ad_playback_context`, as required by yt-dlp's documentation for Premium cookies.

## Existing settings to keep

- `RESOLVER_SECRET`
- Secret file `youtube-cookies.txt`

No Cloudflare Worker change is required from V7.2.

## Optional tuning

- `YOUTUBE_PRIMARY_CLIENT=tv_downgraded`
- `YOUTUBE_FALLBACK_CLIENT=mweb`
- `YOUTUBE_FAST_CLIENT_TIMEOUT_SECONDS=12`
- `STREAM_START_TIMEOUT_SECONDS=0`

The 12-second primary timeout prevents a broken TV-client attempt from hanging forever before the mweb fallback begins.

## Expected health

- `service`: `veeb-youtube-resolver-v19`
- `primaryClient`: `tv_downgraded`
- `fallbackClient`: `mweb`
- `fetchPotPolicy`: `auto`
- `mwebAdPlaybackContext`: `true`
- `streamTransport`: `format18-fast-client-fallback-cache-prefetch`
- `rangeSeeking`: `true`

## Useful cold-play logs

Fast path success should look like:

`cold playback attempt ... "client":"tv_downgraded"`

then:

`cold first media bytes ... "client":"tv_downgraded","totalColdElapsedSeconds":...`

If TV fails, V19 logs the failure and automatically tries mweb. On mweb, the phase logs will show webpage, player API, JS challenge, PO-token request, format selection, and first-byte timing.

Cached and prefetched playback works exactly as in V18.
