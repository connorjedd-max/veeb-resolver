from __future__ import annotations

import asyncio
import importlib.metadata
import http.cookiejar
import json
import os
import re
import shutil
import socket
import sys
import signal
import tempfile
import hashlib
import wave
import math
import secrets
from acquisition_guard import AcquisitionGuard
from source_broker import SourceBroker
from mp3_validation import MP3Validator
from source_support import CookieStore, inspect_cookies, redact, failure_code, SourceAttemptError
from source_slots import source_slot
from progressive_source import ProgressiveSource
from media_jobs import MediaJobs, JobError, mp3_frames_valid
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import parse_qs, urlparse
import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
app = FastAPI(title='Veeb YouTube Resolver V39.2 MP3 Stream', docs_url=None, redoc_url=None)
RESOLVER_SECRET = os.environ.get('RESOLVER_SECRET', '')
VIDEO_ID_RE = re.compile('^[A-Za-z0-9_-]{11}$')
YOUTUBE_COOKIE_FILE = os.environ.get('YOUTUBE_COOKIE_FILE', '/etc/secrets/youtube-cookies.txt')
WRITABLE_COOKIE_FILE = os.environ.get('WRITABLE_COOKIE_FILE', '/tmp/veeb-youtube-cookies.txt')
YTDLP_CACHE_DIR = os.environ.get('YTDLP_CACHE_DIR', '/tmp/veeb-yt-dlp-cache')
JSC_RUNTIME = os.environ.get('YOUTUBE_JSC_RUNTIME', 'deno').strip() or 'deno'
YTDLP_SOURCE_SELECTOR = os.environ.get('YOUTUBE_SOURCE_SELECTOR', '').strip() or 'bestaudio[ext=webm]/bestaudio/best[acodec!=none]/best'
DIRECT_PREFETCH_CONCURRENCY = max(1, int(os.environ.get('VEEB_DIRECT_PREFETCH_CONCURRENCY', '3')))
BGUTIL_BASE_URL = os.environ.get('VEEB_BGUTIL_BASE_URL', 'http://127.0.0.1:4416').rstrip('/')
YTDLP_SOCKET_TIMEOUT_SECONDS = max(5, int(os.environ.get('VEEB_YTDLP_SOCKET_TIMEOUT', '15')))
YTDLP_EXTRACTOR_RETRIES = max(0, int(os.environ.get('VEEB_YTDLP_EXTRACTOR_RETRIES', '0')))
YTDLP_AUTH_CLIENT = os.environ.get('YOUTUBE_AUTH_FALLBACK_CLIENT', 'default').strip() or 'default'
RESOLVED_URL_FALLBACK_TTL_SECONDS = max(60, int(os.environ.get('VEEB_RESOLVED_URL_TTL', '1800')))
RESOLVED_URL_EXPIRY_MARGIN_SECONDS = max(30, int(os.environ.get('VEEB_RESOLVED_URL_EXPIRY_MARGIN', '120')))
RESOLVED_URL_MAX_ENTRIES = max(16, int(os.environ.get('VEEB_RESOLVED_URL_MAX_ENTRIES', '512')))
RESOLVE_TIMEOUT_SECONDS = max(15.0, float(os.environ.get('VEEB_RESOLVE_TIMEOUT', '45')))
UPSTREAM_CONNECT_TIMEOUT_SECONDS = max(2.0, float(os.environ.get('VEEB_UPSTREAM_CONNECT_TIMEOUT', '8')))
UPSTREAM_READ_TIMEOUT_SECONDS = max(10.0, float(os.environ.get('VEEB_UPSTREAM_READ_TIMEOUT', '45')))
MP3_BITRATE_KBPS = max(96, min(192, int(os.environ.get('VEEB_MP3_BITRATE_KBPS', '128'))))
MP3_CHUNK_BYTES = max(16 * 1024, int(os.environ.get('VEEB_MP3_CHUNK_BYTES', str(64 * 1024))))
MP3_FIRST_BYTE_TIMEOUT_SECONDS = max(2.0, float(os.environ.get('VEEB_MP3_FIRST_BYTE_TIMEOUT', '8')))
MP3_STARTUP_MIN_BYTES = max(4096, int(os.environ.get('VEEB_MP3_STARTUP_MIN_BYTES', '4096')))
MP3_MAX_CONCURRENT_TRANSCODES = max(1, int(os.environ.get('VEEB_MP3_MAX_CONCURRENT', '2')))
SOURCE_MODE = os.environ.get('VEEB_SOURCE_MODE', 'direct').strip().lower()
SOURCE_AGENT_SECRET = os.environ.get('VEEB_SOURCE_AGENT_SECRET', '')
SOURCE_DOWNLOAD_TIMEOUT = max(15, min(90, float(os.environ.get('VEEB_SOURCE_DOWNLOAD_TIMEOUT', '45'))))
YOUTUBE_PROXY_URL = os.environ.get('YOUTUBE_PROXY_URL', '').strip()
_source_guard = AcquisitionGuard()
_source_broker = SourceBroker()
_last_source_success = None

@dataclass
class ResolvedMedia:
    video_id: str
    url: str
    http_headers: dict[str, str]
    client: str
    format_id: str | None
    ext: str | None
    content_type: str | None
    acodec: str | None
    vcodec: str | None
    abr: float | None
    duration: float | None
    title: str | None
    resolved_at: float
    expires_at: float
    resolver_path: str

    def valid(self) -> bool:
        return time.time() < self.expires_at
_resolved_cache: dict[str, ResolvedMedia] = {}
_resolve_tasks: dict[str, asyncio.Task[ResolvedMedia]] = {}
_http_client: httpx.AsyncClient | None = None
_fg_auth_pool = None
_fg_mweb_auth_pool = None
_fg_pot_pool = None
_fg_android_pool = None
_fg_safari_pool = None
_fg_embedded_pool = None
_fg_visionos_pool = None
_direct_prefetch_sem = asyncio.Semaphore(DIRECT_PREFETCH_CONCURRENCY)
_mp3_transcode_sem = asyncio.Semaphore(MP3_MAX_CONCURRENT_TRANSCODES)
_mp3_cache_fill_sem = asyncio.Semaphore(1)
_resolve_task_purpose: dict[str, str] = {}
_youtube_cookie_header: str = ''
_youtube_cookie_authenticated = False
_active_intent_video_id: str | None = None

def require_auth(authorization: str | None) -> None:
    if not RESOLVER_SECRET:
        raise HTTPException(status_code=503, detail='RESOLVER_SECRET is not configured')
    if not secrets.compare_digest(authorization or '', f'Bearer {RESOLVER_SECRET}'):
        raise HTTPException(status_code=401, detail='Unauthorized')

def validate_video_id(video_id: str) -> str:
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise HTTPException(status_code=400, detail='Invalid YouTube video ID')
    return video_id

_cookie_store = None
_last_cookie_session_test = {'tested': False, 'youtubeReportsLoggedIn': None, 'status': 'not_tested'}

def get_writable_cookie_file() -> str | None:
    global _cookie_store, _last_cookie_session_test
    if (_cookie_store is None or str(_cookie_store.source) != YOUTUBE_COOKIE_FILE
            or str(_cookie_store.runtime) != WRITABLE_COOKIE_FILE):
        _cookie_store = CookieStore(YOUTUBE_COOKIE_FILE, WRITABLE_COOKIE_FILE)
    before = _cookie_store._digest
    snapshot = _cookie_store.snapshot()
    if before != _cookie_store._digest or not snapshot:
        _last_cookie_session_test = {'tested': False, 'youtubeReportsLoggedIn': None, 'status': 'not_tested'}
    return snapshot

def load_youtube_cookie_session(force: bool=False) -> bool:
    global _youtube_cookie_authenticated
    audit = inspect_cookies(YOUTUBE_COOKIE_FILE)
    _youtube_cookie_authenticated = audit['activeAuthCookieFieldsPresent']
    if force:
        print('v39.2 cookie file audit', json.dumps(audit), flush=True)
    return _youtube_cookie_authenticated

def pot_http_server_ready() -> bool:
    try:
        with socket.create_connection(('127.0.0.1', 4416), timeout=0.25):
            return True
    except OSError:
        return False

def parse_googlevideo_expiry(media_url: str) -> float | None:
    try:
        values = parse_qs(urlparse(media_url).query).get('expire')
        if not values:
            return None
        value = float(values[0])
        return value if value > time.time() else None
    except (TypeError, ValueError):
        return None

def resolved_expiry(media_url: str) -> float:
    now = time.time()
    google_expiry = parse_googlevideo_expiry(media_url)
    if google_expiry:
        return max(now + 30, google_expiry - RESOLVED_URL_EXPIRY_MARGIN_SECONDS)
    return now + RESOLVED_URL_FALLBACK_TTL_SECONDS

def cleanup_resolved_cache() -> None:
    now = time.time()
    for video_id in list(_resolved_cache):
        if _resolved_cache[video_id].expires_at <= now:
            _resolved_cache.pop(video_id, None)
    if len(_resolved_cache) <= RESOLVED_URL_MAX_ENTRIES:
        return
    oldest = sorted(_resolved_cache.values(), key=lambda item: item.resolved_at)
    for media in oldest[:len(_resolved_cache) - RESOLVED_URL_MAX_ENTRIES]:
        _resolved_cache.pop(media.video_id, None)

def get_cached_media(video_id: str) -> ResolvedMedia | None:
    media = _resolved_cache.get(video_id)
    if media and media.valid():
        return media
    if media:
        _resolved_cache.pop(video_id, None)
    return None

def invalidate_media(video_id: str) -> None:
    _resolved_cache.pop(video_id, None)

def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(connect=UPSTREAM_CONNECT_TIMEOUT_SECONDS, read=UPSTREAM_READ_TIMEOUT_SECONDS, write=UPSTREAM_READ_TIMEOUT_SECONDS, pool=UPSTREAM_CONNECT_TIMEOUT_SECONDS), limits=httpx.Limits(max_connections=40, max_keepalive_connections=20), http2=True)
    return _http_client

class YtdlpPhaseLogger:
    """Tiny yt-dlp logger that emits only cold-start milestones, never secrets."""

    def __init__(self, engine_id: str):
        self.engine_id = engine_id
        self._lock = threading.Lock()
        self.video_id = ''
        self.purpose = ''
        self.started = 0.0
        self._seen: set[str] = set()

    def begin(self, video_id: str, purpose: str) -> None:
        with self._lock:
            self.video_id = video_id
            self.purpose = purpose
            self.started = time.monotonic()
            self._seen = set()

    def _phase(self, phase: str) -> None:
        with self._lock:
            if not self.video_id or phase in self._seen:
                return
            self._seen.add(phase)
            elapsed = time.monotonic() - self.started
            payload = {'videoId': self.video_id, 'purpose': self.purpose, 'engine': self.engine_id, 'phase': phase, 'elapsedSeconds': round(elapsed, 3)}
        print('cold resolve phase', json.dumps(payload), flush=True)

    def debug(self, message: str) -> None:
        low = str(message).lower()
        if 'downloading webpage' in low:
            self._phase('webpage')
        elif 'player api json' in low:
            self._phase('player_api')
        elif 'generating a gvs po token' in low or 'generating pot' in low:
            self._phase('pot_request')
        elif 'solving js challenge' in low or 'solving js challenges' in low:
            self._phase('js_challenge')
        elif 'downloading player ' in low:
            self._phase('player_js')
        elif 'downloading 1 format' in low or 'format(s):' in low:
            self._phase('format_selected')

    def warning(self, message: str) -> None:
        self.debug(message)

    def error(self, message: str) -> None:
        self.debug(message)

def youtube_extractor_args_dict(client: str) -> dict[str, list[str]]:
    args = {}
    if client not in {'', 'default', 'anonymous'}:
        args['player_client'] = [client]
    if client in {'visionos', 'android'}:
        args['player_skip'] = ['webpage']
    if client == 'mweb':
        # Identical provider policy in cookie-free and authenticated modes.
        args['fetch_pot'] = ['auto']
    return args

def ytdlp_options(client_name: str, logger: YtdlpPhaseLogger, use_cookies: bool=False) -> dict[str, Any]:
    cookie_file = get_writable_cookie_file() if use_cookies else None
    if use_cookies and not cookie_file:
        raise SourceAttemptError('No usable Netscape cookie file with active login fields.',
                                 stage='cookie-file', code='COOKIE_FILE_UNUSABLE')
    selector = '18/best[acodec!=none]/bestaudio/best' if client_name == 'android' else YTDLP_SOURCE_SELECTOR
    opts = {'format': selector, 'skip_download': True, 'noplaylist': True,
            'quiet': True, 'no_warnings': False, 'verbose': True, 'cachedir': YTDLP_CACHE_DIR,
            'socket_timeout': YTDLP_SOCKET_TIMEOUT_SECONDS, 'retries': 0,
            'extractor_retries': YTDLP_EXTRACTOR_RETRIES, 'check_formats': False,
            'js_runtimes': {JSC_RUNTIME: {}},
            'extractor_args': {'youtube': youtube_extractor_args_dict(client_name),
                              'youtubepot-bgutilhttp': {'base_url': [BGUTIL_BASE_URL]}}, 'logger': logger}
    if cookie_file:
        opts['cookiefile'] = cookie_file
    if YOUTUBE_PROXY_URL:
        proxy = urlparse(YOUTUBE_PROXY_URL)
        if proxy.scheme not in {'http', 'https', 'socks5', 'socks5h'} or not proxy.hostname:
            raise SourceAttemptError('YOUTUBE_PROXY_URL must be an HTTP(S) or SOCKS5 proxy URL.', code='SOURCE_PROXY_CONFIG_INVALID')
        # yt-dlp also passes this same proxy to its PO-token provider.
        opts['proxy'] = YOUTUBE_PROXY_URL
    return opts
_extraction_sem = asyncio.Semaphore(1)
_source_download_sem = asyncio.Semaphore(MP3_MAX_CONCURRENT_TRANSCODES)
_fg_anon_pool = None

async def spawn_owned_process(*args, **kwargs):
    """Cancellation during spawn still waits for ownership, kills and reaps."""
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(*args, **kwargs))
    try:
        return await asyncio.shield(spawn)
    except asyncio.CancelledError:
        process = await spawn
        try:
            if kwargs.get('start_new_session'):
                os.killpg(process.pid, signal.SIGKILL)
            elif process.returncode is None:
                process.kill()
        except ProcessLookupError:
            pass
        await process.communicate()
        raise


class YtdlpEnginePool:
    """One owned process, cookie snapshot and bounded source directory per attempt."""
    def __init__(self, name: str, size: int, client_name: str, resolver_path: str, use_cookies: bool | None=None):
        self.name, self.client_name, self.resolver_path = name, client_name, resolver_path
        self.use_cookies = name == 'fg-auth' if use_cookies is None else bool(use_cookies)

    async def _child(self, video_id: str, *, download: bool, timeout: float, operation: str='source', directory=None) -> dict[str, Any]:
        async with source_slot(_extraction_sem, _source_download_sem, download=download) as lease:
            if operation == 'source':
                _source_guard.check()
            options = ytdlp_options(self.client_name, None, use_cookies=self.use_cookies)
            options.pop('logger', None)
            directory = (directory or tempfile.mkdtemp(prefix='veeb-ytdlp-source-')) if download else None
            if directory:
                lease.watch(directory)
            process = communication = None
            cookie_update_dir = cookie_update_path = cookie_digest = None
            keep_source = False
            try:
                if options.get('cookiefile'):
                    cookie_update_dir = tempfile.mkdtemp(prefix='veeb-cookie-update-')
                    cookie_update_path = str(Path(cookie_update_dir) / 'cookies.txt')
                    cookie_digest = hashlib.sha256(Path(options['cookiefile']).read_bytes()).hexdigest()
                process = await spawn_owned_process(
                    sys.executable, os.path.join(os.path.dirname(__file__), 'extract_source.py'),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, start_new_session=True)
                communication = asyncio.create_task(process.communicate(json.dumps({
                    'videoId': video_id, 'options': options, 'download': download,
                    'operation': operation, 'downloadDirectory': directory, 'cookieUpdatePath': cookie_update_path,
                }).encode()))
                stdout, stderr = await asyncio.wait_for(asyncio.shield(communication), timeout=timeout)
                try:
                    result = json.loads(stdout)
                except (ValueError, UnicodeError) as exc:
                    raise SourceAttemptError('Source process returned invalid JSON: ' + redact(stderr.decode('utf-8', 'replace'), 200),
                                             stage='process', code='EXTRACTOR_PROCESS_ERROR') from exc
                if not result.get('ok'):
                    raise SourceAttemptError(result.get('error') or 'Source attempt failed',
                        stage=result.get('stage') or 'extract', evidence=result.get('evidence'),
                        diagnostics=result.get('diagnostics'), code=result.get('code'))
                if directory:
                    path = Path(str((result.get('media') or {}).get('local_path') or ''))
                    if not path.is_file() or path.resolve().parent != Path(directory).resolve():
                        raise SourceAttemptError('Source process did not return its owned file', stage='download')
                    keep_source = True
                return result
            except asyncio.TimeoutError as exc:
                raise SourceAttemptError('Source download timed out' if download else 'Source extraction timed out',
                                         stage='download' if download else operation, code='SOURCE_TIMEOUT') from exc
            finally:
                if process is not None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    if communication is not None:
                        await asyncio.gather(communication, return_exceptions=True)
                    else:
                        await process.communicate()
                if cookie_update_dir:
                    try:
                        if _cookie_store is not None and cookie_update_path and Path(cookie_update_path).is_file():
                            _cookie_store.accept_update(cookie_digest, cookie_update_path)
                    except OSError:
                        pass
                    finally:
                        shutil.rmtree(cookie_update_dir, ignore_errors=True)
                if directory and not keep_source:
                    shutil.rmtree(directory, ignore_errors=True)

    def _media(self, video_id: str, info: dict[str, Any], *, resolver_path: str) -> ResolvedMedia:
        media = ResolvedMedia(video_id=video_id, url=info.get('url') or '', http_headers=info.get('http_headers') or {},
            client=self.client_name, format_id=info.get('format_id'), ext=info.get('ext'), content_type=info.get('container'),
            acodec=info.get('acodec'), vcodec=info.get('vcodec'), abr=info.get('abr'), duration=info.get('duration'),
            title=info.get('title'), resolved_at=time.time(), expires_at=resolved_expiry(info.get('url') or ''), resolver_path=resolver_path)
        media._uses_cookies = self.use_cookies
        if info.get('local_path'):
            media._local_path = str(info['local_path'])
        return media

    async def resolve(self, video_id: str, purpose: str) -> ResolvedMedia:
        result = await self._child(video_id, download=False, timeout=18.0)
        media = self._media(video_id, result['media'], resolver_path=self.resolver_path)
        media._source_evidence = result.get('evidence') or {}
        print('v39.2 source selected', json.dumps({'videoId': video_id, 'client': self.client_name,
              'usesCookies': self.use_cookies, 'formatId': media.format_id}), flush=True)
        return media

    async def download_source(self, video_id: str, purpose: str, *, directory=None) -> ResolvedMedia:
        result = await self._child(video_id, download=True, timeout=SOURCE_DOWNLOAD_TIMEOUT, directory=directory)
        media = self._media(video_id, result['media'], resolver_path=self.resolver_path + '-yt-dlp-download')
        media._source_evidence = result.get('evidence') or {}
        if not getattr(media, '_local_path', None):
            raise SourceAttemptError('Source download returned no local file', stage='download')
        try:
            probe = await probe_local_audio(media._local_path)
            duration = probe['duration']
            if media.duration is not None and abs(float(media.duration) - duration) > max(2, float(media.duration) * .01):
                raise SourceAttemptError('Downloaded source duration does not match the expected track.', stage='download', code='SOURCE_TRUNCATED')
            if media.duration is None:
                media.duration = duration
        except BaseException:
            cleanup_owned_source(media)
            raise
        print('v39.2 source downloaded by yt-dlp', json.dumps({'videoId': video_id, 'client': self.client_name,
              'usesCookies': self.use_cookies, 'formatId': media.format_id, 'bytes': result.get('downloadBytes')}), flush=True)
        return media

    async def stream_source(self, video_id: str, purpose: str) -> ResolvedMedia:
        owned = ProgressiveSource(
            lambda directory: self.download_source(video_id, purpose, directory=directory),
            lambda info: self._media(video_id, info, resolver_path=self.resolver_path + '-yt-dlp-progressive'))
        return await owned.start()

    async def inspect_session(self, video_id: str) -> dict[str, Any]:
        result = await self._child(video_id, download=False, timeout=12.0, operation='session')
        return result.get('cookieSessionTest') or {'tested': False, 'youtubeReportsLoggedIn': None, 'status': 'unknown'}

    def close(self) -> None:
        pass

def init_ytdlp_pools() -> None:
    global _fg_auth_pool, _fg_pot_pool, _fg_anon_pool, _fg_mweb_auth_pool
    if _fg_anon_pool is None:
        _fg_pot_pool = YtdlpEnginePool('fg-pot', 1, 'mweb', 'yt-dlp-mweb-v39.2', False)
        _fg_mweb_auth_pool = YtdlpEnginePool('fg-mweb-auth', 1, 'mweb', 'yt-dlp-mweb-auth-v39.2', True)
        _fg_anon_pool = YtdlpEnginePool('fg-anon', 1, 'anonymous', 'yt-dlp-anonymous-v39.2', False)
        _fg_auth_pool = YtdlpEnginePool('fg-auth', 1, YTDLP_AUTH_CLIENT, 'yt-dlp-auth-v39.2', True)


def foreground_pools():
    init_ytdlp_pools()
    cookies = bool(get_writable_cookie_file())
    # The earlier fast resolver used cookies on its first mweb attempt. Do not
    # make a usable session wait behind an anonymous bot rejection. A negative
    # session diagnostic disables cookie attempts until the secret changes.
    rejected = _last_cookie_session_test.get('youtubeReportsLoggedIn') is False
    use_auth = cookies and not rejected
    pools = [_fg_mweb_auth_pool] if use_auth else []
    pools.extend([_fg_pot_pool, _fg_anon_pool])
    if use_auth and YTDLP_AUTH_CLIENT != 'mweb':
        pools.append(_fg_auth_pool)
    return pools


def attempt_detail(pool, error, stage='extract', suffix=''):
    return {'path': (pool.name if pool else 'cached') + suffix,
            'client': pool.client_name if pool else 'cached', 'usesCookies': bool(pool and pool.use_cookies),
            'stage': getattr(error, 'stage', stage), 'code': getattr(error, 'code', failure_code(error, stage)),
            'error': redact(error, 350), 'evidence': getattr(error, 'evidence', {}),
            'diagnostics': [redact(x, 180) for x in getattr(error, 'diagnostics', [])][-2:]}

async def resolve_ytdlp_foreground_v35(video_id: str, purpose: str) -> ResolvedMedia:
    errors = []
    for pool in foreground_pools():
        try:
            return await pool.resolve(video_id, purpose)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            errors.append(attempt_detail(pool, exc))
    error = SourceAttemptError('All configured source attempts failed; inspect per-attempt evidence.', code='SOURCE_ACQUISITION_FAILED')
    error.attempts = errors
    raise error

async def resolve_live_cold_v35(video_id: str, purpose: str) -> ResolvedMedia:
    return await resolve_ytdlp_foreground_v35(video_id, purpose)

async def resolve_prefetch_v35(video_id: str, purpose: str) -> ResolvedMedia:
    cached = get_cached_media(video_id)
    if cached:
        return cached
    raise RuntimeError('speculative extraction paused while acquisition is being verified')

async def resolve_media_uncached(video_id: str, purpose: str) -> ResolvedMedia:
    cached = get_cached_media(video_id)
    if cached:
        return cached
    if purpose == 'live-intent':
        media = await resolve_prefetch_v35(video_id, purpose)
    elif purpose.startswith('live'):
        media = await resolve_live_cold_v35(video_id, purpose)
    else:
        media = await resolve_prefetch_v35(video_id, purpose)
    _resolved_cache[video_id] = media
    cleanup_resolved_cache()
    return media

def resolve_task_finished(video_id: str, task: asyncio.Task[ResolvedMedia]) -> None:
    if _resolve_tasks.get(video_id) is task:
        _resolve_tasks.pop(video_id, None)
        _resolve_task_purpose.pop(video_id, None)
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        print('background resolve failed', json.dumps({'videoId': video_id, 'error': str(exc)[-1600:]}), flush=True)

def start_resolve_task(video_id: str, purpose: str) -> asyncio.Task[ResolvedMedia]:
    existing = _resolve_tasks.get(video_id)
    if existing and (not existing.done()):
        return existing

    async def runner() -> ResolvedMedia:
        if purpose == 'prefetch':
            async with _direct_prefetch_sem:
                return await resolve_media_uncached(video_id, purpose)
        return await resolve_media_uncached(video_id, purpose)
    task = asyncio.create_task(asyncio.wait_for(runner(), timeout=RESOLVE_TIMEOUT_SECONDS))
    _resolve_tasks[video_id] = task
    _resolve_task_purpose[video_id] = purpose
    task.add_done_callback(lambda done: resolve_task_finished(video_id, done))
    return task

async def get_or_resolve(video_id: str, purpose: str) -> tuple[ResolvedMedia, str]:
    cached = get_cached_media(video_id)
    if cached:
        return (cached, 'HIT')
    task = _resolve_tasks.get(video_id)
    if task and (not task.done()):
        existing_purpose = _resolve_task_purpose.get(video_id, '')
        if purpose == 'live' and existing_purpose == 'live-intent':
            global _active_intent_video_id
            if _resolve_tasks.get(video_id) is task:
                _resolve_tasks.pop(video_id, None)
                _resolve_task_purpose.pop(video_id, None)
            task.cancel()
            if _active_intent_video_id == video_id:
                _active_intent_video_id = None
            print('foreground replacing direct-only intent resolver', json.dumps({'videoId': video_id}), flush=True)
            foreground = start_resolve_task(video_id, 'live')
            return (await asyncio.shield(foreground), 'MISS-AFTER-INTENT')
        if purpose == 'live' and existing_purpose == 'prefetch':
            print('foreground promoting speculative resolve to single-flight', json.dumps({'videoId': video_id}), flush=True)

            async def foreground_runner() -> ResolvedMedia:
                return await resolve_media_uncached(video_id, 'live')
            foreground = asyncio.create_task(foreground_runner())
            _resolve_tasks[video_id] = foreground
            _resolve_task_purpose[video_id] = 'live'
            foreground.add_done_callback(lambda done: resolve_task_finished(video_id, done))
            return (await asyncio.shield(foreground), 'MISS-FOREGROUND')
        return (await asyncio.shield(task), 'WAIT')
    task = start_resolve_task(video_id, purpose)
    return (await asyncio.shield(task), 'MISS')

def build_transcode_source_headers(media: ResolvedMedia) -> dict[str, str]:
    """Headers for the disposable source fetch feeding FFmpeg.

    A live MP3 transcode always starts at the beginning of the source. Browser
    byte ranges apply only to completed R2 MP3 objects, never to this source.
    """
    blocked = {'authorization', 'cookie', 'host', 'content-length', 'connection', 'transfer-encoding', 'range'}
    headers = {k: v for k, v in media.http_headers.items() if k.lower() not in blocked}
    headers['Accept-Encoding'] = 'identity'
    return headers

def live_mp3_headers(media: ResolvedMedia, cache_state: str, request: Request) -> dict[str, str]:
    return {'Content-Type': 'audio/mpeg', 'Cache-Control': 'private, no-store', 'Accept-Ranges': 'none', 'X-Content-Type-Options': 'nosniff', 'X-Veeb-Resolver': 'v39.2-mp3-stream', 'X-Veeb-Resolved-Cache': cache_state, 'X-Veeb-Playback-Client': media.client, 'X-Veeb-Source-Format': media.format_id or YTDLP_SOURCE_SELECTOR, 'X-Veeb-Resolver-Path': media.resolver_path, 'X-Veeb-Direct-Proxy': '0', 'X-Veeb-Transcode': 'verified-agent-mp3' if media.client == 'source-agent' else 'ffmpeg-owned-source-mp3', 'X-Veeb-MP3-Bitrate': str(MP3_BITRATE_KBPS), 'X-Veeb-Ignored-Range': '1' if request.headers.get('range') else '0'}

def ffmpeg_http_input_args(media: ResolvedMedia) -> list[str]:
    """Build FFmpeg HTTP input options for the resolved Googlevideo URL.

    FFmpeg owns the upstream HTTP connection. That is intentional: selected source
    containers may require seeking/range requests before audio decoding can start.
    """
    headers = build_transcode_source_headers(media)
    user_agent = ''
    header_lines: list[str] = []
    for raw_name, raw_value in headers.items():
        name = str(raw_name or '').strip()
        value = str(raw_value or '').strip()
        if not name or not value or '\r' in name or ('\n' in name) or ('\r' in value) or ('\n' in value):
            continue
        if name.lower() == 'user-agent':
            user_agent = value
            continue
        header_lines.append(f'{name}: {value}\r\n')
    args = ['-rw_timeout', '15000000', '-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_delay_max', '2']
    if user_agent:
        args += ['-user_agent', user_agent]
    if header_lines:
        args += ['-headers', ''.join(header_lines)]
    return args

async def _read_ffmpeg_stderr(process: asyncio.subprocess.Process) -> bytes:
    try:
        if process.stderr is None:
            return b''
        return await process.stderr.read()
    except Exception:
        return b''

async def prepare_live_mp3_stream(media: ResolvedMedia, video_id: str, request: Request) -> AsyncIterator[bytes]:
    """Prove MP3 startup from an owned source; download success still gates completion."""
    purpose = (request.headers.get('x-veeb-purpose') or 'playback').strip().lower()
    cache_fill = purpose == 'cache-fill'
    owned = getattr(media, '_owned_download', None)
    cache_slot = False
    try:
        if cache_fill:
            await _mp3_cache_fill_sem.acquire()
            cache_slot = True
        await _mp3_transcode_sem.acquire()
    except BaseException:
        if cache_slot:
            _mp3_cache_fill_sem.release()
        if owned is not None:
            await owned.close()
        cleanup_owned_source(media)
        raise
    process: asyncio.subprocess.Process | None = None
    stderr_task: asyncio.Task[bytes] | None = None
    feeder = None
    released = False

    async def release_slots() -> None:
        nonlocal released
        if released:
            return
        released = True
        _mp3_transcode_sem.release()
        if cache_fill:
            _mp3_cache_fill_sem.release()

    async def cleanup(kill: bool=False) -> bytes:
        if process is not None and kill and (process.returncode is None):
            try:
                process.kill()
            except ProcessLookupError:
                pass
        if process is not None:
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except Exception:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
        stderr = b''
        if stderr_task is not None:
            try:
                stderr = await asyncio.wait_for(asyncio.shield(stderr_task), timeout=1.0)
            except Exception:
                pass
        if feeder is not None:
            if not feeder.done():
                feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)
        if owned is not None:
            await owned.close()
        cleanup_owned_source(media)
        await release_slots()
        return stderr
    try:
        source_path = getattr(media, '_local_path', None)
        if owned is not None:
            input_args = ['-protocol_whitelist', 'file,pipe', '-f', 'matroska', '-probesize', '32768', '-analyzeduration', '100000']
            input_value = 'pipe:0'
        else:
            input_args = ['-protocol_whitelist', 'file,pipe'] if source_path else ffmpeg_http_input_args(media)
            input_value = source_path or media.url
        process = await spawn_owned_process('ffmpeg', '-hide_banner', '-loglevel', 'error', '-xerror', '-nostdin', *input_args, '-i', input_value, '-map', '0:a:0', '-vn', '-map_metadata', '-1', '-threads', '1', '-c:a', 'libmp3lame', '-b:a', f'{MP3_BITRATE_KBPS}k', '-ac', '2', '-ar', '44100', '-id3v2_version', '0', '-write_xing', '0', '-f', 'mp3', '-flush_packets', '1', 'pipe:1', stdin=asyncio.subprocess.PIPE if owned else asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        if owned is not None:
            feeder = asyncio.create_task(owned.feed(process.stdin))
        stderr_task = asyncio.create_task(_read_ffmpeg_stderr(process))
        assert process.stdout is not None
        startup_buffer = bytearray()
        startup_deadline = time.monotonic() + MP3_FIRST_BYTE_TIMEOUT_SECONDS
        while len(startup_buffer) < MP3_STARTUP_MIN_BYTES:
            remaining = startup_deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError('FFmpeg MP3 first-byte timeout')
            chunk = await asyncio.wait_for(process.stdout.read(min(MP3_CHUNK_BYTES, MP3_STARTUP_MIN_BYTES - len(startup_buffer))), timeout=remaining)
            if not chunk:
                break
            startup_buffer.extend(chunk)
        first_chunk = bytes(startup_buffer)
        if len(first_chunk) < MP3_STARTUP_MIN_BYTES:
            stderr = await cleanup(kill=True)
            raise RuntimeError(f'FFmpeg produced only {len(first_chunk)} startup MP3 bytes' + (': ' + redact_source_error(stderr.decode('utf-8', 'replace')) if stderr else ''))
        print('v39.2 mp3 first bytes ready', json.dumps({'videoId': video_id, 'purpose': purpose, 'sourceFormat': media.format_id, 'resolverPath': media.resolver_path, 'firstChunkBytes': len(first_chunk), 'bitrateKbps': MP3_BITRATE_KBPS}), flush=True)

        async def body() -> AsyncIterator[bytes]:
            try:
                yield first_chunk
                assert process is not None and process.stdout is not None
                while True:
                    chunk = await asyncio.wait_for(process.stdout.read(MP3_CHUNK_BYTES), timeout=20.0)
                    if not chunk:
                        break
                    yield chunk
                if feeder is not None:
                    await feeder
                rc = await process.wait()
                stderr = b''
                if stderr_task is not None:
                    try:
                        stderr = await stderr_task
                    except Exception:
                        pass
                if rc != 0:
                    print('v39.2 ffmpeg ended non-zero', json.dumps({'videoId': video_id, 'returnCode': rc, 'error': redact_source_error(stderr.decode('utf-8', 'replace'))}), flush=True)
                    raise RuntimeError('FFmpeg failed before MP3 completion')
            except asyncio.CancelledError:
                raise
            finally:
                await cleanup(kill=True)
        return body()
    except BaseException:
        await cleanup(kill=True)
        raise

def redact_source_error(message: str) -> str:
    return redact(message, 600)

def error_detail(error: BaseException) -> dict[str, str]:
    if isinstance(error, (JobError, SourceAttemptError)):
        return {'code': error.code, 'message': redact(error, 450)}
    if isinstance(error, asyncio.TimeoutError):
        return {'code': 'SOURCE_TIMEOUT', 'message': 'Acquisition or conversion exceeded its time budget.'}
    return {'code': failure_code(error), 'message': redact(error, 450)}

async def produce_mp3(video_id: str, request: Request, job):
    global _last_source_success
    imported = getattr(request, 'scope', {}).get('_veeb_import_media')
    if imported is not None:
        job.metadata.update(media=imported, cache='R2-IMPORT', attempts=[])
        return await prepare_live_mp3_stream(imported, video_id, request)

    attempts = []
    job.metadata['attempts'] = attempts
    if SOURCE_MODE == 'agent':
        if not SOURCE_AGENT_SECRET:
            raise JobError('SOURCE_AGENT_NOT_CONFIGURED', 'Set VEEB_SOURCE_AGENT_SECRET on Render and the source computer.')
        media = await _source_broker.acquire(video_id)
        media._output_bitrate = MP3_BITRATE_KBPS
        job.metadata.update(media=media, cache='MISS-AGENT')
        attempts.append({'path': 'source-agent', 'stage': 'mp3-upload-verified', 'ok': True})
        return owned_mp3_reader(media)
    if SOURCE_MODE != 'direct':
        raise JobError('SOURCE_MODE_INVALID', 'VEEB_SOURCE_MODE must be direct or agent.')

    _source_guard.check()
    try:
        async with asyncio.timeout(20):
            for pool in foreground_pools():
                media = None
                try:
                    # One yt-dlp process owns the source. Direct WebM can feed
                    # FFmpeg while downloading; other containers remain seekable.
                    media = await pool.stream_source(video_id, 'live-download')
                    media._output_bitrate = MP3_BITRATE_KBPS
                    iterator = await prepare_live_mp3_stream(media, video_id, request)
                    job.metadata.update(media=media, cache='MISS-PROGRESSIVE' if getattr(media, '_owned_download', None) else 'MISS-DOWNLOADED')
                    attempts.append({'path': pool.name, 'stage': 'mp3-startup', 'ok': True,
                                     'usesCookies': pool.use_cookies, 'sourceFormat': media.format_id,
                                     'evidence': getattr(media, '_source_evidence', {})})
                    _source_guard.succeeded()
                    _last_source_success = {'videoId': video_id, 'checkedAtUnix': int(time.time()),
                                            'route': 'proxy' if YOUTUBE_PROXY_URL else 'direct',
                                            'scope': 'Source bytes and MP3 startup verified; full download and MP3 completion are separate'}
                    return iterator
                except asyncio.CancelledError:
                    if media:
                        cleanup_owned_source(media)
                    raise
                except Exception as exc:
                    if media:
                        cleanup_owned_source(media)
                    detail = attempt_detail(pool, exc, 'download')
                    attempts.append(detail)
                    print('v39.2 source attempt failed', json.dumps({'videoId': video_id, **detail}), flush=True)
                    if getattr(exc, 'code', '') in {'SOURCE_ROUTE_COOLDOWN', 'SOURCE_PROXY_CONFIG_INVALID', 'SOURCE_DURATION_UNSUPPORTED'}:
                        raise
    except TimeoutError as exc:
        attempts.append({'path': 'acquisition', 'stage': 'deadline', 'code': 'SOURCE_TIMEOUT'})
        raise JobError('SOURCE_TIMEOUT', 'Source startup exceeded 20 seconds. Inspect per-attempt source errors.') from exc
    codes = {a.get('code') for a in attempts if a.get('code')}
    _source_guard.failed(video_id, codes)
    if codes and codes <= AcquisitionGuard.BLOCK_CODES:
        message = (
            'YouTube refused source access. Its session check reports the saved cookies are not logged in. '
            'Replace the YouTube cookie secret file and run the playback test again. Cached tracks can still play.'
            if _last_cookie_session_test.get('youtubeReportsLoggedIn') is False else
            'YouTube refused source access on this route. This does not prove bad cookies or an IP ban. '
            'Cached tracks can still play. Inspect the source-access diagnostic before retrying.')
        error = JobError('SOURCE_ACCESS_DENIED',
                         message)
        error.retry_after = 120
        raise error
    raise JobError('SOURCE_ACQUISITION_FAILED', 'Source attempts failed. Per-attempt stages identify extraction, download or conversion failure.')


def cleanup_owned_source(media):
    path = getattr(media, '_local_path', None)
    if path:
        shutil.rmtree(Path(path).parent, ignore_errors=True)


async def owned_mp3_reader(media):
    try:
        with open(media._local_path, 'rb') as source:
            while chunk := source.read(65536):
                yield chunk
    finally:
        cleanup_owned_source(media)

_media_jobs = MediaJobs(produce_mp3)

async def proxy_media(request: Request, video_id: str):
    cache_fill = request.headers.get('x-veeb-purpose') == 'cache-fill'
    if request.method == 'HEAD':
        return Response(status_code=200, headers={'Content-Type': 'audio/mpeg', 'Accept-Ranges': 'none', 'X-Veeb-Resolver': 'v39.2-mp3-stream'})
    try:
        job = _media_jobs.get(video_id, request, background=cache_fill)
        await _media_jobs.wait(job, complete=cache_fill)
        headers = live_mp3_headers(job.metadata['media'], job.metadata['cache'], request)
        headers['X-Veeb-MP3-Job'] = job.job_id
        if cache_fill or job.done.is_set():
            headers['Content-Length'] = str(job.size)
            headers['X-Veeb-MP3-Complete'] = '1'
        body, close = _media_jobs.open_reader(job)
        return StreamingResponse(body, headers=headers, media_type='audio/mpeg', background=BackgroundTask(close))
    except Exception as exc:
        detail = error_detail(exc)
        status = 503 if detail['code'] in {'RESOLVER_BUSY', 'SOURCE_ROUTE_COOLDOWN', 'SOURCE_AGENT_OFFLINE'} else 502
        return JSONResponse({'ok': False, 'stage': 'source-or-transcode', **detail}, status_code=status, headers={'Cache-Control': 'no-store', 'Retry-After': str(getattr(exc, 'retry_after', 15)), 'X-Veeb-Error-Code': detail['code']})

@app.on_event('startup')
async def startup_session() -> None:
    load_youtube_cookie_session(force=True)
    get_http_client()
    init_ytdlp_pools()
    print('v39.2 ready: session-aware mweb priority; independent extraction/download slots; shared MP3 jobs', flush=True)

@app.on_event('shutdown')
async def shutdown_http_client() -> None:
    global _http_client
    await _media_jobs.close()
    tasks = list(_resolve_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None

@app.head('/')
async def root_head() -> Response:
    return Response(status_code=200)

@app.get('/')
async def root() -> dict[str, Any]:
    return {'ok': True, 'service': 'veeb-resolver', 'version': 'v39.2-mp3-stream'}

@app.get('/health')
async def health(authorization: str | None=Header(default=None)) -> dict[str, Any]:
    require_auth(authorization)
    cleanup_resolved_cache()
    get_writable_cookie_file()
    try:
        version = importlib.metadata.version('yt-dlp')
    except importlib.metadata.PackageNotFoundError:
        version = 'unknown'
    audit, stats = inspect_cookies(YOUTUBE_COOKIE_FILE), _media_jobs.stats()
    return {'ok': True, 'service': 'veeb-resolver', 'version': 'v39.2-mp3-stream',
            'ytDlpVersion': version, 'sourceSelector': YTDLP_SOURCE_SELECTOR,
            'cookieFilePresent': audit['present'], 'cookieAuthFieldsPresent': audit['activeAuthCookieFieldsPresent'],
            'cookieSessionRecognized': _last_cookie_session_test.get('youtubeReportsLoggedIn'),
            'cookieAuthenticationVerified': _last_cookie_session_test.get('youtubeReportsLoggedIn'),
            'cookieAuthenticationVerificationScope': 'YouTube page-level login, not media permission',
            'cookieAudit': audit, 'cookieSessionTest': _last_cookie_session_test,
            'acquisitionModes': [{'path': p.name, 'client': p.client_name, 'usesCookies': p.use_cookies} for p in foreground_pools()],
            'potHttpReady': pot_http_server_ready(), 'ffmpegInstalled': bool(shutil.which('ffmpeg')),
            'jsRuntimeInstalled': bool(shutil.which(JSC_RUNTIME)), 'sourceAccessVerified': _last_source_success is not None,
            'lastSourceSuccess': _last_source_success, 'sourceMode': SOURCE_MODE,
            'sourceProxyConfigured': bool(YOUTUBE_PROXY_URL), 'sourceRoute': _source_guard.status(),
            'sourceAgent': _source_broker.status(), 'sourceAgentConfigured': bool(SOURCE_AGENT_SECRET),
            'sourceDownloadOwner': 'yt-dlp', 'sourceFragmentsMayBeSkipped': False,
            'progressiveSource': 'direct-audio-webm', 'mp3StartupBytes': MP3_STARTUP_MIN_BYTES,
            'sourceStartupDeadlineSeconds': 20,
            'note': 'Liveness only. Current completed jobs prove MP3 production; cookie fields alone prove no login.',
            'deliveryFormat': 'audio/mpeg', 'speculativeExtraction': False, 'extractionConcurrency': 1,
            'sourceDownloadConcurrency': MP3_MAX_CONCURRENT_TRANSCODES,
            'extractionSlotReleasedAfterSourceBytes': True,
            'completedMp3Endpoint': '/completed/{videoId}', 'completedEndpointStartsAcquisition': False, 'maxConcurrentTranscodes': MP3_MAX_CONCURRENT_TRANSCODES, 'jobs': stats}

async def codec_self_test():
    """Exercise this instance's encoder without contacting YouTube or R2."""
    directory = tempfile.mkdtemp(prefix='veeb-codec-test-')
    path, iterator = Path(directory) / 'source.wav', None
    try:
        with wave.open(str(path), 'wb') as output:
            output.setnchannels(2)
            output.setsampwidth(2)
            output.setframerate(44100)
            output.writeframes(bytes(44100 * 2 * 2 * 3))
        media = ResolvedMedia('codec-test', '', {}, 'local-self-test', 'wav', 'wav', 'audio/wav',
                              'pcm_s16le', 'none', None, 3, 'self-test', time.time(), time.time()+30, 'local-self-test')
        media._local_path = str(path)
        request = Request({'type': 'http', 'method': 'GET', 'headers': []})
        iterator = await prepare_live_mp3_stream(media, 'codec-test', request)
        data = b''.join([chunk async for chunk in iterator])
        valid = mp3_frames_valid(data)
        return {'ok': valid, 'mp3Bytes': len(data), 'mp3FramesValid': valid,
                'scope': 'Synthetic source through the production FFmpeg encoder; no YouTube/R2 request'}
    finally:
        if iterator is not None:
            await iterator.aclose()
        shutil.rmtree(directory, ignore_errors=True)


@app.post('/diagnose/{video_id}')
async def diagnose(video_id: str, authorization: str | None=Header(default=None)):
    global _last_cookie_session_test
    require_auth(authorization)
    video_id = validate_video_id(video_id)
    started, audit = time.monotonic(), inspect_cookies(YOUTUBE_COOKIE_FILE)
    try:
        codec_test = await asyncio.wait_for(codec_self_test(), timeout=12)
    except Exception as exc:
        codec_test = {'ok': False, 'error': redact(exc, 180)}
    if not codec_test['ok']:
        return JSONResponse({'ok': False, 'version': 'v39.2-mp3-stream', 'videoId': video_id,
                             'code': 'MP3_SELF_TEST_FAILED', 'codecSelfTest': codec_test, 'cookieAudit': audit}, status_code=424)
    init_ytdlp_pools()
    if SOURCE_MODE == 'agent':
        _last_cookie_session_test = {'tested': False, 'youtubeReportsLoggedIn': None, 'status': 'session_owned_by_source_agent'}
    elif get_writable_cookie_file():
        try:
            _last_cookie_session_test = await asyncio.wait_for(_fg_mweb_auth_pool.inspect_session(video_id), timeout=15)
        except Exception as exc:
            _last_cookie_session_test = {'tested': True, 'youtubeReportsLoggedIn': None, 'status': 'request_failed', 'error': redact(exc, 180)}
    else:
        _last_cookie_session_test = {'tested': False, 'youtubeReportsLoggedIn': None, 'status': 'no_usable_cookie_file'}
    _last_cookie_session_test['checkedAtUnix'] = int(time.time())
    report = {'version': 'v39.2-mp3-stream', 'videoId': video_id, 'cookieAudit': audit,
              'sourceMode': SOURCE_MODE, 'sourceAgent': _source_broker.status(),
              'cookieSessionTest': dict(_last_cookie_session_test), 'codecSelfTest': codec_test}
    request = Request({'type': 'http', 'method': 'GET', 'headers': []})
    job = None
    try:
        job = _media_jobs.get(video_id, request)
        await _media_jobs.wait(job, complete=True)
        media = job.metadata['media']
        report.update(ok=True, mp3Complete=True, bytes=job.size, jobId=job.job_id,
                      sourceClient=media.client, sourceFormat=media.format_id,
                      sourceUsedCookies=bool(getattr(media, '_uses_cookies', False)))
    except Exception as exc:
        report.update(ok=False, **error_detail(exc))
    report['attempts'] = job.metadata.get('attempts', [])[-6:] if job else []
    report['elapsedSeconds'] = round(time.monotonic()-started, 2)
    # The existing Worker limits resolver response JSON to 8192 bytes.
    if len(json.dumps(report).encode()) > 7400:
        for attempt in report['attempts']:
            attempt.pop('diagnostics', None)
    return JSONResponse(report, status_code=200 if report['ok'] else 424, headers={'Cache-Control': 'no-store'})

@app.get('/resolve/{video_id}')
async def resolve_endpoint(video_id: str, authorization: str | None=Header(default=None)) -> JSONResponse:
    require_auth(authorization)
    video_id = validate_video_id(video_id)
    try:
        request = Request({'type': 'http', 'method': 'GET', 'headers': []})
        job = _media_jobs.get(video_id, request)
        await _media_jobs.wait(job)
        media, cache_state = job.metadata['media'], job.metadata['cache']
    except Exception as exc:
        return JSONResponse({'ok': False, **error_detail(exc)}, status_code=502)
    return JSONResponse({'provider': 'veeb-v37.6-mp3-resolver', 'videoId': video_id, 'title': media.title, 'duration': media.duration, 'formatId': media.format_id, 'client': media.client, 'resolverPath': media.resolver_path, 'cache': cache_state, 'expiresInSeconds': max(0, int(media.expires_at - time.time())), 'proxied': True})

@app.post('/prefetch/{video_id}')
async def prefetch_endpoint(video_id: str, intent: int=Query(default=0), authorization: str | None=Header(default=None)) -> JSONResponse:
    require_auth(authorization)
    video_id = validate_video_id(video_id)
    return JSONResponse({'ok': True, 'videoId': video_id, 'status': 'cached' if get_cached_media(video_id) else 'deferred', 'reason': 'foreground-first'}, status_code=202)

@app.post('/prefetch-batch')
async def prefetch_batch_endpoint(request: Request, authorization: str | None=Header(default=None)) -> JSONResponse:
    require_auth(authorization)
    body = await request.json()
    raw_ids = body.get('videoIds') if isinstance(body, dict) else None
    if not isinstance(raw_ids, list):
        raise HTTPException(status_code=400, detail='videoIds must be an array')
    video_ids: list[str] = []
    for raw in raw_ids[:8]:
        value = str(raw)
        if VIDEO_ID_RE.fullmatch(value) and value not in video_ids:
            video_ids.append(value)
    statuses = []
    for video_id in video_ids:
        if get_cached_media(video_id):
            statuses.append({'videoId': video_id, 'status': 'cached'})
            continue
        statuses.append({'videoId': video_id, 'status': 'deferred'})
    return JSONResponse({'ok': True, 'tracks': statuses}, status_code=202)

@app.api_route('/stream/{video_id}', methods=['GET', 'HEAD'])
async def stream_endpoint(request: Request, video_id: str, authorization: str | None=Header(default=None)):
    require_auth(authorization)
    video_id = validate_video_id(video_id)
    return await proxy_media(request, video_id)

os.makedirs(YTDLP_CACHE_DIR, exist_ok=True)


# V39.2: a read-only handoff for R2. This route MUST NEVER call get(), resolve,
# a downloader, or FFmpeg. A miss is cheap and cannot steal a playback slot.
@app.api_route('/completed/{video_id}', methods=['GET', 'HEAD'])
async def completed_mp3(video_id: str, request: Request,
                        authorization: str | None=Header(default=None)):
    require_auth(authorization)
    video_id = validate_video_id(video_id)
    _media_jobs._evict()
    job = _media_jobs.jobs.get(video_id)
    common = {'Cache-Control': 'no-store', 'X-Veeb-Resolver': 'v39.2-mp3-stream'}
    if job is None:
        return JSONResponse({'ok': False, 'code': 'COMPLETED_MP3_MISS',
                             'startsAcquisition': False}, status_code=404, headers=common)
    if not job.done.is_set():
        return JSONResponse({'ok': False, 'code': 'MP3_STILL_RUNNING',
                             'startsAcquisition': False}, status_code=202,
                            headers={**common, 'Retry-After': '15'})
    if job.error:
        return JSONResponse({'ok': False, 'code': 'MP3_JOB_FAILED',
                             'startsAcquisition': False}, status_code=424, headers=common)
    media = job.metadata.get('media')
    if not media or job.size <= 0 or not job.path.is_file():
        return JSONResponse({'ok': False, 'code': 'COMPLETED_MP3_MISS',
                             'startsAcquisition': False}, status_code=404, headers=common)
    # Preserve the file long enough for the following GET and use a reader-owned
    # descriptor so ordinary eviction cannot remove a file while it is streamed.
    job.touched = time.monotonic()
    headers = live_mp3_headers(media, job.metadata.get('cache', 'LOCAL'), request)
    headers.update(common)
    headers.update({'Content-Length': str(job.size), 'X-Veeb-MP3-Complete': '1',
                    'X-Veeb-MP3-Job': job.job_id, 'X-Veeb-Starts-Acquisition': '0'})
    if request.method == 'HEAD':
        return Response(headers=headers)
    body, close = _media_jobs.open_reader(job)
    return StreamingResponse(body, headers=headers, media_type='audio/mpeg',
                             background=BackgroundTask(close))


_import_sem = asyncio.Semaphore(1)
_IMPORT_MAX_BYTES = 80 * 1024 * 1024

@app.post('/import-cached-source/{video_id}')
async def import_cached_source(video_id: str, request: Request,
                               authorization: str | None=Header(default=None)):
    """Authenticated binary import, no URL fetching and no YouTube access.

    The Worker selects an existing, exact legacy R2 key. Uploads are bounded;
    playlists/manifests are rejected and probing uses local protocols only.
    """
    require_auth(authorization)
    video_id = validate_video_id(video_id)
    try:
        size = int(request.headers.get('content-length', '0'))
    except ValueError:
        size = 0
    if size <= 0 or size > _IMPORT_MAX_BYTES:
        return JSONResponse({'ok': False, 'code': 'IMPORT_SIZE_INVALID'}, status_code=413)
    try:
        await asyncio.wait_for(_import_sem.acquire(), timeout=0.05)
    except asyncio.TimeoutError:
        return JSONResponse({'ok': False, 'code': 'IMPORT_BUSY'}, status_code=503)
    directory = None
    owned_by_job = False
    try:
        _media_jobs._evict()
        existing = _media_jobs.jobs.get(video_id)
        if existing:
            if not existing.done.is_set() or existing.readers or existing.waiters:
                return JSONResponse({'ok': False, 'code': 'TRACK_BUSY'}, status_code=409)
            if not existing.error and existing.path.is_file():
                return {'ok': True, 'mp3Complete': True, 'bytes': existing.size,
                        'jobId': existing.job_id, 'reused': True}
            _media_jobs._remove(video_id)
        directory = tempfile.mkdtemp(prefix='veeb-cache-import-')
        path = Path(directory) / 'source.media'
        received = 0
        async with asyncio.timeout(90):
            with path.open('wb') as output:
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > size or received > _IMPORT_MAX_BYTES:
                        return JSONResponse({'ok': False, 'code': 'IMPORT_SIZE_MISMATCH'}, status_code=413)
                    output.write(chunk)
        if received != size:
            return JSONResponse({'ok': False, 'code': 'IMPORT_SIZE_MISMATCH'}, status_code=400)
        # Restrict input to actual binary MP4/WebM/WAV/MP3 containers. This route
        # is not a URL/playlist relay, including for authenticated callers.
        with path.open('rb') as source:
            prefix = source.read(16)
        binary = (prefix[4:8] == b'ftyp' or prefix[:4] == b'\x1aE\xdf\xa3' or
                  prefix[:4] == b'RIFF' or prefix[:3] == b'ID3' or
                  (len(prefix) >= 2 and prefix[0] == 255 and prefix[1] & 0xE0 == 0xE0))
        if not binary:
            return JSONResponse({'ok': False, 'code': 'IMPORT_CONTAINER_INVALID'}, status_code=415)
        proc = await spawn_owned_process('ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
            '-show_entries', 'format=duration:stream=codec_type,codec_name', '-of', 'json', str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        communication = asyncio.create_task(proc.communicate())
        try:
            stdout, _ = await asyncio.wait_for(asyncio.shield(communication), timeout=10)
            probe = json.loads(stdout)
        finally:
            if proc.returncode is None:
                try: os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError: pass
            await asyncio.gather(communication, return_exceptions=True)
        duration = float(probe.get('format', {}).get('duration') or 0)
        if not (1 <= duration <= 1800) or not any(x.get('codec_type') == 'audio' for x in probe.get('streams', [])):
            return JSONResponse({'ok': False, 'code': 'IMPORT_AUDIO_INVALID'}, status_code=415)
        media = ResolvedMedia(video_id, '', {}, 'r2-legacy', 'r2-import', 'media', None, None, None,
                              None, duration, 'R2 import', time.time(), time.time()+900, 'r2-legacy-import')
        media._local_path = str(path)
        media._output_bitrate = MP3_BITRATE_KBPS
        local_request = Request({'type': 'http', 'method': 'GET', 'headers': [], '_veeb_import_media': media})
        # Upload/probe awaited. Re-check so a foreground job created during
        # that time is never relabelled as a source-free import.
        if _media_jobs.jobs.get(video_id) is not None:
            return JSONResponse({'ok': False, 'code': 'TRACK_BUSY'}, status_code=409)
        # No await between this check and transferring ownership.
        job = _media_jobs.get(video_id, local_request, background=True)
        owned_by_job = True
        job.task.add_done_callback(lambda _done, folder=directory: shutil.rmtree(folder, ignore_errors=True))
        await _media_jobs.wait(job, complete=True)
        return {'ok': True, 'version': 'v39.2-mp3-stream', 'mp3Complete': True,
                'bytes': job.size, 'jobId': job.job_id, 'source': 'legacy-r2',
                'youtubeContacted': False, 'originalDeleted': False}
    except Exception as exc:
        return JSONResponse({'ok': False, 'code': 'IMPORT_FAILED', 'message': redact(exc, 240)}, status_code=424)
    finally:
        if directory and not owned_by_job:
            shutil.rmtree(directory, ignore_errors=True)
        _import_sem.release()


async def probe_local_audio(path):
    process = await spawn_owned_process('ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
        '-show_entries', 'format=duration:stream=codec_type,codec_name', '-of', 'json', str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    communication = asyncio.create_task(process.communicate())
    try:
        stdout, _ = await asyncio.wait_for(asyncio.shield(communication), timeout=10)
        probe = json.loads(stdout)
        duration = float(probe.get('format', {}).get('duration') or 0)
        if (process.returncode or not math.isfinite(duration) or not 1 <= duration <= 1800
                or not any(s.get('codec_type') == 'audio' for s in probe.get('streams', []))):
            raise SourceAttemptError('Source duration must be between 1 and 1800 seconds with a readable audio stream.',
                                     stage='probe', code='SOURCE_DURATION_UNSUPPORTED')
        return {'duration': duration}
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await asyncio.gather(communication, return_exceptions=True)


def require_agent_auth(authorization):
    if not SOURCE_AGENT_SECRET:
        raise HTTPException(503, 'VEEB_SOURCE_AGENT_SECRET is not configured')
    if not secrets.compare_digest(authorization or '', f'Bearer {SOURCE_AGENT_SECRET}'):
        raise HTTPException(401, 'Unauthorized')
    if SOURCE_MODE != 'agent':
        raise HTTPException(409, 'VEEB_SOURCE_MODE must be agent on the resolver')


@app.post('/agent/claim')
async def agent_claim(authorization: str | None=Header(default=None)):
    require_agent_auth(authorization)
    claim = _source_broker.claim(MP3_BITRATE_KBPS)
    return JSONResponse({'ok': True, 'job': claim}, headers={'Cache-Control': 'no-store'})


@app.post('/agent/heartbeat')
async def agent_heartbeat(request: Request, authorization: str | None=Header(default=None)):
    require_agent_auth(authorization)
    _source_broker.last_seen = _source_broker.clock()
    video_id, token = request.headers.get('x-veeb-video-id', ''), request.headers.get('x-veeb-lease', '')
    ticket = _source_broker.owned(video_id, token)
    if ticket is not None:
        ticket.expires = _source_broker.clock() + _source_broker.lease_seconds
    return {'ok': True}


@app.post('/agent/result/{video_id}')
async def agent_result(video_id: str, request: Request, authorization: str | None=Header(default=None)):
    global _last_source_success
    require_agent_auth(authorization)
    video_id = validate_video_id(video_id)
    token = request.headers.get('x-veeb-lease', '')
    ticket = _source_broker.owned(video_id, token)
    if ticket is None or ticket.uploading:
        return JSONResponse({'ok': False, 'code': 'STALE_OR_BUSY_LEASE'}, status_code=409)
    try:
        size = int(request.headers.get('content-length', '0'))
        duration = float(request.headers.get('x-veeb-source-duration', '0'))
        digest = request.headers.get('x-veeb-sha256', '')
    except ValueError:
        size, duration, digest = 0, 0, ''
    if not 2048 <= size <= 44 * 1024 * 1024 or not 1 <= duration <= 1800 or not re.fullmatch('[a-f0-9]{64}', digest):
        return JSONResponse({'ok': False, 'code': 'AGENT_METADATA_INVALID'}, status_code=400)
    ticket.uploading = True
    directory = tempfile.mkdtemp(prefix='veeb-agent-upload-')
    path = Path(directory) / 'audio.mp3'
    transferred = False
    try:
        validator, checksum, received = MP3Validator(), hashlib.sha256(), 0
        async with asyncio.timeout(45):
            with path.open('wb') as output:
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > size:
                        raise JobError('AGENT_SIZE_INVALID', 'Agent upload exceeded its declared size.')
                    validator.feed(chunk)
                    checksum.update(chunk)
                    output.write(chunk)
        actual_duration = validator.finish()
        if received != size or not secrets.compare_digest(checksum.hexdigest(), digest):
            raise JobError('AGENT_CHECKSUM_INVALID', 'Agent upload was incomplete or failed SHA-256 verification.')
        if abs(actual_duration - duration) > max(2, duration * .01):
            raise JobError('SOURCE_TRUNCATED', 'Agent audio duration did not match its source.')
        expected_bytes = actual_duration * MP3_BITRATE_KBPS * 125
        if abs(received - expected_bytes) > max(1024, expected_bytes * .005):
            raise JobError('AGENT_BITRATE_INVALID', 'Agent MP3 does not match the requested bitrate.')
        if _source_broker.owned(video_id, token) is not ticket:
            return JSONResponse({'ok': False, 'code': 'STALE_OR_BUSY_LEASE'}, status_code=409)
        media = ResolvedMedia(video_id, '', {}, 'source-agent', 'agent-mp3', 'mp3', 'audio/mpeg',
                              'mp3', 'none', MP3_BITRATE_KBPS, duration, None, time.time(), time.time()+900, 'source-agent-mp3')
        media._local_path = str(path)
        ticket.future.set_result(media)
        transferred = True
        _last_source_success = {'videoId': video_id, 'checkedAtUnix': int(time.time()), 'route': 'source-agent',
                                'scope': 'Verified complete MP3 supplied by authenticated source agent, possibly from its local cache'}
        _source_broker.last_result = {'ok': True, 'videoId': video_id, 'checkedAtUnix': int(time.time())}
        return {'ok': True, 'accepted': True, 'bytes': received, 'sha256': digest}
    except Exception as exc:
        return JSONResponse({'ok': False, **error_detail(exc)}, status_code=422)
    finally:
        ticket.uploading = False
        if not transferred:
            shutil.rmtree(directory, ignore_errors=True)


@app.post('/agent/failure/{video_id}')
async def agent_failure(video_id: str, request: Request, authorization: str | None=Header(default=None)):
    require_agent_auth(authorization)
    video_id = validate_video_id(video_id)
    ticket = _source_broker.owned(video_id, request.headers.get('x-veeb-lease', ''))
    if ticket is None or ticket.uploading:
        return JSONResponse({'ok': False, 'code': 'STALE_OR_BUSY_LEASE'}, status_code=409)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 4096:
            raise HTTPException(413, 'Failure report too large')
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError):
        raise HTTPException(400, 'Expected JSON')
    # Upload/probe can yield control. Revalidate lease ownership after each await.
    if _source_broker.owned(video_id, request.headers.get('x-veeb-lease', '')) is not ticket:
        return JSONResponse({'ok': False, 'code': 'STALE_OR_BUSY_LEASE'}, status_code=409)
    code = payload.get('code', 'SOURCE_AGENT_FAILED') if isinstance(payload, dict) else 'SOURCE_AGENT_FAILED'
    if not isinstance(code, str) or not re.fullmatch('[A-Z_]{3,60}', code):
        code = 'SOURCE_AGENT_FAILED'
    message = redact(payload.get('message', 'Source agent could not acquire this track.'), 350) if isinstance(payload, dict) else 'Source agent failed.'
    _source_broker.fail(ticket, code, message)
    return {'ok': True, 'accepted': True}


@app.post('/prepare/{video_id}')
async def prepare_endpoint(video_id: str, authorization: str | None=Header(default=None)):
    require_auth(authorization)
    video_id = validate_video_id(video_id)
    try:
        job = _media_jobs.get(video_id, Request({'type': 'http', 'method': 'GET', 'headers': []}))
    except Exception as exc:
        return JSONResponse({'ok': False, **error_detail(exc)}, status_code=503)
    return JSONResponse({'ok': True, 'jobId': job.job_id, 'statusUrl': f'/jobs/{video_id}'}, status_code=202)


@app.get('/jobs/{video_id}')
async def job_status(video_id: str, authorization: str | None=Header(default=None)):
    require_auth(authorization)
    video_id = validate_video_id(video_id)
    _media_jobs._evict()
    job = _media_jobs.jobs.get(video_id)
    if job is None:
        return JSONResponse({'ok': False, 'code': 'JOB_MISS'}, status_code=404)
    data = {'ok': not bool(job.error), 'jobId': job.job_id,
            'state': 'failed' if job.error else ('complete' if job.done.is_set() else 'running'),
            'bytes': job.size, 'attempts': job.metadata.get('attempts', [])[-4:]}
    if job.error:
        data.update(error_detail(job.error))
    return JSONResponse(data, headers={'Cache-Control': 'no-store'})
