#!/usr/bin/env python3
"""Read-only, explicitly scoped evidence receipts; no shell pipelines or live mail.

Requests and receipts are immutable invocation values. Correct inputs by rerunning
and retaining both receipts. Nonzero exits denote producer or evidence-integrity
errors; bounded coverage and unmatched exclusion globs are useful advisories.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import cast

type Json = None | bool | int | float | str | list[Json] | dict[str, Json]
type Receipt = dict[str, Json]


class EvidenceError(ValueError):
    """Malformed evidence, preserving its location and raw diagnostic value."""

    def __init__(self, reason: str, raw: Json = None) -> None:
        super().__init__(reason)
        self.raw = raw


def _object(value: Json, location: str) -> Receipt:
    if not isinstance(value, dict):
        raise EvidenceError(f"{location}: expected object", value)
    return value


def _array(value: Json, location: str) -> list[Json]:
    if not isinstance(value, list):
        raise EvidenceError(f"{location}: expected array", value)
    return value


def _required(row: Receipt, key: str, location: str) -> Json:
    if key not in row:
        raise EvidenceError(f"{location}: missing {key}", row)
    return row[key]


def _text(value: Json, location: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise EvidenceError(f"{location}: expected nonempty text without NUL", value)
    return value


def _boolean(row: Receipt, key: str, location: str) -> bool:
    value = _required(row, key, location)
    if not isinstance(value, bool):
        raise EvidenceError(f"{location}.{key}: expected boolean", row)
    return value


def _count(row: Receipt, key: str, location: str) -> int:
    value = _required(row, key, location)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvidenceError(f"{location}.{key}: expected nonnegative integer", row)
    return value


def _timestamp(value: Json, location: str) -> datetime:
    text = _text(value, location)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise EvidenceError(f"{location}: invalid ISO datetime", value) from exc
    # Canonical inbox timestamps may be naive UTC. Use the same interpretation
    # for a caller's cutoff, and disclose the strict greater-than boundary.
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _relative(path: str, root: Path) -> str:
    candidate = Path(_text(path, "path"))
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root):
        raise EvidenceError("path escapes the explicit root", path)
    return resolved.relative_to(root).as_posix()


def partition_paths(root: Path, population: Json, excludes: list[str]) -> Receipt:
    """Partition occurrences, preserving duplicates and every pattern's count.

    Globs use fnmatchcase on root-relative POSIX strings (including its slash
    semantics), not gitignore semantics. Existing symlinks resolve before the
    containment check; the explicit root defines the domain.
    """
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise EvidenceError("root must be a directory", str(root))
    _validate_patterns(excludes)
    paths = [_relative(_text(path, f"paths[{index}]"), root) for index, path in enumerate(_array(population, "paths"))]
    included, excluded, counts = _partition_matches(paths, excludes)
    unmatched: list[Json] = [pattern for pattern, count in zip(excludes, counts, strict=True) if count == 0]
    return {
        "mode": "partition",
        "status": "ok",
        "root": str(root),
        "glob_domain": "root-relative POSIX paths; fnmatchcase; occurrences preserved",
        "input_count": len(paths),
        "normalized_count": len(paths),
        "included_count": len(included),
        "excluded_count": len(excluded),
        "included": included,
        "excluded": excluded,
        "patterns": [{"pattern": pattern, "match_count": count} for pattern, count in zip(excludes, counts, strict=True)],
        "unmatched_patterns": unmatched,
        "advisories": ["valid exclusion patterns matched zero occurrences"] if unmatched else [],
    }


def _validate_patterns(excludes: list[str]) -> None:
    for pattern in excludes:
        _text(pattern, "exclusion")
        if PurePosixPath(pattern).is_absolute() or ".." in PurePosixPath(pattern).parts or "\\" in pattern:
            raise EvidenceError("exclusions must be safe root-relative POSIX globs", pattern)


def _partition_matches(paths: list[str], excludes: list[str]) -> tuple[list[Json], list[Json], list[int]]:
    included: list[Json] = []
    excluded: list[Json] = []
    counts = [0 for _ in excludes]
    for path in paths:
        matches = [index for index, pattern in enumerate(excludes) if fnmatchcase(path, pattern)]
        for index in matches:
            counts[index] += 1
        (excluded if matches else included).append(path)
    return included, excluded, counts


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    # Explicit roots must not inherit a caller's GIT_DIR/GIT_WORK_TREE/index.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    argv = ["git", "--no-replace-objects", "-C", str(root), *args]
    try:
        return subprocess.run(argv, env=environment, capture_output=True, check=False, shell=False, timeout=60)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(argv, 124, b"", str(exc).encode("utf-8"))
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, b"", str(exc).encode("utf-8"))


def _producer_row(path: str, ref: str, result: subprocess.CompletedProcess[bytes]) -> Receipt:
    return {"path": path, "ref": ref, "producer_returncode": result.returncode, "stderr": result.stderr.decode("utf-8", errors="replace")}


def git_blobs(root: Path, ref: str, paths: list[str]) -> tuple[Receipt, int]:
    """Collect all producer statuses before computing any successful digest."""
    root = root.resolve(strict=True)
    _text(ref, "ref")
    if not paths:
        raise EvidenceError("at least one blob path is required")
    for path in paths:
        _text(path, "blob path")
        if PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
            raise EvidenceError("blob paths must be repository-relative without traversal", path)
    resolved = _git(root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    receipt: Receipt = {"mode": "git-blobs", "repo_root": str(root), "ref": ref, "requested_count": len(paths), "attempted_count": 0, "succeeded_count": 0, "failed_count": len(paths)}
    if resolved.returncode != 0:
        receipt.update(status="error", blobs=[_producer_row(path, ref, resolved) for path in paths], error="ref resolution failed")
        return receipt, 1
    commit = resolved.stdout.decode("ascii").strip()
    results = [(path, _git(root, "show", f"{commit}:{path}")) for path in paths]
    rows: list[Json] = []
    successes = 0
    for path, result in results:
        row, succeeded = _blob_row(root, path, ref, commit, result)
        rows.append(row)
        successes += int(succeeded)
    failed = len(paths) - successes
    receipt.update(status="error" if failed else "ok", resolved_commit=commit, attempted_count=len(results), succeeded_count=successes, failed_count=failed, blobs=rows)
    return receipt, int(failed != 0)


def _blob_row(root: Path, path: str, ref: str, commit: str, result: subprocess.CompletedProcess[bytes]) -> tuple[Receipt, bool]:
    row = _producer_row(path, ref, result)
    row["resolved_commit"] = commit
    if result.returncode != 0:
        return row, False
    kind = _git(root, "cat-file", "-t", f"{commit}:{path}")
    if kind.returncode != 0 or kind.stdout.strip() != b"blob":
        row.update(error="requested object is not a readable blob", object_type_returncode=kind.returncode, object_type_stderr=kind.stderr.decode("utf-8", errors="replace"))
        return row, False
    row.update(byte_count=len(result.stdout), sha256=hashlib.sha256(result.stdout).hexdigest(), empty=not result.stdout)
    return row, True


def _source_bounds(source: Receipt) -> tuple[bool, list[Json], list[Json]]:
    complete = _boolean(source, "complete", "source")
    direct_exhausted = _boolean(source, "direct_exhausted", "source")
    role_exhausted = _boolean(source, "role_exhausted", "source")
    _validate_source_status(source, complete, direct_exhausted, role_exhausted)
    _source_horizon(source)
    advisories: list[Json] = []
    if not complete:
        advisories.append("source domain is incomplete")
    if source["since_applied"] is not None:
        advisories.append("source applied a time horizon")
    coverage = _role_coverage(source, advisories)
    return complete, coverage, advisories


def _validate_source_status(source: Receipt, complete: bool, direct_exhausted: bool, role_exhausted: bool) -> None:
    status = _required(source, "role_section_status", "source")
    error = _required(source, "role_section_error", "source")
    if status not in ("ok", "error") or (error is not None and not isinstance(error, str)):
        raise EvidenceError("source role status/error is malformed", source)
    if (status == "ok" and error is not None) or (complete and not (direct_exhausted and role_exhausted and status == "ok")):
        raise EvidenceError("source completeness/status is inconsistent", source)


def _source_horizon(source: Receipt) -> None:
    since = _required(source, "since_applied", "source")
    if since is None:
        return
    horizons = _object(since, "source.since_applied")
    for section in ("direct", "role"):
        horizon = _required(horizons, section, "source.since_applied")
        if horizon is not None:
            _timestamp(horizon, f"source.since_applied.{section}")


def _role_coverage(source: Receipt, advisories: list[Json]) -> list[Json]:
    raw_coverage = _required(source, "role_read_coverage", "source")
    if raw_coverage is None:
        advisories.append("source role coverage is unavailable")
        return []
    coverage = _array(raw_coverage, "source.role_read_coverage")
    for index, raw in enumerate(coverage):
        location = f"source.role_read_coverage[{index}]"
        row = _object(raw, location)
        _text(_required(row, "role", location), location)
        _boolean(row, "covered_mark_present", location)
        hidden = _count(row, "floor_hidden", location) + _count(row, "receipt_hidden", location)
        truncated = _boolean(row, "scan_truncated", location) if "scan_truncated" in row else False
        if hidden or truncated:
            advisories.append(f"{location}: hidden rows or truncated scan")
    return coverage


def _message_rows(source: Receipt) -> list[tuple[Receipt, str, datetime]]:
    rows: list[tuple[Receipt, str, datetime]] = []
    counts = {"direct": 0, "role": 0}
    for index, raw in enumerate(_array(_required(source, "messages", "source"), "source.messages")):
        location = f"source.messages[{index}]"
        row = _object(raw, location)
        section = _text(_required(row, "section", location), location)
        if section not in counts:
            raise EvidenceError(f"{location}: unknown section", row)
        message = _object(_required(row, "message", location), location)
        stamp = _timestamp(_required(message, "created_at", location), location)
        counts[section] += 1
        rows.append((row, section, stamp))
    for section, count in counts.items():
        if _count(source, f"{section}_count", "source") != count:
            raise EvidenceError(f"source.{section}_count does not match message rows", source)
    return rows


def _select_messages(rows: list[tuple[Receipt, str, datetime]], sections: list[str], boundary: datetime | None) -> list[Json]:
    return [row for row, section, stamp in rows if section in sections and (boundary is None or stamp > boundary)]


def inbox_coverage(payload: Json, sections: list[str], cutoff: str | None) -> Receipt:
    """Summarize captured canonical merged inbox JSON, with whole-mailbox scope."""
    source = _object(payload, "source")
    complete, _, advisories = _source_bounds(source)
    rows = _message_rows(source)
    if not sections or any(section not in ("direct", "role") for section in sections):
        raise EvidenceError("select direct, role or both sections", cast(Json, sections))
    selected_sections = sorted(set(sections))
    boundary = _timestamp(cutoff, "cutoff") if cutoff is not None else None
    selected = _select_messages(rows, selected_sections, boundary)
    if selected_sections != ["direct", "role"] or cutoff is not None:
        advisories.append("caller selected a section/time subset")
    full = not advisories
    return {
        "mode": "inbox-coverage",
        "status": "ok",
        "coverage": "full" if full else "partial",
        "source_direct_count": source["direct_count"],
        "source_role_count": source["role_count"],
        "source_count": len(rows),
        "selected_count": len(selected),
        "omitted_count": len(rows) - len(selected),
        "selected_sections": cast(Json, selected_sections),
        "cutoff": cutoff,
        "cutoff_comparison": "created_at > cutoff; naive timestamps are UTC",
        "source_complete": complete,
        "source_direct_exhausted": source["direct_exhausted"],
        "source_role_exhausted": source["role_exhausted"],
        "source_role_section_status": source["role_section_status"],
        "source_role_section_error": source["role_section_error"],
        "source_since_applied": source["since_applied"],
        "role_read_coverage": source["role_read_coverage"],
        "full_inbox_absence": full and not selected,
        "messages": selected,
        "advisories": advisories,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    blobs = modes.add_parser("git-blobs", help="hash explicit Git blobs after checking producer status")
    blobs.add_argument("--repo-root", required=True, type=Path)
    blobs.add_argument("--ref", required=True)
    blobs.add_argument("--path", action="append", required=True)
    partition = modes.add_parser("partition", help="partition a JSON path array under an explicit root")
    partition.add_argument("--root", required=True, type=Path)
    partition.add_argument("--input", required=True, type=Path)
    partition.add_argument("--exclude", action="append", default=[])
    inbox = modes.add_parser("inbox-coverage", help="summarize captured merged inbox JSON, including source coverage")
    inbox.add_argument("--input", required=True, type=Path)
    inbox.add_argument("--section", action="append", choices=("direct", "role"))
    inbox.add_argument("--cutoff", help="strict created_at > ISO datetime; any cutoff makes coverage partial")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    input_reference = str(args.input) if args.mode != "git-blobs" else str(args.repo_root)
    try:
        if args.mode == "git-blobs":
            receipt, code = git_blobs(args.repo_root, args.ref, args.path)
        else:
            payload = cast(Json, json.loads(args.input.read_text(encoding="utf-8")))
            if args.mode == "partition":
                receipt = partition_paths(args.root, payload, args.exclude)
            else:
                receipt = inbox_coverage(payload, args.section or ["direct", "role"], args.cutoff)
            code = 0
    except (EvidenceError, OSError, ValueError, subprocess.SubprocessError) as exc:
        receipt = {"mode": args.mode, "status": "error", "input_reference": input_reference, "reason": str(exc), "raw": exc.raw if isinstance(exc, EvidenceError) else None}
        code = 1
    print(json.dumps(receipt, ensure_ascii=True, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
