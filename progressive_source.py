"""Tail an owned yt-dlp WebM download without treating temporary EOF as success.

Only direct, audio-only WebM is eligible. MP4 and segmented sources keep the
seekable completed-file path. The downloader still owns source access, retries,
session and validation; its final result is mandatory before MP3 completion.
"""
import asyncio
import json
from pathlib import Path
import shutil
import tempfile

PROGRESS_FILE = '.veeb-progress.json'


class ProgressiveSource:
    def __init__(self, download, make_media):
        self.directory = Path(tempfile.mkdtemp(prefix='veeb-ytdlp-source-'))
        self.task = asyncio.create_task(download(str(self.directory)))
        self.make_media = make_media
        self.file = None
        self.closed = False

    async def start(self):
        try:
            while not self.task.done():
                try:
                    info = json.loads((self.directory / PROGRESS_FILE).read_text())
                except (OSError, ValueError):
                    info = None
                if info and info.get('ext') == 'webm' and info.get('vcodec') == 'none':
                    path = Path(info['part_path'])
                    if path.resolve().parent != self.directory.resolve():
                        raise RuntimeError('Progress source is outside its owned directory')
                    # yt-dlp may rename the file between publication and open.
                    for candidate in (path, path.with_suffix('') if path.suffix == '.part' else path):
                        try:
                            self.file = candidate.open('rb', buffering=0)
                            break
                        except FileNotFoundError:
                            pass
                    if self.file is not None:
                        media = self.make_media(info)
                        media._owned_download = self
                        return media
                await asyncio.sleep(.02)
            # A fast or unstreamable download is already a validated local file.
            media = self.task.result()
            (self.directory / PROGRESS_FILE).unlink(missing_ok=True)
            return media
        except BaseException:
            await self.close()
            raise

    async def feed(self, writer):
        try:
            while True:
                complete = self.task.done()
                if complete:
                    self.task.result()  # A failed download can never become clean EOF.
                chunk = self.file.read(16 * 1024)
                if chunk:
                    writer.write(chunk)
                    await writer.drain()
                elif complete:
                    break
                else:
                    await asyncio.sleep(.02)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

    async def close(self):
        if self.closed:
            return
        self.closed = True
        if not self.task.done():
            self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        if self.file:
            self.file.close()
        shutil.rmtree(self.directory, ignore_errors=True)


def publish_streamable_progress(data, directory):
    """Called by the actual yt-dlp hook. No URLs or credentials in the sidecar."""
    marker = Path(directory) / PROGRESS_FILE
    if marker.exists() or int(data.get('downloaded_bytes') or 0) < 8192:
        return
    info = data.get('info_dict') or {}
    if (info.get('ext') != 'webm' or info.get('vcodec') != 'none'
            or info.get('protocol') not in {'http', 'https'}
            or not 1 <= float(info.get('duration') or 0) <= 1800):
        return
    part = Path(str(data.get('tmpfilename') or ''))
    if not part.is_file() or part.resolve().parent != Path(directory).resolve():
        return
    fields = ('format_id', 'ext', 'container', 'acodec', 'vcodec', 'abr', 'duration', 'title')
    progress = {key: info.get(key) for key in fields}
    progress['part_path'] = str(part)
    temporary = marker.with_suffix('.tmp')
    temporary.write_text(json.dumps(progress))
    temporary.chmod(0o600)
    temporary.replace(marker)
