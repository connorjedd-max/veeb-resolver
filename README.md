# Veeb Render Resolver V20 - FAST COLD START

V20 is built directly from the V19 timing logs. V19 showed that `tv_downgraded`
added 12 seconds before the known-good mweb path even started, and the largest
remaining delay was YouTube's JavaScript challenge stage.

## V20 changes

1. **No tv_downgraded attempt.** It goes straight to mweb.
2. **Native Deno 2.8.1 for EJS.** yt-dlp currently recommends Deno for YouTube
   JavaScript challenge solving. Node remains installed only for the bgutil POT server.
3. **`player_skip=configs`.** Removes the client-config network request while keeping
   the webpage and JS steps required by the working format-18 path.
4. **Foreground playback priority.** A cold live play cancels unrelated speculative
   prefetch tasks so Render CPU is spent on the song the user actually tapped.
5. Keeps V18/V19 cache, prefetch and byte-range serving.
6. Prefetch concurrency defaults to 1 to avoid two EJS challenges fighting for the
   limited CPU on the free Render instance.

## Keep existing Render settings

- `RESOLVER_SECRET`
- Secret file: `youtube-cookies.txt`

No new secret is required.

## Expected health

- `service`: `veeb-youtube-resolver-v20`
- `youtubeClient`: `mweb`
- `jsRuntime`: `deno`
- `playerSkip`: `["configs"]`
- `poTokenHttpServerReady`: `true`
- `streamTransport`: `format18-mweb-deno-fast-cold-cache-prefetch`

## Logs to compare

The key number is:

`cold first media bytes ... totalColdElapsedSeconds`

Compare that directly with V19's 48.42 seconds and the earlier mweb-only ~20-30 second
starts. The `tv_downgraded` 12-second penalty should be completely gone.
