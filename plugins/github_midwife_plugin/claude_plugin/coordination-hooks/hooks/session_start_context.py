#!/usr/bin/env python3
"""Bounded, read-only register and inbox observations for SessionStart.

Python 3.13. No session claims, display acknowledgements, files or register
writes. An env-less session (no launcher identity; desktop app or
hand-started) reads its fleet identity through the read-only
_session_identity.py, and this hook then prints one line naming that identity
and the CLI claim path. Failures are context, never an empty-state assertion
or startup veto.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

_HOOK_DIRECTORY = str(Path(__file__).resolve().parent)
if _HOOK_DIRECTORY not in sys.path:
    sys.path.insert(0, _HOOK_DIRECTORY)
import _session_identity as session_identity  # noqa: E402 -- sibling helper beside this hook

READ_SECONDS = 3.0
DIGEST_BYTES = 5800
INBOX_BYTES = 1000
HORIZON = "1970-01-01T00:00:00Z"
INBOX_PROCESS = "plugin::agent_messaging_plugin::peer_inbox"


class ContextReadError(Exception):
    """A safe error class, never raw child stderr or database credentials."""


def _json_object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContextReadError("InvalidResponse")
    return value


def _json_list(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise ContextReadError("InvalidResponse")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ContextReadError("InvalidResponse")
    return value


def _encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _bounded(text: str, budget: int) -> str:
    if len(text.encode("utf-8")) > budget:
        raise ContextReadError("ByteBudgetExceeded")
    return text


def _run(argv: list[str], timeout: float) -> dict[str, Any]:
    if timeout <= 0:
        raise ContextReadError("TimeoutExpired")
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise ContextReadError("source_unavailable" if result.returncode == 3 else "ProcessExit")
    return _json_object(json.loads(result.stdout))


def register_context() -> str:
    """Read the landed global digest with its smallest supported row limit."""
    default = Path.home() / "Workspace/project-solet/.venv/bin/psolet"
    executable = os.environ.get("PROJECT_SOLET_CLI", str(default))
    value = _run([executable, "resume-digest", "--limit", "1"], READ_SECONDS)
    if value.get("schema") != "project-solet/resume-digest/v1":
        raise ContextReadError("InvalidResponse")
    if value.get("status") != "ok":
        raise ContextReadError("source_unavailable")
    return _bounded(
        "Register-wide resume-digest (bounded data; full reads retain authority):\n" + _encode(value),
        DIGEST_BYTES,
    )


def _page(arguments: dict[str, object], timeout: float, instance: str) -> dict[str, Any]:
    bridge = os.environ.get("SOLET_BRIDGE", "solet-bridge")
    envelope = _run([bridge, "call", INBOX_PROCESS, _encode(arguments)], timeout)
    result = _json_object(envelope.get("result"))
    if result.get("success") is not True:
        raise ContextReadError("BridgeReadFailed")
    data = _json_object(result.get("data"))
    if data.get("recipient_agent_instance_id") != instance:
        raise ContextReadError("IdentityMismatch")
    if data.get("role_section_status") != "ok":
        raise ContextReadError("RoleReadFailed")
    if data.get("role_read_page_status") != "disabled" or data.get("role_read_page_token") is not None:
        raise ContextReadError("ObserverContractMismatch")
    if "next_role_cursor" not in data:
        raise ContextReadError("InvalidResponse")
    horizon = _text(data.get("role_since_created_at"))
    if horizon not in {HORIZON, "1970-01-01T00:00:00", "1970-01-01T00:00:00+00:00"}:
        raise ContextReadError("HorizonMismatch")
    return data


def _roles(page: dict[str, Any]) -> set[str]:
    return {_text(_json_object(row).get("role")) for row in _json_list(page.get("role_read_coverage"))}


def _message_id(entry: dict[str, Any]) -> str:
    return _text(_json_object(entry.get("message")).get("id"))


def _collect_roles(page: dict[str, Any], messages: dict[str, set[str]]) -> None:
    for raw in _json_list(page.get("role_entries")):
        entry = _json_object(raw)
        thread = _text(entry.get("thread_id"))
        role = thread.removeprefix("role:")
        if not thread.startswith("role:") or role not in messages:
            raise ContextReadError("RoleMismatch")
        messages[role].add(_message_id(entry))


def _cursor(value: object, seen: set[str]) -> str:
    cursor = _text(value)
    if cursor in seen:
        raise ContextReadError("RepeatedCursor")
    seen.add(cursor)
    return cursor


def _inbox_arguments() -> tuple[dict[str, object], str]:
    session = os.environ.get("AGENT_SESSION_ID", "")
    instance = os.environ.get("AGENT_INSTANCE_ID", "")
    if not session or not instance or not os.environ.get("SOLET_NAME"):
        raise ContextReadError("MissingIdentity")
    return {
        "agent_session_id": session,
        "observer": True,
        "limit": 100,
        "since": HORIZON,
    }, instance


def _advance_direct(page: dict[str, Any], arguments: dict[str, object], messages: set[str], cursors: set[str]) -> bool:
    messages.update(_message_id(_json_object(row)) for row in _json_list(page.get("entries")))
    if type(page.get("instance_exhausted")) is not bool:
        raise ContextReadError("InvalidResponse")
    done: bool = page["instance_exhausted"]
    if not done:
        arguments.pop("since", None)
        arguments["since_cursor"] = _cursor(page.get("next_since_cursor"), cursors)
    return done


def _advance_roles(
    page: dict[str, Any],
    arguments: dict[str, object],
    messages: dict[str, set[str]],
    cursors: set[str],
) -> bool:
    _collect_roles(page, messages)
    done = page.get("next_role_cursor") is None
    if not done:
        arguments["role_after"] = _cursor(page.get("next_role_cursor"), cursors)
    return done


def _inbox_totals() -> tuple[set[str], dict[str, set[str]]]:
    arguments, instance = _inbox_arguments()
    deadline = time.monotonic() + READ_SECONDS
    direct: set[str] = set()
    role_messages: dict[str, set[str]] | None = None
    direct_cursors: set[str] = set()
    role_cursors: set[str] = set()
    direct_done = role_done = False
    while not (direct_done and role_done):
        page = _page(arguments, deadline - time.monotonic(), instance)
        roles = _roles(page)
        if role_messages is None:
            role_messages = {role: set() for role in roles}
        if roles != set(role_messages):
            raise ContextReadError("BindingChangedDuringRead")
        if not direct_done:
            direct_done = _advance_direct(page, arguments, direct, direct_cursors)
        if not role_done:
            role_done = _advance_roles(page, arguments, role_messages, role_cursors)
    return direct, role_messages or {}


def inbox_context() -> str:
    """Page both sections, never acknowledge a page or invent unread semantics."""
    direct, role_messages = _inbox_totals()
    counts = {role: len(ids) for role, ids in sorted(role_messages.items())}
    return _bounded(
        "Live peer_inbox observer totals (paged observation, not an atomic snapshot).\nCurrent bound roles and pending above role covered mark (display/consumption unknown): " + _encode(counts) + f"\nInstance stored-message count: {len(direct)}; unread: unknown (no platform unread semantics).",
        INBOX_BYTES,
    )


def _observe(reader: Callable[[], str], label: str) -> str:
    try:
        return reader()
    except ContextReadError as error:
        return f"{label} unavailable ({error})."
    except Exception as error:  # noqa: BLE001 -- hook failure must not block startup
        return f"{label} unavailable ({type(error).__name__})."


def _env_less(event: dict[str, Any]) -> Any:
    """Overlay an env-less session's identity; ``None`` under a launcher."""
    try:
        return session_identity.apply(__file__, payload_session_id=str(event.get("session_id") or ""))
    except Exception:  # noqa: BLE001 -- hook failure must not block startup
        return None


def identity_line(identity: Any) -> str:
    """One line naming the overlaid identity and how to claim a role."""
    return f"Fleet identity (env-less session; persisted record keyed by CLAUDE_CODE_SESSION_ID): agent_instance_id={identity.agent_instance_id} agent_session_id={identity.agent_session_id} label={identity.session_label}. Claim a role with: {session_identity.CLAIM_PATH}."


def main() -> int:
    try:
        event = _json_object(json.loads(sys.stdin.read() or "{}"))
    except (AttributeError, OSError, ValueError, ContextReadError):
        event = {}
    if event.get("hook_event_name", "SessionStart") != "SessionStart":
        return 0
    if event.get("source", "startup") not in {"startup", "resume", "clear"}:
        return 0
    identity = _env_less(event)
    lines = [identity_line(identity)] if identity is not None else []
    lines += [
        _observe(register_context, "Register-wide resume-digest"),
        _observe(inbox_context, "peer_inbox count"),
    ]
    context = "\n".join(line for line in lines if line)
    print(
        _encode(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": context,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
