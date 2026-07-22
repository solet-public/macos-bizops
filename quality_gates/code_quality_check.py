#!/usr/bin/env python3
"""
Code Quality Pre-Commit Check for the homunculus platform
Runs pyright (strict), ruff, and the three coherence-aware gates
(god-class, radon cyclomatic complexity, radon maintainability index)
with their tracked-debt allowlists.

Exit codes:
  0 - All checks passed
  1 - Type errors found (pyright)
  2 - Lint errors found (ruff)
  3 - Type + lint errors
  4 - Syntax errors (critical, supersedes all)
  5 - Blocking-gate violations (non-allowlisted findings from any
      blocking structural gate: god_class_check / radon_cc_check /
      radon_mi_check / whole_tree_integration_gate /
      service_interface_ast_check / return_shape_gate; the summary
      line names the specific failing gate(s))

Note: codes 2 and 3 have historical drift from the original docstring
(which used 2 for "complexity issues"). The blocking-gate code 5 is
the new authoritative non-allowlisted-findings signal; the legacy 2
remains the ruff signal for backward compatibility. CI consumers and
the git-controller-commit SKILL should treat exit 5 as a non-allowlisted
blocking-gate block — the gap that let PluginBase 16-public-method
regression land in master without Step 7 catching it (2026-06-03).

Per CLAUDE.md "Per-file gate scope": gates run against the platform's
quality surface only — `ananta/src`, `ananta/tests` (operator-ruled
2026-07-07, GTE-05), `plugins/*/src`, `plugins/*/tests`, and
`quality_gates`. Operator-tooling (research/tools/migrations/
parity_tests/, workbench, deployment, top-level utilities) is excluded.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

_QUALITY_GATES_DIR = Path("quality_gates")
_GOD_CLASS_CHECK = _QUALITY_GATES_DIR / "god_class_check.py"
_GOD_CLASS_ALLOWLIST = _QUALITY_GATES_DIR / "god_class_allowlist.txt"
_RADON_CC_CHECK = _QUALITY_GATES_DIR / "radon_cc_check.py"
_RADON_CC_ALLOWLIST = _QUALITY_GATES_DIR / "radon_cc_allowlist.txt"
_RADON_MI_CHECK = _QUALITY_GATES_DIR / "radon_mi_check.py"
_RADON_MI_ALLOWLIST = _QUALITY_GATES_DIR / "radon_mi_allowlist.txt"
_W_INT_CHECK = _QUALITY_GATES_DIR / "whole_tree_integration_gate.py"
_W_INT_ALLOWLIST = _QUALITY_GATES_DIR / "whole_tree_integration_gate_allowlist.txt"
_W_WINT2_CHECK = _QUALITY_GATES_DIR / "wint2_driver_import_check.py"
_W_WINT2_ALLOWLIST = _QUALITY_GATES_DIR / "wint2_driver_import_allowlist.txt"
_W_WINT2_VAULT_KEY_CHECK = _QUALITY_GATES_DIR / "wint2_vault_key_declaration_check.py"
_W_WINT2_VAULT_KEY_ALLOWLIST = _QUALITY_GATES_DIR / "wint2_vault_key_declaration_allowlist.txt"
_SI_AST_CHECK = _QUALITY_GATES_DIR / "service_interface_ast_check.py"
_SI_AST_ALLOWLIST = _QUALITY_GATES_DIR / "service_interface_ast_allowlist.txt"
_RETURN_SHAPE_CHECK = _QUALITY_GATES_DIR / "return_shape_gate.py"
_RETURN_SHAPE_ALLOWLIST = _QUALITY_GATES_DIR / "return_shape_allowlist.txt"

# Exit codes returned by the three wrapper scripts (see each script's
# docstring): 0 = clean (or every finding allowlisted), 2 = one or more
# non-allowlisted findings, 64 = usage error (bad arguments / missing
# allowlist / non-existent paths).
_WRAPPER_OK = 0
_WRAPPER_BLOCKING = 2
_WRAPPER_USAGE_ERROR = 64

# Per-file gate scope: the platform's quality surface, mirroring the
# SKILL Step 6 SCOPE regex and the radon_cc/mi allowlists' coverage.
# Operator-tooling (research/tools/migrations/parity_tests, workbench,
# deployment) lives outside this scope per CLAUDE.md.
_PER_FILE_GATE_TOP_LEVEL = (Path("ananta/src"), Path("ananta/tests"), Path("quality_gates"))
_PER_FILE_GATE_PLUGIN_GLOBS = ("plugins/*/src", "plugins/*/tests")

# Path-segment prefix that flags a directory as a bundled venv (e.g.
# `.venv`, `.venv_cosyvoice`). Bundled venvs ship vendored library code
# (Cython, torch internals, etc.) that has unrelated coherence
# characteristics and is not part of the platform's quality surface.
_BUNDLED_VENV_PREFIX = ".venv"


def run_command(cmd: str, description: str, timeout: int = 60) -> tuple[bool, str]:
    """Run a command and return (success, output)."""
    print(f"🔍 {description}...")
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return False, f"Command timed out after {timeout} seconds"
    except Exception as e:
        return False, str(e)


# Paths whose staged changes trigger the prompt assembly fixture replay.
_PROMPT_ASSEMBLY_TRIGGERS = (
    "ananta/src/ananta/core/prompts/",
    "ananta/knowledge_base/processes/",
    "plugins/default_inference_plugin/src/default_inference_plugin/plugin.py",
    "plugins/default_inference_plugin/research/prompt_engineering/prompt_sets/",
)


def _staged_files_touch_prompt_assembly() -> bool:
    """Check if any staged files fall under prompt assembly paths."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        staged = result.stdout.strip().splitlines()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return any(
        f.startswith(trigger) for f in staged for trigger in _PROMPT_ASSEMBLY_TRIGGERS
    )


_PROMPT_SET_DIR = "plugins/default_inference_plugin/research/prompt_engineering/prompt_sets"
_RENDERER_SCRIPT = "plugins/default_inference_plugin/research/prompt_engineering/render_prompt_readable.py"


def _staged_prompt_json_files() -> list[str]:
    """Return staged *_prompt.json paths under the prompt set directory."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        staged = result.stdout.strip().splitlines()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    return [
        f for f in staged
        if f.startswith(_PROMPT_SET_DIR)
        and f.endswith("_prompt.json")
        and "/archive/" not in f
    ]


def _existing_readable_title(readable: Path) -> str | None:
    try:
        first_line = readable.read_text().splitlines()[0].strip()
    except Exception:
        return None
    return first_line or None


def _render_to_temp(venv_python: Path, renderer: Path, json_file: Path, title: str | None) -> str | None:
    """Run the renderer to a temp file; return generated content or None on failure."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tmp:
        tmp_path = tmp.name
    cmd = [str(venv_python), str(renderer), "--prompt", str(json_file), "--out", tmp_path]
    if title:
        cmd.extend(["--title", title])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    try:
        if result.returncode != 0:
            return None
        return Path(tmp_path).read_text()
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _check_single_readable(project_root: Path, venv_python: Path, renderer: Path, rel_json: str) -> str | None:
    """Return a stale-line marker for this JSON, or None if up-to-date."""
    json_file = project_root / rel_json
    if not json_file.is_file():
        return None
    readable = json_file.with_name(
        json_file.name.replace("_prompt.json", "_readable.txt")
    )
    if not readable.exists():
        return f"  missing: {readable.relative_to(project_root)}"

    # Pass the existing readable's title so the renderer uses the same
    # title as a normal regeneration (which reads it from out_path).
    # Without this, --out to a temp file means the renderer can't find
    # the existing title and falls through to a different default.
    existing_title = _existing_readable_title(readable)
    generated = _render_to_temp(venv_python, renderer, json_file, existing_title)
    if generated is None:
        return None
    if generated != readable.read_text():
        return f"  stale:   {readable.relative_to(project_root)}"
    return None


def _check_readable_freshness(project_root: Path, venv_python: Path) -> bool:
    """Check that *_readable.txt files match their staged *_prompt.json sources.

    Only checks JSON fixtures that are in the current staging area, so
    pre-existing drift in unmodified prompt sets does not block unrelated commits.

    Returns True if stale files were found (should block commit).
    """
    renderer = project_root / _RENDERER_SCRIPT
    if not renderer.is_file():
        return False

    staged_jsons = _staged_prompt_json_files()
    if not staged_jsons:
        return False

    print("\n📊 Readable Fixture Freshness Check...")
    stale = [
        marker
        for rel_json in staged_jsons
        if (marker := _check_single_readable(project_root, venv_python, renderer, rel_json)) is not None
    ]

    if not stale:
        print("✅ Readable fixtures up-to-date with staged JSON sources")
        return False

    print("❌ BLOCKING: Readable fixture files are out of sync with JSON sources!")
    for line in stale:
        print(line)
    print(f"\n💡 Regenerate with: {venv_python} {renderer} --prompt <json_file>")
    return True


def _run_fixture_replay_if_needed(project_root: Path, venv_python: Path) -> bool:
    """Run fixture replay tests if prompt assembly files are staged.

    Returns True if tests failed (should block commit).
    """
    if not _staged_files_touch_prompt_assembly():
        return False

    print("\n📊 Prompt Assembly Fixture Replay...")
    test_dir = "ananta/src/ananta/core/prompts/tests/"
    if not (project_root / test_dir).is_dir():
        print("⏭️  Fixture replay: test directory not present, skipping")
        return False
    pytest_cmd = f"cd {project_root} && {venv_python} -m pytest {test_dir} -q --tb=short 2>&1"
    success, output = run_command(pytest_cmd, "Running fixture replay tests", timeout=30)
    if success:
        print("✅ Fixture replay: all tests passed")
        return False

    print("❌ BLOCKING: Fixture replay tests failed!")
    # Show last 15 lines of output (summary + failures)
    lines = output.strip().splitlines()
    for line in lines[-15:]:
        print(f"   {line}")
    print(f"\n💡 Run: {venv_python} -m pytest {test_dir} -v")
    return True


@dataclass
class _CheckResults:
    has_type_errors: bool = False
    has_lint_errors: bool = False
    # Names of blocking structural gates (coherence trio, W-INT,
    # service-interface AST) that reported non-allowlisted findings —
    # the summary attributes the failure to each gate by name.
    failed_blocking_gates: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _GateSpec:
    name: str
    description: str
    script: Path
    allowlist: Path


_COHERENCE_GATES: tuple[_GateSpec, ...] = (
    _GateSpec(
        name="god_class",
        description="god-class structure",
        script=_GOD_CLASS_CHECK,
        allowlist=_GOD_CLASS_ALLOWLIST,
    ),
    _GateSpec(
        name="radon_cc",
        description="cyclomatic complexity",
        script=_RADON_CC_CHECK,
        allowlist=_RADON_CC_ALLOWLIST,
    ),
    _GateSpec(
        name="radon_mi",
        description="maintainability index",
        script=_RADON_MI_CHECK,
        allowlist=_RADON_MI_ALLOWLIST,
    ),
)

# Whole-tree integration gate (W-INT) runs differently from the per-file
# coherence gates: it walks the tree from its own anchors (service public.py
# files, plugin.py files, KB JSON dirs, scheduling call-sites), so no
# scope_paths argument is threaded through. Wrapper signature, exit codes
# (0/1/64) and allowlist semantics mirror the per-file gates; integration
# uses a separate runner to skip the scope-path argv splicing.
_W_INT_GATE = _GateSpec(
    name="whole_tree_integration",
    description="whole-tree integration",
    script=_W_INT_CHECK,
    allowlist=_W_INT_ALLOWLIST,
)
_W_INT_WRAPPER_BLOCKING = 1  # gate uses 1 (mirrors design doc), not 2

# Service-interface AST gate. Tree-walking (no scope-path argv splicing) —
# walks every ananta/src/ananta/services/*/interfaces/public.py and every
# ananta/knowledge_base/processes/<provider>/*.json on each invocation.
# Three checks per design v1 §1: (a) @service_interface_process(name=...)
# matches function __name__; (b) every @abstractmethod in a service-
# interface ABC carries @service_interface_process; (c) every JSON in
# processes/<provider>/ matches a registered decorator. Exit codes
# follow _WRAPPER_OK (0) / _WRAPPER_BLOCKING (2) — canonical pattern.
# See `workbench/2026-06-12_service_interface_ast_gate_design_v1.md`.
_SI_AST_GATE = _GateSpec(
    name="service_interface_ast",
    description="service-interface AST consistency",
    script=_SI_AST_CHECK,
    allowlist=_SI_AST_ALLOWLIST,
)

# Process return-shape gate (GTE-07, REL-11 follow-up). Tree-walking (no
# scope-path argv splicing) — AST-scans every @service_interface_process /
# @platform_process decorated def across ananta/src + plugins/*/src and
# fails on return annotations the process_call dispatch contract rejects
# (non-dict; dataclass DTOs allowed on the service path only, TypedDict
# allowed on both). Exit codes follow _WRAPPER_OK (0) / _WRAPPER_BLOCKING (2).
_RETURN_SHAPE_GATE = _GateSpec(
    name="return_shape",
    description="process return-shape",
    script=_RETURN_SHAPE_CHECK,
    allowlist=_RETURN_SHAPE_ALLOWLIST,
)

# W-INT Cycle 2 driver-import gate (W-WINT2-EARLY) ships in WARN mode per
# master plan §1.7. Findings print but do NOT contribute to the blocking
# verdict — the gate's role at Tier 0 is to ratchet against NEW driver-
# import drift while cleanup workstreams remove the existing bypass sites.
# W-WINT2-FINAL (Tier 7) drops the `--warn-only` flag to flip to fail-mode.
_W_WINT2_GATE = _GateSpec(
    name="wint2_driver_import",
    description="W-INT Cycle 2 driver-import (warn)",
    script=_W_WINT2_CHECK,
    allowlist=_W_WINT2_ALLOWLIST,
)

# W-INT Cycle 2 vault-key-declaration gate (W-PLUGIN-LAUNCH-KEYS sub-1)
# ships in WARN mode at sub-1 landing per brief §5.3. Findings print but
# do NOT contribute to the blocking verdict; sub-2 (W-VAULT-CALLER-ENFORCE)
# drops the `--warn-only` flag to flip to fail-mode in lockstep with the
# runtime readiness gate's mode flip.
_W_WINT2_VAULT_KEY_GATE = _GateSpec(
    name="wint2_vault_key_declaration",
    description="W-INT Cycle 2 vault-key declaration (warn)",
    script=_W_WINT2_VAULT_KEY_CHECK,
    allowlist=_W_WINT2_VAULT_KEY_ALLOWLIST,
)


def _per_file_gate_paths(project_root: Path) -> list[Path]:
    """Enumerate every in-scope `.py` file (CLAUDE.md "Per-file gate scope").

    Walks each scope root recursively, returning concrete `.py` paths and
    pruning any directory whose name starts with `.venv` (bundled venvs
    ship vendored library code outside the platform's quality surface).
    The wrappers receive the concrete file list as separate argv entries
    — never directories that would re-rglob into bundled venvs.
    """
    roots: list[Path] = []
    for top in _PER_FILE_GATE_TOP_LEVEL:
        candidate = project_root / top
        if candidate.exists():
            roots.append(candidate)
    for pattern in _PER_FILE_GATE_PLUGIN_GLOBS:
        roots.extend(sorted(project_root.glob(pattern)))

    py_files: list[Path] = []
    for root in roots:
        for path in root.rglob("*.py"):
            if any(part.startswith(_BUNDLED_VENV_PREFIX) for part in path.parts):
                continue
            py_files.append(path)
    return py_files


def _resolve_gate_artifacts(project_root: Path, gate: _GateSpec) -> tuple[Path, Path] | None:
    """Resolve and validate gate script + allowlist paths. Print + None on miss."""
    script = project_root / gate.script
    allowlist = project_root / gate.allowlist
    if not script.exists():
        print(f"❌ BLOCKING: gate script missing: {script}")
        return None
    if not allowlist.exists():
        print(f"❌ BLOCKING: allowlist missing: {allowlist}")
        return None
    return script, allowlist


def _invoke_gate_subprocess(
    venv_python: Path, script: Path, scope_paths: list[Path], allowlist: Path,
) -> subprocess.CompletedProcess[str] | str:
    """Invoke the wrapper script; return CompletedProcess or an error string."""
    argv = [
        str(venv_python),
        str(script),
        *[str(p) for p in scope_paths],
        "--allowlist",
        str(allowlist),
    ]
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return "timed out after 180s"
    except FileNotFoundError as exc:
        return f"cannot invoke gate: {exc}"


def _print_gate_blocking(gate_name: str, combined: str, venv_python: Path,
                         script: Path, allowlist: Path) -> None:
    """Print the blocking-finding report for one gate.

    Filters out [allowlisted] lines (they're tracked debt, not findings),
    radon mi's per-file `<path> - A (...)` / `<path> - B (...)` clean-rank
    chatter, and python-process boot noise. What survives is the actual
    non-allowlisted findings + the wrapper's summary line.
    """
    print(f"❌ BLOCKING: {gate_name} gate found non-allowlisted findings:")
    survivors: list[str] = []
    for line in combined.splitlines():
        if "[allowlisted]" in line:
            continue
        if " - A (" in line or " - B (" in line:
            continue
        if line.startswith("W") and "torch" in line:
            continue
        if line.strip():
            survivors.append(line)
    for line in survivors[:30]:
        print(f"   {line}")
    extra = max(0, len(survivors) - 30)
    if extra:
        print(f"   ... and {extra} more lines")
    print(f"\n💡 Re-run: {venv_python} {script} <paths> --allowlist {allowlist}")


def _print_gate_ok(gate_name: str, combined: str) -> None:
    """Print the clean-summary line for one gate (suppressing allowlisted noise)."""
    for line in combined.splitlines():
        if line.startswith("OK:"):
            print(f"✅ {line}")
            return
    print(f"✅ {gate_name} gate clean")


def _interpret_gate_result(
    gate_name: str, result: subprocess.CompletedProcess[str],
    venv_python: Path, script: Path, allowlist: Path,
) -> bool:
    """Translate a wrapper's exit code into the blocking/non-blocking verdict."""
    combined = (result.stdout + result.stderr).rstrip()
    if result.returncode == _WRAPPER_OK:
        _print_gate_ok(gate_name, combined)
        return False
    if result.returncode == _WRAPPER_BLOCKING:
        _print_gate_blocking(gate_name, combined, venv_python, script, allowlist)
        return True
    label = "usage error (exit 64)" if result.returncode == _WRAPPER_USAGE_ERROR else (
        f"unexpected exit {result.returncode}"
    )
    print(f"❌ BLOCKING: {gate_name} gate {label}:")
    print(combined or "(no output)")
    return True


def _run_coherence_gate(
    venv_python: Path, gate: _GateSpec, project_root: Path, scope_paths: list[Path],
) -> bool:
    """Run one coherence gate; return True if it has non-allowlisted findings."""
    artifacts = _resolve_gate_artifacts(project_root, gate)
    if artifacts is None:
        return True
    script, allowlist = artifacts

    print(f"\n📊 {gate.description.title()} Gate ({gate.name})...")
    result = _invoke_gate_subprocess(venv_python, script, scope_paths, allowlist)
    if isinstance(result, str):
        print(f"❌ BLOCKING: {gate.name} gate {result}")
        return True
    return _interpret_gate_result(gate.name, result, venv_python, script, allowlist)


def _check_coherence_gates(project_root: Path, venv_python: Path) -> list[str]:
    """Run all three coherence gates; return the names of gates that blocked."""
    scope_paths = _per_file_gate_paths(project_root)
    if not scope_paths:
        print("\n❌ BLOCKING: per-file gate scope resolved to zero paths")
        return ["per-file-scope-resolution"]
    return [
        gate.name
        for gate in _COHERENCE_GATES
        if _run_coherence_gate(venv_python, gate, project_root, scope_paths)
    ]


def _check_whole_tree_integration_gate(project_root: Path, venv_python: Path) -> bool:
    """Run the W-INT gate (structural mode). True iff non-allowlisted findings."""
    artifacts = _resolve_gate_artifacts(project_root, _W_INT_GATE)
    if artifacts is None:
        return True
    script, allowlist = artifacts

    print(f"\n📊 {_W_INT_GATE.description.title()} Gate ({_W_INT_GATE.name})...")
    argv = [str(venv_python), str(script), "--allowlist", str(allowlist)]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        print(f"❌ BLOCKING: {_W_INT_GATE.name} gate timed out after 180s")
        return True
    except FileNotFoundError as exc:
        print(f"❌ BLOCKING: {_W_INT_GATE.name} gate cannot invoke: {exc}")
        return True

    combined = (result.stdout + result.stderr).rstrip()
    if result.returncode == _WRAPPER_OK:
        _print_gate_ok(_W_INT_GATE.name, combined)
        return False
    if result.returncode == _W_INT_WRAPPER_BLOCKING:
        _print_gate_blocking(_W_INT_GATE.name, combined, venv_python, script, allowlist)
        return True
    print(f"❌ BLOCKING: {_W_INT_GATE.name} gate unexpected exit {result.returncode}:")
    print(combined or "(no output)")
    return True


def _check_service_interface_ast_gate(project_root: Path, venv_python: Path) -> bool:
    """Run the service-interface AST gate. True iff non-allowlisted findings.

    Tree-walking gate per design v1 §5 — walks every interfaces/public.py +
    every processes/<provider>/*.json on each invocation. Three checks
    (a/b/c). Exit codes follow the canonical _WRAPPER_OK / _WRAPPER_BLOCKING
    pattern.
    """
    artifacts = _resolve_gate_artifacts(project_root, _SI_AST_GATE)
    if artifacts is None:
        return True
    script, allowlist = artifacts

    print(f"\n📊 {_SI_AST_GATE.description.title()} Gate ({_SI_AST_GATE.name})...")
    argv = [str(venv_python), str(script), "--allowlist", str(allowlist)]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print(f"❌ BLOCKING: {_SI_AST_GATE.name} gate timed out after 120s")
        return True
    except FileNotFoundError as exc:
        print(f"❌ BLOCKING: {_SI_AST_GATE.name} gate cannot invoke: {exc}")
        return True

    combined = (result.stdout + result.stderr).rstrip()
    if result.returncode == _WRAPPER_OK:
        _print_gate_ok(_SI_AST_GATE.name, combined)
        return False
    if result.returncode == _WRAPPER_BLOCKING:
        _print_gate_blocking(_SI_AST_GATE.name, combined, venv_python, script, allowlist)
        return True
    print(f"❌ BLOCKING: {_SI_AST_GATE.name} gate unexpected exit {result.returncode}:")
    print(combined or "(no output)")
    return True


def _check_return_shape_gate(project_root: Path, venv_python: Path) -> bool:
    """Run the process return-shape gate. True iff non-allowlisted findings.

    Tree-walking gate (GTE-07): AST over every decorated verb; a non-dict
    return annotation is dead on arrival over process_call (the REL-11
    class). Exit codes follow the canonical _WRAPPER_OK / _WRAPPER_BLOCKING
    pattern.
    """
    artifacts = _resolve_gate_artifacts(project_root, _RETURN_SHAPE_GATE)
    if artifacts is None:
        return True
    script, allowlist = artifacts

    print(f"\n📊 {_RETURN_SHAPE_GATE.description.title()} Gate ({_RETURN_SHAPE_GATE.name})...")
    argv = [str(venv_python), str(script), "--allowlist", str(allowlist)]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print(f"❌ BLOCKING: {_RETURN_SHAPE_GATE.name} gate timed out after 120s")
        return True
    except FileNotFoundError as exc:
        print(f"❌ BLOCKING: {_RETURN_SHAPE_GATE.name} gate cannot invoke: {exc}")
        return True

    combined = (result.stdout + result.stderr).rstrip()
    if result.returncode == _WRAPPER_OK:
        _print_gate_ok(_RETURN_SHAPE_GATE.name, combined)
        return False
    if result.returncode == _WRAPPER_BLOCKING:
        _print_gate_blocking(
            _RETURN_SHAPE_GATE.name, combined, venv_python, script, allowlist,
        )
        return True
    print(
        f"❌ BLOCKING: {_RETURN_SHAPE_GATE.name} gate unexpected exit {result.returncode}:"
    )
    print(combined or "(no output)")
    return True


def _check_wint2_warn_only_gate(
    project_root: Path, venv_python: Path, gate: _GateSpec,
) -> None:
    """Run a W-INT Cycle 2 wrapper script in WARN mode.

    Shared driver for the two Cycle 2 gates (driver-import +
    vault-key-declaration). Per master plan §1.7 + brief §5.3,
    findings print to the summary but do NOT contribute to the
    blocking verdict during the state-service consolidation
    campaign. Mode flips warn→fail in a follow-up commit (driver-
    import at W-WINT2-FINAL; vault-key-declaration at sub-2
    W-VAULT-CALLER-ENFORCE) by dropping the `--warn-only` flag.
    """
    script = project_root / gate.script
    allowlist = project_root / gate.allowlist
    if not script.exists():
        print(
            f"⚠️ {gate.name} gate script missing: {script} "
            "(warn mode: continuing)",
        )
        return
    if not allowlist.exists():
        print(
            f"⚠️ {gate.name} gate allowlist missing: {allowlist} "
            "(warn mode: continuing)",
        )
        return

    print(f"\n📊 {gate.description.title()} Gate ({gate.name})...")
    argv = [
        str(venv_python), str(script),
        "--allowlist", str(allowlist),
        "--warn-only",
    ]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        print(
            f"⚠️ {gate.name} gate timed out after 180s "
            "(warn mode: continuing)",
        )
        return
    except FileNotFoundError as exc:
        print(
            f"⚠️ {gate.name} gate cannot invoke: {exc} "
            "(warn mode: continuing)",
        )
        return

    combined = (result.stdout + result.stderr).rstrip()
    if result.returncode == _WRAPPER_OK:
        _print_gate_ok(gate.name, combined)
        return
    print(
        f"⚠️ {gate.name} gate unexpected exit "
        f"{result.returncode} (warn mode: continuing):",
    )
    print(combined or "(no output)")


def _check_wint2_driver_import_gate(project_root: Path, venv_python: Path) -> None:
    """Run W-INT Cycle 2 driver-import gate in WARN mode.

    Per master plan §1.7, findings print to the summary but do NOT
    contribute to the blocking verdict during the state-service
    consolidation campaign. Mode flips warn→fail at W-WINT2-FINAL by
    dropping the `--warn-only` flag below.
    """
    script = project_root / _W_WINT2_GATE.script
    allowlist = project_root / _W_WINT2_GATE.allowlist
    if not script.exists():
        print(
            f"⚠️ {_W_WINT2_GATE.name} gate script missing: {script} "
            "(warn mode: continuing)"
        )
        return
    if not allowlist.exists():
        print(
            f"⚠️ {_W_WINT2_GATE.name} gate allowlist missing: {allowlist} "
            "(warn mode: continuing)"
        )
        return

    print(f"\n📊 {_W_WINT2_GATE.description.title()} Gate ({_W_WINT2_GATE.name})...")
    argv = [
        str(venv_python), str(script),
        "--allowlist", str(allowlist),
        "--warn-only",
    ]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        print(
            f"⚠️ {_W_WINT2_GATE.name} gate timed out after 180s "
            "(warn mode: continuing)"
        )
        return
    except FileNotFoundError as exc:
        print(
            f"⚠️ {_W_WINT2_GATE.name} gate cannot invoke: {exc} "
            "(warn mode: continuing)"
        )
        return

    combined = (result.stdout + result.stderr).rstrip()
    # `--warn-only` always returns 0; non-zero is a harness anomaly. Print
    # and continue either way — no blocking verdict in warn mode.
    if result.returncode == _WRAPPER_OK:
        _print_gate_ok(_W_WINT2_GATE.name, combined)
        return
    print(
        f"⚠️ {_W_WINT2_GATE.name} gate unexpected exit "
        f"{result.returncode} (warn mode: continuing):"
    )
    print(combined or "(no output)")


def _find_venv_python(project_root: Path) -> Path | None:
    venv_paths = [
        project_root / ".venv" / "bin" / "python3",
        project_root / "ananta" / "venv" / "bin" / "python3",
        project_root / "venv" / "bin" / "python3",
    ]
    for path in venv_paths:
        if path.exists():
            return path
    return None


def _report_pyright_errors(output: str, error_count: int) -> None:
    print(f"❌ BLOCKING: {error_count} type errors found")
    print("\nFirst 5 errors:")
    error_lines = [line for line in output.split("\n") if " - error:" in line]
    for line in error_lines[:5]:
        print(f"   {line}")
    if error_count > 5:
        print(f"   ... and {error_count - 5} more errors")
    print("\n💡 Run: pyright")


def _check_pyright(project_root: Path, venv_python: Path) -> bool:
    """Run pyright; return True if blocking type errors were found."""
    venv_activate = venv_python.parent / "activate"
    print("\n📊 Type Checking with pyright (strict)...")
    pyright_cmd = f"cd {project_root} && source {venv_activate} && python3 -m pyright 2>&1"
    success, output = run_command(pyright_cmd, "Running pyright", timeout=120)

    error_count = 0
    if not success and output.strip():
        error_count = len([line for line in output.split("\n") if " - error:" in line])

    if error_count > 0:
        _report_pyright_errors(output, error_count)
        return True
    if success:
        print("✅ ZERO type errors - strict enforcement passed!")
    else:
        print("⚠️ pyright check completed but status unclear")
    return False


def _check_ruff(project_root: Path, venv_python: Path) -> bool:
    """Run ruff; return True if lint errors were found."""
    print("\n📊 Linting with ruff...")
    ruff_cmd = f"cd {project_root} && {venv_python} -m ruff check ananta/src ananta/tests plugins --config pyproject.toml 2>&1"
    success, output = run_command(ruff_cmd, "Running ruff linter")

    if success:
        print("✅ No lint errors found")
        return False

    lint_lines = [line for line in output.split("\n") if line.strip()]
    if not lint_lines:
        return False
    print("❌ Lint errors found:")
    for line in lint_lines[:10]:
        print(f"   {line}")
    if len(lint_lines) > 10:
        print(f"   ... and {len(lint_lines) - 10} more")
    print("\n💡 Run: ruff check ananta/src ananta/tests plugins --config pyproject.toml --fix")
    return True


def _check_syntax(project_root: Path) -> list[str]:
    """Validate Python syntax across the tree; return list of error strings."""
    print("\n📊 Python Syntax Validation...")
    syntax_errors: list[str] = []
    for py_file in project_root.glob("**/*.py"):
        if ".venv" in str(py_file) or "venv" in str(py_file):
            continue
        result = subprocess.run(
            ["python3", "-m", "py_compile", str(py_file)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            syntax_errors.append(f"{py_file}: {result.stderr}")

    if not syntax_errors:
        print("✅ Python syntax valid")
    else:
        print("❌ CRITICAL: Python syntax errors found!")
        for err in syntax_errors[:5]:
            print(f"   {err}")
    return syntax_errors


def _check_debuggers(project_root: Path) -> None:
    print("\n📊 Quick Issue Scan...")
    debugger_cmd = f"cd {project_root} && grep -r 'import pdb\\|pdb.set_trace\\|breakpoint()' ananta/src plugins --include='*.py' 2>/dev/null"
    success, output = run_command(debugger_cmd, "Checking for debugger statements")

    if success or not output.strip():
        print("✅ No debugger statements found")
    else:
        print("⚠️ Warning: Debugger statements found:")
        print(output[:200])


def _summary_exit_code(results: _CheckResults) -> int:
    """Pure mapping from results to exit code; see module docstring."""
    if results.failed_blocking_gates:
        return 5
    if results.has_type_errors and results.has_lint_errors:
        return 3
    if results.has_type_errors:
        return 1
    if results.has_lint_errors:
        return 2
    return 0


def _print_summary(results: _CheckResults) -> int:
    print("\n" + "=" * 60)
    print("📊 CODE QUALITY CHECK SUMMARY")
    print("=" * 60)
    exit_code = _summary_exit_code(results)
    if exit_code == 0:
        print("✅ All code quality checks passed!")
        return 0
    if results.has_type_errors:
        print("❌ FAILED: Type errors found - fix before committing!")
    if results.has_lint_errors:
        print("❌ FAILED: Lint errors found - fix before committing!")
    if results.failed_blocking_gates:
        print(
            "❌ FAILED: Blocking gate violations "
            f"({', '.join(results.failed_blocking_gates)}) — fix before committing!"
        )
    return exit_code


def main() -> int:
    """Run code quality checks and report results."""
    project_root = Path(__file__).parent.parent

    print("=" * 60)
    print("🔍 CODE QUALITY CHECK")
    print("=" * 60)

    venv_python = _find_venv_python(project_root)
    if venv_python is None:
        print("❌ venv not found - run: python3 -m venv .venv")
        print("   Then: source .venv/bin/activate && pip install ruff pyright radon")
        return 4

    results = _CheckResults()
    results.has_type_errors = _check_pyright(project_root, venv_python)
    results.has_lint_errors = _check_ruff(project_root, venv_python)
    results.failed_blocking_gates.extend(
        _check_coherence_gates(project_root, venv_python)
    )
    if _check_whole_tree_integration_gate(project_root, venv_python):
        results.failed_blocking_gates.append(_W_INT_GATE.name)
    if _check_service_interface_ast_gate(project_root, venv_python):
        results.failed_blocking_gates.append(_SI_AST_GATE.name)
    if _check_return_shape_gate(project_root, venv_python):
        results.failed_blocking_gates.append(_RETURN_SHAPE_GATE.name)
    # W-INT Cycle 2 driver-import gate runs in WARN mode per master plan
    # §1.7 — emits findings but never blocks. Mode flip at W-WINT2-FINAL.
    _check_wint2_driver_import_gate(project_root, venv_python)
    # W-INT Cycle 2 vault-key-declaration gate (W-PLUGIN-LAUNCH-KEYS sub-1)
    # runs in WARN mode per brief §5.3 — emits findings but never blocks.
    # Mode flip at sub-2 W-VAULT-CALLER-ENFORCE in lockstep with the
    # runtime readiness gate.
    _check_wint2_warn_only_gate(
        project_root, venv_python, _W_WINT2_VAULT_KEY_GATE,
    )

    if _check_syntax(project_root):
        return 4

    _check_debuggers(project_root)

    if _run_fixture_replay_if_needed(project_root, venv_python):
        results.has_type_errors = True  # Block commit on fixture failures

    if _check_readable_freshness(project_root, venv_python):
        results.has_type_errors = True  # Block commit on stale readable files

    return _print_summary(results)


if __name__ == "__main__":
    sys.exit(main())
