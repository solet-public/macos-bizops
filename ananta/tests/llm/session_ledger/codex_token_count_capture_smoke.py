#!/usr/bin/env python3
"""Codex usage capture: deterministic filesystem/resume and pushed contracts."""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "ananta" / "src"))
from ananta.llm.session_ledger.types import EventType  # noqa: E402
from ananta.llm.session_ledger.vendor import codex as vendor  # noqa: E402

TS = "2026-10-17T00:00:00Z"


def line(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"timestamp": TS, "type": kind, "payload": payload}


def token(total: int, last: int) -> dict[str, Any]:
    return line(
        "event_msg",
        {
            "type": "token_count",
            "info": {
                "total_token_usage": {"input_tokens": total, "output_tokens": total // 2},
                "last_token_usage": {"input_tokens": last, "output_tokens": last // 2},
                "model_context_window": 200000,
            },
        },
    )


def fixture() -> list[dict[str, Any]]:
    messages = [line("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"message {i}"}]}) for i in range(6)]
    return [line("session_meta", {"id": "s"}), line("turn_context", {"model": "model-a"}), messages[0], token(100, 10), messages[1], line("event_msg", {"type": "token_count", "info": None}), messages[2], line("turn_context", {"model": "model-b"}), messages[3], token(200, 20), messages[4], messages[5]]


def read(path: Path, rows: list[dict[str, Any]], start: int = 0) -> list[Any]:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return list(vendor.iter_events_from_path(path, start_line=start))


def counts(events: list[Any]) -> list[Any]:
    return [raw for _, raw in events if raw.payload.get("_codex_item_type") == "token_count"]


def check(ok: bool, name: str, detail: object = "") -> None:
    assert ok, f"{name}: {detail}"
    print(f"PASS {name}")


def check_count(path: Path, rows: list[dict[str, Any]], events: list[Any], tokens: list[Any]) -> None:
    check(len(tokens) == 2, "TOKEN_COUNT_EVENTS_EMITTED_EXACTLY_N", f"got {len(tokens)}, expected 2")


def check_total(path: Path, rows: list[dict[str, Any]], events: list[Any], tokens: list[Any]) -> None:
    check([r.payload["usage"]["total"] for r in tokens] == [rows[i]["payload"]["info"]["total_token_usage"] for i in (3, 9)], "TOTAL_EQUALS_SOURCE")


def check_last(path: Path, rows: list[dict[str, Any]], events: list[Any], tokens: list[Any]) -> None:
    check([r.payload["usage"]["last"] for r in tokens] == [rows[i]["payload"]["info"]["last_token_usage"] for i in (3, 9)], "LAST_EQUALS_SOURCE")


def check_window(path: Path, rows: list[dict[str, Any]], events: list[Any], tokens: list[Any]) -> None:
    check(all(r.payload["usage"]["model_context_window"] == 200000 for r in tokens), "CONTEXT_WINDOW_CARRIED")


def check_model(path: Path, rows: list[dict[str, Any]], events: list[Any], tokens: list[Any]) -> None:
    check([r.payload["usage"]["model"] for r in tokens] == ["model-a", "model-b"], "MODEL_FROM_PRECEDING_TURN_CONTEXT")


def check_missing(path: Path, rows: list[dict[str, Any]], events: list[Any], tokens: list[Any]) -> None:
    missing = counts(read(path, [token(10, 1)]))
    check(all(r.payload["usage"]["model"] is None for r in missing), "MODEL_NONE_WHEN_NO_TURN_CONTEXT")


def check_null(path: Path, rows: list[dict[str, Any]], events: list[Any], tokens: list[Any]) -> None:
    check(not read(path, [line("event_msg", {"type": "token_count", "info": None}), line("event_msg", {"type": "token_count", "info": "bad"})]), "NULL_INFO_SKIPPED_NOT_RAISED")


def check_existing(path: Path, rows: list[dict[str, Any]], events: list[Any], tokens: list[Any]) -> None:
    ordinary = [asdict(raw) for _, raw in events if raw.payload.get("_codex_item_type") == "message"]
    expected = [{"external_session_id": "rollout-test", "payload": {**rows[i]["payload"], "_codex_item_type": "message"}, "event_at": vendor._parse_iso(TS), "vendor_event_id": None, "vendor_parent_event_id": None} for i in (2, 4, 6, 8, 10, 11)]
    check(json.dumps(ordinary, default=str, sort_keys=True) == json.dumps(expected, default=str, sort_keys=True), "EXISTING_EVENTS_BYTE_IDENTICAL")


def check_no_counts(path: Path, rows: list[dict[str, Any]], events: list[Any], tokens: list[Any]) -> None:
    check(not counts(read(path, [row for row in rows if row["type"] != "event_msg"])), "ROLLOUT_WITHOUT_TOKEN_COUNT_EMITS_NONE")


def check_resume(path: Path, rows: list[dict[str, Any]], events: list[Any], tokens: list[Any]) -> None:
    resumed = read(path, rows, 3)
    check(counts(resumed)[0].payload["usage"]["model"] == "model-a" and [offset for offset, _ in resumed] == [3, 4, 6, 8, 9, 10, 11], "MODEL_CARRIED_ACROSS_START_LINE")
    # A damaged unrelated prefix must not be parsed; damaged context is skipped.
    path.write_text('not json\n{"type":"turn_context",bad\n' + json.dumps(rows[1]) + "\n" + json.dumps(rows[3]) + "\n")
    recovered = list(vendor.iter_events_from_path(path, start_line=3))
    assert recovered[0][1].payload["usage"]["model"] == "model-a", "MODEL_CARRIED_ACROSS_START_LINE: malformed prefix"


def check_normalized(path: Path, rows: list[dict[str, Any]], events: list[Any], tokens: list[Any]) -> None:
    try:
        normalized = [vendor.normalize_raw(raw) for raw in tokens]
    except ValueError as exc:
        raise AssertionError(f"TOKEN_COUNT_NORMALIZES_TO_SYSTEM: {exc}") from exc
    check(len(normalized) == 2, "TOKEN_COUNT_NORMALIZES_TO_SYSTEM")
    for event, raw, number in zip(normalized, tokens, (4, 10), strict=True):
        assert event.event_type == EventType.SYSTEM, "TOKEN_COUNT_NORMALIZES_TO_SYSTEM: type"
        assert event.content_text is None and event.content_json == {"subtype": "codex_token_count"}, "TOKEN_COUNT_NORMALIZES_TO_SYSTEM: content"
        assert event.usage_json == raw.payload["usage"], "TOKEN_COUNT_NORMALIZES_TO_SYSTEM: usage"
        assert event.vendor_event_id == f"token_count:{TS}:{number}", "TOKEN_COUNT_NORMALIZES_TO_SYSTEM: id"


def check_diagnostics() -> None:
    output = StringIO()
    handler = logging.StreamHandler(output)
    log = logging.getLogger(vendor.__name__)
    prior = log.level
    log.setLevel(logging.DEBUG)
    log.addHandler(handler)
    try:
        events = [token(10, 1), line("event_msg", {"type": "token_count", "info": None}), line("event_msg", {"type": "token_count", "info": "bad"})]
        list(vendor.parse_chunk(json.dumps({"external_session_id": "s", "events": events})))
        assert output.getvalue().splitlines() == ["codex token_count: null-info skipped 2, model-missing 1"], "PUSHED_CHUNK_MODEL_LIMIT: aggregate diagnostic"
    finally:
        log.removeHandler(handler)
        log.setLevel(prior)


def check_pushed(path: Path, rows: list[dict[str, Any]], events: list[Any], tokens: list[Any]) -> None:
    first = list(vendor.parse_chunk(json.dumps({"external_session_id": "s", "events": rows[1:4]})))
    second = list(vendor.parse_chunk(json.dumps({"external_session_id": "s", "events": [token(300, 30)]})))
    check(first[-1].payload["usage"]["model"] == "model-a" and second[0].payload["usage"]["model"] is None, "PUSHED_CHUNK_MODEL_LIMIT")
    check_diagnostics()


def check_skipped(path: Path, kind: str, name: str) -> None:
    # A meter-shaped payload must still be skipped: only event_msg is a meter.
    rows = [line(kind, token(999, 99)["payload"]), token(10, 1)]
    try:
        events = read(path, rows)
        pushed = list(vendor.parse_chunk(json.dumps({"external_session_id": "s", "events": rows})))
    except ValueError as exc:
        raise AssertionError(f"{name}: {exc}") from exc
    check(len(events) == len(pushed) == 1 and len(counts(events)) == 1, name)
    check(events[0][1].vendor_event_id == f"token_count:{TS}:2" and pushed[0].payload["usage"]["total"]["input_tokens"] == 10, name + ": stable line identity")


def check_world_state(path: Path, rows: list[dict[str, Any]], events: list[Any], tokens: list[Any]) -> None:
    check_skipped(path, "world_state", "WORLD_STATE_TOKEN_COUNT_REACHED")


def check_usage_record(path: Path, rows: list[dict[str, Any]], events: list[Any], tokens: list[Any]) -> None:
    check_skipped(path, "token_usage_record", "TOKEN_USAGE_RECORD_SKIPPED")


CASES = {
    "WORLD_STATE_TOKEN_COUNT_REACHED": check_world_state,
    "TOKEN_USAGE_RECORD_SKIPPED": check_usage_record,
    "TOKEN_COUNT_EVENTS_EMITTED_EXACTLY_N": check_count,
    "TOTAL_EQUALS_SOURCE": check_total,
    "LAST_EQUALS_SOURCE": check_last,
    "CONTEXT_WINDOW_CARRIED": check_window,
    "MODEL_FROM_PRECEDING_TURN_CONTEXT": check_model,
    "MODEL_NONE_WHEN_NO_TURN_CONTEXT": check_missing,
    "NULL_INFO_SKIPPED_NOT_RAISED": check_null,
    "EXISTING_EVENTS_BYTE_IDENTICAL": check_existing,
    "ROLLOUT_WITHOUT_TOKEN_COUNT_EMITS_NONE": check_no_counts,
    "MODEL_CARRIED_ACROSS_START_LINE": check_resume,
    "TOKEN_COUNT_NORMALIZES_TO_SYSTEM": check_normalized,
    "PUSHED_CHUNK_MODEL_LIMIT": check_pushed,
}


def run(selected: str | None = None) -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "rollout-test.jsonl"
        rows = fixture()
        events = read(path, rows)
        tokens = counts(events)
        cases = [CASES[selected]] if selected else CASES.values()
        for case in cases:
            case(path, rows, events, tokens)


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None)
