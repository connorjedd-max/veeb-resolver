import asyncio
import http.cookiejar
import urllib.request
import os
import re
import time
import json
import subprocess
import shutil
import importlib.metadata
from typing import Any


import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

app = FastAPI(title="Veeb YouTube Resolver", docs_url=None, redoc_url=None)

RESOLVER_SECRET = os.environ.get("RESOLVER_SECRET", "")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
CACHE_TTL_SECONDS = int(os.environ.get("RESOLVER_CACHE_TTL", "600"))
YOUTUBE_COOKIE_FILE = os.environ.get("YOUTUBE_COOKIE_FILE", "/etc/secrets/youtube-cookies.txt")
WRITABLE_COOKIE_FILE = os.environ.get("WRITABLE_COOKIE_FILE", "/tmp/veeb-youtube-cookies.txt")
MAX_UPSTREAM_CHUNK_BYTES = int(os.environ.get("MAX_UPSTREAM_CHUNK_BYTES", str(8 * 1024 * 1024)))

# Cache only yt-dlp's resolved media metadata. The media itself is never stored.
_resolve_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = asyncio.Lock()


def require_auth(authorization: str | None) -> None:
    if not RESOLVER_SECRET:
        raise HTTPException(status_code=503, detail="RESOLVER_SECRET is not configured")
    expected = f"Bearer {RESOLVER_SECRET}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


def validate_video_id(video_id: str) -> str:
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise HTTPException(status_code=400, detail="Invalid YouTube video ID")
    return video_id


def get_writable_cookie_file() -> str | None:
    # Render mounts Secret Files under /etc/secrets as read-only. yt-dlp reads
    # cookies and then saves its cookie jar on exit, so passing the mounted file
    # directly causes Errno 30. Copy it once to /tmp and let yt-dlp update that
    # private, writable runtime copy for the lifetime of this resolver process.
    if not os.path.isfile(YOUTUBE_COOKIE_FILE):
        return None

    if not os.path.isfile(WRITABLE_COOKIE_FILE):
        shutil.copyfile(YOUTUBE_COOKIE_FILE, WRITABLE_COOKIE_FILE)
        os.chmod(WRITABLE_COOKIE_FILE, 0o600)
        print(
            "cookie runtime copy ready",
            json.dumps({
                "source": YOUTUBE_COOKIE_FILE,
                "runtime": WRITABLE_COOKIE_FILE,
            }),
            flush=True,
        )

    return WRITABLE_COOKIE_FILE


def extract_with_ytdlp(video_id: str) -> dict[str, Any]:
    watch_url = f"https://www.youtube.com/watch?v={video_id}"

    # V10 keeps the V9 mweb + bgutil + chunked relay setup, but passes yt-dlp
    # a writable /tmp copy of Render's read-only Secret File.
    cookie_file = get_writable_cookie_file()
    cmd = [
        "yt-dlp",
        "--dump-single-json",
        "--no-playlist",
        "--skip-download",
        "--no-warnings",
        "--socket-timeout", "20",
        "--retries", "2",
        "--fragment-retries", "2",
        "--js-runtimes", "node",
        "--extractor-args", "youtube:player_client=mweb",
        "--extractor-args", "youtubepot-bgutilscript:server_home=/opt/bgutil/server",
    ]

    if cookie_file:
        cmd.extend(["--cookies", cookie_file])

    cmd.extend([
        "-f", "bestaudio[protocol^=http][vcodec=none]/bestaudio[protocol^=http]/bestaudio/best",
        watch_url,
    ])

    env = os.environ.copy()
    env.setdefault("TOKEN_TTL", "6")

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=55,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("yt-dlp timed out while resolving YouTube playback") from exc

    stderr = (completed.stderr or "").strip()
    if completed.returncode != 0:
        if stderr:
            print("yt-dlp resolver error:", stderr[-4000:], flush=True)
        raise RuntimeError(stderr[-1800:] or f"yt-dlp exited with code {completed.returncode}")

    try:
        info = json.loads(completed.stdout)
    except Exception as exc:
        if stderr:
            print("yt-dlp non-JSON stderr:", stderr[-2000:], flush=True)
        raise RuntimeError("yt-dlp returned invalid JSON metadata") from exc

    if not info or not isinstance(info, dict):
        raise RuntimeError("yt-dlp returned no media information")

    media_url = info.get("url")
    if not isinstance(media_url, str) or not media_url.startswith(("http://", "https://")):
        raise RuntimeError("yt-dlp did not return a direct HTTP media URL")

    headers = info.get("http_headers") or {}
    if not isinstance(headers, dict):
        headers = {}

    print(
        "yt-dlp resolved",
        json.dumps({
            "videoId": video_id,
            "title": info.get("title"),
            "formatId": info.get("format_id"),
            "ext": info.get("ext"),
            "acodec": info.get("acodec"),
            "duration": info.get("duration"),
        }),
        flush=True,
    )

    return {
        "id": video_id,
        "title": info.get("title"),
        "duration": info.get("duration"),
        "format_id": info.get("format_id"),
        "ext": info.get("ext"),
        "acodec": info.get("acodec"),
        "abr": info.get("abr"),
        "url": media_url,
        "http_headers": {str(k): str(v) for k, v in headers.items() if v is not None},
    }


async def resolve_video(video_id: str, force_refresh: bool = False) -> dict[str, Any]:
    now = time.monotonic()

    if not force_refresh:
        cached = _resolve_cache.get(video_id)
        if cached and cached[0] > now:
            return cached[1]

    # Avoid duplicate extraction work when several range requests arrive together.
    async with _cache_lock:
        now = time.monotonic()
        if not force_refresh:
            cached = _resolve_cache.get(video_id)
            if cached and cached[0] > now:
                return cached[1]

        try:
            info = await asyncio.to_thread(extract_with_ytdlp, video_id)
        except DownloadError as exc:
            raise HTTPException(status_code=502, detail=f"YouTube resolver failed: {exc}") from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"YouTube resolver failed: {exc}") from exc

        _resolve_cache[video_id] = (time.monotonic() + CACHE_TTL_SECONDS, info)
        return info


async def close_upstream(client: httpx.AsyncClient, response: httpx.Response) -> None:
    try:
        await response.aclose()
    finally:
        await client.aclose()


def clamp_range_header(range_header: str | None) -> str | None:
    if not range_header:
        return f"bytes=0-{MAX_UPSTREAM_CHUNK_BYTES - 1}"

    match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header.strip())
    if not match:
        return range_header

    start = int(match.group(1))
    raw_end = match.group(2)
    requested_end = int(raw_end) if raw_end else None
    max_end = start + MAX_UPSTREAM_CHUNK_BYTES - 1
    end = min(requested_end, max_end) if requested_end is not None else max_end
    return f"bytes={start}-{end}"

def get_media_cookie_header(media_url: str) -> str:
    cookie_path = "/tmp/veeb-youtube-cookies.txt"

    if not os.path.isfile(cookie_path):
        return ""

    try:
        jar = http.cookiejar.MozillaCookieJar(cookie_path)

        jar.load(
            ignore_discard=True,
            ignore_expires=True,
        )

        cookie_request = urllib.request.Request(media_url)

        jar.add_cookie_header(cookie_request)

        return cookie_request.get_header("Cookie") or ""

    except Exception as exc:
        print(
            "media cookie load failed",
            {
                "error": str(exc),
                "cookiePath": cookie_path,
            },
            flush=True,
        )

        return ""

async def open_media_stream(
    info: dict[str, Any],
    range_header: str | None,
    method: str,
) -> tuple[httpx.AsyncClient, httpx.Response]:
    upstream_headers = dict(info.get("http_headers") or {})
    cookie_header = get_media_cookie_header(info["url"])

    if cookie_header:
        upstream_headers["Cookie"] = cookie_header

    print(
        "media auth",
        {
            "cookieHeaderPresent": bool(cookie_header),
            "cookieHeaderLength": len(cookie_header),
            "headerNames": sorted(upstream_headers.keys()),
        },
        flush=True,
    )
    upstream_headers.setdefault("Accept", "*/*")

    safe_range = clamp_range_header(range_header)
    if safe_range:
        upstream_headers["Range"] = safe_range

    print(
        "media fetch",
        json.dumps({
            "videoId": info.get("id"),
            "method": method,
            "browserRange": range_header,
            "upstreamRange": safe_range,
            "formatId": info.get("format_id"),
            "ext": info.get("ext"),
        }),
        flush=True,
    )

    client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(connect=15.0, read=60.0, write=30.0, pool=15.0),
    )

    request = client.build_request(method, info["url"], headers=upstream_headers)
    response = await client.send(request, stream=True)

    print(
        "media upstream",
        json.dumps({
            "videoId": info.get("id"),
            "status": response.status_code,
            "contentType": response.headers.get("content-type"),
            "contentLength": response.headers.get("content-length"),
            "contentRange": response.headers.get("content-range"),
        }),
        flush=True,
    )

    return client, response


@app.get("/health")
async def health() -> dict[str, Any]:
    try:
        pot_version = importlib.metadata.version("bgutil-ytdlp-pot-provider")
    except importlib.metadata.PackageNotFoundError:
        pot_version = None

    return {
        "ok": True,
        "service": "veeb-youtube-resolver-v10",
        "secretConfigured": bool(RESOLVER_SECRET),
        "youtubeClient": "mweb",
        "poTokenProvider": "bgutil",
        "poTokenProviderVersion": pot_version,
        "poTokenServerPresent": os.path.isdir("/opt/bgutil/server"),
        "cookieFileConfigured": bool(YOUTUBE_COOKIE_FILE),
        "cookieFilePresent": os.path.isfile(YOUTUBE_COOKIE_FILE),
        "cookieFilePath": YOUTUBE_COOKIE_FILE,
        "writableCookieFilePath": WRITABLE_COOKIE_FILE,
        "writableCookieFilePresent": os.path.isfile(WRITABLE_COOKIE_FILE),
        "maxUpstreamChunkBytes": MAX_UPSTREAM_CHUNK_BYTES,
    }


@app.get("/resolve/{video_id}")
async def resolve_endpoint(video_id: str, authorization: str | None = Header(default=None)) -> JSONResponse:
    require_auth(authorization)
    validate_video_id(video_id)
    info = await resolve_video(video_id)

    # Never expose Google's short-lived media URL to the caller. The resolver must
    # fetch the bytes itself so resolution and media fetch originate from one host.
    return JSONResponse(
        {
            "id": info.get("id"),
            "title": info.get("title"),
            "duration": info.get("duration"),
            "formatId": info.get("format_id"),
            "ext": info.get("ext"),
            "audioCodec": info.get("acodec"),
            "abr": info.get("abr"),
            "provider": "yt-dlp",
        },
        headers={"Cache-Control": "no-store"},
    )


@app.api_route("/stream/{video_id}", methods=["GET", "HEAD"])
async def stream_endpoint(
    video_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    require_auth(authorization)
    validate_video_id(video_id)

    range_header = request.headers.get("range")
    method = request.method
    info = await resolve_video(video_id)

    client, upstream = await open_media_stream(info, range_header, method)

    # A cached Google media URL can expire or become unusable. Refresh it once.
    if upstream.status_code in (401, 403, 410):
        await close_upstream(client, upstream)
        _resolve_cache.pop(video_id, None)
        info = await resolve_video(video_id, force_refresh=True)
        client, upstream = await open_media_stream(info, range_header, method)

    if upstream.status_code >= 400:
        body = await upstream.aread()
        status = upstream.status_code
        await close_upstream(client, upstream)
        detail = body[:500].decode("utf-8", errors="replace")
        raise HTTPException(status_code=502, detail=f"YouTube media origin returned {status}: {detail}")

    passthrough = {
        "content-type",
        "content-length",
        "content-range",
        "accept-ranges",
        "etag",
        "last-modified",
    }
    headers: dict[str, str] = {}
    for name, value in upstream.headers.items():
        if name.lower() in passthrough:
            headers[name] = value

    headers["Cache-Control"] = "private, no-store, max-age=0"
    headers["X-Veeb-Resolver"] = "yt-dlp"
    headers.setdefault("Accept-Ranges", "bytes")

    if method == "HEAD":
        await close_upstream(client, upstream)
        return Response(status_code=upstream.status_code, headers=headers)

    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=headers,
        background=BackgroundTask(close_upstream, client, upstream),
    )
