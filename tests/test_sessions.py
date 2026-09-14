"""Offline session/ownership checks. YouTube and yt-dlp responses here are mocks."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from source_support import CookieStore, inspect_cookies, redact, reported_login_state, SourceAttemptError, DiagnosticLog, failure_code
import extract_source
from test_pipeline import load_core

def cookies(value='dummy-cookie',expires='0'):
    return '# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t'+expires+'\tSAPISID\t'+value+'\n'

class CookieTests(unittest.TestCase):
    def test_fields_do_not_verify_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'s';p.write_text(cookies());a=inspect_cookies(p)
            self.assertTrue(a['activeAuthCookieFieldsPresent']);self.assertIsNone(a['authenticationVerified'])
            self.assertNotIn('dummy-cookie',json.dumps(a))
    def test_zero_expiry_is_session_not_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'s';p.write_text(cookies(expires='1'));a=inspect_cookies(p)
            self.assertEqual(a['expiredYoutubeCookieCount'],1);self.assertFalse(a['activeAuthCookieFieldsPresent'])
            p.write_text(cookies(expires='0'));self.assertTrue(inspect_cookies(p)['activeAuthCookieFieldsPresent'])
    def test_malformed_file_does_not_leak_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'s';p.write_text('{"cookies":"SECRET"}');a=inspect_cookies(p)
            self.assertEqual(a['status'],'invalid_netscape_header');self.assertNotIn('SECRET',json.dumps(a))
    def test_secret_change_refreshes_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Path(tmp)/'s';r=Path(tmp)/'r';s.write_text(cookies('old'));store=CookieStore(s,r)
            store.snapshot();s.write_text(cookies('new'));store.snapshot()
            self.assertIn('new',r.read_text());self.assertNotIn('old',r.read_text());self.assertEqual(r.stat().st_mode&0o777,0o600)
    def test_invalid_replacement_cannot_reuse_old_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Path(tmp)/'s';r=Path(tmp)/'r';s.write_text(cookies());store=CookieStore(s,r);store.snapshot()
            s.write_text('invalid');self.assertIsNone(store.snapshot())
    def test_rotation_preserved_without_modifying_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Path(tmp)/'s';r=Path(tmp)/'r';u=Path(tmp)/'u';s.write_text(cookies('original'));store=CookieStore(s,r)
            store.snapshot();before=hashlib.sha256(r.read_bytes()).hexdigest();u.write_text(cookies('rotated'))
            self.assertTrue(store.accept_update(before,u));store.snapshot()
            self.assertIn('rotated',r.read_text());self.assertIn('original',s.read_text())
    def test_old_child_cannot_overwrite_new_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Path(tmp)/'s';r=Path(tmp)/'r';u=Path(tmp)/'u';s.write_text(cookies('old'));store=CookieStore(s,r)
            store.snapshot();before=hashlib.sha256(r.read_bytes()).hexdigest()
            s.write_text(cookies('new'));store.snapshot();u.write_text(cookies('old-response'))
            self.assertFalse(store.accept_update(before,u));self.assertIn('new',r.read_text())
    def test_bad_runtime_update_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Path(tmp)/'s';r=Path(tmp)/'r';u=Path(tmp)/'u';s.write_text(cookies());store=CookieStore(s,r)
            store.snapshot();before=hashlib.sha256(r.read_bytes()).hexdigest();u.write_text('invalid')
            self.assertFalse(store.accept_update(before,u));self.assertEqual(hashlib.sha256(r.read_bytes()).hexdigest(),before)
    def test_login_flag_true_false_unknown_conflict(self):
        self.assertIs(reported_login_state('ytcfg.set({"LOGGED_IN":true});'),True)
        self.assertIs(reported_login_state('{"LOGGED_IN":false}'),False)
        self.assertIsNone(reported_login_state('consent or challenge'))
        self.assertIsNone(reported_login_state('{"LOGGED_IN":true}{"LOGGED_IN":false}'))
    def test_tokens_and_url_tails_are_redacted(self):
        for msg in ["PoTokenResponse(po_token='TOKEN-ABC-SECRET', expires_at=1)",
                    '{"integrityToken":"TOKEN-ABC-SECRET"}',
                    'https://example.googlevideo.com/videoplayback?pot=TOKEN-ABC-SECRET&sig=otherSECRET 403',
                    'tail&itag=251&pot=TOKEN-ABC-SECRET&sig=otherSECRET 403']:
            text=redact(msg);self.assertNotIn('TOKEN-ABC-SECRET',text);self.assertNotIn('otherSECRET',text)
    def test_token_cache_spam_cannot_hide_main_error(self):
        log=DiagnosticLog();log.warning('n challenge solving failed')
        for _ in range(30):log.debug('[pot:cache] PoTokenResponse(po_token=SECRET)')
        log.error('HTTP Error 403: Forbidden');self.assertTrue(log.evidence['jsChallengeFailed'])
        self.assertIn('403',log.lines[-1]);self.assertNotIn('SECRET',json.dumps(log.lines))
    def test_source_phase_timing_is_redacted_and_monotonic(self):
        log=DiagnosticLog()
        log.debug('Downloading webpage')
        log.debug('Downloading mweb client config')
        log.debug('Detected a 15s ad skippable after 5s for mweb')
        log.debug('Generating a GVS PO Token')
        log.debug('Retrieved a GVS PO Token')
        log.debug('Invoking http downloader')
        log.progress({'status':'downloading','downloaded_bytes':8192,'info_dict':{'format_id':'251','protocol':'https'}})
        timing=log.timing_snapshot()
        for key in ('webpageMs','clientConfigMs','adDetectedMs','potRequestMs','gvsTokenReadyMs','downloaderInvokedMs','firstDownloadProgressMs','source8192Ms'):
            self.assertIn(key,timing)
            self.assertGreaterEqual(timing[key],0)
        self.assertTrue(log.evidence['gvsTokenReturned'])
        self.assertEqual(log.evidence['downloadedBytes'],8192)

    def test_permanent_video_failure_is_distinguished_from_route_failure(self):
        self.assertEqual(failure_code('ERROR: [youtube] abc: Video unavailable'),'SOURCE_VIDEO_UNAVAILABLE')
        self.assertEqual(failure_code('This video is unavailable'),'SOURCE_VIDEO_UNAVAILABLE')
        self.assertEqual(failure_code('This video is not available in your country'),'SOURCE_REGION_RESTRICTED')
        self.assertEqual(failure_code('Sign in to confirm you are not a bot'),'SOURCE_ACCESS_DENIED')
    def test_anonymous_child_has_no_cookiefile(self):
        seen=[]
        class Ydl:
            def __init__(self,opts):seen.append(opts)
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def extract_info(self,*a,**k):return {'url':'https://example.test/media','is_live':False}
        with patch.dict(sys.modules,{'yt_dlp':types.SimpleNamespace(YoutubeDL=Ydl)}):
            result=extract_source.run({'videoId':'siRAwwaNc1M','options':{}})
        self.assertTrue(result['ok']);self.assertNotIn('cookiefile',seen[0])
    def test_auth_child_private_cookie_copy_is_deleted(self):
        seen=[]
        class Ydl:
            def __init__(self,opts):self.opts=opts;seen.append(opts['cookiefile'])
            def __enter__(self):return self
            def __exit__(self,*a):Path(self.opts['cookiefile']).write_text('changed by library')
            def extract_info(self,*a,**k):return {'url':'https://example.test/media','is_live':False}
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'s';p.write_text(cookies())
            with patch.dict(sys.modules,{'yt_dlp':types.SimpleNamespace(YoutubeDL=Ydl)}):
                result=extract_source.run({'videoId':'siRAwwaNc1M','options':{'cookiefile':str(p)}})
            self.assertTrue(result['ok']);self.assertEqual(p.read_text(),cookies())
            self.assertNotEqual(seen[0],str(p));self.assertFalse(Path(seen[0]).exists())
    def test_download_failure_keeps_stage_reason_and_cleans_files(self):
        class Ydl:
            def __init__(self,opts):self.opts=opts
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def extract_info(self,*a,**k):
                self.opts['logger'].debug('Invoking http downloader on https://example.test/?pot=SECRET')
                raise RuntimeError('HTTP Error 403: Forbidden')
        with tempfile.TemporaryDirectory() as tmp:
            owned=Path(tmp)/'owned'
            with patch.dict(sys.modules,{'yt_dlp':types.SimpleNamespace(YoutubeDL=Ydl)}):
                result=extract_source.run({'videoId':'siRAwwaNc1M','download':True,'downloadDirectory':str(owned),'options':{}})
            self.assertFalse(result['ok']);self.assertEqual(result['stage'],'download')
            self.assertEqual(result['code'],'MEDIA_HTTP_403');self.assertIn('403',result['error']);self.assertFalse(owned.exists())

class SessionPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):self.core=load_core();self.core.init_ytdlp_pools()
    async def asyncTearDown(self):await self.core._media_jobs.close()
    async def test_authenticated_mweb_owns_download_from_start(self):
        c=self.core;media=types.SimpleNamespace(client='mweb',format_id='251',video_id='siRAwwaNc1M')
        job=types.SimpleNamespace(metadata={});iterator=object()
        with patch.object(c,'get_writable_cookie_file',return_value='/private/cookies'), \
             patch.object(c._fg_pot_pool,'download_source',AsyncMock(side_effect=AssertionError('auth must not wait for anonymous'))) as anonymous, \
             patch.object(c._fg_mweb_auth_pool,'resolve',AsyncMock(side_effect=AssertionError('must retain session'))) as resolve, \
             patch.object(c._fg_mweb_auth_pool,'download_source',AsyncMock(return_value=media)) as download, \
             patch.object(c,'prepare_live_mp3_stream',AsyncMock(return_value=iterator)):
            result=await c.produce_mp3('siRAwwaNc1M',None,job)
        self.assertIs(result,iterator);download.assert_awaited_once();resolve.assert_not_awaited()
        self.assertTrue(job.metadata['attempts'][-1]['usesCookies']);anonymous.assert_not_awaited()
    async def test_skip_client_config_default_is_safe_off(self):
        self.assertFalse(self.core.YTDLP_SKIP_MWEB_CLIENT_CONFIG)

    async def test_authenticated_mweb_uses_stable_config_flow_by_default(self):
        with patch.object(self.core,'get_writable_cookie_file',return_value='/private/cookies'):
            fallback=self.core.ytdlp_options('mweb',None,False);fast=self.core.ytdlp_options('mweb',None,True)
        self.assertNotIn('cookiefile',fallback);self.assertEqual(fast['cookiefile'],'/private/cookies')
        self.assertNotIn('pot_trace',fallback['extractor_args']['youtube'])
        self.assertEqual(fallback['extractor_args']['youtube'].get('use_ad_playback_context'), ['true'])
        self.assertEqual(fast['extractor_args']['youtube'].get('use_ad_playback_context'), ['true'])
        self.assertNotIn('player_skip',fallback['extractor_args']['youtube'])
        self.assertNotIn('player_skip',fast['extractor_args']['youtube'])

    async def test_skip_client_config_is_explicit_opt_in_only(self):
        with patch.object(self.core,'YTDLP_SKIP_MWEB_CLIENT_CONFIG',False):
            args=self.core.youtube_extractor_args_dict('mweb',use_cookies=True)
        self.assertNotIn('player_skip',args)
        with patch.object(self.core,'YTDLP_SKIP_MWEB_CLIENT_CONFIG',True):
            args=self.core.youtube_extractor_args_dict('mweb',use_cookies=True)
        self.assertEqual(args.get('player_skip'),['configs'])
        self.assertNotIn('player_skip',self.core.youtube_extractor_args_dict('mweb',use_cookies=False))
    async def test_ad_playback_context_has_single_flag_rollback(self):
        with patch.object(self.core,'YTDLP_USE_AD_PLAYBACK_CONTEXT',False):
            args=self.core.youtube_extractor_args_dict('mweb')
        self.assertNotIn('use_ad_playback_context',args)
        with patch.object(self.core,'YTDLP_USE_AD_PLAYBACK_CONTEXT',True):
            args=self.core.youtube_extractor_args_dict('mweb')
        self.assertEqual(args.get('use_ad_playback_context'),['true'])
        self.assertNotIn('use_ad_playback_context',self.core.youtube_extractor_args_dict('default'))

    async def test_missing_auth_file_is_not_silent_anonymous_attempt(self):
        with patch.object(self.core,'get_writable_cookie_file',return_value=None):
            with self.assertRaises(SourceAttemptError):self.core.ytdlp_options('mweb',None,True)
    async def test_cancel_cleans_parent_owned_download_directory(self):
        c=self.core
        with tempfile.TemporaryDirectory() as tmp:
            marker=Path(tmp)/'marker';script=Path(tmp)/'extract_source.py'
            script.write_text("import json,sys,time\nfrom pathlib import Path\np=json.load(sys.stdin)\nPath("+repr(str(marker))+").write_text(p['downloadDirectory'])\nPath(p['downloadDirectory'],'partial.webm').write_bytes(b'partial')\ntime.sleep(60)\n")
            with patch.object(c,'__file__',str(Path(tmp)/'veeb_resolver.py')):
                task=asyncio.create_task(c._fg_pot_pool.download_source('siRAwwaNc1M','test'))
                for _ in range(100):
                    if marker.exists():break
                    await asyncio.sleep(.01)
                self.assertTrue(marker.exists());owned=Path(marker.read_text());task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
            self.assertFalse(owned.exists());self.assertFalse(c._extraction_sem.locked())
