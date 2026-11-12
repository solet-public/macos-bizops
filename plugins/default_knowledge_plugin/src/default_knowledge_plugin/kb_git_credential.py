"""Fixed Git clone/fetch credential consumer and inherited-pipe helper.

The helper receives only a descriptor number in argv. Credential bytes travel
through that descriptor and Git's credential protocol, never environment or
configuration text. There is no service or arbitrary-command entry point.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit


class GitTransportError(RuntimeError):
    """Public operation category and exit status, without child diagnostics."""


def _git_environment() -> dict[str, str]:
    """Keep this credential consumer's diagnostic channels free of auth data."""
    return dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_TRACE="0", GIT_TRACE_CURL="0", GIT_CURL_VERBOSE="0", GIT_TRACE_PACKET="0", GIT_TRACE2="0", GIT_TRACE2_EVENT="0", GIT_TRACE2_PERF="0", GIT_TRACE2_CONFIG_PARAMS="", GIT_TRACE2_ENV_VARS="")


def _public_origin(directory: Path, origin: str) -> None:
    """Replace an old clone's credential URL before any remote interaction."""
    if not origin:
        return
    try:
        result = subprocess.run(["git", "remote", "set-url", "origin", origin], cwd=directory, env=_git_environment(), capture_output=True, check=False)
    except OSError:
        raise GitTransportError("git fetch origin update could not start") from None
    if result.returncode:
        raise GitTransportError(f"git fetch origin update failed (exit {result.returncode})")


def _write_credential(descriptor: int, payload: bytes) -> None:
    try:
        with os.fdopen(descriptor, "wb") as writer:
            writer.write(payload)
    except BrokenPipeError:
        # Git may fail or be cancelled before it requests authentication.
        return


def run_git_transport(
    operation: Literal["clone", "fetch"],
    origin: str,
    directory: Path,
    token: str | None,
) -> None:
    """Execute only the two shipped authenticated Git operations."""
    if operation == "fetch":
        _public_origin(directory, origin)
    reader, writer = os.pipe()
    parsed = urlsplit(origin)
    payload = json.dumps({"protocol": parsed.scheme, "host": parsed.netloc, "token": token}).encode()
    feeder = threading.Thread(target=_write_credential, args=(writer, payload), daemon=True)
    helper = f"!{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} --fd {reader}"
    argv = ["git", "-c", "credential.helper=", "-c", f"credential.helper={helper}", "-c", "credential.interactive=false"]
    if operation == "clone":
        argv.extend(["clone", "--", origin, str(directory)])
        cwd = None
    else:
        argv.extend(["fetch", "origin"])
        cwd = directory
    environment = _git_environment()
    feeder.start()
    try:
        try:
            result = subprocess.run(argv, cwd=cwd, env=environment, pass_fds=(reader,), capture_output=True, check=False)
        except OSError:
            raise GitTransportError(f"git {operation} could not start") from None
        if result.returncode:
            raise GitTransportError(f"git {operation} failed (exit {result.returncode})")
    finally:
        os.close(reader)
        feeder.join()


def _request_fields() -> dict[str, str]:
    """Read Git's public protocol/host request up to its blank terminator."""
    request: dict[str, str] = {}
    for line in sys.stdin:
        if line == "\n":
            break
        key, separator, value = line.rstrip("\n").partition("=")
        if separator:
            request[key] = value
    return request


def _valid_token(token: object) -> bool:
    """Prevent a credential from injecting additional Git protocol fields."""
    return isinstance(token, str) and bool(token) and not any(character in token for character in ("\n", "\r", "\0"))


def _serve(descriptor: int, operation: str) -> None:
    """Answer Git's get only, for the exact original protocol and host."""
    if operation != "get":
        return
    request = _request_fields()
    with os.fdopen(descriptor, "rb") as reader:
        credential = json.load(reader)
    if request.get("protocol") != credential["protocol"] or request.get("host") != credential["host"]:
        return
    token = credential["token"]
    if not _valid_token(token):
        return
    sys.stdout.write(f"username=x-access-token\npassword={token}\n\n")


if __name__ == "__main__":
    try:
        if len(sys.argv) == 4 and sys.argv[1] == "--fd":
            _serve(int(sys.argv[2]), sys.argv[3])
    except Exception:
        # A malformed exchange yields no credential, never a credential-bearing
        # parser exception or traceback on Git's inherited stderr.
        raise SystemExit(1) from None
