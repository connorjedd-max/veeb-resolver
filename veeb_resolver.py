import asyncio
import os
import re
import time
import json
import subprocess
import shutil
import importlib.metadata
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

app = FastAPI(title="Veeb YouTube Resolver", docs_url=None, redoc_url=None)

RESOLVER_SECRET = os.environ.get("RESOLVER_SECRET", "")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
CACHE_TTL_SECONDS = int(os.environ.get("RESOLVER_CACHE_TTL", "600"))
YOUTUBE_COOKIE_FILE = os.environ.get("YOUTUBE_COOKIE_FILE", "/etc/secrets/youtube-cookies.txt")
WRITABLE_COOKIE_FILE = os.environ.get("WRITABLE_COOKIE_FILE", "/tmp/veeb-youtube-cookies.txt")
FORMAT_SELECTOR = "bestaudio[protocol^=http][vcodec=none]/bestaudio[protocol^=http]/bestaudio/best"

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


def base_ytdlp_args() -> list[str]:
    args = [
        "--no-playlist",
        "--no-warnings",
        "--socket-timeout", "20",
        "--retries", "2",
        "--fragment-retries", "2",
        "--js-runtimes", "node",
        "--extractor-args", "youtube:player_client=mweb",
        "--extractor-args", "youtubepot-bgutilscript:server_home=/opt/bgutil/server",
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
        "-f", FORMAT_SELECTOR,
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


def content_type_for(info: dict[str, Any]) -> str:
    ext = str(info.get("ext") or "").lower()
    acodec = str(info.get("acodec") or "").lower()

    if ext == "webm" or "opus" in acodec:
        return "audio/webm"
    if ext in ("m4a", "mp4"):
        return "audio/mp4"
    if ext == "mp3":
        return "audio/mpeg"
    if ext == "ogg":
        return "audio/ogg"
    return "application/octet-stream"


async def log_process_stderr(process: asyncio.subprocess.Process, video_id: str) -> None:
    if process.stderr is None:
        return
    tail: list[str] = []
    while True:
        line = await process.stderr.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace").rstrip()
        if text:
            tail.append(text)
            if len(tail) > 40:
                tail.pop(0)
    if process.returncode not in (None, 0) and tail:
        print(
            "yt-dlp stream stderr",
            json.dumps({"videoId": video_id, "tail": tail[-20:]}),
            flush=True,
        )


async def close_stream_process(process: asyncio.subprocess.Process, stderr_task: asyncio.Task | None) -> None:
    try:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=4)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
    finally:
        if stderr_task:
            try:
                await asyncio.wait_for(stderr_task, timeout=2)
            except Exception:
                stderr_task.cancel()


async def stream_process_stdout(process: asyncio.subprocess.Process, video_id: str):
    assert process.stdout is not None
    total = 0
    try:
        while True:
            chunk = await process.stdout.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            yield chunk
    finally:
        print(
            "yt-dlp stream ended",
            json.dumps({
                "videoId": video_id,
                "bytesSent": total,
                "returnCode": process.returncode,
            }),
            flush=True,
        )


async def start_ytdlp_stream(video_id: str) -> tuple[asyncio.subprocess.Process, asyncio.Task]:
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [
        "yt-dlp",
        "-o", "-",
        "--no-progress",
        "--no-part",
        *base_ytdlp_args(),
        "-f", FORMAT_SELECTOR,
        watch_url,
    ]

    env = os.environ.copy()
    env.setdefault("TOKEN_TTL", "6")

    print(
        "yt-dlp stream start",
        json.dumps({"videoId": video_id, "transport": "stdout"}),
        flush=True,
    )

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    stderr_task = asyncio.create_task(log_process_stderr(process, video_id))
    return process, stderr_task


@app.get("/health")
async def health() -> dict[str, Any]:
    try:
        pot_version = importlib.metadata.version("bgutil-ytdlp-pot-provider")
    except importlib.metadata.PackageNotFoundError:
        pot_version = None

    get_writable_cookie_file()

    return {
        "ok": True,
        "service": "veeb-youtube-resolver-v12",
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
        "streamTransport": "yt-dlp-stdout",
        "rangeSeeking": False,
    }


@app.get("/resolve/{video_id}")
async def resolve_endpoint(video_id: str, authorization: str | None = Header(default=None)) -> JSONResponse:
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

    # Resolve first so an unavailable/private/dead ID returns a real 502 before
    # we commit a 200 streaming response to the browser.
    info = await resolve_video(video_id)

    headers = {
        "Content-Type": content_type_for(info),
        "Cache-Control": "private, no-store, max-age=0",
        "X-Veeb-Resolver": "yt-dlp-stdout",
        # V12 intentionally does not advertise byte ranges. yt-dlp owns the
        # upstream media request so its PO-token/session state stays intact.
        "Accept-Ranges": "none",
    }

    if request.method == "HEAD":
        return Response(status_code=200, headers=headers)

    process, stderr_task = await start_ytdlp_stream(video_id)

    # Give yt-dlp a brief chance to fail before headers are committed.
    await asyncio.sleep(0.35)
    if process.returncode is not None and process.returncode != 0:
        try:
            await asyncio.wait_for(stderr_task, timeout=1)
        except Exception:
            pass
        raise HTTPException(status_code=502, detail="yt-dlp failed to start audio stream")

    return StreamingResponse(
        stream_process_stdout(process, video_id),
        status_code=200,
        headers=headers,
        background=BackgroundTask(close_stream_process, process, stderr_task),
    )
