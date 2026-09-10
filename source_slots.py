"""Serialize extraction without serializing already-started source downloads."""
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
async def source_slot(extraction, downloads, *, download):
    """Download capacity remains owned until child cleanup, including cancellation."""
    download_held = False
    lease = None
    try:
        if download:
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
        return  # A finished download can be renamed between progress callbacks.
    if ready:
        marker.touch(mode=0o600, exist_ok=True)
