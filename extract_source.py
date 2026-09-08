"""Isolated yt-dlp worker. JSON on stdin/stdout; never print signed URLs in logs."""
import contextlib
import io
import json
import re
import sys


def run(payload):
    import yt_dlp
    video_id = payload['videoId']
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        raise ValueError('Invalid video ID')
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        with yt_dlp.YoutubeDL(payload['options']) as ydl:
            info = ydl.extract_info('https://www.youtube.com/watch?v=' + video_id, download=False)
    if not isinstance(info, dict) or not str(info.get('url', '')).startswith(('https://', 'http://')):
        raise RuntimeError('No usable source URL')
    if info.get('is_live'):
        raise RuntimeError('Live broadcasts are not supported by the finite MP3 cache')
    fields = ('url', 'http_headers', 'format_id', 'ext', 'container', 'acodec', 'vcodec', 'abr', 'duration', 'title')
    return {key: info.get(key) for key in fields}


if __name__ == '__main__':
    try:
        result = {'ok': True, 'media': run(json.load(sys.stdin))}
    except Exception as exc:
        # This crosses a private subprocess pipe, but redact URLs even here.
        result = {'ok': False, 'error': re.sub(r'https?://[^\s]+', '[url]', str(exc))[-2000:]}
    print(json.dumps(result))
