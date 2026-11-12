"""Process definitions and EDGE registrations for Jira binding and typed families."""

from __future__ import annotations

from collections.abc import Callable
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
from ananta.core.domain.enums import ProcessorPolicyCategory
from ananta.interfaces.edge_process_provider import EdgeProcessDefinition

from .families import INVENTORY, operations
from .site_context import SITE_PARAMETER, selected_site
from .sites import SiteStore

FAMILY_NAMES: tuple[str, ...] = tuple(INVENTORY["families"])


def family_edges() -> dict[str, EdgeProcessDefinition]:
    return {
        name: EdgeProcessDefinition(
            name=name,
            result_processor_template_customizations=MergeResultProcessorCustomizations(
                action_label=f"Jira {name}",
                result_type=f"jira_{name}_result",
                result_description="Site-labelled queued job or binding readback. Job completion includes native vendor response and pagination.",
            ),
            error_processor_template_customizations=MergeErrorProcessorCustomizations(
                action_context=f"Jira {name} operation",
                error_interpretation="Typed jira errors identify invalid parameters, missing sites, authentication, permissions or vendor faults.",
                recovery_guidance="Correct the parameters or site binding; honor vendor permissions and Retry-After. Do not retry a write blindly.",
                retryable=False,
            ),
        )
        for name in (*FAMILY_NAMES, "sites", "oauth")
    }


def _parameter(description: str, kind: ParameterType = ParameterType.STRING, required: bool = False) -> ParameterMetadata:
    return ParameterMetadata(type=kind, required=required, description=description)


def _parameters(family: str) -> dict[str, ParameterMetadata]:
    return {
        "site": SITE_PARAMETER,
        "operation": _parameter("Vendor operation name: " + operations(family), required=True),
        "path": _parameter("Vendor path identifiers using the exact camelCase names in the operation inventory.", ParameterType.OBJECT),
        "query": _parameter("Every documented vendor query parameter; preserve names/types, including pagination and expansion.", ParameterType.OBJECT),
        "body": _parameter("Complete vendor JSON request body; all documented fields accepted, including optional fields.", ParameterType.OBJECT),
        "body_json": _parameter("Alternative JSON text for scalar/array/null entity-property values; mutually exclusive with body."),
        "headers": _parameter("Vendor request headers; authentication and routing come from the selected site.", ParameterType.OBJECT),
    }


class FamilyProcesses:
    """Process mixin; the plugin supplies execution and envelope helpers."""

    def _run(self, produce: Callable[[], dict[str, Any]], endpoint_name: str) -> dict[str, Any]:
        raise NotImplementedError

    def _dispatch_async(self, action_name: str, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def _require_sites(self) -> SiteStore:
        raise NotImplementedError

    @platform_process(
        name="sites",
        display_name="Jira: Sites",
        description="List, bind/correct, set the default, or disconnect Jira site bindings. Bind API tokens from vault-referenced address-book entries; no raw credentials are accepted. Writes require the version returned by list.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "site": SITE_PARAMETER,
            "operation": _parameter("list, history, bind_api_token, set_default, disconnect", required=True),
            "entry_name": _parameter("Address-book entry with base_url/email/api_token/expires_at/scope_note; token is a vault reference."),
            "expected_version": _parameter("Current sites version from list; required for binding mutations.", ParameterType.INTEGER),
            "set_default": _parameter("Make this bound site the default.", ParameterType.BOOLEAN),
        },
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Non-secret site index, default and current version."),
        context_handling=ContextHandling.NONE,
    )
    def sites(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda: {**self._require_sites().manage(params), "site": params.get("site", "legacy")}, "sites")

    @platform_process(
        name="oauth",
        display_name="Jira: OAuth",
        description="Begin, inspect, complete or cancel Atlassian OAuth 2.0 authorization-code consent for a site. Uses an address-book OAuth app and a vaulted authorization code, verifies state and accessible resources, and persists rotating refresh tokens. Completion does not revoke other sites.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "site": SITE_PARAMETER,
            "operation": _parameter("begin, status, complete, cancel", required=True),
            "base_url": _parameter("HTTPS Atlassian site origin to bind, used by begin."),
            "app_entry": _parameter("Address-book OAuth application, defaults jira_oauth_app."),
            "state": _parameter("State from the registered redirect; must match begin's authorization URL."),
            "code_vault_key": _parameter("Plugin-owned oauth_code vault key holding the redirect's authorization code; consumed after success."),
            "expected_version": _parameter("Current sites version; required for complete.", ParameterType.INTEGER),
            "set_default": _parameter("Set this site as default after completed consent.", ParameterType.BOOLEAN),
        },
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Consent URL or pending status or non-secret binding readback; never tokens."),
        context_handling=ContextHandling.NONE,
    )
    def oauth(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda: self._require_sites().oauth(params), "oauth")

    @platform_process(
        name="projects",
        display_name="Jira: Projects",
        description="Run the complete documented projects operation family on the selected Jira site, including writes and deletes where the vendor provides them. operation selects an exact vendor method/path; path, query and body preserve vendor names. Returns a queued job; completion includes site, status, native response and next_query pagination.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=_parameters("projects"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id/status; site-labelled vendor result arrives at completion."),
        context_handling=ContextHandling.NONE,
    )
    @selected_site
    def projects(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("projects", params, state)

    @platform_process(
        name="boards",
        display_name="Jira: Boards",
        description="Run the complete documented boards operation family on the selected Jira site, including writes and deletes where the vendor provides them. operation selects an exact vendor method/path; path, query and body preserve vendor names. Returns a queued job; completion includes site, status, native response and next_query pagination.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=_parameters("boards"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id/status; site-labelled vendor result arrives at completion."),
        context_handling=ContextHandling.NONE,
    )
    @selected_site
    def boards(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("boards", params, state)

    @platform_process(
        name="sprints",
        display_name="Jira: Sprints",
        description="Run the complete documented sprints operation family on the selected Jira site, including writes and deletes where the vendor provides them. operation selects an exact vendor method/path; path, query and body preserve vendor names. Returns a queued job; completion includes site, status, native response and next_query pagination.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=_parameters("sprints"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id/status; site-labelled vendor result arrives at completion."),
        context_handling=ContextHandling.NONE,
    )
    @selected_site
    def sprints(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("sprints", params, state)

    @platform_process(
        name="backlog",
        display_name="Jira: Backlog",
        description="Run the complete documented backlog operation family on the selected Jira site, including writes and deletes where the vendor provides them. operation selects an exact vendor method/path; path, query and body preserve vendor names. Returns a queued job; completion includes site, status, native response and next_query pagination.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=_parameters("backlog"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id/status; site-labelled vendor result arrives at completion."),
        context_handling=ContextHandling.NONE,
    )
    @selected_site
    def backlog(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("backlog", params, state)

    @platform_process(
        name="agile_issues",
        display_name="Jira: Agile Issues",
        description="Run the complete documented agile_issues operation family on the selected Jira site, including writes and deletes where the vendor provides them. operation selects an exact vendor method/path; path, query and body preserve vendor names. Returns a queued job; completion includes site, status, native response and next_query pagination.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=_parameters("agile_issues"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id/status; site-labelled vendor result arrives at completion."),
        context_handling=ContextHandling.NONE,
    )
    @selected_site
    def agile_issues(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("agile_issues", params, state)

    @platform_process(
        name="versions",
        display_name="Jira: Versions",
        description="Run the complete documented versions operation family on the selected Jira site, including writes and deletes where the vendor provides them. operation selects an exact vendor method/path; path, query and body preserve vendor names. Returns a queued job; completion includes site, status, native response and next_query pagination.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=_parameters("versions"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id/status; site-labelled vendor result arrives at completion."),
        context_handling=ContextHandling.NONE,
    )
    @selected_site
    def versions(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("versions", params, state)

    @platform_process(
        name="worklogs",
        display_name="Jira: Worklogs",
        description="Run the complete documented worklogs operation family on the selected Jira site, including writes and deletes where the vendor provides them. operation selects an exact vendor method/path; path, query and body preserve vendor names. Returns a queued job; completion includes site, status, native response and next_query pagination.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=_parameters("worklogs"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id/status; site-labelled vendor result arrives at completion."),
        context_handling=ContextHandling.NONE,
    )
    @selected_site
    def worklogs(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("worklogs", params, state)
