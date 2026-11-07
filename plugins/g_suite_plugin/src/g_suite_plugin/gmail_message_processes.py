"""``@platform_process`` Gmail message, attachment and thread verbs (mixed into ``GSuitePlugin``).

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


class GmailMessageProcesses:
    """Gmail verbs; ``GSuitePlugin`` supplies ``_run_deferred``."""

    def _run_deferred(self, params: dict[str, Any], state: dict[str, Any], verb: str) -> dict[str, Any]:
        raise NotImplementedError

    @platform_process(
        name="gmail_list_messages",
        display_name="Gmail: List / Search Messages",
        description="Search the mailbox with Gmail's own query syntax and return one page of message and thread ids, newest first, with next_page_token and result_size_estimate. Page through the whole mailbox by passing next_page_token back as page_token. Returns ids only; fetch content with gmail_get_message or a whole conversation with gmail_get_thread."
        + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "query": ParameterMetadata(type=ParameterType.STRING, required=False, description="Gmail search query using Gmail's own operators (from:, to:, subject:, is:unread, has:attachment, label:, newer_than:, before:). Empty matches everything."),
            "max": ParameterMetadata(type=ParameterType.INTEGER, required=False, description="Rows per page (default 500, Gmail's per-call maximum)."),
            "page_token": ParameterMetadata(type=ParameterType.STRING, required=False, description="next_page_token from the previous page; omit for the first page."),
            "include_spam_trash": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="Also match Spam and Trash (default false)."),
            "label_ids": ParameterMetadata(type=ParameterType.LIST, required=False, description="Only items carrying all of these label ids, e.g. ['INBOX', 'UNREAD']."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def gmail_list_messages(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_list_messages")

    @platform_process(
        name="gmail_get_message",
        display_name="Gmail: Get Message",
        description="Fetch one message. format=full (default) returns key headers (from, to, cc, subject, date, message-id, in-reply-to, references), the plain-text and HTML bodies, attachment metadata (attachment_id, name, mime, size), label ids, internal date and history id. format=raw returns the whole RFC 822 message as raw (base64url, lossless, accepted by gmail_import_message) and raw_rfc822 (decoded text). format=metadata returns all headers; minimal returns ids and labels."
        + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The Gmail message id (from gmail_list_messages)."),
            "format": ParameterMetadata(type=ParameterType.STRING, required=False, description="full (default: parsed headers, text and HTML bodies, attachment list), raw (whole RFC 822 message), metadata (headers only) or minimal (ids and labels)."),
            "metadata_headers": ParameterMetadata(type=ParameterType.LIST, required=False, description="With format=metadata, only these headers, e.g. ['From', 'Subject']."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def gmail_get_message(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_get_message")

    @platform_process(
        name="gmail_send",
        display_name="Gmail: Send Message",
        description="Send an email from the connected account: plain text and optional HTML alternative, cc and bcc, blob-backed attachments, and replies kept in their conversation via thread_id plus in_reply_to/references. Returns the sent message id, thread id and labels." + _NOTE,
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
    def gmail_send(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_send")

    @platform_process(
        name="gmail_modify_message",
        display_name="Gmail: Modify Message Labels",
        description="Change one message's labels (users.messages.modify): add_label_ids / remove_label_ids, or the conveniences mark_read, archive and star, which map to the UNREAD, INBOX and STARRED system labels. Returns the message's resulting label ids." + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The Gmail message id (from gmail_list_messages)."),
            "add_label_ids": ParameterMetadata(type=ParameterType.LIST, required=False, description="Label ids to add (at most 100)."),
            "remove_label_ids": ParameterMetadata(type=ParameterType.LIST, required=False, description="Label ids to remove (at most 100)."),
            "mark_read": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="true marks read (removes UNREAD); false marks unread (adds UNREAD)."),
            "archive": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="true archives (removes INBOX); false moves back to the inbox (adds INBOX)."),
            "star": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="true stars (adds STARRED); false unstars."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def gmail_modify_message(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_modify_message")

    @platform_process(
        name="gmail_batch_modify_messages",
        display_name="Gmail: Batch Modify Messages",
        description="Apply one label change to up to 1,000 messages in a single call (users.messages.batchModify): add/remove label ids plus mark_read, archive and star conveniences. Returns how many ids were modified and the applied changes." + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "ids": ParameterMetadata(type=ParameterType.LIST, required=True, description="Gmail message ids, at most 1,000 per call."),
            "add_label_ids": ParameterMetadata(type=ParameterType.LIST, required=False, description="Label ids to add (at most 100)."),
            "remove_label_ids": ParameterMetadata(type=ParameterType.LIST, required=False, description="Label ids to remove (at most 100)."),
            "mark_read": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="true marks read (removes UNREAD); false marks unread (adds UNREAD)."),
            "archive": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="true archives (removes INBOX); false moves back to the inbox (adds INBOX)."),
            "star": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="true stars (adds STARRED); false unstars."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def gmail_batch_modify_messages(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_batch_modify_messages")

    @platform_process(
        name="gmail_trash_message",
        display_name="Gmail: Trash Message",
        description="Move one message to Trash (users.messages.trash); Gmail deletes it permanently after 30 days unless it is restored with gmail_untrash_message. Needs only the default gmail.modify scope." + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The Gmail message id (from gmail_list_messages)."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def gmail_trash_message(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_trash_message")

    @platform_process(
        name="gmail_untrash_message",
        display_name="Gmail: Untrash Message",
        description="Restore one message from Trash (users.messages.untrash). Returns its labels after the restore." + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The Gmail message id (from gmail_list_messages)."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def gmail_untrash_message(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_untrash_message")

    @platform_process(
        name="gmail_delete_message",
        display_name="Gmail: Delete Message Permanently",
        description="Permanently and immediately delete one message, skipping Trash (users.messages.delete). Gmail accepts only the full mailbox scope for this: connect the account with gmail_permanent_delete=true, otherwise the verb returns gsuite.missing_scope naming that step. Prefer gmail_trash_message for recoverable deletion."
        + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The Gmail message id (from gmail_list_messages)."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def gmail_delete_message(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_delete_message")

    @platform_process(
        name="gmail_batch_delete_messages",
        display_name="Gmail: Batch Delete Messages Permanently",
        description="Permanently delete up to 1,000 messages in one call, skipping Trash (users.messages.batchDelete). Needs the full mailbox scope (connect_account gmail_permanent_delete=true); without it the verb returns gsuite.missing_scope." + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "ids": ParameterMetadata(type=ParameterType.LIST, required=True, description="Gmail message ids, at most 1,000 per call."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def gmail_batch_delete_messages(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_batch_delete_messages")

    @platform_process(
        name="gmail_insert_message",
        display_name="Gmail: Insert Message",
        description="Place a raw RFC 822 message directly into the mailbox without sending it and without spam or delivery scanning, like IMAP APPEND (users.messages.insert). Takes raw (base64url) or raw_rfc822 (text) plus optional label ids." + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "raw": ParameterMetadata(type=ParameterType.STRING, required=False, description="The message as base64url RFC 822 (exactly what gmail_get_message format=raw returns). Pass raw or raw_rfc822."),
            "raw_rfc822": ParameterMetadata(type=ParameterType.STRING, required=False, description="The message as RFC 822 text (headers, blank line, body). Pass raw or raw_rfc822."),
            "label_ids": ParameterMetadata(type=ParameterType.LIST, required=False, description="Label ids to apply, e.g. ['INBOX', 'UNREAD']."),
            "internal_date_source": ParameterMetadata(type=ParameterType.STRING, required=False, description="receivedTime (default) or dateHeader: which date Gmail files the message under."),
            "deleted": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="Mark as deleted, visible only to a Google Vault administrator (Workspace)."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def gmail_insert_message(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_insert_message")

    @platform_process(
        name="gmail_import_message",
        display_name="Gmail: Import Message",
        description="Import a raw RFC 822 message into the mailbox with Gmail's standard delivery scanning and classification, as if it had been received (users.messages.import). Options: never_mark_spam, process_for_calendar, label ids, internal_date_source." + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "raw": ParameterMetadata(type=ParameterType.STRING, required=False, description="The message as base64url RFC 822 (exactly what gmail_get_message format=raw returns). Pass raw or raw_rfc822."),
            "raw_rfc822": ParameterMetadata(type=ParameterType.STRING, required=False, description="The message as RFC 822 text (headers, blank line, body). Pass raw or raw_rfc822."),
            "label_ids": ParameterMetadata(type=ParameterType.LIST, required=False, description="Label ids to apply, e.g. ['INBOX', 'UNREAD']."),
            "internal_date_source": ParameterMetadata(type=ParameterType.STRING, required=False, description="receivedTime (default) or dateHeader: which date Gmail files the message under."),
            "deleted": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="Mark as deleted, visible only to a Google Vault administrator (Workspace)."),
            "never_mark_spam": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="Never send this message to Spam."),
            "process_for_calendar": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="Process calendar invites in the message and add them to the user's calendar."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def gmail_import_message(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_import_message")

    @platform_process(
        name="gmail_get_attachment",
        display_name="Gmail: Get Attachment",
        description="Download one attachment's bytes (users.messages.attachments.get). With path (absolute; parent must exist; overwrite=true to replace) the bytes are written there; otherwise they are stored in blob storage and attachment_blob_key is returned. attachment_id comes from gmail_get_message's attachments list."
        + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "message_id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The message id carrying the attachment."),
            "attachment_id": ParameterMetadata(type=ParameterType.STRING, required=True, description="attachment_id from gmail_get_message's attachments list."),
            "path": ParameterMetadata(type=ParameterType.STRING, required=False, description="Absolute local file path to write; omit to store in blob storage."),
            "overwrite": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="Replace an existing file at path (default false)."),
            "filename": ParameterMetadata(type=ParameterType.STRING, required=False, description="Name for the stored blob (use the attachment's name)."),
            "mime_type": ParameterMetadata(type=ParameterType.STRING, required=False, description="MIME type for the stored blob (use the attachment's mime)."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def gmail_get_attachment(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_get_attachment")

    @platform_process(
        name="gmail_list_threads",
        display_name="Gmail: List / Search Threads",
        description="Search conversations with Gmail's query syntax and return one page of thread ids with snippets and history ids, plus next_page_token and result_size_estimate (users.threads.list)." + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "query": ParameterMetadata(type=ParameterType.STRING, required=False, description="Gmail search query using Gmail's own operators (from:, to:, subject:, is:unread, has:attachment, label:, newer_than:, before:). Empty matches everything."),
            "max": ParameterMetadata(type=ParameterType.INTEGER, required=False, description="Rows per page (default 500, Gmail's per-call maximum)."),
            "page_token": ParameterMetadata(type=ParameterType.STRING, required=False, description="next_page_token from the previous page; omit for the first page."),
            "include_spam_trash": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="Also match Spam and Trash (default false)."),
            "label_ids": ParameterMetadata(type=ParameterType.LIST, required=False, description="Only items carrying all of these label ids, e.g. ['INBOX', 'UNREAD']."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def gmail_list_threads(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_list_threads")

    @platform_process(
        name="gmail_get_thread",
        display_name="Gmail: Get Thread",
        description="Fetch a whole conversation (users.threads.get): every message parsed like gmail_get_message (format full, metadata or minimal), in order, plus the thread's history id." + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The Gmail thread id (from gmail_list_threads or a message's thread_id)."),
            "format": ParameterMetadata(type=ParameterType.STRING, required=False, description="full (default), metadata or minimal."),
            "metadata_headers": ParameterMetadata(type=ParameterType.LIST, required=False, description="With format=metadata, only these headers."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def gmail_get_thread(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_get_thread")

    @platform_process(
        name="gmail_modify_thread",
        display_name="Gmail: Modify Thread Labels",
        description="Change the labels of every message in a conversation (users.threads.modify): add/remove label ids plus mark_read, archive and star conveniences. Returns the thread's message ids and resulting labels." + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The Gmail thread id (from gmail_list_threads or a message's thread_id)."),
            "add_label_ids": ParameterMetadata(type=ParameterType.LIST, required=False, description="Label ids to add (at most 100)."),
            "remove_label_ids": ParameterMetadata(type=ParameterType.LIST, required=False, description="Label ids to remove (at most 100)."),
            "mark_read": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="true marks read (removes UNREAD); false marks unread (adds UNREAD)."),
            "archive": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="true archives (removes INBOX); false moves back to the inbox (adds INBOX)."),
            "star": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="true stars (adds STARRED); false unstars."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def gmail_modify_thread(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_modify_thread")

    @platform_process(
        name="gmail_trash_thread",
        display_name="Gmail: Trash Thread",
        description="Move every message of a conversation to Trash (users.threads.trash); recoverable for 30 days with gmail_untrash_thread." + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The Gmail thread id (from gmail_list_threads or a message's thread_id)."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def gmail_trash_thread(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_trash_thread")

    @platform_process(
        name="gmail_untrash_thread",
        display_name="Gmail: Untrash Thread",
        description="Restore every message of a conversation from Trash (users.threads.untrash)." + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The Gmail thread id (from gmail_list_threads or a message's thread_id)."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def gmail_untrash_thread(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_untrash_thread")

    @platform_process(
        name="gmail_delete_thread",
        display_name="Gmail: Delete Thread Permanently",
        description="Permanently delete every message of a conversation, skipping Trash (users.threads.delete). Needs the full mailbox scope (connect_account gmail_permanent_delete=true); without it the verb returns gsuite.missing_scope." + _NOTE,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "account": ACCOUNT_PARAM,
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The Gmail thread id (from gmail_list_threads or a message's thread_id)."),
        },
        return_value_schema=_QUEUED,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def gmail_delete_thread(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_delete_thread")
