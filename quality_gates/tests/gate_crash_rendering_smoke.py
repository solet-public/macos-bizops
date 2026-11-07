#!/usr/bin/env python3
"""Smoke tests for gate-crash rendering and tracked-only scope (no pytest, per project rule).

Two properties, both regressions of 2026-08-16 defects:

1. A gate that CRASHED must never be reported as a gate that found
   violations. ``code_quality_check.py`` mapped every unrecognised exit code
   to "BLOCKING", so ``radon_cc``'s ``RecursionError`` surfaced as
   "FAILED: Blocking gate violations (radon_cc)" — which reads as complexity
   debt in the reader's own code when nothing had been measured at all.

2. The wrappers' directory expansion must cover IN-REPO files — tracked, or
   untracked and not ignored — and nothing else. A bare run over
   ``plugins/cosyvoice2_tts_plugin`` reached 18,321 vendored files under
   ``src/.venv_cosyvoice``, reported sympy's C-grade functions as this repo's
   findings, and then crashed on one of them. The first fix over-corrected to
   TRACKED-only and produced a false green inside the hour: a brand-new
   never-added smoke with a CC-12 function went unmeasured while the aggregate
   printed "radon_cc gate clean". Both directions are regressions, so both are
   asserted below.

Each property is checked with a NEGATIVE CONTROL — a real violation must
still read as a violation, and an explicitly named untracked file must still
be scanned — because a check that can only pass is not a check.

Run: ``.venv/bin/python3 quality_gates/tests/gate_crash_rendering_smoke.py``
"""

from __future__ import annotations

import io
import subprocess
import sys
import tempfile
from collections.abc import Generator
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

_GATE_DIR = Path(__file__).resolve().parent.parent
if str(_GATE_DIR) not in sys.path:
    sys.path.insert(0, str(_GATE_DIR))

import code_quality_check as cqc  # noqa: E402
import gate_scope  # noqa: E402
import god_class_check  # noqa: E402
import radon_cc_check  # noqa: E402
import radon_mi_check  # noqa: E402

# `radon` is gate-toolchain tooling declared in `quality_gates/requirements.txt`
# (R9, 2026-08-17). Every environment this smoke runs in — checkout venv, and a
# born clone since `birth_throwaway` installs that file — is required to have it;
# a ModuleNotFoundError here is a real toolchain-install defect, not a condition
# to degrade around. Import unguarded so the radon wrappers are asserted, not
# skipped.
_WRAPPERS: tuple[ModuleType, ...] = (god_class_check, radon_cc_check, radon_mi_check)

_TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "quality_gates/radon_cc_check.py", line 135, in _scan_file\n'
    "    visited = cc_visit(source)\n"
    "RecursionError: maximum recursion depth exceeded\n"
)
_FINDINGS = "CC C (12): some/file.py:10 some_function\n"

_FAILURES: list[str] = []


def check(name: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}")
    if not condition:
        _FAILURES.append(name)


def _fake(
    returncode: int, output: str, argv: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=argv or ["gate"], returncode=returncode, stdout=output, stderr="",
    )


@contextmanager
def _scratch_repo() -> Generator[Path]:
    """A throwaway git repo shaped like the checkout's quality surface.

    The scope tests must not interrogate the surrounding checkout: a born
    clone of the shipped bundle has no ``.git``, so a test that needs one
    can never pass there while the property it asserts still holds for any
    adopter's real clone. ``git ls-files --cached --others
    --exclude-standard`` needs neither identity nor a commit, so ``init``
    plus ``add`` is a complete fixture.
    """
    with tempfile.TemporaryDirectory(prefix="gate_scope_fixture_") as tmp:
        root = Path(tmp).resolve()

        def git(*args: str) -> None:
            subprocess.run(
                ["git", *args], cwd=str(root), check=True,
                capture_output=True, text=True, timeout=60,
            )

        git("init", "-q")
        (root / "quality_gates").mkdir()
        (root / "quality_gates" / "tracked_module.py").write_text(
            "def f() -> None:\n    pass\n", encoding="utf-8",
        )
        vendored = root / "quality_gates" / ".venv_fixture" / "lib"
        vendored.mkdir(parents=True)
        (vendored / "vendored_module.py").write_text(
            "def g() -> None:\n    pass\n", encoding="utf-8",
        )
        plugin_src = root / "plugins" / "fixture_plugin" / "src"
        ignored = plugin_src / "vendor_tree"
        ignored.mkdir(parents=True)
        (ignored / "ignored_module.py").write_text(
            "def h() -> None:\n    pass\n", encoding="utf-8",
        )
        (plugin_src / "shipped.py").write_text(
            "def s() -> None:\n    pass\n", encoding="utf-8",
        )
        (root / ".gitignore").write_text(
            ".venv_fixture/\nplugins/fixture_plugin/src/vendor_tree/\n",
            encoding="utf-8",
        )
        git(
            "add", ".gitignore", "quality_gates/tracked_module.py",
            "plugins/fixture_plugin/src/shipped.py",
        )
        yield root


def _render(
    returncode: int, output: str, argv: list[str] | None = None,
    blocking_code: int = cqc._WRAPPER_BLOCKING,
) -> tuple[cqc._CheckResults, str]:
    """Drive one gate result through classification + summary; capture stdout."""
    results = cqc._CheckResults()
    buf = io.StringIO()
    with redirect_stdout(buf):
        blocking = cqc._interpret_tree_gate_result(
            cqc._W_INT_GATE, _fake(returncode, output, argv),
            Path("py"), Path("script"), Path("allowlist"), results,
            blocking_code=blocking_code,
        )
        if blocking:
            results.failed_blocking_gates.append(cqc._W_INT_GATE.name)
        exit_code = cqc._print_summary(results)
    return results, f"{buf.getvalue()}\nEXIT={exit_code}"


# ---------------------------------------------------------------------------


def test_blocking_whole_tree_hint_replays_gate_argv() -> None:
    print("whole-tree blocking hint reproduces the gate argv")
    argv = [
        "py", "quality_gates/shipped_doc_gate.py", "--repo-root", ".",
        "--allowlist", "quality_gates/cited_path_gate_allowlist.txt",
    ]
    _, out = _render(
        cqc._W_INT_WRAPPER_BLOCKING, _FINDINGS, argv,
        blocking_code=cqc._W_INT_WRAPPER_BLOCKING,
    )
    hint = next(line for line in out.splitlines() if "Re-run:" in line)
    check("hint contains the argv the whole-tree gate ran",
          hint == "💡 Re-run: " + " ".join(argv))
    check("whole-tree hint omits <paths>", "<paths>" not in hint)


def test_path_scoped_hint_keeps_paths_placeholder() -> None:
    print("path-scoped blocking hint keeps the <paths> placeholder")
    result = _fake(
        cqc._WRAPPER_BLOCKING, _FINDINGS,
        ["py", "script", "one.py", "two.py", "--allowlist", "allowlist"],
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        cqc._interpret_gate_result(
            "radon_cc_check", result, Path("py"), Path("script"),
            Path("allowlist"), cqc._CheckResults(),
        )
    hint = next(line for line in buf.getvalue().splitlines() if "Re-run:" in line)
    check("path-scoped hint retains <paths>", "<paths>" in hint)
    check("path-scoped hint omits the full file list",
          "one.py" not in hint and "two.py" not in hint)


def test_crash_is_not_rendered_as_a_violation() -> None:
    print("crash renders as GATE CRASH, never as a violation count")
    results, out = _render(1, _TRACEBACK)
    check("recorded on crashed_gates", len(results.crashed_gates) == 1)
    check("failed_blocking_gates stays empty", results.failed_blocking_gates == [])
    check("summary says GATE CRASH", "GATE CRASH" in out)
    check("summary omits 'Blocking gate violations'",
          "Blocking gate violations" not in out)
    check("excerpt carries the exception", "RecursionError" in out)
    check("exit 6 (crash), not 5 (violations)", "EXIT=6" in out)


def test_dedicated_crash_exit_code() -> None:
    print("wrappers' exit 70 is recognised without needing a traceback")
    results, out = _render(70, "GATE-CRASH: f.py: RecursionError in cc_visit\n")
    check("exit 70 classified as crash", len(results.crashed_gates) == 1)
    check("exit 6", "EXIT=6" in out)


def test_negative_control_real_violation_still_reads_as_one() -> None:
    print("NEGATIVE CONTROL: a real violation is still a violation")
    results, out = _render(cqc._WRAPPER_BLOCKING, _FINDINGS)
    check("recorded on failed_blocking_gates", results.failed_blocking_gates != [])
    check("crashed_gates stays empty", results.crashed_gates == [])
    check("summary says 'Blocking gate violations'",
          "Blocking gate violations" in out)
    check("summary omits GATE CRASH", "GATE CRASH" not in out)
    check("exit 5", "EXIT=5" in out)


def test_negative_control_clean_gate_stays_clean() -> None:
    print("NEGATIVE CONTROL: a clean gate stays clean")
    results, out = _render(0, "OK: 2132 file(s) scanned, 0 violations.\n")
    check("neither crash nor violation",
          not results.crashed_gates and not results.failed_blocking_gates)
    check("exit 0", "EXIT=0" in out)


def test_exit_code_collision_traceback_outranks_exit_code() -> None:
    print("exit-code collision: W-INT blocks with 1, Python also crashes with 1")
    crash = cqc._classify_gate_exit(1, _TRACEBACK, blocking_code=1)
    check("exit 1 + traceback is a CRASH", crash is cqc._GateOutcome.CRASH)
    blocking = cqc._classify_gate_exit(1, _FINDINGS, blocking_code=1)
    check("exit 1 + findings is still BLOCKING",
          blocking is cqc._GateOutcome.BLOCKING)


def test_crash_supersedes_violations() -> None:
    print("a crash outranks a violation in the exit code, and both are printed")
    results = cqc._CheckResults()
    results.failed_blocking_gates.append("radon_cc")
    results.crashed_gates.append(("radon_mi", "RecursionError"))
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cqc._print_summary(results)
    out = buf.getvalue()
    check("both lines print", "Blocking gate violations" in out and "GATE CRASH" in out)
    check("exit 6, not 5", code == 6)


def test_aggregate_scope_is_in_repo_only() -> None:
    print("aggregate scope walk admits in-repo files, and no ignored ones")
    with _scratch_repo() as root:
        paths = cqc._per_file_gate_paths(root)
        check("scope is non-empty", len(paths) > 0)
        check("no bundled-venv paths", not any(".venv" in p.name for p in paths))
        in_repo = cqc.repo_files(root)
        check("every scoped path is in-repo",
              all(p.resolve() in in_repo for p in paths))
        check("no vendored venv file is in scope",
              not any(".venv_fixture" in str(p) for p in paths))
        check("gitignored vendored tree is not in scope",
              not any("vendor_tree" in str(p) for p in paths))


def test_tracked_filter_is_load_bearing_without_the_name_prune() -> None:
    print("MUTATION: with the .venv name prune disabled, the ignore filter still holds")
    original = cqc.BUNDLED_VENV_PREFIX
    with _scratch_repo() as root:
        try:
            # Both name prunes off: the aggregate's own rglob prune and the
            # one `gate_scope.repo_files` applies (iss_c795a79b). Disabling
            # only one would let the other stand in for the ignore filter.
            cqc.BUNDLED_VENV_PREFIX = "\0never-matches"
            gate_scope.BUNDLED_VENV_PREFIX = "\0never-matches"
            mutated = cqc._per_file_gate_paths(root)
        finally:
            cqc.BUNDLED_VENV_PREFIX = original
            gate_scope.BUNDLED_VENV_PREFIX = original
        leaked = [
            p for p in mutated
            if f"{original}_" in str(p) or f"/{original}/" in str(p)
        ]
        check("no vendored venv files leak through", leaked == [])


def test_repo_files_prunes_bundled_venv_by_name_like_its_sibling() -> None:
    """iss_c795a79b: `repo_files` applies the same bundled-venv name prune as
    `repo_python_files`, so a venv file the ignore filter admits (force-added,
    or under an un-ignored venv directory) is out of scope for both.

    Before the fix `repo_python_files` dropped such a file and `repo_files`
    kept it: the aggregate's own rglob prune hid the disagreement, but any
    consumer intersecting against `repo_files` alone saw vendored code.
    """
    print("repo_files applies the bundled-venv name prune its sibling applies")
    with _scratch_repo() as root:
        forced = root / "quality_gates" / ".venv_fixture" / "lib" / "force_tracked.py"
        forced.write_text("def v() -> None:\n    pass\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "-f", str(forced.relative_to(root))], cwd=str(root),
            check=True, capture_output=True, text=True, timeout=60,
        )
        raw = subprocess.run(
            ["git", "ls-files", "--cached", "--", "quality_gates"], cwd=str(root),
            check=True, capture_output=True, text=True, timeout=60,
        ).stdout
        check("control: the ignore filter alone ADMITS the force-tracked venv file",
              ".venv_fixture/lib/force_tracked.py" in raw)
        sibling = gate_scope.repo_python_files(root / "quality_gates")
        check("repo_python_files prunes the force-tracked venv file",
              forced.resolve() not in {p.resolve() for p in sibling})
        whole = gate_scope.repo_files(root)
        check("repo_files prunes the same file", forced.resolve() not in whole)
        check("repo_files still holds the tracked module",
              (root / "quality_gates" / "tracked_module.py").resolve() in whole)


def test_wrappers_expand_directories_to_in_repo_files() -> None:
    """Both regressions at once: ignored code out, brand-new code IN.

    The second assertion is the one that matters most. A never-added file is
    the file most likely to carry a new defect, and a tracked-only predicate
    made exactly that file invisible — the gate reported clean on code it had
    not read.
    """
    print(f"wrappers expand directories to in-repo files (x{len(_WRAPPERS)})")
    with _scratch_repo() as root:
        vendored_dir = root / "plugins" / "fixture_plugin"
        work = root / "workdir"
        work.mkdir()
        fresh = work / "brand_new_module.py"
        fresh.write_text("def f() -> None:\n    pass\n", encoding="utf-8")
        for mod in _WRAPPERS:
            name = mod.__name__
            expanded = {p.resolve() for p in mod._expand_targets([str(work)])}
            check(f"{name}: brand-new untracked file IS gated",
                  fresh.resolve() in expanded)
            vend = mod._expand_targets([str(vendored_dir)])
            check(f"{name}: gitignored vendored tree is NOT gated",
                  not any("vendor_tree" in str(p) for p in vend))
            named = {p.resolve() for p in mod._expand_targets([str(fresh)])}
            check(f"{name}: explicitly named file is scanned", fresh.resolve() in named)


def test_wrappers_expose_the_crash_contract() -> None:
    print(f"wrappers share the crash exit code and exception type (x{len(_WRAPPERS)})")
    for mod in _WRAPPERS:
        name = mod.__name__
        check(f"{name}: exit code 70", mod.GATE_CRASH_EXIT == 70)
        check(f"{name}: GateCrashError is an Exception",
              issubclass(mod.GateCrashError, Exception))


def test_pyright_no_verdict_is_not_reported_as_passed() -> None:
    print("pyright timeout and launch failure are reported as unmeasured")
    for output in (
        "Command timed out after 120 seconds",
        "OSError: Bad CPU type in executable",
    ):
        results = cqc._CheckResults()
        buf = io.StringIO()
        with patch.object(cqc, "run_command", return_value=(False, output)):
            with redirect_stdout(buf):
                exit_code = cqc._run_pyright_checked(
                    Path("."), Path(".venv/bin/python"), results,
                )
                if exit_code is None:
                    exit_code = cqc._print_summary(results)
        rendered = buf.getvalue()
        check(f"{output}: does not report all checks passed",
              "All code quality checks passed!" not in rendered)
        check(f"{output}: names pyright as unmeasured",
              "pyright" in rendered.lower() and "unmeasured" in rendered.lower())
        check(f"{output}: exit code stays 0", exit_code == 0)


def test_pyright_success_error_and_unavailable_paths() -> None:
    print("pyright success, parsed errors, and module-unavailable paths")
    with patch.object(cqc, "run_command", return_value=(True, "")):
        success_results = cqc._CheckResults()
        success_output = io.StringIO()
        with redirect_stdout(success_output):
            code = cqc._run_pyright_checked(
                Path("."), Path(".venv/bin/python"), success_results,
            )
            if code is None:
                code = cqc._print_summary(success_results)
    check("success is not recorded as no verdict",
          not success_results.pyright_unmeasured)
    check("success still prints the all-passed summary",
          code == 0 and "All code quality checks passed!" in success_output.getvalue())
    errors = "file.py:1:1 - error: expected error\n"
    with patch.object(cqc, "run_command", return_value=(False, errors)):
        error_results = cqc._CheckResults()
        with redirect_stdout(io.StringIO()):
            code = cqc._run_pyright_checked(
                Path("."), Path(".venv/bin/python"), error_results,
            )
            if code is None:
                code = cqc._print_summary(error_results)
    check("parsed errors remain blocking", error_results.has_type_errors)
    check("parsed errors retain exit code 1", code == 1)
    with patch.object(
        cqc, "run_command", return_value=(False, "No module named pyright"),
    ):
        try:
            cqc._check_pyright(Path("."), Path(".venv/bin/python"))
        except cqc.ToolUnavailableError:
            unavailable = True
        else:
            unavailable = False
    check("missing module still raises ToolUnavailableError", unavailable)


# ---------------------------------------------------------------------------


def test_explicit_paths_are_measured_or_loudly_skipped() -> None:
    print("explicit paths are reported without narrowing whole-tree scope")
    with _scratch_repo() as root, tempfile.TemporaryDirectory(
        prefix="explicit_path_outside_",
    ) as outside_dir:
        tracked = root / "quality_gates" / "tracked_module.py"
        untracked = root / "quality_gates" / "untracked_module.py"
        untracked.write_text("def u() -> None:\n    pass\n", encoding="utf-8")
        missing = root / "quality_gates" / "missing_module.py"
        non_python = root / "quality_gates" / "notes.txt"
        non_python.write_text("notes\n", encoding="utf-8")
        outside = Path(outside_dir) / "outside_module.py"
        outside.write_text("def x() -> None:\n    pass\n", encoding="utf-8")
        arguments = [
            str(outside), str(missing.relative_to(root)),
            str(non_python.relative_to(root)), str(tracked.relative_to(root)),
            str(untracked.relative_to(root)),
        ]
        expected_scope = tuple(cqc._per_file_gate_paths(root))
        captured_scopes: list[tuple[Path, ...]] = []

        def capture_scope(
            _venv_python: Path, _gate: cqc._GateSpec, _project_root: Path,
            scope_paths: list[Path], _results: cqc._CheckResults,
        ) -> cqc._GateOutcome:
            captured_scopes.append(tuple(scope_paths))
            return cqc._GateOutcome.OK

        output = io.StringIO()
        with patch.object(sys, "argv", ["code_quality_check.py", *arguments]):
            with patch.object(cqc, "_run_coherence_gate", side_effect=capture_scope):
                with redirect_stdout(output):
                    cqc._check_coherence_gates(
                        root, Path("python"), cqc._CheckResults(),
                    )
        captured_output = output.getvalue()
        check(
            "explicit outside-root path is loudly skipped with a reason",
            f"ADVISORY explicit path skipped: {arguments[0]} "
            "(outside quality-surface roots)" in captured_output,
        )
        check(
            "missing and non-Python paths have specific advisories",
            f"ADVISORY explicit path skipped: {arguments[1]} (path does not exist)"
            in captured_output
            and f"ADVISORY explicit path skipped: {arguments[2]} (not a .py file)"
            in captured_output,
        )
        check(
            "explicit paths leave the complete whole-tree scope unchanged",
            len(captured_scopes) == len(cqc._COHERENCE_GATES)
            and all(scope == expected_scope for scope in captured_scopes)
            and tracked.resolve() in expected_scope
            and untracked.resolve() in expected_scope,
        )
        check(
            "tracked in-scope path is reported as included",
            f"INFO explicit path included in whole-tree per-file checks: {arguments[3]}"
            in captured_output,
        )
        check(
            "untracked in-scope path is reported as included",
            f"INFO explicit path included in whole-tree per-file checks: {arguments[4]}"
            in captured_output,
        )


# ---------------------------------------------------------------------------


def main() -> int:
    print("Gate-crash rendering + tracked-scope smoke\n")
    for test in (
        test_explicit_paths_are_measured_or_loudly_skipped,
        test_blocking_whole_tree_hint_replays_gate_argv,
        test_path_scoped_hint_keeps_paths_placeholder,
        test_crash_is_not_rendered_as_a_violation,
        test_dedicated_crash_exit_code,
        test_negative_control_real_violation_still_reads_as_one,
        test_negative_control_clean_gate_stays_clean,
        test_exit_code_collision_traceback_outranks_exit_code,
        test_crash_supersedes_violations,
        test_aggregate_scope_is_in_repo_only,
        test_tracked_filter_is_load_bearing_without_the_name_prune,
        test_repo_files_prunes_bundled_venv_by_name_like_its_sibling,
        test_wrappers_expand_directories_to_in_repo_files,
        test_wrappers_expose_the_crash_contract,
        test_pyright_no_verdict_is_not_reported_as_passed,
        test_pyright_success_error_and_unavailable_paths,
    ):
        test()
    print()
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} check(s): {_FAILURES}")
        return 1
    print("ALL SMOKE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
