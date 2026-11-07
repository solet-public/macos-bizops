#!/usr/bin/env python3
"""Offline evidence receipts: producer failures, path domains and mailbox scope."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from fnmatch import fnmatchcase
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "quality_gates/verification_evidence.py"
type Json = None | bool | int | float | str | list[Json] | dict[str, Json]
type Receipt = dict[str, Json]


def test_cli_present() -> None:
    helper_path = HELPER
    assert helper_path.is_file(), "absent-feature red: evidence CLI is missing"


def _env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(root), *args], env=_env(), capture_output=True, check=False)


def _fixture(root: Path) -> None:
    root.mkdir()
    assert _git(root, "init", "-q").returncode == 0
    (root / "empty").write_bytes(b"")
    (root / "literal $name; ' [x]:.txt").write_bytes(b"literal bytes\n")
    assert _git(root, "add", ".").returncode == 0
    result = _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "-qm", "Fixture")
    assert result.returncode == 0, result.stderr


def _cli(*args: str) -> tuple[int, Receipt]:
    result = subprocess.run([sys.executable, str(HELPER), *args], env=_env(), capture_output=True, text=True, check=False)
    print(f"CLI argv={args!r} exit={result.returncode} receipt={result.stdout.strip()}", flush=True)
    payload = cast(Receipt, json.loads(result.stdout))
    return result.returncode, payload


def _rows(receipt: Receipt, key: str) -> list[Receipt]:
    return cast(list[Receipt], receipt[key])


def test_blob_failure(root: Path) -> None:
    # Reproduce the old consumer error without a shell pipeline: the failed
    # producer's empty stdout is a plausible digest/count to an unchecked sink.
    missing = _git(root, "show", "HEAD:missing")
    assert missing.returncode != 0 and len(missing.stdout) == 0
    assert hashlib.sha256(missing.stdout).hexdigest() == hashlib.sha256(b"").hexdigest()
    code, receipt = _cli("git-blobs", "--repo-root", str(root), "--ref", "HEAD", "--path", "missing")
    assert code != 0 and "sha256" not in _rows(receipt, "blobs")[0], "failed producer must not become a success digest"
    assert receipt["failed_count"] == 1 and receipt["attempted_count"] == 1
    row = _rows(receipt, "blobs")[0]
    assert row["producer_returncode"] != 0 and row["stderr"]
    _blob_batches(root)
    _bad_blob_inputs(root)


def _blob_batches(root: Path) -> None:
    for paths in (("empty", "literal $name; ' [x]:.txt"), ("empty", "missing")):
        argv = ["git-blobs", "--repo-root", str(root), "--ref", "HEAD"]
        for path in paths:
            argv.extend(("--path", path))
        code, receipt = _cli(*argv)
        assert (code == 0) == ("missing" not in paths)
        assert receipt["requested_count"] == receipt["attempted_count"] == 2
        rows = _rows(receipt, "blobs")
        assert len(rows) == 2
        _empty_blob(rows[0])
        if code == 0:
            assert rows[1]["sha256"] == hashlib.sha256(b"literal bytes\n").hexdigest()
            assert rows[1]["empty"] is False
        else:
            assert "sha256" not in rows[1] and "equal" not in receipt


def _empty_blob(row: Receipt) -> None:
    assert row["empty"] is True and row["byte_count"] == 0
    assert row["sha256"] == hashlib.sha256(b"").hexdigest()


def _bad_blob_inputs(root: Path) -> None:
    code, receipt = _cli("git-blobs", "--repo-root", str(root), "--ref", "no-such-ref", "--path", "empty")
    assert code != 0 and receipt["attempted_count"] == 0 and "sha256" not in _rows(receipt, "blobs")[0]
    code, receipt = _cli("git-blobs", "--repo-root", str(root), "--ref", "HEAD", "--path", "../outside")
    assert code != 0 and receipt["status"] == "error"


def test_path_parity(base: Path) -> None:
    for name in ("first", "second"):
        root = base / name
        root.mkdir()
        paths = ["docs/a.md", "src/special $x;[a].py", "docs/a.md"]
        absolute = [str(root / path) for path in paths]
        # The old relative predicate silently misses every absolute path.
        assert fnmatchcase(paths[0], "docs/**") and not fnmatchcase(absolute[0], "docs/**")
        receipts: list[Receipt] = []
        for population in (paths, absolute):
            source = base / "paths.json"
            source.write_text(json.dumps(population), encoding="utf-8")
            code, receipt = _cli("partition", "--root", str(root), "--input", str(source), "--exclude", "docs/**", "--exclude", "absent/**")
            assert code == 0
            receipts.append(receipt)
        assert receipts[0] == receipts[1], "normalization must give equal absolute and relative partitions"
        assert receipts[0]["input_count"] == receipts[0]["normalized_count"] == 3
        assert receipts[0]["included_count"] == 1 and receipts[0]["excluded_count"] == 2
        assert receipts[0]["unmatched_patterns"] == ["absent/**"] and receipts[0]["advisories"]
        _partition_invalid(base, root)


def _partition_invalid(base: Path, root: Path) -> None:
    source = base / "paths.json"
    for population, pattern, valid in (([], "docs/**", True), (["../outside"], "docs/**", False), ([str(base / "outside")], "docs/**", False), (["x"], "/absolute/**", False), (["x"], "../outside/**", False), ({"bad": []}, "docs/**", False), ([4], "docs/**", False)):
        source.write_text(json.dumps(population), encoding="utf-8")
        code, receipt = _cli("partition", "--root", str(root), "--input", str(source), "--exclude", pattern)
        assert (code == 0) == valid
        if valid:
            assert receipt["input_count"] == receipt["included_count"] == receipt["excluded_count"] == 0


def _source(sections: tuple[str, ...] = ()) -> Receipt:
    rows: list[Json] = [{"section": section, "message": {"created_at": "2026-10-18T00:00:00Z", "id": f"fixture-{index}"}} for index, section in enumerate(sections)]
    return {"messages": rows, "direct_count": sections.count("direct"), "role_count": sections.count("role"), "direct_exhausted": True, "role_exhausted": True, "role_section_status": "ok", "role_section_error": None, "complete": True, "since_applied": None, "role_read_coverage": []}


def _summarize(source: Path, payload: Receipt, *args: str) -> tuple[int, Receipt]:
    source.write_text(json.dumps(payload), encoding="utf-8")
    return _cli("inbox-coverage", "--input", str(source), *args)


def _inbox_full_and_boundary(source: Path, payload: Receipt, count: int) -> None:
    # The old role-only projection silently hid an existing direct row.
    assert not [row for row in _rows(_source(("direct",)), "messages") if row["section"] == "role"]
    code, receipt = _summarize(source, payload)
    assert code == 0 and receipt["coverage"] == "full" and receipt["selected_count"] == count
    assert receipt["full_inbox_absence"] == (count == 0)
    code, receipt = _summarize(source, payload, "--cutoff", "2026-10-18T00:00:00Z")
    assert code == 0 and receipt["selected_count"] == 0 and receipt["omitted_count"] == count


def _bound_variants() -> list[Receipt]:
    return [
        {"complete": False, "direct_exhausted": False},
        {"complete": False, "role_exhausted": False},
        {"complete": False, "role_section_status": "error", "role_section_error": "fixture bounded failure"},
        {"since_applied": {"direct": "2026-10-18T00:00:00Z", "role": "2026-10-18T00:00:00Z"}},
        {"role_read_coverage": [{"role": "Fixture", "covered_mark_present": True, "floor_hidden": 2, "receipt_hidden": 0}]},
        {"role_read_coverage": [{"role": "Fixture", "covered_mark_present": False, "floor_hidden": 0, "receipt_hidden": 1}]},
        {"role_read_coverage": [{"role": "Fixture", "covered_mark_present": False, "floor_hidden": 0, "receipt_hidden": 0, "scan_truncated": True}]},
        {"role_read_coverage": None},
    ]


def _bound_readback(receipt: Receipt, payload: Receipt) -> None:
    assert receipt["selected_count"] == 0 and receipt["full_inbox_absence"] is False
    assert receipt["source_since_applied"] == payload["since_applied"]
    assert receipt["role_read_coverage"] == payload["role_read_coverage"]
    assert receipt["advisories"]


def _bad_inbox(source: Path) -> None:
    for field in tuple(_source()):
        payload = _source()
        del payload[field]
        code, receipt = _summarize(source, payload)
        assert code != 0 and receipt["input_reference"] == str(source) and receipt["reason"]
    variants: list[Receipt] = [{"direct_count": 3}, {"messages": [{"section": "direct"}]}, {"messages": [None]}, {"role_read_coverage": [{"role": "Fixture"}]}, {"role_count": True}, {"complete": True, "direct_exhausted": False}, {"since_applied": "nonsense"}]
    for changes in variants:
        code, receipt = _summarize(source, _source() | changes)
        assert code != 0 and receipt["status"] == "error" and "raw" in receipt


def test_inbox_scope(base: Path) -> None:
    source = base / "inbox.json"
    for sections in ((), ("direct",), ("role",), ("direct", "role")):
        payload = _source(sections)
        _inbox_full_and_boundary(source, payload, len(sections))
        for options in (("--section", "direct"), ("--section", "role"), ("--cutoff", "2099-01-01T00:00:00Z")):
            code, receipt = _summarize(source, payload, *options)
            assert code == 0
            assert receipt["coverage"] == "partial", "subset cannot claim full mailbox coverage"
            assert receipt["full_inbox_absence"] is False
    for changes in _bound_variants():
        payload = _source() | changes
        code, receipt = _summarize(source, payload)
        assert code == 0
        assert receipt["coverage"] == "partial", "source bounds or hidden rows must remain partial even at zero selected rows"
        _bound_readback(receipt, payload)
    payload = _source()
    payload["role_read_coverage"] = [{"role": "Fixture", "covered_mark_present": True, "floor_hidden": 0, "receipt_hidden": 0}]
    assert _summarize(source, payload)[1]["coverage"] == "full"
    _bad_inbox(source)


def main() -> int:
    test_cli_present()
    with tempfile.TemporaryDirectory(prefix="verification_evidence_") as tmp:
        base = Path(tmp)
        root = base / "repository"
        _fixture(root)
        test_blob_failure(root)
        test_path_parity(base)
        test_inbox_scope(base)
    print("PASS verification evidence: blob failure, path parity, inbox scope")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
