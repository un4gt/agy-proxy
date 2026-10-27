"""Account for parsed upstream usageMetadata, independently of CLI counters."""


def normalize_usage(metadata: object) -> dict[str, int] | None:
    """Keep the store's uncached-input convention; output includes thinking."""
    if not isinstance(metadata, dict):
        return None
    fields = ("promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount",
              "cachedContentTokenCount", "totalTokenCount")
    if "promptTokenCount" not in metadata or "totalTokenCount" not in metadata:
        return None
    values = [metadata.get(name, 0) for name in fields]
    if any(type(value) is not int or value < 0 for value in values):
        return None
    prompt, candidates, thinking, cached, total = values
    output = candidates + thinking
    if cached > prompt or total < prompt + output:
        return None
    return {"input_tokens": prompt - cached, "output_tokens": output,
            "thinking_tokens": thinking, "cache_read_tokens": cached,
            "total_tokens": total - cached}


class NativeUsage:
    """Replace each request's SSE snapshot, then sum distinct agent requests."""

    def __init__(self):
        self._snapshots: dict[str, dict[str, int]] = {}
        self._invalid: set[str] = set()
        self._finished: set[str] = set()
        self._final_usage: set[str] = set()

    def observe(self, request_id: str, metadata: object, *, finished: bool = False):
        if finished:
            self._finished.add(request_id)
        if metadata is None:
            return
        usage = normalize_usage(metadata)
        if usage is None:
            # Preserve earlier known consumption, but never claim it is complete.
            self._invalid.add(request_id)
            return
        self._snapshots[request_id] = usage
        self._invalid.discard(request_id)
        if request_id in self._finished:
            self._final_usage.add(request_id)

    def snapshot(self, selected, completed) -> tuple[dict[str, int], bool]:
        selected = set(selected)
        totals: dict[str, int] = {}
        for request_id in selected:
            for name, value in self._snapshots.get(request_id, {}).items():
                totals[name] = totals.get(name, 0) + value
        complete = (bool(selected) and selected <= completed and
                    selected <= self._final_usage and not selected & self._invalid)
        return totals, complete
