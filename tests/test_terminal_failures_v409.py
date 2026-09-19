from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import veeb_resolver as core


class FakeRequest:
    def __init__(self, purpose='r2-library-warm'):
        self.headers = {'x-veeb-purpose': purpose}
        self.scope = {}


class PermanentFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        core._permanent_source_failures.clear()

    async def test_background_unavailable_stops_after_first_route(self):
        calls = []
        class P:
            client_name='mweb'; use_cookies=True
            def __init__(self, name): self.name=name
            async def stream_source(self, video_id, purpose):
                calls.append(self.name)
                raise core.SourceAttemptError('Video unavailable', code='SOURCE_VIDEO_UNAVAILABLE')
        job=types.SimpleNamespace(metadata={}, job_id='j')
        with patch.object(core,'foreground_pools',return_value=[P('one'),P('two'),P('three')]), \
             patch.object(core._source_guard,'check'):
            with self.assertRaises(core.JobError) as cm:
                await core.produce_mp3('siRAwwaNc1M', FakeRequest(), job)
        self.assertEqual(cm.exception.code,'SOURCE_VIDEO_UNAVAILABLE')
        self.assertEqual(calls,['one'])
        self.assertEqual(core.cached_permanent_source_failure('siRAwwaNc1M'),'SOURCE_VIDEO_UNAVAILABLE')

    async def test_foreground_unavailable_gets_two_confirmations(self):
        calls = []
        class P:
            client_name='mweb'; use_cookies=True
            def __init__(self, name): self.name=name
            async def stream_source(self, video_id, purpose):
                calls.append(self.name)
                raise core.SourceAttemptError('Video unavailable', code='SOURCE_VIDEO_UNAVAILABLE')
        job=types.SimpleNamespace(metadata={}, job_id='j')
        with patch.object(core,'foreground_pools',return_value=[P('one'),P('two'),P('three')]), \
             patch.object(core._source_guard,'check'):
            with self.assertRaises(core.JobError) as cm:
                await core.produce_mp3('siRAwwaNc1M', FakeRequest('playback'), job)
        self.assertEqual(cm.exception.code,'SOURCE_VIDEO_UNAVAILABLE')
        self.assertEqual(calls,['one','two'])

    async def test_negative_cache_avoids_ytdlp_routes(self):
        core.remember_permanent_source_failure('siRAwwaNc1M','SOURCE_VIDEO_UNAVAILABLE')
        job=types.SimpleNamespace(metadata={}, job_id='j')
        with patch.object(core,'foreground_pools',return_value=[]) as pools:
            with self.assertRaises(core.JobError) as cm:
                await core.produce_mp3('siRAwwaNc1M', FakeRequest(), job)
        self.assertEqual(cm.exception.code,'SOURCE_VIDEO_UNAVAILABLE')
        pools.assert_not_called()


if __name__ == '__main__':
    unittest.main()
