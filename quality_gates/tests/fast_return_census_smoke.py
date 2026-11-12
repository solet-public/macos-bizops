"""Offline census integrity smoke; slow plugin verbs never fail this check.

Run: PYTHONPATH=src .venv/bin/python
quality_gates/tests/fast_return_census_smoke.py

Reads real manifests and runtime action discovery independently of the census.
Only a local synthetic handler exercises the timing engine. Checkpoint tests
use synthetic evidence and prove completed rows are never invoked again. Pure
classifier regressions retain the named frozen observations from review B1/B2.
No measured plugin latency is a pass condition. The provisioned body exercises
real handlers and services in a disposable private database; it requires the
registered manifest and skips with 77 without it. Vendors and host vaults stay
behind the census guard, and background workers remain parked by default.
"""

from __future__ import annotations

import ast
import contextlib
import copy
import hashlib
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import tomllib
from collections.abc import Callable, Generator
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fast_return_census as census  # noqa: E402
import fast_return_census_fixtures as fixtures  # noqa: E402

_passed = 0
_failed: list[str] = []
_home_calls: list[Path] = []
_home_handlers: list[logging.FileHandler] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"PASS {label}")
    else:
        _failed.append(label)
        print(f"FAIL {label}")


def _manifest_names() -> set[str]:
    names: set[str] = set()
    for manifest in ROOT.glob("plugins/*/pyproject.toml"):
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        names.update(data.get("project", {}).get("entry-points", {}).get("ananta.plugins", {}))
    return names


def _runtime_readback(inventory: list[census.PluginVerbs]) -> dict[str, list[str]]:
    from ananta.core.plugins.action_discovery import discover_actions
    from ananta.core.process_registry.registration_census import should_skip_plugin_action

    actions: dict[str, list[str]] = {}
    for plugin in inventory:
        if plugin.plugin_class is None:
            continue
        # Runtime discovery only needs the completed class and its provider name;
        # constructors/readiness are intentionally outside this readback.
        instance: Any = object.__new__(plugin.plugin_class)
        instance.name = plugin.entry.plugin
        actions[plugin.entry.plugin] = sorted(action.name for action in discover_actions(instance) if not should_skip_plugin_action(action))
    return actions


class _FixturePlugin:
    """A local-only dispatch fixture; no real plugin handler is remeasured."""

    def __init__(self) -> None:
        self._async_job_manager: census.JobLedger | None = None

    def queued(self, _params: dict[str, Any], _state: dict[str, Any]) -> dict[str, Any]:
        assert self._async_job_manager is not None
        return self._async_job_manager.create_job(action_name="queued")


@contextlib.contextmanager
def _synthetic_context(root: Path, scratch: Path, plugin: str, cls: type[Any], probe: Any, modules: list[Any]) -> Generator[Any]:
    """Explicit synthetic-only injection; the CLI always uses real services."""
    del root, cls, probe, modules
    with fixtures.temporary_app_home(scratch) as home:
        jobs = census.JobLedger()

        def wire(instance: Any) -> None:
            instance.orchestrator_ref = SimpleNamespace(APP_HOME=str(home), async_job_manager=jobs, get_service=lambda _name: None)

        context = SimpleNamespace(app_home=home, scratch=home, modules=[], config=None, jobs=jobs, state=SimpleNamespace(calls=[]), seeded={}, wire=wire, guard_options=lambda: {}, initialize=lambda _instance: None, receipt={"plugin": plugin})
        yield context
    context.receipt["fixture_removed"] = not home.exists()


def _fixture_inventory() -> list[census.PluginVerbs]:
    action = SimpleNamespace(name="queued", function="queued", parameters={}, examples=[])
    return [census.PluginVerbs(census.ManifestEntry(name, "census_fixture:Fixture", ROOT / "plugins" / name), _FixturePlugin, [action]) for name in ("census_fixture_one", "census_fixture_two")]


def _check_manifest(scratch: Path) -> None:
    report = census.run_census(ROOT, scratch, census.Settings(), measure=False)
    expected = _manifest_names()
    rows = report["plugins"]
    _check(bool(expected) and bool(rows), "manifest and census populations are nonempty")
    _check({row["plugin"] for row in rows} == expected, "enumeration covers exactly the manifest entry points")
    _check(not any(row["import_error"] for row in rows), "all manifest classes resolve in the measured tree")
    _check(report["manifest_verb_count"] > 0, "registered verb population is nonempty")
    with census.isolated_boundaries(ROOT, scratch, census.Probe(census.Settings())):
        runtime = _runtime_readback(census.enumerate_verbs(ROOT))
    for row in rows:
        actual = sorted(verb["verb"] for verb in row["verbs"])
        _check(row["verb_count"] == len(actual) and actual == runtime.get(row["plugin"]), f"{row['plugin']} verb count and names match runtime registry")


def _check_engine(scratch: Path) -> None:
    plugin = _fixture_inventory()[0]
    measurement = census.measure_verb(ROOT, plugin, plugin.actions[0], scratch, census.Settings())
    _check(measurement["verb"] == "queued" and measurement["wall_ms"] is not None and measurement["class"] in census.CLASSES, "measurement engine emits a classified row for a synthetic local handler")


class _AppHomePlugin:
    """Exercise the census's bound-handler caller, including real fixture writes."""

    def __init__(self) -> None:
        self._app_home: Path | None = None
        self.orchestrator_ref: Any = None

    def prepare_for_readiness(self) -> None:
        self._app_home = Path(self.orchestrator_ref.APP_HOME)

    def local(self, _params: dict[str, Any], _state: dict[str, Any]) -> dict[str, Any]:
        if self._app_home is None:
            raise census.MissingFixtureError("app_home requires the plugin's readiness hook")
        target = self._app_home / "data" / "fixture.json"
        target.write_text('{"fixture":true}', encoding="utf-8")
        assert target.read_text(encoding="utf-8") == '{"fixture":true}'
        handler = logging.FileHandler(self._app_home / "data" / "fixture.log")
        logging.getLogger("census-app-home-fixture").addHandler(handler)
        _home_handlers.append(handler)
        _home_calls.append(self._app_home)
        return {"action_status": "completed"}

    def credential(self, _params: dict[str, Any], _state: dict[str, Any]) -> dict[str, Any]:
        if os.environ.get("CENSUS_REAL_CREDENTIAL"):
            raise AssertionError("the census exposed a host credential")
        raise census.MissingFixtureError("credential: no isolated credential is supplied")


def _home_inventory(verb: str = "local") -> list[census.PluginVerbs]:
    action = SimpleNamespace(name=verb, function=verb, parameters={}, examples=[])
    # Use a real app-home readiness policy, with a synthetic local-only handler.
    entry = census.ManifestEntry("audio_processing_plugin", "census_fixture:AppHome", ROOT)
    return [census.PluginVerbs(entry, _AppHomePlugin, [action])]


def _check_app_home(scratch: Path) -> None:
    _home_calls.clear()
    _home_handlers.clear()
    inventory = _home_inventory()
    with patch.object(census, "enumerate_verbs", return_value=inventory):
        report = census.run_census(ROOT, scratch, census.Settings(), measure=True, context_factory=_synthetic_context)
    row = report["plugins"][0]["verbs"][0]
    _check(row["class"] == "LOCAL_FAST" and row["wall_ms"] is not None and len(_home_calls) == 1, "app_home is measured by the real bound-handler call after readiness")
    _check(bool(_home_calls) and all(path.is_relative_to(scratch) and not path.exists() for path in _home_calls), "app_home writes stay in the fixture and its directory is removed after success")
    _check_home_write_audit(row)
    _check_home_resources(row, report)


def _check_home_write_audit(row: dict[str, Any]) -> None:
    writes = [item for item in row["setup_file_touches"] + row["file_touches"] if item["write"] and item["allowed"]]
    _check(bool(writes) and all(_contained_write(item, _home_calls[0]) for item in writes), "process write audit resolves every allowed handler write beneath its temporary home")


def _contained_write(item: dict[str, Any], home: Path) -> bool:
    return bool(item["paths"]) and all(Path(path).is_relative_to(home) for path in item["paths"])


def _check_home_resources(row: dict[str, Any], report: dict[str, Any]) -> None:
    _check(row["fixture_removed"] and report["fixture_removed"] and all(handler.stream is None for handler in _home_handlers), "run cleanup removes both homes and closes only their owned file handlers")


def _check_app_home_failure(scratch: Path) -> None:
    _home_calls.clear()
    _home_handlers.clear()
    with patch.object(census, "enumerate_verbs", return_value=_home_inventory()), patch.object(census, "_record_probe", side_effect=RuntimeError("midway census failure")):
        try:
            census.run_census(ROOT, scratch, census.Settings(), measure=True, context_factory=_synthetic_context)
        except RuntimeError as exc:
            _check(str(exc) == "midway census failure" and len(_home_calls) == 1, "failure injection reaches the real app_home handler before failing midway")
        else:
            _check(False, "failure injection reaches the real app_home handler before failing midway")
    _check(bool(_home_calls) and all(not path.exists() for path in _home_calls), "app_home is removed when the census fails midway")
    _check(bool(_home_handlers) and all(handler.stream is None for handler in _home_handlers), "midway failure closes the fixture file handlers before removing its home")


def _check_credential_cause(scratch: Path) -> None:
    inventory = _home_inventory("credential")
    with patch.dict(os.environ, {"CENSUS_REAL_CREDENTIAL": "host-value-must-not-reach-the-handler"}), patch.object(census, "enumerate_verbs", return_value=inventory):
        report = census.run_census(ROOT, scratch, census.Settings(), measure=True, context_factory=_synthetic_context)
    row = report["plugins"][0]["verbs"][0]
    _check(row["class"] == "NOT_MEASURABLE" and row["missing_piece"] == "MissingFixtureError: credential: no isolated credential is supplied", "credential-dependent handler retains its single credential cause without a host credential")


def _check_unsafe_prerequisites(scratch: Path) -> None:
    for (name, verb), cause in census._UNMEASURED_ACTIONS.items():
        action = SimpleNamespace(name=verb, function=verb, parameters={}, examples=[])
        plugin = census.PluginVerbs(census.ManifestEntry(name, "census_fixture:Unsafe", ROOT), _AppHomePlugin, [action])
        with patch.object(_AppHomePlugin, "__init__", side_effect=AssertionError("unsafe constructor was reached")) as constructor:
            row = census.measure_verb(ROOT, plugin, action, scratch, census.Settings())
        _check(constructor.call_count == 0 and row["wall_ms"] is None and row["missing_piece"] == f"MissingFixtureError: {cause}", f"{name}.{verb} names its unsafe prerequisite without construction or invocation")
    try:
        census.run_census(ROOT, ROOT / "profile" / "forbidden-census", census.Settings())
    except ValueError as exc:
        _check("protected" in str(exc), "a real profile cannot be selected as census scratch")
    else:
        _check(False, "a real profile cannot be selected as census scratch")


def _report_rows(report: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for plugin in report["plugins"]:
        for row in plugin["verbs"]:
            key = (plugin["plugin"], row["verb"])
            if key in rows:
                raise ValueError(f"duplicate frozen observation: {key}")
            rows[key] = row
    return rows


def _replay_classes(report: dict[str, Any]) -> dict[str, Any]:
    """Replay retained observations; a truncated failure stays explicitly unknown."""
    differences: list[dict[str, Any]] = []
    unavailable: list[tuple[str, str]] = []
    rows = _report_rows(report)
    for key, old in rows.items():
        row = copy.deepcopy(old)
        result: Any = None
        error = ""
        outcome = old.get("outcome", "")
        try:
            result = ast.literal_eval(outcome)
        except (SyntaxError, ValueError):
            error = outcome
            if outcome.startswith(("{", "[")):
                unavailable.append(key)
        census._classify(row, result, error, census.Settings(**report["settings"]), enqueued=bool(old.get("enqueued_actions")))
        if row["class"] != old["class"]:
            differences.append({"plugin": key[0], "verb": key[1], "before": old["class"], "after": row["class"]})
    return {"count": len(rows), "differences": differences, "unavailable_full_result": unavailable}


def _fresh_differences(frozen: dict[str, Any], fresh: dict[str, Any], repaired: set[tuple[str, str]], cohort: set[tuple[str, str]]) -> list[dict[str, Any]]:
    """Account for every class change; unrelated differences remain findings."""
    before, after = _report_rows(frozen), _report_rows(fresh)
    differences: list[dict[str, Any]] = []
    for key in sorted(before.keys() | after.keys()):
        old, new = before.get(key, {}), after.get(key, {})
        if old.get("class") == new.get("class"):
            continue
        kind = "expected_repair" if key in repaired else "app_home_cohort" if key in cohort else "finding"
        reason = "registered landed repair" if kind == "expected_repair" else new.get("missing_piece") or new.get("outcome", "")
        differences.append({"plugin": key[0], "verb": key[1], "kind": kind, "before": old.get("class"), "after": new.get("class"), "before_wall_ms": old.get("wall_ms"), "after_wall_ms": new.get("wall_ms"), "reason": reason})
    return differences


def _check_replay_and_differences() -> None:
    queued = {"data": {"job_id": "census-job-1", "status": "queued"}}
    row = _classified({"verb": "docs_export", "worker_reachable": True, "enqueued_actions": ["docs_export"]}, queued, enqueued=True)
    row["outcome"] = str(queued)
    frozen: dict[str, Any] = {"settings": {"delay_seconds": 0.3, "fast_ms": 50.0, "cpu_ms": 250.0}, "plugins": [{"plugin": "g_suite_plugin", "verbs": [row]}]}
    _check(_replay_classes(frozen)["differences"] == [], "frozen observations retain their original classes under pure replay")
    fresh: dict[str, Any] = copy.deepcopy(frozen)
    changed: dict[str, Any] = fresh["plugins"][0]["verbs"][0]
    changed.update({"class": "NOT_MEASURABLE", "missing_piece": "credential: isolated token absent"})
    known = {("g_suite_plugin", "docs_export")}
    differences = _fresh_differences(frozen, fresh, known, set())
    _check(len(differences) == 1 and differences[0]["kind"] == "expected_repair", "fresh differences for the declared landed repair ids stay explicitly disclosed")
    differences = _fresh_differences(frozen, fresh, set(), set())
    _check(len(differences) == 1 and differences[0]["kind"] == "finding" and all(item["reason"] for item in differences), "every difference outside the repaired ids is explained and retained as a finding")
    changed["missing_piece"] = ""
    changed["outcome"] = ""
    _check(any(not item["reason"] for item in _fresh_differences(frozen, fresh, set(), set())), "an unexplained non-repair difference cannot silently disappear")
    try:
        assert None is not None
    except AssertionError as exc:
        _check("assert None is not None" in fixtures.exception_reason(ROOT, exc), "blank assertions name the source-bound missing prerequisite")


def _classified(observations: dict[str, Any], result: Any, *, enqueued: bool = False) -> dict[str, Any]:
    row = {"class": "NOT_MEASURABLE", "wall_ms": 1.0, "fake_touched": False, "touches": [], "calling_thread": 8689368960, "caller_io": False, "worker_reachable": False, "missing_piece": ""} | observations
    census._classify(row, result, "", census.Settings(), enqueued=enqueued)
    return row


def _check_frozen_classifications() -> None:
    # Relevant observations from hash-bound frozen results, never new calls:
    # g_suite 56085b738d97d79b37c41bf76e78369cf8bb72e5a45c90bab492835cba1f1729
    # aws_account_admin b5af8c16d59c8f8962c40b4d87d725fddd0fc8b7ed843440350c0b7c09da8adf
    # aws_midwife 977c707c15223fda583d815175805450f4d9120270adf69bf93b603358376577
    library = "/opt/homebrew/Cellar/python@3.13/3.13.15/Frameworks/Python.framework/Versions/3.13/lib/python3.13/encodings/__pycache__"
    files = [{"operation": "open", "path": f"{library}/{name}.cpython-313.pyc", "thread_id": 8689368960} for name in ("utf_16_le", "utf_16_be", "utf_32_le", "utf_32_be")]
    queued = {"action_status": "completed", "data": {"job_id": "census-job-1", "status": "queued"}, "error": None}
    row = _classified({"verb": "calendar_acl_delete", "wall_ms": 9.570083115249872, "caller_io": True, "file_touches": files, "worker_reachable": True}, queued, enqueued=True)
    _check(row["class"] == "FAST_RETURN" and not row["caller_io"], "B1 frozen g_suite.calendar_acl_delete library reads preserve its matching queued receipt")
    cases = (
        ("audit_lifecycle_roles", 0.024874694645404816, "failed", "plugin not ready; prepare_for_readiness has not run successfully."),
        ("build_and_push", 0.022915657609701157, "rejected", "aws_midwife_plugin not ready: prepare_for_readiness has not run successfully (no address-book config cached)."),
    )
    for verb, wall_ms, status, message in cases:
        result = {"action_status": "completed", "data": {"status": status, "message": message}, "error": None}
        row = _classified({"verb": verb, "wall_ms": wall_ms}, result)
        _check(row["class"] == "NOT_MEASURABLE" and row["missing_piece"] == f"requires valid fixture state/parameters: {message}", f"B2 frozen {verb} names its missing readiness prerequisite")


def _check_classification_controls() -> None:
    failures = (
        {"data": {"error": "missing isolated state"}},
        {"data": {"result": {"success": False, "message": "missing isolated state"}}},
        {"data": {"ready": False, "message": "missing isolated state"}},
        {"data": {"missing_readiness": "missing isolated state"}},
    )
    for result in failures:
        row = _classified({}, {"action_status": "completed"} | result)
        _check(row["class"] == "NOT_MEASURABLE" and "missing isolated state" in row["missing_piece"], f"nested failure marker remains unmeasured: {result}")
    success = {"action_status": "completed", "data": {"status": "completed", "ready": True, "records": [{"status": "failed"}]}}
    _check(_classified({}, success)["class"] == "LOCAL_FAST", "successful local read may contain historical failed records")
    transport = {"fake_touched": True, "touches": [{"boundary": "subprocess.run", "thread_id": 8689368960}]}
    _check(_classified(transport, failures[0])["class"] == "INLINE_IO", "a caller transport touch establishes inline I/O even with a failed reply")
    row = _classified(transport | {"unsafe_attempt": "guard denied host path"}, success)
    _check(row["class"] == "NOT_MEASURABLE", "guard denial stays unmeasurable even with transport evidence")
    probe = census.Probe(census.Settings())
    call = census._Call(started=0.0, finished=0.001, files=[{"operation": "open", "path": "fixture.json", "thread_id": 8689368960}])
    probe_row: dict[str, Any] = {"calling_thread": 8689368960}
    census._record_probe(probe_row, probe, census.JobLedger(), SimpleNamespace(name="local"), call)
    _check(not probe_row["caller_io"] and probe_row["file_touches"] == call.files, "probe retains file diagnostics without promoting them to transport evidence")


def _check_library_roots(scratch: Path) -> None:
    active = scratch / "private-venv/site-packages"
    shared = scratch / "dependency-store/site-packages"
    package = shared / "fixture_dependency"
    unrelated = scratch / "host-data"
    calls = scratch / "calls"
    for directory in (active, package, unrelated, calls):
        directory.mkdir(parents=True)
    (active / package.name).symlink_to(package, target_is_directory=True)
    with patch.object(fixtures.sysconfig, "get_path", return_value=str(active)), patch.object(fixtures, "_protected_roots", return_value=(shared, unrelated)):
        _check(shared in census.library_roots(), "private-venv dependency symlinks retain their physical library root")
        with census.isolated_boundaries(ROOT, calls, census.Probe(census.Settings())) as guard:
            guard.check_path(package / "fixture.py", False)
            _check(True, "linked dependency code is readable under the audit guard")
            _check_guard_refusal(guard.check_path, unrelated / "fixture.txt", False, "unrelated host paths remain outside the library read permission")
            _check_guard_refusal(guard.check_path, package / "fixture.py", True, "linked library permission never allows writes outside scratch")


def _check_descriptor_cleanup(scratch: Path) -> None:
    fixture = scratch / "descriptor-cleanup"
    fixture.mkdir()
    with census.isolated_boundaries(ROOT, fixture, census.Probe(census.Settings())) as guard:
        with tempfile.TemporaryDirectory(dir=fixture) as directory:
            parent = Path(directory)
            (parent / "target").mkdir()
            (parent / "link").symlink_to(parent / "target", target_is_directory=True)
            descriptor = os.open(parent, os.O_RDONLY)
            try:
                os.unlink("link", dir_fd=descriptor)
            finally:
                os.close(descriptor)
        _check(not parent.exists() and not guard.blocked, "descriptor-relative temporary cleanup resolves inside its real fixture parent")
        removals = [entry for entry in guard.files if entry["operation"] == "os.remove"]
        _check(bool(removals) and all(entry["allowed"] and all(Path(path).is_relative_to(fixture) for path in entry["paths"]) for entry in removals), "descriptor cleanup write audit retains resolved contained paths")
        descriptor = os.open(ROOT, os.O_RDONLY)
        try:
            try:
                os.unlink("census-must-never-delete", dir_fd=descriptor)
            except census.UnsafeAttempt:
                _check(True, "a directory descriptor outside the fixture is refused before removal")
            else:
                _check(False, "a directory descriptor outside the fixture is refused before removal")
        finally:
            os.close(descriptor)


def _check_guard_refusal(check: Any, path: Path, write: bool, label: str) -> None:
    try:
        check(path, write)
    except census.UnsafeAttempt:
        _check(True, label)
    else:
        _check(False, label)


def _check_operation_refused(operation: Callable[[], Any], label: str) -> None:
    try:
        operation()
    except census.UnsafeAttempt:
        _check(True, label)
    else:
        _check(False, label)


def _check_descriptor_open(scratch: Path) -> None:
    raw_open = os.open
    with fixtures.descriptor_fixture(scratch) as fixture:
        original = fixture.foreign_snapshot()
        with census.isolated_boundaries(ROOT, fixture.home, census.Probe(census.Settings())) as guard:
            with contextlib.chdir(fixture.home):
                _check_operation_refused(partial(fixture.write, "escaped.txt", fixture.foreign_fd), "R1 foreign-dir_fd open is refused with cwd inside the call home")
                fixture.write("contained.txt", fixture.own_fd)
                _check_operation_refused(partial(fixture.write, "alias-escape.txt", fixture.foreign_fd, raw_open), "a pre-bound relative descriptor-write alias cannot bypass the open wrapper")
            with contextlib.chdir(fixture.home / "nested"):
                fixture.write("live-cwd.txt")
            with contextlib.chdir(fixture.foreign):
                _check_operation_refused(partial(fixture.write, "outside-cwd.txt"), "relative writes after chdir outside the home are refused")
            _check_descriptor_fallback(fixture, guard.files)
            _check_descriptor_family(fixture)
        _check(fixture.foreign_snapshot() == original, "refused descriptor operations preserve every foreign entry and byte")
        _check_open_audit(guard.files, fixture.home, fixture.foreign, bool(guard.blocked))
        writes = [row for row in guard.files if row["write"] and row["allowed"]]
        _check(bool(writes) and all(_contained_write(row, fixture.home) for row in writes), "every allowed open or sibling write resolves inside the owned home")
    _check(all(fixtures._open_writes(("fixture", None, flag)) for flag in (os.O_WRONLY, os.O_RDWR, os.O_CREAT, os.O_TRUNC, os.O_APPEND)), "all five descriptor-open write-intent flags are recognized")


def _check_open_audit(records: list[dict[str, Any]], home: Path, foreign: Path, blocked: bool) -> None:
    opens = {row["path"]: row for row in records if row["operation"] == "open" and row["write"]}
    _check(opens["escaped.txt"]["paths"] == [str(foreign / "escaped.txt")] and not opens["escaped.txt"]["allowed"] and blocked, "R1 audit records the actual foreign target and blocked refusal")
    for name, target in (("contained.txt", home / "contained.txt"), ("live-cwd.txt", home / "nested/live-cwd.txt")):
        _check(opens[name]["paths"] == [str(target)] and opens[name]["allowed"], f"{name} is allowed and audited against its live cwd or directory fd")


def _check_descriptor_fallback(fixture: fixtures.DescriptorFixture, records: list[dict[str, Any]]) -> None:
    before = len(records)
    with patch.object(fixtures.fcntl, "fcntl", side_effect=OSError("synthetic F_GETPATH unavailable")):
        fixture.write("fallback.txt", fixture.own_fd)
        _check_operation_refused(partial(fixture.write, "fallback-escape.txt", fixture.foreign_fd), "owned-root fstat fallback refuses a foreign descriptor")
    bindings = [binding for row in records[before:] for binding in row.get("descriptor_resolutions", [])]
    _check(bool(bindings) and all(row["method"] == "owned fstat scan" and row["fallback_reason"] for row in bindings), "descriptor fallback is explicit in the audit diagnostics")


def _check_descriptor_family(fixture: fixtures.DescriptorFixture) -> None:
    with contextlib.chdir(fixture.home):
        for (name, allowed), (_, refused) in zip(_foreign_operations(fixture.own_fd, fixture.own_fd, fixture.own_file), _foreign_operations(fixture.own_fd, fixture.foreign_fd, fixture.foreign_file), strict=True):
            fixture.prepare_home()
            allowed()
            _check(True, f"{name} permits an owned descriptor")
            _check_operation_refused(refused, f"{name} refuses a foreign descriptor before mutation")


def _foreign_operations(own: int, foreign: int, file: int) -> list[tuple[str, Callable[[], Any]]]:
    return [
        ("mkdir", partial(os.mkdir, "created-directory", dir_fd=foreign)),
        ("unlink", partial(os.unlink, "seed", dir_fd=foreign)),
        ("remove", partial(os.remove, "seed", dir_fd=foreign)),
        ("rmdir", partial(os.rmdir, "directory", dir_fd=foreign)),
        ("chmod", partial(os.chmod, "seed", 0o600, dir_fd=foreign)),
        ("utime", partial(os.utime, "seed", ns=(1, 2), dir_fd=foreign)),
        ("truncate(fd)", partial(os.truncate, file, 0)),
        ("rename source", partial(os.rename, "seed", "escaped", src_dir_fd=foreign, dst_dir_fd=own)),
        ("rename destination", partial(os.rename, "seed", "escaped", src_dir_fd=own, dst_dir_fd=foreign)),
        ("replace destination", partial(os.replace, "seed", "escaped", src_dir_fd=own, dst_dir_fd=foreign)),
        ("link source", partial(os.link, "seed", "escaped", src_dir_fd=foreign, dst_dir_fd=own)),
        ("link destination", partial(os.link, "seed", "escaped", src_dir_fd=own, dst_dir_fd=foreign)),
        ("symlink", partial(os.symlink, "seed", "escaped", dir_fd=foreign)),
    ]


def _check_checkpoint(scratch: Path) -> None:
    settings = census.Settings()
    inventory = _fixture_inventory()
    unknown = {"verb": "queued", "class": "NOT_MEASURABLE", "wall_ms": None, "missing_piece": "synthetic fixture needs state", "static_hits": []}
    with patch.object(census, "enumerate_verbs", return_value=inventory), patch.object(census, "measure_verb", return_value=unknown) as dispatch:
        first = census.run_census(ROOT, scratch, settings, measure=True, only={inventory[0].entry.plugin}, checkpoint_dir=scratch / "checkpoints", context_factory=_synthetic_context)
        _check(dispatch.call_count == 1, "only the selected unfinished fixture is measured")
        first_index = Path(first["checkpoint_index"])
        first_bytes = first_index.read_bytes()
        dispatch.reset_mock()
        second = census.run_census(ROOT, scratch, settings, measure=True, resume_index=first_index, checkpoint_dir=scratch / "checkpoints", context_factory=_synthetic_context)
        _check(dispatch.call_count == 1 and dispatch.call_args is not None and dispatch.call_args.args[1].entry.plugin == inventory[1].entry.plugin, "partial resume dispatches only the missing plugin")
    second_index = Path(second["checkpoint_index"])
    with patch.object(census, "enumerate_verbs", return_value=inventory), patch.object(census, "measure_verb", side_effect=AssertionError("completed handler invoked")):
        final = census.run_census(ROOT, scratch, settings, measure=True, resume_index=second_index)
    _check(final["plugins"] == second["plugins"] and final["plugins"][0] == first["plugins"][0], "completed plugin rows and classifications remain unchanged")
    _check(first_index.read_bytes() == first_bytes, "later checkpoints retain earlier immutable index bytes")
    _check(not final["stopped"] and bool(final.get("advisories")), "an entirely unmeasurable population remains advisory")
    _check_checkpoint_integrity(scratch, second_index, settings)
    _check_carryover_index(scratch, second_index, settings)
    _check_registry_change(scratch, second_index, settings)
    _check_old_schema(scratch, second_index, settings)


def _check_old_schema(scratch: Path, index: Path, settings: census.Settings) -> None:
    """A v1 job-fake observation cannot be resumed under the v2 real context.

    The original index remains byte-for-byte intact. Rejection concerns the
    evidence premise, and never rejects a slow or unavailable plugin result.
    """
    document = json.loads(index.read_text())
    document["schema"] = "fast-return-frozen-plugin-index/v1"
    legacy = scratch / "legacy-v1-index.json"
    legacy.write_text(json.dumps(document))
    try:
        census.load_checkpoint(legacy, settings)
    except ValueError as error:
        _check("schema unknown" in str(error), "v1 fake-job checkpoints cannot enter the v2 context report")
    else:
        _check(False, "v1 fake-job checkpoints cannot enter the v2 context report")


def _check_carryover_index(scratch: Path, index: Path, settings: census.Settings) -> None:
    document = json.loads(index.read_text())
    source = scratch / "source-report.json"
    source.write_text(json.dumps({"settings": document.pop("settings")}))
    document.update(source=str(source), source_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    frozen_index = scratch / "frozen-index.json"
    frozen_index.write_text(json.dumps(document))
    _check(census.load_checkpoint(frozen_index, settings) == census.load_checkpoint(index, settings), "registered frozen-index format reads settings from its hash-bound source report")


def _check_registry_change(scratch: Path, index: Path, settings: census.Settings) -> None:
    inventory = _fixture_inventory()
    inventory[0].actions = [SimpleNamespace(name="different_verb")]
    with patch.object(census, "enumerate_verbs", return_value=inventory), patch.object(census, "measure_verb", side_effect=AssertionError("stale registry dispatched")):
        try:
            census.run_census(ROOT, scratch, settings, measure=True, resume_index=index)
        except ValueError as exc:
            _check("verb population differs" in str(exc), "changed runtime registry refuses stale evidence before dispatch")
        else:
            _check(False, "changed runtime registry refuses stale evidence before dispatch")


def _check_checkpoint_integrity(scratch: Path, index: Path, settings: census.Settings) -> None:
    document = json.loads(index.read_text())
    document["plugins"][0]["sha256"] = "0" * 64
    corrupted = scratch / "corrupt-index.json"
    corrupted.write_text(json.dumps(document))
    try:
        census.load_checkpoint(corrupted, settings)
    except ValueError as exc:
        _check("SHA-256 mismatch" in str(exc), "altered evidence bytes are refused before handler invocation")
    else:
        _check(False, "altered evidence bytes are refused before handler invocation")
    try:
        census.load_checkpoint(index, census.Settings(delay_seconds=0.4))
    except ValueError as exc:
        _check("settings" in str(exc), "incompatible measurement settings cannot silently reuse evidence")
    else:
        _check(False, "incompatible measurement settings cannot silently reuse evidence")
    _check(len(census.load_checkpoint(index, settings)) == 2, "valid checkpoint readback still succeeds after rejected inputs")


def _check_private_connection(scratch: Path) -> None:
    """Check native libpq isolation before permitting a connection.

    The red assertion must run before connect so a broken guard cannot contact
    the host's default socket. Successful connect then proves the explicit
    private TCP route and database, rather than only an environment string.
    """
    import psycopg

    from quality_gates.private_test_pg import load_fixture

    config = load_fixture("fast_return_census")
    with census.isolated_boundaries(ROOT, scratch, census.Probe(census.Settings())):
        # This assertion precedes connect: a red run must never touch the live
        # default socket while demonstrating the missing environment pin.
        assert os.environ.get("PGPORT") == str(config.port), "empty conninfo must resolve to the provisioned private port"
        _check(os.environ.get("TZ") == "UTC", "the inner isolation guard retains the explicit fixture timezone")
        with psycopg.connect("") as connection:
            _check(connection.info.host == "127.0.0.1" and connection.info.port == config.port and connection.info.dbname == config.database, "empty libpq connection resolves only to the private TCP endpoint and census database")


def _check_listener_threads(scratch: Path) -> None:
    """Exercise explicit setup grants while keeping ordinary workers parked.

    The only real bind uses an ephemeral IPv4 loopback endpoint. Both sockets
    close in their own scopes, and the named thread joins before the guard is
    removed. A parked target never starts and cannot outlive this fixture.
    """
    allowed = threading.Event()
    parked = threading.Event()
    with fixtures.isolated_boundaries(ROOT, scratch, census.Probe(census.Settings(delay_seconds=0))) as guard:
        with socket.socket() as listener:
            try:
                listener.bind(("127.0.0.1", 0))
            except OSError:
                _check(True, "loopback bind requires preparer opt-in")
            else:
                _check(False, "loopback bind requires preparer opt-in")
    with fixtures.isolated_boundaries(ROOT, scratch, census.Probe(census.Settings()), run_threads=frozenset({"census-named"}), loopback_listener=True) as guard:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            _check(guard.listener_ports == [listener.getsockname()[1]], "opted-in ephemeral loopback listener is recorded")
        named = threading.Thread(target=allowed.set, name="census-named")
        unnamed = threading.Thread(target=parked.set, name="census-parked")
        named.start()
        unnamed.start()
        named.join()
        _check(allowed.is_set() and not parked.is_set(), "exact named setup thread runs while unnamed worker stays parked")
    unnamed.join(timeout=0)
    unnamed.start()
    _check(not parked.is_set(), "parked targets remain inert during later executor cleanup")


def _check_provisioned(scratch: Path) -> int:
    """Drive discovery and the real service graph through their public caller.

    Pure assertions run before this body. An absent manifest keeps those
    observations visible but returns the established private-fixture skip.
    Provisioned failures propagate and retain the provisioner's teardown.
    """
    from quality_gates.private_test_pg import MissingFixtureError, load_fixture, skip_fixture

    try:
        config = load_fixture("fast_return_census")
    except MissingFixtureError:
        return skip_fixture()
    _check_private_connection(scratch)
    before = _schema_objects(config, scratch)
    inventory = census._census_inventory(ROOT, scratch, census.Settings())
    context, row = _exercise_blob_context(inventory, scratch)
    _check_real_audit(context, row)
    additions = _schema_objects(config, scratch) - before
    _check(bool(additions) and all(schema == context.schema for schema, _kind, _identity in additions), "physical schema-object additions remain in this context's census schema")
    _check_platform_fixtures(inventory, scratch)
    print("PRIVATE_PG_BODY fast_return_census")
    return 0


def _exercise_blob_context(inventory: list[census.PluginVerbs], scratch: Path) -> tuple[Any, dict[str, Any]]:
    """Use a provisioned blob context for state, job and lookup regressions.

    Metadata and attachment retrieval must identify the same stored object.
    The preparer reserves a separate object for destructive handler calls in
    the full census, whose alphabetical order runs deletion before retrieval.
    The returned context receipt survives teardown for the physical audit.
    """
    plugin = next(item for item in inventory if item.entry.plugin == "default_blob_storage_plugin")
    assert plugin.plugin_class is not None
    action = next(item for item in plugin.actions if item.name == "get_blob_metadata")
    with census.plugin_context(ROOT, scratch, plugin.entry.plugin, plugin.plugin_class, census.Probe(census.Settings()), census.discover_preparers()) as context:
        _check_real_state_identity(context)
        row = _check_real_dispatch(context, plugin, action, scratch)
        get_blob = next(item for item in plugin.actions if item.name == "get_blob")
        retrieved = census.measure_verb(ROOT, plugin, get_blob, scratch, census.Settings(), context)
        _check(retrieved["succeeded"] and retrieved["parameters"]["name"] == context.seeded["blob_id"], "get_blob uses its name parameter to retrieve the real seeded blob")
        _check_real_jobs(context)
    return context, row


def _check_real_state_identity(context: Any) -> None:
    """Exercise the runtime protocol boundary that rejected the old facade.

    Python's runtime protocol check inspects attributes statically. Successful
    dynamic delegation therefore did not prove that production consumers could
    bind the service. Keep both the exact concrete type and the protocol check
    in this regression. The full census exercises each retained consumer's
    lookup. This smoke also ships in seeds that omit CodeVetting, so it needs
    no optional consumer import. A transaction must retain its native handle;
    accepting a permissive transaction stub would weaken the job proof.
    """
    from ananta.interfaces.state_management_interface import StateTransaction
    from ananta.interfaces.state_service_protocol import StateServiceProtocol
    from ananta.services.state_service import StateService

    _check(isinstance(context.state, StateServiceProtocol) and type(context.state) is StateService, "recording preserves real StateService identity and runtime-protocol acceptance")
    with context.state.transactional() as transaction:
        _check(isinstance(transaction, StateTransaction), "recorded transaction retains its real native handle")


def _check_platform_fixtures(inventory: list[census.PluginVerbs], scratch: Path) -> None:
    """Advance the formerly unchanged causes through their real consumers.

    These assertions constrain fixture validity, without a timing threshold.
    Cloud status reads its provider's persisted canonical job. Scheduling and
    vector deletion must pass parameter admission and reach their real stores.
    A registry sweep must have actual processes to inspect. Memory lookup may
    legitimately find no matching slug, but it must reach the bound backend.
    Each context stops and removes its home before advancing to the next one.
    ComfyUI also needs its setter-created job facade. Reaching the real store
    for an absent job proves that a direct field assignment cannot satisfy it.
    """
    cases = [
        ("claude_code_cloud_session_source_plugin", "backfill_status"),
        ("codex_cloud_session_source_plugin", "backfill_status"),
        ("default_scheduling_plugin", "execute_in_seconds"),
        ("pgvector_service_plugin", "delete_vectors_action"),
        ("platform_health_plugin", "execute_registry_sweep"),
        ("agent_messaging_plugin", "reinforce_by_slug"),
        ("comfyui_image_generation_plugin", "check_generation_status"),
    ]
    for name, verb in cases:
        plugin = next(item for item in inventory if item.entry.plugin == name)
        assert plugin.plugin_class is not None
        action = next(item for item in plugin.actions if item.name == verb)
        with census.plugin_context(ROOT, scratch, name, plugin.plugin_class, census.Probe(census.Settings()), census.discover_preparers()) as context:
            row = census.measure_verb(ROOT, plugin, action, scratch, census.Settings(), context)
            missing_bindings = {"agent_messaging_plugin": "memory_service_unavailable", "comfyui_image_generation_plugin": "AsyncJobManager not available"}
            if name in missing_bindings:
                _check(bool(row["state_calls"]) and missing_bindings[name] not in row["missing_piece"], f"{name} reaches its real backend beyond service binding")
            else:
                _check(row["succeeded"], f"{name}.{verb} reaches its real fixture-backed operation; cause={row['missing_piece']}")
            if name == "platform_health_plugin":
                _check(bool(context.get_service("process_registry")["processes"]), "health sweep consumes a nonempty production-built registry")


def _schema_objects(config: Any, scratch: Path) -> set[tuple[str, str, str]]:
    """Read physical private-database object identities around the context.

    State-call annotations alone cannot prove that a service's own DDL did
    not create public objects. The provisioner installed vector beforehand;
    new relations, functions, operators and types must belong to this context.
    Native driver use here is a private-fixture probe, never a data consumer.
    """
    import psycopg

    query = """SELECT n.nspname, 'relation', c.oid::text FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    UNION ALL SELECT n.nspname, 'function', p.oid::text FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
    UNION ALL SELECT n.nspname, 'operator', o.oid::text FROM pg_operator o JOIN pg_namespace n ON n.oid=o.oprnamespace
    UNION ALL SELECT n.nspname, 'type', t.oid::text FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace"""
    with fixtures.isolated_boundaries(ROOT, scratch, census.Probe(census.Settings()), postgres=config), psycopg.connect("") as connection:
        rows = connection.execute(query).fetchall()
    return {(str(schema), str(kind), str(identity)) for schema, kind, identity in rows if not str(schema).startswith("pg_") and schema != "information_schema"}


def _check_real_dispatch(context: Any, plugin: census.PluginVerbs, action: Any, scratch: Path) -> dict[str, Any]:
    """Use the real bound handler and a blob seeded by the discovered preparer.

    A state call is required as well as a successful result: an empty or
    early-return fixture cannot masquerade as measuring the database path.
    """
    row = census.measure_verb(ROOT, plugin, action, scratch, census.Settings(), context)
    _check(row["succeeded"] and row["wall_ms"] is not None and bool(row["state_calls"]), "discovered platform preparer measures a real state-backed handler")
    _check(row["preparer"] == ["connectors-v1", "platform-v1", "stores-v1"] and bool(context.seeded.get("blob_id")), "real context consumes configure, seed and parameters through preparer discovery")
    _check(all(call["schema"] == context.schema for call in row["state_calls"]), "all observed state operations target the owned census schema")
    return row


def _check_real_jobs(context: Any) -> None:
    """Retain the real manager's refusal and independently read its valid job.

    This is the NM-1 permissive-fake regression: accepting an absent notes
    field would invalidate the premise of every new FAST_RETURN observation.
    """
    refused = context.jobs.create_job(plugin_name="census", action_name="missing-notes")
    accepted = context.jobs.create_job(plugin_name="census", action_name="valid", request_data={"notes": "synthetic fixture"})
    readback = context.jobs.get_job(accepted.get("data", {}).get("job_id", ""))
    _check(refused["action_status"] == "error" and readback.get("data", {}).get("job", {}).get("provider_name") == "census.valid", "real job manager refuses absent notes and persists valid job for readback")
    scheduler = context.registry.plugins["default_scheduling_plugin"]
    _check(scheduler._services_started and scheduler._scheduler_manager is not None, "scheduler fixture runs its production start hook before schedule dispatch")


def _check_real_audit(context: Any, row: dict[str, Any]) -> None:
    """Check retained write custody after the real lifecycle has stopped.

    Setup and measured writes remain separate in the row, but both must have
    resolved operands under the same owned scratch. Directory removal and
    service-stop receipts are independent assertions, not inferred from class.
    """
    writes = [item for item in context.receipt["setup_file_touches"] + row["file_touches"] if item["write"] and item["allowed"]]
    _check(bool(writes) and all(all(Path(path).is_relative_to(context.scratch) for path in item["paths"]) for item in writes), "real context write audit contains every allowed path in scratch")
    _check(context.receipt["stopped"] and context.receipt["fixture_removed"], "private context stops its state lifecycle and removes the home")


def _check_nm2_classification() -> None:
    """Replay OD-1's state/CPU distinction without timing-sensitive sleeps.

    Recorded waits are not CPU evidence. Sanctioned job persistence remains
    in the evidence population while being excluded from inline-state cost.
    """
    state = {"method": "read_state", "write": False, "elapsed_ms": 60.0, "thread_id": 8689368960}
    observations = {"wall_ms": 100.0, "thread_cpu_ms": 1.0, "state_calls": [state]}
    row = _classified(observations, {"action_status": "completed"})
    _check(row["class"] == "INLINE_IO" and row["inline_boundary"] == "state_service", "slow caller-state work is inline I/O under OD-1")
    observations["state_calls"] = [state | {"sanctioned_job_persistence": True}]
    _check(_classified(observations, {"action_status": "completed"})["class"] == "NOT_MEASURABLE", "sanctioned persistence remains evidence without becoming inline-state cost")
    observations.update(wall_ms=500.0, state_calls=[])
    _check(_classified(observations, {"action_status": "completed"})["class"] == "NOT_MEASURABLE", "wall-time waiting alone cannot establish CPU_INLINE")
    observations["thread_cpu_ms"] = 300.0
    _check(_classified(observations, {"action_status": "completed"})["class"] == "CPU_INLINE", "calling-thread CPU establishes CPU_INLINE")


def _check_residual_contract(scratch: Path) -> None:
    """Drive the optional RESIDUAL contract through the actual census caller.

    The proof runs without constructing or invoking the excluded handler.
    This tests the consumer only; U4 owns the real prover's semantic tests.
    """
    inventory = _fixture_inventory()[:1]
    plugin = inventory[0]
    proofs: list[str] = []

    def prove(root: Path, cls: type[Any], function_name: str) -> dict[str, object]:
        assert root == ROOT and cls is _FixturePlugin
        proofs.append(function_name)
        return {"result": "inconclusive", "evidence": ["synthetic contract fixture"], "prover": "fixture-v1"}

    module = SimpleNamespace(PREPARER_NAME="fixture-residual", PLUGINS=frozenset(), RESIDUAL={(plugin.entry.plugin, "queued"): "R1"}, prove=prove)
    with patch.object(census, "enumerate_verbs", return_value=inventory), patch.object(census, "discover_preparers", return_value=[module]), patch.object(_FixturePlugin, "__init__", side_effect=AssertionError("residual handler constructed")):
        report = census.run_census(ROOT, scratch, census.Settings(), measure=True, context_factory=_synthetic_context)
    row = report["plugins"][0]["verbs"][0]
    _check(proofs == ["queued"] and row["class"] == "RESIDUAL" and row["wall_ms"] is None, "discovered residual proof yields a verdict without handler execution")
    _check(report["residual"] == {"by_reason": {"R1": 1}, "by_proof": {"inconclusive": 1}}, "residual code and proof counts remain separate from measured fast classes")
    _check("inline_state_calls" in report and "state_writes" in report and report["schema"] == "fast-return-census/v2", "v2 report retains context/state evidence even without measured handlers")


def main() -> int:
    with fixtures.smoke_scratch(ROOT) as scratch:
        _check_manifest(scratch)
        _check_library_roots(scratch)
        _check_descriptor_cleanup(scratch)
        _check_descriptor_open(scratch)
        _check_frozen_classifications()
        _check_classification_controls()
        _check_nm2_classification()
        _check_residual_contract(scratch)
        _check_engine(scratch)
        _check_app_home(scratch)
        _check_app_home_failure(scratch)
        _check_credential_cause(scratch)
        _check_unsafe_prerequisites(scratch)
        _check_replay_and_differences()
        _check_checkpoint(scratch)
        _check_listener_threads(scratch)
        provisioned = _check_provisioned(scratch)
    print(f"{_passed} passed, {len(_failed)} failed")
    return 1 if _failed else provisioned


if __name__ == "__main__":
    raise SystemExit(main())
