"""High-precision checks for explicit assistant-control text in user-facing replies.

This guard targets protocol leakage and a narrow class of unquoted self-deliberation
about already-sent and planned messages. It cannot identify arbitrary unmarked
reasoning from semantics; ACP channel separation remains the primary boundary.
"""
from __future__ import annotations

import re


class ReplyContentError(ValueError):
    """A reply contains explicit internal control/protocol material."""

    def __init__(self, code: str = "OUTPUT_CHANNEL_MISMATCH") -> None:
        self.code = code
        super().__init__("Reply withheld because internal control text was detected")


# Exclude common literal contexts before looking for cues. This deliberately keeps
# Markdown blockquotes, fenced/inline code, and quoted translation/examples intact.
_FENCED_CODE = re.compile(r"(?ms)^\s*(```+|~~~+)[^\n]*\n.*?^\s*\1\s*$")
_INLINE_CODE = re.compile(r"(`+)(?!`)(.*?)\1", re.S)
_BLOCKQUOTE_LINE = re.compile(r"(?m)^\s{0,3}>.*(?:\n|$)")
_QUOTED_SPAN = re.compile(
    r"“[^”\n]*”|‘[^’\n]*’|\"[^\"\n]*\"|(?<![\w])'[^'\n]{2,}'"
)

_INTERNAL_WRAPPER = re.compile(
    r"(?i)</?(?:think|thinking|analysis|reasoning|assistant_thought)\s*>"
    r"|<\|(?:dsml|analysis|reasoning|thought|assistant_thought|final)\|>"
    r"|<\|(?:assistant|model)_(?:analysis|reasoning|thought)\|>"
    r"|</?[|｜]DSML[|｜][^>\n]{0,120}>"
)

# Each expression is a distinct cue family. A single phrase is not enough to
# reject prose: the incident class needs multiple independent signs of the model
# narrating its own turn control (for example, a prior send plus a planned final).
_CUE_FAMILIES = (
    re.compile(
        r"\bi\s+(?:have\s+)?(?:already\s+)?(?:sent|posted|replied|answered)\s+"
        r"(?:(?:the|my)\s+)?(?:(?:first|second|one|initial|previous|earlier)\s+)?"
        r"(?:message|reply|response|line)?\b|"
        r"\bi\s+(?:have\s+)?(?:already\s+)?(?:sent|posted|replied|answered)\s+"
        r"(?:first|second|one|the first|the second)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:the\s+)?final\s+(?:message|text|answer|reply|line)\s+"
        r"(?:should|will|must|needs?\s+to|has\s+to|is\s+going\s+to|ought\s+to)\b|"
        r"\bfinal\s+(?:text|message|reply|answer)\s+i\s+"
        r"(?:output|write|send|produce)\s+(?:will|should|must)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:let\s+me|i\s+should|i\s+need\s+to)\s+"
        r"(?:reconsider|rethink|revise\s+(?:the\s+)?(?:final|wording)|"
        r"make\s+it\s+natural|change\s+the\s+wording)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:now|next)\s+(?:i\s+)?(?:output|write|send|produce)\s+"
        r"(?:the\s+)?final\b|\boutput\s+the?\s*final\b",
        re.I,
    ),
)


def _visible_prose(text: str) -> str:
    text = _FENCED_CODE.sub(" ", text)
    text = _INLINE_CODE.sub(" ", text)
    text = _BLOCKQUOTE_LINE.sub(" ", text)
    return _QUOTED_SPAN.sub(" ", text)


def validate_reply(text: str) -> None:
    """Raise :class:`ReplyContentError` for explicit channel/control leakage.

    The guard never rewrites or truncates a reply. It rejects only explicit
    internal wrappers or a strong combination of turn-control cue families.
    Ordinary English, code, quotations, and discussion of quoted examples pass.
    """
    if not isinstance(text, str):
        raise TypeError("reply text must be a string")

    prose = _visible_prose(text)
    if _INTERNAL_WRAPPER.search(prose):
        raise ReplyContentError()

    sent, planned, revised, emitting = (bool(cue.search(prose)) for cue in _CUE_FAMILIES)
    # Planning/revising ordinary messages is legitimate writing assistance.
    # Require an explicit final-emission command plus another cue, or all three
    # retrospective-send/final-planning/self-revision cues together.
    if (emitting and (sent or planned or revised)) or (sent and planned and revised):
        raise ReplyContentError()
