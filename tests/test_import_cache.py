"""Existing R2 source migration with real local ffprobe/FFmpeg, no YouTube."""
import asyncio
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
import veeb_resolver as server
from media_jobs import MediaJobs

class ImportTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_dir=tempfile.TemporaryDirectory()
        path=Path(cls.fixture_dir.name)/'fixture.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=330:duration=3','-c:a','aac','-movflags','+faststart',str(path)],check=True)
        cls.data=path.read_bytes()
    @classmethod
    def tearDownClass(cls):cls.fixture_dir.cleanup()
    async def asyncSetUp(self):
        self.jobs=MediaJobs(server.produce_mp3)
        self.patches=[patch.object(server,'_media_jobs',self.jobs),patch.object(server,'RESOLVER_SECRET','unit-only'),
                      patch.object(server,'foreground_pools',side_effect=AssertionError('YouTube extraction must not run'))]
        for p in self.patches:p.start()
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://test')
        self.headers={'Authorization':'Bearer unit-only','Content-Type':'application/octet-stream'}
    async def asyncTearDown(self):
        await self.client.aclose();await self.jobs.close()
        for p in reversed(self.patches):p.stop()
    async def test_real_source_import_and_completed_read(self):
        r=await self.client.post('/import-cached-source/siRAwwaNc1M',content=self.data,headers=self.headers)
        self.assertEqual(r.status_code,200,r.text);self.assertTrue(r.json()['mp3Complete']);self.assertFalse(r.json()['youtubeContacted'])
        audio=await self.client.get('/completed/siRAwwaNc1M',headers=self.headers)
        self.assertEqual(audio.status_code,200);self.assertTrue(server.mp3_frames_valid(audio.content))
    async def test_auth_required_before_upload(self):
        r=await self.client.post('/import-cached-source/siRAwwaNc1M',content=self.data)
        self.assertEqual(r.status_code,401);self.assertFalse(self.jobs.jobs)
    async def test_text_playlist_cannot_be_used_as_source(self):
        r=await self.client.post('/import-cached-source/siRAwwaNc1M',content=b'#EXTM3U\nhttp://127.0.0.1/secret',headers=self.headers)
        self.assertEqual(r.status_code,415);self.assertFalse(self.jobs.jobs)
    async def test_body_limit(self):
        r=await self.client.post('/import-cached-source/siRAwwaNc1M',content=b'bad',headers={**self.headers,'Content-Length':str(90*1024*1024)})
        self.assertEqual(r.status_code,413)
    async def test_wrong_content_length(self):
        r=await self.client.post('/import-cached-source/siRAwwaNc1M',content=self.data,headers={**self.headers,'Content-Length':str(len(self.data)+3)})
        self.assertEqual(r.status_code,400)
    async def test_duplicate_import_does_not_reconvert(self):
        r=await self.client.post('/import-cached-source/siRAwwaNc1M',content=self.data,headers=self.headers);self.assertEqual(r.status_code,200)
        again=await self.client.post('/import-cached-source/siRAwwaNc1M',content=self.data,headers=self.headers)
        self.assertEqual(again.status_code,200);self.assertTrue(again.json()['reused']);self.assertEqual(r.json()['jobId'],again.json()['jobId'])
