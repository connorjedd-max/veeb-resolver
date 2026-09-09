"""A route cooldown requires failures on distinct tracks, never one private video."""
import time
from media_jobs import JobError


class AcquisitionGuard:
    BLOCK_CODES = {'SOURCE_ACCESS_DENIED', 'SOURCE_HTTP_403', 'MEDIA_HTTP_403', 'SOURCE_RATE_LIMITED'}

    def __init__(self, cooldown=120, clock=time.monotonic):
        self.cooldown, self.clock = cooldown, clock
        self.failures = {}
        self.until = 0

    def check(self):
        if self.until > self.clock():
            exc = JobError('SOURCE_ROUTE_COOLDOWN',
                'Source requests were refused on multiple tracks. This route is cooling down. '
                'Use the source-agent probe to test the same downloader outside Render.')
            exc.retry_after = max(1, int(self.until - self.clock()) + 1)
            raise exc

    def failed(self, video_id, codes):
        if not codes or not set(codes) <= self.BLOCK_CODES:
            return
        now = self.clock()
        self.failures = {v: t for v, t in self.failures.items() if now - t < self.cooldown}
        self.failures[video_id] = now
        if len(self.failures) >= 2:
            self.until = now + self.cooldown

    def succeeded(self):
        self.failures.clear()
        self.until = 0

    def status(self):
        return {'cooldownSecondsRemaining': max(0, int(self.until - self.clock())),
                'refusedDistinctTracks': len(self.failures)}
