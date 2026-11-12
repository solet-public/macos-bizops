"""Retire the setup choice through real create planning and Manager update.

Host lifecycle seams are fake; the Manager plans, backs up, applies, and
reads back the seed adapter's actual filesystem changes in disposable installs.
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import os
import runpy
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [
    str(Path(__file__).resolve().parent),
    str(_ROOT / "solet_cli/tests"),
    str(_ROOT / "solet_cli/src"),
    str(_ROOT / "plugins/github_midwife_plugin/src"),
    str(_ROOT),
]

from executable_hydration_smoke import FakeRuntime, _raw_request  # noqa: E402
from github_midwife_plugin.setup_adapter import dispatch_request  # noqa: E402
from github_midwife_plugin.setup_adapter_contract import AdapterRequest  # noqa: E402
from solet_manager import contract_reconciliation as reconciliation_module  # noqa: E402
from solet_manager.config import CreateConfig  # noqa: E402
from solet_manager.contracts import ContractBundle, contract_filenames, load_contract_reconciliations  # noqa: E402
from solet_manager.errors import ContractError, ProbeDriftError, StateConflictError  # noqa: E402
from solet_manager.existing_install_doctor_probe import artifact_check  # noqa: E402
from solet_manager.existing_solet_diagnostics import DiagnosticStatus  # noqa: E402
from solet_manager.managed_artifact_backup import backup_root  # noqa: E402
from solet_manager.models import JsonValue  # noqa: E402
from solet_manager.plan_builder import build_setup_plan  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.transaction import load_transaction, target_install_state_projection, write_transaction  # noqa: E402
from solet_manager.update_execution import apply_update, preview_update_instance  # noqa: E402

from solet_cli.tests._step5_support import (  # noqa: E402
    KB,
    TEMPLATES,
    Release,
    advance_to_source_advanced,
    build_fixture,
    bundle_document,
    default_artifacts,
    digest_of,
    runtime_fingerprint,
    template_bytes,
)


def retirement_artifacts() -> list[dict[str, Any]]:
    """The release's closed paths: three launchers and its sourced environment."""
    return [
        {
            "artifact_id": f"retired_controller_{name}",
            "kind": "rendered_whole",
            "logical_destination": destination,
            "preservation_class": "manager_generated_whole",
            "marker": None,
            "stamp": "# rendered-from: {TEMPLATE_REF}@{TEMPLATE_DIGEST}",
            "template_ref": f"{TEMPLATES}/{template}",
            "template_digest": digest_of(template_bytes(template)),
            "previous_template_digests": [],
        }
        for name, destination, template in (
            ("claude_launcher", "{TARGET}/client/bin/claude-{NAME}", "claude_launcher.template"),
            ("codex_launcher", "{TARGET}/client/bin/codex-{NAME}", "codex_launcher.template"),
            ("fleet_launcher", "{TARGET}/client/{NAME}-fleet.zsh", "fleet_functions.zsh.template"),
            ("shell_environment", "{TARGET}/client/{NAME}.zsh", "solet.zsh.template"),
        )
    ]


def check_fresh_plan(root: Path) -> None:
    bundle = ContractBundle.load(source_revision="a" * 40, directory=_ROOT / KB)
    assert "git_mutation_control" not in bundle.decisions
    assert "git_controller_name" not in bundle.inputs
    assert "git_controller_name" not in json.dumps(bundle.flow)
    decisions: dict[str, JsonValue] = {
        "coding_agents": ["claude_code"],
        "embeddings_implementation": "coreai",
        "inference_implementation": "apple_foundation_models",
        "session_sources": [],
        "execution_topology": "solo",
    }
    plan = build_setup_plan(
        bundle=bundle,
        config=CreateConfig("fresh-plan", root / "target", True, decisions),
        seed=SeedLock(
            "https://github.com/solet-public/macos-bizops.git",
            "r1",
            "a" * 40,
            "b" * 40,
            "c" * 64,
            "macos-bizops",
        ),
        journal_path=root / "journal.json",
        decision_selections=decisions,
    )
    assert plan is not None, "fresh setup builds without a Git-control question"
    shipped = json.loads((_ROOT / KB / "existing_install_flow.json").read_text())
    actual = {row["artifact_id"]: row for row in shipped["managed_artifacts"]}
    for artifact in retirement_artifacts():
        assert actual.get(artifact["artifact_id"]) == artifact, artifact["artifact_id"]


def _document(baseline: Release) -> dict[str, Any]:
    return bundle_document(baseline, artifacts=[*default_artifacts(), *retirement_artifacts()])


def _legacy_controller_template(name: str) -> bytes:
    """Reconstruct immutable base templates without needing a Git checkout.

    The exact hashes bind these ordinary supported inputs to base 0f5db311.
    In particular, none already contains the unset whose absence caused C2-R1.
    """
    if name == "fleet_functions.zsh.template":
        content = (_ROOT / "plugins/github_midwife_plugin/tests/fixtures/fleet_functions_before_controller_retirement.zsh").read_bytes()
    else:
        content = template_bytes(name)
    if name == "claude_launcher.template":
        content = content.replace(b"(the quality gate path)", b"(the Git-Controller gate path)", 1)
        comment = (
            b"# Git controller: records the ONE role the operator chose as the git-mutating\n"
            b"# role in this deployment directory. It is a recorded choice only: the\n"
            b"# coordination-hooks git gate that read it was removed in 0.9.0 (operator\n"
            b"# ruling rul_e71e43be), so exporting it arms nothing and deleting it exempts\n"
            b"# nothing; git control is the repository's own responsibility (rul_d8893e33).\n"
            b"# A solo deployment may delete this line (see TEMPLATE_VARS.md). Keep it\n"
            b"# identical in this launcher and in {{SOLET_NAME}}-fleet.zsh so the recorded\n"
            b"# choice does not diverge between launchers.\n"
        )
        content = content.replace(b"# Solet name: NOT", comment + b"# Solet name: NOT")
        content = (
            content.replace(
                b"# it; keep the session launchers in lockstep.\n",
                b"# it; keep both launchers in lockstep the same way the git-controller export\n# above is kept in lockstep with the fleet script.\n",
            )
            .replace(
                b"# after hydration Step 2 is re-run.\nunset GIT_CONTROLLER_NAME\n",
                b"# after hydration Step 2 is re-run. Keep the SHELL line before the\n# GIT_CONTROLLER_NAME line: a solo clone deletes that line and must keep SHELL.\n",
            )
            .replace(
                b"exec claude --name",
                b'GIT_CONTROLLER_NAME="{{GIT_CONTROLLER_NAME}}" \\\nexec claude --name',
            )
        )
    elif name == "codex_launcher.template":
        content = content.replace(
            b"unset GIT_CONTROLLER_NAME\n",
            b'export GIT_CONTROLLER_NAME="{{GIT_CONTROLLER_NAME}}"\n',
        )
    digests = {
        "claude_launcher.template": "4e45eb8a6304be32af81690090a10270fcf17efe41cc652a20750f8b681b7b38",
        "codex_launcher.template": "7360e7ce8ef3d6e6bd2596b8ba65730c8c12e1e0d076091056024061febff039",
        "fleet_functions.zsh.template": "a3e0c0c0661e1e0e32f65f615f01235736e2558ba2d5965c35379098fea2bf16",
        "solet.zsh.template": "d4c5fb1cc8ee16d971ba38cac9f9a89df52622430f8da6285a41e2468f464947",
    }
    assert digest_of(content) == "sha256:" + digests[name], name
    assert b"unset GIT_CONTROLLER_NAME" not in content
    return content


def _legacy_controller_file(name: str, target: Path) -> tuple[bytes, bytes]:
    """Render authentic predecessors; only the environment adds a legacy export."""
    content = _legacy_controller_template(name).replace(b"{{SOLET_NAME}}", b"fixture").replace(b"{{CLONE_DIR_ZSH}}", shlex.quote(str(target)).encode()).replace(b"{{CLONE_DIR}}", str(target).encode()).replace(b"{{GIT_CONTROLLER_NAME}}", b"Lead-Git")
    assert b"{{" not in content
    if name == "solet.zsh.template":
        content += b"export GIT_CONTROLLER_NAME=old-controller\n"
    content += b"# operator content\n# GIT_CONTROLLER_NAME=comment stays\nexport KEEP='value with spaces'"
    if name in {"claude_launcher.template", "fleet_functions.zsh.template"}:
        indent = b"  " if name == "fleet_functions.zsh.template" else b""
        expected = content.replace(indent + b'GIT_CONTROLLER_NAME="Lead-Git" \\\n', b"")
        prefix = indent + b'SOLET_NAME="fixture" \\\n'
        expected = expected.replace(prefix, indent + b"unset GIT_CONTROLLER_NAME\n" + prefix)
    else:
        assignment = b"export GIT_CONTROLLER_NAME=old-controller\n" if name == "solet.zsh.template" else b'export GIT_CONTROLLER_NAME="Lead-Git"\n'
        expected = content.replace(assignment, b"unset GIT_CONTROLLER_NAME\n")
    return content, expected


def _installed_controller_files(target: Path, newline: bytes = b"\n") -> tuple[dict[Path, bytes], dict[Path, bytes]]:
    originals: dict[Path, bytes] = {}
    expected: dict[Path, bytes] = {}
    for artifact in retirement_artifacts():
        path = Path(artifact["logical_destination"].replace("{TARGET}", str(target)).replace("{NAME}", "fixture"))
        path.parent.mkdir(parents=True, exist_ok=True)
        before, after = _legacy_controller_file(Path(artifact["template_ref"]).name, target)
        originals[path] = before.replace(b"\n", newline)
        expected[path] = after.replace(b"\n", newline)
        path.write_bytes(originals[path])
        path.chmod(0o640)
    return originals, expected


def _check_updated_files(fixture: Any, originals: dict[Path, bytes], expected: dict[Path, bytes]) -> None:
    for path, content in expected.items():
        assert path.read_bytes() == content, f"real update clears the retired setting and preserves every other byte: {path.name}"
        assert path.stat().st_mode & 0o777 == 0o640
    record = fixture.record()
    backups = backup_root(fixture.paths, record.instance_id, cast(str, record.last_verified_operation_id))
    for artifact in retirement_artifacts():
        path = Path(artifact["logical_destination"].replace("{TARGET}", str(fixture.target)).replace("{NAME}", "fixture"))
        assert (backups / artifact["artifact_id"] / "before.bytes").read_bytes() == originals[path], "Manager backup retains exact original bytes"


def _check_update_idempotency(fixture: Any, fingerprint: str, originals: dict[Path, bytes]) -> None:
    snapshot = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in originals}
    second = preview_update_instance(fixture.request)
    assert second.exit_code == 0, (second.status, second.error_kind)
    try:
        apply_update(fixture.request, fingerprint)
    except ProbeDriftError as exc:
        assert "already promoted" in str(exc), str(exc)
    else:
        raise AssertionError("the existing Manager reports an already promoted install on repeated apply")
    assert all((path.read_bytes(), path.stat().st_mtime_ns) == value for path, value in snapshot.items()), "a second update is byte- and write-identical"


def _check_updated_launcher_inheritance(root: Path, target: Path) -> None:
    """Execute the updated LF files; harmless child binaries observe all four families."""
    binaries = root / "test-bin"
    binaries.mkdir()
    probe = '#!/bin/sh\nif [ "${GIT_CONTROLLER_NAME+x}" = x ]; then printf inherited; else printf missing; fi\nprintf "|%s|%s\\n" "${SOLET_NAME-}" "${AGENT_SESSION_LABEL-}"\n'
    for name in ("claude", "codex"):
        binary = binaries / name
        binary.write_text(probe)
        binary.chmod(0o700)
    tmux = binaries / "tmux"
    tmux.write_text('#!/bin/sh\nif [ "$1" = display-message ]; then echo RetirementProbe; else exit 1; fi\n')
    tmux.chmod(0o700)
    commands = [
        (
            ["/bin/zsh", str(target / "client/bin/claude-fixture"), "RetirementProbe"],
            "missing|fixture|RetirementProbe",
        ),
        (
            ["/bin/zsh", str(target / "client/bin/codex-fixture"), "RetirementProbe"],
            "missing|fixture|RetirementProbe",
        ),
        (
            [
                "/bin/zsh",
                "-f",
                "-c",
                'source "$1"; _claude_for_fixture RetirementProbe',
                "--",
                str(target / "client/fixture-fleet.zsh"),
            ],
            "missing|fixture|RetirementProbe",
        ),
        (
            [
                "/bin/zsh",
                "-f",
                "-c",
                'source "$1"; "$2"',
                "--",
                str(target / "client/fixture.zsh"),
                str(binaries / "codex"),
            ],
            "missing|caller-solet|Caller",
        ),
    ]
    environment = {
        **os.environ,
        "PATH": f"{binaries}:/usr/bin:/bin",
        "CODEX_BIN": str(binaries / "codex"),
        "GIT_CONTROLLER_NAME": "stale-parent",
        "SOLET_NAME": "caller-solet",
        "AGENT_SESSION_LABEL": "Caller",
        "fixture_FLEET_TRANSPORT": "mcp",
        "TMUX": "fixture-only",
    }
    for command, expected in commands:
        completed = subprocess.run(command, env=environment, capture_output=True, text=True, check=False, timeout=15)
        assert (completed.returncode, completed.stdout.strip()) == (0, expected), (
            command,
            completed.stdout,
            completed.stderr,
        )


def _check_real_update_case(root: Path, newline: bytes) -> None:
    """The real Manager preserves LF/CRLF, backs up originals, and verifies removal."""
    extra: dict[str, str | bytes] = {
        f"{TEMPLATES}/{name}": template_bytes(name)
        for name in (
            "claude_launcher.template",
            "codex_launcher.template",
            "fleet_functions.zsh.template",
            "solet.zsh.template",
        )
    }
    fixture = build_fixture(root, document=_document, extra_candidate_files=extra)
    exclude = fixture.target / ".git/info/exclude"
    exclude.write_text(exclude.read_text() + "client/\n")
    originals, expected = _installed_controller_files(fixture.target, newline)
    advance_to_source_advanced(fixture)
    fingerprint = runtime_fingerprint(fixture)
    assert all(path.read_bytes() == content for path, content in originals.items()), "preview writes no bytes"
    applied = apply_update(fixture.request, fingerprint)
    assert applied.status == "promoted", (applied.status, applied.error_kind, applied.data)
    if newline == b"\n":
        _check_updated_launcher_inheritance(root, fixture.target)
    _check_updated_files(fixture, originals, expected)
    _check_update_idempotency(fixture, fingerprint, originals)


def check_real_update(root: Path) -> None:
    """Authentic old launchers acquire clearing, without dropping their session identity."""
    for name, newline in (("lf", b"\n"), ("crlf", b"\r\n")):
        _check_real_update_case(root / name, newline)
    diagnostic = artifact_check(
        "unknown-state",
        "retired_controller_claude_launcher",
        {
            "kind": "rendered_whole",
            "state": "made_up",
            "action": "none",
            "conflict": "none",
            "verification_scope": "retired_controller_assignments",
        },
        "fixture",
    )
    assert diagnostic.status is DiagnosticStatus.FAILED and diagnostic.reason_code == "artifact_state_unrecognised", "Doctor rejects an invented state even on a removal-scoped artifact"


def check_materialized_launchers(root: Path) -> None:
    """Materialize through the install operation, then execute every launcher."""
    for topology in ("solo", "fleet"):
        target = root / topology
        runtime = FakeRuntime(root / f"{topology}-home")
        request = AdapterRequest.from_dict(
            _raw_request(
                target,
                phase="apply",
                purpose=None,
                public_inputs={
                    "execution_topology": topology,
                    "selected_coding_agents": ["claude_code", "codex"],
                },
            )
        )
        applied = dispatch_request(request, runtime)
        assert applied["checkpoint_status"] == "applied", applied
        fleet = target / "client/iris-fleet.zsh"
        assert fleet.exists() == (topology == "fleet")
        commands = [["/bin/zsh", str(target / "client/bin" / name)] for name in ("claude-iris", "codex-iris")]
        if topology == "fleet":
            commands.append(["/bin/zsh", "-f", "-c", 'source "$1"; _claude_for_iris TestRole', "--", str(fleet)])
        binaries = target / "test-bin"
        binaries.mkdir()
        for name in ("claude", "codex"):
            binary = binaries / name
            binary.write_text('#!/bin/sh\nif [ "${GIT_CONTROLLER_NAME+x}" = x ]; then echo inherited; else echo missing; fi\n')
            binary.chmod(0o700)
        tmux = binaries / "tmux"
        tmux.write_text('#!/bin/sh\nif [ "$1" = display-message ]; then echo TestRole; else exit 1; fi\n')
        tmux.chmod(0o700)
        environment = {
            **os.environ,
            "PATH": f"{binaries}:/usr/bin:/bin",
            "CODEX_BIN": str(binaries / "codex"),
            "GIT_CONTROLLER_NAME": "stale-caller",
            "iris_FLEET_TRANSPORT": "mcp",
            "TMUX": "fixture-only",
        }
        for command in commands:
            completed = subprocess.run(command, env=environment, capture_output=True, text=True, check=False, timeout=15)
            assert (completed.returncode, completed.stdout.strip()) == (0, "missing"), (
                command,
                completed.stdout,
                completed.stderr,
            )


def materialize_pre_retirement_contracts(destination: Path) -> Path:
    """Rebuild the exact preceding contract without requiring a Git checkout."""
    destination.mkdir(parents=True, exist_ok=True)
    for filename in contract_filenames():
        source = _ROOT / KB / filename
        if filename == "macos_setup_flow.json":
            source = _ROOT / "solet_cli/tests/fixtures/setup_flow_before_controller_retirement.fixture"
        shutil.copyfile(source, destination / filename)
    ContractBundle.load(
        source_revision="a" * 40,
        directory=destination,
        expected_digest="sha256:3bb6857b4fee5a2c36cfe4f2f90d8313bb800b45eea2021e999dcd13d2b62563",
    )
    return destination


def _retirement_manager(root: Path, unknown_section: str | None = None) -> tuple[Any, Any, Any]:
    support = runpy.run_path(str(_ROOT / "solet_cli/tests/contract_reconciliation_lm_studio_not_recommended_smoke.py"))
    source = ContractBundle.load(
        source_revision=support["_LIVE_STABLE_REVISION"],
        directory=support["_SOURCE_FIXTURE"],
        expected_digest=support["_SOURCE_DIGEST"],
    )
    manager, paths, before = support["_manager"](root, source)
    answers = cast(dict[str, Any], copy.deepcopy(before.answers))
    answers["public_inputs"]["git_controller_name"] = "Chosen-Controller"
    if unknown_section is not None:
        answers[unknown_section]["unlisted_retired_field"] = "must-remain-an-error"
    before = before.with_answers(answers)
    write_transaction(paths.transaction_path("fixture"), before)
    (root / "target/.solet/install-state.json").write_text(json.dumps(target_install_state_projection(before)) + "\n")
    return manager, paths, before


def _check_retirement_record(record_path: Path, transaction_path: Path) -> None:
    receipt = json.loads(record_path.read_text())
    assert receipt["state"] == "applied" and receipt["schema_version"] == 3
    assert receipt["retired_answers"] == [
        {
            "section": "decisions",
            "id": "git_mutation_control",
            "prior_value": "designated_controller",
        },
        {
            "section": "public_inputs",
            "id": "git_controller_name",
            "prior_value": "Chosen-Controller",
        },
    ]
    backup = next(row for row in receipt["files"] if row["path"] == str(transaction_path))
    before = json.loads(base64.b64decode(backup["before_base64"]))["answers"]
    assert before["decisions"]["git_mutation_control"] == "designated_controller"
    assert before["public_inputs"]["git_controller_name"] == "Chosen-Controller"
    assert len(list(record_path.parent.glob("*.json"))) == 2, "prepared and applied records with before-images are retained separately"


def check_retirement_reconciliation(root: Path) -> None:
    manager, paths, before = _retirement_manager(root / "known")
    transaction_path = paths.transaction_path("fixture")
    original = transaction_path.read_bytes()
    preview = manager.run("fixture", dry_run=True, approved_fingerprint=None)
    assert preview.status == "preview_ready" and transaction_path.read_bytes() == original
    stale = manager.run("fixture", dry_run=False, approved_fingerprint="sha256:" + "0" * 64)
    assert stale.status != "reconciled" and transaction_path.read_bytes() == original
    applied = manager.run("fixture", dry_run=False, approved_fingerprint=str(preview.data["approval_fingerprint"]))
    assert applied.status == "reconciled", applied
    after = load_transaction(transaction_path)
    assert after is not None
    answers = cast(dict[str, Any], after.answers)
    assert "git_mutation_control" not in answers["decisions"] and "git_controller_name" not in answers["public_inputs"]
    assert before.flow_source_revision == after.flow_source_revision
    _check_retirement_record(Path(str(applied.data["retirement_record"])), transaction_path)
    assert reconciliation_module.reconciliation_receipt_before_flow_source_revision(paths, "fixture") == before.flow_source_revision
    _check_unlisted_answers_rejected(root)
    _check_retirement_metadata_validation(root)
    _check_retirement_recovery(root)


def _check_unlisted_answers_rejected(root: Path) -> None:
    for section in ("decisions", "public_inputs"):
        manager, paths, _before = _retirement_manager(root / section, section)
        path = paths.transaction_path("fixture")
        original = path.read_bytes()
        try:
            manager.run("fixture", dry_run=True, approved_fingerprint=None)
        except StateConflictError as exc:
            assert "normalized answers reference unknown ids" in str(exc) and "unlisted_retired_field" in str(exc)
        else:
            raise AssertionError("an unlisted unknown decision or input must remain an error")
        assert path.read_bytes() == original, "an unknown answer never changes persisted state"


def _check_retirement_metadata_validation(root: Path) -> None:
    manifest = _ROOT / "solet_cli/src/solet_manager/released_metadata/contract_reconciliation_manifest.json"
    entries = json.loads(manifest.read_text())["migrations"]
    entry = next(row for row in entries if row.get("retired_decision_ids"))
    for field in ("retired_decision_ids", "retired_public_input_ids"):
        for value in ("not-an-array", ["duplicate", "duplicate"], [""]):
            invalid = copy.deepcopy(entry)
            invalid[field] = value
            path = root / "invalid-retirement.json"
            path.write_text(json.dumps({"schema_version": 1, "migrations": [invalid]}))
            try:
                load_contract_reconciliations(manifest_path=path)
            except ContractError:
                pass
            else:
                raise AssertionError(f"invalid retirement list accepted: {field}={value}")


def _check_retirement_recovery(root: Path) -> None:
    manager, paths, before = _retirement_manager(root / "recovery")
    path = paths.transaction_path("fixture")
    original = path.read_bytes()
    preview = manager.run("fixture", dry_run=True, approved_fingerprint=None)
    replace_bytes = reconciliation_module.atomic_replace_bytes
    journal_written = False

    def interrupted(destination: Path, content: bytes, *, mode: int) -> None:
        nonlocal journal_written
        if journal_written:
            raise RuntimeError("injected retirement interruption")
        replace_bytes(destination, content, mode=mode)
        # The cache is staged before a receipt exists. Interrupt only after
        # the real transaction has changed and its before-image is durable.
        journal_written = destination == path

    with patch.object(reconciliation_module, "atomic_replace_bytes", side_effect=interrupted):
        try:
            manager.run(
                "fixture",
                dry_run=False,
                approved_fingerprint=str(preview.data["approval_fingerprint"]),
            )
        except RuntimeError as exc:
            assert str(exc) == "injected retirement interruption"
        else:
            raise AssertionError("the interrupted retirement must not report success")
    assert path.read_bytes() != original, "the injected crash occurs after the real journal changes"
    assert reconciliation_module.recover_contract_reconciliation(paths, "fixture")
    assert path.read_bytes() == original, "recovery restores the exact transaction and both retired answers"
    assert reconciliation_module.reconciliation_receipt_before_flow_source_revision(paths, "fixture") == before.flow_source_revision
    assert len(list((paths.reconciliations_dir / "fixture.history").glob("*.recovered.json"))) == 1
    assert not reconciliation_module.recover_contract_reconciliation(paths, "fixture"), "terminal recovery is idempotent"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=("fresh", "update", "launchers", "reconciliation"))
    selected = parser.parse_args().case
    for name, check in (
        ("fresh", check_fresh_plan),
        ("update", check_real_update),
        ("launchers", check_materialized_launchers),
        ("reconciliation", check_retirement_reconciliation),
    ):
        if selected is None or selected == name:
            with TemporaryDirectory(prefix="controller-retirement-") as temporary:
                check(Path(temporary))
            print(f"controller_setting_retirement_smoke: {name} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
