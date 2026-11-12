#!/usr/bin/env python3
from __future__ import annotations

import io
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "quality_gates")]

from quality_gates import run_smokes  # noqa: E402
from quality_gates.smoke_proof import TRUNCATED_OUTPUT_MARKER  # noqa: E402

# A deliberately small retained tail, so a short child overflows it.
_SMALL_TAIL_BYTES = "64"


def report(output: str) -> str:
    stream = io.StringIO()
    started = time.monotonic()
    with redirect_stdout(stream):
        run_smokes._report_result("fake_smoke.py", "failed", output, time.monotonic() - started, [], [], [])
    return stream.getvalue()


def collect(source: str) -> tuple[str, str, int]:
    """Run a short child through the runner's actual collector path."""
    with tempfile.TemporaryDirectory() as directory:
        smoke = Path(directory) / "fake_smoke.py"
        smoke.write_text(source, encoding="utf-8")
        return run_smokes._run_one(Path(sys.executable), smoke)


_EARLY_FAILURE = "print('FAIL early assertion'); [print('later-' + str(i)) for i in range(20)]; raise SystemExit(1)\n"


class FailureMarkerRetention(unittest.TestCase):
    def test_fake_failure_survives_twenty_later_lines(self) -> None:
        verdict, collected, _exit_code = collect(_EARLY_FAILURE)
        self.assertEqual(verdict, "failed")
        output = report(collected)
        self.assertIn("FAIL early assertion", output)
        self.assertIn("later-19", output)
        self.assertIn("lines elided", output)

    def test_omitted_output_and_cancellation_are_disclosed(self) -> None:
        with patch.dict(os.environ, {"SMOKE_PROCESS_TAIL_BYTES": _SMALL_TAIL_BYTES}):
            verdict, collected, _exit_code = collect(_EARLY_FAILURE)
        self.assertEqual(verdict, "failed")
        self.assertTrue(collected.startswith(f"{TRUNCATED_OUTPUT_MARKER} stdout retained the last {_SMALL_TAIL_BYTES} of "), collected)
        self.assertNotIn("FAIL early assertion", collected)
        self.assertIn(f"{TRUNCATED_OUTPUT_MARKER} stdout retained the last {_SMALL_TAIL_BYTES} of ", report(collected))
        cancelled = report("\n".join(["CANCELLED: battery interrupted; real exit code -15", *["later-" + str(i) for i in range(40)]]))
        self.assertIn("CANCELLED: battery interrupted; real exit code -15\n", cancelled)
        self.assertIn("lines elided", cancelled)

    def test_all_markers_survive_bounded_nonmarker_context(self) -> None:
        lines = ["noise-" + str(i) for i in range(200)]
        for i in range(0, 200, 2):
            lines[i] = "FAIL assertion-" + str(i)
        output = report("\n".join(lines))
        for i in range(0, 200, 2):
            self.assertIn("FAIL assertion-" + str(i) + "\n", output)
        self.assertLessEqual(sum("noise-" in line for line in output.splitlines()), 24)
        self.assertIn("lines elided", output)

    def test_house_markers_and_repeated_text_keep_original_order(self) -> None:
        lines = ["  ✗ first", "noise", "❌ second", "FAIL duplicate", *["noise"] * 30, "FAIL duplicate", "last"]
        output = report("\n".join(lines))
        self.assertIn("✗ first", output)
        self.assertIn("❌ second", output)
        self.assertEqual(output.count("FAIL duplicate"), 2)
        self.assertLess(output.index("✗ first"), output.index("❌ second"))
        self.assertTrue(output.rstrip().endswith("last"))

    def test_single_failure_print_keeps_marker(self) -> None:
        output = "\n".join(["FAIL early assertion", *["later-" + str(i) for i in range(20)]])
        stream = io.StringIO()
        failures: list[str] = []
        with redirect_stdout(stream):
            run_smokes._report_result("fake.py", "failed", output, 0.1, [], failures, [])
        printed = stream.getvalue()
        self.assertEqual(failures, ["fake.py"])
        self.assertEqual(printed.count("FAIL early assertion"), 1)
        self.assertIn("FAIL early assertion", printed)
        self.assertIn("lines elided", printed)


if __name__ == "__main__":
    print(f"module.__file__: {run_smokes.__file__}", flush=True)
    unittest.main()
