"""The Google Calendar API v3 document this plugin calls, at vendor revision 20261102.

google-api-python-client 2.198.0 bundles Calendar discovery revision 20260614,
which predates four vendor changes: ``calendars.transferOwnership``, the
``showOwnOrganizationOnly`` filter on calendarList.list/watch, the
``eventLabelVersion`` query parameter on events.insert/update/patch/import, and
the ``calendar.settings`` scope. ``OVERLAY_REVISION`` records the live vendor
document (calendar-json.googleapis.com/$discovery/rest?version=v3) these
deltas were copied from, verbatim. The client is built from the bundled
document plus this overlay with ``build_from_document`` — no discovery HTTP
call, no new dependency — so every verb runs the exact REST method and path
the vendor documents. Request and response bodies are passed through as JSON,
so the overlay carries no schema deltas.

``offline_service`` builds the same client with an inert transport. Building a
request (without executing it) applies the vendor document's own required,
enum, pattern and repeated rules, which is how dispatch validates arguments
before any job exists.
"""

from __future__ import annotations

import copy
import json
import re
from functools import cache
from importlib import resources
from typing import Any, Final

from ananta.core.actions.action_metadata import ParameterMetadata, ParameterType
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build_from_document, key2param
from googleapiclient.http import HttpMock

_AUTH: Final[str] = "https://www.googleapis.com/auth/"
OVERLAY_REVISION: Final[str] = "20261102"
CREDENTIAL_PARAMETERS: Final[frozenset[str]] = frozenset({"key", "access_token", "oauth_token"})

_SETTINGS_SCOPE: Final[str] = f"{_AUTH}calendar.settings"
_SHOW_OWN_ORGANIZATION_ONLY: Final[dict[str, Any]] = {
    "location": "query",
    "description": "Whether to show only entries for calendars from the organization. This parameter is only applicable to Google Workspace users. Optional. The default is False.",
    "type": "boolean",
}
_EVENT_LABEL_VERSION: Final[dict[str, Any]] = {
    "format": "int32",
    "type": "integer",
    "minimum": "0",
    "maximum": "1",
    "description": "Version number of the event label feature supported by the API client. Version 0 assumes no event label support and processes the colorId field for color management. Version 1 enables support for event labels, and processes the eventLabelId in the event's body. In this case, the colorId field is ignored. The default is 0.",
    "location": "query",
}
_TRANSFER_OWNERSHIP: Final[dict[str, Any]] = {
    "httpMethod": "POST",
    "parameterOrder": ["calendarId", "newDataOwner", "useAdminAccess"],
    "parameters": {
        "newDataOwner": {
            "description": "The email address of a user who will become the data owner of the calendar.",
            "location": "query",
            "type": "string",
            "required": True,
        },
        "useAdminAccess": {
            "required": True,
            "description": "When true, the method runs using the user's Google Workspace administrator privileges. The calling user must be a Google Workspace administrator with the Manage Calendars privilege. This method currently only supports admin access, thus only true is accepted for this field.",
            "location": "query",
            "type": "boolean",
        },
        "calendarId": {
            "description": "Calendar identifier. To retrieve calendar IDs, call the calendarList.list method.",
            "type": "string",
            "location": "path",
            "required": True,
        },
    },
    "description": "Transfers a secondary calendar between users within a Google Workspace organization. Requires user authentication with Manage Calendars administrator privilege, and one of the following authorization scopes: \n- https://www.googleapis.com/auth/calendar \n- https://www.googleapis.com/auth/calendar.calendars In the request, set useAdminAccess to true. The secondary calendar must be active to be transferred. Transferring disabled or deleted calendars isn't supported.",
    "path": "calendars/{calendarId}/transferOwnership",
    "id": "calendar.calendars.transferOwnership",
    "scopes": [f"{_AUTH}calendar", f"{_AUTH}calendar.calendars"],
}

_PARAMETER_TYPES: Final[dict[str, ParameterType]] = {
    "string": ParameterType.STRING,
    "integer": ParameterType.INTEGER,
    "boolean": ParameterType.BOOLEAN,
}


def _bundled_document() -> dict[str, Any]:
    package = resources.files("googleapiclient") / "discovery_cache" / "documents" / "calendar.v3.json"
    document: dict[str, Any] = json.loads(package.read_text(encoding="utf-8"))
    return document


def _apply_overlay(document: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``document`` carrying the revision-20261102 deltas (idempotent)."""
    patched = copy.deepcopy(document)
    patched["auth"]["oauth2"]["scopes"].setdefault(_SETTINGS_SCOPE, {"description": "View and edit your Calendar settings"})
    resources_ = patched["resources"]
    resources_["calendars"]["methods"].setdefault("transferOwnership", copy.deepcopy(_TRANSFER_OWNERSHIP))
    for name in ("list", "watch"):
        resources_["calendarList"]["methods"][name]["parameters"].setdefault("showOwnOrganizationOnly", dict(_SHOW_OWN_ORGANIZATION_ONLY))
    for name in ("insert", "update", "patch", "import"):
        resources_["events"]["methods"][name]["parameters"].setdefault("eventLabelVersion", dict(_EVENT_LABEL_VERSION))
    for resource, method in (("settings", "get"), ("settings", "list"), ("settings", "watch"), ("channels", "stop")):
        scopes: list[str] = resources_[resource]["methods"][method]["scopes"]
        if _SETTINGS_SCOPE not in scopes:
            scopes.append(_SETTINGS_SCOPE)
            scopes.sort()
    return patched


@cache
def calendar_document() -> dict[str, Any]:
    """The Calendar v3 discovery document at the overlay revision.

    Shared and cached: never hand it to ``build_from_document`` directly, which
    adds the API-wide standard parameters to every method in place.
    """
    return _apply_overlay(_bundled_document())


def vendor_method(resource: str, method: str) -> dict[str, Any]:
    """One method entry of the vendor document (httpMethod, path, parameters, scopes)."""
    entry: dict[str, Any] = calendar_document()["resources"][resource]["methods"][method]
    return entry


def build_service(access_token: str) -> Any:
    """A Calendar client that authenticates with ``access_token`` (no refresh token is handed over)."""
    return build_from_document(copy.deepcopy(calendar_document()), credentials=Credentials(token=access_token))


@cache
def offline_service() -> Any:
    """A Calendar client whose transport is never used: dispatch only builds requests with it."""
    return build_from_document(copy.deepcopy(calendar_document()), http=HttpMock())


def snake_name(vendor_name: str) -> str:
    """Keyword-safe vendor argument: ``iCalUID`` -> ``i_cal_uid``, ``$.xgafv`` -> ``x__xgafv``."""
    vendor_name = key2param(vendor_name)
    split = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", vendor_name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", split).lower()


def parameter_specs(resource: str, method: str) -> dict[str, Any]:
    """API-wide and method-specific parameters, excluding account-owned credentials."""
    specs = {**calendar_document().get("parameters", {}), **vendor_method(resource, method).get("parameters", {})}
    return {name: spec for name, spec in specs.items() if name not in CREDENTIAL_PARAMETERS}


def vendor_parameters(resource: str, method: str) -> dict[str, str]:
    """``snake_name -> vendor name`` for every path and query parameter of one vendor method."""
    return {snake_name(name): name for name in parameter_specs(resource, method)}


def parameter_metadata(resource: str, method: str) -> dict[str, ParameterMetadata]:
    """Vendor arguments plus credential names declared only to route their account-contract refusal."""
    declared: dict[str, ParameterMetadata] = {}
    for name, spec in sorted(parameter_specs(resource, method).items(), key=lambda item: (not item[1].get("required", False), item[0])):
        kind = ParameterType.LIST if spec.get("repeated") else _PARAMETER_TYPES[spec["type"]]
        declared[snake_name(name)] = ParameterMetadata(
            type=kind,
            required=bool(spec.get("required", False)),
            description=_parameter_description(name, spec),
        )
    for name in sorted(CREDENTIAL_PARAMETERS):
        declared[name] = ParameterMetadata(
            type=ParameterType.STRING,
            required=False,
            description="Refused: credentials belong to the Google account contract. Use connect_account to connect credentials and account to select a connected account.",
        )
    return declared


def _parameter_description(name: str, spec: dict[str, Any]) -> str:
    sentence = str(spec.get("description", "")).split(". ")[0].strip().rstrip(".")
    notes = [f"vendor {spec.get('location', 'query')} parameter {name}"]
    if spec.get("enum"):
        notes.append("one of " + ", ".join(str(value) for value in spec["enum"]))
    if spec.get("minimum") is not None:
        notes.append(f"minimum {spec['minimum']}")
    if spec.get("maximum") is not None:
        notes.append(f"maximum {spec['maximum']}")
    if spec.get("repeated"):
        notes.append("a list of strings")
    return f"{sentence} ({'; '.join(notes)})."
