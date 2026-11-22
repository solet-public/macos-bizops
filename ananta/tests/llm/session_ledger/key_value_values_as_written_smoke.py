#!/usr/bin/env python3
"""Smoke: the session-ledger key-value readers take a value as the store returns it, without decoding it by hand.

The store returns a value as it was written (an object stays an object, a string stays a string).
A reader that also ran ``json.loads`` on a string value knew the storage format; this pins that
no reader does: JSON text stored as a string is a string, so it is not progress and not a checkpoint.
"""

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
    CORRUPT_GENERATION,
    SweepCheckpoint,
    decode_checkpoint,
)
from ananta.llm.session_ledger.repository import SessionLedgerRepository  # noqa: E402

_NAMESPACE = "session_ledger"
_PROGRESS_KEY = "event_embed_drain_progress"
_PROGRESS: dict[str, object] = {"pages": 3, "embedded": 120}
_CHECKPOINT = SweepCheckpoint("generation-a", "tokens:2048+chars:8192", None)
_LOGGER = logging.getLogger("ananta.llm.session_ledger.event_embedding_checkpoint")


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _check(condition: bool, label: str) -> bool:
    print(f"  {'PASS' if condition else 'FAIL'} {label}")
    return condition


def _progress_checks() -> list[bool]:
    state = StubStateService()
    repository = SessionLedgerRepository(cast("Any", state))
    state.set_key_value(_NAMESPACE, _PROGRESS_KEY, _PROGRESS)
    checks = [_check(repository.get_event_embed_progress() == _PROGRESS, "a progress record written as an object reads back as that object")]
    state.set_key_value(_NAMESPACE, _PROGRESS_KEY, json.dumps(_PROGRESS))
    checks.append(_check(repository.get_event_embed_progress() is None, "progress stored as a string is not decoded by hand: it reads as no progress"))
    return checks


def _checkpoint_checks() -> list[bool]:
    capture = _LogCapture()
    _LOGGER.addHandler(capture)
    _LOGGER.propagate = False
    try:
        checks = [
            _check(decode_checkpoint(_CHECKPOINT.payload()) == _CHECKPOINT, "a checkpoint payload as an object decodes to that checkpoint"),
            _check(not capture.messages, "a valid checkpoint emits no corruption advisory"),
        ]
        marker = decode_checkpoint(json.dumps(_CHECKPOINT.payload()))
        checks.append(
            _check(
                marker == SweepCheckpoint(CORRUPT_GENERATION, "", None),
                "a checkpoint payload as a JSON string is not decoded by hand: it is the corrupt marker",
            ),
        )
        checks.append(
            _check(
                any("event embedding checkpoint corrupt" in message for message in capture.messages),
                "the corrupt marker comes through the existing advisory path",
            ),
        )
    finally:
        _LOGGER.removeHandler(capture)
        _LOGGER.propagate = True
    return checks


def main() -> int:
    checks = [*_progress_checks(), *_checkpoint_checks()]
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
