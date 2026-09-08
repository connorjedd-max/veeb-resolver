"""Bounded shared progressive MP3 jobs. Standard-library only, one ASGI worker.

Readers have independent file offsets. One producer resolves/transcodes a song,
so playback, retries, and cache-fill never share a consumable iterator. Completed
files are held briefly on ephemeral disk; R2 remains the persistent cache.
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


def mp3_frames_valid(data):
    """Check two consecutive MPEG-1 Layer III frames, matching 44.1kHz encoder.

    Reject a mislabeled HTML/JSON body and a single accidental sync word.
    Bitrate need not be constant across input frames.
    """
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


class MediaJobs:
    def __init__(self, producer, *, directory=None, max_jobs=8, max_bytes=80*1024*1024,
                 disk_bytes=256*1024*1024, ttl=900, timeout=240, failure_ttl=15):
        self.producer = producer
        self.directory = Path(directory or tempfile.mkdtemp(prefix='veeb-mp3-'))
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_jobs, self.max_bytes, self.disk_bytes = max_jobs, max_bytes, disk_bytes
        self.ttl, self.timeout, self.failure_ttl = ttl, timeout, failure_ttl
        self.jobs = {}

    def _remove(self, video_id):
        job = self.jobs.pop(video_id)
        job.path.unlink(missing_ok=True)

    def _evict(self, needed=0, extra_job=False):
        now = time.monotonic()
        for video_id, job in list(self.jobs.items()):
            age = now - job.touched
            if job.done.is_set() and not job.readers and not job.waiters and age > (self.failure_ttl if job.error else self.ttl):
                self._remove(video_id)
        candidates = sorted((j for j in self.jobs.values() if j.done.is_set() and not j.readers and not j.waiters), key=lambda j: j.touched)
        # Keep recent failure entries until cooldown expires to prevent retry storms.
        candidates = [j for j in candidates if not j.error]
        while candidates and (sum(j.size for j in self.jobs.values()) + needed > self.disk_bytes or
                              (extra_job and len(self.jobs) >= self.max_jobs)):
            self._remove(candidates.pop(0).video_id)

    def get(self, video_id, request, background=False):
        self._evict()
        existing = self.jobs.get(video_id)
        if existing:
            existing.touched = time.monotonic() if not existing.error else existing.touched
            return existing
        # Never let background cache misses create a queue behind active work.
        if background and any(not j.done.is_set() for j in self.jobs.values()):
            raise JobError('RESOLVER_BUSY', 'Cache fill deferred while playback jobs are active.')
        self._evict(extra_job=True)
        if len(self.jobs) >= self.max_jobs:
            raise JobError('RESOLVER_BUSY', 'Resolver job capacity is full. Retry shortly.')
        fd, filename = tempfile.mkstemp(prefix=video_id+'-', suffix='.mp3.part', dir=self.directory)
        os.close(fd)
        job = Job(video_id, Path(filename))
        self.jobs[video_id] = job
        job.task = asyncio.create_task(self._run(job, request))
        return job

    async def _run(self, job, request):
        iterator = None
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
                        output.write(chunk)
                        job.size += len(chunk)
                        job.ready.set()
                        job.changed.set()
                if not job.size:
                    raise JobError('INVALID_MP3', 'Encoder produced no usable MP3 audio.')
                # CBR output should closely match source duration. A clean process
                # exit on a prematurely ended manifest must not enter R2 either.
                duration = getattr(job.metadata.get('media'), 'duration', None)
                bitrate = getattr(job.metadata.get('media'), '_output_bitrate', None)
                if duration and bitrate:
                    expected = float(duration) * float(bitrate) * 125
                    if job.size < expected * 0.90:
                        raise JobError('TRUNCATED_MP3', 'MP3 is substantially shorter than the source duration.')
        except asyncio.CancelledError:
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
                # Open readers retain their descriptor on Linux, but must receive
                # an error rather than treating a partial file as a normal EOF.
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
        return {'active': sum(not j.done.is_set() for j in self.jobs.values()),
                'complete': sum(j.done.is_set() and not j.error for j in self.jobs.values()),
                'failed': sum(bool(j.error) for j in self.jobs.values()),
                'diskBytes': sum(j.size for j in self.jobs.values())}

    async def close(self):
        tasks = [job.task for job in self.jobs.values() if job.task and not job.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for video_id in list(self.jobs):
            self._remove(video_id)
        self.directory.rmdir()
