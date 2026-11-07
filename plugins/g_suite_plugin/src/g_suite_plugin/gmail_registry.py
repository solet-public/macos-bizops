"""Every Gmail verb: its action function, retry policy and result type (one table).

The plugin builds its deferred producers and EDGE definitions from this table,
so a verb cannot exist in one place and be missing from the other.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import gmail_actions, gmail_label_draft_actions, gmail_mailbox_actions
from .gmail_actions import GmailIO

GmailOperation = Callable[[Any, dict[str, Any], GmailIO], dict[str, Any]]

# verb -> (action, retryable, result_type)
GMAIL_OPERATIONS: dict[str, tuple[GmailOperation, bool, str]] = {
    "gmail_list_messages": (gmail_actions.list_messages, True, "g_suite_gmail_list_result"),
    "gmail_get_message": (gmail_actions.get_message, True, "g_suite_gmail_message_result"),
    "gmail_send": (gmail_actions.send_message, False, "g_suite_gmail_send_result"),
    "gmail_modify_message": (gmail_mailbox_actions.modify_message, False, "g_suite_gmail_modify_message_result"),
    "gmail_batch_modify_messages": (gmail_mailbox_actions.batch_modify_messages, False, "g_suite_gmail_batch_modify_messages_result"),
    "gmail_trash_message": (gmail_mailbox_actions.trash_message, False, "g_suite_gmail_trash_message_result"),
    "gmail_untrash_message": (gmail_mailbox_actions.untrash_message, False, "g_suite_gmail_untrash_message_result"),
    "gmail_delete_message": (gmail_mailbox_actions.delete_message, False, "g_suite_gmail_delete_message_result"),
    "gmail_batch_delete_messages": (gmail_mailbox_actions.batch_delete_messages, False, "g_suite_gmail_batch_delete_messages_result"),
    "gmail_insert_message": (gmail_mailbox_actions.insert_message, False, "g_suite_gmail_insert_message_result"),
    "gmail_import_message": (gmail_mailbox_actions.import_message, False, "g_suite_gmail_import_message_result"),
    "gmail_get_attachment": (gmail_mailbox_actions.get_attachment, True, "g_suite_gmail_get_attachment_result"),
    "gmail_list_threads": (gmail_mailbox_actions.list_threads, True, "g_suite_gmail_list_threads_result"),
    "gmail_get_thread": (gmail_mailbox_actions.get_thread, True, "g_suite_gmail_get_thread_result"),
    "gmail_modify_thread": (gmail_mailbox_actions.modify_thread, False, "g_suite_gmail_modify_thread_result"),
    "gmail_trash_thread": (gmail_mailbox_actions.trash_thread, False, "g_suite_gmail_trash_thread_result"),
    "gmail_untrash_thread": (gmail_mailbox_actions.untrash_thread, False, "g_suite_gmail_untrash_thread_result"),
    "gmail_delete_thread": (gmail_mailbox_actions.delete_thread, False, "g_suite_gmail_delete_thread_result"),
    "gmail_list_labels": (gmail_label_draft_actions.list_labels, True, "g_suite_gmail_list_labels_result"),
    "gmail_get_label": (gmail_label_draft_actions.get_label, True, "g_suite_gmail_get_label_result"),
    "gmail_create_label": (gmail_label_draft_actions.create_label, False, "g_suite_gmail_create_label_result"),
    "gmail_patch_label": (gmail_label_draft_actions.patch_label, False, "g_suite_gmail_patch_label_result"),
    "gmail_update_label": (gmail_label_draft_actions.update_label, False, "g_suite_gmail_update_label_result"),
    "gmail_delete_label": (gmail_label_draft_actions.delete_label, False, "g_suite_gmail_delete_label_result"),
    "gmail_create_draft": (gmail_label_draft_actions.create_draft, False, "g_suite_gmail_create_draft_result"),
    "gmail_get_draft": (gmail_label_draft_actions.get_draft, True, "g_suite_gmail_get_draft_result"),
    "gmail_list_drafts": (gmail_label_draft_actions.list_drafts, True, "g_suite_gmail_list_drafts_result"),
    "gmail_update_draft": (gmail_label_draft_actions.update_draft, False, "g_suite_gmail_update_draft_result"),
    "gmail_send_draft": (gmail_label_draft_actions.send_draft, False, "g_suite_gmail_send_draft_result"),
    "gmail_delete_draft": (gmail_label_draft_actions.delete_draft, False, "g_suite_gmail_delete_draft_result"),
    "gmail_list_history": (gmail_label_draft_actions.list_history, True, "g_suite_gmail_list_history_result"),
    "gmail_get_profile": (gmail_label_draft_actions.get_profile, True, "g_suite_gmail_get_profile_result"),
}
