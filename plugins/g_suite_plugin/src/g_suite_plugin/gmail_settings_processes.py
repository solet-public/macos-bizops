"""``@platform_process`` Gmail build 2 verbs: push watch/stop, settings, filters, forwarding.

Every decorator keeps a literal ``name`` and takes the rest of its declaration
from the verb's row in :mod:`gmail_settings_spec` through :func:`settings_process`,
so the parameters a verb declares are exactly the ones its executor accepts.
Deferred-completion EDGE verbs: dispatch validates the account and enqueues;
the worker runs :func:`gmail_settings_actions.run_settings_verb` as the
resolved account. Existing completion metadata identifies a Gmail job before
its request is read, and the request must name the same verb. Results are
returned as received.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, cast

from ananta.core.actions.action_metadata import (
    ContextHandling,
    MergeErrorProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.domain.enums import ProcessorPolicyCategory

from . import calendar_operations
from .account_processes import ACCOUNT_PARAM, ACTIVE_ACCOUNT, AccountProcesses
from .accounts import AccountStore
from .calendar_processes import CalendarProcesses
from .gmail_settings_actions import GMAIL_SETTINGS_VERBS, run_settings_verb
from .gmail_settings_spec import BOOL, INT, INT64, STR, Field

_QUEUED = ReturnValueSchema(type=ParameterType.OBJECT, description="job_id for the queued Gmail call plus its status ('queued').")
_NOTE = " Runs as a background job: returns a job_id at once and the result arrives as a follow-up message. Optional account (email or alias) picks the connected Google account; the result names the account used."
_TYPES = {STR: ParameterType.STRING, BOOL: ParameterType.BOOLEAN, INT: ParameterType.INTEGER, INT64: ParameterType.INTEGER}


def _parameter(field: Field) -> ParameterMetadata:
    return ParameterMetadata(type=_TYPES.get(field.kind, ParameterType.LIST), required=field.required, description=field.description)


def settings_process(verb: str) -> dict[str, Any]:
    """Every ``@platform_process`` keyword except ``name`` for one build-2 verb."""
    spec = GMAIL_SETTINGS_VERBS[verb]
    return {
        "display_name": spec.title,
        "description": spec.summary + _NOTE,
        "processor_policy_category": ProcessorPolicyCategory.EDGE,
        "is_async": True,
        "is_long_running": True,
        "parameters": {"account": ACCOUNT_PARAM, **{field.param: _parameter(field) for field in spec.fields}},
        "return_value_schema": _QUEUED,
        "error_processor_customizations": MergeErrorProcessorCustomizations(retryable=not spec.write),
        "context_handling": ContextHandling.NONE,
    }


class GmailSettingsProcesses:
    """Build-2 verbs and their producers; ``GSuitePlugin`` supplies the runtime hooks."""

    def _run_deferred(self, params: dict[str, Any], state: dict[str, Any], verb: str) -> dict[str, Any]:
        raise NotImplementedError

    def _require_accounts(self) -> AccountStore:
        raise NotImplementedError

    def _require_factory(self) -> Any:
        raise NotImplementedError

    def _call_as_account[Result](self, verb: str, params: dict[str, Any], produce: Callable[[], Result]) -> Result:
        """Keep Calendar's account path and the shared one on one seam."""
        if verb in calendar_operations.OPERATIONS_BY_VERB:
            return cast(Result, CalendarProcesses._call_as_account(cast(CalendarProcesses, self), verb, params, cast(Callable[[], dict[str, Any]], produce)))
        return AccountProcesses._call_as_account(cast(AccountProcesses, self), verb, params, produce)

    @staticmethod
    def _gmail_job_verb(job: dict[str, Any]) -> str:
        """Read the existing completion metadata before loading the request."""
        value: Any = job.get("metadata")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return ""
        for key in ("completion_handlers", "result", "template", "params", "prompt", "observation", "process_key"):
            value = value.get(key) if isinstance(value, dict) else None
        verb = value.rsplit("::", 1)[-1] if isinstance(value, str) else ""
        return verb if verb in GMAIL_SETTINGS_VERBS and value == f"plugin::g_suite_plugin::{verb}" else ""

    @staticmethod
    def _queued_request(payload_result: dict[str, Any], known_verb: str) -> tuple[str, dict[str, Any]]:
        request_data: dict[str, Any] = (payload_result.get("data") or {}).get("payload") or {}
        verb = str(request_data.get("verb", known_verb))
        if known_verb and verb != known_verb:
            raise ValueError("Gmail job identity does not match its queued request")
        params = request_data.get("params")
        if verb in GMAIL_SETTINGS_VERBS and not isinstance(params, dict):
            raise ValueError("Gmail job request parameters are unavailable")
        return verb, params or {}

    def _gmail_settings_producer(self, verb: str) -> Callable[[dict[str, Any]], dict[str, Any] | str]:
        """Worker-side thunk for one build-2 verb, run inside the account runtime."""
        spec = GMAIL_SETTINGS_VERBS[verb]

        def produce(params: dict[str, Any]) -> dict[str, Any] | str:
            account = ACTIVE_ACCOUNT.get()
            facts = self._require_accounts().token_store(account).credential_facts()
            return run_settings_verb(spec, lambda: self._require_factory().gmail(), params, facts, account)

        return produce

    @platform_process(name="gmail_watch", **settings_process("gmail_watch"))
    def gmail_watch(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_watch")

    @platform_process(name="gmail_stop", **settings_process("gmail_stop"))
    def gmail_stop(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_stop")

    @platform_process(name="gmail_get_auto_forwarding", **settings_process("gmail_get_auto_forwarding"))
    def gmail_get_auto_forwarding(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_get_auto_forwarding")

    @platform_process(name="gmail_update_auto_forwarding", **settings_process("gmail_update_auto_forwarding"))
    def gmail_update_auto_forwarding(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_update_auto_forwarding")

    @platform_process(name="gmail_get_imap", **settings_process("gmail_get_imap"))
    def gmail_get_imap(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_get_imap")

    @platform_process(name="gmail_update_imap", **settings_process("gmail_update_imap"))
    def gmail_update_imap(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_update_imap")

    @platform_process(name="gmail_get_pop", **settings_process("gmail_get_pop"))
    def gmail_get_pop(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_get_pop")

    @platform_process(name="gmail_update_pop", **settings_process("gmail_update_pop"))
    def gmail_update_pop(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_update_pop")

    @platform_process(name="gmail_get_language", **settings_process("gmail_get_language"))
    def gmail_get_language(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_get_language")

    @platform_process(name="gmail_update_language", **settings_process("gmail_update_language"))
    def gmail_update_language(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_update_language")

    @platform_process(name="gmail_get_vacation", **settings_process("gmail_get_vacation"))
    def gmail_get_vacation(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_get_vacation")

    @platform_process(name="gmail_update_vacation", **settings_process("gmail_update_vacation"))
    def gmail_update_vacation(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_update_vacation")

    @platform_process(name="gmail_list_filters", **settings_process("gmail_list_filters"))
    def gmail_list_filters(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_list_filters")

    @platform_process(name="gmail_get_filter", **settings_process("gmail_get_filter"))
    def gmail_get_filter(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_get_filter")

    @platform_process(name="gmail_create_filter", **settings_process("gmail_create_filter"))
    def gmail_create_filter(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_create_filter")

    @platform_process(name="gmail_delete_filter", **settings_process("gmail_delete_filter"))
    def gmail_delete_filter(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_delete_filter")

    @platform_process(name="gmail_list_forwarding_addresses", **settings_process("gmail_list_forwarding_addresses"))
    def gmail_list_forwarding_addresses(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_list_forwarding_addresses")

    @platform_process(name="gmail_get_forwarding_address", **settings_process("gmail_get_forwarding_address"))
    def gmail_get_forwarding_address(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_get_forwarding_address")

    @platform_process(name="gmail_create_forwarding_address", **settings_process("gmail_create_forwarding_address"))
    def gmail_create_forwarding_address(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_create_forwarding_address")

    @platform_process(name="gmail_delete_forwarding_address", **settings_process("gmail_delete_forwarding_address"))
    def gmail_delete_forwarding_address(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_delete_forwarding_address")
