#!/usr/bin/env python3
"""Tier U: real shared selection/store dispatch, reuse and invalidation."""

# ruff: noqa: E402
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from quality_gates import run_smokes
from quality_gates.smoke_identity import FrozenTree, freeze_request
from quality_gates.smoke_pass_store import SmokePassStore, clean_observation
from quality_gates.smoke_proof import TRUNCATED_OUTPUT_MARKER, _observation, execute_registered, finish_full_proofs, main, prepare_full_proofs, run_selected_smokes, select_smokes
from quality_gates.tests._focused_smoke_selector_fixture import FrozenProofFixture

# A deliberately small retained tail, so a short child overflows it.
_SMALL_TAIL_BYTES = "64"


class _LoadMonitor:
    def __enter__(self) -> _LoadMonitor:
        return self

    def __exit__(self, *_error: object) -> None:
        return None

    def snapshot(self) -> dict[str, object]:
        return {}


class ContractTests(unittest.TestCase):
    def test_clean_reuse_and_closure_key_invalidation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            store = SmokePassStore(fixture.root / "proofs")
            calls: list[list[str]] = []

            def execute(entries: list[str]) -> dict[str, Any]:
                calls.append(entries)
                return {"exit_code": 0, "import_environment": {"state": "verified"}, "smoke_results": {smoke: {"verdict": "passed", "retried": False, "seconds": 0.01, "attempts": [{"output": "3 passed", "verdict": "passed"}]} for smoke in entries}}

            with fixture.patches():
                first = run_selected_smokes(fixture.root, fixture.request(), store=store, principal="fixture", executor=execute)
                second_request = fixture.request()
                second_request["attribution"] = {"unit_id": "unrelated-candidate"}
                second = run_selected_smokes(fixture.root, second_request, store=store, principal="fixture", executor=execute)
                self.assertEqual(first["executed_count"], 1)
                self.assertEqual(second["reused_count"], 1)
                self.assertEqual(calls, [["suite/check_smoke.py"], []])
                classified = fixture.request()
                classified["known_failures"] = {"suite/check_smoke.py": "retained fixture classification"}
                known = run_selected_smokes(fixture.root, classified, store=store, principal="fixture", executor=execute)
                self.assertEqual(known["executed_count"], 1)
                self.assertEqual(known["counts"]["known_failure"], 1)
                self.assertFalse(known["proofs"][0]["proof"]["reusable"])
                old_key = first["selection"]["selected"][0]["key"]
                fixture.sources["suite/leaf.py"] = b"VALUE = 'data/input.csv'\n# changed fixture content\n"
                fixture.candidate = FrozenTree("e" * 40, "f" * 40, fixture._entries(fixture.sources))
                fixture._write()
                changed = select_smokes(fixture.root, fixture.request(), store=store)
                self.assertNotEqual(old_key, changed["selected"][0]["key"])
                self.assertFalse(changed["selected"][0]["reusable"])

    def test_physical_full_ignores_cache_and_invalidates_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            store = SmokePassStore(fixture.root / "proofs")
            observed = {"import_environment": {"state": "verified"}, "smoke_results": {"suite/check_smoke.py": {"verdict": "passed", "attempts": [{"output": "3 passed"}]}}}
            with fixture.patches(), patch("quality_gates.smoke_identity.delta_entries", return_value=[]), patch("quality_gates.smoke_proof.SmokePassStore", return_value=store):
                request = freeze_request(fixture.root, fixture.candidate.commit, fixture.candidate.commit, repository_id="rep_7111540c-ac4a-468c-8f84-b67d8756d1d0", named_smokes=["suite/check_smoke.py"])
                clean = run_selected_smokes(fixture.root, request, store=store, principal="fixture", executor=lambda _: observed)
                key = clean["proofs"][0]["proof"]["key"]
                self.assertTrue(store.get(key)["reusable"])
                context = prepare_full_proofs(fixture.root, fixture.candidate.commit, principal="fixture", attribution={})
                self.assertTrue(context["selection"]["selected"][0]["reusable"])
                self.assertFalse(context["runtime_rows"][0]["reusable"])
                self.assertFalse(store.get(key)["reusable"])
                with self.assertRaises(ValueError):
                    store.reinstate(key, current_key=key, expected_version=3, principal="fixture", reason="cannot revive prior pass during work")
                red = {"smoke_results": [{"smoke": "suite/check_smoke.py", "verdict": "failed", "attempts": [{"output": "original assertion RED"}]}]}
                proofs = finish_full_proofs(fixture.root, context, red, {"state": "verified"}, principal="fixture")
                self.assertFalse(proofs[0]["proof"]["reusable"])
                self.assertEqual([event["operation"] for event in store.history(key)], ["begin", "observe", "begin", "observe"])
                self.assertIn("original assertion RED", store.get(key)["observation"]["attempts"][0]["output"])

    def test_cli_run_reports_failure_and_keeps_stdout_structured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            request_file = fixture.root / "request.json"
            request_file.write_text(json.dumps(fixture.request()))
            stdout, stderr = io.StringIO(), io.StringIO()

            def execute(_entries: list[str]) -> dict[str, Any]:
                print("canonical runner progress")
                return {"exit_code": 1, "smoke_results": {"suite/check_smoke.py": {"verdict": "failed", "attempts": [{"output": "original RED"}]}}}

            with fixture.patches(), patch("quality_gates.run_smokes.execute_selected", side_effect=execute), redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["run", "--repo-root", str(fixture.root), "--store-root", str(fixture.root / "proofs"), "--request-json", str(request_file), "--principal", "fixture"])
            response = json.loads(stdout.getvalue())
            self.assertEqual(code, 1)
            self.assertEqual(response["run"]["exit_code"], 1)
            self.assertEqual(response["executed_count"], 1)
            self.assertFalse(response["proofs"][0]["proof"]["reusable"])
            self.assertIn("canonical runner progress", stderr.getvalue())

    def test_profile_change_invalidates_and_failed_attempt_retained(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            store = SmokePassStore(fixture.root / "proofs")

            def red(entries: list[str]) -> dict[str, Any]:
                return {"exit_code": 1, "smoke_results": {smoke: {"verdict": "failed", "attempts": [{"output": "original RED assertion", "verdict": "failed"}]} for smoke in entries}}

            with fixture.patches():
                result = run_selected_smokes(fixture.root, fixture.request(), store=store, principal="fixture", executor=red)
                row = result["selection"]["selected"][0]
                self.assertFalse(store.get(row["key"])["reusable"])
                self.assertIn("original RED", store.history(row["key"])[-1]["payload"]["attempts"][0]["output"])
                with patch("quality_gates.smoke_proof.tool_profile", return_value={"rule_version": "new-profile", "inputs": {}}):
                    new = select_smokes(fixture.root, fixture.request(), store=store)
                self.assertNotEqual(row["key"], new["selected"][0]["key"])

    def test_empty_reaches_actual_runner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            fixture.old_sources = fixture.sources
            fixture.base = FrozenTree(fixture.base.commit, fixture.candidate.tree, fixture.candidate.entries)
            fixture.delta = []
            with fixture.patches():
                result = run_selected_smokes(fixture.root, fixture.request(empty=True), store=SmokePassStore(fixture.root / "proofs"), principal="fixture")
            self.assertEqual(result["status"], "no_tests")
            self.assertEqual(result["run"]["status"], "no_tests")
            self.assertEqual(result["run"]["harness_defects"], [])
            self.assertEqual((result["selected_count"], result["executed_count"], result["reused_count"]), (0, 0, 0))
            self.assertEqual(result["proofs"], [])

    def test_registered_harness_defect_is_listed_and_never_a_verdict(self) -> None:
        """The real runner's exit 124 reaches the proof run as a listed defect with an unchanged exit code."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "harness_smoke.py").write_text(f"print('own timeout wrapper gave up')\nraise SystemExit({run_smokes._HARNESS_DEFECT_EXIT_CODE})\n", encoding="utf-8")
            with (
                patch.dict(os.environ, {"RUN_SMOKES_BATTERY_RECEIPT_DIR": str(root / "receipts"), "SOLET_NAME": os.environ.get("SOLET_NAME", "fixture-solet")}),
                patch.object(run_smokes, "_REPO_ROOT", root),
                patch.object(run_smokes, "_read_register", return_value=["harness_smoke.py"]),
                patch.object(run_smokes, "_read_serial_only", return_value=frozenset()),
                patch.object(run_smokes, "_venv_python", return_value=Path(sys.executable)),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                result = execute_registered(["harness_smoke.py"], run_smokes)
        row = result["smoke_results"]["harness_smoke.py"]
        observed = (result["exit_code"], result["harness_defects"], row["verdict"], [attempt["verdict"] for attempt in row["attempts"]])
        self.assertEqual(observed, (0, ["harness_smoke.py"], "not_run", ["not_run"]))

    def test_harness_defect_observation_is_flagged_counted_and_never_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            store = SmokePassStore(fixture.root / "proofs")
            attempts = [{"output": f"HARNESS-DEFECT: exit {run_smokes._HARNESS_DEFECT_EXIT_CODE} is a harness defect to file", "verdict": "not_run"}]
            run = {"exit_code": 0, "import_environment": {"state": "verified"}, "smoke_results": {"suite/check_smoke.py": {"verdict": "not_run", "retried": False, "attempts": attempts}}}
            observed: list[tuple[object, ...]] = []
            with fixture.patches():
                # A run that lists the defect, then one whose list is unknown.
                for produced in ({**run, "harness_defects": ["suite/check_smoke.py"]}, run):
                    result = run_selected_smokes(fixture.root, fixture.request(), store=store, principal="fixture", executor=lambda _entries, produced=produced: produced)
                    proof = result["proofs"][0]["proof"]
                    observed.append((proof["observation"]["harness_defect"], proof["observation"]["complete"], proof["reusable"], result["counts"]["harness_defect"]))
            # A listed defect is flagged and counted; an unlisted run leaves it unknown.
            self.assertEqual(observed, [(True, False, False, 1), (None, False, False, 0)])

    def test_full_campaign_harness_defects_reach_each_published_proof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            store = SmokePassStore(fixture.root / "proofs")
            listed = {"harness_defects": ["suite/check_smoke.py"], "smoke_results": [{"smoke": "suite/check_smoke.py", "verdict": "not_run", "attempts": [{"output": "HARNESS-DEFECT", "verdict": "not_run"}]}]}
            unpublished = {"result": "interrupted", "exit_code": None, "harness_defects": None}
            flags: list[object] = []
            with fixture.patches(), patch("quality_gates.smoke_identity.delta_entries", return_value=[]), patch("quality_gates.smoke_proof.SmokePassStore", return_value=store):
                for result in (listed, unpublished):
                    context = prepare_full_proofs(fixture.root, fixture.candidate.commit, principal="fixture", attribution={})
                    proofs = finish_full_proofs(fixture.root, context, result, {"state": "verified"}, principal="fixture")
                    flags.append(proofs[0]["proof"]["observation"]["harness_defect"])
            self.assertEqual(flags, [True, None])

    def test_cli_run_reports_harness_defect_without_changing_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            request_file = fixture.root / "request.json"
            request_file.write_text(json.dumps(fixture.request()))
            stdout, stderr = io.StringIO(), io.StringIO()

            def execute(_entries: list[str]) -> dict[str, Any]:
                return {"exit_code": 0, "harness_defects": ["suite/check_smoke.py"], "smoke_results": {"suite/check_smoke.py": {"verdict": "not_run", "attempts": [{"output": "HARNESS-DEFECT", "verdict": "not_run"}]}}}

            with fixture.patches(), patch("quality_gates.run_smokes.execute_selected", side_effect=execute), redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["run", "--repo-root", str(fixture.root), "--store-root", str(fixture.root / "proofs"), "--request-json", str(request_file), "--principal", "fixture"])
            response = json.loads(stdout.getvalue())
            observed = (code, response["run"]["exit_code"], response["run"]["harness_defects"], response["counts"]["harness_defect"], response["proofs"][0]["proof"]["observation"]["harness_defect"])
            self.assertEqual(observed, (0, 0, ["suite/check_smoke.py"], 1, True))

    def test_registered_execution_namespace_has_no_timeout(self) -> None:
        captured: list[dict[str, object]] = []
        started = time.monotonic()

        def run_suite(entries: list[str], args: argparse.Namespace, _serial_only: frozenset[str], *, retry: bool, smoke_results: Any) -> tuple[list[str], list[str], list[str], list[str], float, list[dict[str, object]], list[str]]:
            captured.append({"entries": entries, "args": dict(vars(args)), "retry": retry, "observations": type(smoke_results).__name__})
            return [], [], [], [], time.monotonic() - started, [], []

        runner = SimpleNamespace(
            _DEFAULT_REGISTER=Path("register"),
            _DEFAULT_SERIAL_ONLY=Path("serial"),
            _DEFAULT_JOBS=run_smokes._DEFAULT_JOBS,
            _MAX_SMOKE_STARTS_PER_WINDOW=run_smokes._MAX_SMOKE_STARTS_PER_WINDOW,
            _SMOKE_START_WINDOW_SECONDS=run_smokes._SMOKE_START_WINDOW_SECONDS,
            _read_register=lambda _path: ["suite/check_smoke.py"],
            _read_serial_only=lambda _path: frozenset(),
            _require_solet_name=lambda: "fixture",
            _initial_smoke_results=run_smokes._initial_smoke_results,
            _receipt_directory=lambda: Path("receipts"),
            contention_signal=SimpleNamespace(count_concurrent_live_batteries=lambda _directory: 0, measure=lambda **_values: SimpleNamespace(as_dict=dict)),
            host_load=SimpleNamespace(HostLoadMonitor=_LoadMonitor),
            _scratch_runtime=lambda: lambda _supervisor, _terminate: nullcontext(),
            _SmokeProcessSupervisor=lambda *_limits: object(),
            _terminate_process_group=run_smokes._terminate_process_group,
            _run_suite_with_retry=run_suite,
            _summarize=lambda *_counts, **_policy: 0,
        )
        result = execute_registered(["suite/check_smoke.py"], runner)
        self.assertEqual(captured, [{"entries": ["suite/check_smoke.py"], "args": {"jobs": run_smokes._DEFAULT_JOBS, "fail_on_skip": False}, "retry": False, "observations": "SmokeObservations"}])
        self.assertEqual((result["status"], result["exit_code"]), ("completed", 0))

    def test_truncated_runner_output_is_partial_and_never_reused(self) -> None:
        row = {"key": "f" * 64, "smoke": "suite/verbose_smoke.py", "identity": {}, "inputs": {}, "profile_sha256": "0" * 64, "repository_manifest_sha256": None, "identity_complete": True}
        request = {"candidate_commit": "b" * 40, "candidate_tree": "d" * 40}
        for environment, truncated in (({}, False), ({"SMOKE_PROCESS_TAIL_BYTES": _SMALL_TAIL_BYTES}, True)):
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, environment):
                smoke = Path(directory) / "verbose_smoke.py"
                smoke.write_text("[print('passing line', number) for number in range(20)]\n", encoding="utf-8")
                verdict, output, _exit_code = run_smokes._run_one(Path(sys.executable), smoke)
            observation = _observation(row, {"verdict": verdict, "attempts": [{"output": output, "verdict": verdict}], "import_environment": {"state": "verified"}}, request)
            self.assertEqual(verdict, "passed")
            self.assertEqual(output.startswith(TRUNCATED_OUTPUT_MARKER), truncated, output)
            self.assertEqual((observation["partial"], clean_observation(observation)), (truncated, not truncated), observation)

    def test_request_cannot_remove_changed_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            with fixture.patches():
                request = fixture.request()
                request["delta_entries"] = []
                with self.assertRaisesRegex(ValueError, "delta_entries"):
                    select_smokes(fixture.root, request)


if __name__ == "__main__":
    unittest.main()
