#!/usr/bin/env python3
"""Real installed-checker proof for explicitly selected test-file operands.

The proof needs Pyright installed in the invoking interpreter's environment.
A born seed does not install it (no new dependency, iss_b7d65ec8), so without
it this smoke exits the dedicated skip code, 77, with an UNMEASURED line and
the born-clone skip witness: a missing tool is disclosed, never a pass and
never a release blocker (rul_becb4bed).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
BAD = 'value: int = "wrong"\n'
GOOD = "value: int = 1\n"


class SelectedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="selected pyright ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "fixture with spaces"
        self.root.mkdir()
        self.tmp = Path(self.temporary.name) / "deep" / "temporary files" / "nested"
        self.tmp.mkdir(parents=True)
        self.write("quality_gates/pyright_repo.py", (ROOT / "quality_gates/pyright_repo.py").read_text())
        (self.root / ".venv").symlink_to(ROOT / ".venv", target_is_directory=True)
        self.write(
            "pyproject.toml",
            '[tool.pyright]\npythonVersion = "3.13"\ntypeCheckingMode = "strict"\nvenvPath = "."\nvenv = ".venv"\nextraPaths = ["lib"]\nstubPath = "stubs"\ninclude = ["src", "plugins", "quality_gates/tests"]\nexclude = ["plugins/**/tests", "quality_gates/tests", "disabled_plugins", "plugins/*/research/**", "**/.venv", "**/venv_*", "**/node_modules"]\n',
        )
        self.write("src/clean.py", GOOD)
        self.write("plugins/sample/tests/nested/test_selected.py", GOOD)
        self.write("quality_gates/tests/test_other.py", BAD)
        self.write("lib/fixture_library.py", "number: int = 42\n")
        self.write("stubs/fixture_stub.pyi", "number: int\n")

    def write(self, path: str, content: str) -> Path:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def run_checker(self, *arguments: str, errors: int = 0, files: int | None = None) -> tuple[dict[str, Any], str]:
        result = subprocess.run([sys.executable, str(self.root / "quality_gates/pyright_repo.py"), "--outputjson", *arguments], cwd=self.root, env=dict(os.environ, TMPDIR=str(self.tmp)), capture_output=True, text=True, check=False)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1 if errors else 0, output)
        self.assertNotIn("No source files found", output)
        report: dict[str, Any] = json.loads(result.stdout)
        self.assertEqual(report["summary"]["errorCount"], errors, output)
        if files is not None:
            self.assertEqual(report["summary"]["filesAnalyzed"], files, output)
        self.assertIn("Node verified:", result.stderr)
        self.assertIn("Pyright node interpreter:", result.stderr)
        for line in result.stderr.splitlines():
            if line.startswith("Pyright selected-test config: "):
                config = Path(line.removeprefix("Pyright selected-test config: "))
                self.assertFalse(config.exists(), "temporary config must be removed after success and checker failure")
                self.assertFalse(config.parent.exists())
                self.assertTrue(config.is_relative_to(self.tmp.resolve()), output)
                self.assertFalse(config.is_relative_to(self.root))
        self.assertEqual(list(self.tmp.iterdir()), [], "no temporary config survives")
        return report, result.stderr

    def assert_error_path(self, report: dict[str, Any], target: Path) -> None:
        failures = [item for item in report["generalDiagnostics"] if item["severity"] == "error"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(Path(failures[0]["file"]), target)
        self.assertEqual(failures[0]["rule"], "reportAssignmentType")

    def test_selected_test_file_reports_and_clears_type_error(self) -> None:
        target = self.write("plugins/sample/tests/nested/test_selected.py", BAD)
        report, stderr = self.run_checker(str(target.relative_to(self.root)), errors=1, files=1)
        self.assert_error_path(report, target)
        self.assertIn("Pyright selected-test config:", stderr)
        target.write_text(GOOD)
        self.run_checker(str(target), files=1)

    def test_mixed_paths_and_project_options(self) -> None:
        target = self.write("plugins/sample/tests/nested/test_selected.py", BAD)
        original = (self.root / "pyproject.toml").read_bytes()
        report, _ = self.run_checker("src/clean.py", str(target), "--project", "pyproject.toml", "--level", "error", errors=1, files=2)
        self.assert_error_path(report, target)
        target.write_text(GOOD)
        self.run_checker("--project=.", "src/clean.py", str(target), "--pythonversion", "3.13", files=2)
        # A custom JSONC project inherits root paths and contributes its own
        # exclusions and diagnostics. Its settings must survive the overlay.
        custom = self.write("config/custom.json", '{\n// project selection remains authoritative\n"extends": "../pyproject.toml", "reportUnusedVariable": false,\n}\n')
        target.write_text("def answer() -> int:\n    unused = 1\n    return 2\n")
        self.run_checker("-p", str(custom), "src/clean.py", str(target), files=2)
        custom.write_text('{"extends": "../pyproject.toml", "exclude": ["../plugins/**/tests"]}\n')
        target.write_text(BAD)
        operand = "plugins/sample/tests/nested/../nested/test_selected.py"
        report, _ = self.run_checker("--project", str(custom), "src/clean.py", operand, errors=1, files=2)
        self.assert_error_path(report, target)
        target.write_text(GOOD)
        self.run_checker("--project", str(custom), "src/clean.py", operand, files=2)
        self.assertEqual((self.root / "pyproject.toml").read_bytes(), original)

    def test_scoped_config_preserves_root_paths_and_exemptions(self) -> None:
        target = self.write("plugins/sample/tests/nested/test_selected.py", "from fixture_library import number\nfrom fixture_stub import number as stub_number\nvalue: int = number + stub_number\n")
        exempt = ["disabled_plugins/retired/tests/bad.py", "plugins/sample/research/tests/bad.py", "plugins/sample/.venv/tests/bad.py", "plugins/sample/venv_other/tests/bad.py", "plugins/sample/node_modules/tests/bad.py"]
        for path in exempt:
            self.write(path, BAD)
        self.run_checker(str(target), "quality_gates/tests", *exempt, files=1)
        # Lifting one test exclusion does not lift unrelated test directories.
        self.run_checker(str(target), "quality_gates/tests/test_other.py", errors=1, files=2)
        self.assertTrue((self.root / ".venv/bin/python3").is_file())

    def test_no_file_mode_and_option_values_keep_original_config(self) -> None:
        _, stderr = self.run_checker(files=1)
        self.assertNotIn("Pyright selected-test config:", stderr)
        interpreter = self.root / "plugins/sample/tests/python.py"
        interpreter.symlink_to(Path(sys.executable))
        _, stderr = self.run_checker("--pythonpath", str(interpreter), "--project", "pyproject.toml", files=1)
        self.assertNotIn("Pyright selected-test config:", stderr)


SKIP_EXIT_CODE = 77
PYRIGHT_SKIP_WITNESS = 'BORN_CLONE_SKIP_WITNESS={"reason": "ambient_executable_missing", "executable": "pyright"}'


def pyright_unavailable() -> str | None:
    """The checker's own reason Pyright cannot run from this interpreter, or None."""
    spec = importlib.util.spec_from_file_location("pyright_repo_under_test", ROOT / "quality_gates/pyright_repo.py")
    assert spec is not None and spec.loader is not None
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    try:
        checker.installed_pyright_script()
    except checker.PyrightUnavailableError as exc:
        return str(exc)
    return None


if __name__ == "__main__":
    print(f"module.__file__: {ROOT / 'quality_gates/pyright_repo.py'}", flush=True)
    missing = pyright_unavailable()
    if missing is not None:
        print(PYRIGHT_SKIP_WITNESS)
        print(f"SKIP: UNMEASURED -- {missing}; the selected-tests Pyright proof did not run.")
        raise SystemExit(SKIP_EXIT_CODE)
    unittest.main()
