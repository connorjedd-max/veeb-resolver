import asyncio
import os
import re
import time
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


def extract_with_ytdlp(video_id: str) -> dict[str, Any]:
    watch_url = f"https://www.youtube.com/watch?v={video_id}"

    # Intentionally no account cookies and no PO-token provider here. This resolver
    # relies on yt-dlp's normal public-video extraction from the resolver host's IP.
    # If YouTube challenges that host, move the resolver to a network where normal
    # public YouTube playback works rather than trying to defeat the challenge.
    ydl_opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "cachedir": False,
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
        "format": "bestaudio[protocol^=http][vcodec=none]/bestaudio[protocol^=http]/bestaudio/best",
        # Node 22 is present in the supplied Docker image. Current yt-dlp uses an
        # external JS runtime for full YouTube extraction support.
        "js_runtimes": {"node": {"path": None}},
    }

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(watch_url, download=False)

    if not info or not isinstance(info, dict):
        raise RuntimeError("yt-dlp returned no media information")

    media_url = info.get("url")
    if not isinstance(media_url, str) or not media_url.startswith(("http://", "https://")):
        raise RuntimeError("yt-dlp did not return a direct HTTP media URL")

    headers = info.get("http_headers") or {}
    if not isinstance(headers, dict):
        headers = {}

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


async def open_media_stream(
    info: dict[str, Any],
    range_header: str | None,
    method: str,
) -> tuple[httpx.AsyncClient, httpx.Response]:
    upstream_headers = dict(info.get("http_headers") or {})
    upstream_headers.setdefault("Accept", "*/*")
    if range_header:
        upstream_headers["Range"] = range_header

    client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(connect=15.0, read=60.0, write=30.0, pool=15.0),
    )

    request = client.build_request(method, info["url"], headers=upstream_headers)
    response = await client.send(request, stream=True)
    return client, response


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "veeb-youtube-resolver",
        "secretConfigured": bool(RESOLVER_SECRET),
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
