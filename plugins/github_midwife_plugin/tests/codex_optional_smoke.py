"""Claude-only setup and explicit Codex selection across the Manager and target adapter."""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

import yaml

_ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [
    str(_ROOT / "plugins/github_midwife_plugin/src"),
    str(_ROOT / "plugins/github_midwife_plugin/tests"),
    str(_ROOT / "solet_cli/src"),
]

from executable_hydration_smoke import FakeRuntime, _request  # noqa: E402
from genesis_integration_smoke import _run_sandboxed_genesis  # noqa: E402
from github_midwife_plugin.genesis import GenesisError, _coding_agents_from_environment  # noqa: E402
from github_midwife_plugin.installation_state_doctor import _launcher_identity_valid, session_roots  # noqa: E402
from github_midwife_plugin.setup_adapter import dispatch_request  # noqa: E402
from github_midwife_plugin.setup_adapter_contract import (  # noqa: E402
    AdapterInputError,
    AdapterRequest,
    JsonObject,
)
from solet_manager.answer_validation import static_option_available  # noqa: E402
from solet_manager.completion_verifier import resolved_completion_probe_ids  # noqa: E402
from solet_manager.config import CreateConfig  # noqa: E402
from solet_manager.contract_reconciliation_chain import resolve_reconciliation_chain  # noqa: E402
from solet_manager.contracts import (  # noqa: E402
    ContractBundle,
    load_contract_reconciliations,
)
from solet_manager.errors import StateConflictError  # noqa: E402
from solet_manager.flow import initial_stage_probe_statuses  # noqa: E402
from solet_manager.models import JsonValue  # noqa: E402
from solet_manager.operation_records import operation_probe_request, operation_request  # noqa: E402
from solet_manager.plan_builder import SetupPlan, build_setup_plan  # noqa: E402
from solet_manager.preview_rendering import render_consents  # noqa: E402
from solet_manager.probe_input_projection import probe_public_inputs  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.stage_activation import (  # noqa: E402
    initial_probe_activations,
    reconcile_contract_stage_probe_state,
)
from solet_manager.transaction import Transaction  # noqa: E402

_KB = _ROOT / "plugins/github_midwife_plugin/knowledge_base"
_MCP_CONSENT = "codex_mcp_configuration_consent"


class CodexOptional(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.target = self.root / "clone"
        self.target.mkdir()
        self.runtime = FakeRuntime(self.root)
        self.bundle = ContractBundle.load(source_revision="a" * 40, directory=_KB)
        self.seed = SeedLock("example/seed", "v1", "a" * 40, "b" * 40, "c" * 64, "macos-bizops")

    def _plan(
        self,
        selected: tuple[str, ...] | None,
        *,
        recorded: dict[str, JsonValue] | None = None,
        prospective: bool = True,
        bundle: ContractBundle | None = None,
    ) -> SetupPlan:
        decisions: dict[str, JsonValue] = {"session_sources": []}
        if selected is not None:
            decisions["coding_agents"] = list(selected)
        return build_setup_plan(
            bundle=bundle or self.bundle,
            config=CreateConfig(name="iris", target=self.target, autostart=False),
            seed=self.seed,
            journal_path=self.root / "transaction.json",
            decision_selections=decisions,
            recorded_answers=recorded,
            prospective_consents=prospective,
        )

    def _transaction(self, plan: SetupPlan) -> Transaction:
        return Transaction.create(
            name="iris",
            target=self.target,
            input_fingerprint="sha256:" + "d" * 64,
            answers=plan.answers,
            seed=self.seed,
            flow_id=self.bundle.flow_id,
            flow_source_revision=self.bundle.source_revision,
            flow_contract_digest=self.bundle.contract_digest,
            stage_ids=tuple(self.bundle.stages),
            completion_probe_ids=resolved_completion_probe_ids(self.bundle, plan.answers),
        )

    def _operation_request(self, plan: SetupPlan, operation_id: str) -> AdapterRequest:
        operation = next(item for item in plan.operations if item.operation_id == operation_id)
        request = operation_request(
            self._transaction(plan),
            self.bundle,
            operation,
            phase="probe",
            probe_purpose="preview",
            approval=None,
            attempt=1,
        )
        return AdapterRequest.from_dict(cast(dict[str, object], request.to_dict()))

    def test_bizops_keeps_offering_its_shipped_codex_source(self) -> None:
        options = cast(dict[str, JsonValue], self.bundle.decisions["session_sources"]["option_source"])
        codex = cast(dict[str, dict[str, JsonValue]], options["options"])["codex_local"]
        self.assertTrue(static_option_available(codex, {"setup_profile": "macos-bizops"}))
        self.assertTrue(static_option_available(codex, {"setup_profile": "free"}))
        profile = cast(
            dict[str, object],
            yaml.safe_load((_KB / "profile_templates/macos-bizops.yaml").read_text()),
        )
        self.assertIn("codex_filesystem_session_source_plugin", cast(list[str], profile["plugins"]))

    def test_reviewed_default_is_claude_only(self) -> None:
        plan = self._plan(None)
        decisions = cast(dict[str, JsonValue], plan.answers["decisions"])
        self.assertEqual(decisions["coding_agents"], ["claude_code"])
        self.assertNotIn(_MCP_CONSENT, cast(dict[str, JsonValue], plan.answers["consents"]))

    def _assert_claude_only_plan(self, plan: SetupPlan) -> None:
        operation_ids = {item.operation_id for item in plan.operations}
        self.assertNotIn("install_node", operation_ids)
        self.assertFalse(any("codex" in item for item in operation_ids))
        self.assertNotIn(_MCP_CONSENT, cast(dict[str, JsonValue], plan.answers["consents"]))
        self.assertFalse(any(item["id"] == _MCP_CONSENT for item in cast(list[dict[str, JsonValue]], render_consents(self.bundle, plan, []))))
        completion = resolved_completion_probe_ids(self.bundle, plan.answers)
        self.assertFalse(any("codex" in item for item in completion))

    def test_claude_only_plan_genesis_shell_and_doctor(self) -> None:
        plan = self._plan(("claude_code",))
        self._assert_claude_only_plan(plan)
        genesis = self._operation_request(plan, "run_genesis")
        preview = dispatch_request(genesis, self.runtime)
        self.assertEqual(preview["checkpoint_status"], "pending")
        self.assertNotIn(
            "genesis.configure_codex_mcp_server",
            [item["id"] for item in cast(list[JsonObject], preview["planned_actions"])],
        )
        python = self.target / ".venv/bin/python3"
        python.parent.mkdir(parents=True)
        python.touch()
        applied = dispatch_request(
            replace(
                genesis,
                phase="apply",
                probe_purpose=None,
                dry_run=False,
                approval_fingerprint="sha256:" + "e" * 64,
            ),
            self.runtime,
        )
        self.assertEqual(applied["checkpoint_status"], "applied")
        self.assertEqual(self.runtime.command_environments[-1]["SOLET_CODING_AGENTS"], "claude_code")

        shell = self._operation_request(plan, "install_shell_integration")
        preview = dispatch_request(shell, self.runtime)
        paths = {str(item["target"]) for item in cast(list[JsonObject], preview["planned_actions"])}
        self.assertFalse(any("codex-" in item or "/.agents/" in item for item in paths))
        dispatch_request(
            replace(
                shell,
                phase="apply",
                probe_purpose=None,
                dry_run=False,
                approval_fingerprint="sha256:" + "e" * 64,
            ),
            self.runtime,
        )
        self.assertTrue((self.target / "client/bin/claude-iris").is_file())
        self.assertFalse((self.target / "client/bin/codex-iris").exists())
        self.assertFalse((self.target / ".agents").exists())
        self.assertFalse((self.runtime.home / ".codex").exists())
        self.assertTrue(_launcher_identity_valid(shell, ("claude_code",)))

        operation = next(item for item in plan.operations if item.operation_id == "install_shell_integration")
        _, request = operation_probe_request(
            self._transaction(plan),
            self.bundle,
            operation,
            probe_id="fresh_shell_path_valid",
            purpose="post_apply",
            attempt=1,
        )
        probe = AdapterRequest.from_dict(cast(dict[str, object], request.to_dict()))
        self.assertEqual(probe.public_inputs, {"selected_coding_agents": ["claude_code"]})
        self.assertEqual(dispatch_request(probe, self.runtime)["checkpoint_status"], "verified")
        self.assertEqual(self.runtime.commands[-1][2], 'command -v "$1" && command -v "claude-$1"')
        self.assertNotIn("codex", json.dumps(self.runtime.commands[-1]))

    def test_explicit_codex_preserves_config_artifacts_and_probe(self) -> None:
        plan = self._plan(("codex", "claude_code"))
        self.assertTrue(cast(dict[str, JsonValue], plan.answers["consents"])[_MCP_CONSENT])
        self.assertIn("install_codex_cli", {item.operation_id for item in plan.operations})
        genesis = dispatch_request(self._operation_request(plan, "run_genesis"), self.runtime)
        self.assertIn(
            "genesis.configure_codex_mcp_server",
            [item["id"] for item in cast(list[JsonObject], genesis["planned_actions"])],
        )
        shell = self._operation_request(plan, "install_shell_integration")
        dispatch_request(
            replace(
                shell,
                phase="apply",
                probe_purpose=None,
                dry_run=False,
                approval_fingerprint="sha256:" + "e" * 64,
            ),
            self.runtime,
        )
        self.assertTrue((self.target / "client/bin/codex-iris").is_file())
        self.assertTrue((self.target / ".agents/plugins/marketplace.json").is_file())
        self.assertTrue(_launcher_identity_valid(shell, ("claude_code", "codex")))
        probe = _request(
            self.target,
            operation_ref="hydration::shell.probe_path",
            public_inputs=probe_public_inputs(self._transaction(plan), "hydration::shell.probe_path"),
        )
        dispatch_request(probe, self.runtime)
        self.assertEqual(
            self.runtime.commands[-1][2],
            'command -v "$1" && command -v "claude-$1" && command -v "codex-$1"',
        )

    def test_codex_only_path_requires_only_codex_launcher(self) -> None:
        plan = self._plan(("codex",))
        probe = _request(
            self.target,
            operation_ref="hydration::shell.probe_path",
            public_inputs=probe_public_inputs(self._transaction(plan), "hydration::shell.probe_path"),
        )
        dispatch_request(probe, self.runtime)
        self.assertEqual(self.runtime.commands[-1][2], 'command -v "$1" && command -v "codex-$1"')

    def test_inactive_declined_consent_does_not_survive_in_current_plan(self) -> None:
        original = copy.deepcopy(self._plan(("claude_code",)).answers)
        cast(dict[str, JsonValue], original["consents"])[_MCP_CONSENT] = False
        plan = self._plan(("claude_code",), recorded=original, prospective=False)
        self.assertNotIn(_MCP_CONSENT, cast(dict[str, JsonValue], plan.answers["consents"]))
        self.assertNotIn(_MCP_CONSENT, plan.unresolved_consents)
        self.assertIs(cast(dict[str, JsonValue], original["consents"])[_MCP_CONSENT], False)

    def test_selected_declined_consent_stays_required(self) -> None:
        original = copy.deepcopy(self._plan(("codex",)).answers)
        cast(dict[str, JsonValue], original["consents"])[_MCP_CONSENT] = False
        plan = self._plan(("codex",), recorded=original, prospective=False)
        self.assertIn(_MCP_CONSENT, plan.unresolved_consents)
        self.assertIs(cast(dict[str, JsonValue], plan.answers["consents"])[_MCP_CONSENT], False)

    def test_unresolved_selection_keeps_consent_required(self) -> None:
        flow = copy.deepcopy(self.bundle.flow)
        cast(dict[str, dict[str, JsonValue]], flow["decisions"])["coding_agents"]["recommended_option_refs"] = []
        plan = self._plan(None, bundle=replace(self.bundle, flow=flow), prospective=False)
        self.assertIn("coding_agents", plan.unresolved_decisions)
        self.assertIn(_MCP_CONSENT, plan.unresolved_consents)

    def test_absent_codex_roots_stay_verified_when_explicitly_probed(self) -> None:
        request = _request(self.target, operation_ref="hydration::sessions.probe_codex_roots", public_inputs={})
        self.assertEqual(session_roots(request, self.runtime)["checkpoint_status"], "verified")
        self.assertFalse((self.runtime.home / ".codex").exists())

    def test_sandboxed_birth_does_not_touch_unselected_codex_config(self) -> None:
        for selected in (("claude_code",), ("claude_code", "codex")):
            with self.subTest(selected=selected):
                root = self.root / selected[-1]
                root.mkdir()
                config = root / "home/.codex/config.toml"
                if "codex" not in selected:
                    config.parent.mkdir(parents=True)
                    config.write_text("operator-owned malformed Codex config\n")
                _run_sandboxed_genesis(root, autostart=False, coding_agents=selected)
                if "codex" in selected:
                    self.assertIn("[mcp_servers.testhum]", config.read_text())
                else:
                    self.assertEqual(config.read_text(), "operator-owned malformed Codex config\n")

    def test_cli_selection_and_malformed_request_integrity(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_coding_agents_from_environment(), ("claude_code",))
        with patch.dict(os.environ, {"SOLET_CODING_AGENTS": "codex,claude_code"}):
            self.assertEqual(_coding_agents_from_environment(), ("codex", "claude_code"))
        with (
            patch.dict(os.environ, {"SOLET_CODING_AGENTS": "codex,codex"}),
            self.assertRaises(GenesisError),
        ):
            _coding_agents_from_environment()
        with self.assertRaises(AdapterInputError):
            _request(self.target, public_inputs={"selected_coding_agents": ["codex", "codex"]})
        for invalid in ([], ["unknown"], "codex"):
            with self.subTest(invalid=invalid):
                request = _request(self.target, public_inputs={"selected_coding_agents": invalid})
                self.assertEqual(dispatch_request(request, self.runtime)["error_kind"], "adapter_protocol_error")

    def test_missing_selection_is_advisory_and_uses_reviewed_default(self) -> None:
        request = _request(self.target, public_inputs={})
        with self.assertLogs("github_midwife_plugin.setup_operations", level="WARNING"):
            preview = dispatch_request(request, self.runtime)
        self.assertNotEqual(preview["checkpoint_status"], "blocked")
        self.assertFalse(any("codex-" in str(item["target"]) or "/.agents/" in str(item["target"]) for item in cast(list[JsonObject], preview["planned_actions"])))

    def test_unresolved_probe_selection_warns_and_uses_claude_default(self) -> None:
        transaction = self._transaction(self._plan(("claude_code",)))
        selections: tuple[JsonValue, ...] = (None, [], ["codex", "codex"], ["unknown"], "codex")
        for selection in selections:
            answers = copy.deepcopy(transaction.answers)
            decisions = cast(dict[str, JsonValue], answers["decisions"])
            decisions.pop("coding_agents")
            if selection is not None:
                decisions["coding_agents"] = selection
            pending = transaction.with_answers(answers)
            for probe_ref in ("hydration::shell.probe_path", "setup::coding_agents.verify_plugins", "setup::coding_agents.verify_hooks", "plugin::agent_messaging_plugin.peer_identity"):
                with self.subTest(selection=selection, probe_ref=probe_ref), self.assertLogs("solet_manager.probe_input_projection", level="WARNING") as captured:
                    self.assertEqual(probe_public_inputs(pending, probe_ref), {"selected_coding_agents": ["claude_code"]})
                self.assertIn("reviewed Claude-only default", captured.output[0])

    def test_forward_reconciliation_preserves_recorded_agents_and_consent(self) -> None:
        old_digest = "sha256:ca8c9b246b4aed63c5d39582e14fbe87b899d29ccd172cb3bf384839cdaceb5d"
        directory = _ROOT / "solet_cli/tests/fixtures/reconciliation_operation_status_reset" / old_digest.removeprefix("sha256:")
        declarations = load_contract_reconciliations()
        for revision in (
            "ebb5a8a2c7e8403e8bbd91d61b1da9f3a7b72044",
            "20afa263280c31f11958df0f9c89d4250460d665",
            "ece0927b9d6cbebfff3cd03afa3282af8e141f01",
            "81b725eb649552024fee35dab1bbb5e06a9cc9a8",
        ):
            source = ContractBundle.load(source_revision=revision, directory=directory, expected_digest=old_digest)
            for selected, granted in ((("claude_code",), False), (("claude_code", "codex"), True)):
                with self.subTest(revision=revision, selected=selected):
                    plan = self._plan(selected)
                    answers = copy.deepcopy(plan.answers)
                    answers["flow_source_revision"] = revision
                    cast(dict[str, JsonValue], answers["public_inputs"])["repository_ref"] = revision
                    cast(dict[str, JsonValue], answers["consents"])[_MCP_CONSENT] = granted
                    before = Transaction.create(
                        name="iris",
                        target=self.target,
                        input_fingerprint="sha256:" + "d" * 64,
                        answers=answers,
                        seed=replace(self.seed, commit=revision),
                        flow_id=source.flow_id,
                        flow_source_revision=revision,
                        flow_contract_digest=source.contract_digest,
                        stage_ids=tuple(source.stages),
                        completion_probe_ids=resolved_completion_probe_ids(source, answers),
                        stage_probe_statuses=initial_stage_probe_statuses(source, answers),
                        probe_activations=initial_probe_activations(source, answers),
                    )
                    bridge = resolve_reconciliation_chain(before, self.bundle.contract_digest, declarations)
                    self.assertFalse(bridge.stage_probe_mappings or bridge.operation_statuses_to_reset or bridge.answer_value_migrations)
                    after = reconcile_contract_stage_probe_state(self.bundle, before, bridge)
                    self.assertEqual(after.answers, before.answers)
                    self.assertEqual(after.stages, before.stages)
                    current = self._plan(None, recorded=after.answers, prospective=False)
                    consents = cast(dict[str, JsonValue], current.answers["consents"])
                    if granted:
                        self.assertIs(consents[_MCP_CONSENT], True)
                    else:
                        self.assertNotIn(_MCP_CONSENT, consents)
        unknown = replace(
            self._transaction(self._plan(("claude_code",))),
            flow_source_revision="f" * 40,
            flow_contract_digest=old_digest,
        )
        with self.assertRaises(StateConflictError):
            resolve_reconciliation_chain(unknown, self.bundle.contract_digest, declarations)


if __name__ == "__main__":
    unittest.main()
