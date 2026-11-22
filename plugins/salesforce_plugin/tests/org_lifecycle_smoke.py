"""Offline smoke for build 6's org lifecycle verb: scratch orgs, sandboxes, list, display and open.

The real plugin, worker and ``SalesforceCliExecutor`` run against the scripted
``sf`` CLI and in-memory vault of ``org_fixtures``. Each transcript is the
``--json`` output ``@salesforce/cli`` 2.142.7 prints for that command, read
from its source: ``org create scratch --async`` returns
``{username, scratchOrgInfo, warnings}`` (the ScratchOrgInfo record without
AuthCode); ``org resume scratch --wait 0`` fails with ``StillInProgressError``
and an action until the org is Active; sandbox commands return the
SandboxProcess fields plus ``SandboxUsername``, and ``org resume sandbox``
prints them with a ``Message`` and exit code 68 while the copy runs;
``org open`` returns ``{orgId, url, username}``; ``sandbox_status`` reads
SandboxProcess through ``sf api request rest --include`` on the Tooling query
resource. No sf executable, no network and no Salesforce org are needed.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

from org_fixtures import (  # noqa: E402
    CLIENT_SECRET,
    HUB_ID,
    LIFECYCLE_DOCUMENTED_FLAGS,
    PEM,
    PROD_ID,
    REDACTED_TOKEN,
    SANDBOX_ID,
    SECRETS_WARNING,
    STATE,
    Harness,
    assert_every_flag_reaches_the_cli,
    script_org,
    variants,
)
from salesforce_plugin import async_jobs  # noqa: E402
from salesforce_plugin.constants import VAULT_KEY_ORG_SECRET_PREFIX  # noqa: E402
from salesforce_plugin.org_lifecycle import OPERATIONS  # noqa: E402

SCRATCH_ID = "00DR0000000Scr1MAC"
SCRATCH_USER = "test-xyz1abc@example.com"
SCRATCH_HOST = "speed-power-1234-dev-ed.scratch.my.salesforce.com"
JOB_ID = "2SRR0000000oHbVOAU"
PROCESS_ID = "0GRR00000004CXqOAM"
SANDBOX_USER = "admin@acme.com.uat"
ORG_HEADERS = "date: Fri, 20 Nov 2026 12:30:00 GMT\nx-content-type-options: nosniff\nsforce-limit-info: api-usage=52/15000\n"


def scratch_info(status: str, *, org_id: str | None = None) -> dict[str, Any]:
    """The ScratchOrgInfo record `sobject('ScratchOrgInfo').retrieve(id)` returns, AuthCode omitted by the CLI."""
    return {
        "attributes": {"type": "ScratchOrgInfo", "url": f"/services/data/v62.0/sobjects/ScratchOrgInfo/{JOB_ID}"},
        "Id": JOB_ID,
        "OwnerId": "005R0000000HubAIAS",
        "IsDeleted": False,
        "Name": "00000071",
        "CreatedDate": "2026-11-20T12:30:00.000+0000",
        "Edition": "Developer",
        "OrgName": "Acme Scratch",
        "Status": status,
        "SignupUsername": SCRATCH_USER,
        "DurationDays": 7,
        "ExpirationDate": "2026-11-27",
        "ConnectedAppConsumerKey": "PlatformCLI",
        "LoginUrl": f"https://{SCRATCH_HOST}" if org_id else None,
        "ScratchOrg": org_id[:15] if org_id else None,
        "ErrorCode": None,
        "Snapshot": None,
        "SourceOrg": None,
        "Release": "Current",
        "Namespace": None,
    }


def sandbox_process(status: str, *, progress: int, message: str | None = None, org_id: str | None = None) -> dict[str, Any]:
    process: dict[str, Any] = {
        "attributes": {"type": "SandboxProcess", "url": f"/services/data/v62.0/tooling/sobjects/SandboxProcess/{PROCESS_ID}"},
        "Id": PROCESS_ID,
        "Status": status,
        "SandboxName": "uat",
        "SandboxInfoId": "0GQR000000048DzOAI",
        "LicenseType": "DEVELOPER",
        "CreatedDate": "2026-11-20T12:30:00.000+0000",
        "CopyProgress": progress,
        "SandboxOrganization": org_id[:15] if org_id else None,
        "SourceId": None,
        "Description": "UAT for build 6",
        "EndDate": "2026-11-20T13:42:00.000+0000" if org_id else None,
        "Features": None,
        "SandboxUsername": SANDBOX_USER,
    }
    if message is not None:
        process["Message"] = message
    return process


def transcript(status: int, body: Any) -> str:
    """What `sf api request rest --include` prints: status line, headers, no separator, pretty JSON."""
    return f"HTTP/1.1 {status}\n{ORG_HEADERS}content-type: application/json;charset=UTF-8\n{json.dumps(body, indent=2)}\n"


class Lifecycle(Harness):
    def setUp(self) -> None:
        super().setUp()
        self.vault.values[VAULT_KEY_ORG_SECRET_PREFIX + "hub_key"] = PEM
        self.vault.values[VAULT_KEY_ORG_SECRET_PREFIX + "hub_app"] = CLIENT_SECRET
        self.hub_key = self.plugin._org_secrets().jwt_key_file("hub_key").path  # noqa: SLF001
        self.cli.watched.append(self.hub_key)
        login = {"status": 0, "result": {"accessToken": REDACTED_TOKEN, "instanceUrl": "https://acme-hub.my.salesforce.com", "orgId": HUB_ID, "username": "admin@acme-hub.com", "isDevHub": True, "privateKey": str(self.hub_key)}, "warnings": [SECRETS_WARNING]}
        self.cli.on("org", "login", "jwt", out=login)
        script_org(self.cli, HUB_ID, "admin@acme-hub.com", "acme-hub.my.salesforce.com", hub=True)
        script_org(self.cli, PROD_ID, "admin@acme.com", "acme.my.salesforce.com", alias="prod")
        self.ok("login", {"method": "jwt", "jwt_key_secret": "hub_key", "alias": "hub", "flags": {"username": "admin@acme-hub.com", "client-id": "3MVG9fixtureConsumerKey"}})
        self.ok("bind_org", {"target_org": "prod"})
        self.cli.calls.clear()

    def last(self, *words: str) -> dict[str, Any]:
        return next(call for call in reversed(self.cli.calls) if call["argv"][1 : 1 + len(words)] == list(words))

    def bind_scratch(self) -> dict[str, Any]:
        self.cli.on(
            "org",
            "resume",
            "scratch",
            out={
                "status": 0,
                "result": {
                    "username": SCRATCH_USER,
                    "scratchOrgInfo": scratch_info("Active", org_id=SCRATCH_ID),
                    "authFields": {"accessToken": REDACTED_TOKEN, "instanceUrl": f"https://{SCRATCH_HOST}", "orgId": SCRATCH_ID, "username": SCRATCH_USER, "devHubUsername": "admin@acme-hub.com", "privateKey": str(self.hub_key)},
                    "warnings": [],
                    "orgId": SCRATCH_ID,
                },
            },
        )
        script_org(self.cli, SCRATCH_ID, SCRATCH_USER, SCRATCH_HOST, alias="s1", dev_hub="admin@acme-hub.com")
        return self.ok("org_lifecycle", {"operation": "resume_scratch", "alias": "s1", "flags": {"job-id": JOB_ID}})


class Coverage(Lifecycle):
    def test_every_documented_flag_is_accepted_or_set_by_the_plugin_with_a_reason(self) -> None:
        self.assertEqual(set(OPERATIONS), set(LIFECYCLE_DOCUMENTED_FLAGS) | {"sandbox_status"})
        for name, documented in LIFECYCLE_DOCUMENTED_FLAGS.items():
            command = OPERATIONS[name].command
            with self.subTest(operation=name):
                self.assertIsNotNone(command)
                if command is not None:
                    self.assertEqual(command.documented(), documented)
                    self.assertTrue(all(reason for reason in command.managed.values()))

    def test_every_caller_flag_reaches_the_cli_in_its_documented_form(self) -> None:
        for name, operation in OPERATIONS.items():
            if operation.command is not None:
                assert_every_flag_reaches_the_cli(self, name, operation.command)

    def test_malformed_requests_are_refused_before_any_job(self) -> None:
        refusals = {
            "operation": {"operation": "clone"},
            "plugin flag": {"operation": "create_scratch", "flags": {"wait": 10}},
            "async": {"operation": "create_sandbox", "flags": {"async": True}},
            "undocumented": {"operation": "display", "flags": {"json": True}},
            "choice": {"operation": "create_scratch", "flags": {"edition": "ultimate"}},
            "integer": {"operation": "create_scratch", "flags": {"duration-days": "7"}},
            "one of": {"operation": "resume_scratch", "flags": {}},
            "clone source": {"operation": "clone_sandbox", "flags": {"name": "uat2"}},
            "create is not clone": {"operation": "create_sandbox", "flags": {"source-sandbox-name": "uat"}},
            "two definitions": {"operation": "create_scratch", "definition": {"edition": "Developer"}, "flags": {"definition-file": "/x.json"}},
            "definition elsewhere": {"operation": "create_sandbox", "definition": {"edition": "Developer"}},
            "client id alone": {"operation": "create_scratch", "flags": {"client-id": "3MVG9app"}},
            "secret alone": {"operation": "create_scratch", "connected_app_secret": "hub_app"},
            "sandbox name": {"operation": "sandbox_status", "sandbox_name": "uat' OR Name != '"},
            "status flags": {"operation": "sandbox_status", "sandbox_name": "uat", "flags": {"all": True}},
        }
        for label, params in refusals.items():
            with self.subTest(label=label):
                refused = self.plugin.org_lifecycle(params, STATE)
                self.assertEqual(refused["error"]["code"], "sf.invalid_params", refused)
        reason = self.plugin.org_lifecycle({"operation": "create_scratch", "flags": {"wait": 10}}, STATE)["error"]["message"]
        self.assertIn("--wait is set by the plugin", reason)
        self.assertEqual(self.cli.calls, [])


class ListDisplayOpen(Lifecycle):
    def test_list_returns_describing_fields_only_with_every_bound_key_present(self) -> None:
        row = {
            "accessToken": REDACTED_TOKEN,
            "password": "[REDACTED] Use 'sf org auth show-user-password' to view",
            "instanceUrl": "https://acme-hub.my.salesforce.com",
            "orgId": HUB_ID,
            "username": "admin@acme-hub.com",
            "loginUrl": "https://login.salesforce.com",
            "clientId": "3MVG9fixtureConsumerKey",
            "isDevHub": True,
            "instanceApiVersion": "62.0",
            "isSandbox": False,
            "isScratch": False,
            "privateKey": str(self.hub_key),
            "alias": "hub",
            "aliases": ["hub"],
            "isDefaultUsername": False,
            "isDefaultDevHubUsername": True,
            "defaultMarker": "(D)",
            "connectedStatus": "Connected",
            "lastUsed": "2026-11-20T12:00:00.000Z",
        }
        scratch = {
            "accessToken": REDACTED_TOKEN,
            "orgId": SCRATCH_ID,
            "username": SCRATCH_USER,
            "instanceUrl": f"https://{SCRATCH_HOST}",
            "devHubUsername": "admin@acme-hub.com",
            "expirationDate": "2026-11-27",
            "status": "Active",
            "orgName": "Acme Scratch",
            "edition": "Developer",
            "isExpired": False,
            "namespace": None,
            "createdBy": "admin@acme-hub.com",
            "createdDate": "2026-11-20T12:30:00.000+0000",
            "signupUsername": SCRATCH_USER,
            "isScratch": True,
            "tracksSource": True,
            "alias": "s1",
        }
        self.cli.on("org", "list", out={"status": 0, "result": {"other": [], "sandboxes": [], "nonScratchOrgs": [row], "devHubs": [row], "scratchOrgs": [scratch]}, "warnings": ["Secrets are now hidden from 'sf org list' command output."]})
        result = self.ok("org_lifecycle", {"operation": "list", "flags": {"all": True, "skip-connection-status": True}})
        call = self.last("org", "list")
        self.assertEqual(call["argv"][3:], ["--all", "--skip-connection-status", "--no-prompt", "--json"])
        self.assertEqual(call["files"][str(self.hub_key)][0], PEM + "\n", "a JWT Dev Hub's scratch orgs are read with its key present")
        self.assertEqual(result["devHubs"][0]["alias"], "hub")
        self.assertEqual(result["scratchOrgs"][0]["expirationDate"], "2026-11-27")
        printed = json.dumps(result)
        for absent in ("accessToken", "password", "privateKey", "clientId", "REDACTED"):
            self.assertNotIn(absent, printed)
        self.assertFalse(self.hub_key.exists())

    def test_list_goes_on_without_a_key_the_vault_no_longer_holds(self) -> None:
        del self.vault.values[VAULT_KEY_ORG_SECRET_PREFIX + "hub_key"]
        self.cli.on("org", "list", out={"status": 0, "result": {"other": [], "sandboxes": [], "nonScratchOrgs": [], "devHubs": [], "scratchOrgs": []}})
        with self.assertLogs("salesforce_plugin.org_secrets", level="WARNING"):
            self.ok("org_lifecycle", {"operation": "list", "flags": {}})
        self.assertIsNone(self.last("org", "list")["files"][str(self.hub_key)])

    def test_display_of_a_scratch_org_names_it_and_drops_the_auth_envelope(self) -> None:
        self.bind_scratch()
        result = self.ok("org_lifecycle", {"operation": "display", "org": "s1", "flags": {"api-version": "62.0", "verbose": True}})
        shown = result["org_display"]
        self.assertEqual((shown["id"], shown["status"], shown["expirationDate"], shown["devHubId"], result["org"]), (SCRATCH_ID, "Active", "2026-11-27", "admin@acme-hub.com", "s1"))
        self.assertNotIn("accessToken", shown)
        self.assertNotIn("clientId", shown)
        self.assertEqual(self.last("org", "display")["argv"][3:], ["--api-version=62.0", "--verbose", "--target-org", SCRATCH_USER, "--json"])

    def test_open_launches_the_browser_or_returns_a_single_use_link(self) -> None:
        frontdoor = "https://acme.my.salesforce.com/secur/frontdoor.jsp?otp=fixtureOneTimeCode&cshc=fixture"
        self.cli.on("org", "open", out={"status": 0, "result": {"orgId": PROD_ID, "url": frontdoor, "username": "admin@acme.com"}, "warnings": []})
        opened = self.ok("org_lifecycle", {"operation": "open", "org": "prod", "flags": {"path": "lightning/setup/SetupOneHome/home", "browser": "chrome"}})
        self.assertEqual((opened.get("opened"), "url" in opened), (True, False))
        self.assertEqual(self.last("org", "open")["argv"][3:], ["--path=lightning/setup/SetupOneHome/home", "--browser=chrome", "--target-org", "admin@acme.com", "--json"])
        link = self.ok("org_lifecycle", {"operation": "open", "org": "prod", "flags": {"url-only": True, "path": "-lightning"}})
        self.assertEqual(link["url"], frontdoor)
        self.assertIn("single-use", link["url_note"])
        # A value that starts with '-' cannot be read as another flag.
        self.assertIn("--path=-lightning", self.last("org", "open")["argv"])


class ScratchOrgs(Lifecycle):
    def test_create_submits_async_from_the_dev_hub_with_an_inline_definition(self) -> None:
        self.cli.on("org", "create", "scratch", out={"status": 0, "result": {"username": SCRATCH_USER, "scratchOrgInfo": scratch_info("New"), "warnings": []}, "warnings": []})
        definition = {"orgName": "Acme Scratch", "edition": "Developer", "features": ["EnableSetPasswordInApi"], "settings": {"lightningExperienceSettings": {"enableS1DesktopEnabled": True}}}
        flags = {"alias": "s1", "duration-days": 7, "track-source": False, "no-namespace": True, "release": "preview", "admin-email": "ops@acme.com", "description": "build 6 smoke", "client-id": "3MVG9scratchApp"}
        result = self.ok("org_lifecycle", {"operation": "create_scratch", "definition": definition, "connected_app_secret": "hub_app", "flags": flags})
        call = self.last("org", "create", "scratch")
        argv = call["argv"]
        self.assertEqual(argv[4:12], ["--alias=s1", "--duration-days=7", "--no-track-source", "--no-namespace", "--release=preview", "--admin-email=ops@acme.com", "--description=build 6 smoke", "--client-id=3MVG9scratchApp"])
        self.assertIn("--async", argv)
        self.assertEqual(argv[argv.index("--target-dev-hub") + 1], "admin@acme-hub.com", "the only bound Dev Hub is chosen when org is omitted")
        definition_path = next(item.split("=", 1)[1] for item in argv if item.startswith("--definition-file="))
        self.assertFalse(Path(definition_path).exists(), "the inline definition file is removed after the call")
        self.assertEqual(call["input"], CLIENT_SECRET + "\n")
        self.assertEqual(call["files"][str(self.hub_key)][0], PEM + "\n")
        self.assertEqual((result["job_id"], result["scratch_org_info"]["Status"], result["org"]), (JOB_ID, "New", "hub"))
        self.assertIn("resume_scratch", result["next"])
        printed = json.dumps(result) + " ".join(argv)
        for form in variants(CLIENT_SECRET) + variants(PEM):
            self.assertNotIn(form, printed)

    def test_the_inline_definition_reaches_the_cli_as_a_file(self) -> None:
        seen: dict[str, Any] = {}
        original = self.cli.__call__

        def capture(argv: list[str], **kwargs: Any) -> Any:
            for item in argv:
                if item.startswith("--definition-file="):
                    seen["definition"] = json.loads(Path(item.split("=", 1)[1]).read_text(encoding="utf-8"))
            return original(argv, **kwargs)

        self.cli.on("org", "create", "scratch", out={"status": 0, "result": {"username": SCRATCH_USER, "scratchOrgInfo": scratch_info("New"), "warnings": []}})
        definition = {"edition": "Enterprise", "hasSampleData": True}
        queued = self.plugin.org_lifecycle({"operation": "create_scratch", "org": "hub", "definition": definition}, STATE)
        with patch("salesforce_plugin.client.subprocess.run", side_effect=capture), patch.object(async_jobs, "ensure_worker_started"):
            async_jobs._process_job(self.plugin, self.jobs, queued["data"]["job_id"], "org_lifecycle")  # noqa: SLF001
        self.assertEqual(seen["definition"], definition)

    def test_resume_reports_not_ready_then_binds_the_org_with_the_hub_key(self) -> None:
        self.cli.on(
            "org",
            "resume",
            "scratch",
            out={"name": "StillInProgressError", "message": "The scratch org isn't ready yet (Status = Creating).", "exitCode": 1, "actions": ["Wait for a few minutes, and then try the command again"], "context": "OrgResumeScratch", "status": 1, "commandName": "OrgResumeScratch", "warnings": []},
            code=1,
        )
        waiting = self.ok("org_lifecycle", {"operation": "resume_scratch", "flags": {"job-id": JOB_ID}})
        self.assertEqual((waiting["ready"], waiting["actions"]), (False, ["Wait for a few minutes, and then try the command again"]))
        self.assertIn("Status = Creating", waiting["message"])
        self.assertEqual(self.last("org", "resume", "scratch")["argv"][4:], [f"--job-id={JOB_ID}", "--wait=0", "--json"])
        ready = self.bind_scratch()
        binding = ready["binding"]
        self.assertEqual((ready["ready"], ready["org"], ready["dev_hub"]), (True, "s1", "hub"))
        self.assertEqual((binding["org_type"], binding["is_dev_hub"], binding["dev_hub_org_id"], binding["jwt_key_secret"], binding["login_method"]), ("scratch", False, HUB_ID, "hub_key", "scratch_create"))
        self.assertFalse(any("ScratchOrgInfo" in call["soql"] and SCRATCH_USER in call["argv"] for call in self.cli.calls), "a scratch org is never probed as a Dev Hub")
        self.assertNotIn("authFields", json.dumps(ready))

    def test_delete_removes_the_org_and_its_binding_but_keeps_the_hub_key(self) -> None:
        self.bind_scratch()
        self.cli.on("org", "delete", "scratch", out={"status": 0, "result": {"username": SCRATCH_USER, "orgId": SCRATCH_ID}, "warnings": []})
        result = self.ok("org_lifecycle", {"operation": "delete_scratch", "org": "s1"})
        self.assertEqual(self.last("org", "delete", "scratch")["argv"][4:], ["--no-prompt", "--target-org", SCRATCH_USER, "--json"])
        self.assertEqual((result["deleted"], result["binding_removed"], result["secrets_deleted"], result["org"]), (True, True, [], "s1"))
        self.assertEqual([binding.alias for binding in self.index().bindings], ["hub", "prod"])
        self.assertIn(VAULT_KEY_ORG_SECRET_PREFIX + "hub_key", self.vault.values)

    def test_cli_failures_carry_name_message_and_next_step(self) -> None:
        self.cli.on(
            "org",
            "create",
            "scratch",
            out={
                "name": "SignupFailedError",
                "message": "The request to create a scratch org failed with error code: C-1007.",
                "exitCode": 1,
                "actions": ["See https://developer.salesforce.com/docs/atlas.en-us.sfdx_dev.meta/sfdx_dev/sfdx_dev_scratch_orgs_error_codes.htm for information on error code C-1007."],
                "status": 1,
            },
            code=1,
        )
        error = self.failed("org_lifecycle", {"operation": "create_scratch", "org": "hub", "flags": {"edition": "developer"}})
        self.assertEqual(error["code"], "sf.api_error")
        self.assertIn("SignupFailedError: The request to create a scratch org failed with error code: C-1007.", error["message"])
        self.assertIn("Next: See https://developer.salesforce.com", error["message"])
        self.cli.on("org", "delete", "scratch", out={"name": "NamedOrgNotFoundError", "message": "No authorization information found for s1.", "exitCode": 1, "status": 1}, code=1)
        self.bind_scratch()
        self.assertEqual(self.failed("org_lifecycle", {"operation": "delete_scratch", "org": "s1"})["code"], "sf.auth_failed")

    def test_a_failure_that_carried_the_client_secret_returns_only_the_error_name(self) -> None:
        echo = f"Unable to authorize with client secret {CLIENT_SECRET}"
        self.cli.on("org", "create", "scratch", out={"name": "AuthCodeExchangeError", "message": echo, "exitCode": 1, "actions": [echo], "status": 1}, code=1)
        error = self.failed("org_lifecycle", {"operation": "create_scratch", "org": "hub", "connected_app_secret": "hub_app", "flags": {"client-id": "3MVG9scratchApp"}})
        self.assertEqual(error["code"], "sf.api_error")
        self.assertIn("AuthCodeExchangeError", error["message"])
        for form in variants(CLIENT_SECRET):
            self.assertNotIn(form, json.dumps(error))

    def test_without_a_dev_hub_the_message_lists_none(self) -> None:
        self.cli.on("org", "logout", out={"status": 0, "result": ["admin@acme-hub.com"]})
        self.ok("disconnect_org", {"org": "hub"})
        error = self.failed("org_lifecycle", {"operation": "create_scratch", "flags": {"edition": "developer"}})
        self.assertEqual(error["code"], "sf.unknown_org")
        self.assertIn("Bound Dev Hubs: none", error["message"])


class Sandboxes(Lifecycle):
    def test_create_clone_and_refresh_are_submitted_async_on_the_production_org(self) -> None:
        self.cli.on("org", "create", "sandbox", out={"status": 0, "result": sandbox_process("Pending", progress=0), "warnings": []})
        self.cli.on("org", "refresh", "sandbox", out={"status": 0, "result": sandbox_process("Pending", progress=0), "warnings": []})
        created = self.ok("org_lifecycle", {"operation": "create_sandbox", "org": "prod", "flags": {"name": "uat", "license-type": "Developer", "alias": "uat", "no-track-source": True}})
        self.assertEqual(self.last("org", "create", "sandbox")["argv"][4:], ["--name=uat", "--license-type=Developer", "--alias=uat", "--no-track-source", "--async", "--no-prompt", "--target-org", "admin@acme.com", "--json"])
        self.assertEqual((created["sandbox_process"]["Status"], created["sandbox_process"]["SandboxUsername"], created["org"]), ("Pending", SANDBOX_USER, "prod"))
        self.assertNotIn("attributes", created["sandbox_process"])
        self.assertIn("resume_sandbox", created["next"])
        self.ok("org_lifecycle", {"operation": "clone_sandbox", "org": "prod", "flags": {"name": "uat2", "source-sandbox-name": "uat"}})
        self.assertEqual(self.last("org", "create", "sandbox")["argv"][4:6], ["--name=uat2", "--source-sandbox-name=uat"])
        self.ok("org_lifecycle", {"operation": "refresh_sandbox", "org": "prod", "flags": {"name": "uat", "no-auto-activate": True}})
        self.assertEqual(self.last("org", "refresh", "sandbox")["argv"][4:], ["--name=uat", "--no-auto-activate", "--async", "--no-prompt", "--target-org", "admin@acme.com", "--json"])

    def test_resume_is_not_ready_on_exit_68_then_binds_the_completed_sandbox(self) -> None:
        self.cli.on("org", "resume", "sandbox", out={"status": 68, "result": sandbox_process("Processing", progress=40, message="Sandbox status is Processing; timed out waiting for completion."), "warnings": []}, code=68)
        waiting = self.ok("org_lifecycle", {"operation": "resume_sandbox", "org": "prod", "flags": {"name": "uat"}})
        self.assertEqual((waiting["ready"], waiting["sandbox_process"]["CopyProgress"]), (False, 40))
        self.assertEqual(self.last("org", "resume", "sandbox")["argv"][4:], ["--name=uat", "--wait=0", "--target-org", "admin@acme.com", "--json"])
        self.cli.on("org", "resume", "sandbox", out={"status": 0, "result": sandbox_process("Completed", progress=100, org_id=SANDBOX_ID), "warnings": []})
        script_org(self.cli, SANDBOX_ID, SANDBOX_USER, "acme--uat.sandbox.my.salesforce.com", sandbox=True)
        ready = self.ok("org_lifecycle", {"operation": "resume_sandbox", "org": "prod", "alias": "uat", "flags": {"name": "uat"}})
        binding = ready["binding"]
        self.assertEqual((ready["ready"], ready["org"], ready["production_org"]), (True, "uat", "prod"))
        self.assertEqual((binding["org_type"], binding["username"], binding["login_method"]), ("sandbox", SANDBOX_USER, "sandbox_create"))

    def test_status_reads_sandbox_process_through_tooling(self) -> None:
        body = {"size": 1, "totalSize": 1, "done": True, "queryLocator": None, "entityTypeName": "SandboxProcess", "records": [sandbox_process("Processing", progress=55)]}
        self.cli.on("api", "request", "rest", out=transcript(200, body))
        result = self.ok("org_lifecycle", {"operation": "sandbox_status", "org": "prod", "sandbox_name": "uat"})
        argv = self.last("api", "request", "rest")["argv"]
        target = urlsplit(argv[4])
        self.assertEqual(target.path, "/services/data/v62.0/tooling/query")
        soql = parse_qs(target.query)["q"][0]
        self.assertIn("FROM SandboxProcess WHERE SandboxName = 'uat' ORDER BY CreatedDate DESC", soql)
        self.assertEqual(argv[argv.index("--target-org") + 1], "admin@acme.com")
        self.assertEqual((result["status"], result["latest"]["CopyProgress"], len(result["sandbox_processes"]), result["org"]), (200, 55, 1, "prod"))

    def test_delete_removes_a_bound_sandbox(self) -> None:
        script_org(self.cli, SANDBOX_ID, SANDBOX_USER, "acme--uat.sandbox.my.salesforce.com", sandbox=True)
        self.ok("bind_org", {"target_org": SANDBOX_USER, "alias": "uat"})
        self.cli.on("org", "delete", "sandbox", out={"status": 0, "result": {"username": SANDBOX_USER, "orgId": SANDBOX_ID}, "warnings": []})
        result = self.ok("org_lifecycle", {"operation": "delete_sandbox", "org": "uat"})
        self.assertEqual(self.last("org", "delete", "sandbox")["argv"][4:], ["--no-prompt", "--target-org", SANDBOX_USER, "--json"])
        self.assertEqual((result["deleted"], result["org"], result["cli_result"]), (True, "uat", {"username": SANDBOX_USER, "orgId": SANDBOX_ID}))
        self.assertEqual([binding.alias for binding in self.index().bindings], ["hub", "prod"])

    def test_every_lifecycle_call_runs_with_secrets_hidden(self) -> None:
        self.cli.on("org", "create", "sandbox", out={"status": 0, "result": sandbox_process("Pending", progress=0)})
        self.ok("org_lifecycle", {"operation": "create_sandbox", "org": "prod", "flags": {"name": "uat", "license-type": "Developer"}})
        self.assertTrue(self.cli.calls)
        self.assertTrue(all(call["env"]["SF_TEMP_SHOW_SECRETS"] == "false" for call in self.cli.calls))


if __name__ == "__main__":
    unittest.main()
