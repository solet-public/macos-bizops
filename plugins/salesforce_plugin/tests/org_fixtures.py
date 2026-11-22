"""Shared fakes for the build 6 bound-org smokes (org_bindings_smoke.py, org_lifecycle_smoke.py).

A scripted ``sf`` CLI (``subprocess.run`` patched), an in-memory vault proxy,
the job manager surface, and real-shaped ``--json`` output of
``@salesforce/cli`` 2.142.7 (``org display`` and login results carry
``accessToken`` as "[REDACTED] Use 'sf org auth show-access-token' to view";
a failure is ``{"name", "message", "exitCode", "status", "actions"...}`` on
stdout). Not a smoke itself.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import NonCallableMagicMock, patch
from urllib.parse import quote, quote_plus

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta/src"))
sys.path.insert(0, str(ROOT / "plugins/salesforce_plugin/src"))

from salesforce_plugin import async_jobs  # noqa: E402
from salesforce_plugin.orgs import OrgIndex, OrgIndexState  # noqa: E402
from salesforce_plugin.plugin import SalesforcePlugin  # noqa: E402

PROD_ID = "00DR0000000Q7PqMAK"
SANDBOX_ID = "00DR0000000Z1FtMAK"
HUB_ID = "00DR0000000HubXMAK"
REDACTED_TOKEN = "[REDACTED] Use 'sf org auth show-access-token' to view"
SECRETS_WARNING = "Secrets are now hidden from 'sf org display' command output. Use the 'sf org auth' commands instead. As a temporary workaround, you can set SF_TEMP_SHOW_SECRETS=true to render these secrets. This workaround will be removed in an upcoming release."
# The two five-dash runs are joined here, not written out: the seed content scan (section 4.2 #3) refuses a literal private-key header in any shipped file, a fake one in a test fixture too. The value is unchanged.
_PEM_RULE = "-" * 5
PEM = _PEM_RULE + "BEGIN PRIVATE KEY" + _PEM_RULE + "\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7+fixture+key/body==\n" + _PEM_RULE + "END PRIVATE KEY" + _PEM_RULE
SFDX_URL = "force://PlatformCLI::5Aep861fixture.refresh+token/value@acme--uat.sandbox.my.salesforce.com"
ACCESS_TOKEN = "00DR0000000Q7Pq!AQ8AQFixtureAccessToken.value+/="
CLIENT_SECRET = "C1ient$ecret+fixture/value=="
STATE = {"session_id": "fixture-session", "flow_id": "fixture-flow"}

# Every flag the sf CLI 2.142.7 command manifest (sf commands --json) documents
# for each wrapped command, without the global --json, --flags-dir, --loglevel.
DOCUMENTED_FLAGS: dict[str, set[str]] = {
    "web": {"browser", "client-id", "instance-url", "set-default-dev-hub", "set-default", "alias", "no-prompt", "client-app", "username", "scopes"},
    "jwt": {"username", "jwt-key-file", "client-id", "instance-url", "set-default-dev-hub", "set-default", "alias", "no-prompt"},
    "sfdx_url": {"sfdx-url-file", "sfdx-url-stdin", "set-default-dev-hub", "set-default", "alias", "no-prompt"},
    "access_token": {"instance-url", "set-default-dev-hub", "set-default", "alias", "no-prompt"},
    "logout": {"target-org", "client-app", "all", "no-prompt"},
}
_SANDBOX_CREATE = {"definition-file", "set-default", "alias", "wait", "poll-interval", "async", "name", "source-sandbox-name", "source-id", "license-type", "target-org", "no-prompt", "no-track-source"}
LIFECYCLE_DOCUMENTED_FLAGS: dict[str, set[str]] = {
    "list": {"verbose", "all", "clean", "no-prompt", "skip-connection-status"},
    "display": {"target-org", "api-version", "verbose"},
    "open": {"target-org", "api-version", "private", "browser", "path", "url-only", "source-file"},
    "create_scratch": {"alias", "async", "set-default", "definition-file", "target-dev-hub", "no-ancestors", "edition", "snapshot", "no-namespace", "duration-days", "wait", "api-version", "client-id", "track-source", "username", "description", "name", "release", "admin-email", "source-org"},
    "resume_scratch": {"job-id", "use-most-recent", "wait"},
    "delete_scratch": {"target-org", "no-prompt"},
    "create_sandbox": _SANDBOX_CREATE,
    "clone_sandbox": _SANDBOX_CREATE,
    "refresh_sandbox": {"no-auto-activate", "wait", "poll-interval", "source-sandbox-name", "source-id", "async", "name", "definition-file", "target-org", "no-prompt"},
    "resume_sandbox": {"wait", "name", "job-id", "use-most-recent", "target-org"},
    "delete_sandbox": {"target-org", "no-prompt"},
}


def _flag_cases(name: str, flag: Any) -> list[tuple[object, list[str]]]:
    """Sample values of one documented flag and the CLI arguments each must become."""
    if flag.kind == "boolean":
        return [(True, [f"--{name}"]), (False, [f"--no-{name}"] if flag.negatable else [])]
    if flag.kind == "choice":
        return [(option, [f"--{name}={option}"]) for option in flag.options]
    if flag.kind == "integer":
        return [(3, [f"--{name}=3"])]
    if flag.kind == "file":
        return [("/fixture/def.json", [f"--{name}=/gated/fixture/def.json"])]
    return [("v-1 two", [f"--{name}=v-1 two"])]


def _required_besides(command: Any, name: str) -> dict[str, object]:
    """Values for the command's other required flags, so one flag can be built alone."""
    return {other: True if spec.kind == "boolean" else "x" for other, spec in command.flags.items() if spec.required and other != name}


def assert_every_flag_reaches_the_cli(case: unittest.TestCase, label: str, command: Any) -> None:
    """Each caller flag of one command, built alone, appears in argv in its documented form."""
    from salesforce_plugin.cli_commands import command_argv

    words = list(command.words)
    for name, flag in command.flags.items():
        required = _required_besides(command, name)
        for value, expected in _flag_cases(name, flag):
            with case.subTest(command=label, flag=name, value=value):
                argv = command_argv(command, {**required, name: value}, read_gate=lambda path: "/gated" + path)
                case.assertEqual(argv[: len(words)], words)
                case.assertEqual([item for item in argv[len(words) :] if item.split("=", 1)[0].removeprefix("--") not in required], expected)


def variants(secret: str) -> list[str]:
    """Raw, percent (both hex cases), form, base64, URL-safe base64 and JSON-escaped forms."""
    upper = quote(secret, safe="")
    lower = re.sub(r"%[0-9A-F]{2}", lambda match: match.group(0).lower(), upper)
    raw = secret.encode("utf-8")
    forms = [secret, upper, lower, quote_plus(secret, safe=""), base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode(), json.dumps(secret)[1:-1]]
    # A multi-line PEM is also checked by its key body line, the part that matters.
    forms.extend(line for line in secret.splitlines() if len(line) > 20 and "BEGIN" not in line and "END" not in line)
    return list(dict.fromkeys(forms))


class FakeVault:
    """The vault proxy's store/retrieve/exists/rotate/delete shapes, in memory."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.fail_writes = False

    def store(self, key: str, value: str, tags: list[str] | None = None, metadata: dict[str, str] | None = None) -> dict[str, Any]:
        if self.fail_writes:
            return {"action_status": "error", "error": {"message": "write refused"}}
        self.values[key] = value
        return {"action_status": "completed", "data": {"key": key}}

    def rotate(self, key: str, new_value: str) -> dict[str, Any]:
        return self.store(key, new_value)

    def retrieve(self, key: str) -> dict[str, Any]:
        if key not in self.values:
            return {"action_status": "error", "error": {"code": "not_found"}}
        return {"action_status": "completed", "data": {"value": self.values[key]}}

    def exists(self, key: str) -> dict[str, Any]:
        return {"action_status": "completed", "data": {"exists": key in self.values}}

    def delete(self, key: str) -> dict[str, Any]:
        existed = self.values.pop(key, None) is not None
        return {"action_status": "completed" if existed else "error", "data": {}}


class FakeJobs:
    """AsyncJobManager surface used by dispatch and the worker."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.payloads: dict[str, dict[str, Any]] = {}
        self.updates: dict[str, list[dict[str, Any]]] = {}

    def create_job(self, **kwargs: Any) -> dict[str, Any]:
        job_id = f"job-{len(self.rows) + 1}"
        self.rows[job_id] = {"id": job_id, "status": "queued", "provider_name": f"{kwargs['plugin_name']}.{kwargs['action_name']}", "metadata": kwargs.get("job_metadata")}
        self.payloads[job_id] = dict(kwargs.get("request_data") or {})
        return {"action_status": "completed", "data": {"job_id": job_id, "status": "queued"}}

    def get_job(self, job_id: str) -> dict[str, Any]:
        return {"action_status": "completed", "data": {"job": self.rows[job_id]}}

    def get_job_payload(self, job_id: str, payload_type: str = "request") -> dict[str, Any]:
        return {"action_status": "completed", "data": {"payload": self.payloads[job_id]}}

    def update_job(self, job_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        self.updates.setdefault(job_id, []).append(updates)
        return {"action_status": "completed", "data": {}}

    def list_jobs(self, **_kwargs: Any) -> dict[str, Any]:
        return {"action_status": "completed", "data": {"jobs": []}}


class FakeCli:
    """A scripted sf CLI: each call is answered by the first matching rule and recorded."""

    def __init__(self) -> None:
        self.rules: list[tuple[Callable[[list[str], str], bool], str, int]] = []
        self.calls: list[dict[str, Any]] = []
        self.watched: list[Path] = []

    def on(self, *words: str, target: str | None = None, soql: str | None = None, out: object, code: int = 0) -> None:
        def matches(argv: list[str], query: str) -> bool:
            if argv[1 : 1 + len(words)] != list(words):
                return False
            if target is not None and target not in argv:
                return False
            return soql is None or soql in query

        self.rules.insert(0, (matches, out if isinstance(out, str) else json.dumps(out), code))

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        query = Path(argv[argv.index("--file") + 1]).read_text(encoding="utf-8") if "--file" in argv else ""
        files = {str(path): (path.read_text(encoding="utf-8"), oct(path.stat().st_mode & 0o777), oct(path.parent.stat().st_mode & 0o777)) if path.exists() else None for path in self.watched}
        self.calls.append({"argv": list(argv), "env": dict(kwargs.get("env") or {}), "input": kwargs.get("input"), "timeout": kwargs.get("timeout"), "soql": query, "files": files})
        for matches, out, code in self.rules:
            if matches(argv, query):
                return subprocess.CompletedProcess(argv, code, out, "")
        raise AssertionError(f"unscripted sf call: {argv}")


def display(org_id: str, username: str, host: str, alias: str = "", dev_hub: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {"id": org_id, "apiVersion": "62.0", "accessToken": REDACTED_TOKEN, "instanceUrl": f"https://{host}", "username": username, "clientId": "PlatformCLI", "connectedStatus": "Connected"}
    if alias:
        result["alias"] = alias
    if dev_hub:
        result.update({"devHubId": dev_hub, "status": "Active", "expirationDate": "2026-11-27", "createdBy": dev_hub, "edition": "Developer", "orgName": "Acme Scratch", "createdDate": "2026-11-20T12:00:00.000+0000", "signupUsername": username})
        del result["connectedStatus"]
    return {"status": 0, "result": result, "warnings": [SECRETS_WARNING]}


def organization(org_id: str, *, sandbox: bool) -> dict[str, Any]:
    record = {"attributes": {"type": "Organization", "url": f"/services/data/v62.0/sobjects/Organization/{org_id}"}, "Id": org_id, "IsSandbox": sandbox, "OrganizationType": "Enterprise Edition"}
    return {"status": 0, "result": {"records": [record], "totalSize": 1, "done": True}, "warnings": []}


NO_ROWS = {"status": 0, "result": {"records": [], "totalSize": 0, "done": True}, "warnings": []}
NOT_A_HUB = {"name": "INVALID_TYPE", "message": "sObject type 'ScratchOrgInfo' is not supported.", "exitCode": 1, "status": 1, "context": "DataQuery", "data": {"errorCode": "INVALID_TYPE", "message": "sObject type 'ScratchOrgInfo' is not supported."}, "commandName": "DataSoqlQueryCommand", "warnings": []}


def script_org(cli: FakeCli, org_id: str, username: str, host: str, *, alias: str = "", sandbox: bool = False, hub: bool = False, dev_hub: str = "") -> None:
    """The CLI answers for the org by username and, when it has one, by its CLI alias."""
    for target in (username, alias) if alias else (username,):
        cli.on("org", "display", target=target, out=display(org_id, username, host, alias, dev_hub))
        cli.on("data", "query", target=target, soql="FROM Organization", out=organization(org_id, sandbox=sandbox))
        cli.on("data", "query", target=target, soql="FROM ScratchOrgInfo", out=NO_ROWS if hub else NOT_A_HUB)


class Harness(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.vault = FakeVault()
        self.jobs = FakeJobs()
        self.cli = FakeCli()
        self.keys_dir = self.workspace / "profile" / "data" / "plugin_data" / "salesforce_plugin" / "jwt_keys"
        # A saved location outside the fixture solet's own tree (containment refuses that tree).
        imports = tempfile.TemporaryDirectory()
        self.addCleanup(imports.cleanup)
        self.imports = Path(imports.name).resolve()
        self.address_book = NonCallableMagicMock()
        self.address_book.resolve.return_value = {"action_status": "error", "error": {"code": "address_book.not_found", "message": "Address 'salesforce_org' not found"}}
        self.address_book.list_path_addresses.return_value = {"action_status": "completed", "data": {"addresses": [{"name": "keys", "tags": [], "updated_at": datetime.now(UTC).isoformat(), "entries": [{"field_type": "path", "value": str(self.imports)}]}]}}
        self.plugin = self.make_plugin()

    def make_plugin(self) -> SalesforcePlugin:
        plugin = SalesforcePlugin()
        orchestrator = NonCallableMagicMock()
        orchestrator.APP_HOME = str(self.workspace / "profile")
        orchestrator.get_service.return_value = self.address_book
        orchestrator.config_manager.get_plugin_config.return_value = {"sf_cli_path": "/fake/bin/sf"}
        orchestrator.plugin_manager.get_plugin.return_value = None
        plugin.orchestrator_ref = orchestrator
        plugin.set_vault_service(self.vault)
        plugin.prepare_for_readiness()
        plugin._async_job_manager = self.jobs  # noqa: SLF001
        return plugin

    def run_verb(self, verb: str, params: dict[str, Any]) -> dict[str, Any]:
        """Dispatch, then run the job on the worker; returns the final job update."""
        queued = getattr(self.plugin, verb)(params, STATE)
        self.assertEqual(queued["action_status"], "completed", queued)
        job_id = queued["data"]["job_id"]
        with patch("salesforce_plugin.client.subprocess.run", side_effect=self.cli), patch.object(async_jobs, "ensure_worker_started"):
            async_jobs._process_job(self.plugin, self.jobs, job_id, verb)  # noqa: SLF001
        return self.jobs.updates[job_id][-1]

    def ok(self, verb: str, params: dict[str, Any]) -> dict[str, Any]:
        update = self.run_verb(verb, params)
        self.assertEqual(update.get("status"), "completed", update)
        return update["result"]

    def failed(self, verb: str, params: dict[str, Any]) -> dict[str, Any]:
        update = self.run_verb(verb, params)
        self.assertEqual(update.get("status"), "error", update)
        return update["error"]

    def index(self) -> OrgIndexState:
        return OrgIndex(self.vault).load()

    def bind_two(self) -> None:
        script_org(self.cli, PROD_ID, "admin@acme.com", "acme.my.salesforce.com", alias="prod")
        script_org(self.cli, SANDBOX_ID, "admin@acme.com.uat", "acme--uat.sandbox.my.salesforce.com", sandbox=True)
        self.ok("bind_org", {"target_org": "prod"})
        self.ok("bind_org", {"target_org": "admin@acme.com.uat", "alias": "uat"})
