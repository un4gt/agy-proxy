"""Adapter-local parsing and error helpers."""

from __future__ import annotations

import pytest

from agy_proxy_core.agy import AgyError

from agy_proxy_adapters.errors import openai_error
from agy_proxy_adapters.protocol_adapters import ADAPTERS, chat_request


def test_registry_has_only_downstream_protocols():
    assert set(ADAPTERS) == {"chat", "responses", "messages"}


def test_chat_request_requires_string_model():
    with pytest.raises(AgyError):
        chat_request({"model": 1})
    assert chat_request({})["model"] == "default"


def test_openai_error_shape():
    response = openai_error(400, "bad", "invalid_request_error", "invalid_request")
    assert response.status_code == 400
    assert response.body
