"""Plan and account for static review coverage; never execute a gate.

Integrity refusals require well-formed, exact-bound inputs. This CLI produces
those inputs at each review handoff; the fixture smoke exercises the pass path.
Absent ancillary evidence is advisory UNMEASURABLE, never physical PASS.

The Subject freezes candidate identity, the complete path delta, tree manifest,
and the required empty rename-map evidence path. Planning emits exact argv but
never launches review gates. A retained receipt binds its gate, selection,
argv, and hashes; incomplete coverage remains partial or unmeasurable.
Readback and correction require the same Subject and retain the prior report
digest. Controller entries stay deferred until a separate physical run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import cast

_GATE_DIR = Path(__file__).resolve().parent
# This is the capture layer's pinned shell-name rule; it only rejects wrappers.
_CAPTURE_SHELL_BASENAMES = frozenset({"sh", "bash", "dash", "zsh", "ksh", "fish", "cmd", "cmd.exe", "powershell", "pwsh"})
if str(_GATE_DIR) not in sys.path:
    sys.path.insert(0, str(_GATE_DIR))

from quality_gates.candidate_tree import CandidateTreeError, read_scope_file, validate_candidate_manifest  # noqa: E402
from quality_gates.code_quality_check import is_per_file_gate_scoped_path  # noqa: E402
from quality_gates.code_quality_scope import CLASS_NAMES  # noqa: E402


class CoverageError(ValueError):
    """Malformed or identity-inconsistent input: invalid measurement."""


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CoverageError("expected JSON object")
    return cast(dict[str, object], value)


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CoverageError(f"expected nonblank text: {value!r}")
    return value


def _safe_path(raw: str) -> None:
    path = PurePosixPath(raw)
    unsafe = path.is_absolute() or raw != path.as_posix() or ".." in path.parts or not path.parts or path.parts[0] == ".git"
    if unsafe or any(c in raw for c in "\0\r\n"):
        raise CoverageError(f"unsafe path: {raw!r}")


def _paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CoverageError("paths must be an array")
    paths = tuple(_text(item) for item in cast(list[object], value))
    for raw in paths:
        _safe_path(raw)
    if len(set(paths)) != len(paths):
        raise CoverageError("duplicate paths")
    return tuple(sorted(paths))


def _read(path: Path) -> dict[str, object]:
    return _object(json.loads(path.read_bytes()))


def _git(root: Path, *args: str) -> bytes:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    result = subprocess.run(["git", *args], cwd=root, env=env, capture_output=True, check=False, timeout=60)
    if result.returncode:
        raise CoverageError(f"git {args!r}: exit={result.returncode} stderr={result.stderr.decode(errors='replace')}")
    return result.stdout


def _oid(value: object) -> str:
    text = _text(value)
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", text) is None:
        raise CoverageError("full Git object ID required")
    return text


def _entry(root: Path, commit: str, path: str) -> object:
    raw = _git(root, "ls-tree", "-z", commit, "--", path)
    if not raw:
        return "absent"
    meta, actual = raw.rstrip(b"\0").split(b"\t")
    mode, kind, oid = meta.split()
    if actual.decode() != path or kind != b"blob":
        raise CoverageError(f"non-blob or ambiguous entry: {path}")
    return {"mode": int(mode, 8), "sha256": digest(_git(root, "cat-file", "blob", oid.decode()))}


@dataclass(frozen=True)
class Subject:
    """Verified immutable candidate identity and complete delta."""

    candidate_sha256: str
    base: str
    commit: str
    tree: str
    manifest_sha256: str
    changed: tuple[str, ...]
    live: tuple[str, ...]
    deleted: tuple[str, ...]
    repo_root: str
    delta_file: str
    rename_file: str
    delta_sha256: str


def _delta(repo: Path, base: str, commit: str) -> tuple[str, ...]:
    return _paths([p.decode() for p in _git(repo, "diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", base, commit).split(b"\0") if p])


def _verify_entries(repo: Path, payload: dict[str, object], base: str, commit: str, changed: tuple[str, ...]) -> tuple[str, ...]:
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise CoverageError("native identity entries required")
    entries = [_object(item) for item in cast(list[object], raw_entries)]
    if _paths([entry.get("path") for entry in entries]) != changed:
        raise CoverageError("identity delta differs from immutable Git delta")
    for entry in entries:
        path = _text(entry.get("path"))
        for label, ref in (("base", base), ("final", commit)):
            if entry.get(label) != _entry(repo, ref, path):
                raise CoverageError(f"identity {label} entry mismatch: {path}")
    return tuple(sorted(_text(entry["path"]) for entry in entries if entry["final"] == "absent"))


def _verify_snapshot_entry(snapshot: Path, record: bytes) -> str:
    meta, raw_path = record.split(b"\t", 1)
    raw_mode, kind, oid = meta.split()
    path = raw_path.decode()
    _safe_path(path)
    if kind != b"blob":
        raise CoverageError(f"non-blob snapshot entry: {path}")
    actual = snapshot / path
    data = os.readlink(actual).encode() if actual.is_symlink() else actual.read_bytes()
    mode = 0o120000 if actual.is_symlink() else (0o100755 if actual.stat().st_mode & 0o111 else 0o100644)
    algorithm = "sha1" if len(oid) == 40 else "sha256"
    actual_oid = hashlib.new(algorithm, f"blob {len(data)}\0".encode() + data).hexdigest()
    if actual_oid != oid.decode() or mode != int(raw_mode, 8):
        raise CoverageError(f"snapshot bytes/mode mismatch: {path}")
    return path


def _verify_snapshot(repo: Path, commit: str, snapshot: Path, manifest: Path) -> tuple[str, ...]:
    listed = validate_candidate_manifest(snapshot, manifest)
    records = [record for record in _git(repo, "ls-tree", "-rz", commit).split(b"\0") if record]
    tree_paths = tuple(sorted(record.split(b"\t", 1)[1].decode() for record in records))
    if listed != tree_paths:
        raise CoverageError("manifest differs from candidate Git tree")
    for record in records:
        _verify_snapshot_entry(snapshot, record)
    return listed


def _scope_digest(path: Path, changed: tuple[str, ...]) -> str:
    if _paths(list(read_scope_file(path))) != changed:
        raise CoverageError("identity delta file differs from immutable Git delta")
    return digest(path.read_bytes())


def _rename_target(path: Path) -> Path:
    """Resolve its parent while leaving the final evidence entry un-followed."""
    if not str(path).strip() or path.name in {"", ".", ".."}:
        raise CoverageError("rename file path must name a file")
    try:
        parent = path.parent.resolve(strict=True)
    except OSError as error:
        raise CoverageError(f"rename file parent must already exist: {path.parent}: {error}") from error
    if not parent.is_dir():
        raise CoverageError(f"rename file parent must be a directory: {parent}")
    return parent / path.name


def _rename_file(path: Path, *, provision: bool) -> Path:
    """Optionally create, then require an empty regular file without following symlinks."""
    target = _rename_target(path)
    if provision:
        try:
            # Exclusive creation protects existing evidence from accidental truncation.
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        except OSError as error:
            raise CoverageError(f"cannot provision rename file {target}: {error}") from error
        else:
            os.close(descriptor)
    try:
        # Re-open with O_NOFOLLOW on every claim; the pathname may have changed since planning.
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise CoverageError(f"rename file must be a readable regular file: {target}: {error}") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise CoverageError(f"rename file must be a regular file: {target}")
        if os.read(descriptor, 1):
            raise CoverageError(f"rename file must be empty: {target}")
    finally:
        os.close(descriptor)
    return target


def load_subject(
    repo: Path,
    identity: Path,
    snapshot: Path,
    manifest: Path,
    delta_file: Path,
    rename_file: Path,
) -> Subject:
    """Verify native identity entries against Git and a separate complete tree."""
    payload = _read(identity)
    base = _oid(payload.get("base_commit_sha"))
    commit = _oid(payload.get("candidate_commit_sha"))
    tree = _oid(payload.get("candidate_tree_sha"))
    _git(repo, "merge-base", "--is-ancestor", base, commit)
    if _git(repo, "rev-parse", "--verify", f"{commit}^{{tree}}").decode().strip() != tree:
        raise CoverageError("candidate tree mismatch")
    changed = _delta(repo, base, commit)
    deleted = _verify_entries(repo, payload, base, commit, changed)
    patch = _git(repo, "diff", "--binary", "--full-index", "--no-renames", "--no-ext-diff", "--no-textconv", "--src-prefix=a/", "--dst-prefix=b/", base, commit)
    if digest(patch) != payload.get("patch_sha256"):
        raise CoverageError("native patch digest mismatch")
    listed = _verify_snapshot(repo, commit, snapshot, manifest)
    live = tuple(path for path in changed if path in listed)
    resolved_rename_file = _rename_file(rename_file, provision=True)
    return Subject(
        digest(canonical(payload)),
        base,
        commit,
        tree,
        digest(manifest.read_bytes()),
        changed,
        live,
        deleted,
        str(repo.resolve()),
        str(delta_file.resolve()),
        str(resolved_rename_file),
        _scope_digest(delta_file, changed),
    )


@dataclass(frozen=True)
class Gate:
    key: str
    audience: str
    applicability: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class Catalog:
    sha256: str
    gates: tuple[Gate, ...]


def _catalog_argv(key: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise CoverageError(f"{key}: catalog argv must be a nonempty array")
    tokens = cast(list[object], value)
    if any(not isinstance(token, str) or not token.strip() for token in tokens):
        raise CoverageError(f"{key}: catalog argv tokens must be nonempty strings")
    argv = tuple(cast(list[str], tokens))
    shell = Path(argv[0]).name
    # Capture invokes a direct executable, so a shell-wrapped argv is not replayable.
    if shell in _CAPTURE_SHELL_BASENAMES:
        raise CoverageError(f"{key}: capture argv may not begin with shell basename {shell!r}")
    return argv


def _gate(value: object) -> Gate:
    row = _object(value)
    key = _text(row.get("key"))
    gate = Gate(
        key,
        _text(row.get("audience")),
        _text(row.get("applicability")),
        _catalog_argv(key, row.get("argv")),
    )
    if gate.audience not in {"peer", "controller"} or gate.applicability not in {"python_delta", "candidate"}:
        raise CoverageError(f"unknown catalog applicability/audience: {gate.key}")
    return gate


def load_catalog(path: Path = _GATE_DIR / "review_static_gates.json") -> Catalog:
    raw = path.read_bytes()
    payload = _object(json.loads(raw))
    if payload.get("aggregate_classes") != "quality_gates.code_quality_scope:CLASS_NAMES":
        raise CoverageError("aggregate class source must be canonical CLASS_NAMES")
    rows = payload.get("gates")
    if not isinstance(rows, list):
        raise CoverageError("catalog gates must be an array")
    gates = [_gate(value) for value in cast(list[object], rows)]
    # Generated aggregate rows use the same capture compatibility check as source rows.
    for name in CLASS_NAMES:
        key = f"aggregate:{name}"
        argv = _catalog_argv(key, [".venv/bin/python", "quality_gates/code_quality_check.py"])
        gates.append(Gate(key, "controller", "candidate", argv))
    if len({gate.key for gate in gates}) != len(gates):
        raise CoverageError("duplicate catalog key")
    return Catalog(digest(canonical({"catalog_file_sha256": digest(raw), "aggregate_classes": CLASS_NAMES})), tuple(gates))


def _binding(subject: Subject, catalog: Catalog) -> dict[str, str]:
    return {"candidate_sha256": subject.candidate_sha256, "catalog_sha256": catalog.sha256, "manifest_sha256": subject.manifest_sha256, "delta_sha256": subject.delta_sha256}


def _check_binding(raw: Mapping[str, object], subject: Subject, catalog: Catalog) -> None:
    if any(raw.get(key) != value for key, value in _binding(subject, catalog).items()):
        raise CoverageError("STALE_SUBJECT: candidate/catalog/manifest digest mismatch")


def _receipt_outcome(receipt: dict[str, object]) -> tuple[str, str]:
    if not isinstance(receipt.get("output"), str):
        return "UNMEASURABLE", "run receipt lacks captured output"
    code = receipt.get("exit_code")
    if type(code) is not int or receipt.get("status") not in {"completed", "unavailable"}:
        raise CoverageError("malformed run status/exit code")
    if receipt["status"] == "unavailable":
        return "UNMEASURABLE", str(receipt.get("reason", "unavailable"))
    return ("PASS", "retained run receipt; execution not performed by this planner") if code == 0 else ("FAIL", f"gate exit {code}")


def _evidence(claim: dict[str, object], subject: Subject, catalog: Catalog) -> tuple[str, str]:
    """Read retained run receipt; a declaration alone is not an observed run."""
    ref = claim.get("evidence_ref")
    if not isinstance(ref, str) or not ref.strip():
        return "UNMEASURABLE", "missing evidence reference"
    try:
        raw = Path(ref).read_bytes()
    except OSError as error:
        return "UNMEASURABLE", str(error)
    if digest(raw) != claim.get("evidence_sha256"):
        raise CoverageError("evidence digest mismatch")
    receipt = _object(json.loads(raw))
    _check_binding(receipt, subject, catalog)
    for key in ("gate", "selected", "argv", "exit_code", "status"):
        if receipt.get(key) != claim.get(key):
            raise CoverageError(f"run receipt differs from claim: {key}")
    return _receipt_outcome(receipt)


def _expand_paths(argv: tuple[str, ...], selected: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(arg for token in argv for arg in (selected if token == "{paths}" else (token,)))


def _planned_argv(gate: Gate, selected: tuple[str, ...], subject: Subject) -> tuple[str, ...]:
    bindings = {
        "{repo}": subject.repo_root,
        "{commit}": subject.commit,
        "{delta_file}": subject.delta_file,
        "{rename_file}": subject.rename_file,
    }
    return tuple(bindings.get(token, token) for token in _expand_paths(gate.argv, selected))


def _identity_scope(subject: Subject, selected: tuple[str, ...]) -> None:
    if selected != subject.changed:
        raise CoverageError("identity scope does not match selected complete delta")
    # Recheck the evidence input at claim time instead of trusting plan-time bytes.
    _rename_file(Path(subject.rename_file), provision=False)
    resolved = _git(Path(subject.repo_root), "rev-parse", "--verify", f"{_oid(subject.commit)}^{{commit}}").decode().strip()
    if resolved != subject.commit:
        raise CoverageError("identity commit is not the verified candidate commit")
    if _scope_digest(Path(subject.delta_file), subject.changed) != subject.delta_sha256:
        raise CoverageError("identity delta file digest mismatch")


def _claim_argv(claim: dict[str, object], gate: Gate, selected: tuple[str, ...], subject: Subject) -> None:
    argv = claim.get("argv")
    if not isinstance(argv, list) or not argv:
        raise CoverageError("exact argv required")
    actual = tuple(_text(arg) for arg in cast(list[object], argv))
    if actual != _planned_argv(gate, selected, subject):
        raise CoverageError("argv does not match exact-bound catalog template")
    if gate.key == "identity":
        _identity_scope(subject, selected)


def _required_evidence(claim: dict[str, object]) -> None:
    _text(claim.get("evidence_ref"))
    evidence_digest = _text(claim.get("evidence_sha256"))
    if re.fullmatch(r"[0-9a-f]{64}", evidence_digest) is None:
        raise CoverageError("evidence_sha256 must be a full SHA-256")
    if type(claim.get("exit_code")) is not int or claim.get("status") not in {"completed", "unavailable"}:
        raise CoverageError("claim requires valid exit_code and status")


def _validate_claim(claim: dict[str, object], gate: Gate, subject: Subject) -> None:
    selected = _paths(claim.get("selected"))
    _claim_argv(claim, gate, selected, subject)
    _required_evidence(claim)


def _apply_claim(row: dict[str, object], gate: Gate, expected: tuple[str, ...], claim: dict[str, object], subject: Subject, catalog: Catalog) -> None:
    scanned = _paths(claim.get("selected"))
    missing = sorted(set(expected) - set(scanned))
    unexpected = sorted(set(scanned) - set(expected))
    reason = claim.get("partial_reason")
    status, evidence_reason = _evidence(claim, subject, catalog)
    row.update(scanned=list(scanned), missing=missing, unexpected=unexpected, unrun=status == "UNMEASURABLE", status=status, reason=evidence_reason, claim=claim)
    if missing or unexpected or reason is not None:
        declared = isinstance(reason, str) and bool(reason.strip())
        row.update(status="PARTIAL", partial_reason=reason, partial_declared=declared, run_status=status)
        if not declared:
            row["reason"] = f"undeclared/blank partial scope; {evidence_reason}"


def _empty_row(gate: Gate, subject: Subject) -> tuple[dict[str, object], tuple[str, ...]]:
    expected = subject.changed if gate.applicability == "candidate" else tuple(path for path in subject.live if is_per_file_gate_scoped_path(path))
    excluded = [{"path": path, "reason": "deleted; file scanner N/A" if path in subject.deleted else "outside canonical Python surface"} for path in subject.changed if path not in expected]
    row: dict[str, object] = {
        "gate": gate.key,
        "audience": gate.audience,
        "command_template": list(gate.argv),
        "planned_argv": list(_planned_argv(gate, expected, subject)),
        "expected": list(expected),
        "scanned": [],
        "excluded": excluded,
        "missing": list(expected),
        "unexpected": [],
        "unrun": True,
        "status": "NOT RUN",
        "reason": "no run receipt",
    }
    return row, expected


def _row(gate: Gate, subject: Subject, catalog: Catalog, claim: dict[str, object] | None) -> dict[str, object]:
    row, expected = _empty_row(gate, subject)
    if gate.audience == "controller":
        row.update(status="CONTROLLER-DEFERRED", reason="separate Controller physical run required")
        if claim is not None:
            row["claim"] = claim
        return row
    if claim is not None:
        _apply_claim(row, gate, expected, claim, subject, catalog)
    elif not expected and gate.applicability == "python_delta":
        row.update(status="N/A", reason="no live in-scope Python delta", missing=[])
    return row


def _claims_index(claims: Sequence[dict[str, object]], subject: Subject, catalog: Catalog) -> dict[str, dict[str, object]]:
    indexed: dict[str, dict[str, object]] = {}
    gates = {gate.key: gate for gate in catalog.gates}
    for claim in claims:
        key = _text(claim.get("gate"))
        if key in indexed or key not in gates:
            raise CoverageError(f"duplicate or unknown gate claim: {key}")
        _check_binding(claim, subject, catalog)
        _validate_claim(claim, gates[key], subject)
        indexed[key] = claim
    return indexed


def report(subject: Subject, catalog: Catalog, claims: Sequence[dict[str, object]] = (), *, supersedes: dict[str, object] | None = None) -> dict[str, object]:
    """Create deterministic accounting; correction retains immutable prior digest."""
    indexed = _claims_index(claims, subject, catalog)
    if supersedes is not None:
        validate_report(supersedes, subject, catalog)
    rows = [_row(gate, subject, catalog, indexed.get(gate.key)) for gate in catalog.gates]
    peer = [row for row in rows if row["audience"] == "peer"]
    complete = all(row["status"] in {"PASS", "N/A"} for row in peer)
    return {
        "schema": "static-review-coverage/v2",
        **_binding(subject, catalog),
        "subject": asdict(subject),
        "status": "FOCUSED-COMPLETE" if complete else "PARTIAL",
        "full_battery_complete": False,
        "execution": "accounting only; no gates executed",
        "rows": rows,
        "supersedes_sha256": digest(canonical(supersedes)) if supersedes is not None else None,
    }


def validate_report(value: dict[str, object], subject: Subject, catalog: Catalog) -> None:
    """Read back an immutable report against its current exact subject."""
    _check_binding(value, subject, catalog)
    rows = value.get("rows")
    if not isinstance(rows, list):
        raise CoverageError("report rows required")
    claims = [_object(row)["claim"] for row in cast(list[object], rows) if "claim" in _object(row)]
    rebuilt = report(subject, catalog, [_object(claim) for claim in claims])
    for key in ("schema", "subject", "status", "full_battery_complete", "execution", "rows"):
        if canonical(value.get(key)) != canonical(rebuilt[key]):
            raise CoverageError(f"report readback mismatch: {key}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--snapshot-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--delta-file", type=Path, required=True, help="complete native changed-path scope file, verified against immutable Git")
    parser.add_argument("--rename-file", type=Path, required=True, help="empty evidence file for the complete no-renames delta")
    parser.add_argument("--catalog", type=Path, default=_GATE_DIR / "review_static_gates.json")
    parser.add_argument("--claims", type=Path)
    parser.add_argument("--selected-paths", type=Path, help="validate a proposed scope file; never records execution")
    parser.add_argument("--supersedes", type=Path)
    parser.add_argument("--read-report", type=Path)
    args = parser.parse_args()
    try:
        subject = load_subject(
            args.repo_root,
            args.identity,
            args.snapshot_root,
            args.manifest,
            args.delta_file,
            args.rename_file,
        )
        catalog = load_catalog(args.catalog)
        if args.selected_paths:
            _paths(list(read_scope_file(args.selected_paths)))
        if args.read_report:
            result = _read(args.read_report)
            validate_report(result, subject, catalog)
        else:
            raw: object = json.loads(args.claims.read_bytes()) if args.claims else []
            if not isinstance(raw, list):
                raise CoverageError("claims must be an array")
            claims = [_object(item) for item in cast(list[object], raw)]
            result = report(subject, catalog, claims, supersedes=_read(args.supersedes) if args.supersedes else None)
        print(canonical(result).decode())
    except (CoverageError, CandidateTreeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"status": "INVALID MEASUREMENT", "reason": str(error)}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
