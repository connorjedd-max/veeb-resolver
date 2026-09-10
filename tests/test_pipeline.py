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
        manager=self.manager(producer,max_jobs=1);job=manager.get('siRAwwaNc1M',None)
        await manager.wait(job)
        with self.assertRaises(JobError):manager.get('dQw4w9WgXcQ',None,background=True)
        first=manager.read(job);await anext(first);await first.aclose()
        second=asyncio.create_task(self.collect(manager,job));release.set()
        self.assertEqual(await second,self.mp3)

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


if __name__=='__main__':unittest.main(verbosity=2)

# V37.7 static regression notes are also exercised by the existing cookie/options tests.
