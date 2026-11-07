"""`<name> attend` — register an operator-attended session through its OAuth client.

A terminal the operator attends directly has no launcher env (no
``AGENT_SESSION_ID``, ``AGENT_SESSION_LABEL`` or ``AGENT_INSTANCE_ID``) and may
have no MCP configuration. This command joins it to fleet identity using ONLY
a credential the bridge already authenticates: an operator-registered OAuth
client (``service_interface::vault_service::oauth_client_register``, grant
``client_credentials``).

The command obtains a bearer at the bridge's ``/oauth/token``, opens the
bearer-verified Streamable HTTP session, and reads the identity the server
sealed from that bearer back with ``current_identity``. It then holds
presence on the session's SSE stream, printing one JSON line per delivery,
re-arms with a fresh token whenever the stream ends, and DELETEs its session
on exit.

No flag, environment variable, PID or pane ever supplies identity: the server
seals ``agi-oauth-<client_id>`` from the verified credential, and anyone
holding that client's secret IS that identity. There is no role claim, no
catch-up drain and no wake pairing here.

Refusals (each is an authentication or input boundary):

* R1 token refused — the bridge issued no token on the first arm. Pass
  condition: an operator-approved client whose grant types include
  ``client_credentials``.
* R2 unauthenticated identity — the session's identity is not
  bearer-verified (``identity_trust`` is ``outer_boundary_only`` or
  ``streamable_no_auth`` is set). Pass condition: the bridge runs its real
  bearer verifier (``streamable_no_auth`` false).
* R3 revoked on re-arm — the token endpoint answers 400/401/403 with an
  OAuth error code. Pass condition unchanged (the client still exists);
  an unavailable bridge (5xx, 429, transport error, no port file) is not
  refused: it prints an ``attend=retry`` advisory and re-resolves the base.
* R4 empty secret — local input validation.
"""

from __future__ import annotations

import json
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, NoReturn

import click
import httpx
from ananta.constants import ExitCodes

from . import __version__
from .client import SoletIdentityError, SoletNotRunningError, resolve_base_url

# Wire constants. Each mirrors a server symbol; the local CLI import chain
# must not pull in mcp_streamable (fastapi), so a smoke pins the equality.
# mcp_streamable/router.py STREAMABLE_PATH (line 76).
STREAMABLE_PATH: Final[str] = "/api/v1/mcp/streamable"
# mcp_streamable/oauth.py OAUTH_TOKEN_PATH (line 93).
TOKEN_PATH: Final[str] = "/oauth/token"
# mcp_streamable/tools.py SUPPORTED_PROTOCOL_VERSION (line 448).
PROTOCOL_VERSION: Final[str] = "2025-03-26"
# The literal mcp_streamable/dispatch.py _identity_trust returns for a
# no-auth session (line 983).
OUTER_BOUNDARY_TRUST: Final[str] = "outer_boundary_only"

ATTEND_RECONNECT_DELAY_S: Final[float] = 2.0
ATTEND_REQUEST_TIMEOUT_S: Final[float] = 30.0
# The server sends an SSE heartbeat comment every ~25 s of silence
# (notifications.py _LONG_POLL_TIMEOUT_S), so three missed beats is a dead
# stream, which re-arms like any other stream end.
ATTEND_STREAM_READ_TIMEOUT_S: Final[float] = 75.0

_SESSION_HEADER: Final[str] = "Mcp-Session-Id"
_REFUSAL_STATUSES: Final[frozenset[int]] = frozenset({400, 401, 403})
_OAUTH_REFUSAL_CODES: Final[frozenset[str]] = frozenset(
    {"invalid_client", "unauthorized_client", "invalid_grant", "invalid_target", "invalid_request"},
)


class _UnavailableError(Exception):
    """The bridge could not complete a step; not a refusal of the client."""


class _TokenRefusedError(Exception):
    """The token endpoint answered something other than a token."""

    def __init__(self, status: int, code: str) -> None:
        self.status = status
        self.code = code
        super().__init__(f"{status} {code}".strip())

    @property
    def is_oauth_refusal(self) -> bool:
        return self.status in _REFUSAL_STATUSES and self.code in _OAUTH_REFUSAL_CODES


class _RefusalError(Exception):
    """A designed refusal; converted to an exit outside the interrupt handler."""

    def __init__(self, message: str, code: ExitCodes) -> None:
        self.code = code
        super().__init__(message)


@dataclass
class _Attendance:
    client_id: str
    secret: str
    http: httpx.Client
    base: str = ""
    session_id: str = ""

    def fetch_token(self) -> str:
        response = self.http.post(
            self.base + TOKEN_PATH,
            data={"grant_type": "client_credentials"},
            auth=httpx.BasicAuth(self.client_id, self.secret),
        )
        if response.status_code != 200:
            raise _TokenRefusedError(response.status_code, _oauth_error_code(response))
        token = _json_dict(response).get("access_token")
        if not isinstance(token, str) or not token:
            raise _UnavailableError("the token response carried no access_token")
        return token

    def rpc(self, token: str, payload: dict[str, Any], step: str) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers[_SESSION_HEADER] = self.session_id
        response = self.http.post(self.base + STREAMABLE_PATH, json=payload, headers=headers)
        if not response.is_success:
            raise _UnavailableError(f"{step} answered HTTP {response.status_code}")
        return response

    def open_session(self, token: str) -> None:
        response = self.rpc(
            token,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "solet-bridge attend", "version": __version__},
                },
            },
            "initialize",
        )
        session_id = response.headers.get(_SESSION_HEADER, "")
        if not session_id:
            raise _UnavailableError(f"initialize returned no {_SESSION_HEADER}")
        self.session_id = session_id
        self.rpc(token, {"jsonrpc": "2.0", "method": "notifications/initialized"}, "initialized")

    def read_identity(self, token: str) -> dict[str, Any]:
        response = self.rpc(
            token,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "current_identity", "arguments": {}},
            },
            "current_identity",
        )
        result = _json_dict(response).get("result")
        structured = result.get("structuredContent") if isinstance(result, dict) else None
        if not isinstance(structured, dict):
            raise _UnavailableError("current_identity returned no structuredContent")
        return structured

    def delete_session(self) -> None:
        """Best-effort DELETE of the current session with a fresh token."""
        if not self.session_id:
            return
        session_id, self.session_id = self.session_id, ""
        try:
            token = self.fetch_token()
            self.http.delete(
                self.base + STREAMABLE_PATH,
                headers={"Authorization": f"Bearer {token}", _SESSION_HEADER: session_id},
            )
        except (_TokenRefusedError, _UnavailableError, httpx.HTTPError):
            return

    def arm(self) -> None:
        """Steps (ii)-(vi): resolve, token, initialize, read and check identity."""
        self.base = resolve_base_url()
        token = self.fetch_token()
        try:
            self.open_session(token)
            identity = self.read_identity(token)
        except (_UnavailableError, httpx.HTTPError):
            self.delete_session()
            raise
        if not _is_authenticated(identity):
            self.delete_session()
            raise _RefusalError(
                f"attend refused: the bridge did not authenticate this identity (identity_trust={identity.get('identity_trust')})",
                ExitCodes.EXTERNAL_ERROR,
            )
        _emit({"attend": "armed", **{key: identity.get(key) for key in _ARMED_KEYS}})

    def hold(self) -> None:
        """Step (vii): hold the SSE stream, one line per JSON-RPC notification."""
        token = self.fetch_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
            _SESSION_HEADER: self.session_id,
        }
        timeout = httpx.Timeout(ATTEND_REQUEST_TIMEOUT_S, read=ATTEND_STREAM_READ_TIMEOUT_S)
        with self.http.stream("GET", self.base + STREAMABLE_PATH, headers=headers, timeout=timeout) as response:
            if not response.is_success:
                return
            for line in response.iter_lines():
                _emit_sse_line(line)


_ARMED_KEYS: Final[tuple[str, ...]] = (
    "agent_id",
    "agent_instance_id",
    "agent_session_id",
    "session_label",
    "identity_trust",
    "mcp_session_id",
)


def _emit(payload: dict[str, Any]) -> None:
    click.echo(json.dumps(payload, sort_keys=True))


def _emit_sse_line(line: str) -> None:
    if not line.startswith("data:"):
        return
    try:
        message = json.loads(line[len("data:") :].strip())
    except json.JSONDecodeError:
        return
    if isinstance(message, dict) and "method" in message and "id" not in message:
        _emit({"attend": "event", "params": message.get("params")})


def _json_dict(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _oauth_error_code(response: httpx.Response) -> str:
    code = _json_dict(response).get("error")
    return code if isinstance(code, str) else ""


def _is_authenticated(identity: dict[str, Any]) -> bool:
    return bool(identity.get("agent_instance_id")) and identity.get("identity_trust") != OUTER_BOUNDARY_TRUST and not identity.get("streamable_no_auth")


def _first_arm(attendance: _Attendance) -> None:
    """The first arm keeps hard exits: an operator is watching this terminal."""
    try:
        attendance.arm()
    except (SoletNotRunningError, SoletIdentityError) as exc:
        raise _RefusalError(str(exc), ExitCodes.CONNECTION_ERROR) from exc
    except _TokenRefusedError as exc:
        raise _RefusalError(
            f"attend refused: the bridge did not issue a token for client {attendance.client_id} ({exc})",
            ExitCodes.EXTERNAL_ERROR,
        ) from exc
    except httpx.TransportError as exc:
        raise _RefusalError(
            f"attend: cannot reach the solet bridge at {attendance.base}: {exc}",
            ExitCodes.CONNECTION_ERROR,
        ) from exc
    except (_UnavailableError, httpx.HTTPError) as exc:
        raise _RefusalError(f"attend: {exc}", ExitCodes.EXTERNAL_ERROR) from exc


def _rearm(attendance: _Attendance) -> None:
    """Step (viii): re-arm until armed; only an OAuth refusal ends the loop."""
    while True:
        time.sleep(ATTEND_RECONNECT_DELAY_S)
        try:
            attendance.arm()
            return
        except _TokenRefusedError as exc:
            if exc.is_oauth_refusal:
                raise _RefusalError(
                    f"attend: the bridge no longer accepts client {attendance.client_id} ({exc.status})",
                    ExitCodes.EXTERNAL_ERROR,
                ) from exc
            reason = f"token endpoint answered {exc}"
        except (SoletNotRunningError, SoletIdentityError, _UnavailableError) as exc:
            reason = str(exc)
        except httpx.HTTPError as exc:
            reason = f"{type(exc).__name__}: {exc}"
        _emit({"attend": "retry", "reason": reason})


def _hold_until_stopped(attendance: _Attendance) -> None:
    while True:
        try:
            attendance.hold()
        except (_TokenRefusedError, _UnavailableError, httpx.HTTPError):
            pass
        attendance.delete_session()
        _rearm(attendance)


def _install_sigterm_unwind() -> None:
    """SIGTERM -> SystemExit, so the session DELETE on exit still runs."""

    def _terminate(signum: int, _frame: object) -> NoReturn:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _terminate)


def _read_secret(path: str) -> str:
    secret = Path(path).read_text(encoding="utf-8").strip()
    if not secret:
        click.echo("attend needs a non-empty client secret in --client-secret-file", err=True)
        raise SystemExit(int(ExitCodes.UNKNOWN_ERROR))
    return secret


@click.command(name="attend")
@click.option(
    "--client-id",
    required=True,
    help="An operator-registered OAuth client (grant client_credentials).",
)
@click.option(
    "--client-secret-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="File holding that client's secret; read once, never echoed.",
)
def attend(client_id: str, client_secret_file: str) -> None:
    """Join this attended session to fleet identity through its OAuth client.

    Prints one JSON line when armed (identity as the server sealed it), one per
    delivered message, and one per re-arm retry. Exits 0 on interrupt.
    """
    secret = _read_secret(client_secret_file)
    _install_sigterm_unwind()
    with httpx.Client(timeout=ATTEND_REQUEST_TIMEOUT_S) as http:
        attendance = _Attendance(client_id=client_id, secret=secret, http=http)
        try:
            _first_arm(attendance)
            _hold_until_stopped(attendance)
        except _RefusalError as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(int(exc.code)) from exc
        except (KeyboardInterrupt, SystemExit):
            attendance.delete_session()
            raise SystemExit(0) from None
