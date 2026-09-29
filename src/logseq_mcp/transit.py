"""A minimal transit-json decoder.

Logseq's DB version stores every row of its SQLite file as a transit-json
document. We only need to *read* it, and only the subset Logseq emits, so this
is a small decoder instead of a dependency.

The one non-obvious part is the string cache. Transit replaces strings it has
already seen in the same document with back-references like ``"^1"``. Map keys
are always cacheable; values are cacheable only if they are keywords, symbols
or tagged strings (``~:``, ``~$``, ``~#``) longer than 3 chars. The cache is
filled in *document order*, so a key must be decoded before its value.

Tags other than the cache are left as-is (``"~:block/title"`` stays a string,
``"~u..."`` stays a string). The graph layer knows what those mean.
"""

from __future__ import annotations

import json
from typing import Any

# Transit encodes cache indexes in base 44, starting at ASCII 48 ("0").
_BASE = 44
_OFFSET = 48
_MAP_MARKER = "^ "


class TransitError(ValueError):
    """The document references a cache entry that does not exist."""


def _is_cacheable(s: str, as_key: bool) -> bool:
    if len(s) <= 3:
        return False
    return as_key or (s[0] == "~" and s[1] in ":$#")


def _cache_index(code: str) -> int:
    if len(code) == 1:
        return ord(code) - _OFFSET
    return (ord(code[0]) - _OFFSET) * _BASE + (ord(code[1]) - _OFFSET)


class _Cache:
    def __init__(self) -> None:
        self._entries: list[str] = []

    def read(self, s: str, as_key: bool) -> str:
        if len(s) > 1 and s[0] == "^" and s[1] != " ":
            i = _cache_index(s[1:])
            # Out of range means the cache is misaligned. Fail loudly: returning
            # "something close" would put values where attributes belong and the
            # output would be wrong without any warning.
            if not 0 <= i < len(self._entries):
                raise TransitError(f"cache reference {s!r} out of range ({len(self._entries)} entries)")
            return self._entries[i]
        if _is_cacheable(s, as_key):
            self._entries.append(s)
        return s


def _unescape(s: str) -> str:
    # A plain string that starts with "~", "^" or "`" is written with a leading "~".
    return s[1:] if len(s) > 1 and s[0] == "~" and s[1] in "~^`" else s


def _decode(node: Any, cache: _Cache, as_key: bool = False) -> Any:
    if isinstance(node, str):
        return _unescape(cache.read(node, as_key))
    if isinstance(node, list):
        if node and node[0] == _MAP_MARKER:
            out = {}
            for i in range(1, len(node) - 1, 2):
                # Two statements on purpose. In `out[k(..)] = v(..)` Python
                # evaluates the right-hand side first, the cache fills in the
                # wrong order and every later "^N" points to the wrong string.
                key = _decode(node[i], cache, as_key=True)
                out[key] = _decode(node[i + 1], cache)
            return out
        return [_decode(x, cache) for x in node]
    return node


def loads(text: str) -> Any:
    """Decode one transit-json document. Each document has its own cache."""
    return _decode(json.loads(text), _Cache())
