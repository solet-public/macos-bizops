#!/usr/bin/env python3
"""Tier U: frozen identity, relative/sibling/resource closure and invalidation."""

# ruff: noqa: E402
from __future__ import annotations

import hashlib
import importlib.metadata
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from quality_gates import run_smokes, smoke_identity
from quality_gates.smoke_identity import FrozenTree, closure, dependency_graph, digest, safe_path, validate_request, verify_worktree
from quality_gates.smoke_pass_store import SmokePassStore
from quality_gates.smoke_proof import finish_full_proofs, prepare_full_proofs, run_selected_smokes, select_smokes
from quality_gates.tests._focused_smoke_selector_fixture import FrozenProofFixture


class IdentityTests(unittest.TestCase):
    def test_frozen_positive_and_wrong_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            with fixture.patches():
                request = fixture.request()
                validate_request(fixture.root, request)
                verify_worktree(fixture.root, fixture.candidate, list(fixture.sources))
                request["candidate_tree"] = "f" * 40
                with self.assertRaisesRegex(ValueError, "candidate_tree"):
                    validate_request(fixture.root, request)

    def test_transitive_resource_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            graph, unknown = dependency_graph(fixture.sources, fixture.candidate.entries)
            self.assertFalse(unknown)
            self.assertIn("data/input.csv", closure(graph, "suite/check_smoke.py"))
            with fixture.patches():
                result = select_smokes(fixture.root, fixture.request(), store=SmokePassStore(fixture.root / "store"))
            self.assertEqual([row["smoke"] for row in result["selected"]], ["suite/check_smoke.py"])
            closure_reason = next(reason for reason in result["selected"][0]["reasons"] if reason["rule"] == "input_closure")
            self.assertEqual(closure_reason["import_chain"], ["suite/check_smoke.py", "suite/helper.py", "suite/leaf.py", "data/input.csv"])

    def test_relative_sibling_dynamic_and_unknown(self) -> None:
        sources = {"pkg/a.py": b"from .b import x\n", "pkg/b.py": b"x=1\n", "test/check.py": b"import helper\n", "test/helper.py": b'import importlib\nimportlib.import_module("pkg.a")\n', "dynamic.py": b"import importlib\nimportlib.import_module(variable)\n"}
        graph, unknown = dependency_graph(sources, FrozenProofFixture._entries(sources))
        self.assertIn("pkg/b.py", closure(graph, "test/check.py"))
        self.assertIn("dynamic.py", unknown)

    def test_composed_resource_has_declared_edge_and_uncached_uncertainty(self) -> None:
        sources = {"pkg/check_smoke.py": b'from pathlib import Path\nVALUE = (Path(__file__).parent / "fixtures" / "value.csv").read_text()\n', "pkg/fixtures/value.csv": b"value"}
        graph, unknown = dependency_graph(sources, FrozenProofFixture._entries(sources))
        self.assertIn("pkg/fixtures/value.csv", closure(graph, "pkg/check_smoke.py"))
        self.assertTrue(any("resource target" in notice for notice in unknown["pkg/check_smoke.py"]))

    def _symlinked_row(self, area: Path, relative: str) -> tuple[SmokePassStore, dict[str, Any]]:
        """The fixture with ``relative`` a symlink in the candidate; its store and the one selected row."""
        fixture = FrozenProofFixture(area / "repo")
        (area / "outside.py").write_text("print('1 passed')\n")
        path = fixture.root / relative
        if path.exists():
            path.unlink()
        target = "../../outside.py"
        path.symlink_to(target)
        fixture.sources.pop(relative, None)
        entry = {"mode": "120000", "kind": "blob", "blob": hashlib.sha1(f"blob {len(target)}\0".encode() + target.encode(), usedforsecurity=False).hexdigest()}
        fixture.candidate = FrozenTree("b" * 40, "d" * 40, {**fixture.candidate.entries, relative: entry})
        old = fixture.base.entries.get(relative)
        delta: dict[str, Any] = {"path": relative, "status": "M" if old else "A", "base": old, "candidate": entry}
        fixture.delta[:] = sorted([row for row in fixture.delta if row["path"] != relative] + [delta], key=lambda item: str(item["path"]))
        profile = {"rule_version": "fixture-v1", "inputs": {relative: entry} if relative.startswith("quality_gates/") else {}}
        store = SmokePassStore(area / "proofs")
        with fixture.patches(), patch("quality_gates.smoke_proof.tool_profile", return_value=profile):
            verify_worktree(fixture.root, fixture.candidate, [relative])
            row = select_smokes(fixture.root, fixture.request(), store=store)["selected"][0]
            store.observe(row["key"], {"key": row["key"], "smoke": row["smoke"], "verdict": "passed", "exit_code": 0, "complete": True, "identity_complete": True, "attempt_count": 1}, expected_version=0, principal="fixture")
            reused = select_smokes(fixture.root, fixture.request(), store=store)["selected"][0]["reusable"]
        return store, {**row, "reused_after_a_clean_pass": reused}

    def test_an_opaque_smoke_or_tool_input_is_incomplete_and_never_reused(self) -> None:
        for relative in ("suite/check_smoke.py", "quality_gates/opaque_tool.py"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                _, row = self._symlinked_row(Path(directory), relative)
                self.assertFalse(row["identity_complete"])
                self.assertTrue(any("opaque" in reason for reason in row["identity_advisories"]))
                self.assertFalse(row["reused_after_a_clean_pass"])

    def test_an_opaque_entry_only_in_the_closure_keys_the_whole_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, row = self._symlinked_row(Path(directory), "data/input.csv")
            self.assertEqual((row["key_scope"], row["scope_limits"], row["identity_complete"], row["identity_advisories"]), ("tree", ["opaque-mode"], True, []))
            self.assertEqual((row["inputs_count"], bool(row["repository_manifest_sha256"])), (0, True))
            self.assertTrue(row["reused_after_a_clean_pass"])

    def test_modes_bytes_and_path_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            target = fixture.root / "suite/leaf.py"
            target.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "differs"):
                verify_worktree(fixture.root, fixture.candidate, ["suite/leaf.py"])
            target.chmod(0o644)
            target.write_bytes(b"changed\n")
            with self.assertRaisesRegex(ValueError, "differs"):
                verify_worktree(fixture.root, fixture.candidate, ["suite/leaf.py"])
        for path in ("../escape.py", "/tmp/x", "x/../../y", ".git/config"):
            with self.assertRaises(ValueError):
                safe_path(path)


_ROOT = Path(__file__).resolve().parents[2]
_FUNCTION_LEVEL_IMPORTS = ("smoke_process", "smoke_import_environment", "run_smokes_scratch", "source_root", "candidate_repo", "candidate_tree", "editable_finder", "git_environment")


def _tool_sources() -> dict[str, bytes]:
    """The ``quality_gates/*.py`` files as they are on disk, keyed as the frozen tree keys them."""
    return {f"quality_gates/{path.name}": path.read_bytes() for path in sorted((_ROOT / "quality_gates").glob("*.py"))}


class ToolIdentityTests(unittest.TestCase):
    """What the profile holds: the runner's and the proof tool's own imports, the Postgres tool, the environment."""

    def test_a_closure_that_only_reads_the_environment_has_no_notice(self) -> None:
        body = b"import os\nfrom pathlib import Path\nimport tempfile\nVALUE = (os.environ.get('X'), os.getenv('Y'), Path.home(), tempfile.gettempdir())\n"
        _, unknown = dependency_graph({"pkg/check.py": body}, FrozenProofFixture._entries({"pkg/check.py": body}))
        self.assertEqual(unknown, {})

    def test_the_remaining_notices_say_not_proven(self) -> None:
        sources = {"a.py": b"import subprocess\n", "b.py": b"open(name)\n", "c.py": b"import importlib\nimportlib.import_module(name)\n"}
        _, unknown = dependency_graph(sources, FrozenProofFixture._entries(sources))
        self.assertEqual(sorted(unknown), ["a.py", "b.py", "c.py"])
        for notices in unknown.values():
            self.assertTrue(all("not proven" in notice and "uncached" not in notice for notice in notices), notices)

    def test_tool_inputs_follow_imports_at_any_depth(self) -> None:
        sources = {
            "quality_gates/run_smokes.py": b"import host_load\nfrom quality_gates.smoke_proof import main\n\n\ndef later() -> None:\n    import source_root\n    import quality_gates.candidate_repo\n    from quality_gates import candidate_tree\n    from git_environment import helper\n",
            "quality_gates/smoke_proof.py": b"import os\nfrom quality_gates.smoke_identity import digest\n",
            "quality_gates/smoke_identity.py": b"VALUE = 1\n",
            "quality_gates/host_load.py": b"import json\n",
            "quality_gates/source_root.py": b"import candidate_tree\n",
            "quality_gates/candidate_repo.py": b"",
            "quality_gates/candidate_tree.py": b"",
            "quality_gates/git_environment.py": b"",
            "quality_gates/pyright_repo.py": b"import host_load\n",
        }
        expected = ["host_load", "smoke_proof", "smoke_identity", "source_root", "candidate_repo", "candidate_tree", "git_environment", "run_smokes"]
        self.assertEqual(smoke_identity.tool_inputs(sources), sorted(f"quality_gates/{name}.py" for name in expected))

    def test_tool_inputs_of_the_real_tree(self) -> None:
        found = set(smoke_identity.tool_inputs(_tool_sources()))
        for name in ("run_smokes", "smoke_proof", "smoke_identity", "smoke_pass_store", "host_load", "private_test_pg", *_FUNCTION_LEVEL_IMPORTS):
            self.assertIn(f"quality_gates/{name}.py", found)
        self.assertEqual([path for path in found if path.endswith("_check.py")], [])
        self.assertNotIn("quality_gates/pyright_repo.py", found)

    def test_the_recorders_files_are_tool_inputs_no_import_reaches_and_each_moves_the_profile(self) -> None:
        sources = _tool_sources()
        recorder = ("quality_gates/smoke_read_hooks.py", "quality_gates/smoke_read_worker.py", "quality_gates/smoke_reads.py")
        self.assertLessEqual(set(recorder), set(smoke_identity.tool_inputs(sources)))
        # The worker is started by path and nothing imports it, so only the fixed list reaches it; a tree without a recorder file lacks it.
        bare = {"quality_gates/run_smokes.py": b"", "quality_gates/smoke_proof.py": b"", **dict.fromkeys(recorder, b"")}
        self.assertEqual(smoke_identity.tool_inputs(bare), sorted(["quality_gates/run_smokes.py", "quality_gates/smoke_proof.py", *recorder]))
        self.assertEqual(smoke_identity.tool_inputs({"quality_gates/run_smokes.py": b""}), ["quality_gates/run_smokes.py"])
        tree = FrozenTree("a" * 40, "b" * 40, FrozenProofFixture._entries(sources))
        before = digest(smoke_identity.tool_profile(_ROOT, tree, sources))
        for path in recorder:
            with self.subTest(path=path):
                changed = {**sources, path: sources[path] + b"\n# changed\n"}
                moved = FrozenTree("a" * 40, "b" * 40, FrozenProofFixture._entries(changed))
                self.assertNotEqual(digest(smoke_identity.tool_profile(_ROOT, moved, changed)), before)

    def test_a_recorded_key_follows_its_certificate_smoke_and_profile_and_nothing_else(self) -> None:
        profile = {"rule_version": "fixture-v1", "inputs": {}}
        key = smoke_identity.recorded_key(_FIRST, "c" * 64, profile)
        self.assertEqual(key, smoke_identity.recorded_key(_FIRST, "c" * 64, dict(profile)))
        others = (smoke_identity.recorded_key(_SECOND, "c" * 64, profile), smoke_identity.recorded_key(_FIRST, "d" * 64, profile), smoke_identity.recorded_key(_FIRST, "c" * 64, {**profile, "rule_version": "fixture-v2"}))
        self.assertEqual(len({key, *others}), 4)
        self.assertEqual(smoke_identity.RECORDED_SCOPE, "recorded")

    def test_parse_never_reuse(self) -> None:
        body = b"# header\n\na.py  # net\n  b.py # live: tmux, launchd # more\n"
        self.assertEqual(smoke_identity.parse_never_reuse(body), {"a.py": "net", "b.py": "live: tmux, launchd # more"})
        self.assertEqual(smoke_identity.parse_never_reuse(None), {})
        for refused in (b"c.py\n", b"a.py # fine\nc.py #  \n"):
            with self.subTest(refused=refused), self.assertRaisesRegex(ValueError, r"c\.py"):
                smoke_identity.parse_never_reuse(refused)

    def test_the_profile_of_the_real_root(self) -> None:
        sources = _tool_sources()
        tree = FrozenTree("a" * 40, "b" * 40, FrozenProofFixture._entries(sources))
        profile = smoke_identity.tool_profile(_ROOT, tree, sources)
        self.assertIsInstance(profile["postgres"], str)
        self.assertEqual(set(profile), {"rule_version", "python", "executable", "platform", "dependencies", "import_paths", "child_environment_sha256", "inputs", "solet_name", "postgres"})
        self.assertEqual(sorted(profile["inputs"]), smoke_identity.tool_inputs(sources))
        self.assertEqual(profile["rule_version"], "platform-focused-union/v3")

    def test_an_installed_distribution_changes_the_profile(self) -> None:
        sources = _tool_sources()
        tree = FrozenTree("a" * 40, "b" * 40, FrozenProofFixture._entries(sources))
        installed = list(importlib.metadata.distributions())
        extra = SimpleNamespace(metadata={"Name": "fixture-distribution"}, version="1.0")
        with patch("importlib.metadata.distributions", return_value=installed):
            before = digest(smoke_identity.tool_profile(_ROOT, tree, sources))
            again = digest(smoke_identity.tool_profile(_ROOT, tree, sources))
        with patch("importlib.metadata.distributions", return_value=[*installed, extra]):
            after = digest(smoke_identity.tool_profile(_ROOT, tree, sources))
        self.assertEqual(before, again)
        self.assertNotEqual(before, after)

    def test_a_second_record_of_an_installed_distribution_at_the_same_version_changes_nothing(self) -> None:
        sources = _tool_sources()
        tree = FrozenTree("a" * 40, "b" * 40, FrozenProofFixture._entries(sources))
        installed = list(importlib.metadata.distributions())
        first = installed[0]
        name = first.metadata.get("Name", "unknown")
        landing = SimpleNamespace(metadata={"Name": "solet-setup-contracts"}, version="0.1.0")

        def profile(distributions: list[Any]) -> str:
            with patch("importlib.metadata.distributions", return_value=distributions):
                return digest(smoke_identity.tool_profile(_ROOT, tree, sources))

        before = profile([*installed])
        self.assertEqual(before, profile([*installed, SimpleNamespace(metadata={"Name": name}, version=first.version)]))
        self.assertEqual(before, profile([*installed, *installed]))
        self.assertEqual(profile([*installed, landing]), profile([*installed, landing, landing]))

    def test_a_different_version_of_an_installed_distribution_changes_the_profile(self) -> None:
        sources = _tool_sources()
        tree = FrozenTree("a" * 40, "b" * 40, FrozenProofFixture._entries(sources))
        installed = list(importlib.metadata.distributions())
        first = installed[0]
        name = first.metadata.get("Name", "unknown")

        def profile(distributions: list[Any]) -> str:
            with patch("importlib.metadata.distributions", return_value=distributions):
                return digest(smoke_identity.tool_profile(_ROOT, tree, sources))

        newer = SimpleNamespace(metadata={"Name": name}, version=first.version + ".1")
        self.assertNotEqual(profile([*installed]), profile([*installed, newer]))

    def test_a_changed_tool_file_changes_the_profile_and_a_changed_other_file_does_not(self) -> None:
        sources = _tool_sources()
        tree = FrozenTree("a" * 40, "b" * 40, FrozenProofFixture._entries(sources))
        before = digest(smoke_identity.tool_profile(_ROOT, tree, sources))
        for path, effect in (("quality_gates/smoke_process.py", True), ("quality_gates/gate_smokes.txt", False), ("quality_gates/pyright_repo.py", False)):
            with self.subTest(path=path):
                changed = {**sources, path: sources.get(path, b"") + b"\n# changed\n"}
                moved = FrozenTree("a" * 40, "b" * 40, FrozenProofFixture._entries(changed))
                self.assertEqual(digest(smoke_identity.tool_profile(_ROOT, moved, changed)) != before, effect)

    def test_the_postgres_identity_is_the_first_pg_ctl_version_or_a_word(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            area = Path(directory)
            for name, body in (("good", "echo 'pg_ctl (PostgreSQL) 17.2'"), ("bad", "exit 3")):
                tool = area / name / "pg_ctl"
                tool.parent.mkdir()
                tool.write_text(f"#!/bin/sh\n{body}\n")
                tool.chmod(0o755)
            (area / "empty").mkdir()
            cases = {"version": ((area / "empty", area / "good", area / "bad"), "pg_ctl (PostgreSQL) 17.2"), "first only": ((area / "bad", area / "good"), "unreadable"), "none": ((area / "empty",), "absent")}
            for name, (directories, expected) in cases.items():
                with self.subTest(name), patch("quality_gates.private_test_pg.PG_BIN_DIRECTORIES", tuple(str(path) for path in directories)):
                    self.assertEqual(smoke_identity._postgres_identity(), expected)


_FIRST, _SECOND = "suite/check_smoke.py", "suite/second_smoke.py"


def _passing(entries: list[str]) -> dict[str, Any]:
    """What the canonical runner reports for smokes that all pass once."""
    return {"exit_code": 0, "import_environment": {"state": "verified"}, "smoke_results": {smoke: {"verdict": "passed", "seconds": 0.01, "attempts": [{"output": "3 passed", "verdict": "passed"}]} for smoke in entries}}


def _register(fixture: FrozenProofFixture, smokes: tuple[str, ...], extra: Mapping[str, bytes] | None = None) -> None:
    """Register smokes over the fixture's closure, in the base and the candidate alike; ``extra`` adds files to both."""
    for sources in (fixture.sources, fixture.old_sources):
        sources.update(extra or {})
        for smoke in smokes:
            sources.setdefault(smoke, sources[_FIRST])
        sources["quality_gates/gate_smokes.txt"] = "".join(f"{smoke}\n" for smoke in smokes).encode()
    fixture.base = FrozenTree(fixture.base.commit, fixture.base.tree, fixture._entries(fixture.old_sources))
    fixture.candidate = FrozenTree(fixture.candidate.commit, fixture.candidate.tree, fixture._entries(fixture.sources))
    fixture.delta = [{"path": "data/input.csv", "status": "M", "base": fixture.base.entries["data/input.csv"], "candidate": fixture.candidate.entries["data/input.csv"]}]
    fixture._write()


def _named(fixture: FrozenProofFixture, *smokes: str) -> dict[str, object]:
    """The fixture's request naming the smokes, so each is selected whatever the delta changes."""
    request = fixture.request()
    request["named_smokes"] = sorted(smokes)
    return request


def _advance(fixture: FrozenProofFixture, changes: Mapping[str, bytes], mark: str) -> None:
    """Move the candidate to a new tree that differs from the last one by exactly ``changes``."""
    fixture.sources.update(changes)
    fixture.candidate = FrozenTree(mark * 40, mark * 40, fixture._entries(fixture.sources))
    fixture.delta = [{"path": path, "status": "M", "base": fixture.base.entries.get(path), "candidate": fixture.candidate.entries[path]} for path in sorted(changes)]
    fixture._write()


class KeyScopeTests(unittest.TestCase):
    """One key per smoke: its closure when that is provable, the whole tree when it is not, none for a smoke that is never reused."""

    _READER, _PROCESS, _LISTED = "suite/reader_smoke.py", "suite/process_smoke.py", "suite/never_smoke.py"

    def _run(self, fixture: FrozenProofFixture, store: SmokePassStore, calls: list[list[str]], *smokes: str) -> dict[str, Any]:
        def execute(entries: list[str]) -> dict[str, Any]:
            calls.append(list(entries))
            return _passing(entries)

        return run_selected_smokes(fixture.root, _named(fixture, *smokes), store=store, principal="fixture", executor=execute)

    def _counts(self, result: Mapping[str, Any]) -> tuple[int, int]:
        return result["executed_count"], result["reused_count"]

    def test_a_closure_key_follows_the_closure_and_nothing_else(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            store, calls = SmokePassStore(fixture.root / "proofs"), []
            with fixture.patches():
                first = self._run(fixture, store, calls, _FIRST)
                row = first["selection"]["selected"][0]
                self.assertEqual((row["key_scope"], row["scope_limits"], row["identity_complete"], row["repository_manifest_sha256"]), ("closure", [], True, None))
                self.assertGreater(row["inputs_count"], 0)
                self.assertRegex(row["inputs_sha256"], r"[0-9a-f]{64}\Z")
                _advance(fixture, {"notes.txt": b"changed, and not read by the smoke\n"}, "e")
                second = self._run(fixture, store, calls, _FIRST)
                _advance(fixture, {"data/input.csv": b"two\n"}, "f")
                third = self._run(fixture, store, calls, _FIRST)
            self.assertEqual(second["selection"]["selected"][0]["key"], row["key"])
            self.assertEqual([self._counts(first), self._counts(second), self._counts(third)], [(1, 0), (0, 1), (1, 0)])
            self.assertNotEqual(third["selection"]["selected"][0]["key"], row["key"])
            self.assertEqual(calls, [[_FIRST], [], [_FIRST]])

    def test_a_smoke_that_reads_a_file_it_cannot_name_is_keyed_by_the_whole_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            _register(fixture, (self._READER,), {self._READER: b"name = 'unlisted'\nopen(name)\n"})
            store, calls = SmokePassStore(fixture.root / "proofs"), []
            with fixture.patches():
                first = self._run(fixture, store, calls, self._READER)
                again = self._run(fixture, store, calls, self._READER)
                _advance(fixture, {"notes.txt": b"changed\n"}, "e")
                moved = self._run(fixture, store, calls, self._READER)
            row = first["selection"]["selected"][0]
            self.assertEqual((row["key_scope"], row["scope_limits"], row["identity_complete"], row["inputs_count"]), ("tree", ["file-read"], True, 0))
            self.assertTrue(row["repository_manifest_sha256"])
            self.assertEqual([self._counts(first), self._counts(again), self._counts(moved)], [(1, 0), (0, 1), (1, 0)])
            self.assertNotEqual(moved["selection"]["selected"][0]["key"], row["key"])

    def test_each_limit_widens_the_key_to_the_tree_and_is_named(self) -> None:
        cases = {
            "external-process": b"import subprocess\n",
            "dynamic-load": b"import importlib\nimportlib.import_module(name)\n",
            "repository-scan": b"from pathlib import Path\nPath('.').rglob('*.py')\n",
            "unparsed": b"def broken(:\n",
            "file-read": b"from pathlib import Path\nPath(name).read_text()\n",
        }
        for limit, body in cases.items():
            with self.subTest(limit), tempfile.TemporaryDirectory() as directory:
                fixture = FrozenProofFixture(Path(directory))
                _register(fixture, (self._READER,), {self._READER: body})
                with fixture.patches():
                    row = select_smokes(fixture.root, _named(fixture, self._READER), store=SmokePassStore(fixture.root / "proofs"))["selected"][0]
                self.assertEqual((row["key_scope"], row["scope_limits"], row["identity_complete"]), ("tree", [limit], True))

    def test_a_smoke_that_starts_a_process_is_keyed_by_the_whole_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            _register(fixture, (self._PROCESS,), {self._PROCESS: b"import subprocess\n"})
            store, calls = SmokePassStore(fixture.root / "proofs"), []
            with fixture.patches():
                first = self._run(fixture, store, calls, self._PROCESS)
                again = self._run(fixture, store, calls, self._PROCESS)
            self.assertEqual(first["selection"]["selected"][0]["key_scope"], "tree")
            self.assertEqual([self._counts(first), self._counts(again)], [(1, 0), (0, 1)])

    def _never_fixture(self, area: Path) -> FrozenProofFixture:
        fixture = FrozenProofFixture(area)
        _register(fixture, (_FIRST, self._LISTED), {smoke_identity.NEVER_REUSE_RELPATH: f"{self._LISTED}  # network: the fixture says so\n".encode()})
        return fixture

    def test_a_never_reused_smoke_runs_every_time_and_stores_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._never_fixture(Path(directory))
            store, calls = SmokePassStore(fixture.root / "proofs"), []
            with fixture.patches():
                first = self._run(fixture, store, calls, _FIRST, self._LISTED)
                second = self._run(fixture, store, calls, _FIRST, self._LISTED)
            self.assertEqual(calls, [[_FIRST, self._LISTED], [self._LISTED]])
            self.assertEqual([self._counts(first), self._counts(second)], [(2, 0), (1, 1)])
            rows = {row["smoke"]: row for row in second["selection"]["selected"]}
            self.assertEqual((rows[self._LISTED]["key_scope"], rows[self._LISTED]["key"], rows[self._LISTED]["reusable"]), ("never", None, False))
            self.assertEqual(rows[self._LISTED]["reuse_reason"], "never reused: network: the fixture says so")
            proofs = {row["smoke"]: row for row in second["proofs"]}
            self.assertEqual((proofs[self._LISTED]["proof"], proofs[self._LISTED]["reused"], proofs[_FIRST]["reused"]), (None, False, True))
            self.assertEqual({path.name for path in store.root.iterdir() if len(path.name) == 64}, {rows[_FIRST]["key"]})
            self.assertEqual(second["counts"]["clean_reusable"], 1)

    def test_a_full_campaign_publishes_nothing_for_a_never_reused_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._never_fixture(Path(directory))
            store = SmokePassStore(fixture.root / "proofs")
            green = {"smoke_results": [{"smoke": smoke, "verdict": "passed", "attempts": [{"output": "3 passed", "verdict": "passed"}]} for smoke in (_FIRST, self._LISTED)]}
            with fixture.patches(), patch("quality_gates.smoke_identity.delta_entries", return_value=[]), patch("quality_gates.smoke_proof.SmokePassStore", return_value=store):
                context = prepare_full_proofs(fixture.root, fixture.candidate.commit, principal="campaign", attribution={})
                proofs = finish_full_proofs(fixture.root, context, green, {"state": "verified"}, principal="campaign")
            self.assertEqual({row["smoke"]: row["proof"] is None for row in proofs}, {_FIRST: False, self._LISTED: True})
            self.assertEqual(len([path for path in store.root.iterdir() if len(path.name) == 64]), 1)

    def test_a_failed_and_a_skipped_smoke_run_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            _register(fixture, (_FIRST, _SECOND))
            store = SmokePassStore(fixture.root / "proofs")

            def execute(_entries: list[str]) -> dict[str, Any]:
                return {
                    "exit_code": 1,
                    "import_environment": {"state": "verified"},
                    "smoke_results": {
                        _FIRST: {"verdict": "failed", "attempts": [{"output": "original RED", "verdict": "failed"}]},
                        _SECOND: {"verdict": "passed", "attempts": [{"output": "SKIPPED: no database", "verdict": "passed"}]},
                    },
                }

            with fixture.patches():
                runs = [run_selected_smokes(fixture.root, fixture.request(), store=store, principal="fixture", executor=execute) for _ in range(2)]
            self.assertEqual([self._counts(run) for run in runs], [(2, 0), (2, 0)])

    def test_the_caller_is_not_part_of_the_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            store = SmokePassStore(fixture.root / "proofs")
            with fixture.patches():
                keys = [select_smokes(fixture.root, {**fixture.request(), "repository_id": caller}, store=store)["selected"][0]["key"] for caller in ("fixture-repository", "another-repository")]
            self.assertEqual(keys[0], keys[1])

    def test_the_real_never_reuse_list_names_registered_smokes_with_reasons(self) -> None:
        listed = smoke_identity.parse_never_reuse((_ROOT / smoke_identity.NEVER_REUSE_RELPATH).read_bytes())
        registered = set(run_smokes._read_register(run_smokes._DEFAULT_REGISTER))
        self.assertTrue(listed)
        self.assertEqual(sorted(set(listed) - registered), [])
        self.assertTrue(all(reason for reason in listed.values()))
        self.assertLessEqual(10 * len(listed), len(registered))


if __name__ == "__main__":
    unittest.main()
