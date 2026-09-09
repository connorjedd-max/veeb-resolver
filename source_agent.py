"""Run in the supplied container on an always-on computer, with no inbound port.

The resolver sends IDs and leases only. No remote commands, URLs, credentials or
cookies are executed. Download and conversion use this same package locally.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from urllib.parse import urlparse

import httpx
from media_jobs import mp3_frames_valid
from mp3_validation import MP3Validator
from source_support import redact

# The agent always acquires locally. It must never recursively broker to itself.
os.environ['VEEB_SOURCE_MODE'] = 'direct'
import veeb_resolver as engine

CACHE_DIR = Path(os.environ.get('VEEB_AGENT_CACHE_DIR', '/data/audio'))
CACHE_LIMIT = max(64, int(os.environ.get('VEEB_AGENT_CACHE_MB', '1024'))) * 1024 * 1024


def cached_audio(video_id, bitrate):
    path = CACHE_DIR / f'{video_id}-{bitrate}.mp3'
    meta_path = path.with_suffix('.json')
    try:
        meta = json.loads(meta_path.read_text())
        if meta['bytes'] != path.stat().st_size or meta['sha256'] != digest_file(path):
            return None
        meta['path'] = str(path)
        os.utime(path, None)
        return meta
    except (OSError, ValueError, KeyError):
        return None


def digest_file(path):
    with open(path, 'rb') as src:
        return hashlib.file_digest(src, 'sha256').hexdigest()


def evict_local_cache(needed):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(CACHE_DIR.glob('*.mp3'), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    for path in files:
        if total + needed <= CACHE_LIMIT:
            break
        total -= path.stat().st_size
        path.unlink(missing_ok=True)
        path.with_suffix('.json').unlink(missing_ok=True)
    if total + needed > CACHE_LIMIT:
        raise RuntimeError('Agent cache limit is too small for this track')


async def acquire_audio(video_id, bitrate):
    engine.validate_video_id(video_id)
    cached = cached_audio(video_id, bitrate)
    if cached:
        return {**cached, 'localCacheHit': True}
    engine.MP3_BITRATE_KBPS = bitrate
    existing = engine._media_jobs.jobs.get(video_id)
    old_bitrate = getattr(existing.metadata.get('media'), '_output_bitrate', bitrate) if existing else bitrate
    if existing and old_bitrate != bitrate:
        if not existing.done.is_set() or existing.readers or existing.waiters:
            raise RuntimeError('An earlier bitrate conversion for this track is still active')
        engine._media_jobs._remove(video_id)
    request = engine.Request({'type': 'http', 'method': 'GET', 'headers': []})
    job = engine._media_jobs.get(video_id, request)
    await engine._media_jobs.wait(job, complete=True)
    evict_local_cache(job.size)
    path = CACHE_DIR / f'{video_id}-{bitrate}.mp3'
    tmp = path.with_suffix('.part')
    shutil.copyfile(job.path, tmp)
    validator = MP3Validator()
    with tmp.open('rb') as source:
        while chunk := source.read(65536):
            validator.feed(chunk)
    duration = validator.finish()
    meta = {'bytes': job.size, 'sha256': digest_file(tmp),
            'duration': duration, 'bitrateKbps': bitrate, 'createdAtUnix': int(time.time())}
    os.replace(tmp, path)
    metadata_path = path.with_suffix('.json')
    metadata_temp = path.with_suffix('.json.part')
    metadata_temp.write_text(json.dumps(meta))
    os.replace(metadata_temp, metadata_path)
    return {**meta, 'path': str(path), 'localCacheHit': False}


async def upload_result(client, claim, audio):
    headers = {'X-Veeb-Lease': claim['leaseToken'], 'X-Veeb-SHA256': audio['sha256'],
               'X-Veeb-Source-Duration': str(audio['duration']),
               'Content-Type': 'audio/mpeg', 'Content-Length': str(audio['bytes'])}
    # Reopen the file for each transfer retry. Never re-download YouTube here.
    for attempt in range(3):
        async def body():
            with open(audio['path'], 'rb') as source:
                while chunk := source.read(65536):
                    yield chunk
        try:
            response = await client.post('/agent/result/' + claim['videoId'], headers=headers, content=body())
            if response.status_code == 409:
                print(json.dumps({'videoId': claim['videoId'], 'status': 'lease_expired_or_already_received',
                                  'audioRetainedLocally': True}), flush=True)
                return
            response.raise_for_status()
            print(json.dumps({'videoId': claim['videoId'], 'status': 'delivered', 'bytes': audio['bytes'],
                              'localCacheHit': audio['localCacheHit']}), flush=True)
            return
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500:
                raise
        except httpx.TransportError:
            pass
        if attempt < 2:
            await asyncio.sleep(2 ** attempt)
    raise RuntimeError('MP3 delivery failed after three transfers; the local copy is retained')


async def heartbeat(client, active):
    while True:
        try:
            claim = active.get('claim')
            headers = {'X-Veeb-Video-Id': claim['videoId'], 'X-Veeb-Lease': claim['leaseToken']} if claim else {}
            response = await client.post('/agent/heartbeat', headers=headers)
            response.raise_for_status()
        except Exception as exc:
            print(json.dumps({'stage': 'heartbeat', 'error': redact(exc, 180)}), flush=True)
        await asyncio.sleep(3)


async def serve():
    endpoint = os.environ.get('VEEB_RESOLVER_URL', '').rstrip('/')
    secret = os.environ.get('VEEB_SOURCE_AGENT_SECRET', '')
    parsed = urlparse(endpoint)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {'', '/'}):
        raise RuntimeError('Set VEEB_RESOLVER_URL to the HTTPS origin of the Render resolver')
    if not secret:
        raise RuntimeError('VEEB_SOURCE_AGENT_SECRET is required')
    async with httpx.AsyncClient(base_url=endpoint, headers={'Authorization': 'Bearer ' + secret},
                                 timeout=60, follow_redirects=False) as client:
        first = await client.post('/agent/heartbeat')
        first.raise_for_status()
        print('Source agent connected. Waiting for requested tracks.', flush=True)
        active = {}
        pulse = asyncio.create_task(heartbeat(client, active))
        try:
            while True:
                claim = None
                try:
                    response = await client.post('/agent/claim')
                    response.raise_for_status()
                    claim = response.json().get('job')
                    if claim:
                        active['claim'] = claim
                        video_id = engine.validate_video_id(claim['videoId'])
                        bitrate = int(claim['bitrateKbps'])
                        if not 96 <= bitrate <= 192:
                            raise RuntimeError('Unexpected output bitrate')
                        audio = await acquire_audio(video_id, bitrate)
                        await upload_result(client, claim, audio)
                except Exception as exc:
                    detail = engine.error_detail(exc)
                    print(json.dumps({'stage': 'source-agent', **detail}), flush=True)
                    if claim:
                        try:
                            await client.post('/agent/failure/' + claim['videoId'], json=detail,
                                              headers={'X-Veeb-Lease': claim['leaseToken']})
                        except Exception:
                            pass
                finally:
                    active.pop('claim', None)
                await asyncio.sleep(2)
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--probe', metavar='VIDEO_ID', help='Download and convert one exact track, then exit; does not contact Render')
    args = parser.parse_args()
    try:
        if args.probe:
            started = time.monotonic()
            try:
                audio = await acquire_audio(args.probe, 128)
                print(json.dumps({'ok': True, 'videoId': args.probe, 'mp3Complete': True,
                                  'bytes': audio['bytes'], 'sha256': audio['sha256'],
                                  'localCacheHit': audio['localCacheHit'],
                                  'elapsedSeconds': round(time.monotonic()-started, 2)}), flush=True)
            except Exception as exc:
                print(json.dumps({'ok': False, 'videoId': args.probe, **engine.error_detail(exc)}), flush=True)
                raise SystemExit(1)
        else:
            await serve()
    finally:
        await engine.shutdown_http_client()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
