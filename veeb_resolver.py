import asyncio
import importlib.metadata
import json
import os
import re
import shutil
import socket
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

app = FastAPI(title="Veeb YouTube Resolver V30", docs_url=None, redoc_url=None)

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

# These contexts are copied from current yt-dlp client definitions, but the hot
# path only asks Innertube for the player response. We accept only already-signed
# direct URLs. Anything requiring JS decipher, DRM, SABR, or POT falls through to
# the mature yt-dlp fallback.
DIRECT_CLIENTS: dict[str, dict[str, Any]] = {
    "android_vr": {
        "clientName": "ANDROID_VR",
        "clientVersion": "1.65.10",
        "deviceMake": "Oculus",
        "deviceModel": "Quest 3",
        "androidSdkVersion": 32,
        "userAgent": "com.google.android.apps.youtube.vr.oculus/1.65.10 (Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip",
        "osName": "Android",
        "osVersion": "12L",
        "hl": "en",
    },
    "web_embedded": {
        "clientName": "WEB_EMBEDDED_PLAYER",
        "clientVersion": "2.20260708.00.00",
        "hl": "en",
    },
    "tv": {
        "clientName": "TVHTML5",
        "clientVersion": "7.20260707.07.00",
        "userAgent": "Mozilla/5.0 (ChromiumStylePlatform) Cobalt/25.lts.30.1034943-gold (unlike Gecko), Unknown_TV_Unknown_0/Unknown (Unknown, Unknown)",
        "hl": "en",
    },
}
DIRECT_CLIENT_ORDER = [
    name.strip()
    for name in os.environ.get("VEEB_DIRECT_CLIENTS", "android_vr,web_embedded,tv").split(",")
    if name.strip() in DIRECT_CLIENTS
]
if not DIRECT_CLIENT_ORDER:
    DIRECT_CLIENT_ORDER = ["android_vr", "web_embedded", "tv"]

YTDLP_FALLBACK_CLIENT = os.environ.get("YOUTUBE_FALLBACK_CLIENT", "mweb").strip() or "mweb"
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
_ytdlp_lock = asyncio.Lock()
_direct_prefetch_sem = asyncio.Semaphore(DIRECT_PREFETCH_CONCURRENCY)
_direct_client_cooldown_until: dict[str, float] = {}
_visitor_data: str | None = None


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
    headers = {
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Content-Type": "application/json",
        "Origin": "https://www.youtube.com",
        "X-Youtube-Client-Name": str({"WEB_EMBEDDED_PLAYER": 56, "ANDROID_VR": 28, "TVHTML5": 7}.get(cfg["clientName"], 1)),
        "X-Youtube-Client-Version": str(cfg["clientVersion"]),
    }
    if cfg.get("userAgent"):
        headers["User-Agent"] = str(cfg["userAgent"])
    if _visitor_data:
        headers["X-Goog-Visitor-Id"] = _visitor_data
    return headers


def innertube_payload(video_id: str, client: str) -> dict[str, Any]:
    cfg = dict(DIRECT_CLIENTS[client])
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


def youtube_extractor_args(client: str) -> str:
    args = [f"player_client={client}"]
    if client in {"mweb", "web_music"}:
        args.append("fetch_pot=auto")
        if not YOUTUBE_PREMIUM_ACCOUNT:
            args.append("use_ad_playback_context=true")
    args.append("player_skip=configs")
    args.append("skip=hls,dash")
    args.append(f"playback_wait={PLAYBACK_WAIT_SECONDS:g}")
    return "youtube:" + ";".join(args)


def build_ytdlp_command(video_id: str) -> list[str]:
    command = [
        "yt-dlp", "--dump-single-json", "--no-download", "--no-playlist",
        "--no-warnings", "--no-progress", "--no-check-formats",
        "--socket-timeout", "20", "--retries", "1", "--cache-dir", YTDLP_CACHE_DIR,
        "--js-runtimes", JSC_RUNTIME,
        "--extractor-args", youtube_extractor_args(YTDLP_FALLBACK_CLIENT),
    ]
    cookie_file = get_writable_cookie_file()
    if cookie_file:
        command.extend(["--cookies", cookie_file])
    command.extend(["-f", SOURCE_FORMAT, f"https://www.youtube.com/watch?v={video_id}"])
    return command


async def resolve_ytdlp_fallback(video_id: str, purpose: str) -> ResolvedMedia:
    started = time.monotonic()
    async with _ytdlp_lock:
        cached = get_cached_media(video_id)
        if cached:
            return cached
        print("yt-dlp fallback resolve attempt", json.dumps({
            "videoId": video_id,
            "purpose": purpose,
            "client": YTDLP_FALLBACK_CLIENT,
            "potHttpReady": pot_http_server_ready(),
        }), flush=True)
        process = await asyncio.create_subprocess_exec(
            *build_ytdlp_command(video_id),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=RESOLVE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise RuntimeError("yt-dlp fallback timed out")
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        stderr_text = stderr.decode("utf-8", "replace")
        if process.returncode != 0:
            tail = [line for line in stderr_text.splitlines() if line.strip()][-12:]
            raise RuntimeError("yt-dlp fallback failed: " + " | ".join(tail)[-1800:])
        try:
            info = json.loads(stdout.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"yt-dlp fallback returned invalid JSON: {exc}") from exc
        media_url = str(info.get("url") or "").strip()
        if not media_url.startswith(("https://", "http://")):
            raise RuntimeError("yt-dlp fallback did not return a direct URL")
        raw_headers = info.get("http_headers") or {}
        media = ResolvedMedia(
            video_id=video_id,
            url=media_url,
            http_headers={str(k): str(v) for k, v in raw_headers.items() if v is not None},
            client=YTDLP_FALLBACK_CLIENT,
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
            resolver_path="yt-dlp-fallback",
        )
        print("yt-dlp fallback resolve success", json.dumps({
            "videoId": video_id,
            "purpose": purpose,
            "formatId": media.format_id,
            "elapsedSeconds": round(time.monotonic() - started, 2),
        }), flush=True)
        return media


async def resolve_media_uncached(video_id: str, purpose: str) -> ResolvedMedia:
    cached = get_cached_media(video_id)
    if cached:
        return cached
    fast_started = time.monotonic()
    try:
        media = await resolve_direct_fast(video_id, purpose)
        _resolved_cache[video_id] = media
        cleanup_resolved_cache()
        return media
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print("direct innertube fast path missed", json.dumps({
            "videoId": video_id,
            "purpose": purpose,
            "elapsedSeconds": round(time.monotonic() - fast_started, 3),
            "error": str(exc)[-1500:],
        }), flush=True)

    media = await resolve_ytdlp_fallback(video_id, purpose)
    _resolved_cache[video_id] = media
    cleanup_resolved_cache()
    return media


def resolve_task_finished(video_id: str, task: asyncio.Task[ResolvedMedia]) -> None:
    if _resolve_tasks.get(video_id) is task:
        _resolve_tasks.pop(video_id, None)
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
        # Prefetch HTTP fast paths can run concurrently. The yt-dlp fallback is
        # independently serialized by _ytdlp_lock.
        if purpose == "prefetch":
            async with _direct_prefetch_sem:
                return await resolve_media_uncached(video_id, purpose)
        return await resolve_media_uncached(video_id, purpose)

    task = asyncio.create_task(runner())
    _resolve_tasks[video_id] = task
    task.add_done_callback(lambda done: resolve_task_finished(video_id, done))
    return task


async def get_or_resolve(video_id: str, purpose: str) -> tuple[ResolvedMedia, str]:
    cached = get_cached_media(video_id)
    if cached:
        return cached, "HIT"
    task = _resolve_tasks.get(video_id)
    if task and not task.done():
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
    headers["X-Veeb-Resolver"] = "hybrid-innertube-v30"
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
        if rejected_path == "innertube-direct":
            media = await resolve_ytdlp_fallback(video_id, "live-gvs-fallback")
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


@app.on_event("shutdown")
async def shutdown_http_client() -> None:
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


@app.get("/")
async def root() -> dict[str, Any]:
    return {"ok": True, "service": "veeb-resolver", "version": "v30-hybrid-innertube"}


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
        "version": "v30-hybrid-innertube",
        "ytDlpVersion": ytdlp_version,
        "sourceFormat": SOURCE_FORMAT,
        "directClients": DIRECT_CLIENT_ORDER,
        "fallbackClient": YTDLP_FALLBACK_CLIENT,
        "potHttpReady": pot_http_server_ready(),
        "resolvedUrlCacheEntries": len(_resolved_cache),
        "activeResolves": len([t for t in _resolve_tasks.values() if not t.done()]),
        "directClientCooldowns": {
            name: max(0, int(until - time.time()))
            for name, until in _direct_client_cooldown_until.items()
            if until > time.time()
        },
        "architecture": "direct-innertube-race-with-ytdlp-fallback",
    }


@app.get("/resolve/{video_id}")
async def resolve_endpoint(video_id: str, authorization: str | None = Header(default=None)) -> JSONResponse:
    require_auth(authorization)
    video_id = validate_video_id(video_id)
    media, cache_state = await get_or_resolve(video_id, "metadata")
    return JSONResponse({
        "provider": "veeb-hybrid-resolver",
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
