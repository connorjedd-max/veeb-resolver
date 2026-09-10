"""Serialize extraction while reserving download capacity for foreground playback."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

DOWNLOAD_STARTED_FILE = '.veeb-download-started'


class ExtractionLease:
    def __init__(self, semaphore):
        self.semaphore = semaphore
        self.held = True
        self.watcher = None

    def release(self):
        if self.held:
            self.held = False
            self.semaphore.release()

    def watch(self, directory):
        async def monitor():
            marker = Path(directory) / DOWNLOAD_STARTED_FILE
            while not marker.is_file():
                await asyncio.sleep(.02)
            self.release()
        self.watcher = asyncio.create_task(monitor())

    async def close(self):
        if self.watcher is not None:
            self.watcher.cancel()
            await asyncio.gather(self.watcher, return_exceptions=True)
        self.release()


@asynccontextmanager
async def source_slot(extraction, downloads, *, download, background=False, background_downloads=None):
    """Own source capacity until cleanup, with a reserved foreground lane.

    Background jobs must acquire the smaller background semaphore before the
    shared total-download semaphore. This means background work can never fill
    every source-download slot, while foreground jobs acquire only the shared
    semaphore and can use the reserved capacity immediately.
    """
    download_held = False
    background_download_held = False
    lease = None
    try:
        if download:
            if background:
                if background_downloads is None:
                    raise RuntimeError('Background download limiter is not configured')
                await background_downloads.acquire()
                background_download_held = True
            await downloads.acquire()
            download_held = True
        await extraction.acquire()
        lease = ExtractionLease(extraction)
        yield lease
    finally:
        if lease is not None:
            await lease.close()
        if download_held:
            downloads.release()
        if background_download_held:
            background_downloads.release()


def publish_download_started(data, directory):
    """Only actual bytes in this child's owned file release the extraction slot."""
    if data.get('status') not in {'downloading', 'finished'}:
        return
    if int(data.get('downloaded_bytes') or 0) < 8192:
        return
    directory = Path(directory).resolve()
    marker = directory / DOWNLOAD_STARTED_FILE
    if marker.is_file():
        return
    source = Path(str(data.get('tmpfilename') or data.get('filename') or ''))
    try:
        ready = (source.is_file() and source.resolve().parent == directory
                 and source.stat().st_size >= 8192)
    except FileNotFoundError:
        return
    if ready:
        marker.touch(mode=0o600, exist_ok=True)
