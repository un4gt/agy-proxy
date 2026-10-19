"""AGY input/total exclude cache reads; output already includes thinking.

Keep CLI counters unchanged internally (including stored historical usage).
OpenAI and Gemini expose inclusive input/total; Anthropic keeps cache separate.
"""


def inclusive_counts(usage: dict) -> tuple[int, int, int]:
    uncached = usage.get("input_tokens") or 0
    cached = usage.get("cache_read_tokens") or 0
    output = usage.get("output_tokens") or 0
    return uncached + cached, output, (usage.get("total_tokens") or uncached + output) + cached
