"""Runtime and source mutations; plugins/github_midwife_plugin/tests/recovery_runtime_smoke.py.

No platform or developer dependencies. Mutants are real alternate source trees.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from recovery_test_support import COMMON, recovery  # pyright: ignore[reportMissingImports] - retained sibling


def grammar_stdlib() -> None:
    assert set(recovery.MODULES) == {path.name for path in COMMON.glob("*.py")}
    for name in sorted(path.name for path in COMMON.glob("*.py")):
        _check_imports(name)
    interpreter = shutil.which("python3.11")
    if interpreter is None:
        print("UNMEASURED: Python 3.11 runtime absent; grammar and stdlib census PASS")
    else:
        result = subprocess.run([interpreter, str(Path(__file__).with_name("working_tree_recovery_smoke.py").resolve()), "--probe", "test_capture_layers_custody_git_unchanged_and_safe_output"], capture_output=True, text=True, check=False)
        assert result.returncode == 0, (result.stdout, result.stderr)
        print("PASS: real Python 3.11 public handle/CLI capture")


def mutations() -> None:
    cases = (
        ("classifier_negation", "command_walk.py", "return tuple(_walk(command, cwd.resolve(), cwd.resolve(), query, 0))", "return ()", "working_tree_recovery_smoke.py", "test_capture_layers_custody_git_unchanged_and_safe_output"),
        ("restore_overwrite", "recovery_restore.py", "if current.mode is not None and (empty or (current.mode, current.oid) not in known):", "if False:", "working_tree_recovery_smoke.py", "test_restore_never_overwrites_newer_edit"),
        (
            "restore_atomic_publish_overwrite",
            "recovery_restore.py",
            "preserved = _publish_existing(parent, temporary, parts[-1], (latest, identity), object_format)",
            'os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)\n                preserved = ""',
            "working_tree_recovery_smoke.py",
            "test_restore_atomic_publish_preserves_completed_writer",
        ),
        ("prune_undelivered", "recovery_retention.py", "if not is_delivered:\n            return", "if False:\n            return", "recovery_lifecycle_smoke.py", "test_prune_undelivered_never_pruned_and_newest_turn_protected"),
        ("ignored_bytes_leak", "recovery_refs.py", "else not (current[path] or carried[path])", "else True", "recovery_refs_smoke.py", "test_visible_paths_p1_p3e_whole_store_and_finally"),
    )
    for name, module, before, after, script, probe in cases:
        baseline = subprocess.run([sys.executable, str(Path(__file__).with_name(script).resolve()), "--probe", probe], capture_output=True, text=True, check=False)
        assert baseline.returncode == 0, (name, "baseline must pass", baseline.stdout, baseline.stderr)
        with tempfile.TemporaryDirectory(prefix=f"recovery-mutant-{name}-") as temporary:
            root = Path(temporary)
            for source in COMMON.glob("*.py"):
                raw = source.read_text()
                if source.name == module:
                    assert raw.count(before) == 1, (name, raw.count(before))
                    raw = raw.replace(before, after)
                (root / source.name).write_text(raw)
            environment = {**os.environ, "RECOVERY_TEST_COMMON": str(root)}
            result = subprocess.run([sys.executable, str(Path(__file__).with_name(script).resolve()), "--probe", probe], capture_output=True, text=True, env=environment, check=False)
            assert result.returncode != 0 and "FAIL:" in result.stderr, (name, result.stdout, result.stderr)
            print(f"PASS: killed named mutation {name}")


def _check_imports(name: str) -> None:
    tree = ast.parse((COMMON / name).read_bytes(), feature_version=(3, 11))
    allowed = {path.stem for path in COMMON.glob("*.py")}
    for node in ast.walk(tree):
        names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
        assert all(name.split(".")[0] in sys.stdlib_module_names | allowed for name in names), (name, names)


if __name__ == "__main__":
    grammar_stdlib()
    mutations()
