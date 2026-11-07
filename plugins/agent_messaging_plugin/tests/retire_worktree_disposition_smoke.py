#!/usr/bin/env python3
"""D3 retirement ordering and worktree-disposition regression proof.

The fixture is a real-shaped in-memory StateManagementInterface.  It replaces
only the worktree and host seams, so no test command can touch a real lane
worktree or host process.  The custody proofs below use contained real-Git
repositories owned by a temporary root and an explicitly seeded register double;
no actual session, register or shared checkout is read.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Any, Final, cast
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402  # pyright: ignore[reportMissingImports] - resolved at runtime by the sys.path.insert calls above; plugins/<X>/tests is not a pyright extraPath
from _register_unit_double import (  # noqa: E402  # pyright: ignore[reportMissingImports] - resolved at runtime by the sys.path.insert calls above; plugins/<X>/tests is not a pyright extraPath
    FIXTURE_REPOSITORY_ID,
    FIXTURE_UNIT_ID,
    RegisterUnitDouble,
    fixture_assignment,
)
from ananta.interfaces.state_management_interface import StateManagementInterface  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from lane_worktrees_smoke import _custody_store, _intent_to_add, _run_git  # noqa: E402  # pyright: ignore[reportMissingImports] - resolved at runtime by the sys.path.insert calls above; plugins/<X>/tests is not a pyright extraPath
from session_lifecycle_transport_smoke import PacketLane, packet_lane  # noqa: E402  # pyright: ignore[reportMissingImports] - resolved at runtime by the sys.path.insert calls above; plugins/<X>/tests is not a pyright extraPath

import agent_messaging_plugin.session_lifecycle_control as session_lifecycle_control  # noqa: E402
import agent_messaging_plugin.session_lifecycle_verbs as lifecycle_verbs  # noqa: E402
from agent_messaging_plugin.lane_worktree_evidence import (  # noqa: E402
    RetirementCustodyContext,
    canonical_identity_sha256,
)
from agent_messaging_plugin.lane_worktree_preservation import PacketWriter  # noqa: E402
from agent_messaging_plugin.lane_worktrees import (  # noqa: E402
    LaneWorktree,
    LaneWorktreeDisposability,
    LaneWorktreeError,
    LaneWorktreeNotDisposableError,
    lane_worktree_disposability,
    lane_worktree_for,
    provision_lane_worktree,
    remove_lane_worktree,
)
from agent_messaging_plugin.register_unit_client import RegisteredRevision  # noqa: E402
from agent_messaging_plugin.schema import LIFECYCLE_LIVE, LIFECYCLE_RETIRED, LIFECYCLE_TERMINATED  # noqa: E402
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    StaleLifecycleStateError,
    insert_managed_session,
    read_managed_session,
    transition_lifecycle_state,
)
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    VerbError,
    retire_session,
    session_status,
)

_REGULAR = 0o100644
_EXECUTABLE = 0o100755
_DIRECTED_BY = "operator:none"


def _state() -> StateManagementInterface:
    return cast("StateManagementInterface", RealShapeState())


def _insert_live(state: StateManagementInterface, agent_instance_id: str) -> None:
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=agent_instance_id,
            lane_id="lane-d3",
            assignment=fixture_assignment(),
            work_class="production_mutation",
            budget_line="d3",
            host="operator",
            role_name="lane-d3",
        ),
    )
    transition_lifecycle_state(
        state,
        agent_instance_id=agent_instance_id,
        from_state="spawning",
        to_state=LIFECYCLE_LIVE,
        directed_by="operator:none",
    )


def _assert_refusal_precedes_termination() -> None:
    """Red mutation: move adjudication after terminate_session; then it calls the seam."""
    state = _state()
    _insert_live(state, "agi-d3-refusal")
    original_adjudicate = lifecycle_verbs._adjudicate_retire_lane_worktree  # noqa: SLF001
    original_terminate = lifecycle_verbs.terminate_session
    termination_calls: list[str] = []

    def refuse(*_: object, **__: object) -> None:
        raise VerbError("lane_worktree_not_disposable", "fixture names unlanded bytes")

    def must_not_terminate(*_: Any, **__: Any) -> dict[str, object]:
        termination_calls.append("called")
        return {"session_terminal_edges_fired": 0}

    lifecycle_verbs._adjudicate_retire_lane_worktree = refuse  # type: ignore[assignment]  # noqa: SLF001
    lifecycle_verbs.terminate_session = must_not_terminate  # type: ignore[assignment]
    try:
        try:
            retire_session(state, agent_instance_id="agi-d3-refusal", directed_by="operator:none")
        except VerbError as exc:
            assert exc.code == "lane_worktree_not_disposable", exc
        else:
            raise AssertionError("a non-disposable worktree must refuse retirement")
    finally:
        lifecycle_verbs._adjudicate_retire_lane_worktree = original_adjudicate  # type: ignore[assignment]  # noqa: SLF001
        lifecycle_verbs.terminate_session = original_terminate  # type: ignore[assignment]

    assert termination_calls == [], termination_calls
    assert read_managed_session(state, "agi-d3-refusal")["lifecycle_state"] == LIFECYCLE_LIVE


def _assert_teardown_error_records_terminal_outcome() -> None:
    """Red mutation: propagate remove_lane_worktree's error; retirement then wedges."""
    state = _state()
    _insert_live(state, "agi-d3-error")
    original_root = lifecycle_verbs._resolve_lane_repo_root  # noqa: SLF001
    original_for = lifecycle_verbs.lane_worktree_for
    original_disposability = lifecycle_verbs.lane_worktree_disposability
    original_remove = lifecycle_verbs.remove_lane_worktree
    worktree = LaneWorktree(
        repo_root=Path("/fixture-repo"),
        root=Path("/fixture-root"),
        path=Path("/fixture-root/lane-d3"),
        branch="lane/lane-d3/agi-d3-error",
    )

    def disposable(_: LaneWorktree, **__: object) -> LaneWorktreeDisposability:
        return LaneWorktreeDisposability(True, (), "fixture disposable", 0)

    def teardown_error(_: LaneWorktree, **__: object) -> None:
        raise LaneWorktreeError("fixture removal error")

    lifecycle_verbs._resolve_lane_repo_root = lambda _root="": worktree.repo_root  # type: ignore[assignment]  # noqa: SLF001
    lifecycle_verbs.lane_worktree_for = lambda *_args, **_kwargs: worktree  # type: ignore[assignment]
    lifecycle_verbs.lane_worktree_disposability = disposable  # type: ignore[assignment]
    lifecycle_verbs.remove_lane_worktree = teardown_error  # type: ignore[assignment]
    try:
        result = retire_session(state, agent_instance_id="agi-d3-error", directed_by="operator:none")
    finally:
        lifecycle_verbs._resolve_lane_repo_root = original_root  # type: ignore[assignment]  # noqa: SLF001
        lifecycle_verbs.lane_worktree_for = original_for  # type: ignore[assignment]
        lifecycle_verbs.lane_worktree_disposability = original_disposability  # type: ignore[assignment]
        lifecycle_verbs.remove_lane_worktree = original_remove  # type: ignore[assignment]

    assert (result["already_retired"], result["dependencies_fired"]) == (False, 0), result
    assert result["host_action"] == "none_available", result
    row = read_managed_session(state, "agi-d3-error")
    assert row["lifecycle_state"] == LIFECYCLE_RETIRED, row
    assert row["worktree_disposition"] == "retained_error", row
    # session_status is an intentional reader, so the ledger field is visible
    # to an operator instead of being an inert write-only status.
    assert session_status(state, "agi-d3-error")["worktree_disposition"] == "retained_error"
    replay = retire_session(state, agent_instance_id="agi-d3-error", directed_by="operator:none")
    assert (replay["already_retired"], replay["dependencies_fired"]) == (True, 0), replay
    assert replay["host_action"] == "not_attempted", replay


def _assert_unprovisioned_row_never_resolves_a_repo_root() -> None:
    """Red mutation: resolve the repo root before the no-worktree gate; APP_HOME then errors."""
    state = _state()
    _insert_live(state, "agi-d3-unprovisioned")
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-d3-unprovisioned"}},
        {"role_name": "", "local_name": ""},
    )
    prior_app_home = os.environ.pop("APP_HOME", None)
    try:
        result = retire_session(
            state,
            agent_instance_id="agi-d3-unprovisioned",
            directed_by="operator:none",
        )
    finally:
        if prior_app_home is not None:
            os.environ["APP_HOME"] = prior_app_home

    assert (result["already_retired"], result["dependencies_fired"]) == (False, 0), result
    assert result["host_action"] == "none_available", result
    row = read_managed_session(state, "agi-d3-unprovisioned")
    assert row["lifecycle_state"] == LIFECYCLE_RETIRED, row
    assert row["worktree_disposition"] == "not_provisioned", row
    assert session_status(state, "agi-d3-unprovisioned")["worktree_disposition"] == "not_provisioned"


def _git_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_") or key == "GIT_CONFIG_NOSYSTEM"}


def _git(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(cwd), *arguments),
        check=False,
        capture_output=True,
        text=True,
        env=_git_environment(),
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def _git_bytes(cwd: Path, spec: str) -> bytes:
    result = subprocess.run(("git", "-C", str(cwd), "show", spec), check=True, capture_output=True, env=_git_environment())
    return result.stdout


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@contextmanager
def _contained_git(root: Path) -> Generator[None]:
    """Point Git's user and system configuration away from the host for these proofs."""
    replacements = {
        "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(root / "xdg-config"),
        "XDG_CACHE_HOME": str(root / "xdg-cache"),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    saved = {key: os.environ.get(key) for key in replacements}
    os.environ.update(replacements)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class _CustodyLane:
    """One contained repository, its provisioned lane, a live session and its register double."""

    def __init__(self, root: Path, name: str) -> None:
        self.repo = root / name / "repo"
        self.repo.mkdir(parents=True)
        _git(self.repo, "init", "--quiet", "--initial-branch", "master")
        _git(self.repo, "config", "user.email", "fixture@example.invalid")
        _git(self.repo, "config", "user.name", "fixture")
        (self.repo / ".venv").mkdir()
        for path, text in (("tracked.txt", "base\n"), ("shared.md", "base\n"), ("SECURITY.md", "policy\n")):
            (self.repo / path).write_text(text, encoding="utf-8")
        _git(self.repo, "add", "tracked.txt", "shared.md", "SECURITY.md")
        _git(self.repo, "commit", "--quiet", "-m", "fixture base")
        self.agent_instance_id = f"agi-{name}"
        self.worktree = lane_worktree_for(self.repo, role_name=name, agent_instance_id=self.agent_instance_id)
        provision_lane_worktree(self.worktree)
        self.state = _state()
        insert_managed_session(
            self.state,
            ManagedSessionSpec(
                agent_instance_id=self.agent_instance_id,
                lane_id=name,
                assignment=fixture_assignment(),
                work_class="production_mutation",
                budget_line="custody",
                host="operator",
                role_name=name,
                lane_repo_root=str(self.repo.resolve()),
            ),
        )
        transition_lifecycle_state(
            self.state,
            agent_instance_id=self.agent_instance_id,
            from_state="spawning",
            to_state=LIFECYCLE_LIVE,
            directed_by=_DIRECTED_BY,
        )
        self.register = RegisterUnitDouble()
        self.register.unit_evidence = {
            "unit_id": FIXTURE_UNIT_ID,
            "repository_id": FIXTURE_REPOSITORY_ID,
            "owner": {"agent_instance_id": self.agent_instance_id},
            "landings": {"rows": [], "truncated": False, "next_cursor": None},
        }

    @property
    def branch_ref(self) -> str:
        return f"refs/heads/{self.worktree.branch}"

    @property
    def store(self) -> Path:
        common = _git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        return Path(common) / "project-solet-candidates"

    def commit_on_master(self, path: str, text: str) -> str:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        _git(self.repo, "add", path)
        _git(self.repo, "commit", "--quiet", "-m", f"master lands {path}")
        return _git(self.repo, "rev-parse", "HEAD")

    def commit_off_master(self, path: str, text: str) -> str:
        """A real commit that the integration head never contains."""
        _git(self.repo, "checkout", "--quiet", "-b", "side")
        commit = self.commit_on_master(path, text)
        _git(self.repo, "checkout", "--quiet", "master")
        return commit

    def write(self, path: str, data: bytes) -> None:
        target = self.worktree.path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def land(self, commit: str, *, is_deleted: bool = False) -> None:
        cast("list[dict[str, Any]]", self.evidence["landings"]["rows"]).append(
            {
                "merge_sha": commit,
                "candidate_commit_sha": None,
                "repository_id": FIXTURE_REPOSITORY_ID,
                "is_deleted": is_deleted,
            }
        )

    @property
    def evidence(self) -> dict[str, Any]:
        assert self.register.unit_evidence is not None
        return self.register.unit_evidence

    def freeze(
        self,
        finals: Mapping[str, bytes],
        *,
        state: str = "frozen",
        unit_id: str = FIXTURE_UNIT_ID,
        mode: int = _REGULAR,
        packet: bool = True,
    ) -> dict[str, Any]:
        """Seed a candidate identity and, like ``candidate-revision pack``, its packet files."""
        identity: dict[str, Any] = {
            "unit_id": unit_id,
            "repository_id": FIXTURE_REPOSITORY_ID,
            "base_commit_sha": "0" * 40,
            "patch_path": "candidate.patch",
            "patch_sha256": "0" * 64,
            "entries": [{"path": path, "base": "absent", "final": {"mode": mode, "sha256": _sha256(data)}} for path, data in sorted(finals.items())],
        }
        digest = canonical_identity_sha256(identity)
        if packet:
            for path, data in finals.items():
                target = self.store / digest / "packet" / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                target.chmod(0o755 if mode == _EXECUTABLE else 0o644)
        row = {"revision_id": f"crv_{digest[:12]}", "state": state, "identity": identity, "identity_sha256": digest}
        self.register.candidate_revisions.append(row)
        return row

    def register_document(self, path: str, data: bytes, *, stored: bool = True) -> str:
        revision_id = f"drv_{_sha256(path.encode())[:12]}"
        self.register.unit_references.append(
            {
                "kind": "document",
                "owner_kind": "unit",
                "owner_id": FIXTURE_UNIT_ID,
                "is_deleted": False,
                "document_path": path,
                "content_sha256": _sha256(data),
                "version_id": revision_id,
            }
        )
        if stored:
            self.register.revisions[revision_id] = RegisteredRevision(revision_id, _sha256(data), len(data), data.decode())
        return revision_id

    def custody(self) -> RetirementCustodyContext:
        return RetirementCustodyContext.for_session(read_managed_session(self.state, self.agent_instance_id), self.register)

    def verdict(self) -> LaneWorktreeDisposability:
        return lane_worktree_disposability(self.worktree, custody=self.custody())

    def retire(self) -> dict[str, Any]:
        return retire_session(
            self.state,
            agent_instance_id=self.agent_instance_id,
            directed_by=_DIRECTED_BY,
            register=self.register,
        )

    def disposition(self) -> str:
        return str(read_managed_session(self.state, self.agent_instance_id)["worktree_disposition"])


_GROUP_FILES = {
    "shared.md": b"base\nlane entry\n",
    "pkg/new_module.py": b"VALUE = 1\n",
    "pkg/__pycache__/new_module.cpython-313.pyc": b"\x00cache",
    "tracked.txt": b"lane member bytes\n",
    "workbench/report.md": b"# exact registered report\n",
}


def _assert_group_custody_retires_and_recovers(root: Path) -> int:
    """Acceptance: every safe class passes together, and each byte stays recoverable.

    Red mutation: compare tracked paths only with the current master and the
    landed ``shared.md`` refuses, so the simulated lifecycle keeps the worktree.
    """
    lane = _CustodyLane(root, "lane-group")
    landed = lane.commit_on_master("shared.md", "base\nlane entry\n")
    lane.commit_on_master("shared.md", "base\nlane entry\nanother lane appended\n")
    lane.commit_on_master("pkg/new_module.py", "VALUE = 1\n")
    lane.commit_on_master("tracked.txt", "composed batch members\n")
    lane.land(landed)
    for path, data in _GROUP_FILES.items():
        lane.write(path, data)
    (lane.worktree.path / "profile" / "data" / "logs").mkdir(parents=True)
    candidate = lane.freeze({"tracked.txt": _GROUP_FILES["tracked.txt"]})
    report = lane.register_document("workbench/report.md", _GROUP_FILES["workbench/report.md"])

    verdict = lane.verdict()
    assert verdict.disposable, verdict
    assert dict(verdict.custody) == {
        "build_output": (".venv", "pkg/__pycache__/new_module.cpython-313.pyc"),
        "empty_scaffold": ("profile/",),
        "current_master": ("pkg/new_module.py",),
        "landed_history": ("shared.md",),
        "frozen_candidate": ("tracked.txt",),
        "registered_document": ("workbench/report.md",),
    }, verdict.custody
    tip = _git(lane.repo, "rev-parse", lane.branch_ref)

    result = lane.retire()
    assert result["already_retired"] is False, result
    assert lane.disposition() == "removed", lane.disposition()
    assert not lane.worktree.path.exists(), "a fully recoverable lane is removed without manual cleanup"
    assert _git(lane.repo, "rev-parse", lane.branch_ref) == tip, "removal never moves the lane branch"
    packet = lane.store / str(candidate["identity_sha256"]) / "packet"
    recovered = {
        "shared.md": _git_bytes(lane.repo, f"{landed}:shared.md"),
        "pkg/new_module.py": _git_bytes(lane.repo, "master:pkg/new_module.py"),
        "tracked.txt": (packet / "tracked.txt").read_bytes(),
        "workbench/report.md": lane.register.read_revision(report).text.encode("utf-8"),
    }
    assert recovered == {path: _GROUP_FILES[path] for path in recovered}, recovered
    again = lane.retire()
    assert again["already_retired"] is True and again["host_action"] == "not_attempted", again
    return 9


def _landing_master_moved_past(lane: _CustodyLane) -> str:
    """Land the lane's shared.md bytes, then let another lane append, as in production."""
    landed = lane.commit_on_master("shared.md", "base\nlane entry\n")
    lane.commit_on_master("shared.md", "base\nlane entry\nanother lane appended\n")
    return landed


def _landed_shared_bytes(lane: _CustodyLane) -> None:
    lane.land(_landing_master_moved_past(lane))
    lane.write("shared.md", b"base\nlane entry\n")


def _control_bytes_differ_from_landing(lane: _CustodyLane) -> None:
    lane.land(_landing_master_moved_past(lane))
    lane.write("shared.md", b"base\nlane entry edited after landing\n")


def _control_landing_withdrawn(lane: _CustodyLane) -> None:
    lane.land(_landing_master_moved_past(lane), is_deleted=True)
    lane.write("shared.md", b"base\nlane entry\n")


def _control_landing_off_master(lane: _CustodyLane) -> None:
    lane.land(lane.commit_off_master("shared.md", "base\nside entry\n"))
    lane.write("shared.md", b"base\nside entry\n")


def _control_post_delivery_edit(lane: _CustodyLane) -> None:
    """A recorded landing licenses only the bytes it holds, never the whole worktree."""
    _landed_shared_bytes(lane)
    lane.write("SECURITY.md", b"policy\nedited after delivery\n")


def _control_superseded_candidate(lane: _CustodyLane) -> None:
    lane.write("tracked.txt", b"lane member bytes\n")
    lane.freeze({"tracked.txt": b"lane member bytes\n"}, state="superseded")


def _control_foreign_candidate(lane: _CustodyLane) -> None:
    lane.write("tracked.txt", b"lane member bytes\n")
    lane.freeze({"tracked.txt": b"lane member bytes\n"}, unit_id="unt_another-unit")


def _control_candidate_without_packet(lane: _CustodyLane) -> None:
    lane.write("tracked.txt", b"lane member bytes\n")
    lane.freeze({"tracked.txt": b"lane member bytes\n"}, packet=False)


def _control_candidate_mode_differs(lane: _CustodyLane) -> None:
    lane.write("tracked.txt", b"lane member bytes\n")
    lane.freeze({"tracked.txt": b"lane member bytes\n"}, mode=_EXECUTABLE)


def _control_document_hash_only(lane: _CustodyLane) -> None:
    """A junction naming the digest without readable bytes is a fact, not custody."""
    lane.write("workbench/report.md", b"# unique report\n")
    lane.register_document("workbench/report.md", b"# unique report\n", stored=False)


def _control_evidence_names_another_owner(lane: _CustodyLane) -> None:
    _landed_shared_bytes(lane)
    lane.evidence["owner"] = {"agent_instance_id": "agi-someone-else"}


def _control_checkout_resolves_elsewhere(lane: _CustodyLane) -> None:
    _landed_shared_bytes(lane)
    lane.register.repository_id = "rep_another-repository"


def _control_register_unavailable(lane: _CustodyLane) -> None:
    _landed_shared_bytes(lane)
    lane.register.custody_failures["evidence"] = "unit_register_unavailable"


def _control_candidate_listing_truncated(lane: _CustodyLane) -> None:
    lane.write("tracked.txt", b"lane member bytes\n")
    lane.freeze({"tracked.txt": b"lane member bytes\n"})
    lane.register.custody_failures["candidates"] = "unit_evidence_truncated"


_CONTROLS: tuple[tuple[str, Callable[[_CustodyLane], None], str, str], ...] = (
    ("landing-bytes", _control_bytes_differ_from_landing, "shared.md", ""),
    ("landing-withdrawn", _control_landing_withdrawn, "shared.md", ""),
    ("landing-unreachable", _control_landing_off_master, "shared.md", "retire_custody_landing_unreachable"),
    ("post-delivery", _control_post_delivery_edit, "SECURITY.md", ""),
    ("candidate-stale", _control_superseded_candidate, "tracked.txt", "retire_custody_candidate_not_frozen"),
    ("candidate-foreign", _control_foreign_candidate, "tracked.txt", "retire_custody_candidate_foreign"),
    ("candidate-packet", _control_candidate_without_packet, "tracked.txt", ""),
    ("candidate-mode", _control_candidate_mode_differs, "tracked.txt", ""),
    ("document-facts", _control_document_hash_only, "workbench/report.md", "retire_custody_document_unverified"),
    ("wrong-owner", _control_evidence_names_another_owner, "shared.md", "retire_custody_identity_unverified"),
    ("wrong-repository", _control_checkout_resolves_elsewhere, "shared.md", "retire_custody_identity_unverified"),
    ("register-unavailable", _control_register_unavailable, "shared.md", "retire_custody_identity_unverified"),
    ("candidates-truncated", _control_candidate_listing_truncated, "tracked.txt", "retire_custody_candidates_unavailable"),
)


def _assert_custody_controls_protect(root: Path) -> int:
    """Each positive class has a control that keeps its path and prevents removal.

    Red mutations: trust a landing without per-path bytes (post-delivery), drop the
    Unit/repository binding (candidate-foreign) or accept a document digest without
    its verified bytes (document-facts); each control then removes unique bytes.
    """
    for name, build, protected, advisory in _CONTROLS:
        lane = _CustodyLane(root, f"lane-{name}")
        build(lane)
        before = (lane.worktree.path / protected).read_bytes()
        verdict = lane.verdict()
        assert not verdict.disposable and protected in verdict.offending_paths, (name, verdict)
        assert advisory in " ".join(verdict.advisories), (name, verdict.advisories)
        try:
            remove_lane_worktree(lane.worktree, custody=lane.custody())
        except LaneWorktreeNotDisposableError as exc:
            assert protected in exc.verdict.offending_paths, (name, exc.verdict)
        else:
            raise AssertionError(f"{name}: removal must refuse unique bytes")
        assert (lane.worktree.path / protected).read_bytes() == before, name
    return 4 * len(_CONTROLS)


def _assert_unavailable_register_keeps_local_passes(root: Path) -> int:
    """No register data never turns a locally provable disposal into a refusal."""
    lane = _CustodyLane(root, "lane-local-only")
    lane.commit_on_master("pkg/new_module.py", "VALUE = 1\n")
    lane.write("pkg/new_module.py", b"VALUE = 1\n")
    (lane.worktree.path / "profile" / "data" / "logs").mkdir(parents=True)
    lane.register.custody_failures.update(evidence="unit_register_unavailable", candidates="unit_register_unavailable")
    verdict = lane.verdict()
    assert verdict.disposable and verdict.advisories == (), verdict
    assert lane.register.reads == [], "local custody sufficed, so the register is never read"
    unbound = RetirementCustodyContext.for_session({"agent_instance_id": "agi-historical"}, lane.register)
    claims = unbound.read(lane.repo)
    assert claims.landing_commits == () and "retire_custody_unit_unbound" in claims.advisories[0], claims
    assert lane.register.reads == [], "an unbound historical session reads no Unit at all"
    return 4


def _retire_with_change_between_phases(lane: _CustodyLane, change: Callable[[], None]) -> dict[str, Any]:
    """Run the real lifecycle, applying ``change`` after host termination and before removal."""
    original = lifecycle_verbs._retire_lane_worktree  # noqa: SLF001

    def changed_then_removed(row: Mapping[str, object], custody: RetirementCustodyContext | None = None) -> str:
        change()
        return original(row, custody=custody)

    lifecycle_verbs._retire_lane_worktree = changed_then_removed  # type: ignore[assignment]  # noqa: SLF001
    try:
        return lane.retire()
    finally:
        lifecycle_verbs._retire_lane_worktree = original  # type: ignore[assignment]  # noqa: SLF001


def _assert_second_phase_revalidates(root: Path) -> int:
    """Removal re-reads files and custody; adjudication's earlier verdict licenses nothing.

    Red mutation: skip the predicate inside ``remove_lane_worktree``; the leaf
    changed after adjudication is then destroyed.
    """
    checks = 0
    member = b"lane member bytes\n"
    for name in ("changed-leaf", "withdrawn-candidate"):
        lane = _CustodyLane(root, f"lane-{name}")
        lane.write("tracked.txt", member)
        candidate = lane.freeze({"tracked.txt": member})
        tip = _git(lane.repo, "rev-parse", lane.branch_ref)
        assert lane.verdict().disposable, name

        def change(lane: _CustodyLane = lane, candidate: dict[str, Any] = candidate, name: str = name) -> None:
            if name == "changed-leaf":
                lane.write("tracked.txt", b"edited between adjudication and removal\n")
            else:
                candidate["state"] = "superseded"

        result = _retire_with_change_between_phases(lane, change)
        assert result["already_retired"] is False, (name, result)
        row = read_managed_session(lane.state, lane.agent_instance_id)
        assert row["lifecycle_state"] == LIFECYCLE_RETIRED, (name, row)
        assert lane.disposition() == "retained_not_disposable", (name, lane.disposition())
        assert (lane.worktree.path / "tracked.txt").exists(), name
        assert _git(lane.repo, "rev-parse", lane.branch_ref) == tip, name
        checks += 5
    return checks


def _assert_unique_bytes_refuse_before_termination(root: Path) -> int:
    lane = _CustodyLane(root, "lane-unique")
    lane.write("workbench/notes.md", b"only here\n")
    try:
        lane.retire()
    except VerbError as exc:
        assert exc.code == "lane_worktree_not_disposable" and "workbench/notes.md" in exc.message, exc
    else:
        raise AssertionError("unique bytes must refuse retirement before the host is terminated")
    row = read_managed_session(lane.state, lane.agent_instance_id)
    assert row["lifecycle_state"] == LIFECYCLE_LIVE, row
    return 2


def _packet_retire(
    lane: PacketLane,
    *,
    adjudicated: Callable[[], object] | None = None,
    terminated: Callable[[], object] | None = None,
    **request: Any,
) -> tuple[dict[str, Any], int]:
    """Retire through the public verb, changing the lane after adjudication or after termination.

    Returns the response and how many times the host termination seam ran.
    """
    original_adjudicate = lifecycle_verbs._adjudicate_retire_lane_worktree  # noqa: SLF001
    original_retire = lifecycle_verbs._retire_lane_worktree  # noqa: SLF001

    def adjudicate(row: Mapping[str, object], custody: RetirementCustodyContext | None = None) -> None:
        original_adjudicate(row, custody=custody)
        if adjudicated is not None:
            adjudicated()

    def retire(row: Mapping[str, object], custody: RetirementCustodyContext | None = None) -> str:
        if terminated is not None:
            terminated()
        return original_retire(row, custody=custody)

    with (
        patch.object(lifecycle_verbs, "_adjudicate_retire_lane_worktree", adjudicate),
        patch.object(lifecycle_verbs, "_retire_lane_worktree", retire),
        patch.object(session_lifecycle_control, "_terminate_host", wraps=session_lifecycle_control._terminate_host) as host,  # noqa: SLF001
    ):
        return lane.call("retire_session", **request), host.call_count


def _pinned(packet: Mapping[str, Any]) -> dict[str, Any]:
    return {"packet_id": str(packet["packet_id"]), "expected_fingerprint": str(packet["fingerprint"])}


def _corrupt_payload(lane: PacketLane, packet: Mapping[str, Any]) -> dict[str, Any]:
    payload = _custody_store(lane.repo, lane.identity["agent_instance_id"], lane.environment) / packet["packet_id"] / "payload" / _sha256(b"\x00\xff\x10binary\x00")
    payload.chmod(0o644)
    payload.write_bytes(b"\x00\xff\x10altered\x00")
    return _pinned(packet)


def _late_leaf(lane: PacketLane, packet: Mapping[str, Any]) -> dict[str, Any]:
    (lane.lane.path / "notes" / "late.txt").write_bytes(b"written after consent\n")
    return _pinned(packet)


def _stale_consent(lane: PacketLane, packet: Mapping[str, Any]) -> dict[str, Any]:
    _late_leaf(lane, packet)
    return {"packet_id": str(packet["packet_id"]), "expected_fingerprint": lane.fingerprint()}


def _foreign_packet(lane: PacketLane, packet: Mapping[str, Any]) -> dict[str, Any]:
    """Another session's verified packet, copied under this session's own store name."""
    other = packet_lane(lane.repo.parent / "foreign", "retire-foreign")
    theirs = other.preserve()["data"]
    source = _custody_store(other.repo, other.identity["agent_instance_id"], other.environment) / theirs["packet_id"]
    shutil.copytree(source, _custody_store(lane.repo, lane.identity["agent_instance_id"], lane.environment) / theirs["packet_id"])
    del packet
    return _pinned(theirs)


_INITIAL_REFUSALS: tuple[tuple[str, Callable[[PacketLane, Mapping[str, Any]], dict[str, Any]]], ...] = (
    ("custody_packet_corrupt", _corrupt_payload),
    ("custody_lane_changed", _late_leaf),
    ("custody_fingerprint_stale", _stale_consent),
    ("custody_packet_foreign", _foreign_packet),
    ("custody_packet_missing", lambda _lane, packet: {"packet_id": "0" * 64, "expected_fingerprint": str(packet["fingerprint"])}),
)


def _assert_packet_refusals_keep_host(root: Path) -> int:
    """A packet that fails initial integrity adjudication terminates no host and keeps every byte.

    Red mutation M1: skip the packet read in the verifier and the corrupt
    packet retires the lane.
    """
    checks = 0
    for code, damage in _INITIAL_REFUSALS:
        lane = packet_lane(root / code, "retire-refused")
        request = damage(lane, lane.preserve()["data"])
        response, hosts = _packet_retire(lane, **request)
        message = str(cast("dict[str, Any]", response.get("error") or {}).get("message"))
        assert (response.get("error") or {}).get("code") == "lane_worktree_not_disposable" and code in message, (code, response)
        assert (hosts, lane.lifecycle(), lane.lane.path.exists()) == (0, LIFECYCLE_LIVE, True), f"{code}: integrity failure must keep the host and the lane"
        checks += 2
    return checks


def _staged_only(lane: PacketLane) -> None:
    """A staged blob whose worktree equals HEAD: local custody cannot see it, only the index holds it."""
    target = lane.lane.path / "tracked.txt"
    original = target.read_bytes()
    target.write_bytes(b"unique staged bytes\n")
    _run_git(lane.lane.path, "add", "tracked.txt", environment=lane.environment)
    target.write_bytes(original)


def _nested_ignored(lane: PacketLane) -> None:
    (lane.lane.path / "notes" / "deep" / "late.log").write_bytes(b"ignored, unique, after consent\n")


def _retype(lane: PacketLane) -> None:
    target = lane.lane.path / "notes" / "crlf.txt"
    target.unlink()
    target.symlink_to("tail.txt")


_SECOND_PHASE: tuple[tuple[str, str, Callable[[PacketLane], object]], ...] = (
    ("adjudicated", "staged-only", _staged_only),
    ("adjudicated", "nested-ignored", _nested_ignored),
    ("terminated", "staged-only", _staged_only),
    ("terminated", "bytes", lambda lane: (lane.lane.path / "notes" / "tail.txt").write_bytes(b"changed after termination")),
    ("terminated", "mode", lambda lane: (lane.lane.path / "notes" / "tool").chmod(0o644)),
    ("terminated", "type", _retype),
)


def _assert_packet_second_phase(root: Path) -> int:
    """A lane changed after consent is retained honestly, even where local custody alone would pass.

    The clean-base lanes hold only a staged-only edit after consent, which
    local custody cannot see.  Red mutation M2: skip the current-snapshot
    check and the staged-only edit is destroyed; M4: skip the predicate in
    ``remove_lane_worktree`` and every post-termination change is destroyed.
    """
    checks = 0
    for phase, name, change in _SECOND_PHASE:
        lane = packet_lane(root / f"{phase}-{name}", "retire-phase", unique_state=name != "staged-only")
        packet = lane.preserve()["data"]
        tip = lane.tip()
        hook = partial(change, lane)
        response, hosts = _packet_retire(
            lane,
            adjudicated=hook if phase == "adjudicated" else None,
            terminated=hook if phase == "terminated" else None,
            **_pinned(packet),
        )
        assert response.get("action_status") == "completed" and hosts == 1, (phase, name, response)
        assert response["data"]["custody_packet"]["worktree_disposition"] == "retained_not_disposable", (phase, name, response)
        assert (lane.lane.path.exists(), lane.lifecycle(), lane.disposition(), lane.tip()) == (True, LIFECYCLE_RETIRED, "retained_not_disposable", tip), (phase, name)
        replay, replay_hosts = _packet_retire(lane, **_pinned(packet))
        assert (replay["data"]["already_retired"], replay_hosts, lane.lane.path.exists()) == (True, 0, True), "a retired retry touches no host and removes nothing"
        checks += 4
    return checks


def _assert_packet_partial_retry(root: Path) -> int:
    """A lost terminated->retired write leaves a re-drivable row; the retry needs its own proof."""
    lane = packet_lane(root / "cas", "retire-cas")
    pinned = _pinned(lane.preserve()["data"])
    original = session_lifecycle_control.transition_lifecycle_state

    def lose_retire_race(*args: Any, **kwargs: Any) -> None:
        if kwargs.get("to_state") == LIFECYCLE_RETIRED:
            raise StaleLifecycleStateError(lane.identity["agent_instance_id"], kwargs["from_state"], LIFECYCLE_RETIRED)
        original(*args, **kwargs)

    unpinned, hosts = _packet_retire(lane)
    assert ((unpinned.get("error") or {}).get("code"), hosts) == ("lane_worktree_not_disposable", 0), "omission selects no packet for that call"
    with patch.object(session_lifecycle_control, "transition_lifecycle_state", lose_retire_race):
        lost, hosts = _packet_retire(lane, **pinned)
    assert ((lost.get("error") or {}).get("code"), hosts, lane.lifecycle(), lane.lane.path.exists()) == ("stale_lifecycle_state", 1, "terminated", False), lost
    retried, hosts = _packet_retire(lane, **pinned)
    outcome = (retried["data"]["already_retired"], retried["data"]["host_action"], hosts, retried["data"]["custody_packet"]["worktree_disposition"])
    assert outcome == (False, "not_attempted", 0, "removed"), retried
    again, hosts = _packet_retire(lane)
    assert (again["data"]["already_retired"], hosts) == (True, 0), again
    assert lane.call("read_lane_evidence", packet_id=pinned["packet_id"])["data"]["custody_verified"] is True, "retirement withdraws no packet"
    return 5


def _assert_packet_capture_refusals(root: Path) -> int:
    """One-call capture that cannot publish, or a lane it cannot hold, retires nothing.

    Red mutation M5: let a superseded disposition without a packet fall back
    to local custody and the staged-only bytes are destroyed.
    """
    lane = packet_lane(root / "publication", "retire-publication")
    capture: dict[str, Any] = {"preserve_evidence": True, "expected_fingerprint": lane.fingerprint(), "disposition": "preserved", "rationale": "one-call"}
    original = lifecycle_verbs.capture_lane_custody

    def failing_rename(*_: object, **__: object) -> None:
        raise OSError(errno.EIO, "injected rename failure")

    with patch.object(lifecycle_verbs, "capture_lane_custody", lambda context, **kw: original(context, writer=PacketWriter(rename=failing_rename), **kw)):
        failed, hosts = _packet_retire(lane, **capture)
    assert ((failed.get("error") or {}).get("code"), hosts, lane.lane.path.exists()) == ("custody_publication_failed", 0, True), failed
    unsupported = packet_lane(root / "unsupported", "retire-unsupported")
    _intent_to_add(unsupported.lane, unsupported.environment)
    response, hosts = _packet_retire(unsupported, **(capture | {"expected_fingerprint": unsupported.fingerprint()}))
    assert ((response.get("error") or {}).get("code"), hosts, unsupported.lane.path.exists()) == ("custody_incomplete", 0, True), response
    blind = packet_lane(root / "superseded", "retire-superseded", unique_state=False)
    _staged_only(blind)
    response, hosts = _packet_retire(blind, disposition="superseded", rationale="superseded without a packet")
    assert ((response.get("error") or {}).get("code"), hosts, blind.lane.path.exists()) == ("custody_selection_invalid", 0, True), response
    return 3


_SWEEP_LANES: Final[tuple[tuple[str, str], ...]] = (
    ("lane-terminal", LIFECYCLE_TERMINATED),
    ("lane-active", LIFECYCLE_LIVE),
    ("lane-retiring", LIFECYCLE_LIVE),
)


def _sweep_repo(root: Path) -> Path:
    """One contained repository with the shared venv the real provisioning needs."""
    repo = root / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "--quiet", "--initial-branch", "master")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "user.name", "fixture")
    (repo / ".venv").mkdir()
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "--quiet", "-m", "fixture base")
    return repo


def _sweep_lane(state: StateManagementInterface, repo: Path, name: str, lifecycle: str) -> LaneWorktree:
    """Provision one real lane worktree and drive its real ledger row to ``lifecycle``."""
    agent_instance_id = f"agi-{name}"
    worktree = lane_worktree_for(repo, role_name=name, agent_instance_id=agent_instance_id)
    provision_lane_worktree(worktree)
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=agent_instance_id,
            lane_id=name,
            assignment=fixture_assignment(),
            work_class="production_mutation",
            budget_line="sweep",
            host="operator",
            role_name=name,
            lane_repo_root=str(repo.resolve()),
        ),
    )
    transition_lifecycle_state(
        state,
        agent_instance_id=agent_instance_id,
        from_state="spawning",
        to_state=LIFECYCLE_LIVE,
        directed_by=_DIRECTED_BY,
    )
    if lifecycle == LIFECYCLE_TERMINATED:
        transition_lifecycle_state(
            state,
            agent_instance_id=agent_instance_id,
            from_state=LIFECYCLE_LIVE,
            to_state=LIFECYCLE_TERMINATED,
            directed_by=_DIRECTED_BY,
        )
    return worktree


def _assert_terminal_sibling_swept_at_retirement(root: Path) -> int:
    """Acceptance: one retirement reclaims the terminal lane trees earlier retirements left.

    Every lane here is a real worktree provisioned by the real
    ``provision_lane_worktree`` in a real Git repository, driven through the
    real ledger writes and retired by the real ``retire_session``; the
    measurement is the sibling directory's absence, not a log line.

    Red mutation: delete the ``_sweep_terminal_lane_worktrees`` call from
    ``retire_session`` and ``lane-terminal``'s directory survives, because
    nothing else reclaims it until the next unrelated lane spawn.
    """
    repo = _sweep_repo(root)
    state = _state()
    lanes = {name: _sweep_lane(state, repo, name, lifecycle) for name, lifecycle in _SWEEP_LANES}
    assert all(worktree.path.is_dir() for worktree in lanes.values()), lanes

    result = retire_session(state, agent_instance_id="agi-lane-retiring", directed_by=_DIRECTED_BY)

    row = read_managed_session(state, "agi-lane-retiring")
    # The retiring lane's own disposition is the value it had before the sweep
    # existed: this change adds tidying, it does not touch the result contract.
    assert (result["already_retired"], row["lifecycle_state"], row["worktree_disposition"]) == (False, LIFECYCLE_RETIRED, "removed"), (result, row)
    assert not lanes["lane-terminal"].path.exists(), "a retirement must reclaim the terminal sibling lane worktree"
    branches = [_git(repo, "rev-parse", f"refs/heads/{lanes[name].branch}") for name in ("lane-terminal", "lane-active")]
    assert (lanes["lane-active"].path.is_dir(), lanes["lane-retiring"].path.exists(), len(branches)) == (True, False, 2), "a live row's lane worktree is never swept, the retiring tree is gone, and no lane branch is deleted"

    replay = retire_session(state, agent_instance_id="agi-lane-retiring", directed_by=_DIRECTED_BY)
    assert (replay["already_retired"], replay["host_action"], lanes["lane-active"].path.is_dir()) == (True, "not_attempted", True), replay
    return 10


def main() -> int:
    _assert_refusal_precedes_termination()
    _assert_teardown_error_records_terminal_outcome()
    _assert_unprovisioned_row_never_resolves_a_repo_root()
    checks = 13
    with tempfile.TemporaryDirectory(prefix="retire-custody-") as temporary:
        root = Path(temporary).resolve()
        with _contained_git(root):
            checks += _assert_group_custody_retires_and_recovers(root / "group")
            checks += _assert_custody_controls_protect(root / "controls")
            checks += _assert_unavailable_register_keeps_local_passes(root / "local")
            checks += _assert_second_phase_revalidates(root / "phases")
            checks += _assert_unique_bytes_refuse_before_termination(root / "unique")
            checks += _assert_terminal_sibling_swept_at_retirement(root / "sweep-siblings")
        checks += _assert_packet_refusals_keep_host(root / "packet-refusals")
        checks += _assert_packet_second_phase(root / "packet-phases")
        checks += _assert_packet_partial_retry(root / "packet-retry")
        checks += _assert_packet_capture_refusals(root / "packet-capture")
    print(f"retire_worktree_disposition_smoke OK: {checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
