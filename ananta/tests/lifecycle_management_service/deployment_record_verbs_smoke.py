#!/usr/bin/env python3
"""Deployment record verbs on the lifecycle service (no pytest).

Drives the REAL ``LifecycleManagementService`` verbs ``record_deployment``,
``read_deployments`` and ``set_deployment_state`` over the schema-enforced
real-shape state fake (the store the service reaches through
``orchestrator.get_service("state_service")``). Pins:

* record twice: ``created`` True then False, same ``deployment_id``;
* read ``current``, ``history`` (paged by ``next``) and ``get``;
* ``set_deployment_state`` with a stale ``expected_version`` is refused with
  ``stale_version`` and writes nothing; an unknown id is ``unknown_deployment``;
  a bad sha is ``invalid_sha``; no store is ``state_service_unavailable``;
* each verb's returned keys equal the keys its decorator's ``return_value_schema``
  declares and its KB JSON's ``result_description`` names.

Run from repo root:
    PYTHONPATH=src .venv/bin/python ananta/tests/lifecycle_management_service/deployment_record_verbs_smoke.py
"""

from __future__ import annotations

import json
import sys
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
from ananta.services.lifecycle_management_service.deployment_record_schema import (  # noqa: E402
    DEPLOYMENT_RECORD_NAMESPACE,
    TABLE_DEPLOYMENT_RECORD,
    TABLE_DEPLOYMENT_RECORD_EVENT,
    get_deployment_record_schema,
)
from ananta.services.lifecycle_management_service.interfaces.public import (  # noqa: E402
    LifecycleManagementAPI,
)
from ananta.services.lifecycle_management_service.service import (  # noqa: E402
    LifecycleManagementService,
)

KB_DIR = REPO_ROOT / "ananta" / "knowledge_base" / "processes" / "lifecycle_management_service"
PROVIDER_KEY = "service_interface::lifecycle_management_service::"
SOLET = "demo-solet"
SHA_A = "a" * 40
SHA_B = "b" * 40
ACTOR: dict[str, Any] = {"agent_instance_id": "agi-test", "actor_model": "claude-sonnet-5-5", "actor_effort": "medium"}

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
    schema = get_deployment_record_schema()
    for table in (TABLE_DEPLOYMENT_RECORD, TABLE_DEPLOYMENT_RECORD_EVENT):
        fake._enforced_columns[table] = frozenset(schema.tables[table].columns) | _STANDARDIZER_COLUMNS  # noqa: SLF001 — smoke wiring
    return fake


def _service(state: object | None) -> LifecycleManagementService:
    return LifecycleManagementService(_Orchestrator(state))


def _record(svc: LifecycleManagementService, *, sha: str = SHA_A, release: str = "rel-1", at: str = "2026-11-13T12:00:00+00:00") -> dict[str, Any]:
    return svc.record_deployment(
        solet_name=SOLET,
        release_id=release,
        git_sha=sha,
        deployed_at=at,
        trigger="landing",
        manifest_sha256="m" * 64,
        **ACTOR,
    )


def _declared_keys(verb: str) -> set[str]:
    schema = getattr(LifecycleManagementAPI, verb)._service_interface_metadata.return_value_schema  # noqa: SLF001
    return set(schema.properties)


def _kb_json(verb: str) -> dict[str, Any]:
    return json.loads((KB_DIR / f"{verb}.json").read_text())


def _refused(result: dict[str, Any], code: str, label: str) -> None:
    _check(
        result.get("action_status") == "error" and (result.get("data") or {}).get("error_code") == code and str(result.get("error", "")).startswith(code),
        label,
    )


def _case_record() -> tuple[LifecycleManagementService, RealShapeState, str]:
    print("record_deployment: create then idempotent repeat")
    store = _store()
    svc = _service(store)
    first = _record(svc)
    row = first["data"]
    _check(first["action_status"] == "completed" and row["created"] is True, "first call reports created True")
    _check(set(row) == _declared_keys("record_deployment"), "record keys equal the return_value_schema keys")
    _check(row["deployed_at"] == "2026-11-13T12:00:00+00:00" and row["state"] == "recorded" and row["version"] == 1, "row is recorded at version 1 with an offset instant")
    again = _record(svc)["data"]
    _check(again["created"] is False and again["deployment_id"] == row["deployment_id"], "second call reports created False with the same id")
    _check(len(store.rows(DEPLOYMENT_RECORD_NAMESPACE, TABLE_DEPLOYMENT_RECORD)) == 1, "exactly one row stored")
    return svc, store, str(row["deployment_id"])


def _case_read(svc: LifecycleManagementService, first_id: str) -> None:
    print("read_deployments: current, history, get")
    current = svc.read_deployments(solet_name=SOLET, mode="current")["data"]
    _check(set(current) == {"rows", "next"} == _declared_keys("read_deployments"), "read keys equal the return_value_schema keys")
    _check(len(current["rows"]) == 1 and current["rows"][0]["deployment_id"] == first_id, "current returns the one recorded row")
    _check("created" not in current["rows"][0], "read rows omit created")
    empty = svc.read_deployments(solet_name="no-such-solet", mode="current")["data"]
    _check(empty["rows"] == [], "current for a solet with no deployment is zero rows")
    second = _record(svc, sha=SHA_B, release="rel-2", at="2026-11-13T13:00:00+00:00")["data"]
    _check(svc.read_deployments(solet_name=SOLET, mode="current")["data"]["rows"][0]["deployment_id"] == second["deployment_id"], "current is the newest deployment")
    page1 = svc.read_deployments(solet_name=SOLET, mode="history", limit=1)["data"]
    _check(len(page1["rows"]) == 1 and page1["rows"][0]["deployment_id"] == second["deployment_id"] and page1["next"], "history page 1 is newest first with a next token")
    page2 = svc.read_deployments(solet_name=SOLET, mode="history", limit=1, after=page1["next"])["data"]
    _check(len(page2["rows"]) == 1 and page2["rows"][0]["deployment_id"] == first_id and page2["next"] is None, "history page 2 ends the list")
    got = svc.read_deployments(solet_name=SOLET, mode="get", deployment_id=first_id)["data"]
    _check(len(got["rows"]) == 1 and got["rows"][0]["deployment_id"] == first_id, "get returns the row by id")
    _refused(svc.read_deployments(solet_name=SOLET, mode="get", deployment_id="dpr-missing"), "unknown_deployment", "get of an unknown id is refused unknown_deployment")
    _refused(svc.read_deployments(solet_name="other-solet", mode="get", deployment_id=first_id), "unknown_deployment", "get under another solet is refused unknown_deployment")
    _refused(svc.read_deployments(solet_name=SOLET, mode="get"), "invalid_argument", "get without an id is refused")
    _refused(svc.read_deployments(solet_name=SOLET, mode="bogus"), "invalid_argument", "an unknown mode is refused")
    _refused(svc.read_deployments(solet_name=SOLET, mode="history", limit=101), "invalid_argument", "a limit above 100 is refused")
    _refused(svc.read_deployments(solet_name=SOLET, mode="history", after="garbage"), "invalid_argument", "a malformed after is refused")


def _case_set_state(svc: LifecycleManagementService, store: RealShapeState, first_id: str) -> None:
    print("set_deployment_state: withdraw, stale, unknown, reinstate")
    events_before = len(store.rows(DEPLOYMENT_RECORD_NAMESPACE, TABLE_DEPLOYMENT_RECORD_EVENT))
    _check(events_before == 2, "two recorded events exist before the stale attempt")
    stale = svc.set_deployment_state(deployment_id=first_id, withdrawn=True, expected_version=7, reason="wrong", **ACTOR)
    _refused(stale, "stale_version", "a stale expected_version is refused stale_version")
    _check(len(store.rows(DEPLOYMENT_RECORD_NAMESPACE, TABLE_DEPLOYMENT_RECORD_EVENT)) == events_before, "the refused write added no event")
    _refused(svc.set_deployment_state(deployment_id="dpr-missing", withdrawn=True, expected_version=1, reason="x", **ACTOR), "unknown_deployment", "an unknown id is refused unknown_deployment")
    moved = svc.set_deployment_state(deployment_id=first_id, withdrawn=True, expected_version=1, reason="recorded wrongly", **ACTOR)["data"]
    _check(set(moved) == _declared_keys("set_deployment_state"), "set keys equal the return_value_schema keys")
    _check(moved["state"] == "withdrawn" and moved["version"] == 2, "withdraw moves state and version")
    kept = svc.read_deployments(solet_name=SOLET, mode="history", limit=10)["data"]["rows"]
    _check(any(r["deployment_id"] == first_id and r["state"] == "withdrawn" for r in kept), "history still lists the withdrawn row")
    back = svc.set_deployment_state(deployment_id=first_id, withdrawn=False, expected_version=2, reason="reinstated", **ACTOR)["data"]
    _check(back["state"] == "recorded" and back["version"] == 3, "reinstate moves it back")


def _case_refusals() -> None:
    print("refusals: sha, timestamp, trigger, no store")
    svc = _service(_store())
    _refused(_record(svc, sha="a" * 39), "invalid_sha", "a 39-character sha is refused invalid_sha")
    _refused(_record(svc, at="2026-11-13T12:00:00"), "invalid_deployed_at", "a timestamp without an offset is refused")
    _refused(_record(svc, at="yesterday"), "invalid_deployed_at", "an unparseable timestamp is refused")
    bad = svc.record_deployment(solet_name=SOLET, release_id="r", git_sha=SHA_A, deployed_at="2026-11-13T12:00:00+00:00", trigger="manual", manifest_sha256="m", **ACTOR)
    _refused(bad, "invalid_trigger", "an unknown trigger is refused invalid_trigger")
    _refused(_record(_service(None)), "state_service_unavailable", "no state service is refused, not silently empty")


def _case_kb() -> None:
    print("KB JSON: process_key and result keys match the verb")
    for verb in ("record_deployment", "read_deployments", "set_deployment_state"):
        doc = _kb_json(verb)
        _check(doc["process_key"] == PROVIDER_KEY + verb, f"{verb}: process_key equals the contract key")
        description = doc["result_processor_customizations"]["result_description"]
        _check(all(key in description for key in _declared_keys(verb)), f"{verb}: result_description names every return key")


def main() -> int:
    svc, store, first_id = _case_record()
    _case_read(svc, first_id)
    _case_set_state(svc, store, first_id)
    _case_refusals()
    _case_kb()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
