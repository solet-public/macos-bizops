#!/usr/bin/env python3
"""Bounded, read-only peer-inbox and principle-statement observation for SessionStart.

Python 3.13. No session claims, display acknowledgements, files or register
writes. The principle statement is read from the solet's own knowledge-base file, and through its bridge only when no
file is found; the inbox is read through the bridge. An env-less session (no launcher identity; desktop app or
hand-started) reads its fleet identity through the read-only
_session_identity.py, and this hook then prints one line naming that identity
and the CLI claim path. Failures are context, never an empty-state assertion
or startup veto. It answers startup, resume, clear and compact.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
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

# The inbox read and the bridge read of the principles (used only when no statement file is found) are each capped here.
# The principles are read first, from the solet's own file when there is one, which no host load slows, so with a file the
# worst case is this one cap, 3.5 s, and with no file 2 x READ_SECONDS = 7.0 s, both under the 30 s hooks.json timeout. A bridge
# read measured 1.8 to 3.0 s at host load 5 and 7.5 to 61.6 s at load above 30, so no cap on it is both short and safe; a
# timeout drops the principles from the session start, which is why the file comes first.
READ_SECONDS = 3.5
INBOX_BYTES = 1000
# The host limits one additionalContext string to 10,000 characters; the principle text, the inbox and identity
# lines and the label together stay under it with this cap.
PRINCIPLE_BYTES = 8000
# After a compaction the host runs SessionStart again with source "compact"; the hook answers it as it answers a start, so the
# principle statement is in the session's context again. Any other source gets no output.
ACTIVATION_SOURCES = frozenset({"startup", "resume", "clear", "compact"})
HORIZON = "1970-01-01T00:00:00Z"
INBOX_PROCESS = "plugin::agent_messaging_plugin::peer_inbox"
PRINCIPLE_PROCESS = "service_interface::knowledge_service::read_file"
PRINCIPLE_ARGUMENTS = {"name": "ananta_platform", "path": "24_operator_communication/principles_statement.md"}
STATEMENT_FILE = Path("ananta/knowledge_bases", PRINCIPLE_ARGUMENTS["name"], PRINCIPLE_ARGUMENTS["path"])  # where a solet keeps the article, under its root
PRINCIPLE_HEADING = "## Approved statement"
PRINCIPLE_LABEL = "Principles (this solet's knowledge base, ananta_platform/24_operator_communication/principles_statement.md):"
PRINCIPLE_CLIPPED = f"Principles clipped at {PRINCIPLE_BYTES} bytes: the article is longer."
_MARKDOWN_HEADING = re.compile(r"#{1,6}\s")


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


def _approved_statement(article: str) -> str:
    """The text under the approved-statement heading, up to the next heading or the end of the article."""
    section: list[str] | None = None
    for line in article.splitlines():
        if section is None:
            if line.rstrip() == PRINCIPLE_HEADING:
                section = []
        elif _MARKDOWN_HEADING.match(line):
            break
        else:
            section.append(line)
    text = "\n".join(section or []).strip()
    if not text:
        raise ContextReadError("StatementMissing")
    return text


def _clipped(text: str) -> tuple[str, bool]:
    """``text`` within PRINCIPLE_BYTES at a whole-line boundary; one line longer than the cap is cut by bytes."""
    if len(text.encode("utf-8")) <= PRINCIPLE_BYTES:
        return text, False
    kept: list[str] = []
    size = 0
    for line in text.split("\n"):
        added = len(line.encode("utf-8")) + (1 if kept else 0)
        if size + added > PRINCIPLE_BYTES:
            break
        kept.append(line)
        size += added
    if kept:
        return "\n".join(kept), True
    return text.encode("utf-8")[:PRINCIPLE_BYTES].decode("utf-8", errors="ignore"), True


def _bridge_root() -> Path | None:
    """The tree the bridge in use runs from: ``<root>/.venv/bin/solet-bridge`` in a checkout, ``<release>/venv/bin/solet-bridge`` in a deployed release."""
    bridge = os.environ.get("SOLET_BRIDGE", "solet-bridge")
    if os.sep in bridge:
        located: Path | None = Path(bridge)
    else:  # a bare name is the first PATH directory that holds a file of that name
        located = next((Path(directory) / bridge for directory in os.environ.get("PATH", "").split(os.pathsep) if directory and (Path(directory) / bridge).is_file()), None)
    if located is None:
        return None
    parents = located.resolve().parents
    return parents[2] if len(parents) > 2 else None


def _statement_roots() -> list[Path]:
    """Where the solet's own knowledge base may be, in order: the project directory the host names, the working directory, the tree the bridge runs from, and that tree's ``code`` directory (a release's layout)."""
    roots = [Path(os.environ["CLAUDE_PROJECT_DIR"])] if os.environ.get("CLAUDE_PROJECT_DIR") else []
    with contextlib.suppress(OSError):  # a deleted working directory is no root, and no reason to fail a start
        roots.append(Path.cwd())
    bridge_root = _bridge_root()
    if bridge_root is not None:
        roots += [bridge_root, bridge_root / "code"]
    return roots


def _statement_file() -> Path | None:
    """The first regular file at the article's place under a root, or None when no root holds one."""
    return next((path for root in _statement_roots() if (path := root / STATEMENT_FILE).is_file()), None)


def _bridge_article(timeout: float) -> str:
    """The article text, read from the solet's knowledge base through its bridge within ``timeout``."""
    bridge = os.environ.get("SOLET_BRIDGE", "solet-bridge")
    job = _run([bridge, "call", PRINCIPLE_PROCESS, _encode(PRINCIPLE_ARGUMENTS)], timeout)
    if job.get("status") != "completed":
        raise ContextReadError("BridgeReadFailed")
    result = _json_object(job.get("result"))
    if result.get("status") != "success":
        raise ContextReadError("BridgeReadFailed")
    return _text(result.get("content"))


def principles_context(timeout: float) -> str:
    """The solet's own principle statement: from its knowledge-base file on disk, which no host load can slow, and through its bridge within ``timeout`` only when no such file is found."""
    path = _statement_file()
    article = path.read_text(encoding="utf-8") if path is not None else _bridge_article(timeout)
    text, clipped = _clipped(_approved_statement(article))
    block = f"{PRINCIPLE_LABEL}\n{text}"
    return f"{block}\n{PRINCIPLE_CLIPPED}" if clipped else block


def _page(arguments: dict[str, object], timeout: float, instance: str) -> dict[str, Any]:
    bridge = os.environ.get("SOLET_BRIDGE", "solet-bridge")
    envelope = _run([bridge, "call", INBOX_PROCESS, _encode(arguments)], timeout)
    result = _json_object(envelope.get("result"))
    if result.get("success") is not True and result.get("action_status") != "completed":
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


def _inbox_totals(timeout: float) -> tuple[set[str], dict[str, set[str]]]:
    arguments, instance = _inbox_arguments()
    deadline = time.monotonic() + timeout
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


def inbox_context(timeout: float) -> str:
    """Page both sections within ``timeout``, never acknowledge a page or invent unread semantics."""
    direct, role_messages = _inbox_totals(timeout)
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
    if event.get("source", "startup") not in ACTIVATION_SOURCES:
        return 0
    identity = _env_less(event)
    principles = _observe(lambda: principles_context(READ_SECONDS), "Principles")  # first: in hand before the one read that can stall
    inbox = _observe(lambda: inbox_context(READ_SECONDS), "peer_inbox count")
    lines = [identity_line(identity)] if identity is not None else []
    lines += [inbox, principles]
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
