"""Read-only R2 handoff. Real ASGI routes/MP3 bytes; no YouTube requests."""
import asyncio
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
import veeb_resolver as server
from media_jobs import MediaJobs, JobError

class CompletedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.media=server.ResolvedMedia('siRAwwaNc1M','file:///fixture',{},'fixture','fixture','mp3','audio/mpeg','mp3','none',128,3,'fixture',0,9999999999,'fixture')
        self.calls=0
        async def producer(video,request,job):
            self.calls+=1
            job.metadata={'media':self.media,'cache':'TEST'}
            proc=await asyncio.create_subprocess_exec('ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=3','-c:a','libmp3lame','-b:a','128k','-ar','44100','-id3v2_version','0','-write_xing','0','-f','mp3','pipe:1',stdout=asyncio.subprocess.PIPE)
            data,_=await proc.communicate()
            async def stream():yield data
            return stream()
        self.jobs=MediaJobs(producer,directory=Path(self.tmp.name)/'jobs')
        self.patches=[patch.object(server,'_media_jobs',self.jobs),patch.object(server,'RESOLVER_SECRET','unit-only')]
        for p in self.patches:p.start()
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://test')
        self.headers={'Authorization':'Bearer unit-only'}
    async def asyncTearDown(self):
        await self.client.aclose();await self.jobs.close()
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    async def test_requires_auth(self):
        self.assertEqual((await self.client.get('/completed/siRAwwaNc1M')).status_code,401)
        self.assertEqual(self.calls,0)
    async def test_rejects_invalid_id(self):
        self.assertEqual((await self.client.get('/completed/invalid',headers=self.headers)).status_code,400)
        self.assertEqual(self.calls,0)
    async def test_miss_never_creates_job(self):
        r=await self.client.get('/completed/siRAwwaNc1M',headers=self.headers)
        self.assertEqual(r.status_code,404);self.assertFalse(r.json()['startsAcquisition'])
        self.assertEqual(self.calls,0);self.assertFalse(self.jobs.jobs)
    async def test_running_is_202_without_new_work(self):
        self.jobs.jobs['siRAwwaNc1M']=types.SimpleNamespace(done=asyncio.Event(),touched=0)
        r=await self.client.get('/completed/siRAwwaNc1M',headers=self.headers)
        self.assertEqual(r.status_code,202);self.assertEqual(self.calls,0)
        self.jobs.jobs.clear()
    async def test_complete_mp3_get_and_head_reuse_producer(self):
        job=self.jobs.get('siRAwwaNc1M',None);await self.jobs.wait(job,complete=True)
        for method in ['HEAD','GET','GET']:
            r=await self.client.request(method,'/completed/siRAwwaNc1M',headers=self.headers)
            self.assertEqual(r.status_code,200);self.assertEqual(r.headers['x-veeb-mp3-complete'],'1')
            self.assertEqual(int(r.headers['content-length']),job.size)
            if method=='GET':self.assertTrue(server.mp3_frames_valid(r.content))
            else:self.assertFalse(r.content)
        self.assertEqual(self.calls,1)
    async def test_failed_job_is_not_served(self):
        job=self.jobs.get('siRAwwaNc1M',None);await self.jobs.wait(job,complete=True)
        job.error=JobError('INVALID_MP3','test failure')
        r=await self.client.get('/completed/siRAwwaNc1M',headers=self.headers)
        self.assertEqual(r.status_code,424);self.assertEqual(self.calls,1)
    async def test_deleted_file_returns_miss(self):
        job=self.jobs.get('siRAwwaNc1M',None);await self.jobs.wait(job,complete=True);job.path.unlink()
        r=await self.client.get('/completed/siRAwwaNc1M',headers=self.headers)
        self.assertEqual(r.status_code,404);self.assertEqual(self.calls,1)
    async def test_expired_job_does_not_reextract(self):
        job=self.jobs.get('siRAwwaNc1M',None);await self.jobs.wait(job,complete=True);job.touched=time.monotonic()-self.jobs.ttl-1
        r=await self.client.get('/completed/siRAwwaNc1M',headers=self.headers)
        self.assertEqual(r.status_code,404);self.assertEqual(self.calls,1)
