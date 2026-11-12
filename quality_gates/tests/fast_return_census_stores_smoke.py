"""Owned stores fixtures through census discovery and the real caller.

quality_gates/tests/fast_return_census_stores_smoke.py

Timing classes are advisory. With a private-PG manifest, each of the three
plugins runs once in its normal verb order. Without it, the owned-file and
parameter contract still runs and the real dispatch leg explicitly skips.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(Path(__file__).resolve().parent), str(ROOT), str(ROOT / "ananta/src"), *(str(path) for path in sorted(ROOT.glob("plugins/*/src")))]

import fast_return_census as census  # noqa: E402
from fast_return_census_context import CensusContext, prepare_parameters, preparers_for  # noqa: E402
from fast_return_census_fixtures import audited_setup, isolated_boundaries, library_roots, private_pg_environment, temporary_app_home  # noqa: E402

TARGETS = {
    "actr_memory_plugin": frozenset({"purge_memories_action", "learn_action", "import_memories_action"}),
    "default_blob_storage_plugin": frozenset({"store_blob"}),
}
PLUGINS = frozenset({*TARGETS, "default_address_book_plugin"})
REQUIRED = frozenset({*PLUGINS, "postgres_state_management_plugin", "default_scheduling_plugin", "openai_embeddings_plugin", "pgvector_service_plugin"})
REMAINDERS = {
    ("default_address_book_plugin", "resolve_location"): "N021: agent_messaging_plugin absent from census context",
    ("default_blob_storage_plugin", "store_blob_from_file"): "N053: owned by C2's connector parameter fixture",
}


def _assert_measured(plugin: str, row: dict[str, Any]) -> None:
    """A fixture must reach a real result or transport, without a host refusal."""
    assert row["class"] != "NOT_MEASURABLE", (plugin, row["verb"], row["missing_piece"])
    assert not row.get("blocked_effects"), (plugin, row["verb"], row["blocked_effects"])
    assert row["succeeded"] or row["fake_touched"], (plugin, row["verb"], row["outcome"])


def _contract(scratch: Path) -> None:
    """Use the discovered public seed and the census's parameter caller."""
    modules = census.discover_preparers()
    stores = next(module for module in modules if module.__name__.endswith(".stores"))
    assert stores.PREPARER_NAME == "stores-v1"
    assert set(stores.PLUGINS) == set(TARGETS)
    assert not any(getattr(stores, name, None) for name in ("RUN_THREADS", "LOOPBACK_LISTENER", "MEASURE_EXCLUDED"))
    for plugin in sorted(TARGETS):
        _owned_contract(stores, modules, plugin, scratch)


def _owned_contract(stores: Any, modules: list[Any], plugin: str, scratch: Path) -> None:
    home = scratch / plugin
    application, export = home / "application", home / "export"
    application.mkdir(parents=True)
    export.mkdir()
    view = SimpleNamespace(app_home=application, export_root=export, scratch=home)
    probe = census.Probe(census.Settings())
    with isolated_boundaries(ROOT, home, probe) as audit:
        setup_touches = list(probe.touches)
        print(f"SETUP stores boundary installation {plugin}: {json.dumps(setup_touches)}", flush=True)
        # Match the census caller: patch-installation probes precede fixtures.
        probe.touches.clear()
        seeded = stores.seed(plugin, view)
        context = SimpleNamespace(modules=preparers_for(plugin, modules), seeded=seeded, app_home=application)
        for verb in sorted(TARGETS[plugin]):
            _parameter_contract(context, plugin, verb, home, export)
        untouched = {"memory_id": "already-owned"}
        assert stores.parameters(plugin, "stop_memorizing_action", untouched, seeded, application) == untouched
        assert not probe.touches and not audit.blocked, {"touches": probe.touches, "blocked": audit.blocked, "setup_touches": setup_touches}
        writes = [item for item in audit.files if item["write"]]
        assert all(Path(path).is_relative_to(home) for item in writes for path in item["paths"])
    print(f"PASS discovered owned stores contract: {plugin}")


def _parameter_contract(context: Any, plugin: str, verb: str, home: Path, export: Path) -> None:
    original: dict[str, Any] = {"unrelated": "retained", "content": "wrong string"}
    before = copy.deepcopy(original)
    parameters = prepare_parameters(context, plugin, verb, original)
    assert original == before
    assert parameters["unrelated"] == "retained"
    if plugin == "actr_memory_plugin":
        _assert_memory_input(verb, parameters, home, export)
    else:
        _assert_blob_input(verb, parameters)


def _assert_memory_input(verb: str, parameters: dict[str, Any], home: Path, export: Path) -> None:
    if verb == "purge_memories_action":
        assert parameters["confirm"] is True
        assert json.loads((home / "config/identity.json").read_text())["identity"]
    elif verb == "import_memories_action":
        path = Path(parameters["file_path"])
        assert path.is_relative_to(export) and json.loads(path.read_text())["memories"][0]["content"]
    else:
        assert verb == "learn_action"
        assert Path(parameters["path"]).is_relative_to(home)
        assert Path(parameters["path"]).read_text()


def _assert_blob_input(verb: str, parameters: dict[str, Any]) -> None:
    assert verb == "store_blob"
    assert isinstance(parameters["content"], bytes) and parameters["content"]


@contextmanager
def _context(root: Path, scratch: Path, plugin: str, plugin_class: type[Any], probe: Any, modules: list[Any]) -> Generator[CensusContext]:
    """Give concurrent private-PG smoke processes distinct physical schemas."""
    with temporary_app_home(scratch) as home:
        context = CensusContext(root, home, plugin, plugin_class, probe, modules)
        context.schema = f"stores_{uuid4().hex}"
        context.app_home, context.export_root = home / "application", home / "export"
        context.app_home.mkdir()
        context.export_root.mkdir()
        environment = {"SOLET_NAME": context.identity, "APP_HOME": str(context.app_home), "HOME": str(home), "TMPDIR": str(home), "TZ": "UTC", "PATH": os.environ.get("PATH", ""), **private_pg_environment(context.config)}
        with patch.dict(os.environ, environment, clear=True), audited_setup(root, home):
            try:
                context.start()
                yield context
            finally:
                context.stop()
    context.receipt["fixture_removed"] = not home.exists()


def _measure(scratch: Path) -> None:
    """The entry point owns normal ordering, guards, clocks and classification."""
    report = census.run_census(ROOT, scratch, census.Settings(), measure=True, only=set(PLUGINS), context_factory=_context)
    selected = [item for item in report["plugins"] if item["plugin"] in PLUGINS]
    print("STORES_CENSUS_REPORT=" + json.dumps({"plugins": selected, "fixture_removed": report["fixture_removed"]}, default=str), flush=True)
    assert len(selected) == len(PLUGINS)
    for plugin in selected:
        _check_plugin(plugin)
    assert report["fixture_removed"]


def _check_plugin(plugin: dict[str, Any]) -> None:
    name = plugin["plugin"]
    assert plugin["context_run"]["stopped"] and plugin["context_run"]["fixture_removed"]
    if name == "actr_memory_plugin":
        _assert_reseed(plugin)
    for row in plugin["verbs"]:
        if row["verb"] in TARGETS.get(name, frozenset()):
            _assert_measured(name, row)
            if row["verb"] == "learn_action":
                assert row["fake_touched"] or row["worker_reachable"], "owned nonempty learn file must reach embedding, not the hidden-path skip"
        remainder = REMAINDERS.get((name, row["verb"]))
        if remainder is not None:
            print(f"NAMED PREREQUISITE {remainder}; {row['missing_piece']}")
    print(f"PASS real ordered stores census: {name} {plugin['counts']}")


def _assert_reseed(plugin: dict[str, Any]) -> None:
    """Real purge precedes the hook; setup I/O never enters the stop clocks."""
    rows = plugin["verbs"]
    names = [row["verb"] for row in rows]
    assert names.index("purge_memories_action") < names.index("stop_memorizing_action")
    row = rows[names.index("stop_memorizing_action")]
    _assert_measured(plugin["plugin"], row)
    hooks = plugin["context_run"]["before_verb"]
    assert len(hooks) == 1
    hook = hooks[0]
    assert hook["verb"] == "stop_memorizing_action"
    assert hook["memory_id"] != hook["previous_memory_id"]
    assert row["parameters"]["memory_id"] == hook["memory_id"]
    assert any(call["table"] == "memorization" and call["write"] for call in hook["state_calls"])
    assert all(call not in row["state_calls"] for call in hook["state_calls"])
    print(f"PASS untimed real memory reseed after purge: stop_memorizing_action={row['class']}")


def _can_measure() -> bool:
    absent = [name for name in sorted(REQUIRED) if not (ROOT / "plugins" / name / "src" / name).is_dir()]
    for name in absent:
        print(f"SKIP real stores census: {name} plugin absent from this profile")
    if absent:
        return False
    from quality_gates.private_test_pg import MissingFixtureError, load_fixture

    try:
        load_fixture("fast_return_census")
    except MissingFixtureError:
        print("SKIP real stores census: private PostgreSQL fast_return_census fixture absent; use quality_gates/provision_test_pg.py --exec")
        return False
    return True


@contextmanager
def _scratch() -> Generator[Path]:
    """ACTR ignores hidden ancestors, including the usual .scratch directory."""
    parent = ROOT / "workbench"
    created = not parent.exists()
    parent.mkdir(exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="census-stores-", dir=parent) as directory:
            yield Path(directory).resolve()
    finally:
        if created:
            try:
                parent.rmdir()
            except OSError as error:
                print(f"ADVISORY stores smoke retains nonempty/unavailable parent {parent}: {error}")


def main() -> int:
    allowed = (ROOT, Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), *library_roots())
    sys.path[:] = [value for value in sys.path if any(Path(value).resolve().is_relative_to(base) for base in allowed)]
    with _scratch() as scratch:
        _contract(scratch)
        if _can_measure():
            _measure(scratch)
    assert not scratch.exists()
    print("PASS stores fixture cleanup")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
