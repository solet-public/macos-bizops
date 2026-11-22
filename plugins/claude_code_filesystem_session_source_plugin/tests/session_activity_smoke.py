#!/usr/bin/env python3
"""Smoke for the per-session activity measurement: its rules, its command line and its job verb.

Run: .venv/bin/python3 plugins/claude_code_filesystem_session_source_plugin/tests/session_activity_smoke.py

Every transcript is synthetic and lives in a TemporaryDirectory; the patterns are neutral words.
Checks C1-C9 print PASS or FAIL lines. The run ends with ALL CHECKS PASSED only when every
check passed, and exits 1 otherwise.
"""

from __future__ import annotations

import builtins
import io
import json
import os
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "claude_code_filesystem_session_source_plugin" / "src"))

from claude_code_filesystem_session_source_plugin import session_activity as activity  # noqa: E402
from claude_code_filesystem_session_source_plugin.plugin import ClaudeCodeFilesystemSessionSourcePlugin  # noqa: E402

SINCE = datetime(2026, 11, 12, 11, 50, tzinfo=UTC)
UNTIL = SINCE + timedelta(hours=24)
SINCE_TEXT = "2026-11-12T11:50:00Z"
UNTIL_TEXT = "2026-11-13T11:50:00Z"
PATTERNS = {"alpha": r"^Bash .*\balpha\b", "bravo": r"^mcp__x__process_call .*bravo", "never": "zzz"}
SOURCE_KEYS = ("person", "peer_report", "job_completion", "result_delivery", "overdue_notice", "rotation_notice", "own_background_task", "scheduled_wake", "other")
JOB_LABEL = "System (Job Completion)"
EM_DASH = "—"
_FAILED: list[str] = []


def _check(condition: object, label: str) -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        _FAILED.append(label)
        print(f"  FAIL  {label}")


# ----------------------------------------------------------------------------
# Synthetic transcript records
# ----------------------------------------------------------------------------


def _stamp(offset: float) -> str:
    return (SINCE + timedelta(seconds=offset)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _user(offset: float, content: object, **extra: object) -> dict[str, Any]:
    return {"type": "user", "uuid": f"u-{offset}", "timestamp": _stamp(offset), "message": {"role": "user", "content": content}, **extra}


def _person(offset: float, text: str = "please look") -> dict[str, Any]:
    return _user(offset, text, origin={"kind": "human"})


def _channel(offset: float, event: str, body: str) -> dict[str, Any]:
    text = f'<channel source="fixture" event_type="post_message" source_event_type="{event}" flow_id="f-{offset}" cursor="1">\n{body}\n</channel>'
    return _user(offset, text, origin={"kind": "channel", "server": "fixture"})


def _peer_body(label: str, text: str = "hello") -> str:
    return f'[peer:claude_code "{label}" instance=agi-1] {text}'


def _pane(offset: float, label: str) -> dict[str, Any]:
    return _person(offset, f"delivery waiting from {label} {EM_DASH} drain peer_inbox")


def _usage(inp: int, read: int, write: int, out: int) -> dict[str, int]:
    return {"input_tokens": inp, "cache_read_input_tokens": read, "cache_creation_input_tokens": write, "output_tokens": out}


def _call(offset: float, request_id: str, usage: dict[str, int] | None = None, blocks: Sequence[dict[str, Any]] = (), model: str = "model-a") -> dict[str, Any]:
    message: dict[str, Any] = {"id": f"m-{request_id}-{offset}", "role": "assistant", "model": model, "content": list(blocks)}
    if usage is not None:
        message["usage"] = usage
    return {"type": "assistant", "uuid": f"a-{request_id}-{offset}", "requestId": request_id, "timestamp": _stamp(offset), "message": message}


def _tool(name: str, **tool_input: object) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"t-{name}", "name": name, "input": tool_input}


def _write(root: Path, project: str, session: str, records: Sequence[dict[str, Any]], extra_lines: Sequence[str] = (), mtime: datetime | None = None) -> Path:
    path = root / project / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(record) for record in records] + list(extra_lines)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    stamp = (mtime or UNTIL).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def _measure(root: Path | None, **kwargs: Any) -> dict[str, Any]:
    return activity.measure(root, SINCE, UNTIL, activity.compile_patterns(PATTERNS), **kwargs)


def _row(doc: dict[str, Any], session: str) -> dict[str, Any]:
    rows = [row for row in doc["sessions"] if row["session_id"] == session]
    return rows[0] if rows else {}


@contextmanager
def _recording_opens() -> Generator[list[str]]:
    """Record every transcript path opened by any thread while the context is active."""
    opened: list[str] = []
    real_open = io.open

    def recording(file: Any, *args: Any, **kwargs: Any) -> Any:
        name = os.fspath(file) if isinstance(file, str | os.PathLike) else ""
        if name.endswith(".jsonl"):
            opened.append(name)
        return real_open(file, *args, **kwargs)

    with patch.object(builtins, "open", recording), patch.object(io, "open", recording):
        yield opened


# ----------------------------------------------------------------------------
# C1 - C7: the rules of the measurement
# ----------------------------------------------------------------------------


def _wake_records() -> list[dict[str, Any]]:
    return [
        _person(0),
        _channel(1, "post_message", _peer_body("Alpha Seat")),
        _channel(2, "post_message", _peer_body(JOB_LABEL, "Job j1 finished")),
        _channel(3, "peer_message", "[peer_message] Job j2 finished"),
        _channel(4, "bridge_delivery_result", "delivered"),
        _channel(5, "session_overdue_notice", "overdue"),
        _channel(6, "session_overdue_quiet_notice", "overdue quietly"),
        _channel(7, "rotation_due_notice", "rotate"),
        _user(8, "background task finished", origin={"kind": "task-notification", "producer": "session-task"}),
        _user(9, "scheduled prompt", turnOrigin="scheduled"),
        _channel(10, "gauge_stale_notice", "stale"),
        _user(11, "no origin at all"),
        _user(12, "from a peer", origin={"kind": "peer"}),
        _user(13, [{"type": "tool_result", "tool_use_id": "t-1", "content": "ok"}]),
        _user(14, "summary of the earlier conversation", isCompactSummary=True),
        _channel(20, "post_message", _peer_body("Bravo Seat")),
        _pane(120, "Bravo Seat"),
        _channel(200, "post_message", _peer_body("Charlie Seat")),
        _pane(600, "Charlie Seat"),
        _channel(700, "post_message", _peer_body(JOB_LABEL, "Job j3 finished")),
        _pane(750, JOB_LABEL),
        _pane(1100, JOB_LABEL),
    ]


def check_c1_wake_rules() -> None:
    print("C1 wakes by source")
    with tempfile.TemporaryDirectory() as tmp:
        _write(Path(tmp), "p1", "s-wakes", _wake_records())
        wakes = _row(_measure(Path(tmp)), "s-wakes")["wakes"]
    expected = {"person": 1, "peer_report": 6, "job_completion": 5, "result_delivery": 1, "overdue_notice": 2, "rotation_notice": 1, "own_background_task": 1, "scheduled_wake": 1, "other": 2}
    _check(set(wakes["by_source"]) == set(SOURCE_KEYS), "by_source carries all nine keys")
    _check(wakes["by_source"] == expected, f"each wake rule lands in its source: {wakes['by_source']}")
    _check(wakes["total"] == 20, f"a tool_result record and a compact summary are not wakes (total {wakes['total']})")
    _check(wakes["pane_notices"] == 4, f"four pane notices ({wakes['pane_notices']})")
    _check(wakes["duplicate_pane_notices"] == 2, f"a pane notice 100 s after its channel wake is a duplicate, 400 s after is not ({wakes['duplicate_pane_notices']})")
    _check(wakes["total"] == sum(wakes["by_source"].values()), "total counts every wake record, duplicates included")


def check_c2_calls_and_context() -> None:
    print("C2 model calls, usage and context statistics")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(
            root,
            "p1",
            "s-calls",
            [
                _call(0, "r1", _usage(1, 2, 3, 4)),
                _call(1, "r1", _usage(1, 2, 3, 4)),
                _call(2, "r1", _usage(10, 20, 30, 40)),
                _call(3, "r2", _usage(100, 200, 300, 400)),
                _call(4, "r3", _usage(9999, 9999, 9999, 9999), model="<synthetic>"),
            ],
        )
        _write(root, "p1", "s-stats", [_call(i, f"q{i}", _usage(10 * i, 0, 0, 1)) for i in range(1, 11)])
        doc = _measure(root)
    calls = _row(doc, "s-calls")
    _check(calls["model_calls"] == 2, f"three records of one requestId and one of another are 2 calls ({calls['model_calls']})")
    _check(calls["tokens"] == {"input": 110, "cache_read": 220, "cache_write": 330, "output": 440, "input_total": 660}, f"usage comes from the last record of each call: {calls['tokens']}")
    _check(calls["context_tokens"] == {"median": 330, "p90": 600, "max": 600}, f"context of two calls: {calls['context_tokens']}")
    stats = _row(doc, "s-stats")
    _check(stats["context_tokens"] == {"median": 55, "p90": 90, "max": 100}, f"median, p90 and max over ten calls are exact: {stats['context_tokens']}")
    _check(doc["totals"]["model_calls"] == 12 and doc["totals"]["sessions"] == 2, f"totals count calls and sessions ({doc['totals']['model_calls']}, {doc['totals']['sessions']})")
    _check(doc["totals"]["context_tokens"] == {"median": 60, "p90": 100, "max": 600}, f"totals context is over all calls: {doc['totals']['context_tokens']}")
    _check(doc["totals"]["tokens"]["input_total"] == 660 + 550 and doc["totals"]["tokens"]["output"] == 440 + 10, f"totals sum the tokens: {doc['totals']['tokens']}")


def check_c3_window_edges() -> None:
    print("C3 window edges")
    seconds = (UNTIL - SINCE).total_seconds()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root, "p1", "s-edge", [_person(-1), _person(0), _person(seconds - 1), _person(seconds)])
        _write(root, "p1", "s-outside", [_person(-1), _person(seconds)])
        doc = _measure(root)
    _check([row["session_id"] for row in doc["sessions"]] == ["s-edge"], "a session with no in-window record is omitted")
    _check(_row(doc, "s-edge")["wakes"]["total"] == 2, "since is inside, since minus 1 s and until are outside")
    _check(doc["window"] == {"since": SINCE.isoformat(), "until": UNTIL.isoformat()}, f"the window is echoed: {doc['window']}")


def check_c4_tool_calls() -> None:
    print("C4 tool calls and caller patterns")
    records = [
        _call(0, "q1", _usage(1, 1, 1, 1), [_tool("Bash", command="echo alpha"), _tool("mcp__x__process_call", process_key="bravo")]),
        _call(1, "q1", _usage(1, 1, 1, 1), [_tool("Bash", command="tmux capture-pane -p -t pane1")]),
        _call(2, "q2", _usage(1, 1, 1, 1), [_tool("ScheduleWakeup", delaySeconds=60), _tool("CronCreate", cron="* * * * *")]),
        _call(3, "q3", _usage(1, 1, 1, 1), [_tool("Bash", command="echo bravo"), _tool("Bash", command="echo alpha"), _tool("Bash", command="echo xtmux capture-pane")]),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        _write(Path(tmp), "p1", "s-tools", records)
        compact = {**PATTERNS, "compact": r'^mcp__x__process_call \{"process_key":"bravo"\}$'}
        doc = activity.measure(Path(tmp), SINCE, UNTIL, activity.compile_patterns(compact))
    row = _row(doc, "s-tools")
    _check(row["model_calls"] == 3, f"three calls ({row['model_calls']})")
    _check(row["command_counts"] == {"alpha": 2, "bravo": 1, "never": 0, "compact": 1}, f"every pattern is counted, blocks of every record of a call included: {row['command_counts']}")
    _check(row["tmux_capture_pane_calls"] == 1, f"tmux capture-pane on a word boundary ({row['tmux_capture_pane_calls']})")
    _check(row["self_scheduled_wakeup_calls"] == 2, f"ScheduleWakeup and CronCreate ({row['self_scheduled_wakeup_calls']})")
    _check(doc["totals"]["command_counts"] == row["command_counts"], "totals sum the pattern counts")


def check_c5_file_selection() -> None:
    print("C5 file selection")
    before = SINCE - timedelta(hours=1)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root, "p1", "s-old", [_person(5)], mtime=before)
        _write(root, "p1", "s-new", [_person(5)])
        _write(root, "p2", "s-other", [_person(5), _person(6)])
        with _recording_opens() as opened:
            everything = _measure(root)
        names = sorted(Path(name).stem for name in opened)
        _check(names == ["s-new", "s-other"], f"a file whose mtime is before since is not opened: {names}")
        _check([row["session_id"] for row in everything["sessions"]] == ["s-new", "s-other"], "sessions are sorted by id")
        with _recording_opens() as opened:
            one = _measure(root, session_ids=["s-other"])
        _check([Path(name).stem for name in opened] == ["s-other"] and [row["session_id"] for row in one["sessions"]] == ["s-other"], "session_ids filters")
        _check(one["advisories"] == [], f"a found id gives no advisory: {one['advisories']}")
        missing = _measure(root, session_ids=["s-new", "s-gone-b", "s-gone-a"])
        _check(missing["advisories"] == ["sessions_not_found:s-gone-a,s-gone-b"], f"unknown ids are advisory: {missing['advisories']}")
        old = _measure(root, session_ids=["s-old"])
        _check(old["sessions"] == [] and old["advisories"] == [], f"an id whose file predates the window exists and has nothing in it: {old['advisories']}")


def _zero_totals(totals: dict[str, Any]) -> bool:
    wakes = totals["wakes"]
    quiet = wakes["total"] == 0 and wakes["pane_notices"] == 0 and wakes["duplicate_pane_notices"] == 0 and set(wakes["by_source"].values()) == {0}
    tokens = set(totals["tokens"].values()) == {0}
    return quiet and tokens and totals["model_calls"] == 0 and totals["sessions"] == 0 and totals["tmux_capture_pane_calls"] == 0 and totals["self_scheduled_wakeup_calls"] == 0


def check_c6_missing_root() -> None:
    print("C6 missing transcript root")
    with tempfile.TemporaryDirectory() as tmp:
        doc = _measure(Path(tmp) / "absent")
        nothing = _measure(None, advisories=["source_not_registered"])
        out, err = io.StringIO(), io.StringIO()
        argv = ["--root", str(Path(tmp) / "absent"), "--since", SINCE_TEXT, "--until", UNTIL_TEXT, "--pattern", "probe=frobnicate"]
        with redirect_stdout(out), redirect_stderr(err):
            code = activity.main(argv)
        printed = json.loads(out.getvalue())
        bad_out, bad_err = io.StringIO(), io.StringIO()
        with redirect_stdout(bad_out), redirect_stderr(bad_err):
            bad_code = activity.main(["--root", tmp, "--since", UNTIL_TEXT, "--until", SINCE_TEXT])
    _check(doc["sessions"] == [] and _zero_totals(doc["totals"]), "no sessions and zero totals")
    _check(doc["totals"]["context_tokens"] == {"median": None, "p90": None, "max": None}, f"context stats are null with no call: {doc['totals']['context_tokens']}")
    _check(doc["advisories"] == ["transcript_root_missing"], f"advisory transcript_root_missing: {doc['advisories']}")
    _check(nothing["sessions"] == [] and nothing["advisories"] == ["source_not_registered"], f"root None reads nothing and adds no root advisory: {nothing['advisories']}")
    _check(code == 0 and err.getvalue() == "" and printed == activity.measure(Path(tmp) / "absent", SINCE, UNTIL, activity.compile_patterns({"probe": "frobnicate"})), "the command line prints the one document and exits 0")
    _check(out.getvalue().strip() == json.dumps(printed, sort_keys=True), "the document is printed with sorted keys")
    _check(bad_code == 2 and bad_out.getvalue() == "" and bad_err.getvalue().strip() != "", f"a bad window exits 2 with the message on stderr ({bad_code})")


def check_c7_unparseable_lines() -> None:
    print("C7 lines that are not records")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root, "p1", "s-mixed", [_person(1), _person(2)], extra_lines=["this is not json {"])
        single = _measure(root)
        (root / "p1" / "s-mixed.jsonl").write_text(json.dumps(_person(1)) + "\n[1, 2]\n" + "not json\n", encoding="utf-8")
        os.utime(root / "p1" / "s-mixed.jsonl", (UNTIL.timestamp(), UNTIL.timestamp()))
        (root / "p1" / "s-dir.jsonl").mkdir()
        os.utime(root / "p1" / "s-dir.jsonl", (UNTIL.timestamp(), UNTIL.timestamp()))
        many = _measure(root)
    _check(single["advisories"] == ["unparseable_lines:1"], f"one non-JSON line: {single['advisories']}")
    _check(_row(single, "s-mixed")["wakes"]["total"] == 2, "the rest of the file is measured")
    _check("unparseable_lines:2" in many["advisories"], f"a JSON line that is not an object counts too: {many['advisories']}")
    _check("unreadable_files:1" in many["advisories"], f"a path that cannot be read is advisory and the rest is measured: {many['advisories']}")
    _check(_row(many, "s-mixed")["wakes"]["total"] == 1, "the readable file is still measured")


# ----------------------------------------------------------------------------
# C8 - C9: the born-async verb
# ----------------------------------------------------------------------------


class _Jobs:
    """The platform job ledger port in memory; a held ledger keeps each job at 'processing'."""

    def __init__(self, hold: bool = False) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.requests: dict[str, dict[str, Any]] = {}
        self.lock = threading.RLock()
        self.release = threading.Event()
        if not hold:
            self.release.set()

    def create_job(self, **kwargs: Any) -> dict[str, Any]:
        with self.lock:
            job_id = f"fixture-job-{len(self.rows) + 1}"
            self.rows[job_id] = {"id": job_id, "provider_name": f"{kwargs['plugin_name']}.{kwargs['action_name']}", "status": "queued", "metadata": kwargs.get("job_metadata", {})}
            self.requests[job_id] = json.loads(json.dumps(kwargs["request_data"]))
        return {"action_status": "completed", "data": {"job_id": job_id, "status": "queued"}}

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            return {"action_status": "completed", "data": {"job": dict(self.rows[job_id])}}

    def get_job_payload(self, job_id: str, payload_type: str) -> dict[str, Any]:
        with self.lock:
            return {"action_status": "completed", "data": {"payload": self.requests[job_id] if payload_type == "request" else self.rows[job_id].get(payload_type)}}

    def update_job(self, job_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        if updates.get("status") == "processing" and not self.release.wait(10):
            raise AssertionError("fixture job was never released")
        with self.lock:
            self.rows[job_id].update(json.loads(json.dumps(updates)))
        return {"action_status": "completed", "data": {"outcome_committed": True}}

    def terminal(self, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            row = self.get_job(job_id)["data"]["job"]
            if row["status"] in {"completed", "error"}:
                return row
            time.sleep(0.005)
        raise AssertionError(f"job did not finish: {self.rows}")


def _plugin(jobs: _Jobs, sources: list[dict[str, Any]] | None) -> ClaudeCodeFilesystemSessionSourcePlugin:
    """A verb host whose ledger service lists ``sources``; None means no ledger service at all."""
    plugin = ClaudeCodeFilesystemSessionSourcePlugin()
    service = None if sources is None else SimpleNamespace(list_sources=lambda: {"sources": sources})
    plugin.orchestrator_ref = SimpleNamespace(async_job_manager=jobs, get_service=lambda name: service if name == "session_ledger_service" else None)
    return plugin


def _source(root: Path, kind: str = "claude_code_local", enabled: bool = True) -> dict[str, Any]:
    return {"source_id": f"src-{kind}", "source_kind": kind, "enabled": enabled, "root_uri": root.as_uri()}


def _params(**extra: Any) -> dict[str, Any]:
    return {"since": SINCE_TEXT, "until": UNTIL_TEXT, "command_patterns": PATTERNS, **extra}


STATE = {"session_id": "fixture-session", "flow_id": "fixture-flow"}


def _run_job(plugin: ClaudeCodeFilesystemSessionSourcePlugin, jobs: _Jobs, params: dict[str, Any]) -> dict[str, Any]:
    queued = plugin.measure_session_activity(params, dict(STATE))
    jobs.release.set()
    return jobs.terminal(queued["data"]["job_id"])


def check_c8_job_verb() -> None:
    print("C8 the born-async verb")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root, "p1", "s-wakes", _wake_records())
        expected = json.loads(json.dumps(_measure(root)))
        held = _Jobs(hold=True)
        plugin = _plugin(held, [_source(root)])
        with _recording_opens() as opened:
            queued = plugin.measure_session_activity(_params(), dict(STATE))
            seen_before = list(opened)
            held.release.set()
            row = held.terminal(queued["data"]["job_id"])
        _check(queued["action_status"] == "completed" and set(queued["data"]) == {"job_id", "status"} and queued["data"]["status"] == "queued", f"the verb returns a queued handle: {queued['data']}")
        _check(seen_before == [], f"no transcript is opened until the job drains: {seen_before}")
        _check(row["status"] == "completed" and row["result"] == expected, "the drained job result equals measure() on the enabled source root")
        _check(held.rows[queued["data"]["job_id"]]["provider_name"] == f"{plugin.name}.measure_session_activity", "the job is filed under this plugin and verb")
        jobs = _Jobs()
        rows = [
            ("a disabled source", _run_job(_plugin(jobs, [_source(root, enabled=False)]), jobs, _params()), "source_not_registered"),
            ("another source kind", _run_job(_plugin(jobs, [_source(root, kind="codex_local")]), jobs, _params()), "source_not_registered"),
            ("no source", _run_job(_plugin(jobs, []), jobs, _params()), "source_not_registered"),
            ("no ledger service", _run_job(_plugin(jobs, None), jobs, _params()), "ledger_service_unavailable"),
        ]
        for label, outcome, advisory in rows:
            _check(outcome["status"] == "completed" and outcome["result"]["sessions"] == [] and outcome["result"]["advisories"] == [advisory], f"{label}: sessions [] and advisory {advisory}")
        filtered = _run_job(_plugin(jobs, [_source(root)]), jobs, _params(session_ids=["s-wakes", "s-gone"]))
        _check(filtered["result"]["advisories"] == ["sessions_not_found:s-gone"] and len(filtered["result"]["sessions"]) == 1, f"session_ids reach the job: {filtered['result']['advisories']}")


def check_c9_invalid_parameters() -> None:
    print("C9 invalid parameters")
    cases = {
        "since equals until": _params(until=SINCE_TEXT),
        "since after until": _params(since=UNTIL_TEXT, until=SINCE_TEXT),
        "no time zone": _params(since="2026-11-12T11:50:00"),
        "missing until": {"since": SINCE_TEXT},
        "bad regex": _params(command_patterns={"alpha": "("}),
        "pattern value not a string": _params(command_patterns={"alpha": 5}),
        "patterns not an object": _params(command_patterns=["alpha"]),
        "session_ids not a list of strings": _params(session_ids=[1]),
    }
    with tempfile.TemporaryDirectory() as tmp:
        jobs = _Jobs()
        plugin = _plugin(jobs, [_source(Path(tmp))])
        for label, params in cases.items():
            result = plugin.measure_session_activity(params, dict(STATE))
            _check(result["action_status"] == "failed" and result["error"]["code"] == "parameter_invalid" and result["error"]["message"], f"{label}: failed with parameter_invalid")
        _check(jobs.rows == {}, f"no job was created: {sorted(jobs.rows)}")


CHECKS: tuple[Callable[[], None], ...] = (
    check_c1_wake_rules,
    check_c2_calls_and_context,
    check_c3_window_edges,
    check_c4_tool_calls,
    check_c5_file_selection,
    check_c6_missing_root,
    check_c7_unparseable_lines,
    check_c8_job_verb,
    check_c9_invalid_parameters,
)


def main() -> int:
    for check in CHECKS:
        try:
            check()
        except Exception:
            traceback.print_exc()
            _FAILED.append(f"{check.__name__} raised")
    if _FAILED:
        print(f"{len(_FAILED)} FAILED: {_FAILED}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
