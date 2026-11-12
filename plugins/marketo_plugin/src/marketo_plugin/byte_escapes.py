"""Lossless, linear views of adjacent percent, hex and octal byte escapes."""

from __future__ import annotations

import re

BYTE_TOKEN = r"%[0-9a-fA-F]{2}|\\x[0-9a-fA-F]{2}|\\[0-7]{1,3}"
BYTE_RUN = "(?:" + BYTE_TOKEN + ")+"
_TOKEN = re.compile(BYTE_TOKEN)
_RUN = re.compile(BYTE_RUN)
UTF8_SEQUENCE = r"[\xc2-\xdf][\x80-\xbf]|\xe0[\xa0-\xbf][\x80-\xbf]|[\xe1-\xec\xee-\xef][\x80-\xbf]{2}|\xed[\x80-\x9f][\x80-\xbf]|\xf0[\x90-\xbf][\x80-\xbf]{2}|[\xf1-\xf3][\x80-\xbf]{3}|\xf4[\x80-\x8f][\x80-\xbf]{2}"
_UTF8_SEQUENCE = re.compile(UTF8_SEQUENCE)
_UTF8_CHARACTER = re.compile(r"[\x00-\x7f]|" + UTF8_SEQUENCE)


def utf8_byte_view(value: str) -> str:
    """Decode valid sequences strictly; leave invalid Latin1 byte spellings."""
    return _UTF8_SEQUENCE.sub(lambda match: match.group().encode("latin1").decode("utf-8", errors="strict"), value)


def _decode_chunk(tokens: list[str], values: list[int], *, utf8: bool) -> str:
    raw = bytes(values).decode("latin1")
    if not utf8:
        return raw
    result: list[str] = []
    previous = 0
    for match in _UTF8_CHARACTER.finditer(raw):
        result.extend(tokens[previous : match.start()])
        result.append(match.group().encode("latin1").decode("utf-8", errors="strict"))
        previous = match.end()
    result.extend(tokens[previous:])
    return "".join(result)


def decode_byte_run(value: str, *, utf8: bool = True) -> str:
    """Assemble mixed spellings; preserve each invalid byte's own token."""
    result: list[str] = []
    tokens: list[str] = []
    values: list[int] = []
    for match in _TOKEN.finditer(value):
        token = match.group()
        number = int(token[1:], 16) if token.startswith("%") else int(token[2:], 16) if token.startswith(r"\x") else int(token[1:], 8)
        if number > 255:
            result.extend((_decode_chunk(tokens, values, utf8=utf8), token))
            tokens, values = [], []
        else:
            tokens.append(token)
            values.append(number)
    result.append(_decode_chunk(tokens, values, utf8=utf8))
    return "".join(result)


def byte_escape_view(value: str) -> str:
    """Retain the supplementary bijective per-byte view, including invalid UTF8."""
    return _RUN.sub(lambda match: decode_byte_run(match.group(), utf8=False), value)
