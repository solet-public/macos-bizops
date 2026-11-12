"""HTML5 decoding with bounded numeric conversion at every occurrence."""

from __future__ import annotations

import re
from html import unescape as _html_unescape

DECIMAL_DIGITS = 7
HEX_DIGITS = 6
MAX_CODEPOINT = 0x10FFFF
# The HTML5 named-reference lexer uses at most32 name characters. Numeric
# runs are maximal, so a bounded prefix never consumes an over-bound suffix.
_REFERENCE = re.compile(r"&#(?:[xX][0-9a-fA-F]+|[0-9]+);?|&[^\t\n\f <&#;]{1,32};?")


def _decode_reference(match: re.Match[str]) -> str:
    token = match.group()
    if not token.startswith("&#"):
        return _html_unescape(token)
    hexadecimal = token[2:3] in ("x", "X")
    digits = token[3 if hexadecimal else 2 :].removesuffix(";")
    significant = digits.lstrip("0") or "0"
    if len(significant) > (HEX_DIGITS if hexadecimal else DECIMAL_DIGITS):
        return token
    # int() sees at most6hex or7decimal significant digits, never the raw
    # spelling (including an arbitrary number of leading zeros).
    codepoint = int(significant, 16 if hexadecimal else 10)
    if codepoint > MAX_CODEPOINT:
        return token
    return _html_unescape("&#" + str(codepoint) + ";")


def unescape(value: str) -> str:
    """Decode bounded HTML5 references; retain every over-bound spelling."""
    return _REFERENCE.sub(_decode_reference, value)
