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
from dataclasses import dataclass
from typing import Any, AsyncIterator
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx
import yt_dlp
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

app = FastAPI(title="Veeb YouTube Resolver V36.1", docs_url=None, redoc_url=None)

RESOLVER_SECRET = os.environ.get("RESOLVER_SECRET", "")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_COOKIE_FILE = os.environ.get("YOUTUBE_COOKIE_FILE", "/etc/secrets/youtube-cookies.txt")
WRITABLE_COOKIE_FILE = os.environ.get("WRITABLE_COOKIE_FILE", "/tmp/veeb-youtube-cookies.txt")
YTDLP_CACHE_DIR = os.environ.get("YTDLP_CACHE_DIR", "/tmp/veeb-yt-dlp-cache")
JSC_RUNTIME = os.environ.get("YOUTUBE_JSC_RUNTIME", "deno").strip() or "deno"
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
DIRECT_MWEB_POT_TIMEOUT_SECONDS = max(1.0, float(os.environ.get("VEEB_DIRECT_MWEB_POT_TIMEOUT", "8.0")))
DIRECT_MWEB_PLAYER_TIMEOUT_SECONDS = max(1.0, float(os.environ.get("VEEB_DIRECT_MWEB_PLAYER_TIMEOUT", "4.0")))
YTDLP_FG_AUTH_ENGINES = max(1, int(os.environ.get("VEEB_YTDLP_FG_AUTH_ENGINES", "1")))
YTDLP_FG_POT_ENGINES = max(1, int(os.environ.get("VEEB_YTDLP_FG_POT_ENGINES", "1")))
YTDLP_PREFETCH_ENGINES = max(1, int(os.environ.get("VEEB_YTDLP_PREFETCH_ENGINES", "1")))
YTDLP_SOCKET_TIMEOUT_SECONDS = max(5, int(os.environ.get("VEEB_YTDLP_SOCKET_TIMEOUT", "15")))
YTDLP_EXTRACTOR_RETRIES = max(0, int(os.environ.get("VEEB_YTDLP_EXTRACTOR_RETRIES", "0")))
YTDLP_SKIP_WEBPAGE_WITH_VISITOR = os.environ.get("VEEB_YTDLP_SKIP_WEBPAGE_WITH_VISITOR", "false").strip().lower() in {"1", "true", "yes", "on"}
HEAVY_PREFETCH = os.environ.get("VEEB_HEAVY_PREFETCH", "false").strip().lower() in {"1", "true", "yes", "on"}

# These contexts are copied from current yt-dlp client definitions, but the hot
# path only asks Innertube for the player response. We accept only already-signed
# direct URLs. Anything requiring JS decipher, DRM, SABR, or POT falls through to
# the mature yt-dlp fallback.
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


def make_sid_authorization(scheme: str, sid: str, origin: str) -> str:
    timestamp = str(round(time.time()))
    digest = hashlib.sha1(f"{timestamp} {sid} {origin}".encode()).hexdigest()
    return f"{scheme} {timestamp}_{digest}"


def youtube_cookie_auth_header(origin: str = "https://www.youtube.com") -> str | None:
    load_youtube_cookie_session()
    values = _youtube_cookie_values
    sapisid = values.get("SAPISID") or values.get("__Secure-3PAPISID")
    candidates = (
        ("SAPISIDHASH", sapisid),
        ("SAPISID1PHASH", values.get("__Secure-1PAPISID")),
        ("SAPISID3PHASH", values.get("__Secure-3PAPISID")),
    )
    parts = [make_sid_authorization(scheme, sid, origin) for scheme, sid in candidates if sid]
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
    streaming = data.get("streamingData") or {}
    formats = list(streaming.get("formats") or []) + list(streaming.get("adaptiveFormats") or [])
    usable = [
        fmt for fmt in formats
        if isinstance(fmt, dict)
        and isinstance(fmt.get("url"), str)
        and fmt.get("url", "").startswith("http")
        and not format_has_drm(fmt)
    ]
    for fmt in usable:
        if str(fmt.get("itag")) == SOURCE_FORMAT:
            return fmt
    # Fallback to a directly signed MP4 that contains audio. Combined formats
    # are preferred because every browser that handled itag 18 can play them.
    combined_mp4 = [
        fmt for fmt in usable
        if str(fmt.get("mimeType", "")).startswith("video/mp4")
        and "audioQuality" in fmt
    ]
    if combined_mp4:
        return sorted(combined_mp4, key=lambda item: int(item.get("bitrate") or 0))[0]
    audio_mp4 = [
        fmt for fmt in usable
        if str(fmt.get("mimeType", "")).startswith("audio/mp4")
    ]
    if audio_mp4:
        return sorted(audio_mp4, key=lambda item: int(item.get("bitrate") or 0), reverse=True)[0]
    return None


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


def append_query_param(url: str, key: str, value: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query[key] = [value]
    encoded = urlencode([(k, item) for k, values in query.items() for item in values])
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, encoded, parsed.fragment))


def mweb_context() -> dict[str, Any]:
    cfg = dict(MWEB_DIRECT_CONFIG)
    cfg.setdefault("timeZone", "UTC")
    cfg.setdefault("utcOffsetMinutes", 0)
    return {
        "client": cfg,
        "request": {"useSsl": True, "internalExperimentFlags": []},
        "user": {"lockedSafetyMode": False},
    }


def mweb_headers() -> dict[str, str]:
    origin = "https://www.youtube.com"
    headers = {
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Content-Type": "application/json",
        "Origin": origin,
        "User-Agent": str(MWEB_DIRECT_CONFIG["userAgent"]),
        "X-Youtube-Client-Name": "2",
        "X-Youtube-Client-Version": str(MWEB_DIRECT_CONFIG["clientVersion"]),
    }
    if _visitor_data:
        headers["X-Goog-Visitor-Id"] = _visitor_data
    if load_youtube_cookie_session():
        if _youtube_cookie_header:
            headers["Cookie"] = _youtube_cookie_header
        authorization = youtube_cookie_auth_header(origin)
        if authorization:
            headers["Authorization"] = authorization
            headers["X-Origin"] = origin
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
    expires_at = float(payload.get("expiresAt") or (time.time() + 300))
    print("v36.1 POT ready", json.dumps({
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


async def resolve_direct_mweb_pot(video_id: str, purpose: str) -> ResolvedMedia:
    """Adaptive mweb resolver for current WebPO binding experiments.

    Current yt-dlp can observe GVS proofs bound either to authenticated session
    Data Sync state or, under rollout experiments, directly to the video ID.
    Do not hard-code one interpretation. Generate both proof candidates, race
    small authenticated /player requests, then probe the signed media URL with
    both GVS proof candidates. First verified media wins.
    """
    global _visitor_data
    started = time.monotonic()
    context = mweb_context()

    session_task = asyncio.create_task(get_session_gvs_pot())
    video_task = asyncio.create_task(get_bgutil_pot(video_id, context, "gvs-video-candidate"))
    try:
        (session_pot, _data_sync_binding), (video_pot, video_binding, _video_expiry) = await asyncio.gather(
            session_task, video_task
        )
    except Exception:
        for task in (session_task, video_task):
            if not task.done():
                task.cancel()
        raise
    if video_binding and video_binding != video_id:
        raise RuntimeError("bgutil returned an unexpected video-bound content binding")

    client = get_http_client()

    async def call_player(label: str, include_video_proof: bool) -> tuple[str, dict[str, Any], dict[str, Any]]:
        payload: dict[str, Any] = {
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
        # mweb currently requires GVS proof. A PLAYER proof is not universally
        # required, but during YouTube experiments a video-bound proof can be
        # relevant to the player transaction. Race both forms instead of
        # forcing one global interpretation.
        if include_video_proof:
            payload["serviceIntegrityDimensions"] = {"poToken": video_pot}
        player_started = time.monotonic()
        response = await client.post(
            INNERTUBE_PLAYER_URL,
            params={"key": INNERTUBE_API_KEY, "prettyPrint": "false"},
            headers=mweb_headers(),
            json=payload,
            timeout=DIRECT_MWEB_PLAYER_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            raise RuntimeError(f"{label} mweb /player returned HTTP {response.status_code}")
        data = response.json()
        visitor = ((data.get("responseContext") or {}).get("visitorData"))
        if isinstance(visitor, str) and visitor:
            _visitor_data = visitor
        fmt = select_direct_format(data)
        if not fmt:
            raise RuntimeError(f"{label} mweb /player returned no usable signed format: " + (playability_error(data) or "unknown"))
        print("v36.2 mweb player candidate ready", json.dumps({
            "videoId": video_id,
            "purpose": purpose,
            "candidate": label,
            "formatId": str(fmt.get("itag")) if fmt.get("itag") is not None else None,
            "elapsedSeconds": round(time.monotonic() - player_started, 3),
        }), flush=True)
        return label, data, fmt

    player_tasks = {
        asyncio.create_task(call_player("plain", False)),
        asyncio.create_task(call_player("video-proof", True)),
    }
    player_errors: list[str] = []
    player_result: tuple[str, dict[str, Any], dict[str, Any]] | None = None
    try:
        for done in asyncio.as_completed(player_tasks):
            try:
                player_result = await done
                break
            except Exception as exc:
                player_errors.append(str(exc))
    finally:
        if player_result is not None:
            for task in player_tasks:
                if not task.done():
                    task.cancel()
    if player_result is None:
        raise RuntimeError("adaptive mweb /player failed: " + " || ".join(player_errors)[-1800:])

    player_label, data, fmt = player_result
    raw_url = str(fmt["url"])
    user_agent = str(MWEB_DIRECT_CONFIG["userAgent"])

    async def probe_candidate(label: str, token: str) -> tuple[str, str]:
        candidate_url = append_query_param(raw_url, "pot", token)
        probe_started = time.monotonic()
        await probe_direct_media(candidate_url, user_agent)
        print("v36.2 GVS proof candidate verified", json.dumps({
            "videoId": video_id,
            "purpose": purpose,
            "binding": label,
            "probeSeconds": round(time.monotonic() - probe_started, 3),
        }), flush=True)
        return label, candidate_url

    probe_tasks = {
        asyncio.create_task(probe_candidate("video", video_pot)),
        asyncio.create_task(probe_candidate("session", session_pot)),
    }
    probe_errors: list[str] = []
    media_url: str | None = None
    winning_binding = ""
    try:
        for done in asyncio.as_completed(probe_tasks):
            try:
                winning_binding, media_url = await done
                break
            except Exception as exc:
                probe_errors.append(str(exc))
    finally:
        if media_url:
            for task in probe_tasks:
                if not task.done():
                    task.cancel()
    if not media_url:
        raise RuntimeError("adaptive GVS proof probes failed: " + " || ".join(probe_errors)[-1800:])

    mime = str(fmt.get("mimeType") or "")
    mime_base = mime.split(";", 1)[0].strip() or None
    details = data.get("videoDetails") or {}
    media = ResolvedMedia(
        video_id=video_id,
        url=media_url,
        http_headers={"User-Agent": user_agent},
        client="mweb-direct-pot",
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
        resolver_path="mweb-adaptive-gvs-direct-v36.2",
    )
    print("v36.2 direct mweb resolve success", json.dumps({
        "videoId": video_id,
        "purpose": purpose,
        "playerCandidate": player_label,
        "gvsBinding": winning_binding,
        "formatId": media.format_id,
        "elapsedSeconds": round(time.monotonic() - started, 3),
    }), flush=True)
    return media


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

    # Let pure HTTP resolution win without immediately competing for CPU with
    # yt-dlp/Deno. This head start is intentionally tiny compared with V35's 12s.
    head_start = max(0.0, float(os.environ.get("VEEB_V36_DIRECT_HEAD_START", "1.25")))
    if head_start:
        done, _ = await asyncio.wait({generic_direct, direct_pot}, timeout=head_start, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            try:
                winner = task.result()
                print("v36 cold direct won", json.dumps({
                    "videoId": video_id,
                    "client": winner.client,
                    "resolverPath": winner.resolver_path,
                    "elapsedSeconds": round(time.monotonic() - started, 3),
                }), flush=True)
                for pending in (generic_direct, direct_pot):
                    if pending is not task and not pending.done():
                        pending.cancel()
                return winner
            except Exception as exc:
                errors.append(str(exc))

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
                task.add_done_callback(
                    lambda finished, label="v36-loser": _consume_background_task(finished, label, video_id)
                )
            return winner
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError("all V36 cold resolver paths failed: " + " || ".join(errors)[-2600:])


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
    headers["X-Veeb-Resolver"] = "adaptive-pot-v36.2"
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
        if rejected_path in {"innertube-direct", "mweb-bgutil-direct-v36", "mweb-adaptive-gvs-direct-v36.2"}:
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
    # Pay Python/plugin/extractor construction at process startup, before the
    # first user tap. Docker already warms Deno and the bgutil POT service.
    init_ytdlp_pools()
    global _session_gvs_task
    _session_gvs_task = asyncio.create_task(warm_session_gvs_pot())
    print("v36.1 resolver stack warm", json.dumps({
        "elapsedSeconds": round(time.monotonic() - started, 3),
        "foregroundAuthEngines": YTDLP_FG_AUTH_ENGINES,
        "foregroundPotEngines": YTDLP_FG_POT_ENGINES,
        "prefetchEngines": YTDLP_PREFETCH_ENGINES,
        "potHttpReady": pot_http_server_ready(),
    }), flush=True)


@app.on_event("shutdown")
async def shutdown_http_client() -> None:
    global _http_client
    for pool in (_fg_auth_pool, _fg_pot_pool, _prefetch_pool):
        if pool is not None:
            pool.close()
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


@app.get("/")
async def root() -> dict[str, Any]:
    return {"ok": True, "service": "veeb-resolver", "version": "v36.2-adaptive-pot"}


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
        "version": "v36.2-adaptive-pot",
        "ytDlpVersion": ytdlp_version,
        "sourceFormat": SOURCE_FORMAT,
        "directClients": DIRECT_CLIENT_ORDER,
        "authFallbackClient": YTDLP_AUTH_CLIENT,
        "potFallbackClient": YTDLP_POT_CLIENT,
        "authenticatedCookies": _youtube_cookie_authenticated,
        "potHttpReady": pot_http_server_ready(),
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
        "architecture": "v36.2-adaptive-gvs-binding-plus-single-flight-with-ytdlp-safety-fallback",
    }


@app.get("/resolve/{video_id}")
async def resolve_endpoint(video_id: str, authorization: str | None = Header(default=None)) -> JSONResponse:
    require_auth(authorization)
    video_id = validate_video_id(video_id)
    media, cache_state = await get_or_resolve(video_id, "metadata")
    return JSONResponse({
        "provider": "veeb-v36.2-adaptive-pot-resolver",
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
    existing = _resolve_tasks.get(video_id)
    if existing and not existing.done():
        return JSONResponse({"ok": True, "status": "warming", "videoId": video_id}, status_code=202)
    start_resolve_task(video_id, "prefetch")
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
