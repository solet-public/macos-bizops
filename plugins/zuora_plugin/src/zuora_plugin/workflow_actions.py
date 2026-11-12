"""Zuora Workflow family: every operation in the vendor's Workflows API section.

Routes, parameters and limits were checked against the Zuora v1 API reference
(developer.zuora.com, ``/v1-api-reference/api/workflows``, reference version
2026-11-06): workflow definitions, export/import, versions, runs, tasks, task
usage metrics, and the callout authentication providers the same section
publishes. Required permissions (Workflow View/Manage/Run Access) are the
vendor's RBAC boundary; this module adds no allowlist of its own.

Lifecycle inventory (vendor-defined): a definition is created by import,
corrected by ``update_workflow`` (name/description, with an empty description
clearing it), closed and reopened through ``status`` Inactive/Active, rolled
back by setting ``active_workflow_version_id``, and deleted by
``delete_workflow``. Versions are append-only: import a higher version number,
delete one by id. A run is started, read and stopped; a task is read, rerun or
batch-corrected. Zuora publishes no ETag or version precondition on these
routes, so a concurrent edit is last-writer-wins; read before you write.

Request headers: each route takes exactly the headers the Workflow reference
documents for it (:data:`WORKFLOW_HEADER_KEYS`), a narrower set than the v1
billing routes: most document no ``Zuora-Version``, and the definition
export, version and delete routes document only ``Zuora-Track-Id``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Final

from .commerce_fields import IDEMPOTENT_HEADERS
from .typed_operations import Field, Operation, PathGate, collect, object_param, read_json_file

_WORKFLOW_ID: Final[dict[str, Field]] = {"workflow_id": Field("workflow_id", "int_id", required=True)}
_RUN_ID: Final[dict[str, Field]] = {"workflow_run_id": Field("workflow_run_id", "str_id", required=True)}
_TASK_ID: Final[dict[str, Field]] = {"task_id": Field("task_id", "str_id", required=True)}
_PROVIDER_ID: Final[dict[str, Field]] = {"provider_id": Field("provider_id", "str_id", required=True)}

# GET /workflows documents ``page_length`` maximum 50; GET /workflows/tasks documents 100.
WORKFLOW_PAGE_LENGTH_MAX: Final[int] = 50
TASK_PAGE_LENGTH_MAX: Final[int] = 100

# Reserved words the vendor documents for the Run-a-workflow request body.
RUN_RESERVED_PARAMETERS: Final[frozenset[str]] = frozenset({"controller", "action", "id", "user_email", "user_token", "format"})
WORKFLOW_STATUSES: Final[tuple[str, ...]] = ("Active", "Inactive")
AUTH_PROVIDER_TYPES: Final[tuple[str, ...]] = ("basic_auth", "oauth_1.0", "oauth_2.0", "NetsuiteTBA", "hmac", "s3")


def _definition(params: dict[str, Any], gate: PathGate) -> dict[str, Any] | None:
    """An export document from ``definition`` or a contained ``input_path`` .json backup — exactly one."""
    inline = object_param(params, "definition", required=False)
    has_path = params.get("input_path") is not None
    if (inline is None) == (not has_path):
        raise ValueError("pass exactly one of definition (an export document) or input_path (a .json backup)")
    if inline is not None:
        if not inline:
            raise ValueError("definition must be a non-empty workflow export object")
        return inline
    return read_json_file(gate, params["input_path"], param_name="input_path")


def _update_body(params: dict[str, Any], _gate: PathGate) -> dict[str, Any]:
    body = collect(
        {
            "name": Field("name", "text"),
            "description": Field("description", "optional_text"),
            "status": Field("status", "choice", choices=WORKFLOW_STATUSES),
            "active_workflow_version_id": Field("active_workflow_version_id", "int_id"),
        },
        params,
    )
    if not body:
        raise ValueError("pass at least one of name, description, status or active_workflow_version_id")
    return body


def _run_body(params: dict[str, Any], _gate: PathGate) -> dict[str, Any]:
    parameters = object_param(params, "parameters", required=False) or {}
    reserved = sorted(RUN_RESERVED_PARAMETERS.intersection(parameters))
    if reserved:
        raise ValueError(f"parameters must not use Zuora's reserved run words: {', '.join(reserved)}")
    return parameters


def _tasks_body(params: dict[str, Any], _gate: PathGate) -> dict[str, Any]:
    tasks = params.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("tasks must be a non-empty list of task objects")
    for task in tasks:
        if not isinstance(task, dict) or isinstance(task.get("id"), bool) or not isinstance(task.get("id"), int):
            raise ValueError("every task object needs its integer id")
    return {"data": tasks}


def _auth_provider_body(params: dict[str, Any], *, creating: bool) -> dict[str, Any]:
    provider = collect(
        {
            "name": Field("name", "text", required=creating),
            "service_name": Field("service_name", "text", required=creating),
            "description": Field("description", "optional_text"),
            "auth_type": Field("auth_type", "choice", required=creating, choices=AUTH_PROVIDER_TYPES),
            "active": Field("active", "bool"),
        },
        params,
    )
    if not creating and "service_name" in provider:
        raise ValueError("service_name is fixed at creation; Zuora's update accepts name, description, active and credentials")
    if not creating and "auth_type" in provider:
        raise ValueError("auth_type is fixed at creation; Zuora's update accepts name, description, active and credentials")
    credentials = object_param(params, "credentials", required=creating)
    if credentials is not None:
        provider["credentials"] = credentials
    if not provider:
        raise ValueError("pass at least one of name, description, active or credentials")
    return {"auth_provider": provider}


_TRACK: Final[tuple[str, ...]] = ("zuora_track_id",)
_SCOPED: Final[tuple[str, ...]] = ("zuora_entity_ids", "zuora_org_ids", "zuora_track_id")
_IMPORT: Final[tuple[str, ...]] = ("idempotency_key", "zuora_track_id")
# Per-route request headers, as the Workflow API reference (2026-11-06) documents them.
WORKFLOW_HEADER_KEYS: Final[dict[str, tuple[str, ...]]] = {
    "list_workflows": _SCOPED,
    "get_workflow": _SCOPED,
    "update_workflow": ("idempotency_key", *_SCOPED),
    "delete_workflow": _SCOPED,
    "export_workflow": _TRACK,
    "import_workflow": _IMPORT,
    "list_workflow_versions": _TRACK,
    "import_workflow_version": _IMPORT,
    "delete_workflow_version": _TRACK,
    "run_workflow": ("idempotency_key", *_SCOPED),
    "get_workflow_run": _SCOPED,
    "stop_workflow_run": _SCOPED,
    "list_workflow_tasks": _SCOPED,
    "get_workflow_task": _SCOPED,
    "rerun_workflow_task": ("idempotency_key", *_SCOPED),
    "update_workflow_tasks": _SCOPED,
    "get_workflow_usage": _SCOPED,
    "create_workflow_auth_provider": ("idempotency_key", "zuora_track_id", "zuora_version"),
    "update_workflow_auth_provider": ("idempotency_key", "zuora_track_id", "zuora_version"),
    "delete_workflow_auth_provider": _TRACK,
}

_ROUTES: Final[tuple[Operation, ...]] = (
    Operation(
        "list_workflows",
        "GET",
        "/workflows",
        query_fields={
            "name": Field("name", "text"),
            "callout_trigger": Field("callout_trigger", "bool"),
            "ondemand_trigger": Field("ondemand_trigger", "bool"),
            "scheduled_trigger": Field("scheduled_trigger", "bool"),
            "interval": Field("interval", "text"),
            "page": Field("page", "count"),
            "page_length": Field("page_length", "count", maximum=WORKFLOW_PAGE_LENGTH_MAX),
        },
        paging="page_number",
    ),
    Operation("get_workflow", "GET", "/workflows/{workflow_id}", path_fields=_WORKFLOW_ID),
    Operation("update_workflow", "PATCH", "/workflows/{workflow_id}", path_fields=_WORKFLOW_ID, body=_update_body),
    Operation("delete_workflow", "DELETE", "/workflows/{workflow_id}", path_fields=_WORKFLOW_ID),
    Operation(
        "export_workflow",
        "GET",
        "/workflows/{workflow_id}/export",
        path_fields=_WORKFLOW_ID,
        query_fields={"version": Field("version", "text")},
        json_output=True,
    ),
    Operation(
        "import_workflow",
        "POST",
        "/workflows/import",
        query_fields={
            "workflow_definition_id": Field("workflow_definition_id", "int_id"),
            "version": Field("version", "text"),
            "activate": Field("activate", "bool"),
        },
        body=_definition,
    ),
    Operation("list_workflow_versions", "GET", "/workflows/{workflow_id}/versions", path_fields=_WORKFLOW_ID),
    Operation(
        "import_workflow_version",
        "POST",
        "/workflows/{workflow_id}/versions/import",
        path_fields=_WORKFLOW_ID,
        query_fields={"version": Field("version", "text", required=True), "activate": Field("activate", "bool")},
        body=_definition,
    ),
    Operation(
        "delete_workflow_version",
        "DELETE",
        "/workflows/versions/{version_id}",
        path_fields={"version_id": Field("version_id", "int_id", required=True)},
    ),
    Operation("run_workflow", "POST", "/workflows/{workflow_id}/run", path_fields=_WORKFLOW_ID, body=_run_body),
    Operation("get_workflow_run", "GET", "/workflows/workflow_runs/{workflow_run_id}", path_fields=_RUN_ID),
    Operation("stop_workflow_run", "PUT", "/workflows/workflow_runs/{workflow_run_id}/stop", path_fields=_RUN_ID),
    Operation(
        "list_workflow_tasks",
        "GET",
        "/workflows/tasks",
        query_fields={
            "id": Field("id", "text"),
            "name": Field("name", "text"),
            "instance": Field("instance", "bool"),
            "action_type": Field("action_type", "text"),
            "object": Field("object", "text"),
            "object_id": Field("object_id", "text"),
            "call_type": Field("call_type", "text"),
            "workflow_id": Field("workflow_id", "text"),
            "tags": Field("tags[]", "str_list"),
            "page": Field("page", "count"),
            "page_length": Field("page_length", "count", maximum=TASK_PAGE_LENGTH_MAX),
        },
        paging="page_number",
    ),
    Operation("get_workflow_task", "GET", "/workflows/tasks/{task_id}", path_fields=_TASK_ID),
    Operation("rerun_workflow_task", "POST", "/workflows/tasks/{task_id}/rerun", path_fields=_TASK_ID),
    Operation("update_workflow_tasks", "PUT", "/workflows/tasks/batch_update", body=_tasks_body),
    Operation(
        "get_workflow_usage",
        "GET",
        "/workflows/metrics.json",
        query_fields={
            "start_date": Field("startDate", "date", required=True),
            "end_date": Field("endDate", "date", required=True),
            "metrics": Field("metrics", "choice", required=True, choices=("taskCount",), default="taskCount"),
        },
    ),
    Operation(
        "create_workflow_auth_provider",
        "POST",
        "/auth_providers",
        body=lambda params, _gate: _auth_provider_body(params, creating=True),
    ),
    Operation(
        "update_workflow_auth_provider",
        "PUT",
        "/auth_providers/{provider_id}",
        path_fields=_PROVIDER_ID,
        body=lambda params, _gate: _auth_provider_body(params, creating=False),
    ),
    Operation("delete_workflow_auth_provider", "DELETE", "/auth_providers/{provider_id}", path_fields=_PROVIDER_ID),
)

OPERATIONS: Final[tuple[Operation, ...]] = tuple(replace(op, header_fields={key: IDEMPOTENT_HEADERS[key] for key in WORKFLOW_HEADER_KEYS[op.name]}) for op in _ROUTES)
