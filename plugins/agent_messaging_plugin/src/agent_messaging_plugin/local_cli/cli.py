"""`homunculus` — invoke a running homunculus over its localhost bridge (no MCP).

Every command discovers THIS homunculus's bridge port from the CLI's own
install location (never a flag or ambient env), opens a one-shot bridge
session, performs the operation, prints the JSON result to stdout, and closes.
Errors go to stderr with a mapped exit code.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, NoReturn

import click
import httpx
from ananta.constants import ExitCodes

from . import __version__
from .client import (
    DEFAULT_POLL_TIMEOUT_S,
    BridgeCallError,
    BridgeClient,
    BridgeResultTimeoutError,
    HomunculusIdentityError,
    HomunculusNotRunningError,
    resolve_base_url,
)


def _emit(payload: dict[str, Any]) -> None:
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


def _die(message: str, code: ExitCodes) -> NoReturn:
    click.echo(f"homunculus: {message}", err=True)
    raise SystemExit(int(code))


def _parse_json_args(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        _die(f"arguments must be a JSON object: {exc}", ExitCodes.UNKNOWN_ERROR)
    if not isinstance(parsed, dict):
        _die(
            'arguments must be a JSON object, e.g. \'{"query": "..."}\'',
            ExitCodes.UNKNOWN_ERROR,
        )
    return parsed


def _run(fn: Callable[[BridgeClient], dict[str, Any]]) -> dict[str, Any]:
    """Open a bridge for THIS homunculus, run ``fn`` against it, map failures."""
    try:
        base_url = resolve_base_url()
    except (HomunculusNotRunningError, HomunculusIdentityError) as exc:
        _die(str(exc), ExitCodes.CONNECTION_ERROR)
    try:
        with BridgeClient(base_url) as client:
            return fn(client)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        _die(f"cannot reach the homunculus bridge at {base_url}: {exc}",
             ExitCodes.CONNECTION_ERROR)
    except BridgeResultTimeoutError as exc:
        _die(str(exc), ExitCodes.TIMEOUT_ERROR)
    except (BridgeCallError, httpx.HTTPError) as exc:
        _die(str(exc), ExitCodes.EXTERNAL_ERROR)


@click.group()
@click.version_option(__version__, prog_name="homunculus")
def cli() -> None:
    """Invoke this homunculus's capabilities over its localhost bridge (no MCP)."""


@cli.command()
@click.argument("process_key")
@click.argument("arguments", default="{}")
@click.option("--reason", default=None, help="Optional human reason for the call.")
@click.option(
    "--timeout",
    "timeout_s",
    type=float,
    default=DEFAULT_POLL_TIMEOUT_S,
    show_default=True,
    help="Seconds to wait for the result before giving up.",
)
def call(
    process_key: str,
    arguments: str,
    reason: str | None,
    timeout_s: float,
) -> None:
    """Invoke PROCESS_KEY with ARGUMENTS (a JSON object) and wait for the result."""
    args = _parse_json_args(arguments)
    result = _run(
        lambda c: c.call_and_wait(
            process_key, args, reason=reason, poll_timeout_s=timeout_s,
        ),
    )
    _emit(result)
    if str(result.get("status")) != "completed":
        raise SystemExit(int(ExitCodes.EXTERNAL_ERROR))


@cli.command()
@click.argument("query")
@click.option("--max-results", "-n", type=int, default=8, show_default=True)
def search(query: str, max_results: int) -> None:
    """Discover process keys by semantic QUERY."""
    _emit(_run(lambda c: c.process_search(query, max_results)))


@cli.command()
@click.argument("process_key")
def schema(process_key: str) -> None:
    """Fetch the argument schema for PROCESS_KEY."""
    _emit(_run(lambda c: c.process_schema(process_key)))


@cli.command()
@click.argument("action_id")
@click.option(
    "--wait/--no-wait",
    default=False,
    help="Poll until the action reaches a terminal state.",
)
@click.option(
    "--timeout",
    "timeout_s",
    type=float,
    default=DEFAULT_POLL_TIMEOUT_S,
    show_default=True,
)
def result(action_id: str, wait: bool, timeout_s: float) -> None:
    """Fetch (or --wait for) the result of a previously dispatched ACTION_ID."""
    if wait:
        payload = _run(
            lambda c: c.wait_for_result(action_id, poll_timeout_s=timeout_s),
        )
    else:
        payload = _run(lambda c: c.process_result(action_id))
    _emit(payload)


@cli.command()
def health() -> None:
    """Check whether the homunculus bridge is answering."""
    try:
        base_url = resolve_base_url()
    except (HomunculusNotRunningError, HomunculusIdentityError) as exc:
        _die(str(exc), ExitCodes.CONNECTION_ERROR)
    client = BridgeClient(base_url)
    try:
        payload = client.health()
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        _die(f"cannot reach the homunculus bridge at {base_url}: {exc}",
             ExitCodes.CONNECTION_ERROR)
    except httpx.HTTPError as exc:
        _die(str(exc), ExitCodes.EXTERNAL_ERROR)
    finally:
        client.close()
    _emit(payload)


def main() -> None:
    """Console-script entry point."""
    cli()


if __name__ == "__main__":
    main()
