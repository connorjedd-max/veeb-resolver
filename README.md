# Veeb resolver V40.1

Start with **DEPLOY.txt**. This is a full priority-aware resolver release. Pair it with
**veeb-worker-r2-priority-parallel-v40.1.txt** in Cloudflare.

V40.1 keeps serialized extraction startup but makes active MP3 jobs priority-aware.
Background R2 work has bounded parallel capacity, foreground playback has reserved
capacity, and foreground can preempt a lower-priority background job if total capacity
is full. Next-track prefetch also outranks bulk library warming.

Cookie-based mweb gets first priority when a usable cookie file is available and
has not been explicitly rejected by the session diagnostic. Known rejected
sessions are skipped until the secret changes. Cookie fields and recognised
login now have separate health fields.

Progressive WebM-to-MP3, shared jobs, finite MP3 validation, R2 completion rules,
source cooldowns and owned-process cancellation are preserved. A rejected
YouTube session must still be repaired in Render.

**COMPARISON.md** explains the older fast path, its reproduced cancellation bug,
the supplied live diagnostic and the changes made here.
**TEST-RESULTS.txt** describes local verification and its production limits.
The Docker build discovers all tests under `tests/` and requires them to pass.
The complete release has nine test modules and 85 tests.

The optional desktop agent remains available for existing users. Normal
installation uses `VEEB_SOURCE_MODE=direct`. The agent uploads completed MP3s
and is not an instant-play solution.
