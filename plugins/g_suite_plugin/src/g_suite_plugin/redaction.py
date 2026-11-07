"""Scrub every issued Google secret from text and result payloads.

A secret can reach an error message or a job result in more than one spelling:
raw (a token echoed in a library error), percent-encoded (a token inside a URL
or a form body, ``quote`` and ``quote_plus``), or base64 (a token inside a
Basic-auth header or an encoded blob, standard and URL-safe alphabets, padded
and unpadded). :func:`redact` replaces all of them with :data:`REDACTED`.

Secrets shorter than :data:`_MIN_SECRET_LENGTH` are ignored: redacting a
two-character value would shred unrelated text, and no Google token or client
secret is that short.
"""

from __future__ import annotations

import base64
from collections.abc import Iterable
from urllib.parse import quote, quote_plus

REDACTED = "[REDACTED]"
_MIN_SECRET_LENGTH = 8


def secret_variants(secret: str) -> set[str]:
    """Every spelling of ``secret`` that :func:`redact` removes."""
    raw = secret.encode()
    variants = {secret, quote(secret, safe=""), quote_plus(secret)}
    for encoded in (base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode()):
        variants.add(encoded)
        variants.add(encoded.rstrip("="))
    return {variant for variant in variants if variant}


def _ordered_variants(secrets: Iterable[str]) -> list[str]:
    variants: set[str] = set()
    for secret in secrets:
        if len(secret) >= _MIN_SECRET_LENGTH:
            variants |= secret_variants(secret)
    # Longest first, so a padded base64 form is removed before its unpadded prefix.
    return sorted(variants, key=len, reverse=True)


def redact_text(text: str, secrets: Iterable[str]) -> str:
    """Return ``text`` with every variant of every secret replaced."""
    for variant in _ordered_variants(secrets):
        text = text.replace(variant, REDACTED)
    return text


def redact(value: object, secrets: Iterable[str]) -> object:
    """Recursively redact strings inside dicts, lists and tuples."""
    variants = _ordered_variants(secrets)
    if not variants:
        return value
    return _redact_value(value, variants)


def _redact_value(value: object, variants: list[str]) -> object:
    if isinstance(value, str):
        for variant in variants:
            value = value.replace(variant, REDACTED)
        return value
    if isinstance(value, dict):
        return {key: _redact_value(item, variants) for key, item in value.items()}  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, (list, tuple)):
        return [_redact_value(item, variants) for item in value]  # pyright: ignore[reportUnknownVariableType]
    return value
