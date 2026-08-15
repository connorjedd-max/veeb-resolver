import asyncio
import importlib.metadata
import hashlib
import http.cookiejar
import json
import os
import re
import shutil
import socket
import threading
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, AsyncIterator
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx
import yt_dlp
from yt_dlp.extractor.youtube.jsc.provider import (
    JsChallengeRequest, JsChallengeType, NChallengeInput, SigChallengeInput,
)
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

app = FastAPI(title="Veeb YouTube Resolver V36.15 YouTube.js", docs_url=None, redoc_url=None)

RESOLVER_SECRET = os.environ.get("RESOLVER_SECRET", "")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_COOKIE_FILE = os.environ.get("YOUTUBE_COOKIE_FILE", "/etc/secrets/youtube-cookies.txt")
WRITABLE_COOKIE_FILE = os.environ.get("WRITABLE_COOKIE_FILE", "/tmp/veeb-youtube-cookies.txt")
YTDLP_CACHE_DIR = os.environ.get("YTDLP_CACHE_DIR", "/tmp/veeb-yt-dlp-cache")
JSC_RUNTIME = os.environ.get("YOUTUBE_JSC_RUNTIME", "deno").strip() or "deno"
JSC_TRACE = os.environ.get("VEEB_JSC_TRACE", "true").strip().lower() in {"1", "true", "yes", "on"}
JSC_PLAYER_VARIANT = os.environ.get("VEEB_JSC_PLAYER_VARIANT", "actual").strip().lower() or "actual"
JSC_REMOTE_COMPONENTS = [
    item.strip()
    for item in os.environ.get("VEEB_JSC_REMOTE_COMPONENTS", "ejs:npm").split(",")
    if item.strip()
]
SOURCE_FORMAT = os.environ.get("YOUTUBE_STREAM_FORMAT", "18").strip() or "18"
YOUTUBE_PREMIUM_ACCOUNT = os.environ.get("YOUTUBE_PREMIUM_ACCOUNT", "false").strip().lower() in {
    "1", "true", "yes", "on"
}

# Public Innertube key used by YouTube's own web clients. It can be overridden
# without changing code if YouTube rotates client configuration.
INNERTUBE_API_KEY = os.environ.get(
    "YOUTUBE_INNERTUBE_API_KEY",
    "AIzaSyC9XL3ZjWddXya6X74dJoCTL-WEYFDNX30",
).strip()
INNERTUBE_PLAYER_URL = "https://www.youtube.com/youtubei/v1/player"
DIRECT_FAST_TIMEOUT_SECONDS = max(1.0, float(os.environ.get("VEEB_DIRECT_FAST_TIMEOUT", "3.5")))
DIRECT_CLIENT_COOLDOWN_SECONDS = max(60, int(os.environ.get("VEEB_DIRECT_CLIENT_COOLDOWN", "1800")))
DIRECT_PREFETCH_CONCURRENCY = max(1, int(os.environ.get("VEEB_DIRECT_PREFETCH_CONCURRENCY", "3")))
BGUTIL_BASE_URL = os.environ.get("VEEB_BGUTIL_BASE_URL", "http://127.0.0.1:4416").rstrip("/")
YOUTUBEJS_BASE_URL = os.environ.get("VEEB_YOUTUBEJS_BASE_URL", "http://127.0.0.1:4417").rstrip("/")
YOUTUBEJS_DECIPHER_TIMEOUT_SECONDS = max(1.0, float(os.environ.get("VEEB_YOUTUBEJS_DECIPHER_TIMEOUT", "5.0")))
BGUTIL_WARM_VIDEO_ID = os.environ.get("VEEB_BGUTIL_WARM_VIDEO_ID", "dQw4w9WgXcQ").strip()
DIRECT_MWEB_POT_TIMEOUT_SECONDS = max(3.0, float(os.environ.get("VEEB_DIRECT_MWEB_POT_TIMEOUT", "15.0")))
DIRECT_MWEB_PLAYER_TIMEOUT_SECONDS = max(1.0, float(os.environ.get("VEEB_DIRECT_MWEB_PLAYER_TIMEOUT", "4.0")))
YTDLP_FG_AUTH_ENGINES = max(1, int(os.environ.get("VEEB_YTDLP_FG_AUTH_ENGINES", "1")))
YTDLP_FG_POT_ENGINES = max(1, int(os.environ.get("VEEB_YTDLP_FG_POT_ENGINES", "1")))
YTDLP_PREFETCH_ENGINES = max(1, int(os.environ.get("VEEB_YTDLP_PREFETCH_ENGINES", "1")))
YTDLP_SOCKET_TIMEOUT_SECONDS = max(5, int(os.environ.get("VEEB_YTDLP_SOCKET_TIMEOUT", "15")))
YTDLP_EXTRACTOR_RETRIES = max(0, int(os.environ.get("VEEB_YTDLP_EXTRACTOR_RETRIES", "0")))
YTDLP_SKIP_WEBPAGE_WITH_VISITOR = os.environ.get("VEEB_YTDLP_SKIP_WEBPAGE_WITH_VISITOR", "false").strip().lower() in {"1", "true", "yes", "on"}
HEAVY_PREFETCH = os.environ.get("VEEB_HEAVY_PREFETCH", "false").strip().lower() in {"1", "true", "yes", "on"}

# These contexts are copied from current yt-dlp client definitions, but the hot
# path asks Innertube for the player response and can decipher a selected ciphered
# format in-process using yt-dlp's JS challenge director. The mature full extractor
# remains only as fallback.
DIRECT_CLIENTS: dict[str, dict[str, Any]] = {
    # Current yt-dlp defaults to tv_downgraded + web_safari when logged-in
    # cookies are supplied. These clients support cookie authentication.
    "tv_downgraded": {
        "clientName": "TVHTML5",
        "clientVersion": "5.20260707",
        "userAgent": "Mozilla/5.0 (ChromiumStylePlatform) Cobalt/Version",
        "hl": "en",
        "supportsCookies": True,
    },
    "web_embedded": {
        "clientName": "WEB_EMBEDDED_PLAYER",
        "clientVersion": "2.20260708.00.00",
        "hl": "en",
        "supportsCookies": True,
    },
    "web_safari": {
        "clientName": "WEB",
        "clientVersion": "2.20260708.00.00",
        "userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.5 Safari/605.1.15,gzip(gfe)",
        "hl": "en",
        "supportsCookies": True,
    },
}
MWEB_DIRECT_CONFIG: dict[str, Any] = {
    "clientName": "MWEB",
    "clientVersion": "2.20260708.05.00",
    "userAgent": "Mozilla/5.0 (iPad; CPU OS 16_7_10 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1,gzip(gfe)",
    "hl": "en",
}

DIRECT_CLIENT_ORDER = [
    name.strip()
    for name in os.environ.get(
        "VEEB_DIRECT_CLIENTS",
        "tv_downgraded,web_embedded,web_safari",
    ).split(",")
    if name.strip() in DIRECT_CLIENTS
]
if not DIRECT_CLIENT_ORDER:
    DIRECT_CLIENT_ORDER = ["tv_downgraded", "web_embedded", "web_safari"]

# Two yt-dlp fallbacks. The authenticated/default pass is deliberately first;
# with cookies current yt-dlp selects tv_downgraded + web_safari. Only if that
# fails do we pay the expensive mweb + BotGuard/PO-token path.
YTDLP_AUTH_CLIENT = os.environ.get("YOUTUBE_AUTH_FALLBACK_CLIENT", "default").strip() or "default"
YTDLP_POT_CLIENT = os.environ.get("YOUTUBE_FALLBACK_CLIENT", "mweb").strip() or "mweb"

PLAYBACK_WAIT_SECONDS = max(0.0, float(os.environ.get("YOUTUBE_PLAYBACK_WAIT", "0")))
RESOLVED_URL_FALLBACK_TTL_SECONDS = max(60, int(os.environ.get("VEEB_RESOLVED_URL_TTL", "1800")))
RESOLVED_URL_EXPIRY_MARGIN_SECONDS = max(30, int(os.environ.get("VEEB_RESOLVED_URL_EXPIRY_MARGIN", "120")))
RESOLVED_URL_MAX_ENTRIES = max(16, int(os.environ.get("VEEB_RESOLVED_URL_MAX_ENTRIES", "512")))
RESOLVE_TIMEOUT_SECONDS = max(15.0, float(os.environ.get("VEEB_RESOLVE_TIMEOUT", "45")))
UPSTREAM_CONNECT_TIMEOUT_SECONDS = max(2.0, float(os.environ.get("VEEB_UPSTREAM_CONNECT_TIMEOUT", "8")))
UPSTREAM_READ_TIMEOUT_SECONDS = max(10.0, float(os.environ.get("VEEB_UPSTREAM_READ_TIMEOUT", "45")))
PROXY_CHUNK_BYTES = max(64 * 1024, int(os.environ.get("VEEB_PROXY_CHUNK_BYTES", str(256 * 1024))))

os.makedirs(YTDLP_CACHE_DIR, exist_ok=True)


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
_prefetch_pool = None
_direct_prefetch_sem = asyncio.Semaphore(DIRECT_PREFETCH_CONCURRENCY)
_resolve_task_purpose: dict[str, str] = {}
_direct_client_cooldown_until: dict[str, float] = {}
_visitor_data: str | None = None
_youtube_cookie_header: str = ""
_youtube_cookie_values: dict[str, str] = {}
_youtube_cookie_authenticated = False
_data_sync_id: str | None = None
_session_gvs_pot: str | None = None
_session_gvs_pot_expires_at: float = 0.0
_session_gvs_task: asyncio.Task[tuple[str, str]] | None = None
_session_gvs_lock = asyncio.Lock()

_player_sts_cache: dict[str, int] = {}
_global_player_bootstrap: dict[str, Any] | None = None
_global_player_bootstrap_expires_at = 0.0
_global_player_bootstrap_lock = asyncio.Lock()
GLOBAL_PLAYER_BOOTSTRAP_TTL_SECONDS = max(60, int(os.environ.get("VEEB_GLOBAL_PLAYER_TTL", "900")))
_mweb_bootstrap_lock = asyncio.Lock()
_active_intent_video_id: str | None = None
_youtubejs_ready = False
_youtubejs_player_id: str | None = None


def require_auth(authorization: str | None) -> None:
    if not RESOLVER_SECRET:
        raise HTTPException(status_code=503, detail="RESOLVER_SECRET is not configured")
    if authorization != f"Bearer {RESOLVER_SECRET}":
        raise HTTPException(status_code=401, detail="Unauthorized")


def validate_video_id(video_id: str) -> str:
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise HTTPException(status_code=400, detail="Invalid YouTube video ID")
    return video_id


def get_writable_cookie_file() -> str | None:
    if not os.path.isfile(YOUTUBE_COOKIE_FILE):
        return None
    if not os.path.isfile(WRITABLE_COOKIE_FILE):
        shutil.copyfile(YOUTUBE_COOKIE_FILE, WRITABLE_COOKIE_FILE)
        os.chmod(WRITABLE_COOKIE_FILE, 0o600)
        print("cookie runtime copy ready", json.dumps({"source": YOUTUBE_COOKIE_FILE, "runtime": WRITABLE_COOKIE_FILE}), flush=True)
    return WRITABLE_COOKIE_FILE


def load_youtube_cookie_session(force: bool = False) -> bool:
    """Load the existing Netscape cookie file once for the direct fast path.

    We never log cookie values. The same cookie file remains available to yt-dlp.
    """
    global _youtube_cookie_header, _youtube_cookie_values, _youtube_cookie_authenticated
    if _youtube_cookie_header and not force:
        return _youtube_cookie_authenticated
    cookie_file = get_writable_cookie_file()
    if not cookie_file:
        _youtube_cookie_header = ""
        _youtube_cookie_values = {}
        _youtube_cookie_authenticated = False
        return False
    jar = http.cookiejar.MozillaCookieJar(cookie_file)
    try:
        jar.load(ignore_discard=True, ignore_expires=False)
    except Exception as exc:
        print("cookie session load failed", json.dumps({"error": str(exc)[:500]}), flush=True)
        return False
    now = time.time()
    pairs: list[str] = []
    values: dict[str, str] = {}
    for cookie in jar:
        domain = (cookie.domain or "").lower().lstrip(".")
        # Match yt-dlp's auth behavior: only cookies that belong to YouTube
        # are attached to youtube.com Innertube requests.
        if not (domain == "youtube.com" or domain.endswith(".youtube.com")):
            continue
        if cookie.expires is not None and cookie.expires <= now:
            continue
        pairs.append(f"{cookie.name}={cookie.value}")
        values[cookie.name] = cookie.value
    _youtube_cookie_header = "; ".join(pairs)
    _youtube_cookie_values = values
    sid_present = bool(values.get("SAPISID") or values.get("__Secure-1PAPISID") or values.get("__Secure-3PAPISID"))
    _youtube_cookie_authenticated = bool(values.get("LOGIN_INFO") and sid_present)
    print("authenticated cookie session ready", json.dumps({
        "cookieCount": len(values),
        "hasLoginInfo": bool(values.get("LOGIN_INFO")),
        "hasSidAuth": sid_present,
        "authenticated": _youtube_cookie_authenticated,
    }), flush=True)
    return _youtube_cookie_authenticated


def parse_data_sync_session(data_sync_id: str | None) -> tuple[str | None, str | None]:
    """Mirror yt-dlp's Data Sync ID parsing for cookie-authenticated API calls."""
    if not data_sync_id:
        return None, None
    first, sep, second = str(data_sync_id).partition("||")
    if not sep:
        return None, first or None
    if second:
        return first or None, second or None
    return None, first or None


def make_sid_authorization(
    scheme: str, sid: str, origin: str, user_session_id: str | None = None,
) -> str:
    """Generate YouTube SID auth, including the optional Data Sync user-session binding."""
    timestamp = str(round(time.time()))
    hash_parts: list[str] = []
    if user_session_id:
        hash_parts.append(user_session_id)
    hash_parts.extend([timestamp, sid, origin])
    digest = hashlib.sha1(" ".join(hash_parts).encode()).hexdigest()
    suffix = "_u" if user_session_id else ""
    return f"{scheme} {timestamp}_{digest}{suffix}"


def youtube_cookie_auth_header(
    origin: str = "https://www.youtube.com", user_session_id: str | None = None,
) -> str | None:
    load_youtube_cookie_session()
    values = _youtube_cookie_values
    sapisid = values.get("SAPISID") or values.get("__Secure-3PAPISID")
    candidates = (
        ("SAPISIDHASH", sapisid),
        ("SAPISID1PHASH", values.get("__Secure-1PAPISID")),
        ("SAPISID3PHASH", values.get("__Secure-3PAPISID")),
    )
    parts = [
        make_sid_authorization(scheme, sid, origin, user_session_id=user_session_id)
        for scheme, sid in candidates if sid
    ]
    return " ".join(parts) or None


def pot_http_server_ready() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 4416), timeout=0.25):
            return True
    except OSError:
        return False


def parse_googlevideo_expiry(media_url: str) -> float | None:
    try:
        values = parse_qs(urlparse(media_url).query).get("expire")
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
    for media in oldest[: len(_resolved_cache) - RESOLVED_URL_MAX_ENTRIES]:
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
        _http_client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(
                connect=UPSTREAM_CONNECT_TIMEOUT_SECONDS,
                read=UPSTREAM_READ_TIMEOUT_SECONDS,
                write=UPSTREAM_READ_TIMEOUT_SECONDS,
                pool=UPSTREAM_CONNECT_TIMEOUT_SECONDS,
            ),
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=20),
            http2=True,
        )
    return _http_client


def client_on_cooldown(client: str) -> bool:
    return _direct_client_cooldown_until.get(client, 0) > time.time()


def cool_down_client(client: str, reason: str) -> None:
    _direct_client_cooldown_until[client] = time.time() + DIRECT_CLIENT_COOLDOWN_SECONDS
    print("direct client cooldown", json.dumps({"client": client, "seconds": DIRECT_CLIENT_COOLDOWN_SECONDS, "reason": reason[:500]}), flush=True)


def innertube_headers(client: str) -> dict[str, str]:
    cfg = DIRECT_CLIENTS[client]
    origin = "https://www.youtube.com"
    headers = {
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Content-Type": "application/json",
        "Origin": origin,
        "X-Youtube-Client-Name": str({"WEB_EMBEDDED_PLAYER": 56, "WEB": 1, "TVHTML5": 7}.get(cfg["clientName"], 1)),
        "X-Youtube-Client-Version": str(cfg["clientVersion"]),
    }
    if cfg.get("userAgent"):
        headers["User-Agent"] = str(cfg["userAgent"])
    if _visitor_data:
        headers["X-Goog-Visitor-Id"] = _visitor_data

    # Mirror the essential cookie-auth headers used by yt-dlp's YouTube
    # extractor. This turns the direct /player request into the same logged-in
    # session represented by youtube-cookies.txt, instead of an anonymous
    # datacenter request.
    if cfg.get("supportsCookies") and load_youtube_cookie_session():
        if _youtube_cookie_header:
            headers["Cookie"] = _youtube_cookie_header
        authorization = youtube_cookie_auth_header(origin)
        if authorization:
            headers["Authorization"] = authorization
            headers["X-Origin"] = origin
        headers["X-Goog-AuthUser"] = "0"
        headers["X-Youtube-Bootstrap-Logged-In"] = "true"
    return headers


def innertube_payload(video_id: str, client: str) -> dict[str, Any]:
    cfg = dict(DIRECT_CLIENTS[client])
    cfg.pop("supportsCookies", None)
    cfg.setdefault("timeZone", "UTC")
    cfg.setdefault("utcOffsetMinutes", 0)
    context: dict[str, Any] = {
        "client": cfg,
        "request": {"useSsl": True, "internalExperimentFlags": []},
        "user": {"lockedSafetyMode": False},
    }
    if client == "web_embedded":
        context["thirdParty"] = {"embedUrl": "https://www.google.com/"}
    return {
        "context": context,
        "videoId": video_id,
        "contentCheckOk": True,
        "racyCheckOk": True,
        "playbackContext": {
            "contentPlaybackContext": {
                "html5Preference": "HTML5_PREF_WANTS",
                "lactMilliseconds": "-1",
            }
        },
    }


def format_has_drm(fmt: dict[str, Any]) -> bool:
    return bool(fmt.get("drmFamilies") or fmt.get("licenseInfos") or fmt.get("drmTrackType"))


def select_direct_format(data: dict[str, Any]) -> dict[str, Any] | None:
    """Select the target format even when YouTube returns signatureCipher instead of url."""
    streaming = data.get("streamingData") or {}
    formats = list(streaming.get("formats") or []) + list(streaming.get("adaptiveFormats") or [])
    candidates = [
        fmt for fmt in formats
        if isinstance(fmt, dict)
        and not format_has_drm(fmt)
        and (
            (isinstance(fmt.get("url"), str) and fmt.get("url", "").startswith("http"))
            or isinstance(fmt.get("signatureCipher"), str)
            or isinstance(fmt.get("cipher"), str)
        )
    ]
    for fmt in candidates:
        if str(fmt.get("itag")) == SOURCE_FORMAT:
            return fmt
    combined_mp4 = [
        fmt for fmt in candidates
        if str(fmt.get("mimeType", "")).startswith("video/mp4")
        and "audioQuality" in fmt
    ]
    if combined_mp4:
        return sorted(combined_mp4, key=lambda item: int(item.get("bitrate") or 0))[0]
    audio_mp4 = [
        fmt for fmt in candidates
        if str(fmt.get("mimeType", "")).startswith("audio/mp4")
    ]
    if audio_mp4:
        return sorted(audio_mp4, key=lambda item: int(item.get("bitrate") or 0), reverse=True)[0]
    return None


def normalize_player_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    value = value.replace('\\/', '/')
    if value.startswith('//'):
        return 'https:' + value
    if value.startswith('/'):
        return 'https://www.youtube.com' + value
    if value.startswith('http://') or value.startswith('https://'):
        return value
    return None


def player_url_from_response(data: dict[str, Any]) -> str | None:
    assets = data.get('assets') or {}
    for value in (
        assets.get('js'),
        assets.get('jsUrl'),
        data.get('playerJsUrl'),
    ):
        if result := normalize_player_url(value):
            return result

    # Some Innertube responses bury the player URL outside the historical
    # assets/js fields. Walk the response once before falling back to global
    # player discovery. This keeps the media request itself Innertube-only.
    stack: list[Any] = [data]
    seen = 0
    while stack and seen < 5000:
        value = stack.pop()
        seen += 1
        if isinstance(value, dict):
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
        elif isinstance(value, str) and '/s/player/' in value and ('.js' in value or 'base.js' in value):
            match = re.search(r'((?:https?:)?//[^\"\s]+/s/player/[^\"\s]+\.js[^\"\s]*|/s/player/[^\"\s]+\.js[^\"\s]*)', value.replace('\\/', '/'))
            if match and (result := normalize_player_url(match.group(1))):
                return result
    return None


def jsc_player_url(actual_player_url: str) -> str:
    """Return the stable player variant yt-dlp uses for JS deciphering.

    The mweb webpage can prescribe a plasma/es6 player variant that the current
    EJS solver may not understand even though the same player version has a
    supported TV/main variant. yt-dlp has a player_js_variant mechanism for this
    exact reason. Keep the player version/hash, switch only the variant path.
    """
    match = re.search(r'/s/player/([A-Za-z0-9_-]{8,})/', actual_player_url or '')
    if not match or JSC_PLAYER_VARIANT == 'actual':
        return actual_player_url
    player_id = match.group(1)
    paths = {
        'tv': 'tv-player-ias.vflset/tv-player-ias.js',
        'tv_es6': 'tv-player-es6.vflset/tv-player-es6.js',
        'main': 'player_ias.vflset/en_US/base.js',
        'es6': 'player_es6.vflset/en_US/base.js',
        'phone': 'player-plasma-ias-phone-en_US.vflset/base.js',
    }
    path = paths.get(JSC_PLAYER_VARIANT, paths['tv'])
    return f'https://www.youtube.com/s/player/{player_id}/{path}'


def playability_error(data: dict[str, Any]) -> str:
    status = data.get("playabilityStatus") or {}
    return " | ".join(str(x) for x in [status.get("status"), status.get("reason")] if x)


async def probe_direct_media(media_url: str, user_agent: str) -> None:
    client = get_http_client()
    response = await client.get(
        media_url,
        headers={
            "Range": "bytes=0-0",
            "Accept-Encoding": "identity",
            "User-Agent": user_agent,
        },
        timeout=max(2.0, min(DIRECT_FAST_TIMEOUT_SECONDS, 4.0)),
    )
    try:
        if response.status_code not in {200, 206}:
            raise RuntimeError(f"Google Video probe returned HTTP {response.status_code}")
    finally:
        await response.aclose()


async def probe_media_url(
    media_url: str,
    source_headers: dict[str, str],
    video_id: str,
    label: str,
) -> None:
    """Verify that a signed Google Video URL returns media without downloading it."""
    started = time.monotonic()
    allowed = {"user-agent", "referer", "origin"}
    headers = {
        key: value for key, value in source_headers.items()
        if key.lower() in allowed
    }
    headers.setdefault("User-Agent", str(MWEB_DIRECT_CONFIG.get("userAgent") or "Mozilla/5.0"))
    headers.setdefault("Referer", "https://www.youtube.com/")
    headers["Range"] = "bytes=0-0"
    headers["Accept-Encoding"] = "identity"

    client = get_http_client()
    request = client.build_request("GET", media_url, headers=headers)
    response = await client.send(request, stream=True)
    try:
        if response.status_code not in {200, 206}:
            raise RuntimeError(f"{label} Google Video probe returned HTTP {response.status_code}")
        print("v36.15 media probe success", json.dumps({
            "videoId": video_id,
            "label": label,
            "status": response.status_code,
            "elapsedSeconds": round(time.monotonic() - started, 3),
        }), flush=True)
    finally:
        await response.aclose()


async def direct_resolve_one(video_id: str, client_name: str, purpose: str) -> ResolvedMedia:
    global _visitor_data
    started = time.monotonic()
    if client_on_cooldown(client_name):
        raise RuntimeError(f"{client_name} is on cooldown")
    client = get_http_client()
    try:
        response = await client.post(
            INNERTUBE_PLAYER_URL,
            params={"key": INNERTUBE_API_KEY, "prettyPrint": "false"},
            headers=innertube_headers(client_name),
            json=innertube_payload(video_id, client_name),
            timeout=DIRECT_FAST_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        raise RuntimeError(f"Innertube transport failed: {exc}") from exc
    if response.status_code != 200:
        raise RuntimeError(f"Innertube HTTP {response.status_code}")
    try:
        data = response.json()
    except Exception as exc:
        raise RuntimeError(f"Innertube returned invalid JSON: {exc}") from exc

    visitor = ((data.get("responseContext") or {}).get("visitorData"))
    if isinstance(visitor, str) and visitor:
        _visitor_data = visitor

    fmt = select_direct_format(data)
    if not fmt:
        reason = playability_error(data) or "no already-signed non-DRM direct format"
        low = reason.lower()
        if "sign in" in low or "bot" in low or "login_required" in low:
            cool_down_client(client_name, reason)
        raise RuntimeError(reason)

    details = data.get("videoDetails") or {}
    mime = str(fmt.get("mimeType") or "")
    mime_base = mime.split(";", 1)[0].strip() or None
    media_url = str(fmt["url"])
    user_agent = str(DIRECT_CLIENTS[client_name].get("userAgent") or "Mozilla/5.0")
    await probe_direct_media(media_url, user_agent)
    headers = {"User-Agent": user_agent}
    media = ResolvedMedia(
        video_id=video_id,
        url=media_url,
        http_headers=headers,
        client=client_name,
        format_id=str(fmt.get("itag")) if fmt.get("itag") is not None else None,
        ext="mp4" if "mp4" in mime else None,
        content_type=mime_base,
        acodec=None,
        vcodec=None,
        abr=(float(fmt.get("averageBitrate") or fmt.get("bitrate")) / 1000) if isinstance(fmt.get("averageBitrate") or fmt.get("bitrate"), (int, float)) else None,
        duration=float(details.get("lengthSeconds")) if str(details.get("lengthSeconds") or "").isdigit() else None,
        title=str(details.get("title")) if details.get("title") is not None else None,
        resolved_at=time.time(),
        expires_at=resolved_expiry(media_url),
        resolver_path="innertube-direct",
    )
    print("direct innertube resolve success", json.dumps({
        "videoId": video_id,
        "purpose": purpose,
        "client": client_name,
        "formatId": media.format_id,
        "elapsedSeconds": round(time.monotonic() - started, 3),
    }), flush=True)
    return media


async def resolve_direct_fast(video_id: str, purpose: str) -> ResolvedMedia:
    candidates = [name for name in DIRECT_CLIENT_ORDER if not client_on_cooldown(name)]
    if not candidates:
        raise RuntimeError("all direct Innertube clients are on cooldown")

    # Race the first two clients. The player API calls are tiny, so racing two is
    # far cheaper than sequentially paying several seconds before fallback.
    race = candidates[:2]
    tasks = {asyncio.create_task(direct_resolve_one(video_id, name, purpose)): name for name in race}
    errors: list[str] = []
    try:
        for done in asyncio.as_completed(tasks):
            try:
                winner = await done
                for task in tasks:
                    if not task.done():
                        task.cancel()
                return winner
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors.append(str(exc))
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()

    # If a third client exists, try it once after the race.
    for name in candidates[2:]:
        try:
            return await direct_resolve_one(video_id, name, purpose)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    raise RuntimeError("direct fast path failed: " + " || ".join(errors)[-1800:])


def resolved_from_direct_format(
    video_id: str,
    media_url: str,
    fmt: dict[str, Any],
    client_name: str,
    resolver_path: str,
    details: dict[str, Any] | None = None,
) -> ResolvedMedia:
    """Convert one verified Innertube format into Veeb's proxy metadata."""
    details = details or {}
    mime = str(fmt.get("mimeType") or "")
    mime_base = mime.split(";", 1)[0].strip() or None
    codecs_match = re.search(r'codecs="([^"]+)"', mime)
    codecs = [item.strip() for item in codecs_match.group(1).split(",")] if codecs_match else []
    acodec = None
    vcodec = None
    if mime_base and mime_base.startswith("audio/"):
        acodec = codecs[0] if codecs else None
        vcodec = "none"
    elif mime_base and mime_base.startswith("video/"):
        vcodec = codecs[0] if codecs else None
        acodec = codecs[1] if len(codecs) > 1 else None

    raw_bitrate = fmt.get("averageBitrate") or fmt.get("bitrate")
    try:
        abr = float(raw_bitrate) / 1000 if raw_bitrate is not None else None
    except (TypeError, ValueError):
        abr = None

    duration = None
    raw_duration_ms = fmt.get("approxDurationMs")
    try:
        if raw_duration_ms is not None:
            duration = float(raw_duration_ms) / 1000
        elif details.get("lengthSeconds") is not None:
            duration = float(details.get("lengthSeconds"))
    except (TypeError, ValueError):
        duration = None

    ext = None
    if mime_base:
        if "mp4" in mime_base:
            ext = "mp4"
        elif "webm" in mime_base:
            ext = "webm"

    user_agent = str(MWEB_DIRECT_CONFIG.get("userAgent") or "Mozilla/5.0") if client_name == "mweb" else "Mozilla/5.0"
    return ResolvedMedia(
        video_id=video_id,
        url=media_url,
        http_headers={
            "User-Agent": user_agent,
            "Referer": "https://www.youtube.com/",
        },
        client=client_name,
        format_id=str(fmt.get("itag")) if fmt.get("itag") is not None else None,
        ext=ext,
        content_type=mime_base,
        acodec=acodec,
        vcodec=vcodec,
        abr=abr,
        duration=duration,
        title=str(details.get("title")) if details.get("title") is not None else None,
        resolved_at=time.time(),
        expires_at=resolved_expiry(media_url),
        resolver_path=resolver_path,
    )


def append_query_param(url: str, key: str, value: str) -> str:
    """Add one query value without re-encoding YouTube's signed URL.

    Rebuilding the entire query string can change escaping inside signed Google
    Video URLs and invalidate the signature. Only the new value is encoded.
    """
    base, marker, fragment = url.partition("#")
    encoded_pair = urlencode({key: value})
    encoded_key = encoded_pair.split("=", 1)[0]
    existing = re.compile(rf"([?&]){re.escape(encoded_key)}=[^&#]*")
    if existing.search(base):
        base = existing.sub(lambda match: match.group(1) + encoded_pair, base, count=1)
    else:
        base += ("&" if "?" in base else "?") + encoded_pair
    return base + (("#" + fragment) if marker else "")


def mweb_context() -> dict[str, Any]:
    cfg = dict(MWEB_DIRECT_CONFIG)
    cfg.setdefault("timeZone", "UTC")
    cfg.setdefault("utcOffsetMinutes", 0)
    return {
        "client": cfg,
        "request": {"useSsl": True, "internalExperimentFlags": []},
        "user": {"lockedSafetyMode": False},
    }


def mweb_headers(
    data_sync_id: str | None = None, visitor_data: str | None = None,
) -> dict[str, str]:
    """Generate mweb API headers using the same session-bound cookie auth model as yt-dlp."""
    origin = "https://www.youtube.com"
    delegated_session_id, user_session_id = parse_data_sync_session(data_sync_id)
    headers = {
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Content-Type": "application/json",
        "Origin": origin,
        "User-Agent": str(MWEB_DIRECT_CONFIG["userAgent"]),
        "X-Youtube-Client-Name": "2",
        "X-Youtube-Client-Version": str(MWEB_DIRECT_CONFIG["clientVersion"]),
    }
    effective_visitor = visitor_data or _visitor_data
    if effective_visitor:
        headers["X-Goog-Visitor-Id"] = effective_visitor
    if load_youtube_cookie_session():
        if _youtube_cookie_header:
            headers["Cookie"] = _youtube_cookie_header
        authorization = youtube_cookie_auth_header(origin, user_session_id=user_session_id)
        if authorization:
            headers["Authorization"] = authorization
            headers["X-Origin"] = origin
        if delegated_session_id:
            headers["X-Goog-PageId"] = delegated_session_id
        headers["X-Goog-AuthUser"] = "0"
        headers["X-Youtube-Bootstrap-Logged-In"] = "true"
    return headers


async def get_bgutil_pot(content_binding: str, context: dict[str, Any], label: str) -> tuple[str, str | None, float]:
    """Ask bgutil for one WebPO token and retain its advertised expiry."""
    started = time.monotonic()
    client = get_http_client()
    response = await client.post(
        BGUTIL_BASE_URL + "/get_pot",
        json={
            "content_binding": content_binding,
            "innertube_context": context,
            "bypass_cache": False,
        },
        timeout=DIRECT_MWEB_POT_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        raise RuntimeError(f"bgutil /get_pot returned HTTP {response.status_code}: {response.text[-500:]}")
    try:
        payload = response.json()
    except Exception as exc:
        raise RuntimeError(f"bgutil returned invalid JSON: {exc}") from exc
    token = payload.get("poToken")
    if not isinstance(token, str) or not token:
        raise RuntimeError("bgutil returned no poToken")
    returned_binding = payload.get("contentBinding")
    raw_expires_at = payload.get("expiresAt")
    if raw_expires_at in (None, ""):
        expires_at = time.time() + 300
    elif isinstance(raw_expires_at, (int, float)):
        expires_at = float(raw_expires_at)
        if expires_at > 10_000_000_000:
            expires_at /= 1000.0
    elif isinstance(raw_expires_at, str):
        value = raw_expires_at.strip()
        try:
            expires_at = float(value)
            if expires_at > 10_000_000_000:
                expires_at /= 1000.0
        except ValueError:
            try:
                expires_at = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError as exc:
                raise RuntimeError(f"bgutil returned invalid expiresAt: {raw_expires_at!r}") from exc
    else:
        raise RuntimeError(f"bgutil returned unsupported expiresAt type: {type(raw_expires_at).__name__}")

    print("v36.15 POT ready", json.dumps({
        "bindingType": label,
        "elapsedSeconds": round(time.monotonic() - started, 3),
        "bindingMatches": str(returned_binding) == content_binding,
        "expiresInSeconds": max(0, round(expires_at - time.time())),
    }), flush=True)
    return token, str(returned_binding) if returned_binding is not None else None, expires_at


async def fetch_data_sync_id() -> str:
    """Resolve the logged-in YouTube Data Sync ID once for authenticated GVS WebPO."""
    global _data_sync_id
    if _data_sync_id:
        return _data_sync_id
    if not load_youtube_cookie_session():
        raise RuntimeError("authenticated cookies are required for Data Sync GVS binding")
    started = time.monotonic()
    client = get_http_client()
    headers = mweb_headers()
    headers["Accept"] = "text/html,application/xhtml+xml"
    response = await client.get("https://www.youtube.com/", headers=headers, timeout=6.0)
    if response.status_code != 200:
        raise RuntimeError(f"YouTube session bootstrap returned HTTP {response.status_code}")
    text = response.text
    patterns = (
        r'"DATASYNC_ID"\s*:\s*"([^"\]+)"',
        r'"datasyncId"\s*:\s*"([^"\]+)"',
        r'DATASYNC_ID\\?"\s*:\s*\\?"([^"\]+)',
    )
    value = None
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = match.group(1).replace("\\u003d", "=").replace("\\/", "/")
            break
    if not value:
        raise RuntimeError("authenticated YouTube page did not expose DATASYNC_ID")
    _data_sync_id = value
    print("v36.1 Data Sync ID ready", json.dumps({
        "elapsedSeconds": round(time.monotonic() - started, 3),
        "length": len(value),
    }), flush=True)
    return value


async def get_session_gvs_pot() -> tuple[str, str]:
    """Return a reusable authenticated GVS token bound to the Data Sync ID."""
    global _session_gvs_pot, _session_gvs_pot_expires_at
    if _session_gvs_pot and time.time() < (_session_gvs_pot_expires_at - 60):
        return _session_gvs_pot, await fetch_data_sync_id()
    async with _session_gvs_lock:
        if _session_gvs_pot and time.time() < (_session_gvs_pot_expires_at - 60):
            return _session_gvs_pot, await fetch_data_sync_id()
        binding = await fetch_data_sync_id()
        token, returned_binding, expires_at = await get_bgutil_pot(binding, mweb_context(), "gvs-session")
        if returned_binding and returned_binding != binding:
            raise RuntimeError("bgutil returned an unexpected GVS content binding")
        _session_gvs_pot = token
        _session_gvs_pot_expires_at = expires_at
        print("v36.1 session GVS POT cached", json.dumps({
            "expiresInSeconds": max(0, round(expires_at - time.time())),
        }), flush=True)
        return token, binding


async def warm_session_gvs_pot() -> None:
    try:
        await get_session_gvs_pot()
    except Exception as exc:
        print("v36.1 session GVS warm failed", json.dumps({"error": str(exc)[-1000:]}), flush=True)


def _consume_simple_task(task: asyncio.Task[Any], label: str, video_id: str) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        print(label + " background task failed", json.dumps({
            "videoId": video_id,
            "error": str(exc)[-1000:],
        }), flush=True)


async def fetch_global_player_bootstrap(video_id: str | None = None, force: bool = False) -> dict[str, Any]:
    """Discover the active YouTube player without depending on /watch.

    /iframe_api is a tiny global script and is not video-specific. It exposes the
    active player build id. If that route is unavailable, an embed page is used as
    a last-resort metadata source. Neither route is used to resolve media formats;
    all media discovery still comes from the direct Innertube /player response.
    """
    global _global_player_bootstrap, _global_player_bootstrap_expires_at
    now = time.time()
    if (
        not force
        and _global_player_bootstrap
        and now < _global_player_bootstrap_expires_at
    ):
        return dict(_global_player_bootstrap)

    async with _global_player_bootstrap_lock:
        now = time.time()
        if (
            not force
            and _global_player_bootstrap
            and now < _global_player_bootstrap_expires_at
        ):
            return dict(_global_player_bootstrap)

        started = time.monotonic()
        client = get_http_client()
        headers = {
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'User-Agent': str(MWEB_DIRECT_CONFIG['userAgent']),
        }
        errors: list[str] = []
        player_url: str | None = None
        source = None

        try:
            response = await client.get(
                'https://www.youtube.com/iframe_api',
                headers=headers,
                timeout=4.0,
            )
            if response.status_code == 200:
                text = response.text.replace('\\/', '/')
                match = re.search(r'/s/player/([A-Za-z0-9_-]{8,})/', text)
                if match:
                    player_id = match.group(1)
                    player_url = f'https://www.youtube.com/s/player/{player_id}/tv-player-ias.vflset/tv-player-ias.js'
                    source = 'iframe-api'
                else:
                    errors.append('iframe-api:no-player-id')
            else:
                errors.append(f'iframe-api:http-{response.status_code}')
        except Exception as exc:
            errors.append('iframe-api:' + str(exc)[-300:])

        if not player_url and video_id:
            try:
                response = await client.get(
                    f'https://www.youtube.com/embed/{video_id}',
                    params={'hl': 'en'},
                    headers={**headers, 'Accept': 'text/html,application/xhtml+xml'},
                    timeout=4.0,
                )
                if response.status_code == 200:
                    text = response.text.replace('\\/', '/')
                    match = re.search(
                        r'(?:(?:"jsUrl"|"PLAYER_JS_URL"|"js")\s*:\s*")([^"\\]+/s/player/[^"\\]+\.js[^"\\]*)',
                        text,
                    )
                    if not match:
                        match = re.search(r'(/s/player/[A-Za-z0-9_-]+/[^"\\]+\.js[^"\\]*)', text)
                    if match:
                        actual = normalize_player_url(match.group(1))
                        if actual:
                            player_url = jsc_player_url(actual)
                            source = 'embed'
                    if not player_url:
                        errors.append('embed:no-player-url')
                else:
                    errors.append(f'embed:http-{response.status_code}')
            except Exception as exc:
                errors.append('embed:' + str(exc)[-300:])

        if not player_url:
            raise RuntimeError('global player discovery failed: ' + ' || '.join(errors)[-1200:])

        result = {
            'playerUrl': player_url,
            'playerId': (re.search(r'/s/player/([A-Za-z0-9_-]{8,})/', player_url) or [None, None])[1],
            'source': source,
        }
        _global_player_bootstrap = result
        _global_player_bootstrap_expires_at = time.time() + GLOBAL_PLAYER_BOOTSTRAP_TTL_SECONDS
        print('v36.15 global player bootstrap ready', json.dumps({
            'source': source,
            'playerId': result.get('playerId'),
            'playerVariant': JSC_PLAYER_VARIANT,
            'elapsedSeconds': round(time.monotonic() - started, 3),
            'ttlSeconds': GLOBAL_PLAYER_BOOTSTRAP_TTL_SECONDS,
        }), flush=True)
        return dict(result)


async def warm_global_player_bootstrap() -> None:
    try:
        await fetch_global_player_bootstrap()
    except Exception as exc:
        print('v36.15 global player warm missed', json.dumps({
            'error': str(exc)[-1200:],
        }), flush=True)


async def fetch_mweb_player_bootstrap(video_id: str) -> dict[str, Any]:
    """Fetch only the tiny watch-page config needed to mirror yt-dlp's /player call.

    This is intentionally not a full extraction. It discovers the active player JS,
    signatureTimestamp (STS), visitor data, and Data Sync ID when present.
    """
    global _visitor_data, _data_sync_id
    started = time.monotonic()
    client = get_http_client()
    headers = mweb_headers()
    headers.pop("Content-Type", None)
    headers["Accept"] = "text/html,application/xhtml+xml"
    response = await client.get(
        "https://www.youtube.com/watch",
        params={"v": video_id, "bpctr": "9999999999", "has_verified": "1"},
        headers=headers,
        timeout=4.0,
    )
    if response.status_code != 200:
        fallback_headers = {
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": str(MWEB_DIRECT_CONFIG["userAgent"]),
        }
        if _youtube_cookie_header:
            fallback_headers["Cookie"] = _youtube_cookie_header
        response = await client.get(
            "https://www.youtube.com/watch",
            params={"v": video_id, "bpctr": "9999999999", "has_verified": "1"},
            headers=fallback_headers,
            timeout=4.0,
        )
    if response.status_code != 200:
        raise RuntimeError(f"mweb watch bootstrap returned HTTP {response.status_code}")
    text = response.text

    def first(patterns: tuple[str, ...]) -> str | None:
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1).replace('\\u0026', '&').replace('\\u003d', '=').replace('\\/', '/')
        return None

    visitor = first((
        r'"VISITOR_DATA"\s*:\s*"([^"]+)"',
        r'"visitorData"\s*:\s*"([^"]+)"',
    ))
    if visitor:
        _visitor_data = visitor

    data_sync_id = first((
        r'"DATASYNC_ID"\s*:\s*"([^"]+)"',
        r'"datasyncId"\s*:\s*"([^"]+)"',
    ))
    if data_sync_id:
        _data_sync_id = data_sync_id

    player_url = first((
        r'"PLAYER_JS_URL"\s*:\s*"([^"]+)"',
        r'"jsUrl"\s*:\s*"([^"]+base\.js[^"]*)"',
        r'"js"\s*:\s*"([^"]+base\.js[^"]*)"',
    ))
    if player_url and player_url.startswith('//'):
        player_url = 'https:' + player_url
    elif player_url and player_url.startswith('/'):
        player_url = 'https://www.youtube.com' + player_url

    sts = None
    sts_text = first((
        r'"STS"\s*:\s*(\d{5})',
        r'"signatureTimestamp"\s*:\s*(\d{5})',
    ))
    if sts_text:
        sts = int(sts_text)
    elif player_url:
        sts = _player_sts_cache.get(player_url)
        if sts is None:
            js_headers = mweb_headers()
            js_headers.pop("Content-Type", None)
            js_response = await client.get(player_url, headers=js_headers, timeout=5.0)
            if js_response.status_code == 200:
                match = re.search(r'(?:signatureTimestamp|sts)\s*:\s*(\d{5})', js_response.text)
                if match:
                    sts = int(match.group(1))
                    _player_sts_cache[player_url] = sts

    result = {
        "sts": sts,
        "playerUrl": player_url,
        "visitorData": visitor,
        "dataSyncId": data_sync_id,
    }
    print("v36.15 mweb bootstrap ready", json.dumps({
        "videoId": video_id,
        "elapsedSeconds": round(time.monotonic() - started, 3),
        "hasSts": sts is not None,
        "hasPlayerUrl": bool(player_url),
        "hasVisitorData": bool(visitor),
        "hasDataSyncId": bool(data_sync_id),
    }), flush=True)
    return result


async def ensure_youtubejs_helper_ready() -> dict[str, Any]:
    """Verify that the persistent YouTube.js player-decipher helper is warm."""
    started = time.monotonic()
    client = get_http_client()
    response = await client.get(
        YOUTUBEJS_BASE_URL + "/health",
        timeout=min(5.0, YOUTUBEJS_DECIPHER_TIMEOUT_SECONDS),
    )
    if response.status_code != 200:
        raise RuntimeError(f"YouTube.js helper health returned HTTP {response.status_code}")
    payload = response.json()
    if not payload.get("ready"):
        raise RuntimeError("YouTube.js helper is not ready")
    print("v36.15 YouTube.js helper ready", json.dumps({
        "playerId": payload.get("playerId"),
        "signatureTimestamp": payload.get("signatureTimestamp"),
        "elapsedSeconds": round(time.monotonic() - started, 3),
    }), flush=True)
    return payload


async def youtubejs_decipher_format_url(video_id: str, fmt: dict[str, Any]) -> str:
    """Decipher one Innertube format through the persistent YouTube.js Player.

    YouTube.js extracts and caches the active player's signature and nsig logic.
    This avoids launching Deno/EJS for every track while preserving the fast
    direct mweb Innertube discovery path.
    """
    started = time.monotonic()
    direct_url = fmt.get("url") if isinstance(fmt.get("url"), str) else None
    signature_cipher = fmt.get("signatureCipher") if isinstance(fmt.get("signatureCipher"), str) else None
    cipher = fmt.get("cipher") if isinstance(fmt.get("cipher"), str) else None
    if not direct_url and not signature_cipher and not cipher:
        raise RuntimeError("selected Innertube format has no URL or signature cipher")

    client = get_http_client()
    response = await client.post(
        YOUTUBEJS_BASE_URL + "/decipher",
        json={
            "videoId": video_id,
            "url": direct_url,
            "signatureCipher": signature_cipher,
            "cipher": cipher,
        },
        timeout=YOUTUBEJS_DECIPHER_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        detail = response.text[-1200:]
        raise RuntimeError(
            f"YouTube.js decipher returned HTTP {response.status_code}"
            + (f": {detail}" if detail else "")
        )
    payload = response.json()
    media_url = payload.get("url")
    if not isinstance(media_url, str) or not media_url.startswith("http"):
        raise RuntimeError("YouTube.js helper returned no media URL")

    global _youtubejs_ready, _youtubejs_player_id
    _youtubejs_ready = True
    if isinstance(payload.get("playerId"), str):
        _youtubejs_player_id = payload.get("playerId")
    print("v36.15 YouTube.js decipher success", json.dumps({
        "videoId": video_id,
        "formatId": str(fmt.get("itag")) if fmt.get("itag") is not None else None,
        "playerId": payload.get("playerId"),
        "helperElapsedMs": payload.get("elapsedMs"),
        "elapsedSeconds": round(time.monotonic() - started, 3),
    }), flush=True)
    return media_url


async def warm_bgutil_integrity() -> None:
    """Prime bgutil's expensive integrity state before the first user presses play."""
    if not VIDEO_ID_RE.fullmatch(BGUTIL_WARM_VIDEO_ID):
        print("v36.15 bgutil warm skipped", json.dumps({
            "reason": "VEEB_BGUTIL_WARM_VIDEO_ID is invalid",
        }), flush=True)
        return
    started = time.monotonic()
    try:
        await get_bgutil_pot(BGUTIL_WARM_VIDEO_ID, mweb_context(), "startup-integrity-warm")
        print("v36.15 bgutil integrity warm", json.dumps({
            "elapsedSeconds": round(time.monotonic() - started, 3),
        }), flush=True)
    except Exception as exc:
        print("v36.15 bgutil warm failed", json.dumps({
            "error": str(exc)[-1200:],
            "elapsedSeconds": round(time.monotonic() - started, 3),
        }), flush=True)


async def resolve_direct_mweb_pot(video_id: str, purpose: str) -> ResolvedMedia:
    """Resolve a cold track with direct Innertube + YouTube.js + bgutil.

    The direct mweb /player request, YouTube.js signature/nsig decipher and the
    video-bound GVS token are independent pieces. Player discovery starts first;
    deciphering begins as soon as itag 18 arrives, while bgutil generates the PO
    token in parallel. The slow generic yt-dlp extractor is only a fallback.
    """
    global _visitor_data
    started = time.monotonic()
    context = mweb_context()
    client = get_http_client()

    async def call_plain_player() -> tuple[dict[str, Any], dict[str, Any]]:
        global _visitor_data
        payload: dict[str, Any] = {
            "context": context,
            "videoId": video_id,
            "contentCheckOk": True,
            "racyCheckOk": True,
            "playbackContext": {
                "contentPlaybackContext": {
                    "html5Preference": "HTML5_PREF_WANTS",
                },
                "adPlaybackContext": {"pyv": True},
            },
        }
        t0 = time.monotonic()
        response = await client.post(
            INNERTUBE_PLAYER_URL,
            params={"key": INNERTUBE_API_KEY, "prettyPrint": "false"},
            headers=mweb_headers(),
            json=payload,
            timeout=DIRECT_MWEB_PLAYER_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            raise RuntimeError(f"plain mweb /player returned HTTP {response.status_code}")
        data = response.json()
        visitor = ((data.get("responseContext") or {}).get("visitorData"))
        if isinstance(visitor, str) and visitor:
            _visitor_data = visitor
        fmt = select_direct_format(data)
        if not fmt:
            streaming = data.get("streamingData") or {}
            diagnostics = {
                "status": ((data.get("playabilityStatus") or {}).get("status")),
                "formats": len(streaming.get("formats") or []),
                "adaptiveFormats": len(streaming.get("adaptiveFormats") or []),
                "hasServerAbr": bool(streaming.get("serverAbrStreamingUrl")),
                "itag18Present": any(
                    str(item.get("itag")) == SOURCE_FORMAT
                    for item in (list(streaming.get("formats") or []) + list(streaming.get("adaptiveFormats") or []))
                    if isinstance(item, dict)
                ),
            }
            raise RuntimeError(
                "plain mweb /player returned no usable format: "
                + (playability_error(data) or "unknown")
                + " diagnostics=" + json.dumps(diagnostics, separators=(",", ":"))
            )
        print("v36.15 mweb player candidate ready", json.dumps({
            "videoId": video_id,
            "purpose": purpose,
            "candidate": "plain",
            "formatId": str(fmt.get("itag")) if fmt.get("itag") is not None else None,
            "urlMode": "direct" if isinstance(fmt.get("url"), str) else "cipher",
            "hasSignatureCipher": bool(fmt.get("signatureCipher") or fmt.get("cipher")),
            "elapsedSeconds": round(time.monotonic() - t0, 3),
        }), flush=True)
        return data, fmt

    player_task = asyncio.create_task(call_plain_player())
    pot_task = asyncio.create_task(get_bgutil_pot(video_id, context, "gvs-video-candidate"))

    try:
        _data, fmt = await player_task
    except BaseException:
        if not pot_task.done():
            pot_task.cancel()
        raise
    decipher_task = asyncio.create_task(youtubejs_decipher_format_url(video_id, fmt))

    try:
        base_url, pot_result = await asyncio.gather(decipher_task, pot_task)
    except Exception:
        for task in (decipher_task, pot_task):
            if not task.done():
                task.cancel()
        raise

    video_token, returned_binding, expires_at = pot_result
    if returned_binding and returned_binding != video_id:
        raise RuntimeError("bgutil returned unexpected video binding")

    media_url = append_query_param(base_url, "pot", video_token)
    await probe_media_url(
        media_url,
        mweb_headers(),
        video_id,
        "v36.15-youtubejs-video",
    )
    media = resolved_from_direct_format(
        video_id,
        media_url,
        fmt,
        "mweb",
        "mweb-innertube-youtubejs-v36.15",
        details=(_data.get("videoDetails") or {}),
    )
    print("v36.15 direct mweb resolve success", json.dumps({
        "videoId": video_id,
        "playerCandidate": "plain",
        "proofCandidate": "video",
        "formatId": media.format_id,
        "expiresInSeconds": max(0, round(expires_at - time.time())),
        "elapsedSeconds": round(time.monotonic() - started, 3),
    }), flush=True)
    return media


class JscDiagnosticLogger:
    """Filtered yt-dlp logger for the direct JS challenge engine.

    The previous builds silenced the exact provider rejection that mattered.
    Keep normal yt-dlp chatter out of Render logs, but preserve JSC/provider/runtime
    diagnostics. Challenge strings are truncated so logs stay readable.
    """

    _KEYWORDS = (
        "jsc", "challenge", "provider", "deno", "ejs", "npm",
        "remote component", "javascript runtime", "signature",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._recent: list[str] = []

    @staticmethod
    def _clean(message: Any) -> str:
        text = str(message).replace("\x00", "<NUL>")
        text = re.sub(r"challenges=\[[^\]]{120,}\]", "challenges=[<truncated>]", text)
        text = re.sub(r"(input\s*=\s*).{600,}", r"\1<truncated>", text)
        return text[-1800:]

    def _emit(self, level: str, message: Any) -> None:
        text = self._clean(message)
        low = text.lower()
        if not JSC_TRACE and level == "debug":
            return
        if level == "debug" and not any(word in low for word in self._KEYWORDS):
            return
        with self._lock:
            self._recent.append(f"{level}: {text}")
            self._recent = self._recent[-12:]
        print("v36.15 jsc yt-dlp", json.dumps({
            "level": level,
            "message": text,
        }), flush=True)

    def debug(self, message: Any) -> None:
        self._emit("debug", message)

    def warning(self, message: Any) -> None:
        self._emit("warning", message)

    def error(self, message: Any) -> None:
        self._emit("error", message)

    def tail(self) -> list[str]:
        with self._lock:
            return list(self._recent[-6:])


class DirectCipherSolver:
    """Solve one already-discovered format with yt-dlp's JSC framework.

    This engine deliberately uses the SAME YouTube/mweb extractor configuration
    as Veeb's known-working foreground yt-dlp engine. Unlike V36.10, it does not
    create a stripped-down YoutubeDL instance that can initialize a different JSC
    provider environment.

    SIG follows yt-dlp's permutation-spec model: solve a synthetic string whose
    length equals the encrypted signature, cache the returned permutation, and
    apply it locally to each track's real ``s`` value.
    """

    def __init__(self) -> None:
        self.logger = JscDiagnosticLogger()

        # Build from the exact working mweb options, then enable JSC tracing.
        opts = ytdlp_options(YTDLP_POT_CLIENT, self.logger)
        opts["quiet"] = False
        opts["no_warnings"] = False
        opts["verbose"] = bool(JSC_TRACE)
        yt_args = dict(opts.get("extractor_args", {}).get("youtube", {}))
        if JSC_TRACE:
            yt_args["jsc_trace"] = ["true"]
        opts["extractor_args"] = {"youtube": yt_args}
        if JSC_REMOTE_COMPONENTS:
            # Current Deno JSC can use the ejs:npm component. Allowing it here
            # makes provider availability explicit instead of silently returning
            # zero results when the required solver source is not already cached.
            opts["remote_components"] = list(JSC_REMOTE_COMPONENTS)

        cookie_file = get_writable_cookie_file()
        if cookie_file:
            opts["cookiefile"] = cookie_file

        self.ydl = yt_dlp.YoutubeDL(opts)
        self.ie = self.ydl.get_info_extractor("Youtube")
        self.ie.initialize()
        self.lock = threading.Lock()
        self._sig_spec_cache: dict[tuple[str, int], list[int]] = {}
        self._provider_snapshot("startup", [])

    @staticmethod
    def _supported_types(provider: Any) -> list[str] | None:
        supported = getattr(provider, "_SUPPORTED_TYPES", None)
        if supported is None:
            return None
        out: list[str] = []
        try:
            for item in supported:
                out.append(getattr(item, "value", str(item)))
        except Exception:
            return [str(supported)]
        return out

    def _provider_snapshot(self, reason: str, requests: list[JsChallengeRequest]) -> None:
        director = getattr(self.ie, "_jsc_director", None)
        providers = getattr(director, "providers", {}) if director is not None else {}
        preferences = getattr(director, "preferences", []) if director is not None else []
        snapshot: list[dict[str, Any]] = []
        for key, provider in providers.items():
            try:
                available: bool | str = bool(provider.is_available())
            except Exception as exc:
                available = f"error:{type(exc).__name__}:{exc}"
            score = None
            if requests and isinstance(available, bool) and available:
                try:
                    score = sum(pref(provider, requests) for pref in preferences)
                except Exception as exc:
                    score = f"error:{type(exc).__name__}:{exc}"
            runtime_info = getattr(provider, "runtime_info", None)
            runtime_path = getattr(runtime_info, "path", None) if runtime_info is not None else None
            snapshot.append({
                "key": str(key),
                "name": str(getattr(provider, "PROVIDER_NAME", type(provider).__name__)),
                "class": type(provider).__name__,
                "available": available,
                "preference": score,
                "supportedTypes": self._supported_types(provider),
                "runtimePath": str(runtime_path) if runtime_path else None,
            })
        print("v36.15 JSC provider snapshot", json.dumps({
            "reason": reason,
            "runtime": JSC_RUNTIME,
            "runtimeBinary": shutil.which(JSC_RUNTIME),
            "remoteComponents": JSC_REMOTE_COMPONENTS,
            "providerCount": len(snapshot),
            "providers": snapshot,
        }), flush=True)

    def solve(self, video_id: str, fmt: dict[str, Any], player_url: str) -> str:
        with self.lock:
            direct = fmt.get("url")
            cipher_text = fmt.get("signatureCipher") or fmt.get("cipher")
            encrypted_sig = None
            sp = "signature"
            if isinstance(direct, str) and direct.startswith("http"):
                media_url = direct
            elif isinstance(cipher_text, str):
                sc = parse_qs(cipher_text)
                media_url = (sc.get("url") or [None])[0]
                encrypted_sig = (sc.get("s") or [None])[0]
                sp = (sc.get("sp") or ["signature"])[0]
                if not isinstance(media_url, str) or not media_url.startswith("http"):
                    raise RuntimeError("signatureCipher contained no media URL")
            else:
                raise RuntimeError("selected format has neither url nor signatureCipher")

            parsed = urlparse(media_url)
            query = parse_qs(parsed.query, keep_blank_values=True)
            n_challenge = (query.get("n") or [None])[0]
            requests: list[JsChallengeRequest] = []
            sig_spec_key: tuple[str, int] | None = None
            synthetic_sig_challenge: str | None = None
            cached_sig_spec: list[int] | None = None

            if encrypted_sig:
                sig_spec_key = (player_url, len(encrypted_sig))
                cached_sig_spec = self._sig_spec_cache.get(sig_spec_key)
                if cached_sig_spec is None:
                    synthetic_sig_challenge = ''.join(map(chr, range(len(encrypted_sig))))
                    requests.append(JsChallengeRequest(
                        type=JsChallengeType.SIG,
                        video_id=video_id,
                        input=SigChallengeInput(
                            player_url=player_url,
                            challenges=[synthetic_sig_challenge],
                        ),
                    ))
                else:
                    print("v36.15 SIG permutation cache hit", json.dumps({
                        "videoId": video_id,
                        "signatureLength": len(encrypted_sig),
                    }), flush=True)

            if n_challenge:
                requests.append(JsChallengeRequest(
                    type=JsChallengeType.N,
                    video_id=video_id,
                    input=NChallengeInput(player_url=player_url, challenges=[n_challenge]),
                ))

            if requests:
                self._provider_snapshot("before-solve", requests)
                player_load_started = time.monotonic()
                print("v36.15 cipher player load started", json.dumps({
                    "videoId": video_id,
                    "playerUrl": player_url[-120:],
                }), flush=True)
                self.ie._load_player(video_id=video_id, player_url=player_url, fatal=True)
                print("v36.15 cipher player loaded", json.dumps({
                    "videoId": video_id,
                    "elapsedSeconds": round(time.monotonic() - player_load_started, 3),
                }), flush=True)

                challenge_started = time.monotonic()
                print("v36.15 cipher challenge solve entered", json.dumps({
                    "videoId": video_id,
                    "hasSig": bool(encrypted_sig),
                    "hasN": bool(n_challenge),
                    "requestTypes": [request.type.value for request in requests],
                }), flush=True)
                results = self.ie._jsc_director.bulk_solve(requests)
                challenge_elapsed = time.monotonic() - challenge_started
                print("v36.15 cipher challenge solve returned", json.dumps({
                    "videoId": video_id,
                    "elapsedSeconds": round(challenge_elapsed, 3),
                    "resultCount": len(results),
                    "resultTypes": [request.type.value for request, _response in results],
                }), flush=True)

                if not results:
                    self._provider_snapshot("zero-results", requests)
                    tail = self.logger.tail()
                    raise RuntimeError(
                        "JSC returned zero challenge results"
                        + ("; lastJscLog=" + tail[-1][-500:] if tail else "")
                    )

                solved: dict[tuple[str, str], str] = {}
                for request, response in results:
                    for challenge, result in response.output.results.items():
                        solved[(request.type.value, challenge)] = result

                if encrypted_sig:
                    sig_spec = cached_sig_spec
                    if sig_spec is None:
                        raw_spec = solved.get((JsChallengeType.SIG.value, synthetic_sig_challenge or ''))
                        if not raw_spec:
                            raise RuntimeError("signature permutation challenge was not solved")
                        sig_spec = [ord(char) for char in raw_spec]
                        if any(index < 0 or index >= len(encrypted_sig) for index in sig_spec):
                            raise RuntimeError("signature permutation contained an out-of-range index")
                        assert sig_spec_key is not None
                        self._sig_spec_cache[sig_spec_key] = sig_spec
                        print("v36.15 SIG permutation cached", json.dumps({
                            "videoId": video_id,
                            "signatureLength": len(encrypted_sig),
                            "specLength": len(sig_spec),
                        }), flush=True)
                    sig = ''.join(encrypted_sig[index] for index in sig_spec)
                    query[sp] = [sig]

                if n_challenge:
                    n_result = solved.get((JsChallengeType.N.value, n_challenge))
                    if not n_result:
                        raise RuntimeError("n challenge was not solved")
                    query["n"] = [n_result]

                media_url = urlunparse(parsed._replace(query=urlencode(query, doseq=True)))
            return media_url

    def close(self) -> None:
        close = getattr(self.ydl, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


_direct_cipher_solver: DirectCipherSolver | None = None


def init_direct_cipher_solver() -> None:
    global _direct_cipher_solver
    if _direct_cipher_solver is None:
        _direct_cipher_solver = DirectCipherSolver()


async def solve_direct_format_url(video_id: str, fmt: dict[str, Any], player_url: str) -> str:
    init_direct_cipher_solver()
    assert _direct_cipher_solver is not None
    started = time.monotonic()
    url = await asyncio.to_thread(_direct_cipher_solver.solve, video_id, fmt, player_url)
    print("v36.15 cipher solved", json.dumps({
        "videoId": video_id,
        "formatId": str(fmt.get("itag")) if fmt.get("itag") is not None else None,
        "hadSignatureCipher": bool(fmt.get("signatureCipher") or fmt.get("cipher")),
        "elapsedSeconds": round(time.monotonic() - started, 3),
    }), flush=True)
    return url


class YtdlpPhaseLogger:
    """Tiny yt-dlp logger that emits only cold-start milestones, never secrets."""

    def __init__(self, engine_id: str):
        self.engine_id = engine_id
        self._lock = threading.Lock()
        self.video_id = ""
        self.purpose = ""
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
            payload = {
                "videoId": self.video_id,
                "purpose": self.purpose,
                "engine": self.engine_id,
                "phase": phase,
                "elapsedSeconds": round(elapsed, 3),
            }
        print("cold resolve phase", json.dumps(payload), flush=True)

    def debug(self, message: str) -> None:
        low = str(message).lower()
        if "downloading webpage" in low:
            self._phase("webpage")
        elif "player api json" in low:
            self._phase("player_api")
        elif "generating a gvs po token" in low or "generating pot" in low:
            self._phase("pot_request")
        elif "solving js challenge" in low or "solving js challenges" in low:
            self._phase("js_challenge")
        elif "downloading player " in low:
            self._phase("player_js")
        elif "downloading 1 format" in low or "format(s):" in low:
            self._phase("format_selected")

    def warning(self, message: str) -> None:
        self.debug(message)

    def error(self, message: str) -> None:
        self.debug(message)


def youtube_extractor_args_dict(client: str) -> dict[str, list[str]]:
    args: dict[str, list[str]] = {}
    if client:
        args["player_client"] = [client]
    if client in {"mweb", "web_music"}:
        args["fetch_pot"] = ["auto"]
        if not YOUTUBE_PREMIUM_ACCOUNT:
            args["use_ad_playback_context"] = ["true"]
    player_skip = ["configs"]
    # Optional turbo mode. Current yt-dlp supports webpage/config skipping when
    # visitor data is explicitly supplied, but its own docs warn this can be less
    # stable. It is therefore available as an environment switch, not forced.
    if YTDLP_SKIP_WEBPAGE_WITH_VISITOR and _visitor_data:
        player_skip = ["webpage", "configs"]
        args["visitor_data"] = [_visitor_data]
    args["player_skip"] = player_skip
    args["skip"] = ["hls", "dash"]
    args["playback_wait"] = [f"{PLAYBACK_WAIT_SECONDS:g}"]
    if JSC_PLAYER_VARIANT not in {"", "actual"}:
        args["player_js_variant"] = [JSC_PLAYER_VARIANT]
    return args


def ytdlp_options(client_name: str, logger: YtdlpPhaseLogger) -> dict[str, Any]:
    cookie_file = get_writable_cookie_file()
    opts: dict[str, Any] = {
        "format": SOURCE_FORMAT,
        "skip_download": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "cachedir": YTDLP_CACHE_DIR,
        "socket_timeout": YTDLP_SOCKET_TIMEOUT_SECONDS,
        "retries": 0,
        "extractor_retries": YTDLP_EXTRACTOR_RETRIES,
        "check_formats": False,
        "js_runtimes": {JSC_RUNTIME: {}},
        "extractor_args": {"youtube": youtube_extractor_args_dict(client_name)},
        "logger": logger,
    }
    if cookie_file:
        opts["cookiefile"] = cookie_file
    return opts


class YtdlpEngine:
    """A long-lived in-process yt-dlp instance dedicated to one extraction at a time."""

    def __init__(self, engine_id: str, client_name: str, resolver_path: str):
        self.engine_id = engine_id
        self.client_name = client_name
        self.resolver_path = resolver_path
        self.logger = YtdlpPhaseLogger(engine_id)
        self.ydl = yt_dlp.YoutubeDL(ytdlp_options(client_name, self.logger))
        # Force the YouTube extractor class to be loaded during app startup, not
        # on the user's first cold tap.
        try:
            self.ydl.get_info_extractor("Youtube")
        except Exception:
            pass

    def resolve(self, video_id: str, purpose: str) -> ResolvedMedia:
        started = time.monotonic()
        self.logger.begin(video_id, purpose)
        # Refresh dynamic visitor data for this isolated engine just before use.
        self.ydl.params["extractor_args"] = {
            "youtube": youtube_extractor_args_dict(self.client_name)
        }
        try:
            info = self.ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}",
                download=False,
            )
        except Exception as exc:
            raise RuntimeError(f"in-process yt-dlp {self.client_name} failed: {exc}") from exc
        if not isinstance(info, dict):
            raise RuntimeError(f"in-process yt-dlp {self.client_name} returned no metadata")
        media_url = str(info.get("url") or "").strip()
        if not media_url.startswith(("https://", "http://")):
            raise RuntimeError(f"in-process yt-dlp {self.client_name} did not return a direct URL")
        raw_headers = info.get("http_headers") or {}
        media = ResolvedMedia(
            video_id=video_id,
            url=media_url,
            http_headers={str(k): str(v) for k, v in raw_headers.items() if v is not None},
            client=self.client_name,
            format_id=str(info.get("format_id")) if info.get("format_id") is not None else None,
            ext=str(info.get("ext")) if info.get("ext") is not None else None,
            content_type=str(info.get("container")) if info.get("container") is not None else None,
            acodec=str(info.get("acodec")) if info.get("acodec") is not None else None,
            vcodec=str(info.get("vcodec")) if info.get("vcodec") is not None else None,
            abr=float(info.get("abr")) if isinstance(info.get("abr"), (int, float)) else None,
            duration=float(info.get("duration")) if isinstance(info.get("duration"), (int, float)) else None,
            title=str(info.get("title")) if info.get("title") is not None else None,
            resolved_at=time.time(),
            expires_at=resolved_expiry(media_url),
            resolver_path=self.resolver_path,
        )
        print("in-process yt-dlp resolve success", json.dumps({
            "videoId": video_id,
            "purpose": purpose,
            "client": self.client_name,
            "engine": self.engine_id,
            "resolverPath": self.resolver_path,
            "formatId": media.format_id,
            "extractionSeconds": round(time.monotonic() - started, 3),
        }), flush=True)
        return media

    def close(self) -> None:
        close = getattr(self.ydl, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


class YtdlpEnginePool:
    def __init__(self, name: str, size: int, client_name: str, resolver_path: str):
        self.name = name
        self.client_name = client_name
        self.resolver_path = resolver_path
        self.engines = [
            YtdlpEngine(f"{name}-{index + 1}", client_name, resolver_path)
            for index in range(size)
        ]
        self.queue: asyncio.Queue[YtdlpEngine] = asyncio.Queue()
        for engine in self.engines:
            self.queue.put_nowait(engine)

    async def resolve(self, video_id: str, purpose: str) -> ResolvedMedia:
        queued_at = time.monotonic()
        engine = await self.queue.get()
        queue_wait = time.monotonic() - queued_at
        print("in-process yt-dlp slot acquired", json.dumps({
            "videoId": video_id,
            "purpose": purpose,
            "pool": self.name,
            "engine": engine.engine_id,
            "queueWaitSeconds": round(queue_wait, 3),
            "availableAfterAcquire": self.queue.qsize(),
        }), flush=True)
        try:
            media = await asyncio.to_thread(engine.resolve, video_id, purpose)
            # Validate the winning URL before allowing it to win a cold race.
            await probe_resolved_media(media)
            return media
        finally:
            self.queue.put_nowait(engine)

    def close(self) -> None:
        for engine in self.engines:
            engine.close()


def init_ytdlp_pools() -> None:
    global _fg_auth_pool, _fg_pot_pool, _prefetch_pool
    if _fg_auth_pool is None:
        _fg_auth_pool = YtdlpEnginePool(
            "fg-auth", YTDLP_FG_AUTH_ENGINES, YTDLP_AUTH_CLIENT, "yt-dlp-auth-inproc-v35"
        )
    if _fg_pot_pool is None:
        _fg_pot_pool = YtdlpEnginePool(
            "fg-pot", YTDLP_FG_POT_ENGINES, YTDLP_POT_CLIENT, "yt-dlp-mweb-pot-inproc-v35"
        )
    if _prefetch_pool is None:
        _prefetch_pool = YtdlpEnginePool(
            "prefetch", YTDLP_PREFETCH_ENGINES, YTDLP_AUTH_CLIENT, "yt-dlp-prefetch-inproc-v35"
        )


async def probe_resolved_media(media: ResolvedMedia) -> None:
    client = get_http_client()
    headers = {
        k: v for k, v in media.http_headers.items()
        if k.lower() not in {"authorization", "cookie", "host", "content-length", "connection", "transfer-encoding"}
    }
    headers["Range"] = "bytes=0-0"
    headers["Accept-Encoding"] = "identity"
    request = client.build_request("GET", media.url, headers=headers)
    response = await client.send(request, stream=True)
    try:
        if response.status_code not in {200, 206}:
            raise RuntimeError(f"resolved media probe returned HTTP {response.status_code}")
    finally:
        await response.aclose()


def _consume_background_task(task: asyncio.Task[Any], label: str, video_id: str) -> None:
    try:
        result = task.result()
        if isinstance(result, ResolvedMedia) and not get_cached_media(video_id):
            _resolved_cache[video_id] = result
            cleanup_resolved_cache()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        print("cold race background path finished with error", json.dumps({
            "videoId": video_id,
            "path": label,
            "error": str(exc)[-1200:],
        }), flush=True)


async def resolve_ytdlp_foreground_v35(video_id: str, purpose: str) -> ResolvedMedia:
    """Foreground-first resolver. Run exactly one expensive mweb/PO job at a time.

    On small Render instances, racing multiple yt-dlp/Deno/BotGuard jobs makes
    each one slower. The known-working mweb+PO path gets the CPU to itself.
    Only if it fails do we try the authenticated/default extractor.
    """
    init_ytdlp_pools()
    started = time.monotonic()
    try:
        winner = await _fg_pot_pool.resolve(video_id, purpose + "-pot")
        print("v35 foreground resolver won", json.dumps({
            "videoId": video_id,
            "client": winner.client,
            "resolverPath": winner.resolver_path,
            "elapsedSeconds": round(time.monotonic() - started, 3),
        }), flush=True)
        return winner
    except asyncio.CancelledError:
        raise
    except Exception as pot_exc:
        print("v35 mweb foreground failed, trying auth fallback", json.dumps({
            "videoId": video_id,
            "elapsedSeconds": round(time.monotonic() - started, 3),
            "error": str(pot_exc)[-1200:],
        }), flush=True)
        try:
            winner = await _fg_auth_pool.resolve(video_id, purpose + "-auth-fallback")
            print("v35 auth fallback won", json.dumps({
                "videoId": video_id,
                "client": winner.client,
                "resolverPath": winner.resolver_path,
                "elapsedSeconds": round(time.monotonic() - started, 3),
            }), flush=True)
            return winner
        except Exception as auth_exc:
            raise RuntimeError(
                "V35 foreground resolvers failed: "
                + str(pot_exc)[-900:] + " || " + str(auth_exc)[-900:]
            ) from auth_exc


async def resolve_live_cold_v35(video_id: str, purpose: str) -> ResolvedMedia:
    """V36 true cold path.

    Start two cheap direct HTTP strategies immediately. Give the purpose-built
    mweb+bgutil resolver a short head start. If it has not produced verified
    media quickly, start the proven in-process yt-dlp mweb fallback without
    cancelling the direct work. First verified media wins.
    """
    init_ytdlp_pools()
    started = time.monotonic()
    generic_direct = asyncio.create_task(resolve_direct_fast(video_id, purpose + "-direct"))
    direct_pot = asyncio.create_task(resolve_direct_mweb_pot(video_id, purpose + "-mweb-direct-pot"))
    errors: list[str] = []

    # Give the real direct POT/cipher path an actual head start. A failure from
    # the cheap direct probe must NOT end this window early.
    head_start = max(0.0, float(os.environ.get("VEEB_V36_DIRECT_HEAD_START", "3.5")))
    deadline = time.monotonic() + head_start
    head_tasks = {generic_direct, direct_pot}
    while head_tasks and time.monotonic() < deadline:
        timeout = max(0.0, deadline - time.monotonic())
        done, pending = await asyncio.wait(head_tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if not done:
            break
        for task in done:
            head_tasks.discard(task)
            try:
                winner = task.result()
                print("v36 cold direct won", json.dumps({
                    "videoId": video_id,
                    "client": winner.client,
                    "resolverPath": winner.resolver_path,
                    "elapsedSeconds": round(time.monotonic() - started, 3),
                }), flush=True)
                for pending_task in (generic_direct, direct_pot):
                    if pending_task is not task and not pending_task.done():
                        pending_task.cancel()
                return winner
            except Exception as exc:
                errors.append(str(exc))
                print("v36.15 direct head-start path failed", json.dumps({
                    "videoId": video_id,
                    "path": "generic-direct" if task is generic_direct else "direct-pot",
                    "error": str(exc)[-1200:],
                    "elapsedSeconds": round(time.monotonic() - started, 3),
                }), flush=True)
        # If the real direct path failed, there is no reason to hold the fallback.
        if direct_pot.done():
            break

    fallback = asyncio.create_task(resolve_ytdlp_foreground_v35(video_id, purpose))
    tasks = {generic_direct, direct_pot, fallback}
    for done in asyncio.as_completed(tasks):
        try:
            winner = await done
            print("v36 cold race won", json.dumps({
                "videoId": video_id,
                "client": winner.client,
                "resolverPath": winner.resolver_path,
                "elapsedSeconds": round(time.monotonic() - started, 3),
            }), flush=True)
            for task in tasks:
                if task.done():
                    continue
                task.cancel()
                task.add_done_callback(
                    lambda finished, label="v36-loser": _consume_background_task(finished, label, video_id)
                )
            return winner
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            errors.append(str(exc))
            print("v36.15 cold race path failed", json.dumps({"videoId": video_id, "error": str(exc)[-1200:], "elapsedSeconds": round(time.monotonic() - started, 3)}), flush=True)
    raise RuntimeError("all V36.3 cold resolver paths failed: " + " || ".join(errors)[-2600:])


async def resolve_prefetch_v35(video_id: str, purpose: str) -> ResolvedMedia:
    """Keep speculation cheap so it cannot steal CPU from a true cold playback.

    By default a prefetch only attempts the sub-second direct Innertube path.
    Set VEEB_HEAVY_PREFETCH=true only if the host has enough CPU for background
    yt-dlp challenge work without hurting foreground latency.
    """
    init_ytdlp_pools()
    fast_started = time.monotonic()
    try:
        return await resolve_direct_fast(video_id, purpose)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print("direct innertube fast path missed", json.dumps({
            "videoId": video_id,
            "purpose": purpose,
            "elapsedSeconds": round(time.monotonic() - fast_started, 3),
            "error": str(exc)[-1500:],
            "heavyPrefetch": HEAVY_PREFETCH,
        }), flush=True)
        if not HEAVY_PREFETCH:
            raise RuntimeError("cheap prefetch fast path unavailable; heavy prefetch disabled") from exc
    return await _prefetch_pool.resolve(video_id, purpose + "-prefetch")


async def resolve_media_uncached(video_id: str, purpose: str) -> ResolvedMedia:
    cached = get_cached_media(video_id)
    if cached:
        return cached
    if purpose.startswith("live"):
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
        print("background resolve failed", json.dumps({"videoId": video_id, "error": str(exc)[-1600:]}), flush=True)


def start_resolve_task(video_id: str, purpose: str) -> asyncio.Task[ResolvedMedia]:
    existing = _resolve_tasks.get(video_id)
    if existing and not existing.done():
        return existing

    async def runner() -> ResolvedMedia:
        # Prefetch HTTP fast paths can run concurrently. The yt-dlp fallback uses a dedicated background engine pool.
        if purpose == "prefetch":
            async with _direct_prefetch_sem:
                return await resolve_media_uncached(video_id, purpose)
        return await resolve_media_uncached(video_id, purpose)

    task = asyncio.create_task(runner())
    _resolve_tasks[video_id] = task
    _resolve_task_purpose[video_id] = purpose
    task.add_done_callback(lambda done: resolve_task_finished(video_id, done))
    return task


async def get_or_resolve(video_id: str, purpose: str) -> tuple[ResolvedMedia, str]:
    cached = get_cached_media(video_id)
    if cached:
        return cached, "HIT"
    task = _resolve_tasks.get(video_id)
    if task and not task.done():
        existing_purpose = _resolve_task_purpose.get(video_id, "")
        if purpose == "live" and existing_purpose == "live-intent":
            global _active_intent_video_id
            _resolve_task_purpose[video_id] = "live"
            if _active_intent_video_id == video_id:
                _active_intent_video_id = None
            print("foreground joining intent resolver", json.dumps({
                "videoId": video_id,
            }), flush=True)
            return await asyncio.shield(task), "WAIT-INTENT"
        if purpose == "live" and existing_purpose == "prefetch":
            print("foreground promoting speculative resolve to single-flight", json.dumps({
                "videoId": video_id
            }), flush=True)
            async def foreground_runner() -> ResolvedMedia:
                return await resolve_media_uncached(video_id, "live")
            foreground = asyncio.create_task(foreground_runner())
            # Replace the speculative registry entry immediately. Any duplicate
            # browser GET/Range request for this same track now joins this exact
            # foreground task instead of starting a second expensive resolve.
            _resolve_tasks[video_id] = foreground
            _resolve_task_purpose[video_id] = "live"
            foreground.add_done_callback(lambda done: resolve_task_finished(video_id, done))
            return await asyncio.shield(foreground), "MISS-FOREGROUND"
        return await asyncio.shield(task), "WAIT"
    task = start_resolve_task(video_id, purpose)
    return await asyncio.shield(task), "MISS"


def build_upstream_headers(media: ResolvedMedia, request: Request) -> dict[str, str]:
    blocked = {"authorization", "cookie", "host", "content-length", "connection", "transfer-encoding"}
    headers = {k: v for k, v in media.http_headers.items() if k.lower() not in blocked}
    requested_range = request.headers.get("range")
    if requested_range:
        headers["Range"] = requested_range
    headers["Accept-Encoding"] = "identity"
    return headers


PASSTHROUGH_RESPONSE_HEADERS = {
    "accept-ranges", "content-length", "content-range", "content-type", "etag", "last-modified",
}


def build_downstream_headers(upstream: httpx.Response, media: ResolvedMedia, cache_state: str) -> dict[str, str]:
    headers = {k: v for k, v in upstream.headers.items() if k.lower() in PASSTHROUGH_RESPONSE_HEADERS}
    headers.setdefault("Content-Type", media.content_type or "video/mp4")
    headers.setdefault("Accept-Ranges", "bytes")
    headers["Cache-Control"] = "private, no-store"
    headers["X-Veeb-Resolver"] = "innertube-resilient-v36.3"
    headers["X-Veeb-Resolved-Cache"] = cache_state
    headers["X-Veeb-Playback-Client"] = media.client
    headers["X-Veeb-Source-Format"] = media.format_id or SOURCE_FORMAT
    headers["X-Veeb-Resolver-Path"] = media.resolver_path
    headers["X-Veeb-Direct-Proxy"] = "1"
    return headers


async def upstream_body(response: httpx.Response) -> AsyncIterator[bytes]:
    try:
        async for chunk in response.aiter_raw(PROXY_CHUNK_BYTES):
            if chunk:
                yield chunk
    finally:
        await response.aclose()


async def open_media_upstream(media: ResolvedMedia, request: Request) -> httpx.Response:
    client = get_http_client()
    upstream_request = client.build_request(request.method, media.url, headers=build_upstream_headers(media, request))
    return await client.send(upstream_request, stream=True)


async def proxy_media(request: Request, video_id: str):
    media, cache_state = await get_or_resolve(video_id, "live")
    response = await open_media_upstream(media, request)

    # A direct Innertube URL can still be rejected at GVS even though the player
    # endpoint returned it. If so, invalidate and go straight to the mature
    # fallback instead of retrying the same experimental client ladder.
    if response.status_code in {403, 410}:
        rejected_path = media.resolver_path
        await response.aclose()
        invalidate_media(video_id)
        print("media url rejected", json.dumps({
            "videoId": video_id,
            "status": response.status_code,
            "client": media.client,
            "resolverPath": rejected_path,
        }), flush=True)
        if rejected_path in {
            "innertube-direct",
            "mweb-bgutil-direct-v36",
            "mweb-adaptive-gvs-direct-v36.2",
            "mweb-resilient-direct-v36.3",
            "mweb-innertube-youtubejs-v36.15",
        }:
            media = await resolve_ytdlp_foreground_v35(video_id, "live-gvs-fallback")
            _resolved_cache[video_id] = media
        else:
            media = await resolve_media_uncached(video_id, "live-refresh")
        cache_state = "REFRESH"
        response = await open_media_upstream(media, request)

    if response.status_code >= 400:
        status = response.status_code
        body = await response.aread()
        await response.aclose()
        detail = body[:800].decode("utf-8", "replace") if body else ""
        raise HTTPException(status_code=502, detail=f"Upstream media server returned HTTP {status}" + (f": {detail}" if detail else ""))

    headers = build_downstream_headers(response, media, cache_state)
    print("direct media proxy open", json.dumps({
        "videoId": video_id,
        "client": media.client,
        "formatId": media.format_id,
        "resolverPath": media.resolver_path,
        "resolvedCache": cache_state,
        "range": request.headers.get("range"),
        "upstreamStatus": response.status_code,
    }), flush=True)
    if request.method == "HEAD":
        await response.aclose()
        return Response(status_code=response.status_code, headers=headers)
    return StreamingResponse(upstream_body(response), status_code=response.status_code, headers=headers, media_type=headers.get("Content-Type"))


@app.on_event("startup")
async def startup_session() -> None:
    started = time.monotonic()
    load_youtube_cookie_session(force=True)
    get_http_client()
    # Keep the proven yt-dlp fallback warm, but do not construct the broken
    # standalone EJS decipher engine. The hot path uses a persistent YouTube.js
    # Player helper that is already warm before Uvicorn starts.
    init_ytdlp_pools()
    global _session_gvs_task, _youtubejs_ready, _youtubejs_player_id
    _session_gvs_task = None
    helper_result, _warm_result = await asyncio.gather(
        ensure_youtubejs_helper_ready(),
        warm_bgutil_integrity(),
        return_exceptions=True,
    )
    if isinstance(helper_result, Exception):
        print("v36.15 YouTube.js helper warm failed", json.dumps({
            "error": str(helper_result)[-1200:],
        }), flush=True)
        helper_state: dict[str, Any] = {}
    else:
        helper_state = helper_result
    _youtubejs_ready = bool(helper_state.get("ready"))
    _youtubejs_player_id = helper_state.get("playerId") if isinstance(helper_state.get("playerId"), str) else None
    print("v36.15 resolver stack warm", json.dumps({
        "elapsedSeconds": round(time.monotonic() - started, 3),
        "foregroundAuthEngines": YTDLP_FG_AUTH_ENGINES,
        "foregroundPotEngines": YTDLP_FG_POT_ENGINES,
        "prefetchEngines": YTDLP_PREFETCH_ENGINES,
        "potHttpReady": pot_http_server_ready(),
        "youtubejsReady": bool(helper_state.get("ready")),
        "youtubejsPlayerId": helper_state.get("playerId"),
    }), flush=True)


@app.on_event("shutdown")
async def shutdown_http_client() -> None:
    global _http_client
    for pool in (_fg_auth_pool, _fg_pot_pool, _prefetch_pool):
        if pool is not None:
            pool.close()
    if _direct_cipher_solver is not None:
        _direct_cipher_solver.close()
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


@app.head("/")
async def root_head() -> Response:
    return Response(status_code=200)


@app.get("/")
async def root() -> dict[str, Any]:
    return {"ok": True, "service": "veeb-resolver", "version": "v36.15-youtubejs"}


@app.get("/health")
async def health(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_auth(authorization)
    cleanup_resolved_cache()
    try:
        ytdlp_version = importlib.metadata.version("yt-dlp")
    except importlib.metadata.PackageNotFoundError:
        ytdlp_version = "unknown"
    return {
        "ok": True,
        "service": "veeb-resolver",
        "version": "v36.15-youtubejs",
        "ytDlpVersion": ytdlp_version,
        "sourceFormat": SOURCE_FORMAT,
        "directClients": DIRECT_CLIENT_ORDER,
        "authFallbackClient": YTDLP_AUTH_CLIENT,
        "potFallbackClient": YTDLP_POT_CLIENT,
        "authenticatedCookies": _youtube_cookie_authenticated,
        "potHttpReady": pot_http_server_ready(),
        "youtubejsReady": _youtubejs_ready,
        "youtubejsPlayerId": _youtubejs_player_id,
        "resolvedUrlCacheEntries": len(_resolved_cache),
        "activeResolves": len([t for t in _resolve_tasks.values() if not t.done()]),
        "foregroundAuthSlotsFree": _fg_auth_pool.queue.qsize() if _fg_auth_pool else 0,
        "foregroundPotSlotsFree": _fg_pot_pool.queue.qsize() if _fg_pot_pool else 0,
        "prefetchSlotsFree": _prefetch_pool.queue.qsize() if _prefetch_pool else 0,
        "inProcessYtDlp": True,
        "directClientCooldowns": {
            name: max(0, int(until - time.time()))
            for name, until in _direct_client_cooldown_until.items()
            if until > time.time()
        },
        "heavyPrefetch": HEAVY_PREFETCH,
        "architecture": "direct-mweb-innertube-plus-persistent-youtubejs-decipher-plus-warm-bgutil-with-ytdlp-fallback",
    }


@app.get("/resolve/{video_id}")
async def resolve_endpoint(video_id: str, authorization: str | None = Header(default=None)) -> JSONResponse:
    require_auth(authorization)
    video_id = validate_video_id(video_id)
    media, cache_state = await get_or_resolve(video_id, "metadata")
    return JSONResponse({
        "provider": "veeb-v36.15-youtubejs-resolver",
        "videoId": video_id,
        "title": media.title,
        "duration": media.duration,
        "formatId": media.format_id,
        "client": media.client,
        "resolverPath": media.resolver_path,
        "cache": cache_state,
        "expiresInSeconds": max(0, int(media.expires_at - time.time())),
        "proxied": True,
    })


@app.post("/prefetch/{video_id}")
async def prefetch_endpoint(
    video_id: str,
    intent: int = Query(default=0),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    require_auth(authorization)
    video_id = validate_video_id(video_id)
    if get_cached_media(video_id):
        return JSONResponse({"ok": True, "status": "cached", "videoId": video_id})
    global _active_intent_video_id
    if intent and _active_intent_video_id and _active_intent_video_id != video_id:
        old_id = _active_intent_video_id
        old_task = _resolve_tasks.get(old_id)
        if old_task and not old_task.done() and _resolve_task_purpose.get(old_id) == "live-intent":
            _resolve_tasks.pop(old_id, None)
            _resolve_task_purpose.pop(old_id, None)
            old_task.cancel()
            print("intent prefetch replacing previous intent", json.dumps({
                "fromVideoId": old_id,
                "toVideoId": video_id,
            }), flush=True)

    existing = _resolve_tasks.get(video_id)
    if existing and not existing.done():
        existing_purpose = _resolve_task_purpose.get(video_id, "")
        if intent and existing_purpose == "prefetch":
            if _resolve_tasks.get(video_id) is existing:
                _resolve_tasks.pop(video_id, None)
                _resolve_task_purpose.pop(video_id, None)
            existing.cancel()
            print("intent prefetch replacing speculative resolve", json.dumps({
                "videoId": video_id,
            }), flush=True)
            start_resolve_task(video_id, "live-intent")
            _active_intent_video_id = video_id
        return JSONResponse({"ok": True, "status": "warming", "videoId": video_id, "intent": bool(intent)}, status_code=202)
    start_resolve_task(video_id, "live-intent" if intent else "prefetch")
    if intent:
        _active_intent_video_id = video_id
    return JSONResponse({"ok": True, "status": "warming", "videoId": video_id, "intent": bool(intent)}, status_code=202)


@app.post("/prefetch-batch")
async def prefetch_batch_endpoint(
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    require_auth(authorization)
    body = await request.json()
    raw_ids = body.get("videoIds") if isinstance(body, dict) else None
    if not isinstance(raw_ids, list):
        raise HTTPException(status_code=400, detail="videoIds must be an array")
    video_ids: list[str] = []
    for raw in raw_ids[:8]:
        value = str(raw)
        if VIDEO_ID_RE.fullmatch(value) and value not in video_ids:
            video_ids.append(value)
    statuses = []
    for video_id in video_ids:
        if get_cached_media(video_id):
            statuses.append({"videoId": video_id, "status": "cached"})
            continue
        task = _resolve_tasks.get(video_id)
        if not task or task.done():
            start_resolve_task(video_id, "prefetch")
        statuses.append({"videoId": video_id, "status": "warming"})
    return JSONResponse({"ok": True, "tracks": statuses}, status_code=202)


@app.api_route("/stream/{video_id}", methods=["GET", "HEAD"])
async def stream_endpoint(
    request: Request,
    video_id: str,
    authorization: str | None = Header(default=None),
):
    require_auth(authorization)
    video_id = validate_video_id(video_id)
    return await proxy_media(request, video_id)
