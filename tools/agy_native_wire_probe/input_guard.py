"""Preserve proxy turn envelopes at the pinned CLI's serializer byte limit.

AGY 1.4.0 truncates each user part at 192,000 bytes, including its closing
wrapper. A replayed client conversation is one part: clipping it loses client
history, tool results, and recovery feedback. This guard only changes that
serializer limit for the two envelopes produced by the proxy, within the
observer's existing 16 MiB bound. Other CLI truncation remains unchanged.
"""

TRUNCATION_FUNCTION = ('google3/third_party/oneharness/internal/core/serializers/'
                       'truncation/truncation.Truncate')
CLI_INPUT_LIMIT = 192_000
MAX_INPUT_BYTES = 16 * 1024 * 1024
PROXY_PREFIXES = (b'<USER_REQUEST>\n[Complete client conversation]\n',
                  b'<USER_REQUEST>\n[New client messages]\n')
PREFIX_BYTES = max(map(len, PROXY_PREFIXES))


class InputLimitError(ValueError):
    code = 'native_input_too_large'


def preserved_limit(prefix: bytes, length: int, limit: int) -> int | None:
    if limit != CLI_INPUT_LIMIT or length <= limit or not prefix.startswith(PROXY_PREFIXES):
        return None
    if length > MAX_INPUT_BYTES:
        raise InputLimitError('Native input exceeds the 16 MiB capture bound')
    return length
