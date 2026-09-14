# V40.5.1 - stability rollback

- `VEEB_YTDLP_SKIP_MWEB_CLIENT_CONFIG` now defaults to `false`.
- `player_skip=configs` remains available only as an explicit opt-in experiment.
- The authenticated mweb path therefore returns to the proven v40.4/v40.4.1 client-config flow by default.
- `use_ad_playback_context=true` remains enabled by default because v40.4 showed a measurable startup improvement without the v40.5 403 regression.
- No changes to source ownership, concurrency, FFmpeg, progressive thresholds, cookies, POT provider, or R2 completion.
