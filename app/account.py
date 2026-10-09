"""Panel-only account health and the quota data used by agy's /usage panel.

The Cloud Code endpoint is internal; unknown quota fields stay unknown. OAuth
credentials are read locally, refreshed in memory, and never returned or logged.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .config import Config
from .dashboard import _panel_denied

router = APIRouter()
_HOSTS = ("daily-cloudcode-pa.googleapis.com", "cloudcode-pa.googleapis.com")
# Installed application OAuth client, shared by the CLI (not a user secret).
_CLIENT_ID = ""
_CLIENT_SECRET = ""


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
            fraction = bucket.get("remainingFraction")
            if (
                type(fraction) not in (int, float)
                or not math.isfinite(fraction)
                or not 0 <= fraction <= 1
            ):
                fraction = None
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
    def __init__(self, cfg: Config):
        self.cfg = cfg
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
                raw = credentials_path().read_bytes()
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
            ttl = 60 if self._cache and self._cache["status"] == "connected" else 10
            age = time.monotonic() - self._cached_at
            if self._cache and (
                (not refresh and age < ttl)
                or self._cached_at >= requested_at
                or age < 2
            ):
                return self._cache
            result = {
                "id": "current",
                "email": _email(data),
                "tier": None,
                "status": "error",
                "message": "",
                "checked_at": time.time(),
                "usage": None,
                "usage_error": None,
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
                            info, quota = await self._query(client, access)
                        except AccountError as exc:
                            if exc.state != "unauthorized":
                                raise
                            access = await self._token(client, data, force=True)
                            info, quota = await self._query(client, access)
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
                        "额度数据格式已变化，暂时无法展示。请稍后刷新。"
                    )
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
            response = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "client_id": _CLIENT_ID,
                    "client_secret": _CLIENT_SECRET,
                },
            )
            if response.status_code in (400, 401):
                raise AccountError("unauthorized", "登录已失效，请重新授权。")
            response.raise_for_status()
            fresh = response.json()
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

    async def _query(self, client: httpx.AsyncClient, access: str) -> tuple[dict, dict]:
        headers = {
            "Authorization": f"Bearer {access}",
            "User-Agent": "antigravity-agy-proxy/0.0.13",
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
                quota = await self._post(
                    client,
                    host,
                    "retrieveUserQuotaSummary",
                    {"project": project},
                    headers,
                )
                return info, quota
            except httpx.HTTPStatusError:
                if host == _HOSTS[-1]:
                    raise
        raise AccountError(
            "unavailable", "该账号暂未提供 Antigravity 项目信息，请检查服务权限。"
        )

    @staticmethod
    async def _post(client, host, method, body, headers) -> dict:
        response = await client.post(
            f"https://{host}/v1internal:{method}", json=body, headers=headers
        )
        if response.status_code == 401:
            raise AccountError("unauthorized", "登录已失效，请重新授权。")
        if response.status_code == 403:
            raise AccountError(
                "forbidden", "上游拒绝访问，请检查账号的 Antigravity 使用权限。"
            )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("Invalid response")
        return result


def _service(request: Request) -> AccountService:
    if not hasattr(request.app.state, "account_service"):
        request.app.state.account_service = AccountService(request.app.state.cfg)
    return request.app.state.account_service


@router.get("/api/accounts")
async def accounts(request: Request, refresh: bool = False):
    denied = _panel_denied(request)
    if denied is not None:
        return denied
    snapshot = await _service(request).snapshot(refresh)
    summary = {k: v for k, v in snapshot.items() if k not in ("usage", "usage_error")}
    return JSONResponse({"accounts": [summary]}, headers={"Cache-Control": "no-store"})


@router.get("/api/accounts/{account_id}/usage")
async def account_usage(account_id: str, request: Request, refresh: bool = False):
    denied = _panel_denied(request)
    if denied is not None:
        return denied
    if account_id != "current":
        return JSONResponse({"error": "账号不存在"}, status_code=404)
    return JSONResponse(
        await _service(request).snapshot(refresh), headers={"Cache-Control": "no-store"}
    )
