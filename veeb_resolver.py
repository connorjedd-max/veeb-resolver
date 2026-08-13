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
LIVE_PREBUFFER_BYTES = max(64 * 1024, int(os.environ.get("VEEB_LIVE_PREBUFFER_BYTES", str(192 * 1024))))
PLAYBACK_WAIT_SECONDS = max(0.0, float(os.environ.get("YOUTUBE_PLAYBACK_WAIT", "0")))

# V24 uses one authenticated mweb pipeline only. Racing extra clients on the
# small Render instance made the known-good path materially slower.
YTDLP_CACHE_DIR = Path(os.environ.get("YTDLP_CACHE_DIR", "/tmp/veeb-yt-dlp-cache"))

_cache_tasks: dict[str, asyncio.Task] = {}
_prefetch_started: set[str] = set()
_prefetch_started_at: dict[str, float] = {}
_process_cookie_files: dict[int, str] = {}
_foreground_tasks: dict[str, asyncio.Task] = {}
_build_states: dict[str, "ProgressiveBuildState"] = {}
# Exactly one yt-dlp/ffmpeg pipeline at a time. On the free Render CPU,
# concurrent challenge solving increased cold-start latency rather than helping.
_youtube_pipeline_lock = asyncio.Lock()
_prefetch_semaphore = asyncio.Semaphore(PREFETCH_CONCURRENCY)

YTDLP_CACHE_DIR.mkdir(parents=True, exist_ok=True)


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

    if client in {"mweb", "web_music"} and not YOUTUBE_PREMIUM_ACCOUNT:
        args.append("use_ad_playback_context=true")

    args.append("player_skip=configs")
    args.append("skip=hls,dash")
    args.append(f"playback_wait={PLAYBACK_WAIT_SECONDS:g}")

    return "youtube:" + ";".join(args)


def format_selector_for_client(client: str) -> str:
    return SOURCE_FORMAT


def make_attempt_cookie_file(client: str) -> str | None:
    if not client_uses_cookies(client):
        return None
    return get_writable_cookie_file()


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


def build_pipeline(video_id: str, client: str, cookie_file: str | None = None) -> str:
    watch_url = f"https://www.youtube.com/watch?v={video_id}"

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
        "--no-check-formats",
        "--cache-dir", str(YTDLP_CACHE_DIR),
        "--js-runtimes", JSC_RUNTIME,
        "--extractor-args", youtube_extractor_args(client),
    ]

    if cookie_file:
        ytdlp.extend(["--cookies", cookie_file])

    ytdlp.extend(["-f", format_selector_for_client(client), watch_url])

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

    cookie_path = _process_cookie_files.pop(process.pid, None)
    if cookie_path:
        try:
            os.unlink(cookie_path)
        except OSError:
            pass


async def start_attempt(video_id: str, purpose: str, client: str, attempt_number: int):
    attempt_cookie_file = make_attempt_cookie_file(client)
    pipeline = build_pipeline(video_id, client, attempt_cookie_file)
    env = os.environ.copy()
    attempt_started = time.monotonic()

    print(
        "cold playback attempt",
        json.dumps({
            "videoId": video_id,
            "purpose": purpose,
            "attempt": attempt_number,
            "client": client,
            "sourceFormat": format_selector_for_client(client),
            "extractorArgs": youtube_extractor_args(client),
            "potHttpReady": pot_http_server_ready(),
        }),
        flush=True,
    )

    try:
        process = await asyncio.create_subprocess_exec(
            "bash", "-o", "pipefail", "-c", pipeline,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
    except Exception:
        if attempt_cookie_file:
            try:
                os.unlink(attempt_cookie_file)
            except OSError:
                pass
        raise

    if attempt_cookie_file and attempt_cookie_file != WRITABLE_COOKIE_FILE:
        _process_cookie_files[process.pid] = attempt_cookie_file

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


async def open_foreground_stream(video_id: str):
    """Open the known-good mweb stream with no competing client."""
    return await open_stream_with_fallback(video_id, "live")


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


class ProgressiveBuildState:
    def __init__(self, video_id: str, purpose: str):
        ensure_cache_dir()
        self.video_id = video_id
        self.purpose = purpose
        self.temp_path = CACHE_DIR / f".{video_id}.{uuid.uuid4().hex}.progressive.part"
        self.started = time.monotonic()
        self.bytes_written = 0
        self.client: str | None = None
        self.success = False
        self.error: str | None = None
        self.ready = asyncio.Event()
        self.done = asyncio.Event()
        self.condition = asyncio.Condition()
        self.task: asyncio.Task | None = None


async def _notify_build_state(state: ProgressiveBuildState) -> None:
    async with state.condition:
        state.condition.notify_all()


async def build_progressive_cache(state: ProgressiveBuildState) -> bool:
    """Build one track while exposing a growing, already-buffered file.

    V24 waited for the entire song before the browser received a byte. Logs
    showed that this could add another 7-10 seconds after yt-dlp had already
    produced playable media. V25 keeps the reliable cache build independent of
    the browser, but lets playback attach once a substantial fMP4 prebuffer is
    safely on disk.
    """
    video_id = state.video_id
    if cache_is_valid(video_id):
        state.success = True
        state.ready.set()
        state.done.set()
        await _notify_build_state(state)
        return True

    process = None
    stderr_task = None
    stderr_tail: list[str] = []
    total = 0
    success = False

    try:
        async with _youtube_pipeline_lock:
            if cache_is_valid(video_id):
                state.success = True
                state.ready.set()
                state.done.set()
                await _notify_build_state(state)
                return True

            (
                process,
                stderr_task,
                stderr_tail,
                first_chunk,
                client,
                _,
            ) = await open_stream_with_fallback(video_id, state.purpose)
            state.client = client

            assert process.stdout is not None
            with open(state.temp_path, "wb") as handle:
                if first_chunk:
                    handle.write(first_chunk)
                    handle.flush()
                    total += len(first_chunk)
                    state.bytes_written = total
                    if total >= LIVE_PREBUFFER_BYTES:
                        state.ready.set()
                    await _notify_build_state(state)

                while True:
                    chunk = await process.stdout.read(FILE_CHUNK_BYTES)
                    if not chunk:
                        break
                    handle.write(chunk)
                    handle.flush()
                    total += len(chunk)
                    state.bytes_written = total
                    if total >= LIVE_PREBUFFER_BYTES:
                        state.ready.set()
                    await _notify_build_state(state)

            await process.wait()

            if process.returncode == 0 and total >= 1024:
                os.replace(state.temp_path, cache_path(video_id))
                success = True
                state.success = True
                cleanup_cache()
            else:
                state.error = f"pipeline return code {process.returncode}"

            print(
                "progressive cache build finished",
                json.dumps({
                    "videoId": video_id,
                    "purpose": state.purpose,
                    "ok": success,
                    "client": state.client,
                    "bytes": total,
                    "elapsedSeconds": round(time.monotonic() - state.started, 2),
                    "returnCode": process.returncode,
                    "stderrTail": [] if success else stderr_tail[-20:],
                }),
                flush=True,
            )
            return success
    except asyncio.CancelledError:
        state.error = "cancelled"
        raise
    except Exception as exc:
        state.error = str(exc)[:1200]
        print(
            "progressive cache build error",
            json.dumps({"videoId": video_id, "purpose": state.purpose, "error": state.error}),
            flush=True,
        )
        return False
    finally:
        state.bytes_written = total
        state.success = success or state.success
        state.ready.set()
        state.done.set()
        await _notify_build_state(state)
        if not state.success:
            try:
                state.temp_path.unlink()
            except OSError:
                pass
        if process is not None:
            await terminate_process(process, stderr_task)


def _build_state_finished(video_id: str, state: ProgressiveBuildState, task: asyncio.Task) -> None:
    current = _build_states.get(video_id)
    if current is state and state.task is task:
        # Keep the state object reachable until callbacks/streamers have observed
        # completion. Cached requests do not need it after this point.
        _build_states.pop(video_id, None)

    if _cache_tasks.get(video_id) is task:
        _cache_tasks.pop(video_id, None)
    if _foreground_tasks.get(video_id) is task:
        _foreground_tasks.pop(video_id, None)

    _prefetch_started.discard(video_id)
    _prefetch_started_at.pop(video_id, None)

    try:
        task.result()
    except asyncio.CancelledError:
        if state.purpose == "prefetch":
            print("audio prefetch task cancelled", json.dumps({"videoId": video_id}), flush=True)
    except Exception as exc:
        print(
            "audio build task error",
            json.dumps({"videoId": video_id, "purpose": state.purpose, "error": str(exc)[:1200]}),
            flush=True,
        )


def start_progressive_build(video_id: str, purpose: str) -> ProgressiveBuildState:
    existing = _build_states.get(video_id)
    if existing and existing.task and not existing.task.done():
        return existing

    state = ProgressiveBuildState(video_id, purpose)
    task = asyncio.create_task(build_progressive_cache(state))
    state.task = task
    _build_states[video_id] = state
    task.add_done_callback(lambda done: _build_state_finished(video_id, state, done))
    return state


def start_foreground_build(video_id: str) -> asyncio.Task:
    state = start_progressive_build(video_id, "live")
    assert state.task is not None
    _foreground_tasks[video_id] = state.task
    return state.task


def start_prefetch(video_id: str, *, intent: bool = False) -> tuple[str, asyncio.Task | None]:
    if cache_is_valid(video_id):
        return "cached", None

    state = _build_states.get(video_id)
    if state and state.task and not state.task.done():
        return "warming", state.task

    active_other = [
        (other_id, task)
        for other_id, task in _cache_tasks.items()
        if other_id != video_id and not task.done()
    ]

    if active_other:
        if not intent:
            return "busy", None
        for other_id, task in active_other:
            print(
                "audio replacing speculative prefetch for user intent",
                json.dumps({"fromVideoId": other_id, "toVideoId": video_id}),
                flush=True,
            )
            task.cancel()

    _prefetch_started.add(video_id)
    _prefetch_started_at[video_id] = time.monotonic()
    state = start_progressive_build(video_id, "prefetch")
    assert state.task is not None
    _cache_tasks[video_id] = state.task
    return "warming", state.task


async def wait_for_progressive_ready(state: ProgressiveBuildState) -> None:
    if state.ready.is_set():
        return
    await state.ready.wait()


async def progressive_file_body(state: ProgressiveBuildState) -> AsyncIterator[bytes]:
    """Read from the growing temp file without owning/cancelling the build."""
    offset = 0
    handle = None

    try:
        while True:
            if handle is None:
                source = state.temp_path
                if not source.is_file() and cache_path(state.video_id).is_file():
                    source = cache_path(state.video_id)
                if source.is_file():
                    handle = open(source, "rb")
                elif state.done.is_set():
                    break
                else:
                    async with state.condition:
                        await state.condition.wait()
                    continue

            handle.seek(offset)
            chunk = handle.read(FILE_CHUNK_BYTES)
            if chunk:
                offset += len(chunk)
                yield chunk
                await asyncio.sleep(0)
                continue

            if state.done.is_set():
                break

            async with state.condition:
                if state.bytes_written <= offset and not state.done.is_set():
                    await state.condition.wait()
    finally:
        if handle is not None:
            handle.close()


def progressive_headers(state: ProgressiveBuildState) -> dict[str, str]:
    return {
        "Content-Type": STREAM_CONTENT_TYPE,
        "Accept-Ranges": "none",
        "Cache-Control": "no-store",
        "X-Veeb-Resolver": "yt-dlp-progressive-v25",
        "X-Veeb-Cache": "BUILDING",
        "X-Veeb-Progressive": "1",
        "X-Veeb-Source-Format": SOURCE_FORMAT,
        "X-Veeb-Prebuffer-Bytes": str(state.bytes_written),
    }


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
        "X-Veeb-Resolver": "yt-dlp-ffmpeg-cache-v24",
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
        "service": "veeb-youtube-resolver-v25",
        "secretConfigured": bool(RESOLVER_SECRET),
        "youtubeClients": client_plan(),
        "youtubeClient": PRIMARY_CLIENT,
        "jsRuntime": JSC_RUNTIME,
        "foregroundClient": PRIMARY_CLIENT,
        "foregroundRace": False,
        "singleFlightYouTube": True,
        "ytDlpCacheDir": str(YTDLP_CACHE_DIR),
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
        "streamTransport": "mweb-progressive-prebuffer-singleflight",
        "audioCodec": "aac-copy",
        "cacheDir": str(CACHE_DIR),
        "cacheTtlSeconds": CACHE_TTL_SECONDS,
        "cacheMaxFiles": CACHE_MAX_FILES,
        "livePrebufferBytes": LIVE_PREBUFFER_BYTES,
        "cachedTracks": len(cached_files),
        "prefetchConcurrency": PREFETCH_CONCURRENCY,
        "activePrefetches": len([task for task in _cache_tasks.values() if not task.done()]),
        "rangeSeeking": True,
    }


@app.post("/prefetch/{video_id}")
async def prefetch_endpoint(
    video_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    require_auth(authorization)
    validate_video_id(video_id)

    intent = request.query_params.get("intent") in {"1", "true", "yes"}
    status, _ = start_prefetch(video_id, intent=intent)
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

    if request.method == "HEAD":
        state = _build_states.get(video_id)
        return Response(
            status_code=200,
            headers={
                "Content-Type": STREAM_CONTENT_TYPE,
                "Accept-Ranges": "none" if state and not state.done.is_set() else "bytes",
                "Cache-Control": "no-store",
                "X-Veeb-Resolver": "yt-dlp-progressive-v25",
                "X-Veeb-Cache": "BUILDING" if state and not state.done.is_set() else "MISS",
            },
        )

    state = _build_states.get(video_id)
    if state and state.task and not state.task.done():
        print(
            "audio joining active build",
            json.dumps({
                "videoId": video_id,
                "purpose": state.purpose,
                "elapsedSeconds": round(time.monotonic() - state.started, 2),
                "bytesReady": state.bytes_written,
            }),
            flush=True,
        )
    else:
        # Foreground owns the Render CPU. Kill unrelated speculative work first.
        preempted_tasks = []
        preempted_ids = []
        for other_id, other_task in list(_cache_tasks.items()):
            if other_id != video_id and not other_task.done():
                preempted_ids.append(other_id)
                preempted_tasks.append(other_task)
                other_task.cancel()

        if preempted_tasks:
            print(
                "audio preempting speculative work",
                json.dumps({"videoId": video_id, "preempted": sorted(preempted_ids)}),
                flush=True,
            )
            try:
                await asyncio.wait_for(
                    asyncio.gather(*preempted_tasks, return_exceptions=True),
                    timeout=1.25,
                )
            except asyncio.TimeoutError:
                print(
                    "audio prefetch teardown timeout",
                    json.dumps({"videoId": video_id, "preempted": sorted(preempted_ids)}),
                    flush=True,
                )

        start_foreground_build(video_id)
        state = _build_states[video_id]
        print(
            "audio foreground progressive build",
            json.dumps({
                "videoId": video_id,
                "preemptedSpeculativePrefetches": sorted(preempted_ids),
                "client": PRIMARY_CLIENT,
                "livePrebufferBytes": LIVE_PREBUFFER_BYTES,
                "playbackWaitSeconds": PLAYBACK_WAIT_SECONDS,
            }),
            flush=True,
        )

    await wait_for_progressive_ready(state)

    if cache_is_valid(video_id):
        print("audio cache ready before progressive handoff", json.dumps({"videoId": video_id}), flush=True)
        return await serve_cached(cache_path(video_id), request)

    if state.bytes_written < 1024 or state.error:
        if state.task and not state.task.done():
            try:
                await state.task
            except Exception:
                pass
        if cache_is_valid(video_id):
            return await serve_cached(cache_path(video_id), request)
        raise HTTPException(
            status_code=502,
            detail=state.error or "Audio preparation completed without playable media",
        )

    print(
        "audio progressive handoff",
        json.dumps({
            "videoId": video_id,
            "purpose": state.purpose,
            "bytesReady": state.bytes_written,
            "elapsedSeconds": round(time.monotonic() - state.started, 2),
        }),
        flush=True,
    )

    return StreamingResponse(
        progressive_file_body(state),
        status_code=200,
        headers=progressive_headers(state),
    )
