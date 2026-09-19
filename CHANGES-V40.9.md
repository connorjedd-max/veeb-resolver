# Veeb Resolver v40.9 - terminal source failures

- Keep all v40.8 September YouTube compatibility work and explicit mweb format-18 fallback.
- Treat `SOURCE_VIDEO_UNAVAILABLE` and `SOURCE_REGION_RESTRICTED` as per-video terminal failures rather than generic resolver failures.
- Background R2 warms stop after the first definitive terminal result instead of trying all five YouTube routes.
- Foreground playback requires two matching terminal confirmations before giving up.
- Return HTTP 410 for unavailable videos and HTTP 451 for region-restricted videos, with `X-Veeb-Error-Code`, so upstream circuit breakers do not confuse dead media with resolver outages.
- Add a six-hour in-memory negative cache to coalesce repeated requests while the Worker persists its durable unavailable state.
- `/health` exposes the terminal-failure cache and policy.
