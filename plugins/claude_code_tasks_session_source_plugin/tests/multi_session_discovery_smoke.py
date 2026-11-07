"""M10 §1.3 — multi-session discovery + drain order smoke.

Mirrors the operator's actual filesystem layout: multiple session subdirs
each carrying a different number of task files, plus session-management
noise files (`.highwatermark`, `.lock`) that must NOT count as tasks.

Verifies:
- Each subdir yields exactly one ExternalSessionRef
- Empty subdirs (no *.json files) are skipped
- Noise files (`.highwatermark`, `.lock`) are not emitted as events
- Tasks within a session emit in lexically-sorted order
- normalize() produces SYSTEM events with the right subtype + lifted fields
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0,
    str(_REPO_ROOT / "plugins" / "claude_code_tasks_session_source_plugin" / "src"),
)

from ananta.llm.session_ledger.types import EventType, MessageRole  # noqa: E402
from claude_code_tasks_session_source_plugin.plugin import (  # noqa: E402
    ClaudeCodeTasksSessionSourcePlugin,
)


class _StubProvider:
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def get(self, key: str) -> object | None:
        return self._values.get(key)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _write_task(path: Path, task_id: str) -> None:
    path.write_text(
        json.dumps(
            {
                "id": task_id,
                "subject": f"subject {task_id}",
                "description": f"description {task_id}",
                "activeForm": f"doing {task_id}",
                "status": "pending",
                "blocks": [],
                "blockedBy": [],
            }
        )
    )


def _seed_fixture_tree(root: Path) -> None:
    """Lay out three session subdirs mirroring the operator's filesystem shape."""
    # Session 1: three tasks plus noise files that MUST be filtered out.
    s1 = root / "uuid-1"
    s1.mkdir()
    _write_task(s1 / "1.json", "t1")
    _write_task(s1 / "2.json", "t2")
    _write_task(s1 / "3.json", "t3")
    (s1 / ".highwatermark").write_text("3")
    (s1 / ".lock").write_text("")
    # Session 2: one task.
    s2 = root / "uuid-2"
    s2.mkdir()
    _write_task(s2 / "1.json", "u1")
    # Session 3: empty (operator deleted all tasks) — must be skipped.
    s3 = root / "uuid-3"
    s3.mkdir()
    (s3 / ".highwatermark").write_text("0")


def _build_plugin(root: Path) -> tuple[ClaudeCodeTasksSessionSourcePlugin, str]:
    plugin = ClaudeCodeTasksSessionSourcePlugin()
    # P1.1.E: the walk root is threaded per-call as root_uri, not read from config.
    plugin.config_provider = _StubProvider({"glob": "*.json"})
    return plugin, str(root)


def case_discover_skips_empty_subdir(plugin: ClaudeCodeTasksSessionSourcePlugin, root_uri: str) -> list[Any]:
    sessions = list(plugin.discover_sessions(root_uri, None))
    ids = sorted(s.external_session_id for s in sessions)
    _assert(
        ids == ["uuid-1", "uuid-2"],
        f"empty subdir uuid-3 must be skipped; got {ids}",
    )
    return sessions


def _expected_event_count(external_session_id: str) -> int:
    return 3 if external_session_id == "uuid-1" else 1


def _assert_system_event_shape(external_session_id: str, normalized: list[Any]) -> None:
    _assert(
        all(n.event_type is EventType.SYSTEM for n in normalized),
        f"{external_session_id}: every event must be EventType.SYSTEM",
    )
    _assert(
        all(n.role is MessageRole.SYSTEM for n in normalized),
        f"{external_session_id}: every event must have role=SYSTEM",
    )


def _assert_content_json_payload(external_session_id: str, n: Any) -> None:
    _assert(
        n.content_json is not None,
        f"{external_session_id}: SYSTEM event must carry content_json",
    )
    _assert(
        n.content_json["subtype"] == "task_state",
        f"unexpected subtype: {n.content_json['subtype']}",
    )
    required = {"id", "subject", "description", "activeForm", "status", "blocks", "blockedBy"}
    missing = required - n.content_json.keys()
    _assert(
        not missing,
        f"content_json missing required fields: missing={missing} got={list(n.content_json.keys())}",
    )
    _assert(
        n.content_text is None,
        f"M10 SYSTEM events have no content_text (subtype carries everything); got {n.content_text!r}",
    )


def case_normalize_and_drain_order(plugin: ClaudeCodeTasksSessionSourcePlugin, sessions: list[Any], root_uri: str) -> None:
    for ref in sessions:
        events = list(plugin.read_events(root_uri, ref, None))
        normalized = [plugin.normalize(e) for e in events]
        _assert_system_event_shape(ref.external_session_id, normalized)
        for n in normalized:
            _assert_content_json_payload(ref.external_session_id, n)
        expected = _expected_event_count(ref.external_session_id)
        _assert(
            len(events) == expected,
            f"{ref.external_session_id}: got {len(events)} events, want {expected}",
        )


def _poll_case(root: Path, kind: str, plugin_name: str, fixture_name: str) -> Any:
    """Bind a real adapter fixture to a synthetic root and typed runtime.

    All source observations and durable cursors stay in this case."""
    from types import SimpleNamespace

    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "ananta" / "tests" / "llm" / "session_ledger"))
    import importer_smoke as runtime
    from ananta.llm.session_ledger import types
    from ananta.llm.session_ledger.poll_budget import COMPLETED_VERSION_KEY, DiscoveryBudgetYield

    fixture = runtime._fixture_module(plugin_name, fixture_name)
    return SimpleNamespace(root=root, kind=kind, fixture=fixture, runtime=runtime, types=types, completed_key=COMPLETED_VERSION_KEY, yield_type=DiscoveryBudgetYield, source_key=("runtime-source", types.CursorScope.DISCOVERY, None))


def _poll_fresh(case: Any) -> Any:
    """Recreate the actual adapter using its real ConfigProvider.

    Only the configured glob or filename is supplied; discovery is unchanged."""
    configs = {"codex": {}, "claude": {"glob": "*.jsonl"}, "tasks": {"glob": "*.json"}, "ambient": {"filename": "ambient-suggestions.json"}}
    plugin = case.runtime._legacy_plugin(case.kind, case.fixture, case.root)
    plugin.initialize(configs[case.kind])
    return plugin


def _poll_seed(case: Any, distribution: str) -> None:
    """Create 53 valid sessions with tied or deliberately unordered mtimes.

    The future-dated legacy cursor must reopen rather than exclude them."""
    import os

    case.paths = [case.runtime._legacy_path(case.kind, case.fixture, case.root, index) for index in range(53)]
    for index, path in enumerate(case.paths):
        stamp = 1_700_000_000 + (index % 3 if distribution == "nonmonotonic" else 0)
        os.utime(path, ns=(stamp * 1_000_000_000, stamp * 1_000_000_000))
    plugin = _poll_fresh(case)
    assert plugin.supports_whole_session_poll, "real filesystem adapter must activate complete polling hooks"
    case.expected = []
    for ref in plugin.discover_sessions(str(case.root), None):
        case.expected.extend(plugin.normalize(raw) for raw in plugin.read_events(str(case.root), ref, None))
    case.order = [item.external_id for item in sorted(plugin._poll_candidates(str(case.root)), key=lambda item: item.pair, reverse=True)]
    _, case.repo, case.state = case.runtime.runtime_importer(plugin, str(case.root))
    case.repo.cursors[case.source_key] = {"mtime_high_water_iso": "2099-01-01T00:00:00+00:00", "file_mtime_iso": "2099-01-01T00:00:00+00:00", "highest_filename_stem": "zzzz"}
    case.observed, case.visited = [], []


def _poll_cut_visits(case: Any) -> None:
    """Recreate plugin and importer after every one-session deadline cut.

    Normalization advances only admission time; the admitted session finishes."""
    from ananta.llm.session_ledger.types import NormalizedSessionEvent, RawSessionEvent

    for _ in range(55):
        plugin = _poll_fresh(case)
        clock = [0.0]
        normalize = plugin.normalize

        def timed(raw: RawSessionEvent, normalize_current: Any = normalize, clock_current: list[float] = clock) -> NormalizedSessionEvent:
            normalized = normalize_current(raw)
            case.observed.append(normalized)
            if not case.visited or case.visited[-1] != raw.external_session_id:
                case.visited.append(raw.external_session_id)
            clock_current[0] = 61.0
            return normalized

        plugin.normalize = timed
        importer, _, _ = case.runtime.runtime_importer(plugin, str(case.root), repository=case.repo, clock=lambda clock=clock: clock[0])
        report = importer.poll_once()
        assert report.batches_failed == 0, "valid actual filesystem fixture must poll successfully"
        assert report.sessions_seen <= 1, "admission stops only after one complete actual session"
        cursor = case.repo.cursors[case.source_key]
        if report.sessions_seen:
            assert cursor["state"] == "backfilling", "deadline is pending, never EOF/caught_up"
        if cursor["state"] == "caught_up":
            return
    raise AssertionError("pair-resume must reach EOF and reset, not strand oldest anchor")


def _poll_assert_coverage(case: Any) -> None:
    """Compare ordered surviving paths and every successful EOF marker.

    This distinguishes a completed sweep from an accidentally exhausted suffix."""
    assert case.visited == case.order, "equal/nonmonotonic pair restart visits every surviving path in exact newest order"
    assert case.repo.cursors[case.source_key]["after"] is None, "actual EOF clears the after key"
    completed = [key for key, value in case.repo.cursors.items() if key[1] is case.types.CursorScope.EVENT_READ and case.completed_key in value]
    assert len(completed) == 53, "each whole successful version has one completed cursor"


def _poll_assert_events(case: Any) -> None:
    """Compare all normalized fields and existing vendor/derived identities.

    The expected stream is an uninterrupted read of the identical real inputs."""
    from collections import Counter

    from ananta.llm.session_ledger.importer import _event_external_id

    assert Counter(map(repr, case.observed)) == Counter(map(repr, case.expected)), "whole-session restart preserves exact vendor events, usage and model data"
    rows = [row.record for row in case.state.upserts if row.table == "event"]
    assert len(rows) == len(case.expected), "restart preserves exact event counts"
    # Derivation uses the actual persisted session ID and unchanged whole-walk
    # occurrence order, including sources without vendor-supplied IDs.
    ordinals = {}
    for row, normalized in zip(rows, case.observed, strict=True):
        derived = _event_external_id(normalized=normalized, session_id=row["session_id"], ordinals=ordinals)
        assert row["external_id"] == derived, "derived/vendor external IDs remain unchanged"


def _poll_prefix(case: Any) -> None:
    """Expire during an unchanged prefix that yields no session references.

    Its examined metadata must be acknowledged before resuming below the pair."""
    reads = [0]
    progress = case.types.PollDiscoveryProgress()

    def read_cursor(key: str) -> dict[str, object] | None:
        reads[0] += 1
        return case.repo.read_cursor(source_id="runtime-source", scope=case.types.CursorScope.EVENT_READ, scope_key=key)

    fresh = _poll_fresh(case)
    try:
        list(fresh.discover_sessions_for_poll(str(case.root), None, read_event_cursor=read_cursor, can_admit_next=lambda: reads[0] < 5, progress=progress))
    except case.yield_type:
        pass
    else:
        raise AssertionError("completed-prefix deadline must yield, not claim EOF")
    assert reads[0] == 5 and progress.last_examined is not None, "metadata prefix advances before budget yield"
    partial = fresh.completed_discovery_cursor(str(case.root), None, exhausted=False, pending=True, progress=progress)
    assert partial["state"] == "backfilling" and partial["after"] is not None, "budget stop preserves pending pair rather than EOF"
    assert not list(fresh.discover_sessions_for_poll(str(case.root), partial, read_event_cursor=read_cursor, can_admit_next=lambda: True, progress=case.types.PollDiscoveryProgress()))
    assert reads[0] == 53, "restart resumes strictly below the metadata prefix including path ties"


def _poll_append_old(case: Any) -> None:
    """Add a genuine event to an old version without changing parser rules.

    Snapshot sources replay their new population using existing stable IDs."""
    old = case.paths[0]
    if case.kind == "codex":
        line = case.fixture._basic_rollout_lines()[1]
        line["payload"]["content"][0]["text"] = "second-sweep"
        old.write_text(old.read_text() + json.dumps(line) + "\n")
    elif case.kind == "claude":
        line = case.fixture._line_user_text()
        line["uuid"] = "second-sweep"
        line["sessionId"] = old.stem
        old.write_text(old.read_text() + json.dumps(line) + "\n")
    elif case.kind == "tasks":
        case.fixture._write_task(old.with_name("0.json"), "second-sweep")
    else:
        obj = json.loads(old.read_text())
        obj["suggestions"].append({**obj["suggestions"][0], "id": "second-sweep"})
        old.write_text(json.dumps(obj))


def _poll_second_sweep(case: Any) -> None:
    """Observe old appended content and an inserted non-head session.

    A previous caught_up result is not permission to stop enumeration."""
    import os

    _poll_append_old(case)
    os.utime(case.paths[0], ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
    case.added = case.runtime._legacy_path(case.kind, case.fixture, case.root, 99)
    os.utime(case.added, ns=(1_600_000_001_000_000_000, 1_600_000_001_000_000_000))
    case.importer, _, _ = case.runtime.runtime_importer(_poll_fresh(case), str(case.root), repository=case.repo)
    second = case.importer.poll_once()
    assert second.sessions_seen == 2, "second full sweep sees old append and new non-head file"
    assert any("second-sweep" in str(row.record) for row in case.state.upserts if row.table == "event"), "second sweep imports actual old-append event, not only metadata"
    assert case.repo.cursors[case.source_key] == {"version": 1, "state": "caught_up", "after": None}


def _poll_fail_reset(case: Any) -> None:
    """Inject cursor-write failure, retry it, then model confirmed reset.

    The previous durable version survives failure; reset reopens all sessions."""
    previous = dict(case.repo.cursors)
    case.added.write_bytes(case.added.read_bytes() + (b"\n" if case.kind in ("codex", "claude") else b" "))
    case.repo.fail_cursor = True
    case.importer.poll_once()
    assert case.repo.cursors == previous, "failed event/source writes preserve prior durable versions"
    case.repo.fail_cursor = False
    case.importer.poll_once()
    assert case.repo.cursors != previous, "retry corrects the changed completed version"
    case.repo.cursors.clear()
    replay = case.importer.poll_once()
    assert replay.sessions_seen == 54, "confirmed cursor reset reopens the full population"


def _poll_churn(case: Any) -> None:
    """Replace a visited inode, delete unseen content and insert a new head.

    Comparison boundaries survive deletion; the next reset includes changes."""
    import os

    paths = [case.runtime._legacy_path(case.kind, case.fixture, case.root, index) for index in range(4)]
    for path in paths:
        os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    plugin = _poll_fresh(case)
    importer, repo, _ = case.runtime.runtime_importer(plugin, str(case.root), clock=case.runtime.expire_plugin_clock(plugin))
    importer.poll_once()
    cursor = dict(repo.cursors[case.source_key])
    assert cursor["state"] == "backfilling" and cursor["after"] is not None, "cut retains a tie-safe comparison boundary"
    replacement = paths[3].with_name(paths[3].name + ".replacement")
    replacement.write_bytes(paths[3].read_bytes())
    replacement.replace(paths[3])
    paths[0].unlink()
    added = case.runtime._legacy_path(case.kind, case.fixture, case.root, 99)
    os.utime(added, ns=(1_800_000_000_000_000_000, 1_800_000_000_000_000_000))
    for _ in range(2):
        plugin = _poll_fresh(case)
        importer, _, _ = case.runtime.runtime_importer(plugin, str(case.root), repository=repo)
        importer.poll_once()
    for item in plugin._poll_candidates(str(case.root)):
        saved = repo.read_cursor(source_id="runtime-source", scope=case.types.CursorScope.EVENT_READ, scope_key=item.external_id)
        assert saved is not None and saved.get(case.completed_key) == item.version, "next reset sweep includes surviving replacement/new versions above saved key"
    assert repo.cursors[case.source_key]["after"] is None


def _poll_has_marker(case: Any) -> bool:
    """Read successful version publication from the existing event cursors."""
    return any(case.completed_key in value for key, value in case.repo.cursors.items() if key[1] is case.types.CursorScope.EVENT_READ)


def _poll_partial(case: Any) -> None:
    """Finish a trailing partial input after a fresh importer restart.

    Parser return alone cannot certify a physically incomplete version."""
    path = case.runtime._legacy_path(case.kind, case.fixture, case.root, 0)
    complete = path.read_bytes()
    path.write_bytes(complete[:-2])
    importer, case.repo, _ = case.runtime.runtime_importer(_poll_fresh(case), str(case.root))
    importer.poll_once()
    assert not _poll_has_marker(case), "partial physical record must never receive a completed fingerprint"
    assert case.repo.cursors[case.source_key]["state"] == "backfilling", "incomplete version keeps regular sweep pending"
    path.write_bytes(complete)
    importer, _, _ = case.runtime.runtime_importer(_poll_fresh(case), str(case.root), repository=case.repo)
    importer.poll_once()
    assert _poll_has_marker(case), "later-completed record reopens and reaches successful EOF"


def test_resumable_filesystem_poll() -> None:
    """Real metadata/normalizer/restart coverage; no discovery substitution."""
    for distribution in ("equal", "nonmonotonic"):
        with tempfile.TemporaryDirectory(prefix="resumable_tasks_") as scratch:
            root = Path(scratch) / "sessions"
            root.mkdir()
            case = _poll_case(root, "tasks", "claude_code_tasks_session_source_plugin", "multi_session_discovery_smoke.py")
            _poll_seed(case, distribution)
            _poll_cut_visits(case)
            _poll_assert_coverage(case)
            _poll_assert_events(case)
            _poll_prefix(case)
            _poll_second_sweep(case)
            _poll_fail_reset(case)
    for scenario in ("churn", "partial", "empty"):
        with tempfile.TemporaryDirectory(prefix=scenario + "_tasks_") as scratch:
            root = Path(scratch) / "sessions"
            root.mkdir()
            case = _poll_case(root, "tasks", "claude_code_tasks_session_source_plugin", "multi_session_discovery_smoke.py")
            if scenario == "churn":
                _poll_churn(case)
            elif scenario == "partial":
                _poll_partial(case)
            else:
                importer, repo, _ = case.runtime.runtime_importer(_poll_fresh(case), str(root))
                assert importer.poll_once().sessions_seen == 0
                assert repo.cursors[case.source_key] == {"version": 1, "state": "caught_up", "after": None}, "empty/no-ref EOF resets state"
    print("PASS real tasks pair/version/restart/EOF/reset coverage")


def _oversized_setup(case: Any) -> None:
    """Build equal-size inputs and grow one strictly above the test threshold.

    Both remain actual parseable vendor inputs, never a fake source adapter."""
    small = case.runtime._legacy_path(case.kind, case.fixture, case.root, 0)
    large = case.runtime._legacy_path(case.kind, case.fixture, case.root, 1)
    case.threshold = small.stat().st_size
    assert large.stat().st_size == case.threshold
    large.write_bytes(large.read_bytes() + (b"\n" if case.kind in ("codex", "claude") else b" "))
    case.large_key = large.stem if case.kind in ("codex", "claude") else large.parent.name
    case.plugin = _poll_fresh(case)
    case.importer, case.repo, _ = case.runtime.runtime_importer(case.plugin, str(case.root))
    case.reads = []


def _oversized_deferral(case: Any) -> None:
    """Prove equality stays regular and deferred bytes do not prevent caught_up.

    Deferral publishes diagnostics without recording false event completion."""
    progress = case.types.PollDiscoveryProgress()
    refs = list(case.plugin.discover_sessions_for_poll(str(case.root), None, read_event_cursor=lambda key: None, can_admit_next=lambda: True, progress=progress))
    assert len(refs) == 1 and refs[0].aggregate_bytes == case.threshold, "equal threshold remains regular; strictly greater defers"
    assert progress.deferred_count == 1 and progress.deferred_bytes == case.threshold + 1, "deferral names/counts exact aggregate bytes"
    assert case.importer.poll_once().sessions_seen == 1
    assert case.repo.cursors[case.source_key]["state"] == "caught_up", "regular EOF reaches caught_up despite oversized backlog"
    assert ("runtime-source", case.types.CursorScope.EVENT_READ, case.large_key) not in case.repo.cursors, "deferral never stamps completion"


def _oversized_fresh_regular(case: Any) -> None:
    """Insert regular work before the authoritative leased recheck.

    F1 may invalidate only state, preserving version and the existing boundary."""
    from collections.abc import Iterator

    case.runtime._legacy_path(case.kind, case.fixture, case.root, 2)
    original = dict(case.repo.cursors[case.source_key])
    case.repo.cursors[case.source_key] = dict(original)
    read_events = case.plugin.read_events

    def track(root_uri: str, ref: Any, cursor: dict[str, object] | None) -> Iterator[Any]:
        case.reads.append(ref.external_session_id)
        yield from read_events(root_uri, ref, cursor)

    case.plugin.read_events = track
    case.repo.trace.clear()
    case.importer.poll_one_oversized()
    assert not case.reads, "fresh regular pending must forbid every oversized read"
    assert case.repo.cursors[case.source_key] == {**original, "state": "backfilling"}, "leased F1 invalidation changes only state preserving version/after"
    assert case.repo.trace[0] == "acquire" and case.repo.trace.index("acquire") < case.repo.trace.index("write:discovery"), "source lease precedes authoritative selection and writes"
    case.importer.poll_once()
    assert case.reads and case.repo.cursors[case.source_key]["state"] == "caught_up", "next normal producer imports fresh regular work"
    case.reads.clear()


def _oversized_refusal_success(case: Any) -> None:
    """Contrast lease contention with a successful selected whole-session read.

    The worker must leave the regular discovery cursor byte-identical."""
    before = json.dumps(case.repo.cursors[case.source_key], sort_keys=True)
    case.repo.refuse = True
    case.repo.trace.clear()
    case.importer.poll_one_oversized()
    assert case.repo.trace == ["acquire"] and not case.reads, "contended real provider performs no source/event reads or writes"
    case.repo.refuse = False
    case.importer.poll_one_oversized()
    assert case.reads == [case.large_key], "eventual oversized attempt reads exactly one selected real session"
    assert case.repo.cursors["runtime-source", case.types.CursorScope.EVENT_READ, case.large_key][case.completed_key], "eventual oversized completion stores EOF fingerprint"
    assert json.dumps(case.repo.cursors[case.source_key], sort_keys=True) == before, "successful oversized completion leaves regular cursor byte-identical"
    assert not case.repo.held, "real oversized provider releases source lease"


def test_oversized_provider_policy() -> None:
    """Reach real regular caught_up, then exercise A's leased provider recheck."""
    from unittest.mock import patch

    from ananta.llm.session_ledger import filesystem_poll as policy
    from ananta.llm.session_ledger import importer as importer_mod

    with tempfile.TemporaryDirectory(prefix="large_policy_tasks_") as scratch:
        root = Path(scratch) / "sessions"
        root.mkdir()
        case = _poll_case(root, "tasks", "claude_code_tasks_session_source_plugin", "multi_session_discovery_smoke.py")
        _oversized_setup(case)
        with patch.object(policy, "OVERSIZED_SESSION_BYTES", case.threshold), patch.object(importer_mod, "OVERSIZED_SESSION_BYTES", case.threshold):
            _oversized_deferral(case)
            _oversized_fresh_regular(case)
            _oversized_refusal_success(case)
    print("PASS real tasks threshold/regular-pending/leased-oversized coverage")


def main() -> int:
    test_resumable_filesystem_poll()
    test_oversized_provider_policy()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _seed_fixture_tree(root)
        plugin, root_uri = _build_plugin(root)
        sessions = case_discover_skips_empty_subdir(plugin, root_uri)
        case_normalize_and_drain_order(plugin, sessions, root_uri)
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
