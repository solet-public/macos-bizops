"""``@platform_process`` declarations for the Zuora Workflow family (mixed into ``ZuoraPlugin``).

Every verb is EDGE, dispatches in milliseconds through the plugin's D0.3
deferred-completion worker, and runs exactly one vendor operation from
``workflow_actions.OPERATIONS``. The vendor role on the registered OAuth client
(Workflow View, Manage or Run Access) decides what succeeds; a 403 completes
the job with ``zuora.permission_denied`` and Zuora's own reason.
"""

from __future__ import annotations

from typing import Any, Final

from ananta.core.actions.action_metadata import (
    ContextHandling,
    MergeErrorProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.domain.enums import ProcessorPolicyCategory

from .commerce_params import IDEMPOTENT_HEADER_PARAMS
from .workflow_actions import WORKFLOW_HEADER_KEYS


def _headers(name: str) -> dict[str, ParameterMetadata]:
    """The request headers the Workflow reference documents for this route."""
    return {key: IDEMPOTENT_HEADER_PARAMS[key] for key in WORKFLOW_HEADER_KEYS[name]}


QUEUED: Final[ReturnValueSchema] = ReturnValueSchema(
    type=ParameterType.OBJECT,
    description="Dispatch envelope — job_id + status: queued. The vendor result arrives when the job completes.",
    properties={
        "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
        "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
    },
)
_QUEUED_NOTE: Final[str] = " Returns job_id/status queued at once; dispatch is not completion."
_WORKFLOW_ID = ParameterMetadata(type=ParameterType.INTEGER, required=True, description="Workflow definition id.")
_RUN_ID = ParameterMetadata(type=ParameterType.STRING, required=True, description="Workflow run id (the id run_workflow returned).")
_TASK_ID = ParameterMetadata(type=ParameterType.STRING, required=True, description="Workflow task id.")
_PROVIDER_ID = ParameterMetadata(type=ParameterType.STRING, required=True, description="Authentication provider id.")
_PAGE = ParameterMetadata(type=ParameterType.INTEGER, required=False, description="Page number, from 1.")
_DEFINITION = ParameterMetadata(type=ParameterType.OBJECT, required=False, description="An export document (workflow, tasks, linkages). Pass this or input_path.")
_INPUT_PATH = ParameterMetadata(type=ParameterType.STRING, required=False, description="Absolute .json backup path under a registered path address. Pass this or definition.")
_ACTIVATE = ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="Make the imported version active (Zuora default false).")
_CREDENTIALS = ParameterMetadata(type=ParameterType.OBJECT, required=False, description="Credential fields for the auth_type.")
_READ: Final[MergeErrorProcessorCustomizations] = MergeErrorProcessorCustomizations(retryable=True)
_WRITE: Final[MergeErrorProcessorCustomizations] = MergeErrorProcessorCustomizations(retryable=False)
_EDGE: Final[ProcessorPolicyCategory] = ProcessorPolicyCategory.EDGE
_NONE: Final[ContextHandling] = ContextHandling.NONE


class WorkflowProcesses:
    """Typed Workflow verbs. ``ZuoraPlugin`` supplies ``_dispatch_typed``."""

    def _dispatch_typed(self, action_name: str, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @platform_process(
        name="list_workflows",
        display_name="Zuora: List Workflows",
        description="List workflow definitions (GET /workflows) with optional name, trigger and interval filters, one page per call (page_length up to 50). Completion carries data, the vendor pagination object and next_page." + _QUEUED_NOTE,
        processor_policy_category=_EDGE,
        parameters={
            **_headers("list_workflows"),
            "name": ParameterMetadata(type=ParameterType.STRING, required=False, description="Filter by workflow name."),
            "callout_trigger": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="Filter by callout trigger."),
            "ondemand_trigger": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="Filter by on-demand trigger."),
            "scheduled_trigger": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="Filter by scheduled trigger."),
            "interval": ParameterMetadata(type=ParameterType.STRING, required=False, description="Filter by CRON interval."),
            "page": _PAGE,
            "page_length": ParameterMetadata(type=ParameterType.INTEGER, required=False, description="Definitions per page, 1-50."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=_READ,
        context_handling=_NONE,
    )
    def list_workflows(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_workflows", params, state)

    @platform_process(
        name="get_workflow",
        display_name="Zuora: Get Workflow",
        description="Read one workflow definition and its active version (GET /workflows/{workflow_id})." + _QUEUED_NOTE,
        processor_policy_category=_EDGE,
        parameters={**_headers("get_workflow"), "workflow_id": _WORKFLOW_ID},
        return_value_schema=QUEUED,
        error_processor_customizations=_READ,
        context_handling=_NONE,
    )
    def get_workflow(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_workflow", params, state)

    @platform_process(
        name="update_workflow",
        display_name="Zuora: Update Workflow",
        description="Correct a workflow definition (PATCH /workflows/{workflow_id}): rename, set or clear (empty string) the description, set status Active or Inactive, or roll the active version to another version id." + _QUEUED_NOTE,
        processor_policy_category=_EDGE,
        parameters={
            **_headers("update_workflow"),
            "workflow_id": _WORKFLOW_ID,
            "name": ParameterMetadata(type=ParameterType.STRING, required=False, description="New name."),
            "description": ParameterMetadata(type=ParameterType.STRING, required=False, description="New description; an empty string clears it."),
            "status": ParameterMetadata(type=ParameterType.STRING, required=False, description="Active or Inactive."),
            "active_workflow_version_id": ParameterMetadata(type=ParameterType.INTEGER, required=False, description="Version id to make active."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=_WRITE,
        context_handling=_NONE,
    )
    def update_workflow(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_workflow", params, state)

    @platform_process(
        name="delete_workflow",
        display_name="Zuora: Delete Workflow",
        description="Delete a workflow definition (DELETE /workflows/{workflow_id}); Zuora answers with the id and success. Export it first if a backup is wanted." + _QUEUED_NOTE,
        processor_policy_category=_EDGE,
        parameters={**_headers("delete_workflow"), "workflow_id": _WORKFLOW_ID},
        return_value_schema=QUEUED,
        error_processor_customizations=_WRITE,
        context_handling=_NONE,
    )
    def delete_workflow(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("delete_workflow", params, state)

    @platform_process(
        name="export_workflow",
        display_name="Zuora: Export Workflow",
        description="Export a workflow version as JSON (GET /workflows/{workflow_id}/export): the active version by default, or a version number or 'latest'. With output_path the export is written to a contained .json backup file and a path/bytes/sha256 handle is returned." + _QUEUED_NOTE,
        processor_policy_category=_EDGE,
        parameters={
            **_headers("export_workflow"),
            "workflow_id": _WORKFLOW_ID,
            "version": ParameterMetadata(type=ParameterType.STRING, required=False, description="Version number or 'latest'; default is the active version."),
            "output_path": ParameterMetadata(type=ParameterType.STRING, required=False, description="Absolute .json path under a registered path address."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=_READ,
        context_handling=_NONE,
    )
    def export_workflow(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("export_workflow", params, state)

    @platform_process(
        name="import_workflow",
        display_name="Zuora: Import Workflow",
        description="Create a workflow definition and its first version from an export document (POST /workflows/import), inline or from a contained .json backup; optional workflow_definition_id, version and activate." + _QUEUED_NOTE,
        processor_policy_category=_EDGE,
        parameters={
            **_headers("import_workflow"),
            "definition": _DEFINITION,
            "input_path": _INPUT_PATH,
            "workflow_definition_id": ParameterMetadata(type=ParameterType.INTEGER, required=False, description="Existing definition to import under."),
            "version": ParameterMetadata(type=ParameterType.STRING, required=False, description="Version number for the imported version."),
            "activate": _ACTIVATE,
        },
        return_value_schema=QUEUED,
        error_processor_customizations=_WRITE,
        context_handling=_NONE,
    )
    def import_workflow(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("import_workflow", params, state)

    @platform_process(
        name="list_workflow_versions",
        display_name="Zuora: List Workflow Versions",
        description="List every version of a workflow definition (GET /workflows/{workflow_id}/versions)." + _QUEUED_NOTE,
        processor_policy_category=_EDGE,
        parameters={**_headers("list_workflow_versions"), "workflow_id": _WORKFLOW_ID},
        return_value_schema=QUEUED,
        error_processor_customizations=_READ,
        context_handling=_NONE,
    )
    def list_workflow_versions(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_workflow_versions", params, state)

    @platform_process(
        name="import_workflow_version",
        display_name="Zuora: Import Workflow Version",
        description="Add a new version under an existing workflow (POST /workflows/{workflow_id}/versions/import); version is required and must exceed every existing version number. Source is definition or a contained .json backup." + _QUEUED_NOTE,
        processor_policy_category=_EDGE,
        parameters={
            **_headers("import_workflow_version"),
            "workflow_id": _WORKFLOW_ID,
            "version": ParameterMetadata(type=ParameterType.STRING, required=True, description="New version number, greater than any existing one."),
            "definition": _DEFINITION,
            "input_path": _INPUT_PATH,
            "activate": _ACTIVATE,
        },
        return_value_schema=QUEUED,
        error_processor_customizations=_WRITE,
        context_handling=_NONE,
    )
    def import_workflow_version(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("import_workflow_version", params, state)

    @platform_process(
        name="delete_workflow_version",
        display_name="Zuora: Delete Workflow Version",
        description="Delete one workflow version by version id (DELETE /workflows/versions/{version_id}); Zuora refuses deleting a version it still needs." + _QUEUED_NOTE,
        processor_policy_category=_EDGE,
        parameters={**_headers("delete_workflow_version"), "version_id": ParameterMetadata(type=ParameterType.INTEGER, required=True, description="Workflow version id.")},
        return_value_schema=QUEUED,
        error_processor_customizations=_WRITE,
        context_handling=_NONE,
    )
    def delete_workflow_version(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("delete_workflow_version", params, state)

    @platform_process(
        name="run_workflow",
        display_name="Zuora: Run Workflow",
        description="Start a workflow run (POST /workflows/{workflow_id}/run) with optional input parameters; Zuora's reserved words controller, action, id, user_email, user_token and format are refused. Completion carries the run id and status." + _QUEUED_NOTE,
        processor_policy_category=_EDGE,
        parameters={
            **_headers("run_workflow"),
            "workflow_id": _WORKFLOW_ID,
            "parameters": ParameterMetadata(type=ParameterType.OBJECT, required=False, description="Run input parameters defined on the workflow."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=_WRITE,
        context_handling=_NONE,
    )
    def run_workflow(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("run_workflow", params, state)

    @platform_process(
        name="get_workflow_run",
        display_name="Zuora: Get Workflow Run",
        description="Read a workflow run's status, timing, messages and task summary (GET /workflows/workflow_runs/{workflow_run_id})." + _QUEUED_NOTE,
        processor_policy_category=_EDGE,
        parameters={**_headers("get_workflow_run"), "workflow_run_id": _RUN_ID},
        return_value_schema=QUEUED,
        error_processor_customizations=_READ,
        context_handling=_NONE,
    )
    def get_workflow_run(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_workflow_run", params, state)

    @platform_process(
        name="stop_workflow_run",
        display_name="Zuora: Stop Workflow Run",
        description="Stop a running workflow run (PUT /workflows/workflow_runs/{workflow_run_id}/stop); the run's terminal action." + _QUEUED_NOTE,
        processor_policy_category=_EDGE,
        parameters={**_headers("stop_workflow_run"), "workflow_run_id": _RUN_ID},
        return_value_schema=QUEUED,
        error_processor_customizations=_WRITE,
        context_handling=_NONE,
    )
    def stop_workflow_run(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("stop_workflow_run", params, state)

    @platform_process(
        name="list_workflow_tasks",
        display_name="Zuora: List Workflow Tasks",
        description="List workflow tasks (GET /workflows/tasks) filtered by id, name, instance, action_type, object, object_id, call_type, workflow_id or tags, one page per call (page_length up to 100)." + _QUEUED_NOTE,
        processor_policy_category=_EDGE,
        parameters={
            **_headers("list_workflow_tasks"),
            "id": ParameterMetadata(type=ParameterType.STRING, required=False, description="Task id filter."),
            "name": ParameterMetadata(type=ParameterType.STRING, required=False, description="Task name filter."),
            "instance": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="True for run instances, false for definitions."),
            "action_type": ParameterMetadata(type=ParameterType.STRING, required=False, description="Task action type filter."),
            "object": ParameterMetadata(type=ParameterType.STRING, required=False, description="Zuora object filter."),
            "object_id": ParameterMetadata(type=ParameterType.STRING, required=False, description="Zuora object id filter."),
            "call_type": ParameterMetadata(type=ParameterType.STRING, required=False, description="API call type filter."),
            "workflow_id": ParameterMetadata(type=ParameterType.STRING, required=False, description="Workflow id filter."),
            "tags": ParameterMetadata(type=ParameterType.LIST, required=False, description="Tags; tasks with any tag match."),
            "page": _PAGE,
            "page_length": ParameterMetadata(type=ParameterType.INTEGER, required=False, description="Tasks per page, 1-100."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=_READ,
        context_handling=_NONE,
    )
    def list_workflow_tasks(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_workflow_tasks", params, state)

    @platform_process(
        name="get_workflow_task",
        display_name="Zuora: Get Workflow Task",
        description="Read one workflow task with its status, parameters, data and error detail (GET /workflows/tasks/{task_id})." + _QUEUED_NOTE,
        processor_policy_category=_EDGE,
        parameters={**_headers("get_workflow_task"), "task_id": _TASK_ID},
        return_value_schema=QUEUED,
        error_processor_customizations=_READ,
        context_handling=_NONE,
    )
    def get_workflow_task(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_workflow_task", params, state)

    @platform_process(
        name="rerun_workflow_task",
        display_name="Zuora: Rerun Workflow Task",
        description="Rerun one workflow task (POST /workflows/tasks/{task_id}/rerun), typically after an error; completion carries the task record." + _QUEUED_NOTE,
        processor_policy_category=_EDGE,
        parameters={**_headers("rerun_workflow_task"), "task_id": _TASK_ID},
        return_value_schema=QUEUED,
        error_processor_customizations=_WRITE,
        context_handling=_NONE,
    )
    def rerun_workflow_task(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("rerun_workflow_task", params, state)

    @platform_process(
        name="update_workflow_tasks",
        display_name="Zuora: Update Workflow Tasks",
        description="Batch-correct workflow tasks (PUT /workflows/tasks/batch_update); each task object needs its integer id plus fields such as name, status, tags, object, object_id or concurrent_limit." + _QUEUED_NOTE,
        processor_policy_category=_EDGE,
        parameters={**_headers("update_workflow_tasks"), "tasks": ParameterMetadata(type=ParameterType.LIST, required=True, description="Task objects, each with its integer id.")},
        return_value_schema=QUEUED,
        error_processor_customizations=_WRITE,
        context_handling=_NONE,
    )
    def update_workflow_tasks(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_workflow_tasks", params, state)

    @platform_process(
        name="get_workflow_usage",
        display_name="Zuora: Get Workflow Usage",
        description="Read daily workflow task-run counts between start_date and end_date (GET /workflows/metrics.json, metric taskCount)." + _QUEUED_NOTE,
        processor_policy_category=_EDGE,
        parameters={
            **_headers("get_workflow_usage"),
            "start_date": ParameterMetadata(type=ParameterType.STRING, required=True, description="YYYY-MM-DD start date."),
            "end_date": ParameterMetadata(type=ParameterType.STRING, required=True, description="YYYY-MM-DD end date."),
            "metrics": ParameterMetadata(type=ParameterType.STRING, required=False, description="Metric name; Zuora supports taskCount (the default)."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=_READ,
        context_handling=_NONE,
    )
    def get_workflow_usage(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_workflow_usage", params, state)

    @platform_process(
        name="create_workflow_auth_provider",
        display_name="Zuora: Create Workflow Auth Provider",
        description="Create a workflow callout authentication provider (POST /auth_providers) with name, service_name, auth_type (basic_auth, oauth_1.0, oauth_2.0, NetsuiteTBA, hmac, s3) and credentials." + _QUEUED_NOTE,
        processor_policy_category=_EDGE,
        parameters={
            **_headers("create_workflow_auth_provider"),
            "name": ParameterMetadata(type=ParameterType.STRING, required=True, description="Display name."),
            "service_name": ParameterMetadata(type=ParameterType.STRING, required=True, description="Logical service identifier."),
            "auth_type": ParameterMetadata(type=ParameterType.STRING, required=True, description="basic_auth, oauth_1.0, oauth_2.0, NetsuiteTBA, hmac or s3."),
            "credentials": ParameterMetadata(type=ParameterType.OBJECT, required=True, description="Credential fields for the auth_type."),
            "description": ParameterMetadata(type=ParameterType.STRING, required=False, description="Description."),
            "active": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="Whether the provider is active."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=_WRITE,
        context_handling=_NONE,
    )
    def create_workflow_auth_provider(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_workflow_auth_provider", params, state)

    @platform_process(
        name="update_workflow_auth_provider",
        display_name="Zuora: Update Workflow Auth Provider",
        description="Correct a workflow authentication provider (PUT /auth_providers/{provider_id}): name, description (empty clears), active to deactivate or reactivate, or rotated credentials." + _QUEUED_NOTE,
        processor_policy_category=_EDGE,
        parameters={
            **_headers("update_workflow_auth_provider"),
            "provider_id": _PROVIDER_ID,
            "name": ParameterMetadata(type=ParameterType.STRING, required=False, description="New display name."),
            "description": ParameterMetadata(type=ParameterType.STRING, required=False, description="New description; empty string clears it."),
            "active": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="False deactivates, true reactivates."),
            "credentials": _CREDENTIALS,
        },
        return_value_schema=QUEUED,
        error_processor_customizations=_WRITE,
        context_handling=_NONE,
    )
    def update_workflow_auth_provider(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_workflow_auth_provider", params, state)

    @platform_process(
        name="delete_workflow_auth_provider",
        display_name="Zuora: Delete Workflow Auth Provider",
        description="Delete a workflow authentication provider (DELETE /auth_providers/{provider_id}); Zuora answers status deleted." + _QUEUED_NOTE,
        processor_policy_category=_EDGE,
        parameters={**_headers("delete_workflow_auth_provider"), "provider_id": _PROVIDER_ID},
        return_value_schema=QUEUED,
        error_processor_customizations=_WRITE,
        context_handling=_NONE,
    )
    def delete_workflow_auth_provider(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("delete_workflow_auth_provider", params, state)
