# Veeb resolver v40.7

September 2026 YouTube compatibility release. Start with **DEPLOY-V40.7.txt** and **CHANGES-V40.7.md**.

# Veeb resolver v40.6

Start with **DEPLOY-V40.6.txt** and **CHANGES-V40.6.md**.

v40.6 is a narrow foreground-priority reliability release. Production v40.5.1
telemetry showed that background warming could still add several seconds of
`sourceSlotWaitMs` to real playback because source extraction is serialized even
though download/transcode capacity reserves a foreground lane.

v40.6 keeps extraction concurrency at one and preserves the v40.5.1 source,
POT, cookie, progressive WebM, FFmpeg and R2 architecture. The only behavioral
change is that foreground playback preempts background jobs that are still in
source acquisition. Background jobs that have already published source bytes
continue normally.

Known stable startup settings remain:

- `VEEB_YTDLP_USE_AD_PLAYBACK_CONTEXT=true`
- `VEEB_YTDLP_SKIP_MWEB_CLIENT_CONFIG=false`
- `YOUTUBE_JSC_RUNTIME=deno`

The primary v40.6 production metric is foreground `sourceSlotWaitMs` while
background warming is active. It should now remain near zero.

Historical change notes remain in the `CHANGES-V40.*.md` files.
