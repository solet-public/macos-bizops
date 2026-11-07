"""Seed #91: a v2-only enrollment is visible and its six-command advice has an exit."""

from __future__ import annotations

import json
import re
import shlex
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src"), str(Path(__file__).resolve().parent)]

from _step5_support import Fixture, build_fixture  # noqa: E402
from _step6_support import cli_resume  # noqa: E402
from import_enrollment_rerun_smoke import _inspection  # noqa: E402
from solet_manager import import_enrollment as enrollment  # noqa: E402
from solet_manager import manager_cli  # noqa: E402
from solet_manager.enrolled_target import TARGET_RECOVERY, identity_drift_message, observe_identity_mismatches  # noqa: E402
from solet_manager.errors import StateError  # noqa: E402
from solet_manager.existing_install_inspection import (  # noqa: E402
    ExistingInstallInspectionRequest,
    ExistingInstallInspectionResult,
    InspectionEffectTracker,
    InspectionStatus,
    _inventory_check,
    _ProductionInspectionEffectTracker,
)
from solet_manager.import_enrollment import ImportRequest  # noqa: E402
from solet_manager.maintenance_inventory import read_maintenance_inventory_v2, write_maintenance_inventory_v2  # noqa: E402
from solet_manager.models import ActiveOperation, InstanceInventoryRecordV2, MaintenanceOperationKind  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.state_io import write_content_addressed_json  # noqa: E402

from solet_setup_contracts import canonical_sha256  # noqa: E402


def _with_inventory(request: ExistingInstallInspectionRequest, **_kwargs: Any) -> ExistingInstallInspectionResult:
    result = _inspection(request)
    check = _inventory_check(request.manager_paths, _ProductionInspectionEffectTracker(), result.target_identity)
    return replace(result, checks=(*result.checks, check))


class EnrolledInventoryCollisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def _fixture(self) -> tuple[Fixture, InstanceInventoryRecordV2]:
        fixture = build_fixture(self.root, at_candidate=True)
        record = fixture.record()
        stale = replace(record, target=replace(record.target, filesystem_identity=replace(record.target.filesystem_identity, inode=record.target.filesystem_identity.inode + 1)))
        write_maintenance_inventory_v2(fixture.paths.maintenance_inventory_path, (stale,))
        self.assertFalse(fixture.paths.registry_path.exists(), "the enrolled v2 row has no v1 create registry")
        return fixture, stale

    def test_six_commands_have_terminal_recovery_advice(self) -> None:
        fixture, record = self._fixture()
        inventory_before = fixture.paths.maintenance_inventory_path.read_bytes()
        commands = {
            "update": ["update", "fixture", "--dry-run"],
            "inspect": ["inspect", "--target", str(fixture.target), "--channel", "stable"],
            "import_preview": ["import", "fixture", "--target", str(fixture.target), "--channel", "stable", "--dry-run"],
            "import_apply": ["import", "fixture", "--target", str(fixture.target), "--channel", "stable", "--yes", "--approval-fingerprint", "sha256:" + "f" * 64],
            "doctor": ["doctor", "fixture"],
            "reconcile": ["reconcile", "fixture", "--dry-run"],
        }

        def loader(channel: str, tracker: InspectionEffectTracker) -> Any:
            return fixture.request.descriptor_loader(channel, tracker).metadata

        with patch.object(enrollment, "_paired_installed_metadata", loader), patch.object(manager_cli, "load_installed_inspection_metadata", loader):
            results = {name: cli_resume(fixture, argv)[1] for name, argv in commands.items()}
            self.assertEqual(results["update"]["error_kind"], "managed_identity_drift")
            self.assertEqual(results["doctor"]["error_kind"], "target_identity_invalid")
            self.assertEqual(results["reconcile"]["error_kind"], "no_active_update")
            for name in ("import_preview", "import_apply"):
                self.assertEqual(results[name]["error_kind"], "registry_uniqueness_violation")
                self.assertEqual(results[name]["status"], "blocked")
                self.assertNotIn("approval_fingerprint", results[name]["data"])
            check = next(item for item in results["inspect"]["data"]["checks"] if item["check_id"] == "manager_inventory")
            self.assertEqual(check["status"], "verified")
            enrolled = check["observed"]["enrolled_instances"][0]
            self.assertEqual(enrolled["instance_id"], record.instance_id)
            self.assertEqual(enrolled["enrolled_path"], str(fixture.target))
            self.assertFalse(enrolled["target_identity_matches"])
            self.assertEqual(enrolled["identity_mismatch_reasons"], ["target_inode"])
            self.assertEqual(enrolled["next_step"], TARGET_RECOVERY)
            self.assertNotIn("`", TARGET_RECOVERY, "inspection ends the command cycle with an explicit manual recovery")
            for name, result in results.items():
                print("CHAIN", name, result["status"], result.get("error_kind"), result["message"], result.get("repair"))
                if name == "inspect":
                    continue
                self.assertIn(record.instance_id, result["message"])
                self.assertIn(TARGET_RECOVERY, result["repair"])
                advised = re.findall(r"`([^`]+)`", result["repair"])
                self.assertEqual(len(advised), 1)
                argv = shlex.split(advised[0])
                self.assertEqual(argv[:2], ["solet-manager", "inspect"])
                code, readback = cli_resume(fixture, argv[1:])
                self.assertEqual(code, 0, f"{name} advice must be executable: {readback}")
        self.assertEqual(fixture.paths.maintenance_inventory_path.read_bytes(), inventory_before)
        self.assertEqual(fixture.record(), record)

    def test_inventory_reports_both_stores_and_partial_read_failures(self) -> None:
        fixture, record = self._fixture()
        request = ExistingInstallInspectionRequest(fixture.target, "stable", fixture.paths)
        target = _inspection(request).target_identity
        write = fixture.paths.registry_path.write_bytes
        write(b'{"schema_version":1,"instances":{}}')
        check = _inventory_check(fixture.paths, _ProductionInspectionEffectTracker(), target)
        self.assertEqual(check.status, InspectionStatus.VERIFIED)
        write(b"not-json")
        check = _inventory_check(fixture.paths, _ProductionInspectionEffectTracker(), target)
        self.assertEqual(check.status, InspectionStatus.FAILED)
        self.assertIn(record.instance_id, json.dumps(check.observed))
        from solet_manager import inventory_inspection  # noqa: PLC0415

        original = inventory_inspection._read_regular_bytes

        def denied(path: Path) -> bytes:
            if path == fixture.paths.registry_path:
                raise PermissionError("fixture denied v1 read")
            return original(path)

        with patch.object(inventory_inspection, "_read_regular_bytes", denied):
            check = _inventory_check(fixture.paths, _ProductionInspectionEffectTracker(), target)
        self.assertEqual(check.status, InspectionStatus.UNKNOWN)
        self.assertIn(record.instance_id, json.dumps(check.observed))
        self.assertIn("fixture denied", json.dumps(check.observed))
        with patch.object(Path, "stat", side_effect=PermissionError("fixture target unreadable")):
            reasons = observe_identity_mismatches(record)
        self.assertEqual(reasons, ("target_unavailable",))
        self.assertIn("cannot be read", identity_drift_message(record, reasons))

    def test_matching_identity_device_renumber_and_active_inventory_are_preserved(self) -> None:
        fixture, record = self._fixture()
        actual = _inspection(ExistingInstallInspectionRequest(fixture.target, "stable", fixture.paths)).target_identity
        matched = replace(actual, target_inode=record.target.filesystem_identity.inode, target_device=actual.target_device + 4)
        check = _inventory_check(fixture.paths, _ProductionInspectionEffectTracker(), matched)
        self.assertIn('"target_identity_matches": true', json.dumps(check.observed))
        elsewhere = replace(matched, canonical_display=self.root / "different-target")
        check = _inventory_check(fixture.paths, _ProductionInspectionEffectTracker(), elsewhere)
        self.assertIn('"enrolled_path"', json.dumps(check.observed))
        self.assertIn('"target_identity_matches": false', json.dumps(check.observed))
        active = replace(record, active_operation=ActiveOperation(MaintenanceOperationKind.UPDATE, "opr_" + "6" * 32))
        write_maintenance_inventory_v2(fixture.paths.maintenance_inventory_path, (active,))
        before = fixture.paths.maintenance_inventory_path.read_bytes()
        with patch.object(enrollment, "inspect_existing_install", _with_inventory):
            code, payload = cli_resume(fixture, ["import", "fixture", "--target", str(fixture.target), "--channel", "stable", "--dry-run"])
        self.assertEqual(code, 3)
        self.assertEqual(payload["error_kind"], "registry_uniqueness_violation")
        self.assertEqual(fixture.paths.maintenance_inventory_path.read_bytes(), before)

    def test_import_readback_ignores_inventory_context_and_preserves_history(self) -> None:
        target = self.root / "target with spaces"
        target.mkdir()
        paths = ManagerPaths(self.root / "config", self.root / "state", self.root / "cache")
        request = ImportRequest("fixture", target, "stable", paths)
        with patch.object(enrollment, "inspect_existing_install", _with_inventory):
            first = enrollment.preview_import(request)
            enrollment.enroll_import(request, first.fingerprint)
            before = paths.maintenance_inventory_path.read_bytes()
            preview = enrollment.preview_import(request)
            self.assertEqual(enrollment.enroll_import(request, preview.fingerprint).status, "already_managed")
            self.assertEqual(paths.maintenance_inventory_path.read_bytes(), before)
            # Earlier bundles carried informational inventory. Keep their bytes and row
            # intact while comparing the same immutable enrollment premises.
            cached = dict(first.bundle)
            cached["checks"] = [check.to_dict() for check in _with_inventory(ExistingInstallInspectionRequest(target, "stable", paths)).checks]
            digest = canonical_sha256(cached)
            write_content_addressed_json(paths.contract_cache_path(digest), cached, digest)
            record = read_maintenance_inventory_v2(paths.maintenance_inventory_path)[0]
            write_maintenance_inventory_v2(paths.maintenance_inventory_path, (replace(record, inspection_bundle_digest=digest),))
            historical = paths.maintenance_inventory_path.read_bytes()
            preview = enrollment.preview_import(request)
            self.assertEqual(enrollment.enroll_import(request, preview.fingerprint).status, "already_managed")
            self.assertEqual(paths.maintenance_inventory_path.read_bytes(), historical)
            paths.contract_cache_path(digest).write_text("{}", encoding="utf-8")
            with self.assertRaises(StateError):
                enrollment.preview_import(request)


def main() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(EnrolledInventoryCollisionTests)
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
