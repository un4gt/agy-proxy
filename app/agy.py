"""Shared agy CLI helpers and model discovery.

Chat traffic runs through the fixed-SHA 1.4.2 native bridge (``app.bridge``).
This module only spawns short-lived CLI commands for login, account setup,
updates and ``agy models``; it does not own a duplex stream-json process.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import time

from .account_pool import AccountPoolError
from .config import Config

log = logging.getLogger("agy_proxy.agy")

_SLUG_RE = re.compile(r"\b[a-z0-9]+(?:-[a-z0-9._]+)+\b")


class AgyError(Exception):
    """Error that should surface to the API client with an HTTP status."""

    def __init__(self, message: str, *, status_code: int = 502, detail: str | None = None,
                 code: str | None = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail
        self.code = code or {400: "invalid_request", 401: "authentication_error", 404: "not_found",
                             409: "session_busy", 429: "rate_limit_exceeded", 504: "upstream_timeout"}.get(
                                 status_code, "cli_protocol_error")
        self.retryable = retryable


def _agy_argv(cfg: Config, extra: list[str]) -> list[str]:
    # AGY_BIN may be a bare binary name or a full command line ("python fake.py").
    posix = os.name != "nt"
    parts = shlex.split(cfg.agy_bin, posix=posix)
    if not parts:
        raise AgyError("AGY_PROXY_BIN is empty", status_code=500)
    return parts + extra


def _child_env(cfg: Config) -> dict[str, str]:
    """Isolate native state and force file credentials for managed accounts."""
    env = os.environ.copy()
    # The image pins one CLI build; a background auto-update can silently
    # change the bridge binary between requests. 1.4.2 checks the value is
    # exactly "true" (four bytes), not "1". Explicit `agy update` still works
    # with this flag, but the bridge disables it at the HTTP layer.
    env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
    if cfg.http_proxy:
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env[name] = cfg.http_proxy
    if cfg.account_home:
        root = os.fspath(cfg.account_home)
        for name in ("HOME", "USERPROFILE"):
            env[name] = root
    return env


# -- model discovery --------------------------------------------------------

# cache keyed by agy_bin: (timestamp, slugs, error message or None)
_model_caches: dict[object, tuple[float, list[str], str | None]] = {}
# cache keyed by agy_bin: slug -> human label from `agy models`
_model_labels: dict[object, dict[str, str]] = {}
_MODELS_NEGATIVE_TTL = 60.0

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_SPINNER_CHARS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _clean_models_error(raw: bytes) -> str:
    """Extract the human-readable error from stderr noise (ANSI codes, spinner)."""
    text = _ANSI_RE.sub("", raw.decode("utf-8", errors="replace")).replace("\r", "\n")
    lines = []
    for line in text.split("\n"):
        line = line.strip(_SPINNER_CHARS).strip()
        if line and "fetching" not in line.lower():
            lines.append(line)
    return " ".join(lines[-2:])[:300]


async def list_models(cfg: Config, *, force: bool = False, owner: str | None = None,
                      model: str | None = None) -> list[str]:
    """Run `agy models` and cache the slug list (failures cached briefly).

    A previously-successful list past its TTL is served immediately while a
    refresh runs in the background, so the periodic `agy models` round-trip
    never blocks chat requests.
    """
    cfg = _discovery_config(cfg, owner=owner, model=model)
    cache_key = _models_key(cfg)
    now = time.monotonic()
    cached = _model_caches.get(cache_key)
    if cached is not None and not force:
        ts, slugs, error = cached
        if now - ts < (cfg.models_ttl if error is None else _MODELS_NEGATIVE_TTL):
            if error is not None:
                raise AgyError(error, status_code=502)
            return slugs
        if slugs:
            _spawn_models_refresh(cfg)
            return slugs

    argv = _agy_argv(cfg, ["models"])
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_child_env(cfg),
            cwd=cfg.agy_cwd or None,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=45)
    except (FileNotFoundError, PermissionError) as exc:
        raise AgyError(f"failed to start agy binary ({cfg.agy_bin!r}): {exc}", status_code=503) from exc
    except asyncio.TimeoutError:
        proc.kill()
        raise AgyError("`agy models` timed out", status_code=504) from None

    text = out.decode("utf-8", errors="replace")
    slugs: list[str] = []
    labels: dict[str, str] = {}
    for line in text.splitlines():
        # `agy models` prints "<slug>\t<Human Label>"; remember the label so the
        # dashboard can show a friendly name next to the slug.
        fields = [f.strip() for f in line.split("\t") if f.strip()]
        if len(fields) >= 2 and _SLUG_RE.fullmatch(fields[0]):
            labels[fields[0]] = fields[1]
    for token in _SLUG_RE.findall(text):
        if token not in slugs and (any(c.isdigit() for c in token) or token.startswith(("gemini", "auto"))):
            slugs.append(token)

    if not slugs:
        message = _clean_models_error(err) or f"`agy models` returned no models (rc={proc.returncode})"
        log.warning("`agy models` failed (rc=%s): %.300s", proc.returncode, message)
        _model_caches[cache_key] = (now, [], message)
        raise AgyError(message, status_code=502)

    _model_caches[cache_key] = (now, slugs, None)
    _model_labels[cache_key] = labels
    return slugs


def model_labels(cfg: Config) -> dict[str, str]:
    """slug -> human label, empty until a successful `agy models` run."""
    return dict(_model_labels.get(_models_key(_discovery_config(cfg)), {}))


def _discovery_config(cfg: Config, *, owner: str | None = None, model: str | None = None) -> Config:
    if cfg.account_pool and cfg.account_pool.enabled and not cfg.account_home:
        try:
            profile = cfg.account_pool.select(owner, cfg.model_map.get(model, model) or cfg.default_model,
                                              require_model=False)
            cfg.account_pool.prepare(profile)
        except (AccountPoolError, OSError) as exc:
            raise AgyError(str(exc), status_code=503) from None
        return profile.configured(cfg)
    return cfg


def _models_key(cfg: Config):
    return (cfg.agy_bin, cfg.account_home) if cfg.account_home else cfg.agy_bin


def _spawn_models_refresh(cfg: Config) -> None:
    """Refresh the models cache in the background (deduplicated per config)."""
    tasks = getattr(_spawn_models_refresh, "_tasks", {})
    key = _models_key(cfg)
    if key in tasks and not tasks[key].done():
        return
    try:
        task = asyncio.create_task(list_models(cfg, force=True))
    except RuntimeError:  # no running loop (shouldn't happen inside a request)
        return
    tasks = {k: t for k, t in tasks.items() if not t.done()}
    tasks[key] = task
    _spawn_models_refresh._tasks = tasks


def resolve_model(requested: str | None, cfg: Config, slugs: list[str]) -> str | None:
    """Map a client-requested model name onto an agy slug (or None for agy default)."""
    if not requested or requested.lower() in ("default", "(default)"):
        return cfg.default_model
    if requested in cfg.model_map:
        return cfg.model_map[requested]
    if requested in slugs:
        return requested
    if slugs:
        norm = requested.lower()
        matches = [s for s in slugs if norm in s or s in norm]
        if matches:
            return sorted(matches, key=len)[0]
    return cfg.default_model if cfg.default_model else requested
