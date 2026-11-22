#!/usr/bin/env python3
"""Work folder record and sweep smoke (no pytest).

Proves the solet's own record of a scratch folder and its sweep over the state
interface, against real temporary directories:

* open -> close -> harvest_record -> sweep(apply) leaves the row ``deleted``
  with events opened, closed, harvested, deleted at ordinals 1-4 and the
  directory gone; a dry run removes nothing;
* a regenerable folder closed then swept is harvested and removed; an
  evidence_copy folder closed without a receipt is not removed and appears in
  the sweep's advisories; an open folder is never touched;
* a stale ``expected_version`` is refused and writes nothing; every transition
  appends one event whose ordinal is the new version;
* reopen after harvest clears the receipt while the history keeps it; retire
  then restore; a deleted row accepts nothing and a new open makes a new row;
* a symlink substituted for the directory, a changed (dev, ino), and a harvested
  row whose custody was corrected away are skipped with an advisory and left
  untouched; an absent path is recorded deleted with reason absent; a removal
  that raises is recorded as a ``removal_failed`` event;
* a naive ``now``, a symlink-unsafe ``rmtree`` and a limit of 1 behave as the
  sweep contract says; two opens of one live path return one row.

Offline: the shared real-shape state fake, schema-enforced for both tables.

Run from repo root:
    PYTHONPATH=src .venv/bin/python ananta/tests/lifecycle_management_service/work_folder_records_smoke.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "tests"))

from _real_state_fake import (  # noqa: E402
    _STANDARDIZER_COLUMNS,
    RealShapeState,
)
from ananta.config.core_schemas import CoreSchemaDefinitions  # noqa: E402
from ananta.error_handling import FrameworkError  # noqa: E402
from ananta.services.lifecycle_management_service import work_folder_sweep  # noqa: E402
from ananta.services.lifecycle_management_service.work_folder_records import (  # noqa: E402
    Actor,
    InvalidFieldError,
    InvalidPathError,
    InvalidTransitionError,
    StaleVersionError,
    UnknownFolderError,
    close,
    correct,
    get,
    harvest_record,
    history,
    list_folders,
    open_folder,
    reopen,
    require_work_folder_store,
    restore,
    retire,
)
from ananta.services.lifecycle_management_service.work_folder_schema import (  # noqa: E402
    TABLE_WORK_FOLDER,
    TABLE_WORK_FOLDER_EVENT,
    WORK_FOLDER_NAMESPACE,
    get_work_folder_schema,
)
from ananta.services.lifecycle_management_service.work_folder_sweep import SweepResult, sweep  # noqa: E402

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _raises(exc: type[Exception], fn: object, label: str) -> None:
    try:
        assert callable(fn)
        fn()
    except exc:
        _check(True, label)
    except Exception as other:  # noqa: BLE001 — the smoke reports any wrong type
        _check(False, f"{label} (raised {type(other).__name__}: {other})")
    else:
        _check(False, f"{label} (did not raise)")


def _store() -> RealShapeState:
    fake = RealShapeState()
    schema = get_work_folder_schema()
    for table in (TABLE_WORK_FOLDER, TABLE_WORK_FOLDER_EVENT):
        fake._enforced_columns[table] = (  # noqa: SLF001 — smoke wiring
            frozenset(schema.tables[table].columns) | _STANDARDIZER_COLUMNS
        )
    return fake


ACTOR = Actor(instance="agi-test", session="ases-test", model="claude-sonnet-5-5", effort="high")
NOW = datetime(2026, 11, 13, 12, 0, 0, tzinfo=UTC)
Row = dict[str, object]


def _make_dir(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir()
    (path / "scratch.txt").write_text("scratch", encoding="utf-8")
    return path


def _open(store: RealShapeState, path: Path, *, harvest: str = "evidence_copy", bound: str = "run-1") -> Row:
    row, _created = open_folder(
        store,
        path=str(path),
        purpose="release scratch",
        bound_kind="release_run",
        bound_ref=bound,
        harvest_kind=harvest,
        owner="ases-test",
        actor=ACTOR,
    )
    return row


def _version(row: Row) -> int:
    return int(str(row["version"]))


def _id(row: Row) -> str:
    return str(row["folder_id"])


def _kinds(store: RealShapeState, folder_id: str) -> list[tuple[int, str]]:
    page = history(store, folder_id=folder_id, limit=100)
    return [(int(str(event["ordinal"])), str(event["kind"])) for event in page.rows]


def _to_harvested(store: RealShapeState, row: Row, receipt: dict[str, object] | None = None) -> Row:
    due = close(store, folder_id=_id(row), expected_version=_version(row), actor=ACTOR)
    return harvest_record(
        store,
        folder_id=_id(due),
        expected_version=_version(due),
        receipt={"kind": "evidence_copy", "path": "/evidence/x"} if receipt is None else receipt,
        actor=ACTOR,
    )


def _case_open_row_shape(root: Path) -> None:
    print("open: the row records identity; close and harvest_record move it")
    store = _store()
    path = _make_dir(root, "full")
    row = _open(store, path)
    _check(_id(row).startswith("wkf-"), "folder_id carries the wkf prefix")
    _check(row["state"] == "open" and _version(row) == 1, "row opens at version 1")
    _check(row["path"] == str(path.resolve()) and row["parent_dir"] == str(root.resolve()), "path and parent_dir are canonical real paths")
    info = os.lstat(path)
    _check(row["dev"] == str(info.st_dev) and row["ino"] == str(info.st_ino), "dev and ino record the directory identity as text")
    harvested = _to_harvested(store, row)
    _check(harvested["state"] == "harvested" and _version(harvested) == 3, "harvest_record leaves the row harvested at version 3")
    _check(str(harvested["harvest_receipt"]).startswith("{"), "the receipt is stored as JSON text")


def _case_full_lifecycle(root: Path) -> None:
    print("lifecycle: harvested, dry run, sweep apply")
    store = _store()
    path = _make_dir(root, "full-sweep")
    row = _open(store, path)
    harvested = _to_harvested(store, row)
    dry = sweep(store, apply=False, now=NOW)
    _check(path.is_dir() and get(store, folder_id=_id(row)) == harvested, "a dry run removes nothing and writes nothing")
    candidates = dry["candidates"]
    _check(len(candidates) == 1 and candidates[0]["folder_id"] == _id(row), "the dry run lists the harvested folder as a candidate")
    applied = sweep(store, apply=True, now=NOW)
    final = get(store, folder_id=_id(row))
    _check(final is not None and final["state"] == "deleted" and not path.exists(), "sweep apply leaves the row deleted and the directory gone")
    _check(
        _kinds(store, _id(row)) == [(1, "opened"), (2, "closed"), (3, "harvested"), (4, "deleted")],
        "events are opened, closed, harvested, deleted at ordinals 1-4",
    )
    _check(applied["aborted"] is False and applied["counts"]["deleted"] == 1, "the sweep reports one deletion and no abort")
    again = sweep(store, apply=True, now=NOW)
    _check(again["candidates"] == [] and again["counts"]["deleted"] == 0, "a second sweep finds nothing to do")


def _case_deleted_is_terminal(root: Path) -> None:
    print("terminal: a deleted row accepts nothing; the path can be opened again")
    store = _store()
    row = _to_harvested(store, _open(store, _make_dir(root, "terminal")))
    sweep(store, apply=True, now=NOW)
    final = get(store, folder_id=_id(row))
    assert final is not None
    version = _version(final)
    again, created = open_folder(
        store,
        path=str(_make_dir(root, "terminal")),
        purpose="again",
        bound_kind="release_run",
        bound_ref="run-2",
        harvest_kind="regenerable",
        owner="ases-test",
        actor=ACTOR,
    )
    _check(created is True and _id(again) != _id(row), "a deleted row is terminal: opening the path again makes a new row")
    _raises(
        InvalidTransitionError,
        lambda: reopen(store, folder_id=_id(row), expected_version=version, actor=ACTOR),
        "a deleted row accepts no transition",
    )
    _raises(
        InvalidTransitionError,
        lambda: correct(store, folder_id=_id(row), expected_version=version, actor=ACTOR, owner="x"),
        "a deleted row accepts no correction",
    )


def _case_regenerable_and_unharvested(root: Path) -> None:
    print("sweep: regenerable removed; open, due without receipt kept")
    store = _store()
    regen = _make_dir(root, "regen")
    row = _open(store, regen, harvest="regenerable", bound="run-regen")
    due = close(store, folder_id=_id(row), expected_version=1, actor=ACTOR)
    _check(due["state"] == "due", "close of an open folder makes it due")
    evidence = _make_dir(root, "evidence")
    evidence_row = _open(store, evidence, bound="run-evidence")
    close(store, folder_id=_id(evidence_row), expected_version=1, actor=ACTOR)
    still_open = _make_dir(root, "stay-open")
    open_row = _open(store, still_open, bound="run-open")
    dry = sweep(store, apply=False, now=NOW)
    _check(regen.is_dir() and get(store, folder_id=_id(row))["state"] == "due", "a dry run does not harvest or remove a regenerable folder")  # type: ignore[index]
    _check(any(c["folder_id"] == _id(row) for c in dry["candidates"]), "the dry run lists the regenerable due folder")  # type: ignore[union-attr]
    applied = sweep(store, apply=True, now=NOW)
    _check(not regen.exists() and get(store, folder_id=_id(row))["state"] == "deleted", "a regenerable folder closed then swept is removed")  # type: ignore[index]
    _check(
        _kinds(store, _id(row)) == [(1, "opened"), (2, "closed"), (3, "harvested"), (4, "deleted")],
        "the regenerable folder's events include the automatic harvest",
    )
    _check(evidence.is_dir() and get(store, folder_id=_id(evidence_row))["state"] == "due", "an evidence_copy folder closed without a receipt is not removed")  # type: ignore[index]
    advisories = applied["advisories"]
    _check(
        any(a["folder_id"] == _id(evidence_row) and a["code"] == "awaiting_harvest_receipt" for a in advisories),
        "the missing receipt appears in the sweep's advisories",
    )
    _check(still_open.is_dir() and get(store, folder_id=_id(open_row))["state"] == "open", "an open folder is left alone")  # type: ignore[index]


def _case_stale_and_correct(root: Path) -> None:
    print("versions: stale refused, correct, transition table")
    store = _store()
    row = _open(store, _make_dir(root, "stale"))
    before_rows = list(store.rows(WORK_FOLDER_NAMESPACE, TABLE_WORK_FOLDER))
    before_events = len(store.rows(WORK_FOLDER_NAMESPACE, TABLE_WORK_FOLDER_EVENT))
    _raises(
        StaleVersionError,
        lambda: close(store, folder_id=_id(row), expected_version=9, actor=ACTOR),
        "a stale expected_version is refused",
    )
    _check(
        store.rows(WORK_FOLDER_NAMESPACE, TABLE_WORK_FOLDER) == before_rows and len(store.rows(WORK_FOLDER_NAMESPACE, TABLE_WORK_FOLDER_EVENT)) == before_events,
        "the stale write changed no row and wrote no event",
    )
    _raises(UnknownFolderError, lambda: close(store, folder_id="wkf-missing", expected_version=1, actor=ACTOR), "an unknown folder_id raises unknown_folder")
    corrected = correct(store, folder_id=_id(row), expected_version=1, actor=ACTOR, purpose="new purpose", owner="ases-other", harvest_kind="custody_packet")
    _check(
        corrected["purpose"] == "new purpose" and corrected["owner"] == "ases-other" and corrected["harvest_kind"] == "custody_packet" and _version(corrected) == 2,
        "correct changes purpose, owner and harvest_kind and bumps the version",
    )
    _check(_kinds(store, _id(row))[-1] == (2, "corrected"), "correct appends a corrected event")
    _raises(InvalidFieldError, lambda: correct(store, folder_id=_id(row), expected_version=2, actor=ACTOR, harvest_kind="bogus"), "an unknown harvest_kind is refused")
    _raises(InvalidFieldError, lambda: correct(store, folder_id=_id(row), expected_version=2, actor=ACTOR), "a correction that changes nothing is refused")
    _raises(InvalidTransitionError, lambda: reopen(store, folder_id=_id(row), expected_version=2, actor=ACTOR), "reopen of an open folder is not in the table")
    _raises(
        InvalidTransitionError,
        lambda: harvest_record(store, folder_id=_id(row), expected_version=2, receipt={"kind": "evidence_copy"}, actor=ACTOR),
        "harvest_record needs a due folder",
    )
    due = close(store, folder_id=_id(row), expected_version=2, actor=ACTOR, reason="finished")
    _raises(InvalidTransitionError, lambda: close(store, folder_id=_id(due), expected_version=3, actor=ACTOR), "close of a due folder is not in the table")
    _raises(
        InvalidFieldError,
        lambda: harvest_record(store, folder_id=_id(due), expected_version=3, receipt=None, actor=ACTOR),
        "a non-regenerable folder cannot be harvested without a receipt",
    )
    _raises(
        InvalidFieldError,
        lambda: harvest_record(store, folder_id=_id(due), expected_version=3, receipt={}, actor=ACTOR),
        "an empty receipt is refused",
    )
    _check(get(store, folder_id=_id(due))["version"] == 3, "refused transitions left the version alone")  # type: ignore[index]


def _case_reopen_retire_restore(root: Path) -> None:
    print("reverse: reopen clears the receipt, retire and restore")
    store = _store()
    row = _open(store, _make_dir(root, "rev"))
    harvested = _to_harvested(store, row, {"kind": "evidence_copy", "path": "/evidence/rev"})
    reopened = reopen(store, folder_id=_id(harvested), expected_version=_version(harvested), actor=ACTOR, reason="more work")
    _check(reopened["state"] == "open" and reopened["harvest_receipt"] == "", "reopen after harvest clears the current receipt")
    events = history(store, folder_id=_id(row), limit=100).rows
    _check(any(e["kind"] == "harvested" and "/evidence/rev" in str(e["detail"]) for e in events), "the history keeps the harvested receipt")
    retired = retire(store, folder_id=_id(reopened), expected_version=_version(reopened), actor=ACTOR, reason="keep it")
    _check(retired["state"] == "retired", "retire moves an open folder to retired")
    _raises(InvalidTransitionError, lambda: retire(store, folder_id=_id(retired), expected_version=_version(retired), actor=ACTOR), "retire of a retired folder is not in the table")
    _raises(InvalidTransitionError, lambda: close(store, folder_id=_id(retired), expected_version=_version(retired), actor=ACTOR), "a retired folder cannot be closed")
    swept = sweep(store, apply=True, now=NOW)
    _check(swept["candidates"] == [] and Path(str(row["path"])).is_dir(), "retire before the sweep keeps the folder")
    restored = restore(store, folder_id=_id(retired), expected_version=_version(retired), actor=ACTOR)
    _check(restored["state"] == "open", "restore moves a retired folder back to open")
    _check(
        [kind for _ordinal, kind in _kinds(store, _id(row))] == ["opened", "closed", "harvested", "reopened", "retired", "restored"],
        "the history reads opened, closed, harvested, reopened, retired, restored",
    )
    _raises(InvalidTransitionError, lambda: restore(store, folder_id=_id(restored), expected_version=_version(restored), actor=ACTOR), "restore of an open folder is not in the table")
    retire_due = close(store, folder_id=_id(restored), expected_version=_version(restored), actor=ACTOR)
    _check(retire(store, folder_id=_id(retire_due), expected_version=_version(retire_due), actor=ACTOR)["state"] == "retired", "a due folder can be retired")


def _advisory_codes(result: SweepResult, folder_id: str) -> list[str]:
    return [a["code"] for a in result["advisories"] if a.get("folder_id") == folder_id]


def _case_substituted_directory(root: Path) -> None:
    print("sweep: a symlink or a changed (dev, ino) is skipped and left untouched")
    store = _store()
    link_target = _make_dir(root, "link-target")
    victim = _make_dir(root, "victim")
    victim_row = _to_harvested(store, _open(store, victim, bound="run-victim"))
    shutil.rmtree(victim)
    os.symlink(link_target, victim)
    moved = _make_dir(root, "moved")
    moved_row = _to_harvested(store, _open(store, moved, bound="run-moved"))
    store.update_state(WORK_FOLDER_NAMESPACE, {"table": TABLE_WORK_FOLDER, "filters": {"folder_id": _id(moved_row)}}, {"ino": "1"})
    result = sweep(store, apply=True, now=NOW)
    _check((link_target / "scratch.txt").exists() and os.path.islink(victim), "a symlink substituted for the directory is left untouched")
    _check(_advisory_codes(result, _id(victim_row)) == ["not_a_real_directory"], "the symlink is skipped with an advisory")
    _check(moved.is_dir(), "a changed (dev, ino) leaves the directory in place")
    _check(_advisory_codes(result, _id(moved_row)) == ["identity_changed"], "a changed (dev, ino) is skipped with an advisory")
    states = {get(store, folder_id=_id(r)).get("state") for r in (victim_row, moved_row)}  # type: ignore[union-attr]
    _check(states == {"harvested"}, "the skipped rows stay harvested")
    _check(result["counts"]["skipped"] == 2 and result["counts"]["deleted"] == 0, "the sweep counts the skips and deletes nothing")


def _case_no_custody_and_absent(root: Path) -> None:
    print("sweep: custody corrected away is kept; an absent path is recorded deleted")
    store = _store()
    nocustody = _make_dir(root, "nocustody")
    row = _open(store, nocustody, harvest="regenerable", bound="run-nocustody")
    due = close(store, folder_id=_id(row), expected_version=1, actor=ACTOR)
    harvested = harvest_record(store, folder_id=_id(due), expected_version=2, receipt=None, actor=ACTOR)
    correct(store, folder_id=_id(harvested), expected_version=3, actor=ACTOR, harvest_kind="evidence_copy")
    gone = _make_dir(root, "gone")
    gone_row = _to_harvested(store, _open(store, gone, bound="run-gone"))
    shutil.rmtree(gone)
    result = sweep(store, apply=True, now=NOW)
    _check(nocustody.is_dir(), "a harvested row without a receipt is never removed")
    _check(_advisory_codes(result, _id(row)) == ["no_custody"], "the missing custody is an advisory")
    final = get(store, folder_id=_id(gone_row))
    events = history(store, folder_id=_id(gone_row), limit=100).rows
    _check(final is not None and final["state"] == "deleted", "an absent path is recorded deleted")
    _check("absent" in str(events[-1]["detail"]) and events[-1]["kind"] == "deleted", "the deleted event gives the reason absent")
    _check(result["counts"]["absent"] == 1, "the sweep counts the absent path")


def _case_failed_removal(root: Path) -> None:
    print("sweep: a removal that raises is recorded and retried by the next run")
    store = _store()
    stuck = _make_dir(root, "stuck")
    stuck_row = _to_harvested(store, _open(store, stuck, bound="run-stuck"))
    real_rmtree = shutil.rmtree

    def broken(path: object, *args: object, **kwargs: object) -> None:
        raise PermissionError("denied by the smoke")

    broken.avoids_symlink_attacks = True  # type: ignore[attr-defined]
    shutil.rmtree = broken  # type: ignore[assignment]
    try:
        result = sweep(store, apply=True, now=NOW)
    finally:
        shutil.rmtree = real_rmtree
    _check(stuck.is_dir() and get(store, folder_id=_id(stuck_row))["state"] == "harvested", "a failed removal leaves the row harvested")  # type: ignore[index]
    _check(_kinds(store, _id(stuck_row))[-1][1] == "removal_failed", "a failed removal is recorded as an event")
    _check(result["counts"]["failed"] == 1, "the sweep counts the failed removal")
    retry = sweep(store, apply=True, now=NOW)
    _check(not stuck.exists() and get(store, folder_id=_id(stuck_row))["state"] == "deleted", "the next sweep removes it once removal works")  # type: ignore[index]
    _check(retry["counts"]["deleted"] == 1, "the retry reports the deletion")


def _two_harvested(store: RealShapeState, root: Path) -> tuple[Path, Path]:
    first = _make_dir(root, "limit-a")
    second = _make_dir(root, "limit-b")
    _to_harvested(store, _open(store, first, bound="run-a"))
    _to_harvested(store, _open(store, second, bound="run-b"))
    return first, second


def _case_invalid_now(root: Path) -> None:
    print("sweep: a naive or non-datetime now aborts; another offset is accepted")
    store = _store()
    first, second = _two_harvested(store, root)
    for bad, label in ((datetime(2026, 11, 13, 12, 0, 0), "naive"), ("2026-11-13T12:00:00Z", "string")):
        aborted = sweep(store, apply=True, now=bad)  # type: ignore[arg-type]
        _check(aborted["aborted"] is True and first.is_dir() and second.is_dir(), f"a {label} now aborts and deletes nothing")
        _check([a["code"] for a in aborted["advisories"]] == ["invalid_now"], f"a {label} now aborts with an advisory")
    offset = sweep(store, apply=False, now=datetime(2026, 11, 13, 5, 0, 0, tzinfo=timezone(timedelta(hours=-7))))
    _check(offset["aborted"] is False, "an aware now in another offset is accepted")
    _raises(InvalidFieldError, lambda: sweep(store, apply=False, limit=0, now=NOW), "a limit below 1 is a caller error")


def _case_unsafe_rmtree_and_limit(root: Path) -> None:
    print("sweep: a symlink-unsafe rmtree aborts an applying run; limit defers the rest")
    store = _store()
    first, second = _two_harvested(store, root)
    original = shutil.rmtree.avoids_symlink_attacks
    shutil.rmtree.avoids_symlink_attacks = False  # type: ignore[attr-defined]
    try:
        unsafe = sweep(store, apply=True, now=NOW)
        dry_unsafe = sweep(store, apply=False, now=NOW)
    finally:
        shutil.rmtree.avoids_symlink_attacks = original  # type: ignore[attr-defined]
    _check(unsafe["aborted"] is True and first.is_dir() and second.is_dir(), "a symlink-unsafe rmtree aborts an applying sweep and deletes nothing")
    _check([a["code"] for a in unsafe["advisories"]] == ["rmtree_not_symlink_safe"], "the abort carries an advisory")
    _check(dry_unsafe["aborted"] is False, "the symlink check only gates an applying sweep")
    one = sweep(store, apply=True, limit=1, now=NOW)
    _check(len([p for p in (first, second) if p.exists()]) == 1 and one["counts"]["deleted"] == 1, "limit 1 with two candidates removes one")
    _check(len(one["deferred"]) == 1, "the other candidate is listed as deferred")
    two = sweep(store, apply=True, limit=1, now=NOW)
    _check(not first.exists() and not second.exists() and two["deferred"] == [], "the next run removes the other")


def _open_args(path: str, **override: str) -> dict[str, object]:
    args: dict[str, object] = {
        "path": path,
        "purpose": "p",
        "bound_kind": "release_run",
        "bound_ref": "r1",
        "harvest_kind": "regenerable",
        "owner": "o",
        "actor": ACTOR,
    }
    args.update(override)
    return args


def _case_open_idempotent(root: Path) -> None:
    print("open: two opens of one live path return one row")
    store = _store()
    path = _make_dir(root, "once")
    first, created_first = open_folder(store, **_open_args(str(path)))  # type: ignore[arg-type]
    second, created_second = open_folder(store, **_open_args(str(path), purpose="other", bound_ref="r9", harvest_kind="evidence_copy"))  # type: ignore[arg-type]
    _check(created_first is True and created_second is False and _id(first) == _id(second), "two opens of the same live path return one row")
    _check(len(store.rows(WORK_FOLDER_NAMESPACE, TABLE_WORK_FOLDER)) == 1, "the repeat wrote no row")
    _check(len(store.rows(WORK_FOLDER_NAMESPACE, TABLE_WORK_FOLDER_EVENT)) == 1, "the repeat wrote no event")
    _check(second["purpose"] == "p" and second["bound_ref"] == "r1", "the repeat returns the existing row unchanged")
    link = root / "alias"
    os.symlink(path, link)
    via_link, created_link = open_folder(store, **_open_args(str(link)))  # type: ignore[arg-type]
    _check(created_link is False and _id(via_link) == _id(first), "a symlinked spelling opens the same canonical path")


def _case_open_refusals(root: Path) -> None:
    print("open: relative, missing, file and root paths and unknown kinds are refused")
    store = _store()
    path = _make_dir(root, "refused")
    bad_paths = (("relative/dir", "a relative path"), (str(root / "missing"), "a missing directory"), (str(path / "scratch.txt"), "a file"), ("/", "the filesystem root"))
    for bad, label in bad_paths:
        _raises(InvalidPathError, lambda bad=bad: open_folder(store, **_open_args(bad)), f"{label} is invalid_path")  # type: ignore[arg-type]
    _raises(InvalidFieldError, lambda: open_folder(store, **_open_args(str(path), harvest_kind="bogus")), "open with an unknown harvest_kind is refused")  # type: ignore[arg-type]
    _check(store.rows(WORK_FOLDER_NAMESPACE, TABLE_WORK_FOLDER) == [], "a refused open wrote nothing")


def _case_list_paging(root: Path) -> None:
    print("list: bounded pages, filters")
    store = _store()
    rows = [_open(store, _make_dir(root, f"many-{index}"), bound=f"many-{index}") for index in range(6)]
    parent = str(root.resolve())
    page = list_folders(store, parent_dir=parent, limit=2)
    _check(len(page.rows) == 2 and page.next_after is not None, "list returns a bounded page with a continuation")
    seen = list(page.rows)
    while page.next_after is not None:
        page = list_folders(store, parent_dir=parent, limit=2, after=page.next_after)
        seen.extend(page.rows)
    _check(len(seen) == 6 and len({_id(r) for r in seen}) == 6, "paging by the continuation visits every row once")
    close(store, folder_id=_id(rows[0]), expected_version=1, actor=ACTOR)
    _check([r["state"] for r in list_folders(store, state="due", limit=10).rows] == ["due"], "list filters by state")
    _check(len(list_folders(store, state=["open", "due"], limit=10).rows) == 6, "list filters by a set of states")
    bound_rows = list_folders(store, bound_kind="release_run", bound_ref="many-3", limit=10).rows
    _check(len(bound_rows) == 1 and bound_rows[0]["bound_ref"] == "many-3", "list filters by the bound pair")
    _check(list_folders(store, parent_dir="/nowhere", limit=10).rows == [], "list of an unknown parent is empty")
    _raises(InvalidFieldError, lambda: list_folders(store, limit=101), "a list limit over the cap is a caller error")
    _raises(InvalidFieldError, lambda: list_folders(store, state="bogus", limit=10), "an unknown state filter is refused")


def _case_history_paging(root: Path) -> None:
    print("history: bounded pages, oldest first, no gap")
    store = _store()
    row = _open(store, _make_dir(root, "hist"))
    for _ in range(3):
        current = get(store, folder_id=_id(row))
        assert current is not None
        retired = retire(store, folder_id=_id(row), expected_version=_version(current), actor=ACTOR)
        restore(store, folder_id=_id(retired), expected_version=_version(retired), actor=ACTOR)
    page = history(store, folder_id=_id(row), limit=3)
    _check(len(page.rows) == 3 and page.next_after is not None, "history returns a bounded page")
    collected = list(page.rows)
    while page.next_after is not None:
        page = history(store, folder_id=_id(row), limit=3, after=page.next_after)
        collected.extend(page.rows)
    _check([int(str(e["ordinal"])) for e in collected] == list(range(1, 8)), "history pages by ordinal, oldest first, with no gap")
    _raises(UnknownFolderError, lambda: history(store, folder_id="wkf-missing", limit=3), "history of an unknown folder raises unknown_folder")


def _case_wiring() -> None:
    print("wiring: store guard, schema registration, prefixes")
    store = _store()
    _check(require_work_folder_store(store) is store, "the guard accepts an object with the store surface")
    _raises(FrameworkError, lambda: require_work_folder_store(object()), "the guard fails loud on an object without it")
    registered = {schema.namespace: schema for schema in CoreSchemaDefinitions.get_all_core_schemas() if TABLE_WORK_FOLDER in schema.tables}
    _check(WORK_FOLDER_NAMESPACE in registered, "the work folder schema is listed in get_all_core_schemas")
    schema = get_work_folder_schema()
    _check(schema.tables[TABLE_WORK_FOLDER].id_prefix == "wkf" and schema.tables[TABLE_WORK_FOLDER_EVENT].id_prefix == "wfe", "the table prefixes are wkf and wfe")
    _check(work_folder_sweep.__name__.endswith("work_folder_sweep"), "the sweep module is importable on its own")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="work-folder-smoke-") as raw:
        base = Path(raw).resolve()
        for index, case in enumerate(
            (
                _case_open_row_shape,
                _case_full_lifecycle,
                _case_deleted_is_terminal,
                _case_regenerable_and_unharvested,
                _case_stale_and_correct,
                _case_reopen_retire_restore,
                _case_substituted_directory,
                _case_no_custody_and_absent,
                _case_failed_removal,
                _case_invalid_now,
                _case_unsafe_rmtree_and_limit,
                _case_open_idempotent,
                _case_open_refusals,
                _case_list_paging,
                _case_history_paging,
            )
        ):
            root = base / f"case-{index}"
            root.mkdir()
            case(root)
        _case_wiring()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
