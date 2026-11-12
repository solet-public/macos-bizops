"""Hermetic fakes shared by the g_suite_plugin smokes (no network, no Keychain).

``FakeVault`` mirrors the vault service's store/retrieve/exists/rotate/delete
envelope shapes in memory. ``connect_accounts`` wires a plugin to a REAL
``AccountStore`` over that vault and connects the given emails, so smokes
exercise the actual multiple-account model rather than a stub. ``FakeJobs`` is
the in-memory job ledger the plugin's dispatch and worker use, and
``run_queued`` runs one queued verb's job to its end on the calling thread.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "g_suite_plugin" / "src"))

from g_suite_plugin.accounts import AccountStore  # noqa: E402
from g_suite_plugin.oauth.app_config import AppConfigLoader  # noqa: E402
from g_suite_plugin.oauth.token_store import TokenStore  # noqa: E402

CLIENT_SECRET = "client-secret-XYZ-123456"


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"action_status": "completed", "data": data}


class FakeVault:
    """In-memory vault with the real service's result envelopes."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.deleted: list[str] = []

    def store(self, key: str, value: str, tags: list[str] | None = None, metadata: dict[str, str] | None = None) -> dict[str, Any]:
        self.values[key] = value
        return _ok({"key": key})

    def rotate(self, key: str, value: str) -> dict[str, Any]:
        self.values[key] = value
        return _ok({"key": key})

    def retrieve(self, key: str) -> dict[str, Any]:
        if key not in self.values:
            return {"action_status": "error", "error": {"message": "missing"}}
        return _ok({"value": self.values[key]})

    def exists(self, key: str) -> dict[str, Any]:
        return _ok({"exists": key in self.values})

    def delete(self, key: str) -> dict[str, Any]:
        self.values.pop(key, None)
        self.deleted.append(key)
        return _ok({"deleted": True})


def fake_loader() -> AppConfigLoader:
    config = SimpleNamespace(client_id="client-id", client_secret=CLIENT_SECRET, redirect_uri="http://127.0.0.1/cb")
    return SimpleNamespace(load=lambda: config)  # type: ignore[return-value]


def connect_accounts(plugin: Any, emails: list[str], vault: FakeVault | None = None) -> AccountStore:
    """Bind a real AccountStore to ``plugin`` and connect ``emails`` (first = default)."""
    vault = vault or FakeVault()
    loader = fake_loader()
    accounts = AccountStore(vault, loader)
    plugin._app_config_loader = loader  # noqa: SLF001
    plugin._token_store = TokenStore(vault, loader)  # noqa: SLF001
    plugin._accounts = accounts  # noqa: SLF001
    for index, email in enumerate(emails):
        store = accounts.token_store(email)
        store.store_initial_tokens(refresh_token=f"refresh-{index}-abcdefgh", access_token=f"access-{index}-abcdefgh", expires_in=3600)
        store.record_credential_facts(
            granted_scopes=("https://www.googleapis.com/auth/gmail.modify",),
            account_email=email,
            account_tier="consumer" if email.endswith("@gmail.com") else "workspace_user",
        )
        accounts.upsert(email, alias="", set_default=False)
    return accounts


STATE: dict[str, str] = {"session_id": "smoke-session", "flow_id": "smoke-flow"}
TERMINAL_STATUSES = ("completed", "error")


class FakeJobs:
    """In-memory AsyncJobManager surface: create, list, payload, update, plus a wait for tests."""

    def __init__(self) -> None:
        self.requests: dict[str, dict[str, Any]] = {}
        self.rows: dict[str, dict[str, Any]] = {}
        self._changed = threading.Condition()

    def create_job(self, **kwargs: Any) -> dict[str, Any]:
        with self._changed:
            job_id = f"job-{len(self.rows) + 1}"
            self.requests[job_id] = kwargs["request_data"]
            self.rows[job_id] = {"id": job_id, "status": "queued", "provider_name": f"{kwargs['plugin_name']}.{kwargs['action_name']}", "metadata": kwargs.get("job_metadata")}
        return _ok({"job_id": job_id, "status": "queued"})

    def list_jobs(self, status: str | None = None, provider_name: str | None = None, limit: int = 100, **_: Any) -> dict[str, Any]:
        with self._changed:
            jobs = [dict(row) for row in self.rows.values() if status in (None, row["status"]) and provider_name in (None, row["provider_name"])]
        return _ok({"jobs": jobs[:limit]})

    def get_job_payload(self, job_id: str, payload_type: str = "request") -> dict[str, Any]:
        return _ok({"payload": self.requests[job_id]})

    def update_job(self, job_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        with self._changed:
            self.rows[job_id].update(updates)
            self._changed.notify_all()
        return _ok({"job_id": job_id, "updated": True})

    def wait_terminal(self, job_id: str, timeout_s: float) -> dict[str, Any]:
        """The job row once it is completed or error (or as it stands at the timeout)."""
        with self._changed:
            self._changed.wait_for(lambda: self.rows[job_id]["status"] in TERMINAL_STATUSES, timeout=timeout_s)
            return dict(self.rows[job_id])


def job_envelope(row: dict[str, Any]) -> dict[str, Any]:
    """A finished job row in the verb envelope shape the verb used to return inline."""
    if row["status"] == "completed":
        return {"action_status": "completed", "data": row["result"], "error": None}
    return {"action_status": "error", "data": {}, "error": row.get("error")}


def run_queued(plugin: Any, verb: str, params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch ``verb`` and run its job here; return the job outcome, or the dispatch error as is."""
    if plugin._deferred.manager is None:  # noqa: SLF001
        plugin._deferred.manager = FakeJobs()  # noqa: SLF001
    jobs: FakeJobs = plugin._deferred.manager  # noqa: SLF001
    response: dict[str, Any] = getattr(plugin, verb)(params, STATE)
    job_id = response["data"].get("job_id") if response["action_status"] == "completed" else None
    if not isinstance(job_id, str):
        return response
    plugin._process_job({"id": job_id})  # noqa: SLF001
    return job_envelope(jobs.rows[job_id])
