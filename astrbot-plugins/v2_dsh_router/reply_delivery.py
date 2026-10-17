"""Conservative semantic grouping for long final replies."""

from __future__ import annotations

import re


_LONG_REPLY_THRESHOLD = 600
_GROUP_TARGET = 400
_GROUP_MIN = 300
_GROUP_MAX = 500
_CODE_FENCE = re.compile(r"(?m)^\s*(```|~~~)")
_NUMBERED_SECTION = re.compile(r"(?m)^[ \t]*(?:\d+[.)、][ \t]*|[-*][ \t]+)")


def split_final_reply(text: str) -> list[str]:
    """Group a long reply at existing paragraph/topic boundaries.

    The result is lossless: concatenating the returned chunks reproduces the
    original text exactly. Short replies and replies containing fenced code
    remain a single message. No sentence, line, URL, or code is cut in half.
    If there are no suitable natural boundaries, keep the reply intact.
    """
    if len(text) <= _LONG_REPLY_THRESHOLD or _CODE_FENCE.search(text):
        return [text]

    boundaries = {match.end() for match in re.finditer(r"\n[ \t]*\n+", text)}
    boundaries.update(match.start() for match in _NUMBERED_SECTION.finditer(text)
                      if match.start() > 0)
    ordered = sorted(boundaries)
    if not ordered:
        return [text]

    chunks: list[str] = []
    start = 0
    while len(text) - start > _GROUP_MAX:
        remaining = [point for point in ordered if start < point < len(text)]
        usable = [point for point in remaining
                  if _GROUP_MIN <= point - start <= _GROUP_MAX]
        if usable:
            end = min(usable, key=lambda point: abs((point - start) - _GROUP_TARGET))
        else:
            # A single semantic unit can exceed the target. Keep it whole,
            # but still separate the following topics instead of giving up.
            later = [point for point in remaining if point - start >= _GROUP_MIN]
            if not later:
                break
            end = later[0]
        chunks.append(text[start:end])
        start = end
    if not chunks:
        return [text]
    chunks.append(text[start:])
    return chunks
