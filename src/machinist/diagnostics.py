"""Bounded, shareable controller diagnostics without consulting credentials.

Recognized credential syntax is redacted as defense in depth. Arbitrary output
can still contain secrets in unrecognized forms; this is not a secret detector.
"""

from __future__ import annotations

import re

_REDACTED = "[REDACTED]"
_TRUNCATED = "\n… [truncated]"

# Remove payload-bearing terminal commands, not just their escape introducer.
# OSC includes hyperlinks and clipboard writes; DCS/SOS/PM/APC end at ST.
_TERMINAL_STRINGS = re.compile(
    r"(?:\x1b\]|\x9d)[\s\S]*?(?:\x07|\x1b\\|\x9c|$)"
    r"|(?:\x1b[PX^_]|[\x90\x98\x9e\x9f])[\s\S]*?(?:\x1b\\|\x9c|$)"
)
_TERMINAL_SEQUENCES = re.compile(
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*(?:[@-~]|$)|\x1b[ -/]*[@-Z\\-_]"
)
_CONTROLS = re.compile(
    r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]"
)
_URL_USERINFO = re.compile(
    r"(?<![a-z0-9+.-])(?P<scheme>[a-z][a-z0-9+.-]*://)[^\s/?#<>\"']+@",
    re.IGNORECASE,
)

# Quoted values may contain spaces and escaped quotes. An unterminated quoted
# value consumes the rest of the message rather than exposing its contents.
_VALUE = (
    r'"(?:\\[\s\S]|[^"\\])*(?:"|$)'
    r"|'(?:\\[\s\S]|[^'\\])*(?:'|$)"
    r"|(?:\\[\s\S]|[^\s,;&\\\"'<>])+"
)
_AUTHORIZATION = re.compile(
    r"(?P<prefix>\b(?:proxy[-_])?authorization[\"']?[ \t]*[:=][ \t]*)"
    r"(?P<scheme>(?:bearer|basic)[ \t]+)?(?P<value>" + _VALUE + r")",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?P<prefix>(?<![\w-])(?:[a-z0-9_-]*"
    r"(?:token|password|passwd|secret|api[_-]?key|access[_-]?key|private[_-]?key|credentials?))"
    r"[\"']?[ \t]*[:=][ \t]*)(?P<value>" + _VALUE + r")",
    re.IGNORECASE,
)


def _redact_value(match: re.Match[str]) -> str:
    value = match["value"]
    quote = value[0] if value[0] in "\"'" else ""
    scheme = match.groupdict().get("scheme") or ""
    return match["prefix"] + scheme + quote + _REDACTED + quote


def sanitize_diagnostic(value: object, *, limit: int = 2000) -> str:
    """Redact recognized secrets and controls, then bound the complete message.

    Newlines and tabs remain useful for troubleshooting. ``limit`` counts
    characters including the truncation notice; zero returns an empty string.
    Callers should pass the complete rendered message so prefixes are bounded
    too. This helper never reads the environment or changes successful output.
    """
    if limit < 0:
        raise ValueError("diagnostic limit must not be negative")
    text = (
        value.decode("utf-8", errors="replace")
        if isinstance(value, bytes)
        else str(value)
    )
    text = _TERMINAL_STRINGS.sub("", text)
    text = _TERMINAL_SEQUENCES.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROLS.sub("", text)
    text = _URL_USERINFO.sub(lambda match: match["scheme"] + _REDACTED + "@", text)
    text = _AUTHORIZATION.sub(_redact_value, text)
    text = _SECRET_ASSIGNMENT.sub(_redact_value, text).strip()
    if len(text) <= limit:
        return text
    if limit <= len(_TRUNCATED):
        return _TRUNCATED[:limit]
    return text[: limit - len(_TRUNCATED)] + _TRUNCATED
