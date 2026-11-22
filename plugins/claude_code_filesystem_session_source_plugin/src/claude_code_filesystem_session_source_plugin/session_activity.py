"""Measure, for a time window, how each Claude Code session was woken and what it ran.

Reads ``<root>/<project>/<session>.jsonl`` transcripts and writes nothing. Standard library
only, so the command line runs with no platform and no other system present::

    python -m claude_code_filesystem_session_source_plugin.session_activity \\
        --root ~/.claude/projects --since 2026-11-12T11:50:00Z --until 2026-11-13T11:50:00Z \\
        [--session <transcript id>]... [--pattern NAME=REGEX]...

The one document it returns holds ``window``, ``sessions`` (sorted by transcript id), ``totals``
and ``advisories``. A session row counts, inside the window ``since <= timestamp < until``:

* wakes: every user record that carries no tool result and is not a compaction summary, by the
  source that woke the session, plus the pane notices among them and the duplicates of a
  channel wake that reached the session in the previous 300 seconds;
* model calls: assistant records grouped by request (requestId, else message id, else uuid)
  with the last record's usage, synthetic records ignored; context per call is input plus
  cache read plus cache write tokens, reported as median, p90 and maximum;
* tool calls: how many tool_use blocks of those records match each caller pattern, how many
  are tmux capture-pane calls and how many schedule the session's own wake-up.

Unknown or unreadable input is an advisory, never a refusal.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SOURCES = ("person", "peer_report", "job_completion", "result_delivery", "overdue_notice", "rotation_notice", "own_background_task", "scheduled_wake", "other")
PANE_PREFIX = "delivery waiting from "
PANE_SUFFIX = " — drain peer_inbox"
JOB_LABEL = "System (Job Completion)"
JOB_BODY_PREFIX = "[peer_message] Job "
DUPLICATE_SECONDS = 300
SYNTHETIC_MODEL = "<synthetic>"
SELF_SCHEDULING = frozenset({"ScheduleWakeup", "CronCreate"})
_EVENT_SOURCES = {
    "session_overdue_notice": "overdue_notice",
    "session_overdue_quiet_notice": "overdue_notice",
    "rotation_due_notice": "rotation_notice",
    "bridge_delivery_result": "result_delivery",
}
_CHANNEL_TAG = re.compile(r"\s*<channel\b([^>]*)>")
_SOURCE_EVENT = re.compile(r'\bsource_event_type="([^"]*)"')
_PEER_LABEL = re.compile(r'\[peer:\S+ "([^"]*)"')
_TMUX_CAPTURE = re.compile(r"\btmux\s+capture-pane\b")
_NO_USAGE = (0, 0, 0, 0)

type Usage = tuple[int, int, int, int]
"""Input, cache read, cache write and output tokens of one call."""


# ----------------------------------------------------------------------------
# Arguments
# ----------------------------------------------------------------------------


def _parse_moment(name: str, value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO-8601 timestamp string")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} is not an ISO-8601 timestamp: {value!r}") from exc
    if moment.tzinfo is None:
        raise ValueError(f"{name} must carry a time zone: {value!r}")
    return moment.astimezone(UTC)


def parse_window(since: object, until: object) -> tuple[datetime, datetime]:
    """Return the aware window; a malformed bound or ``since >= until`` is a ValueError."""
    start = _parse_moment("since", since)
    end = _parse_moment("until", until)
    if start >= end:
        raise ValueError("since must be earlier than until")
    return start, end


def compile_patterns(raw: object) -> dict[str, re.Pattern[str]]:
    """Compile ``{name: regex}``; a non-object, an empty name or a non-string regex is a ValueError."""
    if not isinstance(raw, Mapping):
        raise ValueError("command_patterns must be an object of name: regular expression")
    compiled: dict[str, re.Pattern[str]] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name:
            raise ValueError("a pattern name must be a non-empty string")
        if not isinstance(value, str):
            raise ValueError(f"pattern {name!r} must be a string")
        try:
            compiled[name] = re.compile(value)
        except re.error as exc:
            raise ValueError(f"pattern {name!r} is not a valid regular expression: {exc}") from exc
    return compiled


# ----------------------------------------------------------------------------
# Record rules
# ----------------------------------------------------------------------------


def _load(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _moment(record: Mapping[str, Any]) -> datetime | None:
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _content(record: Mapping[str, Any]) -> object:
    message = record.get("message")
    return message.get("content") if isinstance(message, dict) else None


def _blocks(content: object) -> list[dict[str, Any]]:
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def _wake_text(content: object) -> str | None:
    """The wake's text, or None when the record carries a tool result and so is not a wake."""
    if isinstance(content, str):
        return content
    blocks = _blocks(content)
    if any(block.get("type") == "tool_result" for block in blocks):
        return None
    return "\n".join(block["text"] for block in blocks if isinstance(block.get("text"), str))


def _split_channel(text: str) -> tuple[str, str]:
    """The channel tag's source_event_type (empty when there is none) and the body after the tag."""
    tag = _CHANNEL_TAG.match(text)
    if tag is None:
        return "", text.lstrip()
    event = _SOURCE_EVENT.search(tag.group(1))
    return (event.group(1) if event else ""), text[tag.end() :].lstrip()


def _channel_source(text: str) -> tuple[str, str | None]:
    """Classify a channel wake; the second value is the sending peer's label when it names one."""
    event, body = _split_channel(text)
    named = _PEER_LABEL.match(body)
    label = named.group(1) if named else None
    if event in _EVENT_SOURCES:
        return _EVENT_SOURCES[event], label
    if body.startswith(JOB_BODY_PREFIX) or (label is not None and label.startswith(JOB_LABEL)):
        return "job_completion", label
    return ("peer_report" if label is not None else "other"), label


def _plain_source(kind: object, record: Mapping[str, Any]) -> str:
    """The source of a wake that is neither a channel wake nor a pane notice."""
    if kind == "human":
        return "person"
    if kind == "task-notification":
        return "own_background_task"
    if kind == "peer":
        return "peer_report"
    return "scheduled_wake" if record.get("turnOrigin") == "scheduled" else "other"


def _call_key(record: Mapping[str, Any], message: Mapping[str, Any], line_number: int) -> str:
    for candidate in (record.get("requestId"), message.get("id"), record.get("uuid")):
        if isinstance(candidate, str) and candidate:
            return candidate
    return f"line:{line_number}"


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _usage(raw: object) -> Usage | None:
    if not isinstance(raw, dict):
        return None
    return (_count(raw.get("input_tokens")), _count(raw.get("cache_read_input_tokens")), _count(raw.get("cache_creation_input_tokens")), _count(raw.get("output_tokens")))


def _tool_text(name: str, tool_input: object) -> str:
    if name == "Bash":
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        return f"Bash {command if isinstance(command, str) else ''}"
    return f"{name} {json.dumps(tool_input, sort_keys=True, separators=(',', ':'))}"


# ----------------------------------------------------------------------------
# Counting
# ----------------------------------------------------------------------------


@dataclass
class _Tally:
    """Counts for one session; ``usage_by_call`` keeps the last record's usage of each call."""

    names: tuple[str, ...]
    records: int = 0
    wake_total: int = 0
    by_source: dict[str, int] = field(default_factory=lambda: dict.fromkeys(SOURCES, 0))
    pane_notices: int = 0
    duplicate_pane_notices: int = 0
    usage_by_call: dict[str, Usage] = field(default_factory=dict)
    command_counts: dict[str, int] = field(init=False)
    tmux_capture_pane_calls: int = 0
    self_scheduled_wakeup_calls: int = 0

    def __post_init__(self) -> None:
        self.command_counts = dict.fromkeys(self.names, 0)

    def add_wake(self, source: str, pane: bool, duplicate: bool) -> None:
        self.wake_total += 1
        self.by_source[source] += 1
        self.pane_notices += pane
        self.duplicate_pane_notices += duplicate


class _Scan:
    """Feeds one transcript file, line by line, into a tally."""

    def __init__(self, tally: _Tally, since: datetime, until: datetime, patterns: Mapping[str, re.Pattern[str]]) -> None:
        self.tally = tally
        self.since = since
        self.until = until
        self.patterns = tuple(patterns.items())
        self.last_channel: dict[str, datetime] = {}
        self.unparseable = 0
        self.line_number = 0

    def feed(self, line: str) -> None:
        self.line_number += 1
        if not line.strip():
            return
        record = _load(line)
        if record is None:
            self.unparseable += 1
            return
        moment = _moment(record)
        inside = moment is not None and self.since <= moment < self.until
        if inside:
            self.tally.records += 1
        kind = record.get("type")
        if kind == "user":
            self._user(record, moment, inside)
        elif kind == "assistant" and inside:
            self._assistant(record)

    def _user(self, record: Mapping[str, Any], moment: datetime | None, inside: bool) -> None:
        text = _wake_text(_content(record))
        if text is None or moment is None or record.get("isCompactSummary") is True:
            return
        source, pane, duplicate = self._wake(record, text, moment)
        if inside:
            self.tally.add_wake(source, pane, duplicate)

    def _wake(self, record: Mapping[str, Any], text: str, moment: datetime) -> tuple[str, bool, bool]:
        """Classify one wake as (source, is a pane notice, is a duplicate of a recent channel wake)."""
        origin = record.get("origin")
        kind = origin.get("kind") if isinstance(origin, dict) else None
        if kind == "channel":
            source, label = _channel_source(text)
            if label is not None:
                self.last_channel[label] = moment
            return source, False, False
        if kind == "human" and text.startswith(PANE_PREFIX):
            sender = text[len(PANE_PREFIX) :].partition(PANE_SUFFIX)[0]
            return ("job_completion" if sender.startswith(JOB_LABEL) else "peer_report"), True, self._reached_recently(sender, moment)
        return _plain_source(kind, record), False, False

    def _reached_recently(self, sender: str, moment: datetime) -> bool:
        reached = self.last_channel.get(sender)
        return reached is not None and 0 <= (moment - reached).total_seconds() <= DUPLICATE_SECONDS

    def _assistant(self, record: Mapping[str, Any]) -> None:
        message = record.get("message")
        if not isinstance(message, dict) or message.get("model") == SYNTHETIC_MODEL:
            return
        key = _call_key(record, message, self.line_number)
        usage = _usage(message.get("usage"))
        if usage is not None or key not in self.tally.usage_by_call:
            self.tally.usage_by_call[key] = usage or _NO_USAGE
        for block in _blocks(message.get("content")):
            if block.get("type") == "tool_use" and isinstance(block.get("name"), str):
                self._tool(block["name"], block.get("input"))

    def _tool(self, name: str, tool_input: object) -> None:
        text = _tool_text(name, tool_input)
        for pattern_name, pattern in self.patterns:
            if pattern.search(text):
                self.tally.command_counts[pattern_name] += 1
        if name == "Bash" and _TMUX_CAPTURE.search(text):
            self.tally.tmux_capture_pane_calls += 1
        if name in SELF_SCHEDULING:
            self.tally.self_scheduled_wakeup_calls += 1


# ----------------------------------------------------------------------------
# The document
# ----------------------------------------------------------------------------


def _context_stats(values: list[int]) -> dict[str, int | None]:
    if not values:
        return {"median": None, "p90": None, "max": None}
    ordered = sorted(values)
    return {"median": round(statistics.median(ordered)), "p90": ordered[(9 * len(ordered) + 9) // 10 - 1], "max": ordered[-1]}


def _total(parts: Sequence[_Tally], attribute: str) -> int:
    return sum(getattr(part, attribute) for part in parts)


def _wake_summary(parts: Sequence[_Tally]) -> dict[str, Any]:
    return {
        "total": _total(parts, "wake_total"),
        "by_source": {source: sum(part.by_source[source] for part in parts) for source in SOURCES},
        "pane_notices": _total(parts, "pane_notices"),
        "duplicate_pane_notices": _total(parts, "duplicate_pane_notices"),
    }


def _token_summary(usages: Sequence[Usage]) -> dict[str, int]:
    tokens = {"input": sum(u[0] for u in usages), "cache_read": sum(u[1] for u in usages), "cache_write": sum(u[2] for u in usages), "output": sum(u[3] for u in usages)}
    tokens["input_total"] = tokens["input"] + tokens["cache_read"] + tokens["cache_write"]
    return tokens


def _summarize(parts: Sequence[_Tally], names: Sequence[str]) -> dict[str, Any]:
    """Sum the fields of every part; context statistics are over all of their calls."""
    usages = [usage for part in parts for usage in part.usage_by_call.values()]
    return {
        "wakes": _wake_summary(parts),
        "model_calls": len(usages),
        "context_tokens": _context_stats([sum(u[:3]) for u in usages]),
        "tokens": _token_summary(usages),
        "command_counts": {name: sum(part.command_counts[name] for part in parts) for name in names},
        "tmux_capture_pane_calls": _total(parts, "tmux_capture_pane_calls"),
        "self_scheduled_wakeup_calls": _total(parts, "self_scheduled_wakeup_calls"),
    }


@dataclass
class _Collection:
    tallies: dict[str, _Tally] = field(default_factory=dict)
    found: set[str] = field(default_factory=set)
    unparseable: int = 0
    unreadable: int = 0


def _scan_file(path: Path, scan: _Scan) -> None:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            scan.feed(line)


def _collect(root: Path, since: datetime, until: datetime, patterns: Mapping[str, re.Pattern[str]], wanted: Collection[str] | None) -> _Collection:
    collection = _Collection()
    since_epoch = since.timestamp()
    names = tuple(patterns)
    for path in sorted(root.glob("*/*.jsonl")):
        collection.found.add(path.stem)
        if wanted is not None and path.stem not in wanted:
            continue
        try:
            if path.stat().st_mtime < since_epoch:
                continue
            tally = collection.tallies.setdefault(path.stem, _Tally(names))
            scan = _Scan(tally, since, until, patterns)
            _scan_file(path, scan)
        except OSError:
            collection.unreadable += 1
            continue
        collection.unparseable += scan.unparseable
    return collection


def _collection_notes(collection: _Collection, wanted: Collection[str] | None) -> list[str]:
    """What the walk could not measure: unknown ids, unreadable files and lines that are not records."""
    notes: list[str] = []
    missing = sorted(set(wanted or ()) - collection.found)
    if missing:
        notes.append(f"sessions_not_found:{','.join(missing)}")
    if collection.unreadable:
        notes.append(f"unreadable_files:{collection.unreadable}")
    if collection.unparseable:
        notes.append(f"unparseable_lines:{collection.unparseable}")
    return notes


def measure(
    root: Path | None,
    since: datetime,
    until: datetime,
    patterns: Mapping[str, re.Pattern[str]],
    session_ids: Collection[str] | None = None,
    advisories: Sequence[str] = (),
) -> dict[str, Any]:
    """Return the activity document for the window; ``root`` None reads nothing."""
    notes = list(advisories)
    collection = _Collection()
    if root is None:
        pass
    elif not root.is_dir():
        notes.append("transcript_root_missing")
    else:
        collection = _collect(root, since, until, patterns, session_ids)
        notes.extend(_collection_notes(collection, session_ids))
    names = tuple(patterns)
    measured = [(session, tally) for session, tally in sorted(collection.tallies.items()) if tally.records]
    return {
        "window": {"since": since.isoformat(), "until": until.isoformat()},
        "sessions": [{"session_id": session, **_summarize([tally], names)} for session, tally in measured],
        "totals": {**_summarize([tally for _, tally in measured], names), "sessions": len(measured)},
        "advisories": notes,
    }


# ----------------------------------------------------------------------------
# Command line
# ----------------------------------------------------------------------------


def _pattern_arguments(values: Sequence[str]) -> dict[str, str]:
    raw: dict[str, str] = {}
    for value in values:
        name, separator, expression = value.partition("=")
        if not separator or not name:
            raise ValueError(f"--pattern must be NAME=REGEX, got {value!r}")
        if name in raw:
            raise ValueError(f"--pattern name given twice: {name!r}")
        raw[name] = expression
    return raw


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="session_activity", description="Measure how each Claude Code session was woken and what it ran in a time window.")
    parser.add_argument("--root", required=True, help="transcript root holding <project>/<session>.jsonl")
    parser.add_argument("--since", required=True, help="inclusive window start, aware ISO-8601")
    parser.add_argument("--until", required=True, help="exclusive window end, aware ISO-8601")
    parser.add_argument("--session", action="append", default=[], help="transcript id to measure; repeatable; default every session")
    parser.add_argument("--pattern", action="append", default=[], help="NAME=REGEX counted over the tool calls; repeatable")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Print the document and return 0; return 2 with the message on stderr for a bad argument."""
    args = _parser().parse_args(argv)
    try:
        since, until = parse_window(args.since, args.until)
        patterns = compile_patterns(_pattern_arguments(args.pattern))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    document = measure(Path(args.root), since, until, patterns, session_ids=args.session or None)
    print(json.dumps(document, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
