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

from . import marketing_actions
from .app_config import AppConfigError, AppConfigLoader
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
        self._blob_storage_service: Any | None = None
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
        # blob_storage is needed only for the four spilling reads and is
        # resolved lazily at first use (_blob_service): the platform constructs
        # blob_storage_service in the init_service_manager startup step, AFTER
        # every plugin's prepare_for_readiness — resolving it here caches None
        # forever and every spill hard-fails (field-verified on a live
        # deployment).
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

    def _blob_service(self) -> Any | None:
        """Resolve blob_storage_service lazily at point of use (cached once found).

        Readiness-time resolution is a known trap: the platform constructs
        blob_storage_service after every plugin's prepare_for_readiness, so a
        readiness-time get_service() returns None and the miss would be cached
        for the life of the plugin.
        """
        if self._blob_storage_service is None and self.orchestrator_ref is not None:
            self._blob_storage_service = self.orchestrator_ref.get_service("blob_storage_service")
        return self._blob_storage_service

    def _store_blob(self, content: bytes, filename: str, mime_type: str) -> str:
        """Store a spilled result as a blob; return the blob id (the *_blob_key)."""
        blob_service = self._blob_service()
        if blob_service is None:
            raise MarketoServiceError(
                ERROR_BLOB_STORAGE_NOT_AVAILABLE,
                f"blob_storage_service is not available for result spill: the serialized "
                f"result is {len(content)} bytes and the inline-return cap is "
                f"{INLINE_BYTE_CAP} bytes; request fewer ids/fields (or a smaller page) "
                f"so the result fits inline",
            )
        result = blob_service.store_blob(
            BLOB_NAMESPACE, content, {"filename": filename, "mime_type": mime_type}
        )
        if not isinstance(result, dict) or result.get("action_status") != "completed":
            raise MarketoServiceError(
                ERROR_BLOB_STORAGE_NOT_AVAILABLE,
                f"failed to store result blob for '{filename}' ({len(content)} bytes)",
            )
        blob_id = (result.get("data") or {}).get("blob_id")
        if not isinstance(blob_id, str) or not blob_id:
            raise MarketoServiceError(
                ERROR_BLOB_STORAGE_NOT_AVAILABLE,
                f"blob storage returned no blob_id for '{filename}' ({len(content)} bytes)",
            )
        return blob_id

    def _run(self, produce: Callable[[MarketoClient], dict[str, Any]], endpoint_name: str) -> dict[str, Any]:
        """Shared error-classification path for every Marketo verb."""
        try:
            client = self._require_client()
            data = produce(client)
        except ValueError as exc:
            return self._error(ERROR_INVALID_PARAMS, str(exc))
        except AppConfigError as exc:
            return self._error(ERROR_NOT_CONFIGURED, str(exc))
        except MarketoServiceError as exc:
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
            "Fetch the full lead field metadata list and the instance-specific "
            "searchable_fields accepted by get_leads.filter_type."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description=(
                "records (field descriptors) inline or result_blob_key on "
                "spill, plus row_count and searchable_fields."
            ),
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def describe_lead_fields(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda client: marketing_actions.describe_lead_fields(client, params, self._store_blob), "describe_lead_fields")

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
            "Query leads by an instance-supported filter_type and up to 300 "
            "filter_values. Read describe_lead_fields.searchable_fields to "
            "discover valid standard and custom filter types first. Optional "
            "fields restrict returned columns; next_page_token continues a page."
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
            "next_page_token": ParameterMetadata(type=ParameterType.STRING, required=False, description="Continue a prior get_leads page."),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT, description="records inline or result_blob_key on spill, plus row_count, next_page_token, more_result."
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def get_leads(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda client: marketing_actions.get_leads(client, params, self._store_blob), "get_leads")

    @platform_process(
        name="list_activity_types",
        display_name="Marketo: List Activity Types",
        description=(
            "List the configured Marketo instance's activity type ids and "
            "metadata. Use these per-instance ids as the mandatory "
            "activity_type_ids for get_activities."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description=(
                "records (activity type descriptors) inline or result_blob_key "
                "on spill, plus row_count."
            ),
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
                self._store_blob,
            ),
            "list_activity_types",
        )

    @platform_process(
        name="get_activities",
        display_name="Marketo: Get Lead Activities",
        description=(
            "Read the Marketo activity log — what leads actually DID, or had done to them "
            "(emails sent/delivered, alerts, campaign requests, data value changes). Pass "
            "since_datetime (ISO-8601) to start a new read, or next_page_token to continue. "
            "The since_datetime boundary is second-granularity: a fractional-seconds component "
            "is floor-truncated before the paging-token request because Marketo otherwise "
            "rewinds that window to midnight UTC; every other byte is preserved. "
            "activity_type_ids is mandatory on every page (max 10); discover valid ids with "
            "list_activity_types. Optional lead_ids (max 30) filter server-side. "
            "AFTER-THE-FACT audit: it reports what a write already caused; it cannot promise "
            "that a future merge/update will stay silent. PAGING: more_result is the only usable "
            "continuation signal here — Adobe documents that this endpoint always returns a "
            "token, so token presence cannot terminate the loop (the inverse of get_leads/"
            "list_campaigns/list_static_lists). Page until more_result is false; a page with "
            "fewer than 300 items does not mean the end. The flag's reliability on this endpoint "
            "is documented but UNMEASURED — the one live measurement of moreResult anywhere found "
            "it violated on list_campaigns, and here there is no fallback."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "since_datetime": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description=(
                    "Second-granularity ISO-8601 instant to read activities from "
                    "(e.g. 2026-07-28T00:00:00-07:00). A fractional-seconds "
                    "component is floor-truncated before the paging-token request; "
                    "all other bytes are preserved. Required unless next_page_token "
                    "is given."
                ),
            ),
            "next_page_token": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="Continue a prior get_activities page. Required unless since_datetime is given.",
            ),
            "lead_ids": ParameterMetadata(
                type=ParameterType.LIST,
                required=False,
                description="Up to 30 lead ids to restrict the read to (Marketo's own server-side cap).",
            ),
            "activity_type_ids": ParameterMetadata(
                type=ParameterType.LIST,
                required=True,
                description=(
                    "One to 10 ids from list_activity_types for this Marketo "
                    "instance. Required on every page."
                ),
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description=(
                "records (activity items) + row_count + spilled=false inline, OR result_blob_key "
                "+ row_count + spilled=true when large; plus next_page_token and more_result. "
                "more_result true means keep paging even if records is empty."
            ),
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def get_activities(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda client: marketing_actions.get_activities(client, params, self._store_blob), "get_activities")

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
            "List campaigns, optionally filtered by names and/or program_names. Marketo pages "
            "at 300 campaigns — check more_result and continue with next_page_token, otherwise "
            "an instance with more than 300 campaigns gives you an arbitrary slice."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "names": ParameterMetadata(type=ParameterType.LIST, required=False, description="Optional list of exact campaign names to filter by."),
            "program_names": ParameterMetadata(type=ParameterType.LIST, required=False, description="Optional list of exact program names to filter by."),
            "next_page_token": ParameterMetadata(type=ParameterType.STRING, required=False, description="Continue a prior list_campaigns page."),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="records inline or result_blob_key on spill, plus row_count, next_page_token, more_result.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_campaigns(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda client: marketing_actions.list_campaigns(client, params, self._store_blob), "list_campaigns")

    @platform_process(
        name="trigger_campaign",
        display_name="Marketo: Trigger Campaign",
        description=(
            "Trigger (Request Campaign) a campaign for up to 100 leads, with optional campaign "
            "tokens. Destructive-class write action: the campaign's flow runs against real people, "
            "is irreversible, and is visible outside this system (it may send email, alert sales, "
            "change scoring, or move program status). Marketo's REST API exposes no way to read a "
            "campaign's flow steps first and no dry-run, so a caller cannot establish what this "
            "will do before it happens — trigger only campaigns you authored."
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
            "List static lists, optionally filtered by names. Same 300-per-page cap as "
            "list_campaigns — check more_result and continue with next_page_token."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "names": ParameterMetadata(type=ParameterType.LIST, required=False, description="Optional list of exact static list names to filter by."),
            "next_page_token": ParameterMetadata(type=ParameterType.STRING, required=False, description="Continue a prior list_static_lists page."),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="records inline or result_blob_key on spill, plus row_count, next_page_token, more_result.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_static_lists(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda client: marketing_actions.list_static_lists(client, params, self._store_blob), "list_static_lists")

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
            lambda client: marketing_actions.check_setup(client, self._store_blob), "check_setup"
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
