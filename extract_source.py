"""Isolated yt-dlp child. Credentials/media URLs stay on private subprocess pipes."""
from __future__ import annotations
import contextlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from source_support import DiagnosticLog, failure_code, redact, reported_login_state
from progressive_source import publish_streamable_progress
MAX_SOURCE_BYTES = 80 * 1024 * 1024


def _safe_diagnostics(text):
    logger = DiagnosticLog()
    for line in str(text or '').splitlines(): logger.record(line)
    return logger.lines


def verify_cookie_session(ydl):
    """Explicit page-level login flag, not a claim of media-download permission."""
    response = None
    try:
        from yt_dlp.networking import Request
        response = ydl.urlopen(Request('https://www.youtube.com/feed/subscriptions', headers={'Accept': 'text/html'}))
        page = response.read(2 * 1024 * 1024).decode('utf-8', 'replace')
        logged_in = reported_login_state(page)
        return {'tested': True, 'youtubeReportsLoggedIn': logged_in,
                'status': 'recognised' if logged_in is True else ('not_recognised' if logged_in is False else 'unknown'),
                'httpStatus': getattr(response, 'status', 200), 'scope': 'Page-level session only; media tested separately'}
    except Exception as exc:
        return {'tested': True, 'youtubeReportsLoggedIn': None, 'status': 'request_failed', 'error': redact(exc, 200)}
    finally:
        if response is not None: response.close()


def run(payload):
    import yt_dlp
    video_id = str(payload.get('videoId') or '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id): raise ValueError('Invalid video ID')
    download = bool(payload.get('download'))
    session_only = payload.get('operation') == 'session'
    options = dict(payload['options'])
    logger = DiagnosticLog()
    evidence = logger.evidence
    evidence.update(stage='session' if session_only else 'extract', usesCookies=bool(options.get('cookiefile')))
    captured_out, captured_err = io.StringIO(), io.StringIO()
    download_dir = None
    succeeded = False
    try:
        with tempfile.TemporaryDirectory(prefix='veeb-auth-snapshot-') as private_dir:
            if options.get('cookiefile'):
                snapshot = Path(private_dir) / 'cookies.txt'
                shutil.copyfile(options['cookiefile'], snapshot)
                os.chmod(snapshot, 0o600)
                options['cookiefile'] = str(snapshot)
            options.update(logger=logger, no_warnings=False, verbose=True, quiet=True)
            if download:
                download_dir = str(payload.get('downloadDirectory') or tempfile.mkdtemp(prefix='veeb-ytdlp-source-'))
                Path(download_dir).mkdir(parents=True, exist_ok=True)
                options.update(skip_download=False, outtmpl=os.path.join(download_dir, '%(id)s.%(ext)s'),
                               nopart=False, concurrent_fragment_downloads=1, max_filesize=MAX_SOURCE_BYTES,
                               fragment_retries=2, skip_unavailable_fragments=False,
                               hls_prefer_native=True)
                def progress(data):
                    logger.progress(data)
                    publish_streamable_progress(data, download_dir)
                    if int(data.get('downloaded_bytes') or 0) > MAX_SOURCE_BYTES:
                        raise RuntimeError('Source exceeds the 80 MiB size limit')
                options['progress_hooks'] = [progress]
                def finite_only(info, *, incomplete=False):
                    if info.get('is_live') or info.get('live_status') in {'is_live', 'is_upcoming'}:
                        return 'Live broadcasts are not supported by the finite MP3 cache'
                    duration = info.get('duration')
                    if duration is not None and (not math.isfinite(float(duration)) or not 1 <= float(duration) <= 1800):
                        return 'Source duration must be between 1 and 1800 seconds'
                    return None
                options['match_filter'] = finite_only
            try:
                with contextlib.redirect_stdout(captured_out), contextlib.redirect_stderr(captured_err):
                    with yt_dlp.YoutubeDL(options) as ydl:
                        if session_only:
                            if not options.get('cookiefile'):
                                return {'ok': True, 'cookieSessionTest': {'tested': False, 'youtubeReportsLoggedIn': None, 'status': 'no_usable_cookie_file'}}
                            return {'ok': True, 'cookieSessionTest': verify_cookie_session(ydl)}
                        info = ydl.extract_info('https://www.youtube.com/watch?v=' + video_id, download=download)
            finally:
                update_path = payload.get('cookieUpdatePath')
                if update_path and options.get('cookiefile') and Path(options['cookiefile']).is_file():
                    shutil.copyfile(options['cookiefile'], update_path)
                    os.chmod(update_path, 0o600)
            if not isinstance(info, dict) or not str(info.get('url', '')).startswith(('https://', 'http://')):
                raise RuntimeError('No usable source URL')
            if info.get('is_live'): raise RuntimeError('Live broadcasts are not supported by the finite MP3 cache')
            fields = ('url', 'http_headers', 'format_id', 'ext', 'container', 'acodec', 'vcodec', 'abr', 'duration', 'title')
            media = {key: info.get(key) for key in fields}
            evidence.update(sourceSelected=True, sourceFormat=str(info.get('format_id') or ''), sourceProtocol=str(info.get('protocol') or ''))
            download_bytes = None
            if download:
                candidates = [p for p in Path(download_dir).iterdir() if p.is_file() and p.name.startswith(video_id + '.') and not p.name.endswith(('.part', '.ytdl'))]
                if len(candidates) != 1: raise RuntimeError('yt-dlp did not return exactly one completed source file')
                source = max(candidates, key=lambda p: p.stat().st_size)
                download_bytes = source.stat().st_size
                if not 0 < download_bytes <= MAX_SOURCE_BYTES: raise RuntimeError('Downloaded source is empty or exceeds 80 MiB')
                media['local_path'] = str(source)
                evidence.update(stage='download', downloadCompleted=True, downloadedBytes=download_bytes)
            succeeded = True
            return {'ok': True, 'media': media, 'diagnostics': logger.lines[-3:], 'evidence': evidence, 'downloadBytes': download_bytes}
    except Exception as exc:
        stage = evidence.get('stage', 'extract')
        return {'ok': False, 'code': failure_code(exc, stage), 'stage': stage, 'error': redact(exc, 600), 'evidence': evidence, 'diagnostics': logger.lines[-3:]}
    finally:
        if download_dir and not succeeded: shutil.rmtree(download_dir, ignore_errors=True)


if __name__ == '__main__':
    try: result = run(json.load(sys.stdin))
    except Exception as exc: result = {'ok': False, 'code': 'EXTRACTOR_PROCESS_ERROR', 'stage': 'setup', 'error': redact(exc), 'diagnostics': []}
    print(json.dumps(result))
