"""Persistent account homes and quota-only, credential-scoped routing.

Each account owns its native CLI files. state.json records a stable UUID that
is materialized as the CLI's installation_id, not an OS hardware identifier.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, replace
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import json_codec as json
from .config import Config

NATIVE_TOKEN = Path(".gemini/antigravity-cli/antigravity-oauth-token")
_ID = re.compile(r"[a-f0-9]{32}\Z")


class AccountPoolError(ValueError):
    pass


def write_private(path: Path, data: bytes) -> None:
    """Replace a private file atomically; never leave a partial credential file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".account-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def native_credentials(data: dict) -> dict:
    """Accept native CLI credentials or a full agy-switch account JSON."""
    if not isinstance(data, dict):
        raise AccountPoolError("凭据必须是 JSON 对象。")
    token = data.get("token", data)
    if not isinstance(token, dict):
        raise AccountPoolError("凭据中的 token 必须是对象。")
    access, refresh = token.get("access_token"), token.get("refresh_token")
    if not any(isinstance(v, str) and v.strip() for v in (access, refresh)):
        raise AccountPoolError("凭据缺少 access_token 或 refresh_token。")
    if any(v is not None and (not isinstance(v, str) or len(v) > 65536) for v in (access, refresh)):
        raise AccountPoolError("凭据字段格式无效。")
    expiry = token.get("expiry")
    if not expiry:
        stamp = token.get("expiry_timestamp")
        if stamp is None and type(token.get("expiry_date")) in (float, int):
            stamp = token["expiry_date"] / 1000
        try:
            expiry = datetime.fromtimestamp(stamp or 0, timezone.utc).isoformat()
        except (ValueError, TypeError, OverflowError, OSError):
            raise AccountPoolError("凭据过期时间无效。") from None
    if not isinstance(expiry, str):
        raise AccountPoolError("凭据过期时间无效。")
    result = {"token": {"access_token": access or "", "refresh_token": refresh or "",
                        "token_type": "Bearer", "expiry": expiry},
              "auth_method": data.get("auth_method", "consumer")}
    if result["auth_method"] != "consumer":
        raise AccountPoolError("多账号隔离目前支持 consumer OAuth 凭据。")
    id_token = data.get("id_token") or token.get("id_token")
    if isinstance(id_token, str):
        result["id_token"] = id_token
    return result


def quota_scope(model: str | None) -> str:
    name = (model or "").lower()
    if name.startswith("gemini"):
        return "gemini"
    if name.startswith(("claude", "gpt")):
        return "third-party"
    return name if name and name != "auto" else "*"


def quota_exhausted(message: object) -> bool:
    """A generic 429/RESOURCE_EXHAUSTED may be a rate or capacity limit."""
    if not isinstance(message, str):
        return False
    if re.search(r"per[_ -](?:minute|second)|\b(?:rpm|tpm)\b|rate[_ -]limit|concurren|overload|capacity", message, re.I):
        return False
    return bool(re.search(
        r"\bquota[_ -](?:exhausted|exceeded)\b|"
        r"\b(?:exhausted|exceeded|used up|run out of)\b[^\n.]{0,60}\b(?:quota|credits)\b|"
        r"\bquota\b[^\n.]{0,60}\b(?:exhausted|exceeded|depleted)\b|"
        r"\b(?:weekly|daily|five.hour|5.hour)\b[^\n.]{0,40}\blimit\b[^\n.]{0,20}\b(?:reached|exceeded)\b|"
        r"额度(?:已)?(?:耗尽|用尽)|配额(?:已)?(?:耗尽|用尽)", message, re.I))


@dataclass(frozen=True)
class AccountProfile:
    id: str
    name: str
    home: Path

    @property
    def credentials(self) -> Path:
        return self.home / NATIVE_TOKEN

    def configured(self, cfg: Config) -> Config:
        return replace(cfg, account_home=str(self.home), account_id=self.id)


class AccountPool:
    def __init__(self, cfg: Config):
        self.root = (Path(cfg.data_dir).expanduser() if cfg.data_dir else Path.home() / ".agy-proxy").resolve()
        self.path = self.root / "accounts.json"
        self._lock = threading.RLock()
        self._removing: set[str] = set()
        self.data = {"version": 1, "accounts": [], "bindings": {}, "exhausted": {}}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_bytes())
                if (data.get("version") != 1 or not isinstance(data.get("accounts"), list)
                        or not isinstance(data.get("bindings"), dict) or not isinstance(data.get("exhausted"), dict)
                        or type(data.get("managed", False)) is not bool):
                    raise ValueError()
                for entry in data["accounts"]:
                    if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                            or not _ID.fullmatch(entry["id"]) or not isinstance(entry.get("name"), str)):
                        raise ValueError()
                ids = {a["id"] for a in data["accounts"]}
                if len(ids) != len(data["accounts"]) or any(v not in ids for v in data["bindings"].values()):
                    raise ValueError()
                for account_id, records in data["exhausted"].items():
                    if account_id not in ids or not isinstance(records, dict):
                        raise ValueError()
                    for record in records.values():
                        if not isinstance(record, dict):
                            raise ValueError()
                        for name in ("observed_at", "until"):
                            value = record.get(name)
                            if value is None and name == "until":
                                continue
                            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                                raise ValueError()
                self.data = data
            except (OSError, ValueError, TypeError, AttributeError):
                # Losing a binding on a corrupt file would silently switch users.
                raise AccountPoolError("无法读取 accounts.json；请恢复账户索引后再启动。") from None
        self._saved = json.dumps(self.data)

    @property
    def enabled(self) -> bool:
        # Removing the last managed account must not reactivate legacy credentials.
        return bool(self.data["accounts"]) or self.data.get("managed", False)

    def _save(self) -> None:
        payload = json.dumps(self.data)
        try:
            write_private(self.path, payload)
        except OSError:
            self.data = json.loads(self._saved)
            raise AccountPoolError("无法保存账号绑定，请检查数据目录权限与磁盘空间。") from None
        self._saved = payload

    def profiles(self) -> list[AccountProfile]:
        with self._lock:
            return [AccountProfile(a["id"], a["name"], self.root / "accounts" / a["id"] / "home")
                    for a in self.data["accounts"]]

    def get(self, account_id: str) -> AccountProfile:
        with self._lock:
            for profile in self.profiles():
                if profile.id == account_id:
                    return profile
        raise AccountPoolError("账号不存在。")

    def create(self, name: str, credentials: dict | None = None, *, machine_id: str | None = None) -> AccountProfile:
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise AccountPoolError("账号名称须为 1–100 个字符。")
        payload = native_credentials(credentials) if credentials is not None else None
        digest = None
        if payload:
            token = payload["token"]
            digest = hashlib.sha256((token["refresh_token"] or token["access_token"]).encode()).hexdigest()
        with self._lock:
            if digest:
                existing = next((a for a in self.data["accounts"] if a.get("credential_key") == digest), None)
                if existing is None:
                    # Login and token refresh are performed by the native CLI,
                    # so the current credential may not be indexed yet.
                    for entry in self.data["accounts"]:
                        try:
                            current = native_credentials(json.loads(self.get(entry["id"]).credentials.read_bytes()))["token"]
                        except (OSError, ValueError, TypeError):
                            continue
                        current_key = hashlib.sha256((current["refresh_token"] or current["access_token"]).encode()).hexdigest()
                        if current_key == digest:
                            existing = entry
                            break
                if existing:
                    # Reimporting does not reset identity, quota state or routing.
                    profile = self.get(existing["id"])
                    self.prepare(profile)
                    write_private(profile.credentials, json.dumps(payload))
                    if existing.get("credential_key") != digest:
                        existing["credential_key"] = digest
                        self._save()
                    return profile
            account_id = uuid.uuid4().hex
            directory = self.root / "accounts" / account_id
            machine_id = str(uuid.UUID(machine_id)) if machine_id else str(uuid.uuid4())
            if any(self._machine_id(p) == machine_id for p in self.profiles()):
                raise AccountPoolError("机器标识已由另一账号使用。")
            profile = AccountProfile(account_id, name.strip(), directory / "home")
            write_private(directory / "state.json", json.dumps({
                "version": 1, "account_id": account_id, "machine_id": machine_id,
            }))
            self.prepare(profile)
            if payload:
                write_private(profile.credentials, json.dumps(payload))
            self.data["accounts"].append({"id": account_id, "name": name.strip(),
                                          "credential_key": digest, "created_at": time.time()})
            self._save()
            return profile

    def _machine_id(self, profile: AccountProfile) -> str:
        try:
            state = json.loads((profile.home.parent / "state.json").read_bytes())
            if state["account_id"] != profile.id or state["version"] != 1:
                raise ValueError()
            return str(uuid.UUID(state["machine_id"]))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise AccountPoolError("账号 state.json 无效；请恢复原机器标识，不能重新生成。") from None

    def rename(self, account_id: str, name: str) -> dict:
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise AccountPoolError("账号名称须为 1–100 个字符。")
        with self._lock:
            self.get(account_id)
            if account_id in self._removing:
                raise AccountPoolError("账号正在删除，请稍后重试。")
            entry = next(a for a in self.data["accounts"] if a["id"] == account_id)
            entry["name"] = name.strip()
            self._save()
            return self.public(account_id)

    @contextmanager
    def removing(self, account_id: str):
        """Block new logins/discovery while idle sessions are being closed."""
        with self._lock:
            self.get(account_id)
            if account_id in self._removing:
                raise AccountPoolError("账号正在删除，请稍后重试。")
            self._removing.add(account_id)
        try:
            yield
        finally:
            with self._lock:
                self._removing.discard(account_id)

    def remove(self, account_id: str) -> dict:
        """Explicit admin deletion releases bindings, unlike automatic failover."""
        with self._lock:
            profile = self.get(account_id)
            directory = profile.home.parent
            if (directory.is_symlink() or not directory.resolve().is_relative_to(self.root)
                    or directory.resolve().parent != (self.root / "accounts").resolve()):
                raise AccountPoolError("账号数据目录无效，无法删除。")
            released = sum(value == account_id for value in self.data["bindings"].values())
            self.data["accounts"] = [a for a in self.data["accounts"] if a["id"] != account_id]
            self.data["bindings"] = {k: v for k, v in self.data["bindings"].items() if v != account_id}
            self.data["exhausted"].pop(account_id, None)
            self.data["managed"] = True
            # Persist first: failed writes must leave credentials and identity intact.
            self._save()
            result = {"deleted": account_id, "released_clients": released}
            try:
                if directory.exists():
                    shutil.rmtree(directory)
            except OSError:
                result["warning"] = "账号已移除，但部分本地数据未能清理，请检查服务器目录权限。"
            return result

    def prepare(self, profile: AccountProfile) -> None:
        """Materialize identity once; keep all other native state account-local."""
        if profile.id in self._removing:
            raise AccountPoolError("账号正在删除，请稍后重试。")
        machine_id = self._machine_id(profile)
        for base in (profile.home / ".gemini", profile.home / ".gemini/antigravity-cli"):
            base.mkdir(parents=True, exist_ok=True, mode=0o700)
            identity = base / "installation_id"
            if identity.exists():
                if identity.read_text(encoding="utf-8").strip() != machine_id:
                    raise AccountPoolError("账号 installation_id 与固定机器标识不一致。")
            else:
                write_private(identity, machine_id.encode())
        native_state = profile.home / ".gemini/state.json"
        if not native_state.exists():
            write_private(native_state, b"{}")
        for sub in (".config", ".cache", ".local/share", ".local/state", ".run", "tmp",
                    "AppData/Roaming", "AppData/Local"):
            (profile.home / sub).mkdir(parents=True, exist_ok=True, mode=0o700)

    def _blocked(self, account_id: str, model: str | None) -> bool:
        records = self.data["exhausted"].get(account_id, {})
        for scope in ("*", quota_scope(model)):
            record = records.get(scope)
            if record and (record.get("until") is None or record["until"] > time.time()):
                return True
        return False

    def select(self, owner: str | None = None, model: str | None = None) -> AccountProfile | None:
        from .agy import AgyError
        with self._lock:
            if not self.enabled:
                return None
            # API key owners are already hashed. Hash again so direct callers
            # cannot accidentally persist a raw key or a session ID.
            key = hashlib.sha256((owner or "default").encode()).hexdigest()
            bound = self.data["bindings"].get(key)
            if bound:
                if bound in self._removing:
                    raise AgyError("绑定账号正在删除，请稍后重试。", status_code=503)
                try:
                    profile = self.get(bound)
                except AccountPoolError:
                    raise AgyError("绑定账号不存在；已停止请求以避免意外切号。", status_code=503) from None
                if not self._blocked(bound, model):
                    if not profile.credentials.is_file():
                        raise AgyError("绑定账号的凭据缺失，请重新登录该账号。", status_code=503)
                    return profile
            profiles = self.profiles()
            if bound:
                index = next(i for i, p in enumerate(profiles) if p.id == bound)
                profiles = profiles[index + 1:] + profiles[:index + 1]
            profile = next((p for p in profiles if p.id not in self._removing
                            and p.credentials.is_file() and not self._blocked(p.id, model)), None)
            if profile is None:
                raise AgyError("没有可用额度的已登录账号，请刷新额度或添加账号。", status_code=429)
            self.data["bindings"][key] = profile.id
            self._save()
            return profile

    def mark_exhausted(self, account_id: str, model: str | None) -> None:
        with self._lock:
            self.get(account_id)
            self.data["exhausted"].setdefault(account_id, {})[quota_scope(model)] = {
                "observed_at": time.time(), "until": None,
            }
            self._save()

    def observe_quota(self, account_id: str, usage: dict | None) -> None:
        """Only fresh, explicit quota numbers can affect routing or clear a failure."""
        if not usage or not isinstance(usage.get("groups"), list):
            return
        observed = usage.get("fetched_at", 0)
        now = time.time()
        if type(observed) not in (int, float) or not math.isfinite(observed) or not 0 <= now - observed < 65:
            return
        grouped: dict[str, list[dict]] = {}
        for group in usage["groups"]:
            description = (group.get("name", "") + " " + group.get("models", "")).lower()
            scopes = []
            if "gemini" in description:
                scopes.append("gemini")
            if any(word in description for word in ("claude", "gpt", "third-party", "third party")):
                scopes.append("third-party")
            for scope in scopes:
                grouped.setdefault(scope, []).extend(group.get("buckets", []))
        with self._lock:
            self.get(account_id)
            records = self.data["exhausted"].setdefault(account_id, {})
            before = json.dumps(records)
            for scope, buckets in grouped.items():
                previous = records.get(scope)
                if previous and observed < previous["observed_at"]:
                    continue
                valid = [b for b in buckets if type(b.get("remaining_fraction")) in (int, float)
                         and math.isfinite(b["remaining_fraction"]) and 0 <= b["remaining_fraction"] <= 1
                         and (b.get("reset_at") is None or b["reset_at"] > now)]
                empty = [b for b in valid if b["remaining_fraction"] == 0]
                if empty:
                    resets = [b.get("reset_at") for b in empty]
                    records[scope] = {"observed_at": observed,
                                      "until": max(resets) if all(r is not None for r in resets) else None}
                elif buckets and len(valid) == len(buckets) and all(b["remaining_fraction"] > 0 for b in valid):
                    records.pop(scope, None)
            # An auto/default-model error has unknown scope. Clear it only when
            # every reported bucket is fresh and positive; never infer recovery
            # from one healthy model group or an empty/unknown quota response.
            wildcard = records.get("*")
            buckets = [b for group in usage["groups"] for b in group.get("buckets", [])]
            if wildcard and observed >= wildcard["observed_at"] and buckets and all(
                type(b.get("remaining_fraction")) in (int, float)
                and math.isfinite(b["remaining_fraction"]) and 0 < b["remaining_fraction"] <= 1
                and (b.get("reset_at") is None or b["reset_at"] > now) for b in buckets
            ):
                records.pop("*", None)
            if before != json.dumps(records):
                self._save()

    def public(self, account_id: str) -> dict:
        with self._lock:
            profile = self.get(account_id)
            return {"id": profile.id, "name": profile.name, "isolated": True,
                    "bound_clients": sum(v == account_id for v in self.data["bindings"].values()),
                    "exhausted_scopes": [scope for scope in self.data["exhausted"].get(account_id, {})
                                         if self._blocked(account_id, scope)]}
