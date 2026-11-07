"""Typed Tasks methods from Google's discovery revision 20261106.

Each process has explicit named, typed vendor parameters and runs through the
shared account-aware deferred producer. See workspace_registry for schemas.
"""

from __future__ import annotations

from typing import Any

from ananta.core.actions.action_metadata import ContextHandling, MergeErrorProcessorCustomizations, ParameterType, ReturnValueSchema, platform_process
from ananta.core.domain.enums import ProcessorPolicyCategory

from .workspace_registry import process_parameters


class TasksAPIProcesses:
    """Complete Tasks API; the plugin supplies the deferred dispatcher."""

    def _run_deferred(self, params: dict[str, Any], state: dict[str, Any], verb: str) -> dict[str, Any]:
        raise NotImplementedError

    @platform_process(
        name="tasks_tasklists_delete",
        display_name="Tasks Tasklists Delete",
        description="tasks.tasklists.delete: DELETE tasks/v1/users/@me/lists/{tasklist}. Deletes the authenticated user's specified task list. If the list contains assigned tasks, both the assigned tasks and the original tasks in the assignment surface (Docs, Chat Spaces) are deleted. The user's default task list cannot be deleted. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("tasks_tasklists_delete"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def tasks_tasklists_delete(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "tasks_tasklists_delete")

    @platform_process(
        name="tasks_tasklists_get",
        display_name="Tasks Tasklists Get",
        description="tasks.tasklists.get: GET tasks/v1/users/@me/lists/{tasklist}. Returns the authenticated user's specified task list. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("tasks_tasklists_get"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def tasks_tasklists_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "tasks_tasklists_get")

    @platform_process(
        name="tasks_tasklists_insert",
        display_name="Tasks Tasklists Insert",
        description="tasks.tasklists.insert: POST tasks/v1/users/@me/lists. Creates a new task list and adds it to the authenticated user's task lists. A user can have up to 2000 lists at a time. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("tasks_tasklists_insert"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def tasks_tasklists_insert(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "tasks_tasklists_insert")

    @platform_process(
        name="tasks_tasklists_list",
        display_name="Tasks Tasklists List",
        description="tasks.tasklists.list: GET tasks/v1/users/@me/lists. Returns all the authenticated user's task lists. A user can have up to 2000 lists at a time. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("tasks_tasklists_list"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def tasks_tasklists_list(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "tasks_tasklists_list")

    @platform_process(
        name="tasks_tasklists_patch",
        display_name="Tasks Tasklists Patch",
        description="tasks.tasklists.patch: PATCH tasks/v1/users/@me/lists/{tasklist}. Updates the authenticated user's specified task list. This method supports patch semantics. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("tasks_tasklists_patch"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def tasks_tasklists_patch(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "tasks_tasklists_patch")

    @platform_process(
        name="tasks_tasklists_update",
        display_name="Tasks Tasklists Update",
        description="tasks.tasklists.update: PUT tasks/v1/users/@me/lists/{tasklist}. Updates the authenticated user's specified task list. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("tasks_tasklists_update"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def tasks_tasklists_update(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "tasks_tasklists_update")

    @platform_process(
        name="tasks_tasks_clear",
        display_name="Tasks Tasks Clear",
        description="tasks.tasks.clear: POST tasks/v1/lists/{tasklist}/clear. Clears all completed tasks from the specified task list. The affected tasks will be marked as 'hidden' and no longer be returned by default when retrieving all tasks for a task list. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("tasks_tasks_clear"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def tasks_tasks_clear(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "tasks_tasks_clear")

    @platform_process(
        name="tasks_tasks_delete",
        display_name="Tasks Tasks Delete",
        description="tasks.tasks.delete: DELETE tasks/v1/lists/{tasklist}/tasks/{task}. Deletes the specified task from the task list. If the task is assigned, both the assigned task and the original task (in Docs, Chat Spaces) are deleted. To delete the assigned task only, navigate to the assignment surface and unassign the task from there. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("tasks_tasks_delete"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def tasks_tasks_delete(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "tasks_tasks_delete")

    @platform_process(
        name="tasks_tasks_get",
        display_name="Tasks Tasks Get",
        description="tasks.tasks.get: GET tasks/v1/lists/{tasklist}/tasks/{task}. Returns the specified task. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("tasks_tasks_get"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def tasks_tasks_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "tasks_tasks_get")

    @platform_process(
        name="tasks_tasks_insert",
        display_name="Tasks Tasks Insert",
        description="tasks.tasks.insert: POST tasks/v1/lists/{tasklist}/tasks. Creates a new task on the specified task list. Tasks assigned from Docs or Chat Spaces cannot be inserted from Tasks Public API; they can only be created by assigning them from Docs or Chat Spaces. A user can have up to 20,000 non-hidden tasks per list and up to 100,000 tasks in total at a time. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("tasks_tasks_insert"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def tasks_tasks_insert(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "tasks_tasks_insert")

    @platform_process(
        name="tasks_tasks_list",
        display_name="Tasks Tasks List",
        description="tasks.tasks.list: GET tasks/v1/lists/{tasklist}/tasks. Returns all tasks in the specified task list. Doesn't return assigned tasks by default (from Docs, Chat Spaces). A user can have up to 20,000 non-hidden tasks per list and up to 100,000 tasks in total at a time. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("tasks_tasks_list"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def tasks_tasks_list(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "tasks_tasks_list")

    @platform_process(
        name="tasks_tasks_move",
        display_name="Tasks Tasks Move",
        description="tasks.tasks.move: POST tasks/v1/lists/{tasklist}/tasks/{task}/move. Moves the specified task to another position in the destination task list. If the destination list is not specified, the task is moved within its current list. This can include putting it as a child task under a new parent and/or move it to a different position among its sibling tasks. A user can have up to 2,000 subtasks per task. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("tasks_tasks_move"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def tasks_tasks_move(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "tasks_tasks_move")

    @platform_process(
        name="tasks_tasks_patch",
        display_name="Tasks Tasks Patch",
        description="tasks.tasks.patch: PATCH tasks/v1/lists/{tasklist}/tasks/{task}. Updates the specified task. This method supports patch semantics. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("tasks_tasks_patch"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def tasks_tasks_patch(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "tasks_tasks_patch")

    @platform_process(
        name="tasks_tasks_update",
        display_name="Tasks Tasks Update",
        description="tasks.tasks.update: PUT tasks/v1/lists/{tasklist}/tasks/{task}. Updates the specified task. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("tasks_tasks_update"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def tasks_tasks_update(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "tasks_tasks_update")
