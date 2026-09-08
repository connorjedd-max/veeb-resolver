"""Isolated yt-dlp worker. JSON on stdin/stdout; never print signed URLs in logs."""
import contextlib
import io
import json
import re
import sys


def _safe_diagnostics(text):
    keep = []
    keywords = ('[pot', 'po token', 'player client', 'downloading webpage', 'player api',
                'sign in to confirm', 'requested format', 'm3u8', 'hls', 'sabr', 'provider')
    for raw in str(text or '').splitlines():
        low = raw.lower()
        if any(word in low for word in keywords):
            clean = re.sub(r'https?://[^\s]+', '[url]', raw)
            clean = re.sub(r'(?i)(po[_ -]?token|pot)[=: ]+[A-Za-z0-9_+\-/=]{24,}', r'\1=<redacted>', clean)
            keep.append(clean[-700:])
    return keep[-20:]


def run(payload):
    import yt_dlp
    video_id = payload['videoId']
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        raise ValueError('Invalid video ID')
    captured_out, captured_err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(captured_out), contextlib.redirect_stderr(captured_err):
            with yt_dlp.YoutubeDL(payload['options']) as ydl:
                info = ydl.extract_info('https://www.youtube.com/watch?v=' + video_id, download=False)
    except Exception as exc:
        exc._veeb_diagnostics = _safe_diagnostics(captured_out.getvalue() + '\n' + captured_err.getvalue())
        raise
    if not isinstance(info, dict) or not str(info.get('url', '')).startswith(('https://', 'http://')):
        raise RuntimeError('No usable source URL')
    if info.get('is_live'):
        raise RuntimeError('Live broadcasts are not supported by the finite MP3 cache')
    fields = ('url', 'http_headers', 'format_id', 'ext', 'container', 'acodec', 'vcodec', 'abr', 'duration', 'title')
    return {key: info.get(key) for key in fields}, _safe_diagnostics(captured_out.getvalue() + '\n' + captured_err.getvalue())


if __name__ == '__main__':
    try:
        media, diagnostics = run(json.load(sys.stdin))
        result = {'ok': True, 'media': media, 'diagnostics': diagnostics}
    except Exception as exc:
        # This crosses a private subprocess pipe, but redact URLs and tokens here.
        result = {'ok': False, 'error': re.sub(r'https?://[^\s]+', '[url]', str(exc))[-2000:],
                  'diagnostics': getattr(exc, '_veeb_diagnostics', [])}
    print(json.dumps(result))
