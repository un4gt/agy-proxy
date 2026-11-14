"""Image input validation: local decode safety, not upstream content policy."""
from __future__ import annotations

import asyncio
import base64
import io

from PIL import Image

from agy_proxy_core.images import ImageInput


def _data_url(size=(32, 32), color=(255, 0, 0)) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _body(count: int) -> dict:
    parts = [{"type": "text", "text": "inspect these screenshots"}]
    parts.extend({"type": "image_url", "image_url": {"url": _data_url()}}
                 for _ in range(count))
    return {"messages": [{"role": "user", "content": parts}]}


def test_image_count_is_left_to_upstream():
    body = _body(13)

    prepared = asyncio.run(ImageInput.prepare(body))

    assert len(prepared.attachments) == 13
    markers = prepared.messages[0]["content"].count("[Image attachment image_")
    assert markers == 13


def test_image_marker_order_is_preserved():
    body = _body(3)

    prepared = asyncio.run(ImageInput.prepare(body))

    assert "[Image attachment image_1;" in prepared.messages[0]["content"]
    assert "[Image attachment image_3;" in prepared.messages[0]["content"]
    assert prepared.attachments[0].mime == "image/png"
