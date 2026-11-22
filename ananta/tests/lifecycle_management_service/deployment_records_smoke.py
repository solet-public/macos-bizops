#!/usr/bin/env python3
"""Deployment record store smoke (no pytest).

Proves the solet's own record of a deployment over the state interface:

* record creates one row + one ``recorded`` event (ordinal 1); the same
  (solet, sha, release) again is the same row (idempotent, ``created`` False);
* a second sha is a second row; ``current`` is the newest ``deployed_at`` among
  ``recorded`` rows and skips withdrawn ones;
* withdraw / reinstate move ``state`` by compare-and-set on ``version``; a stale
  ``expected_version`` raises ``StaleVersionError`` and writes nothing; every
  transition appends one event (append-only history, readable via ``events``);
* ``history`` lists newest first, including withdrawn rows, and pages by ``after``;
* a 39-character sha raises ``InvalidShaError``; naive and aware datetimes both
  store as UTC; the schema is listed in ``get_all_core_schemas``.

Offline: the shared real-shape state fake, schema-enforced for both tables.

Run from repo root:
    PYTHONPATH=src .venv/bin/python ananta/tests/lifecycle_management_service/deployment_records_smoke.py
"""

from __future__ import annotations

import sys
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
from ananta.services.lifecycle_management_service.deployment_record_schema import (  # noqa: E402
    DEPLOYMENT_RECORD_NAMESPACE,
    TABLE_DEPLOYMENT_RECORD,
    TABLE_DEPLOYMENT_RECORD_EVENT,
    get_deployment_record_schema,
)
from ananta.services.lifecycle_management_service.deployment_records import (  # noqa: E402
    Actor,
    InvalidShaError,
    InvalidTriggerError,
    StaleVersionError,
    UnknownDeploymentError,
    current,
    events,
    get,
    history,
    record,
    set_withdrawn,
)

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
    schema = get_deployment_record_schema()
    for table in (TABLE_DEPLOYMENT_RECORD, TABLE_DEPLOYMENT_RECORD_EVENT):
        fake._enforced_columns[table] = (  # noqa: SLF001 — smoke wiring
            frozenset(schema.tables[table].columns) | _STANDARDIZER_COLUMNS
        )
    return fake


ACTOR = Actor(instance="agi-test", model="claude-sonnet-5-5", effort="medium")
SOLET = "demo-solet"
SHA_A = "a" * 40
SHA_B = "b" * 40
T0 = datetime(2026, 11, 13, 12, 0, 0, tzinfo=UTC)


def _record(
    store: RealShapeState,
    *,
    sha: str = SHA_A,
    release: str = "rel-1",
    at: datetime = T0,
) -> tuple[dict[str, object], bool]:
    return record(
        store,
        solet_name=SOLET,
        release_id=release,
        git_sha=sha,
        deployed_at=at,
        trigger="landing",
        manifest_sha256="m" * 64,
        actor=ACTOR,
    )


def _rows(store: RealShapeState, table: str) -> list[dict[str, object]]:
    return store.rows(DEPLOYMENT_RECORD_NAMESPACE, table)


def _case_record(store: RealShapeState) -> str:
    print("record: create and idempotent repeat")
    row, created = _record(store)
    deployment_id = str(row["deployment_id"])
    _check(created is True, "first record reports created")
    _check(deployment_id.startswith("dpr-"), "deployment_id carries the dpr prefix")
    _check(row["state"] == "recorded" and row["version"] == 1, "row is recorded at version 1")
    _check(row["recorded_by_instance"] == "agi-test", "actor identity is recorded")
    evs = events(store, deployment_id=deployment_id)
    _check(len(evs) == 1, "one event after the first record")
    _check(
        evs[0]["ordinal"] == 1 and evs[0]["kind"] == "recorded" and evs[0]["prior_version"] == 0,
        "first event is ordinal 1, kind recorded, prior_version 0",
    )
    again, created_again = _record(store)
    _check(created_again is False, "same (solet, sha, release) reports created False")
    _check(again["deployment_id"] == deployment_id, "the repeat returns the same row")
    _check(len(_rows(store, TABLE_DEPLOYMENT_RECORD)) == 1, "still one row after the repeat")
    _check(len(_rows(store, TABLE_DEPLOYMENT_RECORD_EVENT)) == 1, "still one event after the repeat")

    return deployment_id


def _case_current(store: RealShapeState, first_id: str) -> dict[str, object]:
    print("record: second sha, current")
    row_b, created_b = _record(store, sha=SHA_B, release="rel-2", at=T0 + timedelta(hours=1))
    _check(created_b is True and row_b["deployment_id"] != first_id, "a second sha is a second row")
    newest = current(store, solet_name=SOLET)
    _check(newest is not None and newest["git_sha"] == SHA_B, "current is the newest deployed_at")
    _check(current(store, solet_name="other") is None, "current is None for an unknown solet")

    return row_b


def _case_withdraw(store: RealShapeState, row_b: dict[str, object]) -> None:
    print("withdraw: stale refused, then CAS, fallback, reinstate")
    _raises(
        StaleVersionError,
        lambda: set_withdrawn(
            store,
            deployment_id=str(row_b["deployment_id"]),
            withdrawn=True,
            expected_version=7,
            reason="stale",
            actor=ACTOR,
        ),
        "withdraw with a stale expected_version raises StaleVersionError",
    )
    _check(
        get(store, deployment_id=str(row_b["deployment_id"]))["state"] == "recorded"  # type: ignore[index]
        and len(events(store, deployment_id=str(row_b["deployment_id"]))) == 1,
        "the stale write changed no row and wrote no event",
    )
    withdrawn = set_withdrawn(
        store,
        deployment_id=str(row_b["deployment_id"]),
        withdrawn=True,
        expected_version=1,
        reason="wrong sha",
        actor=ACTOR,
    )
    _check(withdrawn["state"] == "withdrawn" and withdrawn["version"] == 2, "withdraw sets state and bumps version")
    fallback = current(store, solet_name=SOLET)
    _check(fallback is not None and fallback["git_sha"] == SHA_A, "current falls back to the older row")
    _raises(
        StaleVersionError,
        lambda: set_withdrawn(
            store,
            deployment_id=str(row_b["deployment_id"]),
            withdrawn=False,
            expected_version=1,
            reason="reuse old version",
            actor=ACTOR,
        ),
        "a reused version is refused after the row moved on",
    )
    reinstated = set_withdrawn(
        store,
        deployment_id=str(row_b["deployment_id"]),
        withdrawn=False,
        expected_version=2,
        reason="it was right",
        actor=ACTOR,
    )
    _check(reinstated["state"] == "recorded" and reinstated["version"] == 3, "reinstate returns it")
    restored = current(store, solet_name=SOLET)
    _check(restored is not None and restored["git_sha"] == SHA_B, "current sees the reinstated row")
    kinds = [(e["ordinal"], e["kind"], e["prior_version"]) for e in events(store, deployment_id=str(row_b["deployment_id"]))]
    _check(
        kinds == [(1, "recorded", 0), (2, "withdrawn", 1), (3, "reinstated", 2)],
        "events are append-only: recorded, withdrawn, reinstated",
    )
    _raises(
        UnknownDeploymentError,
        lambda: set_withdrawn(
            store,
            deployment_id="dpr-missing",
            withdrawn=True,
            expected_version=1,
            reason="x",
            actor=ACTOR,
        ),
        "withdraw of an unknown deployment raises UnknownDeploymentError",
    )
    _check(get(store, deployment_id="dpr-missing") is None, "get of an unknown id is None")


def _case_history(store: RealShapeState, row_b: dict[str, object]) -> None:
    print("history: newest first, withdrawn included, paged by after")
    set_withdrawn(
        store,
        deployment_id=str(row_b["deployment_id"]),
        withdrawn=True,
        expected_version=3,
        reason="again",
        actor=ACTOR,
    )
    _record(store, sha="c" * 40, release="rel-3", at=T0 + timedelta(hours=2))
    page1 = history(store, solet_name=SOLET, limit=2)
    _check([r["git_sha"] for r in page1.rows] == ["c" * 40, SHA_B], "page 1 is newest first and includes the withdrawn row")
    _check(page1.next_after is not None, "page 1 offers a cursor")
    page2 = history(store, solet_name=SOLET, limit=2, after=page1.next_after)
    _check([r["git_sha"] for r in page2.rows] == [SHA_A], "page 2 continues after the cursor")
    _check(page2.next_after is None, "the last page has no cursor")


def _case_validation(store: RealShapeState) -> None:
    print("validation: sha, trigger, timezone")
    _raises(InvalidShaError, lambda: _record(store, sha="a" * 39), "a 39-character sha raises InvalidShaError")
    _raises(InvalidShaError, lambda: _record(store, sha="A" * 40), "an uppercase sha raises InvalidShaError")
    _raises(
        InvalidTriggerError,
        lambda: record(
            store,
            solet_name=SOLET,
            release_id="r",
            git_sha=SHA_A,
            deployed_at=T0,
            trigger="manual",
            manifest_sha256="m",
            actor=ACTOR,
        ),
        "an unknown trigger raises InvalidTriggerError",
    )
    tz_store = _store()
    naive, _ = _record(tz_store, sha="d" * 40, release="n", at=datetime(2026, 11, 13, 9, 0, 0))
    aware, _ = _record(
        tz_store,
        sha="e" * 40,
        release="a",
        at=datetime(2026, 11, 13, 2, 0, 0, tzinfo=timezone(timedelta(hours=-7))),
    )
    _check(
        str(naive["deployed_at"]).startswith("2026-11-13T09:00:00") and str(aware["deployed_at"]).startswith("2026-11-13T09:00:00"),
        "naive and aware datetimes both store as the same UTC instant",
    )


def _case_schema() -> None:
    print("schema registration")
    namespaces = {(schema.namespace, name) for schema in CoreSchemaDefinitions.get_all_core_schemas() for name in schema.tables}
    _check(
        (DEPLOYMENT_RECORD_NAMESPACE, TABLE_DEPLOYMENT_RECORD) in namespaces and (DEPLOYMENT_RECORD_NAMESPACE, TABLE_DEPLOYMENT_RECORD_EVENT) in namespaces,
        "both tables are listed in get_all_core_schemas",
    )


def main() -> int:
    print("deployment record smoke")
    store = _store()
    first_id = _case_record(store)
    row_b = _case_current(store, first_id)
    _case_withdraw(store, row_b)
    _case_history(store, row_b)
    _case_validation(store)
    _case_schema()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
