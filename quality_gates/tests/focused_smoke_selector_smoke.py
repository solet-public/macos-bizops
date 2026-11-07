#!/usr/bin/env python3
"""Regression for the advisory focused smoke selector (wgr_3690c324).

Routines 1-4 run the selector on the real checkout, which is the producer of
the member-case shapes. They share ONE selection over all five changed paths,
and each asserts a reason that names its own path. A seed clone carries
PROVENANCE.json and prunes the origin-side smokes those cases name, so there
they SKIP with a banner. Routines 5-9 use fixture trees from
``_focused_smoke_selector_fixture``. Each routine ends in a labelled assert, so
the first failure names its routine.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from functools import cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from quality_gates import focused_smoke_selector  # noqa: E402
from quality_gates.tests import _focused_smoke_selector_fixture as fixture  # noqa: E402

_SELECTOR_SCRIPT = focused_smoke_selector.__file__
_CASE_1_PATH = "solet_cli/src/solet_manager/update_candidate.py"
_CASE_2_PATH = "plugins/agent_messaging_plugin/tests/_register_unit_double.py"
_CASE_3_PATH = "plugins/default_thinking_plugin/src/default_thinking_plugin/plugin.py"
_PROFILE_PATH = "initialization/profiles/local.yaml"
_RELEASE_NOTES_PATH = "RELEASE_NOTES.md"
_CASE_2_SMOKES = (
    "plugins/agent_messaging_plugin/tests/fleet_identity_qualification_smoke.py",
    "plugins/agent_messaging_plugin/tests/headless_adapter_init_event_capture_smoke.py",
    "plugins/agent_messaging_plugin/tests/headless_adapter_stdout_drain_smoke.py",
)
_RELEASE_NOTES_SMOKES = (
    "plugins/seed_factory_plugin/tests/release_run_smoke.py",
    "solet_cli/tests/real_style_tree_5_smoke.py",
)


def _passed(label: str) -> None:
    print(f"  [PASS] {label}")


def _skipped_on_seed_clone(label: str) -> bool:
    if (ROOT / "PROVENANCE.json").is_file():
        print(f"  [SKIP] {label}: seed clone (PROVENANCE.json) prunes the origin-side smokes this case names")
        return True
    return False


@cache
def _real_selection() -> focused_smoke_selector.Selection:
    paths = (_CASE_1_PATH, _CASE_2_PATH, _CASE_3_PATH, _PROFILE_PATH, _RELEASE_NOTES_PATH)
    return focused_smoke_selector.select_focused_smokes(ROOT, paths)


def _has_reason(selection: focused_smoke_selector.Selection, smoke: str, rule: str, path: str) -> bool:
    return any(reason.rule == rule and path in reason.detail for reason in selection.reasons.get(smoke, ()))


def _check_member_case_whole_source_gate() -> None:
    label = "MEMBER_CASE_1_REPAIR_TEXT_SELECTED"
    if _skipped_on_seed_clone(label):
        return
    smoke = "solet_cli/tests/repair_text_verb_gate_smoke.py"
    assert _has_reason(_real_selection(), smoke, "scanned_root", _CASE_1_PATH), label
    _passed(label)


def _check_member_case_transitive_fixture() -> None:
    label = "MEMBER_CASE_2_TRANSITIVE_FIXTURE_SELECTED"
    if _skipped_on_seed_clone(label):
        return
    selection = _real_selection()
    assert all(_has_reason(selection, smoke, "imports_changed_module", _CASE_2_PATH) for smoke in _CASE_2_SMOKES), label
    _passed(label)


def _check_member_case_edge_plugin() -> None:
    label = "MEMBER_CASE_3_EDGE_PLUGIN_SELECTED"
    if _skipped_on_seed_clone(label):
        return
    smoke = "ananta/tests/core/process_registry/edge_definition_customizations_smoke.py"
    assert _has_reason(_real_selection(), smoke, "entry_point_plugin", _CASE_3_PATH), label
    _passed(label)


def _check_profile_and_release_notes_classes() -> None:
    label = "INTENT_CLASSES_PROFILE_AND_RELEASE_NOTES_SELECTED"
    if _skipped_on_seed_clone(label):
        return
    selection = _real_selection()
    profile = _has_reason(selection, "plugins/macos_midwife_plugin/tests/profile_lockstep_smoke.py", "literal", _PROFILE_PATH)
    notes = all(_has_reason(selection, smoke, "literal", _RELEASE_NOTES_PATH) for smoke in _RELEASE_NOTES_SMOKES)
    assert profile and notes, label
    _passed(label)


def _run_cli(root: Path, evidence: Path, paths: list[str], *extra: str) -> subprocess.CompletedProcess[str]:
    paths_file = evidence / "changed_paths.txt"
    paths_file.write_text("".join(f"{path}\n" for path in paths), encoding="utf-8")
    command = [
        sys.executable,
        _SELECTOR_SCRIPT,
        "--repo-root",
        str(root),
        "--paths-file",
        str(paths_file),
        "--register-out",
        str(evidence / "focused_register.txt"),
        "--report-out",
        str(evidence / "focused_report.json"),
        *extra,
    ]
    return subprocess.run(command, env=fixture.git_environment(), capture_output=True, text=True, check=False)


def _report_if_written(evidence: Path) -> dict[str, Any]:
    report_path = evidence / "focused_report.json"
    return json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}


def _selected_smokes(report: dict[str, Any]) -> list[str]:
    return [entry["smoke"] for entry in report.get("selected", [])]


def _check_unknown_path_is_advisory_never_refusal() -> None:
    label = "UNKNOWN_PATH_NEVER_REFUSES"
    # Every observation goes through the real CLI, so a refusal (an exception or a
    # nonzero exit) fails on this label instead of escaping the routine.
    with fixture.build_repository() as tree, tempfile.TemporaryDirectory() as scratch:
        root, alone, mixed = Path(tree), Path(scratch) / "alone", Path(scratch) / "mixed"
        alone.mkdir()
        mixed.mkdir()
        _run_cli(root, alone, [fixture.DOCS_PATH])
        completed = _run_cli(root, mixed, [fixture.UNKNOWN_PATH, fixture.DOCS_PATH])
        baseline, report = _report_if_written(alone), _report_if_written(mixed)
        register_written = (mixed / "focused_register.txt").is_file()
    advisory = f"no registered smoke maps {fixture.UNKNOWN_PATH}"
    assert completed.returncode == 0 and advisory in completed.stderr and report.get("unmapped") == [fixture.UNKNOWN_PATH] and _selected_smokes(report) == _selected_smokes(baseline) != [] and register_written, label
    _passed(label)


def _advisory_count(selection: focused_smoke_selector.Selection, name: str) -> int:
    return sum(1 for advisory in selection.advisories if name in advisory)


def _check_stale_inputs_are_advisories() -> None:
    label = "STALE_INPUT_ADVISORY_NOT_REFUSAL"
    # A python changed path makes the import rule list and parse the tree.
    paths = [fixture.DATA_PATH, fixture.DOTTED_PATH]
    with fixture.build_repository() as tree:
        tracked = focused_smoke_selector.select_focused_smokes(Path(tree), paths)
    with fixture.build_plain_tree() as tree, tempfile.TemporaryDirectory() as scratch:
        plain = focused_smoke_selector.select_focused_smokes(Path(tree), paths)
        completed = _run_cli(Path(tree), Path(scratch), paths)
    assert (
        _advisory_count(tracked, fixture.MISSING_SMOKE) == 1
        and _advisory_count(tracked, f"{fixture.BROKEN_SMOKE} does not parse") == 1
        and {fixture.LITERAL_PATH_SMOKE, fixture.BROKEN_SMOKE} <= set(tracked.reasons)
        and _advisory_count(plain, "import rule skipped") == 1
        and {fixture.LITERAL_PATH_SMOKE, fixture.BROKEN_SMOKE} <= set(plain.reasons)
        and completed.returncode == 0
    ), label
    _passed(label)


def _selected_by(selection: focused_smoke_selector.Selection, rule: str) -> set[str]:
    return {smoke for smoke, reasons in selection.reasons.items() if any(reason.rule == rule for reason in reasons)}


def _check_skill_rules_are_kept() -> None:
    label = "SKILL_RULES_PORTED_UNCHANGED"
    with fixture.build_repository() as tree:
        root = Path(tree)
        literal = focused_smoke_selector.select_focused_smokes(root, [fixture.DATA_PATH, fixture.DOCS_PATH, fixture.DOTTED_PATH, fixture.SRC_DOTTED_PATH])
        top_level = focused_smoke_selector.select_focused_smokes(root, [fixture.TOP_LEVEL_PATH])
        roots = focused_smoke_selector.select_focused_smokes(root, [fixture.NAMED_ROOT_PATH, fixture.PLUGINS_DATA_PATH])
        empty = focused_smoke_selector.select_focused_smokes(root, [])
    expected_literal = {
        fixture.LITERAL_PATH_SMOKE,
        fixture.LITERAL_BASENAME_SMOKE,
        fixture.LITERAL_DOTTED_SMOKE,
        fixture.LITERAL_SRC_DOTTED_SMOKE,
        fixture.BROKEN_SMOKE,
    }
    assert (
        _selected_by(literal, "literal") == expected_literal
        and fixture.SAME_ROOT_SCANNER_SMOKE in _selected_by(literal, "scanned_root")
        and _selected_by(top_level, "scanned_root") == set(fixture.SCANNER_SMOKES)
        and {fixture.NAMED_ROOT_SCANNER_SMOKE, fixture.CROSS_ROOT_SCANNER_SMOKE} <= _selected_by(roots, "scanned_root")
        and _selected_by(top_level, "real_root_evaluator") == {fixture.REAL_ROOT_SMOKE}
        and fixture.UNREGISTERED_SMOKE not in literal.reasons
        and not empty.reasons
        and any("focused register is empty" in advisory for advisory in empty.advisories)
    ), label
    _passed(label)


def _check_deleted_relative_and_sibling_imports() -> None:
    label = "DELETED_MODULE_SELECTS_IMPORTERS"
    with fixture.build_repository() as tree:
        root = Path(tree)
        deleted = focused_smoke_selector.select_focused_smokes(root, [fixture.DELETED_MODULE_PATH])
        sibling = focused_smoke_selector.select_focused_smokes(root, [fixture.SIBLING_FIXTURE_PATH])
    assert fixture.RELATIVE_IMPORT_SMOKE in _selected_by(deleted, "imports_changed_module") and fixture.SIBLING_IMPORT_SMOKE in _selected_by(sibling, "imports_changed_module"), label
    _passed(label)


def _check_cli_outputs_are_deterministic() -> None:
    label = "CLI_RERUN_IS_BYTE_IDENTICAL"
    paths = [fixture.DOCS_PATH, fixture.DATA_PATH, fixture.UNKNOWN_PATH]
    with fixture.build_repository() as tree, tempfile.TemporaryDirectory() as scratch:
        root, evidence = Path(tree), Path(scratch)
        named = evidence / "named.txt"
        named.write_text(f"# review-named\n{fixture.REAL_ROOT_SMOKE}\n{fixture.UNREGISTERED_SMOKE}\n", encoding="utf-8")
        first = _run_cli(root, evidence, paths, "--named-file", str(named))
        outputs = [(evidence / name).read_bytes() for name in ("focused_register.txt", "focused_report.json")]
        second = _run_cli(root, evidence, paths, "--named-file", str(named))
        again = [(evidence / name).read_bytes() for name in ("focused_register.txt", "focused_report.json")]
    usage = subprocess.run([sys.executable, _SELECTOR_SCRIPT], capture_output=True, text=True, check=False)
    register = outputs[0].decode("utf-8").splitlines()
    report = json.loads(outputs[1])
    fields = {"schema", "changed_paths", "selected", "rule_counts", "advisories", "unmapped"}
    assert (
        first.returncode == 0
        and second.returncode == 0
        and outputs == again
        and first.stdout == second.stdout
        and first.stdout.startswith("FOCUSED-SELECTOR selected=")
        and register == sorted(register)
        and set(report) == fields
        and report["schema"] == focused_smoke_selector.SCHEMA
        and [entry["smoke"] for entry in report["selected"]] == register
        and report["rule_counts"]["named"] == 1
        and "is not registered; not added" in first.stderr
        and usage.returncode == 2
    ), label
    _passed(label)


def _check_unreadable_input_files_are_advisories() -> None:
    label = "UNREADABLE_INPUT_FILE_ADVISORY"
    with fixture.build_repository() as tree, tempfile.TemporaryDirectory() as scratch:
        root, evidence = Path(tree), Path(scratch)
        missing = evidence / "absent_paths.txt"
        directory = evidence / "named_as_directory"
        directory.mkdir()
        absent = subprocess.run(
            [
                sys.executable,
                _SELECTOR_SCRIPT,
                "--repo-root",
                str(root),
                "--paths-file",
                str(missing),
                "--register-out",
                str(evidence / "absent_register.txt"),
                "--report-out",
                str(evidence / "absent_report.json"),
            ],
            env=fixture.git_environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        named = _run_cli(root, evidence, [fixture.DOCS_PATH], "--named-file", str(directory))
        outputs_written = all((evidence / name).is_file() for name in ("absent_register.txt", "absent_report.json", "focused_register.txt", "focused_report.json"))
    assert absent.returncode == 0 and f"ADVISORY focused_smoke_selector: paths file {missing} is unreadable" in absent.stderr and named.returncode == 0 and f"ADVISORY focused_smoke_selector: named file {directory} is unreadable" in named.stderr and outputs_written, label
    _passed(label)


def _check_fragment_registered_smoke_is_selectable() -> None:
    """Routine 11 — a smoke registered only in a fragment is selectable.

    The fixture register and the fragment are real files on disk and the real
    selector parses them; the fragment is the smoke's ONLY registration, so a
    selection can only have come through the union loader (prb_ae6d81ea). The
    negative control removes the fragment and re-selects, which is what makes
    this routine measure the fragment rather than some other rule.
    """
    label = "routine 11: a fragment-registered smoke is selectable, and is not registered without its fragment"
    with fixture.build_repository() as tmp:
        root = Path(tmp)
        selection = focused_smoke_selector.select_focused_smokes(root, [fixture.FRAGMENT_PATH])
        selected = fixture.FRAGMENT_REGISTERED_SMOKE in selection.reasons
        literal_reason = _has_reason(selection, fixture.FRAGMENT_REGISTERED_SMOKE, "literal", fixture.FRAGMENT_PATH)
        no_register_advisory = not any("register" in advisory and "unreadable" in advisory for advisory in selection.advisories)

        (root / fixture.FRAGMENT_REGISTER_RELPATH).unlink()
        without = focused_smoke_selector.select_focused_smokes(root, [fixture.FRAGMENT_PATH])
        gone = fixture.FRAGMENT_REGISTERED_SMOKE not in without.reasons
    assert selected and literal_reason and no_register_advisory and gone, label
    _passed(label)


def _check_an_unreadable_fragment_is_an_advisory() -> None:
    """Routine 12 — a damaged fragment is one advisory, never a refusal.

    The rest of the register must still select normally: an author's broken
    file cannot take the selector down for everyone (rul_becb4bed).
    """
    label = "routine 12: a damaged fragment is an advisory and the rest of the register still selects"
    with fixture.build_repository() as tmp:
        root = Path(tmp)
        damaged = root / "quality_gates/gate_smokes.d/damaged.txt"
        damaged.write_bytes(b"\xff\xfe not utf-8\n")
        selection = focused_smoke_selector.select_focused_smokes(root, [fixture.DATA_PATH, fixture.FRAGMENT_PATH])
        advisory = any("damaged.txt" in notice and notice.startswith("ADVISORY") for notice in selection.advisories)
        main_register_still_works = fixture.LITERAL_PATH_SMOKE in selection.reasons
        readable_fragment_still_works = fixture.FRAGMENT_REGISTERED_SMOKE in selection.reasons
    assert advisory and main_register_still_works and readable_fragment_still_works, label
    _passed(label)


def main() -> int:
    print("Focused smoke selector smoke\n")
    _check_member_case_whole_source_gate()
    _check_member_case_transitive_fixture()
    _check_member_case_edge_plugin()
    _check_profile_and_release_notes_classes()
    _check_unknown_path_is_advisory_never_refusal()
    _check_stale_inputs_are_advisories()
    _check_skill_rules_are_kept()
    _check_deleted_relative_and_sibling_imports()
    _check_cli_outputs_are_deterministic()
    _check_unreadable_input_files_are_advisories()
    _check_fragment_registered_smoke_is_selectable()
    _check_an_unreadable_fragment_is_an_advisory()
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
