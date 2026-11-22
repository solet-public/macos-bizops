"""Salesforce plugin constants.

Single source of truth for every magic value: the vault keys of the bound
orgs, the pre-build-6 address-book entry + field identifiers, the sf CLI
invocation knobs, the pinned API version, error codes (``sf.*`` prefix),
result types, and caps. No magic strings anywhere else in the plugin.

Auth model (operator-ratified 2026-07-14, full CLI delegation — replacing
the sf-CLI session-borrow client factory, dead on current CLI releases
because `sf org display --json` now redacts `accessToken` unconditionally):
every verb shells out to the `sf` CLI itself, and the durable credential of
each bound org is the CLI's own session for it. The plugin's vault holds the
index of bound orgs and, only for orgs that log in unattended, the login
secrets the operator stored (``org_secrets.py``); the plugin never holds a
session.
"""

import os
from typing import Final


def _solet_or_fail() -> str:
    """Resolve SOLET_NAME at import time for scoped vault keys (``<solet>.<plugin>.<credential>``)."""
    name = os.environ.get("SOLET_NAME", "").strip()
    if not name:
        raise RuntimeError("salesforce_plugin.constants: SOLET_NAME env var is required to resolve scoped vault keys.")
    return name


_SOLET = _solet_or_fail()

# ---------------------------------------------------------------------------
# Plugin identity
# ---------------------------------------------------------------------------
PLUGIN_NAME: Final[str] = "salesforce_plugin"
PLUGIN_VERSION: Final[str] = "3.0.0"

# ---------------------------------------------------------------------------
# Bound orgs (build 6, the Google multi-account pattern; orgs.py). The index
# of bindings and the default org is ONE vault value in this plugin's own
# namespace. Operator-stored login secrets (a JWT private key, an SFDX auth
# URL, an access token, a connected app client secret) live under the
# org_secret__ prefix and are read only inside the call that uses them.
# ---------------------------------------------------------------------------
VAULT_KEY_ORGS_INDEX: Final[str] = f"{_SOLET}.salesforce_plugin.orgs_index"
VAULT_KEY_ORG_SECRET_PREFIX: Final[str] = f"{_SOLET}.salesforce_plugin.org_secret__"
VAULT_TAG_ORGS_INDEX: Final[str] = "salesforce_orgs_index"
# APP_HOME/data/plugin_data/<plugin>/jwt_keys: a JWT key file exists there only
# while a CLI call for an org that logged in with it runs (org_secrets.py).
JWT_KEY_DIRECTORY_PARTS: Final[tuple[str, ...]] = ("data", "plugin_data", "salesforce_plugin", "jwt_keys")

# ---------------------------------------------------------------------------
# Address book entry — the single org binding that predates build 6. It is
# migrated into the vault index on first use and kept until that org is
# disconnected (orgs.py).
# ---------------------------------------------------------------------------
ADDRESS_BOOK_ENTRY_NAME: Final[str] = "salesforce_org"
ADDRESS_BOOK_FIELD_TARGET_ORG: Final[str] = "target_org"
ADDRESS_BOOK_FIELD_INSTANCE_HOST: Final[str] = "instance_host"
# The address book's own error code for a name that is not registered.
ADDRESS_BOOK_NOT_FOUND_CODE: Final[str] = "address_book.not_found"

# ---------------------------------------------------------------------------
# sf CLI invocation
# ---------------------------------------------------------------------------
# Path to the sf binary. The default resolves via PATH; deployments where the
# platform process's PATH does not carry the CLI (LaunchAgent) pin an absolute
# path via the plugin config key below.
CONFIG_KEY_SF_CLI_PATH: Final[str] = "sf_cli_path"
DEFAULT_SF_CLI_PATH: Final[str] = "sf"
SF_CLI_TIMEOUT_SECONDS: Final[float] = 30.0
# A browser login waits for the operator to sign in (SSO and MFA included).
SF_CLI_WEB_LOGIN_TIMEOUT_SECONDS: Final[float] = 300.0
# Org lifecycle calls that finish server-side work before answering: resuming
# a scratch org authorizes it and deploys its settings; deletes and sandbox
# requests make several vendor calls. Creates are always submitted --async.
SF_CLI_LIFECYCLE_TIMEOUT_SECONDS: Final[float] = 300.0
# The sf CLI prints access tokens, passwords and SFDX auth URLs unmasked when
# this is true in its environment; every child process gets it false.
SF_SHOW_SECRETS_ENV: Final[str] = "SF_TEMP_SHOW_SECRETS"
RESULT_TYPE_AUTHENTICATED_REQUEST: Final[str] = "salesforce_authenticated_request_result"
# Sforce-Locator and Sforce-NumberOfRecords page Bulk API 2.0 query results.
REQUEST_RESPONSE_HEADERS: Final[frozenset[str]] = frozenset({"content-type", "retry-after", "sforce-limit-info", "x-request-id", "sforce-locator", "sforce-numberofrecords"})
# Parsing evidence, never a request permission policy: an unfamiliar printed
# field leaves the complete transcript in the body rather than guessing.
CLI_RESPONSE_HEADER_NAMES: Final[frozenset[str]] = REQUEST_RESPONSE_HEADERS | frozenset(
    {"content-length", "transfer-encoding", "connection", "keep-alive", "date", "server", "content-encoding", "cache-control", "etag", "last-modified", "vary", "location", "set-cookie", "authorization", "www-authenticate", "strict-transport-security"}
)
REDACTED: Final[str] = "[REDACTED]"

# ---------------------------------------------------------------------------
# API version pin (never floats — an operator override still resolves
# through this same config key, plugin.yaml's `api_version`).
# ---------------------------------------------------------------------------
DEFAULT_API_VERSION: Final[str] = "62.0"
CONFIG_KEY_API_VERSION: Final[str] = "api_version"

# ---------------------------------------------------------------------------
# Caps + export (business-data limits + data-export migration, 2026-08-02 —
# workbench/2026-08-02_business_data_limits_and_spill_floor_design_coordinator_day.md).
# Both soql_query and export_soql now ALWAYS write to a caller-supplied path
# (07-29 data-export requirement, unconditional — the former INLINE_BYTE_CAP/inline-return
# branch is deleted, not lowered: no record-read verb returns record values
# inline at any size). DEFAULT_ROW_LIMIT is the fetch ceiling absent an
# explicit, acknowledged override; SOQL_MAX_RECORDS_CAP is soql_query's
# override ceiling, SOQL_EXPORT_ROW_CAP is export_soql's (the "N>>500"
# Pattern-A route, §7.2) — both are pushed into the sf CLI's own
# SF_ORG_MAX_QUERY_LIMIT env override (soql_actions._run_soql), never
# fetch-everything-then-truncate.
# ---------------------------------------------------------------------------
DEFAULT_ROW_LIMIT: Final[int] = 500
SOQL_MAX_RECORDS_CAP: Final[int] = 1000
# SOQL_EXPORT_ROW_CAP reconciled per Reviewer-D's §3.2 adjudication + Dawn's
# ruling (2026-08-02): the number is REAL and REACHABLE (jsforce autoFetch
# pages through the REST API's own nextRecordsUrl up to the passed
# SF_ORG_MAX_QUERY_LIMIT), but its attribution is OURS-ARBITRARY, not a
# Salesforce-imposed ceiling — 50,000 is the Apex governor limit (SOQL run
# from inside Apex code), a different call path this plugin never uses (it
# shells to the sf CLI -> jsforce REST client, whose actual vendor fact is a
# 2,000/call REST query batch size with no vendor total ceiling). Never cite
# 50,000 as a Salesforce limit in a process description; cite the 2,000/call
# REST fact if a vendor number is wanted at all.
SOQL_EXPORT_ROW_CAP: Final[int] = 50_000
TSV_SUFFIX: Final[str] = ".tsv"
CSV_SUFFIX: Final[str] = ".csv"
# Bulk v2 ingest input containment — the read-side mirror of
# the same live path addresses as exports (realpath containment
# via ananta.file_locations.containment). See knowledge_base/01_salesforce_overview.md
# "Bulk v2 ingest" section for the full posture.

# ---------------------------------------------------------------------------
# Bulk v2 ingest — the vendor's full operation set. hardDelete bypasses the
# recycle bin; Salesforce itself requires the "Bulk API Hard Delete" system
# permission for it, so vendor RBAC is the boundary (USER ruling rul_4c99a30d).
# ---------------------------------------------------------------------------
BULK_INGEST_HARD_DELETE_OPERATION: Final[str] = "hardDelete"
BULK_INGEST_OPERATIONS: Final[frozenset[str]] = frozenset({"insert", "update", "upsert", "delete", BULK_INGEST_HARD_DELETE_OPERATION})
BULK_JOB_ABORT_STATE: Final[str] = "Aborted"

# ---------------------------------------------------------------------------
# Override friction (§5) — required together or not at all; absent means the
# effective limit is DEFAULT_ROW_LIMIT.
# ---------------------------------------------------------------------------
PARAM_ACKNOWLEDGE_OVERRIDE: Final[str] = "acknowledge_default_limit_override"
PARAM_ROW_LIMIT: Final[str] = "row_limit"
# soql_query/export_soql: true selects the vendor queryAll resource (sf data
# query --all-rows), which also returns deleted and archived records.
PARAM_INCLUDE_DELETED: Final[str] = "include_deleted"

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
ERROR_EXPORT_PATH_REFUSED: Final[str] = "sf.export_path_refused"
ERROR_IMPORT_PATH_REFUSED: Final[str] = "sf.import_path_refused"
ERROR_PROVISION_CONSENT_REQUIRED: Final[str] = "sf.provision_consent_required"
# Typed-family HTTP outcomes with no narrower sf.* class above.
ERROR_BAD_REQUEST: Final[str] = "sf.bad_request"
ERROR_CONFLICT: Final[str] = "sf.conflict"
ERROR_PRECONDITION_FAILED: Final[str] = "sf.precondition_failed"
# Bound orgs: `org` names no binding, or several are bound and none is the default.
ERROR_UNKNOWN_ORG: Final[str] = "sf.unknown_org"
# The vault index of bound orgs could not be read or written.
ERROR_ORG_REGISTRY_FAILED: Final[str] = "sf.org_registry_failed"
ERROR_VAULT_NOT_AVAILABLE: Final[str] = "vault_service_not_available"

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
RESULT_TYPE_PROBE_CLI: Final[str] = "salesforce_probe_cli_result"
RESULT_TYPE_PROVISION_CLI: Final[str] = "salesforce_provision_cli_result"
RESULT_TYPE_BULK_INGEST_SUBMIT: Final[str] = "salesforce_bulk_ingest_submit_result"
RESULT_TYPE_BULK_JOB_STATUS: Final[str] = "salesforce_bulk_job_status_result"
RESULT_TYPE_BULK_JOB_RESULTS: Final[str] = "salesforce_bulk_job_results_result"
RESULT_TYPE_BULK_JOB_ABORT: Final[str] = "salesforce_bulk_job_abort_result"
RESULT_TYPE_COMPOSITE: Final[str] = "salesforce_composite_result"
RESULT_TYPE_TOOLING: Final[str] = "salesforce_tooling_result"
RESULT_TYPE_BULK_JOBS: Final[str] = "salesforce_bulk_jobs_result"
RESULT_TYPE_REPORTS: Final[str] = "salesforce_reports_result"
RESULT_TYPE_DASHBOARDS: Final[str] = "salesforce_dashboards_result"
RESULT_TYPE_REST_RESOURCES: Final[str] = "salesforce_rest_resources_result"
RESULT_TYPE_ACTIONS: Final[str] = "salesforce_actions_result"
RESULT_TYPE_METADATA: Final[str] = "salesforce_metadata_result"
RESULT_TYPE_CONTENT_FILES: Final[str] = "salesforce_content_files_result"
RESULT_TYPE_LIST_ORGS: Final[str] = "salesforce_list_orgs_result"
RESULT_TYPE_BIND_ORG: Final[str] = "salesforce_bind_org_result"
RESULT_TYPE_LOGIN: Final[str] = "salesforce_login_result"
RESULT_TYPE_LOGOUT: Final[str] = "salesforce_logout_result"
RESULT_TYPE_SET_DEFAULT_ORG: Final[str] = "salesforce_set_default_org_result"
RESULT_TYPE_DISCONNECT_ORG: Final[str] = "salesforce_disconnect_org_result"
RESULT_TYPE_ORG_LIFECYCLE: Final[str] = "salesforce_org_lifecycle_result"
