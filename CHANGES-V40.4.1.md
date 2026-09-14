# Veeb resolver V40.4.1 package fix

This is a test-only packaging correction for V40.4. Runtime resolver behaviour is unchanged.

The V40.4 runtime intentionally adds `youtube:use_ad_playback_context=true` to the existing mweb extractor arguments when `VEEB_YTDLP_USE_AD_PLAYBACK_CONTEXT=true`.

The failed Render build still contained the old V40.3 regression assertion:

    self.assertNotIn('use_ad_playback_context', a['extractor_args']['youtube'])

That assertion contradicted the new V40.4 behaviour and caused the Docker build to fail even though the runtime change was intentional.

The corrected test now verifies:

    self.assertEqual(a['extractor_args']['youtube'].get('use_ad_playback_context'), ['true'])

The separate rollback test still verifies that setting `VEEB_YTDLP_USE_AD_PLAYBACK_CONTEXT=false` removes the flag, and that the flag is never added to the default/non-mweb client.

No resolver runtime code, source ordering, cookie handling, POT behaviour, concurrency, progressive source logic, FFmpeg settings, startup thresholds, or R2 behaviour is changed by V40.4.1.
