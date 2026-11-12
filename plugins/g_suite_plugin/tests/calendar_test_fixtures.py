"""Shared hermetic Calendar client/account/job fixtures; no network or vault access."""

from __future__ import annotations

import copy
import json
from typing import Any

from g_suite_plugin import calendar_discovery  # pyright: ignore[reportMissingImports]
from g_suite_plugin.calendar_operations import CalendarOperation  # pyright: ignore[reportMissingImports]
from g_suite_plugin.constants import CALENDAR_OAUTH_SCOPES  # pyright: ignore[reportMissingImports]
from g_suite_plugin.plugin import GSuitePlugin  # pyright: ignore[reportMissingImports]
from googleapiclient.discovery import build_from_document
from googleapiclient.http import HttpMockSequence
from gsuite_fakes import connect_accounts  # pyright: ignore[reportMissingImports]

__all__ = ["TOKEN", "_error", "_minimal", "_ok", "_plugin", "_service"]

TOKEN = "ya29.a0AfB_byC/token+with=chars"


def _service(responses: list[tuple[dict[str, str], str]]) -> tuple[Any, HttpMockSequence]:
    http = HttpMockSequence(responses)
    return build_from_document(copy.deepcopy(calendar_discovery.calendar_document()), http=http), http


def _ok(body: dict[str, Any] | None = None, status: int = 200) -> tuple[dict[str, str], str]:
    return {"status": str(status)}, json.dumps(body) if body is not None else ""


def _error(status: int, reason: str, message: str, *, status_text: str = "") -> tuple[dict[str, str], str]:
    payload = {"error": {"code": status, "message": message, "errors": [{"domain": "global", "reason": reason, "message": message}], "status": status_text}}
    return {"status": str(status)}, json.dumps(payload)


class _FakeJobs:
    def __init__(self) -> None:
        self.payloads: dict[str, dict[str, Any]] = {}
        self.updates: list[tuple[str, dict[str, Any]]] = []

    def create_job(self, **kwargs: Any) -> dict[str, Any]:
        job_id = f"job-{len(self.payloads) + 1}"
        self.payloads[job_id] = kwargs["request_data"]
        return {"action_status": "completed", "data": {"job_id": job_id}}

    def get_job_payload(self, job_id: str) -> dict[str, Any]:
        return {"data": {"payload": self.payloads[job_id]}}

    def update_job(self, job_id: str, updates: dict[str, Any]) -> None:
        self.updates.append((job_id, updates))


def _plugin() -> tuple[GSuitePlugin, _FakeJobs]:
    plugin = GSuitePlugin()
    jobs = _FakeJobs()
    accounts = connect_accounts(plugin, ["ana@gmail.com", "work@example.com"])
    for email in ("ana@gmail.com", "work@example.com"):
        store = accounts.token_store(email)
        store.store_initial_tokens(refresh_token=f"refresh-{email}", access_token=TOKEN if email.endswith("@gmail.com") else "workspace-access-token", expires_in=3600)
        store.record_credential_facts(granted_scopes=CALENDAR_OAUTH_SCOPES, account_email=email, account_tier="consumer" if email.endswith("@gmail.com") else "workspace_user")
    accounts.upsert("ana@gmail.com", alias="home", set_default=False)
    accounts.upsert("work@example.com", alias="work", set_default=False)
    plugin._deferred.manager = jobs  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    return plugin, jobs


def _minimal(op: CalendarOperation) -> dict[str, Any]:
    """The required arguments of one verb, with valid sample values."""
    samples: dict[str, Any] = {"calendar_id": "team@group.calendar.google.com", "event_id": "evt123", "rule_id": "user:ana@example.com", "setting": "timezone", "text": "Lunch tomorrow 12:30", "destination": "other@group.calendar.google.com", "new_data_owner": "bo@example.com", "use_admin_access": True}
    params: dict[str, Any] = {}
    for name, spec in calendar_discovery.parameter_metadata(op.resource, op.method).items():
        if spec.required:
            params[name] = samples[name]
    if op.takes_body:
        params["body"] = {"id": "chan-1", "type": "web_hook", "address": "https://hooks.example.com/cal"} if op.method in ("watch", "stop") else {"summary": "x"}
    return params
