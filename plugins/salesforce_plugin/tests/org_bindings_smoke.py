"""Offline smoke for build 6's bound orgs: the vault index, org selection, login, logout and disconnect.

Every test drives the real plugin, its job worker and the real
``SalesforceCliExecutor`` against a fake ``sf`` CLI and an in-memory vault
(``org_fixtures``). No sf executable, no network and no Salesforce org are
needed.

Secrets (a PEM key, an SFDX auth URL, an access token) are checked absent in
their raw, percent-encoded (upper- and lower-case hex), form-encoded, base64,
URL-safe base64 and JSON-escaped forms from job results, job errors, log
records, every argv, the job ledger's request payloads and the vault index;
each reaches the CLI only through its documented channel (a 0600 key file
during the call, standard input, or the environment of that one call).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from org_fixtures import (  # noqa: E402
    ACCESS_TOKEN,
    DOCUMENTED_FLAGS,
    HUB_ID,
    PEM,
    PROD_ID,
    REDACTED_TOKEN,
    SANDBOX_ID,
    SECRETS_WARNING,
    SFDX_URL,
    STATE,
    FakeVault,
    Harness,
    assert_every_flag_reaches_the_cli,
    script_org,
    variants,
)
from salesforce_plugin.constants import VAULT_KEY_ORG_SECRET_PREFIX, VAULT_KEY_ORGS_INDEX  # noqa: E402
from salesforce_plugin.org_service import LOGIN_COMMANDS, LOGOUT_COMMAND  # noqa: E402
from salesforce_plugin.orgs import OrgBinding, OrgIndex, OrgIndexState  # noqa: E402


class RegistryState(unittest.TestCase):
    """OrgIndexState: the selection rules, defaults and secret ownership."""

    def binding(self, org_id: str, alias: str, **extra: Any) -> OrgBinding:
        return OrgBinding(org_id=org_id, alias=alias, target_org=f"{alias}@acme.com", username=f"{alias}@acme.com", instance_host=f"{alias}.my.salesforce.com", **extra)

    def test_no_binding_says_how_to_bind(self) -> None:
        with self.assertRaises(Exception) as caught:
            OrgIndexState().select(None)
        self.assertEqual(getattr(caught.exception, "code", ""), "sf.not_configured")
        self.assertIn("bind_org", str(caught.exception))

    def test_one_binding_needs_no_org_and_matches_every_name(self) -> None:
        state = OrgIndexState().with_binding(self.binding(PROD_ID, "Prod"), make_default=False)
        self.assertEqual(state.default_org_id, PROD_ID)
        for selector in (None, "prod", "PROD", "prod@acme.com", PROD_ID, PROD_ID[:15]):
            self.assertEqual(state.select(selector).org_id, PROD_ID)

    def test_several_bindings_use_the_default_and_unknown_lists_them(self) -> None:
        state = OrgIndexState().with_binding(self.binding(PROD_ID, "prod"), make_default=False).with_binding(self.binding(SANDBOX_ID, "uat"), make_default=False)
        self.assertEqual(state.select(None).alias, "prod")
        state, chosen = state.with_default("uat")
        self.assertEqual((chosen.alias, state.select(None).alias), ("uat", "uat"))
        with self.assertRaises(Exception) as caught:
            state.select("qa")
        self.assertEqual(getattr(caught.exception, "code", ""), "sf.unknown_org")
        self.assertIn("prod (", str(caught.exception))
        self.assertIn("uat (", str(caught.exception))
        self.assertIn("[default]", str(caught.exception))

    def test_several_without_default_is_refused_with_the_list(self) -> None:
        state = OrgIndexState(bindings=(self.binding(PROD_ID, "prod"), self.binding(SANDBOX_ID, "uat")), default_org_id="")
        with self.assertRaises(Exception) as caught:
            state.select(None)
        self.assertEqual(getattr(caught.exception, "code", ""), "sf.unknown_org")
        self.assertIn("none is the default", str(caught.exception))

    def test_alias_is_unique_and_a_refresh_keeps_the_default(self) -> None:
        state = OrgIndexState().with_binding(self.binding(PROD_ID, "prod"), make_default=False)
        with self.assertRaises(ValueError):
            state.with_binding(self.binding(SANDBOX_ID, "PROD"), make_default=False)
        refreshed = state.with_binding(self.binding(PROD_ID, "prod", is_dev_hub=True), make_default=False)
        self.assertEqual((len(refreshed.bindings), refreshed.default_org_id, refreshed.bindings[0].is_dev_hub), (1, PROD_ID, True))

    def test_an_alias_may_not_name_another_binding(self) -> None:
        # Review unt_dcd93437: an alias equal to another binding's username made find() choose by tuple order.
        state = OrgIndexState().with_binding(self.binding(PROD_ID, "prod"), make_default=False)
        with self.assertRaises(ValueError) as caught:
            state.with_binding(OrgBinding(org_id=SANDBOX_ID, alias="prod@acme.com", target_org="uat@acme.com", username="uat@acme.com", instance_host="uat.my.salesforce.com"), make_default=False)
        self.assertIn("choose another alias", str(caught.exception))
        with self.assertRaises(ValueError):
            state.with_binding(OrgBinding(org_id=SANDBOX_ID, alias=PROD_ID, target_org="uat@acme.com", username="uat@acme.com", instance_host="uat.my.salesforce.com"), make_default=False)
        named_like_a_user = OrgIndexState().with_binding(OrgBinding(org_id=PROD_ID, alias="uat@acme.com", target_org="prod@acme.com", username="prod@acme.com", instance_host="prod.my.salesforce.com"), make_default=False)
        with self.assertRaises(ValueError) as caught:
            named_like_a_user.with_binding(self.binding(SANDBOX_ID, "uat"), make_default=False)
        self.assertIn("another alias first", str(caught.exception))
        for selector, org_id in (("prod", PROD_ID), ("prod@acme.com", PROD_ID)):
            self.assertEqual(state.select(selector).org_id, org_id)

    def test_removal_moves_the_default_and_keeps_shared_secrets(self) -> None:
        hub = self.binding(HUB_ID, "hub", jwt_key_secret="hub_key", secret_names=("hub_key",), is_dev_hub=True)
        scratch = self.binding(SANDBOX_ID, "scratch1", jwt_key_secret="hub_key", org_type="scratch", dev_hub_org_id=HUB_ID)
        state = OrgIndexState().with_binding(hub, make_default=False).with_binding(scratch, make_default=False)
        after, removed, orphaned = state.without("hub")
        self.assertEqual((removed.alias, after.default_org_id, orphaned), ("hub", SANDBOX_ID, ()))
        last, _removed, orphaned = after.without("scratch1")
        self.assertEqual((last.bindings, last.default_org_id, orphaned), ((), "", ("hub_key",)))


class IndexStorage(unittest.TestCase):
    def test_round_trip_store_then_rotate(self) -> None:
        vault = FakeVault()
        index = OrgIndex(vault)
        self.assertEqual(index.load(), OrgIndexState())
        state = OrgIndexState().with_binding(OrgBinding(org_id=PROD_ID, alias="prod", target_org="a@acme.com", username="a@acme.com", instance_host="acme.my.salesforce.com", secret_names=("k",)), make_default=False)
        index.save(state)
        index.save(state)
        self.assertEqual(index.load(), state)
        self.assertEqual(json.loads(vault.values[VAULT_KEY_ORGS_INDEX])["default_org_id"], PROD_ID)

    def test_unreadable_or_unwritable_index_is_an_error_not_an_empty_list(self) -> None:
        vault = FakeVault()
        vault.values[VAULT_KEY_ORGS_INDEX] = "{not json"
        with self.assertRaises(Exception) as caught:
            OrgIndex(vault).load()
        self.assertEqual(getattr(caught.exception, "code", ""), "sf.org_registry_failed")
        vault = FakeVault()
        vault.fail_writes = True
        with self.assertRaises(Exception) as caught:
            OrgIndex(vault).save(OrgIndexState())
        self.assertEqual(getattr(caught.exception, "code", ""), "sf.org_registry_failed")
        with self.assertRaises(Exception) as caught:
            OrgIndex(None).load()
        self.assertEqual(getattr(caught.exception, "code", ""), "vault_service_not_available")


class BindingAndRouting(Harness):
    def test_bind_records_what_the_cli_and_org_report(self) -> None:
        script_org(self.cli, HUB_ID, "admin@acme-hub.com", "acme-hub.my.salesforce.com", alias="hub", hub=True)
        result = self.ok("bind_org", {"target_org": "hub", "instance_host": "ACME-HUB.my.salesforce.com"})
        binding = result["binding"]
        self.assertEqual((binding["org_id"], binding["alias"], binding["username"], binding["target_org"]), (HUB_ID, "hub", "admin@acme-hub.com", "admin@acme-hub.com"))
        self.assertEqual((binding["instance_host"], binding["org_type"], binding["is_dev_hub"], binding["default"]), ("acme-hub.my.salesforce.com", "production", True, True))
        self.assertNotIn("accessToken", json.dumps(result))
        self.assertNotIn("REDACTED", json.dumps(result))
        # Facts come from the org itself, run against the bound username.
        self.assertTrue(all(call["argv"][call["argv"].index("--target-org") + 1] in ("hub", "admin@acme-hub.com") for call in self.cli.calls))
        self.assertTrue(all(call["env"].get("SF_TEMP_SHOW_SECRETS") == "false" for call in self.cli.calls))
        self.assertEqual(self.index().select(None).org_id, HUB_ID)

    def test_an_org_the_cli_does_not_know_is_an_auth_failure_with_the_login_hint(self) -> None:
        self.cli.on("org", "display", target="ghost", out={"name": "NamedOrgNotFoundError", "message": "No authorization information found for ghost.", "exitCode": 1, "status": 1, "actions": ["Ensure the alias or username for the ghost org is correct."]}, code=1)
        error = self.failed("bind_org", {"target_org": "ghost"})
        self.assertEqual(error["code"], "sf.auth_failed")
        self.assertIn("NamedOrgNotFoundError", error["message"])
        self.assertIn("sf org login web --alias ghost", error["message"])
        self.assertEqual(self.index().bindings, ())

    def test_a_pin_that_differs_is_refused(self) -> None:
        script_org(self.cli, PROD_ID, "admin@acme.com", "acme.my.salesforce.com", alias="prod")
        error = self.failed("bind_org", {"target_org": "prod", "instance_host": "other.my.salesforce.com"})
        self.assertEqual(error["code"], "sf.not_configured")
        self.assertEqual(self.index().bindings, ())

    def test_verbs_route_to_the_named_org_and_name_it(self) -> None:
        self.bind_two()
        self.cli.on("data", "get", "record", target="admin@acme.com.uat", out={"status": 0, "result": {"attributes": {"type": "Account"}, "Id": "001R000000Uat01IAB", "Name": "UAT Account"}})
        self.cli.on("data", "get", "record", target="admin@acme.com", out={"status": 0, "result": {"attributes": {"type": "Account"}, "Id": "001R000000Prd01IAB", "Name": "Prod Account"}})
        uat = self.ok("get_record", {"sobject": "Account", "id": "001R000000Uat01IAB", "org": "uat"})
        self.assertEqual((uat["record"]["Name"], uat["org"], uat["org_id"]), ("UAT Account", "uat", SANDBOX_ID))
        default = self.ok("get_record", {"sobject": "Account", "id": "001R000000Prd01IAB"})
        self.assertEqual((default["record"]["Name"], default["org"], default["org_id"]), ("Prod Account", "prod", PROD_ID))
        record_calls = [call["argv"] for call in self.cli.calls if call["argv"][1:4] == ["data", "get", "record"]]
        self.assertEqual([argv[argv.index("--target-org") + 1] for argv in record_calls], ["admin@acme.com.uat", "admin@acme.com"])

    def test_each_org_pin_is_verified_once_per_process(self) -> None:
        self.bind_two()
        self.cli.on("data", "get", "record", out={"status": 0, "result": {"Id": "001R000000Uat01IAB"}})
        before = len([call for call in self.cli.calls if call["argv"][1:3] == ["org", "display"]])
        for org in ("uat", "uat", "prod"):
            self.ok("get_record", {"sobject": "Account", "id": "001R000000Uat01IAB", "org": org})
        after = len([call for call in self.cli.calls if call["argv"][1:3] == ["org", "display"]])
        self.assertEqual(after - before, 0, "bind already verified both executors")

    def test_unknown_org_is_a_job_error_listing_the_bound_orgs(self) -> None:
        self.bind_two()
        error = self.failed("get_record", {"sobject": "Account", "id": "001R000000Uat01IAB", "org": "qa"})
        self.assertEqual(error["code"], "sf.unknown_org")
        self.assertIn("prod (", error["message"])
        self.assertIn("uat (", error["message"])

    def test_dispatch_refuses_a_malformed_org_before_any_job(self) -> None:
        for verb, params in (("set_default_org", {}), ("disconnect_org", {"org": ""}), ("logout", {"org": "prod", "flags": {"all": True}}), ("bind_org", {"alias": "x"})):
            with self.subTest(verb=verb):
                refused = getattr(self.plugin, verb)(params, STATE)
                self.assertEqual(refused["error"]["code"], "sf.invalid_params", refused)
        self.assertEqual(self.jobs.rows, {})

    def test_list_set_default_logout_and_disconnect(self) -> None:
        self.bind_two()
        listed = self.ok("list_orgs", {})
        self.assertEqual(([row["alias"] for row in listed["orgs"]], listed["default_org"], listed["count"], listed["legacy_pending_migration"]), (["prod", "uat"], "prod", 2, False))
        self.assertEqual(self.ok("set_default_org", {"org": SANDBOX_ID})["org"], "uat")
        self.assertEqual(self.index().select(None).alias, "uat")
        self.cli.on("org", "logout", out={"status": 0, "result": ["admin@acme.com.uat"], "warnings": []})
        logged_out = self.ok("logout", {"org": "uat", "flags": {"client-app": "agent-app"}})
        self.assertEqual((logged_out["logged_out_usernames"], logged_out["binding_kept"], logged_out["org"]), (["admin@acme.com.uat"], True, "uat"))
        logout_argv = self.cli.calls[-1]["argv"]
        self.assertEqual(logout_argv[1:], ["org", "logout", "--client-app=agent-app", "--target-org", "admin@acme.com.uat", "--no-prompt", "--json"])
        self.assertEqual(len(self.index().bindings), 2)
        disconnected = self.ok("disconnect_org", {"org": "uat"})
        self.assertEqual((disconnected["logout_status"], disconnected["binding_removed"], disconnected["default_org"]), ("logged_out", True, "prod"))
        self.assertEqual([binding.alias for binding in self.index().bindings], ["prod"])

    def test_disconnect_reports_a_failed_logout_and_still_unbinds(self) -> None:
        self.bind_two()
        self.cli.on("org", "logout", out={"name": "NamedOrgNotFoundError", "message": "No authorization information found for admin@acme.com.", "exitCode": 1, "status": 1}, code=1)
        result = self.ok("disconnect_org", {"org": "prod"})
        self.assertEqual((result["logout_status"], result["default_org"]), ("logout_failed_NamedOrgNotFoundError", "uat"))


class LegacyMigration(Harness):
    def legacy(self) -> None:
        self.address_book.resolve.return_value = {"action_status": "completed", "data": {"name": "salesforce_org", "entries": [{"field_type": "target_org", "value": "Branch"}, {"field_type": "instance_host", "value": "acme.my.salesforce.com"}]}}

    def test_first_call_migrates_the_entry_copy_not_move(self) -> None:
        self.legacy()
        script_org(self.cli, PROD_ID, "admin@acme.com", "acme.my.salesforce.com", alias="Branch")
        self.cli.on("data", "get", "record", out={"status": 0, "result": {"Id": "001R000000Prd01IAB"}})
        result = self.ok("get_record", {"sobject": "Account", "id": "001R000000Prd01IAB"})
        self.assertIn("Bound the org registered in the salesforce_org address-book entry as Branch", result["org_note"])
        binding = self.index().select(None)
        self.assertEqual((binding.alias, binding.target_org, binding.migrated_from_legacy, binding.login_method), ("Branch", "Branch", True, "cli"))
        self.address_book.delete.assert_not_called()
        # Once migrated, the entry is no longer read on each call.
        reads = self.address_book.resolve.call_count
        self.assertNotIn("org_note", self.ok("get_record", {"sobject": "Account", "id": "001R000000Prd01IAB"}))
        self.assertEqual(self.address_book.resolve.call_count, reads)
        self.cli.on("org", "logout", out={"status": 0, "result": ["admin@acme.com"]})
        self.assertTrue(self.ok("disconnect_org", {"org": "Branch"})["legacy_entry_deleted"])
        self.address_book.delete.assert_called_once_with(name="salesforce_org")

    def test_failed_migration_is_an_advisory_when_other_orgs_are_bound(self) -> None:
        script_org(self.cli, SANDBOX_ID, "admin@acme.com.uat", "acme--uat.sandbox.my.salesforce.com", sandbox=True)
        self.ok("bind_org", {"target_org": "admin@acme.com.uat", "alias": "uat"})
        self.legacy()
        self.cli.on("org", "display", target="Branch", out="", code=1)
        self.cli.on("data", "get", "record", out={"status": 0, "result": {"Id": "001R000000Uat01IAB"}})
        result = self.ok("get_record", {"sobject": "Account", "id": "001R000000Uat01IAB"})
        self.assertIn("could not be migrated", result["org_note"])
        self.assertEqual(result["org"], "uat")

    def test_a_legacy_alias_that_names_another_binding_says_how_to_migrate(self) -> None:
        script_org(self.cli, SANDBOX_ID, "admin@acme.com.uat", "acme--uat.sandbox.my.salesforce.com", sandbox=True)
        self.ok("bind_org", {"target_org": "admin@acme.com.uat", "alias": "branch"})
        self.legacy()
        script_org(self.cli, PROD_ID, "admin@acme.com", "acme.my.salesforce.com", alias="Branch")
        self.cli.on("data", "get", "record", out={"status": 0, "result": {"Id": "001R000000Uat01IAB"}})
        result = self.ok("get_record", {"sobject": "Account", "id": "001R000000Uat01IAB"})
        self.assertIn("could not be migrated", result["org_note"])
        self.assertIn("give that binding another alias first", result["org_note"])
        self.assertEqual([binding.alias for binding in self.index().bindings], ["branch"])

    def test_unreadable_address_book_is_unknown_not_absent(self) -> None:
        self.address_book.resolve.return_value = {"action_status": "error", "error": {"code": "state.unavailable"}}
        listed = self.ok("list_orgs", {})
        self.assertIsNone(listed["legacy_pending_migration"])
        self.assertIn("could not be read", listed["advisories"][0])


class LoginHarness(Harness):
    """Login fixtures shared by the login and secret tests; no tests of its own."""

    def jwt_login_result(self, username: str, org_id: str, host: str) -> dict[str, Any]:
        """org login jwt --json: auth fields after redactAuthFields, key path included."""
        return {
            "status": 0,
            "result": {
                "accessToken": REDACTED_TOKEN,
                "clientId": "3MVG9fixtureConsumerKey",
                "created": "1763640000000",
                "createdBy": username,
                "instanceUrl": f"https://{host}",
                "isDevHub": True,
                "loginUrl": "https://login.salesforce.com",
                "orgId": org_id,
                "privateKey": str(self.keys_dir / "x.key"),
                "username": username,
            },
            "warnings": [SECRETS_WARNING],
        }

    def store(self, name: str, value: str) -> None:
        self.vault.values[VAULT_KEY_ORG_SECRET_PREFIX + name] = value


class Login(LoginHarness):
    def test_web_login_waits_for_the_operator_then_binds(self) -> None:
        self.cli.on("org", "login", "web", out={"status": 0, "result": {"accessToken": REDACTED_TOKEN, "instanceUrl": "https://acme.my.salesforce.com", "orgId": PROD_ID, "username": "admin@acme.com", "loginUrl": "https://acme.my.salesforce.com"}})
        script_org(self.cli, PROD_ID, "admin@acme.com", "acme.my.salesforce.com", alias="prod")
        flags = {"instance-url": "https://acme.my.salesforce.com", "alias": "prod", "browser": "firefox", "scopes": "api refresh_token", "client-id": "PlatformCLI", "set-default": False, "set-default-dev-hub": True, "client-app": "agent", "username": "admin@acme.com"}
        result = self.ok("login", {"method": "web", "flags": flags, "make_default": True})
        login = self.cli.calls[0]
        self.assertEqual(login["argv"][1:4], ["org", "login", "web"])
        self.assertEqual(login["argv"][4:], ["--instance-url=https://acme.my.salesforce.com", "--alias=prod", "--browser=firefox", "--scopes=api refresh_token", "--client-id=PlatformCLI", "--set-default-dev-hub", "--client-app=agent", "--username=admin@acme.com", "--no-prompt", "--json"])
        self.assertEqual(login["timeout"], 300.0)
        self.assertEqual((result["method"], result["org"], result["binding"]["login_method"], result["binding"]["default"]), ("web", "prod", "web", True))

    def test_jwt_key_from_the_vault_exists_only_during_its_calls(self) -> None:
        self.store("ci_key", PEM)
        key_file = self.plugin._org_secrets().jwt_key_file("ci_key").path  # noqa: SLF001
        self.cli.watched.append(key_file)
        self.cli.on("org", "login", "jwt", out=self.jwt_login_result("ci@acme.com", HUB_ID, "acme-hub.my.salesforce.com"))
        script_org(self.cli, HUB_ID, "ci@acme.com", "acme-hub.my.salesforce.com", hub=True)
        result = self.ok("login", {"method": "jwt", "jwt_key_secret": "ci_key", "flags": {"username": "ci@acme.com", "client-id": "3MVG9fixtureConsumerKey", "instance-url": "https://login.salesforce.com", "alias": "ci"}})
        self.assertEqual((result["binding"]["jwt_key_secret"], result["binding"]["secret_names"], result["binding"]["is_dev_hub"]), ("ci_key", ["ci_key"], True))
        login = self.cli.calls[0]
        self.assertIn(f"--jwt-key-file={key_file}", login["argv"])
        self.assertEqual(login["files"][str(key_file)], (PEM + "\n", "0o600", "0o700"))
        # Every later call for this org has the key on disk; none outside a call.
        for call in self.cli.calls[1:]:
            self.assertEqual(call["files"][str(key_file)][0], PEM + "\n", call["argv"])
        self.assertFalse(key_file.exists())
        self.cli.on("data", "get", "record", out={"status": 0, "result": {"Id": "001R000000Hub01IAB"}})
        self.ok("get_record", {"sobject": "Account", "id": "001R000000Hub01IAB", "org": "ci"})
        self.assertEqual(self.cli.calls[-1]["files"][str(key_file)][0], PEM + "\n")
        self.assertFalse(key_file.exists())

    def test_binding_again_keeps_how_the_org_logged_in_and_a_new_login_keeps_old_secret_names(self) -> None:
        self.store("ci_key", PEM)
        self.store("ci_key_2026", PEM.replace("fixture", "rotated"))
        key_file = self.plugin._org_secrets().jwt_key_file("ci_key").path  # noqa: SLF001
        self.cli.watched.append(key_file)
        self.cli.on("org", "login", "jwt", out=self.jwt_login_result("ci@acme.com", HUB_ID, "acme-hub.my.salesforce.com"))
        script_org(self.cli, HUB_ID, "ci@acme.com", "acme-hub.my.salesforce.com", alias="ci-cli", hub=True)
        self.ok("login", {"method": "jwt", "jwt_key_secret": "ci_key", "alias": "ci", "flags": {"username": "ci@acme.com", "client-id": "3MVG9fixtureConsumerKey"}})
        refreshed = self.ok("bind_org", {"target_org": "ci@acme.com"})["binding"]
        self.assertEqual((refreshed["alias"], refreshed["login_method"], refreshed["jwt_key_secret"], refreshed["secret_names"]), ("ci", "jwt", "ci_key", ["ci_key"]))
        display_call = next(call for call in reversed(self.cli.calls) if call["argv"][1:3] == ["org", "display"])
        self.assertEqual(display_call["files"][str(key_file)][0], PEM + "\n", "the refresh reads the org with its key present")
        relogin = self.ok("login", {"method": "jwt", "jwt_key_secret": "ci_key_2026", "flags": {"username": "ci@acme.com", "client-id": "3MVG9fixtureConsumerKey"}})["binding"]
        self.assertEqual((relogin["alias"], relogin["jwt_key_secret"], relogin["secret_names"]), ("ci", "ci_key_2026", ["ci_key", "ci_key_2026"]))
        self.cli.on("org", "logout", out={"status": 0, "result": ["ci@acme.com"]})
        self.assertEqual(sorted(self.ok("disconnect_org", {"org": "ci"})["secrets_deleted"]), ["ci_key", "ci_key_2026"])

    def test_jwt_key_file_flag_is_passed_through_contained(self) -> None:
        key_path = self.imports / "server.key"
        key_path.write_text(PEM, encoding="utf-8")
        self.cli.on("org", "login", "jwt", out=self.jwt_login_result("ci@acme.com", HUB_ID, "acme-hub.my.salesforce.com"))
        script_org(self.cli, HUB_ID, "ci@acme.com", "acme-hub.my.salesforce.com", hub=True)
        result = self.ok("login", {"method": "jwt", "flags": {"username": "ci@acme.com", "client-id": "3MVG9fixtureConsumerKey", "jwt-key-file": str(key_path)}})
        self.assertIn(f"--jwt-key-file={key_path.resolve()}", self.cli.calls[0]["argv"])
        self.assertEqual((result["binding"]["jwt_key_secret"], result["binding"]["secret_names"]), ("", []))
        outside = self.workspace / "outside.key"
        outside.write_text(PEM, encoding="utf-8")
        self.assertEqual(self.failed("login", {"method": "jwt", "flags": {"username": "ci@acme.com", "client-id": "x", "jwt-key-file": str(outside)}})["code"], "sf.import_path_refused")

    def test_jwt_takes_exactly_one_key_source(self) -> None:
        self.store("ci_key", PEM)
        base = {"username": "ci@acme.com", "client-id": "3MVG9fixtureConsumerKey"}
        for params in ({"method": "jwt", "flags": base}, {"method": "jwt", "jwt_key_secret": "ci_key", "flags": {**base, "jwt-key-file": "/tmp/k"}}):
            with self.subTest(params=params):
                self.assertEqual(self.failed("login", params)["code"], "sf.invalid_params")
        self.assertEqual(self.cli.calls, [])

    def test_sfdx_url_goes_on_stdin_and_access_token_in_one_calls_env(self) -> None:
        self.store("uat_url", SFDX_URL)
        self.store("uat_token", ACCESS_TOKEN)
        login_out = {"status": 0, "result": {"accessToken": REDACTED_TOKEN, "instanceUrl": "https://acme--uat.sandbox.my.salesforce.com", "orgId": SANDBOX_ID, "username": "admin@acme.com.uat"}}
        self.cli.on("org", "login", "sfdx-url", out=login_out)
        self.cli.on("org", "login", "access-token", out=login_out)
        script_org(self.cli, SANDBOX_ID, "admin@acme.com.uat", "acme--uat.sandbox.my.salesforce.com", sandbox=True)
        self.ok("login", {"method": "sfdx_url", "sfdx_auth_url_secret": "uat_url", "alias": "uat", "flags": {"set-default": True}})
        url_login = next(call for call in self.cli.calls if call["argv"][1:4] == ["org", "login", "sfdx-url"])
        self.assertEqual(url_login["argv"][4:], ["--set-default", "--sfdx-url-stdin=-", "--no-prompt", "--json"])
        self.assertEqual(url_login["input"], SFDX_URL + "\n")
        result = self.ok("login", {"method": "access_token", "access_token_secret": "uat_token", "alias": "uat", "flags": {"instance-url": "https://acme--uat.sandbox.my.salesforce.com"}})
        token_login = next(call for call in self.cli.calls if call["argv"][1:4] == ["org", "login", "access-token"])
        self.assertEqual(token_login["env"]["SF_ACCESS_TOKEN"], ACCESS_TOKEN)
        self.assertEqual(token_login["argv"][4:], ["--instance-url=https://acme--uat.sandbox.my.salesforce.com", "--no-prompt", "--json"])
        # A second login to the same org keeps the first login's secret name for disconnect to delete.
        self.assertEqual(result["binding"]["secret_names"], ["uat_url", "uat_token"])
        others = [call for call in self.cli.calls if call is not token_login]
        self.assertTrue(all("SF_ACCESS_TOKEN" not in call["env"] for call in others))
        self.assertTrue(all(call["input"] is None for call in others if call is not url_login))

    def test_a_missing_secret_names_the_key_to_store(self) -> None:
        error = self.failed("login", {"method": "access_token", "access_token_secret": "absent", "flags": {"instance-url": "https://acme.my.salesforce.com"}})
        self.assertEqual(error["code"], "sf.not_configured")
        self.assertIn(VAULT_KEY_ORG_SECRET_PREFIX + "absent", error["message"])
        self.assertIn("store_from_file", error["message"])

    def test_every_caller_flag_reaches_the_cli_in_its_documented_form(self) -> None:
        for method, command in LOGIN_COMMANDS.items():
            assert_every_flag_reaches_the_cli(self, method, command)
        assert_every_flag_reaches_the_cli(self, "logout", LOGOUT_COMMAND)

    def test_documented_flags_are_accepted_and_plugin_flags_are_refused_with_the_reason(self) -> None:
        for method, command in LOGIN_COMMANDS.items():
            with self.subTest(method=method):
                self.assertEqual(command.documented(), DOCUMENTED_FLAGS[method])
        self.assertEqual(LOGOUT_COMMAND.documented(), DOCUMENTED_FLAGS["logout"])
        refusals = {
            "no-prompt": {"method": "web", "flags": {"no-prompt": True}},
            "sfdx-url-stdin": {"method": "sfdx_url", "sfdx_auth_url_secret": "u", "flags": {"sfdx-url-stdin": "-"}},
            "undocumented": {"method": "web", "flags": {"device-code": True}},
            "type": {"method": "web", "flags": {"set-default": "yes"}},
            "choice": {"method": "web", "flags": {"browser": "safari"}},
            "required": {"method": "access_token", "access_token_secret": "t", "flags": {}},
            "method": {"method": "client_credentials"},
            "secret name": {"method": "access_token", "access_token_secret": "../escape", "flags": {"instance-url": "https://x"}},
        }
        for label, params in refusals.items():
            with self.subTest(label=label):
                refused = self.plugin.login(params, STATE)
                self.assertEqual(refused["error"]["code"], "sf.invalid_params", refused)
        reason = self.plugin.login({"method": "web", "flags": {"no-prompt": True}}, STATE)["error"]["message"]
        self.assertIn("--no-prompt is set by the plugin", reason)


class RefreshByAnyName(LoginHarness):
    """Review unt_dcd93437: a refresh keeps the facts of the org the CLI reports, whatever name reached it."""

    def login_hub(self, *, cli_alias: str, alias: str) -> None:
        self.store("ci_key", PEM)
        self.key_file = self.plugin._org_secrets().jwt_key_file("ci_key").path  # noqa: SLF001
        self.cli.watched.append(self.key_file)
        self.cli.on("org", "login", "jwt", out=self.jwt_login_result("ci@acme.com", HUB_ID, "acme-hub.my.salesforce.com"))
        script_org(self.cli, HUB_ID, "ci@acme.com", "acme-hub.my.salesforce.com", alias=cli_alias, hub=True)
        self.ok("login", {"method": "jwt", "jwt_key_secret": "ci_key", "alias": alias, "flags": {"username": "ci@acme.com", "client-id": "3MVG9fixtureConsumerKey"}})

    def test_refresh_by_a_cli_alias_that_is_not_the_binding_alias_keeps_the_login_facts(self) -> None:
        self.login_hub(cli_alias="ci-cli", alias="ci")
        refreshed = self.ok("bind_org", {"target_org": "ci-cli"})["binding"]
        self.assertEqual((refreshed["alias"], refreshed["login_method"], refreshed["jwt_key_secret"], refreshed["secret_names"]), ("ci", "jwt", "ci_key", ["ci_key"]))
        display_call = next(call for call in reversed(self.cli.calls) if call["argv"][1:3] == ["org", "display"] and "ci-cli" in call["argv"])
        self.assertEqual(display_call["files"][str(self.key_file)][0], PEM + "\n", "the org is read with its key present before it is known")
        self.assertFalse(self.key_file.exists())

    def test_disconnect_after_a_refresh_by_cli_alias_deletes_the_secret(self) -> None:
        self.login_hub(cli_alias="ci-cli", alias="ci")
        self.ok("bind_org", {"target_org": "ci-cli"})
        self.cli.on("org", "logout", out={"status": 0, "result": ["ci@acme.com"]})
        self.assertEqual(self.ok("disconnect_org", {"org": "ci"})["secrets_deleted"], ["ci_key"])
        self.assertNotIn(VAULT_KEY_ORG_SECRET_PREFIX + "ci_key", self.vault.values)

    def test_another_org_whose_cli_alias_equals_a_binding_alias_takes_none_of_its_facts(self) -> None:
        self.login_hub(cli_alias="hub-cli", alias="uat")
        script_org(self.cli, SANDBOX_ID, "admin@acme.com.uat", "acme--uat.sandbox.my.salesforce.com", alias="uat", sandbox=True)
        bound = self.ok("bind_org", {"target_org": "uat", "alias": "sbx"})["binding"]
        self.assertEqual((bound["org_id"], bound["login_method"], bound["jwt_key_secret"], bound["secret_names"], bound["org_type"]), (SANDBOX_ID, "cli", "", [], "sandbox"))
        hub = self.index().select("uat")
        self.assertEqual((hub.org_id, hub.login_method, hub.jwt_key_secret, hub.secret_names), (HUB_ID, "jwt", "ci_key", ("ci_key",)))

    def test_a_key_the_vault_no_longer_holds_does_not_stop_binding_another_org(self) -> None:
        self.login_hub(cli_alias="ci-cli", alias="ci")
        del self.vault.values[VAULT_KEY_ORG_SECRET_PREFIX + "ci_key"]
        script_org(self.cli, SANDBOX_ID, "admin@acme.com.uat", "acme--uat.sandbox.my.salesforce.com", alias="uat", sandbox=True)
        with self.assertLogs("salesforce_plugin.org_secrets", level="WARNING") as logged:
            bound = self.ok("bind_org", {"target_org": "uat"})["binding"]
        self.assertEqual((bound["org_id"], bound["alias"], bound["login_method"]), (SANDBOX_ID, "uat", "cli"))
        display_call = next(call for call in reversed(self.cli.calls) if call["argv"][1:3] == ["org", "display"] and "uat" in call["argv"])
        self.assertIsNone(display_call["files"][str(self.key_file)], "a key the vault lacks is not written")
        self.assertIn(VAULT_KEY_ORG_SECRET_PREFIX + "ci_key", "\n".join(logged.output))


class SecretsNeverLeave(LoginHarness):
    """Each secret reaches only its own channel; no result, error, log, argv or index carries it."""

    def captured(self, action: Callable[[], dict[str, Any]]) -> tuple[dict[str, Any], str]:
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("salesforce_plugin").debug("capture start")
            outcome = action()
        return outcome, "\n".join(logs.output)

    def assert_absent(self, secret: str, *haystacks: str) -> None:
        for form in variants(secret):
            for haystack in haystacks:
                self.assertNotIn(form, haystack, f"a form of the secret leaked: {form[:12]}...")

    def everywhere(self) -> str:
        argv = "\n".join(" ".join(call["argv"]) for call in self.cli.calls)
        return argv + "\n" + "\n".join(self.vault.values.get(VAULT_KEY_ORGS_INDEX, "") for _ in (0,))

    def test_success_paths_carry_no_secret(self) -> None:
        self.store("ci_key", PEM)
        self.store("uat_url", SFDX_URL)
        self.store("uat_token", ACCESS_TOKEN)
        # A CLI that printed secrets unmasked (as with SF_TEMP_SHOW_SECRETS=true) still leaks nothing.
        leaky = self.jwt_login_result("ci@acme.com", HUB_ID, "acme-hub.my.salesforce.com")
        leaky["result"].update({"accessToken": ACCESS_TOKEN, "refreshToken": SFDX_URL, "privateKey": PEM})
        self.cli.on("org", "login", "jwt", out=leaky)
        self.cli.on("org", "login", "sfdx-url", out={"status": 0, "result": {"accessToken": ACCESS_TOKEN, "sfdxAuthUrl": SFDX_URL, "instanceUrl": "https://acme--uat.sandbox.my.salesforce.com", "orgId": SANDBOX_ID, "username": "admin@acme.com.uat"}})
        self.cli.on("org", "login", "access-token", out={"status": 0, "result": {"accessToken": ACCESS_TOKEN, "instanceUrl": "https://acme--uat.sandbox.my.salesforce.com", "orgId": SANDBOX_ID, "username": "admin@acme.com.uat"}})
        script_org(self.cli, HUB_ID, "ci@acme.com", "acme-hub.my.salesforce.com", hub=True)
        script_org(self.cli, SANDBOX_ID, "admin@acme.com.uat", "acme--uat.sandbox.my.salesforce.com", sandbox=True)
        results: list[str] = []
        logs: list[str] = []
        for params in (
            {"method": "jwt", "jwt_key_secret": "ci_key", "flags": {"username": "ci@acme.com", "client-id": "3MVG9fixtureConsumerKey"}},
            {"method": "sfdx_url", "sfdx_auth_url_secret": "uat_url", "alias": "uat"},
            {"method": "access_token", "access_token_secret": "uat_token", "alias": "uat", "flags": {"instance-url": "https://acme--uat.sandbox.my.salesforce.com"}},
        ):
            result, log_text = self.captured(lambda params=params: self.ok("login", params))
            results.append(json.dumps(result))
            logs.append(log_text)
        listed, log_text = self.captured(lambda: self.ok("list_orgs", {}))
        payloads = json.dumps(self.jobs.payloads)
        for secret in (PEM, SFDX_URL, ACCESS_TOKEN):
            self.assert_absent(secret, *results, json.dumps(listed), *logs, log_text, self.everywhere(), payloads)

    def test_failure_paths_do_not_echo_cli_text_when_the_login_carried_a_secret(self) -> None:
        self.store("ci_key", PEM)
        self.store("uat_url", SFDX_URL)
        self.store("uat_token", ACCESS_TOKEN)
        echo = f"Error authenticating with JWT config due to: invalid_grant {PEM} {SFDX_URL} {ACCESS_TOKEN}"
        failure = {"name": "JwtGrantError", "message": echo, "exitCode": 1, "status": 1, "actions": [echo]}
        for words in (("org", "login", "jwt"), ("org", "login", "sfdx-url"), ("org", "login", "access-token")):
            self.cli.on(*words, out=failure, code=1)
        errors: list[str] = []
        for params in (
            {"method": "jwt", "jwt_key_secret": "ci_key", "flags": {"username": "ci@acme.com", "client-id": "3MVG9fixtureConsumerKey"}},
            {"method": "sfdx_url", "sfdx_auth_url_secret": "uat_url"},
            {"method": "access_token", "access_token_secret": "uat_token", "flags": {"instance-url": "https://acme.my.salesforce.com"}},
        ):
            error, log_text = self.captured(lambda params=params: self.failed("login", params))
            self.assertEqual(error["code"], "sf.auth_failed")
            self.assertIn("JwtGrantError", error["message"])
            self.assertIn("not returned", error["message"])
            errors.extend([json.dumps(error), log_text])
        for secret in (PEM, SFDX_URL, ACCESS_TOKEN):
            self.assert_absent(secret, *errors, self.everywhere())
        self.assertEqual(self.index().bindings, ())
        self.assertFalse(any(self.keys_dir.glob("*.key")) if self.keys_dir.exists() else False)

    def test_a_login_without_a_plugin_secret_keeps_the_cli_message(self) -> None:
        self.cli.on("org", "login", "web", out={"name": "AuthCodeExchangeError", "message": "Error authenticating with auth code due to: expired authorization code", "exitCode": 1, "status": 1}, code=1)
        error = self.failed("login", {"method": "web"})
        self.assertEqual(error["code"], "sf.auth_failed")
        self.assertIn("expired authorization code", error["message"])

    def test_the_cli_never_runs_with_secrets_shown(self) -> None:
        self.store("uat_token", ACCESS_TOKEN)
        self.cli.on("org", "login", "access-token", out={"status": 0, "result": {"username": "admin@acme.com.uat"}})
        script_org(self.cli, SANDBOX_ID, "admin@acme.com.uat", "acme--uat.sandbox.my.salesforce.com", sandbox=True)
        with patch.dict(os.environ, {"SF_TEMP_SHOW_SECRETS": "true"}):
            self.ok("login", {"method": "access_token", "access_token_secret": "uat_token", "flags": {"instance-url": "https://acme--uat.sandbox.my.salesforce.com"}})
        self.assertTrue(self.cli.calls)
        self.assertTrue(all(call["env"]["SF_TEMP_SHOW_SECRETS"] == "false" for call in self.cli.calls))

    def test_disconnect_deletes_only_unshared_secrets_and_a_leftover_key_file_is_swept(self) -> None:
        self.store("ci_key", PEM)
        self.cli.on("org", "login", "jwt", out=self.jwt_login_result("ci@acme.com", HUB_ID, "acme-hub.my.salesforce.com"))
        script_org(self.cli, HUB_ID, "ci@acme.com", "acme-hub.my.salesforce.com", hub=True)
        self.ok("login", {"method": "jwt", "jwt_key_secret": "ci_key", "alias": "hub", "flags": {"username": "ci@acme.com", "client-id": "3MVG9fixtureConsumerKey"}})
        self.cli.on("org", "logout", out={"status": 0, "result": ["ci@acme.com"]})
        result = self.ok("disconnect_org", {"org": "hub"})
        self.assertEqual(result["secrets_deleted"], ["ci_key"])
        self.assertNotIn(VAULT_KEY_ORG_SECRET_PREFIX + "ci_key", self.vault.values)
        self.keys_dir.mkdir(parents=True, exist_ok=True)
        (self.keys_dir / "leftover.key").write_text(PEM, encoding="utf-8")
        self.make_plugin()
        self.assertEqual(list(self.keys_dir.glob("*.key")), [])


if __name__ == "__main__":
    unittest.main()
