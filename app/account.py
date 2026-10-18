"""Panel-only account health and the quota data used by agy's /usage panel.

The Cloud Code endpoint is internal; unknown quota fields stay unknown. OAuth
credentials are read locally, refreshed in memory, and never returned or logged.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from . import json_codec as json
import math
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import APIRouter, Request
from .json_codec import JSONResponse

from .config import Config
from .dashboard import _panel_denied
from .account_pool import AccountPoolError, native_credentials

router = APIRouter()
_HOSTS = ("daily-cloudcode-pa.googleapis.com", "cloudcode-pa.googleapis.com")


def credentials_path() -> Path:
    root = Path.home() / ".gemini"
    candidates = (
        root / "antigravity-cli" / "antigravity-oauth-token",
        root / "jetski-standalone-oauth-token",
        root / "oauth_creds.json",
    )
    return next((p for p in candidates if p.is_file()), candidates[0])


def credentials_present() -> bool:
    try:
        data = json.loads(credentials_path().read_text(encoding="utf-8"))
        token = data.get("token", data)
        return isinstance(token, dict) and bool(token.get("access_token"))
    except (OSError, ValueError, AttributeError):
        return False


def _timestamp(value) -> float | None:
    try:
        if not isinstance(value, str):
            return None
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.timestamp() if dt.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def _email(data: dict) -> str | None:
    # Display metadata only. Authentication always uses the upstream response,
    # never unverified claims from the locally stored ID token.
    try:
        part = data.get("id_token", "").split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        value = claims.get("email")
        return value if isinstance(value, str) and len(value) <= 320 else None
    except (ValueError, IndexError, AttributeError, TypeError):
        return None


class AccountError(Exception):
    def __init__(self, state: str, message: str):
        self.state = state
        super().__init__(message)


def _fraction(value) -> float | None:
    if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1:
        return value
    return None


def normalize_models(raw: dict) -> list[dict]:
    """Model availability comes from the catalog, not quota group descriptions."""
    if not isinstance(raw, dict) or not isinstance(raw.get("models"), dict):
        raise ValueError("Invalid models response")
    # New catalogs include unnamed completion/search helpers. Keep named models
    # and explicitly exposed agent/image/tiered IDs, including unnamed Gemini
    # tier routes; the Recommended menu alone can contain only Claude/GPT.
    exposed = set()
    for sort in raw.get("agentModelSorts", []) or []:
        if isinstance(sort, dict):
            for group in sort.get("groups", []) or []:
                if isinstance(group, dict):
                    exposed.update(value for value in group.get("modelIds", []) if isinstance(value, str))
    exposed.update(value for value in raw.get("imageGenerationModelIds", []) or [] if isinstance(value, str))
    tiers = raw.get("tieredModelIds")
    if isinstance(tiers, dict):
        for ids in tiers.values():
            if isinstance(ids, list):
                exposed.update(value for value in ids if isinstance(value, str))
    has_menu = isinstance(raw.get("agentModelSorts"), list)
    models = []
    for model_id, info in raw["models"].items():
        if not isinstance(model_id, str) or not model_id or not isinstance(info, dict):
            raise ValueError("Invalid model")
        name = info.get("displayName")
        named = isinstance(name, str) and bool(name.strip())
        if has_menu and not named and model_id not in exposed:
            continue
        quota = info.get("quotaInfo")
        quota = quota if isinstance(quota, dict) else {}
        models.append({
            "id": model_id,
            "name": name if named else model_id,
            "remaining_fraction": _fraction(quota.get("remainingFraction")),
            "reset_at": _timestamp(quota.get("resetTime")),
        })
    return sorted(models, key=lambda model: (model["name"].casefold(), model["id"]))


def normalize_quota(raw: dict) -> list[dict]:
    if not isinstance(raw, dict) or not isinstance(raw.get("groups"), list):
        raise ValueError("Invalid quota response")
    groups = []
    for group in raw["groups"]:
        if not isinstance(group, dict) or not isinstance(
            group.get("buckets", []), list
        ):
            raise ValueError("Invalid quota group")
        buckets = []
        for bucket in group.get("buckets", []):
            if not isinstance(bucket, dict):
                raise ValueError("Invalid quota bucket")
            label = bucket.get("displayName") or bucket.get("window") or "额度"
            if not isinstance(label, str):
                raise ValueError("Invalid quota label")
            window = str(bucket.get("window", ""))
            kind = (
                "weekly"
                if "week" in (window + label).lower()
                else (
                    "5h"
                    if window == "5h"
                    or any(s in label.lower() for s in ("five hour", "5 hour"))
                    else window
                )
            )
            fraction = _fraction(bucket.get("remainingFraction"))
            buckets.append(
                {
                    "kind": kind,
                    "label": label,
                    "remaining_fraction": fraction,
                    "reset_at": _timestamp(bucket.get("resetTime")),
                }
            )
        name = group.get("displayName", "Models")
        description = group.get("description", "")
        if not isinstance(name, str) or not isinstance(description, str):
            raise ValueError("Invalid quota description")
        groups.append(
            {
                "name": name,
                "models": description.removeprefix("Models within this group:").strip(),
                "buckets": buckets,
            }
        )
    return groups


class AccountService:
    def __init__(self, cfg: Config, path: Path | None = None, account_id: str = "current"):
        self.cfg = cfg
        self.path = path
        self.account_id = account_id
        self._lock = asyncio.Lock()
        self._cache: dict | None = None
        self._cached_at = 0.0
        self._fingerprint = ""
        self._access_token: str | None = None
        self._expires_at = 0.0

    async def snapshot(self, refresh: bool = False) -> dict:
        requested_at = time.monotonic()
        async with self._lock:
            data = {}
            error = None
            try:
                raw = (self.path or credentials_path()).read_bytes()
                fingerprint = hashlib.sha256(raw).hexdigest()
                data = json.loads(raw)
                if not isinstance(data, dict) or not isinstance(
                    data.get("token", data), dict
                ):
                    raise ValueError()
            except FileNotFoundError:
                fingerprint = "missing"
                error = AccountError("disconnected", "尚未连接账号，请先完成登录。")
            except (OSError, ValueError):
                fingerprint = "invalid"
                error = AccountError("error", "无法读取账号凭据，请检查登录状态。")
            if fingerprint != self._fingerprint:
                self._cache = None
                self._access_token = None
                self._expires_at = 0
                self._fingerprint = fingerprint
            # Concurrent refreshes share one request; failures have a short TTL.
            ttl = 60 if self._cache and self._cache["status"] == "connected" and not (
                self._cache.get("usage_error") or self._cache.get("models_error")
            ) else 10
            age = time.monotonic() - self._cached_at
            if self._cache and (
                (not refresh and age < ttl)
                or self._cached_at >= requested_at
                or age < 2
            ):
                return self._cache
            result = {
                "id": self.account_id,
                "email": _email(data),
                "tier": None,
                "status": "error",
                "message": "",
                "checked_at": time.time(),
                "usage": None,
                "usage_error": None,
                "models": None,
                "models_error": None,
            }
            try:
                if error:
                    raise error
                async with asyncio.timeout(25):
                    async with httpx.AsyncClient(
                        proxy=self.cfg.http_proxy or None, timeout=8
                    ) as client:
                        access = await self._token(client, data)
                        try:
                            info, quota, models = await self._query(client, access)
                        except AccountError as exc:
                            if exc.state != "unauthorized":
                                raise
                            access = await self._token(client, data, force=True)
                            info, quota, models = await self._query(client, access)
                result.update(
                    status="connected",
                    message="已通过上游账号验证",
                    checked_at=time.time(),
                )
                tier = info.get("currentTier") or {}
                if isinstance(tier, dict):
                    tier_name = tier.get("name") or tier.get("id")
                    result["tier"] = tier_name if isinstance(tier_name, str) else None
                    if not result["email"]:
                        uri = tier.get("upgradeSubscriptionUri")
                        if isinstance(uri, str):
                            result["email"] = parse_qs(urlparse(uri).query).get(
                                "Email", [None]
                            )[0]
                try:
                    result["usage"] = {
                        "groups": normalize_quota(quota),
                        "fetched_at": result["checked_at"],
                    }
                except (ValueError, TypeError):
                    result["usage_error"] = (
                        "暂时无法获取共享额度，请稍后刷新。"
                    )
                try:
                    result["models"] = normalize_models(models)
                except (ValueError, TypeError):
                    result["models_error"] = "暂时无法获取账号可用模型，请稍后刷新。"
            except AccountError as exc:
                result.update(status=exc.state, message=str(exc))
            except (TimeoutError, httpx.TimeoutException):
                result["message"] = "账号查询超时，请稍后重试。"
            except (httpx.HTTPError, ValueError, TypeError):
                result["message"] = "暂时无法查询上游账号，请稍后重试。"
            self._cache, self._cached_at = result, time.monotonic()
            return result

    async def _token(
        self, client: httpx.AsyncClient, data: dict, force: bool = False
    ) -> str:
        token = data.get("token", data)
        if not force and self._access_token and self._expires_at > time.time() + 60:
            return self._access_token
        access = token.get("access_token")
        expiry = _timestamp(token.get("expiry"))
        # Legacy Gemini credentials use an expiry timestamp in milliseconds.
        if expiry is None and type(token.get("expiry_date")) in (int, float):
            expiry = token["expiry_date"] / 1000
        if force or (expiry is not None and expiry < time.time() + 60):
            refresh = token.get("refresh_token")
            if not isinstance(refresh, str) or not refresh:
                raise AccountError("unauthorized", "登录已失效，请重新授权。")
            if not self.cfg.oauth_client_id or not self.cfg.oauth_client_secret:
                raise AccountError(
                    "unauthorized",
                    "账号凭据需要刷新，请配置 OAuth 客户端信息或通过 agy 重新授权。",
                )
            response = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "client_id": self.cfg.oauth_client_id,
                    "client_secret": self.cfg.oauth_client_secret,
                },
            )
            if response.status_code in (400, 401):
                raise AccountError("unauthorized", "登录已失效，请重新授权。")
            response.raise_for_status()
            fresh = json.loads(response.content)
            if not isinstance(fresh, dict):
                raise ValueError("Invalid token response")
            access = fresh.get("access_token")
            duration = fresh.get("expires_in", 3600)
            expiry = time.time() + (
                duration if type(duration) in (int, float) else 3600
            )
        if not isinstance(access, str) or not access:
            raise AccountError("unauthorized", "没有可用登录凭据，请完成授权。")
        self._access_token, self._expires_at = access, expiry or 0
        return access

    async def _query(
        self, client: httpx.AsyncClient, access: str
    ) -> tuple[dict, dict | None, dict | None]:
        headers = {
            "Authorization": f"Bearer {access}",
            "User-Agent": "antigravity-agy-proxy/0.0.14",
        }
        for host in _HOSTS:
            try:
                info = await self._post(
                    client,
                    host,
                    "loadCodeAssist",
                    {"metadata": {"ideType": "ANTIGRAVITY"}},
                    headers,
                )
                project = info.get("cloudaicompanionProject")
                if not isinstance(project, str) or not project:
                    continue
                quota, models = await asyncio.gather(
                    self._query_data(client, host, "retrieveUserQuotaSummary", project, headers),
                    self._query_data(client, host, "fetchAvailableModels", project, headers),
                )
                return info, quota, models
            except httpx.HTTPStatusError:
                if host == _HOSTS[-1]:
                    raise
        raise AccountError(
            "unavailable", "该账号暂未提供 Antigravity 项目信息，请检查服务权限。"
        )

    async def _query_data(self, client, host, method, project, headers) -> dict | None:
        # Each optional dataset can fail without hiding the other one.
        for endpoint in (host, *(value for value in _HOSTS if value != host)):
            try:
                return await self._post(client, endpoint, method, {"project": project}, headers)
            except AccountError as exc:
                if exc.state == "unauthorized":
                    raise
            except (httpx.HTTPError, ValueError, TypeError):
                pass
        return None

    @staticmethod
    async def _post(client, host, method, body, headers) -> dict:
        response = await client.post(
            f"https://{host}/v1internal:{method}", content=json.dumps(body), headers={**headers, "Content-Type": "application/json"}
        )
        if response.status_code == 401:
            raise AccountError("unauthorized", "登录已失效，请重新授权。")
        if response.status_code == 403:
            raise AccountError(
                "forbidden", "上游拒绝访问，请检查账号的 Antigravity 使用权限。"
            )
        response.raise_for_status()
        result = json.loads(response.content)
        if not isinstance(result, dict):
            raise ValueError("Invalid response")
        return result


def _service(request: Request, account_id: str = "current") -> AccountService:
    if account_id != "current":
        pool = request.app.state.account_pool
        profile = pool.get(account_id)
        services = getattr(request.app.state, "account_services", None)
        if services is None:
            services = request.app.state.account_services = {}
        if account_id not in services:
            services[account_id] = AccountService(request.app.state.cfg, profile.credentials, account_id)
        return services[account_id]
    if not hasattr(request.app.state, "account_service"):
        request.app.state.account_service = AccountService(request.app.state.cfg)
    return request.app.state.account_service


async def _snapshot(request: Request, account_id: str, refresh: bool) -> dict:
    result = dict(await _service(request, account_id).snapshot(refresh))
    if account_id != "current":
        pool = request.app.state.account_pool
        if result["status"] == "connected":
            pool.observe_quota(account_id, result.get("usage"))
        result.update(pool.public(account_id))
    return result


@router.get("/api/accounts")
async def accounts(request: Request, refresh: bool = False):
    denied = _panel_denied(request)
    if denied is not None:
        return denied
    pool = request.app.state.account_pool
    ids = [p.id for p in pool.profiles()] if pool.enabled else ["current"]
    async def snapshot_if_present(account_id):
        try:
            return await _snapshot(request, account_id, refresh)
        except AccountPoolError:
            # A concurrent admin deletion can finish while quota queries await.
            if account_id not in {p.id for p in pool.profiles()}:
                return None
            raise
    snapshots = await asyncio.gather(*[snapshot_if_present(account_id) for account_id in ids])
    snapshots = [snapshot for snapshot in snapshots if snapshot is not None]
    summaries = [{k: v for k, v in snapshot.items() if k not in ("usage", "usage_error", "models", "models_error")} for snapshot in snapshots]
    return JSONResponse({"accounts": summaries}, headers={"Cache-Control": "no-store"})


@router.post("/api/accounts")
async def add_account(request: Request):
    denied = _panel_denied(request)
    if denied is not None:
        return denied
    try:
        raw = await request.body()
        if len(raw) > 256 * 1024:
            raise AccountPoolError("凭据文件超过 256 KiB。")
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise AccountPoolError("请求须为 JSON 对象。")
        name = body.get("name", "Antigravity 账号")
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise AccountPoolError("账号名称无效。")
        credentials = body.get("credentials")
        if credentials is not None:
            credentials = native_credentials(credentials)
        machine_id = None
        pool = request.app.state.account_pool
        source = credentials_path()
        native_source = source.name in ("antigravity-oauth-token", "jetski-standalone-oauth-token")
        installation = Path.home() / ".gemini/antigravity-cli/installation_id"
        if body.get("import_current"):
            if not native_source:
                return JSONResponse({"error": "未找到 agy 原生文件凭据；系统凭据库登录请先导出该账号，再通过凭据文件导入。"}, status_code=400)
            credentials = json.loads(source.read_bytes())
            if installation.is_file():
                machine_id = installation.read_text(encoding="utf-8").strip()
        elif not pool.enabled and native_source and source.is_file():
            # Adding B must not replace the existing A. Snapshot A before the
            # first managed account is added; native credential files stay put.
            pool.create("原服务账号", json.loads(source.read_bytes()),
                        machine_id=installation.read_text(encoding="utf-8").strip() if installation.is_file() else None)
        elif not pool.enabled and any(manager.live_count for manager in (
            request.app.state.sessions, getattr(request.app.state, "native_sessions", None)
        ) if manager is not None):
            return JSONResponse({"error": "原服务仍有会话，但未找到可迁移的文件凭据。请先结束原会话，再优先导入原账号凭据。"}, status_code=409)
        profile = pool.create(name, credentials, machine_id=machine_id)
    except (OSError, ValueError, TypeError):
        # Do not echo parsing errors or credential values back to the browser.
        return JSONResponse({"error": "无法添加账号，请检查名称、凭据 JSON 或本机登录文件。"}, status_code=400)
    return JSONResponse(request.app.state.account_pool.public(profile.id), status_code=201,
                        headers={"Cache-Control": "no-store"})


@router.patch("/api/accounts/{account_id}")
async def edit_account(account_id: str, request: Request):
    denied = _panel_denied(request)
    if denied is not None:
        return denied
    pool = request.app.state.account_pool
    try:
        pool.get(account_id)
    except AccountPoolError:
        return JSONResponse({"error": "账号不存在，请先添加或导入隔离账号。"}, status_code=404)
    try:
        raw = await request.body()
        if len(raw) > 4096:
            raise AccountPoolError("请求过大。")
        body = json.loads(raw)
        if not isinstance(body, dict) or set(body) != {"name"}:
            raise AccountPoolError("仅支持修改账号名称。")
        result = pool.rename(account_id, body["name"])
    except (ValueError, TypeError):
        return JSONResponse({"error": "保存失败，请检查名称（1–100 个字符）及数据目录写入权限。"}, status_code=400)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.delete("/api/accounts/{account_id}")
async def delete_account(account_id: str, request: Request):
    from .agy import AgyError

    denied = _panel_denied(request)
    if denied is not None:
        return denied
    pool = request.app.state.account_pool
    try:
        pool.get(account_id)
    except AccountPoolError:
        return JSONResponse({"error": "账号不存在，请先添加或导入隔离账号。"}, status_code=404)
    bridges = getattr(request.app.state, "account_login_bridges", None)
    if bridges is None:
        bridges = request.app.state.account_login_bridges = {}
    try:
        native = getattr(request.app.state, "native_sessions", None)
        result = await request.app.state.sessions.remove_account(
            account_id, login_bridges=bridges, other_managers=(native,) if native else ())
    except AgyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
    except (AccountPoolError, OSError):
        return JSONResponse({"error": "删除失败，请检查账号状态及数据目录写入权限。"}, status_code=409)
    bridges.pop(account_id, None)
    getattr(request.app.state, "account_services", {}).pop(account_id, None)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.get("/api/accounts/{account_id}/usage")
async def account_usage(account_id: str, request: Request, refresh: bool = False):
    denied = _panel_denied(request)
    if denied is not None:
        return denied
    pool = request.app.state.account_pool
    if account_id == "current" and pool.enabled:
        return JSONResponse({"error": "请选择具体的隔离账号"}, status_code=404)
    try:
        snapshot = await _snapshot(request, account_id, refresh)
    except AccountPoolError:
        return JSONResponse({"error": "账号不存在"}, status_code=404)
    return JSONResponse(
        snapshot, headers={"Cache-Control": "no-store"}
    )
