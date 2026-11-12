"""Per-connection DSN resolution from the address book + JDBC-URL parsing.

Every connection is one ``external_pg::<name>`` address-book entry: literal
host/port/dbname/user/sslmode fields plus a ``vault::`` password reference the
resolver swaps in via ``resolve_with_secrets`` (chain-consumed — see
:func:`constants.vault_key_for_password`). ``resolve`` returns a frozen
:class:`ExternalDsn`; ``list_connection_names`` enumerates the registered names
(client-side prefix filter over the substring-style address-book search).

Registration convenience (§8.2, R7-ii): :func:`parse_jdbc_url` decomposes a
``jdbc:postgresql://…`` / ``postgresql://…`` URL into discrete fields for the
operator's registration runbook. The password is extracted to a SEPARATE field
and NEVER appears in any log/echo/error — both :class:`ExternalDsn` and
:class:`ParsedRegistration` redact it from ``repr``, and parse failures raise
with a :func:`scrub_password_from_url`-scrubbed URL (rev-F R-D4).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

from .constants import (
    CONNECTION_ADDRESS_TYPE,
    CONNECTION_ENTRY_PREFIX,
    DEFAULT_PORT,
    DEFAULT_SSLMODE,
    ERROR_ADDRESS_BOOK_NOT_AVAILABLE,
    ERROR_CONNECTION_UNKNOWN,
    FIELD_DBNAME,
    FIELD_HOST,
    FIELD_PASSWORD,
    FIELD_PORT,
    FIELD_SSLMODE,
    FIELD_USER,
    LIST_CONNECTIONS_SCAN_LIMIT,
)

_PASSWORD_REDACTION = "***"
_ALLOWED_SCHEMES = frozenset({"postgresql", "postgres"})


class ExternalPgConfigError(RuntimeError):
    """Raised when a connection cannot be resolved from the address book."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, repr=False)
class ExternalDsn:
    """A resolved foreign-Postgres connection target.

    ``repr`` deliberately redacts the password so logging the object never
    leaks the secret (rev-F R-D4).
    """

    name: str
    host: str
    port: int
    dbname: str
    user: str
    password: str
    sslmode: str

    def __repr__(self) -> str:
        return f"ExternalDsn(name={self.name!r}, host={self.host!r}, port={self.port!r}, dbname={self.dbname!r}, user={self.user!r}, password={_PASSWORD_REDACTION!r}, sslmode={self.sslmode!r})"


@dataclass(frozen=True, repr=False)
class ParsedRegistration:
    """Discrete fields decomposed from a JDBC/libpq URL for registration.

    The password is a separate field the runbook routes agent-blind to vault; it
    NEVER lands in the address-book entry or a log. ``repr`` redacts it.
    """

    host: str
    port: int
    dbname: str
    user: str
    sslmode: str
    password: str

    def __repr__(self) -> str:
        return f"ParsedRegistration(host={self.host!r}, port={self.port!r}, dbname={self.dbname!r}, user={self.user!r}, sslmode={self.sslmode!r}, password={_PASSWORD_REDACTION!r})"


class AppConfigLoader:
    """Resolve foreign-PG connections from ``service_interface::address_book_service``.

    The service is duck-typed so this plugin never imports from
    ``default_address_book_plugin``.
    """

    def __init__(self, address_book_service: Any) -> None:
        if address_book_service is None:
            raise ExternalPgConfigError(
                ERROR_ADDRESS_BOOK_NOT_AVAILABLE,
                "address_book_service is required for AppConfigLoader",
            )
        self._address_book = address_book_service

    def resolve(self, name: str) -> ExternalDsn:
        """Return the resolved DSN for connection ``name`` (vault password swapped)."""
        if not name:
            raise ExternalPgConfigError(ERROR_CONNECTION_UNKNOWN, "a non-empty connection_name is required")
        result = self._address_book.resolve_with_secrets(name=f"{CONNECTION_ENTRY_PREFIX}{name}")
        if not isinstance(result, dict) or result.get("action_status") != "completed":
            raise ExternalPgConfigError(
                ERROR_CONNECTION_UNKNOWN,
                f"no connection named '{name}' is registered (expected address-book entry '{CONNECTION_ENTRY_PREFIX}{name}')",
            )
        fields = definitive_fields(result)
        if fields is None:
            raise ExternalPgConfigError(
                ERROR_CONNECTION_UNKNOWN,
                f"connection '{name}' entry is malformed: every field row needs a string field_type and a string value, each field once",
            )
        return self._build_dsn(name, fields)

    def registered_credentials(self, name: str) -> tuple[str, str] | None:
        """The user and password registered under ``name``, or ``None`` when they are UNKNOWN.

        Definitive only when :func:`definitive_fields` accepts the whole answer
        and, for a non-empty entry, BOTH a user row and a password row are
        present (a passwordless entry says so with an explicit ``""`` value).
        An empty entry list is the definitive "nothing registered". Nothing is
        defaulted or coerced. Faults and timeouts propagate. Used only to scan an
        outcome whose resolution failed.
        """
        fields = definitive_fields(self._address_book.resolve_with_secrets(name=f"{CONNECTION_ENTRY_PREFIX}{name}"))
        if fields is None:
            return None
        if not fields:
            return "", ""
        if FIELD_USER not in fields or FIELD_PASSWORD not in fields:
            return None
        return fields[FIELD_USER], fields[FIELD_PASSWORD]

    def list_connection_names(self) -> tuple[list[str], bool]:
        """Enumerate registered connection names (bare, prefix-stripped).

        The address-book search is SUBSTRING-style, not a true prefix API, so a
        substring hit like ``foo_external_pg::bar`` is dropped client-side; only
        names that actually start with ``external_pg::`` survive. Returns
        ``(names, truncated)`` — ``truncated`` is True when the scan hit the
        limit (the caller logs it; no silent caps).
        """
        result = self._address_book.search(
            query=CONNECTION_ENTRY_PREFIX,
            address_type=CONNECTION_ADDRESS_TYPE,
            limit=LIST_CONNECTIONS_SCAN_LIMIT,
        )
        addresses = self._extract_addresses(result)
        names: list[str] = []
        for row in addresses:
            raw = row.get("name")
            if isinstance(raw, str) and raw.startswith(CONNECTION_ENTRY_PREFIX):
                names.append(raw[len(CONNECTION_ENTRY_PREFIX) :])
        truncated = len(addresses) >= LIST_CONNECTIONS_SCAN_LIMIT
        return sorted(names), truncated

    @staticmethod
    def _extract_addresses(result: Any) -> list[dict[str, Any]]:
        if not isinstance(result, dict) or result.get("action_status") != "completed":
            return []
        data = result.get("data") or {}
        addresses = data.get("addresses", []) if isinstance(data, dict) else []
        if not isinstance(addresses, list):
            return []
        return [row for row in addresses if isinstance(row, dict)]

    def _build_dsn(self, name: str, fields: dict[str, str]) -> ExternalDsn:
        """The DSN from a definitive field set; an absent or empty required field refuses resolution."""
        host = fields.get(FIELD_HOST, "")
        dbname = fields.get(FIELD_DBNAME, "")
        user = fields.get(FIELD_USER, "")
        password = fields.get(FIELD_PASSWORD, "")
        sslmode = fields.get(FIELD_SSLMODE) or DEFAULT_SSLMODE
        port = _coerce_port(fields.get(FIELD_PORT, ""))
        missing = [
            label
            for label, value in (
                (FIELD_HOST, host),
                (FIELD_DBNAME, dbname),
                (FIELD_USER, user),
                (FIELD_PASSWORD, password),
            )
            if not value
        ]
        if missing:
            raise ExternalPgConfigError(
                ERROR_CONNECTION_UNKNOWN,
                f"connection '{name}' entry is incomplete: missing {missing}. Register host, port, dbname, user, sslmode, and a vault:: password reference.",
            )
        return ExternalDsn(
            name=name,
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            password=password,
            sslmode=sslmode,
        )


def parse_jdbc_url(url: str) -> ParsedRegistration:
    """Decompose a JDBC/libpq Postgres URL into discrete registration fields.

    Accepts ``jdbc:postgresql://…`` (the ``jdbc:`` prefix is stripped),
    ``postgresql://…``, and ``postgres://…``. User/password may live in the
    netloc (``user:pass@host``) or as ``user=``/``password=`` query params. The
    password is extracted to its own field and scrubbed from any error message
    (rev-F R-D4). Raises ``ValueError`` (with a scrubbed URL) on a malformed or
    non-Postgres URL.
    """
    if not url.strip():
        raise ValueError("a non-empty JDBC/libpq URL is required")
    candidate = url.strip()
    if candidate.lower().startswith("jdbc:"):
        candidate = candidate[len("jdbc:") :]
    parts = urlsplit(candidate)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise ValueError(f"unsupported URL scheme '{parts.scheme}' in {scrub_password_from_url(url)}; expected postgresql:// or postgres://")
    query = parse_qs(parts.query)
    host = parts.hostname or ""
    dbname = parts.path.lstrip("/")
    user = parts.username or _first_query(query, "user")
    password = parts.password or _first_query(query, "password")
    sslmode = _first_query(query, "sslmode") or DEFAULT_SSLMODE
    port = parts.port if parts.port is not None else DEFAULT_PORT
    if not dbname:
        raise ValueError(f"URL {scrub_password_from_url(url)} is missing the database name (the /dbname path component)")
    return ParsedRegistration(
        host=host,
        port=port,
        dbname=dbname,
        user=user,
        sslmode=sslmode,
        password=password,
    )


def scrub_password_from_url(url: str) -> str:
    """Return ``url`` with any embedded password replaced by ``***``.

    Fail-safe: if the URL cannot be parsed, return a fully-redacted placeholder
    rather than echoing a possibly-password-bearing raw string.
    """
    try:
        candidate = url.strip()
        prefix = ""
        if candidate.lower().startswith("jdbc:"):
            prefix, candidate = "jdbc:", candidate[len("jdbc:") :]
        parts = urlsplit(candidate)
        netloc = parts.netloc
        if parts.password:
            userinfo, _, hostinfo = netloc.rpartition("@")
            user = parts.username or ""
            netloc = f"{user}:{_PASSWORD_REDACTION}@{hostinfo}" if userinfo else hostinfo
        query = _scrub_query_password(parts.query)
        scrubbed = urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
        return prefix + scrubbed
    except (ValueError, TypeError):
        return "<url redacted>"


def _scrub_query_password(query: str) -> str:
    if not query:
        return query
    out: list[str] = []
    for pair in query.split("&"):
        key, sep, _ = pair.partition("=")
        if key.lower() == "password" and sep:
            out.append(f"{key}={_PASSWORD_REDACTION}")
        else:
            out.append(pair)
    return "&".join(out)


def _first_query(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key)
    return values[0] if values else ""


def definitive_fields(result: Any) -> dict[str, str] | None:
    """A completed address-book answer's field rows as ``{field_type: value}``, or ``None`` when not DEFINITIVE.

    Definitive only when the answer is a completed dict whose ``data`` is a dict
    whose ``entries`` is a list in which EVERY row is a mapping with a non-empty
    string ``field_type`` and a string ``value`` (``""`` is an explicit empty
    value), each field type at most once. A missing, ``None`` or non-string
    value, a missing or non-string name, a duplicate, or any row that cannot be
    type-checked makes the WHOLE answer ``None``: nothing is defaulted, coerced
    or skipped. Other keys a row carries (``description``, ``sort_order``) are
    ignored. An empty list is definitive.
    """
    if not isinstance(result, dict) or result.get("action_status") != "completed":
        return None
    data = result.get("data")
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return None
    fields: dict[str, str] = {}
    for row in entries:
        typed = _typed_row(row)
        if typed is None or typed[0] in fields:
            return None
        fields[typed[0]] = typed[1]
    return fields


def _typed_row(row: Any) -> tuple[str, str] | None:
    """One field row as ``(field_type, value)``: a mapping with a non-empty string name and a string value, else ``None``.

    The single acceptance point: both come out as PLAIN ``str``, so every later
    truth test, discovery and resolution read the same value.
    """
    if not isinstance(row, dict) or "field_type" not in row or "value" not in row:
        return None
    field_type, value = _plain_text(row["field_type"]), _plain_text(row["value"])
    if field_type is None or not field_type or value is None:
        return None
    return field_type, value


def _plain_text(value: Any) -> str | None:
    """``value`` as a plain ``str``; ``None`` when it cannot be trusted as one.

    A plain ``str`` is returned unchanged. A ``str`` subclass may override
    ``__bool__``, ``__len__`` or ``__eq__``, so it is copied ONCE to a plain
    ``str`` through the base implementation; an empty copy is uncertain
    (the subclass may be hiding content) and is rejected. Anything else is
    not text.
    """
    if type(value) is str:
        return value
    if not isinstance(value, str):
        return None
    plain = str.__str__(value)
    return plain if len(plain) > 0 else None


def _coerce_port(raw: str) -> int:
    """Blank/absent port defaults to libpq's 5432 (advisor blind-spot #1).

    A blank port would otherwise crash ``int("")`` in the containment guard or
    silently miss the platform-port compare — both are bypasses.
    """
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError as exc:
        raise ExternalPgConfigError(ERROR_CONNECTION_UNKNOWN, f"port '{raw}' is not an integer") from exc
