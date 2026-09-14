# Veeb resolver V40.4

Start with **DEPLOY-V40.4.txt**.

V40.4 is the first deliberately narrow startup-speed change after V40.3
identified source acquisition as the dominant cold-playback bottleneck.
It enables yt-dlp's official `mweb` ad playback context so yt-dlp can avoid the
mandatory preroll wait before starting the source download.

This is feature-gated with `VEEB_YTDLP_USE_AD_PLAYBACK_CONTEXT`. Set it to
`false` to restore the previous mweb extractor behavior without reverting the
rest of the resolver.

See **CHANGES-V40.4.md** for scope, caveats and expected telemetry.
