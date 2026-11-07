#!/usr/bin/env python3
"""Tier U: native API/catalog lockstep, principals and shared producer calls."""

# ruff: noqa: E402
from __future__ import annotations

import inspect
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT, ROOT / "ananta/src", ROOT / "plugins/platform_dev_surface_plugin/src"):
    sys.path.insert(0, str(source))
from ananta.interfaces.quality_service_interface import QualityServiceInterface
from ananta.services.quality_service.interfaces.public import QualityServicePublicAPI
from platform_dev_surface_plugin.bounded_subprocess import TIMEOUT_EXIT_CODE, SubprocessResult, run_bounded
from platform_dev_surface_plugin.plugin import PlatformDevSurfacePlugin
from platform_dev_surface_plugin.quality import operations
from platform_dev_surface_plugin.quality.operations import QualityGateError, QualityOperations

from quality_gates import run_smokes
from quality_gates.smoke_proof import campaign_results

VERBS = ("select_smokes", "run_selected_smokes", "get_smoke_proof", "list_smoke_proofs", "get_smoke_proof_history", "correct_smoke_proof", "clear_smoke_proof", "retire_smoke_proof", "reinstate_smoke_proof", "ensure_full_smoke_schedule", "trigger_full_smoke_battery")


class SurfaceTests(unittest.TestCase):
    def test_gate_has_no_outer_deadline_and_preserves_real_child_results(self) -> None:
        self.assertEqual(Path(inspect.getfile(operations)).resolve(), ROOT / "plugins/platform_dev_surface_plugin/src/platform_dev_surface_plugin/quality/operations.py")
        self.assertEqual(Path(inspect.getfile(run_bounded)).resolve(), ROOT / "plugins/platform_dev_surface_plugin/src/platform_dev_surface_plugin/bounded_subprocess.py")
        ops = QualityOperations(ROOT, "fixture-solet")
        for code in (0, 3, 77, 124):
            with self.subTest(code=code):
                argv = [str(ROOT / ".venv/bin/python3"), "-c", f"import os, sys; print(os.environ['SOLET_NAME']); print('x' * 20000); print('child-final'); sys.exit({code})"]
                with patch.object(operations.gate_registry, "build_gate_argv", return_value=argv), patch.object(operations, "run_bounded", wraps=run_bounded) as runner:
                    result = ops.run_gate("service_interface_ast")
                self.assertIsNone(runner.call_args.kwargs["timeout"])
                self.assertEqual(result["exit_code"], code)
                self.assertEqual(result["harness_failure"], code == 124)
                self.assertEqual(result["passed"], code == 0)
                self.assertFalse(result["skipped"])
                self.assertFalse(result["timed_out"])
                self.assertTrue(result["truncated"])
                self.assertGreater(result["output_chars_total"], len(result["output"]))
                self.assertEqual(result["summary"], "child-final")
        with patch.object(operations, "run_bounded") as runner:
            with self.assertRaises(QualityGateError):
                ops.run_gate("___not_allowlisted___")
            runner.assert_not_called()

    def test_suite_argv_and_advisory_without_executing_suite(self) -> None:
        ops = QualityOperations(ROOT, "fixture-solet")
        budget = {"seconds": 1, "basis": "fixture estimate", "evidence": None, "advisory": "timing UNKNOWN"}
        child = SubprocessResult(exit_code=124, output="raw failure", truncated=False, output_chars_total=11, timed_out=False)
        with patch.object(operations, "run_bounded", return_value=child) as runner, patch("quality_gates.full_smoke_campaign.campaign_budget", return_value=budget):
            result = ops.run_test()
        runner.assert_called_once()
        self.assertIsNone(runner.call_args.kwargs["timeout"])
        argv = runner.call_args.args[0]
        self.assertEqual(argv[:4], [str(ROOT / ".venv/bin/python3"), str(ROOT / "quality_gates/run_smokes.py"), "--register", str(ROOT / "quality_gates/gate_smokes.txt")])
        self.assertEqual((argv[4], Path(argv[5]).name, len(argv)), ("--results-output", "results.json", 6))
        self.assertEqual(runner.call_args.kwargs["extra_env"], {"SOLET_NAME": "fixture-solet"})
        self.assertEqual(result["exit_code"], 124)
        self.assertTrue(result["harness_failure"])
        # The child published no campaign file, so its defects are unknown, not none.
        self.assertIsNone(result["harness_defects"])
        self.assertFalse(result["passed"])
        self.assertFalse(result["skipped"])
        self.assertEqual(result["output"], "raw failure")
        self.assertEqual(result["budget"]["seconds"], budget["seconds"])
        self.assertEqual(result["budget"]["advisory"], budget["advisory"])
        self.assertEqual(result["budget"]["enforcement"], "advisory_only")
        self.assertNotIn("enforcement", budget)

    def test_suite_reports_the_runner_campaign_harness_defects(self) -> None:
        ops = QualityOperations(ROOT, "fixture-solet")
        smoke = "suite/harness_smoke.py"
        budget = {"seconds": None, "basis": "fixture estimate", "evidence": None, "advisory": "timing UNKNOWN"}
        output = f"harness: ({smoke}) ADVISORY"

        def physical(argv: list[str], **_kwargs: Any) -> SubprocessResult:
            # The runner's own campaign writer, as the real child publishes it.
            campaign_results(Path(argv[argv.index("--results-output") + 1]), run_smokes._initial_smoke_results([smoke]), "passed", 0, run_smokes, harness=[smoke])
            return SubprocessResult(exit_code=0, output=output, truncated=False, output_chars_total=len(output), timed_out=False)

        with patch.object(operations, "run_bounded", side_effect=physical), patch("quality_gates.full_smoke_campaign.campaign_budget", return_value=budget):
            result = ops.run_test()
        # Exit 124 never becomes a verdict, so the suite still passes, with the defect named.
        self.assertEqual((result["passed"], result["exit_code"], result["harness_failure"], result["harness_defects"]), (True, 0, True, [smoke]))

    def test_named_smoke_harness_defect_keeps_its_raw_exit_and_is_listed(self) -> None:
        """A registered smoke's own exit 124 is a disclosed harness defect, never a clean exit 0."""
        ops = QualityOperations(ROOT, "fixture-solet")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "harness_smoke.py").write_text(f"print('own timeout wrapper gave up')\nraise SystemExit({TIMEOUT_EXIT_CODE})\n", encoding="utf-8")
            with (
                patch.dict(os.environ, {"RUN_SMOKES_BATTERY_RECEIPT_DIR": str(root / "receipts"), "SOLET_NAME": "fixture-solet"}),
                patch.object(ops, "_read_smoke_register", return_value=["harness_smoke.py"]),
                patch.object(run_smokes, "_REPO_ROOT", root),
                patch.object(run_smokes, "_read_register", return_value=["harness_smoke.py"]),
                patch.object(run_smokes, "_read_serial_only", return_value=frozenset()),
                patch.object(run_smokes, "_venv_python", return_value=Path(sys.executable)),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                result = ops.run_test(smoke="harness_smoke.py")
        observed = (result["exit_code"], result["harness_failure"], result["harness_defects"], result["passed"], result["skipped"])
        self.assertEqual(observed, (TIMEOUT_EXIT_CODE, True, ["harness_smoke.py"], False, False))
        self.assertIn(f"HARNESS-DEFECT: exit {TIMEOUT_EXIT_CODE}", result["output"])

    def test_native_signatures_and_process_docs(self) -> None:
        for verb in VERBS:
            with self.subTest(verb=verb):
                signatures = [str(inspect.signature(getattr(cls, verb))) for cls in (QualityServiceInterface, QualityServicePublicAPI, PlatformDevSurfacePlugin)]
                self.assertEqual(len(set(signatures)), 1)
                doc = json.loads((ROOT / f"ananta/knowledge_base/processes/quality_service/{verb}.json").read_text())
                self.assertEqual(doc["process_key"], f"service_interface::quality_service::{verb}")
                self.assertTrue(200 <= len(doc["embedding_description"]) <= 400)
        self.assertEqual(QualityServiceInterface.INTERFACE_VERSION, "1.2.0")

    def test_operations_call_shared_producer_and_principal(self) -> None:
        ops = QualityOperations(ROOT, os.environ.get("SOLET_NAME", "fixture-solet"))
        with patch("quality_gates.smoke_proof.select_smokes", return_value={"schema": "measured-fixture"}) as producer:
            self.assertEqual(ops.smoke_proof("select", request={"schema": "request"})["schema"], "measured-fixture")
            producer.assert_called_once_with(ROOT, {"schema": "request"})
        plugin = PlatformDevSurfacePlugin()
        plugin._quality = ops
        with self.assertRaises(PermissionError):
            plugin.run_selected_smokes({"schema": "request"})
        context = SimpleNamespace(calling_plugin="fixture", principal_id="owner", principal_kind="plugin")
        with patch("quality_gates.smoke_proof.run_selected_smokes", return_value={"status": "completed"}) as runner:
            self.assertEqual(plugin.run_selected_smokes({"schema": "request"}, call_context=context)["status"], "completed")
            self.assertEqual(runner.call_args.kwargs["principal"], "plugin:fixture")
        listing = ops.list_gates()
        self.assertTrue(set(VERBS) <= set(listing["proof_lifecycle"]))
        self.assertTrue({"radon_cc", "radon_mi", "god_class", "code_quality"} <= {row["name"] for row in listing["gates"]})


if __name__ == "__main__":
    unittest.main()
