"""Stable public API for agy-proxy-core.

This module is the only surface ``agy-proxy-adapters`` may import. Phase 0
defines the public types; the actual app factory and services arrive with the
core module migration.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class KeyRef:
    """A stable reference to the API key that owns a request."""

    key_id: str | None
    key_name: str
    owner: str


@dataclass(frozen=True)
class Principal:
    """Authenticated identity before session resolution."""

    owner: str
    key_ref: KeyRef
    is_panel_credential: bool = False
    auth_method: str = "api_key"


@dataclass(frozen=True)
class CoreCallContext:
    """Per-request identity passed to the core Gemini service."""

    owner: str
    session_key: str | None
    request_id: str
    protocol: str
    key_ref: KeyRef | None = None


class CoreError(Exception):
    """A protocol-neutral error that HTTP/adapters map to their own shapes."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 500,
        code: str = "internal_error",
        retryable: bool = False,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details


@dataclass(frozen=True)
class CoreServices:
    """The explicit dependency bundle shared with protocol adapters."""

    cfg: Any
    store: Any
    account_pool: Any
    bridge_sessions: Any
    gemini_service: Any
    models: Any = None


def get_services(request: Any) -> CoreServices:
    """Typed accessor for core routes; adapters receive the object explicitly."""
    services = getattr(getattr(request, "app", None), "state", None)
    services = getattr(services, "services", None)
    if not isinstance(services, CoreServices):
        raise RuntimeError("CoreServices is not configured on this application")
    return services


def authenticate(api_key: str | None, headers: Any = None, *, cfg: Any = None,
                 store: Any = None) -> Principal:
    """Validate a downstream credential before reading the request body."""
    from .auth import panel_credential_valid
    from .session_context import fingerprint

    candidate = api_key
    if not candidate and headers is not None:
        for name in ("x-goog-api-key", "x-api-key"):
            value = headers.get(name)
            if value:
                candidate = value.strip()
                break
        if not candidate:
            authorization = headers.get("authorization", "")
            if authorization.lower().startswith("bearer "):
                candidate = authorization[7:].strip()

    if cfg is None or store is None:
        raise RuntimeError("authenticate requires cfg and store")

    is_panel = bool(candidate) and panel_credential_valid(cfg, candidate) if cfg.auth_enabled else False
    record = None
    if cfg.auth_enabled and not is_panel:
        record = store.find_key(candidate)
        if record is None:
            raise CoreError("Invalid API key", status=401, code="invalid_api_key")

    owner = fingerprint(candidate or "anonymous")
    if record is not None:
        key_ref = KeyRef(record.id, record.name, owner)
    elif is_panel:
        key_ref = KeyRef(None, "panel", owner)
    else:
        key_ref = KeyRef(None, "—", owner)
    method = "panel" if is_panel else ("api_key" if record is not None else "anonymous")
    return Principal(owner=owner, key_ref=key_ref, is_panel_credential=is_panel,
                     auth_method=method)


def resolve_context(principal: Principal, headers: Any = None, body: dict | None = None, *,
                    request_id: str = "", protocol: str = "gemini") -> CoreCallContext:
    """Attach session affinity using the same rules as the legacy routes."""
    from .agy import AgyError
    from .session_context import session_identity

    try:
        identity = session_identity(principal.owner, headers or {}, body)
    except AgyError as exc:
        raise CoreError(str(exc), status=exc.status_code, code=exc.code or "invalid_session") from None
    return CoreCallContext(owner=identity.owner, session_key=identity.key,
                           request_id=request_id, protocol=protocol,
                           key_ref=principal.key_ref)


def create_app(cfg: Any = None) -> Any:
    from .app import create_app as _create

    return _create(cfg)


def create_core_only_app(cfg: Any = None) -> Any:
    from .app import create_core_only_app as _create

    return _create(cfg)


__all__ = [
    "CoreCallContext",
    "CoreError",
    "CoreServices",
    "KeyRef",
    "Principal",
    "authenticate",
    "create_app",
    "create_core_only_app",
    "get_services",
    "resolve_context",
]
