#!/usr/bin/env python3
"""The key-value fakes and the bootstrap store pass the same cases as the real store (r74 K2 a4, chg_90ddc12e).

Test fakes that return the object they were given while the real store returns
text hide a bug from every test that uses them. This smoke builds one subject
per fake and runs the shared case table
(``_key_value_round_trip_cases.run_cases``, the same table the Postgres store
passes in ``plugins/postgres_state_management_plugin/tests/key_value_round_trip_smoke.py``)
against each, so a fake that stops behaving like the store fails here:

* ``BootstrapKeyValueStorage`` (the production in-memory store used before plugins load)
* ``RealShapeState`` (agent-messaging role smokes, ``_real_state_fake.py``)
* ``StubStateService`` (session-ledger smokes, ``_stub_state_service.py``)
* ``_FixtureStateService`` (``guard_test_support.py``)

The three test fakes have no ``list_key_values`` (nothing that uses them lists), so the
case table's list check is reported as SKIP for them; it is a real check for the
bootstrap store, which does implement list::

    .venv/bin/python3 ananta/tests/services/state_service/key_value_fake_contract_smoke.py
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[4]
for _source in (
    ROOT,
    ROOT / "ananta" / "src",
    ROOT / "ananta" / "tests" / "services" / "state_service",
    ROOT / "ananta" / "tests" / "llm" / "session_ledger",
    ROOT / "plugins" / "agent_messaging_plugin" / "tests",
    ROOT / "plugins" / "platform_health_plugin" / "tests",
):
    sys.path.insert(0, str(_source))

from _key_value_round_trip_cases import run_cases  # noqa: E402
from _real_state_fake import RealShapeState  # noqa: E402
from _stub_state_service import StubStateService  # noqa: E402  # pyright: ignore[reportMissingImports] -- sibling fixture path is added above
from ananta.services.key_value_operations.bootstrap_key_value_storage import BootstrapKeyValueStorage  # noqa: E402
from guard_test_support import _FixtureStateService  # pyright: ignore[reportPrivateUsage]  # noqa: E402

_LIST_LABEL_PREFIX = "list_key_values"

_passed = 0
_skipped = 0
_failed: list[str] = []


class _ListlessSubject:
    """A fake that has no ``list_key_values``: set and get pass through, list reports no rows."""

    def __init__(self, fake: Any) -> None:
        self._fake = fake

    def set_key_value(self, namespace: str, key: str, value: object, scope: str = "GLOBAL", ttl: int | None = None) -> Mapping[str, object]:
        return cast(Mapping[str, object], self._fake.set_key_value(namespace, key, value, scope, ttl))

    def get_key_value(self, namespace: str, key: str, scope: str = "GLOBAL") -> Mapping[str, object]:
        return cast(Mapping[str, object], self._fake.get_key_value(namespace, key, scope))

    def list_key_values(self, namespace: str | None = None, scope: str | None = None, pattern: str | None = None) -> Mapping[str, object]:
        return {"action_status": "completed", "data": {"values": []}}


def _report(subject: str, has_list: bool):
    def report(condition: object, label: str) -> None:
        global _passed, _skipped
        if not has_list and label.startswith(_LIST_LABEL_PREFIX):
            _skipped += 1
            print(f"  SKIP  {subject}: {label} (the fake has no list_key_values)")
        elif condition:
            _passed += 1
            print(f"  PASS  {subject}: {label}")
        else:
            _failed.append(f"{subject}: {label}")
            print(f"  FAIL  {subject}: {label}")

    return report


def main() -> int:
    subjects: tuple[tuple[str, Any], ...] = (
        ("BootstrapKeyValueStorage", BootstrapKeyValueStorage()),
        ("RealShapeState", RealShapeState()),
        ("StubStateService", StubStateService()),
        ("guard _FixtureStateService", _FixtureStateService()),
    )
    for name, fake in subjects:
        has_list = hasattr(fake, "list_key_values")
        store = fake if has_list else _ListlessSubject(fake)
        run_cases(cast(Any, store), _report(name, has_list), "kv_fake_contract")
    print(f"\n{_passed} passed, {_skipped} skipped, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
