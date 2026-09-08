"""Real ASGI routes and FFmpeg. External source/session responses are mocked."""
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
try:
    import httpx
    import veeb_resolver as server
except ImportError:
    httpx=server=None

@unittest.skipIf(server is None,'FastAPI/httpx unavailable')
class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        server.init_ytdlp_pools();self.secret=patch.object(server,'RESOLVER_SECRET','local-test-only');self.secret.start()
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://test')
        self.headers={'Authorization':'Bearer local-test-only'}
    async def asyncTearDown(self):await self.client.aclose();self.secret.stop()
    async def test_real_codec_self_test(self):
        result=await server.codec_self_test();self.assertTrue(result['ok']);self.assertGreater(result['mp3Bytes'],40000)
    async def test_protected_health_lists_authenticated_mweb(self):
        self.assertEqual((await self.client.get('/health')).status_code,401)
        with patch.object(server,'get_writable_cookie_file',return_value='/test/cookies'):
            r=await self.client.get('/health',headers=self.headers)
        self.assertEqual(r.status_code,200)
        self.assertIn({'path':'fg-mweb-auth','client':'mweb','usesCookies':True},r.json()['acquisitionModes'])
    async def test_real_diagnose_json_preserves_phases_and_cookie_result(self):
        attempts=[{'path':'fg-pot','code':'MEDIA_HTTP_403','stage':'download','error':'HTTP Error 403: Forbidden'},
                  {'path':'fg-mweb-auth','code':'SOURCE_ACCESS_DENIED','stage':'extract','error':'not a bot','usesCookies':True}]
        job=types.SimpleNamespace(metadata={'attempts':attempts})
        manager=types.SimpleNamespace(get=lambda *a,**k:job,wait=AsyncMock(side_effect=server.JobError('SOURCE_ACQUISITION_FAILED','Acquisition failed')))
        with patch.object(server,'_media_jobs',manager), \
             patch.object(server,'get_writable_cookie_file',return_value='/test/cookies'), \
             patch.object(server._fg_mweb_auth_pool,'inspect_session',AsyncMock(return_value={'tested':True,'youtubeReportsLoggedIn':False,'status':'not_recognised'})):
            r=await self.client.post('/diagnose/siRAwwaNc1M',headers=self.headers)
        data=r.json();self.assertEqual(r.status_code,424);self.assertTrue(data['codecSelfTest']['ok'])
        self.assertIs(data['cookieSessionTest']['youtubeReportsLoggedIn'],False)
        self.assertEqual(data['attempts'][0]['code'],'MEDIA_HTTP_403');self.assertLess(len(r.content),8192)
        self.assertEqual(data['code'],'SOURCE_ACQUISITION_FAILED')
