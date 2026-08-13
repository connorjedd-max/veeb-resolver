import asyncio
import os
import re
import time
import json
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
YOUTUBE_COOKIE_FILE = os.environ.get("YOUTUBE_COOKIE_FILE", "/etc/secrets/youtube-cookies.txt")
WRITABLE_COOKIE_FILE = os.environ.get("WRITABLE_COOKIE_FILE", "/tmp/veeb-youtube-cookies.txt")

# V15: try audio-only Opus first, then automatically fall back to muxed MP4
# because V12 proved that path can actually deliver bytes from this Render IP.
PRIMARY_FORMAT = os.environ.get("YOUTUBE_PRIMARY_FORMAT", "251")
FALLBACK_FORMAT = os.environ.get("YOUTUBE_FALLBACK_FORMAT", "18")
STREAM_START_TIMEOUT_SECONDS = float(os.environ.get("STREAM_START_TIMEOUT_SECONDS", "0"))

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
        print("cookie runtime copy ready", json.dumps({
            "source": YOUTUBE_COOKIE_FILE,
            "runtime": WRITABLE_COOKIE_FILE,
        }), flush=True)
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
        "--extractor-args", "youtube:player_client=mweb;fetch_pot=always",
    ]
    cookie_file = get_writable_cookie_file()
    if cookie_file:
        args.extend(["--cookies", cookie_file])
    return args


def content_type_for_format(format_id: str) -> str:
    if format_id == "251":
        return "audio/webm"
    if format_id == "18":
        return "video/mp4"
    return "application/octet-stream"


async def drain_stderr(process: asyncio.subprocess.Process, tail: list[str]) -> None:
    if process.stderr is None:
        return
    while True:
        line = await process.stderr.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace").rstrip()
        if text:
            tail.append(text)
            if len(tail) > 120:
                del tail[:-120]


async def terminate_process(process: asyncio.subprocess.Process, stderr_task: asyncio.Task | None) -> None:
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


async def start_attempt(video_id: str, format_id: str):
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [
        "yt-dlp",
        "-o", "-",
        "--no-progress",
        "--no-part",
        *base_ytdlp_args(),
        "-f", format_id,
        watch_url,
    ]

    env = os.environ.copy()
    env.setdefault("TOKEN_TTL", "6")

    print("yt-dlp stream attempt", json.dumps({
        "videoId": video_id,
        "format": format_id,
        "potHttpReady": pot_http_server_ready(),
    }), flush=True)

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    tail: list[str] = []
    stderr_task = asyncio.create_task(drain_stderr(process, tail))
    return process, stderr_task, tail


async def read_first_chunk(process: asyncio.subprocess.Process) -> bytes:
    assert process.stdout is not None
    if STREAM_START_TIMEOUT_SECONDS > 0:
        return await asyncio.wait_for(
            process.stdout.read(64 * 1024),
            timeout=STREAM_START_TIMEOUT_SECONDS,
        )
    return await process.stdout.read(64 * 1024)


async def select_working_stream(video_id: str):
    last_tail: list[str] = []

    for format_id in (PRIMARY_FORMAT, FALLBACK_FORMAT):
        process, stderr_task, tail = await start_attempt(video_id, format_id)

        try:
            first_chunk = await read_first_chunk(process)
        except asyncio.TimeoutError:
            await terminate_process(process, stderr_task)
            raise HTTPException(status_code=504, detail=f"yt-dlp format {format_id} startup timeout")

        if first_chunk:
            print("yt-dlp first audio bytes", json.dumps({
                "videoId": video_id,
                "format": format_id,
                "bytes": len(first_chunk),
            }), flush=True)
            return process, stderr_task, first_chunk, format_id

        try:
            await asyncio.wait_for(process.wait(), timeout=1)
        except asyncio.TimeoutError:
            pass

        last_tail = list(tail)
        print("yt-dlp format failed", json.dumps({
            "videoId": video_id,
            "format": format_id,
            "returnCode": process.returncode,
            "stderrTail": tail[-30:],
        }), flush=True)
        await terminate_process(process, stderr_task)

    raise HTTPException(
        status_code=502,
        detail=f"yt-dlp produced no media bytes; last error: {' | '.join(last_tail[-4:])}",
    )


async def stream_stdout(
    process: asyncio.subprocess.Process,
    first_chunk: bytes,
    video_id: str,
    format_id: str,
) -> AsyncIterator[bytes]:
    assert process.stdout is not None
    total = 0
    started = time.monotonic()
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
        print("yt-dlp stream body finished", json.dumps({
            "videoId": video_id,
            "format": format_id,
            "bytesSent": total,
            "elapsedSeconds": round(time.monotonic() - started, 2),
            "returnCode": process.returncode,
        }), flush=True)


@app.get("/health")
async def health() -> dict[str, Any]:
    try:
        pot_version = importlib.metadata.version("bgutil-ytdlp-pot-provider")
    except importlib.metadata.PackageNotFoundError:
        pot_version = None

    get_writable_cookie_file()

    return {
        "ok": True,
        "service": "veeb-youtube-resolver-v15",
        "secretConfigured": bool(RESOLVER_SECRET),
        "youtubeClient": "mweb",
        "poTokenProvider": "bgutil",
        "poTokenProviderVersion": pot_version,
        "poTokenHttpServerReady": pot_http_server_ready(),
        "cookieFilePresent": os.path.isfile(YOUTUBE_COOKIE_FILE),
        "writableCookieFilePresent": os.path.isfile(WRITABLE_COOKIE_FILE),
        "streamTransport": "yt-dlp-stdout-format-fallback",
        "primaryFormat": PRIMARY_FORMAT,
        "fallbackFormat": FALLBACK_FORMAT,
        "streamStartTimeoutSeconds": STREAM_START_TIMEOUT_SECONDS,
        "rangeSeeking": False,
    }


@app.api_route("/stream/{video_id}", methods=["GET", "HEAD"])
async def stream_endpoint(
    video_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    require_auth(authorization)
    validate_video_id(video_id)

    if request.method == "HEAD":
        return Response(
            status_code=200,
            headers={
                "Cache-Control": "private, no-store, max-age=0",
                "X-Veeb-Resolver": "yt-dlp-stdout-v15",
                "Accept-Ranges": "none",
            },
        )

    process, stderr_task, first_chunk, format_id = await select_working_stream(video_id)

    headers = {
        "Content-Type": content_type_for_format(format_id),
        "Cache-Control": "private, no-store, max-age=0",
        "X-Veeb-Resolver": "yt-dlp-stdout-v15",
        "X-Veeb-Format": format_id,
        "Accept-Ranges": "none",
    }

    return StreamingResponse(
        stream_stdout(process, first_chunk, video_id, format_id),
        status_code=200,
        headers=headers,
        background=BackgroundTask(terminate_process, process, stderr_task),
    )
