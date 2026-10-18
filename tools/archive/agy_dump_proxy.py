"""Dump the exact upstream request that agy sends.

agy honours CLOUD_CODE_URL, so pointing it at this server makes every
Cloud Code call pass through here. Each request is written verbatim to a
log file and forwarded to the real upstream over an HTTP CONNECT tunnel
(needed on networks where Google is only reachable through a local proxy).

Usage:
    python tools/archive/agy_dump_proxy.py --listen 127.0.0.1:8899 \
        --upstream https://daily-cloudcode-pa.googleapis.com \
        --http-proxy http://127.0.0.1:7890 \
        --log /tmp/agy-dump.jsonl

    CLOUD_CODE_URL=http://127.0.0.1:8899 HTTPS_PROXY=http://127.0.0.1:7890 agy ...

Environment note: agy reads CLOUD_CODE_URL for the Cloud Code base URL and the
usual HTTP_PROXY/HTTPS_PROXY for its own egress. This server receives upstream
authorization headers and forwards them. Use controlled fixtures only.
"""

from __future__ import annotations

import argparse
import http.client
import json
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

# Bodies we want to show in full pretty-printed form (SSE replay stays compact).
PRETTY_PATHS = ("generateContent", "streamGenerateContent", "loadCodeAssist", "fetchAvailableModels")

# Planted in the prompt the proxy hands to agy; the relay finds it in contents
# and swaps agy's own construction for the client's real payload.
NONCE_PREFIX = "[agy-proxy-payload:"

# Which generateContent calls carry the client's turn. agy also issues its own
# utility calls ("checkpoint", "COMMIT_MESSAGE", ...) that must not be swapped.
SWAP_REQUEST_TYPES = {"agent"}


def _filter_tools(inner: dict, keep: set[str]) -> list[str]:
    """Drop every function declaration whose name is not in `keep`."""
    groups = inner.get("tools")
    if not isinstance(groups, list):
        return []
    kept: list[dict] = []
    dropped: list[str] = []
    for group in groups:
        decls = group.get("functionDeclarations")
        if not isinstance(decls, list):
            kept.append(group)
            continue
        survivors = [d for d in decls if d.get("name") in keep]
        dropped += [d.get("name", "?") for d in decls if d.get("name") not in keep]
        if survivors:
            kept.append({**group, "functionDeclarations": survivors})
    if dropped:
        inner["tools"] = kept
        return [f"dropped tools: {', '.join(sorted(set(dropped)))}"]
    return []


def _pretty(path: str, body: bytes) -> str:
    text = body.decode("utf-8", "replace")
    if not text:
        return ""
    for marker in PRETTY_PATHS:
        if marker in path:
            try:
                return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
            except ValueError:
                return text
    return text


class DumpProxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream: str = ""
    http_proxy: tuple[str, int] | None = None
    log_path: str = ""
    strip_tools: bool = False
    tool_mode: str = ""
    # nonce -> payload registered by the proxy, consumed once
    payloads: dict[str, dict] = {}
    payload_lock = threading.Lock()
    # when non-empty, only these tool names survive; agy force-injects
    # manage_task and there is no agent-level switch that removes it
    keep_tools: set[str] = set()
    trace_chunks: bool = False
    lock = threading.Lock()

    def log_message(self, fmt, *args):  # silence the default stderr spam
        pass

    # -- helpers ----------------------------------------------------------
    def _record(self, entry: dict) -> None:
        line = json.dumps(entry, ensure_ascii=False)
        with self.lock:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        head = f"{entry['method']} {entry['path']} -> {entry.get('status', '?')}"
        if entry.get("note"):
            head += f"  [{entry['note']}]"
        print(head, flush=True)
        if entry.get("request_body") and any(m in entry["path"] for m in PRETTY_PATHS):
            print("-" * 70, flush=True)
            print(entry["request_body"][:20000], flush=True)
            print("-" * 70, flush=True)

    def _connect(self):
        split = urlsplit(self.upstream)
        host = split.hostname
        port = split.port or (443 if split.scheme == "https" else 80)
        if split.scheme == "https":
            ctx = ssl.create_default_context()
            if self.http_proxy:
                conn = http.client.HTTPSConnection(self.http_proxy[0], self.http_proxy[1],
                                                   context=ctx, timeout=600)
                conn.set_tunnel(host, port)
            else:
                conn = http.client.HTTPSConnection(host, port, context=ctx, timeout=600)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=600)
        return conn

    # -- request handling --------------------------------------------------
    def _read_body(self) -> bytes:
        """agy streams the generateContent request with chunked encoding."""
        encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in encoding:
            chunks: list[bytes] = []
            while True:
                line = self.rfile.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                size = int(line.split(b";")[0], 16)
                if size == 0:
                    while True:  # drain trailers up to the blank line
                        trailer = self.rfile.readline()
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)  # trailing CRLF
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _find_nonce(self, inner: dict) -> str | None:
        """The proxy plants NONCE_PREFIX<id> in the prompt it gives agy.

        agy's trajectory accumulates every turn, so old nonces stay in
        contents; the newest one (last occurrence) is the current turn's.
        """
        blob = json.dumps(inner.get("contents", []), ensure_ascii=False)
        found = None
        start = 0
        while True:
            at = blob.find(NONCE_PREFIX, start)
            if at < 0:
                break
            tail = blob[at + len(NONCE_PREFIX):at + len(NONCE_PREFIX) + 32]
            if len(tail) == 32 and all(c in "0123456789abcdef" for c in tail):
                found = tail
            start = at + len(NONCE_PREFIX)
        return found

    def _swap_payload(self, inner: dict, nonce: str) -> list[str]:
        """Replace what agy built with what the client actually sent.

        The entry is kept: agy issues several generateContent calls per turn
        (checkpoint, then agent), and all of them should see the same payload.
        """
        with self.payload_lock:
            payload = self.payloads.get(nonce)
        if payload is None:
            return [f"payload {nonce[:8]} not registered"]
        notes = [f"payload {nonce[:8]}"]
        for key in ("systemInstruction", "contents", "tools"):
            if key in payload:
                inner[key] = payload[key]
                notes.append(f"{key}={len(payload[key]) if isinstance(payload[key], list) else 'set'}")
        if "generationConfig" in payload:
            inner["generationConfig"] = payload["generationConfig"]
            notes.append("generationConfig")
        return notes

    def _rewrite(self, path: str, body: bytes) -> tuple[bytes, list[str]]:
        """Swap in a client payload and/or strip the tool surface."""
        if "GenerateContent" not in path:
            return body, []
        try:
            doc = json.loads(body)
        except ValueError:
            return body, []
        inner = doc.get("request")
        if not isinstance(inner, dict):
            return body, []
        notes: list[str] = []

        # Only the main turn speaks for the client; agy's own utility calls
        # (checkpoint summaries, title generation) must keep their real input.
        if doc.get("requestType") in SWAP_REQUEST_TYPES:
            nonce = self._find_nonce(inner)
            if nonce:
                notes += self._swap_payload(inner, nonce)
            if self.keep_tools:
                notes += _filter_tools(inner, self.keep_tools)

        if self.strip_tools and inner.get("tools"):
            notes.append(f"dropped {len(inner['tools'])} tool group(s)")
            inner.pop("tools", None)
        if self.tool_mode:
            cfg = inner.setdefault("toolConfig", {}).setdefault("functionCallingConfig", {})
            cfg["mode"] = self.tool_mode
            notes.append(f"mode={self.tool_mode}")
        return json.dumps(doc, ensure_ascii=False).encode("utf-8"), notes

    def _forward(self) -> None:
        path = self.path
        raw = self._read_body()
        body, notes = self._rewrite(path, raw)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        headers["Content-Length"] = str(len(body))
        entry = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "method": self.command,
            "path": path,
            "request_headers": dict(self.headers.items()),
            "request_body": _pretty(path, body),
            "request_bytes": len(body),
            "original_bytes": len(raw),
            "note": "; ".join(notes) or None,
        }
        try:
            conn = self._connect()
            conn.request(self.command, path, body=body, headers=headers)
            resp = conn.getresponse()
            entry["status"] = resp.status
            entry["response_headers"] = dict(resp.getheaders())
            self.send_response(resp.status)
            for key, value in resp.getheaders():
                if key.lower() not in HOP_BY_HOP:
                    self.send_header(key, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            total = 0
            started = time.monotonic()
            trace: list[dict] = []
            while True:
                # read1 keeps arrival timing: read() blocks until 8 KiB or EOF,
                # which coalesces a streaming SSE response into one chunk.
                chunk = resp.read1(8192)
                if not chunk:
                    break
                total += len(chunk)
                if self.trace_chunks:
                    trace.append({"t_ms": round((time.monotonic() - started) * 1000, 1),
                                  "bytes": len(chunk),
                                  "head": chunk[:120].decode("utf-8", "replace")})
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            if self.trace_chunks and trace:
                entry["chunks"] = trace
                entry["ttfb_ms"] = trace[0]["t_ms"]
                entry["stream_ms"] = trace[-1]["t_ms"]
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            entry["response_bytes"] = total
            conn.close()
        except Exception as exc:  # relay failures must stay visible
            entry["note"] = "; ".join(filter(None, [entry.get("note"),
                                                    f"error: {type(exc).__name__}: {exc}"]))
            try:
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except Exception:
                pass
        self._record(entry)

    def do_POST(self):
        if self.path.startswith("/__payload"):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                doc = json.loads(self.rfile.read(length) or b"{}")
                nonce = doc["nonce"]
                if len(nonce) != 32 or not all(c in "0123456789abcdef" for c in nonce):
                    raise ValueError("nonce must be 32 lowercase hex chars")
                with self.payload_lock:
                    self.payloads[nonce] = doc["payload"]
            except (ValueError, KeyError, TypeError) as exc:
                body = json.dumps({"ok": False, "error": str(exc)}).encode()
                self.send_response(400)
            else:
                body = json.dumps({"ok": True, "pending": len(self.payloads)}).encode()
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            print(f"registered payload {nonce[:8]} ({len(self.payloads)} pending)", flush=True)
            return
        self._forward()

    def do_GET(self):
        self._forward()

    def do_PUT(self):
        self._forward()

    def do_DELETE(self):
        self._forward()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="127.0.0.1:8899")
    ap.add_argument("--upstream", default="https://daily-cloudcode-pa.googleapis.com")
    ap.add_argument("--http-proxy", default="http://127.0.0.1:7890",
                    help="HTTP proxy used for egress; empty string disables it")
    ap.add_argument("--log", default="agy-dump.jsonl")
    ap.add_argument("--strip-tools", action="store_true",
                    help="remove request.tools from every generateContent call")
    ap.add_argument("--tool-mode", default="", choices=["", "NONE", "AUTO", "ANY"],
                    help="force functionCallingConfig.mode on generateContent calls")
    ap.add_argument("--trace-chunks", action="store_true",
                    help="record per-chunk arrival timing of upstream SSE")
    ap.add_argument("--keep-tools", default="",
                    help="comma-separated tool names to keep; everything else is dropped "
                         "(agy force-injects manage_task and cannot be told to stop)")
    args = ap.parse_args()

    host, _, port = args.listen.rpartition(":")
    DumpProxy.upstream = args.upstream.rstrip("/")
    DumpProxy.log_path = args.log
    DumpProxy.strip_tools = args.strip_tools
    DumpProxy.tool_mode = args.tool_mode
    DumpProxy.keep_tools = {n.strip() for n in args.keep_tools.split(",") if n.strip()}
    DumpProxy.trace_chunks = args.trace_chunks
    if args.http_proxy:
        p = urlsplit(args.http_proxy)
        DumpProxy.http_proxy = (p.hostname, p.port or 8080)
    else:
        DumpProxy.http_proxy = None

    server = ThreadingHTTPServer((host or "127.0.0.1", int(port)), DumpProxy)
    print(f"listening on {host}:{port} -> {args.upstream}", flush=True)
    print(f"egress proxy: {args.http_proxy or '(direct)'}", flush=True)
    print(f"log: {args.log}", flush=True)
    print(f"\nrun agy with:\n  CLOUD_CODE_URL=http://{host}:{port} agy ...", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
