#!/usr/bin/env python3
"""Regression guard: `solet-bridge call` tells the sender the truth about a refusal.

``_call_and_emit`` used to print "Waiter release is uncertain ... reconcile
before resubmitting" for EVERY failure while waiting, including a definite
refusal (unknown process, wrong argument name). The sender was told a call
that never left may have been accepted, and did not resend it.

The line now belongs to outcomes that are genuinely unknown. Each case names
the mutation that reds it:

  1. A 4xx on the dispatch (unknown process, unknown argument, missing
     argument) prints the refusal alone, exits EXTERNAL_ERROR and prints no
     uncertain line. (Mutation: restore the unconditional print -> FAIL.)
  2. A read timeout after the request was sent still prints the line.
     (Mutation: treat every httpx error as a refusal -> FAIL.)
  3. A 5xx with a bridge code on the dispatch still prints the line: a server
     fault leaves the server's state unknown. (Mutation: raise the refusal
     type for every error status -> FAIL.)
  4. A 4xx while POLLING for the result still prints the line: the dispatch
     was accepted, only the poll was refused. (Mutation: classify by status
     alone, in ``call_and_wait``'s poll phase too -> FAIL.)
  5. ``--no-wait`` never printed the line and still does not.
  6. KeyboardInterrupt, SystemExit, timeouts, OSError and a body that is not a
     usable answer keep the line. (Mutation: add them to the refusal branch ->
     FAIL.)
  7. ``BridgeClient.process_call`` raises ``BridgeRefusedError`` carrying
     ``status_code`` and ``code`` for a 4xx, and a plain ``BridgeCallError``
     for a 5xx or an unusable 200 body. (Mutation: raise it for a 5xx -> FAIL.)

Project policy: no pytest. Exits 0 on success, 1 on any failure.

Run:

    .venv/bin/python3 plugins/agent_messaging_plugin/tests/cli_refusal_reads_as_refusal_smoke.py
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stderr
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import httpx
from click.testing import CliRunner, Result

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from ananta.constants import ExitCodes  # noqa: E402

from agent_messaging_plugin.local_cli import cli as cli_module  # noqa: E402
from agent_messaging_plugin.local_cli.client import (  # noqa: E402
    BridgeCallError,
    BridgeClient,
    BridgeRefusedError,
    BridgeResultTimeoutError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

UNCERTAIN = "Waiter release is uncertain"
_BRIDGE = "/api/v1/bridge/agc-smoke"
_UNKNOWN_PROCESS = "plugin::agent_messaging_plugin::no_such_process"  # wint:negative-fixture: deliberately unregistered, the refusal under test
_failures: list[str] = []


def _check(condition: bool, message: str) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {message}")
    if not condition:
        _failures.append(message)


def _json(status: int, body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status, json=body)


def _read_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("read timed out after the request was sent", request=request)


def _bridge(
    dispatch: Callable[[httpx.Request], httpx.Response],
    poll: Callable[[httpx.Request], httpx.Response] | None = None,
) -> httpx.MockTransport:
    """A scripted solet: open and close succeed, dispatch and poll are per case."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/bridge/open":
            return _json(200, {"bridge_id": "agc-smoke"})
        if path == f"{_BRIDGE}/close":
            return _json(200, {})
        if path == f"{_BRIDGE}/process/call":
            return dispatch(request)
        if path.startswith(f"{_BRIDGE}/process/result/") and poll is not None:
            return poll(request)
        return _json(404, {"code": "unexpected_route", "message": path})

    return httpx.MockTransport(handler)


def _invoke(transport: httpx.MockTransport, *argv: str) -> Result:
    """Run the real ``call`` command over the real BridgeClient and the scripted solet."""

    def make_client(base_url: str, **kwargs: Any) -> BridgeClient:
        return BridgeClient(base_url, transport=transport, **kwargs)

    with (
        patch.object(cli_module, "resolve_base_url", return_value="http://bridge.invalid"),
        patch.object(cli_module, "BridgeClient", make_client),
        patch.object(cli_module, "_install_sigterm_unwind"),
    ):
        return CliRunner().invoke(cli_module.cli, ["call", *argv])


def _error_lines(result: Result) -> list[str]:
    return [line for line in result.stderr.splitlines() if line.strip()]


def test_a_definite_refusal_prints_the_refusal_alone() -> None:
    """Case 1: unknown process, wrong argument name and missing argument."""
    refusals = (
        (404, _UNKNOWN_PROCESS, "{}", "bridge.process_call_failed", f"Process '{_UNKNOWN_PROCESS}' not found or malformed in registry"),
        (400, "plugin::agent_messaging_plugin::peer_send_by_name", '{"name":"x","message":"y"}', "action.unknown_arguments", "Unknown arguments: ['message']"),
        (400, "plugin::agent_messaging_plugin::peer_send_by_name", '{"name":"x"}', "bridge.process_call_failed", "Missing required arguments: ['content']"),
    )
    for status, process_key, arguments, code, message in refusals:
        transport = _bridge(lambda _request, s=status, c=code, m=message: _json(s, {"code": c, "message": m}))
        result = _invoke(transport, process_key, arguments)
        lines = _error_lines(result)
        label = f"{status} {code}"
        _check(result.exit_code == int(ExitCodes.EXTERNAL_ERROR), f"{label}: exits EXTERNAL_ERROR (got {result.exit_code})")
        _check(len(lines) == 1, f"{label}: stderr is exactly one line (got {lines})")
        _check(UNCERTAIN not in result.stderr, f"{label}: no uncertain line")
        _check(message in result.stderr and f"({status})" in result.stderr, f"{label}: the one line is the refusal, with its status and message")
        _check(result.stdout == "", f"{label}: nothing on stdout")


def test_an_unknown_outcome_keeps_the_uncertain_line() -> None:
    """Cases 2, 3 and 4: the request may have been accepted, so say so."""
    after_send = _invoke(_bridge(_read_timeout), "plugin::agent_messaging_plugin::peer_identity", "{}")
    _check(UNCERTAIN in after_send.stderr, "a read timeout after the request was sent prints the uncertain line")
    _check(after_send.exit_code != 0, "...and exits non-zero")

    fault = _invoke(
        _bridge(lambda _request: _json(500, {"code": "bridge.process_call_failed", "message": "action store unavailable"})),
        "plugin::agent_messaging_plugin::peer_identity",
        "{}",
    )
    _check(UNCERTAIN in fault.stderr, "a 500 with a bridge code on the dispatch prints the uncertain line")

    accepted_then_refused_poll = _invoke(
        _bridge(
            lambda _request: _json(200, {"status": "queued", "action_id": "ae-1", "flow_id": "flow-1", "process_key": "x"}),
            lambda _request: _json(404, {"code": "bridge.action_result_not_found", "message": "no such action"}),
        ),
        "plugin::agent_messaging_plugin::peer_identity",
        "{}",
    )
    _check(UNCERTAIN in accepted_then_refused_poll.stderr, "a 4xx while polling (dispatch accepted) prints the uncertain line")


def test_no_wait_never_printed_the_line() -> None:
    """Case 5."""
    result = _invoke(_bridge(_read_timeout), "plugin::agent_messaging_plugin::peer_identity", "{}", "--no-wait")
    _check(UNCERTAIN not in result.stderr, "--no-wait prints no uncertain line")
    _check(result.exit_code != 0, "--no-wait still exits non-zero on a lost connection")


class _Raising:
    """A client whose dispatch-and-wait raises what the case names."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def call_and_wait(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise self._error


def _attempt(error: BaseException, *, should_wait: bool = True) -> tuple[str, BaseException | None]:
    """Drive the real ``_call_and_emit`` body; return stderr and what it raised."""
    stderr = io.StringIO()
    raised: BaseException | None = None
    with redirect_stderr(stderr):
        try:
            cli_module._call_and_emit(  # noqa: SLF001
                cast("BridgeClient", _Raising(error)),
                "plugin::agent_messaging_plugin::peer_identity",
                {},
                reason=None,
                timeout_s=1.0,
                should_wait=should_wait,
                job_timeout_s=1.0,
            )
        except BaseException as exc:  # noqa: BLE001
            raised = exc
    return stderr.getvalue(), raised


def test_only_the_refusal_type_skips_the_line() -> None:
    """Case 6: every other exception keeps the line and still propagates."""
    refusal = BridgeRefusedError("refused", code="bridge.process_call_failed", status_code=404)
    stderr, raised = _attempt(refusal)
    _check(UNCERTAIN not in stderr and raised is refusal, "BridgeRefusedError propagates with no uncertain line")

    still_uncertain: tuple[tuple[str, BaseException], ...] = (
        ("a timeout", BridgeResultTimeoutError("action ae-1 still 'queued' after 1s")),
        ("KeyboardInterrupt", KeyboardInterrupt()),
        ("SystemExit", SystemExit(143)),
        ("OSError", OSError("connection reset")),
        ("a read timeout", httpx.ReadTimeout("slow")),
        ("a plain BridgeCallError", BridgeCallError("process_call returned no action_id: {}")),
        ("a 5xx BridgeCallError", BridgeCallError("failed (500)", code="bridge.process_call_failed", status_code=500)),
        ("a 4xx BridgeCallError that is not a dispatch refusal", BridgeCallError("failed (404)", code="bridge.action_result_not_found", status_code=404)),
    )
    for label, error in still_uncertain:
        stderr, raised = _attempt(error)
        _check(UNCERTAIN in stderr and raised is error, f"{label} prints the uncertain line and propagates")


def _dispatch_error(dispatch: Callable[[httpx.Request], httpx.Response]) -> BridgeCallError | None:
    """What ``BridgeClient.process_call`` raises for this scripted answer (``None`` if it returns)."""
    client = BridgeClient("http://bridge.invalid", transport=_bridge(dispatch))
    client.open()
    try:
        client.process_call("plugin::agent_messaging_plugin::peer_identity", {})
    except BridgeCallError as exc:
        return exc
    return None


def test_a_4xx_dispatch_raises_the_refusal_type() -> None:
    """Case 7a: the refusal carries the status and the body's own code."""
    for status in (400, 403, 404, 409):
        error = _dispatch_error(lambda _request, s=status: _json(s, {"code": "bridge.process_call_failed", "message": "m"}))
        _check(
            isinstance(error, BridgeRefusedError) and error.status_code == status and error.code == "bridge.process_call_failed",
            f"{status}: BridgeRefusedError carries status_code and code (got {error!r})",
        )


def test_a_5xx_dispatch_is_not_a_refusal() -> None:
    """Case 7b: a server fault leaves the server's state unknown, so it stays a plain error."""
    for status in (500, 502, 503):
        error = _dispatch_error(lambda _request, s=status: _json(s, {"code": "bridge.process_call_failed", "message": "m"}))
        _check(
            type(error) is BridgeCallError and error.status_code == status,
            f"{status}: a plain BridgeCallError carrying status_code (got {error!r})",
        )


def test_an_unusable_success_body_is_not_a_refusal() -> None:
    """Case 7c: a 200 whose body is not an object carries no status to refuse on."""
    error = _dispatch_error(lambda _request: httpx.Response(200, json=["not", "an", "object"]))
    _check(
        type(error) is BridgeCallError and error.status_code is None,
        f"a 200 with an unusable body is a plain BridgeCallError with no status (got {error!r})",
    )


def main() -> int:
    print("bridge CLI refusal-versus-uncertain smoke")
    test_a_definite_refusal_prints_the_refusal_alone()
    test_an_unknown_outcome_keeps_the_uncertain_line()
    test_no_wait_never_printed_the_line()
    test_only_the_refusal_type_skips_the_line()
    test_a_4xx_dispatch_raises_the_refusal_type()
    test_a_5xx_dispatch_is_not_a_refusal()
    test_an_unusable_success_body_is_not_a_refusal()
    if _failures:
        print(f"\nFAIL: {len(_failures)} check(s) failed")
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
