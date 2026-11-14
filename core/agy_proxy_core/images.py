"""Image input validation shared by the client protocols.

The proxy only enforces local decode and transport safety. Image count and
aggregate media limits belong to the upstream model/CLI, not to this proxy.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass
import io
import ipaddress
import socket
import warnings
from urllib.parse import urljoin

import httpx
from PIL import Image, UnidentifiedImageError

from .agy import AgyError

MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 16_000_000
FORMATS = {"PNG": ("image/png", ".png"), "JPEG": ("image/jpeg", ".jpg"),
           "WEBP": ("image/webp", ".webp"), "GIF": ("image/gif", ".gif")}


def invalid(message, status=400):
    return AgyError(message, status_code=status)


def image_part(url, detail="auto"):
    if not isinstance(url, str) or not url:
        raise invalid("Image URL must be a nonempty string; file_id and local paths are unsupported")
    if detail not in ("auto", "low", "high", "original"):
        raise invalid("Image detail must be auto, low, high, or original")
    return {"type": "image_url", "image_url": {"url": url, "detail": detail}}


def normalize_content(content, *, protocol="chat", role="user"):
    """Preserve mixed text/image order in the canonical Chat representation."""
    if content is None or isinstance(content, str):
        return content or ""
    if not isinstance(content, list):
        raise invalid("Message content must be text or an array of content blocks")
    parts = []
    for part in content:
        if not isinstance(part, dict):
            raise invalid("Content blocks must be objects")
        kind = part.get("type")
        if kind in (("text", "input_text", "output_text") if protocol == "responses" else ("text",)):
            if not isinstance(part.get("text"), str):
                raise invalid("Content text must be a string")
            parts.append({"type": "text", "text": part["text"]})
            continue
        if role != "user":
            raise invalid("Images are supported in user messages; image tool results are not supported yet")
        if protocol == "chat" and kind == "image_url":
            ref = part.get("image_url")
            if not isinstance(ref, dict):
                raise invalid("image_url must be an object")
            parts.append(image_part(ref.get("url"), ref.get("detail", "auto")))
        elif protocol == "responses" and kind == "input_image":
            if part.get("file_id"):
                raise invalid("Image file_id requires a Files API; supply image_url or a base64 data URL")
            parts.append(image_part(part.get("image_url"), part.get("detail", "auto")))
        elif protocol == "messages" and kind == "image":
            source = part.get("source")
            if not isinstance(source, dict):
                raise invalid("image.source must be an object")
            if source.get("type") == "url":
                parts.append(image_part(source.get("url")))
            elif source.get("type") == "base64":
                mime, data = source.get("media_type"), source.get("data")
                if mime not in {v[0] for v in FORMATS.values()} or not isinstance(data, str):
                    raise invalid("Image base64 source requires PNG, JPEG, WebP or GIF media_type and string data")
                parts.append(image_part(f"data:{mime};base64,{data}"))
            else:
                raise invalid("Image source must be base64 or url; Files API references are unsupported")
        else:
            raise invalid(f"Unsupported content type: {kind}")
    if all(part["type"] == "text" for part in parts):
        return "".join(part["text"] for part in parts)
    return parts


@dataclass(frozen=True)
class Attachment:
    data: bytes
    mime: str
    suffix: str
    pixels: int


def validate_bytes(data: bytes, mime=None) -> Attachment:
    if not data:
        raise invalid("Image data is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise invalid("Image exceeds the 10 MiB limit", 413)
    try:
        with warnings.catch_warnings(action="error", category=Image.DecompressionBombWarning):
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in FORMATS:
                    raise invalid("Only PNG, JPEG, WebP and non-animated GIF images are supported")
                actual, suffix = FORMATS[image.format]
                if mime is not None and mime != actual:
                    raise invalid("Image media_type does not match its actual format")
                pixels = image.width * image.height
                if not pixels or pixels > MAX_PIXELS:
                    raise invalid("Image exceeds the 16 megapixel limit", 413)
                if getattr(image, "n_frames", 1) != 1:
                    raise invalid("Animated or multi-frame images are unsupported; send a still image")
                image.verify()
            # JPEG verify() does not decode pixels; reject truncated payloads too.
            with Image.open(io.BytesIO(data)) as image:
                image.load()
    except AgyError:
        raise
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise invalid("Image exceeds the 16 megapixel limit", 413) from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise invalid("Invalid or damaged image data") from exc
    return Attachment(data, actual, suffix, pixels)


def decode_data_url(url):
    header, sep, encoded = url.partition(",")
    if not sep or header not in {f"data:{v[0]};base64" for v in FORMATS.values()}:
        raise invalid("Use a base64 data URL with PNG, JPEG, WebP or GIF media type")
    if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise invalid("Image exceeds the 10 MiB limit", 413)
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise invalid("Image contains invalid base64") from exc
    return validate_bytes(data, header[5:].split(";", 1)[0])


async def public_addresses(host, port):
    try:
        records = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM), 5)
        addresses = list(dict.fromkeys(record[4][0] for record in records))
        if not addresses:
            raise ValueError()
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global or (isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped and not ip.ipv4_mapped.is_global):
                raise invalid("Image URLs must resolve only to public Internet addresses")
        return addresses
    except (OSError, ValueError, asyncio.TimeoutError) as exc:
        raise invalid("Could not resolve the image URL") from exc


async def fetch_image(url):
    try:
        return await asyncio.wait_for(_fetch_image(url), timeout=20)
    except asyncio.TimeoutError as exc:
        raise invalid("Image download timed out") from exc


async def _fetch_image(url):
    # Pin DNS results when connecting, while preserving Host and TLS SNI. Every
    # redirect is revalidated; no proxy, cookies or caller credentials are used.
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5), trust_env=False) as client:
            for _ in range(4):
                target = httpx.URL(url)
                port = target.port or (443 if target.scheme == "https" else 80)
                if (target.scheme not in ("https", "http") or not target.host or target.userinfo
                        or port not in (80, 443) or "%" in target.host):
                    raise invalid("Image URLs must be public HTTP(S) URLs on port 80 or 443, without credentials")
                addresses = await public_addresses(target.host, port)
                pinned = target.copy_with(host=addresses[0], fragment=None)
                host = target.netloc.decode("ascii")
                # DNS pinning changes the URL host to an IP. Never let cookies
                # from one redirect target be sent to another sharing that IP.
                client.cookies.clear()
                async with client.stream("GET", pinned, headers={"Host": host, "Accept": "image/*", "Accept-Encoding": "identity"},
                                         extensions={"sni_hostname": target.host}) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise invalid("Image URL returned an invalid redirect")
                        url = urljoin(str(target), location)
                        continue
                    if response.status_code != 200:
                        raise invalid(f"Image download failed with HTTP {response.status_code}")
                    if response.headers.get("content-encoding", "identity") != "identity":
                        raise invalid("Compressed HTTP image responses are unsupported")
                    chunks, size = [], 0
                    async for chunk in response.aiter_raw():
                        size += len(chunk)
                        if size > MAX_IMAGE_BYTES:
                            raise invalid("Image exceeds the 10 MiB limit", 413)
                        chunks.append(chunk)
                    return await asyncio.to_thread(validate_bytes, b"".join(chunks))
            raise invalid("Too many image URL redirects")
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
        raise invalid("Could not download the image URL") from exc


class ImageInput:
    def __init__(self):
        self.attachments = []
        self.messages = []

    @classmethod
    async def prepare(cls, body):
        """Snapshot URLs into request-owned pixels and render positional markers.

        body retains canonical data URLs for bounded Responses history. This
        separate execution transcript contains no encoded image bytes.
        """
        result = cls()
        for message in body.get("messages", []):
            content = normalize_content(message.get("content"), role=message.get("role"))
            message["content"] = content
            copy = dict(message)
            if isinstance(content, list):
                pieces = []
                for part in content:
                    if part["type"] == "text":
                        pieces.append(part["text"])
                        continue
                    ref = part["image_url"]
                    image = (await asyncio.to_thread(decode_data_url, ref["url"]) if ref["url"].startswith("data:")
                             else await fetch_image(ref["url"]))
                    result.attachments.append(image)
                    ref["url"] = f"data:{image.mime};base64," + base64.b64encode(image.data).decode("ascii")
                    pieces.append(f"\n[Image attachment image_{len(result.attachments)}; detail={ref['detail']}]\n")
                copy["content"] = "".join(pieces)
            result.messages.append(copy)
        return result
