"""Explicit owner/session leases for the opt-in native observation prototype.

owner must be supplied by trusted authentication code, never a request's
unverified owner field. No transcript guessing or cross-owner warm reuse.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import json
import time

from .runtime import NativeSession, WireError


@dataclass
class Entry:
    session: object
    configuration: str
    busy: bool = True
    touched: float = 0


class NativeSessions:
    def __init__(self, *, factory=NativeSession, limit=4, ttl=300):
        self.factory, self.limit, self.ttl = factory, limit, ttl
        self.entries = {}
        self.lock = asyncio.Lock()
        self.closed = False

    @staticmethod
    def key(owner, session_id):
        if not isinstance(owner, str) or not owner:
            raise WireError("Authenticated owner is required")
        if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 512:
            raise WireError("Explicit session id is required")
        return hashlib.sha256(json.dumps([owner, session_id]).encode()).hexdigest()

    @asynccontextmanager
    async def lease(self, owner, session_id, **configuration):
        key = self.key(owner, session_id)
        signature = json.dumps(configuration, sort_keys=True, separators=(",", ":"))
        fresh = False
        async with self.lock:
            if self.closed:
                raise WireError("Session registry is closed")
            now = time.monotonic()
            for old_key, old in list(self.entries.items()):
                if not old.busy and (old.session.closed or now-old.touched > self.ttl):
                    await old.session.close()
                    del self.entries[old_key]
            entry = self.entries.get(key)
            if entry and entry.busy:
                raise WireError("This client session already has an active turn")
            if entry and entry.configuration != signature:
                await entry.session.close()
                del self.entries[key]
                entry = None
            if entry is None:
                if len(self.entries) >= self.limit:
                    raise WireError("Native session capacity reached")
                entry = Entry(self.factory(**configuration), signature)
                self.entries[key] = entry
                fresh = True
            entry.busy = True
        successful = False
        try:
            if fresh:
                await entry.session.start()
            yield entry.session
            successful = True
        finally:
            # A failed/cancelled lease is not reusable, even if the generator
            # was abandoned without aclose. Only this owner/key is removed.
            async with self.lock:
                turn_lock = getattr(entry.session, 'lock', None)
                if not successful or entry.session.closed or (turn_lock is not None and turn_lock.locked()):
                    await entry.session.close()
                    if self.entries.get(key) is entry:
                        del self.entries[key]
                else:
                    entry.busy = False
                    entry.touched = time.monotonic()

    async def close(self):
        async with self.lock:
            self.closed = True
            await asyncio.gather(*(entry.session.close() for entry in self.entries.values()))
            self.entries.clear()
