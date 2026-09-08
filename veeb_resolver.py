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
from media_jobs import MediaJobs, JobError
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
app = FastAPI(title='Veeb YouTube Resolver V37.9 MP3 Stream', docs_url=None, redoc_url=None)
RESOLVER_SECRET = os.environ.get('RESOLVER_SECRET', '')
VIDEO_ID_RE = re.compile('^[A-Za-z0-9_-]{11}$')
YOUTUBE_COOKIE_FILE = os.environ.get('YOUTUBE_COOKIE_FILE', '/etc/secrets/youtube-cookies.txt')
WRITABLE_COOKIE_FILE = os.environ.get('WRITABLE_COOKIE_FILE', '/tmp/veeb-youtube-cookies.txt')
YTDLP_CACHE_DIR = os.environ.get('YTDLP_CACHE_DIR', '/tmp/veeb-yt-dlp-cache')
JSC_RUNTIME = os.environ.get('YOUTUBE_JSC_RUNTIME', 'deno').strip() or 'deno'
YTDLP_SOURCE_SELECTOR = os.environ.get('YOUTUBE_SOURCE_SELECTOR', '').strip() or 'bestaudio/best[acodec!=none]/best'
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
MP3_STARTUP_MIN_BYTES = max(4096, int(os.environ.get('VEEB_MP3_STARTUP_MIN_BYTES', '16384')))
MP3_MAX_CONCURRENT_TRANSCODES = max(1, int(os.environ.get('VEEB_MP3_MAX_CONCURRENT', '2')))

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
    if authorization != f'Bearer {RESOLVER_SECRET}':
        raise HTTPException(status_code=401, detail='Unauthorized')

def validate_video_id(video_id: str) -> str:
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise HTTPException(status_code=400, detail='Invalid YouTube video ID')
    return video_id

def get_writable_cookie_file() -> str | None:
    if not os.path.isfile(YOUTUBE_COOKIE_FILE):
        return None
    if not os.path.isfile(WRITABLE_COOKIE_FILE):
        shutil.copyfile(YOUTUBE_COOKIE_FILE, WRITABLE_COOKIE_FILE)
        os.chmod(WRITABLE_COOKIE_FILE, 384)
        print('cookie runtime copy ready', json.dumps({'source': YOUTUBE_COOKIE_FILE, 'runtime': WRITABLE_COOKIE_FILE}), flush=True)
    return WRITABLE_COOKIE_FILE

def load_youtube_cookie_session(force: bool=False) -> bool:
    """Inspect the configured Netscape cookies for health reporting.

    We never log cookie values. The same cookie file remains available to yt-dlp.
    """
    global _youtube_cookie_header, _youtube_cookie_values, _youtube_cookie_authenticated
    if _youtube_cookie_header and (not force):
        return _youtube_cookie_authenticated
    cookie_file = get_writable_cookie_file()
    if not cookie_file:
        _youtube_cookie_header = ''
        _youtube_cookie_values = {}
        _youtube_cookie_authenticated = False
        return False
    jar = http.cookiejar.MozillaCookieJar(cookie_file)
    try:
        jar.load(ignore_discard=True, ignore_expires=False)
    except Exception as exc:
        print('cookie session load failed', json.dumps({'error': str(exc)[:500]}), flush=True)
        return False
    now = time.time()
    pairs: list[str] = []
    values: dict[str, str] = {}
    for cookie in jar:
        domain = (cookie.domain or '').lower().lstrip('.')
        if not (domain == 'youtube.com' or domain.endswith('.youtube.com')):
            continue
        if cookie.expires is not None and cookie.expires <= now:
            continue
        pairs.append(f'{cookie.name}={cookie.value}')
        values[cookie.name] = cookie.value
    _youtube_cookie_header = '; '.join(pairs)
    _youtube_cookie_values = values
    sid_present = bool(values.get('SAPISID') or values.get('__Secure-1PAPISID') or values.get('__Secure-3PAPISID'))
    _youtube_cookie_authenticated = bool(values.get('LOGIN_INFO') and sid_present)
    print('cookie session fields inspected', json.dumps({'cookieCount': len(values), 'hasLoginInfo': bool(values.get('LOGIN_INFO')), 'hasSidAuth': sid_present, 'authenticated': _youtube_cookie_authenticated}), flush=True)
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
    args: dict[str, list[str]] = {}
    if client not in {'', 'default', 'anonymous'}:
        args['player_client'] = [client]
    if client in {'visionos', 'android'}:
        args['player_skip'] = ['webpage']
    if client == 'mweb':
        args['fetch_pot'] = ['always']
        args['pot_trace'] = ['true']
        args['use_ad_playback_context'] = ['true']
    return args

def ytdlp_options(client_name: str, logger: YtdlpPhaseLogger, use_cookies: bool=False) -> dict[str, Any]:
    cookie_file = get_writable_cookie_file() if use_cookies else None
    # V37.9: Android currently still exposes the old combined itag 18 on many
    # public videos even when the newer web-family clients expose no selectable
    # progressive format. Prefer it for this one client only. FFmpeg validates
    # the returned source by actually decoding MP3 startup bytes, so yt-dlp's
    # extra check_formats probe is redundant and can incorrectly discard a URL
    # before the real decoder gets a chance to use it.
    source_selector = '18/best[acodec!=none]/bestaudio/best' if client_name == 'android' else YTDLP_SOURCE_SELECTOR
    opts: dict[str, Any] = {'format': source_selector, 'skip_download': True, 'noplaylist': True, 'quiet': True, 'no_warnings': True, 'verbose': client_name == 'mweb', 'cachedir': YTDLP_CACHE_DIR, 'socket_timeout': YTDLP_SOCKET_TIMEOUT_SECONDS, 'retries': 0, 'extractor_retries': YTDLP_EXTRACTOR_RETRIES, 'check_formats': False, 'js_runtimes': {JSC_RUNTIME: {}}, 'extractor_args': {'youtube': youtube_extractor_args_dict(client_name), 'youtubepot-bgutilhttp': {'base_url': [BGUTIL_BASE_URL]}}, 'logger': logger}
    if cookie_file:
        opts['cookiefile'] = cookie_file
    return opts
_extraction_sem = asyncio.Semaphore(1)
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
    """Each extraction/download owns a killable yt-dlp child process."""

    def __init__(self, name: str, size: int, client_name: str, resolver_path: str):
        self.name, self.client_name, self.resolver_path = (name, client_name, resolver_path)

    async def _child(self, video_id: str, *, download: bool, timeout: float) -> dict[str, Any]:
        async with _extraction_sem:
            options = ytdlp_options(self.client_name, YtdlpPhaseLogger(self.name), use_cookies=self.name == 'fg-auth')
            options.pop('logger', None)
            process = await spawn_owned_process(
                sys.executable, os.path.join(os.path.dirname(__file__), 'extract_source.py'),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            communication = asyncio.create_task(process.communicate(json.dumps({
                'videoId': video_id, 'options': options, 'download': download,
            }).encode()))
            try:
                stdout, _stderr = await asyncio.wait_for(asyncio.shield(communication), timeout=timeout)
                result = json.loads(stdout)
                child_diag = result.get('diagnostics') or []
                if not result.get('ok'):
                    suffix = (' | diagnostics=' + json.dumps(child_diag[-10:], separators=(',', ':'))) if child_diag else ''
                    raise RuntimeError((result.get('error') or 'extractor failed') + suffix)
                return result
            except asyncio.TimeoutError as exc:
                label = 'source download' if download else 'extraction'
                raise RuntimeError(label + ' timed out: ' + self.client_name) from exc
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await asyncio.gather(communication, return_exceptions=True)

    def _media(self, video_id: str, info: dict[str, Any], *, resolver_path: str) -> ResolvedMedia:
        media = ResolvedMedia(
            video_id=video_id, url=info.get('url') or '', http_headers=info.get('http_headers') or {},
            client=self.client_name, format_id=info.get('format_id'), ext=info.get('ext'),
            content_type=info.get('container'), acodec=info.get('acodec'), vcodec=info.get('vcodec'),
            abr=info.get('abr'), duration=info.get('duration'), title=info.get('title'),
            resolved_at=time.time(), expires_at=resolved_expiry(info.get('url') or ''), resolver_path=resolver_path,
        )
        if info.get('local_path'):
            media._local_path = str(info['local_path'])
        return media

    async def resolve(self, video_id: str, purpose: str) -> ResolvedMedia:
        started = time.monotonic()
        result = await self._child(video_id, download=False, timeout=18.0)
        media = self._media(video_id, result['media'], resolver_path=self.resolver_path)
        print('v37.9 source selected', json.dumps({'videoId': video_id, 'client': self.client_name, 'formatId': media.format_id, 'seconds': round(time.monotonic() - started, 3)}), flush=True)
        return media

    async def download_source(self, video_id: str, purpose: str) -> ResolvedMedia:
        started = time.monotonic()
        result = await self._child(video_id, download=True, timeout=45.0)
        media = self._media(video_id, result['media'], resolver_path=self.resolver_path + '-yt-dlp-download')
        if not getattr(media, '_local_path', None):
            raise RuntimeError('yt-dlp source download returned no local file')
        print('v37.9 source downloaded by yt-dlp', json.dumps({'videoId': video_id, 'client': self.client_name, 'formatId': media.format_id, 'bytes': result.get('downloadBytes'), 'seconds': round(time.monotonic() - started, 3)}), flush=True)
        return media

    def close(self) -> None:
        pass

def init_ytdlp_pools() -> None:
    global _fg_auth_pool, _fg_pot_pool, _fg_anon_pool, _fg_android_pool, _fg_safari_pool, _fg_embedded_pool, _fg_visionos_pool
    if _fg_anon_pool is None:
        _fg_visionos_pool = YtdlpEnginePool('fg-visionos', 1, 'visionos', 'yt-dlp-visionos-v37.9')
        _fg_android_pool = YtdlpEnginePool('fg-android', 1, 'android', 'yt-dlp-android-v37.9')
        _fg_pot_pool = YtdlpEnginePool('fg-pot', 1, 'mweb', 'yt-dlp-mweb-pot-v37.9')
        _fg_safari_pool = YtdlpEnginePool('fg-safari', 1, 'web_safari', 'yt-dlp-web-safari-v37.9')
        _fg_embedded_pool = YtdlpEnginePool('fg-embedded', 1, 'web_embedded', 'yt-dlp-web-embedded-v37.9')
        _fg_anon_pool = YtdlpEnginePool('fg-anon', 1, 'anonymous', 'yt-dlp-anonymous-v37.9')
        _fg_auth_pool = YtdlpEnginePool('fg-auth', 1, YTDLP_AUTH_CLIENT, 'yt-dlp-auth-v37.9')

async def resolve_ytdlp_foreground_v35(video_id: str, purpose: str) -> ResolvedMedia:
    init_ytdlp_pools()
    errors = []
    pools = [_fg_visionos_pool, _fg_android_pool, _fg_pot_pool, _fg_embedded_pool, _fg_anon_pool]
    if get_writable_cookie_file():
        pools.append(_fg_auth_pool)
    for pool in pools:
        try:
            return await pool.resolve(video_id, purpose)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = str(exc)
            errors.append(pool.name + ': ' + message)
            print('v37.9 acquisition attempt failed', json.dumps({'videoId': video_id, 'path': pool.name, 'error': redact_source_error(message)}), flush=True)
    raise RuntimeError('acquisition failed: ' + ' | '.join(errors))

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
    return {'Content-Type': 'audio/mpeg', 'Cache-Control': 'private, no-store', 'Accept-Ranges': 'none', 'X-Content-Type-Options': 'nosniff', 'X-Veeb-Resolver': 'v37.9-mp3-stream', 'X-Veeb-Resolved-Cache': cache_state, 'X-Veeb-Playback-Client': media.client, 'X-Veeb-Source-Format': media.format_id or YTDLP_SOURCE_SELECTOR, 'X-Veeb-Resolver-Path': media.resolver_path, 'X-Veeb-Direct-Proxy': '0', 'X-Veeb-Transcode': 'ffmpeg-direct-http-mp3-v3', 'X-Veeb-MP3-Bitrate': str(MP3_BITRATE_KBPS), 'X-Veeb-Ignored-Range': '1' if request.headers.get('range') else '0'}

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
    """Start FFmpeg against the signed URL and prove MP3 bytes before HTTP 200."""
    purpose = (request.headers.get('x-veeb-purpose') or 'playback').strip().lower()
    cache_fill = purpose == 'cache-fill'
    if cache_fill:
        await _mp3_cache_fill_sem.acquire()
    try:
        await _mp3_transcode_sem.acquire()
    except BaseException:
        if cache_fill:
            _mp3_cache_fill_sem.release()
        raise
    process: asyncio.subprocess.Process | None = None
    stderr_task: asyncio.Task[bytes] | None = None
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
        local_path = getattr(media, '_local_path', None)
        if local_path:
            try:
                source_file = Path(local_path)
                source_file.unlink(missing_ok=True)
                try:
                    source_file.parent.rmdir()
                except OSError:
                    pass
            except Exception:
                pass
        await release_slots()
        return stderr
    try:
        source_path = getattr(media, '_local_path', None)
        input_args = [] if source_path else ffmpeg_http_input_args(media)
        input_value = source_path or media.url
        process = await spawn_owned_process('ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', *input_args, '-i', input_value, '-map', '0:a:0', '-vn', '-map_metadata', '-1', '-threads', '1', '-c:a', 'libmp3lame', '-b:a', f'{MP3_BITRATE_KBPS}k', '-ac', '2', '-ar', '44100', '-id3v2_version', '0', '-write_xing', '0', '-f', 'mp3', '-flush_packets', '1', 'pipe:1', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
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
            raise RuntimeError(f'FFmpeg produced only {len(first_chunk)} startup MP3 bytes' + (': ' + stderr.decode('utf-8', 'replace')[-1200:] if stderr else ''))
        print('v37.9 mp3 first bytes ready', json.dumps({'videoId': video_id, 'purpose': purpose, 'sourceFormat': media.format_id, 'resolverPath': media.resolver_path, 'firstChunkBytes': len(first_chunk), 'bitrateKbps': MP3_BITRATE_KBPS}), flush=True)

        async def body() -> AsyncIterator[bytes]:
            try:
                yield first_chunk
                assert process is not None and process.stdout is not None
                while True:
                    chunk = await asyncio.wait_for(process.stdout.read(MP3_CHUNK_BYTES), timeout=20.0)
                    if not chunk:
                        break
                    yield chunk
                rc = await process.wait()
                stderr = b''
                if stderr_task is not None:
                    try:
                        stderr = await stderr_task
                    except Exception:
                        pass
                if rc != 0:
                    print('v37.9 ffmpeg ended non-zero', json.dumps({'videoId': video_id, 'returnCode': rc, 'error': redact_source_error(stderr.decode('utf-8', 'replace'))}), flush=True)
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
    text = str(message)
    text = re.sub(r'(?i)https?://[^\s]+googlevideo\.com/[^\s]+', '[googlevideo-url]', text)
    text = re.sub(r'(?i)https?://[^\s]+', '[url]', text)
    text = re.sub(r'(?i)(?:[?&]|\b)(pot|sig|lsig|spc|n)=([^\s&]+)', r'\1=<redacted>', text)
    text = re.sub(r'(?i)[^\s]{0,120}videoplayback[^\s]*', '[googlevideo-url]', text)
    return text[-1800:]

def error_detail(error: BaseException) -> dict[str, str]:
    message = str(error).lower()
    if 'bot' in message or 'login_required' in message:
        code, text = ('SOURCE_ACCESS_DENIED', 'YouTube rejected source acquisition. MP3 conversion has not solved this access failure.')
    elif '403' in message:
        code, text = ('SOURCE_HTTP_403', 'The media source rejected the resolver request.')
    elif 'timed out' in message or isinstance(error, asyncio.TimeoutError):
        code, text = ('SOURCE_TIMEOUT', 'Source acquisition or conversion exceeded its time budget.')
    elif 'format' in message:
        code, text = ('SOURCE_FORMAT_UNAVAILABLE', 'No usable audio source was returned.')
    elif isinstance(error, JobError):
        code, text = (error.code, str(error))
    else:
        code, text = ('SOURCE_ACQUISITION_FAILED', 'Could not acquire and convert a complete playable source.')
    return {'code': code, 'message': text}

async def produce_mp3(video_id: str, request: Request, job):
    init_ytdlp_pools()
    errors = []
    attempts = []
    job.metadata['attempts'] = attempts
    cached = get_cached_media(video_id)
    pools = [_fg_visionos_pool, _fg_android_pool, _fg_pot_pool, _fg_embedded_pool, _fg_anon_pool]
    if get_writable_cookie_file():
        pools.append(_fg_auth_pool)
    async with asyncio.timeout(65):
        for pool in ([None] if cached else []) + pools:
            try:
                media = cached if pool is None else await pool.resolve(video_id, 'live')
                media._output_bitrate = MP3_BITRATE_KBPS
                try:
                    iterator = await prepare_live_mp3_stream(media, video_id, request)
                    if pool is not None:
                        _resolved_cache[video_id] = media
                        cleanup_resolved_cache()
                    job.metadata = {'media': media, 'cache': 'HIT' if pool is None else 'MISS'}
                    return iterator
                except Exception as direct_exc:
                    if pool is None:
                        raise
                    attempts.append({'path': pool.name + '-direct', 'error': redact_source_error(str(direct_exc))[-1200:]})
                    print('v37.9 direct source fetch failed; trying yt-dlp owned download', json.dumps({'videoId': video_id, 'path': pool.name, 'error': redact_source_error(str(direct_exc))}), flush=True)
                    downloaded = await pool.download_source(video_id, 'live-download-fallback')
                    downloaded._output_bitrate = MP3_BITRATE_KBPS
                    iterator = await prepare_live_mp3_stream(downloaded, video_id, request)
                    job.metadata = {'media': downloaded, 'cache': 'MISS-DOWNLOADED'}
                    return iterator
            except asyncio.CancelledError:
                invalidate_media(video_id)
                raise
            except Exception as exc:
                invalidate_media(video_id)
                errors.append(str(exc))
                attempts.append({'path': pool.name if pool else 'cached', 'error': redact_source_error(str(exc))[-1200:]})
                print('v37.9 source-to-mp3 attempt failed', json.dumps({'videoId': video_id, 'path': pool.name if pool else 'cached', 'error': redact_source_error(str(exc))}), flush=True)
    if errors and all(('sign in to confirm' in item.lower()) or ('login_required' in item.lower()) or ('not a bot' in item.lower()) for item in errors):
        raise JobError('SOURCE_ACCESS_DENIED', 'Every configured YouTube client was challenged before a playable source was returned.')
    raise RuntimeError('No source produced MP3: ' + ' | '.join(errors))
_media_jobs = MediaJobs(produce_mp3)

async def proxy_media(request: Request, video_id: str):
    cache_fill = request.headers.get('x-veeb-purpose') == 'cache-fill'
    if request.method == 'HEAD':
        return Response(status_code=200, headers={'Content-Type': 'audio/mpeg', 'Accept-Ranges': 'none', 'X-Veeb-Resolver': 'v37.9-mp3-stream'})
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
        status = 503 if isinstance(exc, JobError) and exc.code == 'RESOLVER_BUSY' else 502
        return JSONResponse({'ok': False, 'stage': 'source-or-transcode', **detail}, status_code=status, headers={'Cache-Control': 'no-store', 'Retry-After': '15', 'X-Veeb-Error-Code': detail['code']})

@app.on_event('startup')
async def startup_session() -> None:
    load_youtube_cookie_session(force=True)
    get_http_client()
    init_ytdlp_pools()
    print('v37.9 ready: visionos/android webpage-skip, mweb POT, yt-dlp owned-download fallback, optional cookies; shared MP3 jobs', flush=True)

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
    return {'ok': True, 'service': 'veeb-resolver', 'version': 'v37.9-mp3-stream'}

@app.get('/health')
async def health(authorization: str | None=Header(default=None)) -> dict[str, Any]:
    require_auth(authorization)
    cleanup_resolved_cache()
    try:
        ytdlp_version = importlib.metadata.version('yt-dlp')
    except importlib.metadata.PackageNotFoundError:
        ytdlp_version = 'unknown'
    return {'ok': True, 'service': 'veeb-resolver', 'version': 'v37.9-mp3-stream', 'ytDlpVersion': ytdlp_version, 'sourceSelector': YTDLP_SOURCE_SELECTOR, 'androidSourceSelector': '18/best[acodec!=none]/bestaudio/best', 'ytDlpCheckFormats': False, 'playerSkipWebpageClients': ['visionos', 'android'], 'ytDlpOwnedDownloadFallback': True, 'cookieFilePresent': os.path.isfile(YOUTUBE_COOKIE_FILE), 'cookieSessionRecognized': _youtube_cookie_authenticated, 'cookieAuthenticationVerified': None, 'potHttpReady': pot_http_server_ready(), 'ffmpegInstalled': bool(shutil.which('ffmpeg')), 'jsRuntimeInstalled': bool(shutil.which(JSC_RUNTIME)), 'sourceAccessVerified': False, 'note': 'Liveness only. POST /diagnose/<videoId> proves source acquisition and MP3 production. null cookieAuthenticationVerified means not tested.', 'deliveryFormat': 'audio/mpeg', 'speculativeExtraction': False, 'extractionConcurrency': 1, 'maxConcurrentTranscodes': MP3_MAX_CONCURRENT_TRANSCODES, 'jobs': _media_jobs.stats()}

@app.post('/diagnose/{video_id}')
async def diagnose(video_id: str, authorization: str | None=Header(default=None)):
    require_auth(authorization)
    video_id = validate_video_id(video_id)
    started = time.monotonic()
    request = Request({'type': 'http', 'method': 'GET', 'headers': []})
    try:
        job = _media_jobs.get(video_id, request)
        await _media_jobs.wait(job, complete=True)
        media = job.metadata['media']
        return {'ok': True, 'version': 'v37.9-mp3-stream', 'videoId': video_id, 'mp3Complete': True, 'bytes': job.size, 'jobId': job.job_id, 'sourceClient': media.client, 'sourceFormat': media.format_id, 'elapsedSeconds': round(time.monotonic() - started, 2)}
    except Exception as exc:
        payload = {'ok': False, 'version': 'v37.9-mp3-stream', 'videoId': video_id, **error_detail(exc), 'elapsedSeconds': round(time.monotonic() - started, 2)}
        try:
            if job.metadata.get('attempts'):
                payload['attempts'] = job.metadata['attempts'][-8:]
        except Exception:
            pass
        # 424 keeps the diagnostic body intact through intermediaries while
        # correctly describing a failed upstream dependency. Playback /stream
        # still returns 502/503 as appropriate.
        return JSONResponse(payload, status_code=424)

@app.get('/resolve/{video_id}')
async def resolve_endpoint(video_id: str, authorization: str | None=Header(default=None)) -> JSONResponse:
    require_auth(authorization)
    video_id = validate_video_id(video_id)
    try:
        media, cache_state = await get_or_resolve(video_id, 'live')
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
