"""Credential-scoped session hints and server-managed transcript continuity."""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from . import json_codec as json
from .agy import AgyError
from .auth import extract_key


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps_text(value, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class SessionIdentity:
    owner: str
    key: str | None = None


def client_session_key(request, body: dict | None = None) -> SessionIdentity:
    # This contract is client-independent. Adapters may expose the same stable
    # conversation ID under their own header/metadata convention.
    names = ("x-agy-session-id", "x-session-id", "session-id", "session_id", "x-session-affinity",
             "x-opencode-session")
    values = [request.headers[name] for name in names if name in request.headers]
    metadata = (body or {}).get("metadata")
    if isinstance(metadata, dict):
        if "session_id" in metadata:
            values.append(metadata["session_id"])
        # Claude Code 2.1.x embeds JSON with an explicit session_id in user_id.
        # A plain user_id is an identity shared by multiple chats, not a session.
        user = metadata.get("user_id")
        if isinstance(user, str) and user.startswith("{"):
            try:
                user = json.loads(user)
            except json.JSONDecodeError:
                user = None
            if isinstance(user, dict) and "session_id" in user:
                values.append(user["session_id"])
    client_metadata = (body or {}).get("client_metadata")
    if isinstance(client_metadata, dict):
        for name in ("session_id", "thread_id"):
            if name in client_metadata:
                values.append(client_metadata[name])
    source = request.headers.get("x-agy-session-source")
    if source is not None:
        if source != "prompt-cache-key":
            raise AgyError("X-Agy-Session-Source only supports prompt-cache-key", status_code=400)
        value = (body or {}).get("prompt_cache_key")
        if not isinstance(value, str):
            raise AgyError("Session affinity requires a per-session prompt_cache_key", status_code=400)
        values.append(value)
    owner = fingerprint(extract_key(request) or "anonymous")
    if not values:
        # Most stateless API clients echo the transcript, but no session header.
        # The manager can recognize a unique continuation within this owner.
        return SessionIdentity(owner)
    if any(not isinstance(value, str) or not value.strip() or len(value) > 512 for value in values):
        raise AgyError("Session ID must be a nonempty string of at most 512 characters", status_code=400)
    if len(set(values)) != 1:
        raise AgyError("Conflicting session identity headers", status_code=400)
    return SessionIdentity(owner, fingerprint([owner, values[0]]))


@dataclass(frozen=True)
class SessionContext:
    key: str
    configuration: str
    messages: tuple[str, ...]
    owner: str | None = None
    automatic: bool = False

    @classmethod
    def build(cls, key: SessionIdentity | str | None, configuration, messages) -> SessionContext | None:
        if key is None:
            return None
        config = fingerprint(configuration)
        history = tuple(fingerprint(m) for m in messages)
        if isinstance(key, SessionIdentity):
            return cls(key.key or uuid.uuid4().hex, config, history,
                       owner=key.owner, automatic=key.key is None)
        return cls(key, config, history)
