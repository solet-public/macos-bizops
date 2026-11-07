#!/usr/bin/env python3
"""Tier U: frozen identity, relative/sibling/resource closure and invalidation."""

# ruff: noqa: E402
from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from quality_gates.smoke_identity import FrozenTree, closure, dependency_graph, safe_path, validate_request, verify_worktree
from quality_gates.smoke_pass_store import SmokePassStore
from quality_gates.smoke_proof import select_smokes
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

    def test_opaque_smoke_and_tool_targets_never_supply_reuse(self) -> None:
        for relative in ("suite/check_smoke.py", "quality_gates/opaque_tool.py"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                area = Path(directory)
                fixture = FrozenProofFixture(area / "repo")
                (area / "outside.py").write_text("print('1 passed')\n")
                path = fixture.root / relative
                if path.exists():
                    path.unlink()
                target = "../../outside.py"
                path.symlink_to(target)
                fixture.sources.pop(relative, None)
                entry = {"mode": "120000", "kind": "blob", "blob": hashlib.sha1(f"blob {len(target)}\0".encode() + target.encode(), usedforsecurity=False).hexdigest()}
                entries = {**fixture.candidate.entries, relative: entry}
                fixture.candidate = FrozenTree("b" * 40, "d" * 40, entries)
                old = fixture.base.entries.get(relative)
                fixture.delta.append({"path": relative, "status": "M" if old else "A", "base": old, "candidate": entry})
                fixture.delta.sort(key=lambda row: row["path"])
                profile = {"rule_version": "fixture-v1", "inputs": {relative: entry} if relative.startswith("quality_gates/") else {}}
                store = SmokePassStore(area / "proofs")
                with fixture.patches(), patch("quality_gates.smoke_proof.tool_profile", return_value=profile):
                    verify_worktree(fixture.root, fixture.candidate, [relative])
                    row = select_smokes(fixture.root, fixture.request(), store=store)["selected"][0]
                    self.assertFalse(row["identity_complete"])
                    self.assertTrue(any("opaque" in reason for reason in row["identity_advisories"]))
                    store.observe(row["key"], {"key": row["key"], "smoke": row["smoke"], "verdict": "passed", "exit_code": 0, "complete": True, "identity_complete": True, "attempt_count": 1}, expected_version=0, principal="fixture")
                    self.assertFalse(select_smokes(fixture.root, fixture.request(), store=store)["selected"][0]["reusable"])

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


if __name__ == "__main__":
    unittest.main()
