import asyncio
import os
import re
import time
import json
import subprocess
import shutil
import importlib.metadata
import socket
from typing import Any, AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

app = FastAPI(title="Veeb YouTube Resolver", docs_url=None, redoc_url=None)

RESOLVER_SECRET = os.environ.get("RESOLVER_SECRET", "")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
CACHE_TTL_SECONDS = int(os.environ.get("RESOLVER_CACHE_TTL", "600"))
YOUTUBE_COOKIE_FILE = os.environ.get("YOUTUBE_COOKIE_FILE", "/etc/secrets/youtube-cookies.txt")
WRITABLE_COOKIE_FILE = os.environ.get("WRITABLE_COOKIE_FILE", "/tmp/veeb-youtube-cookies.txt")

# V13 deliberately forces YouTube's Opus audio-only format.
# This avoids falling back to muxed MP4/video format 18.
STREAM_FORMAT = os.environ.get("YOUTUBE_STREAM_FORMAT", "251")
STREAM_CONTENT_TYPE = "audio/webm"
PREBUFFER_BYTES = int(os.environ.get("STREAM_PREBUFFER_BYTES", str(64 * 1024)))
PREBUFFER_TIMEOUT_SECONDS = float(os.environ.get("STREAM_PREBUFFER_TIMEOUT_SECONDS", "30"))

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


def pot_http_server_ready() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 4416), timeout=0.4):
            return True
    except OSError:
        return False


def base_ytdlp_args() -> list[str]:
    args = [
        "--no-playlist",
        "--no-warnings",
        "--socket-timeout", "20",
        "--retries", "2",
        "--fragment-retries", "2",
        "--js-runtimes", "node",
        # bgutil's HTTP provider runs locally on 127.0.0.1:4416.
        # The plugin prioritises HTTP when it is available.
        "--extractor-args", "youtube:player_client=mweb;fetch_pot=always",
    ]

    cookie_file = get_writable_cookie_file()
    if cookie_file:
        args.extend(["--cookies", cookie_file])

    return args


def extract_with_ytdlp(video_id: str) -> dict[str, Any]:
    watch_url = f"https://www.youtube.com/watch?v={video_id}"

    cmd = [
        "yt-dlp",
        "--dump-single-json",
        "--skip-download",
        *base_ytdlp_args(),
        "-f", STREAM_FORMAT,
        watch_url,
    ]

    env = os.environ.copy()
    env.setdefault("TOKEN_TTL", "6")

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("yt-dlp timed out while resolving YouTube playback") from exc

    stderr = (completed.stderr or "").strip()
    if completed.returncode != 0:
        if stderr:
            print("yt-dlp resolver error:", stderr[-5000:], flush=True)
        raise RuntimeError(stderr[-2000:] or f"yt-dlp exited with code {completed.returncode}")

    try:
        info = json.loads(completed.stdout)
    except Exception as exc:
        if stderr:
            print("yt-dlp non-JSON stderr:", stderr[-2500:], flush=True)
        raise RuntimeError("yt-dlp returned invalid JSON metadata") from exc

    if not info or not isinstance(info, dict):
        raise RuntimeError("yt-dlp returned no media information")

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
    }


async def resolve_video(video_id: str, force_refresh: bool = False) -> dict[str, Any]:
    now = time.monotonic()

    if not force_refresh:
        cached = _resolve_cache.get(video_id)
        if cached and cached[0] > now:
            return cached[1]

    async with _cache_lock:
        now = time.monotonic()
        if not force_refresh:
            cached = _resolve_cache.get(video_id)
            if cached and cached[0] > now:
                return cached[1]

        try:
            info = await asyncio.to_thread(extract_with_ytdlp, video_id)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"YouTube resolver failed: {exc}") from exc

        _resolve_cache[video_id] = (time.monotonic() + CACHE_TTL_SECONDS, info)
        return info


async def drain_stderr(
    process: asyncio.subprocess.Process,
    video_id: str,
    tail: list[str],
) -> None:
    if process.stderr is None:
        return

    while True:
        line = await process.stderr.readline()
        if not line:
            break

        text = line.decode("utf-8", errors="replace").rstrip()
        if not text:
            continue

        tail.append(text)
        if len(tail) > 80:
            del tail[:-80]


async def terminate_process(
    process: asyncio.subprocess.Process,
    stderr_task: asyncio.Task | None,
) -> None:
    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=4)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    if stderr_task:
        try:
            await asyncio.wait_for(stderr_task, timeout=2)
        except Exception:
            stderr_task.cancel()


async def start_ytdlp_stream(
    video_id: str,
) -> tuple[asyncio.subprocess.Process, asyncio.Task, list[str]]:
    watch_url = f"https://www.youtube.com/watch?v={video_id}"

    cmd = [
        "yt-dlp",
        "-o", "-",
        "--no-progress",
        "--no-part",
        *base_ytdlp_args(),
        "-f", STREAM_FORMAT,
        watch_url,
    ]

    env = os.environ.copy()
    env.setdefault("TOKEN_TTL", "6")

    print(
        "yt-dlp stream start",
        json.dumps({
            "videoId": video_id,
            "transport": "stdout",
            "format": STREAM_FORMAT,
            "potHttpReady": pot_http_server_ready(),
        }),
        flush=True,
    )

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    stderr_tail: list[str] = []
    stderr_task = asyncio.create_task(
        drain_stderr(process, video_id, stderr_tail)
    )

    return process, stderr_task, stderr_tail


async def get_first_stream_chunk(
    process: asyncio.subprocess.Process,
    stderr_task: asyncio.Task,
    stderr_tail: list[str],
    video_id: str,
) -> bytes:
    assert process.stdout is not None

    try:
        first_chunk = await asyncio.wait_for(
            process.stdout.read(PREBUFFER_BYTES),
            timeout=PREBUFFER_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        print(
            "yt-dlp stream prebuffer timeout",
            json.dumps({
                "videoId": video_id,
                "stderrTail": stderr_tail[-20:],
            }),
            flush=True,
        )
        await terminate_process(process, stderr_task)
        raise HTTPException(
            status_code=504,
            detail="yt-dlp did not produce audio bytes before the prebuffer timeout",
        )

    if not first_chunk:
        try:
            await asyncio.wait_for(process.wait(), timeout=1)
        except asyncio.TimeoutError:
            pass

        print(
            "yt-dlp stream produced no bytes",
            json.dumps({
                "videoId": video_id,
                "returnCode": process.returncode,
                "stderrTail": stderr_tail[-30:],
            }),
            flush=True,
        )

        await terminate_process(process, stderr_task)
        raise HTTPException(
            status_code=502,
            detail="yt-dlp produced no audio bytes",
        )

    print(
        "yt-dlp stream prebuffer ready",
        json.dumps({
            "videoId": video_id,
            "bytes": len(first_chunk),
        }),
        flush=True,
    )

    return first_chunk


async def stream_stdout(
    process: asyncio.subprocess.Process,
    first_chunk: bytes,
    video_id: str,
) -> AsyncIterator[bytes]:
    assert process.stdout is not None

    total = 0

    try:
        total += len(first_chunk)
        yield first_chunk

        while True:
            chunk = await process.stdout.read(64 * 1024)
            if not chunk:
                break

            total += len(chunk)
            yield chunk

    finally:
        print(
            "yt-dlp stream body finished",
            json.dumps({
                "videoId": video_id,
                "bytesSent": total,
                "returnCode": process.returncode,
            }),
            flush=True,
        )


@app.get("/health")
async def health() -> dict[str, Any]:
    try:
        pot_version = importlib.metadata.version("bgutil-ytdlp-pot-provider")
    except importlib.metadata.PackageNotFoundError:
        pot_version = None

    get_writable_cookie_file()

    return {
        "ok": True,
        "service": "veeb-youtube-resolver-v13",
        "secretConfigured": bool(RESOLVER_SECRET),
        "youtubeClient": "mweb",
        "poTokenProvider": "bgutil",
        "poTokenProviderVersion": pot_version,
        "poTokenHttpServerReady": pot_http_server_ready(),
        "cookieFileConfigured": bool(YOUTUBE_COOKIE_FILE),
        "cookieFilePresent": os.path.isfile(YOUTUBE_COOKIE_FILE),
        "cookieFilePath": YOUTUBE_COOKIE_FILE,
        "writableCookieFilePath": WRITABLE_COOKIE_FILE,
        "writableCookieFilePresent": os.path.isfile(WRITABLE_COOKIE_FILE),
        "streamTransport": "yt-dlp-stdout-prebuffered",
        "streamFormat": STREAM_FORMAT,
        "streamContentType": STREAM_CONTENT_TYPE,
        "prebufferBytes": PREBUFFER_BYTES,
        "prebufferTimeoutSeconds": PREBUFFER_TIMEOUT_SECONDS,
        "rangeSeeking": False,
    }


@app.get("/resolve/{video_id}")
async def resolve_endpoint(
    video_id: str,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    require_auth(authorization)
    validate_video_id(video_id)

    info = await resolve_video(video_id)

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

    headers = {
        "Content-Type": STREAM_CONTENT_TYPE,
        "Cache-Control": "private, no-store, max-age=0",
        "X-Veeb-Resolver": "yt-dlp-stdout-v13",
        "Accept-Ranges": "none",
    }

    if request.method == "HEAD":
        return Response(status_code=200, headers=headers)

    process, stderr_task, stderr_tail = await start_ytdlp_stream(video_id)

    # V13 does not send HTTP 200 until yt-dlp has actually produced media bytes.
    # This prevents Cloudflare/browser from opening a body that then sits empty
    # while YouTube JS/PO-token work is still happening.
    first_chunk = await get_first_stream_chunk(
        process,
        stderr_task,
        stderr_tail,
        video_id,
    )

    return StreamingResponse(
        stream_stdout(process, first_chunk, video_id),
        status_code=200,
        headers=headers,
        background=BackgroundTask(
            terminate_process,
            process,
            stderr_task,
        ),
    )
