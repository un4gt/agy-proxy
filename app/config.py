"""Environment-driven configuration for agy-proxy."""

from __future__ import annotations

from . import json_codec as json
import os
from dataclasses import dataclass, field

PREFIX = "AGY_PROXY_"


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(PREFIX + name, default)


def _bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = _env(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


@dataclass
class Config:
    agy_bin: str = "agy"
    host: str = "127.0.0.1"
    port: int = 8317
    password: str | None = None
    password_from_api_keys: bool = False
    api_keys: tuple[str, ...] = ()
    data_dir: str | None = None
    default_model: str | None = None
    model_map: dict[str, str] = field(default_factory=dict)
    max_sessions: int = 8
    session_ttl: float = 1800.0
    turn_timeout: float = 600.0
    recovery_attempts: int = 1
    # CLI stdout omits thinking/upstream activity; silence is not a stalled run.
    idle_timeout: float = 0.0
    stream_heartbeat: float = 10.0
    models_ttl: float = 300.0
    effort: str | None = None
    agent: str | None = None
    skip_permissions: bool = False
    sandbox: bool = False
    http_proxy: str | None = None
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = field(default=None, repr=False)
    agy_cwd: str | None = None
    prewarm_models: tuple[str, ...] = ()
    native_chat: bool = False
    native_input_clean: bool = False
    native_bin: str | None = None
    native_symbols: str = "/evidence/symbols.json"
    # Internal per-child context; never change the proxy's own process home.
    account_home: str | None = field(default=None, repr=False)
    account_id: str | None = None
    account_pool: object | None = field(default=None, repr=False, compare=False)

    @property
    def auth_enabled(self) -> bool:
        return bool(self.password) or bool(self.api_keys)


def load_config() -> Config:
    keys_raw = _env("API_KEYS") or ""
    keys = tuple(k.strip() for k in keys_raw.split(",") if k.strip())

    # The panel password is its own secret. Upgrading deployments that only
    # have AGY_PROXY_API_KEYS keep working: the first key doubles as the panel
    # password (logged as a warning at startup) until AGY_PROXY_PASSWORD is set.
    password = _env("PASSWORD")
    password_from_keys = False
    if not password and keys:
        password = keys[0]
        password_from_keys = True

    map_raw = _env("MODEL_MAP")
    model_map: dict[str, str] = {}
    if map_raw:
        try:
            parsed = json.loads(map_raw)
            if isinstance(parsed, dict):
                model_map = {str(k): str(v) for k, v in parsed.items()}
        except json.JSONDecodeError:
            raise SystemExit("AGY_PROXY_MODEL_MAP is not valid JSON")

    http_proxy = _env("HTTP_PROXY")
    if http_proxy and "://" not in http_proxy:
        http_proxy = "http://" + http_proxy

    prewarm_raw = _env("PREWARM_MODELS") or ""
    prewarm = tuple(m.strip() for m in prewarm_raw.split(",") if m.strip())

    return Config(
        agy_bin=(_env("BIN") or "agy"),
        host=(_env("HOST") or "127.0.0.1"),
        port=_int("PORT", 8317),
        password=password,
        password_from_api_keys=password_from_keys,
        api_keys=keys,
        data_dir=_env("DATA_DIR"),
        default_model=_env("DEFAULT_MODEL"),
        model_map=model_map,
        max_sessions=max(1, _int("MAX_SESSIONS", 8)),
        session_ttl=max(30.0, _float("SESSION_TTL", 1800.0)),
        turn_timeout=max(5.0, _float("TURN_TIMEOUT", 600.0)),
        recovery_attempts=min(1, max(0, _int("RECOVERY_ATTEMPTS", 1))),
        idle_timeout=max(0.0, _float("IDLE_TIMEOUT", 0.0)),
        stream_heartbeat=max(0.1, _float("STREAM_HEARTBEAT", 10.0)),
        models_ttl=max(10.0, _float("MODELS_TTL", 300.0)),
        effort=_env("EFFORT"),
        agent=_env("AGENT"),
        skip_permissions=_bool("SKIP_PERMISSIONS", False),
        sandbox=_bool("SANDBOX", False),
        http_proxy=http_proxy,
        oauth_client_id=_env("OAUTH_CLIENT_ID"),
        oauth_client_secret=_env("OAUTH_CLIENT_SECRET"),
        agy_cwd=_env("CWD"),
        prewarm_models=prewarm,
        native_chat=_bool("NATIVE_CHAT", False),
        native_input_clean=_bool("NATIVE_INPUT_CLEAN", False),
        native_bin=_env("NATIVE_BIN"),
        native_symbols=_env("NATIVE_SYMBOLS") or "/evidence/symbols.json",
    )
