"""Per-call site context, explicitly restored by deferred completion workers."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from functools import wraps
from typing import Any

from ananta.core.actions.action_metadata import ParameterMetadata, ParameterType

from .errors import JiraServiceError

CURRENT_SITE: ContextVar[str] = ContextVar("jira_site", default="legacy")
CURRENT_BINDING: ContextVar[str | None] = ContextVar("jira_binding", default=None)
SITE_PARAMETER = ParameterMetadata(
    type=ParameterType.STRING,
    required=False,
    description="Bound site alias; omitted uses the default. legacy selects address-book jira_site.",
)


def site_parameters(parameters: dict[str, ParameterMetadata]) -> dict[str, ParameterMetadata]:
    return {"site": SITE_PARAMETER, **parameters}


def selected_site(function: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Pin the canonical site in params and isolate concurrent calls by context."""

    @wraps(function)
    def selected(plugin: Any, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return _selected(plugin, function, params, state)

    return selected


def _selected(plugin: Any, function: Callable[..., dict[str, Any]], params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    try:
        site = plugin._select_site(params.get("site"))
    except JiraServiceError as exc:
        return plugin._error(exc.code)
    except ValueError:
        return plugin._error("jira.invalid_params")
    except Exception:  # noqa: BLE001 — fixed pre-credential dispatch failure
        return plugin._error("jira.api_error")
    token = CURRENT_SITE.set(site)
    try:
        return function(plugin, {**params, "site": site}, state)
    except Exception:  # noqa: BLE001 — contain faults before worker credential resolution
        return plugin._error("jira.api_error")
    finally:
        CURRENT_SITE.reset(token)
