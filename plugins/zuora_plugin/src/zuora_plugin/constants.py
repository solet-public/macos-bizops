"""Zuora plugin constants.

Single source of truth for every magic value: the address-book entry + field
identifiers, error codes (``zuora.*`` prefix), result types, and caps. No
magic strings anywhere else in the plugin.

Auth model (2026-06-20 design, hardened to the wave-2 security posture):
OAuth 2.0 client-credentials — the simplest of the platform's connector auth
models. No browser, no callback server. The bearer token is short-lived
(~1h), re-mintable, and held ONLY in process memory — never vaulted (the
durable credential is the client_secret, not the token).
"""

import os
from typing import Final


def _homunculus_or_fail() -> str:
    """Resolve HOMUNCULUS_NAME at import-time for the scoped vault key.

    Mirrors the fast-fail helper in salesforce_plugin.constants / jira_plugin.constants.
    """
    name = os.environ.get("HOMUNCULUS_NAME", "").strip()
    if not name:
        raise RuntimeError(
            "zuora_plugin.constants: HOMUNCULUS_NAME env var is "
            "required to resolve the scoped client_secret vault key.",
        )
    return name


_HOMUNCULUS: Final[str] = _homunculus_or_fail()

# ---------------------------------------------------------------------------
# Plugin identity
# ---------------------------------------------------------------------------
PLUGIN_NAME: Final[str] = "zuora_plugin"
PLUGIN_VERSION: Final[str] = "1.0.0"

# Blob storage namespace (Data Query spills + bulk exports).
BLOB_NAMESPACE: Final[str] = "zuora_plugin"

# ---------------------------------------------------------------------------
# Chain-consumed vault key — the OAuth client_secret.
# ---------------------------------------------------------------------------
# The "zuora_tenant" address-book entry's ``client_secret`` field stores a
# ``vault::<homunculus>.default_address_book_plugin.zuora_client_secret``
# reference. CHAIN-CONSUMED via ``resolve_with_secrets`` (never read directly
# under this plugin's identity), so it lives in the RESOLVER's namespace —
# post-2026-06-07 vault namespace enforcement requires the key's ``<plugin>``
# segment to equal the retrieving caller. Therefore NOT declared in
# get_required_vault_keys / get_declared_vault_keys (both return []).
VAULT_KEY_CLIENT_SECRET: Final[str] = (
    f"{_HOMUNCULUS}.default_address_book_plugin.zuora_client_secret"
)

# ---------------------------------------------------------------------------
# Address book entry — Zuora tenant identity + credentials
# ---------------------------------------------------------------------------
ADDRESS_BOOK_ENTRY_NAME: Final[str] = "zuora_tenant"
ADDRESS_BOOK_ENTRY_TYPE: Final[str] = "api"
ADDRESS_BOOK_ENTRY_DESCRIPTION: Final[str] = (
    "Zuora tenant identity + credentials (base_url environment selector, "
    "client_id, client_secret vault-ref) for zuora_plugin."
)
ADDRESS_BOOK_FIELD_BASE_URL: Final[str] = "base_url"
ADDRESS_BOOK_FIELD_CLIENT_ID: Final[str] = "client_id"
ADDRESS_BOOK_FIELD_CLIENT_SECRET: Final[str] = "client_secret"

# Known Zuora REST environments (base_url IS the environment selector).
ZUORA_BASE_URL_US_PRODUCTION: Final[str] = "https://rest.zuora.com"
ZUORA_BASE_URL_EU_PRODUCTION: Final[str] = "https://rest.eu.zuora.com"
ZUORA_BASE_URL_SANDBOX: Final[str] = "https://rest.apisandbox.zuora.com"

# ---------------------------------------------------------------------------
# HTTP client knobs
# ---------------------------------------------------------------------------
DEFAULT_REQUEST_TIMEOUT_SECONDS: Final[float] = 30.0
CONFIG_KEY_REQUEST_TIMEOUT_SECONDS: Final[str] = "request_timeout_seconds"
# Re-fetch the bearer this many seconds before its recorded expiry (clock-skew
# margin), matching the design appendix's token-manager sketch.
TOKEN_REFRESH_MARGIN_SECONDS: Final[float] = 30.0

# ---------------------------------------------------------------------------
# Caps + spill
# ---------------------------------------------------------------------------
DATA_QUERY_DEFAULT_MAX_ROWS: Final[int] = 200
DATA_QUERY_MAX_ROWS_CAP: Final[int] = 1000
INLINE_BYTE_CAP: Final[int] = 200_000
DATA_QUERY_SPILL_FILENAME: Final[str] = "data_query_results.json"
BULK_EXPORT_ROW_CAP: Final[int] = 50_000

EXPORT_FORMAT_CSV: Final[str] = "csv"
EXPORT_FORMAT_JSON: Final[str] = "json"
EXPORT_FORMATS: Final[frozenset[str]] = frozenset({EXPORT_FORMAT_CSV, EXPORT_FORMAT_JSON})
DEFAULT_EXPORT_FORMAT: Final[str] = EXPORT_FORMAT_CSV
MIME_CSV: Final[str] = "text/csv"
MIME_JSON: Final[str] = "application/json"

# ---------------------------------------------------------------------------
# Object types this connector CRUDs (Object/Actions API).
# ---------------------------------------------------------------------------
SUPPORTED_OBJECT_TYPES: Final[frozenset[str]] = frozenset(
    {"Account", "Subscription", "Invoice", "Payment", "Product"}
)

# ---------------------------------------------------------------------------
# Error codes (zuora.* prefix — surfaced to callers of the verbs)
# ---------------------------------------------------------------------------
ERROR_ADDRESS_BOOK_NOT_AVAILABLE: Final[str] = "address_book_service_not_available"
ERROR_BLOB_STORAGE_NOT_AVAILABLE: Final[str] = "blob_storage_service_not_available"
ERROR_ADDRESS_BOOK_ENTRY_MISSING: Final[str] = "address_book_entry_missing"
ERROR_ADDRESS_BOOK_ENTRY_INCOMPLETE: Final[str] = "address_book_entry_incomplete"

ERROR_NOT_CONFIGURED: Final[str] = "zuora.not_configured"
ERROR_INVALID_PARAMS: Final[str] = "zuora.invalid_params"
ERROR_AUTH_FAILED: Final[str] = "zuora.auth_failed"
ERROR_OBJECT_NOT_FOUND: Final[str] = "zuora.object_not_found"
ERROR_VALIDATION_FAILED: Final[str] = "zuora.validation_failed"
ERROR_RATE_LIMITED: Final[str] = "zuora.rate_limited"
ERROR_QUERY_FAILED: Final[str] = "zuora.query_failed"
ERROR_API_ERROR: Final[str] = "zuora.api_error"

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
RESULT_TYPE_DATA_QUERY: Final[str] = "zuora_data_query_result"
RESULT_TYPE_GET_OBJECT: Final[str] = "zuora_get_object_result"
RESULT_TYPE_CREATE_OBJECT: Final[str] = "zuora_create_object_result"
RESULT_TYPE_UPDATE_OBJECT: Final[str] = "zuora_update_object_result"
RESULT_TYPE_LIST_SUBSCRIPTIONS: Final[str] = "zuora_list_subscriptions_result"
RESULT_TYPE_GET_INVOICE: Final[str] = "zuora_get_invoice_result"
RESULT_TYPE_LIST_INVOICES: Final[str] = "zuora_list_invoices_result"
RESULT_TYPE_BULK_EXPORT: Final[str] = "zuora_bulk_export_result"
RESULT_TYPE_TEST_CONNECTION: Final[str] = "zuora_test_connection_result"
