#!/usr/bin/env python3
"""Tier U: the scheduled full battery runs in a detached export of its frozen commit.

The live checkout here is a real git repository and the real identity checks
(read_tree, verify_worktree, prepare_full_proofs, finish_full_proofs) run
unpatched, so a landing, or an uncommitted edit, in the live checkout during a
run is the exact condition the 30 to 90 minute campaign meets in production.
Only the runner is a stand-in: a stub quality_gates/run_smokes.py that records
where it ran and publishes a results file.
"""

# ruff: noqa: E402
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from inspect import signature
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT, ROOT / "ananta/src", ROOT / "plugins/platform_dev_surface_plugin/src", ROOT / "plugins/platform_dev_surface_plugin/tests"):
    sys.path.insert(0, str(source))
from platform_dev_surface_plugin.bounded_subprocess import SubprocessResult, run_bounded
from platform_dev_surface_plugin.quality import full_battery
from platform_dev_surface_plugin.quality.frozen_export import exports_root_for
from platform_dev_surface_plugin.quality.full_battery import FullBatteryProducer

from quality_gates import full_smoke_campaign
from quality_gates.full_smoke_campaign import CampaignStore
from quality_gates.git_environment import scrubbed_git_environment
from quality_gates.smoke_identity import FrozenTree

_HELPER = "suite/tests/_helper.py"
_IDENTITY = {"GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid", "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}

# Stand-in for quality_gates/run_smokes.py. It records where it ran (cwd, its
# own resolved path, the import root the real runner derives from its own
# location), what bytes it saw, and optionally rewrites a tracked file.
_STUB = '''\
"""Stand-in runner: records where it ran, then publishes a results file."""
import json
import os
import sys
from pathlib import Path

here = Path(__file__).resolve()
repo = here.parents[1]
output = Path(sys.argv[sys.argv.index("--results-output") + 1])
mode = os.environ.get("BATTERY_STUB_MODE", "pass")
probe = {
    "cwd": str(Path.cwd().resolve()),
    "script": str(here),
    "import_root": str(repo / "ananta" / "src"),
    "helper": (repo / "suite/tests/_helper.py").read_text(),
    "register": sys.argv[sys.argv.index("--register") + 1],
}
if mode == "tamper":
    target = Path.cwd() / "suite/tests/_helper.py"
    target.write_text(target.read_text() + "# tampered mid-run\\n")
    probe["tampered"] = str(target.resolve())
Path(str(output) + ".probe.json").write_text(json.dumps(probe))
if mode == "fail":
    sys.exit(3)
smokes = ["suite/tests/one_smoke.py"] + (["suite/tests/never_smoke.py"] if mode == "never" else [])
output.write_text(
    json.dumps(
        {
            "result": "passed",
            "exit_code": 0,
            "harness_defects": [],
            "summary": {"total": len(smokes), "passed": len(smokes), "failed": 0, "skipped": 0},
            "smoke_results": [{"smoke": smoke, "verdict": "passed", "attempts": [{"output": "1 passed", "verdict": "passed"}]} for smoke in smokes],
        }
    )
)
'''


def _snapshot(root: Path) -> dict[str, bytes]:
    """Every file of the live checkout outside .git and the .venv link."""
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file() and not {".git", ".venv"} & set(path.relative_to(root).parts)}


class LiveCheckout:
    """A real git repository standing in for the shared checkout."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir()
        self._git("init", "--quiet", "-b", "master")
        files = {
            ".gitignore": ".venv\n",
            "quality_gates/gate_smokes.txt": "suite/tests/one_smoke.py\n",
            "quality_gates/run_smokes.py": _STUB,
            "quality_gates/lane_source_roots.json": (ROOT / "quality_gates/lane_source_roots.json").read_text(encoding="utf-8"),
            "ananta/src/placeholder.py": "",
            "suite/tests/one_smoke.py": "from _helper import VALUE\n",
            _HELPER: "VALUE = 1\n",
        }
        for relative, body in files.items():
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text(body)
        (root / ".venv/bin").mkdir(parents=True)
        (root / ".venv/bin/python3").symlink_to(sys.executable)
        self.first = self.commit("first")

    def _git(self, *arguments: str) -> str:
        done = subprocess.run(("git", "-C", str(self.root), *arguments), env=scrubbed_git_environment(_IDENTITY), capture_output=True, text=True, check=False)
        if done.returncode:
            raise RuntimeError(f"git {arguments} failed: {done.stderr}")
        return done.stdout.strip()

    def commit(self, message: str) -> str:
        self._git("add", "-A")
        self._git("commit", "--quiet", "-m", message)
        return self._git("rev-parse", "HEAD")

    def edit_helper(self, body: str) -> None:
        (self.root / _HELPER).write_text(body)


class DetachedRunTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        self.base = base
        environment = patch.dict(os.environ, {"RUN_SMOKES_BATTERY_RECEIPT_DIR": str(base / "receipts"), "SOLET_NAME": "fixture-solet"})
        environment.start()
        self.addCleanup(environment.stop)
        self.live = LiveCheckout(base / "live")
        self.store = CampaignStore(base / "campaigns")
        self.store.root.mkdir(parents=True)
        self.exports = base / "exports"
        self.producer = FullBatteryProducer(self.live.root, "fixture-solet", store=self.store, exports_root=self.exports)
        self.campaign: dict[str, Any] = {"campaign_id": "a" * 64, "request": {"commit": self.live.first}, "attempts": [{"number": 1}], "attribution": {}}

    def _run(self, mode: str = "pass") -> dict[str, Any]:
        with patch.dict(os.environ, {"BATTERY_STUB_MODE": mode}):
            return json.loads(json.dumps(self.producer._run_battery(self.campaign, self.store.root)))

    def _probe(self) -> dict[str, Any]:
        return json.loads(Path(f"{self.store.root}/{'a' * 64}.1.results.json.probe.json").read_text())

    def _leftover(self) -> list[str]:
        return sorted(path.name for path in self.exports.iterdir()) if self.exports.is_dir() else []

    def _queue_due_request(self, producer: FullBatteryProducer) -> None:
        producer._enqueue(producer._request())

    def _inside_exports(self, value: str) -> bool:
        return Path(value).resolve().is_relative_to(self.exports.resolve())

    def _moving(self, action: Callable[[], None]) -> Callable[..., SubprocessResult]:
        """The real run_bounded, after something happens to the live checkout."""

        def run(argv: list[str], **kwargs: Any) -> SubprocessResult:
            action()
            return run_bounded(argv, **kwargs)

        return run

    def test_a_commit_that_lands_during_the_run_does_not_interrupt_it(self) -> None:
        seen: list[dict[str, bytes]] = []

        def land() -> None:
            self.live.edit_helper("VALUE = 2\n")
            self.live.commit("second")
            seen.append(_snapshot(self.live.root))

        with patch("platform_dev_surface_plugin.quality.full_battery.run_bounded", side_effect=self._moving(land)):
            result = self._run()
        self.assertEqual((result["result"], result["runner_exit_code"]), ("passed", 0))
        self.assertEqual(self._probe()["helper"], "VALUE = 1\n", "the run executed the frozen commit, not the landed one")
        self.assertEqual(_snapshot(self.live.root), seen[0], "the live checkout is byte-identical after the run")

    def test_b_an_uncommitted_edit_in_the_live_checkout_does_not_interrupt_it(self) -> None:
        self.live.edit_helper("VALUE = 99  # uncommitted\n")
        before = _snapshot(self.live.root)
        result = self._run()
        self.assertEqual((result["result"], result["runner_exit_code"]), ("passed", 0))
        self.assertEqual(self._probe()["helper"], "VALUE = 1\n")
        self.assertEqual(_snapshot(self.live.root), before)

    def test_c_the_export_is_gone_after_every_way_a_run_can_end(self) -> None:
        self.assertEqual(self._run()["result"], "passed")
        self.assertTrue(self._inside_exports(self._probe()["cwd"]), "the run executed in an export")
        self.assertEqual(self._leftover(), [], "after a successful run")
        self._run("fail")
        self.assertTrue(self._inside_exports(self._probe()["cwd"]), "the failing run executed in an export")
        self.assertEqual(self._leftover(), [], "after a run whose runner exits non-zero")
        during: list[list[str]] = []

        def explode(_argv: list[str], **_kwargs: Any) -> SubprocessResult:
            during.append(self._leftover())
            raise OSError("runner could not start")

        with patch("platform_dev_surface_plugin.quality.full_battery.run_bounded", side_effect=explode), self.assertRaises(OSError):
            self._run()
        self.assertEqual(len(during[0]), 1, "run_bounded was called while exactly one export existed")
        self.assertEqual(self._leftover(), [], "after a run in which run_bounded raises")
        self.assertTrue((self.live.root / ".venv/bin/python3").is_symlink(), "removing an export never reaches the live .venv")

    def test_d_the_next_run_sweeps_an_export_a_dead_predecessor_left(self) -> None:
        (self.exports / "dead-predecessor/suite").mkdir(parents=True)
        (self.exports / "dead-predecessor/suite/leftover.txt").write_text("left by a killed worker\n")
        (self.exports / "dead-predecessor/.venv").symlink_to(self.live.root / ".venv")
        observed: list[list[str]] = []

        def runner(_campaign: dict[str, Any], _directory: Path) -> dict[str, Any]:
            observed.append(self._leftover())
            return {"result": "failed", "exit_code": 1}

        producer = FullBatteryProducer(self.live.root, "fixture-solet", store=self.store, runner=runner, exports_root=self.exports)
        self._queue_due_request(producer)
        producer.run_due()
        self.assertEqual(observed, [[]], "the stale export was removed before the runner started")
        self.assertTrue((self.live.root / ".venv/bin/python3").exists(), "the sweep removed the link, not what it points to")

    def test_e_the_runner_executes_inside_the_export_and_never_inside_the_live_root(self) -> None:
        self._run()
        probe = self._probe()
        live = self.live.root.resolve()
        for key in ("cwd", "script", "import_root", "register"):
            self.assertTrue(self._inside_exports(probe[key]), f"{key} is inside exports/: {probe[key]}")
            self.assertFalse(Path(probe[key]).resolve().is_relative_to(live), f"{key} is not inside the live root: {probe[key]}")

    def test_f_a_runner_that_rewrites_a_tracked_file_is_still_caught_and_recorded_interrupted(self) -> None:
        before = _snapshot(self.live.root)
        producer = FullBatteryProducer(self.live.root, "fixture-solet", store=self.store, exports_root=self.exports)
        self._queue_due_request(producer)
        with patch.dict(os.environ, {"BATTERY_STUB_MODE": "tamper"}):
            closed = producer.run_due()[0]
        self.assertEqual(closed["state"], "closed")
        self.assertEqual(closed["result"]["result"], "interrupted")
        self.assertIn("executing input differs from frozen candidate", closed["result"]["advisory"])
        probe = json.loads(next(self.store.root.glob("*.probe.json")).read_text())
        self.assertTrue(self._inside_exports(probe["tampered"]), "the rewrite happened inside the export")
        self.assertEqual(_snapshot(self.live.root), before, "the live checkout was not touched")
        self.assertEqual(self._leftover(), [])

    def test_g_a_never_reused_smoke_in_the_register_does_not_interrupt_the_campaign(self) -> None:
        """A never-reuse smoke has no proof; the campaign result lists the proofs of the others and still closes."""
        (self.live.root / "suite/tests/never_smoke.py").write_text("from _helper import VALUE\n")
        (self.live.root / "quality_gates/gate_smokes.txt").write_text("suite/tests/one_smoke.py\nsuite/tests/never_smoke.py\n")
        (self.live.root / "quality_gates/smoke_never_reuse.txt").write_text("suite/tests/never_smoke.py  # timing: its verdict depends on the clock\n")
        self.campaign["request"]["commit"] = self.live.commit("a never-reused smoke")
        result = self._run("never")
        self.assertEqual((result["result"], result["registered_count"]), ("passed", 2))
        self.assertEqual([row["smoke"] for row in result["proofs"]], ["suite/tests/one_smoke.py"], "the never-reused smoke has no proof to list")
        self.assertEqual(self._leftover(), [])

    def test_h_each_store_gets_its_own_export_folder_under_the_system_temporary_directory(self) -> None:
        """The default folder is outside every host profile directory and one per store, so no sweep reaches another store's export."""
        system = self.base.resolve() / "system-temp"
        home = self.base.resolve() / "home"
        with patch.object(tempfile, "tempdir", str(system)):
            first = exports_root_for(self.store.root)
            second = exports_root_for(self.base / "other-store")
            self.assertEqual(first.parent, system / "full-battery-exports")
            self.assertEqual((len(first.name), set(first.name) <= set("0123456789abcdef")), (16, True), "sixteen hex characters name the store")
            self.assertEqual(exports_root_for(self.store.root), first, "one store root, one folder")
            self.assertNotEqual(second, first, "two store roots, two folders")
            self.assertFalse(first.exists(), "naming the folder creates nothing")
            # A store inside a profile directory: its export folder follows the system temporary directory, not the store.
            for name in (".ananta", ".codex", ".claude"):
                inside = exports_root_for(home / name / "runtime" / "store")
                self.assertFalse(inside.is_relative_to(home / name), f"{inside} is inside {name}, which the census smokes protect")

    def test_i_a_sweep_removes_only_its_own_stores_leftovers(self) -> None:
        """Two stores share the system temporary directory; one store's sweep leaves the other store's export alone."""
        with patch.object(tempfile, "tempdir", str(self.base)):
            stores = [CampaignStore(self.base / name) for name in ("campaigns-a", "campaigns-b")]
            for store in stores:
                store.root.mkdir(parents=True)
            producers = [FullBatteryProducer(self.live.root, "fixture-solet", store=store) for store in stores]
            dead = [producer.exports_root / "dead-predecessor" for producer in producers]
            for leftover in dead:
                leftover.mkdir(parents=True)
            self.assertNotEqual(producers[0].exports_root, producers[1].exports_root)
            producers[0].run_due()
        self.assertEqual([leftover.exists() for leftover in dead], [False, True], "the first store's sweep removed its own leftover and left the second store's")


PASSED = {"result": "passed", "exit_code": 0, "registered_count": 1, "summary": {"total": 1, "passed": 1}}
FAILED = {**PASSED, "result": "failed", "exit_code": 1, "summary": {"total": 1, "failed": 1}}
INTERRUPTED = {"result": "interrupted", "exit_code": None}


class RequestPerCommitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = CampaignStore(Path(self.enterContext(tempfile.TemporaryDirectory())))
        self.tip, self.outcome, self.runs = "X", PASSED, []
        self.producer = FullBatteryProducer(ROOT, "fixture-solet", store=self.store, runner=self.run_one)
        self.enterContext(patch.object(full_battery, "read_tree", side_effect=lambda *_: FrozenTree(self.tip, f"{self.tip}-tree", {})))
        self.enterContext(patch.object(full_battery.threading, "Thread", side_effect=lambda **_: SimpleNamespace(start=self.producer._background)))

    def get(self, campaign_id: str) -> dict[str, Any]:
        found = self.store.get(campaign_id)
        assert found is not None
        return found

    def run_one(self, campaign: dict[str, Any], _root: Path) -> dict[str, Any]:
        self.runs.append(campaign["request"]["commit"])
        return self.outcome

    def campaigns_of(self, commit: str) -> list[dict[str, Any]]:
        return [row for row in self.store.all() if row["request"]["commit"] == commit]

    def test_a_one_commit_is_one_campaign_and_one_run(self) -> None:
        first, second = self.producer.trigger("deploy"), self.producer.trigger("deploy")
        self.assertEqual((first["status"], second["status"], second["campaign_id"], second["outcome"]), ("started", "have_result", first["campaign_id"], "passed"))
        self.tip, self.outcome = "Y", FAILED
        self.producer.trigger("deploy")
        self.assertEqual([self.producer.trigger("deploy")["outcome"] for _ in range(2)], ["failed", "failed"])
        self.assertEqual((self.runs, len(self.store.all())), (["X", "Y"], 2))

    def test_b_an_interrupted_campaign_is_requeued_by_the_next_request(self) -> None:
        self.outcome = INTERRUPTED
        first = self.producer.trigger("deploy")
        self.outcome = PASSED
        second = self.producer.trigger("deploy")
        final = self.get(first["campaign_id"])
        self.assertEqual((second["campaign_id"], [row["number"] for row in final["attempts"]], final["attempts"][1]["previous_attempt"], final["result"]["result"], self.runs), (first["campaign_id"], [1, 2], 1, "passed", ["X", "X"]))

    def test_c_a_request_supersedes_the_queued_campaign_of_another_commit(self) -> None:
        queued = self.store.enqueue(self.producer._request())
        self.tip = "Y"
        reply = self.producer.trigger("deploy")
        retained = self.get(queued["campaign_id"])
        self.assertEqual((retained["state"], retained["result"]["successor_campaign"], self.runs), ("closed", reply["campaign_id"], ["Y"]))

    def test_d_the_worker_gives_the_new_tip_one_campaign_and_never_reruns_a_result(self) -> None:
        queued = self.store.enqueue(self.producer._request())
        self.tip = "Y"
        self.producer.run_due()
        (made,) = self.campaigns_of("Y")
        asked = self.producer.trigger("deploy")
        successor = self.get(queued["campaign_id"])["result"]["successor_campaign"]
        self.assertEqual((asked["campaign_id"], successor, "supersedes_campaign" in made["request"]), (made["campaign_id"], made["campaign_id"], False))
        self.store.enqueue({"commit": "W", "tree": "W-tree", "manifest_sha256": "W"})
        self.producer.run_due()
        self.assertEqual((self.runs, len(self.campaigns_of("Y"))), (["Y"], 1), "master moved to a commit that has a result: the runner is not called")

    def test_e_the_worker_runs_a_request_that_arrives_during_a_run_without_another_trigger(self) -> None:
        during: list[str] = []

        def runner(campaign: dict[str, Any], root: Path) -> dict[str, Any]:
            if self.tip == "X":
                self.tip = "Y"
                during.append(self.producer.trigger("deploy")["status"])
            return self.run_one(campaign, root)

        self.producer.runner = runner
        self.producer.trigger("deploy")
        self.assertEqual((during, self.runs, [row["state"] for row in self.store.all()], self.producer._slot.locked()), (["already_running"], ["X", "Y"], ["closed", "closed"], False))

    def test_f_a_request_that_lands_between_the_last_run_and_the_slot_release_still_runs(self) -> None:
        real, landed = self.producer.run_due, []

        def run_due() -> list[dict[str, Any]]:
            ran = real()
            if not landed:
                self.tip = "Y"
                landed.append(self.producer._enqueue(self.producer._request()))
            return ran

        with patch.object(self.producer, "run_due", side_effect=run_due):
            started = self.producer.trigger("deploy")
        landed_state = self.get(landed[0]["campaign_id"])["state"]
        self.assertEqual((started["status"], self.get(started["campaign_id"])["state"], self.runs, landed_state, self.producer._slot.locked()), ("started", "closed", ["X", "Y"], "closed", False))

    def test_g_the_yield_is_gone(self) -> None:
        self.assertFalse(any(hasattr(module, "battery_busy") for module in (full_battery, full_smoke_campaign)))

    def test_h_the_request_is_the_commit_and_the_triggers_are_closed(self) -> None:
        request = self.producer._request()
        self.assertEqual((len(signature(self.producer._request).parameters), set(request)), (0, {"commit", "tree", "manifest_sha256"}))
        with self.assertRaises(ValueError):
            self.producer.trigger("bogus")
        self.assertEqual(self.producer.trigger("deploy")["status"], "started")
        self.assertFalse({"next_due_at", "queue_evidence"} & set(self.store.all()[0]))

    def test_i_the_campaign_row_reports_the_commit_and_the_tree(self) -> None:
        closed = {"campaign_id": "c", "request": self.producer._request(), "finished_at": "2026-01-01T00:00:00+00:00", "result": PASSED}
        self.assertEqual(full_battery._campaign_row(closed, None)["request"], {"commit": "X", "tree": "X-tree"})

    def test_j_the_two_live_triggers_keep_working(self) -> None:
        # A deploy asks for the first commit; a manual request asks for the new tip.
        self.assertEqual((self.producer.trigger("deploy")["status"], self.runs), ("started", ["X"]))
        self.tip = "Z"
        manual = self.producer.trigger("manual")
        self.assertEqual((manual["status"], self.runs, [row["state"] for row in self.campaigns_of("Z")]), ("started", ["X", "Z"], ["closed"]))


if __name__ == "__main__":
    unittest.main()
