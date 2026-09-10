"""Actual ASGI lease, upload, progressive reader and completed handoff tests.

YouTube is excluded. MP3 fixtures come from real FFmpeg; HTTP routes are genuine.
"""
import asyncio
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import veeb_resolver as server
from media_jobs import MediaJobs
from source_broker import SourceBroker
VIDEO = 'siRAwwaNc1M'


class AgentTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.mp3 = subprocess.check_output(['ffmpeg','-v','error','-f','lavfi','-i',
            'sine=frequency=700:duration=3','-c:a','libmp3lame','-b:a','128k','-ar','44100',
            '-ac','2','-id3v2_version','0','-write_xing','0','-f','mp3','pipe:1'])

    async def asyncSetUp(self):
        self.broker = SourceBroker()
        self.jobs = MediaJobs(server.produce_mp3)
        self.patches = [patch.object(server,'SOURCE_MODE','agent'),patch.object(server,'SOURCE_AGENT_SECRET','agent-test-only'),
                       patch.object(server,'RESOLVER_SECRET','worker-test-only'),patch.object(server,'_source_broker',self.broker),
                       patch.object(server,'_media_jobs',self.jobs),patch.object(server,'_last_source_success',None),
                       patch.object(server,'foreground_pools',side_effect=AssertionError('Render must not extract YouTube in agent mode'))]
        for p in self.patches: p.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='https://resolver.test')
        self.worker = {'Authorization':'Bearer worker-test-only'}
        self.agent = {'Authorization':'Bearer agent-test-only'}

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.jobs.close()
        for p in reversed(self.patches): p.stop()

    async def start_job(self):
        self.assertEqual((await self.client.post('/agent/heartbeat',headers=self.agent)).status_code,200)
        r=await self.client.post('/prepare/'+VIDEO,headers=self.worker)
        self.assertEqual(r.status_code,202)
        for _ in range(100):
            r=await self.client.post('/agent/claim',headers=self.agent)
            if r.json()['job']:
                return r.json()['job']
            await asyncio.sleep(.001)
        self.fail('No pending source job')

    def upload_headers(self,claim):
        return {**self.agent,'X-Veeb-Lease':claim['leaseToken'],
                'X-Veeb-SHA256':hashlib.sha256(self.mp3).hexdigest(),
                'X-Veeb-Source-Duration':'3.05','Content-Type':'audio/mpeg'}

    async def test_agent_upload_fulfills_shared_playback_and_completed_cache(self):
        claim=await self.start_job()
        waiting=asyncio.create_task(self.client.get('/stream/'+VIDEO,headers=self.worker))
        duplicate=await self.client.post('/agent/claim',headers=self.agent)
        self.assertIsNone(duplicate.json()['job'])
        r=await self.client.post('/agent/result/'+VIDEO,content=self.mp3,headers=self.upload_headers(claim))
        self.assertEqual(r.status_code,200,r.text)
        stream=await waiting
        self.assertEqual(stream.content,self.mp3)
        completed=await self.client.get('/completed/'+VIDEO,headers=self.worker)
        self.assertEqual(completed.content,self.mp3)
        self.assertEqual(completed.headers['x-veeb-mp3-complete'],'1')
        self.assertEqual(completed.headers['x-veeb-transcode'],'verified-agent-mp3')
        self.assertFalse(self.broker.tickets)
        self.assertEqual(len(self.jobs.jobs),1)

    async def test_agent_key_cannot_access_worker_or_submit_unleased_audio(self):
        self.assertEqual((await self.client.get('/stream/'+VIDEO,headers=self.agent)).status_code,401)
        self.assertEqual((await self.client.post('/agent/claim',headers=self.worker)).status_code,401)
        self.assertEqual((await self.client.post('/agent/result/'+VIDEO,content=self.mp3,headers=self.agent)).status_code,409)
        self.assertFalse(self.jobs.jobs)

    async def test_bad_hash_is_rejected_and_same_lease_can_retry_valid_bytes(self):
        claim=await self.start_job()
        bad={**self.upload_headers(claim),'X-Veeb-SHA256':'0'*64}
        r=await self.client.post('/agent/result/'+VIDEO,content=self.mp3,headers=bad)
        self.assertEqual(r.status_code,422)
        self.assertEqual((await self.client.get('/completed/'+VIDEO,headers=self.worker)).status_code,202)
        r=await self.client.post('/agent/result/'+VIDEO,content=self.mp3,headers=self.upload_headers(claim))
        self.assertEqual(r.status_code,200)
        await self.jobs.wait(self.jobs.jobs[VIDEO],complete=True)

    async def test_old_agent_cannot_replace_reclaimed_lease(self):
        first=await self.start_job()
        self.broker.tickets[VIDEO].expires=0
        second=(await self.client.post('/agent/claim',headers=self.agent)).json()['job']
        self.assertNotEqual(first['leaseToken'],second['leaseToken'])
        r=await self.client.post('/agent/result/'+VIDEO,content=self.mp3,headers=self.upload_headers(first))
        self.assertEqual(r.status_code,409)
        r=await self.client.post('/agent/result/'+VIDEO,content=self.mp3,headers=self.upload_headers(second))
        self.assertEqual(r.status_code,200)
        await self.jobs.wait(self.jobs.jobs[VIDEO],complete=True)

    async def test_offline_agent_returns_clear_failure_without_render_fallback(self):
        r=await self.client.get('/stream/'+VIDEO,headers=self.worker)
        self.assertEqual(r.status_code,503)
        self.assertEqual(r.json()['code'],'SOURCE_AGENT_OFFLINE')

    async def test_agent_failure_reaches_original_waiter(self):
        claim=await self.start_job()
        r=await self.client.post('/agent/failure/'+VIDEO,headers=self.upload_headers(claim),json={
            'code':'SOURCE_ACCESS_DENIED','message':'YouTube challenged this source computer too.'})
        self.assertEqual(r.status_code,200)
        r=await self.client.get('/stream/'+VIDEO,headers=self.worker)
        self.assertEqual(r.json()['code'],'SOURCE_ACCESS_DENIED')
        self.assertFalse(self.jobs.jobs[VIDEO].path.exists())

    async def test_heartbeat_keeps_agent_online_during_download(self):
        claim=await self.start_job()
        self.broker.last_seen-=30
        self.assertFalse(self.broker.online())
        before=self.broker.tickets[VIDEO].expires
        await self.client.post('/agent/heartbeat',headers={**self.agent,'X-Veeb-Video-Id':VIDEO,'X-Veeb-Lease':claim['leaseToken']})
        self.assertTrue(self.broker.online())
        self.assertGreater(self.broker.tickets[VIDEO].expires,before)

    async def test_real_client_upload_function_reuses_completed_bytes(self):
        import source_agent
        claim=await self.start_job()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'audio.mp3';path.write_bytes(self.mp3)
            audio={'path':str(path),'bytes':len(self.mp3),'duration':3.05,
                   'sha256':hashlib.sha256(self.mp3).hexdigest(),'localCacheHit':False}
            self.client.headers.update(self.agent)
            await source_agent.upload_result(self.client,claim,audio)
            await self.jobs.wait(self.jobs.jobs[VIDEO],complete=True)
            self.assertEqual(self.jobs.jobs[VIDEO].path.read_bytes(),self.mp3)
