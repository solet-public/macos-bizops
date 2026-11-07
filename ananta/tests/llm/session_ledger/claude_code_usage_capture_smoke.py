#!/usr/bin/env python3
"""T1 usage-capture lane (2026-08-05 ruling, S2b) — the claude_code vendor
parser threads a message line's per-turn ``usage`` dict through to
``RawSessionEvent.payload['usage']``. Plugin normalization coverage lives with
the plugin that owns it, so capability bundles without that plugin still ship
this vendor-parser smoke.

Covers the tool-only-turn gap a naive "attach usage to the text event"
design would silently drop: an assistant turn with ONLY tool_use blocks
(no text at all) still emits a MESSAGE event (empty content_text) to
carry the line's usage figure -- without this, every tool-only assistant
turn would undercount real spend, defeating this wave's whole purpose.

Run:
    .venv/bin/python3 \
        ananta/tests/llm/session_ledger/claude_code_usage_capture_smoke.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.vendor import claude_code as vendor  # noqa: E402

_passed = 0
_failed: list[str] = []

_USAGE = {
    "input_tokens": 12,
    "output_tokens": 34,
    "cache_creation_input_tokens": 5,
    "cache_read_input_tokens": 6,
}


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _line(*, msg_type: str, content: object, usage: dict[str, object] | None = None) -> str:
    message: dict[str, object] = {
        "role": "assistant" if msg_type == "assistant" else "user",
        "content": content,
    }
    if usage is not None:
        message["usage"] = usage
    return json.dumps(
        {
            "type": msg_type,
            "uuid": "u-usage-1",
            "sessionId": "s-usage-1",
            "timestamp": "2026-08-05T00:00:00.000Z",
            "message": message,
        }
    )


def test_assistant_text_line_carries_usage_through_to_normalized() -> None:
    line = _line(msg_type="assistant", content=[{"type": "text", "text": "hi"}], usage=_USAGE)
    raw_events = vendor.parse_line(line)
    _check(len(raw_events) == 1, "one text block -> one raw event")
    _check(raw_events[0].payload.get("usage") == _USAGE, "raw payload carries usage verbatim")


def test_user_line_has_no_usage_field() -> None:
    line = _line(msg_type="user", content=[{"type": "text", "text": "hello"}])
    raw_events = vendor.parse_line(line)
    _check(raw_events[0].payload.get("usage") is None, "a user line's payload carries no usage key")


def test_tool_only_assistant_turn_still_carries_usage() -> None:
    """The gap this slice's own fix closes: NO text block at all, only a
    tool_use block -- without the ``or usage`` fix in _parse_message_blocks,
    this would emit ONLY the TOOL_CALL event and silently drop usage."""
    content = [
        {"type": "tool_use", "id": "tu-1", "name": "Bash", "input": {"command": "ls"}},
    ]
    line = _line(msg_type="assistant", content=content, usage=_USAGE)
    raw_events = vendor.parse_line(line)
    _check(
        len(raw_events) == 2,
        f"tool-only turn with usage yields 2 events (message carrier + tool_call), got {len(raw_events)}",
    )
    message_events = [event for event in raw_events if event.payload.get("kind") == "message"]
    tool_events = [event for event in raw_events if event.payload.get("kind") == "tool_call"]
    _check(
        len(message_events) == 1,
        "exactly one MESSAGE carrier event is emitted for the tool-only turn",
    )
    _check(
        message_events and message_events[0].payload.get("usage") == _USAGE,
        "MESSAGE carrier preserves line usage",
    )
    _check(
        tool_events and tool_events[0].payload.get("usage") is None,
        "TOOL_CALL does not duplicate line usage",
    )


def test_tool_only_assistant_turn_without_usage_emits_no_carrier() -> None:
    """GREEN companion: when there's genuinely no usage AND no text, no
    empty-carrier MESSAGE event is manufactured -- only the real
    TOOL_CALL event, matching the pre-existing (pre-this-slice) behavior."""
    content = [
        {"type": "tool_use", "id": "tu-2", "name": "Read", "input": {"file_path": "/tmp/x"}},
    ]
    line = _line(msg_type="assistant", content=content, usage=None)
    raw_events = vendor.parse_line(line)
    _check(
        len(raw_events) == 1,
        "tool-only turn with NO usage and no text yields exactly the one TOOL_CALL event -- no manufactured empty carrier",
    )


def test_legacy_string_content_carries_usage() -> None:
    """Legacy single-string ``message.content`` (early Claude Code
    versions) -- usage still threads through the direct-string branch."""
    line = json.dumps(
        {
            "type": "assistant",
            "uuid": "u-legacy-1",
            "sessionId": "s-usage-1",
            "timestamp": "2026-08-05T00:00:00.000Z",
            "message": {"role": "assistant", "content": "plain legacy text", "usage": _USAGE},
        }
    )
    raw_events = vendor.parse_line(line)
    _check(len(raw_events) == 1, "legacy string content -> one raw event")
    _check(
        raw_events[0].payload.get("usage") == _USAGE,
        "legacy-string branch carries usage verbatim too",
    )


def test_multiple_text_blocks_usage_attached_once() -> None:
    content = [
        {"type": "text", "text": "part one"},
        {"type": "text", "text": "part two"},
    ]
    line = _line(msg_type="assistant", content=content, usage=_USAGE)
    raw_events = vendor.parse_line(line)
    _check(len(raw_events) == 1, "multiple text blocks still merge into ONE message event")
    _check(
        raw_events[0].payload.get("usage") == _USAGE,
        "usage is attached once to the merged event, not per text block",
    )


def _identity_line(content: object, message_id: str = "A", uuid: str = "line-a") -> dict[str, object]:
    return {
        "type": "assistant",
        "uuid": uuid,
        "sessionId": "s-identity",
        "timestamp": "2026-10-17T00:00:00Z",
        "message": {"role": "assistant", "content": content, "model": "claude-test", "id": message_id, "usage": {"output_tokens": 100 if message_id == "A" else 50, "details": {"cache": 3}}},
    }


def _identity_events() -> list[list[object]]:
    contents = [[{"type": "text", "text": "hi"}], [{"type": "tool_use", "id": "tool-a", "name": "Read", "input": {}}], "legacy text"]
    return [list(vendor.parse_line_data(_identity_line(content))) for content in contents]


def check_model() -> None:
    carriers = [next(e for e in group if e.payload["kind"] == "message") for group in _identity_events()]
    assert all(e.payload["usage"].get("model") == "claude-test" for e in carriers), "CLAUDE_MESSAGE_MODEL_CARRIED"
    source = _identity_line("text")
    # Source usage identity wins; enrichment preserves nested details and source input.
    source["message"]["usage"].update(model="source-model", message_id="source-id")
    carried = list(vendor.parse_line_data(source))[0].payload["usage"]
    assert carried == source["message"]["usage"] and carried is not source["message"]["usage"], "CLAUDE_IDENTITY_ONLY_ON_MESSAGE: source precedence/copy"
    print("PASS CLAUDE_MESSAGE_MODEL_CARRIED")


def check_id() -> None:
    carriers = [next(e for e in group if e.payload["kind"] == "message") for group in _identity_events()]
    assert all(e.payload["usage"].get("message_id") == "A" for e in carriers), "CLAUDE_MESSAGE_ID_CARRIED"
    print("PASS CLAUDE_MESSAGE_ID_CARRIED")


def check_dedup() -> None:
    repeated = [list(vendor.parse_line_data(_identity_line("repeat", mid, f"line-{i}")))[0] for i, mid in enumerate(("A", "A", "B", "B", "B"))]
    folded: dict[str, dict[str, object]] = {}
    for event in repeated:
        usage = event.payload["usage"]
        assert "message_id" in usage, "CLAUDE_USAGE_DEDUPED_BY_MESSAGE_ID: missing identity"
        folded.setdefault(usage["message_id"], usage)
    naive = sum(e.payload["usage"]["output_tokens"] for e in repeated)
    dedup = sum(u["output_tokens"] for u in folded.values())
    assert len(repeated) == 5 and len({e.vendor_event_id for e in repeated}) == 5 and len(folded) == 2 and naive == 350 and dedup == 150 and naive * 3 == dedup * 7, "CLAUDE_USAGE_DEDUPED_BY_MESSAGE_ID"
    print("PASS CLAUDE_USAGE_DEDUPED_BY_MESSAGE_ID")


def check_only_message() -> None:
    events = _identity_events()
    contents = [None, [{"type": "tool_use", "id": "tool-a", "name": "Read", "input": {}}]]
    tools = [e for group in events for e in group if e.payload["kind"] != "message"]
    source = _identity_line("text")
    before = json.dumps(source, sort_keys=True)
    list(vendor.parse_line_data(source))
    no_usage = _identity_line(contents[1])
    del no_usage["message"]["usage"]
    without = list(vendor.parse_line_data(no_usage))
    plain = json.loads(json.dumps(no_usage))
    del plain["message"]["model"]
    del plain["message"]["id"]
    assert not any(e.payload.get("usage") for e in tools) and len(without) == 1 and without == list(vendor.parse_line_data(plain)) and json.dumps(source, sort_keys=True) == before, "CLAUDE_IDENTITY_ONLY_ON_MESSAGE"
    for content in ("legacy", [{"type": "text", "text": "text"}]):
        source = _identity_line(content)
        del source["message"]["usage"]
        enriched = list(vendor.parse_line_data(source))
        del source["message"]["model"]
        del source["message"]["id"]
        assert enriched == list(vendor.parse_line_data(source)), "CLAUDE_IDENTITY_ONLY_ON_MESSAGE: usage-free bytes"
    print("PASS CLAUDE_IDENTITY_ONLY_ON_MESSAGE")


IDENTITY_CASES = {
    "CLAUDE_MESSAGE_MODEL_CARRIED": check_model,
    "CLAUDE_MESSAGE_ID_CARRIED": check_id,
    "CLAUDE_USAGE_DEDUPED_BY_MESSAGE_ID": check_dedup,
    "CLAUDE_IDENTITY_ONLY_ON_MESSAGE": check_only_message,
}


def test_identity(selected: str | None = None) -> None:
    cases = [IDENTITY_CASES[selected]] if selected else IDENTITY_CASES.values()
    for case in cases:
        case()


def main() -> int:
    test_assistant_text_line_carries_usage_through_to_normalized()
    test_user_line_has_no_usage_field()
    test_tool_only_assistant_turn_still_carries_usage()
    test_tool_only_assistant_turn_without_usage_emits_no_carrier()
    test_legacy_string_content_carries_usage()
    test_multiple_text_blocks_usage_attached_once()

    test_identity()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        test_identity(sys.argv[1])
    else:
        sys.exit(main())
