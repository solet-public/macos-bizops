#!/usr/bin/env python3
"""Work-folder verbs on the lifecycle service (no pytest).

Drives the REAL ``LifecycleManagementService`` verbs ``open_work_folder``,
``read_work_folders``, ``transition_work_folder`` and ``sweep_work_folders`` over
the schema-enforced real-shape state fake (the store the service reaches through
``orchestrator.get_service("state_service")``) and real temporary directories.
Pins:

* open twice for one path: one row, the same ``folder_id``, one stored row and
  one ``opened`` event; a path whose record changed identity comes back with one
  ``identity_changed`` advisory and an unchanged record;
* open -> close -> harvest_record -> sweep(apply) leaves the directory gone, the
  row ``deleted`` and the history ``opened, closed, harvested, deleted``; a dry
  run (the default) lists the folder and removes nothing; a sweep ``limit``
  defers the rest;
* reopen clears the current receipt while the history keeps it; retire then
  restore; correct changes harvest_kind and owner and a correction that changes
  nothing is refused; a deleted row accepts nothing and a new open makes a new row;
* a stale ``expected_version`` is refused ``stale_version`` and writes nothing; an
  unknown id is ``unknown_folder``; a transition outside the table is
  ``invalid_transition``; bad paths, bounds, fields, actions, modes and cursors
  are refused with their code; no store is ``state_service_unavailable``;
* read: show, list (filters, ordering, paging by ``next``) and history (paging);
* each verb's returned keys equal the keys its decorator's ``return_value_schema``
  declares and its KB JSON's ``result_description`` names; the decorator
  parameters and defaults equal the implementation's; the decorator enums equal
  the store's constants.

Each case function is small on purpose: every assertion is one comparison, so
the cyclomatic-complexity gate measures the structure, not the number of checks.

Run from repo root:
    PYTHONPATH=src .venv/bin/python ananta/tests/lifecycle_management_service/work_folder_verbs_smoke.py
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "tests"))

from _real_state_fake import (  # noqa: E402
    _STANDARDIZER_COLUMNS,
    RealShapeState,
)
from ananta.services.lifecycle_management_service.interfaces.public import (  # noqa: E402
    LifecycleManagementAPI,
)
from ananta.services.lifecycle_management_service.service import (  # noqa: E402
    LifecycleManagementService,
)
from ananta.services.lifecycle_management_service.work_folder_schema import (  # noqa: E402
    HARVEST_KINDS,
    STATES,
    TABLE_WORK_FOLDER,
    TABLE_WORK_FOLDER_EVENT,
    WORK_FOLDER_NAMESPACE,
    get_work_folder_schema,
)
from ananta.services.lifecycle_management_service.work_folder_sweep import SWEEP_ACTOR  # noqa: E402

KB_DIR = REPO_ROOT / "ananta" / "knowledge_base" / "processes" / "lifecycle_management_service"
PROVIDER_KEY = "service_interface::lifecycle_management_service::"
VERBS = ("open_work_folder", "read_work_folders", "transition_work_folder", "sweep_work_folders")
# The platform overwrites any declared parameter with one of these names with its own value before the verb runs
# (core/actions/action_processor.py, _inject_session_context), so a verb must not name a caller parameter so.
INJECTED_PARAMETERS = {"state", "session_id", "flow_id"}
ACTOR: dict[str, Any] = {"agent_instance_id": "agi-test", "agent_session_id": "ases-test", "actor_model": "claude-sonnet-5-5", "actor_effort": "max"}
BOUND = "release_run:run-1"
RECEIPT: dict[str, Any] = {"kind": "evidence_copy", "path": "/evidence/run-1", "manifest_sha256": "f" * 64}
ROW_KEYS = {"folder_id", "path", "state", "version", "dev", "ino", "purpose", "bound_kind", "bound_ref", "harvest_kind", "owner", "harvest_receipt", "advisories"}
EVENT_KEYS = {"folder_id", "ordinal", "kind", "prior_version", "actor_instance", "actor_session", "actor_model", "actor_effort", "detail", "recorded_at"}
ACTIONS = {"close", "reopen", "correct", "harvest_record", "retire", "restore"}
READ_KEYS = {"folders", "next"}
REFUSAL_CODES: dict[str, tuple[str, ...]] = {
    "open_work_folder": ("invalid_path", "invalid_argument", "invalid_field", "state_service_unavailable"),
    "read_work_folders": ("invalid_argument", "invalid_field", "unknown_folder", "state_service_unavailable"),
    "transition_work_folder": ("invalid_argument", "stale_version", "unknown_folder", "invalid_transition", "invalid_field", "state_service_unavailable"),
    "sweep_work_folders": ("invalid_field", "unknown_folder", "state_service_unavailable"),
}

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


class _Orchestrator:
    def __init__(self, state: object | None) -> None:
        self._state = state

    def get_service(self, name: str) -> object | None:
        return self._state if name == "state_service" else None


def _store() -> RealShapeState:
    fake = RealShapeState()
    schema = get_work_folder_schema()
    for table in (TABLE_WORK_FOLDER, TABLE_WORK_FOLDER_EVENT):
        fake._enforced_columns[table] = frozenset(schema.tables[table].columns) | _STANDARDIZER_COLUMNS  # noqa: SLF001 — smoke wiring
    return fake


def _service(state: object | None) -> LifecycleManagementService:
    return LifecycleManagementService(_Orchestrator(state))


def _make_dir(base: Path, name: str) -> Path:
    path = base / name
    path.mkdir()
    (path / "scratch.txt").write_text("scratch", encoding="utf-8")
    return path


def _open(svc: LifecycleManagementService, path: Path, *, harvest: str = "evidence_copy", bound: str = BOUND) -> dict[str, Any]:
    return svc.open_work_folder(path=str(path), purpose="release scratch", bound=bound, harvest=harvest, owner="ases-test", **ACTOR)


def _move(svc: LifecycleManagementService, row: dict[str, Any], action: str, **extra: Any) -> dict[str, Any]:
    return svc.transition_work_folder(folder_id=row["folder_id"], action=action, expected_version=row["version"], reason=f"smoke {action}", **ACTOR, **extra)


def _history(svc: LifecycleManagementService, folder_id: str, **extra: Any) -> list[dict[str, Any]]:
    return svc.read_work_folders(mode="history", folder_id=folder_id, **extra)["data"]["folders"]


def _show(svc: LifecycleManagementService, folder_id: str) -> dict[str, Any]:
    return svc.read_work_folders(mode="show", folder_id=folder_id)["data"]["folders"][0]


def _ids(rows: list[dict[str, Any]]) -> list[str]:
    return [row["folder_id"] for row in rows]


def _kinds(events: list[dict[str, Any]]) -> list[str]:
    return [event["kind"] for event in events]


def _refused(result: dict[str, Any], code: str, label: str) -> None:
    _check(
        (result.get("action_status"), (result.get("data") or {}).get("error_code"), str(result.get("error", "")).startswith(code)) == ("error", code, True),
        label,
    )


def _metadata(verb: str) -> Any:
    return getattr(LifecycleManagementAPI, verb)._service_interface_metadata  # noqa: SLF001


def _declared_keys(verb: str) -> set[str]:
    return set(_metadata(verb).return_value_schema.properties)


def _enum(verb: str, parameter: str) -> list[str]:
    return list(_metadata(verb).parameters[parameter].validation["enum"])


def _kb_json(verb: str) -> dict[str, Any]:
    return json.loads((KB_DIR / f"{verb}.json").read_text())


def _case_open_row(base: Path) -> None:
    print("open_work_folder: create and repeat")
    store = _store()
    svc = _service(store)
    path = _make_dir(base, "open-a")
    first = _open(svc, path)
    row = first["data"]
    info = os.lstat(path)
    _check((first["action_status"], set(row), _declared_keys("open_work_folder")) == ("completed", ROW_KEYS, ROW_KEYS), "open returns the row keys the return_value_schema declares")
    _check((row["state"], row["version"], row["harvest_receipt"], row["advisories"]) == ("open", 1, "", []), "a new row is open at version 1 with no receipt and no advisory")
    recorded = (row["path"], row["bound_kind"], row["bound_ref"], row["harvest_kind"], row["owner"])
    _check(recorded == (str(path.resolve()), "release_run", "run-1", "evidence_copy", "ases-test"), "the path is canonical and the bound, harvest kind and owner are recorded")
    _check((row["dev"], row["ino"]) == (str(info.st_dev), str(info.st_ino)), "dev and ino are the directory identity as decimal text")
    again = _open(svc, path)["data"]
    _check((again["folder_id"], again["version"], again["advisories"]) == (row["folder_id"], 1, []), "opening a live path again returns the same row")
    stored = (len(store.rows(WORK_FOLDER_NAMESPACE, TABLE_WORK_FOLDER)), len(store.rows(WORK_FOLDER_NAMESPACE, TABLE_WORK_FOLDER_EVENT)))
    _check(stored == (1, 1), "one row and one opened event are stored")


def _case_open_defaults_and_refusals(base: Path) -> None:
    print("open_work_folder: defaults and refusals")
    svc = _service(_store())
    path = _make_dir(base, "open-bare")
    bare = svc.open_work_folder(path=str(path), purpose="bare", bound="session:s-1")["data"]
    _check((bare["harvest_kind"], bare["owner"]) == ("regenerable", ""), "harvest defaults to regenerable and the owner is optional")
    _check([(e["kind"], e["actor_instance"]) for e in _history(svc, bare["folder_id"])] == [("opened", "")], "an open without identity records an empty actor and is not refused")
    _refused(svc.open_work_folder(path="relative/dir", purpose="p", bound=BOUND), "invalid_path", "a relative path is refused invalid_path")
    _refused(svc.open_work_folder(path=str(base / "no-such-dir"), purpose="p", bound=BOUND), "invalid_path", "a missing directory is refused invalid_path")
    _refused(svc.open_work_folder(path=str(path), purpose="p", bound="no-colon"), "invalid_argument", "a bound without kind:ref is refused invalid_argument")
    _refused(svc.open_work_folder(path=str(path), purpose="p", bound=BOUND, harvest="bogus"), "invalid_field", "an unknown harvest kind is refused invalid_field")


def _case_open_identity(base: Path) -> None:
    print("open_work_folder: a live path whose recorded identity changed")
    store = _store()
    svc = _service(store)
    path = _make_dir(base, "open-identity")
    row = _open(svc, path)["data"]
    store.update_state(WORK_FOLDER_NAMESPACE, {"table": TABLE_WORK_FOLDER, "filters": {"folder_id": row["folder_id"]}}, {"ino": "1"})
    moved = _open(svc, path)["data"]
    advisories = [(a["code"], a["folder_id"], a["path"]) for a in moved["advisories"]]
    _check(advisories == [("identity_changed", row["folder_id"], row["path"])], "a live path whose recorded identity changed comes back with one identity_changed advisory")
    _check((moved["folder_id"], moved["ino"], moved["version"]) == (row["folder_id"], "1", 1), "the existing record is returned unchanged, not rewritten")
    _check(_show(svc, row["folder_id"])["advisories"] == [], "a row read back carries no advisory")


def _case_lifecycle_guards(base: Path) -> None:
    print("transition_work_folder: stale, unknown, invalid action, receipt refusals")
    svc = _service(_store())
    row = _open(svc, _make_dir(base, "life-guards"))["data"]
    stale = svc.transition_work_folder(folder_id=row["folder_id"], action="close", expected_version=7, reason="stale", **ACTOR)
    _refused(stale, "stale_version", "a stale expected_version is refused stale_version")
    untouched = (_kinds(_history(svc, row["folder_id"])), _show(svc, row["folder_id"])["version"])
    _check(untouched == (["opened"], 1), "the refused write added no event and moved no version")
    _refused(svc.transition_work_folder(folder_id="wkf-missing", action="close", expected_version=1, reason="x", **ACTOR), "unknown_folder", "an unknown folder id is refused unknown_folder")
    _refused(svc.transition_work_folder(folder_id=row["folder_id"], action="bogus", expected_version=1), "invalid_argument", "an unknown action is refused invalid_argument")
    closed = _move(svc, row, "close")["data"]
    _check((set(closed), _declared_keys("transition_work_folder")) == (ROW_KEYS, ROW_KEYS), "transition returns the row keys the return_value_schema declares")
    _check((closed["state"], closed["version"]) == ("due", 2), "close moves open to due at version 2")
    _refused(_move(svc, closed, "harvest_record"), "invalid_field", "harvesting an evidence_copy folder without a receipt is refused invalid_field")
    _refused(_move(svc, closed, "harvest_record", receipt_json="not json"), "invalid_field", "a receipt that is not JSON is refused invalid_field")
    _refused(_move(svc, closed, "harvest_record", receipt_json="[1]"), "invalid_field", "a receipt that is not a JSON object is refused invalid_field")
    _refused(_move(svc, closed, "harvest_record", receipt_json="{}"), "invalid_field", "an empty receipt is refused invalid_field")
    harvested = _move(svc, closed, "harvest_record", receipt_json=json.dumps(RECEIPT))["data"]
    stored = (harvested["state"], harvested["version"], json.loads(harvested["harvest_receipt"]))
    _check(stored == ("harvested", 3, RECEIPT), "harvest_record stores the receipt and moves due to harvested")


def _case_lifecycle_sweep(base: Path) -> None:
    print("sweep_work_folders: open, close, harvest_record, sweep (dry run, apply), history, terminal")
    svc = _service(_store())
    path = _make_dir(base, "life-sweep")
    row = _open(svc, path)["data"]
    closed = _move(svc, row, "close")["data"]
    _move(svc, closed, "harvest_record", receipt_json=json.dumps(RECEIPT))
    dry = svc.sweep_work_folders()["data"]
    _check(set(dry) == _declared_keys("sweep_work_folders"), "sweep returns the keys the return_value_schema declares")
    listed = [(c["folder_id"], c["action"], c["outcome"]) for c in dry["candidates"]]
    _check((path.is_dir(), listed, dry["counts"]["deleted"], dry["aborted"]) == (True, [(row["folder_id"], "remove", "would_remove")], 0, False), "a dry run (the default) lists the folder and removes nothing")
    applied = svc.sweep_work_folders(apply=True)["data"]
    _check((path.exists(), applied["counts"]["deleted"], [c["outcome"] for c in applied["candidates"]], applied["aborted"]) == (False, 1, ["deleted"], False), "apply removes the directory and counts one deletion")
    events = _history(svc, row["folder_id"])
    _check((_kinds(events), [e["ordinal"] for e in events]) == (["opened", "closed", "harvested", "deleted"], [1, 2, 3, 4]), "history shows opened, closed, harvested, deleted in order")
    _check((all(set(e) == EVENT_KEYS for e in events), events[0]["actor_instance"], events[3]["actor_instance"]) == (True, "agi-test", SWEEP_ACTOR.instance), "history rows carry the event keys and the actor of each change")
    gone = _show(svc, row["folder_id"])
    _check((gone["state"], gone["version"]) == ("deleted", 4), "the row is kept, deleted, at version 4")
    _refused(_move(svc, gone, "reopen"), "invalid_transition", "a deleted folder accepts no transition (terminal)")
    path.mkdir()
    fresh = _open(svc, path)["data"]
    _check((fresh["folder_id"] != row["folder_id"], fresh["version"]) == (True, 1), "opening the same path again after deletion makes a new row")


def _case_reversal(base: Path) -> None:
    print("transition_work_folder: reopen, retire, restore, correct")
    svc = _service(_store())
    regenerable = _open(svc, _make_dir(base, "rev-a"), harvest="regenerable")["data"]
    due = _move(svc, regenerable, "close")["data"]
    back = _move(svc, due, "reopen")["data"]
    _check((due["state"], back["state"], back["version"]) == ("due", "open", 3), "reopen after close returns state open")
    _refused(_move(svc, back, "reopen"), "invalid_transition", "reopening an open folder is refused invalid_transition")
    evidence = _open(svc, _make_dir(base, "rev-b"))["data"]
    evidence = _move(svc, evidence, "close")["data"]
    evidence = _move(svc, evidence, "harvest_record", receipt_json=json.dumps(RECEIPT))["data"]
    reopened = _move(svc, evidence, "reopen")["data"]
    _check((reopened["state"], reopened["harvest_receipt"]) == ("open", ""), "reopen after harvest clears the current receipt")
    kept = [json.loads(e["detail"])["receipt"] for e in _history(svc, evidence["folder_id"]) if e["kind"] == "harvested"]
    _check(kept == [RECEIPT], "the history still holds the receipt of the harvested event")
    retired = _move(svc, reopened, "retire")["data"]
    _check(retired["state"] == "retired", "retire moves an open folder to retired")
    _refused(_move(svc, retired, "close"), "invalid_transition", "a retired folder accepts only restore")
    restored = _move(svc, retired, "restore")["data"]
    _check(restored["state"] == "open", "restore moves a retired folder back to open")
    _refused(_move(svc, restored, "restore"), "invalid_transition", "restoring a folder that is not retired is refused invalid_transition")
    corrected = _move(svc, restored, "correct", harvest_kind="custody_packet", owner="ases-new")["data"]
    _check((corrected["harvest_kind"], corrected["owner"], corrected["version"]) == ("custody_packet", "ases-new", restored["version"] + 1), "correct changes harvest_kind and owner and raises the version by one")
    last = _history(svc, corrected["folder_id"])[-1]
    _check((last["kind"], json.loads(last["detail"])["changes"]["harvest_kind"]) == ("corrected", ["evidence_copy", "custody_packet"]), "the corrected event keeps the old and the new value")
    _refused(_move(svc, corrected, "correct", harvest_kind="custody_packet"), "invalid_field", "a correction that changes nothing is refused invalid_field")
    _refused(_move(svc, corrected, "correct", harvest_kind="bogus"), "invalid_field", "an unknown harvest kind is refused invalid_field")


def _case_sweep(base: Path) -> None:
    print("sweep_work_folders: regenerable folders, limit, open folders untouched")
    svc = _service(_store())
    paths = [_make_dir(base, f"sweep-{n}") for n in range(3)]
    for path in paths:
        _move(svc, _open(svc, path, harvest="regenerable")["data"], "close")
    untouched = _make_dir(base, "sweep-open")
    _open(svc, untouched, harvest="regenerable")
    _refused(svc.sweep_work_folders(limit=0), "invalid_field", "a limit below 1 is refused invalid_field")
    limited = svc.sweep_work_folders(apply=True, limit=1)["data"]
    taken = ([c["action"] for c in limited["candidates"]], len(limited["deferred"]), limited["counts"]["marked_harvested"], limited["counts"]["deleted"])
    _check(taken == (["harvest_and_remove"], 2, 1, 1), "a closed regenerable folder is harvested and removed, and a limit of 1 defers the rest")
    rest = svc.sweep_work_folders(apply=True)["data"]
    _check((rest["counts"]["deleted"], rest["deferred"], any(p.exists() for p in paths)) == (2, [], False), "the next run takes the deferred folders")
    _check((untouched.is_dir(), svc.sweep_work_folders()["data"]["candidates"]) == (True, []), "an open folder is never a candidate and is never removed")


def _case_sweep_one(base: Path) -> None:
    print("sweep_work_folders: folder_id limits the run to one folder")
    svc = _service(_store())
    paths = [_make_dir(base, f"one-{n}") for n in range(3)]
    rows = [_move(svc, _open(svc, path, harvest="regenerable")["data"], "close")["data"] for path in paths]
    chosen = rows[1]["folder_id"]
    dry = svc.sweep_work_folders(folder_id=chosen)["data"]
    _check(([c["folder_id"] for c in dry["candidates"]], [p.is_dir() for p in paths]) == ([chosen], [True, True, True]), "a dry run with folder_id lists only that folder and removes nothing")
    applied = svc.sweep_work_folders(apply=True, folder_id=chosen)["data"]
    _check((applied["counts"]["deleted"], [p.exists() for p in paths]) == (1, [True, False, True]), "apply with folder_id removes only that folder")
    _check([_show(svc, row["folder_id"])["state"] for row in rows] == ["due", "deleted", "due"], "the other two rows are still due")
    still_open = _open(svc, _make_dir(base, "one-open"), harvest="regenerable")["data"]
    _check(svc.sweep_work_folders(apply=True, folder_id=still_open["folder_id"])["data"]["candidates"] == [], "the id of an open folder gives no candidates")
    _refused(svc.sweep_work_folders(folder_id="wkf-missing"), "unknown_folder", "an unknown folder_id is refused unknown_folder")
    _refused(svc.sweep_work_folders(folder_id=chosen, limit=0), "invalid_field", "a limit below 1 with a folder_id is still refused invalid_field")


def _read_fixture(base: Path, group: str) -> tuple[LifecycleManagementService, dict[str, dict[str, Any]], str]:
    """Three regenerable folders under one parent: two bound to unit:u-1 (one of them due), one to unit:u-2."""
    svc = _service(_store())
    parent = base / group
    parent.mkdir()
    bounds = {"r-a": "unit:u-1", "r-b": "unit:u-1", "r-c": "unit:u-2"}
    rows = {name: _open(svc, _make_dir(parent, name), harvest="regenerable", bound=bound)["data"] for name, bound in bounds.items()}
    _move(svc, rows["r-b"], "close")
    return svc, rows, str(parent.resolve())


def _case_read_list(base: Path) -> None:
    print("read_work_folders: list with filters and paging")
    svc, rows, where = _read_fixture(base, "read-list")
    listed = svc.read_work_folders(mode="list", parent_dir=where)["data"]
    _check((set(listed), _declared_keys("read_work_folders")) == (READ_KEYS, READ_KEYS), "read returns the keys the return_value_schema declares")
    shape = ([r["path"] for r in listed["folders"]], all(set(r) == ROW_KEYS for r in listed["folders"]), listed["next"])
    _check(shape == (sorted(r["path"] for r in rows.values()), True, None), "list returns every folder under the parent in path order with the row keys")
    by_bound = svc.read_work_folders(mode="list", parent_dir=where, bound="unit:u-1")["data"]["folders"]
    _check(sorted(_ids(by_bound)) == sorted([rows["r-a"]["folder_id"], rows["r-b"]["folder_id"]]), "list filters by bound kind:ref")
    by_state = svc.read_work_folders(mode="list", state_filter="due")["data"]["folders"]
    _check(_ids(by_state) == [rows["r-b"]["folder_id"]], "list filters by state")
    page1 = svc.read_work_folders(mode="list", parent_dir=where, limit=2)["data"]
    page2 = svc.read_work_folders(mode="list", parent_dir=where, limit=2, after=page1["next"])["data"]
    _check((len(page1["folders"]), isinstance(page1["next"], str), len(page2["folders"]), page2["next"]) == (2, True, 1, None), "list pages by limit and next, and the last page ends the list")
    _check(_ids(page1["folders"] + page2["folders"]) == _ids(listed["folders"]), "the pages together equal the unpaged list")
    _refused(svc.read_work_folders(mode="list", limit=101), "invalid_field", "a limit above 100 is refused invalid_field, not clamped")
    _refused(svc.read_work_folders(mode="list", limit=0), "invalid_field", "a limit below 1 is refused invalid_field")
    _refused(svc.read_work_folders(mode="list", state_filter="bogus"), "invalid_field", "an unknown state filter is refused invalid_field")
    _refused(svc.read_work_folders(mode="list", bound="no-colon"), "invalid_argument", "a bound filter without kind:ref is refused invalid_argument")
    _refused(svc.read_work_folders(mode="list", after="garbage"), "invalid_argument", "a malformed after is refused invalid_argument")
    _refused(svc.read_work_folders(mode="bogus"), "invalid_argument", "an unknown mode is refused invalid_argument")


def _case_read_show_and_history(base: Path) -> None:
    print("read_work_folders: show and history")
    svc, rows, _where = _read_fixture(base, "read-show")
    one = svc.read_work_folders(mode="show", folder_id=rows["r-a"]["folder_id"])["data"]
    _check((_ids(one["folders"]), one["next"]) == ([rows["r-a"]["folder_id"]], None), "show returns exactly one row")
    _refused(svc.read_work_folders(mode="show", folder_id="wkf-missing"), "unknown_folder", "show of an unknown id is refused unknown_folder")
    _refused(svc.read_work_folders(mode="show"), "invalid_argument", "show without an id is refused invalid_argument")
    folder_id = rows["r-b"]["folder_id"]
    first = svc.read_work_folders(mode="history", folder_id=folder_id, limit=1)["data"]
    second = svc.read_work_folders(mode="history", folder_id=folder_id, limit=1, after=first["next"])["data"]
    paged = (_kinds(first["folders"]), isinstance(first["next"], str), _kinds(second["folders"]), second["next"])
    _check(paged == (["opened"], True, ["closed"], None), "history pages oldest first by limit and next")
    _refused(svc.read_work_folders(mode="history", folder_id="wkf-missing"), "unknown_folder", "history of an unknown id is refused unknown_folder")
    _refused(svc.read_work_folders(mode="history"), "invalid_argument", "history without an id is refused invalid_argument")
    _refused(svc.read_work_folders(mode="history", folder_id=folder_id, after="x|wkf-1"), "invalid_argument", "a history cursor that is not an ordinal is refused invalid_argument")


def _case_no_store(base: Path) -> None:
    print("no state service: every verb refuses state_service_unavailable")
    svc = _service(None)
    _refused(svc.open_work_folder(path=str(base), purpose="p", bound=BOUND), "state_service_unavailable", "open without a store is refused, not silently empty")
    _refused(svc.read_work_folders(mode="list"), "state_service_unavailable", "read without a store is refused, not silently empty")
    _refused(svc.transition_work_folder(folder_id="wkf-x", action="close", expected_version=1), "state_service_unavailable", "transition without a store is refused")
    _refused(svc.sweep_work_folders(), "state_service_unavailable", "sweep without a store is refused, not an empty candidate list")


def _case_contract() -> None:
    print("decorators, implementation and KB JSON: one contract")
    for verb in VERBS:
        parameters = _metadata(verb).parameters
        signature = inspect.signature(getattr(LifecycleManagementService, verb)).parameters
        implemented = {name: p for name, p in signature.items() if name != "self"}
        _check(set(parameters) == set(implemented), f"{verb}: the decorator parameters equal the implementation's")
        _check(not INJECTED_PARAMETERS & set(parameters), f"{verb}: no declared parameter is one the platform injects, so a caller's value always reaches the verb")
        _check(all(parameters[name].required == (p.default is inspect.Parameter.empty) for name, p in implemented.items()), f"{verb}: required parameters are exactly the ones without a default")
        declared = inspect.signature(getattr(LifecycleManagementAPI, verb)).parameters
        _check(all(declared[name].default == p.default for name, p in implemented.items()), f"{verb}: the interface defaults equal the implementation's")
        doc = _kb_json(verb)
        _check(doc["process_key"] == PROVIDER_KEY + verb, f"{verb}: process_key equals the contract key")
        description = doc["result_processor_customizations"]["result_description"]
        _check(all(key in description for key in _declared_keys(verb)), f"{verb}: result_description names every return key")
        interpretation = doc["error_processor_customizations"]["error_interpretation"]
        _check(all(code in interpretation for code in REFUSAL_CODES[verb]), f"{verb}: error_interpretation names every refusal code")
    _check(_enum("open_work_folder", "harvest") == list(HARVEST_KINDS) == _enum("transition_work_folder", "harvest_kind"), "the harvest enums equal the store's harvest kinds")
    _check(_enum("read_work_folders", "state_filter") == list(STATES), "the state enum equals the store's states")
    _check(_enum("read_work_folders", "mode") == ["show", "list", "history"], "the mode enum names the three read modes")
    _check(set(_enum("transition_work_folder", "action")) == ACTIONS, "the action enum names the six transitions")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="work-folder-verbs-smoke-") as raw:
        base = Path(raw).resolve()
        _case_open_row(base)
        _case_open_defaults_and_refusals(base)
        _case_open_identity(base)
        _case_lifecycle_guards(base)
        _case_lifecycle_sweep(base)
        _case_reversal(base)
        _case_sweep(base)
        _case_sweep_one(base)
        _case_read_list(base)
        _case_read_show_and_history(base)
        _case_no_store(base)
    _case_contract()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
