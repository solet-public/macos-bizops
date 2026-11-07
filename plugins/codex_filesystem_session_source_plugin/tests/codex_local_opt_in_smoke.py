#!/usr/bin/env python3
"""Before/after smoke for the opt-in ``codex_local`` source (no pytest).

prb_8c652bf3: setup offers ``codex_local``, so the seed ships this plugin with
``~/.codex/sessions`` as its default root. Codex must stay optional, so boot
registers that root only when ``ledger_allowed_roots`` contains it. Setup
writes the root there when the operator selects ``codex_local``.

Legs, all against the real plugin descriptor and the real boot function:

* BEFORE selection: an empty allow-list registers nothing, logs the skip, and
  does not raise, whether or not Codex exists on the host.
* AFTER selection: an allow-list holding ``~/.codex/sessions`` registers the
  source through the trusted seam, including on a host without Codex.
* A Codex-free host qualifies without a refusal: zero backfill, no failure.

``HOME`` points at a temporary directory for every leg, so the smoke never
reads the operator's real ``~/.codex``.

Run:
    .venv/bin/python3 plugins/codex_filesystem_session_source_plugin/tests/codex_local_opt_in_smoke.py
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import types
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0,
    str(REPO_ROOT / "plugins" / "codex_filesystem_session_source_plugin" / "src"),
)

from ananta.core.orchestration.startup_sequence import (  # noqa: E402
    _auto_register_declared_pulling_sources,  # pyright: ignore[reportPrivateUsage]
)
from ananta.llm.session_ledger.types import IngestSourceKind  # noqa: E402
from ananta.services.session_ledger_service.enforcement import (  # noqa: E402
    filesystem_root_uri_is_allowed,
)
from codex_filesystem_session_source_plugin.plugin import (  # noqa: E402
    CodexFilesystemSessionSourcePlugin,
    _qualify_source,  # pyright: ignore[reportPrivateUsage]
)

_DEFAULT_ROOT = "~/.codex/sessions"
_STARTUP_LOGGER = "ananta.core.orchestration.startup_sequence"

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


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextmanager
def _home(*, with_codex: bool) -> Generator[Path]:
    """Point ``HOME`` at a temporary directory, optionally holding Codex sessions."""
    previous = os.environ.get("HOME")
    with tempfile.TemporaryDirectory() as raw:
        home = Path(raw).resolve()
        if with_codex:
            (home / ".codex" / "sessions").mkdir(parents=True)
        os.environ["HOME"] = str(home)
        try:
            yield home
        finally:
            if previous is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = previous


def _boot(allowed_roots: list[str]) -> tuple[list[str], list[str]]:
    """Run the real boot auto-register over the real Codex descriptor."""
    plugin = CodexFilesystemSessionSourcePlugin()
    registered: list[str] = []

    def _trusted_register(*, root_uri: str, **_kwargs: object) -> dict[str, object]:
        registered.append(root_uri)
        return {"source_id": "src_codex", "outcome": "registered"}

    service = types.SimpleNamespace(
        registry=types.SimpleNamespace(
            list_sources=lambda: [plugin.describe()],
            get_by_kind=lambda *_a, **_kw: plugin,  # pyright: ignore[reportUnknownLambdaType]
        ),
        _register_source_internal=_trusted_register,
        _repository=types.SimpleNamespace(list_sources=lambda *_a, **_kw: []),  # pyright: ignore[reportUnknownLambdaType]
        _ledger_allowed_roots=allowed_roots,
    )
    handler = _Records()
    logger = logging.getLogger(_STARTUP_LOGGER)
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        _auto_register_declared_pulling_sources(service)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    return registered, handler.messages


def test_descriptor_declares_default_root() -> None:
    descriptor = CodexFilesystemSessionSourcePlugin().describe()
    _check(
        descriptor.source_kind is IngestSourceKind.CODEX_LOCAL and descriptor.default_pulling_root_uri == _DEFAULT_ROOT,
        "codex_local descriptor declares ~/.codex/sessions as its default root",
    )


def test_containment_helper() -> None:
    with _home(with_codex=False):
        _check(
            filesystem_root_uri_is_allowed(_DEFAULT_ROOT, ["~/.codex/sessions"]) and filesystem_root_uri_is_allowed("~/.codex/sessions/2026", ["~/.codex"]),
            "filesystem_root_uri_is_allowed admits a root inside an allowed root",
        )
        _check(
            not filesystem_root_uri_is_allowed(_DEFAULT_ROOT, []) and not filesystem_root_uri_is_allowed(_DEFAULT_ROOT, ["~/.codex/sessions-other"]) and not filesystem_root_uri_is_allowed("file://bad-authority/x", ["/"]),
            "filesystem_root_uri_is_allowed refuses an empty list, a prefix twin and a malformed URI",
        )


def test_before_selection_registers_nothing() -> None:
    for with_codex in (True, False):
        host = "with Codex" if with_codex else "without Codex"
        with _home(with_codex=with_codex):
            registered, messages = _boot([])
        _check(registered == [], f"declined, host {host}: boot registers no codex_local source")
        _check(
            any("codex_local" in m and "ledger_allowed_roots" in m for m in messages),
            f"declined, host {host}: boot logs the skip with its reason",
        )
    with _home(with_codex=True):
        registered, _messages = _boot(["~/.claude/projects"])
    _check(registered == [], "a Claude-only allow-list does not admit the Codex root")


def test_after_selection_registers_the_source() -> None:
    for with_codex in (True, False):
        host = "with Codex" if with_codex else "without Codex"
        with _home(with_codex=with_codex):
            registered, messages = _boot([_DEFAULT_ROOT])
        _check(
            registered == [_DEFAULT_ROOT],
            f"selected, host {host}: boot registers codex_local at ~/.codex/sessions",
        )
        _check(
            not any("not auto-registered" in m for m in messages),
            f"selected, host {host}: boot logs no skip",
        )


def test_codex_free_host_qualifies_without_refusal() -> None:
    with _home(with_codex=False) as home:
        root_uri = str(home / ".codex" / "sessions")
        polled: list[str] = []
        ledger = types.SimpleNamespace(
            list_sources=lambda: {
                "sources": [
                    {
                        "source_id": "src_codex",
                        "source_kind": IngestSourceKind.CODEX_LOCAL.value,
                        "root_uri": root_uri,
                        "enabled": True,
                    }
                ]
            },
            poll_source=polled.append,
        )
        proof = _qualify_source(ledger=ledger, source_kind=IngestSourceKind.CODEX_LOCAL.value)
    _check(
        proof
        == {
            "source_registered": True,
            "source_kind": IngestSourceKind.CODEX_LOCAL.value,
            "backfill_count": 0,
            "sample_content_retrieved": False,
        }
        and polled == [],
        "a registered codex_local with no ~/.codex qualifies with zero backfill, not a refusal",
    )


def main() -> int:
    print("=== codex_local opt-in smoke (descriptor + boot allow-list + Codex absent) ===")
    test_descriptor_declares_default_root()
    test_containment_helper()
    test_before_selection_registers_nothing()
    test_after_selection_registers_the_source()
    test_codex_free_host_qualifies_without_refusal()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
