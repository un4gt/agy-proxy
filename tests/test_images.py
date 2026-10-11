from __future__ import annotations

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
import httpx
from PIL import Image
import pytest

from app import create_app
from app.agy import AgyError
from app.config import Config
from app import images


def picture(color="red", fmt="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (24, 16), color).save(output, format=fmt)
    raw = output.getvalue()
    mime = images.FORMATS[fmt][0]
    return raw, f"data:{mime};base64," + base64.b64encode(raw).decode()


@pytest.fixture
def client(tmp_path):
    cfg = Config(agy_bin=f'{sys.executable} {(Path(__file__).parent / "fake_tools_agy.py").as_posix()}',
                 data_dir=str(tmp_path / "data"), api_keys=("key-a", "key-b"))
    with TestClient(create_app(cfg)) as c:
        c.headers["Authorization"] = "Bearer key-a"
        yield c


def body(protocol, urls, prompt="INSPECT_CONTEXT", **kwargs):
    content = []
    for i, url in enumerate(urls):
        content.append({"type": "input_text" if protocol == "responses" else "text",
                        "text": prompt if i == 0 else f"between-{i}"})
        if protocol == "responses":
            content.append({"type": "input_image", "image_url": url})
        elif protocol == "messages":
            if url.startswith("data:"):
                header, encoded = url.split(",", 1)
                source = {"type": "base64", "media_type": header[5:].split(";")[0], "data": encoded}
            else:
                source = {"type": "url", "url": url}
            content.append({"type": "image", "source": source})
        else:
            content.append({"type": "image_url", "image_url": {"url": url}})
    message = {"role": "user", "content": content}
    return {"model": "auto", "max_tokens": 2000,
            "input" if protocol == "responses" else "messages": [message], **kwargs}


def endpoint(protocol):
    return "/v1/" + ("chat/completions" if protocol == "chat" else protocol)


def response_text(response, protocol, stream=False):
    assert response.status_code == 200, response.text
    if stream:
        events = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith("data: ") and line != "data: [DONE]"]
        assert not any(e.get("error") or e.get("type") == "response.failed" for e in events)
        if protocol == "chat":
            return "".join(e["choices"][0]["delta"].get("content", "") for e in events if e.get("choices"))
        if protocol == "messages":
            return "".join(e["delta"].get("text", "") for e in events if e.get("type") == "content_block_delta")
        return events[-1]["response"]["output"][0]["content"][0]["text"]
    result = response.json()
    if protocol == "chat":
        return result["choices"][0]["message"]["content"]
    if protocol == "messages":
        return result["content"][0]["text"]
    return result["output"][0]["content"][0]["text"]


@pytest.mark.parametrize("protocol", ["chat", "responses", "messages"])
@pytest.mark.parametrize("stream", [False, True])
def test_pixels_order_and_cleanup(client, protocol, stream):
    first, u1 = picture("red")
    second, u2 = picture("blue")
    result = json.loads(response_text(client.post(endpoint(protocol), json=body(protocol, [u1, u2], stream=stream)), protocol, stream))
    assert result["image_hashes"] == [hashlib.sha256(raw).hexdigest() for raw in (first, second)]
    # The model receives positional markers, not encoded image text.
    text = result["history"][0]["content"]
    assert text.index("image_1") < text.index("between-1") < text.index("image_2")
    assert "base64" not in text
    assert client.app.state.sessions.live_count == 1
    assert client.portal.call(client.app.state.sessions.close_idle) == 1
    assert not Path(result["cwd"]).exists()
    assert all(not Path(p).exists() for p in result["image_paths"])
    assert client.app.state.sessions.live_count == 0


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP", "GIF"])
def test_actual_supported_formats(client, fmt):
    raw, url = picture(fmt=fmt)
    result = json.loads(response_text(client.post("/v1/chat/completions", json=body("chat", [url])), "chat"))
    assert result["image_hashes"] == [hashlib.sha256(raw).hexdigest()]


@pytest.mark.parametrize("value", ["data:image/png;base64,???", "data:image/png;base64,",
                                     "data:image/png;base64,SGVsbG8=", "file:///etc/passwd", "C:\\secret.png"])
def test_invalid_images_fail_before_leasing(client, value):
    response = client.post("/v1/chat/completions", json=body("chat", [value]))
    assert response.status_code == 400
    assert client.app.state.sessions.live_count == 0


def test_format_mismatch_and_animation(client):
    _, url = picture()
    assert client.post("/v1/chat/completions", json=body("chat", [url.replace("image/png", "image/jpeg")])).status_code == 400
    output = io.BytesIO()
    Image.new("RGB", (20, 20), "red").save(output, format="GIF", save_all=True, append_images=[Image.new("RGB", (20, 20), "blue")])
    url = "data:image/gif;base64," + base64.b64encode(output.getvalue()).decode()
    assert client.post("/v1/chat/completions", json=body("chat", [url])).status_code == 400


def test_image_budgets_and_image_only_message(client, monkeypatch):
    raw, url = picture()
    assert client.post("/v1/chat/completions", json=body("chat", [url] * 9)).status_code == 413
    monkeypatch.setattr(images, "MAX_IMAGE_BYTES", len(raw) - 1)
    assert client.post("/v1/chat/completions", json=body("chat", [url])).status_code == 413
    monkeypatch.setattr(images, "MAX_IMAGE_BYTES", 1024 * 1024)
    payload = body("chat", [url])
    payload["messages"][0]["content"] = payload["messages"][0]["content"][1:]
    assert client.post("/v1/chat/completions", json=payload).status_code == 200


@pytest.mark.parametrize("prompt", ["SKIP_IMAGE_READ", "WRONG_IMAGE_READ"])
def test_missing_or_unauthorized_native_image_read_fails(client, prompt):
    response = client.post("/v1/chat/completions", json=body("chat", [picture()[1]], prompt=prompt))
    assert response.status_code == 502
    assert client.app.state.sessions.live_count == 0


def test_image_and_json_schema(client):
    payload = body("chat", [picture()[1]], prompt="Describe the image", response_format={"type": "json_schema", "json_schema": {
        "name": "inspector", "schema": {"type": "object", "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
                                            "required": ["name", "age"], "additionalProperties": False}}})
    response = client.post("/v1/chat/completions", json=payload)
    assert json.loads(response_text(response, "chat")) == {"name": "Budi", "age": 20}


def test_structured_image_result_cannot_leak_attachment_path(client):
    payload = body("chat", [picture()[1]], prompt="SCHEMA_ECHO", response_format={"type": "json_schema", "json_schema": {
        "name": "inspector", "schema": {"type": "object", "properties": {"prompt": {"type": "string"}, "schema": {"type": "object"}},
                                            "required": ["prompt", "schema"]}}})
    response = client.post("/v1/chat/completions", json=payload)
    assert response.status_code == 502
    assert "internal attachment path" in response.text
    assert client.app.state.sessions.live_count == 0


def test_history_snapshot_ownership_and_parallel_isolation(client, monkeypatch):
    raw, url = picture("orange")
    calls = []
    async def download(_):
        calls.append(1)
        return images.validate_bytes(raw)
    monkeypatch.setattr(images, "fetch_image", download)
    original = client.post("/v1/responses", json=body("responses", ["https://example.com/image.png"]))
    original_result = json.loads(response_text(original, "responses"))
    original_id = original.json()["id"]
    assert len(calls) == 1
    # Subsequent turns retain original pixels without fetching a changed URL.
    def follow(key="key-a", marker=""):
        return client.post("/v1/responses", json={"model": "auto", "previous_response_id": original_id,
                           "input": "INSPECT_CONTEXT" + marker}, headers={"Authorization": "Bearer " + key})
    assert follow("key-b").status_code == 404
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda marker: follow(marker=marker), ("left", "right")))
    results = [json.loads(response_text(r, "responses")) for r in responses]
    assert len(calls) == 1
    assert all(r["image_hashes"] == original_result["image_hashes"] for r in results)
    # One continuation reuses the original workspace; the branch is isolated.
    assert len({r["cwd"] for r in results}) == 2
    assert len({r["cwd"] for r in results} | {original_result["cwd"]}) == 2
    assert len({r["image_path_fingerprints"][0] for r in results}) == 2


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "100.64.0.1"])
def test_private_dns_denied(monkeypatch, address):
    async def scenario():
        async def resolve(*args, **kwargs):
            return [(0, 0, 0, "", (address, 443))]
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
        with pytest.raises(AgyError, match="public Internet"):
            await images.public_addresses("example.test", 443)
    asyncio.run(scenario())


def test_url_dns_pin_and_redirect_revalidation(monkeypatch):
    raw, _ = picture()
    hits, lookups = [], []
    async def addresses(host, port):
        lookups.append(host)
        if host == "private.test":
            raise AgyError("private address", status_code=400)
        return ["93.184.216.34"]
    def handler(request):
        hits.append(request)
        assert request.url.host == "93.184.216.34"
        assert request.headers["Host"] == "images.test"
        assert request.extensions["sni_hostname"] == "images.test"
        assert "authorization" not in request.headers
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "http://private.test/secret"})
        return httpx.Response(200, stream=httpx.ByteStream(raw))
    actual_client = httpx.AsyncClient
    monkeypatch.setattr(images, "public_addresses", addresses)
    monkeypatch.setattr(images.httpx, "AsyncClient", lambda **kwargs: actual_client(transport=httpx.MockTransport(handler), **kwargs))
    assert asyncio.run(images.fetch_image("https://images.test/image")).data == raw
    with pytest.raises(AgyError, match="private address"):
        asyncio.run(images.fetch_image("https://images.test/redirect"))
    assert len(hits) == 2 and lookups == ["images.test", "images.test", "private.test"]


def test_hook_allows_only_exact_request_image(tmp_path):
    own, other = tmp_path / "own.png", tmp_path / "other.png"
    own.write_bytes(picture()[0]); other.write_bytes(picture("blue")[0])
    manifest = tmp_path / "allow.json"
    manifest.write_text(json.dumps([str(own.resolve())]))
    guard = Path(__file__).parents[1] / "app/tool_guard.py"
    for path, tool, expected in [(own, "view_file", "allow"), (other, "view_file", "deny"),
                                  (own, "run_command", "deny")]:
        result = subprocess.run([sys.executable, str(guard), str(manifest)], input=json.dumps({"toolCall": {
            "name": tool, "args": {"AbsolutePath": str(path.resolve())}}}), text=True, capture_output=True, check=True)
        assert json.loads(result.stdout)["decision"] == expected


def test_image_redirect_does_not_forward_cookies_between_hosts(monkeypatch):
    raw, _ = picture()
    hosts = []
    async def addresses(host, port):
        return ["93.184.216.34"]
    def handler(request):
        hosts.append(request.headers["Host"])
        assert "cookie" not in request.headers
        if len(hosts) == 1:
            return httpx.Response(302, headers={"location": "https://second.test/image",
                                               "set-cookie": "private=first-host-only; Path=/"})
        return httpx.Response(200, stream=httpx.ByteStream(raw))
    actual_client = httpx.AsyncClient
    monkeypatch.setattr(images, "public_addresses", addresses)
    monkeypatch.setattr(images.httpx, "AsyncClient", lambda **kwargs: actual_client(transport=httpx.MockTransport(handler), **kwargs))
    assert asyncio.run(images.fetch_image("https://first.test/image")).data == raw
    assert hosts == ["first.test", "second.test"]


def test_hook_requires_all_images_before_finish(tmp_path):
    paths = [tmp_path / 'first.png', tmp_path / 'second.png']
    for path in paths:
        path.write_bytes(picture()[0])
    manifest = tmp_path / 'allow.json'
    manifest.write_text(json.dumps([str(p.resolve()) for p in paths]))
    guard = Path(__file__).parents[1] / 'app/tool_guard.py'
    def decision(name, path=None):
        tool = {'name': name, 'args': {'AbsolutePath': str(path.resolve())} if path else {}}
        output = subprocess.run([sys.executable, str(guard), str(manifest)],
            input=json.dumps({'toolCall': tool}), text=True, capture_output=True, check=True)
        return json.loads(output.stdout)['decision']
    assert decision('finish') == 'deny'
    assert decision('view_file', paths[0]) == 'allow'
    assert decision('finish') == 'deny'
    assert decision('view_file', paths[1]) == 'allow'
    assert decision('finish') == 'allow'


def test_tokens_estimate_does_not_count_base64(client):
    response = client.post("/v1/messages/count_tokens", json=body("messages", [picture()[1]]))
    assert response.status_code == 200
    assert response.headers["X-Agy-Token-Count"] == "estimate"
    assert 0 < response.json()["input_tokens"] < 1000
