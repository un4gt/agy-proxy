"""One bridge turn: canonical client messages in, TurnResult out."""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import aclosing
from typing import AsyncIterator

from jsonschema.exceptions import ValidationError

from .. import json_codec as json
from ..agy import AgyError
from ..contracts import ToolCall
from ..external_tools import ExternalTools
from ..runner import TurnEvent, TurnProfile, TurnResult
from ..structured_output import ResponseFormat
from .protocol import (BridgeProtocolError, ToolAccumulator, build_request,
                       conversation_id_for, finish_reason, usage_from_response)

log = logging.getLogger("agy_proxy.bridge.turn")


class BridgeTurn:
    def __init__(self, cfg, manager, *, external: ExternalTools | None,
                 response_format: ResponseFormat | None, model: str | None,
                 effort: str | None, profile: TurnProfile, session_key=None,
                 max_tokens: int | None = None, images=None):
        self.cfg = cfg
        self.manager = manager
        self.external = external
        self.response_format = response_format
        self.model = model
        self.effort = effort
        self.profile = profile
        self.session_key = session_key
        self.max_tokens = max_tokens
        self.images = images
        self._iterator: AsyncIterator[TurnEvent] | None = None

    def __aiter__(self) -> AsyncIterator[TurnEvent]:
        if self._iterator is None:
            self._iterator = self._run()
        return self

    async def __anext__(self) -> TurnEvent:
        if self._iterator is None:
            self._iterator = self._run()
        return await self._iterator.__anext__()

    async def aclose(self) -> None:
        if self._iterator is not None:
            await self._iterator.aclose()
            self._iterator = None

    async def _run(self) -> AsyncIterator[TurnEvent]:
        if self.response_format is not None and self.external and self.external.contracts:
            raise AgyError("response_format cannot be combined with tools", status_code=400)
        session = None
        try:
            with self.profile.trace.span("session_acquire"):
                session = await self.manager.acquire(
                    model=self.model, effort=self.effort, owner=self._owner())
            self.profile.account_id = session.account_id
            self.profile.account_name = session.account_name or session.account_id
            self.profile.new_session = session.turns == 0
            self.profile.attempts = 1
            self.profile.mark("session_ready")
            yield "ready", None

            messages = self.external.messages if self.external is not None else []
            tools = self.external.contracts if self.external is not None else {}
            choice = self.external.choice if self.external is not None else None
            try:
                request = build_request(
                    messages,
                    tools=tools,
                    tool_choice=choice,
                    model=self.model,
                    effort=self.effort,
                    conversation_id=conversation_id_for(self.session_key),
                    response_format=self.response_format,
                    max_tokens=self.max_tokens,
                    parallel_tool_calls=self.external.parallel if self.external is not None else None,
                    signature_lookup=self.manager.signature,
                    images=self.images,
                )
            except BridgeProtocolError as exc:
                raise AgyError(str(exc), status_code=400, code="invalid_request") from exc
            log.info("bridge request model=%s prompts=%s tools=%d",
                     self.model, [(p.get("source"), len(p.get("prompt") or ""),
                                   len(p.get("toolCalls") or [])) for p in request.payload.get("chatMessagePrompts", [])],
                     len(request.payload.get("tools") or []))

            accumulator = ToolAccumulator()
            text_parts: list[str] = []
            usage: dict = {}
            stop_reason: str | None = None
            message_id: str | None = None
            message_signature: str | None = None
            try:
                async with asyncio.timeout(self.cfg.turn_timeout):
                    async with aclosing(session.stream(request.payload)) as events:
                        async for event in events:
                            if isinstance(event.get("deltaText"), str):
                                fragment = event["deltaText"]
                                if fragment:
                                    text_parts.append(fragment)
                                    self.profile.mark("first_delta")
                                    yield "delta", fragment
                            if event.get("deltaToolCalls"):
                                accumulator.feed(event["deltaToolCalls"])
                            if isinstance(event.get("usage"), dict):
                                usage = usage_from_response(event["usage"])
                            if isinstance(event.get("stopReason"), str):
                                stop_reason = event["stopReason"]
                            if isinstance(event.get("messageId"), str):
                                message_id = event["messageId"]
                            if isinstance(event.get("deltaSignature"), str):
                                message_signature = event["deltaSignature"]
            except TimeoutError:
                raise AgyError(
                    f"bridge turn timed out after {self.cfg.turn_timeout:.0f}s",
                    status_code=504, code="upstream_timeout") from None

            for call_id, signature in accumulator.signatures().items():
                self.manager.remember_signature(call_id, signature)
            first_call = accumulator.first_id()
            if first_call and message_signature:
                self.manager.remember_signature("assistant:" + first_call, message_signature)
            if accumulator.signatures():
                await asyncio.to_thread(self.manager.flush_signatures)

            calls = accumulator.result()
            text = "".join(text_parts)
            if request.structured is not None:
                text, calls = self._structured_result(calls)
            else:
                self._validate_calls(calls)
            if usage:
                self.profile.add_usage(usage)
                self.profile.usage_complete = True
            self.profile.mark("result")
            result = TurnResult(text=text, calls=calls, usage=usage,
                                conversation_id=message_id or self._conversation_id(),
                                num_turns=1, stop_reason=finish_reason(stop_reason, False))
            yield "result", result
        finally:
            if session is not None:
                await self.manager.release(session)

    def _owner(self) -> str | None:
        key = self.session_key
        if key is None:
            return None
        return getattr(key, "owner", None) or getattr(key, "key", None) or str(key)

    def _conversation_id(self) -> str:
        return conversation_id_for(self.session_key)

    def _validate_calls(self, calls: list[ToolCall]) -> None:
        contracts = self.external.contracts if self.external is not None else {}
        for call in calls:
            if not call.id:
                call.id = "call_" + uuid.uuid4().hex[:24]
            contract = contracts.get(call.name)
            if contract is None:
                raise AgyError(f"model requested undeclared tool {call.name!r}",
                               status_code=502, code="tool_contract_invalid")
            try:
                contract.validator.validate(call.arguments)
            except ValidationError as exc:
                raise AgyError(
                    f"invalid arguments for tool {call.name}: "
                    f"{'/'.join(str(p) for p in exc.absolute_path) or '/'} {exc.validator}",
                    status_code=502, code="tool_arguments_invalid") from None
            except Exception:
                raise AgyError("cannot resolve tool schema", status_code=400,
                               code="invalid_tool_schema") from None

    def _structured_result(self, calls: list[ToolCall]) -> tuple[str, list[ToolCall]]:
        assert self.response_format is not None
        name = "structured_output"
        selected = next((call for call in calls if call.name == name), None)
        if selected is None:
            raise AgyError("model did not submit the structured output tool",
                           status_code=502, code="structured_output_missing")
        value = selected.arguments
        if self.response_format.json_mode:
            raw = value.get("json") if isinstance(value, dict) else None
            if not isinstance(raw, str):
                raise AgyError("structured output did not contain a json string",
                               status_code=502, code="output_json_invalid")
            try:
                value = json.loads(raw)
            except (ValueError, TypeError):
                raise AgyError("structured output json string is invalid",
                               status_code=502, code="output_json_invalid") from None
        try:
            self.response_format.validator.validate(value)
        except ValidationError as exc:
            raise AgyError("structured output does not match response_format",
                           status_code=502, code="structured_output_invalid") from exc
        return json.dumps_text(value, strict=True), []
