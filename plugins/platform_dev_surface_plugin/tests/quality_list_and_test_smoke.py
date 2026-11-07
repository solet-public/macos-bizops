#!/usr/bin/env python3
"""quality_service.list_gates + run_test acceptance smoke (no pytest, offline).

Covers B3 §5's ``quality_list_and_test_smoke`` row: list_gates maps to the REAL
server-side gate registry + the REAL smoke register, the coherence trio is
enumerated as run-via-aggregate, run_test is allowlist-only against the
register, and a single registered smoke runs end-to-end through the server-side
argv.

THE REGISTER IS A UNION (prb_ae6d81ea): ``quality_gates/gate_smokes.txt`` plus
every ``*.txt`` fragment under ``quality_gates/gate_smokes.d/``, which is how a
candidate registers a smoke without editing the one shared file. This smoke
holds list_gates to that union, and holds the union to being a SUPERSET of the
main file in which every main-file entry keeps its position -- so the fragment
route cannot quietly drop or reorder an existing entry.

RED-FIRST security pin: if the register-membership guard in
``QualityOperations.run_test`` were removed, ``run_test('not/in/register.py')``
would stop raising and this smoke would fail. The register IS the allowlist
boundary for run_test.

Run from repo root:
    .venv/bin/python3 plugins/platform_dev_surface_plugin/tests/quality_list_and_test_smoke.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[3]
# app_home anchor for locate_repo_root: <worktree>/profile (parent == _REPO_ROOT).
_APP_HOME = _REPO_ROOT / "profile"
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(_REPO_ROOT / "plugins" / "platform_dev_surface_plugin" / "src"))

from platform_dev_surface_plugin.quality import gate_registry  # noqa: E402
from platform_dev_surface_plugin.quality.operations import (  # noqa: E402
    QualityGateError,
    QualityOperations,
)
from platform_dev_surface_plugin.repo_root import locate_repo_root  # noqa: E402

_SELF_REGISTERED_SMOKE = "plugins/platform_dev_surface_plugin/tests/quality_run_gate_smoke.py"

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _main_file_entries(repo_root: Path) -> list[str]:
    """Entries in the main register file ONLY, parsed here rather than imported.

    Keeping this hand-rolled parse is deliberate: it is the independent
    measurement the union is compared against, so a defect in the shared loader
    cannot make this smoke agree with itself.
    """
    register = repo_root / "quality_gates" / "gate_smokes.txt"
    entries: list[str] = []
    for raw in register.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            entries.append(line)
    return entries


def _fragment_entries(repo_root: Path) -> list[str]:
    """Entries contributed by the fragment files, in the same independent way."""
    directory = repo_root / "quality_gates" / "gate_smokes.d"
    if not directory.is_dir():
        return []
    entries: list[str] = []
    for fragment in sorted((path for path in directory.iterdir() if path.is_file() and path.name.endswith(".txt")), key=lambda path: path.name):
        for raw in fragment.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if line:
                entries.append(line)
    return entries


def _real_register_entries(repo_root: Path) -> list[str]:
    """The effective register: the main file then its fragments, de-duplicated."""
    seen: dict[str, None] = {}
    for entry in (*_main_file_entries(repo_root), *_fragment_entries(repo_root)):
        seen.setdefault(entry, None)
    return list(seen)


def _test_list_gates_maps_real_registry() -> None:
    ops = QualityOperations(locate_repo_root(_APP_HOME), os.environ["SOLET_NAME"])
    listing = ops.list_gates()
    registry = gate_registry.all_gates()
    names = {g["name"] for g in listing["gates"]}
    _check(
        {"code_quality", "service_interface_ast", "god_class", "radon_cc"} <= names,
        "list_gates enumerates the expected gate names",
    )
    by_name = {g["name"]: g for g in listing["gates"]}
    _check(
        by_name["god_class"]["directly_runnable"] is False and by_name["god_class"]["run_via"] == "code_quality",
        "coherence trio is enumerated as run-via the code_quality aggregate",
    )
    _check(
        by_name["code_quality"]["directly_runnable"] is True,
        "code_quality aggregate is directly runnable",
    )
    _check(
        all(spec.timeout_seconds is None for spec in registry),
        "every real GateSpec advertises no execution deadline",
    )
    _check(
        all(gate["timeout_seconds"] is None for gate in listing["gates"]),
        "every real list_gates row exposes null timeout_seconds",
    )
    _check(
        {gate["name"] for gate in listing["gates"]} == {spec.name for spec in registry},
        "list_gates covers every real registry gate",
    )


def _test_list_gates_maps_real_register() -> None:
    repo_root = locate_repo_root(_APP_HOME)
    ops = QualityOperations(repo_root, os.environ["SOLET_NAME"])
    listing = ops.list_gates()
    real = _real_register_entries(repo_root)
    main_only = _main_file_entries(repo_root)
    _check(
        listing["smoke_count"] == len(real),
        f"smoke_count ({listing['smoke_count']}) matches the real effective register ({len(real)})",
    )
    _check(listing["smokes"] == real, "smokes list mirrors the effective register (gate_smokes.txt then its fragments) verbatim")
    _check(
        listing["smokes"][: len(main_only)] == main_only,
        "every gate_smokes.txt entry keeps its exact position: the union is a prefix-preserving superset",
    )
    _check(
        set(main_only) <= set(listing["smokes"]) and len(set(listing["smokes"])) == len(listing["smokes"]),
        "the union contains every main-file entry and no duplicate",
    )
    _check(
        _SELF_REGISTERED_SMOKE in listing["smokes"],
        "this B3 smoke pair is present in the register",
    )


def _test_run_test_rejects_unregistered() -> None:
    ops = QualityOperations(locate_repo_root(_APP_HOME), os.environ["SOLET_NAME"])
    try:
        ops.run_test(smoke="plugins/platform_dev_surface_plugin/tests/___not_registered___.py")
    except QualityGateError:
        _check(True, "run_test(unregistered path) raises QualityGateError (allowlist pin)")
        return
    _check(False, "run_test(unregistered path) raises QualityGateError (allowlist pin)")


def _test_run_test_runs_registered_smoke() -> None:
    """Integration: run one registered smoke end-to-end through run_test."""
    ops = QualityOperations(locate_repo_root(_APP_HOME), os.environ["SOLET_NAME"])
    result = ops.run_test(smoke=_SELF_REGISTERED_SMOKE)
    expected_keys = {
        "target",
        "passed",
        "exit_code",
        "timed_out",
        "summary",
        "output",
        "truncated",
        "output_chars_total",
    }
    _check(
        expected_keys <= set(result.keys()),
        "run_test returns the full structured verdict shape",
    )
    _check(result["target"] == _SELF_REGISTERED_SMOKE, "verdict names the smoke run")
    _check(isinstance(result["exit_code"], int), "exit_code is an int")
    _check(
        result["passed"] == (result["exit_code"] == 0),
        "passed is derived from exit_code == 0",
    )
    _check(not result["timed_out"], "the registered smoke did not time out")
    _check(result["passed"], "the real registered smoke passed")
    _check(bool(result["output"]), "the real registered smoke produced output")


def _test_named_smoke_preserves_shared_runner_skip() -> None:
    ops = QualityOperations(locate_repo_root(_APP_HOME), os.environ["SOLET_NAME"])
    selected = {"exit_code": 0, "smoke_results": {_SELF_REGISTERED_SMOKE: {"verdict": "skipped", "attempts": [{"output": "dependency unavailable"}]}}}
    with patch("quality_gates.run_smokes.execute_selected", return_value=selected) as runner, patch("platform_dev_surface_plugin.quality.operations.run_bounded") as outer:
        result = ops.run_test(smoke=_SELF_REGISTERED_SMOKE)
        runner.assert_called_once_with([_SELF_REGISTERED_SMOKE])
        outer.assert_not_called()
    _check(result["exit_code"] == 77 and result["skipped"] and not result["passed"], "shared producer skip remains raw77 and disclosed")
    _check(result["output"] == "dependency unavailable", "shared producer output is retained")


def main() -> int:
    print("quality_service.list_gates + run_test acceptance smoke")
    _test_list_gates_maps_real_registry()
    _test_list_gates_maps_real_register()
    _test_run_test_rejects_unregistered()
    _test_run_test_runs_registered_smoke()
    _test_named_smoke_preserves_shared_runner_skip()

    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
