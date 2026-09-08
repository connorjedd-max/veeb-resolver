"""Isolated yt-dlp worker. JSON on stdin/stdout; never print signed URLs in logs."""
import contextlib
import io
import json
import re
import sys
import os
from pathlib import Path
import shutil
import tempfile


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
            options = dict(payload['options'])
            download = bool(payload.get('download'))
            download_dir = None
            if download:
                download_dir = tempfile.mkdtemp(prefix='veeb-ytdlp-source-')
                options['skip_download'] = False
                options['outtmpl'] = os.path.join(download_dir, '%(id)s.%(ext)s')
                options['nopart'] = True
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info('https://www.youtube.com/watch?v=' + video_id, download=download)
    except Exception as exc:
        try:
            if 'download_dir' in locals() and download_dir:
                shutil.rmtree(download_dir, ignore_errors=True)
        except Exception:
            pass
        exc._veeb_diagnostics = _safe_diagnostics(captured_out.getvalue() + '\n' + captured_err.getvalue())
        raise
    if not isinstance(info, dict) or not str(info.get('url', '')).startswith(('https://', 'http://')):
        raise RuntimeError('No usable source URL')
    if info.get('is_live'):
        raise RuntimeError('Live broadcasts are not supported by the finite MP3 cache')
    fields = ('url', 'http_headers', 'format_id', 'ext', 'container', 'acodec', 'vcodec', 'abr', 'duration', 'title')
    media = {key: info.get(key) for key in fields}
    download_bytes = None
    if download:
        candidates = [p for p in Path(download_dir).iterdir() if p.is_file() and not p.name.endswith(('.part', '.ytdl'))]
        if not candidates:
            shutil.rmtree(download_dir, ignore_errors=True)
            raise RuntimeError('yt-dlp reported success but produced no source file')
        source = max(candidates, key=lambda p: p.stat().st_size)
        media['local_path'] = str(source)
        download_bytes = source.stat().st_size
    return media, _safe_diagnostics(captured_out.getvalue() + '\n' + captured_err.getvalue()), download_bytes


if __name__ == '__main__':
    try:
        media, diagnostics, download_bytes = run(json.load(sys.stdin))
        result = {'ok': True, 'media': media, 'diagnostics': diagnostics, 'downloadBytes': download_bytes}
    except Exception as exc:
        # This crosses a private subprocess pipe, but redact URLs and tokens here.
        result = {'ok': False, 'error': re.sub(r'https?://[^\s]+', '[url]', str(exc))[-2000:],
                  'diagnostics': getattr(exc, '_veeb_diagnostics', [])}
    print(json.dumps(result))
