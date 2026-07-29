"""Zuora plugin entry point — Data Query + object CRUD + billing reads + bulk export.

Headless OAuth 2.0 client-credentials auth (chain-consumed from the
``zuora_tenant`` address-book entry) against a Zuora tenant — no browser
flow, no callback server, the simplest of the platform's connector auth
models. No delete verb (billing records are voided/cancelled through Zuora's
own workflow, not deleted through this tool).

Verbs (all EDGE):
  - data_query                                              — read
  - get_object / list_subscriptions / get_invoice / list_invoices — read
  - create_object / update_object                           — write
  - bulk_export                                              — read (spill)
  - test_connection                                          — diagnostic

Security posture (umbrella design §2, applied to this pre-wave-2 design;
process_export deny retired by operator ruling 2026-07-15 — see
workbench/2026-07-15_result_error_processing_architecture_deep_dive.md): every
verb is directly process_call-able like any other process; auth/rate-limit error
messages are GENERIC fixed strings — never the raw response, which could
leak tenant-specific diagnostic detail (§1.6); the plugin reaches ONLY the
address-book-resolved tenant (no base_url param on any verb). No SQL-shaped
strings anywhere (pure REST), so the SQL-lockdown gate is silent for this
plugin — no allowlist entry needed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ananta.core.actions.action_metadata import (
    ContextHandling,
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.config.config_provider import ConfigProvider
from ananta.core.domain.enums import ActionStatus, ProcessorPolicyCategory
from ananta.core.plugins.plugin_base import PluginBase
from ananta.interfaces.edge_process_provider import (
    EdgeProcessDefinition,
    EdgeProcessProvider,
)

from . import billing_actions
from .app_config import AppConfigError, AppConfigLoader
from .billing_actions import ZuoraResponseError
from .constants import (
    BLOB_NAMESPACE,
    CONFIG_KEY_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ERROR_ADDRESS_BOOK_NOT_AVAILABLE,
    ERROR_API_ERROR,
    ERROR_BLOB_STORAGE_NOT_AVAILABLE,
    ERROR_INVALID_PARAMS,
    ERROR_NOT_CONFIGURED,
    INLINE_BYTE_CAP,
    PLUGIN_NAME,
    RESULT_TYPE_BULK_EXPORT,
    RESULT_TYPE_CREATE_OBJECT,
    RESULT_TYPE_DATA_QUERY,
    RESULT_TYPE_GET_INVOICE,
    RESULT_TYPE_GET_OBJECT,
    RESULT_TYPE_LIST_INVOICES,
    RESULT_TYPE_LIST_SUBSCRIPTIONS,
    RESULT_TYPE_TEST_CONNECTION,
    RESULT_TYPE_UPDATE_OBJECT,
)
from .errors import ZuoraServiceError, classify_zuora_response
from .http_client import ZuoraAuthError, ZuoraClient

# Field sensitivities: Zuora carries real billing/financial PII (invoices,
# payments, subscriptions) — treated at the DB-connector floor (0.5) rather
# than the generic-SaaS floor (0.3), since a leaked invoice/payment record is
# more consequential than a leaked CRM contact row. Metadata/ids stay low.


class ZuoraPlugin(PluginBase, EdgeProcessProvider):
    """Zuora subscription-billing connector (Data Query / object CRUD / billing reads) plugin."""

    name: str = PLUGIN_NAME

    def __init__(self) -> None:
        super().__init__()
        self.logger: logging.Logger | None = None
        self._address_book_service: Any | None = None
        self._blob_storage_service: Any | None = None
        self._app_config_loader: AppConfigLoader | None = None
        self._client: ZuoraClient | None = None

    # ------------------------------------------------------------------
    # VaultKeysProvider — no plugin-owned vault keys
    # ------------------------------------------------------------------

    def get_required_vault_keys(self) -> list[str]:
        """No vault keys are required at readiness.

        The client_secret is chain-consumed through the ``zuora_tenant``
        address-book entry — never a direct vault verb under this plugin's
        identity — so it is declared nowhere here. The re-minted bearer
        token lives only in process memory and is never vaulted.
        """
        return []

    def get_declared_vault_keys(self) -> list[str]:
        """No scoped vault keys are read or written directly by this plugin."""
        return []

    # ------------------------------------------------------------------
    # PluginBase lifecycle
    # ------------------------------------------------------------------

    def initialize(self, config: dict[str, object]) -> None:
        """Bind config_provider so yaml defaults + operator overrides take effect.

        Without this override the base ``initialize`` is a no-op and
        ``config_provider`` stays None forever — the ``or {}`` reads below then
        silently run on empty config (defect class found live on the snowflake
        sibling, 2026-07-16).
        """
        self.config_provider = ConfigProvider(self.name, config)

    def prepare_for_readiness(self) -> None:
        if self.orchestrator_ref is None:
            raise RuntimeError(f"{self.name}: orchestrator_ref not injected")
        self.logger = logging.getLogger(self.name)
        self._address_book_service = self.orchestrator_ref.get_service("address_book_service")
        if self._address_book_service is None:
            raise RuntimeError(
                f"{ERROR_ADDRESS_BOOK_NOT_AVAILABLE}: {self.name} requires "
                "address_book_service to resolve the zuora_tenant credentials"
            )
        # blob_storage is needed only for data_query spill / bulk_export and is
        # resolved lazily at first use (_blob_service): the platform constructs
        # blob_storage_service in the init_service_manager startup step, AFTER
        # every plugin's prepare_for_readiness — resolving it here caches None
        # forever and every spill hard-fails (Dax Part-20 §20.1).
        self._app_config_loader = AppConfigLoader(self._address_book_service)
        self.set_ready()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _request_timeout_seconds(self) -> float:
        config = self.config_provider or {}
        raw = config.get(CONFIG_KEY_REQUEST_TIMEOUT_SECONDS, DEFAULT_REQUEST_TIMEOUT_SECONDS)
        return float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else DEFAULT_REQUEST_TIMEOUT_SECONDS

    def _require_client(self) -> ZuoraClient:
        """Lazily build + cache the Zuora client from the resolved tenant config."""
        if self._app_config_loader is None:
            raise RuntimeError(ERROR_ADDRESS_BOOK_NOT_AVAILABLE)
        if self._client is None:
            config = self._app_config_loader.load()
            self._client = ZuoraClient(config, timeout_seconds=self._request_timeout_seconds())
        return self._client

    def _success(self, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": data,
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def _error(self, code: str, message: str) -> dict[str, Any]:
        return {
            "action_status": ActionStatus.ERROR.value,
            "data": {},
            "actions": [],
            "error": {"code": code, "message": message},
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def _blob_service(self) -> Any | None:
        """Resolve blob_storage_service lazily at point of use (cached once found).

        Readiness-time resolution is a known trap: the platform constructs
        blob_storage_service after every plugin's prepare_for_readiness, so a
        readiness-time get_service() returns None and the miss would be cached
        for the life of the plugin (Dax Part-20 §20.1).
        """
        if self._blob_storage_service is None and self.orchestrator_ref is not None:
            self._blob_storage_service = self.orchestrator_ref.get_service("blob_storage_service")
        return self._blob_storage_service

    def _store_blob(self, content: bytes, filename: str, mime_type: str) -> str:
        """Store a spilled/exported result as a blob; return the blob id (the *_blob_key)."""
        blob_service = self._blob_service()
        if blob_service is None:
            raise ZuoraServiceError(
                ERROR_BLOB_STORAGE_NOT_AVAILABLE,
                f"blob_storage_service is not available for result spill: the serialized "
                f"result is {len(content)} bytes and the inline-return cap is "
                f"{INLINE_BYTE_CAP} bytes; narrow the query (fewer rows/columns or a "
                f"smaller page) so the result fits inline",
            )
        result = blob_service.store_blob(
            BLOB_NAMESPACE, content, {"filename": filename, "mime_type": mime_type}
        )
        if not isinstance(result, dict) or result.get("action_status") != "completed":
            raise ZuoraServiceError(
                ERROR_BLOB_STORAGE_NOT_AVAILABLE,
                f"failed to store result blob for '{filename}' ({len(content)} bytes)",
            )
        blob_id = (result.get("data") or {}).get("blob_id")
        if not isinstance(blob_id, str) or not blob_id:
            raise ZuoraServiceError(
                ERROR_BLOB_STORAGE_NOT_AVAILABLE,
                f"blob storage returned no blob_id for '{filename}' ({len(content)} bytes)",
            )
        return blob_id

    def _run(self, produce: Callable[[ZuoraClient], dict[str, Any]], endpoint_name: str) -> dict[str, Any]:
        """Shared error-classification path for every Zuora verb."""
        try:
            client = self._require_client()
            data = produce(client)
        except ValueError as exc:
            return self._error(ERROR_INVALID_PARAMS, str(exc))
        except AppConfigError as exc:
            return self._error(ERROR_NOT_CONFIGURED, str(exc))
        except ZuoraServiceError as exc:
            return self._error(exc.code, str(exc))
        except ZuoraAuthError:
            return self._error("zuora.auth_failed", "Zuora OAuth token request failed.")
        except ZuoraResponseError as exc:
            code, message = classify_zuora_response(exc.response, is_query=exc.is_query)
            return self._error(code, message)
        except Exception as exc:  # noqa: BLE001 — any other transport fault -> generic
            if self.logger:
                self.logger.warning("%s: unexpected fault (%s)", endpoint_name, type(exc).__name__)
            return self._error(ERROR_API_ERROR, "Zuora API call failed.")
        if self.logger:
            self.logger.debug("%s: success", endpoint_name)
        return self._success(data)

    # ------------------------------------------------------------------
    # EdgeProcessProvider
    # ------------------------------------------------------------------

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        return {
            "data_query": _edge("data_query", RESULT_TYPE_DATA_QUERY, retryable=True),
            "get_object": _edge("get_object", RESULT_TYPE_GET_OBJECT, retryable=True),
            "create_object": _edge(
                "create_object", RESULT_TYPE_CREATE_OBJECT, retryable=False
            ),
            "update_object": _edge(
                "update_object", RESULT_TYPE_UPDATE_OBJECT, retryable=False
            ),
            "list_subscriptions": _edge(
                "list_subscriptions",
                RESULT_TYPE_LIST_SUBSCRIPTIONS,
                retryable=True,
            ),
            "get_invoice": _edge(
                "get_invoice", RESULT_TYPE_GET_INVOICE, retryable=True
            ),
            "list_invoices": _edge(
                "list_invoices", RESULT_TYPE_LIST_INVOICES, retryable=True
            ),
            "bulk_export": _edge(
                "bulk_export", RESULT_TYPE_BULK_EXPORT, retryable=True
            ),
            "test_connection": _edge(
                "test_connection", RESULT_TYPE_TEST_CONNECTION, retryable=True
            ),
        }

    # ------------------------------------------------------------------
    # @platform_process implementations
    # ------------------------------------------------------------------

    @platform_process(
        name="data_query",
        display_name="Zuora: Data Query",
        description=(
            "Run a ZOQL query (e.g. \"SELECT Id, Name FROM Account\") against the configured Zuora "
            "tenant. Returns records inline (up to max_rows, default 200, capped 1000) or a "
            "result_blob_key when the result is large."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "zoql": ParameterMetadata(type=ParameterType.STRING, required=True, description="The ZOQL query string."),
            "max_rows": ParameterMetadata(
                type=ParameterType.INTEGER, required=False, description="Max rows to return inline (default 200, capped 1000)."
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT, description="Records inline or a result_blob_key on spill."
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def data_query(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda client: billing_actions.data_query(client, params, self._store_blob), "data_query")

    @platform_process(
        name="get_object",
        display_name="Zuora: Get Object",
        description=(
            "Fetch one Account/Subscription/Invoice/Payment/Product object by type + id. "
            "Requires type and id."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "type": ParameterMetadata(
                type=ParameterType.STRING, required=True, description="Object type: Account, Subscription, Invoice, Payment, or Product."
            ),
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The object id."),
        },
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="The object's fields."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def get_object(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda client: billing_actions.get_object(client, params), "get_object")

    @platform_process(
        name="create_object",
        display_name="Zuora: Create Object",
        description="Create an Account/Subscription/Invoice/Payment/Product object with the given fields. Write action.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "type": ParameterMetadata(
                type=ParameterType.STRING, required=True, description="Object type: Account, Subscription, Invoice, Payment, or Product."
            ),
            "fields": ParameterMetadata(type=ParameterType.OBJECT, required=True, description="Non-empty object of field values."),
        },
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="New object id and success."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def create_object(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda client: billing_actions.create_object(client, params), "create_object")

    @platform_process(
        name="update_object",
        display_name="Zuora: Update Object",
        description="Apply a non-empty fields object to an existing object by type + id. Write action.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "type": ParameterMetadata(
                type=ParameterType.STRING, required=True, description="Object type: Account, Subscription, Invoice, Payment, or Product."
            ),
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The object id."),
            "fields": ParameterMetadata(type=ParameterType.OBJECT, required=True, description="Non-empty object of field values to set."),
        },
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Confirmation the update was applied."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def update_object(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda client: billing_actions.update_object(client, params), "update_object")

    @platform_process(
        name="list_subscriptions",
        display_name="Zuora: List Subscriptions",
        description="List an account's subscriptions. Requires account_id.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "account_id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The Zuora account id."),
        },
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="subscriptions: a list of subscription objects."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_subscriptions(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda client: billing_actions.list_subscriptions(client, params), "list_subscriptions")

    @platform_process(
        name="get_invoice",
        display_name="Zuora: Get Invoice",
        description="Fetch one invoice by id.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The invoice id."),
        },
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="The invoice's fields."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def get_invoice(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda client: billing_actions.get_invoice(client, params), "get_invoice")

    @platform_process(
        name="list_invoices",
        display_name="Zuora: List Invoices",
        description="List an account's invoices. Requires account_id.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "account_id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The Zuora account id."),
        },
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="invoices: a list of invoice objects."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_invoices(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda client: billing_actions.list_invoices(client, params), "list_invoices")

    @platform_process(
        name="bulk_export",
        display_name="Zuora: Bulk Export",
        description=(
            "Run a ZOQL query and export the full result (up to 50000 rows) to a csv or json blob; "
            "returns a result_blob_key. Requires zoql; format is csv (default) or json."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "zoql": ParameterMetadata(type=ParameterType.STRING, required=True, description="The ZOQL query string."),
            "format": ParameterMetadata(type=ParameterType.STRING, required=False, description="Export format: 'csv' (default) or 'json'."),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT, description="result_blob_key referencing the exported bytes, plus row_count and format."
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def bulk_export(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda client: billing_actions.bulk_export(client, params, self._store_blob), "bulk_export")

    @platform_process(
        name="test_connection",
        display_name="Zuora: Test Connection",
        description="Verify the configured Zuora credentials by minting a bearer token. Returns ok, base_url, client_id.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="ok, base_url, client_id."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def test_connection(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        loader = self._app_config_loader

        def _do(client: ZuoraClient) -> dict[str, Any]:
            config = loader.load() if loader is not None else None
            client.ensure_authenticated()
            return {
                "ok": True,
                "base_url": config.base_url if config is not None else "",
                "client_id": config.client_id if config is not None else "",
            }

        return self._run(_do, "test_connection")


def _edge(
    name: str,
    result_type: str,
    *,
    retryable: bool,
) -> EdgeProcessDefinition:
    return EdgeProcessDefinition(
        name=name,
        result_processor_template_customizations=MergeResultProcessorCustomizations(
            result_type=result_type,
        ),
        error_processor_template_customizations=MergeErrorProcessorCustomizations(
            retryable=retryable,
        ),
    )
