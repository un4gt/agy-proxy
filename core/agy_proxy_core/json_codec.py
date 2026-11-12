"""JSON boundaries use orjson; decode to text only for prompts/string fields."""
from __future__ import annotations

import math

import orjson
from starlette.responses import JSONResponse as BaseJSONResponse

loads = orjson.loads
JSONDecodeError = orjson.JSONDecodeError
dumps = orjson.dumps


def _finite(value) -> None:
    # orjson encodes non-finite floats as null. Validated structured output must
    # reject them instead of silently changing an upstream value.
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Non-finite numbers are not valid JSON")
    if isinstance(value, dict):
        for child in value.values():
            _finite(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _finite(child)


def dumps_text(value, *, sort_keys=False, strict=False) -> str:
    if strict:
        _finite(value)
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS if sort_keys else 0).decode("utf-8")


class JSONResponse(BaseJSONResponse):
    def render(self, content) -> bytes:
        _finite(content)
        return orjson.dumps(content)
