"""Account checks never access real credentials or upstream services."""

import asyncio
import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import create_app
from app import account
from app.config import Config


QUOTA = {
    "groups": [
        {
            "displayName": "Gemini Models",
            "description": "Models within this group: Gemini Flash, Gemini Pro",
            "buckets": [
                {
                    "displayName": "Weekly Limit Remaining",
                    "window": "weekly",
                    "remainingFraction": 0.9806,
                    "resetTime": "2030-01-02T03:04:05Z",
                },
                {
                    "displayName": "Five Hour Limit Remaining",
                    "window": "5h",
                    "remainingFraction": 1,
                },
            ],
        }
    ]
}


@pytest.fixture
def credential(tmp_path, monkeypatch):
    path = tmp_path / "antigravity-oauth-token"
    payload = (
        base64.urlsafe_b64encode(json.dumps({"email": "demo@example.com"}).encode())
        .decode()
        .rstrip("=")
    )
    path.write_text(
        json.dumps(
            {
                "token": {
                    "access_token": "PRIVATE_ACCESS",
                    "refresh_token": "PRIVATE_REFRESH",
                    "expiry": "2030-01-01T00:00:00Z",
                },
                "id_token": f"header.{payload}.signature",
            }
        )
    )
    monkeypatch.setattr(account, "credentials_path", lambda: path)
    return path


def mocked(monkeypatch, handler):
    cls = httpx.AsyncClient
    monkeypatch.setattr(
        account.httpx,
        "AsyncClient",
        lambda **kwargs: cls(transport=httpx.MockTransport(handler), **kwargs),
    )


def ok(request):
    if request.url.path.endswith("loadCodeAssist"):
        assert "antigravity" in request.headers["user-agent"]
        return httpx.Response(
            200,
            json={
                "cloudaicompanionProject": "PRIVATE_PROJECT",
                "currentTier": {"id": "pro-tier", "name": "Pro"},
            },
        )
    return httpx.Response(200, json=QUOTA)


def test_real_schema_and_panel_auth(credential, monkeypatch, tmp_path):
    mocked(monkeypatch, ok)
    app = create_app(Config(password="panel", data_dir=str(tmp_path / "data")))
    generated = asyncio.run(app.state.store.create_key("Client only"))
    client = TestClient(app)
    for path in ("/api/accounts", "/api/accounts/current/usage"):
        assert client.get(path).status_code == 401
        assert (
            client.get(
                path, headers={"Authorization": f"Bearer {generated.key}"}
            ).status_code
            == 401
        )
    assert client.post("/signin", json={"key": "panel"}).status_code == 200
    listing = client.get("/api/accounts")
    assert listing.headers["cache-control"] == "no-store"
    record = listing.json()["accounts"][0]
    assert record["email"] == "demo@example.com"
    assert record["status"] == "connected"
    assert "usage" not in record
    detail = client.get("/api/accounts/current/usage")
    buckets = detail.json()["usage"]["groups"][0]["buckets"]
    assert buckets[0]["remaining_fraction"] == 0.9806
    assert buckets[0]["reset_at"] == 1893553445
    assert buckets[1]["kind"] == "5h"
    assert "PRIVATE" not in detail.text
    assert client.get("/api/accounts/other/usage").status_code == 404
    assert client.get("/health").json()["signed_in"] is True


def test_refresh_expired_token_in_memory(credential, monkeypatch):
    original = json.loads(credential.read_text())
    original["token"]["expiry"] = "2020-01-01T00:00:00Z"
    credential.write_text(json.dumps(original))
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.host == "oauth2.googleapis.com":
            assert b"PRIVATE_REFRESH" in request.content
            return httpx.Response(
                200, json={"access_token": "FRESH_ACCESS", "expires_in": 3600}
            )
        assert request.headers["authorization"] == "Bearer FRESH_ACCESS"
        return ok(request)

    mocked(monkeypatch, handler)

    async def run():
        service = account.AccountService(Config())
        assert (await service.snapshot())["status"] == "connected"
        service._cached_at -= 61
        assert (await service.snapshot(refresh=True))["status"] == "connected"

    asyncio.run(run())
    assert calls.count("/token") == 1
    assert json.loads(credential.read_text()) == original


@pytest.mark.parametrize(
    "failure,state",
    [
        (401, "unauthorized"),
        (403, "forbidden"),
        (500, "error"),
        ("timeout", "error"),
        ("json", "error"),
    ],
)
def test_failures_are_explicit_and_do_not_leak_credentials(
    credential, monkeypatch, failure, state
):
    def handler(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("PRIVATE_ACCESS", request=request)
        if failure == "json":
            return httpx.Response(200, text="PRIVATE_ACCESS")
        return httpx.Response(failure, json={"error": "PRIVATE_ACCESS"})

    mocked(monkeypatch, handler)
    result = asyncio.run(account.AccountService(Config()).snapshot())
    assert result["status"] == state
    assert result["usage"] is None
    assert "PRIVATE" not in json.dumps(result)


def test_no_credentials_and_corrupt_credentials(credential):
    credential.unlink()
    service = account.AccountService(Config())
    assert asyncio.run(service.snapshot())["status"] == "disconnected"
    credential.write_text("[]")
    assert asyncio.run(service.snapshot())["status"] == "error"
    assert not account.credentials_present()


def test_cache_coalesces_and_invalidates_on_account_change(credential, monkeypatch):
    calls = []

    async def handler(request):
        calls.append(request.url.path)
        await asyncio.sleep(0.01)
        return ok(request)

    mocked(monkeypatch, handler)

    async def run():
        service = account.AccountService(Config())
        results = await asyncio.gather(
            *(service.snapshot(refresh=True) for _ in range(8))
        )
        assert all(r["status"] == "connected" for r in results)
        assert len(calls) == 2
        assert (await service.snapshot())["status"] == "connected"
        assert len(calls) == 2
        credential.unlink()
        result = await service.snapshot()
        assert result["status"] == "disconnected"
        assert result["usage"] is None and result["email"] is None

    asyncio.run(run())


@pytest.mark.parametrize(
    "value", [None, "0.9", True, -1, 2, float("nan"), float("inf")]
)
def test_unknown_fraction_is_not_zero_or_full(value):
    raw = {
        "groups": [{"buckets": [{"remainingFraction": value, "resetTime": "invalid"}]}]
    }
    result = account.normalize_quota(raw)[0]["buckets"][0]
    assert result["remaining_fraction"] is None
    assert result["reset_at"] is None


def test_zero_is_real_exhausted_quota():
    raw = {"groups": [{"buckets": [{"remainingFraction": 0}]}]}
    assert account.normalize_quota(raw)[0]["buckets"][0]["remaining_fraction"] == 0


def test_schema_change_is_usage_error(credential, monkeypatch):
    def handler(request):
        if request.url.path.endswith("loadCodeAssist"):
            return ok(request)
        return httpx.Response(200, json={"newFormat": []})

    mocked(monkeypatch, handler)
    result = asyncio.run(account.AccountService(Config()).snapshot())
    assert result["status"] == "connected"
    assert result["usage"] is None and result["usage_error"]


def test_rejected_token_is_refreshed_and_retried_once(credential, monkeypatch):
    refreshes = []

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            refreshes.append(1)
            return httpx.Response(
                200, json={"access_token": "REPLACEMENT", "expires_in": 3600}
            )
        if request.headers["authorization"] != "Bearer REPLACEMENT":
            return httpx.Response(401, json={})
        return ok(request)

    mocked(monkeypatch, handler)
    assert (
        asyncio.run(account.AccountService(Config()).snapshot())["status"]
        == "connected"
    )
    assert len(refreshes) == 1


def test_prod_fallback_for_wrong_host(credential, monkeypatch):
    def handler(request):
        if request.url.host.startswith("daily-"):
            return httpx.Response(404, json={})
        return ok(request)

    mocked(monkeypatch, handler)
    assert (
        asyncio.run(account.AccountService(Config()).snapshot())["status"]
        == "connected"
    )


def test_empty_proxy_from_compose_means_direct_connection(credential, monkeypatch):
    mocked(monkeypatch, ok)
    assert asyncio.run(account.AccountService(Config(http_proxy="")).snapshot())["status"] == "connected"


def test_credential_detection_uses_current_cli_path(tmp_path, monkeypatch):
    monkeypatch.setattr(account.Path, "home", lambda: tmp_path)
    path = tmp_path / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"token": {"access_token": "private"}}))
    assert account.credentials_path() == path
    assert account.credentials_present()
