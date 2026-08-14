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

app = FastAPI(title="Veeb YouTube Resolver V28", docs_url=None, redoc_url=None)

RESOLVER_SECRET = os.environ.get("RESOLVER_SECRET", "")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_COOKIE_FILE = os.environ.get("YOUTUBE_COOKIE_FILE", "/etc/secrets/youtube-cookies.txt")
WRITABLE_COOKIE_FILE = os.environ.get("WRITABLE_COOKIE_FILE", "/tmp/veeb-youtube-cookies.txt")
YTDLP_CACHE_DIR = os.environ.get("YTDLP_CACHE_DIR", "/tmp/veeb-yt-dlp-cache")
JSC_RUNTIME = os.environ.get("YOUTUBE_JSC_RUNTIME", "deno").strip() or "deno"
SOURCE_FORMAT = os.environ.get("YOUTUBE_STREAM_FORMAT", "18").strip() or "18"
PLAYBACK_WAIT_SECONDS = max(0.0, float(os.environ.get("YOUTUBE_PLAYBACK_WAIT", "0")))
YOUTUBE_PREMIUM_ACCOUNT = os.environ.get("YOUTUBE_PREMIUM_ACCOUNT", "false").strip().lower() in {
    "1", "true", "yes", "on"
}

# Try the known-good authenticated path first. If a resolved Google Video URL is
# rejected with 403/410, the foreground request can rotate to another client.
# Keep this configurable because YouTube client enforcement changes over time.
CLIENTS = [
    value.strip()
    for value in os.environ.get(
        "YOUTUBE_CLIENTS",
        os.environ.get("YOUTUBE_CLIENT", "mweb") + ",android_vr,web_embedded",
    ).split(",")
    if value.strip()
]
if not CLIENTS:
    CLIENTS = ["mweb"]

RESOLVED_URL_FALLBACK_TTL_SECONDS = max(
    60,
    int(os.environ.get("VEEB_RESOLVED_URL_TTL", "1800")),
)
RESOLVED_URL_EXPIRY_MARGIN_SECONDS = max(
    30,
    int(os.environ.get("VEEB_RESOLVED_URL_EXPIRY_MARGIN", "120")),
)
RESOLVED_URL_MAX_ENTRIES = max(
    16,
    int(os.environ.get("VEEB_RESOLVED_URL_MAX_ENTRIES", "256")),
)
RESOLVE_TIMEOUT_SECONDS = max(
    15.0,
    float(os.environ.get("VEEB_RESOLVE_TIMEOUT", "45")),
)
UPSTREAM_CONNECT_TIMEOUT_SECONDS = max(
    2.0,
    float(os.environ.get("VEEB_UPSTREAM_CONNECT_TIMEOUT", "8")),
)
UPSTREAM_READ_TIMEOUT_SECONDS = max(
    10.0,
    float(os.environ.get("VEEB_UPSTREAM_READ_TIMEOUT", "45")),
)
PROXY_CHUNK_BYTES = max(
    64 * 1024,
    int(os.environ.get("VEEB_PROXY_CHUNK_BYTES", str(256 * 1024))),
)

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

    def valid(self) -> bool:
        return time.time() < self.expires_at


_resolved_cache: dict[str, ResolvedMedia] = {}
_resolve_tasks: dict[str, asyncio.Task[ResolvedMedia]] = {}
_prefetch_tasks: dict[str, asyncio.Task[ResolvedMedia]] = {}
_resolve_lock = asyncio.Lock()
_http_client: httpx.AsyncClient | None = None


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
        print(
            "cookie runtime copy ready",
            json.dumps({"source": YOUTUBE_COOKIE_FILE, "runtime": WRITABLE_COOKIE_FILE}),
            flush=True,
        )
    return WRITABLE_COOKIE_FILE


def pot_http_server_ready() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 4416), timeout=0.4):
            return True
    except OSError:
        return False


def client_uses_cookies(client: str) -> bool:
    return client not in {"android", "android_vr", "ios", "visionos", "web_embedded"}


def youtube_extractor_args(client: str) -> str:
    args = [f"player_client={client}", "fetch_pot=auto"]
    if client in {"mweb", "web_music"} and not YOUTUBE_PREMIUM_ACCOUNT:
        args.append("use_ad_playback_context=true")
    args.append("player_skip=configs")
    args.append("skip=hls,dash")
    args.append(f"playback_wait={PLAYBACK_WAIT_SECONDS:g}")
    return "youtube:" + ";".join(args)


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


def build_resolve_command(video_id: str, client: str) -> list[str]:
    command = [
        "yt-dlp",
        "--dump-single-json",
        "--no-download",
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--no-check-formats",
        "--socket-timeout",
        "20",
        "--retries",
        "1",
        "--cache-dir",
        YTDLP_CACHE_DIR,
        "--js-runtimes",
        JSC_RUNTIME,
        "--extractor-args",
        youtube_extractor_args(client),
    ]

    cookie_file = get_writable_cookie_file() if client_uses_cookies(client) else None
    if cookie_file:
        command.extend(["--cookies", cookie_file])

    command.extend([
        "-f",
        SOURCE_FORMAT,
        f"https://www.youtube.com/watch?v={video_id}",
    ])
    return command


async def resolve_with_client(video_id: str, client: str, purpose: str) -> ResolvedMedia:
    started = time.monotonic()
    command = build_resolve_command(video_id, client)
    print(
        "url resolve attempt",
        json.dumps({
            "videoId": video_id,
            "purpose": purpose,
            "client": client,
            "sourceFormat": SOURCE_FORMAT,
            "potHttpReady": pot_http_server_ready(),
        }),
        flush=True,
    )

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=RESOLVE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError(f"yt-dlp resolve timed out for {client}")
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        print(
            "url resolve cancelled",
            json.dumps({
                "videoId": video_id,
                "purpose": purpose,
                "client": client,
                "elapsedSeconds": round(time.monotonic() - started, 2),
            }),
            flush=True,
        )
        raise

    stderr_text = stderr.decode("utf-8", "replace")
    if process.returncode != 0:
        tail = [line for line in stderr_text.splitlines() if line.strip()][-12:]
        raise RuntimeError(
            f"yt-dlp resolve failed for {client}: " + " | ".join(tail)[-1600:]
        )

    try:
        info = json.loads(stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"yt-dlp returned invalid JSON for {client}: {exc}") from exc

    media_url = str(info.get("url") or "").strip()
    if not media_url.startswith(("https://", "http://")):
        raise RuntimeError(f"yt-dlp did not return a direct media URL for {client}")

    raw_headers = info.get("http_headers") or {}
    http_headers = {
        str(key): str(value)
        for key, value in raw_headers.items()
        if value is not None
    }

    media = ResolvedMedia(
        video_id=video_id,
        url=media_url,
        http_headers=http_headers,
        client=client,
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
    )

    print(
        "url resolve success",
        json.dumps({
            "videoId": video_id,
            "purpose": purpose,
            "client": client,
            "formatId": media.format_id,
            "expiresInSeconds": max(0, int(media.expires_at - time.time())),
            "elapsedSeconds": round(time.monotonic() - started, 2),
        }),
        flush=True,
    )
    return media


async def resolve_media_uncached(
    video_id: str,
    purpose: str,
    excluded_clients: set[str] | None = None,
) -> ResolvedMedia:
    excluded = excluded_clients or set()
    errors: list[str] = []

    async with _resolve_lock:
        # Another task may have filled the cache while we were waiting.
        if not excluded:
            cached = get_cached_media(video_id)
            if cached:
                return cached

        for client in CLIENTS:
            if client in excluded:
                continue
            try:
                media = await resolve_with_client(video_id, client, purpose)
                _resolved_cache[video_id] = media
                cleanup_resolved_cache()
                return media
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                message = str(exc)
                errors.append(f"{client}: {message}")
                print(
                    "url resolve client failed",
                    json.dumps({
                        "videoId": video_id,
                        "purpose": purpose,
                        "client": client,
                        "error": message[-1200:],
                    }),
                    flush=True,
                )

    raise HTTPException(
        status_code=502,
        detail="No YouTube playback client produced a direct media URL: " + " || ".join(errors)[-2500:],
    )


def resolve_task_finished(video_id: str, task: asyncio.Task[ResolvedMedia]) -> None:
    if _resolve_tasks.get(video_id) is task:
        _resolve_tasks.pop(video_id, None)
    if _prefetch_tasks.get(video_id) is task:
        _prefetch_tasks.pop(video_id, None)
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        print(
            "background url resolve failed",
            json.dumps({"videoId": video_id, "error": str(exc)[-1600:]}),
            flush=True,
        )


def start_resolve_task(video_id: str, purpose: str) -> asyncio.Task[ResolvedMedia]:
    existing = _resolve_tasks.get(video_id)
    if existing and not existing.done():
        return existing

    task = asyncio.create_task(resolve_media_uncached(video_id, purpose))
    _resolve_tasks[video_id] = task
    if purpose == "prefetch":
        _prefetch_tasks[video_id] = task
    task.add_done_callback(lambda done: resolve_task_finished(video_id, done))
    return task


async def get_or_resolve(video_id: str, purpose: str) -> tuple[ResolvedMedia, str]:
    cached = get_cached_media(video_id)
    if cached:
        return cached, "HIT"

    task = _resolve_tasks.get(video_id)
    if task and not task.done():
        return await task, "WAIT"

    if purpose == "live":
        # Foreground playback wins over wrong speculative work. The URL resolver
        # is single-flight because concurrent BotGuard/JS work is slower on the
        # small Render instance.
        for other_id, prefetch_task in list(_prefetch_tasks.items()):
            if other_id != video_id and not prefetch_task.done():
                print(
                    "foreground playback preempting speculative url resolve",
                    json.dumps({"fromVideoId": other_id, "toVideoId": video_id}),
                    flush=True,
                )
                prefetch_task.cancel()

    task = start_resolve_task(video_id, purpose)
    return await task, "MISS"


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
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            http2=True,
        )
    return _http_client


def build_upstream_headers(media: ResolvedMedia, request: Request) -> dict[str, str]:
    blocked = {
        "authorization",
        "cookie",
        "host",
        "content-length",
        "connection",
        "transfer-encoding",
    }
    headers = {
        key: value
        for key, value in media.http_headers.items()
        if key.lower() not in blocked
    }

    requested_range = request.headers.get("range")
    if requested_range:
        headers["Range"] = requested_range

    # Identity avoids intermediary decompression changing byte ranges.
    headers["Accept-Encoding"] = "identity"
    return headers


PASSTHROUGH_RESPONSE_HEADERS = {
    "accept-ranges",
    "content-length",
    "content-range",
    "content-type",
    "etag",
    "last-modified",
}


def build_downstream_headers(
    upstream: httpx.Response,
    media: ResolvedMedia,
    cache_state: str,
) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in upstream.headers.items():
        if key.lower() in PASSTHROUGH_RESPONSE_HEADERS:
            headers[key] = value

    headers.setdefault("Content-Type", "video/mp4")
    headers.setdefault("Accept-Ranges", "bytes")
    headers["Cache-Control"] = "private, no-store"
    headers["X-Veeb-Resolver"] = "direct-url-proxy-v28"
    headers["X-Veeb-Resolved-Cache"] = cache_state
    headers["X-Veeb-Playback-Client"] = media.client
    headers["X-Veeb-Source-Format"] = media.format_id or SOURCE_FORMAT
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
    upstream_request = client.build_request(
        request.method,
        media.url,
        headers=build_upstream_headers(media, request),
    )
    return await client.send(upstream_request, stream=True)


async def proxy_media(request: Request, video_id: str):
    media, cache_state = await get_or_resolve(video_id, "live")
    response = await open_media_upstream(media, request)

    # Expired/bad GVS URLs should be refreshed once. Rotate away from the client
    # that just produced the rejected URL when possible.
    if response.status_code in {403, 410}:
        await response.aclose()
        rejected_client = media.client
        invalidate_media(video_id)
        print(
            "direct media url rejected; resolving fallback",
            json.dumps({
                "videoId": video_id,
                "status": response.status_code,
                "rejectedClient": rejected_client,
            }),
            flush=True,
        )
        media = await resolve_media_uncached(
            video_id,
            "live-fallback",
            excluded_clients={rejected_client},
        )
        cache_state = "REFRESH"
        response = await open_media_upstream(media, request)

    if response.status_code >= 400:
        status = response.status_code
        body = await response.aread()
        await response.aclose()
        detail = body[:800].decode("utf-8", "replace") if body else ""
        raise HTTPException(
            status_code=502,
            detail=f"Upstream media server returned HTTP {status}" + (f": {detail}" if detail else ""),
        )

    headers = build_downstream_headers(response, media, cache_state)

    print(
        "direct media proxy open",
        json.dumps({
            "videoId": video_id,
            "client": media.client,
            "formatId": media.format_id,
            "resolvedCache": cache_state,
            "range": request.headers.get("range"),
            "upstreamStatus": response.status_code,
        }),
        flush=True,
    )

    if request.method == "HEAD":
        await response.aclose()
        return Response(status_code=response.status_code, headers=headers)

    return StreamingResponse(
        upstream_body(response),
        status_code=response.status_code,
        headers=headers,
        media_type=headers.get("Content-Type"),
    )


@app.on_event("shutdown")
async def shutdown_http_client() -> None:
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


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
        "version": "v28-direct-url-proxy",
        "ytDlpVersion": ytdlp_version,
        "sourceFormat": SOURCE_FORMAT,
        "clients": CLIENTS,
        "potHttpReady": pot_http_server_ready(),
        "resolvedUrlCacheEntries": len(_resolved_cache),
        "activeResolves": len([task for task in _resolve_tasks.values() if not task.done()]),
        "architecture": "resolve-once-direct-range-proxy",
    }


@app.get("/resolve/{video_id}")
async def resolve_endpoint(
    video_id: str,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    require_auth(authorization)
    video_id = validate_video_id(video_id)
    media, cache_state = await get_or_resolve(video_id, "metadata")
    return JSONResponse({
        "provider": "yt-dlp-direct-proxy",
        "videoId": video_id,
        "title": media.title,
        "duration": media.duration,
        "formatId": media.format_id,
        "ext": media.ext,
        "audioCodec": media.acodec,
        "videoCodec": media.vcodec,
        "abr": media.abr,
        "client": media.client,
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

    active_other = [
        (other_id, task)
        for other_id, task in _prefetch_tasks.items()
        if other_id != video_id and not task.done()
    ]
    if active_other and not intent:
        return JSONResponse({"ok": True, "status": "busy", "videoId": video_id}, status_code=202)

    if intent:
        for other_id, task in active_other:
            print(
                "intent prefetch replacing speculative url resolve",
                json.dumps({"fromVideoId": other_id, "toVideoId": video_id}),
                flush=True,
            )
            task.cancel()

    start_resolve_task(video_id, "prefetch")
    return JSONResponse({"ok": True, "status": "warming", "videoId": video_id}, status_code=202)


@app.api_route("/stream/{video_id}", methods=["GET", "HEAD"])
async def stream_endpoint(
    request: Request,
    video_id: str,
    authorization: str | None = Header(default=None),
):
    require_auth(authorization)
    video_id = validate_video_id(video_id)
    return await proxy_media(request, video_id)
