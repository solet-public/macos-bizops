"""Gmail build 2: one declarative row per Gmail settings / push method.

Each :class:`SettingsVerb` names the exact Gmail v1 REST method (resource chain
under ``users()``, HTTP method and path), every documented path, query and body
parameter as a :class:`Field`, the narrowest OAuth scope the method accepts
and whether it is a Google Workspace-only feature. The action executor
(:mod:`gmail_settings_actions`), the ``@platform_process`` declarations and
the EDGE definitions are all built from these rows, so a verb cannot be
declared in one place and missing from another.

Source: Gmail v1 live discovery revision 20261102, audited 2026-11-07.
This module holds watch/stop, the five get/update
settings, filters and forwarding addresses; :mod:`gmail_sharing_spec` holds
delegates, send-as, S/MIME and client-side encryption.
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import GMAIL_MODIFY_SCOPE, GMAIL_SETTINGS_BASIC_SCOPE

GMAIL_SETTINGS_SHARING_SCOPE = "https://www.googleapis.com/auth/gmail.settings.sharing"

# Field kinds: how a verb parameter is validated and sent.
STR = "str"
BOOL = "bool"
INT = "int"
INT64 = "int64"  # epoch milliseconds; Gmail's JSON carries int64 as a string
STR_LIST = "str_list"
OBJECT_LIST = "object_list"

DISPOSITIONS = ("dispositionUnspecified", "leaveInInbox", "archive", "trash", "markRead")


@dataclass(frozen=True)
class Field:
    """One verb parameter and the Gmail name it maps to.

    ``api`` is the keyword argument for a path or query field, or a dotted path
    into the JSON request body for a body field (``criteria.from``).
    """

    param: str
    api: str
    kind: str
    description: str
    required: bool = False
    enum: tuple[str, ...] = ()


STANDARD_QUERY_FIELDS = (
    Field("fields", "fields", STR, "Partial-response field selector; omit to return the full resource."),
    Field("pretty_print", "prettyPrint", BOOL, "Whether Google's JSON response is indented; omit for the vendor default."),
    Field("quota_user", "quotaUser", STR, "Quota attribution identifier for server-side calls (at most 40 characters)."),
    Field("xgafv", "$.xgafv", STR, "Google error format: 1 or 2; omit for the vendor default.", enum=("1", "2")),
    Field("alt", "alt", STR, "Typed settings results require json; proto and media formats are unsupported and rejected before a request.", enum=("json",)),
)


@dataclass(frozen=True)
class SettingsVerb:
    """One Gmail REST method exposed as one typed verb."""

    verb: str
    chain: tuple[str, ...]  # resources below users(), then the method name
    rest: str  # HTTP method and path below gmail/v1/users/{userId}/
    title: str
    summary: str
    scope: str = GMAIL_MODIFY_SCOPE
    workspace_only: bool = False
    path: tuple[Field, ...] = ()
    query: tuple[Field, ...] = ()
    body: tuple[Field, ...] = ()
    has_body: bool = False  # the method takes a request body even when no field is set
    list_key: str = ""  # response key holding the rows of a list method
    rows_name: str = ""  # result key for those rows
    paged: bool = False
    empty_result: str = ""  # result flag for a method with an empty response

    @property
    def write(self) -> bool:
        return not self.rest.startswith("GET ")

    @property
    def result_type(self) -> str:
        return f"g_suite_{self.verb}_result"

    @property
    def fields(self) -> tuple[Field, ...]:
        seen: dict[str, Field] = {}
        for item in (*self.path, *self.query, *STANDARD_QUERY_FIELDS, *self.body):
            seen.setdefault(item.param, item)
        return tuple(seen.values())


def paging_fields() -> tuple[Field, ...]:
    return (
        Field("page_size", "pageSize", INT, "Rows per page (Gmail's default when omitted)."),
        Field("page_token", "pageToken", STR, "next_page_token from the previous page; omit for the first page."),
    )


_DISPOSITION_NOTE = "What Gmail does with a message after forwarding or POP access: leaveInInbox, archive, trash or markRead."

_AUTO_FORWARDING = (
    Field("enabled", "enabled", BOOL, "Whether all incoming mail is automatically forwarded."),
    Field("email_address", "emailAddress", STR, "Address to forward to; it must be a verified forwarding address (gmail_list_forwarding_addresses)."),
    Field("disposition", "disposition", STR, _DISPOSITION_NOTE, enum=DISPOSITIONS),
)
_IMAP = (
    Field("enabled", "enabled", BOOL, "Whether IMAP access is on."),
    Field("auto_expunge", "autoExpunge", BOOL, "Expunge immediately when a message is marked deleted in IMAP (otherwise wait for the client)."),
    Field("expunge_behavior", "expungeBehavior", STR, "What happens to a message deleted and expunged in IMAP: archive, trash or deleteForever.", enum=("expungeBehaviorUnspecified", "archive", "trash", "deleteForever")),
    Field("max_folder_size", "maxFolderSize", INT, "Most messages an IMAP folder shows: 0 (no limit), 1000, 2000, 5000 or 10000."),
)
_POP = (
    Field("access_window", "accessWindow", STR, "Which messages POP can download: disabled, fromNowOn or allMail.", enum=("accessWindowUnspecified", "disabled", "fromNowOn", "allMail")),
    Field("disposition", "disposition", STR, _DISPOSITION_NOTE, enum=DISPOSITIONS),
)
_LANGUAGE = (Field("display_language", "displayLanguage", STR, "Gmail display language as an RFC 3066 tag, e.g. en-GB, fr or ja."),)
_VACATION = (
    Field("enable_auto_reply", "enableAutoReply", BOOL, "Turn the vacation auto-reply on (true) or off (false)."),
    Field("response_subject", "responseSubject", STR, "Optional subject prefixed to the auto-reply."),
    Field("response_body_plain_text", "responseBodyPlainText", STR, "Auto-reply body as plain text."),
    Field("response_body_html", "responseBodyHtml", STR, "Auto-reply body as HTML (used instead of the plain text when set)."),
    Field("restrict_to_contacts", "restrictToContacts", BOOL, "Reply only to senders in the account's contacts."),
    Field("restrict_to_domain", "restrictToDomain", BOOL, "Reply only to senders in the account's own Workspace domain."),
    Field("start_time", "startTime", INT64, "When replies start, in milliseconds since the epoch."),
    Field("end_time", "endTime", INT64, "When replies stop, in milliseconds since the epoch."),
)
_FILTER_ID = Field("id", "id", STR, "The filter id (from gmail_list_filters).", required=True)
_FILTER_BODY = (
    Field("from", "criteria.from", STR, "Criteria: sender name or address."),
    Field("to", "criteria.to", STR, "Criteria: recipient (To, Cc or Bcc) name or address."),
    Field("subject", "criteria.subject", STR, "Criteria: case-insensitive phrase in the subject."),
    Field("query", "criteria.query", STR, "Criteria: Gmail search query the message must match (same operators as the search box)."),
    Field("negated_query", "criteria.negatedQuery", STR, "Criteria: Gmail search query the message must NOT match."),
    Field("has_attachment", "criteria.hasAttachment", BOOL, "Criteria: only messages with an attachment."),
    Field("exclude_chats", "criteria.excludeChats", BOOL, "Criteria: leave chats out of the match."),
    Field("size", "criteria.size", INT, "Criteria: message size in bytes, compared with size_comparison."),
    Field("size_comparison", "criteria.sizeComparison", STR, "Criteria: smaller or larger than size.", enum=("unspecified", "smaller", "larger")),
    Field("add_label_ids", "action.addLabelIds", STR_LIST, "Action: label ids to add (e.g. STARRED, TRASH, Label_12; remove INBOX to skip the inbox)."),
    Field("remove_label_ids", "action.removeLabelIds", STR_LIST, "Action: label ids to remove (e.g. INBOX to archive, UNREAD to mark read)."),
    Field("forward", "action.forward", STR, "Action: forward to this verified forwarding address."),
    Field("id", "id", STR, "Server-assigned filter id; Gmail sets it on create (accepted because the Filter resource documents it)."),
)
_FORWARDING_EMAIL = Field("forwarding_email", "forwardingEmail", STR, "The forwarding address, e.g. archive@example.com.", required=True)

_READ_NOTE = "Reads with the default gmail.modify scope."
_BASIC_NOTE = "Needs the gmail.settings.basic scope (part of the default consent; an account connected earlier reconnects once)."
_SHARING_NOTE = "Workspace only: Google allows this method only through the gmail.settings.sharing scope, which it grants to a service account with domain-wide delegation; a consumer account is refused naming its tier."


def _settings_pair(name: str, label: str, what: str, fields: tuple[Field, ...], *, update_scope: str = GMAIL_SETTINGS_BASIC_SCOPE) -> tuple[SettingsVerb, SettingsVerb]:
    camel = name[0].upper() + name[1:]
    snake = "".join(f"_{c.lower()}" if c.isupper() else c for c in name)
    sharing = update_scope == GMAIL_SETTINGS_SHARING_SCOPE
    get = SettingsVerb(f"gmail_get_{snake}", ("settings", f"get{camel}"), f"GET settings/{name}", f"Gmail: Get {label} Settings", f"Read the {what} (users.settings.get{camel}). {_READ_NOTE}")
    update = SettingsVerb(
        f"gmail_update_{snake}",
        ("settings", f"update{camel}"),
        f"PUT settings/{name}",
        f"Gmail: Update {label} Settings",
        f"Replace the {what} (users.settings.update{camel}, HTTP PUT): fields not passed are reset to Gmail's defaults, so read first with gmail_get_{snake}. {_SHARING_NOTE if sharing else _BASIC_NOTE}",
        scope=update_scope,
        workspace_only=sharing,
        body=fields,
        has_body=True,
    )
    return get, update


WATCH_VERBS: tuple[SettingsVerb, ...] = (
    SettingsVerb(
        "gmail_watch",
        ("watch",),
        "POST watch",
        "Gmail: Start Push Notifications",
        "Start or renew push notifications for mailbox changes (users.watch) to a Cloud Pub/Sub topic the caller owns; the topic must grant publish to gmail-api-push@system.gserviceaccount.com. Nothing is subscribed unless this verb is called. Returns the current history_id and the expiration (epoch ms, about 7 days); call again before it expires. Reads with the default gmail.modify scope.",
        body=(
            Field("topic_name", "topicName", STR, "Fully qualified Pub/Sub topic, projects/<project>/topics/<topic>.", required=True),
            Field("label_ids", "labelIds", STR_LIST, "Only report changes to messages with (or, with exclude, without) these label ids."),
            Field("label_filter_behavior", "labelFilterBehavior", STR, "include (default) or exclude: how label_ids filters notifications.", enum=("include", "exclude")),
            Field("label_filter_action", "labelFilterAction", STR, "Older spelling of label_filter_behavior, still accepted by Gmail: include or exclude.", enum=("include", "exclude")),
        ),
        has_body=True,
    ),
    SettingsVerb("gmail_stop", ("stop",), "POST stop", "Gmail: Stop Push Notifications", "Stop all push notifications for the mailbox (users.stop), ending what gmail_watch started.", empty_result="stopped"),
)

SETTINGS_VERBS: tuple[SettingsVerb, ...] = (
    *_settings_pair("autoForwarding", "Auto-Forwarding", "auto-forwarding setting: whether all mail is forwarded, to which address and what happens to the original", _AUTO_FORWARDING, update_scope=GMAIL_SETTINGS_SHARING_SCOPE),
    *_settings_pair("imap", "IMAP", "IMAP access settings: enabled, auto-expunge, expunge behavior and folder size limit", _IMAP),
    *_settings_pair("pop", "POP", "POP access settings: which mail POP can download and what happens after download", _POP),
    *_settings_pair("language", "Language", "Gmail display language", _LANGUAGE),
    *_settings_pair("vacation", "Vacation Responder", "vacation responder (out-of-office auto-reply): on/off, subject, body, audience and start/end time", _VACATION),
)

FILTER_VERBS: tuple[SettingsVerb, ...] = (
    SettingsVerb("gmail_list_filters", ("settings", "filters", "list"), "GET settings/filters", "Gmail: List Filters", f"List every message filter (users.settings.filters.list) with its criteria and actions. {_READ_NOTE}", list_key="filter", rows_name="filters"),
    SettingsVerb("gmail_get_filter", ("settings", "filters", "get"), "GET settings/filters/{id}", "Gmail: Get Filter", f"Read one message filter (users.settings.filters.get): criteria and actions. {_READ_NOTE}", path=(_FILTER_ID,)),
    SettingsVerb(
        "gmail_create_filter",
        ("settings", "filters", "create"),
        "POST settings/filters",
        "Gmail: Create Filter",
        f"Create a message filter (users.settings.filters.create) from criteria (from, to, subject, query, negated_query, has_attachment, exclude_chats, size) and actions (add or remove labels, forward). Gmail allows at most 1,000 filters. {_BASIC_NOTE}",
        scope=GMAIL_SETTINGS_BASIC_SCOPE,
        body=_FILTER_BODY,
        has_body=True,
    ),
    SettingsVerb(
        "gmail_delete_filter",
        ("settings", "filters", "delete"),
        "DELETE settings/filters/{id}",
        "Gmail: Delete Filter",
        f"Delete one message filter (users.settings.filters.delete); messages it already touched keep their labels. {_BASIC_NOTE}",
        scope=GMAIL_SETTINGS_BASIC_SCOPE,
        path=(_FILTER_ID,),
        empty_result="deleted",
    ),
)

FORWARDING_VERBS: tuple[SettingsVerb, ...] = (
    SettingsVerb(
        "gmail_list_forwarding_addresses",
        ("settings", "forwardingAddresses", "list"),
        "GET settings/forwardingAddresses",
        "Gmail: List Forwarding Addresses",
        f"List the addresses mail may be forwarded to (users.settings.forwardingAddresses.list) with each one's verification status (accepted or pending). {_READ_NOTE}",
        list_key="forwardingAddresses",
        rows_name="forwarding_addresses",
    ),
    SettingsVerb(
        "gmail_get_forwarding_address",
        ("settings", "forwardingAddresses", "get"),
        "GET settings/forwardingAddresses/{forwardingEmail}",
        "Gmail: Get Forwarding Address",
        f"Read one forwarding address and its verification status (users.settings.forwardingAddresses.get). {_READ_NOTE}",
        path=(_FORWARDING_EMAIL,),
    ),
    SettingsVerb(
        "gmail_create_forwarding_address",
        ("settings", "forwardingAddresses", "create"),
        "POST settings/forwardingAddresses",
        "Gmail: Create Forwarding Address",
        f"Add a forwarding address (users.settings.forwardingAddresses.create); Gmail emails it a verification link and it stays pending until accepted. {_SHARING_NOTE}",
        scope=GMAIL_SETTINGS_SHARING_SCOPE,
        workspace_only=True,
        body=(
            Field("forwarding_email", "forwardingEmail", STR, "The address to add, e.g. archive@example.com.", required=True),
            Field("verification_status", "verificationStatus", STR, "Read-only at Gmail (set by verification); accepted because the resource documents it.", enum=("verificationStatusUnspecified", "accepted", "pending")),
        ),
        has_body=True,
    ),
    SettingsVerb(
        "gmail_delete_forwarding_address",
        ("settings", "forwardingAddresses", "delete"),
        "DELETE settings/forwardingAddresses/{forwardingEmail}",
        "Gmail: Delete Forwarding Address",
        f"Remove a forwarding address and revoke its verification (users.settings.forwardingAddresses.delete). {_SHARING_NOTE}",
        scope=GMAIL_SETTINGS_SHARING_SCOPE,
        workspace_only=True,
        path=(_FORWARDING_EMAIL,),
        empty_result="deleted",
    ),
)

BASIC_VERBS: tuple[SettingsVerb, ...] = (*WATCH_VERBS, *SETTINGS_VERBS, *FILTER_VERBS, *FORWARDING_VERBS)
