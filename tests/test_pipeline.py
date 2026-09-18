"""Offline regression suite: real FFmpeg, synthetic audio, mocked source acquisition.

Loads the production server core without FastAPI/httpx if unavailable in the
validation environment. HTTP framework deployment is checked separately in CI.
"""
import ast
import asyncio
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from media_jobs import MediaJobs, JobError, mp3_frames_valid


def load_core():
    tree = ast.parse((ROOT/'veeb_resolver.py').read_text())
    kept = []
    for n in tree.body:
        if isinstance(n, ast.Import) and any(a.name == 'httpx' for a in n.names):
            continue
        if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith(('fastapi', 'starlette')):
            continue
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'app' for t in n.targets):
            continue
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            n.decorator_list = [d for d in n.decorator_list if not (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and isinstance(d.func.value, ast.Name) and d.func.value.id == 'app')]
        kept.append(n)
    class Response:
        def __init__(self, content=None, status_code=200, headers=None, **kw):
            self.content, self.status_code, self.headers = content, status_code, headers or {}
    class Request:
        def __init__(self, scope):
            self.method = scope['method']
            self.headers = {k.decode():v.decode() for k,v in scope['headers']}
    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            self.status_code, self.detail = status_code, detail
    module = types.ModuleType('resolver_test_core')
    sys.modules[module.__name__] = module
    module.__dict__.update(__file__=str(ROOT/'veeb_resolver.py'), Response=Response,
        JSONResponse=Response, StreamingResponse=Response, Request=Request,
        HTTPException=HTTPException, Header=lambda **k: None, Query=lambda **k: None,
        BackgroundTask=lambda f:f)
    exec(compile(ast.Module(body=kept, type_ignores=[]), str(ROOT/'veeb_resolver.py'), 'exec'), module.__dict__)
    return module


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.fixture = Path(cls.tmp.name)/'source.mp4'
        # MP4 metadata at the end, reproducing the non-seekable-stdin hazard.
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i',
            'sine=frequency=440:duration=3','-c:a','aac','-y',str(cls.fixture)], check=True)
        cls.mp3 = subprocess.check_output(['ffmpeg','-hide_banner','-loglevel','error','-i',str(cls.fixture),
            '-map','0:a:0','-c:a','libmp3lame','-b:a','128k','-ac','2','-ar','44100',
            '-id3v2_version','0','-write_xing','0','-f','mp3','pipe:1'])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    async def asyncSetUp(self):
        self.core = load_core()
        self.managers = [self.core._media_jobs]

    async def asyncTearDown(self):
        for manager in self.managers:
            await manager.close()

    def manager(self, producer, **kw):
        result = MediaJobs(producer, **kw)
        self.managers.append(result)
        return result

    def request(self, method='GET', purpose='playback'):
        return self.core.Request({'type':'http','method':method,'headers':[(b'x-veeb-purpose',purpose.encode())]})

    async def collect(self, manager, job):
        await manager.wait(job)
        return b''.join([chunk async for chunk in manager.read(job)])

    async def test_real_http_source_to_mp3_and_shared_cache_fill(self):
        fixture = self.fixture.read_bytes()
        requests = []
        async def http(reader, writer):
            try:
                req = (await reader.readuntil(b'\r\n\r\n')).decode()
                requests.append(req)
                start, end = 0, len(fixture)-1
                ranged = False
                for line in req.splitlines():
                    if line.lower().startswith('range: bytes='):
                        raw = line.split('=',1)[1].split('-',1)
                        start = int(raw[0]);end = min(end,int(raw[1])) if raw[1] else end;ranged = True
                data = fixture[start:end+1]
                header = f'HTTP/1.1 {"206 Partial Content" if ranged else "200 OK"}\r\nContent-Type: audio/mp4\r\nContent-Length: {len(data)}\r\nAccept-Ranges: bytes\r\nConnection: close\r\n'
                if ranged:header += f'Content-Range: bytes {start}-{end}/{len(fixture)}\r\n'
                writer.write(header.encode()+b'\r\n'+data);await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(http,'127.0.0.1',0)
        url = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/source.mp4'
        media = self.core.ResolvedMedia('siRAwwaNc1M',url,{},'test','140','m4a','audio/mp4','aac','none',128,3,'tone',0,9999999999,'fixture')
        calls = 0
        async def producer(video_id, request, job):
            nonlocal calls
            calls += 1
            job.metadata['media'] = media
            media._output_bitrate = 128
            return await self.core.prepare_live_mp3_stream(media,video_id,request)
        manager = self.manager(producer)
        try:
            job = manager.get('siRAwwaNc1M',self.request())
            self.assertIs(job,manager.get('siRAwwaNc1M',self.request(purpose='cache-fill'),background=True))
            a,b = await asyncio.gather(self.collect(manager,job),self.collect(manager,job))
            await manager.wait(job,complete=True)
            self.assertEqual(a,b); self.assertEqual(calls,1)
            self.assertTrue(mp3_frames_valid(a)); self.assertEqual(len(a),job.size)
            self.assertGreater(len(a),40000); self.assertTrue(requests)
            subprocess.run(['ffmpeg','-v','error','-f','mp3','-i','pipe:0','-f','null','-'],input=a,check=True)
        finally:
            server.close();await server.wait_closed()

    async def test_failed_stream_never_completes_cache(self):
        release = asyncio.Event()
        async def producer(*args):
            async def body():
                yield self.mp3[:20000]
                await release.wait()
                raise RuntimeError('FFmpeg upstream read failed')
            return body()
        manager=self.manager(producer);job=manager.get('siRAwwaNc1M',None)
        await manager.wait(job)
        reader=manager.read(job)
        self.assertEqual(len(await anext(reader)),20000)
        release.set()
        with self.assertRaisesRegex(RuntimeError,'upstream'):
            await manager.wait(job,complete=True)
        with self.assertRaises(RuntimeError):await anext(reader)
        self.assertFalse(job.path.exists())
        self.assertIs(manager.get('siRAwwaNc1M',None),job)

    async def test_invalid_bytes_fail_before_ready(self):
        async def producer(*args):
            async def body():yield b'<html>not mp3</html>'*400
            return body()
        manager=self.manager(producer);job=manager.get('siRAwwaNc1M',None)
        with self.assertRaisesRegex(JobError,'valid MP3'):
            await manager.wait(job)

    async def test_size_limit_cannot_publish_complete(self):
        async def producer(*args):
            async def body():yield self.mp3
            return body()
        manager=self.manager(producer,max_bytes=3000);job=manager.get('siRAwwaNc1M',None)
        with self.assertRaisesRegex(JobError,'size limit'):await manager.wait(job,complete=True)

    async def test_clean_but_short_source_rejected(self):
        async def producer(video_id,request,job):
            job.metadata['media']=types.SimpleNamespace(duration=300,_output_bitrate=128)
            async def body():yield self.mp3
            return body()
        manager=self.manager(producer);job=manager.get('siRAwwaNc1M',None)
        with self.assertRaisesRegex(JobError,'shorter'):await manager.wait(job,complete=True)

    async def test_background_capacity_and_reader_cancellation(self):
        release=asyncio.Event()
        async def producer(*args):
            async def body():
                yield self.mp3[:20000]
                await release.wait()
                yield self.mp3[20000:]
            return body()
        manager=self.manager(producer,max_jobs=4,max_active=3,max_background=2)
        foreground=manager.get('siRAwwaNc1M',None,purpose='playback')
        background=manager.get('dQw4w9WgXcQ',None,background=True,purpose='r2-background-prefetch')
        await asyncio.gather(manager.wait(foreground),manager.wait(background))
        # A second background job is allowed while one foreground job is active,
        # but the configured background cap still prevents a third background job.
        background2=manager.get('C2elY5Tctqg',None,background=True,purpose='r2-library-warm')
        with self.assertRaises(JobError):
            manager.get('EbVb_qE_T5o',None,background=True,purpose='r2-library-warm')
        first=manager.read(foreground);await anext(first);await first.aclose()
        release.set()
        await asyncio.gather(manager.wait(foreground,complete=True),manager.wait(background,complete=True),
                             manager.wait(background2,complete=True))

    async def test_foreground_preempts_background_when_capacity_is_full(self):
        release=asyncio.Event()
        async def producer(*args):
            async def body():
                yield self.mp3[:20000]
                await release.wait()
                yield self.mp3[20000:]
            return body()
        manager=self.manager(producer,max_active=3,max_background=2)
        bg1=manager.get('dQw4w9WgXcQ',None,background=True,purpose='r2-background-prefetch')
        bg2=manager.get('C2elY5Tctqg',None,background=True,purpose='r2-library-warm')
        fg1=manager.get('siRAwwaNc1M',None,purpose='playback')
        await asyncio.gather(manager.wait(bg1),manager.wait(bg2),manager.wait(fg1))
        fg2=manager.get('EbVb_qE_T5o',None,purpose='playback')
        await asyncio.sleep(0)
        preempted=[job for job in (bg1,bg2) if job.preempt_requested]
        self.assertEqual(len(preempted),1)
        await asyncio.sleep(0)
        self.assertTrue(preempted[0].done.is_set())
        self.assertEqual(getattr(preempted[0].error,'code',None),'BACKGROUND_PREEMPTED')
        self.assertEqual(manager.stats()['backgroundPreemptions'],1)
        release.set()
        await asyncio.gather(manager.wait(fg1,complete=True),manager.wait(fg2,complete=True))

    async def test_foreground_preempts_background_source_acquisition_even_with_free_job_capacity(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        active_background = 0

        async def producer(video_id, request, job):
            nonlocal active_background
            if job.background:
                job.metadata['sourceAcquisitionActive'] = True
                active_background += 1
                if active_background >= 2:
                    entered.set()
                try:
                    await release.wait()
                finally:
                    job.metadata['sourceAcquisitionActive'] = False
                async def background_body():
                    yield self.mp3
                return background_body()
            async def foreground_body():
                yield self.mp3
            return foreground_body()

        manager = self.manager(producer, max_active=3, max_background=2)
        bg1 = manager.get('dQw4w9WgXcQ', None, background=True, purpose='r2-library-warm')
        bg2 = manager.get('C2elY5Tctqg', None, background=True, purpose='r2-background-prefetch')
        await asyncio.wait_for(entered.wait(), 1)

        # Capacity is not full: the old logic would let playback start as a job
        # but leave it queued behind the serialized background extraction lane.
        fg = manager.get('siRAwwaNc1M', None, purpose='playback')
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertTrue(bg1.preempt_requested)
        self.assertTrue(bg2.preempt_requested)
        self.assertEqual(manager.stats()['backgroundSourcePreemptions'], 2)
        await manager.wait(fg, complete=True)
        for job in (bg1, bg2):
            self.assertTrue(job.done.is_set())
            self.assertEqual(getattr(job.error, 'code', None), 'BACKGROUND_PREEMPTED')
        release.set()

    async def test_foreground_does_not_preempt_background_after_source_bytes_are_published(self):
        release = asyncio.Event()
        async def producer(video_id, request, job):
            job.metadata['sourceAcquisitionActive'] = False
            async def body():
                yield self.mp3[:20000]
                if job.background:
                    await release.wait()
                    yield self.mp3[20000:]
                else:
                    yield self.mp3[20000:]
            return body()

        manager = self.manager(producer, max_active=3, max_background=2)
        bg = manager.get('dQw4w9WgXcQ', None, background=True, purpose='r2-library-warm')
        await manager.wait(bg)
        fg = manager.get('siRAwwaNc1M', None, purpose='playback')
        await asyncio.sleep(0)
        self.assertFalse(bg.preempt_requested)
        self.assertEqual(manager.stats()['backgroundSourcePreemptions'], 0)
        await manager.wait(fg, complete=True)
        release.set()
        await manager.wait(bg, complete=True)

    async def test_foreground_claim_of_same_background_source_preempts_other_blocker_not_itself(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        count = 0
        async def producer(video_id, request, job):
            nonlocal count
            if job.background:
                job.metadata['sourceAcquisitionActive'] = True
                count += 1
                if count >= 2:
                    entered.set()
                try:
                    await release.wait()
                finally:
                    job.metadata['sourceAcquisitionActive'] = False
            async def body():
                yield self.mp3
            return body()

        manager = self.manager(producer, max_active=3, max_background=2)
        same = manager.get('siRAwwaNc1M', None, background=True, purpose='r2-next-prefetch')
        other = manager.get('C2elY5Tctqg', None, background=True, purpose='r2-library-warm')
        await asyncio.wait_for(entered.wait(), 1)
        claimed = manager.get('siRAwwaNc1M', None, background=False, purpose='playback')
        await asyncio.sleep(0)
        self.assertIs(claimed, same)
        self.assertFalse(same.background)
        self.assertFalse(same.preempt_requested)
        self.assertTrue(other.preempt_requested)
        release.set()
        await manager.wait(same, complete=True)

    async def test_next_prefetch_preempts_lower_priority_library_warm(self):
        release=asyncio.Event()
        async def producer(*args):
            async def body():
                yield self.mp3[:20000]
                await release.wait()
                yield self.mp3[20000:]
            return body()
        manager=self.manager(producer,max_active=3,max_background=2)
        low1=manager.get('dQw4w9WgXcQ',None,background=True,purpose='r2-library-warm')
        low2=manager.get('C2elY5Tctqg',None,background=True,purpose='r2-library-warm')
        await asyncio.gather(manager.wait(low1),manager.wait(low2))
        high=manager.get('EbVb_qE_T5o',None,background=True,purpose='r2-next-prefetch')
        await asyncio.sleep(0)
        self.assertEqual(sum(job.preempt_requested for job in (low1,low2)),1)
        self.assertEqual(high.priority,70)
        release.set()
        await manager.wait(high,complete=True)

    async def test_foreground_claims_existing_background_job(self):
        release=asyncio.Event()
        async def producer(*args):
            async def body():
                yield self.mp3[:20000]
                await release.wait()
                yield self.mp3[20000:]
            return body()
        manager=self.manager(producer,max_active=3,max_background=2)
        job=manager.get('siRAwwaNc1M',None,background=True,purpose='r2-background-prefetch')
        same=manager.get('siRAwwaNc1M',None,background=False,purpose='playback')
        self.assertIs(job,same)
        self.assertFalse(job.background)
        self.assertEqual(job.purpose,'playback')
        release.set()
        await manager.wait(job,complete=True)

    async def test_timeout_cleans_producer(self):
        cleaned=asyncio.Event()
        async def producer(*args):
            try:await asyncio.Event().wait()
            finally:cleaned.set()
        manager=self.manager(producer,timeout=0.02);job=manager.get('siRAwwaNc1M',None)
        with self.assertRaises(TimeoutError):await manager.wait(job,complete=True)
        self.assertTrue(cleaned.is_set());self.assertFalse(job.path.exists())

    async def test_cookie_modes_and_manifest_options(self):
        core=self.core
        with patch.object(core,'get_writable_cookie_file',return_value='/private/cookies'):
            for client in ('anonymous','mweb','android','visionos'):
                opts=core.ytdlp_options(client,None)
                self.assertNotIn('cookiefile',opts)
                self.assertNotIn('skip',opts['extractor_args']['youtube'])
                if client in ('android','visionos'):
                    self.assertEqual(opts['extractor_args']['youtube']['player_skip'],['webpage'])
                else:
                    self.assertNotIn('player_skip',opts['extractor_args']['youtube'])
                self.assertFalse(opts['check_formats'])
            self.assertEqual(core.ytdlp_options('android',None)['format'].split('/')[0],'18')
            self.assertEqual(core.ytdlp_options('android',None)['extractor_args']['youtube']['player_client'],['android'])
            self.assertEqual(core.ytdlp_options('default',None,True)['cookiefile'],'/private/cookies')
            self.assertNotIn('player_client',core.ytdlp_options('anonymous',None)['extractor_args']['youtube'])

    async def test_anonymous_source_is_owned_by_ytdlp_from_start(self):
        core=self.core; core.init_ytdlp_pools()
        downloaded=types.SimpleNamespace(client='mweb',video_id='siRAwwaNc1M',format_id='251')
        job=types.SimpleNamespace(metadata={}); iterator=object()
        with patch.object(core._fg_pot_pool,'resolve',AsyncMock(side_effect=AssertionError('must not export a signed URL'))) as resolve, \
             patch.object(core._fg_pot_pool,'download_source',AsyncMock(return_value=downloaded)) as download, \
             patch.object(core,'foreground_pools',return_value=[core._fg_pot_pool]), \
             patch.object(core,'prepare_live_mp3_stream',AsyncMock(return_value=iterator)) as prepare:
            result=await core.produce_mp3('siRAwwaNc1M',self.request(),job)
        self.assertIs(result,iterator); prepare.assert_awaited_once()
        download.assert_awaited_once(); resolve.assert_not_awaited()
        self.assertEqual(job.metadata['cache'],'MISS-DOWNLOADED')

    async def test_producer_marks_only_the_source_acquisition_interval(self):
        c = self.core
        observed = []
        class FakePool:
            name = 'fake'
            client_name = 'mweb'
            use_cookies = True
            async def stream_source(self, video_id, purpose):
                observed.append(bool(job.metadata.get('sourceAcquisitionActive')))
                raise c.SourceAttemptError('synthetic unavailable', code='SOURCE_VIDEO_UNAVAILABLE')

        job = types.SimpleNamespace(metadata={}, job_id='test-job')
        with patch.object(c, 'foreground_pools', return_value=[FakePool()]),              patch.object(c._source_guard, 'check'),              patch.object(c._source_guard, 'failed'):
            with self.assertRaises(c.JobError):
                await c.produce_mp3('siRAwwaNc1M', self.request(purpose='r2-library-warm'), job)
        self.assertEqual(observed, [True])
        self.assertFalse(job.metadata.get('sourceAcquisitionActive'))

    async def test_foreground_order_includes_explicit_authenticated_mweb(self):
        core=self.core; core.init_ytdlp_pools(); order=[]
        async def anon_fail(*a,**k):
            order.append('mweb-anon'); raise RuntimeError('no source')
        async def auth_fail(*a,**k):
            order.append('mweb-auth'); raise RuntimeError('no source')
        media=types.SimpleNamespace(client='anon',video_id='siRAwwaNc1M',expires_at=9999999999,valid=lambda:True)
        with patch.object(core._fg_pot_pool,'resolve',AsyncMock(side_effect=anon_fail)), \
             patch.object(core._fg_mweb_auth_pool,'resolve',AsyncMock(side_effect=auth_fail)), \
             patch.object(core._fg_anon_pool,'resolve',AsyncMock(return_value=media)), \
             patch.object(core,'get_writable_cookie_file',return_value='/private/cookies'):
            result=await core.resolve_ytdlp_foreground_v35('siRAwwaNc1M','live')
        self.assertIs(result,media); self.assertEqual(order,['mweb-auth','mweb-anon'])
        self.assertFalse(core._fg_pot_pool.use_cookies)
        self.assertTrue(core._fg_mweb_auth_pool.use_cookies)

    async def test_local_downloaded_source_transcodes_and_is_deleted(self):
        core=self.core
        with tempfile.TemporaryDirectory() as tmp:
            source_dir=Path(tmp)/'owned'
            source_dir.mkdir()
            source=source_dir/'source.mp4'
            source.write_bytes(self.fixture.read_bytes())
            media=core.ResolvedMedia('siRAwwaNc1M','',{},'visionos','140','m4a','audio/mp4','aac','none',128,3,'tone',0,9999999999,'fixture-local')
            media._local_path=str(source)
            body=await core.prepare_live_mp3_stream(media,'siRAwwaNc1M',self.request())
            data=b''.join([chunk async for chunk in body])
            self.assertTrue(mp3_frames_valid(data))
            self.assertFalse(source.exists())

    async def test_head_does_not_launch_job(self):
        response=await self.core.proxy_media(self.request('HEAD'),'siRAwwaNc1M')
        self.assertEqual(response.status_code,200);self.assertEqual(len(self.core._media_jobs.jobs),0)

    async def test_extraction_cancel_kills_child_and_releases_slot(self):
        core=self.core;core.init_ytdlp_pools()
        with tempfile.TemporaryDirectory() as tmp:
            script=Path(tmp)/'extract_source.py'
            script.write_text('import time\ntime.sleep(60)\n')
            with patch.object(core,'__file__',str(Path(tmp)/'veeb_resolver.py')):
                task=asyncio.create_task(core._fg_anon_pool.resolve('siRAwwaNc1M','live'))
                # Wait until the global slot is occupied, then allow spawn/pipe setup.
                await asyncio.sleep(0.10)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
            self.assertFalse(core._extraction_sem.locked())

    async def test_r2_background_prefetch_is_classified_as_background(self):
        core=self.core
        self.assertTrue(core.is_background_purpose('r2-background-prefetch'))
        self.assertTrue(core.is_background_purpose('r2-library-warm'))
        self.assertTrue(core.is_background_purpose('r2-next-prefetch'))
        self.assertFalse(core.is_background_purpose('playback'))

    async def test_cache_response_is_finite_and_complete(self):
        core=self.core
        media=types.SimpleNamespace(client='test',format_id='140',resolver_path='fixture')
        async def producer(video_id,request,job):
            job.metadata={'media':media,'cache':'MISS'}
            async def body():yield self.mp3
            return body()
        manager=self.manager(producer)
        with patch.object(core,'_media_jobs',manager):
            response=await core.proxy_media(self.request(purpose='cache-fill'),'siRAwwaNc1M')
            self.assertEqual(response.headers['Content-Length'],str(len(self.mp3)))
            self.assertEqual(response.headers['X-Veeb-MP3-Complete'],'1')
            self.assertEqual(b''.join([x async for x in response.content]),self.mp3)

    async def test_playback_response_exposes_startup_telemetry_without_changing_startup_bytes(self):
        core=self.core
        media=types.SimpleNamespace(client='test',format_id='251',resolver_path='fixture-progressive')
        async def producer(video_id,request,job):
            core.mark_job_timing(job,'producer_start')
            await asyncio.sleep(.002)
            core.mark_job_timing(job,'source_attempt_start',overwrite=True)
            await asyncio.sleep(.002)
            core.mark_job_timing(job,'source_ready',overwrite=True)
            core.mark_job_timing(job,'ffmpeg_spawn_start',overwrite=True)
            await asyncio.sleep(.002)
            core.mark_job_timing(job,'mp3_first_bytes',overwrite=True)
            job.metadata.update(media=media,cache='MISS-PROGRESSIVE')
            async def body():yield self.mp3
            return body()
        manager=self.manager(producer)
        with patch.object(core,'_media_jobs',manager):
            response=await core.proxy_media(self.request(),'siRAwwaNc1M')
            self.assertEqual(response.headers['X-Veeb-MP3-Startup-Bytes'],str(core.MP3_STARTUP_MIN_BYTES))
            self.assertEqual(response.headers['X-Veeb-Startup-SLA-Ms'],str(core.PLAYBACK_START_SLA_MS))
            self.assertIn('X-Veeb-Startup-Total-Ms',response.headers)
            self.assertIn('X-Veeb-Startup-Source-Ms',response.headers)
            self.assertIn('X-Veeb-Startup-Encoder-Ms',response.headers)
            self.assertRegex(response.headers['X-Veeb-MP3-Job'],r'^[a-f0-9]{12}$')
            self.assertEqual(b''.join([x async for x in response.content]),self.mp3)

    async def test_job_status_reports_stream_health_without_exposing_media_url(self):
        core=self.core
        media=types.SimpleNamespace(client='mweb',format_id='251',resolver_path='fixture-progressive',url='https://secret.example/media?sig=private')
        async def producer(video_id,request,job):
            core.mark_job_timing(job,'producer_start')
            core.mark_job_timing(job,'source_attempt_start',overwrite=True)
            core.mark_job_timing(job,'source_ready',overwrite=True)
            core.mark_job_timing(job,'ffmpeg_spawn_start',overwrite=True)
            core.mark_job_timing(job,'mp3_first_bytes',overwrite=True)
            job.metadata.update(media=media,cache='MISS-PROGRESSIVE',attempts=[{'path':'fg-mweb-auth','ok':True}])
            async def body():yield self.mp3
            return body()
        manager=self.manager(producer)
        job=manager.get('siRAwwaNc1M',self.request())
        await manager.wait(job)
        payload=core.job_status_payload(job)
        self.assertEqual(payload['jobId'],job.job_id)
        self.assertEqual(payload['state'],'complete')
        self.assertEqual(payload['client'],'mweb')
        self.assertNotIn('url',payload)
        self.assertNotIn('secret.example',str(payload))

    async def test_midstream_encoder_stall_has_explicit_error_code(self):
        core=self.core
        class Stdout:
            def __init__(self):self.calls=0
            async def read(self,n):
                self.calls+=1
                if self.calls==1:return b'x'*core.MP3_STARTUP_MIN_BYTES
                await asyncio.sleep(60)
        class Stderr:
            async def read(self):return b''
        class Proc:
            def __init__(self):
                self.stdout=Stdout();self.stderr=Stderr();self.stdin=None;self.returncode=None
            def kill(self):self.returncode=-9
            async def wait(self):
                while self.returncode is None:await asyncio.sleep(.001)
                return self.returncode
        proc=Proc()
        media=core.ResolvedMedia('siRAwwaNc1M','https://example.test/source',{},'test','251','webm','webm','opus','none',128,180,'tone',0,9999999999,'fixture')
        job=types.SimpleNamespace(job_id='abc123abc123',metadata={})
        with patch.object(core,'spawn_owned_process',AsyncMock(return_value=proc)),              patch.object(core,'MP3_OUTPUT_STALL_TIMEOUT_SECONDS',0.01):
            body=await core.prepare_live_mp3_stream(media,'siRAwwaNc1M',self.request(),job=job)
            self.assertEqual(len(await anext(body)),core.MP3_STARTUP_MIN_BYTES)
            with self.assertRaises(JobError) as caught:
                await anext(body)
        self.assertEqual(caught.exception.code,'MP3_OUTPUT_STALLED')
        self.assertEqual(job.metadata['terminalFailure']['code'],'MP3_OUTPUT_STALLED')


if __name__=='__main__':unittest.main(verbosity=2)

# V37.7 static regression notes are also exercised by the existing cookie/options tests.
