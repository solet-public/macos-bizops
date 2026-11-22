#!/usr/bin/env python3
"""Tier U: a run's proof record and the proof store hold digests and counts, and raw output only for a smoke that did not pass cleanly."""

# ruff: noqa: E402
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Generator
from contextlib import contextmanager, redirect_stderr
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from quality_gates import smoke_proof, smoke_reads
from quality_gates.smoke_identity import FrozenTree, digest, recorded_key
from quality_gates.smoke_pass_store import SmokePassStore
from quality_gates.smoke_proof import SmokeObservations, finish_full_proofs, prepare_full_proofs, run_selected_smokes, select_smokes
from quality_gates.smoke_read_store import RecordedReadStore
from quality_gates.tests._focused_smoke_selector_fixture import FrozenProofFixture

# The fixture's smoke, and one more over the same closure.
_FIRST, _SECOND = "suite/check_smoke.py", "suite/second_smoke.py"


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


# A smoke whose closure cannot be proven (it imports a module it cannot name), so its key is the whole tree.
_DYNAMIC = "suite/dynamic_smoke.py"
_DYNAMIC_SOURCE = b"from suite.helper import VALUE\nfrom importlib import import_module\nimport_module(VALUE)\n"
_RECORDED_AT = "2026-11-15T00:00:00+00:00"
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def _recorded(entries: list[str], *, verdict: str = "passed", output: str = "3 passed") -> dict[str, Any]:
    """What the canonical runner reports, with every field a reader of the run record uses."""
    code = 0 if verdict == "passed" else 1
    rows = {smoke: {"verdict": verdict, "seconds": 0.01, "exit_code": code, "output_ref": f"#/smoke_results/{index}/output_tail", "attempts": [{"smoke": smoke, "verdict": verdict, "exit_code": code, "output": output, "seconds": 0.01, "recorded_at": _RECORDED_AT}]} for index, smoke in enumerate(entries)}
    return {"exit_code": code, "harness_defects": [], "import_environment": {"state": "verified"}, "smoke_results": rows}


def _attempt_files(store: SmokePassStore) -> list[Path]:
    """The raw attempts a store holds."""
    return sorted((store.root / "attempts").glob("*.json"))


def _fixture_with(directory: str, smokes: tuple[str, ...]) -> FrozenProofFixture:
    """The fixture with the unprovable smoke in its tree and exactly ``smokes`` registered."""
    fixture = FrozenProofFixture(Path(directory))
    fixture.sources[_DYNAMIC] = fixture.old_sources[_DYNAMIC] = _DYNAMIC_SOURCE
    _register(fixture, smokes)
    return fixture


class SlimRecordTests(unittest.TestCase):
    """A run's proof record and the proof store hold digests and counts, and raw output only for a smoke that did not pass cleanly."""

    def test_a_selection_carries_a_digest_and_a_count_not_the_closure_map(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _fixture_with(directory, (_FIRST, _DYNAMIC))
            store = SmokePassStore(fixture.root / "proofs")
            with fixture.patches():
                selection = select_smokes(fixture.root, fixture.request(), store=store)
            self.assertNotIn("repository_inputs", selection)
            rows = {row["smoke"]: row for row in selection["selected"]}
            self.assertEqual(sorted(rows), [_FIRST, _DYNAMIC])
            for row in rows.values():
                self.assertNotIn("inputs", row)
                self.assertRegex(row["inputs_sha256"], _HEX64)
                self.assertIsInstance(row["inputs_count"], int)
            self.assertGreater(rows[_FIRST]["inputs_count"], 0)
            self.assertEqual((rows[_FIRST]["key_scope"], rows[_DYNAMIC]["key_scope"]), ("closure", "tree"))
            # One string per limit, counting the selected rows it keeps from a proven closure.
            self.assertEqual(selection["advisories"], ["dynamic-load: 1 selected rows"])
            # The full rows keep the map the run path verifies; the response is those rows without it.
            with fixture.patches():
                full_response, full_rows = smoke_proof._select(fixture.root, fixture.request(), store, None)
            self.assertEqual(full_response, selection)
            self.assertIn("data/input.csv", {path for row in full_rows for path in row["inputs"]})
            self.assertEqual([{name: value for name, value in row.items() if name != "inputs"} for row in full_rows], selection["selected"])

    def test_a_tree_scope_run_still_refuses_a_tracked_file_outside_the_closure_that_differs(self) -> None:
        for smoke, refuses in ((_FIRST, False), (_DYNAMIC, True)):
            with self.subTest(smoke=smoke), tempfile.TemporaryDirectory() as directory:
                fixture = _fixture_with(directory, (smoke,))
                (fixture.root / "notes.txt").write_bytes(b"changed after the freeze\n")
                with fixture.patches():
                    if refuses:
                        with self.assertRaisesRegex(ValueError, "differs"):
                            run_selected_smokes(fixture.root, fixture.request(), store=SmokePassStore(fixture.root / "proofs"), principal="fixture", executor=_passing)
                    else:
                        result = run_selected_smokes(fixture.root, fixture.request(), store=SmokePassStore(fixture.root / "proofs"), principal="fixture", executor=_passing)
                        self.assertEqual(result["executed_count"], 1)

    def test_a_clean_run_stores_a_slim_observation_and_no_raw_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            store = SmokePassStore(fixture.root / "proofs")
            with fixture.patches():
                result = run_selected_smokes(fixture.root, fixture.request(), store=store, principal="fixture", executor=_recorded)
            row = result["selection"]["selected"][0]
            current = store.get(row["key"])
            observation = current["observation"]
            self.assertTrue(current["reusable"])
            self.assertFalse({"inputs", "identity"} & observation.keys())
            self.assertEqual((observation["inputs_sha256"], observation["key_scope"]), (row["inputs_sha256"], "closure"))
            attempt = observation["attempts"][0]
            self.assertNotIn("output", attempt)
            self.assertEqual((attempt["output_bytes"], attempt["recorded_at"]), (len("3 passed"), _RECORDED_AT))
            self.assertEqual(observation["output_sha256"], digest("3 passed"))
            self.assertEqual((observation["candidate_commit"], observation["candidate_tree"]), (fixture.candidate.commit, fixture.candidate.tree))
            self.assertEqual(set(store.history(row["key"])[0]["payload"]), {"smoke", "key_scope", "inputs_sha256", "profile_sha256"})
            self.assertEqual(_attempt_files(store), [])

    def test_a_failed_or_skipped_run_keeps_its_output_and_no_raw_copy(self) -> None:
        cases = (("failed", "failed", "original RED assertion"), ("skipped", "skipped", "SKIPPED: no database"), ("zero tests", "passed", "Ran 0 tests"))
        for name, verdict, output in cases:
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                fixture = FrozenProofFixture(Path(directory))
                store = SmokePassStore(fixture.root / "proofs")
                with fixture.patches():
                    result = run_selected_smokes(fixture.root, fixture.request(), store=store, principal="fixture", executor=lambda entries, verdict=verdict, output=output: _recorded(entries, verdict=verdict, output=output))
                row = result["selection"]["selected"][0]
                observation = store.get(row["key"])["observation"]
                # The closure map and the file's identity are dropped from every observation; only the attempts differ by outcome.
                self.assertFalse({"inputs", "identity"} & observation.keys())
                self.assertEqual((observation["inputs_sha256"], observation["key_scope"]), (row["inputs_sha256"], "closure"))
                self.assertEqual(observation["attempts"][0]["output"], output)
                self.assertEqual(_attempt_files(store), [])
                self.assertNotIn("raw_attempt_path", observation)
                self.assertFalse((store.root / "attempts").exists())

    def test_no_attempt_is_written_as_a_raw_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"RUN_SMOKES_BATTERY_RECEIPT_DIR": directory}):
            observations = SmokeObservations([_FIRST], {})
            store = SmokePassStore()
            observations.record_attempt(_FIRST, "passed", "3 passed", 0.01, 0)
            self.assertEqual(_attempt_files(store), [])
            self.assertNotIn("raw_attempt_path", observations.attempts[_FIRST][0])
            observations.record_attempt(_FIRST, "failed", "original RED", 0.01, 1)
            self.assertEqual(_attempt_files(store), [])
            self.assertNotIn("raw_attempt_path", observations.attempts[_FIRST][1])
            self.assertEqual(observations.attempts[_FIRST][1]["output"], "original RED")

    def test_a_run_that_reuses_one_smoke_and_runs_another_answers_the_reader_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            _register(fixture, (_FIRST, _SECOND))
            store = SmokePassStore(fixture.root / "proofs")
            with fixture.patches():
                run_selected_smokes(fixture.root, fixture.request(), store=store, principal="fixture", executor=_recorded, only=(_FIRST,))
                result = run_selected_smokes(fixture.root, fixture.request(), store=store, principal="fixture", executor=_recorded, only=(_FIRST, _SECOND))
            self.assertEqual((result["status"], result["selected_count"], result["executed_count"], result["reused_count"]), ("completed", 2, 1, 1))
            self.assertEqual(result["selection"]["restricted_out"], [])
            proofs = {row["smoke"]: row for row in result["proofs"]}
            self.assertEqual((proofs[_FIRST]["reused"], proofs[_SECOND]["reused"]), (True, False))
            observation = proofs[_FIRST]["proof"]["observation"]
            self.assertRegex(proofs[_FIRST]["proof"]["key"], _HEX64)
            self.assertLessEqual({"verdict", "seconds", "exit_code", "candidate_commit", "candidate_tree"}, observation.keys())
            self.assertTrue(observation["attempts"] and all("recorded_at" in attempt for attempt in observation["attempts"]))
            executed = result["run"]["smoke_results"][_SECOND]
            self.assertLessEqual({"verdict", "seconds", "exit_code", "output_ref"}, executed.keys())
            self.assertTrue(executed["attempts"] and all("recorded_at" in attempt for attempt in executed["attempts"]))
            self.assertEqual(result["run"]["harness_defects"], [])
            # The reused record names the closure it was measured on by the digest the selection row carries.
            row = {row["smoke"]: row for row in result["selection"]["selected"]}[_FIRST]
            self.assertEqual((observation["inputs_sha256"], observation["key_scope"]), (row["inputs_sha256"], row["key_scope"]))

    def test_the_run_result_of_a_passed_smoke_has_no_output_and_a_failed_one_keeps_it(self) -> None:
        cases = (("passed", "passed", "3 passed", False), ("zero tests", "passed", "Ran 0 tests", True), ("failed", "failed", "original RED", True))
        for name, verdict, output, kept_in_store in cases:
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                fixture = FrozenProofFixture(Path(directory))
                store = SmokePassStore(fixture.root / "proofs")
                with fixture.patches():
                    result = run_selected_smokes(fixture.root, fixture.request(), store=store, principal="fixture", executor=lambda entries, verdict=verdict, output=output: _recorded(entries, verdict=verdict, output=output))
                attempt = result["run"]["smoke_results"][_FIRST]["attempts"][0]
                self.assertEqual("output" in attempt, verdict != "passed")
                self.assertEqual(attempt["recorded_at"], _RECORDED_AT)
                # Skip detection reads the full output before it is dropped from the run result.
                observation = store.get(result["selection"]["selected"][0]["key"])["observation"]
                self.assertEqual(("output" in observation["attempts"][0], observation["skipped"]), (kept_in_store, name == "zero tests"))

    def test_a_full_campaign_publishes_the_same_slim_observation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            store = SmokePassStore(fixture.root / "proofs")
            green = {"smoke_results": [{"smoke": _FIRST, "verdict": "passed", "attempts": [{"output": "3 passed", "verdict": "passed", "recorded_at": _RECORDED_AT}]}]}
            with fixture.patches(), patch("quality_gates.smoke_identity.delta_entries", return_value=[]), patch("quality_gates.smoke_proof.SmokePassStore", return_value=store):
                context = prepare_full_proofs(fixture.root, fixture.candidate.commit, principal="fixture", attribution={})
                proofs = finish_full_proofs(fixture.root, context, green, {"state": "verified"}, principal="fixture")
            observation = proofs[0]["proof"]["observation"]
            self.assertFalse({"inputs", "identity"} & observation.keys())
            self.assertEqual(observation["attempts"], [{"verdict": "passed", "recorded_at": _RECORDED_AT, "output_bytes": len("3 passed")}])
            self.assertEqual(set(store.history(proofs[0]["proof"]["key"])[0]["payload"]), {"smoke", "key_scope", "inputs_sha256", "profile_sha256"})
            self.assertEqual(_attempt_files(store), [])


class EndOfProofTests(unittest.TestCase):
    CLOSED = "b" * 40

    def _plant(self, store: SmokePassStore, number: int, age_days: int) -> Path:
        key = f"{number:064x}"
        store.begin(key, {"smoke": _FIRST}, expected_version=0, principal="fixture")
        store.observe(key, {"key": key, "smoke": _FIRST, "key_scope": "tree", "candidate_tree": self.CLOSED, "verdict": "passed", "exit_code": 0, "complete": True, "identity_complete": True, "attempt_count": 1}, expected_version=1, principal="fixture")
        folder = store.root / key
        written_at = time.time() - age_days * 86400
        os.utime(folder, (written_at, written_at))
        return folder

    def test_a_run_prunes_closed_tree_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            store = SmokePassStore(fixture.root / "proofs")
            planted = [self._plant(store, number, 2) for number in (1, 2)]
            stderr = io.StringIO()
            with fixture.patches(), patch("quality_gates.smoke_proof._open_trees", return_value={fixture.candidate.tree}), redirect_stderr(stderr):
                result = run_selected_smokes(fixture.root, fixture.request(), store=store, principal="fixture", executor=_passing)
            self.assertEqual(result["status"], "completed")
            self.assertTrue(all(not folder.exists() for folder in planted))
            self.assertEqual(len(store.list()), 1)
            self.assertIn("proof store: kept 1 keys, removed 2 keys and 0 attempts", stderr.getvalue())

    def test_unreadable_open_trees_use_the_cap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            store = SmokePassStore(fixture.root / "proofs")
            recent = self._plant(store, 1, 2)
            expired = self._plant(store, 2, 8)
            stderr = io.StringIO()
            with fixture.patches(), patch("quality_gates.smoke_proof._open_trees", side_effect=ValueError("no git")), redirect_stderr(stderr):
                result = run_selected_smokes(fixture.root, fixture.request(), store=store, principal="fixture", executor=_passing)
            self.assertEqual(result["status"], "completed")
            self.assertTrue(recent.exists())
            self.assertFalse(expired.exists())
            self.assertIn("ADVISORY proof store: open candidates unreadable", stderr.getvalue())

    def test_a_full_campaign_prunes_the_default_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"RUN_SMOKES_BATTERY_RECEIPT_DIR": directory}):
            fixture = FrozenProofFixture(Path(directory))
            store = SmokePassStore()
            expired = self._plant(store, 1, 8)
            green = {"smoke_results": [{"smoke": _FIRST, "verdict": "passed", "attempts": [{"output": "3 passed", "verdict": "passed", "recorded_at": _RECORDED_AT}]}]}
            with fixture.patches(), patch("quality_gates.smoke_identity.delta_entries", return_value=[]), patch("quality_gates.smoke_proof._open_trees", return_value={fixture.candidate.tree}):
                context = prepare_full_proofs(fixture.root, fixture.candidate.commit, principal="fixture", attribution={})
                finish_full_proofs(fixture.root, context, green, {"state": "verified"}, principal="fixture")
            self.assertFalse(expired.exists())

    def test_open_trees_are_the_head_trees_of_every_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()

            def command(*args: str) -> str:
                result = subprocess.run(("git", "-C", str(repo), *args), capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, f"git {args!r}: {result.stderr}")
                return result.stdout.strip()

            command("init", "-q")
            (repo / "one.txt").write_text("one\n")
            command("add", "one.txt")
            command("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "first")
            (repo / "two.txt").write_text("two\n")
            command("add", "two.txt")
            command("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "second")
            command("worktree", "add", "--detach", str(Path(directory) / "wt"), "HEAD~1")
            expected = {command("rev-parse", "HEAD^{tree}"), command("rev-parse", "HEAD~1^{tree}")}
            self.assertEqual(smoke_proof._open_trees(repo), expected)


def _certificate(fixture: FrozenProofFixture, smoke: str, profile: dict[str, Any]) -> dict[str, Any]:
    """A complete certificate that validates on the fixture: the smoke's own bytes, the root's digest and a matched probe."""
    trace: dict[str, Any] = {
        "schema": smoke_reads.READ_SCHEMA,
        "rule_version": smoke_reads._RULE,
        "smoke": smoke,
        "profile_sha256": digest(profile),
        "complete": True,
        "incomplete_reasons": [],
        "reads": [{"path": smoke, "mode": "100644", "blob": fixture.candidate.entries[smoke]["blob"]}],
        "metadata": [],
        "directories": [],
        "absent": [],
        "code_inputs": [],
        "generated_scratch": [],
        "runtime_inputs": [{"path": "@repository_root", "sha256": hashlib.sha256(os.fsencode(fixture.root.resolve())).hexdigest()}, {"path": "@root_probe", "status": "matched"}],
        "attribution": {},
    }
    trace["trace_sha256"] = smoke_reads._digest(trace)
    return trace


@contextmanager
def _plain_directory(_record: bool) -> Generator[Path]:
    """A directory for traces without work-folder custody: this case's runner is an executor that records nothing."""
    with tempfile.TemporaryDirectory() as directory:
        yield Path(directory)


def _stock_certificates(fixture: FrozenProofFixture, store: SmokePassStore, smokes: tuple[str, ...], profile: dict[str, Any], *, without_proof: str) -> None:
    """A certificate and a clean proof at its recorded key for each smoke; the one named has no proof."""
    reads = RecordedReadStore.beside(store)
    for smoke in smokes:
        trace = _certificate(fixture, smoke, profile)
        key = recorded_key(smoke, trace["trace_sha256"], profile)
        reads.add(trace, key, principal="fixture")
        if smoke != without_proof:
            clean = {"key": key, "smoke": smoke, "verdict": "passed", "exit_code": 0, "complete": True, "identity_complete": True, "attempt_count": 1, "attempts": [{"verdict": "passed", "output_bytes": 8}], "output_sha256": "0" * 64, "inputs_sha256": trace["trace_sha256"], "key_scope": "recorded"}
            store.observe(key, clean, expected_version=0, principal="fixture")


class RecordedRecordTests(unittest.TestCase):
    """A run that reuses most of its smokes by their recorded reads stays small, and what did not pass cleanly keeps its evidence."""

    def assert_recorded_rows_are_compact(self, result: dict[str, Any]) -> None:
        """No row carries a closure map or a clean smoke's output; a recorded row names its certificate by digest and count."""
        self.assertFalse(any("inputs" in row for row in result["selection"]["selected"]))
        reused = [row for row in result["proofs"] if row["reused"]]
        self.assertEqual({row["recorded"]["status"] for row in reused}, {"licensed"})
        self.assertEqual({tuple(sorted(row["recorded"])) for row in reused}, {("certificate", "key", "reads", "reasons", "status")})
        self.assertFalse(any("output" in attempt for row in reused for attempt in row["proof"]["observation"]["attempts"]))

    def assert_the_failure_keeps_its_evidence(self, result: dict[str, Any], store: SmokePassStore, smoke: str) -> None:
        """The one that did not pass keeps its original output in the run, and no raw copy; it was not certified."""
        failed = next(row for row in result["proofs"] if not row["reused"])
        self.assertEqual((failed["smoke"], failed["recorded"]["status"]), (smoke, "not_clean"))
        self.assertEqual(result["run"]["smoke_results"][smoke]["attempts"][0]["output"], "original RED")
        self.assertEqual(_attempt_files(store), [])

    def test_a_118_smoke_result_with_recorded_rows_is_compact_and_keeps_nonclean_evidence(self) -> None:
        smokes = tuple(f"suite/s{number:03d}_smoke.py" for number in range(118))
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            _register(fixture, smokes)
            store = SmokePassStore(fixture.root / "proofs")
            _stock_certificates(fixture, store, smokes, {"rule_version": "fixture-v1", "inputs": {}}, without_proof=smokes[-1])
            stderr = io.StringIO()
            with fixture.patches(), patch("quality_gates.smoke_proof.capture_directory", _plain_directory), redirect_stderr(stderr):
                result = run_selected_smokes(fixture.root, fixture.request(), store=store, principal="fixture", executor=lambda entries: _recorded(entries, verdict="failed", output="original RED"), record_reads=True)
            self.assertEqual((result["selected_count"], result["executed_count"], result["reused_count"]), (118, 1, 117))
            self.assertLess(len(json.dumps(result)), 5 * 1024 * 1024)
            self.assert_recorded_rows_are_compact(result)
            self.assert_the_failure_keeps_its_evidence(result, store, smokes[-1])
            # Pruning ends the certificate whose proof is missing and says so.
            self.assertIn("recorded reads: kept 117 certificates, removed 1", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
