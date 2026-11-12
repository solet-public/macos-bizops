"""Immutable custody packets for the bytes only a lane worktree holds.

A packet records one managed lane's exact Git state outside its removable
worktree: the HEAD commit and tree, every index entry that differs from that
tree, and every worktree entry that differs from the index, which includes each
untracked or ignored leaf and each empty directory.  Bytes are stored raw and
addressed by SHA-256 beside a canonical manifest whose own SHA-256 names the
packet, under ``<git common dir>/lane-evidence/v1/<agent instance>/<digest>/``.
Paths equal to HEAD are held by the lane branch, which removal preserves.

Nothing here removes, prunes or rewrites a packet or its source.  A capture
publishes only when its caller's expected fingerprint and two full inventories
agree and the staged packet verifies, including a rebuild of its index in a
disposable index file; anything it cannot hold exactly is named and the source
is left as it was.  A correction is a new packet naming its verified
predecessor, which stays readable.  Declared build output is named, not stored,
by the same rule retirement applies.  Nothing here treats a packet as authority
to delete anything; retirement does not consult packets yet.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
from functools import partial
from pathlib import Path
from typing import Literal

from . import custody_store as store
from .lane_worktrees import LaneWorktree, LaneWorktreeError, _git_environment, _is_ignorable_untracked, lane_worktree_for
from .register_unit_client import RegisterUnitError, UnitAssignment

PACKET_SCHEMA = "lane-custody-packet/v1"
STORE_PARTS = ("lane-evidence", "v1")
DISPOSITION_PRESERVED = "preserved"
DISPOSITION_SUPERSEDED = "superseded"
KIND_FILE = "file"
KIND_SYMLINK = "symlink"
KIND_DIRECTORY = "directory"
KIND_ABSENT = "absent"

_DISPOSITIONS = frozenset({DISPOSITION_PRESERVED, DISPOSITION_SUPERSEDED})
_LEAF_KINDS = {store.REGULAR_MODE: KIND_FILE, store.EXECUTABLE_MODE: KIND_FILE, store.SYMLINK_MODE: KIND_SYMLINK}
_OID_LENGTHS = {"sha1": 40, "sha256": 64}
_INDEX_FLAG_CODES = {
    "assume_unchanged": "unsupported_index_assume_unchanged",
    "intent_to_add": "unsupported_index_intent_to_add",
    "skip_worktree": "unsupported_index_skip_worktree",
}
_MANIFEST_SCALARS = ("schema", "object_format", "head_commit", "head_tree", "fingerprint")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_HEX = re.compile(r"[0-9a-f]+")
_SUMMARY_PATH_LIMIT = 20


class LaneCustodyError(store.CustodyStoreError):
    """A named reason custody could not be captured, verified or read.

    Public functions turn it into a refusing ``CustodyResult``; ``incomplete``
    carries the paths an incomplete capture left in place.
    """


@dataclass(frozen=True, slots=True)
class LaneCustodyContext:
    """A managed lane's trusted identity; every path is derived from it.

    The caller binds this from the session's verified assignment, never from a
    request, so no source or destination path is ever supplied: the lane is
    ``lane_worktree_for`` of these names and the store is the repository's own
    common directory.  ``actor`` names who asked and is provenance only.
    """

    repo_root: Path
    lane_name: str
    agent_instance_id: str
    assignment: UnitAssignment
    actor: str = ""


@dataclass(frozen=True, slots=True)
class IndexRecord:
    """One index entry as Git lists it, kept apart from the worktree.

    ``mode`` None is a HEAD path the index no longer holds.  A packet holds only
    stage 0 with no flags; any other stage or flag is named as unsupported at
    capture, never flattened.  ``size`` and ``sha256`` name the stored blob.
    """

    path: str
    mode: int | None
    oid: str | None
    stage: int = 0
    flags: tuple[str, ...] = ()
    size: int | None = None
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class WorktreeRecord:
    """One worktree path exactly: a leaf, an empty directory, or absence.

    A ``file`` or ``symlink`` carries its Git mode, object id, size and the
    SHA-256 of its raw bytes or uninterpreted link text; the other kinds carry
    nothing.
    """

    path: str
    kind: str
    mode: int | None = None
    oid: str | None = None
    size: int | None = None
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class CustodyBinding:
    """Whose state a packet holds: repository, lane, session and registered Unit.

    A packet verifies only for a context that derives exactly this binding, so
    another session, Unit, assignment or repository cannot cite it.
    """

    repository_id: str
    common_dir: str
    lane_name: str
    agent_instance_id: str
    branch: str
    unit_id: str
    unit_key: str
    assignment_sha256: str
    revision_id: str


@dataclass(frozen=True, slots=True)
class CustodyProvenance:
    """Who captured a packet, how its bytes are disposed, why, and what it supersedes.

    ``predecessor`` is the digest of an earlier verified packet of the same
    binding; None means the packet begins no lineage.
    """

    actor: str
    disposition: str
    rationale: str
    predecessor: str | None = None


@dataclass(frozen=True, slots=True)
class CustodyManifest:
    """Everything a packet asserts; its canonical bytes are what its digest names.

    ``index`` lists the entries that differ from ``head_tree`` and ``worktree``
    the entries that differ from that index, so HEAD, the index delta and the
    worktree delta together refingerprint to ``fingerprint``.  ``build_output``
    names declared reproducible output that was not stored.
    """

    binding: CustodyBinding
    provenance: CustodyProvenance
    object_format: str
    head_commit: str
    head_tree: str
    fingerprint: str
    index: tuple[IndexRecord, ...]
    worktree: tuple[WorktreeRecord, ...]
    build_output: tuple[str, ...]
    schema: str = PACKET_SCHEMA

    def encode(self) -> bytes:
        return store.canonical_bytes(asdict(self))

    def payloads(self) -> dict[str, tuple[str, int]]:
        """Each stored payload's SHA-256 with the one object id and size it must have."""
        found: dict[str, tuple[str, int]] = {}
        for record in (*self.index, *self.worktree):
            if record.sha256 is not None and record.oid is not None and record.size is not None:
                if found.setdefault(record.sha256, (record.oid, record.size)) != (record.oid, record.size):
                    raise LaneCustodyError("custody_packet_corrupt", f"payload {record.sha256} is named with two identities")
        return found


@dataclass(frozen=True, slots=True)
class CustodySummary:
    """A bounded view of what custody holds, naming at most a fixed number of paths."""

    index_changes: int
    worktree_changes: int
    build_output: int
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CustodySnapshot:
    """The lane's current inventory fingerprint; it publishes nothing.

    A capture must name this fingerprint, so it holds exactly what its caller
    looked at.  ``incomplete`` names each path no packet could hold exactly.
    """

    fingerprint: str
    head_commit: str
    head_tree: str
    incomplete: tuple[tuple[str, str], ...]
    summary: CustodySummary

    @property
    def complete(self) -> bool:
        return not self.incomplete


@dataclass(frozen=True, slots=True)
class VerifiedPacket:
    """A published packet that verified as a whole when it was read.

    Its manifest digest, binding, every payload's bytes and type, its HEAD and
    a rebuild of its index were all checked; ``advisories`` names facts that do
    not affect integrity, such as HEAD no longer being on the lane branch.
    """

    packet_id: str
    manifest: CustodyManifest
    summary: CustodySummary
    payloads: int
    payload_bytes: int
    advisories: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CustodyResult:
    """One engine answer; a refusal names its code and leaves every source untouched.

    ``staging`` names unpublished staging a failed or lost publication left in
    the store; it never verifies and nothing reads it.
    """

    success: bool
    code: str
    detail: str = ""
    snapshot: CustodySnapshot | None = None
    packet: VerifiedPacket | None = None
    payload: bytes | None = None
    incomplete: tuple[tuple[str, str], ...] = ()
    advisories: tuple[str, ...] = ()
    staging: str = ""


@dataclass(frozen=True, slots=True)
class _Session:
    """One request's derived lane, store root and binding.

    Only ``_open_session`` builds one, from the trusted context; nothing in a
    request can point it at another path or repository.
    """

    lane: LaneWorktree
    common_dir: Path
    object_format: str
    binding: CustodyBinding
    environment: Mapping[str, str] | None

    def git(self, *location: str, index_file: Path | None = None) -> _Git:
        return _Git(location or ("--git-dir", str(self.common_dir)), self.environment, index_file)

    def store_parts(self) -> tuple[str, ...]:
        return (*STORE_PARTS, self.binding.agent_instance_id)

    def packet_parts(self, directory: str) -> tuple[str, ...]:
        return (*self.store_parts(), directory)


@dataclass(frozen=True, slots=True)
class _Inventory:
    """One full reading of the lane, never stored as such.

    ``index`` and ``worktree`` hold every entry, not only the differences, so
    two readings compare by fingerprint; ``head_entries`` is HEAD's whole tree
    as ``path -> (mode, object id)``, and ``incomplete`` names each path that
    blocks capture.
    """

    head_commit: str
    head_tree: str
    head_entries: Mapping[str, tuple[int, str]]
    index: tuple[IndexRecord, ...]
    worktree: Mapping[str, WorktreeRecord]
    build_output: tuple[str, ...]
    incomplete: tuple[tuple[str, str], ...]
    fingerprint: str


class _Git:
    """Git plumbing under the caller's environment, bytes in and out.

    ``GIT_OPTIONAL_LOCKS=0`` keeps read commands from refreshing the index, so
    capture never rewrites the source's index file.
    """

    def __init__(self, location: tuple[str, ...], environment: Mapping[str, str] | None, index_file: Path | None) -> None:
        self._location = location
        self._environment = _git_environment(environment) | {"GIT_OPTIONAL_LOCKS": "0"}
        if index_file is not None:
            self._environment["GIT_INDEX_FILE"] = str(index_file)

    def attempt(self, *arguments: str, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        command = ("git", *self._location, *arguments)
        return subprocess.run(command, input=stdin, capture_output=True, check=False, env=self._environment)

    def run(self, *arguments: str, stdin: bytes | None = None) -> bytes:
        result = self.attempt(*arguments, stdin=stdin)
        if result.returncode != 0:
            detail = os.fsdecode(result.stderr or result.stdout).strip()
            raise LaneCustodyError("custody_git_failed", f"git {' '.join(arguments)} failed: {detail}")
        return result.stdout

    def text(self, *arguments: str) -> str:
        return os.fsdecode(self.run(*arguments)).strip()

    def records(self, *arguments: str) -> list[str]:
        """NUL-delimited output, each record decoded losslessly."""
        return [os.fsdecode(record) for record in self.run(*arguments).split(b"\0") if record]


class _Walker:
    """Inventory every entry beneath a worktree root without following a symlink.

    The root's own ``.git`` is administration and never read.  A nested
    repository, special file or unreadable path is named as incomplete custody
    and never opened for its bytes.  Untracked declared build output is named
    by the rule ``lane_worktrees`` uses for retirement and is not read.
    """

    def __init__(self, root: Path, tracked: frozenset[str], object_format: str) -> None:
        self._root = root
        self._tracked = tracked
        self._tracked_directories = frozenset(path.rsplit("/", depth)[0] for path in tracked for depth in range(1, path.count("/") + 1))
        self._object_format = object_format
        self.entries: dict[str, WorktreeRecord] = {}
        self.build_output: list[str] = []
        self.incomplete: list[tuple[str, str]] = []

    def scan(self) -> None:
        with ExitStack() as stack:
            self._directory(store.open_store_directory(self._root, (), stack), "")

    def _directory(self, descriptor: int, prefix: str) -> None:
        with os.scandir(descriptor) as listing:
            names = sorted(entry.name for entry in listing)
        if not names and prefix:
            path = prefix.removesuffix("/")
            self.entries[path] = WorktreeRecord(path, KIND_DIRECTORY)
        for name in names:
            if prefix or name != ".git":
                self._entry(descriptor, prefix, name)

    def _entry(self, descriptor: int, prefix: str, name: str) -> None:
        path = f"{prefix}{name}"
        try:
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError:
            self.incomplete.append((path, "protected_inaccessible"))
            return
        if stat.S_ISDIR(info.st_mode):
            self._subdirectory(descriptor, path, name)
        elif not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
            self.incomplete.append((path, "protected_nonregular"))
        elif path not in self._tracked and _is_ignorable_untracked(path):
            self.build_output.append(path)
        else:
            self._leaf(path)

    def _subdirectory(self, descriptor: int, path: str, name: str) -> None:
        """Descend unless the directory is build output, unreadable or another repository."""
        if path not in self._tracked_directories and _is_ignorable_untracked(path):
            self.build_output.append(f"{path}/")
            return
        try:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
        except OSError:
            self.incomplete.append((f"{path}/", "protected_inaccessible"))
            return
        try:
            if _holds_git_administration(child):
                self.incomplete.append((f"{path}/", "protected_nested_repository"))
            else:
                self._directory(child, f"{path}/")
        finally:
            os.close(child)

    def _leaf(self, path: str) -> None:
        """Hash one leaf's raw bytes or link text through the shared no-follow reader."""
        entry = store.read_entry(self._root, path, object_format=self._object_format, digest=True)
        if entry.mode is None:
            self.incomplete.append((path, "protected_inaccessible" if not entry.readable else "changed_during_inventory"))
            return
        self.entries[path] = WorktreeRecord(path, _LEAF_KINDS[entry.mode], entry.mode, entry.oid, entry.size, entry.sha256)


def snapshot_lane_custody(context: LaneCustodyContext, *, environment: Mapping[str, str] | None = None) -> CustodyResult:
    """Fingerprint the lane's current state and name what no packet could hold.

    Reads only; the returned fingerprint is the consent a capture must repeat.
    """
    try:
        session = _open_session(context, environment, lane=True)
        inventory = _inventory(session)
    except store.CustodyStoreError as exc:
        return _refusal(exc)
    snapshot = CustodySnapshot(
        inventory.fingerprint,
        inventory.head_commit,
        inventory.head_tree,
        inventory.incomplete,
        _summary(_index_delta(inventory), _worktree_delta(inventory), inventory.build_output),
    )
    return CustodyResult(True, "custody_snapshot", snapshot=snapshot, incomplete=inventory.incomplete)


def capture_lane_custody(
    context: LaneCustodyContext,
    *,
    expected_fingerprint: str,
    disposition: str,
    rationale: str,
    predecessor: str | None = None,
    environment: Mapping[str, str] | None = None,
    writer: store.PacketWriter = store.DEFAULT_WRITER,
) -> CustodyResult:
    """Publish one immutable packet of exactly the state ``expected_fingerprint`` names.

    Repeating an identical request returns the packet it already published.
    ``predecessor`` makes the packet a successor of a verified packet of this
    binding and needs a rationale; omitting it starts no lineage.  An
    incomplete, stale or changing lane publishes nothing.
    """
    try:
        session = _open_session(context, environment, lane=True)
        provenance, advisories = _provenance(context, disposition, rationale, predecessor)
        if predecessor is not None:
            _verify_packet(session, predecessor)
        inventory = _inventory(session)
        if inventory.incomplete:
            raise LaneCustodyError("custody_incomplete", "the lane holds state no packet can hold exactly; it stays in place", inventory.incomplete)
        if inventory.fingerprint != expected_fingerprint:
            raise LaneCustodyError("custody_fingerprint_stale", f"the lane is now {inventory.fingerprint}, not the expected {expected_fingerprint}")
        manifest, payloads = _manifest(session, inventory, provenance)
    except store.CustodyStoreError as exc:
        return _refusal(exc)
    return _publish(session, manifest, payloads, writer, advisories, partial(_require_unchanged, session, inventory))


def correct_lane_custody(
    context: LaneCustodyContext,
    predecessor: str,
    *,
    disposition: str,
    rationale: str,
    environment: Mapping[str, str] | None = None,
    writer: store.PacketWriter = store.DEFAULT_WRITER,
) -> CustodyResult:
    """Publish a successor holding the predecessor's verified bytes with corrected provenance.

    Needs no worktree, so a packet stays correctable after its lane is gone;
    the predecessor is never modified and stays readable.
    """
    try:
        session = _open_session(context, environment, lane=False)
        provenance, advisories = _provenance(context, disposition, rationale, predecessor)
        prior = _verify_packet(session, predecessor)
        manifest = replace(prior.manifest, provenance=provenance)
        payloads = _stored_payloads(session, predecessor, manifest)
    except store.CustodyStoreError as exc:
        return _refusal(exc)
    return _publish(session, manifest, payloads, writer, advisories, None)


def read_lane_custody(context: LaneCustodyContext, packet_id: str, *, environment: Mapping[str, str] | None = None) -> CustodyResult:
    """Reverify one published packet of this binding and return its manifest and summary.

    The worktree need not exist; a digest alone proves nothing until its packet
    verifies again here.
    """
    try:
        packet = _verify_packet(_open_session(context, environment, lane=False), packet_id)
    except store.CustodyStoreError as exc:
        return _refusal(exc)
    return CustodyResult(True, "custody_verified", packet=packet, advisories=packet.advisories)


def read_custody_payload(
    context: LaneCustodyContext,
    packet_id: str,
    path: str,
    *,
    layer: Literal["index", "worktree"],
    environment: Mapping[str, str] | None = None,
) -> CustodyResult:
    """Return one selected entry's exact bytes from a packet that verifies as a whole.

    There is no read of every payload at once; each read names its path and
    whether it wants the index blob or the worktree bytes.
    """
    try:
        session = _open_session(context, environment, lane=False)
        packet = _verify_packet(session, packet_id)
        records = packet.manifest.index if layer == "index" else packet.manifest.worktree
        record = next((item for item in records if item.path == path), None)
        if record is None or record.sha256 is None or record.oid is None:
            raise LaneCustodyError("custody_payload_absent", f"{layer} entry {path!r} holds no bytes in packet {packet_id}")
        relative = "/".join((*session.packet_parts(packet_id), store.PAYLOAD_DIRECTORY, record.sha256))
        data = store.read_payload(session.common_dir, relative, session.object_format, (record.sha256, record.oid))
    except store.CustodyStoreError as exc:
        return _refusal(exc)
    return CustodyResult(True, "custody_payload", packet=packet, payload=data)


def _refusal(exc: store.CustodyStoreError, staging: str = "") -> CustodyResult:
    """The refusing answer for a named error, keeping its incomplete paths and any staging.

    Every public function ends here on failure, so no refusal escapes as an
    exception and none is reported as success.
    """
    return CustodyResult(False, exc.code, exc.detail, incomplete=exc.incomplete, staging=staging)


def _open_session(context: LaneCustodyContext, environment: Mapping[str, str] | None, *, lane: bool) -> _Session:
    """Derive the lane and its repository's common directory from the trusted context alone."""
    try:
        worktree = lane_worktree_for(context.repo_root, role_name=context.lane_name, agent_instance_id=context.agent_instance_id)
        assignment_sha256 = context.assignment.sha256
    except (LaneWorktreeError, RegisterUnitError) as exc:
        raise LaneCustodyError("custody_context_invalid", str(exc)) from exc
    repository = _Git(("-C", str(worktree.repo_root)), environment, None)
    common_dir = Path(repository.text("rev-parse", "--path-format=absolute", "--git-common-dir")).resolve(strict=True)
    assignment = context.assignment
    binding = CustodyBinding(
        assignment.repository_id,
        str(common_dir),
        context.lane_name,
        context.agent_instance_id,
        worktree.branch,
        assignment.unit_id,
        assignment.unit_key,
        assignment_sha256,
        assignment.revision.revision_id,
    )
    session = _Session(worktree, common_dir, repository.text("rev-parse", "--show-object-format"), binding, environment)
    if lane:
        _require_lane_shape(session)
    return session


def _require_lane_shape(session: _Session) -> None:
    """The lane must be this repository's own linked worktree, on its own branch, at its root."""
    path = session.lane.path
    try:
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            raise LaneCustodyError("custody_lane_invalid", f"{path} is not a directory")
    except FileNotFoundError as exc:
        raise LaneCustodyError("custody_lane_missing", f"{path} does not exist") from exc
    facts = session.git("-C", str(path)).text("rev-parse", "--path-format=absolute", "--git-common-dir", "--git-dir", "--show-toplevel", "--symbolic-full-name", "HEAD").splitlines()
    common, git_dir, top, head = (*facts, "", "", "", "")[:4]
    checks = (
        ("its repository's common directory", Path(common).resolve() == session.common_dir),
        ("a linked worktree", Path(git_dir).resolve() != session.common_dir),
        ("the worktree root", Path(top).resolve() == path.resolve()),
        ("on its own lane branch", head == f"refs/heads/{session.lane.branch}"),
        ("outside the store", not session.common_dir.is_relative_to(path.resolve())),
    )
    failed = [label for label, holds in checks if not holds]
    if failed:
        raise LaneCustodyError("custody_lane_invalid", f"{path} is not {', '.join(failed)}")


def _provenance(
    context: LaneCustodyContext,
    disposition: str,
    rationale: str,
    predecessor: str | None,
) -> tuple[CustodyProvenance, tuple[str, ...]]:
    """Validate the request; missing audit text is advisory except a successor's reason.

    A successor's rationale is required because it is the only record of why
    the earlier packet was superseded; supplying any text passes.
    """
    if disposition not in _DISPOSITIONS:
        raise LaneCustodyError("custody_request_invalid", f"disposition must be one of {sorted(_DISPOSITIONS)}, not {disposition!r}")
    if predecessor is not None and not rationale.strip():
        raise LaneCustodyError("custody_successor_rationale_required", "a successor must say why it supersedes its predecessor")
    missing = (
        ("custody_actor_unrecorded: provenance names no actor", context.actor),
        ("custody_rationale_unrecorded: provenance gives no rationale", rationale),
    )
    advisories = tuple(advisory for advisory, value in missing if not value.strip())
    return CustodyProvenance(context.actor, disposition, rationale, predecessor), advisories


def _inventory(session: _Session) -> _Inventory:
    """Every index entry and worktree entry of the lane, fingerprinted as one state.

    A tracked path the walk did not find is recorded as absent, exactly as Git
    would see a deletion.
    """
    git = session.git("-C", str(session.lane.path))
    head_commit = git.text("rev-parse", "--verify", "HEAD^{commit}")
    head_tree = git.text("rev-parse", "--verify", "HEAD^{tree}")
    index = _listed_index(git, _index_flags(git))
    walker = _Walker(session.lane.path, frozenset(record.path for record in index), session.object_format)
    walker.scan()
    worktree = {record.path: walker.entries.get(record.path, WorktreeRecord(record.path, KIND_ABSENT)) for record in index} | walker.entries
    incomplete = tuple(sorted({*_index_diagnostics(index), *walker.incomplete}))
    return _Inventory(
        head_commit,
        head_tree,
        _tree_entries(git, head_tree),
        index,
        worktree,
        tuple(sorted(walker.build_output)),
        incomplete,
        _fingerprint(head_commit, head_tree, session.binding.branch, index, worktree, incomplete),
    )


def _require_unchanged(session: _Session, before: _Inventory) -> None:
    """A second full inventory must equal the first, so no new or changed leaf earns custody."""
    after = _inventory(session)
    if after.fingerprint != before.fingerprint:
        raise LaneCustodyError("custody_inventory_changed", "the lane changed while it was captured; nothing was published", after.incomplete)


def _listed_index(git: _Git, flags: Mapping[str, tuple[str, ...]]) -> tuple[IndexRecord, ...]:
    """Every index entry at every stage, with the flags Git keeps beside it."""
    records: list[IndexRecord] = []
    for record in git.records("ls-files", "-z", "-s"):
        meta, _, path = record.partition("\t")
        mode, oid, stage = meta.split(" ")
        records.append(IndexRecord(path, int(mode, 8), oid, int(stage), flags.get(path, ())))
    return tuple(records)


def _index_flags(git: _Git) -> dict[str, tuple[str, ...]]:
    """Assume-unchanged and skip-worktree from ``ls-files -v``; intent-to-add by diff contrast."""
    found: dict[str, set[str]] = {}
    for record in git.records("ls-files", "-z", "-v"):
        tag, _, path = record.partition(" ")
        marks = found.setdefault(path, set())
        if tag.islower():
            marks.add("assume_unchanged")
        if tag.upper() == "S":
            marks.add("skip_worktree")
    for path in _intent_to_add(git):
        found.setdefault(path, set()).add("intent_to_add")
    return {path: tuple(sorted(marks)) for path, marks in found.items() if marks}


def _intent_to_add(git: _Git) -> set[str]:
    """An intent-to-add entry is exactly one whose cached diff changes with its visibility.

    Git lists such an entry with the empty blob, indistinguishable from a staged
    empty file, so only this contrast names it.
    """
    views: list[set[tuple[str, str]]] = []
    for visibility in ("--ita-visible-in-index", "--ita-invisible-in-index"):
        records = git.records("diff-index", "-z", "--cached", "--raw", "--no-renames", visibility, "HEAD")
        views.append(set(zip(records[::2], records[1::2], strict=True)))
    return {path for _, path in views[0] ^ views[1]}


def _index_diagnostics(index: Sequence[IndexRecord]) -> set[tuple[str, str]]:
    """Name every index state a packet cannot rebuild exactly; none is flattened."""
    found: set[tuple[str, str]] = set()
    for record in index:
        if record.stage:
            found.add((record.path, "unsupported_index_unmerged"))
        found.update((record.path, _INDEX_FLAG_CODES[flag]) for flag in record.flags)
        if record.mode not in _LEAF_KINDS:
            found.add((record.path, "unsupported_index_gitlink" if record.mode == 0o160000 else "unsupported_index_mode"))
    return found


def _tree_entries(git: _Git, tree: str) -> dict[str, tuple[int, str]]:
    """HEAD's whole tree, recursively, as ``path -> (mode, object id)``.

    These are the paths a packet need not store: the lane branch keeps them.
    """
    entries: dict[str, tuple[int, str]] = {}
    for record in git.records("ls-tree", "-r", "-z", "--full-tree", tree):
        meta, _, path = record.partition("\t")
        mode, _kind, oid = meta.split(" ")
        entries[path] = (int(mode, 8), oid)
    return entries


def _holds_git_administration(descriptor: int) -> bool:
    """A directory with its own ``.git`` is another repository; unknown counts as one."""
    try:
        os.stat(".git", dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _fingerprint(
    head_commit: str,
    head_tree: str,
    branch: str,
    index: Sequence[IndexRecord],
    worktree: Mapping[str, WorktreeRecord],
    incomplete: Sequence[tuple[str, str]],
) -> str:
    """One digest over the whole inventory: HEAD, every index entry, every worktree entry.

    Declared build output is excluded because it is reproducible and changes
    while tools run; everything else, including incompleteness, is included.
    """
    digest = hashlib.sha256(store.canonical_bytes(["head", head_commit, head_tree, branch]))
    for record in sorted(index, key=lambda item: (item.path, item.stage)):
        digest.update(store.canonical_bytes(["index", record.path, record.mode, record.oid, record.stage, list(record.flags)]))
    for path in sorted(worktree):
        entry = worktree[path]
        digest.update(store.canonical_bytes(["worktree", path, entry.kind, entry.mode, entry.oid]))
    for path, code in incomplete:
        digest.update(store.canonical_bytes(["incomplete", path, code]))
    return digest.hexdigest()


def _index_delta(inventory: _Inventory) -> list[IndexRecord]:
    """Index entries that differ from HEAD, and HEAD paths the index no longer holds."""
    live = {record.path: record for record in inventory.index}
    changed = [record for path, record in live.items() if inventory.head_entries.get(path) != (record.mode, record.oid)]
    dropped = [IndexRecord(path, None, None) for path in inventory.head_entries.keys() - live.keys()]
    return sorted((*changed, *dropped), key=lambda record: record.path)


def _worktree_delta(inventory: _Inventory) -> list[WorktreeRecord]:
    """Worktree entries that differ from the index, including every untracked one."""
    live = {record.path: record for record in inventory.index}

    def clean(entry: WorktreeRecord) -> bool:
        staged = live.get(entry.path)
        return staged is not None and (entry.kind, entry.mode, entry.oid) == (_LEAF_KINDS.get(staged.mode or 0), staged.mode, staged.oid)

    return sorted((entry for entry in inventory.worktree.values() if not clean(entry)), key=lambda entry: entry.path)


def _summary(index: Sequence[IndexRecord], worktree: Sequence[WorktreeRecord], build_output: Sequence[str]) -> CustodySummary:
    """Counts of both deltas and the build output, with the first changed paths only.

    The manifest holds the full lists; a summary stays small however many
    paths a lane changed.
    """
    paths = sorted({record.path for record in (*index, *worktree)})
    return CustodySummary(len(index), len(worktree), len(build_output), tuple(paths[:_SUMMARY_PATH_LIMIT]))


def _manifest(session: _Session, inventory: _Inventory, provenance: CustodyProvenance) -> tuple[CustodyManifest, dict[str, store.Payload]]:
    """The packet's manifest, and where each of its payloads is copied from."""
    worktree = _worktree_delta(inventory)
    payloads = {digest: _worktree_payload(session, entry, digest) for entry in worktree if (digest := entry.sha256)}
    known = {entry.oid: entry for entry in inventory.worktree.values() if entry.oid and entry.sha256}
    index = [_with_content(session, record, known, payloads) for record in _index_delta(inventory)]
    manifest = CustodyManifest(
        session.binding,
        provenance,
        session.object_format,
        inventory.head_commit,
        inventory.head_tree,
        inventory.fingerprint,
        tuple(index),
        tuple(worktree),
        inventory.build_output,
    )
    return manifest, payloads


def _with_content(session: _Session, record: IndexRecord, known: Mapping[str, WorktreeRecord], payloads: dict[str, store.Payload]) -> IndexRecord:
    """Give an index entry its blob's digest, copied from the worktree when the bytes are there.

    A staged-only blob is read raw from the object database and checked against
    its object id, because it is lost with the index once the lane is removed.
    """
    if record.mode is None or record.oid is None:
        return record
    source = known.get(record.oid)
    if source is not None and source.sha256 is not None:
        payloads.setdefault(source.sha256, _worktree_payload(session, source, source.sha256))
        return replace(record, size=source.size, sha256=source.sha256)
    data = session.git().run("cat-file", "blob", record.oid)
    if store.blob_oid(data, session.object_format) != record.oid:
        raise LaneCustodyError("custody_object_mismatch", f"object {record.oid} does not hash to its name")
    digest = hashlib.sha256(data).hexdigest()
    payloads.setdefault(digest, store.Payload.from_bytes(data, session.object_format))
    return replace(record, size=len(data), sha256=digest)


def _worktree_payload(session: _Session, entry: WorktreeRecord, digest: str) -> store.Payload:
    """Copy a leaf straight from the lane, insisting on the mode it was inventoried with.

    The copy re-reads and re-hashes the path, so a file edited since the
    inventory cannot be stored under the old digest.
    """
    return store.Payload.from_path(session.lane.path, entry.path, session.object_format, sha256=digest, oid=entry.oid or "", mode=entry.mode)


def _stored_payloads(session: _Session, packet_id: str, manifest: CustodyManifest) -> dict[str, store.Payload]:
    """A verified packet's payloads, as the sources a successor copies and rechecks."""
    base = "/".join((*session.packet_parts(packet_id), store.PAYLOAD_DIRECTORY))
    return {digest: store.Payload.from_path(session.common_dir, f"{base}/{digest}", session.object_format, sha256=digest, oid=oid) for digest, (oid, _) in manifest.payloads().items()}


def _publish(
    session: _Session,
    manifest: CustodyManifest,
    payloads: Mapping[str, store.Payload],
    writer: store.PacketWriter,
    advisories: tuple[str, ...],
    recheck: Callable[[], None] | None,
) -> CustodyResult:
    """Keep lane binding/semantic checks around the shared byte-store publisher."""
    raw = manifest.encode()
    packet_id = hashlib.sha256(raw).hexdigest()

    def validate(staging: str) -> None:
        if recheck is not None:
            recheck()
        _verify_packet(session, packet_id, directory=staging)

    try:
        existing = _published(session, packet_id)
        if existing is not None:
            return CustodyResult(True, "custody_already_published", packet=existing, advisories=(*advisories, *existing.advisories))
        publication = store.publish_packet(
            session.common_dir,
            session.store_parts(),
            raw,
            payloads,
            writer,
            dir_mode=0o755,
            file_mode=0o444,
            pool=None,
            validate=validate,
            retain_failed_staging=True,
        )
        packet = _verify_packet(session, packet_id)
    except store.CustodyStoreError as exc:
        return _refusal(exc, exc.staging)
    except OSError as exc:
        return CustodyResult(False, "custody_publication_failed", str(exc))
    code = "custody_captured" if publication.created else "custody_already_published"
    return CustodyResult(True, code, packet=packet, advisories=(*advisories, *packet.advisories))


def _published(session: _Session, packet_id: str) -> VerifiedPacket | None:
    """The verified packet already published under this digest, or None if there is none.

    A packet that exists but does not verify is a refusal: it can never be
    replaced, so the same request cannot publish over it.
    """
    try:
        return _verify_packet(session, packet_id)
    except store.CustodyStoreError as exc:
        if exc.code == "custody_packet_missing":
            return None
        raise


def _verify_packet(session: _Session, packet_id: str, *, directory: str | None = None) -> VerifiedPacket:
    """Reverify every byte a packet asserts, then rebuild its index in a disposable file.

    ``directory`` names staging to verify before publication; otherwise the
    packet is read where its digest names it.
    """
    if not _SHA256.fullmatch(packet_id):
        raise LaneCustodyError("custody_packet_id_invalid", f"{packet_id!r} is not a packet digest")
    parts = session.packet_parts(directory or packet_id)
    raw = store.read_verified(session.common_dir, parts, packet_id=packet_id, object_format=session.object_format)
    manifest = _decode_manifest(raw)
    if manifest.binding != session.binding:
        raise LaneCustodyError("custody_packet_foreign", f"packet {packet_id} binds {manifest.binding}, not this lane")
    store.verify_payload_set(session.common_dir, parts, manifest.payloads(), object_format=session.object_format)
    advisories = _verify_history(session, manifest)
    sizes = {digest: size for digest, (_, size) in manifest.payloads().items()}
    return VerifiedPacket(packet_id, manifest, _summary(manifest.index, manifest.worktree, manifest.build_output), len(sizes), sum(sizes.values()), advisories)


def _verify_history(session: _Session, manifest: CustodyManifest) -> tuple[str, ...]:
    """HEAD must still exist, and the rebuilt index plus the worktree delta must refingerprint exactly.

    HEAD no longer being on the lane branch is advisory: the packet verifies
    today, but HEAD-equal paths then depend on something else retaining HEAD.
    """
    git = session.git()
    found = git.attempt("rev-parse", "--verify", "--quiet", f"{manifest.head_commit}^{{tree}}")
    if manifest.object_format != session.object_format or os.fsdecode(found.stdout).strip() != manifest.head_tree:
        raise LaneCustodyError("custody_head_unavailable", f"commit {manifest.head_commit} with tree {manifest.head_tree} is not in this repository")
    index = _rebuilt_index(session, manifest)
    changed = {entry.path: entry for entry in manifest.worktree}
    worktree = {record.path: WorktreeRecord(record.path, _LEAF_KINDS.get(record.mode or 0, "unsupported"), record.mode, record.oid) for record in index} | changed
    if _fingerprint(manifest.head_commit, manifest.head_tree, manifest.binding.branch, index, worktree, ()) != manifest.fingerprint:
        raise LaneCustodyError("custody_rebuild_mismatch", "the rebuilt index and recorded worktree do not reproduce the captured fingerprint")
    retained = git.attempt("merge-base", "--is-ancestor", manifest.head_commit, f"refs/heads/{manifest.binding.branch}")
    if retained.returncode == 0:
        return ()
    return (f"custody_head_unretained: {manifest.head_commit} is not on refs/heads/{manifest.binding.branch}; HEAD-equal paths depend on its retention",)


def _rebuilt_index(session: _Session, manifest: CustodyManifest) -> tuple[IndexRecord, ...]:
    """Rebuild the index from HEAD's tree and the recorded delta in a disposable index file.

    Neither the source's index nor the object database is written; Git reads
    the tree and records the delta's object ids without needing their blobs.
    """
    null = "0" * _OID_LENGTHS[manifest.object_format]
    lines = b"".join(os.fsencode(f"{record.mode:o} {record.oid}\t{record.path}\0" if record.mode is not None else f"0 {null}\t{record.path}\0") for record in manifest.index)
    with tempfile.TemporaryDirectory(prefix="lane-custody-index-") as scratch:
        git = session.git("--git-dir", str(session.common_dir), "--work-tree", scratch, index_file=Path(scratch) / "index")
        git.run("read-tree", manifest.head_tree)
        if lines:
            git.run("update-index", "-z", "--index-info", stdin=lines)
        return _listed_index(git, {})


def _decode_manifest(raw: bytes) -> CustodyManifest:
    """Parse a manifest that must re-encode to exactly its own bytes and hold only valid records."""
    try:
        value = json.loads(raw)
        manifest = CustodyManifest(
            binding=CustodyBinding(**value["binding"]),
            provenance=CustodyProvenance(**value["provenance"]),
            index=tuple(IndexRecord(**(item | {"flags": tuple(item["flags"])})) for item in value["index"]),
            worktree=tuple(WorktreeRecord(**item) for item in value["worktree"]),
            build_output=tuple(value["build_output"]),
            **{key: value[key] for key in _MANIFEST_SCALARS},
        )
        valid = manifest.encode() == raw and _valid_manifest(manifest)
    except (ValueError, KeyError, TypeError) as exc:
        raise LaneCustodyError("custody_packet_corrupt", f"manifest does not decode: {exc}") from exc
    if not valid:
        raise LaneCustodyError("custody_packet_corrupt", "manifest is not a canonical, valid custody manifest")
    return manifest


def _valid_manifest(manifest: CustodyManifest) -> bool:
    """Every field holds the type and shape a capture writes; decoded JSON could hold anything."""
    oid_length = _OID_LENGTHS.get(manifest.object_format, 0)
    provenance = manifest.provenance
    checks = (
        manifest.schema == PACKET_SCHEMA,
        _valid_hex(manifest.head_commit, oid_length),
        _valid_hex(manifest.head_tree, oid_length),
        _valid_hex(manifest.fingerprint, 64),
        provenance.disposition in _DISPOSITIONS,
        _texts(provenance.actor, provenance.rationale, *manifest.build_output),
        provenance.predecessor is None or _valid_hex(provenance.predecessor, 64),
        all(_valid_index_record(record, oid_length) for record in manifest.index),
        all(_valid_worktree_record(record, oid_length) for record in manifest.worktree),
        _unique_paths(manifest.index),
        _unique_paths(manifest.worktree),
    )
    return all(checks)


def _valid_index_record(record: IndexRecord, oid_length: int) -> bool:
    """A packet holds only stage-0 entries with no flags; anything else was never capturable."""
    if not all((_safe_path(record.path), type(record.stage) is int, record.stage == 0, record.flags == ())):
        return False
    if record.mode is None:
        return (record.oid, record.size, record.sha256) == (None, None, None)
    return type(record.mode) is int and record.mode in _LEAF_KINDS and _valid_content(record.oid, record.size, record.sha256, oid_length)


def _valid_worktree_record(record: WorktreeRecord, oid_length: int) -> bool:
    """A leaf's kind must match its mode; a directory or absence carries nothing else."""
    if not _safe_path(record.path):
        return False
    if record.kind in {KIND_ABSENT, KIND_DIRECTORY}:
        return (record.mode, record.oid, record.size, record.sha256) == (None, None, None, None)
    return type(record.mode) is int and _LEAF_KINDS.get(record.mode) == record.kind and _valid_content(record.oid, record.size, record.sha256, oid_length)


def _valid_content(oid: object, size: object, digest: object, oid_length: int) -> bool:
    return _valid_hex(oid, oid_length) and type(size) is int and size >= 0 and _valid_hex(digest, 64)


def _valid_hex(value: object, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and _HEX.fullmatch(value) is not None


def _texts(*values: object) -> bool:
    """Decoded JSON may hold any value where a field is typed as text."""
    return all(isinstance(value, str) for value in values)


def _safe_path(path: object) -> bool:
    """A repository-relative path with no empty, dot or parent component."""
    return isinstance(path, str) and "\0" not in path and not path.startswith("/") and all(part not in {"", ".", ".."} for part in path.split("/"))


def _unique_paths(records: Sequence[IndexRecord | WorktreeRecord]) -> bool:
    """Each path once, in the sorted order a capture writes."""
    paths = [record.path for record in records]
    return paths == sorted(set(paths))
