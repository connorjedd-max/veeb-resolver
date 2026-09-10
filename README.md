# Veeb resolver V39.2

Start with **DEPLOY.txt**. This is a full resolver release. Use the existing
**VEEB-WORKER-v39.1.txt** companion Worker; its version remains unchanged.

V39.2 separates extraction admission from already-started downloads. One
extractor runs at a time; up to two source downloads remain owned and bounded.
The next track can extract while the first is still filling its MP3 cache.

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
The complete release has nine test modules and 81 tests.

The optional desktop agent remains available for existing users. Normal
installation uses `VEEB_SOURCE_MODE=direct`. The agent uploads completed MP3s
and is not an instant-play solution.
