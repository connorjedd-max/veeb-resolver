import asyncio
import importlib.metadata
import json
import os
import re
import shlex
import signal
import shutil
import socket
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

app = FastAPI(title="Veeb YouTube Resolver", docs_url=None, redoc_url=None)

RESOLVER_SECRET = os.environ.get("RESOLVER_SECRET", "")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
YOUTUBE_COOKIE_FILE = os.environ.get("YOUTUBE_COOKIE_FILE", "/etc/secrets/youtube-cookies.txt")
WRITABLE_COOKIE_FILE = os.environ.get("WRITABLE_COOKIE_FILE", "/tmp/veeb-youtube-cookies.txt")

SOURCE_FORMAT = os.environ.get("YOUTUBE_STREAM_FORMAT", "18")
STREAM_CONTENT_TYPE = "audio/mp4"

# V20 cold-start strategy:
# The V19 logs proved tv_downgraded only added a dead 12 second attempt.
# V20 goes directly to the known-good mweb path and uses yt-dlp's recommended
# Deno EJS runtime plus player_skip=configs to remove avoidable startup work.
PRIMARY_CLIENT = os.environ.get("YOUTUBE_CLIENT", "mweb").strip() or "mweb"
FALLBACK_CLIENT = ""
FAST_CLIENT_TIMEOUT_SECONDS = 0.0
STREAM_START_TIMEOUT_SECONDS = float(os.environ.get("STREAM_START_TIMEOUT_SECONDS", "0"))
JSC_RUNTIME = os.environ.get("YOUTUBE_JSC_RUNTIME", "deno").strip() or "deno"
YOUTUBE_PREMIUM_ACCOUNT = os.environ.get("YOUTUBE_PREMIUM_ACCOUNT", "false").strip().lower() in {
    "1", "true", "yes", "on"
}

CACHE_DIR = Path(os.environ.get("VEEB_AUDIO_CACHE_DIR", "/tmp/veeb-audio-cache"))
CACHE_TTL_SECONDS = int(os.environ.get("VEEB_AUDIO_CACHE_TTL", str(6 * 60 * 60)))
CACHE_MAX_FILES = int(os.environ.get("VEEB_AUDIO_CACHE_MAX_FILES", "40"))
PREFETCH_CONCURRENCY = max(1, int(os.environ.get("VEEB_PREFETCH_CONCURRENCY", "1")))
FILE_CHUNK_BYTES = 256 * 1024
FOREGROUND_READY_BYTES = max(64 * 1024, int(os.environ.get("VEEB_FOREGROUND_READY_BYTES", str(128 * 1024))))
PLAYBACK_WAIT_SECONDS = max(0.0, float(os.environ.get("YOUTUBE_PLAYBACK_WAIT", "0")))

# V22 uses only the authenticated mweb client. Android/iOS clients do not
# support account cookies, which makes them a poor fast lane on this Render IP.
FOREGROUND_FAST_CLIENT = ""

_cache_tasks: dict[str, asyncio.Task] = {}
_prefetch_started: set[str] = set()
_foreground_tasks: dict[str, asyncio.Task] = {}
_prefetch_semaphore = asyncio.Semaphore(PREFETCH_CONCURRENCY)


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


def client_plan() -> list[str]:
    result: list[str] = []
    for client in (PRIMARY_CLIENT, FALLBACK_CLIENT):
        if client and client not in result:
            result.append(client)
    return result


def youtube_extractor_args(client: str) -> str:
    args = [f"player_client={client}", "fetch_pot=auto"]

    # yt-dlp documents use_ad_playback_context specifically for mweb and
    # web_music. It removes the mandatory preroll wait. It must not be used
    # with Premium cookies because it can remove Premium formats.
    if client in {"mweb", "web_music"} and not YOUTUBE_PREMIUM_ACCOUNT:
        args.append("use_ad_playback_context=true")

    # Safe request reduction: yt-dlp documents player_skip=configs as skipping
    # the client-config network request. We keep webpage + JS because format 18
    # still needs the normal player/signature path on this account/IP.
    args.append("player_skip=configs")

    # Veeb only requests progressive format 18, so HLS/DASH manifest discovery
    # is unnecessary work. yt-dlp supports skipping both manifest families.
    args.append("skip=hls,dash")

    # yt-dlp's YouTube extractor otherwise has a playback wait between
    # extraction and download. mweb's ad playback context is already enabled,
    # so V22 explicitly starts with zero additional wait. Set the env var back
    # to 6 if YouTube begins rejecting immediately-started format 18 requests.
    args.append(f"playback_wait={PLAYBACK_WAIT_SECONDS:g}")

    return "youtube:" + ";".join(args)


def cache_path(video_id: str) -> Path:
    return CACHE_DIR / f"{video_id}.m4a.mp4"


def cache_is_valid(video_id: str) -> bool:
    path = cache_path(video_id)
    if not path.is_file():
        return False

    try:
        stat = path.stat()
    except OSError:
        return False

    if stat.st_size < 1024:
        return False

    return (time.time() - stat.st_mtime) < CACHE_TTL_SECONDS


def ensure_cache_dir() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def cleanup_cache() -> None:
    ensure_cache_dir()
    now = time.time()
    files: list[tuple[float, Path]] = []

    for path in CACHE_DIR.glob("*.m4a.mp4"):
        try:
            stat = path.stat()
        except OSError:
            continue

        if (now - stat.st_mtime) >= CACHE_TTL_SECONDS:
            try:
                path.unlink()
            except OSError:
                pass
            continue

        files.append((stat.st_mtime, path))

    files.sort(key=lambda item: item[0], reverse=True)
    for _, path in files[CACHE_MAX_FILES:]:
        try:
            path.unlink()
        except OSError:
            pass


def client_uses_cookies(client: str) -> bool:
    # Current yt-dlp client metadata marks mweb/web/tv families as cookie-capable.
    # Android is intentionally run as a public fast lane so it can avoid the
    # logged-in mweb JS-player path.
    return client not in {"android", "android_vr", "ios", "visionos"}


def build_pipeline(video_id: str, client: str) -> str:
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    cookie_file = get_writable_cookie_file() if client_uses_cookies(client) else None

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
        "--js-runtimes", JSC_RUNTIME,
        "--extractor-args", youtube_extractor_args(client),
    ]

    if cookie_file:
        ytdlp.extend(["--cookies", cookie_file])

    ytdlp.extend(["-f", SOURCE_FORMAT, watch_url])

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


def classify_startup_phase(line: str) -> str | None:
    lower = line.lower()
    if "downloading webpage" in lower:
        return "webpage"
    if "client config" in lower:
        return "client_config"
    if "player api json" in lower:
        return "player_api"
    if "solving js challenges" in lower:
        return "js_challenge"
    if "generating a " in lower and "po token" in lower:
        return "pot_request"
    if "downloading 1 format(s)" in lower:
        return "format_selected"
    if "http error 403" in lower:
        return "http_403"
    if "error:" in lower:
        return "error"
    return None


async def drain_stderr(
    process: asyncio.subprocess.Process,
    tail: list[str],
    video_id: str,
    client: str,
    purpose: str,
    attempt_started: float,
) -> None:
    if process.stderr is None:
        return

    seen_phases: set[str] = set()

    while True:
        line = await process.stderr.readline()
        if not line:
            break

        text = line.decode("utf-8", errors="replace").rstrip()
        if not text:
            continue

        tail.append(text)
        if len(tail) > 180:
            del tail[:-180]

        if "sleeping" in text.lower() or "wait" in text.lower():
            print(
                "cold yt-dlp wait",
                json.dumps({
                    "videoId": video_id,
                    "purpose": purpose,
                    "client": client,
                    "elapsedSeconds": round(time.monotonic() - attempt_started, 2),
                    "message": text[:600],
                }),
                flush=True,
            )

        phase = classify_startup_phase(text)
        if phase and phase not in seen_phases:
            seen_phases.add(phase)
            print(
                "cold startup phase",
                json.dumps({
                    "videoId": video_id,
                    "purpose": purpose,
                    "client": client,
                    "phase": phase,
                    "elapsedSeconds": round(time.monotonic() - attempt_started, 2),
                }),
                flush=True,
            )


async def terminate_process(process: asyncio.subprocess.Process, stderr_task: asyncio.Task | None) -> None:
    """Terminate the whole yt-dlp | ffmpeg pipeline, not just its bash wrapper."""
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception:
            try:
                process.terminate()
            except ProcessLookupError:
                pass

        try:
            await asyncio.wait_for(process.wait(), timeout=1.25)
        except asyncio.TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(process.wait(), timeout=1.25)
            except Exception:
                pass

    if stderr_task:
        try:
            await asyncio.wait_for(stderr_task, timeout=0.5)
        except BaseException:
            stderr_task.cancel()


async def start_attempt(video_id: str, purpose: str, client: str, attempt_number: int):
    pipeline = build_pipeline(video_id, client)
    env = os.environ.copy()
    attempt_started = time.monotonic()

    print(
        "cold playback attempt",
        json.dumps({
            "videoId": video_id,
            "purpose": purpose,
            "attempt": attempt_number,
            "client": client,
            "sourceFormat": SOURCE_FORMAT,
            "extractorArgs": youtube_extractor_args(client),
            "potHttpReady": pot_http_server_ready(),
        }),
        flush=True,
    )

    process = await asyncio.create_subprocess_exec(
        "bash", "-o", "pipefail", "-c", pipeline,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        start_new_session=True,
    )

    stderr_tail: list[str] = []
    stderr_task = asyncio.create_task(
        drain_stderr(
            process,
            stderr_tail,
            video_id,
            client,
            purpose,
            attempt_started,
        )
    )
    return process, stderr_task, stderr_tail, attempt_started


async def read_first_chunk(process: asyncio.subprocess.Process, timeout_seconds: float) -> bytes:
    assert process.stdout is not None

    if timeout_seconds > 0:
        return await asyncio.wait_for(
            process.stdout.read(64 * 1024),
            timeout=timeout_seconds,
        )

    return await process.stdout.read(64 * 1024)


async def read_until_ready(
    process: asyncio.subprocess.Process,
    minimum_bytes: int,
) -> bytes:
    """Buffer enough output to prove the candidate is carrying real media.

    ffmpeg can emit a tiny fragmented-MP4 header before yt-dlp has actually
    downloaded playable media. Waiting for a larger threshold prevents the
    foreground race from choosing a client that only emitted an MP4 header and
    then failed with a media-origin error.
    """
    assert process.stdout is not None
    data = bytearray()

    while len(data) < minimum_bytes:
        chunk = await process.stdout.read(min(64 * 1024, minimum_bytes - len(data)))
        if not chunk:
            break
        data.extend(chunk)

    return bytes(data)


async def open_foreground_race(video_id: str):
    """Race a JS-free public client against the known-good mweb path.

    This is foreground-only. The key point is that mweb starts at the same time,
    so a failed or slow Android attempt cannot add a serial penalty like V19 did.
    """
    clients = []
    for client in (FOREGROUND_FAST_CLIENT, PRIMARY_CLIENT):
        if client and client not in clients:
            clients.append(client)

    started_total = time.monotonic()
    candidates = []

    for index, client in enumerate(clients, start=1):
        process, stderr_task, stderr_tail, attempt_started = await start_attempt(
            video_id,
            "live-race",
            client,
            index,
        )
        read_task = asyncio.create_task(
            read_until_ready(process, FOREGROUND_READY_BYTES)
        )
        candidates.append({
            "client": client,
            "process": process,
            "stderrTask": stderr_task,
            "stderrTail": stderr_tail,
            "attemptStarted": attempt_started,
            "readTask": read_task,
        })

    pending = {item["readTask"] for item in candidates}

    try:
        while pending:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in done:
                candidate = next(item for item in candidates if item["readTask"] is task)
                client = candidate["client"]

                try:
                    buffered = task.result()
                except Exception as exc:
                    buffered = b""
                    print(
                        "foreground race candidate read error",
                        json.dumps({
                            "videoId": video_id,
                            "client": client,
                            "error": str(exc)[:800],
                        }),
                        flush=True,
                    )

                if len(buffered) >= FOREGROUND_READY_BYTES:
                    print(
                        "foreground race winner",
                        json.dumps({
                            "videoId": video_id,
                            "client": client,
                            "bufferedBytes": len(buffered),
                            "elapsedSeconds": round(time.monotonic() - started_total, 2),
                        }),
                        flush=True,
                    )

                    for other in candidates:
                        if other is candidate:
                            continue
                        if not other["readTask"].done():
                            other["readTask"].cancel()
                        # Do not wait for loser teardown before letting the winner
                        # continue. Cleanup happens independently.
                        asyncio.create_task(
                            terminate_process(other["process"], other["stderrTask"])
                        )

                    return (
                        candidate["process"],
                        candidate["stderrTask"],
                        candidate["stderrTail"],
                        buffered,
                        client,
                        started_total,
                    )

                print(
                    "foreground race candidate failed",
                    json.dumps({
                        "videoId": video_id,
                        "client": client,
                        "bufferedBytes": len(buffered),
                        "returnCode": candidate["process"].returncode,
                        "elapsedSeconds": round(time.monotonic() - candidate["attemptStarted"], 2),
                        "stderrTail": candidate["stderrTail"][-12:],
                    }),
                    flush=True,
                )
                asyncio.create_task(
                    terminate_process(candidate["process"], candidate["stderrTask"])
                )

        raise HTTPException(status_code=502, detail="No foreground YouTube client produced playable media")
    except asyncio.CancelledError:
        for candidate in candidates:
            if not candidate["readTask"].done():
                candidate["readTask"].cancel()
            asyncio.create_task(
                terminate_process(candidate["process"], candidate["stderrTask"])
            )
        raise


async def open_stream_with_fallback(video_id: str, purpose: str):
    plans = client_plan()
    last_tail: list[str] = []
    total_started = time.monotonic()

    for index, client in enumerate(plans):
        process, stderr_task, stderr_tail, attempt_started = await start_attempt(
            video_id,
            purpose,
            client,
            index + 1,
        )

        timeout_seconds = (
            FAST_CLIENT_TIMEOUT_SECONDS
            if index == 0 and len(plans) > 1
            else STREAM_START_TIMEOUT_SECONDS
        )

        try:
            first_chunk = await read_first_chunk(process, timeout_seconds)
        except asyncio.CancelledError:
            print(
                "cold playback attempt cancelled",
                json.dumps({
                    "videoId": video_id,
                    "purpose": purpose,
                    "client": client,
                    "elapsedSeconds": round(time.monotonic() - attempt_started, 2),
                }),
                flush=True,
            )
            await terminate_process(process, stderr_task)
            raise
        except asyncio.TimeoutError:
            last_tail = list(stderr_tail)
            print(
                "cold playback attempt timeout",
                json.dumps({
                    "videoId": video_id,
                    "purpose": purpose,
                    "client": client,
                    "elapsedSeconds": round(time.monotonic() - attempt_started, 2),
                    "timeoutSeconds": timeout_seconds,
                    "fallingBack": index + 1 < len(plans),
                    "stderrTail": stderr_tail[-15:],
                }),
                flush=True,
            )
            await terminate_process(process, stderr_task)
            continue

        if first_chunk:
            print(
                "cold first media bytes",
                json.dumps({
                    "videoId": video_id,
                    "purpose": purpose,
                    "client": client,
                    "bytes": len(first_chunk),
                    "attemptElapsedSeconds": round(time.monotonic() - attempt_started, 2),
                    "totalColdElapsedSeconds": round(time.monotonic() - total_started, 2),
                    "contentType": STREAM_CONTENT_TYPE,
                }),
                flush=True,
            )
            return process, stderr_task, stderr_tail, first_chunk, client, total_started

        try:
            await asyncio.wait_for(process.wait(), timeout=1)
        except asyncio.TimeoutError:
            pass

        last_tail = list(stderr_tail)
        print(
            "cold playback attempt failed",
            json.dumps({
                "videoId": video_id,
                "purpose": purpose,
                "client": client,
                "returnCode": process.returncode,
                "elapsedSeconds": round(time.monotonic() - attempt_started, 2),
                "fallingBack": index + 1 < len(plans),
                "stderrTail": stderr_tail[-30:],
            }),
            flush=True,
        )
        await terminate_process(process, stderr_task)

    print(
        "cold playback all clients failed",
        json.dumps({
            "videoId": video_id,
            "purpose": purpose,
            "clients": plans,
            "totalColdElapsedSeconds": round(time.monotonic() - total_started, 2),
            "stderrTail": last_tail[-30:],
        }),
        flush=True,
    )
    raise HTTPException(status_code=502, detail="No YouTube playback client produced media bytes")


async def build_cache(video_id: str) -> bool:
    if cache_is_valid(video_id):
        return True

    ensure_cache_dir()
    final_path = cache_path(video_id)
    temp_path = CACHE_DIR / f".{video_id}.{uuid.uuid4().hex}.part"
    started = time.monotonic()

    async with _prefetch_semaphore:
        _prefetch_started.add(video_id)
        if cache_is_valid(video_id):
            _prefetch_started.discard(video_id)
            return True

        process = None
        stderr_task = None
        total = 0
        success = False
        client = None
        stderr_tail: list[str] = []

        try:
            (
                process,
                stderr_task,
                stderr_tail,
                first_chunk,
                client,
                _,
            ) = await open_stream_with_fallback(video_id, "prefetch")

            assert process.stdout is not None
            with open(temp_path, "wb") as handle:
                handle.write(first_chunk)
                total += len(first_chunk)

                while True:
                    chunk = await process.stdout.read(FILE_CHUNK_BYTES)
                    if not chunk:
                        break
                    handle.write(chunk)
                    total += len(chunk)

            await process.wait()

            if process.returncode == 0 and total >= 1024:
                os.replace(temp_path, final_path)
                success = True
                cleanup_cache()

            print(
                "audio prefetch finished",
                json.dumps({
                    "videoId": video_id,
                    "ok": success,
                    "client": client,
                    "bytes": total,
                    "elapsedSeconds": round(time.monotonic() - started, 2),
                    "returnCode": process.returncode,
                    "stderrTail": [] if success else stderr_tail[-20:],
                }),
                flush=True,
            )

            return success
        finally:
            if not success:
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            if process is not None:
                await terminate_process(process, stderr_task)
            _prefetch_started.discard(video_id)


async def build_cache_foreground(video_id: str) -> bool:
    """Build the selected track immediately with the known-good mweb path."""
    if cache_is_valid(video_id):
        return True

    ensure_cache_dir()
    final_path = cache_path(video_id)
    temp_path = CACHE_DIR / f".{video_id}.{uuid.uuid4().hex}.live.part"
    started = time.monotonic()
    process = None
    stderr_task = None
    total = 0
    success = False
    client = None
    stderr_tail: list[str] = []

    try:
        (
            process,
            stderr_task,
            stderr_tail,
            first_chunk,
            client,
            _,
        ) = await open_stream_with_fallback(video_id, "live")

        assert process.stdout is not None
        with open(temp_path, "wb") as handle:
            handle.write(first_chunk)
            total += len(first_chunk)

            while True:
                chunk = await process.stdout.read(FILE_CHUNK_BYTES)
                if not chunk:
                    break
                handle.write(chunk)
                total += len(chunk)

        await process.wait()

        if process.returncode == 0 and total >= 1024:
            os.replace(temp_path, final_path)
            success = True
            cleanup_cache()

        print(
            "foreground cache build finished",
            json.dumps({
                "videoId": video_id,
                "ok": success,
                "client": client,
                "bytes": total,
                "elapsedSeconds": round(time.monotonic() - started, 2),
                "returnCode": process.returncode,
                "stderrTail": [] if success else stderr_tail[-20:],
            }),
            flush=True,
        )
        return success
    finally:
        if not success:
            try:
                temp_path.unlink()
            except OSError:
                pass
        if process is not None:
            await terminate_process(process, stderr_task)


def _foreground_task_finished(video_id: str, task: asyncio.Task) -> None:
    current = _foreground_tasks.get(video_id)
    if current is task:
        _foreground_tasks.pop(video_id, None)
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        print(
            "foreground cache task error",
            json.dumps({"videoId": video_id, "error": str(exc)[:1200]}),
            flush=True,
        )


def start_foreground_build(video_id: str) -> asyncio.Task:
    existing = _foreground_tasks.get(video_id)
    if existing and not existing.done():
        return existing
    task = asyncio.create_task(build_cache_foreground(video_id))
    _foreground_tasks[video_id] = task
    task.add_done_callback(lambda done: _foreground_task_finished(video_id, done))
    return task


def _task_finished(video_id: str, task: asyncio.Task) -> None:
    current = _cache_tasks.get(video_id)
    if current is task:
        _cache_tasks.pop(video_id, None)

    try:
        task.result()
    except asyncio.CancelledError:
        print(
            "audio prefetch task cancelled",
            json.dumps({"videoId": video_id}),
            flush=True,
        )
    except Exception as exc:
        print(
            "audio prefetch task error",
            json.dumps({"videoId": video_id, "error": str(exc)[:1200]}),
            flush=True,
        )


def start_prefetch(video_id: str) -> tuple[str, asyncio.Task | None]:
    if cache_is_valid(video_id):
        return "cached", None

    existing = _cache_tasks.get(video_id)
    if existing and not existing.done():
        return "warming", existing

    task = asyncio.create_task(build_cache(video_id))
    _cache_tasks[video_id] = task
    task.add_done_callback(lambda done: _task_finished(video_id, done))
    return "warming", task


async def file_body(path: Path, start: int, length: int) -> AsyncIterator[bytes]:
    remaining = length
    with open(path, "rb") as handle:
        handle.seek(start)
        while remaining > 0:
            chunk = handle.read(min(FILE_CHUNK_BYTES, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
            await asyncio.sleep(0)


def parse_range(range_header: str | None, size: int) -> tuple[int, int] | None:
    if not range_header:
        return None

    match = RANGE_RE.fullmatch(range_header.strip())
    if not match:
        raise HTTPException(
            status_code=416,
            detail="Unsupported Range header",
            headers={"Content-Range": f"bytes */{size}"},
        )

    start_text, end_text = match.groups()

    if not start_text and not end_text:
        raise HTTPException(status_code=416, detail="Invalid byte range")

    if start_text:
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    else:
        suffix = int(end_text)
        if suffix <= 0:
            raise HTTPException(status_code=416, detail="Invalid suffix range")
        start = max(0, size - suffix)
        end = size - 1

    if start >= size or start < 0:
        raise HTTPException(
            status_code=416,
            detail="Range outside cached file",
            headers={"Content-Range": f"bytes */{size}"},
        )

    end = min(end, size - 1)
    if end < start:
        raise HTTPException(status_code=416, detail="Invalid byte range")

    return start, end


def cached_headers(path: Path) -> dict[str, str]:
    stat = path.stat()
    return {
        "Content-Type": STREAM_CONTENT_TYPE,
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=3600",
        "ETag": f'"{stat.st_size:x}-{int(stat.st_mtime):x}"',
        "X-Veeb-Resolver": "yt-dlp-ffmpeg-cache-v21",
        "X-Veeb-Cache": "HIT",
        "X-Veeb-Source-Format": SOURCE_FORMAT,
    }


async def serve_cached(path: Path, request: Request):
    size = path.stat().st_size
    headers = cached_headers(path)
    parsed = parse_range(request.headers.get("Range"), size)

    if parsed is None:
        headers["Content-Length"] = str(size)
        if request.method == "HEAD":
            return Response(status_code=200, headers=headers)
        return StreamingResponse(
            file_body(path, 0, size),
            status_code=200,
            headers=headers,
        )

    start, end = parsed
    length = end - start + 1
    headers["Content-Length"] = str(length)
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    if request.method == "HEAD":
        return Response(status_code=206, headers=headers)

    return StreamingResponse(
        file_body(path, start, length),
        status_code=206,
        headers=headers,
    )


async def open_live_stream(video_id: str):
    return await open_stream_with_fallback(video_id, "live")


async def live_stream_and_cache(
    process: asyncio.subprocess.Process,
    first_chunk: bytes,
    video_id: str,
    client: str,
) -> AsyncIterator[bytes]:
    assert process.stdout is not None
    ensure_cache_dir()
    final_path = cache_path(video_id)
    temp_path = CACHE_DIR / f".{video_id}.{uuid.uuid4().hex}.live.part"
    total = 0
    started = time.monotonic()
    completed = False

    try:
        with open(temp_path, "wb") as handle:
            handle.write(first_chunk)
            total += len(first_chunk)
            yield first_chunk

            while True:
                chunk = await process.stdout.read(FILE_CHUNK_BYTES)
                if not chunk:
                    break
                handle.write(chunk)
                total += len(chunk)
                yield chunk

        await process.wait()

        if process.returncode == 0 and total >= 1024:
            os.replace(temp_path, final_path)
            completed = True
            cleanup_cache()

        print(
            "audio-only stream finished",
            json.dumps({
                "videoId": video_id,
                "client": client,
                "bytesSent": total,
                "elapsedSeconds": round(time.monotonic() - started, 2),
                "returnCode": process.returncode,
                "cached": completed,
            }),
            flush=True,
        )
    finally:
        if not completed:
            try:
                temp_path.unlink()
            except OSError:
                pass


@app.get("/health")
async def health() -> dict[str, Any]:
    try:
        pot_version = importlib.metadata.version("bgutil-ytdlp-pot-provider")
    except importlib.metadata.PackageNotFoundError:
        pot_version = None

    try:
        ytdlp_version = importlib.metadata.version("yt-dlp")
    except importlib.metadata.PackageNotFoundError:
        ytdlp_version = None

    get_writable_cookie_file()
    ensure_cache_dir()
    cleanup_cache()

    cached_files = list(CACHE_DIR.glob("*.m4a.mp4"))

    return {
        "ok": True,
        "service": "veeb-youtube-resolver-v22",
        "secretConfigured": bool(RESOLVER_SECRET),
        "youtubeClients": client_plan(),
        "youtubeClient": PRIMARY_CLIENT,
        "jsRuntime": JSC_RUNTIME,
        "foregroundFastClient": None,
        "foregroundReliableClient": PRIMARY_CLIENT,
        "foregroundReadyBytes": FOREGROUND_READY_BYTES,
        "foregroundRace": False,
        "playerSkip": ["configs"],
        "manifestSkip": ["hls", "dash"],
        "playbackWaitSeconds": PLAYBACK_WAIT_SECONDS,
        "streamStartTimeoutSeconds": STREAM_START_TIMEOUT_SECONDS,
        "premiumAccountMode": YOUTUBE_PREMIUM_ACCOUNT,
        "mwebAdPlaybackContext": not YOUTUBE_PREMIUM_ACCOUNT,
        "fetchPotPolicy": "auto",
        "ytDlpVersion": ytdlp_version,
        "poTokenProvider": "bgutil",
        "poTokenProviderVersion": pot_version,
        "poTokenHttpServerReady": pot_http_server_ready(),
        "cookieFilePresent": os.path.isfile(YOUTUBE_COOKIE_FILE),
        "writableCookieFilePresent": os.path.isfile(WRITABLE_COOKIE_FILE),
        "sourceFormat": SOURCE_FORMAT,
        "streamContentType": STREAM_CONTENT_TYPE,
        "streamTransport": "foreground-priority-mweb-zero-wait-cache-first",
        "audioCodec": "aac-copy",
        "cacheDir": str(CACHE_DIR),
        "cacheTtlSeconds": CACHE_TTL_SECONDS,
        "cacheMaxFiles": CACHE_MAX_FILES,
        "cachedTracks": len(cached_files),
        "prefetchConcurrency": PREFETCH_CONCURRENCY,
        "activePrefetches": len([task for task in _cache_tasks.values() if not task.done()]),
        "rangeSeeking": True,
    }


@app.post("/prefetch/{video_id}")
async def prefetch_endpoint(
    video_id: str,
    authorization: str | None = Header(default=None),
):
    require_auth(authorization)
    validate_video_id(video_id)

    status, _ = start_prefetch(video_id)
    return JSONResponse(
        {"ok": True, "videoId": video_id, "status": status},
        status_code=200 if status == "cached" else 202,
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

    if cache_is_valid(video_id):
        print("audio cache hit", json.dumps({"videoId": video_id}), flush=True)
        return await serve_cached(cache_path(video_id), request)

    active = _cache_tasks.get(video_id)
    if active and not active.done() and video_id in _prefetch_started:
        # This exact track is already genuinely running, so reuse the work.
        print("audio reusing active prefetch", json.dumps({"videoId": video_id}), flush=True)
        try:
            await active
        except Exception:
            pass

        if cache_is_valid(video_id):
            print("audio cache hit after active prefetch", json.dumps({"videoId": video_id}), flush=True)
            return await serve_cached(cache_path(video_id), request)

    elif active and not active.done():
        # The task exists but is only queued behind another speculative job.
        # Canceling it is cheap because no yt-dlp subprocess has started yet.
        print("audio promoting queued prefetch to foreground", json.dumps({"videoId": video_id}), flush=True)
        active.cancel()

    if request.method == "HEAD":
        return Response(
            status_code=200,
            headers={
                "Content-Type": STREAM_CONTENT_TYPE,
                "Accept-Ranges": "bytes",
                "Cache-Control": "no-store",
                "X-Veeb-Resolver": "yt-dlp-cache-first-v21",
                "X-Veeb-Cache": "MISS",
            },
        )

    # Foreground owns the Render CPU. Cancel every unrelated speculative task
    # and yield once so cancellation reaches terminate_process(), which now
    # kills the entire yt-dlp/ffmpeg process group rather than only bash.
    preempted = []
    for other_id, other_task in list(_cache_tasks.items()):
        if other_id != video_id and not other_task.done():
            preempted.append(other_id)
            other_task.cancel()

    if preempted:
        print(
            "audio preempting speculative work",
            json.dumps({"videoId": video_id, "preempted": sorted(preempted)}),
            flush=True,
        )
        await asyncio.sleep(0)

    task = start_foreground_build(video_id)
    print(
        "audio foreground build started",
        json.dumps({
            "videoId": video_id,
            "preemptedSpeculativePrefetches": sorted(preempted),
            "client": PRIMARY_CLIENT,
            "playbackWaitSeconds": PLAYBACK_WAIT_SECONDS,
        }),
        flush=True,
    )

    try:
        await task
    except asyncio.CancelledError:
        raise HTTPException(status_code=503, detail="Playback preparation was cancelled")
    except Exception as exc:
        print(
            "audio foreground build error",
            json.dumps({"videoId": video_id, "error": str(exc)[:1200]}),
            flush=True,
        )

    if not cache_is_valid(video_id):
        raise HTTPException(status_code=502, detail="Audio preparation completed without a playable cache file")

    print(
        "audio cold cache ready for playback",
        json.dumps({
            "videoId": video_id,
            "bytes": cache_path(video_id).stat().st_size,
        }),
        flush=True,
    )
    return await serve_cached(cache_path(video_id), request)
