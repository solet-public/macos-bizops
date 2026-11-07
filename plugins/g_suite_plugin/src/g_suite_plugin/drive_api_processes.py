"""Typed Drive methods from Google's discovery revision 20261106.

Each process has explicit named, typed vendor parameters and runs through the
shared account-aware deferred producer. See workspace_registry for schemas.
"""

from __future__ import annotations

from typing import Any

from ananta.core.actions.action_metadata import ContextHandling, MergeErrorProcessorCustomizations, ParameterType, ReturnValueSchema, platform_process
from ananta.core.domain.enums import ProcessorPolicyCategory

from .workspace_registry import process_parameters


class DriveAPIProcesses:
    """Complete Drive API; the plugin supplies the deferred dispatcher."""

    def _run_deferred(self, params: dict[str, Any], state: dict[str, Any], verb: str) -> dict[str, Any]:
        raise NotImplementedError

    @platform_process(
        name="drive_about_get",
        display_name="Drive About Get",
        description="drive.about.get: GET about. Gets information about the user, the user's Drive, and system capabilities. For more information, see [Return user info](https://developers.google.com/workspace/drive/api/guides/user-info). Required: The `fields` parameter must be set. To return the exact fields you need, see [Return specific fields](https://developers.google.com/workspace/drive/api/guides/fields-parameter). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_about_get"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_about_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_about_get")

    @platform_process(
        name="drive_accessproposals_get",
        display_name="Drive Accessproposals Get",
        description="drive.accessproposals.get: GET files/{fileId}/accessproposals/{proposalId}. Retrieves an access proposal by ID. For more information, see [Manage pending access proposals](https://developers.google.com/workspace/drive/api/guides/pending-access). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_accessproposals_get"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_accessproposals_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_accessproposals_get")

    @platform_process(
        name="drive_accessproposals_list",
        display_name="Drive Accessproposals List",
        description="drive.accessproposals.list: GET files/{fileId}/accessproposals. List the access proposals on a file. For more information, see [Manage pending access proposals](https://developers.google.com/workspace/drive/api/guides/pending-access). Note: Only approvers are able to list access proposals on a file. If the user isn't an approver, a 403 error is returned. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_accessproposals_list"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_accessproposals_list(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_accessproposals_list")

    @platform_process(
        name="drive_accessproposals_resolve",
        display_name="Drive Accessproposals Resolve",
        description="drive.accessproposals.resolve: POST files/{fileId}/accessproposals/{proposalId}:resolve. Approves or denies an access proposal. For more information, see [Manage pending access proposals](https://developers.google.com/workspace/drive/api/guides/pending-access). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_accessproposals_resolve"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_accessproposals_resolve(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_accessproposals_resolve")

    @platform_process(
        name="drive_approvals_approve",
        display_name="Drive Approvals Approve",
        description="drive.approvals.approve: POST files/{fileId}/approvals/{approvalId}:approve. Approves an approval. For more information, see [Manage approvals](https://developers.google.com/workspace/drive/api/guides/approvals). This is used to update the ReviewerResponse of the requesting user with a Response of `APPROVED`. If this is the last required reviewer response, this also completes the approval and sets the approval Status to `APPROVED`. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_approvals_approve"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_approvals_approve(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_approvals_approve")

    @platform_process(
        name="drive_approvals_cancel",
        display_name="Drive Approvals Cancel",
        description="drive.approvals.cancel: POST files/{fileId}/approvals/{approvalId}:cancel. Cancels an approval. For more information, see [Manage approvals](https://developers.google.com/workspace/drive/api/guides/approvals). Updates the approval Status to `CANCELLED`. This can be called by any user with the `writer` permission on the file while the approval Status is `IN_PROGRESS`. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_approvals_cancel"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_approvals_cancel(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_approvals_cancel")

    @platform_process(
        name="drive_approvals_comment",
        display_name="Drive Approvals Comment",
        description="drive.approvals.comment: POST files/{fileId}/approvals/{approvalId}:comment. Comments on an approval. For more information, see [Manage approvals](https://developers.google.com/workspace/drive/api/guides/approvals). This sends a notification to both the initiator and the reviewers. Additionally, a message is also added to the approval activity log. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_approvals_comment"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_approvals_comment(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_approvals_comment")

    @platform_process(
        name="drive_approvals_decline",
        display_name="Drive Approvals Decline",
        description="drive.approvals.decline: POST files/{fileId}/approvals/{approvalId}:decline. Declines an approval. For more information, see [Manage approvals](https://developers.google.com/workspace/drive/api/guides/approvals). This is used to update the ReviewerResponse of the requesting user with a Response of `DECLINED`. This also completes the approval and sets the approval Status to `DECLINED`. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_approvals_decline"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_approvals_decline(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_approvals_decline")

    @platform_process(
        name="drive_approvals_get",
        display_name="Drive Approvals Get",
        description="drive.approvals.get: GET files/{fileId}/approvals/{approvalId}. Gets an approval by ID. For more information, see [Manage approvals](https://developers.google.com/workspace/drive/api/guides/approvals). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_approvals_get"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_approvals_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_approvals_get")

    @platform_process(
        name="drive_approvals_list",
        display_name="Drive Approvals List",
        description="drive.approvals.list: GET files/{fileId}/approvals. Lists the approvals on a file. For more information, see [Manage approvals](https://developers.google.com/workspace/drive/api/guides/approvals). By default, this method returns a minimal response that may not include the items array. To retrieve approval details, you must explicitly specify the fields you want using the `fields` query parameter. To return the exact fields you need, see [Return specific fields](https://developers.google.com/workspace/drive/api/guides/fields-parameter). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_approvals_list"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_approvals_list(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_approvals_list")

    @platform_process(
        name="drive_approvals_reassign",
        display_name="Drive Approvals Reassign",
        description="drive.approvals.reassign: POST files/{fileId}/approvals/{approvalId}:reassign. Reassigns the reviewers on an approval. For more information, see [Manage approvals](https://developers.google.com/workspace/drive/api/guides/approvals). Adds or replaces reviewers in the ReviewerResponse of the approval. This can be called by any user with the `writer` permission on the file while the approval Status is `IN_PROGRESS` and the Response for the reviewer being reassigned is `NO_RESPONSE`. A user with the `reader` permission can only reassign an approval that's assigned to themselves. Removing a reviewer isn't allowed. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_approvals_reassign"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_approvals_reassign(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_approvals_reassign")

    @platform_process(
        name="drive_approvals_start",
        display_name="Drive Approvals Start",
        description="drive.approvals.start: POST files/{fileId}/approvals:start. Starts an approval on a file. For more information, see [Manage approvals](https://developers.google.com/workspace/drive/api/guides/approvals). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_approvals_start"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_approvals_start(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_approvals_start")

    @platform_process(
        name="drive_apps_get",
        display_name="Drive Apps Get",
        description="drive.apps.get: GET apps/{appId}. Gets a specific app. For more information, see [Return user info](https://developers.google.com/workspace/drive/api/guides/user-info). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_apps_get"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_apps_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_apps_get")

    @platform_process(
        name="drive_apps_list",
        display_name="Drive Apps List",
        description="drive.apps.list: GET apps. Lists a user's installed apps. For more information, see [Return user info](https://developers.google.com/workspace/drive/api/guides/user-info). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_apps_list"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_apps_list(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_apps_list")

    @platform_process(
        name="drive_changes_get_start_page_token",
        display_name="Drive Changes Get Start Page Token",
        description="drive.changes.getStartPageToken: GET changes/startPageToken. Gets the starting pageToken for listing future changes. For more information, see [Retrieve changes](https://developers.google.com/workspace/drive/api/guides/manage-changes). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_changes_get_start_page_token"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_changes_get_start_page_token(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_changes_get_start_page_token")

    @platform_process(
        name="drive_changes_list",
        display_name="Drive Changes List",
        description="drive.changes.list: GET changes. Lists the changes for a user or shared drive. For more information, see [Retrieve changes](https://developers.google.com/workspace/drive/api/guides/manage-changes). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_changes_list"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_changes_list(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_changes_list")

    @platform_process(
        name="drive_changes_watch",
        display_name="Drive Changes Watch",
        description="drive.changes.watch: POST changes/watch. Subscribes to changes for a user. For more information, see [Notifications for resource changes](https://developers.google.com/workspace/drive/api/guides/push). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_changes_watch"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_changes_watch(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_changes_watch")

    @platform_process(
        name="drive_channels_stop",
        display_name="Drive Channels Stop",
        description="drive.channels.stop: POST channels/stop. Stops watching resources through this channel. For more information, see [Notifications for resource changes](https://developers.google.com/workspace/drive/api/guides/push). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_channels_stop"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_channels_stop(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_channels_stop")

    @platform_process(
        name="drive_comments_create",
        display_name="Drive Comments Create",
        description="drive.comments.create: POST files/{fileId}/comments. Creates a comment on a file. For more information, see [Manage comments and replies](https://developers.google.com/workspace/drive/api/guides/manage-comments). Required: The `fields` parameter must be set. To return the exact fields you need, see [Return specific fields](https://developers.google.com/workspace/drive/api/guides/fields-parameter). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_comments_create"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_comments_create(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_comments_create")

    @platform_process(
        name="drive_comments_delete",
        display_name="Drive Comments Delete",
        description="drive.comments.delete: DELETE files/{fileId}/comments/{commentId}. Deletes a comment. For more information, see [Manage comments and replies](https://developers.google.com/workspace/drive/api/guides/manage-comments). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_comments_delete"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_comments_delete(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_comments_delete")

    @platform_process(
        name="drive_comments_get",
        display_name="Drive Comments Get",
        description="drive.comments.get: GET files/{fileId}/comments/{commentId}. Gets a comment by ID. For more information, see [Manage comments and replies](https://developers.google.com/workspace/drive/api/guides/manage-comments). Required: The `fields` parameter must be set. To return the exact fields you need, see [Return specific fields](https://developers.google.com/workspace/drive/api/guides/fields-parameter). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_comments_get"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_comments_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_comments_get")

    @platform_process(
        name="drive_comments_list",
        display_name="Drive Comments List",
        description="drive.comments.list: GET files/{fileId}/comments. Lists a file's comments. For more information, see [Manage comments and replies](https://developers.google.com/workspace/drive/api/guides/manage-comments). Required: The `fields` parameter must be set. To return the exact fields you need, see [Return specific fields](https://developers.google.com/workspace/drive/api/guides/fields-parameter). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_comments_list"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_comments_list(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_comments_list")

    @platform_process(
        name="drive_comments_update",
        display_name="Drive Comments Update",
        description="drive.comments.update: PATCH files/{fileId}/comments/{commentId}. Updates a comment with patch semantics. For more information, see [Manage comments and replies](https://developers.google.com/workspace/drive/api/guides/manage-comments). Required: The `fields` parameter must be set. To return the exact fields you need, see [Return specific fields](https://developers.google.com/workspace/drive/api/guides/fields-parameter). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_comments_update"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_comments_update(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_comments_update")

    @platform_process(
        name="drive_drives_create",
        display_name="Drive Drives Create",
        description="drive.drives.create: POST drives. Creates a shared drive. For more information, see [Manage shared drives](https://developers.google.com/workspace/drive/api/guides/manage-shareddrives). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_drives_create"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_drives_create(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_drives_create")

    @platform_process(
        name="drive_drives_delete",
        display_name="Drive Drives Delete",
        description="drive.drives.delete: DELETE drives/{driveId}. Permanently deletes a shared drive for which the user is an `organizer`. The shared drive cannot contain any untrashed items. For more information, see [Manage shared drives](https://developers.google.com/workspace/drive/api/guides/manage-shareddrives). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_drives_delete"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_drives_delete(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_drives_delete")

    @platform_process(
        name="drive_drives_get",
        display_name="Drive Drives Get",
        description="drive.drives.get: GET drives/{driveId}. Gets a shared drive's metadata by ID. For more information, see [Manage shared drives](https://developers.google.com/workspace/drive/api/guides/manage-shareddrives). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_drives_get"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_drives_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_drives_get")

    @platform_process(
        name="drive_drives_hide",
        display_name="Drive Drives Hide",
        description="drive.drives.hide: POST drives/{driveId}/hide. Hides a shared drive from the default view. For more information, see [Manage shared drives](https://developers.google.com/workspace/drive/api/guides/manage-shareddrives). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_drives_hide"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_drives_hide(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_drives_hide")

    @platform_process(
        name="drive_drives_list",
        display_name="Drive Drives List",
        description="drive.drives.list: GET drives.  Lists the user's shared drives. This method accepts the `q` parameter, which is a search query combining one or more search terms. For more information, see the [Search for shared drives](https://developers.google.com/workspace/drive/api/guides/search-shareddrives) guide. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_drives_list"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_drives_list(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_drives_list")

    @platform_process(
        name="drive_drives_unhide",
        display_name="Drive Drives Unhide",
        description="drive.drives.unhide: POST drives/{driveId}/unhide. Restores a shared drive to the default view. For more information, see [Manage shared drives](https://developers.google.com/workspace/drive/api/guides/manage-shareddrives). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_drives_unhide"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_drives_unhide(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_drives_unhide")

    @platform_process(
        name="drive_drives_update",
        display_name="Drive Drives Update",
        description="drive.drives.update: PATCH drives/{driveId}. Updates the metadata for a shared drive. For more information, see [Manage shared drives](https://developers.google.com/workspace/drive/api/guides/manage-shareddrives). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_drives_update"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_drives_update(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_drives_update")

    @platform_process(
        name="drive_files_copy",
        display_name="Drive Files Copy",
        description="drive.files.copy: POST files/{fileId}/copy. Creates a copy of a file and applies any requested updates with patch semantics. For more information, see [Create and manage files](https://developers.google.com/workspace/drive/api/guides/create-file). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_files_copy"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_files_copy(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_files_copy")

    @platform_process(
        name="drive_files_create",
        display_name="Drive Files Create",
        description='drive.files.create: POST files.  Creates a file. For more information, see [Create and manage files](https://developers.google.com/workspace/drive/api/guides/create-file). This method supports an */upload* URI and accepts uploaded media with the following characteristics: - *Maximum file size:* 5,120 GB - *Accepted Media MIME types:* `*/*` (Specify a valid MIME type, rather than the literal `*/*` value. The literal `*/*` is only used to indicate that any valid MIME type can be uploaded. For more information, see [Google Workspace and Google Drive supported MIME types](https://developers.google.com/workspace/drive/api/guides/mime-types).) For more information on uploading files, see [Upload file data](https://developers.google.com/workspace/drive/api/guides/manage-uploads). Apps creating shortcuts with the `create` method must specify the MIME type `application/vnd.google-apps.shortcut`. Apps should specify a file extension in the `name` property when inserting files with the API. For example, an operation to insert a JPEG file should specify something like `"name": "cat.jpg"` in the metadata. Subsequent `GET` requests include the read-only `fileExtension` property populated with the extension originally specified in the `name` property. When a Google Drive user requests to download a file, or when the file is downloaded through the sync client, Drive builds a full filename (with extension) based on the name. In cases where the extension is missing, Drive attempts to determine the extension based on the file\'s MIME type. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.',
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_files_create"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_files_create(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_files_create")

    @platform_process(
        name="drive_files_delete",
        display_name="Drive Files Delete",
        description="drive.files.delete: DELETE files/{fileId}. Permanently deletes a file owned by the user without moving it to the trash. For more information, see [Trash or delete files and folders](https://developers.google.com/workspace/drive/api/guides/delete). If the file belongs to a shared drive, the user must be an `organizer` on the parent folder. If the target is a folder, all descendants owned by the user are also deleted. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_files_delete"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_files_delete(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_files_delete")

    @platform_process(
        name="drive_files_download",
        display_name="Drive Files Download",
        description="drive.files.download: POST files/{fileId}/download. Downloads the content of a file. For more information, see [Download and export files](https://developers.google.com/workspace/drive/api/guides/manage-downloads). Operations are valid for 24 hours from the time of creation. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_files_download"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_files_download(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_files_download")

    @platform_process(
        name="drive_files_empty_trash",
        display_name="Drive Files Empty Trash",
        description="drive.files.emptyTrash: DELETE files/trash. Permanently deletes all of the user's trashed files. For more information, see [Trash or delete files and folders](https://developers.google.com/workspace/drive/api/guides/delete). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_files_empty_trash"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_files_empty_trash(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_files_empty_trash")

    @platform_process(
        name="drive_files_export",
        display_name="Drive Files Export",
        description="drive.files.export: GET files/{fileId}/export. Exports a Google Workspace document to the requested MIME type and returns exported byte content. For more information, see [Download and export files](https://developers.google.com/workspace/drive/api/guides/manage-downloads). Note that the exported content is limited to 10 MB. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_files_export"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_files_export(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_files_export")

    @platform_process(
        name="drive_files_generate_cse_token",
        display_name="Drive Files Generate Cse Token",
        description="drive.files.generateCseToken: GET files/generateCseToken. Generates a CSE token which can be used to create or update CSE files. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_files_generate_cse_token"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_files_generate_cse_token(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_files_generate_cse_token")

    @platform_process(
        name="drive_files_generate_ids",
        display_name="Drive Files Generate Ids",
        description="drive.files.generateIds: GET files/generateIds. Generates a set of file IDs which can be provided in create or copy requests. For more information, see [Create and manage files](https://developers.google.com/workspace/drive/api/guides/create-file). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_files_generate_ids"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_files_generate_ids(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_files_generate_ids")

    @platform_process(
        name="drive_files_get",
        display_name="Drive Files Get",
        description="drive.files.get: GET files/{fileId}.  Gets a file's metadata or content by ID. For more information, see [Search for files and folders](https://developers.google.com/workspace/drive/api/guides/search-files). If you provide the URL parameter `alt=media`, then the response includes the file contents in the response body. Downloading content with `alt=media` only works if the file is stored in Drive. To download Google Docs, Sheets, and Slides use [`files.export`](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/export) instead. For more information, see [Download and export files](https://developers.google.com/workspace/drive/api/guides/manage-downloads). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_files_get"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_files_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_files_get")

    @platform_process(
        name="drive_files_list",
        display_name="Drive Files List",
        description="drive.files.list: GET files.  Lists the user's files. For more information, see [Search for files and folders](https://developers.google.com/workspace/drive/api/guides/search-files). This method accepts the `q` parameter, which is a search query combining one or more search terms. This method returns *all* files by default, including trashed files. If you don't want trashed files to appear in the list, use the `trashed=false` query parameter to remove trashed files from the results. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_files_list"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_files_list(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_files_list")

    @platform_process(
        name="drive_files_list_labels",
        display_name="Drive Files List Labels",
        description="drive.files.listLabels: GET files/{fileId}/listLabels. Lists the labels on a file. For more information, see [List labels on a file](https://developers.google.com/workspace/drive/api/guides/list-labels). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_files_list_labels"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_files_list_labels(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_files_list_labels")

    @platform_process(
        name="drive_files_modify_labels",
        display_name="Drive Files Modify Labels",
        description="drive.files.modifyLabels: POST files/{fileId}/modifyLabels. Modifies the set of labels applied to a file. For more information, see [Set a label field on a file](https://developers.google.com/workspace/drive/api/guides/set-label). Returns a list of the labels that were added or modified. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_files_modify_labels"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_files_modify_labels(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_files_modify_labels")

    @platform_process(
        name="drive_files_update",
        display_name="Drive Files Update",
        description="drive.files.update: PATCH files/{fileId}.  Updates a file's metadata, content, or both. When calling this method, only populate fields in the request that you want to modify. When updating fields, some fields might be changed automatically, such as `modifiedDate`. This method supports patch semantics. This method supports an */upload* URI and accepts uploaded media with the following characteristics: - *Maximum file size:* 5,120 GB - *Accepted Media MIME types:* `*/*` (Specify a valid MIME type, rather than the literal `*/*` value. The literal `*/*` is only used to indicate that any valid MIME type can be uploaded. For more information, see [Google Workspace and Google Drive supported MIME types](https://developers.google.com/workspace/drive/api/guides/mime-types).) For more information on uploading files, see [Upload file data](https://developers.google.com/workspace/drive/api/guides/manage-uploads). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_files_update"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_files_update(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_files_update")

    @platform_process(
        name="drive_files_watch",
        display_name="Drive Files Watch",
        description="drive.files.watch: POST files/{fileId}/watch. Subscribes to changes to a file. For more information, see [Notifications for resource changes](https://developers.google.com/workspace/drive/api/guides/push). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_files_watch"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_files_watch(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_files_watch")

    @platform_process(
        name="drive_operations_get",
        display_name="Drive Operations Get",
        description="drive.operations.get: GET operations/{name}. Gets the latest state of a long-running operation. Clients can use this method to poll the operation result at intervals as recommended by the API service. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_operations_get"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_operations_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_operations_get")

    @platform_process(
        name="drive_permissions_create",
        display_name="Drive Permissions Create",
        description="drive.permissions.create: POST files/{fileId}/permissions. Creates a permission for a file or shared drive. For more information, see [Share files, folders, and drives](https://developers.google.com/workspace/drive/api/guides/manage-sharing). **Warning:** Concurrent permission modifications (such as update or delete) on the same file, folder, or shared drive aren't supported across any users or clients; only the last update is applied. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_permissions_create"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_permissions_create(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_permissions_create")

    @platform_process(
        name="drive_permissions_delete",
        display_name="Drive Permissions Delete",
        description="drive.permissions.delete: DELETE files/{fileId}/permissions/{permissionId}. Deletes a permission. For more information, see [Share files, folders, and drives](https://developers.google.com/workspace/drive/api/guides/manage-sharing). **Warning:** Concurrent permission modifications (such as update or delete) on the same file, folder, or shared drive aren't supported across any users or clients; only the last update is applied. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_permissions_delete"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_permissions_delete(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_permissions_delete")

    @platform_process(
        name="drive_permissions_get",
        display_name="Drive Permissions Get",
        description="drive.permissions.get: GET files/{fileId}/permissions/{permissionId}. Gets a permission by ID. For more information, see [Share files, folders, and drives](https://developers.google.com/workspace/drive/api/guides/manage-sharing). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_permissions_get"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_permissions_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_permissions_get")

    @platform_process(
        name="drive_permissions_list",
        display_name="Drive Permissions List",
        description="drive.permissions.list: GET files/{fileId}/permissions. Lists a file's or shared drive's permissions. For more information, see [Share files, folders, and drives](https://developers.google.com/workspace/drive/api/guides/manage-sharing). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_permissions_list"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_permissions_list(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_permissions_list")

    @platform_process(
        name="drive_permissions_update",
        display_name="Drive Permissions Update",
        description="drive.permissions.update: PATCH files/{fileId}/permissions/{permissionId}. Updates a permission with patch semantics. For more information, see [Share files, folders, and drives](https://developers.google.com/workspace/drive/api/guides/manage-sharing). **Warning:** Concurrent permission modifications (such as update or delete) on the same file, folder, or shared drive aren't supported across any users or clients; only the last update is applied. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_permissions_update"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_permissions_update(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_permissions_update")

    @platform_process(
        name="drive_replies_create",
        display_name="Drive Replies Create",
        description="drive.replies.create: POST files/{fileId}/comments/{commentId}/replies. Creates a reply to a comment. For more information, see [Manage comments and replies](https://developers.google.com/workspace/drive/api/guides/manage-comments). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_replies_create"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_replies_create(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_replies_create")

    @platform_process(
        name="drive_replies_delete",
        display_name="Drive Replies Delete",
        description="drive.replies.delete: DELETE files/{fileId}/comments/{commentId}/replies/{replyId}. Deletes a reply. For more information, see [Manage comments and replies](https://developers.google.com/workspace/drive/api/guides/manage-comments). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_replies_delete"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_replies_delete(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_replies_delete")

    @platform_process(
        name="drive_replies_get",
        display_name="Drive Replies Get",
        description="drive.replies.get: GET files/{fileId}/comments/{commentId}/replies/{replyId}. Gets a reply by ID. For more information, see [Manage comments and replies](https://developers.google.com/workspace/drive/api/guides/manage-comments). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_replies_get"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_replies_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_replies_get")

    @platform_process(
        name="drive_replies_list",
        display_name="Drive Replies List",
        description="drive.replies.list: GET files/{fileId}/comments/{commentId}/replies. Lists a comment's replies. For more information, see [Manage comments and replies](https://developers.google.com/workspace/drive/api/guides/manage-comments). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_replies_list"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_replies_list(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_replies_list")

    @platform_process(
        name="drive_replies_update",
        display_name="Drive Replies Update",
        description="drive.replies.update: PATCH files/{fileId}/comments/{commentId}/replies/{replyId}. Updates a reply with patch semantics. For more information, see [Manage comments and replies](https://developers.google.com/workspace/drive/api/guides/manage-comments). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_replies_update"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_replies_update(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_replies_update")

    @platform_process(
        name="drive_revisions_delete",
        display_name="Drive Revisions Delete",
        description="drive.revisions.delete: DELETE files/{fileId}/revisions/{revisionId}. Permanently deletes a file version. You can only delete revisions for files with binary content in Google Drive, like images or videos. Revisions for other files, like Google Docs or Sheets, and the last remaining file version can't be deleted. For more information, see [Manage file revisions](https://developers.google.com/drive/api/guides/manage-revisions). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_revisions_delete"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_revisions_delete(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_revisions_delete")

    @platform_process(
        name="drive_revisions_get",
        display_name="Drive Revisions Get",
        description="drive.revisions.get: GET files/{fileId}/revisions/{revisionId}. Gets a revision's metadata or content by ID. For more information, see [Manage file revisions](https://developers.google.com/workspace/drive/api/guides/manage-revisions). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_revisions_get"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_revisions_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_revisions_get")

    @platform_process(
        name="drive_revisions_list",
        display_name="Drive Revisions List",
        description="drive.revisions.list: GET files/{fileId}/revisions. Lists a file's revisions. For more information, see [Manage file revisions](https://developers.google.com/workspace/drive/api/guides/manage-revisions). **Important:** The list of revisions returned by this method might be incomplete for files with a large revision history, including frequently edited Google Docs, Sheets, and Slides. Older revisions might be omitted from the response, meaning the first revision returned may not be the oldest existing revision. The revision history visible in the Workspace editor user interface might be more complete than the list returned by the API. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_revisions_list"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_revisions_list(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_revisions_list")

    @platform_process(
        name="drive_revisions_update",
        display_name="Drive Revisions Update",
        description="drive.revisions.update: PATCH files/{fileId}/revisions/{revisionId}. Updates a revision with patch semantics. For more information, see [Manage file revisions](https://developers.google.com/workspace/drive/api/guides/manage-revisions). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_revisions_update"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_revisions_update(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_revisions_update")

    @platform_process(
        name="drive_teamdrives_create",
        display_name="Drive Teamdrives Create",
        description="drive.teamdrives.create: POST teamdrives. Deprecated: Use `drives.create` instead. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_teamdrives_create"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_teamdrives_create(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_teamdrives_create")

    @platform_process(
        name="drive_teamdrives_delete",
        display_name="Drive Teamdrives Delete",
        description="drive.teamdrives.delete: DELETE teamdrives/{teamDriveId}. Deprecated: Use `drives.delete` instead. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_teamdrives_delete"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_teamdrives_delete(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_teamdrives_delete")

    @platform_process(
        name="drive_teamdrives_get",
        display_name="Drive Teamdrives Get",
        description="drive.teamdrives.get: GET teamdrives/{teamDriveId}. Deprecated: Use `drives.get` instead. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_teamdrives_get"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_teamdrives_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_teamdrives_get")

    @platform_process(
        name="drive_teamdrives_list",
        display_name="Drive Teamdrives List",
        description="drive.teamdrives.list: GET teamdrives. Deprecated: Use `drives.list` instead. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_teamdrives_list"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_teamdrives_list(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_teamdrives_list")

    @platform_process(
        name="drive_teamdrives_update",
        display_name="Drive Teamdrives Update",
        description="drive.teamdrives.update: PATCH teamdrives/{teamDriveId}. Deprecated: Use `drives.update` instead. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("drive_teamdrives_update"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_teamdrives_update(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_teamdrives_update")
