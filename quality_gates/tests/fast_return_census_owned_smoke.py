"""Owned prerequisites for the real census; no handler result is substituted.

Run: PYTHONPATH=src .venv/bin/python quality_gates/tests/fast_return_census_owned_smoke.py

The provisioner owns PostgreSQL. Missing optional plugins skip by name, and
missing private PostgreSQL returns 77. Files remain in disposable scratch;
unrelated reads and writes must still be refused by the original guard.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
import zoneinfo
from collections.abc import Generator
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fast_return_census as census  # noqa: E402
import fast_return_census_fixtures as fixtures  # noqa: E402

from quality_gates.private_test_pg import MissingFixtureError, load_fixture, skip_fixture  # noqa: E402


@contextlib.contextmanager
def _scratch() -> Generator[Path]:
    # A lane-local smoke directory would inherit the real checkout's .git
    # ancestor and conceal the missing owned worktree prerequisite.
    with tempfile.TemporaryDirectory(prefix="census-owned-", dir=Path("/tmp").resolve()) as directory:
        yield Path(directory)


@contextlib.contextmanager
def _context(plugin_name: str, scratch: Path) -> Generator[tuple[census.PluginVerbs, Any]]:
    inventory = census._census_inventory(ROOT, scratch, census.Settings())
    plugin = next((item for item in inventory if item.entry.plugin == plugin_name), None)
    if plugin is None:
        reason = f"SKIP plugin_optional[{plugin_name}]: source/manifest absent from this profile"
        print(reason)
        raise unittest.SkipTest(reason)
    assert plugin.plugin_class is not None, plugin.error
    with census.plugin_context(ROOT, scratch, plugin_name, plugin.plugin_class, census.Probe(census.Settings()), census.discover_preparers()) as context:
        yield plugin, context


def _measure(plugin: census.PluginVerbs, context: Any, scratch: Path, verb: str) -> dict[str, Any]:
    action = next(item for item in plugin.actions if item.name == verb)
    return census.measure_verb(ROOT, plugin, action, scratch, census.Settings(), context)


class SourceCitations(unittest.TestCase):
    def test_symbol_citations(self) -> None:
        """Line movement passes; absent files/symbols and invalid Python fail."""
        import fast_return_census_media_smoke as media_smoke

        with _scratch() as scratch:
            root = scratch / "citation-proof"
            source = root / "plugins/proof.py"
            source.parent.mkdir(parents=True)
            body = 'class Present:\n    def operation(self):\n        return "blob_id"\n'
            source.write_text(body, encoding="utf-8")
            with patch.object(media_smoke, "ROOT", root):
                self.assertEqual(media_smoke._check_citation("plugins/proof.py:2#Present.operation").name, "operation")
                source.write_text("\n" * 10 + body, encoding="utf-8")
                advisory = io.StringIO()
                with contextlib.redirect_stdout(advisory):
                    self.assertEqual(media_smoke._check_citation("plugins/proof.py:2#Present.operation").lineno, 12)
                    self.assertEqual(media_smoke._check_citation("plugins/proof.py:99999#Present.operation").name, "operation")
                self.assertEqual(advisory.getvalue().count("ADVISORY citation line hint drifted"), 2)
                for citation in ("plugins/missing.py:1#Present.operation", "plugins/proof.py:12#Present.missing", "plugins/proof.py:12#Missing.operation"):
                    with self.assertRaisesRegex(AssertionError, "missing.*citation"):
                        media_smoke._check_citation(citation)
                source.write_text("def invalid(\n", encoding="utf-8")
                with self.assertRaises(SyntaxError):
                    media_smoke._check_citation("plugins/proof.py:1#invalid")


class OwnedFixtures(unittest.TestCase):
    def test_audio_sweep(self) -> None:
        with _scratch() as scratch, _context("audio_processing_plugin", scratch) as (plugin, context):
            row = _measure(plugin, context, scratch, "parameter_sweep")
            self.assertNotIn("has no attribute '_app_home'", row["missing_piece"])
            self.assertTrue(row["succeeded"], row["missing_piece"])
            self.assertTrue(row["state_calls"], "the actual inner operation must be reached")

    def test_comfy_workflow(self) -> None:
        with _scratch() as scratch, _context("comfyui_image_generation_plugin", scratch) as (plugin, context):
            row = _measure(plugin, context, scratch, "generate_image")
            self.assertTrue(row["succeeded"], row["missing_piece"])
            self.assertTrue(row["worker_reachable"])
            self.assertFalse(row["blocked_effects"])

    def test_address_dependency(self) -> None:
        with _scratch() as scratch, _context("default_address_book_plugin", scratch) as (plugin, context):
            row = _measure(plugin, context, scratch, "resolve_location")
            self.assertTrue(row["succeeded"], row["missing_piece"])
            self.assertFalse(row["blocked_effects"])

    def test_health_dependency(self) -> None:
        with _scratch() as scratch, _context("platform_health_plugin", scratch) as (plugin, context):
            row = _measure(plugin, context, scratch, "check_model_capability_catalog")
            self.assertTrue(row["succeeded"], row["missing_piece"])
            self.assertTrue(row["worker_reachable"], "the actual health job must be persisted")
            self.assertEqual(row["job_readback"]["data"]["job"]["status"], "queued")
            self.assertIn("platform_health_plugin-job-worker", row["worker_starts"])
            self.assertTrue(row["state_calls"])
            self.assertFalse(row["touches"], "the health transport belongs to the parked worker")
            self.assertFalse(row["blocked_effects"])

    def test_audio_cache(self) -> None:
        with _scratch() as scratch, _context("audio_processing_plugin", scratch) as (plugin, context):
            row = _measure(plugin, context, scratch, "compute_spectrogram")
            self.assertFalse(row["blocked_effects"], row["missing_piece"])
            self.assertTrue(row["touches"], "the real analysis must reach its guarded transport beyond cache setup")
            writes = [item for item in row["setup_file_touches"] + row["file_touches"] if item["allowed"] and item["write"]]
            self.assertEqual(cast(Any, sys.modules["numba.core.config"]).CACHE_DIR, str(context.scratch / "cache/numba"))
            self.assertTrue(any(Path(path).is_relative_to(context.scratch / "cache") for item in writes for path in item["paths"]))
            self.assertTrue(all(Path(path).is_relative_to(context.scratch) for item in writes for path in item["paths"]))
            self.assertFalse(row["blocked_effects"])

    def test_vetting_root(self) -> None:
        with _scratch() as scratch, _context("code_vetting_plugin", scratch) as (plugin, context):
            row = _measure(plugin, context, scratch, "scan_quality_guidelines")
            self.assertTrue(row["succeeded"], row["missing_piece"])
            self.assertTrue(row["worker_reachable"], "the real job must be persisted")
            self.assertTrue(row["worker_starts"], "the scan worker must remain parked")
            self.assertFalse(row["blocked_effects"])
            assert plugin.plugin_class is not None
            locate_worktree_root = cast(Any, sys.modules[plugin.plugin_class.__module__]).locate_worktree_root
            self.assertEqual(locate_worktree_root(context.app_home), context.app_home)
            self.assertTrue(context.app_home.is_relative_to(context.scratch))
            with self.assertRaisesRegex(RuntimeError, "could not locate"):
                locate_worktree_root(scratch / "unrelated")
            readback = _measure(plugin, context, scratch, "get_vetting_run")
            self.assertTrue(readback["succeeded"], readback["missing_piece"])
            self.assertTrue(readback["state_calls"])

    def test_scheduler(self) -> None:
        with _scratch() as scratch, _context("default_scheduling_plugin", scratch) as (plugin, context):
            row = _measure(plugin, context, scratch, "execute_in_seconds")
            self.assertTrue(row["succeeded"], row["missing_piece"])
            self.assertTrue(context.registry.plugins[plugin.entry.plugin]._services_started)
            self.assertTrue(row["state_calls"], "the actual scheduler/store must be reached")
            self.assertFalse(row["blocked_effects"])

    def test_timezone_scope(self) -> None:
        original_path = zoneinfo.TZPATH
        original_environment = os.environ.get("PYTHONTZPATH")
        with _scratch() as scratch:
            home = scratch / "home"
            home.mkdir()
            foreign = scratch / "foreign-UTC"
            foreign.write_bytes(b"unrelated fixture")
            with self.assertRaisesRegex(RuntimeError, "exercise unwind"), fixtures.owned_timezone(home):
                self.assertEqual(zoneinfo.TZPATH, (str(home / "zoneinfo"),))
                with fixtures.isolated_boundaries(ROOT, home, census.Probe(census.Settings())) as guard:
                    utc = zoneinfo.ZoneInfo("UTC")
                    self.assertEqual(utc.tzname(None), "UTC")
                    offset = utc.utcoffset(None)
                    self.assertIsNotNone(offset)
                    assert offset is not None
                    self.assertEqual(offset.total_seconds(), 0)
                    with self.assertRaises(fixtures.UnsafeAttempt):
                        foreign.read_bytes()
                    with self.assertRaises(fixtures.UnsafeAttempt):
                        foreign.write_bytes(b"refused")
                reads = [item for item in guard.files if item["allowed"] and not item["write"]]
                self.assertTrue(any(str(home / "zoneinfo/UTC") in item["paths"] for item in reads))
                raise RuntimeError("exercise unwind")
            self.assertEqual(foreign.read_bytes(), b"unrelated fixture")
        self.assertEqual(zoneinfo.TZPATH, original_path)
        self.assertEqual(os.environ.get("PYTHONTZPATH"), original_environment)


def main() -> int:
    try:
        load_fixture("fast_return_census")
    except MissingFixtureError:
        return skip_fixture()
    result = unittest.main(exit=False)
    print("PRIVATE_PG_BODY fast_return_census_owned")
    return 0 if result.result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
