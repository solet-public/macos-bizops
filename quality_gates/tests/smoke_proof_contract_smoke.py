#!/usr/bin/env python3
"""Tier U: real shared selection/store dispatch, reuse and invalidation."""

# ruff: noqa: E402
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Mapping
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from quality_gates import run_smokes, smoke_proof
from quality_gates.smoke_identity import FrozenTree, canonical, digest, freeze_request
from quality_gates.smoke_pass_store import SmokePassStore, StaleProofVersionError, clean_observation
from quality_gates.smoke_proof import TRUNCATED_OUTPUT_MARKER, _observation, execute_registered, finish_full_proofs, main, prepare_full_proofs, proof_operation, run_request_cli, run_selected_smokes, select_smokes
from quality_gates.tests._focused_smoke_selector_fixture import FrozenProofFixture, git_environment

# A deliberately small retained tail, so a short child overflows it.
_SMALL_TAIL_BYTES = "64"

# The fixture's smoke, and two more over the same closure.
_FIRST, _SECOND, _THIRD = "suite/check_smoke.py", "suite/second_smoke.py", "suite/third_smoke.py"
_REPOSITORY = "fixture-repository"
# The marker a run prints when it executes a smoke whose reuse it could not claim.
_CLAIM_ADVISORY = "key changed before claim"


def _passing(entries: list[str]) -> dict[str, Any]:
    """What the canonical runner reports for smokes that all pass once."""
    return {"exit_code": 0, "import_environment": {"state": "verified"}, "smoke_results": {smoke: {"verdict": "passed", "seconds": 0.01, "attempts": [{"output": "3 passed", "verdict": "passed"}]} for smoke in entries}}


def _register(fixture: FrozenProofFixture, smokes: tuple[str, ...]) -> None:
    """Register more smokes over the fixture's closure, in the base and the candidate alike."""
    for sources in (fixture.sources, fixture.old_sources):
        for smoke in smokes:
            sources.setdefault(smoke, sources[_FIRST])
        sources["quality_gates/gate_smokes.txt"] = "".join(f"{smoke}\n" for smoke in smokes).encode()
    fixture.base = FrozenTree(fixture.base.commit, fixture.base.tree, fixture._entries(fixture.old_sources))
    fixture.candidate = FrozenTree(fixture.candidate.commit, fixture.candidate.tree, fixture._entries(fixture.sources))
    fixture.delta = [{"path": "data/input.csv", "status": "M", "base": fixture.base.entries["data/input.csv"], "candidate": fixture.candidate.entries["data/input.csv"]}]
    fixture._write()


class _RacingStore(SmokePassStore):
    """A store whose named write meets another writer on each of its first ``races`` attempts."""

    def __init__(self, root: Path, operation: str, races: int) -> None:
        super().__init__(root)
        self.operation, self.races, self.attempts = operation, races, 0

    def _race(self, key: str, operation: str, smoke: str) -> None:
        if operation != self.operation:
            return
        self.attempts += 1
        if self.attempts <= self.races:
            super().begin(key, {"smoke": smoke}, expected_version=self.get(key)["version"], principal="landing")

    def begin(self, key: str, identity: Mapping[str, Any], *, expected_version: int, principal: str) -> dict[str, Any]:
        self._race(key, "begin", str(identity["smoke"]))
        return super().begin(key, identity, expected_version=expected_version, principal=principal)

    def observe(self, key: str, observation: Mapping[str, Any], *, expected_version: int, principal: str) -> dict[str, Any]:
        self._race(key, "observe", str(observation["smoke"]))
        return super().observe(key, observation, expected_version=expected_version, principal=principal)


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
                return {"exit_code": 0, "import_environment": {"state": "verified"}, "smoke_results": {smoke: {"verdict": "passed", "seconds": 0.01, "attempts": [{"output": "3 passed", "verdict": "passed"}]} for smoke in entries}}

            with fixture.patches():
                first = run_selected_smokes(fixture.root, fixture.request(), store=store, principal="fixture", executor=execute)
                second_request = fixture.request()
                second_request["attribution"] = {"annotation": "unrelated-candidate"}
                second = run_selected_smokes(fixture.root, second_request, store=store, principal="fixture", executor=execute)
                self.assertEqual(first["executed_count"], 1)
                self.assertEqual(second["reused_count"], 1)
                self.assertEqual(calls, [["suite/check_smoke.py"], []])
                # With no certificate the key is the predecessor's closure key, and the proof row says nothing of recorded reads.
                self.assertEqual((first["selection"]["selected"][0]["key_scope"], "recorded" in second["proofs"][0]), ("closure", False))
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
            run = {"exit_code": 0, "import_environment": {"state": "verified"}, "smoke_results": {"suite/check_smoke.py": {"verdict": "not_run", "attempts": attempts}}}
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

        def run_suite(entries: list[str], args: argparse.Namespace, _serial_only: frozenset[str], *, smoke_results: Any) -> tuple[list[str], list[str], list[str], list[str], float]:
            captured.append({"entries": entries, "args": dict(vars(args)), "observations": type(smoke_results).__name__})
            return [], [], [], [], time.monotonic() - started

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
            _run_suite_with_environment=run_suite,
            _summarize=lambda *_counts, **_policy: 0,
        )
        result = execute_registered(["suite/check_smoke.py"], runner)
        self.assertEqual(captured, [{"entries": ["suite/check_smoke.py"], "args": {"jobs": run_smokes._DEFAULT_JOBS, "fail_on_skip": False}, "observations": "SmokeObservations"}])
        self.assertEqual((result["status"], result["exit_code"]), ("completed", 0))

    def test_truncated_runner_output_is_partial_and_never_reused(self) -> None:
        row = {"key": "f" * 64, "smoke": "suite/verbose_smoke.py", "key_scope": "closure", "inputs_sha256": "1" * 64, "profile_sha256": "0" * 64, "repository_manifest_sha256": None, "identity_complete": True}
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


def _freeze(root: Path, *flags: str) -> tuple[int, str, str]:
    """Run the freeze operation; return its exit code, stdout and stderr."""
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(["freeze", "--repo-root", str(root), *flags])
    return code, stdout.getvalue(), stderr.getvalue()


class FreezeTests(unittest.TestCase):
    """Case (a): one producer of a platform-smoke-proof/v1 request, as a command."""

    def test_freeze_writes_the_request_the_library_producer_returns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            named = fixture.root / "named.txt"
            named.write_text(f"# smokes to prove\n\n{_FIRST}  # the demonstration\n", encoding="utf-8")
            commits = ("--base-commit", fixture.base.commit, "--candidate-commit", fixture.candidate.commit, "--repository-id", _REPOSITORY)
            with fixture.patches():
                code, stdout, stderr = _freeze(fixture.root, *commits, "--named-file", str(named), "--request-out", str(fixture.root / "named.json"))
                bare_code, _, _ = _freeze(fixture.root, *commits, "--request-out", str(fixture.root / "bare.json"))
                named_request = freeze_request(fixture.root, fixture.base.commit, fixture.candidate.commit, repository_id=_REPOSITORY, named_smokes=[_FIRST])
                bare_request = freeze_request(fixture.root, fixture.base.commit, fixture.candidate.commit, repository_id=_REPOSITORY)
            self.assertEqual((code, bare_code, stderr), (0, 0, ""))
            written = (fixture.root / "named.json").read_bytes()
            self.assertEqual(written, canonical(named_request))
            self.assertEqual((fixture.root / "bare.json").read_bytes(), canonical(bare_request))
            self.assertEqual(json.loads(written)["named_smokes"], [_FIRST])
            summary = json.loads(stdout)
            self.assertEqual((summary["request_sha256"], summary["candidate_commit"], summary["named_smokes"]), (hashlib.sha256(written).hexdigest(), fixture.candidate.commit, [_FIRST]))

    def test_freeze_refuses_in_one_line_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            unregistered = fixture.root / "unregistered.txt"
            unregistered.write_text("suite/not_registered_smoke.py\n", encoding="utf-8")
            out = fixture.root / "request.json"
            commits = ("--base-commit", fixture.base.commit, "--candidate-commit", fixture.candidate.commit)
            refusals = {
                "named smoke not registered in the candidate": (*commits, "--repository-id", _REPOSITORY, "--named-file", str(unregistered)),
                "no repository id": (*commits, "--named-file", str(unregistered)),
            }
            with fixture.patches():
                for name, flags in refusals.items():
                    with self.subTest(name):
                        code, stdout, stderr = _freeze(fixture.root, *flags, "--request-out", str(out))
                        self.assertNotEqual(code, 0)
                        self.assertEqual((stdout, len(stderr.splitlines())), ("", 1), stderr)
                        self.assertFalse(out.exists())

    def test_freeze_refuses_a_ref_that_is_not_a_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for arguments in (("init", "-q", str(root)), ("-C", str(root), "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "fixture")):
                subprocess.run(("git", *arguments), check=True, capture_output=True, env=git_environment())
            out = root / "request.json"
            absent = "0" * 40
            for base, candidate in ((absent, "HEAD"), ("HEAD", absent)):
                with self.subTest(base=base, candidate=candidate):
                    code, stdout, stderr = _freeze(root, "--base-commit", base, "--candidate-commit", candidate, "--repository-id", _REPOSITORY, "--request-out", str(out))
                    self.assertNotEqual(code, 0)
                    self.assertEqual((stdout, len(stderr.splitlines())), ("", 1), stderr)
                    self.assertFalse(out.exists())


class RestrictionTests(unittest.TestCase):
    """Case (b): a caller's own register limits a proof run to what it names."""

    def test_a_register_restricts_the_selection_and_says_what_it_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            _register(fixture, (_FIRST, _SECOND, _THIRD))
            store = SmokePassStore(fixture.root / "proofs")
            named = (_FIRST, _SECOND, "suite/unregistered_smoke.py")
            with fixture.patches():
                union = select_smokes(fixture.root, fixture.request(), store=store)
                restricted = select_smokes(fixture.root, fixture.request(), store=store, only=named)
                narrower = select_smokes(fixture.root, fixture.request(), store=store, only=named[:2])
            self.assertEqual([row["smoke"] for row in union["selected"]], [_FIRST, _SECOND, _THIRD])
            self.assertEqual([row["smoke"] for row in restricted["selected"]], [_FIRST, _SECOND])
            self.assertEqual((restricted["restricted_out"], restricted["restricted_missing"]), ([_THIRD], ["suite/unregistered_smoke.py"]))
            self.assertEqual(restricted["restricted_to_register_sha256"], hashlib.sha256("".join(f"{smoke}\n" for smoke in sorted(named)).encode()).hexdigest())
            kept = f"{_FIRST}\n{_SECOND}\n"
            self.assertEqual((restricted["register"], restricted["register_sha256"]), (kept, hashlib.sha256(kept.encode()).hexdigest()))
            # The hash covers the restriction: it moves with the register named, though the rows kept are the same.
            unsealed = {name: value for name, value in restricted.items() if name != "selection_sha256"}
            self.assertEqual(restricted["selection_sha256"], digest(unsealed))
            self.assertNotEqual(restricted["selection_sha256"], union["selection_sha256"])
            self.assertEqual(narrower["selected"], restricted["selected"])
            self.assertNotEqual(narrower["selection_sha256"], restricted["selection_sha256"])
            # A changed path that only a dropped smoke consumes is still mapped: dropping is reported, not hidden as a gap.
            self.assertEqual(restricted["unmapped"], union["unmapped"])

    def test_no_register_restricts_nothing_and_the_response_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            _register(fixture, (_FIRST, _SECOND, _THIRD))
            store = SmokePassStore(fixture.root / "proofs")
            with fixture.patches():
                union = select_smokes(fixture.root, fixture.request(), store=store)
                unrestricted = select_smokes(fixture.root, fixture.request(), store=store, only=None)
            self.assertEqual(unrestricted, union)
            self.assertFalse({"restricted_out", "restricted_missing", "restricted_to_register_sha256"} & union.keys())

    def test_the_proof_command_restricts_only_for_a_register_other_than_the_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            _register(fixture, (_FIRST, _SECOND, _THIRD))
            request_file = fixture.root / "request.json"
            request_file.write_text(json.dumps(fixture.request()))
            focused = fixture.root / "focused.txt"
            focused.write_text(f"# the focused register\n{_FIRST}\n\n{_SECOND}  # second\n", encoding="utf-8")
            executed: list[list[str]] = []

            def execute(entries: list[str]) -> dict[str, Any]:
                executed.append(list(entries))
                return _passing(entries)

            responses: dict[str, dict[str, Any]] = {}
            for name, register in (("focused", focused), ("default", run_smokes._DEFAULT_REGISTER)):
                output = fixture.root / f"{name}.json"
                arguments = argparse.Namespace(register=register, proof_request=request_file, proof_output=output)
                # Each run has its own proof store, so neither reuses the other's verdicts.
                with fixture.patches(), patch.dict(os.environ, {"RUN_SMOKES_BATTERY_RECEIPT_DIR": str(fixture.root / name)}), patch("quality_gates.run_smokes.execute_selected", side_effect=execute):
                    self.assertEqual(run_request_cli(arguments, fixture.root), 0)
                responses[name] = json.loads(output.read_text())
            self.assertEqual(executed, [[_FIRST, _SECOND], [_FIRST, _SECOND, _THIRD]])
            self.assertEqual((responses["focused"]["selected_count"], responses["focused"]["selection"]["restricted_out"]), (2, [_THIRD]))
            self.assertEqual(responses["default"]["selected_count"], 3)
            self.assertFalse({"restricted_out", "restricted_to_register_sha256"} & responses["default"]["selection"].keys())


class RunPathTests(unittest.TestCase):
    """Cases (c) to (g): run paths share the proof store with other writers."""

    def test_a_repeated_unchanged_run_reuses_the_clean_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            store = SmokePassStore(fixture.root / "proofs")
            calls: list[list[str]] = []

            def execute(entries: list[str]) -> dict[str, Any]:
                calls.append(list(entries))
                return _passing(entries)

            with fixture.patches():
                request = fixture.request()
                first = run_selected_smokes(fixture.root, request, store=store, principal="fixture", executor=execute)
                second = run_selected_smokes(fixture.root, request, store=store, principal="fixture", executor=execute)
            self.assertEqual((first["executed_count"], first["reused_count"]), (1, 0))
            self.assertEqual((second["executed_count"], second["reused_count"]), (0, 1))
            self.assertEqual(calls, [[_FIRST], []])
            self.assertIn(_FIRST, first["run"]["smoke_results"])
            self.assertNotIn(_FIRST, second["run"]["smoke_results"])
            self.assertTrue(second["proofs"][0]["reused"])

    def test_a_campaign_writing_during_a_run_loses_no_event_and_does_not_fail_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            _register(fixture, (_FIRST, _SECOND))
            store = SmokePassStore(fixture.root / "proofs")

            with fixture.patches():
                keys = {row["smoke"]: row["key"] for row in select_smokes(fixture.root, fixture.request(), store=store)["selected"]}

                def campaign_writes(entries: list[str]) -> dict[str, Any]:
                    # What a campaign start does to one key and a campaign finish does to another, while this run executes.
                    store.begin(keys[_FIRST], {"smoke": _FIRST}, expected_version=store.get(keys[_FIRST])["version"], principal="campaign")
                    store.observe(keys[_SECOND], {"key": keys[_SECOND], "smoke": _SECOND, "verdict": "failed"}, expected_version=store.get(keys[_SECOND])["version"], principal="campaign")
                    return _passing(entries)

                result = run_selected_smokes(fixture.root, fixture.request(), store=store, principal="fixture", executor=campaign_writes)
            self.assertEqual(result["executed_count"], 2)
            history = {smoke: [(event["operation"], event["principal"]) for event in store.history(key)] for smoke, key in keys.items()}
            self.assertEqual(history[_FIRST], [("begin", "fixture"), ("begin", "campaign"), ("observe", "fixture")])
            self.assertEqual(history[_SECOND], [("begin", "fixture"), ("observe", "campaign"), ("observe", "fixture")])
            self.assertEqual(store.get(keys[_SECOND])["observation"]["verdict"], "passed")

    def test_a_write_that_meets_three_writers_lands_and_five_stale_attempts_raise(self) -> None:
        for operation in ("begin", "observe"):
            for races, lands in ((3, True), (5, False)):
                with self.subTest(operation=operation, races=races), tempfile.TemporaryDirectory() as directory:
                    fixture = FrozenProofFixture(Path(directory))
                    store = _RacingStore(fixture.root / "proofs", operation, races)
                    with fixture.patches():
                        if lands:
                            result = run_selected_smokes(fixture.root, fixture.request(), store=store, principal="fixture", executor=_passing)
                            key = result["selection"]["selected"][0]["key"]
                            self.assertEqual(store.attempts, races + 1)
                            expected = ["begin"] * races + ["begin", "observe"] if operation == "begin" else ["begin"] + ["begin"] * races + ["observe"]
                            self.assertEqual([event["operation"] for event in store.history(key)], expected)
                        else:
                            with self.assertRaises(StaleProofVersionError):
                                run_selected_smokes(fixture.root, fixture.request(), store=store, principal="fixture", executor=_passing)
                            self.assertEqual(store.attempts, races)

    def test_a_reusable_key_that_changes_before_the_claim_executes_instead(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            proofs = SmokePassStore(fixture.root / "proofs")
            calls: list[list[str]] = []

            def execute(entries: list[str]) -> dict[str, Any]:
                calls.append(list(entries))
                return _passing(entries)

            with fixture.patches():
                first = run_selected_smokes(fixture.root, fixture.request(), store=proofs, principal="fixture", executor=execute)
                key = first["selection"]["selected"][0]["key"]
                select_before_claim = smoke_proof._select

                def select_then_retire(root: Path, request: Mapping[str, Any], store: SmokePassStore, only: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
                    selection = select_before_claim(root, request, store, only)
                    proofs.retire(key, expected_version=proofs.get(key)["version"], principal="operator", reason="retired between selection and claim")
                    return selection

                stderr = io.StringIO()
                with patch("quality_gates.smoke_proof._select", side_effect=select_then_retire), redirect_stderr(stderr):
                    second = run_selected_smokes(fixture.root, fixture.request(), store=proofs, principal="fixture", executor=execute)
            self.assertTrue(second["selection"]["selected"][0]["reusable"])
            self.assertEqual((second["executed_count"], second["reused_count"]), (1, 0))
            self.assertEqual(calls, [[_FIRST], [_FIRST]])
            self.assertIn(_CLAIM_ADVISORY, stderr.getvalue())
            self.assertTrue(proofs.get(key)["reusable"])

    def test_a_begin_after_the_claim_does_not_undo_a_reuse_but_a_retire_does(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            store = SmokePassStore(fixture.root / "proofs")
            with fixture.patches():
                first = run_selected_smokes(fixture.root, fixture.request(), store=store, principal="fixture", executor=_passing)
                key = first["selection"]["selected"][0]["key"]

                def campaign_begins(entries: list[str]) -> dict[str, Any]:
                    store.begin(key, {"smoke": _FIRST}, expected_version=store.get(key)["version"], principal="campaign")
                    return _passing(entries)

                def operator_retires(entries: list[str]) -> dict[str, Any]:
                    store.retire(key, expected_version=store.get(key)["version"], principal="operator", reason="retired while consumed")
                    return _passing(entries)

                reused = run_selected_smokes(fixture.root, fixture.request(), store=store, principal="fixture", executor=campaign_begins)
                self.assertEqual((reused["executed_count"], reused["reused_count"], reused["proofs"][0]["reused"]), (0, 1, True))
                self.assertEqual([event["operation"] for event in store.history(key)], ["begin", "observe", "begin"])
                # The campaign's begin made the key unreusable for the next selection; a fresh run executes and observes.
                run_selected_smokes(fixture.root, fixture.request(), store=store, principal="fixture", executor=_passing)
                with self.assertRaisesRegex(ValueError, "proof changed during consumption"):
                    run_selected_smokes(fixture.root, fixture.request(), store=store, principal="fixture", executor=operator_retires)

    def test_a_campaign_that_finishes_after_a_landing_publishes_every_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            store = SmokePassStore(fixture.root / "proofs")
            with fixture.patches(), patch("quality_gates.smoke_identity.delta_entries", return_value=[]), patch("quality_gates.smoke_proof.SmokePassStore", return_value=store):
                context = prepare_full_proofs(fixture.root, fixture.candidate.commit, principal="campaign", attribution={})
                request = freeze_request(fixture.root, fixture.candidate.commit, fixture.candidate.commit, repository_id=_REPOSITORY, named_smokes=[_FIRST])
                landing = run_selected_smokes(fixture.root, request, store=store, principal="landing", executor=_passing)
                key = landing["proofs"][0]["proof"]["key"]
                green = {"smoke_results": [{"smoke": _FIRST, "verdict": "passed", "attempts": [{"output": "3 passed", "verdict": "passed"}]}]}
                proofs = finish_full_proofs(fixture.root, context, green, {"state": "verified"}, principal="campaign")
            self.assertEqual([(event["operation"], event["principal"]) for event in store.history(key)], [("begin", "campaign"), ("begin", "landing"), ("observe", "landing"), ("observe", "campaign")])
            self.assertEqual((len(proofs), proofs[0]["proof"]["version"]), (1, 4))

    def test_lifecycle_writes_keep_the_strict_compare_and_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            store = SmokePassStore(fixture.root / "proofs")
            with fixture.patches():
                key = run_selected_smokes(fixture.root, fixture.request(), store=store, principal="fixture", executor=_passing)["selection"]["selected"][0]["key"]
                arguments: dict[str, dict[str, Any]] = {"correct": {"metadata": {"annotation": "late"}}, "clear": {"fields": ["annotation"]}, "retire": {}, "reinstate": {"request": fixture.request(), "root": fixture.root}}
                for operation, extra in arguments.items():
                    with self.subTest(operation), self.assertRaises(StaleProofVersionError) as raised:
                        proof_operation(store, operation, key=key, expected_version=1, principal="operator", reason="premise read before the run", **extra)
                    self.assertEqual(raised.exception.current_version, 2)
            self.assertEqual([event["operation"] for event in store.history(key)], ["begin", "observe"])


if __name__ == "__main__":
    unittest.main()
