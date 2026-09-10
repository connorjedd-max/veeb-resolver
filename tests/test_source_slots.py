"""Queue ownership and session policy; all source traffic is local or mocked."""
import asyncio
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

from source_slots import source_slot, publish_download_started, DOWNLOAD_STARTED_FILE
from source_support import SourceAttemptError
from test_pipeline import load_core


class SourceSlotTests(unittest.IsolatedAsyncioTestCase):
    async def test_next_extraction_starts_before_previous_download_finishes(self):
        extraction, downloads = asyncio.Semaphore(1), asyncio.Semaphore(2)
        with tempfile.TemporaryDirectory() as directory:
            async with source_slot(extraction, downloads, download=True) as first:
                first.watch(directory)
                acquired = asyncio.Event()
                async def second():
                    async with source_slot(extraction, downloads, download=True):
                        acquired.set()
                task = asyncio.create_task(second())
                await asyncio.sleep(.04)
                self.assertFalse(acquired.is_set())
                source = Path(directory) / 'audio.webm.part'
                source.write_bytes(b'x' * 16384)
                publish_download_started({'status': 'downloading', 'downloaded_bytes': 16384,
                                          'tmpfilename': str(source)}, directory)
                await asyncio.wait_for(acquired.wait(), 1)
                await task
                # The first download is still active here, with its download
                # permit retained even though another extraction has run.
                self.assertEqual(downloads._value, 1)
            self.assertEqual(extraction._value, 1)
            self.assertEqual(downloads._value, 2)

    async def test_download_capacity_remains_bounded_after_extraction_release(self):
        extraction, downloads = asyncio.Semaphore(1), asyncio.Semaphore(2)
        async with source_slot(extraction, downloads, download=True) as first:
            first.release()
            async with source_slot(extraction, downloads, download=True) as second:
                second.release()
                entered = asyncio.Event()
                async def third():
                    async with source_slot(extraction, downloads, download=True):
                        entered.set()
                task = asyncio.create_task(third())
                await asyncio.sleep(.04)
                self.assertFalse(entered.is_set())
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.assertEqual(downloads._value, 0)
        self.assertEqual(downloads._value, 2)
        self.assertEqual(extraction._value, 1)

    async def test_cancel_while_waiting_for_extraction_returns_only_owned_capacity(self):
        extraction, downloads = asyncio.Semaphore(0), asyncio.Semaphore(2)
        async def queued():
            async with source_slot(extraction, downloads, download=True):
                self.fail('No extraction permit exists')
        task = asyncio.create_task(queued())
        await asyncio.sleep(.02)
        self.assertEqual(downloads._value, 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(downloads._value, 2)
        self.assertEqual(extraction._value, 0)

    async def test_marker_requires_owned_source_bytes(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            marker = Path(directory) / DOWNLOAD_STARTED_FILE
            source = Path(directory) / 'source.part'
            source.write_bytes(b'x')
            data = {'status': 'downloading', 'downloaded_bytes': 16384, 'tmpfilename': str(source)}
            publish_download_started(data, directory)
            self.assertFalse(marker.exists())
            foreign = Path(outside) / 'source.part'
            foreign.write_bytes(b'x' * 16384)
            publish_download_started(dict(data, tmpfilename=str(foreign)), directory)
            self.assertFalse(marker.exists())
            source.write_bytes(b'x' * 16384)
            publish_download_started(data, directory)
            self.assertTrue(marker.is_file())

    async def test_real_child_download_does_not_block_second_child_extraction(self):
        core = load_core()
        core.init_ytdlp_pools()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'extract_source.py'
            script.write_text('import sys\nsys.path.insert(0, ' + repr(str(Path(__file__).resolve().parents[1])) + ')\n' + '''import json,sys,time
from pathlib import Path
from source_slots import publish_download_started
p=json.load(sys.stdin)
root=Path(__file__).parent
out=Path(p['downloadDirectory']);out.mkdir(exist_ok=True)
f=out/'source.webm';f.write_bytes(b'x'*16384)
publish_download_started({'status':'downloading','downloaded_bytes':16384,'tmpfilename':str(f)},out)
(root/p['videoId']).touch()
if p['videoId']=='first_track':
 while not (root/'release').exists():time.sleep(.01)
print(json.dumps({'ok':True,'media':{'local_path':str(f)}}))
''')
            tasks = []
            with patch.object(core, '__file__', str(root/'veeb_resolver.py')):
                try:
                    first = asyncio.create_task(core._fg_pot_pool._child(
                        'first_track', download=True, timeout=5, directory=str(root/'one')))
                    tasks.append(first)
                    for _ in range(300):
                        if (root/'first_track').exists(): break
                        await asyncio.sleep(.01)
                    self.assertTrue((root/'first_track').exists())
                    second = asyncio.create_task(core._fg_pot_pool._child(
                        'second_song', download=True, timeout=5, directory=str(root/'two')))
                    tasks.append(second)
                    result = await asyncio.wait_for(asyncio.shield(second), 2)
                    self.assertTrue(result['ok'])
                    self.assertFalse(first.done(), 'First source must remain in progress')
                    (root/'release').touch()
                    await first
                finally:
                    for task in tasks:
                        if not task.done(): task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await core._media_jobs.close()
            self.assertFalse(core._extraction_sem.locked())
            self.assertEqual(core._source_download_sem._value, core.MP3_MAX_CONCURRENT_TRANSCODES)


class SessionPriorityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.core = load_core()
        self.core.init_ytdlp_pools()

    async def asyncTearDown(self):
        await self.core._media_jobs.close()

    async def test_recognised_session_uses_authenticated_mweb_first(self):
        c = self.core
        with patch.object(c, 'get_writable_cookie_file', return_value='/private/cookies'), \
             patch.object(c, '_last_cookie_session_test', {'youtubeReportsLoggedIn': True}):
            self.assertIs(c.foreground_pools()[0], c._fg_mweb_auth_pool)

    async def test_negative_session_skips_cookie_attempts_and_explains_failure(self):
        c = self.core
        with patch.object(c, 'get_writable_cookie_file', return_value='/private/cookies'), \
             patch.object(c, '_last_cookie_session_test', {'youtubeReportsLoggedIn': False}), \
             patch.object(c._fg_pot_pool, 'stream_source', AsyncMock(side_effect=SourceAttemptError('not a bot'))), \
             patch.object(c._fg_anon_pool, 'stream_source', AsyncMock(side_effect=SourceAttemptError('not a bot'))), \
             patch.object(c._fg_mweb_auth_pool, 'stream_source', AsyncMock(side_effect=AssertionError('known rejected session'))) as auth:
            self.assertEqual(c.foreground_pools(), [c._fg_pot_pool, c._fg_anon_pool])
            with self.assertRaises(c.JobError) as failure:
                await c.produce_mp3('siRAwwaNc1M', None, types.SimpleNamespace(metadata={}))
            self.assertEqual(failure.exception.code, 'SOURCE_ACCESS_DENIED')
            self.assertIn('not logged in', str(failure.exception))
            auth.assert_not_awaited()

    async def test_cookie_secret_change_reenables_auth_without_restart(self):
        c = self.core
        def cookies(value):
            return '# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\t'+value+'\n'
        with tempfile.TemporaryDirectory() as directory:
            source, runtime = Path(directory)/'secret', Path(directory)/'runtime'
            source.write_text(cookies('old'))
            with patch.object(c, 'YOUTUBE_COOKIE_FILE', str(source)), \
                 patch.object(c, 'WRITABLE_COOKIE_FILE', str(runtime)):
                c.get_writable_cookie_file()
                c._last_cookie_session_test = {'youtubeReportsLoggedIn': False}
                self.assertFalse(any(p.use_cookies for p in c.foreground_pools()))
                source.write_text(cookies('new'))
                self.assertIs(c.foreground_pools()[0], c._fg_mweb_auth_pool)
                self.assertIsNone(c._last_cookie_session_test['youtubeReportsLoggedIn'])
