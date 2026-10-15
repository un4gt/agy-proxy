"""Static wire adapter registry; imports stay lazy to avoid router cycles."""
from dataclasses import dataclass
from importlib import import_module


@dataclass(frozen=True)
class ProtocolAdapter:
    module: str
    parser: str
    encoder: str
    events: str | None = None
    error_module: str = ".auth"
    error_encoder: str = "openai_error"

    def parse(self, body, **context):
        return getattr(import_module(self.module, __package__), self.parser)(body, **context)

    def encode(self, *args, **kwargs):
        return getattr(import_module(self.module, __package__), self.encoder)(*args, **kwargs)

    def stream(self, *args, **kwargs):
        return getattr(import_module(self.module, __package__), self.events)(*args, **kwargs)

    def error(self, *args, **kwargs):
        return getattr(import_module(self.error_module, __package__), self.error_encoder)(*args, **kwargs)


def chat_request(body):
    from .agy import AgyError
    if body.get("model") is not None and not isinstance(body["model"], str):
        raise AgyError("model must be a string", status_code=400)
    return dict(body, model=body.get("model") or "default")


ADAPTERS = {
    "chat": ProtocolAdapter(".protocol_adapters", "chat_request", "chat_result", "chat_events"),
    "responses": ProtocolAdapter(".responses_api", "translate", "output_items", "completed_events"),
    "messages": ProtocolAdapter(".anthropic_api", "translate", "message_result", "message_events",
                                ".anthropic_api", "error_response"),
    "gemini": ProtocolAdapter(".gemini_adapter", "translate", "result_body", "result_events", ".auth", "gemini_error"),
}


def chat_result(result):
    return result.message()


def chat_events(result, *, chunk, include_usage=False):
    from .openai_api import _usage
    if result.tool_calls:
        yield chunk({"tool_calls": [dict(call, index=i) for i, call in enumerate(result.tool_calls)]}, None)
    yield chunk({}, result.finish_reason)
    if include_usage:
        yield dict(chunk({}, None), choices=[], usage=_usage(result.usage))
