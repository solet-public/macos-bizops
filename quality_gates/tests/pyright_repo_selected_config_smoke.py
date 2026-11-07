#!/usr/bin/env python3
"""Pass-path proof for the selected-test Pyright overlay (iss_1f392041).

Run:

    .venv/bin/python3 quality_gates/tests/pyright_repo_selected_config_smoke.py

WHAT THIS CLOSES. ``pyright_repo.selected_config`` lifts the config's tests
exclusion so an explicitly selected test file is type-checked, by writing an
overlay config and pointing Pyright at it with ``--project``. The overlay is
deliberately created OUTSIDE the repository, and Pyright resolves a relative
path setting against the config that states it -- so the repository's
``venvPath = "."`` and its relative ``extraPaths``/``include`` resolved against
a temporary directory, and the project root Pyright searches for top-level
packages moved there too. Every import in a selected ``quality_gates/tests``
file was therefore reported unresolved, and the pre-commit hook
(``.githooks/lib/commit_quality_checks``) refused the commit. 83 commits since
2026-10-01 touch ``quality_gates/tests/*.py``, so no author on that path could
pass the gate: a refusal with no reachable pass condition, which rul_becb4bed
forbids.

RED-FIRST CONTROL, with the real Pyright and no mock. ``case_overlay_shape``
builds the PRE-FIX overlay payload -- ``extends`` plus ``exclude`` only, exactly
what the code wrote before -- and asserts the real Pyright reports unresolved
imports with it; then builds the payload the current code produces and asserts
the same file comes back clean. If someone reverts the fix, the second half
fails. If someone "fixes" it by weakening the gate, ``case_refusal_preserved``
fails, because a genuine strict-mode error in a selected test file must still be
reported.

PASS CONDITION (rul_becb4bed), restated: running the real
``quality_gates/pyright_repo.py`` over an unmodified ``quality_gates/tests``
file exits 0 with no ``reportMissingImports``. RUNNING COMPONENT AND SCHEDULE:
``quality_gates/pyright_repo.py``, invoked by the tracked pre-commit hook on
every commit in every clone that has run ``deployment/scripts/setup_clone.sh``,
so the condition is exercised continuously in the shipped configuration and
nothing new has to be scheduled.

Offline and hermetic: no live solet, no network, no LM Studio. Every fixture is
a TemporaryDirectory; nothing mutates git or the shared checkout.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

# Imported as a package member, not as a flat sibling: the selected-test overlay
# puts the repository ROOT on the search path, so `quality_gates.pyright_repo`
# resolves for the type checker as well as at runtime. A bare `import
# pyright_repo` would work at runtime and be unresolvable to Pyright.
from quality_gates import pyright_repo  # noqa: E402

#: An unmodified registered smoke under the excluded tests directory. Its own
#: imports are the thing that used to be unresolvable.
_SELECTED = "quality_gates/tests/smoke_proof_contract_smoke.py"

_PYRIGHT_TIMEOUT_S = 300
_FAILURES: list[str] = []


def _check(ok: bool, label: str) -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        _FAILURES.append(label)


def _summary(output: str) -> str:
    for line in reversed(output.strip().splitlines()):
        if "error" in line and "informations" in line:
            return line.strip()
    return output.strip().splitlines()[-1] if output.strip() else "(no output)"


def _run_pyright(overlay: Path, target: Path) -> subprocess.CompletedProcess[str]:
    """Run the REAL Pyright the gate runs, against one overlay config."""
    argv = [
        str(pyright_repo.node_path(_REPO_ROOT)),
        str(pyright_repo.installed_pyright_script()),
        "--project",
        str(overlay),
        str(target),
    ]
    return subprocess.run(argv, capture_output=True, text=True, cwd=_REPO_ROOT, timeout=_PYRIGHT_TIMEOUT_S, check=False)


def _selection() -> tuple[Path, Path, dict[str, Any], list[str]]:
    """The config, its effective config, its settings and the retained excludes."""
    config = pyright_repo.project_config(_REPO_ROOT, None)
    effective, settings = pyright_repo.effective_config(config)
    selected = [Path(os.path.abspath(_REPO_ROOT / _SELECTED))]
    retained = [pattern for pattern in settings.get("exclude", []) if not pyright_repo.suppresses_test(pattern, effective.parent, selected)]
    return config, effective, settings, retained


def case_overlay_shape() -> None:
    """The red/green control: the old payload fails, the production payload passes."""
    print("\nCase 1 — pre-fix overlay payload is RED, production payload is GREEN")
    config, effective, settings, retained = _selection()
    _check(retained != list(settings.get("exclude", [])), "the fixture's selected path really is covered by an excluded tests pattern")

    target = _REPO_ROOT / _SELECTED
    _check(target.is_file(), f"the selected fixture file exists ({_SELECTED})")

    # The overlay must sit outside the repository, where the production code
    # places it; _REPO_ROOT.parent is selected_config's own fallback.
    with tempfile.TemporaryDirectory(prefix="pyright-overlay-regression-", dir=_REPO_ROOT.parent) as directory:
        overlay = Path(directory) / "pyrightconfig.json"
        relative_origin = Path(os.path.relpath(effective.parent, overlay.parent))

        # PRE-FIX: extends + exclude only, exactly what was written before
        # iss_1f392041 was fixed.
        overlay.write_text(json.dumps({"extends": str(effective), "exclude": [str(relative_origin / pattern) for pattern in retained]}), encoding="utf-8")
        before = _run_pyright(overlay, target)
        _check(before.returncode != 0, f"pre-fix payload still reproduces the defect (exit {before.returncode}: {_summary(before.stdout)})")
        _check("reportMissingImports" in before.stdout, "pre-fix failure is unresolved imports, not a real typing error")

        # PRODUCTION: the payload overlay_payload actually writes.
        payload = pyright_repo.overlay_payload(config, effective, settings, overlay.parent, retained)
        overlay.write_text(json.dumps(payload), encoding="utf-8")
        after = _run_pyright(overlay, target)
        _check(after.returncode == 0, f"production payload type-checks the same file cleanly (exit {after.returncode}: {_summary(after.stdout)})")
        _check("reportMissingImports" not in after.stdout, "no import is reported unresolved under the production payload")
        _check("because it is not relative" not in after.stdout + after.stderr, "Pyright drops no path setting as 'not relative'")


def case_sibling_imports_resolve() -> None:
    """Own-directory sibling imports resolve, which is what the execution root buys.

    ``run_smokes`` and ``code_quality_check`` import flat siblings
    (``battery_summary``, ``gate_scope``) that they make importable at runtime by
    inserting their own directory on ``sys.path``. Those resolve only because the
    overlay names the repository as Pyright's execution root.
    """
    print("\nCase 2 — repository-local and sibling imports resolve under the production payload")
    config, effective, settings, retained = _selection()
    siblings = [_REPO_ROOT / "quality_gates/run_smokes.py", _REPO_ROOT / "quality_gates/code_quality_check.py"]
    with tempfile.TemporaryDirectory(prefix="pyright-overlay-siblings-", dir=_REPO_ROOT.parent) as directory:
        overlay = Path(directory) / "pyrightconfig.json"
        overlay.write_text(json.dumps(pyright_repo.overlay_payload(config, effective, settings, overlay.parent, retained)), encoding="utf-8")
        for target in siblings:
            result = _run_pyright(overlay, target)
            _check(result.returncode == 0, f"{target.name} type-checks cleanly (exit {result.returncode}: {_summary(result.stdout)})")
            _check("reportMissingImports" not in result.stdout, f"{target.name} has no unresolved sibling import")


def case_settings_are_rebased_correctly() -> None:
    """Absolute where Pyright wants absolute, relative where it requires relative."""
    print("\nCase 3 — each path setting is rebased the way Pyright accepts it")
    config, effective, settings, retained = _selection()
    origin = effective.parent
    overlay_directory = _REPO_ROOT.parent / "pyright-overlay-fixture"
    payload = pyright_repo.overlay_payload(config, effective, settings, overlay_directory, retained)

    _check(Path(payload["venvPath"]).is_absolute(), f"venvPath is absolute (got {payload['venvPath']!r})")
    _check(Path(payload["venvPath"]) == origin, "venvPath points at the real config's own directory, where the venv is")
    _check((Path(payload["venvPath"]) / settings["venv"]).is_dir(), "venvPath/venv resolves to a real directory")
    _check(all(Path(entry).is_absolute() for entry in payload["extraPaths"]), "every extraPaths entry is absolute")
    _check(all(Path(entry).exists() for entry in payload["extraPaths"]), "every extraPaths entry exists on disk")
    # include/ignore/strict must stay relative: Pyright warns and DROPS an
    # absolute entry, which would silently narrow the analyzed set.
    _check(all(not Path(entry).is_absolute() for entry in payload["include"]), f"every include entry stays relative (got {payload['include'][:1]!r})")
    _check(all((overlay_directory / entry).resolve().exists() for entry in payload["include"]), "every relative include entry resolves back to a real path from the overlay")
    _check("venv" not in payload, "the bare 'venv' NAME is left alone, because it is not a path")

    environments = payload["executionEnvironments"]
    roots = [environment.get("root") for environment in environments]
    _check(len(environments) == 1 and roots == [str(origin)], f"the execution root is the repository, not the overlay directory (got {roots!r})")
    _check("executionEnvironments" not in settings, "and it is injected only because the real config declares none")


def case_production_entry_point_is_clean() -> None:
    """The real CLI the hook runs, over an unmodified selected test file."""
    print("\nCase 4 — the real pyright_repo.py CLI exits 0 on an unmodified selected file")
    result = subprocess.run(
        [str(_REPO_ROOT / ".venv/bin/python3"), "quality_gates/pyright_repo.py", _SELECTED],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        env={**os.environ, "SOLET_NAME": os.environ.get("SOLET_NAME", "pyright-repo-overlay-fixture")},
        timeout=_PYRIGHT_TIMEOUT_S,
        check=False,
    )
    _check(result.returncode == 0, f"the gate's own command exits 0 (got {result.returncode}: {_summary(result.stdout)})")
    _check("Pyright selected-test config:" in result.stderr, "the overlay path really was taken, so this measures the selected-test path")
    _check("reportMissingImports" not in result.stdout, "no unresolved import is reported")


def case_refusal_preserved() -> None:
    """A genuine strict-mode error in a selected test file is STILL reported.

    The fix must not have bought a clean run by weakening the check. The probe
    file is written into the real excluded tests directory and removed again,
    because the exclusion-lifting path is only taken for a path the config
    actually excludes.
    """
    print("\nCase 5 — the refusal still fires on a real typing error")
    probe = _REPO_ROOT / "quality_gates/tests/_pyright_overlay_probe_smoke.py"
    probe.write_text("def broken(value: int) -> str:\n    return value\n", encoding="utf-8")
    try:
        result = subprocess.run(
            [str(_REPO_ROOT / ".venv/bin/python3"), "quality_gates/pyright_repo.py", str(probe.relative_to(_REPO_ROOT))],
            capture_output=True,
            text=True,
            cwd=_REPO_ROOT,
            env={**os.environ, "SOLET_NAME": os.environ.get("SOLET_NAME", "pyright-repo-overlay-fixture")},
            timeout=_PYRIGHT_TIMEOUT_S,
            check=False,
        )
    finally:
        probe.unlink(missing_ok=True)
    _check(result.returncode != 0, f"a genuine error in a selected test file is still a non-zero verdict (got {result.returncode})")
    _check("reportReturnType" in result.stdout, f"the real defect is named (got {_summary(result.stdout)})")


def main() -> int:
    print("Pyright selected-test overlay config smoke\n")
    case_overlay_shape()
    case_sibling_imports_resolve()
    case_settings_are_rebased_correctly()
    case_production_entry_point_is_clean()
    case_refusal_preserved()
    if _FAILURES:
        print(f"\nFAILED ({len(_FAILURES)}): {_FAILURES}")
        return 1
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
