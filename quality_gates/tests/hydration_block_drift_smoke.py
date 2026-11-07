#!/usr/bin/env python3
"""Pass path and deliberate drift fixture for the hydration-block drift check.

Run (from the checkout root, with this checkout's sources first on the path):

    PYTHONPATH=solet_setup_contracts/src:. .venv/bin/python3 quality_gates/tests/hydration_block_drift_smoke.py

WHAT THIS PROVES. ``solet_setup_contracts.agent_instruction_block.hydration_drift`` reports a
checkout's hydration block as stale exactly when it differs from the block its template
renders to, and reports nothing when they agree or when no block is present. The gate
(``shipped_doc_gate._hydration_drift_advisories``) surfaces that as an advisory and never
as a blocking finding.

CASES.
* pass path: a block equal to the rendered template is not drift;
* deliberate drift fixture: one changed line inside the block is reported, with remediation;
* a file with no block is not drift (the check reports stale blocks, it does not require one);
* a template with no block is a finding (the authoritative source is defective);
* remediation round trip: re-merging the rendered block clears the finding;
* gate advisory: a stale root file yields one advisory line, a clean one yields none, and the
  gate's exit path is unchanged (the advisory is a list, not a verdict).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from solet_setup_contracts.agent_instruction_block import (  # noqa: E402
    HYDRATION_BEGIN,
    HYDRATION_END,
    hydration_drift,
    merge_agent_block,
    render_agent_instructions,
)

_FAILURES: list[str] = []

TEMPLATE = f"# Root file for {{{{SOLET_NAME}}}}\n\n{HYDRATION_BEGIN}\nRun `{{{{SOLET_NAME}}}} call` from {{{{CLONE_DIR}}}} before any other tool call.\n{HYDRATION_END}\n"
NAME = "fixture"
CLONE = "/fixture/clone"


def _check(label: str, condition: bool) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        _FAILURES.append(label)


def _rendered() -> str:
    return render_agent_instructions(TEMPLATE, name=NAME, clone_dir=CLONE)


def case_pass_path_is_not_drift() -> None:
    local = merge_agent_block("# Local notes\n", _rendered())
    _check("pass path: block equal to the rendered template is not drift", hydration_drift(local, _rendered()) is None)


def case_drift_fixture_is_reported() -> None:
    drifted = _rendered().replace("before any other tool call", "after the first tool call")
    local = merge_agent_block("# Local notes\n", drifted)
    finding = hydration_drift(local, _rendered())
    _check("drift fixture: a changed line inside the block is reported", finding is not None)
    _check(
        "drift fixture: the finding names the remediation",
        finding is not None and "re-merge" in finding,
    )


def case_no_block_is_not_drift() -> None:
    _check(
        "no block: a file without a hydration block is not drift",
        hydration_drift("# Hand-written root file\n", _rendered()) is None,
    )


def case_template_without_block_is_a_finding() -> None:
    finding = hydration_drift(merge_agent_block("", _rendered()), "# template lost its block\n")
    _check("defective template: a template with no block is a finding", finding is not None and "defective" in finding)


def case_remediation_clears_drift() -> None:
    stale = merge_agent_block("# Local notes\n", _rendered().replace("tool call", "old call"))
    _check("round trip: the stale block is reported before remediation", hydration_drift(stale, _rendered()) is not None)
    fixed = merge_agent_block(stale, _rendered())
    _check("round trip: re-merging the rendered block clears the finding", hydration_drift(fixed, _rendered()) is None)


def case_gate_advisory_is_non_blocking() -> None:
    import quality_gates.shipped_doc_gate as gate

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "root_manifest.yaml").write_text("solet_name: fixture\n", encoding="utf-8")
        template_dir = root / "plugins/github_midwife_plugin/knowledge_base/hydration_templates"
        template_dir.mkdir(parents=True)
        (template_dir / "CLAUDE.md.template").write_text(TEMPLATE, encoding="utf-8")
        (template_dir / "AGENTS.md.template").write_text(TEMPLATE, encoding="utf-8")
        # The gate renders CLONE_DIR as the checkout root, so the fixtures render the same way.
        clone = str(root)
        rendered = render_agent_instructions(TEMPLATE, name=NAME, clone_dir=clone)
        # CLAUDE.md is a stale copy; AGENTS.md is clean.
        (root / "CLAUDE.md").write_text(
            merge_agent_block("", rendered.replace("tool call", "old call")),
            encoding="utf-8",
        )
        (root / "AGENTS.md").write_text(merge_agent_block("", rendered), encoding="utf-8")
        cwd = os.getcwd()
        advisories = gate._hydration_drift_advisories(root)
        os.chdir(cwd)
    _check("gate advisory: one line for the stale root file", len(advisories) == 1)
    _check("gate advisory: the line names CLAUDE.md", bool(advisories) and advisories[0].startswith("CLAUDE.md:"))


def case_gate_advisory_clean_checkout_is_silent() -> None:
    import quality_gates.shipped_doc_gate as gate

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "root_manifest.yaml").write_text("solet_name: fixture\n", encoding="utf-8")
        template_dir = root / "plugins/github_midwife_plugin/knowledge_base/hydration_templates"
        template_dir.mkdir(parents=True)
        (template_dir / "CLAUDE.md.template").write_text(TEMPLATE, encoding="utf-8")
        (template_dir / "AGENTS.md.template").write_text(TEMPLATE, encoding="utf-8")
        clone = str(root)
        rendered = render_agent_instructions(TEMPLATE, name=NAME, clone_dir=clone)
        (root / "CLAUDE.md").write_text("# no block here\n", encoding="utf-8")
        (root / "AGENTS.md").write_text(merge_agent_block("", rendered), encoding="utf-8")
        advisories = gate._hydration_drift_advisories(root)
    _check("gate advisory: a clean checkout yields no advisory", advisories == [])


def case_gate_advisory_non_utf8_root_file_is_an_advisory() -> None:
    """A non-UTF-8 root file is an advisory line, not an exception out of the gate (review follow-up D1)."""
    import quality_gates.shipped_doc_gate as gate

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "root_manifest.yaml").write_text("solet_name: fixture\n", encoding="utf-8")
        template_dir = root / "plugins/github_midwife_plugin/knowledge_base/hydration_templates"
        template_dir.mkdir(parents=True)
        (template_dir / "CLAUDE.md.template").write_text(TEMPLATE, encoding="utf-8")
        (template_dir / "AGENTS.md.template").write_text(TEMPLATE, encoding="utf-8")
        (root / "CLAUDE.md").write_bytes(b"# latin-1 byte \xe9 in a root file\n")
        (root / "AGENTS.md").write_text("# no block here\n", encoding="utf-8")
        try:
            advisories = gate._hydration_drift_advisories(root)
        except UnicodeDecodeError as exc:
            _check(f"non-UTF-8 root file: raised instead of advising ({exc})", False)
            return
    _check(
        "non-UTF-8 root file: one advisory line naming the decode error",
        len(advisories) == 1 and "UnicodeDecodeError" in advisories[0],
    )


def case_non_utf8_advisory_leaves_exit_code_unchanged() -> None:
    """Through the real run(): the advisory prints, and the exit code is whatever the verdict returns (review D4).

    The lint stages are stubbed so run() reaches the verdict, and the verdict is a sentinel. A returned
    sentinel, with the advisory printed before it, proves the advisory does not feed the exit code.
    """
    import contextlib
    import io
    import types

    import quality_gates.shipped_doc_gate as gate

    sentinel = 77
    lint = types.ModuleType("seed_factory_plugin.shipped_doc_lint")
    lint.lint_shipped_docs = lambda *a, **k: object()  # type: ignore[attr-defined]
    lint.load_tolerated_baseline = lambda path: {}  # type: ignore[attr-defined]
    package = types.ModuleType("seed_factory_plugin")
    package.shipped_doc_lint = lint  # type: ignore[attr-defined]
    saved_modules: dict[str, object | None] = {k: sys.modules.get(k) for k in ("seed_factory_plugin", "seed_factory_plugin.shipped_doc_lint")}
    saved_roots = gate._root_instruction_findings
    saved_verdict = gate._verdict
    buf = io.StringIO()
    try:
        sys.modules["seed_factory_plugin"] = package
        sys.modules["seed_factory_plugin.shipped_doc_lint"] = lint
        gate._root_instruction_findings = lambda root: []  # type: ignore[assignment]
        gate._verdict = lambda *a: sentinel  # type: ignore[assignment]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / gate._MANIFEST_RELPATH
            manifest.parent.mkdir(parents=True)
            manifest.write_text("schema_version: 1\n", encoding="utf-8")
            allowlist = root / gate._DEFAULT_ALLOWLIST_RELPATH
            allowlist.parent.mkdir(parents=True, exist_ok=True)
            allowlist.write_text("", encoding="utf-8")
            (root / "root_manifest.yaml").write_text("solet_name: fixture\n", encoding="utf-8")
            template_dir = root / "plugins/github_midwife_plugin/knowledge_base/hydration_templates"
            template_dir.mkdir(parents=True)
            (template_dir / "CLAUDE.md.template").write_text(TEMPLATE, encoding="utf-8")
            (template_dir / "AGENTS.md.template").write_text(TEMPLATE, encoding="utf-8")
            (root / "CLAUDE.md").write_bytes(b"# latin-1 byte \xe9 in a root file\n")
            (root / "AGENTS.md").write_text("# no block here\n", encoding="utf-8")
            with contextlib.redirect_stdout(buf):
                code = gate.run(["--repo-root", str(root)])
    finally:
        for key, value in saved_modules.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value  # type: ignore[assignment]
        gate._root_instruction_findings = saved_roots
        gate._verdict = saved_verdict
    _check(
        "non-UTF-8 root file through run(): the exit code is the verdict's, unchanged by the advisory",
        code == sentinel,
    )
    _check(
        "non-UTF-8 root file through run(): the advisory line is printed",
        "hydration drift not checked" in buf.getvalue(),
    )


def case_installed_contracts_missing_a_name_is_an_advisory() -> None:
    """An installed solet_setup_contracts without hydration_drift is an advisory, not a crash (battery regression).

    The battery runs the gate against the editable install, which can be another checkout's copy. That copy
    lacked hydration_drift and the gate died with ImportError and no verdict. The import guard must cover every
    name the helpers use, so the same mismatch is one advisory line and the gate still returns its verdict.
    """
    import types

    import quality_gates.shipped_doc_gate as gate

    stale = types.ModuleType("solet_setup_contracts.agent_instruction_block")
    stale.AGENT_INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md")  # type: ignore[attr-defined]
    saved = sys.modules.get("solet_setup_contracts.agent_instruction_block")
    sys.modules["solet_setup_contracts.agent_instruction_block"] = stale
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "root_manifest.yaml").write_text("solet_name: fixture\n", encoding="utf-8")
            advisories = gate._hydration_drift_advisories(root)
    finally:
        if saved is None:
            sys.modules.pop("solet_setup_contracts.agent_instruction_block", None)
        else:
            sys.modules["solet_setup_contracts.agent_instruction_block"] = saved
    _check(
        "mismatched installed contracts: one advisory naming the unavailable import, no exception",
        len(advisories) == 1 and "solet_setup_contracts unavailable" in advisories[0],
    )


def main() -> int:
    print(f"hydration_block_drift_smoke (pid {os.getpid()})")
    case_pass_path_is_not_drift()
    case_drift_fixture_is_reported()
    case_no_block_is_not_drift()
    case_template_without_block_is_a_finding()
    case_remediation_clears_drift()
    case_gate_advisory_is_non_blocking()
    case_gate_advisory_clean_checkout_is_silent()
    case_gate_advisory_non_utf8_root_file_is_an_advisory()
    case_non_utf8_advisory_leaves_exit_code_unchanged()
    case_installed_contracts_missing_a_name_is_an_advisory()
    if _FAILURES:
        print(f"\nFAIL: {len(_FAILURES)} check(s) failed:")
        for label in _FAILURES:
            print(f"  - {label}")
        return 1
    print("\nhydration_block_drift_smoke: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
