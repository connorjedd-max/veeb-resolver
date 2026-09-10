"""Outbound-only source agents. Leases refer to one existing MediaJobs producer.

In-memory scheduling is deliberately transient. Completed audio is collected by
the existing Worker into R2; an agent retains its own bounded completed-file cache.
"""
import asyncio
from dataclasses import dataclass
import secrets
from pathlib import Path
import shutil
import time
from media_jobs import JobError


@dataclass
class Ticket:
    video_id: str
    future: asyncio.Future
    token: str = ''
    expires: float = 0
    uploading: bool = False


class SourceBroker:
    def __init__(self, clock=time.monotonic, lease_seconds=120):
        self.clock, self.lease_seconds = clock, lease_seconds
        self.tickets = {}
        self.last_seen = None
        self.changed = asyncio.Event()
        self.last_result = None

    def online(self):
        return self.last_seen is not None and self.clock() - self.last_seen < 15

    async def acquire(self, video_id, *, timeout=180):
        # Fail quickly when the configured source computer is unavailable.
        if not self.online():
            raise JobError('SOURCE_AGENT_OFFLINE',
                'The source agent has not polled within 15 seconds. Start its container on the source computer.')
        ticket = Ticket(video_id, asyncio.get_running_loop().create_future())
        self.tickets[video_id] = ticket
        self.changed.set()
        delivered = False
        try:
            result = await asyncio.wait_for(asyncio.shield(ticket.future), timeout)
            delivered = True
            return result
        except asyncio.TimeoutError as exc:
            raise JobError('SOURCE_AGENT_TIMEOUT', 'The source agent did not deliver complete audio in time.') from exc
        finally:
            self.tickets.pop(video_id, None)
            if not ticket.future.done():
                ticket.future.cancel()
            elif not ticket.future.cancelled():
                # Retrieve failures if the producer was cancelled as an upload finished.
                error = ticket.future.exception()
                if not delivered and error is None:
                    result = ticket.future.result()
                    path = getattr(result, '_local_path', None)
                    if path:
                        shutil.rmtree(Path(path).parent, ignore_errors=True)

    def claim(self, bitrate):
        self.last_seen = self.clock()
        for ticket in self.tickets.values():
            if ticket.future.done() or ticket.uploading:
                continue
            if ticket.token and ticket.expires > self.clock():
                continue
            ticket.token = secrets.token_urlsafe(24)
            ticket.expires = self.clock() + self.lease_seconds
            return {'videoId': ticket.video_id, 'leaseToken': ticket.token,
                    'leaseSeconds': self.lease_seconds, 'bitrateKbps': bitrate}
        return None

    def owned(self, video_id, token):
        ticket = self.tickets.get(video_id)
        if (ticket is None or not token or not secrets.compare_digest(ticket.token, token)
                or ticket.expires <= self.clock() or ticket.future.done()):
            return None
        return ticket

    def fail(self, ticket, code, message):
        self.last_result = {'ok': False, 'videoId': ticket.video_id, 'code': code, 'checkedAtUnix': int(time.time())}
        ticket.future.set_exception(JobError(code, message))

    def status(self):
        return {'online': self.online(), 'pending': sum(not t.future.done() for t in self.tickets.values()),
                'lastPollSecondsAgo': None if self.last_seen is None else round(self.clock()-self.last_seen, 1),
                'lastResult': self.last_result}
