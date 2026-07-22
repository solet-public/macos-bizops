"""Salesforce plugin constants.

Single source of truth for every magic value: the address-book entry + field
identifiers, the sf CLI invocation knobs, the pinned API version, error codes
(``sf.*`` prefix), result types, and caps. No magic strings anywhere else in
the plugin.

Auth model (operator-ratified 2026-07-14, full CLI delegation — replacing
the sf-CLI session-borrow client factory, dead on current CLI releases
because `sf org display --json` now redacts `accessToken` unconditionally):
every verb shells out to the `sf` CLI itself. The durable credential is the
CLI's own keychain-backed refresh token, established once via
``sf org login web`` — the platform stores NO Salesforce secret of its own,
and no access token of any kind ever enters this process.
"""

from typing import Final

# ---------------------------------------------------------------------------
# Plugin identity
# ---------------------------------------------------------------------------
PLUGIN_NAME: Final[str] = "salesforce_plugin"
PLUGIN_VERSION: Final[str] = "3.0.0"

# ---------------------------------------------------------------------------
# Address book entry — which CLI org this plugin is allowed to talk to
# ---------------------------------------------------------------------------
ADDRESS_BOOK_ENTRY_NAME: Final[str] = "salesforce_org"
ADDRESS_BOOK_ENTRY_TYPE: Final[str] = "api"
ADDRESS_BOOK_ENTRY_DESCRIPTION: Final[str] = (
    "Salesforce org binding for salesforce_plugin: target_org (the sf CLI "
    "alias or username every verb is invoked against) and instance_host (the "
    "pinned my-domain host that alias must resolve to)."
)
ADDRESS_BOOK_FIELD_TARGET_ORG: Final[str] = "target_org"
ADDRESS_BOOK_FIELD_INSTANCE_HOST: Final[str] = "instance_host"

# ---------------------------------------------------------------------------
# sf CLI invocation
# ---------------------------------------------------------------------------
# Path to the sf binary. The default resolves via PATH; deployments where the
# platform process's PATH does not carry the CLI (LaunchAgent) pin an absolute
# path via the plugin config key below.
CONFIG_KEY_SF_CLI_PATH: Final[str] = "sf_cli_path"
DEFAULT_SF_CLI_PATH: Final[str] = "sf"
SF_CLI_TIMEOUT_SECONDS: Final[float] = 30.0

# ---------------------------------------------------------------------------
# API version pin (never floats — an operator override still resolves
# through this same config key, plugin.yaml's `api_version`).
# ---------------------------------------------------------------------------
DEFAULT_API_VERSION: Final[str] = "62.0"
CONFIG_KEY_API_VERSION: Final[str] = "api_version"

# ---------------------------------------------------------------------------
# Caps + export (A3/A4, 2026-07-16): interactive reads are inline-only and
# fail loud over the caps; bulk results land as ONE workspace .tsv file via
# export_soql, gated by realpath+commonpath containment under the
# operator-configured roots.
# ---------------------------------------------------------------------------
SOQL_DEFAULT_MAX_RECORDS: Final[int] = 200
SOQL_MAX_RECORDS_CAP: Final[int] = 1000
INLINE_BYTE_CAP: Final[int] = 200_000
# export_soql fetches up to this bound (truncation is flagged, never silent).
SOQL_EXPORT_ROW_CAP: Final[int] = 50_000
TSV_SUFFIX: Final[str] = ".tsv"
CONFIG_KEY_EXPORT_ALLOWED_ROOTS: Final[str] = "export_allowed_roots"

# ---------------------------------------------------------------------------
# Error codes (sf.* prefix — surfaced to callers of the verbs)
# ---------------------------------------------------------------------------
ERROR_ADDRESS_BOOK_NOT_AVAILABLE: Final[str] = "address_book_service_not_available"
ERROR_ADDRESS_BOOK_ENTRY_MISSING: Final[str] = "address_book_entry_missing"
ERROR_ADDRESS_BOOK_ENTRY_INCOMPLETE: Final[str] = "address_book_entry_incomplete"

ERROR_NOT_CONFIGURED: Final[str] = "sf.not_configured"
ERROR_INVALID_PARAMS: Final[str] = "sf.invalid_params"
ERROR_AUTH_FAILED: Final[str] = "sf.auth_failed"
ERROR_SESSION_EXPIRED: Final[str] = "sf.session_expired"
ERROR_PERMISSION_DENIED: Final[str] = "sf.permission_denied"
ERROR_NOT_FOUND: Final[str] = "sf.not_found"
ERROR_MALFORMED_QUERY: Final[str] = "sf.malformed_query"
ERROR_RATE_LIMITED: Final[str] = "sf.rate_limited"
ERROR_API_ERROR: Final[str] = "sf.api_error"
ERROR_RESULT_TOO_LARGE: Final[str] = "sf.result_too_large"
ERROR_EXPORT_PATH_REFUSED: Final[str] = "sf.export_path_refused"

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
RESULT_TYPE_SOQL_QUERY: Final[str] = "salesforce_soql_query_result"
RESULT_TYPE_EXPORT_SOQL: Final[str] = "salesforce_export_soql_result"
RESULT_TYPE_GET_RECORD: Final[str] = "salesforce_get_record_result"
RESULT_TYPE_DESCRIBE_SOBJECT: Final[str] = "salesforce_describe_sobject_result"
RESULT_TYPE_LIST_SOBJECTS: Final[str] = "salesforce_list_sobjects_result"
RESULT_TYPE_CREATE_RECORD: Final[str] = "salesforce_create_record_result"
RESULT_TYPE_UPDATE_RECORD: Final[str] = "salesforce_update_record_result"
RESULT_TYPE_DELETE_RECORD: Final[str] = "salesforce_delete_record_result"
RESULT_TYPE_TEST_CONNECTION: Final[str] = "salesforce_test_connection_result"
