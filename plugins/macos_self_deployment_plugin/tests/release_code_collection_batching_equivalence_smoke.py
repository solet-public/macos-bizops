#!/usr/bin/env python3
"""r68 — the batched release-code collector equals the per-file one, with O(1) spawns.

iss_f694ccef: the collector made one ``git check-ignore`` per file while planning,
one ``cp -c`` per file while materializing and one ``git show`` per file while
attesting the artifact against HEAD, so a 61k-file checkout held the solet's serial
dispatch loop for 86 minutes.  This is a performance change, so the proof is
equivalence plus a spawn count, not red-to-green.

Both collectors run over the SAME throwaway Git fixture: the frozen base copy
(``_release_code_collection_reference``) and the production module.

* [1] clean fixture with ignored directories, a negated ignore, nested environments,
  odd file names and an executable bit: plans, exclusions, manifest payloads,
  manifest bytes and materialized trees are identical.
* [2] dirty fixture: every artifact-to-HEAD mismatch kind is reported identically.
* [3] every refusal (links, FIFO, environment evidence, a changed source) carries the
  same message from both collectors.
* [4] a HEAD gitlink at a path the artifact holds as a file, and a gitless source.
* [5] spawn counts: base is O(files) per phase; the batch is independent of the file
  count in the ignore and attestation phases, and ``cp -c`` runs once per directory
  (and per ``COW_COPY_BATCH_FILES`` sources within one).
* [6] the cutover preflight: root-manifest drift refuses BEFORE any build work.

Run:
    .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/release_code_collection_batching_equivalence_smoke.py
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _release_code_collection_reference as reference  # noqa: E402
import _release_manager_smoke_support as support  # noqa: E402
from macos_self_deployment_plugin import release_code_collection as batched  # noqa: E402
from macos_self_deployment_plugin.constants import COLOR_BLUE, RestartReasonCode  # noqa: E402
from macos_self_deployment_plugin.preflight_probe_runner import ProbeOutcome  # noqa: E402
from macos_self_deployment_plugin.release_manager import (  # noqa: E402
    CandidatePaths,
    GcResult,
    SwapResult,
)
from macos_self_deployment_plugin.router_client import RouterClient  # noqa: E402
from macos_self_deployment_plugin.schema_preflight import PreflightVerdict  # noqa: E402
from macos_self_deployment_plugin.swap_orchestrator import (  # noqa: E402
    PRE_BUILD_PROBE_RELEASE_ID,
    SwapOrchestrator,
)

SUBTREES = ("ananta", "plugins")
_IDENTITY = (
    "-c", "user.name=r68-smoke", "-c", "user.email=r68-smoke@example.invalid",
    "-c", "commit.gpgsign=false",
)
_DRIFT_ENVELOPE = "BLOCKING root_manifest drift: 4 untracked repo-root file(s)"
_REAL_POPEN = subprocess.Popen

_recorder = support.SmokeRecorder()


def _git(source: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(source), *_IDENTITY, *args],
        capture_output=True, text=True, check=False, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {args!r} failed: {result.stderr.strip()}")
    return result.stdout


def _write(path: Path, text: str = "x\n", mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mode is not None:
        path.chmod(mode)


def _build_source(root: Path, *, data_files: int = 12, commit: bool = True) -> Path:
    """A committed fixture shaped like the release tree, plus untracked extras."""
    source = root / "src"
    _write(source / ".gitignore", (
        "*.pyc\nbuild/\n!keep.pyc\nplugins/foo_plugin/local-assets/\n"
        "plugins/foo_plugin/.venv-ignored/\n__pycache__/\n"
    ))
    _write(source / "ananta/src/ananta/__init__.py", "VERSION = 1\n")
    _write(source / "ananta/src/ananta/core.py", "def core() -> int:\n    return 1\n")
    for index in range(data_files):
        _write(source / f"ananta/data/f{index:04d}.txt", f"row {index}\n")
    for index in range(3):
        _write(source / f"ananta/src/ananta/sub{index}/m.py", f"M = {index}\n")
    _write(source / "plugins/foo_plugin/src/foo_plugin/__init__.py")
    _write(source / "plugins/foo_plugin/src/foo_plugin/run.sh", "#!/bin/sh\n", mode=0o755)
    _write(source / "plugins/foo_plugin/src/foo_plugin/with space.py")
    _write(source / "plugins/foo_plugin/src/foo_plugin/ünï.py")
    _write(source / "plugins/foo_plugin/src/foo_plugin/odd\nname.py")
    _write(source / "plugins/foo_plugin/src/foo_plugin/-dash.py")
    _write(source / "plugins/foo_plugin/readonly.txt", "ro\n", mode=0o444)
    _git(source, "init", "--quiet")
    _git(source, "add", "-A")
    if commit:
        _git(source, "commit", "--quiet", "-m", "baseline")
    # Untracked: ignored directories and files, a negated ignore, two environments.
    _write(source / "plugins/foo_plugin/local-assets/blob.bin", "blob\n")
    _write(source / "plugins/foo_plugin/build/out.bin", "out\n")
    _write(source / "plugins/foo_plugin/src/foo_plugin/__pycache__/a.cpython-313.pyc")
    _write(source / "ananta/stray.pyc")
    _write(source / "ananta/keep.pyc", "kept\n")
    for name in (".venv-local", ".venv-ignored"):
        _write(source / f"plugins/foo_plugin/{name}/pyvenv.cfg", "home = /x\n")
        _write(source / f"plugins/foo_plugin/{name}/bin/python3", "#!/bin/sh\n", mode=0o755)
    return source


def _dirty(source: Path) -> None:
    """Every artifact-to-HEAD disagreement kind, after the baseline commit."""
    (source / "ananta/src/ananta/core.py").write_text("def core() -> int:\n    return 2\n")
    (source / "ananta/data/f0001.txt").chmod(0o700)
    (source / "plugins/foo_plugin/src/foo_plugin/run.sh").chmod(0o644)
    (source / "ananta/data/f0002.txt").unlink()
    _write(source / "ananta/untracked_new.py", "NEW = 1\n")


def _collector(module: ModuleType, source: Path, cp_binary: str = "cp") -> Any:
    return module.ReleaseCodeCollector(
        source_root=source, code_subtrees=SUBTREES, cp_binary=cp_binary, clone_timeout_seconds=60.0
    )


def _tree(destination: Path) -> dict[str, tuple[int, str]]:
    result: dict[str, tuple[int, str]] = {}
    for path in sorted(destination.rglob("*")):
        if path.is_file() and not path.is_symlink():
            result[path.relative_to(destination).as_posix()] = (
                path.stat().st_mode & 0o7777,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
    return result


def _attempt(call: Callable[[], Any]) -> tuple[str, Any]:
    try:
        return "ok", call()
    except Exception as exc:  # noqa: BLE001 - the verdict compared IS the refusal text
        return "err", f"{type(exc).__name__}: {exc}"


def _full_run(module: ModuleType, source: Path, scratch: Path, tag: str) -> dict[str, Any]:
    """Plan, attest, write the manifest and materialize; keep every observable."""
    collector = _collector(module, source)
    plan = collector.plan()
    payload = collector.manifest_payload(plan)
    manifest = scratch / f"{tag}.manifest.json"
    manifest_sha = module.write_selected_file_manifest(manifest, payload)
    destination = scratch / f"{tag}.code"
    collector.materialize(plan, destination)
    collector.verify_materialized_manifest(plan, destination, manifest)
    return {
        "plan": dataclasses.asdict(plan),
        "payload": payload,
        "manifest_sha": manifest_sha,
        "tree": _tree(destination),
    }


def _same(label: str, source: Path, scratch: Path) -> dict[str, Any] | None:
    ref = _attempt(lambda: _full_run(reference, source, scratch, "ref"))
    fix = _attempt(lambda: _full_run(batched, source, scratch, "fix"))
    _recorder.check(ref[0] == "ok", f"{label}: reference collector completes")
    _recorder.check(
        ref == fix,
        f"{label}: plan, exclusions, payload, manifest sha and materialized tree are identical",
    )
    return cast("dict[str, Any] | None", ref[1] if ref[0] == "ok" else None)


@contextmanager
def _scratch() -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="r68-collect-equiv-"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


class _Spawns:
    """Count every child process the collector starts, by what it runs."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    @staticmethod
    def _kind(argv: list[str]) -> str:
        if argv and argv[0] == "git":
            rest = argv[3:] if len(argv) > 3 and argv[1] == "-C" else argv[1:]
            return f"git {rest[0]}" if rest else "git"
        return os.path.basename(argv[0]) if argv else "?"

    @contextmanager
    def watching(self) -> Iterator[Counter[str]]:
        counts = self.counts
        kind = self._kind

        class Counting(_REAL_POPEN):  # type: ignore[type-arg, misc]
            def __init__(self, args: Any, *rest: Any, **kwargs: Any) -> None:
                counts[kind([str(part) for part in args])] += 1
                super().__init__(args, *rest, **kwargs)

        subprocess.Popen = Counting  # type: ignore[misc, assignment]
        try:
            yield counts
        finally:
            subprocess.Popen = _REAL_POPEN  # type: ignore[misc]


def _spawn_counts(module: ModuleType, source: Path, scratch: Path, tag: str) -> Counter[str]:
    spawns = _Spawns()
    with spawns.watching() as counts:
        _full_run(module, source, scratch, tag)
    return Counter(counts)


def _check_clean_selection(outcome: dict[str, Any]) -> None:
    exclusions = {item["relative_path"]: item["reason"] for item in outcome["plan"]["exclusions"]}
    selected = {item["relative_path"] for item in outcome["plan"]["files"]}
    _recorder.check(
        exclusions.get("plugins/foo_plugin/local-assets") == "gitignored_path"
        and exclusions.get("ananta/stray.pyc") == "gitignored_path"
        and exclusions.get("plugins/foo_plugin/.venv-local") == "nested_python_environment"
        and exclusions.get("plugins/foo_plugin/.venv-ignored") == "nested_python_environment",
        "[1] fixture exercises ignored directory, ignored file and both nested environments",
    )
    _recorder.check(
        "ananta/keep.pyc" in selected
        and "plugins/foo_plugin/src/foo_plugin/odd\nname.py" in selected
        and "plugins/foo_plugin/src/foo_plugin/-dash.py" in selected,
        "[1] a negated ignore, a newline name and a dash name are selected",
    )
    _recorder.check(
        outcome["payload"]["artifact_to_head_equality"]["state"] == "unequal",
        "[1] the untracked keep.pyc makes the artifact unequal to HEAD, identically",
    )


def _check_dirty_mismatches(outcome: dict[str, Any]) -> None:
    kinds = {
        mismatch["kind"]
        for mismatch in outcome["payload"]["artifact_to_head_equality"]["mismatches"]
    }
    _recorder.check(
        kinds == {
            "missing_from_artifact", "extra_in_artifact", "bytes_differ",
            "executable_mode_differs",
        },
        f"[2] every mismatch kind is exercised and reported ({sorted(kinds)})",
    )


def _leg_equivalence() -> None:
    with _scratch() as root:
        outcome = _same("[1] clean fixture", _build_source(root), root)
        if outcome is not None:
            _check_clean_selection(outcome)
    with _scratch() as root:
        source = _build_source(root)
        _dirty(source)
        outcome = _same("[2] dirty fixture", source, root)
        if outcome is not None:
            _check_dirty_mismatches(outcome)


def _plant(kind: str, source: Path) -> None:
    base = source / "plugins/foo_plugin/src/foo_plugin"
    if kind == "untracked symlink":
        os.symlink("__init__.py", base / "untracked_link.py")
    elif kind == "tracked symlink":
        os.symlink("__init__.py", base / "tracked_link.py")
        _git(source, "add", "-f", "plugins/foo_plugin/src/foo_plugin/tracked_link.py")
    elif kind == "ignored symlink":
        os.symlink("__init__.py", base / "ignored_link.pyc")
    elif kind == "fifo":
        os.mkfifo(base / "pipe.py")
    elif kind == "incomplete environment":
        _write(source / "plugins/foo_plugin/halfenv/pyvenv.cfg", "home = /x\n")
    elif kind == "tracked environment":
        _write(source / "plugins/foo_plugin/trackedenv/pyvenv.cfg", "home = /x\n")
        _write(source / "plugins/foo_plugin/trackedenv/bin/python3", "#!/bin/sh\n", mode=0o755)
        _git(source, "add", "-f", "plugins/foo_plugin/trackedenv")
    elif kind == "code subtree link":
        shutil.rmtree(source / "plugins")
        os.symlink("ananta", source / "plugins")
    else:
        raise ValueError(kind)


def _both_verdicts(source: Path, root: Path) -> tuple[tuple[str, Any], tuple[str, Any]]:
    return (
        _attempt(lambda: _full_run(reference, source, root, "ref")),
        _attempt(lambda: _full_run(batched, source, root, "fix")),
    )


def _changed_source_verdict(module: ModuleType, source: Path, root: Path) -> tuple[str, Any]:
    """Materialize after the source drifted from the plan; return the verdict."""
    collector = _collector(module, source)
    plan = collector.plan()
    target = source / "ananta/data/f0003.txt"
    original = target.read_text()
    target.write_text("changed after selection\n")
    try:
        return _attempt(lambda: collector.materialize(plan, root / f"{module.__name__}.code"))
    finally:
        target.write_text(original)


def _stray_file_verdict(module: ModuleType, source: Path, root: Path) -> tuple[str, Any]:
    """Verify a materialized tree that gained a file the plan never selected."""
    collector = _collector(module, source)
    plan = collector.plan()
    destination = root / f"{module.__name__}.v.code"
    collector.materialize(plan, destination)
    _write(destination / "ananta/data/stray.pyc")
    return _attempt(lambda: collector.verify_materialized(plan, destination))


def _leg_refusals() -> None:
    for kind in (
        "untracked symlink", "tracked symlink", "ignored symlink", "fifo",
        "incomplete environment", "tracked environment", "code subtree link",
    ):
        with _scratch() as root:
            source = _build_source(root)
            _plant(kind, source)
            ref, fix = _both_verdicts(source, root)
            _recorder.check(ref == fix, f"[3] {kind}: same verdict ({ref[0]})")
            if kind != "ignored symlink":
                _recorder.check(
                    ref[0] == "err" and fix[0] == "err", f"[3] {kind}: both collectors refuse"
                )
    with _scratch() as root:
        source = _build_source(root)
        verdicts = [_changed_source_verdict(module, source, root) for module in (reference, batched)]
        _recorder.check(
            verdicts[0] == verdicts[1] and verdicts[0][0] == "err"
            and "source changed after selection" in verdicts[0][1],
            f"[3] a source changed after selection refuses identically ({verdicts[1][1]})",
        )
    with _scratch() as root:
        source = _build_source(root)
        verdicts = [_stray_file_verdict(module, source, root) for module in (reference, batched)]
        _recorder.check(
            verdicts[0] == verdicts[1] and verdicts[0][0] == "err",
            "[3] a stray file in the materialized tree trips the same set-equality refusal",
        )


_TAMPERING_CP = """#!/bin/sh
/bin/cp "$@" || exit $?
for last; do :; done
if [ -d "$last" ]; then target="$last/f0003.txt"; else target="$last"; fi
case "$target" in */f0003.txt) [ -f "$target" ] && echo tampered >> "$target";; esac
exit 0
"""
_FAILING_CP = "#!/bin/sh\necho 'planted cp failure' >&2\nexit 7\n"


def _copy_verdict(module: ModuleType, source: Path, root: Path, script: str) -> tuple[str, Any]:
    """Materialize through a ``cp`` stand-in that corrupts or fails a copy."""
    cp_binary = root / "cp-standin.sh"
    cp_binary.write_text(script)
    cp_binary.chmod(0o755)
    collector = _collector(module, source, str(cp_binary))
    plan = collector.plan()
    return _attempt(lambda: collector.materialize(plan, root / f"{module.__name__}.cp.code"))


def _leg_copy_failures() -> None:
    with _scratch() as root:
        source = _build_source(root)
        verdicts = [_copy_verdict(module, source, root, _TAMPERING_CP) for module in (reference, batched)]
        _recorder.check(
            verdicts[0] == verdicts[1] and verdicts[0][0] == "err"
            and "materialized bytes differ: ananta/data/f0003.txt" in verdicts[0][1],
            f"[3] one tampered copy among a directory's files is refused identically ({verdicts[1][1]})",
        )
    with _scratch() as root:
        source = _build_source(root)
        verdicts = [_copy_verdict(module, source, root, _FAILING_CP) for module in (reference, batched)]
        _recorder.check(
            all(kind == "err" and "exited 7: planted cp failure" in text for kind, text in verdicts),
            "[3] a failing cp refuses in both collectors (the batch names its directory, "
            "not one file)",
        )


def _leg_gitlink_and_gitless() -> None:
    with _scratch() as root:
        source = _build_source(root)
        # A submodule's commit is not in the superproject's object store, which is
        # what makes ``git show HEAD:<gitlink>`` fail.
        absent_commit = "1234567890abcdef1234567890abcdef12345678"
        _git(source, "update-index", "--add", "--cacheinfo", f"160000,{absent_commit},plugins/gitlinked")
        _git(source, "commit", "--quiet", "-m", "gitlink")
        _write(source / "plugins/gitlinked", "a file where HEAD records a gitlink\n")
        outcome = _same("[4] HEAD gitlink at an artifact file", source, root)
        if outcome is not None:
            equality = outcome["payload"]["artifact_to_head_equality"]
            _recorder.check(
                equality["state"] == "unknown" and equality["reason"] == "head_blob_unavailable",
                f"[4] the unavailable HEAD blob stays an unknown verdict ({equality['state']})",
            )
    with _scratch() as root:
        source = _build_source(root)
        shutil.rmtree(source / ".git")
        outcome = _same("[4] gitless source", source, root)
        if outcome is not None:
            _recorder.check(
                outcome["payload"]["artifact_to_head_equality"]["state"] == "unknown",
                "[4] a gitless source reports an unknown artifact-to-HEAD verdict",
            )


def _leg_spawn_counts() -> None:
    results: dict[str, dict[int, Counter[str]]] = {"ref": {}, "fix": {}}
    for data_files in (12, 60):
        with _scratch() as root:
            source = _build_source(root, data_files=data_files)
            results["ref"][data_files] = _spawn_counts(reference, source, root, "ref")
            results["fix"][data_files] = _spawn_counts(batched, source, root, "fix")
    small, large = 12, 60
    growth = large - small
    for phase in ("git check-ignore", "git show", "cp"):
        base_growth = results["ref"][large][phase] - results["ref"][small][phase]
        _recorder.check(
            base_growth >= growth,
            f"[5] base {phase}: {results['ref'][small][phase]} -> {results['ref'][large][phase]} "
            f"spawns for +{growth} files (O(files))",
        )
    fix_small, fix_large = results["fix"][small], results["fix"][large]
    for phase in ("git check-ignore", "git cat-file"):
        _recorder.check(
            fix_small[phase] == fix_large[phase] == 1,
            f"[5] fix {phase}: {fix_small[phase]} -> {fix_large[phase]} spawns (one, whatever the file count)",
        )
    _recorder.check(
        fix_small["git show"] == fix_large["git show"] == 0,
        "[5] fix never runs git show per file",
    )
    _recorder.check(
        fix_small["cp"] == fix_large["cp"] and fix_small["cp"] < results["ref"][small]["cp"],
        f"[5] fix cp -c: {fix_small['cp']} -> {fix_large['cp']} spawns (per directory, not per file; "
        f"base {results['ref'][small]['cp']} -> {results['ref'][large]['cp']})",
    )
    with _scratch() as root:
        source = _build_source(root, data_files=60)
        plan = _collector(batched, source).plan()
        directories = {os.path.dirname(item.relative_path) for item in plan.files}
        original = batched.COW_COPY_BATCH_FILES
        batched.COW_COPY_BATCH_FILES = 7
        try:
            counts = _spawn_counts(batched, source, root, "chunk")
            tree_chunked = _tree(root / "chunk.code")
        finally:
            batched.COW_COPY_BATCH_FILES = original
        per_directory = Counter(os.path.dirname(item.relative_path) for item in plan.files)
        expected = sum(math.ceil(count / 7) for count in per_directory.values())
        _recorder.check(
            counts["cp"] == expected and expected > len(directories),
            f"[5] a directory larger than COW_COPY_BATCH_FILES splits into several cp calls "
            f"({counts['cp']} == {expected})",
        )
        reference_tree = _tree(
            _materialize_reference(source, root / "reference-for-chunk.code")
        )
        _recorder.check(tree_chunked == reference_tree, "[5] the chunked copy materializes the same tree")


def _materialize_reference(source: Path, destination: Path) -> Path:
    collector = _collector(reference, source)
    collector.materialize(collector.plan(), destination)
    return destination


class _Router:
    def status(self) -> dict[str, Any]:
        return {"active_color": COLOR_BLUE, "active_instance_id": "blue-id", "colors": []}


class _ReleaseManager:
    def __init__(self) -> None:
        self.build_count = 0

    def gc(self, *, keep: int | None = None) -> GcResult:
        del keep
        return GcResult(deleted=(), retained=())

    def build_candidate(self, **_kwargs: object) -> CandidatePaths:
        self.build_count += 1
        base = Path("/nonexistent/rel-r68")
        return CandidatePaths(
            release_id="rel-r68", release_dir=base, code_root=base / "code",
            venv_python=base / "venv" / "bin" / "python3", version_file=base / "VERSION",
            missing_pth_targets=(), schema_snapshot=None,
        )

    def cutover(self, candidate: CandidatePaths) -> SwapResult:
        del candidate
        return SwapResult(current="rel-r68", previous=None)

    @property
    def current_release(self) -> str | None:
        return None

    @property
    def previous_release(self) -> str | None:
        return None

    def current_schema_snapshot(self) -> dict[str, object] | None:
        return None


def _drift(candidate: CandidatePaths) -> ProbeOutcome:
    failure = {"check": "root_manifest", "plugin": None, "message": _DRIFT_ENVELOPE,
               "error_class": "RootManifestDrift"}
    return ProbeOutcome(ok=False, payload={
        "failing_step": "root_manifest", "error_class": "RootManifestDrift",
        "detail": _DRIFT_ENVELOPE, "failures": [failure], "release_id": candidate.release_id,
    })


def _green(candidate: CandidatePaths) -> ProbeOutcome:
    return ProbeOutcome(ok=True, payload={"ok": True, "release_id": candidate.release_id})


def _harness_restart(decide: Callable[[CandidatePaths], ProbeOutcome]) -> tuple[Any, int, list[str]]:
    manager = _ReleaseManager()
    probed: list[str] = []

    def probe(*, candidate: CandidatePaths, app_home: Path) -> ProbeOutcome:
        del app_home
        probed.append(candidate.release_id)
        return decide(candidate)

    def spawn(*_args: object, **_kwargs: object) -> int:
        raise OSError("smoke spawn seam: deliberately failing after the gates")

    def preflight(candidate: CandidatePaths, **_kwargs: object) -> PreflightVerdict:
        del candidate
        return PreflightVerdict(is_additive=True, breaking_changes=())

    orchestrator = SwapOrchestrator(
        router_client=cast("RouterClient", _Router()),
        action_factory=cast("Any", object()),
        session_factory=lambda: "sess-r68",
        solet_name="smoke",
        release_manager=cast("Any", manager),
        schema_preflight=cast("Any", preflight),
        preflight_probe=probe,
        set_color_active=lambda _active: None,
        spawn_fn=cast("Any", spawn),
        ready_timeout_seconds=2,
        ready_poll_interval_seconds=0.01,
    )
    result = orchestrator.restart(
        reason="r68-smoke", expected_etag="etag-r68", dry_run=False,
        app_home=Path("/nonexistent/profile"), self_instance_id="blue-id",
        self_color=COLOR_BLUE, set_active_targets=[],
    )
    return result, manager.build_count, probed


def _leg_preflight_order() -> None:
    result, builds, probed = _harness_restart(_drift)
    _recorder.check(
        builds == 0 and probed == [PRE_BUILD_PROBE_RELEASE_ID],
        f"[6] root drift refuses before any build work (builds={builds}, probes={probed})",
    )
    _recorder.check(
        result.status.value == "failed"
        and result.reason_code == RestartReasonCode.ROOT_MANIFEST_DRIFT
        and result.message.startswith("cutover preflight blocked on root_manifest drift:\n")
        and _DRIFT_ENVELOPE in result.message,
        f"[6] the early refusal carries the root_manifest_drift classification ({result.reason_code!r})",
    )
    late, late_builds, late_probed = _harness_restart(
        lambda candidate: _green(candidate)
        if candidate.release_id == PRE_BUILD_PROBE_RELEASE_ID
        else _drift(candidate)
    )
    _recorder.check(
        late_builds == 1 and late_probed == [PRE_BUILD_PROBE_RELEASE_ID, "rel-r68"],
        f"[6] a clean source tree still builds, and the candidate probe still runs ({late_probed})",
    )
    _recorder.check(
        late.message == result.message and late.reason_code == result.reason_code,
        "[6] the early and the post-build refusals say the same thing",
    )

    def other_failure_early(candidate: CandidatePaths) -> ProbeOutcome:
        if candidate.release_id != PRE_BUILD_PROBE_RELEASE_ID:
            return _green(candidate)
        return ProbeOutcome(ok=False, payload={
            "failing_step": "plugin_manifest", "error_class": "ImportError",
            "detail": "planted", "failures": [{"check": "plugin_manifest", "plugin": "p",
                                              "message": "m", "error_class": "ImportError"}],
            "release_id": candidate.release_id,
        })

    _, other_builds, _ = _harness_restart(other_failure_early)
    _recorder.check(
        other_builds == 1,
        "[6] only root-manifest drift refuses early; any other early finding is left to the "
        "post-build probe",
    )


def run_smoke() -> int:
    print("=== release_code_collection_batching_equivalence_smoke (r68) ===")
    _leg_equivalence()
    _leg_refusals()
    _leg_copy_failures()
    _leg_gitlink_and_gitless()
    _leg_spawn_counts()
    _leg_preflight_order()
    return _recorder.report("release_code_collection_batching_equivalence")


if __name__ == "__main__":
    sys.exit(run_smoke())
