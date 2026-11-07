#!/usr/bin/env python3
"""Smoke: a JSON-backed event-embedding checkpoint resumes its generation."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"))

from _stub_state_service import StubStateService  # noqa: E402
from ananta.llm.session_ledger.event_embedding_checkpoint import (  # noqa: E402
    SweepCheckpoint,
    prepare_sweep,
)
from ananta.llm.session_ledger.repository import SessionLedgerRepository  # noqa: E402

_CHECKPOINT_KEY = ("session_ledger", "event_embed_sweep_checkpoint", "GLOBAL")
_TARGET_POLICY = "tokens:2048+chars:8192"
_LOGGER = logging.getLogger("ananta.llm.session_ledger.event_embedding_checkpoint")


class _JsonState(StubStateService):
    """Model the installed state-service KV JSON envelope."""

    def set_key_value(
        self,
        namespace: str,
        key: str,
        value: object,
        scope: str = "GLOBAL",
        ttl: int | None = None,
    ) -> dict[str, Any]:
        if isinstance(value, dict | list):
            value = json.dumps(value)
        return super().set_key_value(namespace, key, value, scope, ttl)


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _check(condition: bool, label: str) -> bool:
    print(f"  {'PASS' if condition else 'FAIL'} {label}")
    return condition


def main() -> int:
    state = _JsonState()
    repository = SessionLedgerRepository(cast("Any", state))
    checkpoint = SweepCheckpoint("serialized-generation", _TARGET_POLICY, None)
    stored = state.set_key_value(
        _CHECKPOINT_KEY[0],
        _CHECKPOINT_KEY[1],
        checkpoint.payload(),
        _CHECKPOINT_KEY[2],
    )
    capture = _LogCapture()
    _LOGGER.addHandler(capture)
    _LOGGER.propagate = False
    try:
        checks = [
            _check(stored["action_status"] == "completed", "checkpoint write succeeds"),
            _check(isinstance(state.key_values[_CHECKPOINT_KEY], str), "checkpoint is persisted as a JSON string"),
        ]
        resumed = repository.get_event_embed_checkpoint()
        checks.append(_check(resumed == checkpoint, "serialized checkpoint decodes as the same generation"))
        active, pending = prepare_sweep(repository, _TARGET_POLICY, scheduled=False)
        checks.append(_check(active == checkpoint and pending == checkpoint, "checkpoint restart resumes instead of creating a new sweep"))
        checks.append(
            _check(
                not any("event embedding checkpoint corrupt" in message for message in capture.messages),
                "checkpoint restart emits no corruption advisory",
            ),
        )
    finally:
        _LOGGER.removeHandler(capture)
        _LOGGER.propagate = True
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
