"""Keep HTTP alive without canceling a pending CLI read or inventing content."""
import asyncio

from anyio import CancelScope
from starlette.responses import StreamingResponse


class TurnStreamingResponse(StreamingResponse):
    """Also release a preflight lease if sending the HTTP headers fails."""
    def __init__(self, *args, turn, **kwargs):
        super().__init__(*args, **kwargs)
        self.turn = turn

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with CancelScope(shield=True):
                await self.turn.aclose()


async def heartbeat_events(events, interval: float):
    pending = None
    try:
        while True:
            pending = asyncio.create_task(anext(events))
            while True:
                done, _ = await asyncio.wait({pending}, timeout=interval)
                if done:
                    break
                yield None
            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            yield event
    finally:
        with CancelScope(shield=True):
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await events.aclose()
