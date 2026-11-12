"""Gmail build 2 executor: run one declarative settings/push row against Gmail.

:func:`run_settings_verb` is the whole verb body for all 47 rows in
:mod:`gmail_settings_spec` and :mod:`gmail_sharing_spec`: refuse a Workspace-only
verb on a consumer account, validate and map every parameter to the exact REST
path, query and body field, call the method on the already-built Gmail client,
and shape the response to snake_case with the path ids echoed.

Errors: invalid parameters raise ``ValueError`` (``gsuite.invalid_params``,
naming the parameter, never echoing a secret's value). A Google error is
classified with the plugin's one taxonomy, given a scope hint for the settings
scopes, and raised as :class:`SettingsVerbError`. Gmail's response is returned
as received, with its keys in snake_case; distinct keys that meet after
normalisation keep every value under `` (N)`` suffixes.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from itertools import count
from typing import Any, cast
from urllib.parse import urlencode

from .constants import ACCOUNT_TIER_CONSUMER, ERROR_MISSING_SCOPE, ERROR_PERMISSION_DENIED, GMAIL_SETTINGS_BASIC_SCOPE
from .errors import classify_exception
from .gmail_params import as_str, require_str
from .gmail_settings_spec import (
    BASIC_VERBS,
    BOOL,
    GMAIL_SETTINGS_SHARING_SCOPE,
    INT,
    INT64,
    OBJECT_LIST,
    STANDARD_QUERY_FIELDS,
    STR,
    STR_LIST,
    Field,
    SettingsVerb,
)
from .gmail_sharing_spec import SHARING_VERBS

GMAIL_SETTINGS_VERBS: dict[str, SettingsVerb] = {spec.verb: spec for spec in (*BASIC_VERBS, *SHARING_VERBS)}

# A Workspace-only Gmail feature called on a consumer (@gmail.com) account.
ERROR_WORKSPACE_ONLY = "gsuite.workspace_only"
_CAMEL = re.compile(r"(?<=[a-z0-9])([A-Z])")


class SettingsVerbError(Exception):
    """A classified settings-verb failure (code + message)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def run_settings_verb(spec: SettingsVerb, gmail_factory: Callable[[], Any], params: dict[str, Any], facts: dict[str, Any], account: str) -> dict[str, Any] | str:
    """Run one row: tier check, validation, the Gmail call and shaping."""
    try:
        _check_tier(spec, facts, account)
        kwargs, echo = _build_request(spec, params)
        response = _call_method(gmail_factory(), spec, kwargs)
        return cast(dict[str, Any], _snake_keys(_shape_result(spec, echo, response)))
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 — classified with the plugin taxonomy, re-raised typed
        code, message = classify_exception(exc, spec.verb, facts.get("granted_scopes"))
        raise SettingsVerbError(code, _scope_hint(spec, code) + message) from None


def _check_tier(spec: SettingsVerb, facts: dict[str, Any], account: str) -> None:
    """Refuse a Workspace-only verb on a consumer account with one message naming the tier.

    Pass condition: the account's recorded tier is not ``consumer``. The tier is
    recorded by the OAuth callback at connect time (``probe_account``); an
    unknown tier (connected before tiers were recorded) is never refused.
    """
    if spec.workspace_only and facts.get("account_tier") == ACCOUNT_TIER_CONSUMER:
        raise SettingsVerbError(
            ERROR_WORKSPACE_ONLY,
            f"{spec.verb} is a Google Workspace feature; account {account or 'this account'} is tier 'consumer' (a personal Gmail account), for which Google does not offer it. Call it with a Workspace account (account=<email or alias>, see list_accounts).",
        )


def _scope_hint(spec: SettingsVerb, code: str) -> str:
    """Prefix naming the settings scope a 403 is about, so it is never a bare 403."""
    if spec.scope == GMAIL_SETTINGS_SHARING_SCOPE and code in (ERROR_MISSING_SCOPE, ERROR_PERMISSION_DENIED):
        return f"{spec.verb} uses Gmail's {GMAIL_SETTINGS_SHARING_SCOPE} scope, which Google grants only to a service account with domain-wide delegation set up by a Workspace administrator; a user OAuth connection cannot be granted it. "
    if spec.scope == GMAIL_SETTINGS_BASIC_SCOPE and code == ERROR_MISSING_SCOPE:
        return f"{spec.verb} needs {GMAIL_SETTINGS_BASIC_SCOPE} (in the default consent since Gmail build 2). "
    return ""


def _build_request(spec: SettingsVerb, params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(client kwargs, path ids to echo in the result) for one call; raises ValueError."""
    kwargs: dict[str, Any] = {"userId": "me"}
    echo: dict[str, Any] = {}
    for field in spec.path:
        value = require_str(params, field.param)
        kwargs[field.api] = value
        echo[field.param] = value
    for field in (*spec.query, *STANDARD_QUERY_FIELDS):
        value = _coerce(field, params.get(field.param))
        if value is not None:
            kwargs[field.api] = value
    if spec.has_body:
        body: dict[str, Any] = {}
        for field in spec.body:
            value = _coerce(field, params.get(field.param))
            if value is not None:
                _put(body, field.api, value)
        kwargs["body"] = body
    return kwargs, echo


def _coerce(field: Field, value: Any) -> Any:
    """Validate one parameter against its kind and enum; None when absent and optional."""
    if value is None:
        if field.required:
            raise ValueError(f"'{field.param}' is required: {field.description}")
        return None
    checked = _CHECKS[field.kind](field, value)
    if field.required and checked in ("", []):
        raise ValueError(f"'{field.param}' is required and must not be empty")
    if field.enum and checked not in field.enum:
        raise ValueError(f"'{field.param}' must be one of {list(field.enum)}, got '{checked}'. {field.description}")
    return checked


def _str(field: Field, value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(f"'{field.param}' must be a string")
    return value


def _bool(field: Field, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"'{field.param}' must be true or false")
    return value


def _int(field: Field, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"'{field.param}' must be an integer")
    return value


def _int64(field: Field, value: Any) -> str:
    if isinstance(value, str) and value.isdigit():
        return value
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"'{field.param}' must be milliseconds since the epoch (a non-negative integer)")
    return str(value)


def _str_list(field: Field, value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):  # pyright: ignore[reportUnknownVariableType]
        raise ValueError(f"'{field.param}' must be a list of non-empty strings")
    return cast(list[str], value)


def _object_list(field: Field, value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):  # pyright: ignore[reportUnknownVariableType]
        raise ValueError(f"'{field.param}' must be a list of objects")
    return cast(list[dict[str, Any]], value)


_CHECKS: dict[str, Callable[[Field, Any], Any]] = {STR: _str, BOOL: _bool, INT: _int, INT64: _int64, STR_LIST: _str_list, OBJECT_LIST: _object_list}


def _put(body: dict[str, Any], dotted: str, value: Any) -> None:
    *parents, leaf = dotted.split(".")
    node = body
    for name in parents:
        node = node.setdefault(name, {})
    node[leaf] = value


def _call_method(gmail: Any, spec: SettingsVerb, kwargs: dict[str, Any]) -> Any:
    """``gmail.users().<resource>()...<method>(**kwargs).execute()`` for the row's chain."""
    node = gmail.users()
    for name in spec.chain[:-1]:
        node = getattr(node, name)()
    # Build the authenticated request with the installed static client, then
    # set wire-spelled query parameters on its HttpRequest URI. The bundled
    # discovery predates chainValidation and mangles $.xgafv's Python name;
    # neither a discovery fetch nor a version-dependent retry is needed.
    query = {key: kwargs[key] for key in ("chainValidation", "$.xgafv") if key in kwargs}
    request = getattr(node, spec.chain[-1])(**{key: value for key, value in kwargs.items() if key not in query})
    if query:
        request.uri += ("&" if "?" in request.uri else "?") + urlencode(query)
    return request.execute()


def _shape_result(spec: SettingsVerb, echo: dict[str, Any], response: Any) -> dict[str, Any]:
    payload = cast(dict[str, Any], response) if isinstance(response, dict) else {}
    if spec.list_key:
        rows = payload.get(spec.list_key) or []
        result: dict[str, Any] = {**echo, spec.rows_name: rows, "count": len(rows)}
        if spec.paged:
            result["next_page_token"] = as_str(payload.get("nextPageToken"))
        return result
    if spec.empty_result:
        return {**echo, spec.empty_result: True}
    return {**echo, **payload}


def _snake_key(key: str) -> str:
    return _CAMEL.sub(r"_\1", key).lower()


def _snake_keys(value: object) -> object:
    """Every mapping key at every depth in snake_case; every value kept unchanged.

    Distinct keys that meet after normalisation (``fieldKey`` and ``field_key``)
    never overwrite each other: the later one takes the next free `` (N)``
    suffix from one counter per mapping, starting at 2.
    """
    if isinstance(value, dict):
        result: dict[str, object] = {}
        suffixes = count(2)
        for key, item in cast(dict[object, object], value).items():
            result[_free_key(_snake_key(str(key)), result, suffixes)] = _snake_keys(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_snake_keys(item) for item in cast(list[object], value)]
    return value


def _free_key(name: str, taken: dict[str, object], suffixes: Iterator[int]) -> str:
    """``name`` when unused, else ``name (N)`` with the next counter value that is unused."""
    finished = name
    while finished in taken:
        finished = f"{name} ({next(suffixes)})"
    return finished
