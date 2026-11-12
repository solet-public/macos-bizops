"""Google Calendar API v3 verbs: one table row per vendor method, one vendor call per job.

Every row names a vendor method of the document in :mod:`calendar_discovery`
(revision 20261102, 38 methods). The verb name is ``calendar_<resource>_<method>``
in snake case, its arguments are the vendor's path and query parameters in snake
case plus ``body`` (the vendor request body, passed through unchanged) where the
method takes one, plus the optional ``account``. Writes and deletes are
first-class; the OAuth scopes Google granted are the only boundary.

Dispatch calls :func:`validate`: it type-checks the arguments against the vendor
document and builds the request offline, so the vendor's own required, enum and
pattern rules refuse bad input before a job exists. The worker calls
:func:`execute`, which runs exactly one vendor request and returns the vendor
body with ``next_page_token`` / ``next_sync_token`` lifted out. Vendor failures
become :class:`CalendarError` with a typed code: a 410 on a sync token asks for
a full resync, a scope the grant lacks names the scope and the reconnect step,
and a Workspace-only request from a consumer account names the account tier.
:func:`execute` returns the vendor body as received; :func:`outward_call` is the
one exit, which only types a failure.
"""

from __future__ import annotations

import json
import keyword
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final, cast

from googleapiclient.discovery import key2param
from googleapiclient.errors import HttpError

from . import calendar_discovery
from .constants import (
    CALENDAR_SCOPE_ACLS,
    CALENDAR_SCOPE_ACLS_READONLY,
    CALENDAR_SCOPE_CALENDARLIST,
    CALENDAR_SCOPE_CALENDARLIST_READONLY,
    CALENDAR_SCOPE_CALENDARS,
    CALENDAR_SCOPE_CALENDARS_READONLY,
    CALENDAR_SCOPE_EVENTS,
    CALENDAR_SCOPE_EVENTS_FREEBUSY,
    CALENDAR_SCOPE_EVENTS_READONLY,
    CALENDAR_SCOPE_SETTINGS_READONLY,
    ERROR_API_ERROR,
    ERROR_AUTH_EXPIRED,
    ERROR_INVALID_PARAMS,
    ERROR_MISSING_SCOPE,
    ERROR_NOT_FOUND,
    ERROR_PERMISSION_DENIED,
    ERROR_RATE_LIMITED,
)
from .errors import classify_exception

# Error codes. The first two match the account model's codes for every verb.
ERROR_WORKSPACE_ONLY: Final[str] = "gsuite.workspace_only"
ERROR_SYNC_TOKEN_EXPIRED: Final[str] = "gsuite.sync_token_expired"
ERROR_GONE: Final[str] = "gsuite.gone"
ERROR_CONFLICT: Final[str] = "gsuite.conflict"

TIER_CONSUMER: Final[str] = "consumer"
CONSUMER_DOMAINS: Final[frozenset[str]] = frozenset({"gmail.com", "googlemail.com"})
ACCOUNT_ARGUMENT: Final[str] = "account"
BODY_ARGUMENT: Final[str] = "body"

_RATE_REASONS: Final[frozenset[str]] = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "RATE_LIMIT_EXCEEDED"})
_SCOPE_REASONS: Final[frozenset[str]] = frozenset({"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"})
_RECONNECT: Final[str] = "Reconnect: start_interface, connect_account, authorize_url; approve Calendar access."


class CalendarError(RuntimeError):
    """A typed Calendar failure; the plugin completes the job with ``code`` and this message.

    ``vendor`` carries Google's raw response headers and error body as
    discovery sources for the exit scan; it is never outward content itself.
    """

    def __init__(self, code: str, message: str, *, vendor: object = None) -> None:
        super().__init__(message)
        self.code = code
        self.vendor = vendor


@dataclass(frozen=True)
class CalendarOperation:
    """One vendor method exposed as one typed verb."""

    resource: str
    method: str
    scope: str
    label: str
    writes: bool

    @property
    def verb(self) -> str:
        return f"calendar_{self.resource.lower()}_{calendar_discovery.snake_name(self.method)}"

    @property
    def result_type(self) -> str:
        return f"g_suite_{self.verb}_result"

    @property
    def retryable(self) -> bool:
        return not self.writes

    @property
    def http_method(self) -> str:
        return str(calendar_discovery.vendor_method(self.resource, self.method)["httpMethod"])

    @property
    def path(self) -> str:
        return str(calendar_discovery.vendor_method(self.resource, self.method)["path"])

    @property
    def vendor_scopes(self) -> frozenset[str]:
        return frozenset(calendar_discovery.vendor_method(self.resource, self.method).get("scopes", []))

    @property
    def takes_body(self) -> bool:
        return "request" in calendar_discovery.vendor_method(self.resource, self.method)


@dataclass(frozen=True)
class CalendarContext:
    """What the account resolver hands the worker for one call.

    ``granted_scopes`` and the tier are ``None`` when unknown (a credential
    recorded before grants were captured): unknown is advisory, never a refusal,
    and Google's own answer decides. ``tier`` is a callable so the tier is only
    looked up for a request that needs it.
    """

    service: Any
    account: str | None = None
    granted_scopes: frozenset[str] | None = None
    tier: Callable[[], str | None] = field(default=lambda: None)


def _op(resource: str, method: str, scope: str, label: str, *, writes: bool) -> CalendarOperation:
    return CalendarOperation(resource=resource, method=method, scope=scope, label=label, writes=writes)


OPERATIONS: Final[tuple[CalendarOperation, ...]] = (
    _op("calendarList", "list", CALENDAR_SCOPE_CALENDARLIST_READONLY, "calendar list read", writes=False),
    _op("calendarList", "get", CALENDAR_SCOPE_CALENDARLIST_READONLY, "calendar list entry read", writes=False),
    _op("calendarList", "insert", CALENDAR_SCOPE_CALENDARLIST, "calendar subscription", writes=True),
    _op("calendarList", "update", CALENDAR_SCOPE_CALENDARLIST, "calendar list entry update", writes=True),
    _op("calendarList", "patch", CALENDAR_SCOPE_CALENDARLIST, "calendar list entry change", writes=True),
    _op("calendarList", "delete", CALENDAR_SCOPE_CALENDARLIST, "calendar unsubscribe", writes=True),
    _op("calendarList", "watch", CALENDAR_SCOPE_CALENDARLIST_READONLY, "calendar list watch", writes=True),
    _op("calendars", "get", CALENDAR_SCOPE_CALENDARS_READONLY, "calendar read", writes=False),
    _op("calendars", "insert", CALENDAR_SCOPE_CALENDARS, "calendar creation", writes=True),
    _op("calendars", "update", CALENDAR_SCOPE_CALENDARS, "calendar update", writes=True),
    _op("calendars", "patch", CALENDAR_SCOPE_CALENDARS, "calendar change", writes=True),
    _op("calendars", "delete", CALENDAR_SCOPE_CALENDARS, "calendar deletion", writes=True),
    _op("calendars", "clear", CALENDAR_SCOPE_CALENDARS, "primary calendar clear", writes=True),
    _op("calendars", "transferOwnership", CALENDAR_SCOPE_CALENDARS, "calendar ownership transfer", writes=True),
    _op("events", "list", CALENDAR_SCOPE_EVENTS_READONLY, "event listing", writes=False),
    _op("events", "get", CALENDAR_SCOPE_EVENTS_READONLY, "event read", writes=False),
    _op("events", "instances", CALENDAR_SCOPE_EVENTS_READONLY, "recurring event instances", writes=False),
    _op("events", "insert", CALENDAR_SCOPE_EVENTS, "event creation", writes=True),
    _op("events", "import", CALENDAR_SCOPE_EVENTS, "event import", writes=True),
    _op("events", "quickAdd", CALENDAR_SCOPE_EVENTS, "quick-add event", writes=True),
    _op("events", "update", CALENDAR_SCOPE_EVENTS, "event update", writes=True),
    _op("events", "patch", CALENDAR_SCOPE_EVENTS, "event change", writes=True),
    _op("events", "delete", CALENDAR_SCOPE_EVENTS, "event deletion", writes=True),
    _op("events", "move", CALENDAR_SCOPE_EVENTS, "event move", writes=True),
    _op("events", "watch", CALENDAR_SCOPE_EVENTS_READONLY, "event watch", writes=True),
    _op("freebusy", "query", CALENDAR_SCOPE_EVENTS_FREEBUSY, "free/busy query", writes=False),
    _op("acl", "list", CALENDAR_SCOPE_ACLS_READONLY, "sharing rules listing", writes=False),
    _op("acl", "get", CALENDAR_SCOPE_ACLS_READONLY, "sharing rule read", writes=False),
    _op("acl", "insert", CALENDAR_SCOPE_ACLS, "sharing rule creation", writes=True),
    _op("acl", "update", CALENDAR_SCOPE_ACLS, "sharing rule update", writes=True),
    _op("acl", "patch", CALENDAR_SCOPE_ACLS, "sharing rule change", writes=True),
    _op("acl", "delete", CALENDAR_SCOPE_ACLS, "sharing rule deletion", writes=True),
    _op("acl", "watch", CALENDAR_SCOPE_ACLS_READONLY, "sharing rules watch", writes=True),
    _op("settings", "list", CALENDAR_SCOPE_SETTINGS_READONLY, "calendar settings listing", writes=False),
    _op("settings", "get", CALENDAR_SCOPE_SETTINGS_READONLY, "calendar setting read", writes=False),
    _op("settings", "watch", CALENDAR_SCOPE_SETTINGS_READONLY, "calendar settings watch", writes=True),
    _op("colors", "get", CALENDAR_SCOPE_CALENDARLIST_READONLY, "calendar colour palette read", writes=False),
    _op("channels", "stop", CALENDAR_SCOPE_CALENDARLIST_READONLY, "watch channel stop", writes=True),
)
OPERATIONS_BY_VERB: Final[dict[str, CalendarOperation]] = {op.verb: op for op in OPERATIONS}
COMPLETION_LABELS: Final[dict[str, str]] = {op.verb: f"Google Calendar {op.label}" for op in OPERATIONS}


def operation(verb: str) -> CalendarOperation:
    return OPERATIONS_BY_VERB[verb]


# ---------------------------------------------------------------------------
# Dispatch-time validation (no I/O)
# ---------------------------------------------------------------------------


def validate(op: CalendarOperation, params: dict[str, Any]) -> None:
    """Raise ``ValueError`` unless ``params`` is a request the vendor would accept in shape."""
    _vendor_kwargs(op, params)


def _vendor_kwargs(op: CalendarOperation, params: dict[str, Any]) -> dict[str, Any]:
    names = calendar_discovery.vendor_parameters(op.resource, op.method)
    _check_argument_names(op, params, names)
    specs = calendar_discovery.parameter_specs(op.resource, op.method)
    kwargs = {names[name]: _checked(op.verb, name, specs[names[name]], value) for name, value in params.items() if name in names and value is not None}
    if op.takes_body:
        kwargs["body"] = _body(op, params)
    _vendor_rules(op, kwargs)
    try:
        _request(calendar_discovery.offline_service(), op, kwargs)
    except TypeError as exc:
        raise ValueError(f"{op.verb}: {_vendor_names_to_ours(str(exc), names)}") from exc
    return kwargs


def _check_argument_names(op: CalendarOperation, params: dict[str, Any], names: dict[str, str]) -> None:
    credentials = sorted(set(params) & calendar_discovery.CREDENTIAL_PARAMETERS)
    if credentials:
        raise ValueError(f"{op.verb}: {credentials} belong to the Google account contract; use connect_account to connect credentials and 'account' to select a connected account")
    allowed = {*names, ACCOUNT_ARGUMENT, *([BODY_ARGUMENT] if op.takes_body else [])}
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise ValueError(f"{op.verb}: unknown argument(s) {unknown}; it takes {sorted(allowed)}")
    account = params.get(ACCOUNT_ARGUMENT)
    if account is not None and (not isinstance(account, str) or not account.strip()):
        raise ValueError(f"{op.verb}: 'account' must be a connected account's email or alias")


def _body(op: CalendarOperation, params: dict[str, Any]) -> dict[str, Any]:
    body = params.get(BODY_ARGUMENT)
    if not isinstance(body, dict):
        raise ValueError(f"{op.verb}: 'body' is required and must be the vendor request object (JSON object)")
    return cast(dict[str, Any], body)


def _is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in cast(list[Any], value))


# vendor parameter type -> (accepts, what the caller must pass)
_TYPE_CHECKS: Final[dict[str, tuple[Callable[[Any], bool], str]]] = {
    "boolean": (lambda value: isinstance(value, bool), "true or false"),
    "string": (lambda value: isinstance(value, str), "a string"),
    "integer": (lambda value: isinstance(value, int) and not isinstance(value, bool), "an integer"),
}


def _checked(verb: str, name: str, spec: dict[str, Any], value: Any) -> Any:
    accepts, wanted = (_is_string_list, "a list of strings") if spec.get("repeated") else _TYPE_CHECKS[spec["type"]]
    if not accepts(value):
        raise ValueError(f"{verb}: '{name}' must be {wanted}")
    if spec.get("minimum") is not None and value < int(spec["minimum"]):
        raise ValueError(f"{verb}: '{name}' must be at least {spec['minimum']}")
    if spec.get("maximum") is not None and value > int(spec["maximum"]):
        raise ValueError(f"{verb}: '{name}' must be at most {spec['maximum']}")
    return value


def _vendor_rules(op: CalendarOperation, kwargs: dict[str, Any]) -> None:
    """Rules the vendor states in prose rather than in the document's machine fields."""
    if op.method == "transferOwnership" and kwargs.get("useAdminAccess") is not True:
        raise ValueError(f"{op.verb}: 'use_admin_access' must be true; Google accepts only administrator access for this method")
    if "syncToken" not in kwargs:
        return
    excluded, never_false = _SYNC_TOKEN_RULES.get(op.resource, ((), ()))
    clash = sorted(name for name in excluded if name in kwargs) + sorted(f"{name}=false" for name in never_false if kwargs.get(name) is False)
    if clash:
        raise ValueError(f"{op.verb}: 'sync_token' cannot be combined with {clash}; Google's syncToken rules keep incremental sync consistent")


# Per the vendor's syncToken parameter text: query parameters that cannot be sent
# with a syncToken, and flags that cannot be false with one.
_SYNC_TOKEN_RULES: Final[dict[str, tuple[tuple[str, ...], tuple[str, ...]]]] = {
    "events": (("iCalUID", "orderBy", "privateExtendedProperty", "q", "sharedExtendedProperty", "timeMin", "timeMax", "updatedMin"), ("showDeleted",)),
    "calendarList": (("minAccessRole",), ("showDeleted", "showHidden")),
    "acl": ((), ("showDeleted",)),
}


def _vendor_names_to_ours(message: str, names: dict[str, str]) -> str:
    for ours, vendor in names.items():
        message = message.replace(f'"{vendor}"', f"'{ours}'")
    return message


def _request(service: Any, op: CalendarOperation, kwargs: dict[str, Any]) -> Any:
    resource = getattr(service, op.resource)()
    method = f"{op.method}_" if keyword.iskeyword(op.method) else op.method
    return getattr(resource, method)(**{key2param(name): value for name, value in kwargs.items()})


# ---------------------------------------------------------------------------
# Worker-side execution (one vendor request)
# ---------------------------------------------------------------------------


def outward_call(produce: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """The one Calendar exit: the result as received, or a classified typed error."""
    try:
        return produce()
    except Exception as exc:  # noqa: BLE001 — contain every vendor/factory exception at the Calendar boundary
        code, message = classify_exception(exc)
        raise CalendarError(code, message) from None


def execute(op: CalendarOperation, params: dict[str, Any], context: CalendarContext) -> dict[str, Any]:
    """Run one vendor request as the resolved account; raise :class:`CalendarError` on failure.

    The result and error text are returned as received through
    :func:`outward_call`.
    """
    kwargs = _vendor_kwargs(op, params)
    _require_grant(op, context.granted_scopes)
    workspace_only = _workspace_only_reason(op, kwargs)
    if workspace_only and context.tier() == TIER_CONSUMER:
        raise CalendarError(ERROR_WORKSPACE_ONLY, _tier_message(op, workspace_only, context.account, None))
    try:
        request = _request(context.service, op, kwargs)
        body: Any = request.execute()
    except HttpError as exc:
        raise _translate(op, kwargs, exc, context, workspace_only) from None
    response: dict[str, Any] = body if isinstance(body, dict) else {}
    result: dict[str, Any] = {"response": response}
    if "nextPageToken" in response:
        result["next_page_token"] = response["nextPageToken"]
    if "nextSyncToken" in response:
        result["next_sync_token"] = response["nextSyncToken"]
    if context.account:
        result["account"] = context.account
    return result


def _require_grant(op: CalendarOperation, granted: frozenset[str] | None) -> None:
    if granted is None or granted & op.vendor_scopes:
        return
    raise CalendarError(
        ERROR_MISSING_SCOPE,
        f"{op.verb} needs scope {op.scope}. {_RECONNECT}",
    )


def _workspace_only_reason(op: CalendarOperation, kwargs: dict[str, Any]) -> str:
    if op.method == "transferOwnership":
        return "Transferring calendar ownership needs a Google Workspace administrator with the Manage Calendars privilege"
    body = kwargs.get("body")
    if op.resource == "acl" and isinstance(body, dict):
        scope = body.get("scope")
        if isinstance(scope, dict) and scope.get("type") == "domain":
            return "A sharing rule for a whole domain needs a Google Workspace domain"
    return ""


def _tier_message(op: CalendarOperation, reason: str, account: str | None, vendor: str | None) -> str:
    who = f"the connected account {account}" if account else "the connected account"
    tail = f" Google said: {vendor}" if vendor else ""
    return f"{op.verb}: {reason}; {who} is a consumer (personal) Google account, which cannot do this.{tail}"


def account_tier(email: str) -> str | None:
    """``consumer`` for a gmail.com / googlemail.com address; ``None`` (unknown) otherwise."""
    return TIER_CONSUMER if email.rpartition("@")[2].lower() in CONSUMER_DOMAINS else None


def _translate(op: CalendarOperation, kwargs: dict[str, Any], exc: HttpError, context: CalendarContext, workspace_only: str) -> CalendarError:
    error = _classify(op, kwargs, exc, context, workspace_only)
    error.vendor = (exc.resp, exc.content)
    return error


def _classify(op: CalendarOperation, kwargs: dict[str, Any], exc: HttpError, context: CalendarContext, workspace_only: str) -> CalendarError:
    status, message, reasons = _http_failure(exc)
    if workspace_only and status in (400, 403):
        return CalendarError(ERROR_WORKSPACE_ONLY, f"{op.verb}: {workspace_only}; Google refused it for this account ({status}). Google said: {message}")
    if status == 410 and "syncToken" in kwargs:
        return CalendarError(
            ERROR_SYNC_TOKEN_EXPIRED,
            f"{op.verb}: Google invalidated this sync token (410 Gone). Drop sync_token and run a full sync: list again without sync_token, page with page_token to the end, keep that last page's next_sync_token, and delete any locally stored copies first.",
        )
    if status == 403 and reasons & _SCOPE_REASONS:
        return CalendarError(ERROR_MISSING_SCOPE, f"{op.verb} needs scope {op.scope}. {_RECONNECT} Google said: {message}")
    if status == 403 and reasons & _RATE_REASONS or status == 429:
        return CalendarError(ERROR_RATE_LIMITED, f"{op.verb}: Google rate limit hit ({status}); retry with exponential backoff. Google said: {message}")
    mapped = {
        400: (ERROR_INVALID_PARAMS, "Google rejected the request"),
        401: (ERROR_AUTH_EXPIRED, "Google rejected the credential; run connect_account to re-authenticate"),
        403: (ERROR_PERMISSION_DENIED, "Google denied access"),
        404: (ERROR_NOT_FOUND, "Resource not found"),
        409: (ERROR_CONFLICT, "The identifier already exists"),
        410: (ERROR_GONE, "The resource was already deleted"),
        412: (ERROR_CONFLICT, "The resource changed since it was read (etag precondition)"),
    }
    code, lead = mapped.get(status, (ERROR_API_ERROR, "Google API error"))
    return CalendarError(code, f"{op.verb}: {lead} ({status}). Google said: {message}")


def _http_failure(exc: HttpError) -> tuple[int, str, frozenset[str]]:
    """(status, Google's message, Google's reason codes) from a vendor error response."""
    status = int(getattr(exc.resp, "status", 0) or 0)
    content = exc.content.decode("utf-8", "replace")
    error = _error_object(content)
    if error is None:
        return status, content or str(exc.reason), frozenset()
    items: list[Any] = [*(error.get("errors") or []), *(error.get("details") or [])]
    reasons = {str(item["reason"]) for item in items if isinstance(item, dict) and item.get("reason")}
    return status, str(error.get("message") or exc.reason), frozenset(reasons)


def _error_object(content: str) -> dict[str, Any] | None:
    """Google's ``{"error": {...}}`` envelope, or ``None`` when the body is not one."""
    try:
        payload: Any = json.loads(content)
    except ValueError:
        return None
    error: Any = payload.get("error") if isinstance(payload, dict) else None
    return cast(dict[str, Any], error) if isinstance(error, dict) else None
