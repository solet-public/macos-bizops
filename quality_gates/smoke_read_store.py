"""Recorded-read certificates: where they are kept, and when one licenses a skip.

A certificate is the sealed trace (``smoke_reads``) of one clean real run. They are kept below the
proof store, content-addressed, with one index per smoke and tool profile that is only a hint: a
reader always reads the immutable certificate and the proof event it names, and revalidates the
certificate against the tree in front of it. A missing, stale, corrupt, incomplete or forged
certificate is an advisory and an execution, never a refusal and never a skip.

Selection reads this store and writes nothing; only a clean real run adds a certificate, and only
after its trace validates against the final tree.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import sys
import tempfile
import time
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, NamedTuple

from quality_gates.smoke_identity import RECORDED_SCOPE, FrozenTree, canonical, digest, recorded_key
from quality_gates.smoke_pass_store import CAP_SECONDS, GRACE_SECONDS, SmokePassStore
from quality_gates.smoke_reads import validate_certificate

# At most this many certificates are kept, and read back, for one smoke under one tool profile.
MAX_CERTIFICATES = 8
_MAX_TRACE_BYTES = 4 * 1024 * 1024
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_PATH_SECTIONS = ("reads", "metadata", "directories", "absent", "code_inputs")
# What a licensed row gives up: its closure, tree or never identity, which it falls back to if the claim fails.
_IDENTITY = ("key", "key_scope", "scope_limits", "inputs_sha256", "inputs_count", "repository_manifest_sha256", "proof_version", "proof_event_id", "reusable", "reuse_reason")
_RANK = {"proof_not_reusable": 0, "invalid": 1}
# Why a row the selection saw as reusable is executed after all, when its key moved to another observation.
CLAIM_CHANGED = "key changed before claim"


@dataclass(frozen=True)
class ReadCapture:
    """One attempt's launch record: where its worker writes, which attempt it is and the profile it is bound to."""

    trace_path: str
    attempt: str
    profile_sha256: str


def capture_plan(rows: Iterable[Mapping[str, Any]], directory: Path, profile_sha256: str, *, record: bool) -> Mapping[str, ReadCapture]:
    """One fresh private sidecar path per smoke the rows will execute under a key, so no earlier attempt's trace is read as this one's.

    Recording is the caller's choice: with ``record`` false the plan is empty and nothing runs under the worker.
    """
    plan = {}
    for row in rows:
        if record and not row["reusable"] and row["key_scope"] != "never":
            attempt = secrets.token_hex(16)
            plan[row["smoke"]] = ReadCapture(str(directory / attempt / "reads.json"), attempt, profile_sha256)
    return MappingProxyType(plan)


@contextmanager
def capture_directory(record: bool) -> Generator[Path]:
    """A private directory for the attempts' traces, in work-folder custody and gone when the run ends; with ``record`` false there are no traces and none is made."""
    if not record:
        yield Path()
        return
    from ananta.work_folders import process_binding, register_work_folder

    path = Path(tempfile.gettempdir()) / f"smoke-reads-{secrets.token_hex(8)}"
    path.mkdir(mode=0o700)
    folder = register_work_folder(path, purpose="smoke_reads", bound=process_binding())
    try:
        yield path
    finally:
        folder.close()


class Misses(list[str]):
    """The smokes to run, carrying the attempts whose reads the runner records.

    Like ``SmokeObservations`` in the proof tool, it adds what a legacy call shape cannot take:
    the capture travels with the entries it describes, so ``execute_selected`` and every executor
    keep their one-argument form. A runner that records nothing leaves no trace, and so no certificate.
    """

    def __init__(self, entries: Iterable[str], capture: Mapping[str, ReadCapture]) -> None:
        super().__init__(entries)
        self.capture = capture


def worker_command(python: Path, root: Path, smoke: Path, record: ReadCapture) -> list[str]:
    """The smoke's argv wrapped by the recorder's worker, which keeps its exit, output and working directory."""
    return [str(python), "-m", "quality_gates.smoke_read_worker", "--root", str(root), "--trace-out", record.trace_path, "--profile-sha256", record.profile_sha256, "--", str(smoke)]


def load_trace(smoke: str, record: ReadCapture) -> tuple[dict[str, Any] | None, str]:
    """The sidecar this attempt's worker wrote, or the reason there is none to trust."""
    try:
        path = Path(record.trace_path)
        if path.stat().st_size > _MAX_TRACE_BYTES:
            return None, "trace-too-large"
        trace = json.loads(path.read_bytes())
    except FileNotFoundError:
        return None, "no-trace"
    except (OSError, ValueError):
        return None, "unreadable-trace"
    if not isinstance(trace, dict) or trace.get("smoke") != smoke or trace.get("profile_sha256") != record.profile_sha256:
        return None, "trace-mismatch"
    return trace, ""


def trace_paths(trace: Mapping[str, Any]) -> dict[str, str]:
    """Each repository path a certificate observed, with the first section that names it."""
    found: dict[str, str] = {}
    for section in _PATH_SECTIONS:
        for row in trace.get(section, ()):
            path = row.get("path") if isinstance(row, dict) else None
            if isinstance(path, str) and not path.startswith("@"):
                found.setdefault(path, section)
    return found


def _read_paths(trace: Mapping[str, Any]) -> set[str]:
    """The repository files a certificate records as read: its recorded list, as opposed to what it only probed or listed."""
    return {row["path"] for row in trace.get("reads", ()) if isinstance(row, dict) and isinstance(row.get("path"), str)}


def _ancestors(path: str) -> Iterator[str]:
    parent = PurePosixPath(path).parent
    while str(parent) != ".":
        yield str(parent)
        parent = parent.parent
    yield "."


def _touched(changed: Sequence[str]) -> dict[str, list[str]]:
    """Every path whose recorded observation a change can alter: the changed path and each directory above it."""
    touched: dict[str, list[str]] = {}
    for path in changed:
        for hit in (path, *_ancestors(path)):
            touched.setdefault(hit, []).append(path)
    return touched


def _check_private(path: Path) -> None:
    """Owned by this uid, not a symlink, and not writable by group or world."""
    if path.is_symlink() or path.exists() and (path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o022):
        raise PermissionError("recorded-read storage must be owned by this uid without group/world write access")


def _write_atomic(path: Path, body: bytes) -> None:
    """Complete bytes or nothing: written, fsynced, then renamed over the name."""
    descriptor, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _name(value: object) -> str:
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise ValueError("certificate and key names must be canonical SHA-256 digests")
    return value


def _stem(smoke: str, profile_sha256: str) -> str:
    return digest({"smoke": smoke, "profile": profile_sha256})


def _has_proof(proofs: SmokePassStore, key: str) -> bool:
    try:
        return proofs.get(key)["observation_event_id"] is not None
    except (OSError, ValueError):
        return False


class Candidate(NamedTuple):
    """One certificate read back from the store, with the key and time its index recorded."""

    trace: dict[str, Any]
    key: str
    at: float


class RecordedReadStore:
    """Immutable certificates by digest, and a bounded index of them per smoke and tool profile."""

    def __init__(self, root: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.root, self._clock = root, clock
        self._certificates, self._indexes = root / "certificates", root / "indexes"

    @classmethod
    def beside(cls, proofs: SmokePassStore, *, clock: Callable[[], float] = time.time) -> RecordedReadStore:
        """The read store that lives below a proof store's own directory."""
        return cls(proofs.root / "read-certificates", clock=clock)

    def _prepare(self, principal: str) -> None:
        if not principal or principal == "unknown":
            raise PermissionError("an explicit authenticated proof principal is required")
        for directory in (self.root, self._certificates, self._indexes):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            _check_private(directory)

    @contextmanager
    def _locked(self, stem: str) -> Generator[None]:
        descriptor = os.open(self._indexes / f"{stem}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _entries(self, path: Path, smoke: str | None = None, profile_sha256: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """An index body and its bounded, validated entries; the caller decides what an unreadable one means."""
        _check_private(path)
        body = json.loads(path.read_bytes())
        if (smoke, profile_sha256) != (None, None) and (body["smoke"], body["profile_sha256"]) != (smoke, profile_sha256):
            raise ValueError("index describes another smoke or profile")
        return body, [{"certificate": _name(row["certificate"]), "key": _name(row["key"]), "at": float(row["at"])} for row in body["entries"][:MAX_CERTIFICATES]]

    def certificate(self, name: str) -> dict[str, Any]:
        """The immutable certificate with this digest; raises if it is missing, unsafe or not what its name says."""
        path = self._certificates / f"{_name(name)}.json"
        _check_private(path)
        trace = json.loads(path.read_bytes())
        if not isinstance(trace, dict) or trace.get("trace_sha256") != name:
            raise ValueError("certificate does not carry its own digest")
        return trace

    def candidates(self, smoke: str, profile_sha256: str, *, limit: int = MAX_CERTIFICATES) -> tuple[list[Candidate], list[str]]:
        """The newest certificates for a smoke under a profile, at most ``limit``, none older than the cap; and what could not be read."""
        path = self._indexes / f"{_stem(smoke, profile_sha256)}.json"
        if not path.exists():
            return [], []
        try:
            _, entries = self._entries(path, smoke, profile_sha256)
        except (OSError, ValueError, KeyError, TypeError) as error:
            return [], [f"{smoke}: certificate index unreadable ({type(error).__name__})"]
        found: list[Candidate] = []
        advisories: list[str] = []
        fresh = [entry for entry in entries if self._clock() - entry["at"] <= CAP_SECONDS]
        for entry in fresh[:limit]:
            try:
                found.append(Candidate(self.certificate(entry["certificate"]), entry["key"], entry["at"]))
            except (OSError, ValueError, KeyError, TypeError) as error:
                advisories.append(f"{smoke}: certificate {entry['certificate'][:12]} unreadable ({type(error).__name__})")
        return found, advisories

    def add(self, trace: Mapping[str, Any], key: str, *, principal: str) -> None:
        """Publish a certificate once, then list it first in its smoke's index; the oldest beyond the bound are removed."""
        self._prepare(principal)
        name, smoke, profile = _name(trace["trace_sha256"]), str(trace["smoke"]), str(trace["profile_sha256"])
        _write_atomic(self._certificates / f"{name}.json", canonical(trace))
        stem = _stem(smoke, profile)
        with self._locked(stem):
            try:
                _, earlier = self._entries(self._indexes / f"{stem}.json", smoke, profile)
            except (OSError, ValueError, KeyError, TypeError):
                earlier = []
            entries = [{"certificate": name, "key": _name(key), "at": self._clock()}, *(entry for entry in earlier if entry["certificate"] != name)]
            _write_atomic(self._indexes / f"{stem}.json", canonical({"smoke": smoke, "profile_sha256": profile, "entries": entries[:MAX_CERTIFICATES]}))
            for evicted in entries[MAX_CERTIFICATES:]:
                (self._certificates / f"{evicted['certificate']}.json").unlink(missing_ok=True)

    def _prune_index(self, index: Path, proofs: SmokePassStore) -> tuple[set[str], int]:
        """Keep the entries that are young enough and whose proof is present; return what the index still names and how many it dropped."""
        with self._locked(index.stem):
            try:
                body, entries = self._entries(index)
            except (OSError, ValueError, KeyError, TypeError):
                index.unlink(missing_ok=True)
                return set(), 1
            live = [entry for entry in entries if self._clock() - entry["at"] <= CAP_SECONDS and _has_proof(proofs, entry["key"])]
            for entry in entries:
                if entry not in live:
                    (self._certificates / f"{entry['certificate']}.json").unlink(missing_ok=True)
            if not live:
                index.unlink(missing_ok=True)
            elif len(live) != len(body["entries"]):
                _write_atomic(index, canonical({**body, "entries": live}))
            return {entry["certificate"] for entry in live}, len(entries) - len(live)

    def prune(self, proofs: SmokePassStore, *, principal: str) -> dict[str, int]:
        """End what no longer licenses a skip: entries past the cap or without their proof, then orphans older than the grace."""
        self._prepare(principal)
        referenced: set[str] = set()
        removed = 0
        for index in sorted(self._indexes.glob("*.json")):
            names, dropped = self._prune_index(index, proofs)
            referenced |= names
            removed += dropped
        for path in self._certificates.glob("*.json"):
            if path.stem not in referenced and self._clock() - path.stat().st_mtime > GRACE_SECONDS:
                path.unlink(missing_ok=True)
                removed += 1
        return {"kept": len(referenced), "removed": removed}


@dataclass(frozen=True)
class Context:
    """The tree a certificate is judged against: its root, its frozen entries and the tool profile in force."""

    root: Path
    entries: Mapping[str, Mapping[str, str]]
    profile: Mapping[str, Any]
    profile_sha256: str


class Licence(NamedTuple):
    """What one smoke's certificates say about the tree in front of them."""

    status: str
    certificate: str | None
    key: str | None
    reads: int
    reasons: list[str]
    proof: Mapping[str, Any] | None


class Assessment(NamedTuple):
    """What a finished attempt's own trace is worth: a certificate to publish, or the reasons it is not one."""

    status: str
    trace: dict[str, Any] | None
    key: str | None
    reasons: list[str]


def _worth_asking(row: Mapping[str, Any], edged: bool) -> bool:
    """Certificates are consulted for a smoke with a key and a complete identity, when its existing key did not reuse it or a path a certificate recorded changed."""
    return row["key_scope"] != "never" and row["identity_complete"] and (edged or not row["reusable"])


def _swap(row: dict[str, Any], licence: Licence, proof: Mapping[str, Any]) -> None:
    """Replace a row's identity by its licensing certificate's, keeping the old identity to fall back to."""
    row["fallback"] = {name: row[name] for name in _IDENTITY}
    row.update(key=licence.key, key_scope=RECORDED_SCOPE, scope_limits=[], inputs_sha256=licence.certificate, inputs_count=licence.reads, repository_manifest_sha256=None, proof_version=proof["version"], proof_event_id=proof["observation_event_id"], reusable=True, reuse_reason="recorded inputs unchanged")


class RecordedReuse:
    """Recorded certificates applied to one frozen tree: the edges they add, the skips they license and the traces they accept."""

    def __init__(self, reads: RecordedReadStore, proofs: SmokePassStore, context: Context) -> None:
        self.reads, self.proofs, self.context = reads, proofs, context

    @classmethod
    def open(cls, proofs: SmokePassStore, root: Path, candidate: FrozenTree, profile: Mapping[str, Any]) -> RecordedReuse:
        return cls(RecordedReadStore.beside(proofs), proofs, Context(root, candidate.entries, profile, digest(profile)))

    @classmethod
    def widened(cls, enabled: bool, proofs: SmokePassStore, root: Path, candidate: FrozenTree, profile: Mapping[str, Any], reasons: dict[str, list[dict[str, str]]], smokes: Iterable[str], changed: Sequence[str]) -> RecordedReuse | None:
        """This tree's certificates, with their recorded reads added to the selection's reasons; None when the caller ignores certificates."""
        if not enabled:
            return None
        reuse = cls.open(proofs, root, candidate, profile)
        reuse.widen(reasons, smokes, changed)
        return reuse

    def widen(self, reasons: dict[str, list[dict[str, str]]], smokes: Iterable[str], changed: Sequence[str]) -> None:
        """Add to the selection's reasons every smoke whose newest certificate observed a changed path, or a directory above one, whatever static analysis said."""
        touched = _touched(changed)
        for smoke in smokes:
            newest, _ = self.reads.candidates(smoke, self.context.profile_sha256, limit=1)
            paths = trace_paths(newest[0].trace) if newest else {}
            reasons.setdefault(smoke, []).extend({"rule": "recorded_read", "detail": f"{smoke} recorded a {paths[hit]} observation of {hit}", "changed_path": path, "source_tree": "candidate", "recorded_path": hit} for hit in sorted(touched.keys() & paths.keys()) for path in touched[hit])
            if not reasons[smoke]:
                del reasons[smoke]

    def _judge(self, smoke: str, candidate: Candidate, known_failure: bool) -> Licence:
        trace, context = candidate.trace, self.context
        name, reads = trace["trace_sha256"], len(trace.get("reads", ()))
        valid, reasons = validate_certificate(trace, root=context.root, entries=context.entries, profile_sha256=context.profile_sha256)
        key = recorded_key(smoke, name, context.profile)
        if not valid or key != candidate.key:
            return Licence("invalid", name, key, reads, reasons[:3] if not valid else ["recorded-key-mismatch"], None)
        try:
            proof = self.proofs.get(key)
        except (OSError, ValueError):
            return Licence("invalid", name, key, reads, ["proof-unreadable"], None)
        usable = proof["reusable"] and not proof["retired"] and not known_failure
        return Licence("licensed" if usable else "proof_not_reusable", name, key, reads, [], proof)

    def _licence(self, smoke: str, known_failure: bool) -> tuple[Licence | None, list[str]]:
        found, advisories = self.reads.candidates(smoke, self.context.profile_sha256)
        best: Licence | None = None
        for candidate in found:
            verdict = self._judge(smoke, candidate, known_failure)
            if verdict.status == "licensed":
                return verdict, advisories
            if best is None or _RANK[verdict.status] < _RANK[best.status]:
                best = verdict
        if best is None and advisories:
            best = Licence("unreadable", None, None, 0, advisories[:3], None)
        return best, advisories

    def settle(self, row: dict[str, Any], known_failure: bool) -> None:
        """Say on the row what its certificates hold, when its existing key did not reuse it or a path a certificate recorded changed.

        A licensed certificate replaces the row's key and keeps the old identity to fall back to;
        a change a certificate recorded forbids the older pass a closure or tree key would have given.
        """
        edged = any(reason["rule"] == "recorded_read" for reason in row["reasons"])
        if not _worth_asking(row, edged):
            return
        licence, advisories = self._licence(row["smoke"], known_failure)
        for advisory in advisories:
            print(f"ADVISORY recorded reads {advisory}; executing", file=sys.stderr, flush=True)
        if licence is None:
            return
        row["recorded"] = {"status": licence.status, "certificate": licence.certificate, "key": licence.key, "reads": licence.reads, "reasons": licence.reasons}
        if licence.status == "licensed" and licence.proof is not None:
            _swap(row, licence, licence.proof)
        elif edged:
            row.update(reusable=False, reuse_reason="a path its certificate recorded changed")

    def _revalidate(self, row: Mapping[str, Any]) -> str | None:
        """Why a licensed row's certificate no longer licenses a skip; None while it still does."""
        context, certificate = self.context, row["recorded"]["certificate"]
        try:
            trace = self.reads.certificate(certificate)
        except (OSError, ValueError, KeyError, TypeError):
            return "certificate unreadable before claim"
        valid, reasons = validate_certificate(trace, root=context.root, entries=context.entries, profile_sha256=context.profile_sha256)
        if not valid:
            return "certificate no longer holds before claim: " + ", ".join(reasons[:3])
        return None if recorded_key(row["smoke"], certificate, context.profile) == row["key"] else "recorded key mismatch before claim"

    def refusal(self, row: Mapping[str, Any]) -> str | None:
        """Why a row selected as reusable cannot be claimed: its certificate no longer holds, or its key moved to another observation; None to claim it."""
        if "fallback" in row and (reason := self._revalidate(row)) is not None:
            return reason
        current = self.proofs.get(row["key"])
        held = current["observation_event_id"] == row["proof_event_id"] and current["reusable"] and not current["retired"]
        return None if held else CLAIM_CHANGED

    def assess(self, smoke: str, record: ReadCapture | None) -> Assessment:
        """The attempt's own sidecar judged against the final tree: a certificate only if it is complete and every observation still holds."""
        if record is None:
            return Assessment("no_trace", None, None, ["not-captured"])
        trace, why = load_trace(smoke, record)
        if trace is None:
            return Assessment("no_trace", None, None, [why])
        context = self.context
        valid, reasons = validate_certificate(trace, root=context.root, entries=context.entries, profile_sha256=context.profile_sha256)
        if not valid:
            return Assessment("incomplete" if trace.get("complete") is not True else "invalid", None, None, [str(reason) for reason in (trace.get("incomplete_reasons") or reasons)][:6])
        return Assessment("certified", trace, recorded_key(smoke, trace["trace_sha256"], context.profile), [])

    def outside_list(self, smoke: str, trace: Mapping[str, Any]) -> dict[str, Any] | None:
        """The paths a new certificate observed that the newest earlier one did not, with both digests; None when it adds none."""
        earlier, _ = self.reads.candidates(smoke, self.context.profile_sha256, limit=1)
        if not earlier or earlier[0].trace["trace_sha256"] == trace["trace_sha256"]:
            return None
        new = sorted(_read_paths(trace) - _read_paths(earlier[0].trace))
        return {"old_sha256": earlier[0].trace["trace_sha256"], "new_sha256": trace["trace_sha256"], "new_paths": new[:20]} if new else None

    def certify(self, row: Mapping[str, Any], capture: Mapping[str, ReadCapture], *, clean: bool, origin: Mapping[str, str], publish: Callable[[str, dict[str, Any]], None], principal: str) -> dict[str, Any]:
        """Certify a clean attempt's own trace: publish its observation under the recorded key, then the index hint; otherwise say why it is no certificate."""
        smoke = row["smoke"]
        if not clean:
            return {"status": "not_clean", "reasons": []}
        assessed = self.assess(smoke, capture.get(smoke))
        if assessed.trace is None or assessed.key is None:
            return {"status": assessed.status, "reasons": assessed.reasons}
        trace, key = assessed.trace, assessed.key
        outside = self.outside_list(smoke, trace)
        publish(key, {**row, "key": key, "key_scope": RECORDED_SCOPE, "inputs_sha256": trace["trace_sha256"], "repository_manifest_sha256": None, "started_event_id": None})
        summary: dict[str, Any] = {"status": "certified", "certificate": trace["trace_sha256"], "key": key, "reasons": []}
        try:
            self.reads.add({**trace, "attribution": {**trace.get("attribution", {}), **origin, "attempt": capture[smoke].attempt}}, key, principal=principal)
        except (OSError, ValueError) as error:
            summary["reasons"] = [f"index-not-updated:{type(error).__name__}"]
            print(f"ADVISORY recorded reads {smoke}: certificate not indexed ({error})", file=sys.stderr, flush=True)
        if outside is not None:
            summary["outside_recorded_list"] = outside
            print(f"ADVISORY {smoke}: outside_recorded_list: now reads {', '.join(outside['new_paths'])} (certificate {outside['old_sha256'][:12]} -> {outside['new_sha256'][:12]})", file=sys.stderr, flush=True)
        return summary


def decline(row: dict[str, Any], reason: str) -> None:
    """Make a row selected as reusable a pending execution; a licensed row first returns to the identity it had before."""
    if "fallback" in row:
        row.update(row.pop("fallback"))
        row["recorded"] = {**row["recorded"], "status": "declined", "reasons": [reason]}
    row.update(reusable=False, reuse_reason=reason)


def current_keys(row: Mapping[str, Any]) -> set[str]:
    """The keys a row's smoke holds on this tree: its own, and its certificate's when that still validates, whatever its proof's state."""
    recorded = row.get("recorded") or {}
    return {row["key"]} | ({recorded["key"]} if recorded.get("status") in ("licensed", "proof_not_reusable") else set())
