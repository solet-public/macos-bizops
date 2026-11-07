"""Credential redaction at REST result and diagnostic boundaries."""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote_from_bytes, quote_plus

from .constants import REST_REDACTED, REST_SECRET_FIELDS


def secret_forms(secret: bytes) -> set[bytes]:
    """Cover raw, form/percent, base64/url and escaped base64 spellings."""
    forms = {secret, repr(secret).encode()}
    try:
        text = secret.decode("utf-8")
    except UnicodeError:
        pass
    else:
        for ensure_ascii in (True, False):
            escaped = json.dumps(text, ensure_ascii=ensure_ascii)[1:-1]
            forms.update((escaped.encode(), escaped.replace("/", r"\/").encode()))
    for encode in (base64.b64encode, base64.urlsafe_b64encode):
        encoded = encode(secret)
        forms.update((encoded, encoded.rstrip(b"=")))
    # A query value can itself contain an escaped token, then be URL-escaped.
    for _ in range(2):
        for value in tuple(forms):
            forms.add(quote_from_bytes(value, safe="").encode())
            if value.isascii():
                forms.add(quote_plus(value.decode("ascii"), safe="").encode())
    return {value for value in forms if value}


def _pattern(value: bytes, encoding: str = "utf-8") -> bytes:
    """Only percent escape hex digits are case insensitive, never the token."""
    parts = re.split(rb"(%(?:25)*[0-9A-Fa-f]{2})", value)
    result = []
    for part in parts:
        literal = re.escape(part if encoding == "utf-8" else part.decode("utf-8").encode(encoding))
        result.append(b"(?i:" + literal + b")" if re.fullmatch(rb"%(?:25)*[0-9A-Fa-f]{2}", part) else literal)
    return b"".join(result)


def _discovery_pairs(pairs: list[tuple[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value} for key, value in pairs]


class SecretRedactor(logging.Filter):
    """One call's secret set, also filtering HTTP diagnostics on its own thread."""

    def __init__(self, secrets: tuple[bytes, ...]) -> None:
        super().__init__()
        self._forms: set[bytes] = set()
        self._patterns: list[re.Pattern[bytes]] = []
        self._thread = threading.get_ident()
        for secret in secrets:
            self.add(secret)

    def add(self, secret: bytes) -> None:
        if secret in self._forms:
            return
        self._forms.update(secret_forms(secret))
        self._patterns = [re.compile(_pattern(form)) for form in sorted(self._forms, key=len, reverse=True)]

    def binary(self, value: bytes, *, text_encodings: bool = False) -> bytes:
        for pattern in self._patterns:
            value = pattern.sub(REST_REDACTED.encode(), value)
        if text_encodings:
            value = self._encoded_json_strings(value)
            value = self._encoded_forms(value)
        return value

    def _encoded_json_strings(self, value: bytes) -> bytes:
        for encoding, width in (("utf-8", 1), ("utf-16-le", 2), ("utf-16-be", 2), ("utf-32-le", 4), ("utf-32-be", 4)):
            end = len(value) - len(value) % width
            errors = "surrogateescape" if width == 1 else "surrogatepass"
            try:
                text = value[:end].decode(encoding, errors=errors)
            except UnicodeError:
                continue
            clean = self._json_strings(text)
            if clean != text:
                value = clean.encode(encoding, errors=errors) + value[end:]
        return value

    def _encoded_forms(self, value: bytes) -> bytes:
        # Keep the original byte stream, including BOMs and invalid suffixes.
        # Only the secret and its replacement use these character encodings.
        for encoding in ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"):
            for form in sorted(self._forms, key=len, reverse=True):
                try:
                    literal = form.decode("utf-8").encode(encoding)
                except UnicodeError:
                    continue
                # Case folding is only a cheap presence test. The pattern
                # still matches token letters exactly and folds escapes only.
                if literal.lower() not in value.lower():
                    continue
                value = re.sub(_pattern(form, encoding), REST_REDACTED.encode(encoding), value)
        return value

    def _json_strings(self, text: str) -> str:
        """Redact decoded JSON strings while retaining all surrounding raw text.

        This catches escaped spellings (including arbitrary Unicode escapes)
        in fallback bodies. Only a string containing a secret is reserialized.
        """
        decoder = json.JSONDecoder()
        pieces: list[str] = []
        cursor = 0
        copied = 0
        while (start := text.find('"', cursor)) >= 0:
            try:
                value, end = decoder.raw_decode(text, start)
            except ValueError:
                cursor = start + 1
                continue
            clean = self.binary(value.encode("utf-8", errors="surrogatepass")).decode("utf-8", errors="surrogatepass")
            if clean != value:
                pieces.extend((text[copied:start], json.dumps(clean)))
                copied = end
            cursor = end
        pieces.append(text[copied:])
        return "".join(pieces)

    def text(self, value: str) -> str:
        return self.binary(self._json_strings(value).encode("utf-8"), text_encodings="\x00" in value).decode("utf-8")

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.text(str(key)): self.value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.value(item) for item in value]
        return value

    def discover(self, value: Any) -> None:
        """New tokens in a vendor response must not escape through another field."""
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower().replace("-", "_") in REST_SECRET_FIELDS and isinstance(item, str):
                    self.add(item.encode())
                    if item.lower().startswith("bearer "):
                        self.add(item.split(" ", 1)[1].encode())
                self.discover(item)
        elif isinstance(value, list):
            for item in value:
                self.discover(item)

    def discover_body(self, raw: bytes) -> None:
        """Discover JSON token members before choosing any result representation.

        A discovery-only object is a list of singleton dictionaries, retaining
        every duplicate pair at every depth. Scan JSON fragments as well, so a
        trailing diagnostic, BOM, streaming prefix or invalid byte cannot hide a
        credential. Decoded views are used only for discovery; output retains
        the original bytes. Latin-1 keeps ASCII JSON inside arbitrary byte data.
        """
        decoder = json.JSONDecoder(object_pairs_hook=_discovery_pairs)
        for encoding in ("utf-8", "latin-1", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"):
            self._discover_json_text(raw.decode(encoding, errors="replace"), decoder)

    def _discover_json_text(self, text: str, decoder: json.JSONDecoder) -> None:
        cursor = 0
        while match := re.search(r'[\[{"]', text[cursor:]):
            start = cursor + match.start()
            try:
                value, end = decoder.raw_decode(text, start)
            except ValueError:
                cursor = start + 1
                continue
            self.discover(value)
            cursor = self._discover_member(value, text, end, decoder)

    def _discover_member(self, value: Any, text: str, end: int, decoder: json.JSONDecoder) -> int:
        # Also handle JSON member fragments in incomplete/text bodies.
        if not isinstance(value, str):
            return end
        match = re.match(r"\s*:\s*", text[end:])
        if match is None:
            return end
        try:
            member, member_end = decoder.raw_decode(text, end + match.end())
        except ValueError:
            return end
        self.discover({value: member})
        return member_end

    def filter(self, record: logging.LogRecord) -> bool:
        if record.thread == self._thread:
            # HTTP DEBUG records can expose newly issued tokens before we can
            # inspect the response. Preserve the event, omit its unsafe detail.
            record.msg = "Snowflake REST transport diagnostic (details withheld)"
            record.args = ()
            # A transport traceback can include an unredacted exception URL.
            record.exc_info = None
            record.exc_text = None
        return True

    @contextmanager
    def diagnostics(self) -> Generator[None]:
        """Filter the actual emitting HTTP loggers, without changing their levels."""
        names = ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy", "httpcore.socks", "httpcore.connection_pool")
        loggers = [logging.getLogger(name) for name in names]
        for logger in loggers:
            logger.addFilter(self)
        try:
            yield
        finally:
            for logger in loggers:
                logger.removeFilter(self)
