#!/usr/bin/env python3
"""Codex Cloud backfill verb smoke — walker mechanics + verb envelope.

Covers the dispatch's acceptance gate 2 list:

* (a) Walker fetches every task from the list endpoint and dispatches the
      task-detail envelope to a stub importer (mocked httpx, no network).
* (b) SET DIFFERENCE skip-already-ingested: when the stub ledger already
      reports the task id under vendor='codex', the walker skips the
      fetch + dispatch.
* (c) ``force=True`` overrides SET DIFFERENCE.
* (d) Concurrency bound: with ``walker_concurrency=2`` the walker never
      issues more than 2 in-flight task-detail fetches simultaneously.
* (e) 429 backoff: a task that 429s on the first call but 200s on the
      second is fetched, dispatched, and reported as fetched.
* (f) 401 abort: a 401 on the list endpoint surfaces as ``auth_expired``
      with the structured error envelope.
* (g) Cross-source dedupe with codex_state: the
      ``_VENDOR_FROM_SOURCE_KIND`` mapping groups CODEX_CLOUD and
      CODEX_STATE under SourceVendor.CODEX so a shared ``task_id``
      external_session_id collapses at the (vendor='codex',
      external_session_id) dedupe key (W5.B canonical-pointer pattern).
* (h) Cloud-homunculus credential transfer gap is documented in the
      plugin docstring (locks the dispatch's acceptance gate 4).

The walker mechanics use an in-process httpx.MockTransport so the smoke is
hermetic — no network, no real auth.json, no real Postgres.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from pathlib import Path
from typing import Any

import httpx

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0,
    str(_REPO_ROOT / "plugins" / "codex_cloud_session_source_plugin" / "src"),
)

from ananta.llm.session_ledger.importer import _VENDOR_FROM_SOURCE_KIND  # noqa: E402
from ananta.llm.session_ledger.types import (  # noqa: E402
    IngestSourceKind,
    SourceVendor,
)
from codex_cloud_session_source_plugin import plugin as plugin_module  # noqa: E402
from codex_cloud_session_source_plugin.walker import (  # noqa: E402
    CodexCloudCredentials,
    CodexCloudWalkerError,
    WalkerConfig,
    WalkerReport,
    fetch_and_dispatch_concurrent,
    list_all_task_summaries,
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


def _walker_config(*, concurrency: int = 2) -> WalkerConfig:
    return WalkerConfig(
        auth_path=Path("/tmp/nonexistent_auth.json"),
        api_base_url="https://chatgpt.com/backend-api",
        list_page_limit=100,
        walker_concurrency=concurrency,
        fetch_timeout_seconds=5,
        rate_limit_backoff_seconds=(0, 0, 0),
    )


def _credentials() -> CodexCloudCredentials:
    return CodexCloudCredentials(
        access_token="test-access",
        chatgpt_account_id="acc_test",
        fedramp=False,
    )


# ─── (a) walker fetches + dispatches every task ────────────────────────────


def test_a_list_and_fetch_dispatches_every_task() -> None:
    counters: dict[str, int] = {"list_calls": 0}
    fetch_calls: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/wham/tasks/list"):
            counters["list_calls"] = counters["list_calls"] + 1
            return httpx.Response(
                200,
                json={
                    "tasks": [
                        {"id": "task_alpha", "title": "alpha", "updated_at": "2026-06-14T00:00:00+00:00"},
                        {"id": "task_beta", "title": "beta", "updated_at": "2026-06-14T01:00:00+00:00"},
                    ],
                    "next_cursor": None,
                },
            )
        prefix = "/wham/tasks/"
        if prefix in request.url.path:
            task_id = request.url.path.rsplit("/", 1)[-1]
            fetch_calls[task_id] = fetch_calls.get(task_id, 0) + 1
            return httpx.Response(
                200,
                json={
                    "current_user_turn": {
                        "id": f"u_{task_id}",
                        "input_items": [
                            {
                                "type": "message",
                                "role": "user",
                                "content": [{"content_type": "text", "text": "hello"}],
                            },
                        ],
                    },
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    config = _walker_config()
    dispatched: list[str] = []
    report = WalkerReport()

    async def _run() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            summaries = await list_all_task_summaries(
                client=client, config=config, creds=_credentials(),
            )
            await fetch_and_dispatch_concurrent(
                summaries=summaries,
                config=config,
                creds=_credentials(),
                dispatch_callable=dispatched.append,
                client=client,
                report=report,
            )

    asyncio.run(_run())
    _check(counters["list_calls"] == 1, "(a) wham/tasks/list called once")
    _check(len(dispatched) == 2, "(a) two task envelopes dispatched")
    _check(fetch_calls == {"task_alpha": 1, "task_beta": 1}, "(a) each task fetched exactly once")
    _check(report.fetched_count == 2, "(a) WalkerReport.fetched_count == 2")
    _check(report.errored_count == 0, "(a) WalkerReport.errored_count == 0")
    decoded = [json.loads(env) for env in dispatched]
    ids = {env["external_session_id"] for env in decoded}
    _check(ids == {"task_alpha", "task_beta"}, "(a) envelopes carry correct task ids")


# ─── (b) SET DIFFERENCE skip-already-ingested ──────────────────────────────


def test_b_set_difference_skip_already_ingested() -> None:
    plugin = plugin_module.CodexCloudSessionSourcePlugin()

    class _Row:
        def __init__(self, external_session_id: str) -> None:
            self.external_session_id = external_session_id

    class _Ledger:
        def list_sessions(self, *, vendor: str) -> dict[str, list[Any]]:
            assert vendor == SourceVendor.CODEX.value
            return {"sessions": [_Row("task_already_in_ledger")]}

    class _Orch:
        def get_service(self, name: str) -> Any:
            if name == "session_ledger_service":
                return _Ledger()
            return None

    plugin.orchestrator_ref = _Orch()
    summaries = [
        {"id": "task_already_in_ledger", "updated_at": "2026-06-14T00:00:00+00:00"},
        {"id": "task_new", "updated_at": "2026-06-14T01:00:00+00:00"},
    ]
    to_fetch, skipped = plugin._filter_already_ingested(summaries=summaries, force=False)
    _check(skipped == 1, "(b) one task skipped via SET DIFFERENCE")
    _check(
        [s["id"] for s in to_fetch] == ["task_new"],
        "(b) only the new task survives SET DIFFERENCE",
    )


# ─── (c) force=True overrides SET DIFFERENCE ──────────────────────────────


def test_c_force_overrides_set_difference() -> None:
    plugin = plugin_module.CodexCloudSessionSourcePlugin()

    class _Row:
        def __init__(self, external_session_id: str) -> None:
            self.external_session_id = external_session_id

    class _Ledger:
        def list_sessions(self, *, vendor: str) -> list[Any]:
            return [_Row("task_already_in_ledger")]

    class _Orch:
        def get_service(self, name: str) -> Any:
            return _Ledger() if name == "session_ledger_service" else None

    plugin.orchestrator_ref = _Orch()
    summaries = [
        {"id": "task_already_in_ledger", "updated_at": "2026-06-14T00:00:00+00:00"},
        {"id": "task_new", "updated_at": "2026-06-14T01:00:00+00:00"},
    ]
    to_fetch, skipped = plugin._filter_already_ingested(summaries=summaries, force=True)
    _check(skipped == 0, "(c) force=True skips nothing")
    _check(len(to_fetch) == 2, "(c) force=True returns every summary")


# ─── (d) concurrency bound ────────────────────────────────────────────────


def test_d_concurrency_bound_at_two_in_flight() -> None:
    in_flight = 0
    max_in_flight = 0
    sleep_event = asyncio.Event()
    fetch_started = asyncio.Event()
    fetch_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_in_flight, fetch_count
        if request.url.path.endswith("/wham/tasks/list"):
            return httpx.Response(
                200,
                json={
                    "tasks": [
                        {"id": f"task_{i}", "title": f"t{i}", "updated_at": "2026-06-14T00:00:00+00:00"}
                        for i in range(6)
                    ],
                },
            )
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        fetch_count += 1
        if fetch_count == 1:
            fetch_started.set()
            await sleep_event.wait()
        else:
            await asyncio.sleep(0)
        in_flight -= 1
        return httpx.Response(
            200,
            json={"current_user_turn": {"id": "tu", "input_items": []}},
        )

    transport = httpx.MockTransport(handler)
    config = _walker_config(concurrency=2)
    dispatched: list[str] = []
    report = WalkerReport()

    async def _run() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            summaries = await list_all_task_summaries(
                client=client, config=config, creds=_credentials(),
            )

            async def _runner() -> None:
                await fetch_and_dispatch_concurrent(
                    summaries=summaries,
                    config=config,
                    creds=_credentials(),
                    dispatch_callable=dispatched.append,
                    client=client,
                    report=report,
                )

            runner_task = asyncio.create_task(_runner())
            await fetch_started.wait()
            # Briefly yield so the second slot acquires the semaphore;
            # then release everything.
            await asyncio.sleep(0)
            sleep_event.set()
            await runner_task

    asyncio.run(_run())
    _check(max_in_flight <= 2, f"(d) max in-flight {max_in_flight} <= concurrency 2")
    _check(len(dispatched) == 6, "(d) all 6 tasks eventually dispatched")


# ─── (e) 429 backoff ──────────────────────────────────────────────────────


def test_e_429_backoff_then_success() -> None:
    attempts: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/wham/tasks/list"):
            return httpx.Response(
                200,
                json={
                    "tasks": [
                        {"id": "task_429", "title": "x", "updated_at": "2026-06-14T00:00:00+00:00"},
                    ],
                },
            )
        if "/wham/tasks/" in request.url.path:
            task_id = request.url.path.rsplit("/", 1)[-1]
            attempts[task_id] = attempts.get(task_id, 0) + 1
            if attempts[task_id] == 1:
                return httpx.Response(429)
            return httpx.Response(
                200,
                json={"current_user_turn": {"id": "tu", "input_items": []}},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    config = _walker_config()
    dispatched: list[str] = []
    report = WalkerReport()

    async def _run() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            summaries = await list_all_task_summaries(
                client=client, config=config, creds=_credentials(),
            )
            await fetch_and_dispatch_concurrent(
                summaries=summaries,
                config=config,
                creds=_credentials(),
                dispatch_callable=dispatched.append,
                client=client,
                report=report,
            )

    asyncio.run(_run())
    task_429_attempts: int = int(attempts.get("task_429", 0))
    _check(task_429_attempts >= 2, "(e) task retried at least twice (429 then 200)")
    _check(len(dispatched) == 1, "(e) one envelope dispatched after backoff")
    _check(report.fetched_count == 1, "(e) WalkerReport.fetched_count == 1")
    _check(report.errored_count == 0, "(e) WalkerReport.errored_count == 0")


# ─── (f) 401 abort with auth_expired ──────────────────────────────────────


def test_f_401_on_list_raises_auth_expired() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Unauthorized"})

    transport = httpx.MockTransport(handler)
    config = _walker_config()
    raised: CodexCloudWalkerError | None = None

    async def _run() -> None:
        nonlocal raised
        async with httpx.AsyncClient(transport=transport) as client:
            try:
                await list_all_task_summaries(
                    client=client, config=config, creds=_credentials(),
                )
            except CodexCloudWalkerError as exc:
                raised = exc

    asyncio.run(_run())
    _check(raised is not None, "(f) 401 on list raises CodexCloudWalkerError")
    if raised is not None:
        _check(raised.code == "auth_expired", "(f) error code is 'auth_expired'")


# ─── (g) cross-source dedupe with codex_state ─────────────────────────────


def test_g_cross_source_dedupe_with_codex_state() -> None:
    cloud_vendor = _VENDOR_FROM_SOURCE_KIND.get(IngestSourceKind.CODEX_CLOUD)
    state_vendor = _VENDOR_FROM_SOURCE_KIND.get(IngestSourceKind.CODEX_STATE)
    _check(
        cloud_vendor is SourceVendor.CODEX,
        "(g) CODEX_CLOUD → SourceVendor.CODEX (locks W5.B dedupe vendor grouping)",
    )
    _check(
        state_vendor is SourceVendor.CODEX,
        "(g) CODEX_STATE → SourceVendor.CODEX",
    )
    _check(
        cloud_vendor == state_vendor,
        "(g) CODEX_CLOUD and CODEX_STATE share the same vendor "
        "→ (vendor, external_session_id) dedupe key collapses overlap",
    )


# ─── (h) cloud-homunculus credential transfer gap documented ──────────────


def test_h_cloud_homunculus_credential_gap_documented_in_docstring() -> None:
    src = inspect.getsource(plugin_module)
    _check(
        "sealed-box" in src.lower() or "secret transfer protocol" in src.lower(),
        "(h) plugin docstring references the sealed-box transfer protocol",
    )
    _check(
        "macos_vault_plugin" in src,
        "(h) plugin docstring references macos_vault_plugin",
    )
    _check(
        "cloud homunculus" in src.lower() or "CLOUD homunculus" in src,
        "(h) plugin docstring explains the cloud-homunculus gap",
    )


# ─── main ───────────────────────────────────────────────────────────────────


def main() -> int:
    print("=== codex_cloud_backfill_smoke (8 cases a–h) ===")
    test_a_list_and_fetch_dispatches_every_task()
    test_b_set_difference_skip_already_ingested()
    test_c_force_overrides_set_difference()
    test_d_concurrency_bound_at_two_in_flight()
    test_e_429_backoff_then_success()
    test_f_401_on_list_raises_auth_expired()
    test_g_cross_source_dedupe_with_codex_state()
    test_h_cloud_homunculus_credential_gap_documented_in_docstring()
    print(
        f"\n{_passed} passed, {len(_failed)} failed"
        + ("" if not _failed else "\n  failures:\n    " + "\n    ".join(_failed)),
    )
    return 0 if not _failed else 1


if __name__ == "__main__":
    sys.exit(main())
