#!/usr/bin/env python3
"""Pass-path proof for the fragment registration route (prb_ae6d81ea, iss_6ec80ecb).

Run:

    .venv/bin/python3 quality_gates/tests/gate_smoke_register_smoke.py

WHAT THIS PROVES. A candidate may register a gate smoke by writing its OWN file
under ``quality_gates/gate_smokes.d/`` instead of editing the one shared
``quality_gates/gate_smokes.txt``, so two candidates registering smokes share no
path and neither invalidates the other's frozen base entries. Each case drives a
REAL register file on disk through the REAL production reader:

* Case 1  the loader's union, deterministic ordering and de-duplication, and the
  row provenance a duplicate report needs -- real files, real ``load``.
* Case 2  a missing fragment directory is normal and silent, and the parsed set
  for a tree without fragments is exactly today's parsed set (acceptance b).
* Case 3  an unreadable fragment is an ADVISORY and the readable entries are
  still used; nothing refuses (rul_becb4bed).
* Case 4  ``run_smokes.py`` RUNS a smoke whose only registration is a line in a
  fragment -- a real subprocess, a real battery, a real passing smoke
  (acceptance a, "is run by the full suite").
* Case 5  ``focused_smoke_selector`` selects a fragment-registered smoke, over a
  real fixture checkout (acceptance a, "is selectable").
* Case 6  ``gate_smoke_registration_gate`` reports a fragment-registered smoke as
  registered AND still reports a genuinely unregistered one -- a real temporary
  git repository, the real census (acceptance a, "does not trip ... as
  unregistered", and the preserved refusal).
* Case 7  the quality_service allowlist -- the real
  ``QualityOperations._read_smoke_register`` over the REAL repository -- contains
  this very smoke, whose only registration is a fragment file (acceptance a,
  "accepted as a valid single --smoke target by the allowlist"). This case is
  the candidate demonstrating its own outcome.
* Case 8  acceptance (c): the two fragment files two candidates would write share
  no path with each other or with the main register.

NOT COVERED HERE, deliberately. The three-surface agreement accepting a
``# not shipped:`` marker that lives in a fragment is measured by the candidate
that added it, not by this one: a shipped plugin cannot import the
repository-root dev package ``quality_gates`` -- it is not on the plugin's path
and not part of what the plugin ships, so that import broke four seed_factory
smokes in the native gate. ``seed_factory_plugin.knowledge_fragments`` is the
plugin-local reader that answered it, and the cases live in
``quality_gates/tests/shipped_contract_fragment_union_smoke.py`` (which also
proves the two readers agree file for file) and in
``quality_gates/tests/smoke_contract_lint_gate_smoke.py``. Nothing here
weakens or restates that agreement.

Nothing here is monkeypatched, stubbed or mocked: every assertion reads a file
this smoke wrote to a real temporary directory and parses it with the shipped
code. Hermetic -- every fixture is a ``TemporaryDirectory``; nothing mutates git,
the shared checkout or a live solet.

DISCLOSED DEPENDENCY. Case 7 imports ``platform_dev_surface_plugin``. If that
plugin is not installed, this smoke exits 77 (the SKIP convention) with an
unmissable banner rather than skipping silently -- the register header's
ratified visible-skip rule.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

_REPO_ROOT = Path(__file__).resolve().parents[2]
# Bind every package this smoke measures to THIS tree. The repository venv is
# shared with the main checkout, so an editable install resolves a plugin
# package to the MAIN checkout's src/ -- in a lane worktree that would silently
# measure code this candidate has not changed. Fronting sys.path with this
# tree's own plugin sources makes the smoke measure the tree it lives in, which
# is also what makes it meaningful on an assembled release tree.
sys.path.insert(0, str(_REPO_ROOT / "plugins/platform_dev_surface_plugin/src"))
sys.path.insert(0, str(_REPO_ROOT))

from quality_gates.focused_smoke_selector import select_focused_smokes  # noqa: E402
from quality_gates.gate_smoke_register import (  # noqa: E402
    FRAGMENT_DIRNAME,
    FRAGMENT_SUFFIX,
    REGISTER_RELPATH,
    fragment_relpaths,
    is_fragment_relpath,
    load,
    load_from_sources,
    parse_register_text,
)
from quality_gates.gate_smoke_registration_gate import inspect_registration  # noqa: E402

_SKIP_EXIT_CODE = 77
_FAILURES: list[str] = []

#: This smoke's own registration lives ONLY here -- never in gate_smokes.txt.
_OWN_FRAGMENT_RELPATH = "quality_gates/gate_smokes.d/gate-smokes-fragments.txt"
_OWN_SMOKE_RELPATH = "quality_gates/tests/gate_smoke_register_smoke.py"

#: A real, offline, sub-second repository smoke, used as the thing a fixture
#: register points at so case 4 proves a fragment entry is really EXECUTED.
_FAST_REAL_SMOKE = "quality_gates/tests/allowlist_schema_smoke.py"


def _check(ok: bool, label: str) -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        _FAILURES.append(label)


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True, timeout=60)


def case_union_ordering_and_dedup() -> None:
    """Case 1 — the union, its deterministic order, de-duplication and provenance."""
    print("\nCase 1 — union, ordering, de-duplication, provenance")
    with TemporaryDirectory() as raw:
        root = Path(raw)
        register = _write(root, "gate_smokes.txt", "# header comment\na/main_one_smoke.py\n\na/main_two_smoke.py  # trailing comment\n")
        # Deliberately written in NON-alphabetical creation order, so a pass can
        # only come from sorting by filename rather than from directory order.
        _write(root, f"{FRAGMENT_DIRNAME}/zeta.txt", "c/zeta_smoke.py\n")
        _write(root, f"{FRAGMENT_DIRNAME}/alpha.txt", "b/alpha_smoke.py\n# a comment-only line\nb/shared_smoke.py\n")
        _write(root, f"{FRAGMENT_DIRNAME}/beta.txt", "b/shared_smoke.py\nb/beta_smoke.py\n")
        _write(root, f"{FRAGMENT_DIRNAME}/notes.md", "b/ignored_smoke.py\n")

        effective = load(register)
        expected = ("a/main_one_smoke.py", "a/main_two_smoke.py", "b/alpha_smoke.py", "b/shared_smoke.py", "b/beta_smoke.py", "c/zeta_smoke.py")
        _check(effective.entries == expected, f"union is main-then-fragments-by-filename, de-duplicated (got {effective.entries!r})")
        _check(effective.advisories == (), f"a well-formed fragment set raises no advisory (got {effective.advisories!r})")
        _check("b/ignored_smoke.py" not in effective.entries, "a non-.txt file beside the fragments is not read as a register")
        _check(load(register).entries == effective.entries, "the loader is deterministic across repeated loads")

        repeats = [row for row in effective.rows if row.path == "b/shared_smoke.py"]
        _check(len(repeats) == 2, f"both registrations of a repeated path are retained as rows (got {len(repeats)})")
        _check({Path(row.source).name for row in repeats} == {"alpha.txt", "beta.txt"}, "each repeated row names the fragment it came from")
        _check(all(row.line > 0 for row in repeats), "each row carries its own 1-based line number")

        # Identical inputs through the frozen-tree reader give the identical union.
        sources = {"quality_gates/gate_smokes.txt": b"a/main_one_smoke.py\n", "quality_gates/gate_smokes.d/alpha.txt": b"b/alpha_smoke.py\n", "quality_gates/gate_smokes.d/nested/deep.txt": b"b/smuggled_smoke.py\n"}
        from_sources = load_from_sources(sources, REGISTER_RELPATH)
        _check(from_sources.entries == ("a/main_one_smoke.py", "b/alpha_smoke.py"), f"the frozen-tree loader unions the same way (got {from_sources.entries!r})")
        _check("b/smuggled_smoke.py" not in from_sources.entries, "a NESTED path under the fragment directory cannot smuggle in an entry")
        _check(fragment_relpaths(REGISTER_RELPATH, sources) == ("quality_gates/gate_smokes.d/alpha.txt",), "only direct .txt children count as fragments")
        _check(is_fragment_relpath(REGISTER_RELPATH, _OWN_FRAGMENT_RELPATH), "this candidate's own fragment is recognised as a fragment")


def case_missing_directory_and_unchanged_semantics() -> None:
    """Case 2 — no fragment directory is normal, silent, and parses as today."""
    print("\nCase 2 — missing fragment directory, and identical parse for a tree without fragments")
    body = "# header\nx/one_smoke.py\n\nx/two_smoke.py\n# not shipped: x/two_smoke.py reason\n"
    with TemporaryDirectory() as raw:
        register = _write(Path(raw), "gate_smokes.txt", body)
        effective = load(register)
        _check(effective.advisories == (), f"a missing fragment directory is silent (got {effective.advisories!r})")
        _check(effective.entries == parse_register_text(body), "a tree with no fragments parses to exactly the historical entry set")

    # The same equivalence, on the REAL register this repository ships.
    real_register = _REPO_ROOT / REGISTER_RELPATH
    main_only = parse_register_text(real_register.read_text(encoding="utf-8"))
    real_effective = load(real_register)
    _check(real_effective.entries[: len(main_only)] == main_only, "every existing gate_smokes.txt entry keeps its exact position and order in the real union")
    _check(set(main_only) <= set(real_effective.entries), "the real effective register is a SUPERSET of the existing register")
    _check(len(set(real_effective.entries)) == len(real_effective.entries), "the real union contains no duplicate entry")


def case_unreadable_fragment_is_advisory() -> None:
    """Case 3 — a damaged fragment is an advisory, never a refusal."""
    print("\nCase 3 — an unreadable fragment is an advisory and the rest is still used")
    with TemporaryDirectory() as raw:
        root = Path(raw)
        register = _write(root, "gate_smokes.txt", "x/main_smoke.py\n")
        _write(root, f"{FRAGMENT_DIRNAME}/good.txt", "x/good_smoke.py\n")
        broken = root / FRAGMENT_DIRNAME / "broken.txt"
        broken.write_bytes(b"x/bad_smoke.py\n\xff\xfe invalid utf-8 \xff\n")

        effective = load(register)
        _check(effective.entries == ("x/main_smoke.py", "x/good_smoke.py"), f"the readable entries are still registered (got {effective.entries!r})")
        _check(len(effective.advisories) == 1, f"exactly one advisory is raised (got {effective.advisories!r})")
        _check("broken.txt" in effective.advisories[0], "the advisory names the file it could not read")
        _check(effective.advisories[0].startswith("ADVISORY"), "the advisory is prefixed ADVISORY, not raised as an error")


def case_run_smokes_runs_a_fragment_only_smoke() -> None:
    """Case 4 — the real runner EXECUTES a smoke registered only in a fragment."""
    print("\nCase 4 — run_smokes.py runs a fragment-only smoke")
    with TemporaryDirectory() as raw:
        directory = Path(raw)
        register = directory / "register.txt"
        # The main register is EMPTY of entries. The only registration of the
        # smoke below is the fragment file beside it, so a run that executes it
        # can only have come through the fragment route.
        register.write_text("# fixture register with no entries of its own\n", encoding="utf-8")
        fragments = directory / FRAGMENT_DIRNAME
        fragments.mkdir()
        (fragments / "candidate.txt").write_text(f"{_FAST_REAL_SMOKE}\n", encoding="utf-8")

        listed = subprocess.run(
            [str(_REPO_ROOT / ".venv/bin/python3"), "quality_gates/run_smokes.py", "--register", str(register), "--list"],
            cwd=_REPO_ROOT,
            env={**os.environ, "SOLET_NAME": "gate-smoke-register-fixture"},
            capture_output=True,
            text=True,
            timeout=120,
        )
        _check(listed.returncode == 0, f"--list over a fragment-only register exits 0 (got {listed.returncode}: {listed.stderr})")
        _check(listed.stdout.strip() == _FAST_REAL_SMOKE, f"--list reports the fragment-registered smoke (got {listed.stdout.strip()!r})")

        receipts = directory / "receipts"
        run = subprocess.run(
            [str(_REPO_ROOT / ".venv/bin/python3"), "quality_gates/run_smokes.py", "--register", str(register), "--jobs", "1", "--campaign", "gate-smoke-register-fixture"],
            cwd=_REPO_ROOT,
            env={**os.environ, "SOLET_NAME": "gate-smoke-register-fixture", "RUN_SMOKES_BATTERY_RECEIPT_DIR": str(receipts), "RUN_SMOKES_DEFAULT_REGISTER_PATH": str(register)},
            capture_output=True,
            text=True,
            timeout=180,
        )
        _check(run.returncode == 0, f"the battery passes with its only smoke registered in a fragment (got {run.returncode}: {run.stdout[-2000:]}{run.stderr[-2000:]})")
        _check(_FAST_REAL_SMOKE in run.stdout, "the runner reports the fragment-registered smoke in its battery output")
        receipt_files = list(receipts.glob("*.json"))
        _check(len(receipt_files) == 1, f"the fixture battery published exactly one receipt (got {receipt_files})")
        if receipt_files:
            receipt = json.loads(receipt_files[0].read_text(encoding="utf-8"))
            recorded = {row["smoke"]: row.get("verdict") for row in receipt.get("smoke_results") or ()}
            _check(recorded.get(_FAST_REAL_SMOKE) == "passed", f"the receipt records a PASSED result for the fragment-registered smoke (got {recorded!r})")


def case_focused_selector_selects_a_fragment_registered_smoke() -> None:
    """Case 5 — the real selector selects a smoke registered only in a fragment."""
    print("\nCase 5 — focused_smoke_selector selects a fragment-registered smoke")
    changed = "ananta/src/ananta/target_module.py"
    fragment_only = "ananta/tests/fragment_only_smoke.py"
    with TemporaryDirectory() as raw:
        root = Path(raw)
        # The main register has no entries of its own: a selection can only come
        # through the fragment. The `literal` rule is the one under proof here --
        # it needs no git top level, so the selector's own git advisory (this
        # fixture is not a repository) cannot mask the result.
        _write(root, REGISTER_RELPATH, "# no entries of its own\n")
        _write(root, f"quality_gates/{FRAGMENT_DIRNAME}/candidate.txt", f"{fragment_only}\n")
        _write(root, fragment_only, f'TARGET = "{changed}"\n\n\ndef main() -> int:\n    return 0\n')
        _write(root, changed, "VALUE = 1\n")

        selection = select_focused_smokes(root, [changed])
        _check(fragment_only in selection.reasons, f"the fragment-registered smoke is selected for a changed path it names (got {sorted(selection.reasons)!r})")
        _check(selection.unmapped == (), f"the changed path is mapped, not unmapped (got {selection.unmapped!r})")
        _check(not any("unreadable" in advisory for advisory in selection.advisories), f"no unreadable-register advisory is raised (got {selection.advisories!r})")

        # Negative control: with the fragment removed the same smoke is NOT
        # selectable, so case 5 is measuring the fragment and nothing else.
        (root / f"quality_gates/{FRAGMENT_DIRNAME}/candidate.txt").unlink()
        without = select_focused_smokes(root, [changed])
        _check(fragment_only not in without.reasons, f"without the fragment the smoke is not registered and not selected (got {sorted(without.reasons)!r})")


def case_registration_gate_accepts_a_fragment() -> None:
    """Case 6 — the census accepts a fragment registration and still catches a miss."""
    print("\nCase 6 — gate_smoke_registration_gate over a real temporary git repository")
    with TemporaryDirectory() as raw:
        root = Path(raw)
        _git(root, "init", "-q")
        _write(root, "quality_gates/tests/fragment_registered_smoke.py", "def main() -> int:\n    return 0\n")
        _write(root, "quality_gates/gate_smokes.txt", "# this candidate registered nothing here\n")
        _write(root, f"quality_gates/{FRAGMENT_DIRNAME}/candidate.txt", "quality_gates/tests/fragment_registered_smoke.py\n")
        _write(root, "quality_gates/gate_smoke_exclusions.txt", "# Exact deliberately-excluded smoke paths.\n")
        _git(root, "add", ".")

        report = inspect_registration(root)
        _check(report.unregistered == (), f"a fragment-registered smoke is NOT reported unregistered (got {report.unregistered!r})")
        _check("quality_gates/tests/fragment_registered_smoke.py" in report.registered, "the fragment-registered smoke is in the registered set")
        _check(report.overlaps == () and report.stale_exclusions == (), "no overlap or stale exclusion is invented")

        # The preserved refusal: a genuinely unregistered smoke is still caught.
        _write(root, "quality_gates/tests/forgotten_smoke.py", "def main() -> int:\n    return 0\n")
        after = inspect_registration(root)
        _check(after.unregistered == ("quality_gates/tests/forgotten_smoke.py",), f"a genuinely unregistered smoke is still reported (got {after.unregistered!r})")


def case_quality_service_allowlist_sees_this_smoke() -> None:
    """Case 7 — the real run_test allowlist contains THIS smoke, fragment-registered."""
    print("\nCase 7 — the quality_service allowlist over the REAL repository")
    from platform_dev_surface_plugin.quality.operations import QualityOperations

    operations = QualityOperations(_REPO_ROOT, os.environ.get("SOLET_NAME", "gate-smoke-register-fixture"))
    allowlisted = operations._read_smoke_register()  # noqa: SLF001 — the allowlist IS the thing under proof
    main_only = parse_register_text((_REPO_ROOT / REGISTER_RELPATH).read_text(encoding="utf-8"))

    _check(_OWN_SMOKE_RELPATH in allowlisted, f"this smoke is a valid single run_test target ({_OWN_SMOKE_RELPATH})")
    _check(_OWN_SMOKE_RELPATH not in main_only, "and its registration is NOT in gate_smokes.txt — the fragment route is what put it there")
    _check(set(main_only) <= set(allowlisted), "every existing register entry is still allowlisted")


def case_two_candidates_share_no_path() -> None:
    """Case 8 — acceptance (c): two registering candidates edit no common path."""
    print("\nCase 8 — two candidates registering a smoke share no path")
    # Derived from the loader's own constants rather than written as literals,
    # so this measures the real fragment layout and not a restatement of itself.
    directory = f"{PurePosixPath(REGISTER_RELPATH).parent / FRAGMENT_DIRNAME}"
    first, second = f"{directory}/candidate-one{FRAGMENT_SUFFIX}", f"{directory}/candidate-two{FRAGMENT_SUFFIX}"
    paths = {first, second, REGISTER_RELPATH}
    _check(len(paths) == 3, f"two candidates write two distinct fragment files, neither of them the shared register (got {sorted(paths)!r})")
    _check(is_fragment_relpath(REGISTER_RELPATH, first) and is_fragment_relpath(REGISTER_RELPATH, second), "both are recognised fragments of the same register")

    with TemporaryDirectory() as raw:
        root = Path(raw)
        register = _write(root, "gate_smokes.txt", "shared/existing_smoke.py\n")
        _write(root, f"{FRAGMENT_DIRNAME}/candidate-one.txt", "one/first_smoke.py\n")
        _write(root, f"{FRAGMENT_DIRNAME}/candidate-two.txt", "two/second_smoke.py\n")
        entries = load(register).entries
        _check(entries == ("shared/existing_smoke.py", "one/first_smoke.py", "two/second_smoke.py"), f"both candidates' smokes are registered without either editing the other's file (got {entries!r})")


def main() -> int:
    print("Gate-smoke register fragment route smoke\n")
    if find_spec("platform_dev_surface_plugin") is None:
        print("=" * 72)
        print("SKIPPED (exit 77): platform_dev_surface_plugin is not importable, so the")
        print("run_test allowlist case cannot be measured in this environment.")
        print("This is a DISCLOSED dependency gap, not a pass.")
        print("=" * 72)
        return _SKIP_EXIT_CODE

    case_union_ordering_and_dedup()
    case_missing_directory_and_unchanged_semantics()
    case_unreadable_fragment_is_advisory()
    case_run_smokes_runs_a_fragment_only_smoke()
    case_focused_selector_selects_a_fragment_registered_smoke()
    case_registration_gate_accepts_a_fragment()
    case_quality_service_allowlist_sees_this_smoke()
    case_two_candidates_share_no_path()

    if _FAILURES:
        print(f"\nFAILED ({len(_FAILURES)}): {_FAILURES}")
        return 1
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
