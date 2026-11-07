"""Exact custody a retiring lane can cite for bytes its worktree still holds.

``lane_worktrees`` proves locally what the integration head already holds.  A
retiring session may also cite what the register recorded for its own Unit:
landing commits, frozen candidate revisions and registered documents.  Those
facts are read here through the configured register CLI, once per predicate
call, and discarded when the call ends; nothing is cached or persisted.  They
are claims until ``lane_worktrees`` checks them against Git objects and packet
bytes, and a read that cannot complete is an advisory rather than a refusal:
the predicate keeps its local pass paths and any unproven byte stays protected.
A caller may instead pin one immutable custody packet; then that packet, read
again with the whole lane at each phase, is the only custody that counts.

``read_entry`` is the one reader of worktree and packet files.  It records a
path the way Git does (mode, raw bytes, absence) and never follows a symlink;
a caller that keeps the bytes passes a sink and receives exactly what it hashed.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .register_unit_client import (
    PsoletRegisterUnitClient,
    RegisteredRevision,
    RegisterUnitError,
    UnitAssignment,
    assignment_from_record,
)

CUSTODY_CURRENT_MASTER = "current_master"
CUSTODY_LANDED_HISTORY = "landed_history"
CUSTODY_FROZEN_CANDIDATE = "frozen_candidate"
CUSTODY_REGISTERED_DOCUMENT = "registered_document"
CUSTODY_BUILD_OUTPUT = "build_output"
CUSTODY_EMPTY_SCAFFOLD = "empty_scaffold"
CUSTODY_VERIFIED_PACKET = "verified_packet"

CANDIDATE_STORE = "project-solet-candidates"
REGULAR_MODE = 0o100644
EXECUTABLE_MODE = 0o100755
SYMLINK_MODE = 0o120000
_ENTRY_MODES = frozenset({REGULAR_MODE, EXECUTABLE_MODE, SYMLINK_MODE})
_LANDING_COMMIT_FIELDS = ("merge_sha", "candidate_commit_sha")
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ_CHUNK = 1 << 20
EVIDENCE_PAGE_SIZE = 100
EVIDENCE_PAGE_LIMIT = 20
UNIT_TERMINAL_STATES = frozenset({"accepted", "cancelled", "superseded", "abandoned", "completed"})
"""The register's own terminal Unit states, named once at the boundary that reads them.

These are ``project_solet.unit_repository.TERMINAL_UNIT_STATES``.  No register
read reports terminality as a flag — ``db unit evidence`` prints the state the
Unit moved to and nothing derived from it — so a caller that needs the fact has
to compare.  The comparison set belongs here, beside
:meth:`PsoletRetirementEvidenceReader.read_unit_evidence`, rather than restated
by each caller.  This is the register's vocabulary, not a lifecycle this
repository owns: nothing here decides a transition or adds a state.
"""


class RetirementEvidenceReader(Protocol):
    """The register reads a retirement may cite; the CLI reader below implements them."""

    def resolve_repository(self, root: Path) -> str: ...
    def read_unit_evidence(self, unit_id: str) -> Mapping[str, Any]: ...
    def list_candidate_revisions(self, unit_id: str) -> list[Mapping[str, Any]]: ...
    def list_unit_references(self, unit_id: str) -> list[Mapping[str, Any]]: ...
    def read_revision(self, revision_id: str) -> RegisteredRevision: ...


class PsoletRetirementEvidenceReader(PsoletRegisterUnitClient):
    """The configured register CLI's timeout-bounded boundary, adding Unit-scoped reads.

    Every listing is read to its end within a fixed bound; a listing that does
    not end, or prints anything but its documented shape, is a named refusal the
    caller turns into an advisory, because an incomplete read proves nothing.
    """

    def read_unit_evidence(self, unit_id: str) -> dict[str, Any]:
        """One Unit's scoped evidence with every landing row, following its cursor."""
        rows: list[Any] = []
        cursor = ""
        for _ in range(EVIDENCE_PAGE_LIMIT):
            command = ("db", "unit", "evidence", unit_id, "--limit", str(EVIDENCE_PAGE_SIZE))
            payload, landings = _landing_page(_cli_json(self._run(*command, *(("--landing-cursor", cursor) if cursor else ())).stdout, "unit evidence"))
            rows.extend(landings["rows"])
            if not landings.get("truncated"):
                return {**payload, "landings": {**landings, "rows": rows}}
            cursor = str(landings.get("next_cursor") or "")
            if not cursor:
                raise RegisterUnitError("unit_register_invalid", "Truncated landing page has no continuation cursor.")
        raise RegisterUnitError("unit_evidence_truncated", f"Unit {unit_id} landings exceed {EVIDENCE_PAGE_LIMIT} pages.")

    def list_candidate_revisions(self, unit_id: str) -> list[Mapping[str, Any]]:
        """Every candidate revision of one Unit, each with its effective state and identity."""
        return self._offset_pages(("db", "candidate-revision", "list", "--unit", unit_id), "candidate-revision list")

    def list_unit_references(self, unit_id: str) -> list[Mapping[str, Any]]:
        """Every current junction of one Unit, naming each document's path and digest."""
        return self._offset_pages(("db", "reference", "list", "--unit", unit_id), "reference list")

    def _offset_pages(self, command: tuple[str, ...], label: str) -> list[Mapping[str, Any]]:
        rows: list[Mapping[str, Any]] = []
        for _ in range(EVIDENCE_PAGE_LIMIT):
            arguments = (*command, "--limit", str(EVIDENCE_PAGE_SIZE), "--offset", str(len(rows)))
            value = _cli_json(self._run(*arguments).stdout, label)
            page = [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []
            if not isinstance(value, list) or len(page) != len(value):
                raise RegisterUnitError("unit_register_invalid", f"psolet {label} did not print a list of objects.")
            rows.extend(page)
            if len(page) < EVIDENCE_PAGE_SIZE:
                return rows
        raise RegisterUnitError("unit_evidence_truncated", f"psolet {label} exceeds {EVIDENCE_PAGE_LIMIT} pages.")


def _landing_page(payload: object) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """One ``unit evidence`` page and its landings section, whose rows must be a list."""
    landings = payload.get("landings") if isinstance(payload, Mapping) else None
    if not isinstance(payload, Mapping) or not isinstance(landings, Mapping) or not isinstance(landings.get("rows"), list):
        raise RegisterUnitError("unit_register_invalid", "Unit evidence carries no landing page.")
    return payload, landings


def unit_lifecycle_state(evidence: Mapping[str, Any]) -> str:
    """The current register state of the Unit one ``read_unit_evidence`` call read.

    ``current_state`` is that Unit's latest undeleted state event and
    ``to_state`` the state it moved to.  A payload without exactly that shape
    proves nothing about the Unit, so it is a named refusal the caller turns
    into an advisory rather than a guess; compare the result with
    :data:`UNIT_TERMINAL_STATES` to learn whether the Unit is finished.
    """
    current = evidence.get("current_state")
    state = current.get("to_state") if isinstance(current, Mapping) else None
    if not isinstance(state, str) or not state:
        raise RegisterUnitError("unit_state_unreadable", "Unit evidence carries no current state.")
    return state


def _cli_json(raw: bytes, label: str) -> object:
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise RegisterUnitError("unit_register_invalid", f"psolet {label} printed invalid JSON.") from exc


@dataclass(frozen=True, slots=True)
class LocalEntry:
    """One path as Git would record it; ``mode`` None is absence.

    ``readable`` False marks a directory, special file or unreadable path, which
    no source can ever match.  Digests and the byte count are None when the
    caller did not ask.
    """

    mode: int | None
    sha256: str | None = None
    oid: str | None = None
    readable: bool = True
    size: int | None = None


ABSENT_ENTRY = LocalEntry(None)
UNREADABLE_ENTRY = LocalEntry(None, readable=False)


@dataclass(frozen=True, slots=True)
class FinalEntry:
    """A source's exact final state for one path: ``mode`` None means absent."""

    mode: int | None
    sha256: str | None

    def holds(self, local: LocalEntry) -> bool:
        if not local.readable:
            return False
        if self.mode is None:
            return local.mode is None
        return local.mode == self.mode and local.sha256 == self.sha256


@dataclass(frozen=True, slots=True)
class CandidateCustody:
    """A frozen candidate of the retiring Unit, exactly as the register states it."""

    revision_id: str
    identity_sha256: str
    commit_sha: str
    tree_sha: str
    entries: Mapping[str, FinalEntry]


@dataclass(frozen=True, slots=True)
class RegisterCustody:
    """What the register says preserves this Unit's bytes, for one call only."""

    landing_commits: tuple[str, ...] = ()
    candidates: tuple[CandidateCustody, ...] = ()
    documents: Mapping[str, tuple[FinalEntry, ...]] = field(default_factory=dict[str, tuple[FinalEntry, ...]])
    advisories: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PacketVerdict:
    """One reading of a pinned custody packet against the lane's whole current state.

    ``holds`` is True only when the packet verifies as a whole, carries the
    caller's expected fingerprint and a complete current snapshot of the lane
    refingerprints to exactly that value.
    """

    holds: bool
    code: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PacketCustody:
    """An immutable custody packet the retiring caller pinned for this call only.

    ``verify`` re-reads the packet and inventories the whole lane every time it
    is called, so each phase of a retirement decides afresh and no verdict is
    cached.  ``lane`` is the worktree the packet's binding derives; a packet
    proves the bytes are recoverable, never that they landed.  ``captured``
    says this call published the packet itself.
    """

    packet_id: str
    expected_fingerprint: str
    lane: Path
    verify: Callable[[], PacketVerdict]
    captured: bool = False


@dataclass(frozen=True, slots=True)
class RetirementCustodyContext:
    """The retiring session's verified identity and register boundary, never its evidence.

    ``packet`` is a caller's explicit custody packet for this call; when present
    it is the only custody that can permit removal of an unheld path.
    """

    agent_instance_id: str
    assignment: UnitAssignment | None
    register: RetirementEvidenceReader | None
    unbound_reason: str = ""
    packet: PacketCustody | None = None

    @classmethod
    def for_session(
        cls,
        row: Mapping[str, Any],
        register: RetirementEvidenceReader | None,
        packet: PacketCustody | None = None,
    ) -> RetirementCustodyContext:
        """Bind to the session's stored, digest-verified assignment; never infer a Unit."""
        agent_instance_id = str(row.get("agent_instance_id") or "")
        try:
            return cls(agent_instance_id, assignment_from_record(row), register, packet=packet)
        except RegisterUnitError as exc:
            return cls(agent_instance_id, None, register, exc.code, packet)

    def read(self, repo_root: Path) -> RegisterCustody:
        """Read this Unit's custody claims; anything unavailable becomes an advisory."""
        assignment, register = self.assignment, self.register
        if assignment is None:
            return _unavailable("retire_custody_unit_unbound", self.unbound_reason or "no registered assignment")
        if register is None:
            return _unavailable("retire_custody_register_unconfigured", "no register reader was supplied")
        try:
            evidence = register.read_unit_evidence(assignment.unit_id)
            mismatches = _identity_mismatches(
                evidence,
                register.resolve_repository(repo_root),
                assignment,
                self.agent_instance_id,
            )
            if mismatches:
                raise RegisterUnitError(
                    "retire_custody_identity_mismatch",
                    f"Unit {assignment.unit_id} does not belong to this retirement: {', '.join(mismatches)}",
                )
            landings = _landing_commits(evidence, assignment.repository_id)
        except RegisterUnitError as exc:
            return _unavailable("retire_custody_identity_unverified", str(exc))
        advisories: list[str] = []
        try:
            rows = register.list_candidate_revisions(assignment.unit_id)
            candidates = _frozen_candidates(rows, assignment, advisories)
        except RegisterUnitError as exc:
            candidates = ()
            advisories.append(f"retire_custody_candidates_unavailable: {exc}")
        try:
            documents = _registered_documents(register, assignment.unit_id, advisories)
        except RegisterUnitError as exc:
            documents = {}
            advisories.append(f"retire_custody_documents_unavailable: {exc}")
        return RegisterCustody(landings, candidates, documents, tuple(advisories))


@dataclass(frozen=True, slots=True)
class UnitEligibility:
    """Positive proof that one session's Unit is finished, or why it is not proved.

    An empty ``advisory`` is the only proved shape.  ``unit_state`` is filled
    only where the register read actually completed, so an advisory never
    carries a state the register did not return.
    """

    unit_id: str = ""
    unit_state: str = ""
    advisory: str = ""


def unit_eligibility(
    context: RetirementCustodyContext,
    register: RetirementEvidenceReader | None,
) -> UnitEligibility:
    """Prove the bound Unit is terminal, or name why this session is not proved.

    Positive proof only: the read must have completed for the session's own
    digest-verified assignment and returned a state the register itself treats
    as terminal.  Every other outcome — no assignment, no configured reader, a
    read that refused, or a Unit still in flight — is an advisory rather than a
    refusal (``rul_becb4bed``), because unknown is never completed and acting
    on a lane is irreversible for its host: unproved is treated as not
    eligible, which is the conservative direction, not a blocked gate.
    """
    assignment = context.assignment
    if assignment is None:
        return UnitEligibility(advisory=f"no registered assignment ({context.unbound_reason or 'unbound'})")
    if register is None:
        return UnitEligibility(assignment.unit_id, advisory="no register reader was supplied")
    try:
        state = unit_lifecycle_state(register.read_unit_evidence(assignment.unit_id))
    except RegisterUnitError as exc:
        return UnitEligibility(assignment.unit_id, advisory=f"register read did not complete: {exc}")
    if state not in UNIT_TERMINAL_STATES:
        return UnitEligibility(assignment.unit_id, state, f"Unit is {state}, which is not terminal")
    return UnitEligibility(assignment.unit_id, state)


def read_entry(
    root: Path,
    relative: str,
    *,
    object_format: str,
    digest: bool,
    sink: Callable[[bytes], None] | None = None,
) -> LocalEntry:
    """Read one path beneath ``root`` as Git records it, following no symlink.

    A parent that is missing, a file or a symlink means Git sees no entry here,
    so the path reads as absent.  Anything else Git cannot record as a blob is
    unreadable, and a file that changes size while it is hashed is unreadable.
    With ``digest``, ``sink`` receives the raw bytes or link text as they are
    hashed; a caller must discard them unless the returned entry is readable.
    """
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return UNREADABLE_ENTRY
    with ExitStack() as stack:
        located = _stat_beneath(root, parts, stack)
        if isinstance(located, LocalEntry):
            return located
        directory, info = located
        if stat.S_ISLNK(info.st_mode):
            return _link_entry(parts[-1], directory, object_format=object_format, digest=digest, sink=sink)
        if not stat.S_ISREG(info.st_mode):
            return UNREADABLE_ENTRY
        mode = EXECUTABLE_MODE if info.st_mode & stat.S_IXUSR else REGULAR_MODE
        if not digest:
            return LocalEntry(mode)
        return _regular_entry(parts[-1], directory, info, mode, object_format, stack, sink)


def candidate_packet(common_dir: Path, candidate: CandidateCustody) -> Path:
    """Where ``candidate-revision pack`` stores this identity's final files."""
    return common_dir / CANDIDATE_STORE / candidate.identity_sha256 / "packet"


def canonical_identity_sha256(identity: Mapping[str, Any]) -> str:
    """The digest ``candidate-revision`` names an identity by."""
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _stat_beneath(root: Path, parts: list[str], stack: ExitStack) -> tuple[int, os.stat_result] | LocalEntry:
    """Open each parent with no-follow, then lstat the leaf in its directory."""
    try:
        directory = os.open(root, _DIRECTORY_FLAGS)
        stack.callback(os.close, directory)
        for part in parts[:-1]:
            directory = os.open(part, _DIRECTORY_FLAGS, dir_fd=directory)
            stack.callback(os.close, directory)
        return directory, os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        return ABSENT_ENTRY
    except OSError as exc:
        return ABSENT_ENTRY if exc.errno == errno.ELOOP else UNREADABLE_ENTRY


def _link_entry(
    name: str,
    directory: int,
    *,
    object_format: str,
    digest: bool,
    sink: Callable[[bytes], None] | None,
) -> LocalEntry:
    """A symlink's blob is its uninterpreted target, never what it points at."""
    if not digest:
        return LocalEntry(SYMLINK_MODE)
    try:
        target = os.fsencode(os.readlink(name, dir_fd=directory))
    except OSError:
        return UNREADABLE_ENTRY
    oid = hashlib.new(object_format, b"blob %d\0" % len(target))
    oid.update(target)
    if sink is not None:
        sink(target)
    return LocalEntry(SYMLINK_MODE, hashlib.sha256(target).hexdigest(), oid.hexdigest(), size=len(target))


def _regular_entry(
    name: str,
    directory: int,
    info: os.stat_result,
    mode: int,
    object_format: str,
    stack: ExitStack,
    sink: Callable[[bytes], None] | None,
) -> LocalEntry:
    """Hash raw bytes in Git's blob framing and in SHA-256, with no filter applied."""
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    except OSError:
        return UNREADABLE_ENTRY
    stack.callback(os.close, descriptor)
    opened = os.fstat(descriptor)
    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
        return UNREADABLE_ENTRY
    sha256 = hashlib.sha256()
    oid = hashlib.new(object_format, b"blob %d\0" % opened.st_size)
    consumed = 0
    while chunk := os.read(descriptor, _READ_CHUNK):
        consumed += len(chunk)
        sha256.update(chunk)
        oid.update(chunk)
        if sink is not None:
            sink(chunk)
    if consumed != opened.st_size:
        return UNREADABLE_ENTRY
    return LocalEntry(mode, sha256.hexdigest(), oid.hexdigest(), size=consumed)


def _unavailable(code: str, detail: str) -> RegisterCustody:
    return RegisterCustody(advisories=(f"{code}: {detail}; only locally provable custody applies",))


def _identity_mismatches(
    evidence: Mapping[str, Any],
    resolved_repository: str,
    assignment: UnitAssignment,
    agent_instance_id: str,
) -> list[str]:
    """Name every way the read evidence fails to be this session's own Unit."""
    owner = evidence.get("owner")
    observed = {
        "checkout repository": (resolved_repository, assignment.repository_id),
        "evidence Unit": (evidence.get("unit_id"), assignment.unit_id),
        "evidence repository": (evidence.get("repository_id"), assignment.repository_id),
        "owning session": (owner.get("agent_instance_id") if isinstance(owner, Mapping) else None, agent_instance_id),
    }
    return [label for label, (actual, expected) in observed.items() if actual != expected]


def _landing_commits(evidence: Mapping[str, Any], repository_id: str) -> tuple[str, ...]:
    """Every commit a current landing row of this repository names, once each."""
    landings = evidence.get("landings")
    rows = landings.get("rows") if isinstance(landings, Mapping) else None
    if not isinstance(rows, list):
        raise RegisterUnitError("unit_register_invalid", "Unit evidence carries no landing rows.")
    commits = [value for row in rows if isinstance(row, Mapping) and not row.get("is_deleted") and row.get("repository_id") == repository_id for key in _LANDING_COMMIT_FIELDS if isinstance(value := row.get(key), str) and _COMMIT_SHA.fullmatch(value)]
    return tuple(dict.fromkeys(commits))


def _frozen_candidates(
    rows: list[Mapping[str, Any]],
    assignment: UnitAssignment,
    advisories: list[str],
) -> tuple[CandidateCustody, ...]:
    kept: list[CandidateCustody] = []
    for row in rows:
        outcome = _frozen_candidate(row, assignment)
        if isinstance(outcome, str):
            advisories.append(outcome)
        else:
            kept.append(outcome)
    return tuple(kept)


def _frozen_candidate(row: Mapping[str, Any], assignment: UnitAssignment) -> CandidateCustody | str:
    """Keep only a frozen, self-consistent identity of exactly this Unit and repository."""
    revision_id = str(row.get("revision_id") or "")
    identity, digest = row.get("identity"), row.get("identity_sha256")
    if row.get("state") != "frozen":
        return f"retire_custody_candidate_not_frozen: {revision_id} state={row.get('state')}"
    if not isinstance(identity, Mapping) or not isinstance(digest, str) or canonical_identity_sha256(identity) != digest:
        return f"retire_custody_candidate_invalid: {revision_id} identity digest disagrees"
    if identity.get("unit_id") != assignment.unit_id or identity.get("repository_id") != assignment.repository_id:
        return f"retire_custody_candidate_foreign: {revision_id} names another Unit or repository"
    entries = _candidate_entries(identity.get("entries"))
    if entries is None:
        return f"retire_custody_candidate_invalid: {revision_id} entries are malformed"
    return CandidateCustody(revision_id, digest, *_candidate_commit(identity), entries)


def _candidate_commit(identity: Mapping[str, Any]) -> tuple[str, str]:
    """The recorded candidate commit and tree, or neither for a legacy identity."""
    commit, tree = identity.get("candidate_commit_sha"), identity.get("candidate_tree_sha")
    if isinstance(commit, str) and isinstance(tree, str) and _COMMIT_SHA.fullmatch(commit) and _COMMIT_SHA.fullmatch(tree):
        return commit, tree
    return "", ""


def _candidate_entries(value: object) -> dict[str, FinalEntry] | None:
    if not isinstance(value, list):
        return None
    parsed = [_final_entry(item) for item in value]
    if any(entry is None for entry in parsed):
        return None
    return dict(entry for entry in parsed if entry is not None)


def _final_entry(item: object) -> tuple[str, FinalEntry] | None:
    """One identity entry: a path plus ``absent`` or exactly a Git mode and SHA-256."""
    if not isinstance(item, Mapping):
        return None
    path, final = item.get("path"), item.get("final")
    if not isinstance(path, str) or not path:
        return None
    if final == "absent":
        return path, FinalEntry(None, None)
    if not isinstance(final, Mapping):
        return None
    mode, digest = final.get("mode"), final.get("sha256")
    if mode in _ENTRY_MODES and isinstance(digest, str) and _SHA256.fullmatch(digest):
        return path, FinalEntry(int(mode), digest)
    return None


def _registered_documents(
    register: RetirementEvidenceReader,
    unit_id: str,
    advisories: list[str],
) -> dict[str, tuple[FinalEntry, ...]]:
    """Cite a document only after reading and verifying its exact registered bytes.

    A hash in a junction or an observation is a fact about bytes, not custody of
    them; only a revision whose full bytes read back and verify preserves a file.
    """
    documents: dict[str, tuple[FinalEntry, ...]] = {}
    for row in register.list_unit_references(unit_id):
        claim = _document_claim(row, unit_id)
        if claim is None:
            continue
        path, digest, revision_id = claim
        try:
            verified = _read_document_digest(register, revision_id)
        except RegisterUnitError as exc:
            advisories.append(f"retire_custody_document_unverified: {revision_id} {path}: {exc.code}")
            continue
        if verified != digest:
            advisories.append(f"retire_custody_document_unverified: {revision_id} {path}: digest disagrees")
            continue
        documents[path] = (*documents.get(path, ()), FinalEntry(REGULAR_MODE, digest))
    return documents


def _read_document_digest(register: RetirementEvidenceReader, revision_id: str) -> str:
    """The SHA-256 of a revision's full bytes, computed only after they read back and verify."""
    revision = register.read_revision(revision_id)
    revision.verify()
    return hashlib.sha256(revision.text.encode("utf-8")).hexdigest()


def _document_claim(row: Mapping[str, Any], unit_id: str) -> tuple[str, str, str] | None:
    """A current document junction of this Unit naming a path, digest and revision."""
    owner = (row.get("kind"), row.get("owner_kind"), row.get("owner_id"), bool(row.get("is_deleted")))
    claim = (row.get("document_path"), row.get("content_sha256"), row.get("version_id"))
    if owner != ("document", "unit", unit_id, False) or not all(isinstance(value, str) and value for value in claim):
        return None
    path, digest, revision_id = (str(value) for value in claim)
    return (path, digest, revision_id) if _SHA256.fullmatch(digest) else None
