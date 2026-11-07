"""Zuora plugin entry point — typed Workflow, SQL Data Query, Files, Orders, Subscriptions, Accounts, Payment Methods, Payments, Custom Objects, Object Query, Billing Documents, Invoices, Credit and Debit Memos, Bill Runs, Usage, ZOQL, object and billing verbs.

Headless OAuth 2.0 client-credentials auth (chain-consumed from the
``zuora_tenant`` address-book entry) against a Zuora tenant — no browser
flow, no callback server, the simplest of the platform's connector auth
models. Vendor RBAC on the registered OAuth client is the only operation
boundary: reads, writes and deletes are equally first-class.

Verbs (all EDGE, all deferred-completion):
  - request                                                  — any relative tenant path (long tail)
  - Workflows (workflow_processes.py): list/get/update/delete, export/import,
    versions list/import/delete, run/get run/stop run, tasks list/get/rerun/
    batch update, usage metrics, callout auth providers create/update/delete
  - SQL Data Query and Files (data_query_processes.py): submit/list/get/cancel
    jobs, download result, download file, file status, restore file
  - Orders (order_processes.py): preview/create (sync and async), list (tenant,
    subscription owner, invoice owner, subscription, pending), get, update,
    activate, cancel, delete (sync and async), custom fields, trigger dates,
    delta metrics, async job status, commitment association rescan
  - Subscriptions (subscription_processes.py): preview new/existing, create,
    get current/version, update, renew, cancel, suspend, resume, delete,
    version custom fields, metrics
  - Accounts (account_processes.py): create/get/update/delete, summary,
    payment methods, default payment method, payment-method cascading
  - Payment Methods and Payments (payment_processes.py): payment-method
    create/decrypt/get/update/verify/scrub/delete, balance, stored credential
    profiles create/list/cancel/expire/profile data, wallet domains, payment
    sessions; payments list/create/get/update/apply/unapply/transfer/refund/
    cancel/delete, parts and item parts
  - Custom Objects and Object Query (custom_object_processes.py): definitions
    create/list/get/update/delete, records create/list/get/replace/patch/
    delete/batch, bulk jobs submit/upload/list/get/errors/cancel; Object
    Query list and get for every standard and custom object
  - Billing Documents and Invoices (invoice_processes.py): billing-document
    list, generate, file-deletion jobs; standalone invoice create (single and
    batch), update (single and batch), bulk post, unpost, cancel, delete,
    reverse, split, write off (sync and async job), email, PDF status, files
    list/upload, items, item taxation items, taxation item create,
    application parts
  - Credit Memos (credit_memo_processes.py) and Debit Memos
    (debit_memo_processes.py): create (from charges, from an invoice, bulk),
    list/get, update (single and bulk), post/unpost, cancel, delete, email,
    PDF generate/status, files list/upload, items, parts, taxation items;
    credit memos apply/unapply (sync and async job), refund, reverse, write
    off; debit memos collect, write off, application parts, due dates
  - Bill Runs and Usage (bill_run_usage_processes.py): bill run create/get/
    post/cancel/delete/email documents; usage import (file), create/get/
    update/delete records, usage rate detail of an invoice item
  - data_query / bulk_export                                 — ZOQL to TSV
  - get_object / list_subscriptions / get_invoice / list_invoices — reads
  - create_object / update_object                            — writes
  - test_connection                                          — diagnostic

Security posture (umbrella design §2, applied to this pre-wave-2 design;
process_export deny retired by operator ruling 2026-07-15 — see
workbench/2026-07-15_result_error_processing_architecture_deep_dive.md): every
verb is directly process_call-able like any other process; auth/rate-limit error
messages are GENERIC fixed strings — never the raw response, which could
leak tenant-specific diagnostic detail (§1.6); the plugin reaches ONLY the
address-book-resolved tenant (no base_url param on any verb) plus, for a
completed SQL Data Query job, the pre-signed dataFile URL Zuora itself issued,
fetched without the bearer token. Vendor queries are not platform persistence;
the SQL-access gate's connector-root declaration covers this plugin.
"""

from __future__ import annotations

import logging
import threading
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

from . import async_jobs, export_containment, request_actions, typed_verbs
from .account_processes import AccountProcesses
from .app_config import AppConfigError, AppConfigLoader
from .bill_run_usage_processes import BillRunUsageProcesses
from .billing_actions import ZuoraResponseError, v1_headers
from .commerce_fields import subscription_read_options
from .commerce_params import SEGMENT_PARAMS, V1_HEADER_PARAMS
from .constants import (
    BULK_EXPORT_ROW_CAP,
    CONFIG_KEY_EXPORT_ALLOWED_ROOTS,
    CONFIG_KEY_REQUEST_TIMEOUT_SECONDS,
    DATA_QUERY_MAX_ROWS_CAP,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_ROW_LIMIT,
    ERROR_ADDRESS_BOOK_NOT_AVAILABLE,
    ERROR_API_ERROR,
    ERROR_INVALID_PARAMS,
    ERROR_NOT_CONFIGURED,
    LIST_ROW_LIMIT_CAP,
    PARAM_ACKNOWLEDGE_OVERRIDE,
    PARAM_ROW_LIMIT,
    PLUGIN_NAME,
    RESULT_TYPE_BULK_EXPORT,
    RESULT_TYPE_CREATE_OBJECT,
    RESULT_TYPE_DATA_QUERY,
    RESULT_TYPE_GET_INVOICE,
    RESULT_TYPE_GET_OBJECT,
    RESULT_TYPE_LIST_INVOICES,
    RESULT_TYPE_LIST_SUBSCRIPTIONS,
    RESULT_TYPE_REQUEST,
    RESULT_TYPE_TEST_CONNECTION,
    RESULT_TYPE_UPDATE_OBJECT,
    TSV_SUFFIX,
    ZUORA_LIST_PAGE_SIZE_MAX,
    ZUORA_QUERY_PAGE_ROW_CAP,
)
from .credit_memo_processes import CreditMemoProcesses
from .custom_object_processes import CustomObjectProcesses
from .data_query_processes import DataQueryFileProcesses
from .debit_memo_processes import DebitMemoProcesses
from .errors import ZuoraServiceError, classify_zuora_response
from .http_client import ZuoraAuthError, ZuoraClient
from .invoice_processes import InvoiceProcesses
from .order_processes import OrderProcesses
from .payment_processes import PaymentProcesses
from .subscription_processes import SubscriptionProcesses
from .workflow_processes import WorkflowProcesses

# Field sensitivities: Zuora carries real billing/financial PII (invoices,
# payments, subscriptions) — treated at the DB-connector floor (0.5) rather
# than the generic-SaaS floor (0.3), since a leaked invoice/payment record is
# more consequential than a leaked CRM contact row. Metadata/ids stay low.


class ZuoraPlugin(
    WorkflowProcesses,
    DataQueryFileProcesses,
    OrderProcesses,
    SubscriptionProcesses,
    AccountProcesses,
    PaymentProcesses,
    CustomObjectProcesses,
    InvoiceProcesses,
    CreditMemoProcesses,
    DebitMemoProcesses,
    BillRunUsageProcesses,
    PluginBase,
    EdgeProcessProvider,
):
    """Zuora subscription-billing connector: typed Workflow, Data Query, Files, Orders, Subscriptions, Accounts, Payment Methods, Payments, Custom Objects, Object Query, Billing Document, Memo, Bill Run and Usage verbs, ZOQL, objects."""

    name: str = PLUGIN_NAME

    def __init__(self) -> None:
        super().__init__()
        self.logger: logging.Logger | None = None
        self._address_book_service: Any | None = None
        self._app_config_loader: AppConfigLoader | None = None
        self._client: ZuoraClient | None = None
        # D0.3 deferred-completion machinery (async_jobs.py) — lazily acquired /
        # started on first async-shaped dispatch, mirroring
        # comfyui_image_generation_plugin's _try_acquire_job_manager /
        # salesforce_plugin's own copy (boot order does not guarantee
        # orchestrator_ref.async_job_manager is set yet at
        # prepare_for_readiness time, but it always is by first dispatch).
        self._async_job_manager: Any | None = None
        self._worker_thread: threading.Thread | None = None
        self._worker_lock = threading.Lock()

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
            raise RuntimeError(f"{ERROR_ADDRESS_BOOK_NOT_AVAILABLE}: {self.name} requires address_book_service to resolve the zuora_tenant credentials")
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

    def _export_path_gate(
        self,
        output_tsv_path: str,
        *,
        param_name: str = "output_tsv_path",
        suffix: str | None = TSV_SUFFIX,
    ) -> str:
        """Admit an export path via workspace-root containment; return the realpath.

        Binds the operator's ``export_allowed_roots`` config (yaml default
        ``[]`` = refuse-all; no hardcoded callsite default per authoring trap
        #10) to the own-copy containment gate. A malformed config value is a
        loud config fault, never a silent admit-all or refuse-all.
        """
        config = self.config_provider or {}
        raw_roots = config.get(CONFIG_KEY_EXPORT_ALLOWED_ROOTS)
        roots: list[str] = []
        if raw_roots is not None:
            if not isinstance(raw_roots, list) or not all(isinstance(entry, str) for entry in raw_roots):
                raise ZuoraServiceError(
                    ERROR_NOT_CONFIGURED,
                    f"{CONFIG_KEY_EXPORT_ALLOWED_ROOTS} must be a list of directory path strings",
                )
            roots = list(raw_roots)
        return export_containment.assert_export_path_allowed(
            output_tsv_path,
            roots,
            config_key=CONFIG_KEY_EXPORT_ALLOWED_ROOTS,
            plugin_name=self.name,
            param_name=param_name,
            suffix=suffix,
        )

    def _run(self, produce: Callable[[ZuoraClient], dict[str, Any]], endpoint_name: str) -> dict[str, Any]:
        """Shared error-classification path for every Zuora verb."""
        try:
            client = self._require_client()
            data = produce(client)
        except ValueError as exc:
            return self._error(ERROR_INVALID_PARAMS, str(exc))
        except AppConfigError as exc:
            return self._error(ERROR_NOT_CONFIGURED, str(exc))
        except (ZuoraServiceError, export_containment.ExportPathRefusedError) as exc:
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

    def _dispatch_async(
        self,
        action_name: str,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """D0.3 ms-scale dispatch: create the job, return immediately — no Zuora REST
        call happens here."""
        try:
            create_result = async_jobs.create_job(
                self,
                action_name=action_name,
                params=params,
                state=state,
            )
        except ValueError as exc:
            return self._error(ERROR_INVALID_PARAMS, str(exc))
        except RuntimeError as exc:
            return self._error(ERROR_NOT_CONFIGURED, str(exc))
        if create_result.get("action_status") != "completed":
            error = create_result.get("error", {})
            message = str(error.get("message", "failed to create async job"))
            return self._error(ERROR_API_ERROR, message)
        return self._success(create_result["data"])

    def _dispatch_typed(
        self,
        action_name: str,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate a typed verb (every family in ``typed_verbs``), then queue it."""
        try:
            typed_verbs.validate_typed(action_name, params)
        except ValueError as exc:
            return self._error(ERROR_INVALID_PARAMS, str(exc))
        return self._dispatch_async(action_name, params, state)

    # ------------------------------------------------------------------
    # EdgeProcessProvider
    # ------------------------------------------------------------------

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        return {
            "request": _edge("request", RESULT_TYPE_REQUEST, retryable=False),
            "data_query": _edge("data_query", RESULT_TYPE_DATA_QUERY, retryable=True),
            "get_object": _edge("get_object", RESULT_TYPE_GET_OBJECT, retryable=True),
            "create_object": _edge("create_object", RESULT_TYPE_CREATE_OBJECT, retryable=False),
            "update_object": _edge("update_object", RESULT_TYPE_UPDATE_OBJECT, retryable=False),
            "list_subscriptions": _edge(
                "list_subscriptions",
                RESULT_TYPE_LIST_SUBSCRIPTIONS,
                retryable=True,
            ),
            "get_invoice": _edge("get_invoice", RESULT_TYPE_GET_INVOICE, retryable=True),
            "list_invoices": _edge("list_invoices", RESULT_TYPE_LIST_INVOICES, retryable=True),
            "bulk_export": _edge("bulk_export", RESULT_TYPE_BULK_EXPORT, retryable=True),
            "test_connection": _edge("test_connection", RESULT_TYPE_TEST_CONNECTION, retryable=True),
            **{name: _edge(name, typed_verbs.result_type(name), retryable=retryable) for name, retryable in typed_verbs.TYPED_RETRYABLE.items()},
        }

    # ------------------------------------------------------------------
    # @platform_process implementations
    # ------------------------------------------------------------------

    @platform_process(
        name="request",
        display_name="Zuora: Request",
        description=(
            "Send one authenticated REST request (GET/POST/PUT/PATCH/DELETE) to a relative "
            "path on the registered Zuora tenant. Vendor RBAC controls all operations. "
            "Returns job_id/status queued immediately; status, redacted headers/body and "
            "pagination arrive on completion. Calls may change vendor state; report the "
            "HTTP status and body faithfully. Never automatically replay a write."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "method": ParameterMetadata(type=ParameterType.STRING, required=True, description="HTTP method, including GET, POST, PUT, PATCH and DELETE."),
            "path": ParameterMetadata(type=ParameterType.STRING, required=True, description="Relative tenant REST path, e.g. /workflows. No absolute URL."),
            "query": ParameterMetadata(type=ParameterType.OBJECT, required=False, description="Query parameters forwarded to Zuora."),
            "headers": ParameterMetadata(type=ParameterType.OBJECT, required=False, description="Non-Authorization HTTP headers without CR/LF; OAuth is injected by the plugin."),
            "body": ParameterMetadata(type=ParameterType.OBJECT, required=False, description="Optional JSON request body, forwarded unchanged."),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Queued job envelope; vendor status/headers/body/pagination arrive on completion.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="queued"),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def request(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        try:
            request_actions.validate_request(params)
        except ValueError as exc:
            return self._error(ERROR_INVALID_PARAMS, str(exc))
        return self._dispatch_async("request", params, state)

    @platform_process(
        name="data_query",
        display_name="Zuora: Data Query",
        description=(
            'Run a ZOQL query (e.g. "SELECT Id, Name FROM Account") against the configured Zuora '
            "tenant. Returns immediately with a job_id and status 'queued' (D0.3 deferred-completion "
            "shape) -- the dispatch returning is NOT the same as the job finishing. When the job "
            "completes, the result is ALWAYS written to the caller-supplied output_tsv_path, never "
            "returned inline. Zuora's own ZOQL query call (POST /v1/action/query) returns at most "
            f"{ZUORA_QUERY_PAGE_ROW_CAP} records per call; this verb follows the vendor's own "
            "queryMore continuation automatically when more remain. The row limit below is "
            f"entirely our own policy. Defaults to {DEFAULT_ROW_LIMIT} records to avoid exhausting "
            "API request limits and disk, and to discourage pulling all records for client-side "
            "filtering that a ZOQL WHERE clause should do instead. To fetch more, pass "
            f"acknowledge_default_limit_override=true together with an explicit row_limit (up to "
            f"{DATA_QUERY_MAX_ROWS_CAP}) — both are required together, and a row_limit above "
            f"{DATA_QUERY_MAX_ROWS_CAP} is refused rather than silently clamped. For pulls beyond "
            f"{DATA_QUERY_MAX_ROWS_CAP} records, use bulk_export instead (same override mechanism, "
            f"hard cap {BULK_EXPORT_ROW_CAP}). When the goal is validating that records exist or "
            "picking one to act on next, prefer selecting stable ID fields over email addresses or "
            "other PII-bearing fields — the ZOQL SELECT list decides what comes back, so a "
            "narrower query is both cheaper and lower-exposure."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "zoql": ParameterMetadata(type=ParameterType.STRING, required=True, description="The ZOQL query string."),
            "output_tsv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description=("ABSOLUTE .tsv destination path, contained under an export_allowed_roots entry."),
            ),
            PARAM_ACKNOWLEDGE_OVERRIDE: ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(
                    f"Must be exactly true, together with row_limit, to fetch more than the default {DEFAULT_ROW_LIMIT} records. Requires understanding why the default exists: avoiding exhausted API request limits/disk, and pulling all records to filter client-side instead of writing a narrower ZOQL WHERE clause."
                ),
            ),
            PARAM_ROW_LIMIT: ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(f"Explicit record ceiling, up to {DATA_QUERY_MAX_ROWS_CAP}. Only honored together with acknowledge_default_limit_override=true; refused (not clamped) above {DATA_QUERY_MAX_ROWS_CAP}."),
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the TSV handle itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def data_query(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("data_query", params, state)

    @platform_process(
        name="get_object",
        display_name="Zuora: Get Object",
        description=(
            "Fetch one Account/Subscription/Invoice/Payment/Product object by type + id. Requires type and id. Returns immediately with a job_id and status 'queued' (D0.3 deferred-completion shape) -- the dispatch returning is NOT the same as the job finishing; the object's fields are delivered when the job completes."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "type": ParameterMetadata(type=ParameterType.STRING, required=True, description="Object type: Account, Subscription, Invoice, Payment, or Product."),
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The object id."),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the object itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def get_object(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("get_object", params, state)

    @platform_process(
        name="create_object",
        display_name="Zuora: Create Object",
        description=(
            "Create an Account/Subscription/Invoice/Payment/Product object with the given fields. "
            "Write action. Returns immediately with a job_id and status 'queued' (D0.3 "
            "deferred-completion shape) -- the dispatch returning is NOT the same as the object "
            "being created; the new object id is delivered when the job completes."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "type": ParameterMetadata(type=ParameterType.STRING, required=True, description="Object type: Account, Subscription, Invoice, Payment, or Product."),
            "fields": ParameterMetadata(type=ParameterType.OBJECT, required=True, description="Non-empty object of field values."),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the created object's id.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def create_object(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("create_object", params, state)

    @platform_process(
        name="update_object",
        display_name="Zuora: Update Object",
        description=("Apply a non-empty fields object to an existing object by type + id. Write action. Returns immediately with a job_id and status 'queued' (D0.3 deferred-completion shape) -- the dispatch returning is NOT the same as the update finishing; the confirmation is delivered when the job completes."),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "type": ParameterMetadata(type=ParameterType.STRING, required=True, description="Object type: Account, Subscription, Invoice, Payment, or Product."),
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The object id."),
            "fields": ParameterMetadata(type=ParameterType.OBJECT, required=True, description="Non-empty object of field values to set."),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not update confirmation.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def update_object(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("update_object", params, state)

    @platform_process(
        name="list_subscriptions",
        display_name="Zuora: List Subscriptions",
        description=(
            "List an account's subscriptions. Returns immediately with a job_id and status "
            "'queued' (D0.3 deferred-completion shape) -- the dispatch returning is NOT the same "
            "as the job finishing. When the job completes, the result is ALWAYS written to the "
            "caller-supplied output_tsv_path, never inline. Requires account_id. Zuora's own endpoint "
            "(GET /v1/subscriptions/accounts/{account_id}) pages internally at "
            f"{ZUORA_LIST_PAGE_SIZE_MAX} subscriptions per call — this verb follows that "
            "pagination automatically up to the effective row limit. The row limit below is "
            f"entirely our own policy. Defaults to {DEFAULT_ROW_LIMIT} to avoid exhausting API "
            "request limits and disk. To fetch more, pass acknowledge_default_limit_override=true "
            f"together with an explicit row_limit (up to {LIST_ROW_LIMIT_CAP}) — both are required "
            f"together, and a row_limit above {LIST_ROW_LIMIT_CAP} is refused rather than silently "
            "clamped. Prefer selecting on stable subscription/account IDs downstream over "
            "billing-contact PII fields when the goal is validation."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "account_id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The Zuora account id."),
            "output_tsv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="ABSOLUTE .tsv destination path, contained under an export_allowed_roots entry.",
            ),
            PARAM_ACKNOWLEDGE_OVERRIDE: ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(f"Must be exactly true, together with row_limit, to fetch more than the default {DEFAULT_ROW_LIMIT} subscriptions."),
            ),
            PARAM_ROW_LIMIT: ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(f"Explicit record ceiling, up to {LIST_ROW_LIMIT_CAP}. Only honored together with acknowledge_default_limit_override=true; refused (not clamped) above {LIST_ROW_LIMIT_CAP}."),
            ),
            **SEGMENT_PARAMS,
            **V1_HEADER_PARAMS,
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the TSV handle itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_subscriptions(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        try:
            subscription_read_options(params)
            v1_headers(params)
        except ValueError as exc:
            return self._error(ERROR_INVALID_PARAMS, str(exc))
        return self._dispatch_async("list_subscriptions", params, state)

    @platform_process(
        name="get_invoice",
        display_name="Zuora: Get Invoice",
        description=(
            "Fetch one invoice by id or number (GET /v1/invoices/{invoiceKey}), optionally with the documented Zuora-Version, Zuora-Entity-Ids, Zuora-Org-Ids and Zuora-Track-Id headers. Returns immediately with a job_id and status 'queued' (D0.3 deferred-completion shape) -- the dispatch returning is NOT the same as the job finishing; the invoice's fields are delivered when the job completes."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The invoice id or invoice number."),
            **V1_HEADER_PARAMS,
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the invoice itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def get_invoice(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        try:
            v1_headers(params)
        except ValueError as exc:
            return self._error(ERROR_INVALID_PARAMS, str(exc))
        return self._dispatch_async("get_invoice", params, state)

    @platform_process(
        name="list_invoices",
        display_name="Zuora: List Invoices",
        description=(
            "List an account's invoices. Returns immediately with a job_id and status 'queued' "
            "(D0.3 deferred-completion shape) -- the dispatch returning is NOT the same as the "
            "job finishing. When the job completes, the result is ALWAYS written to the "
            "caller-supplied output_tsv_path, never inline. Requires account_id. NOTE on this call's ceiling: "
            "Zuora's own pagination support for this specific endpoint "
            "(GET /v1/invoices/accounts/{account_id}) is not independently confirmed in current "
            "vendor documentation, so row_limit is applied as a cap on what is WRITTEN from "
            "Zuora's single-call response rather than a guaranteed pre-fetch ceiling — if an "
            "account has more invoices than Zuora returns in one call, truncated may under-report. "
            f"Defaults to {DEFAULT_ROW_LIMIT} to avoid exhausting disk on an unexpectedly large "
            "account. To raise the cap, pass acknowledge_default_limit_override=true together with "
            f"an explicit row_limit (up to {LIST_ROW_LIMIT_CAP}) — both are required together, and "
            f"a row_limit above {LIST_ROW_LIMIT_CAP} is refused rather than silently clamped. "
            "Prefer selecting on stable invoice/account IDs downstream over billing-contact PII "
            "fields when the goal is validation."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "account_id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The Zuora account id."),
            "output_tsv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="ABSOLUTE .tsv destination path, contained under an export_allowed_roots entry.",
            ),
            PARAM_ACKNOWLEDGE_OVERRIDE: ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(f"Must be exactly true, together with row_limit, to write more than the default {DEFAULT_ROW_LIMIT} invoices."),
            ),
            PARAM_ROW_LIMIT: ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(f"Explicit record ceiling, up to {LIST_ROW_LIMIT_CAP}. Only honored together with acknowledge_default_limit_override=true; refused (not clamped) above {LIST_ROW_LIMIT_CAP}."),
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the TSV handle itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_invoices(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("list_invoices", params, state)

    @platform_process(
        name="bulk_export",
        display_name="Zuora: Bulk Export",
        description=(
            "The N>>500 route: run a ZOQL query and write the result as ONE tab-separated .tsv "
            "file at an ABSOLUTE output_tsv_path in the operator's workspace. Returns immediately "
            "with a job_id and status 'queued' (D0.3 deferred-completion shape) -- the dispatch "
            "returning is NOT the same as the job finishing. The path must lie "
            "under an operator-configured export_allowed_roots entry (empty config refuses every "
            "export). Nested objects are serialized as JSON text in their cells. Same read rules "
            "and override mechanism as data_query, with a higher hard cap: Zuora's own ZOQL query "
            f"call returns at most {ZUORA_QUERY_PAGE_ROW_CAP} records per call, and this verb "
            "follows the vendor's own queryMore continuation automatically to reach the effective "
            f"limit. Defaults to {DEFAULT_ROW_LIMIT} records absent an acknowledged override — for "
            "that common small/default case, data_query has an identical interface with a lower "
            "ceiling. To fetch more, pass acknowledge_default_limit_override=true together with an "
            f"explicit row_limit (up to {BULK_EXPORT_ROW_CAP}) — both are required together, and a "
            f"row_limit above {BULK_EXPORT_ROW_CAP} is refused rather than silently clamped. "
            "Requires zoql and output_tsv_path. When the goal is validating that records exist "
            "rather than inspecting their content, prefer selecting stable ID fields over email "
            "addresses or other PII-bearing fields — the ZOQL SELECT list decides what lands in "
            "the file."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "zoql": ParameterMetadata(type=ParameterType.STRING, required=True, description="The ZOQL query string."),
            "output_tsv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="ABSOLUTE .tsv destination path, contained under an export_allowed_roots entry.",
            ),
            PARAM_ACKNOWLEDGE_OVERRIDE: ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(f"Must be exactly true, together with row_limit, to fetch more than the default {DEFAULT_ROW_LIMIT} records."),
            ),
            PARAM_ROW_LIMIT: ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(f"Explicit record ceiling, up to {BULK_EXPORT_ROW_CAP}. Only honored together with acknowledge_default_limit_override=true; refused (not clamped) above {BULK_EXPORT_ROW_CAP}."),
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the TSV handle itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def bulk_export(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("bulk_export", params, state)

    @platform_process(
        name="test_connection",
        display_name="Zuora: Test Connection",
        description=("Verify the configured Zuora credentials by minting a bearer token. Returns immediately with a job_id and status 'queued' (D0.3 deferred-completion shape) -- the dispatch returning is NOT the same as the job finishing; ok/base_url/client_id are delivered when the job completes."),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the connection check result.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def test_connection(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("test_connection", params, state)


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
