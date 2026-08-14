# Veeb Render Resolver V36.13 - TV-variant cipher path

This is a full Render deploy package.

Changes from V36.12:

- Keeps the proven authenticated mweb Innertube path and format 18.
- After bootstrap, prefers only the session-bound `session-sts` candidate for deciphering.
- Converts the prescribed webpage player URL to the same-version TV player JS variant for SIG/N solving (`VEEB_JSC_PLAYER_VARIANT=tv` by default).
- Keeps the full yt-dlp mweb+POT resolver as fallback.
- Patches bgutil 1.3.1 at Docker build time so its HTTP server binds to `127.0.0.1:4416` instead of `[::]:4416`; this prevents Render from treating the internal POT service as another public service port.
- Explicitly pins yt-dlp-ejs 0.8.0 and Deno 2.8.1.

Expected decisive cold-path logs:

- `v36.13 mweb player candidate ready ... candidate=session-sts ... urlMode=cipher`
- `v36.13 cipher solve started ... playerVariant=tv`
- `v36.13 cipher challenge solve returned ... resultCount=2`
- `v36.13 cipher solved`
- `v36.13 direct mweb resolve success`

The internal POT server should log loopback startup, and Render should no longer announce `New primary port detected: 4416`.
