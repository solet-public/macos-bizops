"""Google Workspace plugin constants.

Single source of truth for every magic value: OAuth endpoints + scopes, vault
keys, address-book field identifiers, error codes, and HTTP defaults.

Auth model (operator-decided 2026-06-20 / narrowed 2026-07-08): enterprise-only,
single-account, full read/write. The OAuth app is configured "Internal" (one
Workspace org). `gmail.modify` and `drive` are Google restricted/sensitive
scopes — a Workspace admin may need to allow them for the app even though it is
Internal (see knowledge_base/01_g_suite_overview.md runbook).
"""

import os
from typing import Final


def _homunculus_or_fail() -> str:
    """Resolve HOMUNCULUS_NAME at import-time for scoped vault keys.

    Vault keys follow the ``<homunculus>.<plugin>.<credential>`` convention.
    Mirrors the fast-fail helper in schwab_market_data_plugin.constants +
    soundcloud_artist_studio_plugin.constants.
    """
    name = os.environ.get("HOMUNCULUS_NAME", "").strip()
    if not name:
        raise RuntimeError(
            "g_suite_plugin.constants: HOMUNCULUS_NAME env var is required to "
            "resolve scoped vault keys.",
        )
    return name


_HOMUNCULUS = _homunculus_or_fail()

# ---------------------------------------------------------------------------
# Plugin identity
# ---------------------------------------------------------------------------
PLUGIN_NAME: Final[str] = "g_suite_plugin"
PLUGIN_VERSION: Final[str] = "1.0.0"

# ---------------------------------------------------------------------------
# Blob storage namespace (downloads/exports/attachments)
# ---------------------------------------------------------------------------
BLOB_NAMESPACE: Final[str] = "g_suite_plugin"

# ---------------------------------------------------------------------------
# OAuth endpoints + scopes
# ---------------------------------------------------------------------------
GOOGLE_AUTH_URI: Final[str] = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URI: Final[str] = "https://oauth2.googleapis.com/token"

OAUTH_CALLBACK_PATH: Final[str] = "/oauth/google/callback"

# One consent set for the whole plugin (all five products), granted once at
# connect time. Full read/write per operator decision (2026-07-08). Docs +
# Slides scopes are included now so Phase 2 needs no re-consent.
OAUTH_SCOPES: Final[tuple[str, ...]] = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/presentations",
)

# ---------------------------------------------------------------------------
# Token lifecycle
# ---------------------------------------------------------------------------
# Google access tokens live ~1h. Treat as stale when <2min remain.
ACCESS_TOKEN_EARLY_REFRESH_SECONDS: Final[int] = 120
# Fallback access-token TTL when Google omits an explicit expiry.
DEFAULT_ACCESS_TTL_SECONDS: Final[int] = 3600
ACCESS_TOKEN_VALUE_KEY_TOKEN: Final[str] = "token"
ACCESS_TOKEN_VALUE_KEY_EXPIRES_AT: Final[str] = "expires_at"

# ---------------------------------------------------------------------------
# Vault keys — scoped ``<homunculus>.<plugin>.<credential>`` (single-account)
# ---------------------------------------------------------------------------
VAULT_KEY_REFRESH_TOKEN: Final[str] = f"{_HOMUNCULUS}.g_suite_plugin.refresh_token"
VAULT_KEY_ACCESS_TOKEN: Final[str] = f"{_HOMUNCULUS}.g_suite_plugin.access_token"

VAULT_TAG_REFRESH_TOKEN: Final[str] = "g_suite_refresh_token"
VAULT_TAG_ACCESS_TOKEN: Final[str] = "g_suite_access_token"

# Vault key the address book's ``google_oauth_app`` client_secret field
# references. CHAIN-CONSUMED via ``resolve_with_secrets`` (never read directly
# under this plugin's identity), so it lives in the RESOLVER's namespace
# (``default_address_book_plugin``) — post-2026-06-07 vault namespace
# enforcement requires the key's ``<plugin>`` segment to equal the retrieving
# caller. Therefore NOT declared in get_required_vault_keys /
# get_declared_vault_keys. Canonical convention: VAULT_AND_ADDRESS_BOOK.md
# §"Pattern: Plugin Configuration via Address Book + Vault".
VAULT_KEY_CLIENT_SECRET: Final[str] = (
    f"{_HOMUNCULUS}.default_address_book_plugin.google_client_secret"
)

# ---------------------------------------------------------------------------
# Address book entry — OAuth identity (client_id, client_secret, redirect_uri)
# ---------------------------------------------------------------------------
ADDRESS_BOOK_ENTRY_NAME: Final[str] = "google_oauth_app"
ADDRESS_BOOK_ENTRY_TYPE: Final[str] = "api"
ADDRESS_BOOK_ENTRY_DESCRIPTION: Final[str] = (
    "Google Workspace OAuth 2.0 application identity "
    "(client_id, client_secret, redirect_uri) for g_suite_plugin."
)
ADDRESS_BOOK_FIELD_CLIENT_ID: Final[str] = "client_id"
ADDRESS_BOOK_FIELD_CLIENT_SECRET: Final[str] = "client_secret"
ADDRESS_BOOK_FIELD_REDIRECT_URI: Final[str] = "redirect_uri"

# ---------------------------------------------------------------------------
# Gmail defaults
# ---------------------------------------------------------------------------
GMAIL_DEFAULT_MAX_RESULTS: Final[int] = 25
GMAIL_MAX_RESULTS_CAP: Final[int] = 100

# ---------------------------------------------------------------------------
# Drive defaults
# ---------------------------------------------------------------------------
DRIVE_DEFAULT_PAGE_SIZE: Final[int] = 25
DRIVE_PAGE_SIZE_CAP: Final[int] = 100
DRIVE_FOLDER_MIME_TYPE: Final[str] = "application/vnd.google-apps.folder"
DRIVE_UPLOAD_DEFAULT_MIME: Final[str] = "application/octet-stream"
DRIVE_SHARE_ROLE_READER: Final[str] = "reader"
DRIVE_SHARE_ROLE_COMMENTER: Final[str] = "commenter"
DRIVE_SHARE_ROLE_WRITER: Final[str] = "writer"
DRIVE_SHARE_ALLOWED_ROLES: Final[frozenset[str]] = frozenset(
    {DRIVE_SHARE_ROLE_READER, DRIVE_SHARE_ROLE_COMMENTER, DRIVE_SHARE_ROLE_WRITER}
)

# ---------------------------------------------------------------------------
# Export mime types — Google-native docs export via Drive's export_media, not
# get_media (see drive_actions.export_media_to_blob). One format->mime map per
# product; each map's keys are the only accepted `format` values for that verb.
# ---------------------------------------------------------------------------
MIME_CSV: Final[str] = "text/csv"
MIME_XLSX: Final[str] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MIME_PDF: Final[str] = "application/pdf"
MIME_DOCX: Final[str] = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MIME_TXT: Final[str] = "text/plain"
MIME_PPTX: Final[str] = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

# Tab-source files for sheets_create_from_files — delimiter is derived from the
# file extension; any other extension is rejected (fail-loud, no sniffing).
SHEETS_TAB_FILE_DELIMITERS: Final[dict[str, str]] = {
    ".csv": ",",
    ".tsv": "\t",
}

SHEETS_EXPORT_FORMAT_CSV: Final[str] = "csv"
SHEETS_EXPORT_FORMAT_XLSX: Final[str] = "xlsx"
SHEETS_EXPORT_MIME_BY_FORMAT: Final[dict[str, str]] = {
    SHEETS_EXPORT_FORMAT_CSV: MIME_CSV,
    SHEETS_EXPORT_FORMAT_XLSX: MIME_XLSX,
}
SHEETS_DEFAULT_EXPORT_FORMAT: Final[str] = SHEETS_EXPORT_FORMAT_CSV
SHEETS_VALUE_INPUT_OPTION_USER_ENTERED: Final[str] = "USER_ENTERED"

DOCS_EXPORT_FORMAT_PDF: Final[str] = "pdf"
DOCS_EXPORT_FORMAT_DOCX: Final[str] = "docx"
DOCS_EXPORT_FORMAT_TXT: Final[str] = "txt"
DOCS_EXPORT_MIME_BY_FORMAT: Final[dict[str, str]] = {
    DOCS_EXPORT_FORMAT_PDF: MIME_PDF,
    DOCS_EXPORT_FORMAT_DOCX: MIME_DOCX,
    DOCS_EXPORT_FORMAT_TXT: MIME_TXT,
}
DOCS_DEFAULT_EXPORT_FORMAT: Final[str] = DOCS_EXPORT_FORMAT_PDF

SLIDES_EXPORT_FORMAT_PDF: Final[str] = "pdf"
SLIDES_EXPORT_FORMAT_PPTX: Final[str] = "pptx"
SLIDES_EXPORT_MIME_BY_FORMAT: Final[dict[str, str]] = {
    SLIDES_EXPORT_FORMAT_PDF: MIME_PDF,
    SLIDES_EXPORT_FORMAT_PPTX: MIME_PPTX,
}
SLIDES_DEFAULT_EXPORT_FORMAT: Final[str] = SLIDES_EXPORT_FORMAT_PDF

# ---------------------------------------------------------------------------
# Error codes (API-facing use the gsuite.* prefix)
# ---------------------------------------------------------------------------
ERROR_VAULT_NOT_AVAILABLE: Final[str] = "vault_service_not_available"
ERROR_ADDRESS_BOOK_NOT_AVAILABLE: Final[str] = "address_book_service_not_available"
ERROR_BLOB_STORAGE_NOT_AVAILABLE: Final[str] = "blob_storage_service_not_available"
ERROR_ADDRESS_BOOK_ENTRY_MISSING: Final[str] = "address_book_entry_missing"
ERROR_ADDRESS_BOOK_ENTRY_INCOMPLETE: Final[str] = "address_book_entry_incomplete"
ERROR_TOKEN_EXCHANGE_FAILED: Final[str] = "token_exchange_failed"
ERROR_REFRESH_TOKEN_ROTATE_FAILED: Final[str] = "refresh_token_rotate_failed"
ERROR_TOKEN_STORE_FAILED: Final[str] = "token_store_failed"
ERROR_OAUTH_STATE_INVALID: Final[str] = "oauth_state_invalid"
ERROR_SERVER_NOT_STARTED: Final[str] = "callback_server_not_started"
ERROR_SERVER_START_FAILED: Final[str] = "callback_server_start_failed"

# gsuite.* — surfaced to callers of the Workspace verbs
ERROR_NOT_CONNECTED: Final[str] = "gsuite.not_connected"
ERROR_AUTH_EXPIRED: Final[str] = "gsuite.auth_expired"
ERROR_PERMISSION_DENIED: Final[str] = "gsuite.permission_denied"
ERROR_RATE_LIMITED: Final[str] = "gsuite.rate_limited"
ERROR_NOT_FOUND: Final[str] = "gsuite.not_found"
ERROR_INVALID_PARAMS: Final[str] = "gsuite.invalid_params"
ERROR_API_ERROR: Final[str] = "gsuite.api_error"

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
RESULT_TYPE_CONNECT: Final[str] = "g_suite_connect_account_result"
RESULT_TYPE_INTERFACE_START: Final[str] = "g_suite_interface_start_result"
RESULT_TYPE_INTERFACE_STOP: Final[str] = "g_suite_interface_stop_result"
RESULT_TYPE_GMAIL_LIST: Final[str] = "g_suite_gmail_list_result"
RESULT_TYPE_GMAIL_MESSAGE: Final[str] = "g_suite_gmail_message_result"
RESULT_TYPE_GMAIL_SEND: Final[str] = "g_suite_gmail_send_result"
RESULT_TYPE_DRIVE_LIST: Final[str] = "g_suite_drive_list_result"
RESULT_TYPE_DRIVE_DOWNLOAD: Final[str] = "g_suite_drive_download_result"
RESULT_TYPE_DRIVE_UPLOAD: Final[str] = "g_suite_drive_upload_result"
RESULT_TYPE_DRIVE_CREATE_FOLDER: Final[str] = "g_suite_drive_create_folder_result"
RESULT_TYPE_DRIVE_SHARE: Final[str] = "g_suite_drive_share_result"
RESULT_TYPE_SHEETS_CREATE: Final[str] = "g_suite_sheets_create_result"
RESULT_TYPE_SHEETS_GET_VALUES: Final[str] = "g_suite_sheets_get_values_result"
RESULT_TYPE_SHEETS_UPDATE_VALUES: Final[str] = "g_suite_sheets_update_values_result"
RESULT_TYPE_SHEETS_APPEND_VALUES: Final[str] = "g_suite_sheets_append_values_result"
RESULT_TYPE_SHEETS_BATCH_UPDATE: Final[str] = "g_suite_sheets_batch_update_result"
RESULT_TYPE_SHEETS_CREATE_FROM_FILES: Final[str] = "g_suite_sheets_create_from_files_result"
RESULT_TYPE_SHEETS_EXPORT: Final[str] = "g_suite_sheets_export_result"
RESULT_TYPE_DOCS_CREATE: Final[str] = "g_suite_docs_create_result"
RESULT_TYPE_DOCS_GET: Final[str] = "g_suite_docs_get_result"
RESULT_TYPE_DOCS_BATCH_UPDATE: Final[str] = "g_suite_docs_batch_update_result"
RESULT_TYPE_DOCS_EXPORT: Final[str] = "g_suite_docs_export_result"
RESULT_TYPE_SLIDES_CREATE: Final[str] = "g_suite_slides_create_result"
RESULT_TYPE_SLIDES_GET: Final[str] = "g_suite_slides_get_result"
RESULT_TYPE_SLIDES_BATCH_UPDATE: Final[str] = "g_suite_slides_batch_update_result"
RESULT_TYPE_SLIDES_EXPORT: Final[str] = "g_suite_slides_export_result"
