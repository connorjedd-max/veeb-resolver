"""Shared finite MP3 jobs with foreground-first, preemptible background capacity.

One producer exists per track so playback, retries and R2 harvesting share the
same bytes. Background jobs may use only their configured share of capacity.
If foreground demand arrives while total capacity is full, the oldest active
background job is cancelled so playback can take its place.
"""
import asyncio
from dataclasses import dataclass, field
import os
from pathlib import Path
import tempfile
import time
import uuid


class JobError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass
class Job:
    video_id: str
    path: Path
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    size: int = 0
    metadata: dict = field(default_factory=dict)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    error: BaseException | None = None
    task: asyncio.Task | None = None
    touched: float = field(default_factory=time.monotonic)
    readers: int = 0
    waiters: int = 0
    background: bool = False
    purpose: str = 'playback'
    preempt_requested: bool = False
    priority: int = 100


def mp3_frames_valid(data):
    """Check two consecutive MPEG-1 Layer III frames, matching 44.1kHz encoder."""
    rates = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
    def length(pos):
        if len(data) < pos + 4:
            return 0
        a, b, c, _ = data[pos:pos+4]
        if a != 255 or b & 0xFE != 0xFA or c & 0x0C != 0:
            return 0
        index = c >> 4
        if not 0 < index < 15:
            return 0
        return 144000 * rates[index] // 44100 + ((c >> 1) & 1)
    first = length(0)
    second = length(first) if first else 0
    return bool(first and second and len(data) >= first + second)


def purpose_priority(purpose, background):
    if not background:
        return 100
    value = str(purpose or '').strip().lower()
    if value == 'r2-next-prefetch':
        return 70
    if value == 'r2-background-prefetch':
        return 40
    if value in {'cache-fill', 'cache-import'}:
        return 30
    if value == 'r2-library-warm' or value.startswith('background-'):
        return 20
    return 25


class MediaJobs:
    def __init__(self, producer, *, directory=None, max_jobs=32, max_bytes=80*1024*1024,
                 disk_bytes=256*1024*1024, ttl=900, timeout=240, failure_ttl=15,
                 max_active=2, max_background=None, collection_grace=90):
        self.producer = producer
        self.directory = Path(directory or tempfile.mkdtemp(prefix='veeb-mp3-'))
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_jobs, self.max_bytes, self.disk_bytes = max_jobs, max_bytes, disk_bytes
        self.ttl, self.timeout, self.failure_ttl = ttl, timeout, failure_ttl
        self.max_active = max(1, int(max_active))
        if max_background is None:
            max_background = max(0, self.max_active - 1)
        self.max_background = max(0, min(int(max_background), self.max_active - 1 if self.max_active > 1 else 0))
        self.collection_grace = collection_grace
        self.jobs = {}
        self.preemptions = 0

    def _remove(self, video_id):
        job = self.jobs.pop(video_id)
        job.path.unlink(missing_ok=True)

    def _evict(self, needed=0, extra_job=False):
        now = time.monotonic()
        for video_id, job in list(self.jobs.items()):
            age = now - job.touched
            failure_age = max(self.failure_ttl, getattr(getattr(job, 'error', None), 'retry_after', 0))
            if job.done.is_set() and not job.readers and not job.waiters and age > (failure_age if job.error else self.ttl):
                self._remove(video_id)
        candidates = sorted((j for j in self.jobs.values() if j.done.is_set() and not j.readers and not j.waiters), key=lambda j: j.touched)
        candidates = [j for j in candidates if not j.error and now - j.touched > self.collection_grace]
        while candidates and (sum(j.size for j in self.jobs.values()) + needed > self.disk_bytes or
                              (extra_job and len(self.jobs) >= self.max_jobs)):
            self._remove(candidates.pop(0).video_id)

    def _active(self):
        return [j for j in self.jobs.values() if not j.done.is_set()]

    def _preempt_one_background(self, incoming_priority=100):
        candidates = [j for j in self._active() if j.background and j.task and not j.task.done()
                      and int(j.priority) < int(incoming_priority)]
        if not candidates:
            return None
        # Preserve the more valuable/older work when possible. Preempt the lowest
        # priority class first, then the newest job within that class.
        lowest = min(int(j.priority) for j in candidates)
        same_class = [j for j in candidates if int(j.priority) == lowest]
        job = max(same_class, key=lambda item: item.touched)
        job.preempt_requested = True
        job.task.cancel()
        self.preemptions += 1
        return job

    def _task_finished(self, job, task):
        # A task can be cancelled before _run receives its first timeslice. Make
        # that edge deterministic so readers/waiters are never left hanging.
        if not task.cancelled() or job.done.is_set():
            return
        if job.preempt_requested:
            error = JobError('BACKGROUND_PREEMPTED', 'Background cache fill yielded to higher-priority playback/cache demand. Retry shortly.')
            error.retry_after = 5
            job.error = error
        else:
            job.error = JobError('JOB_CANCELLED', 'MP3 job was cancelled.')
        job.touched = time.monotonic()
        job.path.unlink(missing_ok=True)
        job.size = 0
        job.done.set()
        job.ready.set()
        job.changed.set()

    def get(self, video_id, request, background=False, purpose=''):
        self._evict()
        purpose = str(purpose or '').strip().lower() or 'playback'
        priority = purpose_priority(purpose, background)
        existing = self.jobs.get(video_id)
        if existing:
            existing.touched = time.monotonic() if not existing.error else existing.touched
            # If a user asks for a track already being warmed, that exact job becomes
            # foreground-owned and must no longer be eligible for preemption.
            if not background and existing.background and not existing.done.is_set():
                existing.background = False
                existing.purpose = purpose
                existing.priority = 100
            return existing

        active = self._active()
        active_background = sum(1 for job in active if job.background)
        if background:
            if self.max_background <= 0:
                raise JobError('RESOLVER_BUSY', 'Background acquisition is disabled at the current capacity.')
            if active_background >= self.max_background or len(active) >= self.max_active:
                preempted = self._preempt_one_background(priority)
                if preempted is None:
                    raise JobError('RESOLVER_BUSY', 'Background acquisition capacity is occupied. Retry shortly.')
        elif len(active) >= self.max_active:
            preempted = self._preempt_one_background(priority)
            if preempted is None:
                raise JobError('RESOLVER_BUSY', 'Foreground acquisition capacity is occupied. Retry shortly.')

        self._evict(extra_job=True)
        if len(self.jobs) >= self.max_jobs:
            raise JobError('RESOLVER_BUSY', 'Resolver job capacity is full. Retry shortly.')
        fd, filename = tempfile.mkstemp(prefix=video_id+'-', suffix='.mp3.part', dir=self.directory)
        os.close(fd)
        job = Job(video_id, Path(filename), background=bool(background), purpose=purpose, priority=priority)
        self.jobs[video_id] = job
        job.task = asyncio.create_task(self._run(job, request))
        job.task.add_done_callback(lambda task, current=job: self._task_finished(current, task))
        return job

    async def _run(self, job, request):
        from mp3_validation import MP3Validator
        iterator = None
        validator = MP3Validator()
        try:
            async with asyncio.timeout(self.timeout):
                iterator = await self.producer(job.video_id, request, job)
                startup = bytearray()
                with job.path.open('wb', buffering=0) as output:
                    async for chunk in iterator:
                        if not chunk:
                            continue
                        if not job.ready.is_set():
                            startup.extend(chunk)
                            if len(startup) < 2048:
                                continue
                            if not mp3_frames_valid(startup):
                                raise JobError('INVALID_MP3', 'Encoder output did not contain valid MP3 frames.')
                            chunk = bytes(startup)
                            startup.clear()
                        if job.size + len(chunk) > self.max_bytes:
                            raise JobError('MP3_TOO_LARGE', 'MP3 exceeds the per-track size limit.')
                        self._evict(needed=len(chunk))
                        if sum(j.size for j in self.jobs.values()) + len(chunk) > self.disk_bytes:
                            raise JobError('RESOLVER_BUSY', 'Temporary MP3 storage is full.')
                        validator.feed(chunk)
                        output.write(chunk)
                        job.size += len(chunk)
                        job.ready.set()
                        job.changed.set()
                if not job.size:
                    raise JobError('INVALID_MP3', 'Encoder produced no usable MP3 audio.')
                decoded_duration = validator.finish()
                job.metadata['validatedDuration'] = decoded_duration
                duration = getattr(job.metadata.get('media'), 'duration', None)
                bitrate = getattr(job.metadata.get('media'), '_output_bitrate', None)
                if duration and bitrate:
                    tolerance = max(2.0, float(duration) * 0.01)
                    if abs(decoded_duration - float(duration)) > tolerance:
                        raise JobError('TRUNCATED_MP3', 'MP3 is substantially shorter than the source duration.')
        except asyncio.CancelledError:
            if job.preempt_requested:
                error = JobError('BACKGROUND_PREEMPTED', 'Background cache fill yielded to higher-priority playback/cache demand. Retry shortly.')
                error.retry_after = 5
                job.error = error
            else:
                job.error = JobError('JOB_CANCELLED', 'MP3 job was cancelled.')
        except BaseException as exc:
            job.error = exc
        finally:
            if iterator is not None:
                try:
                    await iterator.aclose()
                except BaseException:
                    pass
            job.touched = time.monotonic()
            job.done.set()
            job.ready.set()
            job.changed.set()
            if job.error:
                job.path.unlink(missing_ok=True)
                job.size = 0

    async def wait(self, job, complete=False):
        job.waiters += 1
        try:
            await (job.done if complete else job.ready).wait()
            if job.error:
                raise job.error
        finally:
            job.waiters -= 1

    def open_reader(self, job):
        if job.error:
            raise job.error
        source = job.path.open('rb', buffering=0)
        job.readers += 1
        closed = False

        def close():
            nonlocal closed
            if not closed:
                closed = True
                source.close()
                job.readers -= 1
                job.touched = time.monotonic()

        async def body():
            try:
                while True:
                    job.changed.clear()
                    if job.error:
                        raise job.error
                    chunk = source.read(64*1024)
                    if chunk:
                        yield chunk
                    elif job.done.is_set():
                        break
                    else:
                        await job.changed.wait()
            finally:
                close()
        return body(), close

    async def read(self, job):
        body, close = self.open_reader(job)
        try:
            async for chunk in body:
                yield chunk
        finally:
            close()

    def stats(self):
        self._evict()
        active = self._active()
        return {
            'active': len(active),
            'activeForeground': sum(1 for j in active if not j.background),
            'activeBackground': sum(1 for j in active if j.background),
            'complete': sum(j.done.is_set() and not j.error for j in self.jobs.values()),
            'failed': sum(bool(j.error) for j in self.jobs.values()),
            'diskBytes': sum(j.size for j in self.jobs.values()),
            'maxActive': self.max_active,
            'maxBackground': self.max_background,
            'foregroundReserved': max(0, self.max_active - self.max_background),
            'backgroundPreemptions': self.preemptions,
        }

    async def close(self):
        tasks = [job.task for job in self.jobs.values() if job.task and not job.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for video_id in list(self.jobs):
            self._remove(video_id)
        self.directory.rmdir()
