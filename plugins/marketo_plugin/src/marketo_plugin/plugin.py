"""Marketo plugin entry point — lead CRUD/query, campaign trigger, list membership.

Headless OAuth 2.0 client-credentials auth (chain-consumed from the
``marketo_instance`` address-book entry) against a Marketo Engage instance —
no browser flow, no callback server, the same auth-simplicity class as
zuora_plugin. Unlike Zuora, this connector DOES expose destructive verbs
(``delete_leads``, ``merge_leads``) — Marketo's REST API supports lead
deletion and merging natively and lead records are not treated as immutable
audit trail the way Zuora's billing records are; see the knowledge-base
overview for the explicit read/write posture note.

Verbs (all EDGE):
  - describe_lead_fields                                    — read
  - get_leads                                                — read
  - get_api_usage                                            — read (current-day API consumption)
  - list_activity_types                                      — read (per-instance activity metadata)
  - get_activities                                           — read (activity log; verifies what a write CAUSED, after the fact)
  - create_or_update_leads                                   — write
  - delete_leads                                             — write (destructive)
  - merge_leads                                              — write (destructive, irreversible)
  - list_campaigns                                           — read
  - trigger_campaign                                         — write (side-effecting; the flow it runs is NOT readable first — see the KB's campaign flow inspection article)
  - list_static_lists                                        — read
  - add_leads_to_list / remove_leads_from_list               — write
  - test_connection                                          — diagnostic (credentials reachable)
  - check_setup                                              — diagnostic (which READ capabilities the Role grants; PARTIAL, see docstring)

Security posture: every verb is directly process_call-able like any other
process (matches the 2026-07-15 operator ruling retiring the RATIFY-3
process_export deny); auth/rate-limit error messages are GENERIC fixed
strings — never the raw response, which could leak instance-specific
diagnostic detail; the plugin reaches ONLY the address-book-resolved
instance (no base_url param on any verb). No SQL-shaped strings anywhere
(pure REST), so the SQL-lockdown gate is silent for this plugin — no
allowlist entry needed.
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

from . import export_containment, marketing_actions
from .app_config import AppConfigError, AppConfigLoader
from .constants import (
    CONFIG_KEY_EXPORT_ALLOWED_ROOTS,
    CONFIG_KEY_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_ROW_LIMIT,
    ERROR_ADDRESS_BOOK_NOT_AVAILABLE,
    ERROR_API_ERROR,
    ERROR_INVALID_PARAMS,
    ERROR_NOT_CONFIGURED,
    MARKETO_LIST_PAGE_ROW_CAP,
    MARKETO_LIST_ROW_LIMIT_CAP,
    PARAM_ACKNOWLEDGE_OVERRIDE,
    PARAM_ROW_LIMIT,
    PLUGIN_NAME,
    RESULT_TYPE_ADD_LEADS_TO_LIST,
    RESULT_TYPE_CHECK_SETUP,
    RESULT_TYPE_CREATE_OR_UPDATE_LEADS,
    RESULT_TYPE_DELETE_LEADS,
    RESULT_TYPE_DESCRIBE_LEAD_FIELDS,
    RESULT_TYPE_GET_ACTIVITIES,
    RESULT_TYPE_GET_API_USAGE,
    RESULT_TYPE_GET_LEADS,
    RESULT_TYPE_LIST_ACTIVITY_TYPES,
    RESULT_TYPE_LIST_CAMPAIGNS,
    RESULT_TYPE_LIST_STATIC_LISTS,
    RESULT_TYPE_MERGE_LEADS,
    RESULT_TYPE_REMOVE_LEADS_FROM_LIST,
    RESULT_TYPE_TEST_CONNECTION,
    RESULT_TYPE_TRIGGER_CAMPAIGN,
)
from .errors import (
    MarketoEnvelopeError,
    MarketoServiceError,
    MarketoTransportError,
    classify_marketo_envelope,
)
from .http_client import MarketoAuthError, MarketoClient


class MarketoPlugin(PluginBase, EdgeProcessProvider):
    """Marketo Engage connector (lead CRUD/query, campaigns, list membership) plugin."""

    name: str = PLUGIN_NAME

    def __init__(self) -> None:
        super().__init__()
        self.logger: logging.Logger | None = None
        self._address_book_service: Any | None = None
        self._app_config_loader: AppConfigLoader | None = None
        self._client: MarketoClient | None = None

    # ------------------------------------------------------------------
    # VaultKeysProvider — no plugin-owned vault keys
    # ------------------------------------------------------------------

    def get_required_vault_keys(self) -> list[str]:
        """No vault keys are required at readiness.

        The client_secret is chain-consumed through the ``marketo_instance``
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
                "address_book_service to resolve the marketo_instance credentials"
            )
        self._app_config_loader = AppConfigLoader(self._address_book_service)
        self.set_ready()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _request_timeout_seconds(self) -> float:
        config = self.config_provider or {}
        raw = config.get(CONFIG_KEY_REQUEST_TIMEOUT_SECONDS, DEFAULT_REQUEST_TIMEOUT_SECONDS)
        return float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else DEFAULT_REQUEST_TIMEOUT_SECONDS

    def _require_client(self) -> MarketoClient:
        """Lazily build + cache the Marketo client from the resolved instance config."""
        if self._app_config_loader is None:
            raise RuntimeError(ERROR_ADDRESS_BOOK_NOT_AVAILABLE)
        if self._client is None:
            config = self._app_config_loader.load()
            self._client = MarketoClient(config, timeout_seconds=self._request_timeout_seconds())
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

    def _export_path_gate(self, output_tsv_path: str) -> str:
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
            if not isinstance(raw_roots, list) or not all(
                isinstance(entry, str) for entry in raw_roots
            ):
                raise MarketoServiceError(
                    ERROR_NOT_CONFIGURED,
                    f"{CONFIG_KEY_EXPORT_ALLOWED_ROOTS} must be a list of directory "
                    "path strings",
                )
            roots = list(raw_roots)
        return export_containment.assert_export_path_allowed(
            output_tsv_path,
            roots,
            config_key=CONFIG_KEY_EXPORT_ALLOWED_ROOTS,
            plugin_name=self.name,
        )

    def _run(self, produce: Callable[[MarketoClient], dict[str, Any]], endpoint_name: str) -> dict[str, Any]:
        """Shared error-classification path for every Marketo verb."""
        try:
            client = self._require_client()
            data = produce(client)
        except ValueError as exc:
            return self._error(ERROR_INVALID_PARAMS, str(exc))
        except AppConfigError as exc:
            return self._error(ERROR_NOT_CONFIGURED, str(exc))
        except (MarketoServiceError, export_containment.ExportPathRefusedError) as exc:
            return self._error(exc.code, str(exc))
        except MarketoAuthError:
            return self._error("marketo.auth_failed", "Marketo OAuth token request failed.")
        except MarketoEnvelopeError as exc:
            code, message = classify_marketo_envelope(exc)
            return self._error(code, message)
        except MarketoTransportError:
            return self._error(ERROR_API_ERROR, "Marketo API call failed (transport fault).")
        except Exception as exc:  # noqa: BLE001 — any other transport fault -> generic
            if self.logger:
                self.logger.warning("%s: unexpected fault (%s)", endpoint_name, type(exc).__name__)
            return self._error(ERROR_API_ERROR, "Marketo API call failed.")
        if self.logger:
            self.logger.debug("%s: success", endpoint_name)
        return self._success(data)

    # ------------------------------------------------------------------
    # EdgeProcessProvider
    # ------------------------------------------------------------------

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        return {
            "describe_lead_fields": _edge("describe_lead_fields", RESULT_TYPE_DESCRIBE_LEAD_FIELDS, retryable=True),
            "get_leads": _edge("get_leads", RESULT_TYPE_GET_LEADS, retryable=True),
            "get_api_usage": _edge("get_api_usage", RESULT_TYPE_GET_API_USAGE, retryable=True),
            "list_activity_types": _edge("list_activity_types", RESULT_TYPE_LIST_ACTIVITY_TYPES, retryable=True),
            "get_activities": _edge("get_activities", RESULT_TYPE_GET_ACTIVITIES, retryable=True),
            "create_or_update_leads": _edge("create_or_update_leads", RESULT_TYPE_CREATE_OR_UPDATE_LEADS, retryable=False),
            "delete_leads": _edge("delete_leads", RESULT_TYPE_DELETE_LEADS, retryable=False),
            "merge_leads": _edge("merge_leads", RESULT_TYPE_MERGE_LEADS, retryable=False),
            "list_campaigns": _edge("list_campaigns", RESULT_TYPE_LIST_CAMPAIGNS, retryable=True),
            "trigger_campaign": _edge("trigger_campaign", RESULT_TYPE_TRIGGER_CAMPAIGN, retryable=False),
            "list_static_lists": _edge("list_static_lists", RESULT_TYPE_LIST_STATIC_LISTS, retryable=True),
            "add_leads_to_list": _edge("add_leads_to_list", RESULT_TYPE_ADD_LEADS_TO_LIST, retryable=False),
            "remove_leads_from_list": _edge("remove_leads_from_list", RESULT_TYPE_REMOVE_LEADS_FROM_LIST, retryable=False),
            "test_connection": _edge("test_connection", RESULT_TYPE_TEST_CONNECTION, retryable=True),
            "check_setup": _edge("check_setup", RESULT_TYPE_CHECK_SETUP, retryable=True),
        }

    # ------------------------------------------------------------------
    # @platform_process implementations
    # ------------------------------------------------------------------

    @platform_process(
        name="describe_lead_fields",
        display_name="Marketo: Describe Lead Fields",
        description=(
            "Fetch the full lead field metadata list and the instance-specific searchable_fields "
            "accepted by get_leads.filter_type, when the instance's describe response carries that "
            "field. ALWAYS writes the field descriptors to the caller-supplied output_tsv_path, "
            "never inline — this is a business-connector record-read verb under the 07-29 spill "
            "floor even though its content is schema metadata, not customer PII."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "output_tsv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="ABSOLUTE .tsv destination path, contained under an export_allowed_roots entry.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="A handle to the written TSV, plus searchable_fields.",
            properties={
                "path": ParameterMetadata(type=ParameterType.STRING, description="Absolute path of the written TSV file."),
                "row_count": ParameterMetadata(type=ParameterType.INTEGER, description="Number of field descriptors written."),
                "columns": ParameterMetadata(type=ParameterType.LIST, description="Field-descriptor column names, in first-appearance order."),
                "searchable_fields": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Field names valid for get_leads.filter_type; null on an instance whose describe response omits it.",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def describe_lead_fields(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            lambda client: marketing_actions.describe_lead_fields(client, params, self._export_path_gate),
            "describe_lead_fields",
        )

    @platform_process(
        name="get_api_usage",
        display_name="Marketo: Get Current API Usage",
        description=(
            "Read the configured Marketo subscription's current-day REST API "
            "call total and per-user breakdown. Use calls_today when checking "
            "whether a planned batch fits the operator's known daily quota."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description=(
                "date, calls_today, users, records, and row_count for the "
                "current subscription day."
            ),
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def get_api_usage(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        return self._run(
            lambda client: marketing_actions.get_api_usage(client, params),
            "get_api_usage",
        )

    @platform_process(
        name="get_leads",
        display_name="Marketo: Get Leads",
        description=(
            "Query leads by an instance-supported filter_type and up to 300 filter_values. Read "
            "describe_lead_fields.searchable_fields to discover valid standard and custom filter "
            "types first. ALWAYS writes the matching leads to output_tsv_path, never inline. Pages "
            f"internally across Marketo's own {MARKETO_LIST_PAGE_ROW_CAP}-per-call ceiling (fixed "
            "server-side, no query-side parameter to raise) up to the effective row limit — this "
            f"verb never returns a partial page for you to continue; defaults to {DEFAULT_ROW_LIMIT} "
            "to avoid exhausting vendor rate limits and disk, and to discourage pulling all records "
            "for client-side filtering a vendor query should do instead. To fetch more, pass "
            f"acknowledge_default_limit_override=true together with an explicit row_limit (up to "
            f"{MARKETO_LIST_ROW_LIMIT_CAP}) — both required together, refused rather than clamped "
            f"above {MARKETO_LIST_ROW_LIMIT_CAP}. Beyond the hard cap there is no resumption: "
            "re-invoke with a narrower filter_values slice (e.g. a 45,000-lead job needs several "
            f"non-overlapping calls, ~{MARKETO_LIST_ROW_LIMIT_CAP // MARKETO_LIST_PAGE_ROW_CAP} "
            "internal vendor calls each) — check get_api_usage first if the daily quota is a "
            "concern for a large pull. Optional fields restricts returned columns — prefer "
            "requesting stable ID and status fields over email, phone, or address fields when the "
            "goal is validation rather than a task that genuinely needs them."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "filter_type": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description=(
                    "A field from describe_lead_fields.searchable_fields for "
                    "this Marketo instance."
                ),
            ),
            "filter_values": ParameterMetadata(type=ParameterType.LIST, required=True, description="Up to 300 filter values to match."),
            "fields": ParameterMetadata(type=ParameterType.LIST, required=False, description="Optional list of lead field API names to return."),
            "output_tsv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="ABSOLUTE .tsv destination path, contained under an export_allowed_roots entry.",
            ),
            PARAM_ACKNOWLEDGE_OVERRIDE: ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(
                    f"Must be exactly true, together with row_limit, to fetch more than the "
                    f"default {DEFAULT_ROW_LIMIT} leads."
                ),
            ),
            PARAM_ROW_LIMIT: ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(
                    f"Explicit record ceiling, up to {MARKETO_LIST_ROW_LIMIT_CAP}. Only honored "
                    f"together with acknowledge_default_limit_override=true; refused (not clamped) "
                    f"above {MARKETO_LIST_ROW_LIMIT_CAP}."
                ),
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="A handle to the written TSV — never records inline, at any size.",
            properties={
                "path": ParameterMetadata(type=ParameterType.STRING, description="Absolute path of the written TSV file."),
                "row_count": ParameterMetadata(type=ParameterType.INTEGER, description="Number of leads written."),
                "columns": ParameterMetadata(type=ParameterType.LIST, description="Lead field names, in first-appearance order."),
                "truncated": ParameterMetadata(
                    type=ParameterType.BOOLEAN, description="True when row_count hit the effective limit — more leads may exist.",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def get_leads(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            lambda client: marketing_actions.get_leads(client, params, self._export_path_gate), "get_leads",
        )

    @platform_process(
        name="list_activity_types",
        display_name="Marketo: List Activity Types",
        description=(
            "List the configured Marketo instance's activity type ids and metadata. Use these "
            "per-instance ids as the mandatory activity_type_ids for get_activities. ALWAYS writes "
            "the catalog to output_tsv_path, never inline — this is a business-connector "
            "record-read verb under the 07-29 spill floor even though its content is instance "
            "metadata, not customer PII."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "output_tsv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="ABSOLUTE .tsv destination path, contained under an export_allowed_roots entry.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="A handle to the written TSV.",
            properties={
                "path": ParameterMetadata(type=ParameterType.STRING, description="Absolute path of the written TSV file."),
                "row_count": ParameterMetadata(type=ParameterType.INTEGER, description="Number of activity type descriptors written."),
                "columns": ParameterMetadata(type=ParameterType.LIST, description="Descriptor column names, in first-appearance order."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_activity_types(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        return self._run(
            lambda client: marketing_actions.list_activity_types(
                client,
                params,
                self._export_path_gate,
            ),
            "list_activity_types",
        )

    @platform_process(
        name="get_activities",
        display_name="Marketo: Get Lead Activities",
        description=(
            "Read the Marketo activity log — what leads actually DID, or had done to them "
            "(emails sent/delivered, alerts, campaign requests, data value changes). Pass "
            "since_datetime (ISO-8601, second-granularity — a fractional-seconds component is "
            "floor-truncated before the paging-token request because Marketo otherwise rewinds "
            "that window to midnight UTC). activity_type_ids is mandatory (max 10); discover "
            "valid ids with list_activity_types. Optional lead_ids (max 30) filter server-side. "
            "AFTER-THE-FACT audit: it reports what a write already caused; it cannot promise "
            "that a future merge/update will stay silent. ALWAYS writes to output_tsv_path, "
            f"never inline. Pages internally across Marketo's own {MARKETO_LIST_PAGE_ROW_CAP}-"
            "per-call ceiling up to the effective row limit — no pagination token appears on "
            f"this verb; defaults to {DEFAULT_ROW_LIMIT}. To fetch more, pass "
            "acknowledge_default_limit_override=true together with an explicit row_limit (up to "
            f"{MARKETO_LIST_ROW_LIMIT_CAP}) — both required together, refused rather than clamped "
            f"above {MARKETO_LIST_ROW_LIMIT_CAP}. Beyond the hard cap: no resumption, re-invoke "
            "with a later since_datetime. moreResult is the ONLY usable vendor continuation "
            "signal internally (Adobe documents this endpoint always returns a token, so token "
            "presence can't terminate the loop, unlike get_leads/list_campaigns/list_static_lists) "
            "and its reliability on this endpoint is documented but UNMEASURED — the one live "
            "measurement of moreResult anywhere found it violated on list_campaigns, so "
            "truncated=false here is only as honest as Marketo's own flag. Prefer requesting "
            "stable ID and status fields for validation over email or other PII-bearing fields."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "since_datetime": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description=(
                    "Second-granularity ISO-8601 instant to read activities from "
                    "(e.g. 2026-07-28T00:00:00-07:00). A fractional-seconds "
                    "component is floor-truncated before the paging-token request; "
                    "all other bytes are preserved. To read further than the "
                    "effective row limit reaches, re-invoke with a later instant."
                ),
            ),
            "lead_ids": ParameterMetadata(
                type=ParameterType.LIST,
                required=False,
                description="Up to 30 lead ids to restrict the read to (Marketo's own server-side cap).",
            ),
            "activity_type_ids": ParameterMetadata(
                type=ParameterType.LIST,
                required=True,
                description="One to 10 ids from list_activity_types for this Marketo instance.",
            ),
            "output_tsv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="ABSOLUTE .tsv destination path, contained under an export_allowed_roots entry.",
            ),
            PARAM_ACKNOWLEDGE_OVERRIDE: ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(
                    f"Must be exactly true, together with row_limit, to fetch more than the "
                    f"default {DEFAULT_ROW_LIMIT} activity items."
                ),
            ),
            PARAM_ROW_LIMIT: ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(
                    f"Explicit record ceiling, up to {MARKETO_LIST_ROW_LIMIT_CAP}. Only honored "
                    f"together with acknowledge_default_limit_override=true; refused (not clamped) "
                    f"above {MARKETO_LIST_ROW_LIMIT_CAP}."
                ),
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="A handle to the written TSV — never records inline, at any size.",
            properties={
                "path": ParameterMetadata(type=ParameterType.STRING, description="Absolute path of the written TSV file."),
                "row_count": ParameterMetadata(type=ParameterType.INTEGER, description="Number of activity items written."),
                "columns": ParameterMetadata(type=ParameterType.LIST, description="Activity-item field names, in first-appearance order."),
                "truncated": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description=(
                        "True when row_count hit the effective limit or the vendor's own "
                        "moreResult flag never went false — more activity items may exist."
                    ),
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def get_activities(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            lambda client: marketing_actions.get_activities(client, params, self._export_path_gate), "get_activities",
        )

    @platform_process(
        name="create_or_update_leads",
        display_name="Marketo: Create or Update Leads",
        description=(
            "Create and/or update up to 300 lead records in one batch. action is one of "
            "createOrUpdate (default), createOnly, updateOnly, createDuplicate; lookup_field "
            "names the dedupe field (defaults to email). Before writing, one lead-describe "
            "call validates the batch and refuses it whole if any non-key field is REST "
            "read-only. Write action."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "records": ParameterMetadata(type=ParameterType.LIST, required=True, description="Up to 300 objects of lead field values."),
            "action": ParameterMetadata(type=ParameterType.STRING, required=False, description="createOrUpdate (default), createOnly, updateOnly, or createDuplicate."),
            "lookup_field": ParameterMetadata(type=ParameterType.STRING, required=False, description="Dedupe field name (e.g. 'email' or a custom id field)."),
        },
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="results (per-record id/status/reasons), row_count, tallies."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def create_or_update_leads(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda client: marketing_actions.create_or_update_leads(client, params), "create_or_update_leads")

    @platform_process(
        name="delete_leads",
        display_name="Marketo: Delete Leads",
        description="Delete up to 300 leads by id. Destructive write action — Marketo permanently removes the records.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "lead_ids": ParameterMetadata(type=ParameterType.LIST, required=True, description="Up to 300 lead ids to delete."),
        },
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="results (per-record id/status/reasons), row_count, tallies."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def delete_leads(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda client: marketing_actions.delete_leads(client, params), "delete_leads")

    @platform_process(
        name="merge_leads",
        display_name="Marketo: Merge Leads",
        description=(
            "Merge up to 25 losing leads into one winning lead. Read-only fields retain "
            "the winner's value even when empty rather than being filled from a loser under "
            "the general precedence rule. merge_in_crm additionally merges the natively-synced "
            "CRM records — Marketo itself restricts a CRM merge to exactly ONE losing lead per "
            "call, not 25. Destructive write action: losing leads' identities are absorbed into "
            "the winner and cannot be split back apart."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "winning_lead_id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The lead id whose field values win the merge."),
            "losing_lead_ids": ParameterMetadata(type=ParameterType.LIST, required=True, description="Up to 25 lead ids to merge into the winner (only 1 if merge_in_crm is true)."),
            "merge_in_crm": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="Also merge the natively-synced CRM records (caps losing_lead_ids at 1)."),
        },
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="success, request_id."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def merge_leads(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda client: marketing_actions.merge_leads(client, params), "merge_leads")

    @platform_process(
        name="list_campaigns",
        display_name="Marketo: List Campaigns",
        description=(
            "List campaigns, optionally filtered by names and/or program_names. ALWAYS writes to "
            f"output_tsv_path, never inline. Pages internally across Marketo's own "
            f"{MARKETO_LIST_PAGE_ROW_CAP}-per-call ceiling up to the effective row limit — no "
            f"pagination token appears on this verb; defaults to {DEFAULT_ROW_LIMIT} to avoid "
            "exhausting vendor rate limits and disk. To fetch more, pass "
            "acknowledge_default_limit_override=true together with an explicit row_limit (up to "
            f"{MARKETO_LIST_ROW_LIMIT_CAP}) — both required together, refused rather than clamped "
            f"above {MARKETO_LIST_ROW_LIMIT_CAP}. Beyond the hard cap: no resumption, re-invoke "
            "with a narrower names/program_names filter. Prefer selecting on stable campaign IDs "
            "downstream over other fields when the goal is validation."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "names": ParameterMetadata(type=ParameterType.LIST, required=False, description="Optional list of exact campaign names to filter by."),
            "program_names": ParameterMetadata(type=ParameterType.LIST, required=False, description="Optional list of exact program names to filter by."),
            "output_tsv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="ABSOLUTE .tsv destination path, contained under an export_allowed_roots entry.",
            ),
            PARAM_ACKNOWLEDGE_OVERRIDE: ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(
                    f"Must be exactly true, together with row_limit, to fetch more than the "
                    f"default {DEFAULT_ROW_LIMIT} campaigns."
                ),
            ),
            PARAM_ROW_LIMIT: ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(
                    f"Explicit record ceiling, up to {MARKETO_LIST_ROW_LIMIT_CAP}. Only honored "
                    f"together with acknowledge_default_limit_override=true; refused (not clamped) "
                    f"above {MARKETO_LIST_ROW_LIMIT_CAP}."
                ),
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="A handle to the written TSV — never records inline, at any size.",
            properties={
                "path": ParameterMetadata(type=ParameterType.STRING, description="Absolute path of the written TSV file."),
                "row_count": ParameterMetadata(type=ParameterType.INTEGER, description="Number of campaigns written."),
                "columns": ParameterMetadata(type=ParameterType.LIST, description="Campaign field names, in first-appearance order."),
                "truncated": ParameterMetadata(
                    type=ParameterType.BOOLEAN, description="True when row_count hit the effective limit — more campaigns may exist.",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_campaigns(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            lambda client: marketing_actions.list_campaigns(client, params, self._export_path_gate), "list_campaigns",
        )

    @platform_process(
        name="trigger_campaign",
        display_name="Marketo: Trigger Campaign",
        description=(
            "Trigger (Request Campaign) a campaign for up to 100 leads, with optional campaign "
            "tokens. Destructive-class write action: the campaign's flow runs against real people, "
            "is irreversible, and is visible outside this system (it may send email, alert sales, "
            "change scoring, or move program status). A Smart Campaign has two halves: the Smart "
            "List (who/when it fires) IS inspectable via Marketo's Smart Lists Asset API with "
            "includeRules=true; the Flow (what it actually does — Send Email, Change Data Value, "
            "Wait, etc) is NOT — no REST endpoint dereferences a campaign's flowId into its steps, "
            "and there is no dry-run. So a caller can see who/when but never what happens before "
            "triggering — trigger only campaigns you authored."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "campaign_id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The Marketo campaign id."),
            "lead_ids": ParameterMetadata(type=ParameterType.LIST, required=True, description="Up to 100 lead ids to run through the campaign."),
            "tokens": ParameterMetadata(type=ParameterType.LIST, required=False, description="Optional list of {name, value} campaign token overrides."),
        },
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="success, request_id."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def trigger_campaign(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda client: marketing_actions.trigger_campaign(client, params), "trigger_campaign")

    @platform_process(
        name="list_static_lists",
        display_name="Marketo: List Static Lists",
        description=(
            "List static lists, optionally filtered by names. Same page-cap shape as "
            "list_campaigns. ALWAYS writes to output_tsv_path, never inline. Pages internally "
            f"across Marketo's own {MARKETO_LIST_PAGE_ROW_CAP}-per-call ceiling up to the "
            f"effective row limit — no pagination token appears on this verb; defaults to "
            f"{DEFAULT_ROW_LIMIT} to avoid exhausting vendor rate limits and disk. To fetch more, "
            "pass acknowledge_default_limit_override=true together with an explicit row_limit "
            f"(up to {MARKETO_LIST_ROW_LIMIT_CAP}) — both required together, refused rather than "
            f"clamped above {MARKETO_LIST_ROW_LIMIT_CAP}. Beyond the hard cap: no resumption, "
            "re-invoke with a narrower names filter. Prefer selecting on stable list IDs "
            "downstream over other fields when the goal is validation."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "names": ParameterMetadata(type=ParameterType.LIST, required=False, description="Optional list of exact static list names to filter by."),
            "output_tsv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="ABSOLUTE .tsv destination path, contained under an export_allowed_roots entry.",
            ),
            PARAM_ACKNOWLEDGE_OVERRIDE: ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(
                    f"Must be exactly true, together with row_limit, to fetch more than the "
                    f"default {DEFAULT_ROW_LIMIT} static lists."
                ),
            ),
            PARAM_ROW_LIMIT: ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(
                    f"Explicit record ceiling, up to {MARKETO_LIST_ROW_LIMIT_CAP}. Only honored "
                    f"together with acknowledge_default_limit_override=true; refused (not clamped) "
                    f"above {MARKETO_LIST_ROW_LIMIT_CAP}."
                ),
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="A handle to the written TSV — never records inline, at any size.",
            properties={
                "path": ParameterMetadata(type=ParameterType.STRING, description="Absolute path of the written TSV file."),
                "row_count": ParameterMetadata(type=ParameterType.INTEGER, description="Number of static lists written."),
                "columns": ParameterMetadata(type=ParameterType.LIST, description="Static-list field names, in first-appearance order."),
                "truncated": ParameterMetadata(
                    type=ParameterType.BOOLEAN, description="True when row_count hit the effective limit — more static lists may exist.",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_static_lists(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            lambda client: marketing_actions.list_static_lists(client, params, self._export_path_gate),
            "list_static_lists",
        )

    @platform_process(
        name="add_leads_to_list",
        display_name="Marketo: Add Leads to List",
        description="Add up to 300 leads to a static list by id. Write action.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "list_id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The Marketo static list id."),
            "lead_ids": ParameterMetadata(type=ParameterType.LIST, required=True, description="Up to 300 lead ids to add."),
        },
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="results (per-record id/status), row_count, tallies."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def add_leads_to_list(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda client: marketing_actions.add_leads_to_list(client, params), "add_leads_to_list")

    @platform_process(
        name="remove_leads_from_list",
        display_name="Marketo: Remove Leads from List",
        description="Remove up to 300 leads from a static list by id. Write action.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "list_id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The Marketo static list id."),
            "lead_ids": ParameterMetadata(type=ParameterType.LIST, required=True, description="Up to 300 lead ids to remove."),
        },
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="results (per-record id/status), row_count, tallies."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def remove_leads_from_list(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda client: marketing_actions.remove_leads_from_list(client, params), "remove_leads_from_list")

    @platform_process(
        name="test_connection",
        display_name="Marketo: Test Connection",
        description="Verify the configured Marketo credentials by minting a bearer token. Returns ok, base_url, client_id.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="ok, base_url, client_id."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def test_connection(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        loader = self._app_config_loader

        def _do(client: MarketoClient) -> dict[str, Any]:
            config = loader.load() if loader is not None else None
            client.ensure_authenticated()
            return {
                "ok": True,
                "base_url": config.base_url if config is not None else "",
                "client_id": config.client_id if config is not None else "",
            }

        return self._run(_do, "test_connection")

    @platform_process(
        name="check_setup",
        display_name="Marketo: Check Setup",
        description=(
            "Probe the configured API user's READ-ONLY capabilities (lead schema/query, activity "
            "type listing, API usage, campaign listing, static list listing) and report which Access API Role "
            "permission is missing for any that fail. PARTIAL by design: write/execute verbs "
            "(create_or_update_leads, delete_leads, add/remove_leads_from_list, trigger_campaign) "
            "cannot be probed without performing them, so reads_verified=true does NOT mean the "
            "whole setup is confirmed ready — see writes_unverified."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description=(
                "reads_verified (bool, READ capabilities only), checks (per-probe status + "
                "guidance), writes_unverified (verb names whose permissions can't be probed "
                "safely), writes_unverified_note."
            ),
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def check_setup(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            lambda client: marketing_actions.check_setup(client), "check_setup"
        )


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
