"""Real yt-dlp HTTP downloader + real FFmpeg, no external music/source requests."""
import asyncio
from functools import partial
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from test_pipeline import load_core
from media_jobs import MediaJobs, mp3_frames_valid
from progressive_source import ProgressiveSource, publish_streamable_progress, PROGRESS_FILE


class ProgressiveTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.webm = Path(cls.tmp.name) / 'tone.webm'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=660:duration=20',
            '-c:a','libopus','-b:a','128k','-cluster_time_limit','250','-y',str(cls.webm)], check=True)
        cls.data = cls.webm.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    async def asyncSetUp(self):
        self.core = load_core()
        self.managers = [self.core._media_jobs]
        self.release = asyncio.Event()

    async def asyncTearDown(self):
        self.release.set()
        for manager in self.managers:
            await manager.close()

    async def run_http_case(self, fail=False):
        import yt_dlp
        core = self.core
        info = {'id':'siRAwwaNc1M','title':'Synthetic test tone','ext':'webm',
                'format_id':'fixture','acodec':'opus','vcodec':'none','duration':20,
                'abr':128,'protocol':'http'}
        async def http(reader, writer):
            try:
                await reader.readuntil(b'\r\n\r\n')
                writer.write(f'HTTP/1.1 200 OK\r\nContent-Type: audio/webm\r\nContent-Length: {len(self.data)}\r\nConnection: close\r\n\r\n'.encode())
                writer.write(self.data[:48000]); await writer.drain()
                await self.release.wait()
                if not fail:
                    writer.write(self.data[48000:]); await writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                pass
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(http,'127.0.0.1',0)
        info['url'] = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/tone.webm'
        download_finished = asyncio.Event()
        async def download(directory):
            def actual_download():
                with yt_dlp.YoutubeDL({'quiet':True, 'no_warnings':True, 'retries':0,
                    'outtmpl':str(Path(directory)/'%(id)s.%(ext)s'),
                    'progress_hooks':[partial(publish_streamable_progress, directory=directory)]}) as ydl:
                    ydl.process_info(dict(info))
            await asyncio.to_thread(actual_download)
            path = Path(directory)/'siRAwwaNc1M.webm'
            await core.probe_local_audio(path)
            media = core.YtdlpEnginePool('fixture',1,'fixture','fixture')._media(
                'siRAwwaNc1M', {**info,'local_path':str(path)},resolver_path='fixture')
            download_finished.set()
            return media
        owned = ProgressiveSource(download, lambda progress: core.YtdlpEnginePool('fixture',1,'fixture','fixture')._media(
            'siRAwwaNc1M', progress, resolver_path='fixture-progressive'))
        async def produce(video_id, request, job):
            media = await owned.start()
            media._output_bitrate = 128
            job.metadata['media'] = media
            return await core.prepare_live_mp3_stream(media, video_id, request)
        manager = MediaJobs(produce)
        self.managers.append(manager)
        request = core.Request({'type':'http','method':'GET','headers':[]})
        start = time.monotonic()
        job = manager.get('siRAwwaNc1M', request)
        try:
            await asyncio.wait_for(manager.wait(job), 4)
            first_ms = round((time.monotonic()-start)*1000)
            self.assertFalse(download_finished.is_set(), 'Playback must begin before the source finishes')
            self.assertFalse(job.done.is_set())
            self.assertGreaterEqual(job.size,4096)
            reader = manager.read(job)
            first = await anext(reader)
            self.assertTrue(mp3_frames_valid(first))
            self.release.set()
            if fail:
                with self.assertRaises(Exception):
                    await manager.wait(job,complete=True)
                self.assertFalse(job.path.exists())
                self.assertEqual(job.size,0)
                await reader.aclose()
            else:
                output = first + b''.join([chunk async for chunk in reader])
                await manager.wait(job,complete=True)
                self.assertAlmostEqual(job.metadata['validatedDuration'],20,delta=.2)
                subprocess.run(['ffmpeg','-v','error','-f','mp3','-i','pipe:0','-f','null','-'],input=output,check=True)
                print(f'Progressive fixture: first MP3 bytes in {first_ms} ms, source still held open; complete MP3 {len(output)} bytes')
            self.assertFalse(owned.directory.exists())
            return first_ms
        finally:
            self.release.set()
            await owned.close()
            server.close(); await server.wait_closed()

    async def test_real_downloader_starts_mp3_before_source_eof(self):
        self.assertLess(await self.run_http_case(), 2000)

    async def test_late_download_failure_never_publishes_complete_mp3(self):
        await self.run_http_case(fail=True)

    async def test_cancel_reaps_owned_download_while_waiting_for_encoder_slot(self):
        stopped = asyncio.Event()
        async def download(directory):
            try:
                path=Path(directory)/'source.webm.part'; path.write_bytes(self.data[:48000])
                publish_streamable_progress({'downloaded_bytes':48000,'tmpfilename':str(path),
                    'info_dict':{'ext':'webm','vcodec':'none','acodec':'opus','protocol':'http','duration':20}}, directory)
                await asyncio.Event().wait()
            finally:
                stopped.set()
        owned=ProgressiveSource(download, lambda info:self.core.YtdlpEnginePool('f',1,'f','f')._media('siRAwwaNc1M',info,resolver_path='f'))
        media=await owned.start()
        with patch.object(self.core,'_mp3_transcode_sem',asyncio.Semaphore(0)):
            task=asyncio.create_task(self.core.prepare_live_mp3_stream(media,'siRAwwaNc1M',self.core.Request({'method':'GET','headers':[]})))
            await asyncio.sleep(.02); task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(stopped.is_set())
        self.assertFalse(owned.directory.exists())

    async def test_mp4_and_fragmented_sources_do_not_take_pipe_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'source.part'; path.write_bytes(b'x'*9000)
            for ext,protocol in [('m4a','http'),('webm','m3u8_native'),('webm','http_dash_segments')]:
                publish_streamable_progress({'downloaded_bytes':9000,'tmpfilename':str(path),
                    'info_dict':{'ext':ext,'protocol':protocol,'vcodec':'none','duration':20}},directory)
                self.assertFalse((Path(directory)/PROGRESS_FILE).exists())

    async def test_cancel_during_progressive_playback_closes_the_downloader(self):
        stopped = asyncio.Event()
        async def download(directory):
            try:
                path=Path(directory)/'source.webm.part'; path.write_bytes(self.data[:48000])
                publish_streamable_progress({'downloaded_bytes':48000,'tmpfilename':str(path),
                    'info_dict':{'ext':'webm','vcodec':'none','acodec':'opus','protocol':'http','duration':20}}, directory)
                await asyncio.Event().wait()
            finally:
                stopped.set()
        owned=ProgressiveSource(download, lambda info:self.core.YtdlpEnginePool('f',1,'f','f')._media('siRAwwaNc1M',info,resolver_path='f'))
        media=await owned.start()
        body=await self.core.prepare_live_mp3_stream(media,'siRAwwaNc1M',self.core.Request({'method':'GET','headers':[]}))
        self.assertTrue(mp3_frames_valid(await anext(body)))
        await body.aclose()
        self.assertTrue(stopped.is_set())
        self.assertFalse(owned.directory.exists())
        self.assertFalse(self.core._mp3_transcode_sem.locked())
