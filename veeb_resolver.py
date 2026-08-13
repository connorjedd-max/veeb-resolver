import asyncio
import os
import re
import time
import json
import shutil
import importlib.metadata
import socket
import shlex
from typing import Any, AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

app = FastAPI(title="Veeb YouTube Resolver", docs_url=None, redoc_url=None)

RESOLVER_SECRET = os.environ.get("RESOLVER_SECRET", "")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_COOKIE_FILE = os.environ.get("YOUTUBE_COOKIE_FILE", "/etc/secrets/youtube-cookies.txt")
WRITABLE_COOKIE_FILE = os.environ.get("WRITABLE_COOKIE_FILE", "/tmp/veeb-youtube-cookies.txt")

# Format 18 is the YouTube path that has proven reliable from this Render host.
# V17 strips its video track with ffmpeg and relays AAC-only fragmented MP4.
SOURCE_FORMAT = os.environ.get("YOUTUBE_STREAM_FORMAT", "18")
STREAM_CONTENT_TYPE = "audio/mp4"
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


def build_pipeline(video_id: str) -> str:
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    cookie_file = get_writable_cookie_file()

    ytdlp = [
        "yt-dlp",
        "-o", "-",
        "--no-progress",
        "--no-part",
        "--no-playlist",
        "--no-warnings",
        "--socket-timeout", "20",
        "--retries", "2",
        "--fragment-retries", "2",
        "--js-runtimes", "node",
        "--extractor-args", "youtube:player_client=mweb;fetch_pot=always",
    ]

    if cookie_file:
        ytdlp.extend(["--cookies", cookie_file])

    ytdlp.extend(["-f", SOURCE_FORMAT, watch_url])

    # -c:a copy means no audio re-encode. ffmpeg only removes the H.264 video
    # stream and rewrites the existing AAC audio into fragmented MP4 so the
    # browser can begin playback before the full song has downloaded.
    ffmpeg = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-i", "pipe:0",
        "-map", "0:a:0",
        "-vn",
        "-c:a", "copy",
        "-f", "mp4",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "pipe:1",
    ]

    return (
        " ".join(shlex.quote(part) for part in ytdlp)
        + " | "
        + " ".join(shlex.quote(part) for part in ffmpeg)
    )


async def drain_stderr(process: asyncio.subprocess.Process, tail: list[str]) -> None:
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
        if len(tail) > 160:
            del tail[:-160]


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


async def start_stream(video_id: str):
    pipeline = build_pipeline(video_id)

    env = os.environ.copy()
    env.setdefault("TOKEN_TTL", "6")

    print(
        "audio-only stream start",
        json.dumps({
            "videoId": video_id,
            "sourceFormat": SOURCE_FORMAT,
            "output": "aac-fragmented-mp4",
            "potHttpReady": pot_http_server_ready(),
            "startupTimeoutSeconds": STREAM_START_TIMEOUT_SECONDS,
        }),
        flush=True,
    )

    process = await asyncio.create_subprocess_exec(
        "bash",
        "-o", "pipefail",
        "-c", pipeline,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    stderr_tail: list[str] = []
    stderr_task = asyncio.create_task(drain_stderr(process, stderr_tail))
    return process, stderr_task, stderr_tail


async def read_first_chunk(process: asyncio.subprocess.Process) -> bytes:
    assert process.stdout is not None

    if STREAM_START_TIMEOUT_SECONDS > 0:
        return await asyncio.wait_for(
            process.stdout.read(64 * 1024),
            timeout=STREAM_START_TIMEOUT_SECONDS,
        )

    return await process.stdout.read(64 * 1024)


async def open_working_stream(video_id: str):
    process, stderr_task, stderr_tail = await start_stream(video_id)

    try:
        first_chunk = await read_first_chunk(process)
    except asyncio.TimeoutError:
        print(
            "audio-only stream startup timeout",
            json.dumps({"videoId": video_id, "stderrTail": stderr_tail[-30:]}),
            flush=True,
        )
        await terminate_process(process, stderr_task)
        raise HTTPException(status_code=504, detail="Audio stream startup timeout")

    if first_chunk:
        print(
            "audio-only first bytes",
            json.dumps({
                "videoId": video_id,
                "bytes": len(first_chunk),
                "contentType": STREAM_CONTENT_TYPE,
            }),
            flush=True,
        )
        return process, stderr_task, first_chunk

    try:
        await asyncio.wait_for(process.wait(), timeout=1)
    except asyncio.TimeoutError:
        pass

    print(
        "audio-only stream produced no bytes",
        json.dumps({
            "videoId": video_id,
            "returnCode": process.returncode,
            "stderrTail": stderr_tail[-50:],
        }),
        flush=True,
    )

    await terminate_process(process, stderr_task)
    raise HTTPException(status_code=502, detail="yt-dlp/ffmpeg produced no audio bytes")


async def stream_stdout(
    process: asyncio.subprocess.Process,
    first_chunk: bytes,
    video_id: str,
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
        print(
            "audio-only stream finished",
            json.dumps({
                "videoId": video_id,
                "bytesSent": total,
                "elapsedSeconds": round(time.monotonic() - started, 2),
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
        "service": "veeb-youtube-resolver-v17",
        "secretConfigured": bool(RESOLVER_SECRET),
        "youtubeClient": "mweb",
        "poTokenProvider": "bgutil",
        "poTokenProviderVersion": pot_version,
        "poTokenHttpServerReady": pot_http_server_ready(),
        "cookieFilePresent": os.path.isfile(YOUTUBE_COOKIE_FILE),
        "writableCookieFilePresent": os.path.isfile(WRITABLE_COOKIE_FILE),
        "sourceFormat": SOURCE_FORMAT,
        "streamContentType": STREAM_CONTENT_TYPE,
        "streamTransport": "yt-dlp-format18-ffmpeg-audio-only",
        "audioCodec": "aac-copy",
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

    headers = {
        "Content-Type": STREAM_CONTENT_TYPE,
        "Cache-Control": "private, no-store, max-age=0",
        "X-Veeb-Resolver": "yt-dlp-ffmpeg-audio-v17",
        "X-Veeb-Source-Format": SOURCE_FORMAT,
        "Accept-Ranges": "none",
    }

    if request.method == "HEAD":
        return Response(status_code=200, headers=headers)

    process, stderr_task, first_chunk = await open_working_stream(video_id)

    return StreamingResponse(
        stream_stdout(process, first_chunk, video_id),
        status_code=200,
        headers=headers,
        background=BackgroundTask(terminate_process, process, stderr_task),
    )
