"""Address-derived roots, tree protection and connector binding contracts."""

from __future__ import annotations

import ast
import importlib
import io
import os
import sys
import tempfile
import unicodedata
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta/src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import containment_sweep_support as sweep  # noqa: E402
from ananta.file_locations import containment  # noqa: E402
from ananta.file_locations.resolution import PathAddress, SoletTree  # noqa: E402

EXPORTERS = ("external_postgres", "snowflake", "salesforce", "zuora", "marketo")


def address(name: str, directory: Path) -> PathAddress:
    return PathAddress(name, directory, frozenset(), frozenset(), datetime.now(UTC))


def _profile() -> str:
    return os.environ.get("SOLET_PROFILE", "unknown (SOLET_PROFILE unset)")


def _installed_plugins(root: Path, names: tuple[str, ...], profile: str) -> tuple[str, ...]:
    present: list[str] = []
    for name in names:
        if (root / "plugins" / f"{name}_plugin").is_dir():
            present.append(name)
        else:
            print(f"ADVISORY: connector {name}_plugin is absent in profile {profile}; its connector-specific containment checks are unavailable.")
    return tuple(present)


class ContainmentSmoke(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.tree = SoletTree(self.root / "clone", self.root / "clone/profile")
        self.home = self.root / "Documents/demo"

    def api(self, name: str) -> Any:
        self.assertTrue(hasattr(containment, name), f"B2 shared API missing: {name}")
        return getattr(containment, name)

    def check(self, path: Path, roots: Any, *, suffix: str | None = ".tsv") -> Any:
        return self.api("assert_path_allowed")(str(path), roots, self.tree, suffix=suffix, param_name="output_path", plugin_name="test")

    def roots(self, addresses: list[PathAddress], launch: str | None = None) -> Any:
        return self.api("derive_allowed_roots")(addresses, launch, self.tree, self.home)

    def test_registered_launch_and_empty_address_defaults(self) -> None:
        project, launch = self.root / "project", self.root / "launch"
        launch.mkdir()
        roots = self.roots([address("project", project)], str(launch))
        for directory in (project, launch, self.home, self.home / "Exports.nosync"):
            result = self.check(directory / "out.tsv", roots)
            self.assertEqual(result.value, str(directory / "out.tsv"))
            self.assertEqual(result.advisories, roots.advisories)
        fallback = self.roots([])
        self.assertTrue(fallback.advisories)
        self.assertEqual(self.check(self.home / "Exports.nosync/out.tsv", fallback).advisories, fallback.advisories)
        registered = self.roots([address("solet_home", project)])
        self.assertEqual(self.check(project / "Exports.nosync/out.tsv", registered).value, str(project / "Exports.nosync/out.tsv"))

    def test_roots_are_live_correctable_and_retirable(self) -> None:
        old, new = self.root / "old", self.root / "new"
        before = self.roots([address("project", old)])
        after = self.roots([address("project", new)])
        self.assertEqual(self.check(old / "out.tsv", before).value, str(old / "out.tsv"))
        with self.assertRaises(self.api("PathRefusedError")):
            self.check(old / "out.tsv", after)
        self.assertEqual(self.check(new / "out.tsv", after).value, str(new / "out.tsv"))
        with self.assertRaises(self.api("PathRefusedError")):
            self.check(new / "out.tsv", self.roots([]))

    def test_tree_outside_traversal_and_symlink_refusals(self) -> None:
        broad = self.roots([address("legacy_parent", self.root)])
        for destination in (self.tree.repo_root / "out.tsv", self.tree.app_home / "out.tsv"):
            with self.subTest(destination=destination), self.assertRaises(self.api("PathRefusedError")):
                self.check(destination, broad)
        allowed = self.root / "allowed"
        allowed.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (allowed / "escape").symlink_to(outside, target_is_directory=True)
        roots = self.roots([address("project", allowed)])
        for destination in (outside / "out.tsv", allowed / "../outside/out.tsv", allowed / "escape/out.tsv", allowed / "escape/missing/out.tsv"):
            with self.subTest(destination=destination), self.assertRaisesRegex(self.api("PathRefusedError"), "register.*path address"):
                self.check(destination, roots)
        for raw in ("relative.tsv", str(allowed / "out.csv"), str(allowed / "bad\x00.tsv")):
            with self.assertRaises(self.api("PathRefusedError")):
                self.api("assert_path_allowed")(raw, roots, self.tree, suffix=".tsv", param_name="output_path", plugin_name="test")
        self.assertEqual(self.check(allowed / "any.bin", roots, suffix=None).value, str(allowed / "any.bin"))

    def test_session_scope_is_reset_and_advisories_retained(self) -> None:
        scope = self.api("location_session")
        current = self.api("location_session_id")
        advisories = self.api("location_advisories")
        record = self.api("retain_location_advisories")
        self.assertIsNone(current())
        with scope("caller-a"):
            self.assertEqual(current(), "caller-a")
            record(("capture unavailable",))
            self.assertEqual(advisories(), ("capture unavailable",))
            with scope("caller-b"):
                self.assertEqual(current(), "caller-b")
                self.assertEqual(advisories(), ())
            self.assertEqual(current(), "caller-a")
        self.assertIsNone(current())
        self.assertEqual(advisories(), ())

    def test_connector_capture_present_and_unavailable(self) -> None:
        for exporter in _installed_plugins(ROOT, EXPORTERS, _profile()):
            with self.subTest(exporter=exporter):
                sys.path.insert(0, str(ROOT / f"plugins/{exporter}_plugin/src"))
                module = importlib.import_module(f"{exporter}_plugin.plugin")
                classes = {"external_postgres": "ExternalPostgresPlugin", "snowflake": "SnowflakePlugin", "salesforce": "SalesforcePlugin", "zuora": "ZuoraPlugin", "marketo": "MarketoPlugin"}
                plugin = getattr(module, classes[exporter])()
                plugin.initialize({})
                orchestrator = MagicMock()
                orchestrator.APP_HOME = str(self.tree.app_home)
                service = MagicMock()
                service.list_path_addresses.return_value = {"action_status": "completed", "data": {"addresses": []}}
                orchestrator.get_service.return_value = service
                surface = orchestrator.plugin_manager.get_plugin.return_value._platform_surface
                launch = self.root / exporter
                launch.mkdir()
                surface.launch_directory_for_session.return_value = {"path": str(launch), "source": "launcher"}
                plugin.orchestrator_ref = orchestrator
                with self.api("location_session")("exact-session"):
                    self.assertEqual(plugin._export_path_gate(str(launch / "out.tsv")), str(launch / "out.tsv"))
                    surface.launch_directory_for_session.assert_called_with("exact-session")
                surface.launch_directory_for_session.return_value = None
                service.list_path_addresses.return_value = {"action_status": "completed", "data": {"addresses": [{"name": "project", "tags": [], "updated_at": datetime.now(UTC).isoformat(), "entries": [{"field_type": "path", "value": str(launch)}]}]}}
                with self.api("location_session")("missing-capture"):
                    self.assertEqual(plugin._export_path_gate(str(launch / "out.tsv")), str(launch / "out.tsv"))
                    self.assertTrue(any("launch" in text.lower() for text in self.api("location_advisories")()))

    def test_only_shared_connector_containment_survives(self) -> None:
        for exporter in _installed_plugins(ROOT, EXPORTERS, _profile()):
            self._check_exporter(ROOT, exporter)
        self.assertFalse((ROOT / "plugins/salesforce_plugin/src/salesforce_plugin/import_containment.py").exists())

    def _check_exporter(self, root: Path, exporter: str) -> None:
        source = root / f"plugins/{exporter}_plugin/src/{exporter}_plugin"
        self.assertFalse((source / "export_containment.py").exists())
        plugin = ast.parse((source / "plugin.py").read_text())
        calls = [node for node in ast.walk(plugin) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "assert_path_allowed"]
        self.assertEqual(len(calls), 1, exporter)

    def test_no_connector_defines_private_containment(self) -> None:
        for name in _installed_plugins(ROOT, (*EXPORTERS, "jira", "g_suite"), _profile()):
            for path in (ROOT / f"plugins/{name}_plugin/src").rglob("*.py"):
                tree = ast.parse(path.read_text())
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self.assertFalse(any(isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr in {"commonpath", "is_relative_to"} for call in ast.walk(node)), str(path))

        if "snowflake" in _installed_plugins(ROOT, ("snowflake",), _profile()):
            stage = (ROOT / "plugins/snowflake_plugin/src/snowflake_plugin/stage_paths.py").read_text()
            self.assertIn("assert_path_allowed", stage)
        for name in _installed_plugins(ROOT, (*EXPORTERS, "jira", "g_suite"), _profile()):
            declaration = ROOT / f"plugins/{name}_plugin/plugin.yaml"
            text = declaration.read_text()
            self.assertNotIn("export_allowed_roots", text)
            self.assertNotIn("import_allowed_roots", text)

    def test_workers_bind_only_the_job_origin_and_reset(self) -> None:
        decode = self.api("job_location_session")
        self.assertEqual(decode({"action_status": "completed", "data": {"job": {"metadata": '{"session_id":"exact-origin"}'}}}), "exact-origin")
        self.assertIsNone(decode({"action_status": "completed", "data": {"job": {"metadata": "malformed"}}}))
        for name in _installed_plugins(ROOT, EXPORTERS[:-1], _profile()):
            with self.subTest(plugin=name):
                sys.path.insert(0, str(ROOT / f"plugins/{name}_plugin/src"))
                worker = importlib.import_module(f"{name}_plugin.async_jobs")
                manager = MagicMock()
                observed: list[str | None] = []
                with patch.object(worker, "_process_job_in_context", side_effect=lambda *_args, observed=observed: observed.append(self.api("location_session_id")())):
                    manager.get_job.return_value = {"action_status": "completed", "data": {"job": {"metadata": {"session_id": "exact-origin"}}}}
                    worker._process_job(MagicMock(), manager, "job", "export")
                    manager.get_job.return_value = {"action_status": "error"}
                    worker._process_job(MagicMock(), manager, "job", "export")
                self.assertEqual(observed, ["exact-origin", None])
                self.assertIsNone(self.api("location_session_id")())

    def test_optional_connector_presence_absence_and_broken_presence(self) -> None:
        root = self.root / "profile-fixture"
        source = root / "plugins/external_postgres_plugin/src/external_postgres_plugin"
        source.mkdir(parents=True)
        (source / "plugin.py").write_text("def check():\n    return assert_path_allowed()\n")
        self.assertEqual(_installed_plugins(root, ("external_postgres",), "fixture-present"), ("external_postgres",))
        self._check_exporter(root, "external_postgres")
        (source / "export_containment.py").write_text("# broken: a private containment copy survived\n")
        with self.assertRaises(AssertionError):
            self._check_exporter(root, "external_postgres")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(_installed_plugins(root, ("snowflake",), "fixture-absent"), ())
        self.assertIn("snowflake_plugin", output.getvalue())
        self.assertIn("fixture-absent", output.getvalue())
        self.assertIn("ADVISORY", output.getvalue())


class PhysicalContainmentSmoke(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "R\u00e9po"
        self.app = self.repo / "profile"
        self.app.mkdir(parents=True)
        self.tree = SoletTree(self.repo, self.app)
        self.allowed = self.root / "Caf\u00e9"
        self.allowed.mkdir()
        self.roots = containment.Located((self.root,), ())

    def check(self, path: Path, roots: containment.Located[tuple[Path, ...]] | None = None) -> containment.Located[str]:
        return containment.assert_path_allowed(str(path), roots or self.roots, self.tree, suffix=".tsv", param_name="output_path", plugin_name="physical-test")

    def native_alias(self, path: Path, spelling: str, label: str) -> Path:
        alias = path.with_name(spelling)
        if not alias.exists() or not alias.samefile(path):
            advisory = f"ADVISORY: {label} alias is distinct on this native filesystem ({sys.platform}); that alias regression is unavailable."
            print(advisory)
            self.skipTest(advisory)
        self.assertNotEqual(str(path), str(alias))
        self.assertEqual((path.stat().st_dev, path.stat().st_ino), (alias.stat().st_dev, alias.stat().st_ino))
        return alias

    def test_native_unicode_alias_refuses_protected_tree(self) -> None:
        alias = self.native_alias(self.repo, unicodedata.normalize("NFD", self.repo.name), "NFC/NFD")
        existing = self.repo / "existing.tsv"
        existing.write_text("protected fixture")
        self.assertTrue((alias / existing.name).samefile(existing))
        for path in (alias / existing.name, alias / "out.tsv", alias / "missing/out.tsv", alias / "profile/out.tsv"):
            with self.subTest(path=path), self.assertRaises(containment.PathRefusedError):
                self.check(path)

    def test_native_unicode_alias_admits_physical_allowed_root(self) -> None:
        alias = self.native_alias(self.allowed, unicodedata.normalize("NFD", self.allowed.name), "NFC/NFD allowed-root")
        for root, target in ((self.allowed, alias), (alias, self.allowed)):
            with self.subTest(root=root):
                result = self.check(target / "out.tsv", containment.Located((root,), ("retained",)))
                self.assertTrue(Path(result.value).parent.samefile(self.allowed))
                self.assertEqual(result.advisories, ("retained",))

    def test_native_case_aliases_use_physical_roots(self) -> None:
        alias = self.native_alias(self.repo, self.repo.name.swapcase(), "case-different protected-root")
        with self.assertRaises(containment.PathRefusedError):
            self.check(alias / "out.tsv")
        allowed_alias = self.native_alias(self.allowed, self.allowed.name.swapcase(), "case-different allowed-root")
        result = self.check(allowed_alias / "out.tsv", containment.Located((self.allowed,), ()))
        self.assertTrue(Path(result.value).parent.samefile(self.allowed))

    def test_symlink_alias_refuses_tree_and_admits_allowed_root(self) -> None:
        protected_alias, allowed_alias = self.root / "protected-link", self.root / "allowed-link"
        protected_alias.symlink_to(self.repo, target_is_directory=True)
        allowed_alias.symlink_to(self.allowed, target_is_directory=True)
        with self.assertRaises(containment.PathRefusedError):
            self.check(protected_alias / "out.tsv")
        result = self.check(allowed_alias / "out.tsv", containment.Located((self.allowed,), ()))
        self.assertTrue(Path(result.value).parent.samefile(self.allowed))

    def test_hard_link_to_protected_file_is_not_admitted(self) -> None:
        protected = self.repo / "existing.tsv"
        protected.write_text("protected fixture")
        alias = self.allowed / "linked.tsv"
        try:
            os.link(protected, alias)
        except OSError as exc:
            advisory = f"ADVISORY: native hard-link fixture unavailable ({sys.platform}, errno={exc.errno})."
            print(advisory)
            self.skipTest(advisory)
        self.assertTrue(alias.samefile(protected))
        with self.assertRaises(containment.PathRefusedError) as caught:
            self.check(alias, containment.Located((self.allowed,), ()))
        self.assertEqual(caught.exception.code, "file_locations.identity_unavailable")

    def test_identity_failure_refuses_existing_target_with_named_code(self) -> None:
        target = self.allowed / "existing.tsv"
        target.write_text("ordinary fixture")
        self.assertEqual(self.check(target).value, str(target))
        reached: list[sweep.Read] = []

        def unavailable(_index: int, read: sweep.Read) -> None:
            # The guard's identity read of the target: fstatat under its parent's descriptor.
            if read == ("stat", str(target)):
                reached.append(read)
                raise PermissionError("identity unavailable")

        with sweep.counted_metadata_reads(unavailable), self.assertRaises(containment.PathRefusedError) as caught:
            self.check(target)
        self.assertTrue(reached, "fixture must fail the guard's identity read of the target")
        self.assertEqual(caught.exception.code, "file_locations.identity_unavailable")

    def test_missing_allowed_root_does_not_admit_its_existing_parent(self) -> None:
        missing = self.allowed / "not-created"
        roots = containment.Located((missing,), ())
        self.assertEqual(self.check(missing / "nested/out.tsv", roots).value, str(missing / "nested/out.tsv"))
        with self.assertRaises(containment.PathRefusedError):
            self.check(self.allowed / "unregistered/out.tsv", roots)

    def test_all_exporters_refuse_native_unicode_protected_alias(self) -> None:
        alias = self.native_alias(self.repo, unicodedata.normalize("NFD", self.repo.name), "NFC/NFD exporter")
        classes = {"external_postgres": "ExternalPostgresPlugin", "snowflake": "SnowflakePlugin", "salesforce": "SalesforcePlugin", "zuora": "ZuoraPlugin", "marketo": "MarketoPlugin"}
        for name in _installed_plugins(ROOT, EXPORTERS, _profile()):
            with self.subTest(exporter=name):
                sys.path.insert(0, str(ROOT / f"plugins/{name}_plugin/src"))
                module = importlib.import_module(f"{name}_plugin.plugin")
                plugin = getattr(module, classes[name])()
                plugin.initialize({})
                orchestrator = MagicMock()
                orchestrator.APP_HOME = str(self.app)
                service = MagicMock()
                service.list_path_addresses.return_value = {"action_status": "completed", "data": {"addresses": [{"name": "broad", "tags": [], "updated_at": datetime.now(UTC).isoformat(), "entries": [{"field_type": "path", "value": str(self.root)}]}]}}
                orchestrator.get_service.return_value = service
                orchestrator.plugin_manager.get_plugin.return_value._platform_surface.launch_directory_for_session.return_value = None
                plugin.orchestrator_ref = orchestrator
                with containment.location_session("physical-origin"), self.assertRaises(containment.PathRefusedError):
                    plugin._export_path_gate(str(alias / "out.tsv"))


class AncestryChangeSmoke(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "Repo"
        self.app = self.repo / "profile"
        self.app.mkdir(parents=True)
        self.allowed = self.root / "Project"
        self.allowed.mkdir()
        self.child = self.allowed / "current"
        self.child.mkdir()
        self.tree = SoletTree(self.repo, self.app)
        self.roots = containment.Located((self.allowed,), ("retained",))

    def check(self, path: Path) -> containment.Located[str]:
        return containment.assert_path_allowed(str(path), self.roots, self.tree, suffix=None, param_name="path", plugin_name="ancestry-test")

    def replace_parent(self) -> None:
        self.child.rename(self.allowed / "prior-current")
        self.child.symlink_to(self.repo, target_is_directory=True)

    def test_reviewer_parent_replacement_during_root_measurement_is_refused(self) -> None:
        root_reads = 0
        changed = False

        def measured(_index: int, read: sweep.Read) -> None:
            # The walk reads the allowed root by open for the candidate, then by fstatat measuring the root.
            nonlocal root_reads, changed
            if read in (("open", str(self.allowed)), ("stat", str(self.allowed))):
                root_reads += 1
                if root_reads == 2:
                    self.replace_parent()
                    changed = True

        with sweep.counted_metadata_reads(measured), self.assertRaises(containment.PathRefusedError):
            self.check(self.child / "out.tsv")
        self.assertTrue(changed, "reviewer fixture must replace the parent during validation")
        self.assertTrue(self.child.samefile(self.repo))
        self.assertFalse((self.repo / "out.tsv").exists(), "fixture never writes a protected file")

    def test_unchanged_ancestry_returns_existing_and_missing_targets(self) -> None:
        existing = self.child / "existing.tsv"
        existing.write_text("ordinary fixture")
        before = self.child.stat()
        for target in (existing, self.child / "missing/nested/out.tsv"):
            with self.subTest(target=target):
                result = self.check(target)
                self.assertEqual(result.value, str(target))
                self.assertEqual(result.advisories, ("retained",))
        after = self.child.stat()
        self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))

    def test_parent_swap_during_ancestry_capture_is_refused(self) -> None:
        captured = False
        changed = False

        def measured(_index: int, read: sweep.Read) -> None:
            # Swap immediately after the walk's capture open of the parent returns.
            nonlocal captured, changed
            if captured and not changed:
                self.replace_parent()
                changed = True
            captured = captured or read == ("open", str(self.child))

        with sweep.counted_metadata_reads(measured), self.assertRaises(containment.PathRefusedError) as caught:
            self.check(self.child / "out.tsv")
        self.assertTrue(changed, "capture fixture must reach the real filesystem swap")
        self.assertTrue(self.child.samefile(self.repo))
        self.assertEqual(caught.exception.code, "file_locations.identity_changed")
        self.assertFalse((self.repo / "out.tsv").exists())

    def test_parent_swap_during_final_ancestry_verification_is_refused(self) -> None:
        parent_opens = 0
        changed = False

        def measured(_index: int, read: sweep.Read) -> None:
            # The parent is read by the walk's open, then by the final whole-path read (one-call stat or open).
            nonlocal parent_opens, changed
            if read in (("open", str(self.child)), ("stat_without_links", str(self.child))):
                parent_opens += 1
                if parent_opens == 2:
                    self.replace_parent()
                    changed = True

        with sweep.counted_metadata_reads(measured), self.assertRaises(containment.PathRefusedError) as caught:
            self.check(self.child / "out.tsv")
        self.assertTrue(changed, "verification fixture must reach the real filesystem swap")
        self.assertTrue(self.child.samefile(self.repo))
        self.assertEqual(caught.exception.code, "file_locations.identity_changed")
        self.assertFalse((self.repo / "out.tsv").exists())


NoLinkStatSmoke = sweep.NoLinkStatSmoke
MetadataChangeMatrixSmoke = sweep.MetadataChangeMatrixSmoke
FinalReadFixtureSmoke = sweep.FinalReadFixtureSmoke
FallbackLeafMatrixSmoke = sweep.FallbackLeafMatrixSmoke


if __name__ == "__main__":
    unittest.main()
