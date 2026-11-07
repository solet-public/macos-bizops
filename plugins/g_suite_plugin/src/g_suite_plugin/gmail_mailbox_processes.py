"""``@platform_process`` Gmail label, draft, history and profile verbs (mixed into ``GSuitePlugin``).

Deferred-completion EDGE verbs: dispatch validates and enqueues; the worker runs
the action from :data:`gmail_registry.GMAIL_OPERATIONS` as the resolved account.
"""

from __future__ import annotations

from typing import Any

from ananta.core.actions.action_metadata import (
    ContextHandling,
    MergeErrorProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.domain.enums import ProcessorPolicyCategory

from .account_processes import ACCOUNT_PARAM

_QUEUED = ReturnValueSchema(type=ParameterType.OBJECT, description="job_id for the queued Gmail call plus its status ('queued').")
_NOTE = " Runs as a background job: returns a job_id at once and the result arrives as a follow-up message. Optional account (email or alias) picks the connected Google account; the result names the account used."


class GmailMailboxProcesses:
    """Gmail verbs; ``GSuitePlugin`` supplies ``_run_deferred``."""

    def _run_deferred(self, params: dict[str, Any], state: dict[str, Any], verb: str) -> dict[str, Any]:
        raise NotImplementedError

    @platform_process(
        name="gmail_list_labels",
        display_name="Gmail: List Labels",
        description="List every label in the mailbox (users.labels.list): system labels (INBOX, UNREAD, STARRED, SENT, CATEGORY_*) and user labels with id, name, type, visibility and color. Use it to turn label ids on messages into names." + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def gmail_list_labels(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_list_labels")

    @platform_process(
        name="gmail_get_label",
        display_name="Gmail: Get Label",
        description="Read one label with its counts (users.labels.get): messages_total, messages_unread, threads_total, threads_unread, plus name, type, visibility and color." + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The label id (from gmail_list_labels), e.g. Label_12 or a system id such as STARRED."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def gmail_get_label(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_get_label")

    @platform_process(
        name="gmail_create_label",
        display_name="Gmail: Create Label",
        description="Create a user label (users.labels.create) with a name (nest with '/'), optional sidebar and message-list visibility, and optional palette colors." + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "name": ParameterMetadata(type=ParameterType.STRING, required=False, description="Label name; nest with '/', e.g. Clients/Acme."),
            "label_list_visibility": ParameterMetadata(type=ParameterType.STRING, required=False, description="labelShow, labelShowIfUnread or labelHide (label list in the sidebar)."),
            "message_list_visibility": ParameterMetadata(type=ParameterType.STRING, required=False, description="show or hide (label chip on messages)."),
            "text_color": ParameterMetadata(type=ParameterType.STRING, required=False, description="Text color hex from Gmail's label palette, e.g. #000000; pass with background_color."),
            "background_color": ParameterMetadata(type=ParameterType.STRING, required=False, description="Background color hex from Gmail's label palette, e.g. #16a766; pass with text_color."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def gmail_create_label(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_create_label")

    @platform_process(
        name="gmail_patch_label",
        display_name="Gmail: Patch Label",
        description="Change only the label fields passed (users.labels.patch): rename, change visibility or color. Fields not passed stay as they are." + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The label id (from gmail_list_labels), e.g. Label_12 or a system id such as STARRED."),
            "name": ParameterMetadata(type=ParameterType.STRING, required=False, description="Label name; nest with '/', e.g. Clients/Acme."),
            "label_list_visibility": ParameterMetadata(type=ParameterType.STRING, required=False, description="labelShow, labelShowIfUnread or labelHide (label list in the sidebar)."),
            "message_list_visibility": ParameterMetadata(type=ParameterType.STRING, required=False, description="show or hide (label chip on messages)."),
            "text_color": ParameterMetadata(type=ParameterType.STRING, required=False, description="Text color hex from Gmail's label palette, e.g. #000000; pass with background_color."),
            "background_color": ParameterMetadata(type=ParameterType.STRING, required=False, description="Background color hex from Gmail's label palette, e.g. #16a766; pass with text_color."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def gmail_patch_label(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_patch_label")

    @platform_process(
        name="gmail_update_label",
        display_name="Gmail: Replace Label",
        description="Replace a label resource in full (users.labels.update, HTTP PUT): name is required and visibility or color fields not passed are reset to Gmail's defaults. Use gmail_patch_label to change one field." + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The label id (from gmail_list_labels), e.g. Label_12 or a system id such as STARRED."),
            "name": ParameterMetadata(type=ParameterType.STRING, required=False, description="Label name; nest with '/', e.g. Clients/Acme."),
            "label_list_visibility": ParameterMetadata(type=ParameterType.STRING, required=False, description="labelShow, labelShowIfUnread or labelHide (label list in the sidebar)."),
            "message_list_visibility": ParameterMetadata(type=ParameterType.STRING, required=False, description="show or hide (label chip on messages)."),
            "text_color": ParameterMetadata(type=ParameterType.STRING, required=False, description="Text color hex from Gmail's label palette, e.g. #000000; pass with background_color."),
            "background_color": ParameterMetadata(type=ParameterType.STRING, required=False, description="Background color hex from Gmail's label palette, e.g. #16a766; pass with text_color."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def gmail_update_label(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_update_label")

    @platform_process(
        name="gmail_delete_label",
        display_name="Gmail: Delete Label",
        description="Delete a user label (users.labels.delete). The messages it was on are kept; only the label is removed from them. System labels cannot be deleted." + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The label id (from gmail_list_labels), e.g. Label_12 or a system id such as STARRED."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def gmail_delete_label(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_delete_label")

    @platform_process(
        name="gmail_create_draft",
        display_name="Gmail: Create Draft",
        description="Create a draft (users.drafts.create) from to, cc, bcc, subject, plain and HTML body and blob attachments; a reply draft passes thread_id plus in_reply_to and references. Returns the draft id and its message ids." + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "to": ParameterMetadata(type=ParameterType.STRING, required=True, description="Recipient address(es), comma-separated."),
            "cc": ParameterMetadata(type=ParameterType.STRING, required=False, description="Cc address(es), comma-separated."),
            "bcc": ParameterMetadata(type=ParameterType.STRING, required=False, description="Bcc address(es), comma-separated."),
            "subject": ParameterMetadata(type=ParameterType.STRING, required=False, description="Subject line."),
            "body": ParameterMetadata(type=ParameterType.STRING, required=False, description="Plain-text body."),
            "html_body": ParameterMetadata(type=ParameterType.STRING, required=False, description="Optional HTML body, sent as a text/html alternative to body."),
            "attachments": ParameterMetadata(type=ParameterType.LIST, required=False, description="Blob ids (from blob storage) to attach."),
            "thread_id": ParameterMetadata(type=ParameterType.STRING, required=False, description="Thread id, to keep a reply in its conversation."),
            "in_reply_to": ParameterMetadata(type=ParameterType.STRING, required=False, description="Message-ID header of the message being replied to (from gmail_get_message headers)."),
            "references": ParameterMetadata(type=ParameterType.STRING, required=False, description="References header for a reply (the parent's References plus its Message-ID)."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def gmail_create_draft(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_create_draft")

    @platform_process(
        name="gmail_get_draft",
        display_name="Gmail: Get Draft",
        description="Read one draft (users.drafts.get) with its message in full (parsed), raw, metadata or minimal format." + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The draft id (from gmail_list_drafts or gmail_create_draft)."),
            "format": ParameterMetadata(type=ParameterType.STRING, required=False, description="full (default), raw, metadata or minimal."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def gmail_get_draft(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_get_draft")

    @platform_process(
        name="gmail_list_drafts",
        display_name="Gmail: List Drafts",
        description="List drafts (users.drafts.list), optionally filtered by Gmail query, one page at a time with next_page_token. Returns draft ids with their message and thread ids." + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "query": ParameterMetadata(type=ParameterType.STRING, required=False, description="Gmail search query using Gmail's own operators (from:, to:, subject:, is:unread, has:attachment, label:, newer_than:, before:). Empty matches everything."),
            "max": ParameterMetadata(type=ParameterType.INTEGER, required=False, description="Rows per page (default 500, Gmail's per-call maximum)."),
            "page_token": ParameterMetadata(type=ParameterType.STRING, required=False, description="next_page_token from the previous page; omit for the first page."),
            "include_spam_trash": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="Also match Spam and Trash (default false)."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def gmail_list_drafts(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_list_drafts")

    @platform_process(
        name="gmail_update_draft",
        display_name="Gmail: Update Draft",
        description="Replace a draft's content (users.drafts.update): recipients, subject, bodies, attachments and reply threading as in gmail_create_draft." + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The draft id (from gmail_list_drafts or gmail_create_draft)."),
            "to": ParameterMetadata(type=ParameterType.STRING, required=True, description="Recipient address(es), comma-separated."),
            "cc": ParameterMetadata(type=ParameterType.STRING, required=False, description="Cc address(es), comma-separated."),
            "bcc": ParameterMetadata(type=ParameterType.STRING, required=False, description="Bcc address(es), comma-separated."),
            "subject": ParameterMetadata(type=ParameterType.STRING, required=False, description="Subject line."),
            "body": ParameterMetadata(type=ParameterType.STRING, required=False, description="Plain-text body."),
            "html_body": ParameterMetadata(type=ParameterType.STRING, required=False, description="Optional HTML body, sent as a text/html alternative to body."),
            "attachments": ParameterMetadata(type=ParameterType.LIST, required=False, description="Blob ids (from blob storage) to attach."),
            "thread_id": ParameterMetadata(type=ParameterType.STRING, required=False, description="Thread id, to keep a reply in its conversation."),
            "in_reply_to": ParameterMetadata(type=ParameterType.STRING, required=False, description="Message-ID header of the message being replied to (from gmail_get_message headers)."),
            "references": ParameterMetadata(type=ParameterType.STRING, required=False, description="References header for a reply (the parent's References plus its Message-ID)."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def gmail_update_draft(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_update_draft")

    @platform_process(
        name="gmail_send_draft",
        display_name="Gmail: Send Draft",
        description="Send an existing draft exactly as saved (users.drafts.send); the draft is consumed and the sent message id, thread id and labels are returned." + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The draft id (from gmail_list_drafts or gmail_create_draft)."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def gmail_send_draft(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_send_draft")

    @platform_process(
        name="gmail_delete_draft",
        display_name="Gmail: Delete Draft",
        description="Permanently delete a draft (users.drafts.delete); drafts are not moved to Trash." + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The draft id (from gmail_list_drafts or gmail_create_draft)."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def gmail_delete_draft(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_delete_draft")

    @platform_process(
        name="gmail_list_history",
        display_name="Gmail: List History",
        description="List mailbox changes since start_history_id (users.history.list): messages added and deleted and labels added and removed, with the current history_id and next_page_token. Get a starting history id from gmail_get_profile or any message. An expired start id returns gsuite.not_found; then re-sync from a fresh profile history id."
        + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "start_history_id": ParameterMetadata(type=ParameterType.STRING, required=True, description="History id to read changes after (from gmail_get_profile, a message or a previous call)."),
            "history_types": ParameterMetadata(type=ParameterType.LIST, required=False, description="Only these change types: messageAdded, messageDeleted, labelAdded, labelRemoved."),
            "label_id": ParameterMetadata(type=ParameterType.STRING, required=False, description="Only changes involving this label."),
            "max": ParameterMetadata(type=ParameterType.INTEGER, required=False, description="Records per page (default 500)."),
            "page_token": ParameterMetadata(type=ParameterType.STRING, required=False, description="next_page_token from the previous page."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def gmail_list_history(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_list_history")

    @platform_process(
        name="gmail_get_profile",
        display_name="Gmail: Get Profile",
        description="Read the mailbox profile (users.getProfile): email address, total messages, total threads and the current history id (the starting point for gmail_list_history)." + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def gmail_get_profile(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_get_profile")
