"""Regression tests for concrete v38.3 failure modes, with real media bytes."""
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import veeb_resolver as server
from acquisition_guard import AcquisitionGuard
from media_jobs import JobError, MediaJobs
from mp3_validation import MP3Validator
import extract_source

VIDEO = 'siRAwwaNc1M'


class GuardTests(unittest.TestCase):
    def test_one_private_or_challenged_track_does_not_disable_every_track(self):
        guard = AcquisitionGuard()
        guard.failed(VIDEO, {'SOURCE_ACCESS_DENIED'})
        guard.failed(VIDEO, {'SOURCE_ACCESS_DENIED'})
        guard.check()
        guard.failed('EbVb_qE_T5o', {'SOURCE_FORMAT_UNAVAILABLE'})
        guard.check()
        guard.failed('C2elY5Tctqg', {'SOURCE_ACCESS_DENIED'})
        with self.assertRaises(JobError) as result:
            guard.check()
        self.assertGreater(result.exception.retry_after, 0)
        guard.succeeded()
        guard.check()

    def test_cooldown_expires(self):
        now = [0]
        guard = AcquisitionGuard(clock=lambda: now[0])
        for video in (VIDEO, 'C2elY5Tctqg'):
            guard.failed(video, {'MEDIA_HTTP_403'})
        now[0] = 121
        guard.check()

    def test_proxy_credentials_never_appear_in_diagnostics(self):
        from source_support import redact
        self.assertNotIn('private-password', redact('socks5h://user:private-password@127.0.0.1:123'))

    def test_proxy_is_explicit_and_invalid_configuration_is_not_silently_ignored(self):
        with patch.object(server, 'YOUTUBE_PROXY_URL', 'socks5h://user:secret@host.test:1080'):
            self.assertEqual(server.ytdlp_options('mweb', None)['proxy'], server.YOUTUBE_PROXY_URL)
        with patch.object(server, 'YOUTUBE_PROXY_URL', 'pac+http://host.test/config'):
            with self.assertRaises(server.SourceAttemptError):
                server.ytdlp_options('mweb', None)

    def test_real_ytdlp_aborts_missing_hls_fragment_instead_of_publishing_partial_source(self):
        import http.server
        import threading
        import yt_dlp
        original = yt_dlp.YoutubeDL
        segment = subprocess.check_output(['ffmpeg','-v','error','-f','lavfi','-i',
            'sine=frequency=440:duration=1','-c:a','aac','-f','mpegts','pipe:1'])
        manifest = b'#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:1,\nsegment.ts\n#EXTINF:1,\nmissing.ts\n#EXTINF:1,\nsegment.ts\n#EXT-X-ENDLIST\n'
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                if self.path.endswith('.m3u8'):
                    body, kind = manifest, 'application/vnd.apple.mpegurl'
                elif self.path == '/segment.ts':
                    body, kind = segment, 'video/mp2t'
                else:
                    self.send_error(404); return
                self.send_response(200); self.send_header('Content-Type',kind)
                self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
        host = http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread = threading.Thread(target=host.serve_forever,daemon=True); thread.start()
        force_old_skip = [False]
        class LocalFixtureYoutubeDL(original):
            def __init__(self, options):
                options['proxy'] = ''  # Loopback fixture, never a public request.
                if force_old_skip[0]: options['skip_unavailable_fragments'] = True
                super().__init__(options)
            def extract_info(self, url, *args, **kwargs):
                return super().extract_info(f'http://127.0.0.1:{host.server_port}/{VIDEO}.m3u8', *args, **kwargs)
        try:
            with tempfile.TemporaryDirectory() as tmp, patch.object(yt_dlp, 'YoutubeDL', LocalFixtureYoutubeDL):
                owned = Path(tmp)/'source'
                payload = {'videoId':VIDEO,'options':{},'download':True,'downloadDirectory':str(owned)}
                result = extract_source.run(payload)
                self.assertFalse(result['ok'],result)
                self.assertEqual(result['stage'],'download')
                self.assertFalse(owned.exists())
                # Reproduce the old bug with the upstream skip default: a missing
                # middle fragment still becomes a supposedly completed source.
                force_old_skip[0] = True
                old = extract_source.run(payload)
                self.assertTrue(old['ok'],old)
                self.assertTrue(old['evidence']['downloadCompleted'])
        finally:
            host.shutdown(); host.server_close(); thread.join()


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.mp3 = subprocess.check_output(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
            'sine=frequency=600:duration=3', '-c:a', 'libmp3lame', '-b:a', '128k', '-ar', '44100',
            '-ac', '2', '-id3v2_version', '0', '-write_xing', '0', '-f', 'mp3', 'pipe:1'])

    async def test_entire_mp3_not_just_startup_is_validated(self):
        validator = MP3Validator()
        for pos in range(0, len(self.mp3), 127):
            validator.feed(self.mp3[pos:pos+127])
        self.assertAlmostEqual(validator.finish(), 3.056, delta=.05)
        validator = MP3Validator()
        validator.feed(self.mp3[:-7])
        with self.assertRaises(JobError): validator.finish()
        bad = bytearray(self.mp3)
        bad[20000:20200] = b'x'*200
        with self.assertRaises(JobError): MP3Validator().feed(bad)

    async def test_truncated_frame_never_becomes_completed_audio(self):
        async def producer(*args):
            async def stream(): yield self.mp3[:-7]
            return stream()
        jobs = MediaJobs(producer)
        try:
            job = jobs.get(VIDEO, None)
            with self.assertRaises(JobError): await jobs.wait(job, complete=True)
            self.assertFalse(job.path.exists())
        finally: await jobs.close()

    async def test_completed_files_are_protected_during_worker_collection_window(self):
        async def producer(*args):
            async def stream(): yield self.mp3
            return stream()
        jobs = MediaJobs(producer, max_jobs=1)
        try:
            job = jobs.get(VIDEO, None)
            await jobs.wait(job, complete=True)
            with self.assertRaises(JobError): jobs.get('C2elY5Tctqg', None)
            self.assertTrue(job.path.exists())
            job.touched = time.monotonic()-91
            replacement = jobs.get('C2elY5Tctqg', None)
            await jobs.wait(replacement, complete=True)
            self.assertFalse(job.path.exists())
        finally: await jobs.close()

    async def test_imported_cache_does_not_claim_youtube_source_access(self):
        headers = {'Authorization': 'Bearer test-only'}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
            with patch.object(server, 'RESOLVER_SECRET', 'test-only'), patch.object(server, '_last_source_success', None):
                response = await client.get('/health', headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertFalse(response.json()['sourceAccessVerified'])

    async def test_resolve_and_stream_share_one_source_download(self):
        calls = []
        async def producer(video, request, job):
            calls.append(video)
            job.metadata.update(media=server.ResolvedMedia(video,'',{},'test','mp3','mp3','audio/mpeg',
                'mp3','none',128,3,'test',time.time(),time.time()+60,'test'), cache='TEST')
            async def stream(): yield self.mp3
            return stream()
        jobs = MediaJobs(producer)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
                with patch.object(server,'_media_jobs',jobs), patch.object(server,'RESOLVER_SECRET','test-only'):
                    headers = {'Authorization':'Bearer test-only'}
                    responses = await asyncio.gather(client.get('/resolve/'+VIDEO,headers=headers),
                                                     client.get('/stream/'+VIDEO,headers=headers))
                    self.assertEqual([r.status_code for r in responses], [200,200])
                    self.assertEqual(calls,[VIDEO])
                    self.assertEqual(responses[1].content,self.mp3)
        finally: await jobs.close()

    async def test_agent_local_copy_reused_without_another_source_download(self):
        import source_agent
        calls = []
        async def producer(video, request, job):
            calls.append(video)
            async def stream(): yield self.mp3
            return stream()
        jobs = MediaJobs(producer)
        try:
            with tempfile.TemporaryDirectory() as tmp, patch.object(source_agent, 'CACHE_DIR', Path(tmp)), \
                    patch.object(source_agent.engine, '_media_jobs', jobs):
                first = await source_agent.acquire_audio(VIDEO,128)
                second = await source_agent.acquire_audio(VIDEO,128)
                self.assertEqual(calls,[VIDEO]); self.assertTrue(second['localCacheHit'])
                self.assertEqual(first['sha256'],second['sha256'])
        finally: await jobs.close()
