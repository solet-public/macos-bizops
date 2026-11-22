"""Fixture machinery for the deploy-by-rule smoke proof."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "ananta" / "src"))
sys.path.insert(0, str(_ROOT / "plugins" / "agent_messaging_plugin" / "tests"))
sys.path.insert(0, str(_ROOT / "plugins" / "macos_self_deployment_plugin" / "src"))

from _real_state_fake import _STANDARDIZER_COLUMNS, RealShapeState  # noqa: E402
from ananta.services.lifecycle_management_service.deployment_record_schema import (  # noqa: E402
    TABLE_DEPLOYMENT_RECORD,
    TABLE_DEPLOYMENT_RECORD_EVENT,
    get_deployment_record_schema,
)
from ananta.services.lifecycle_management_service.deployment_record_verbs import (  # noqa: E402
    DeploymentRecordVerbs,
)

OLD, TARGET = "a" * 40, "b" * 40
NOTICE = "plugin::agent_messaging_plugin::peer_send_by_name"
RECORD = "service_interface::lifecycle_management_service::record_deployment"
READ = "service_interface::lifecycle_management_service::read_deployments"
FULL_BATTERY = "service_interface::quality_service::trigger_full_smoke_battery"
OLD_RELEASE, RELEASE = "rel-old", "rel-new"
CLOCK = "2026-10-15T00:00:00+00:00"
BASELINE_AT = "2026-10-14T00:00:00+00:00"  # before CLOCK, so the record a run writes is the newest
SOURCE = Path(__file__).resolve().parents[2] / "src/ananta/services/lifecycle_management_service/_deploy_by_rule.py"
MARKER = "deploy-by-rule.schema-route.json"
SCHEMA_REFUSAL = "schema preflight refused deploy — durable rollback only protects code over an additive schema. schema diff has 16 non-additive change(s) (rollback-UNSAFE) (fixture)"


def load() -> ModuleType:
    assert SOURCE.is_file(), "DEPLOY_BY_RULE_AVAILABLE"
    spec = importlib.util.spec_from_file_location("deploy_by_rule_under_test", SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def envelope(data: dict[str, Any]) -> str:
    return json.dumps({"status": "completed", "result": {"action_status": "completed", "data": data}})


class _Orchestrator:
    """The one thing the record verbs ask of the service: its state service."""

    def __init__(self, state: object) -> None:
        self._state = state

    def get_service(self, name: str) -> object | None:
        return self._state if name == "state_service" else None


class RecordStore(DeploymentRecordVerbs):
    """The shipped record and read verbs over an in-memory, schema-enforced store.

    The store is seeded on first use from the scenario state, because a test
    sets its baseline after constructing the fixture. A record the scenario
    marks as already written is in the store from the start, unless the first
    read is to be stale.
    """

    def __init__(self, scenario: SimpleNamespace, solet_name: str) -> None:
        self._scenario = scenario
        self._solet_name = solet_name
        self._seeded = False
        state = RealShapeState()
        schema = get_deployment_record_schema()
        for table in (TABLE_DEPLOYMENT_RECORD, TABLE_DEPLOYMENT_RECORD_EVENT):
            state._enforced_columns[table] = frozenset(schema.tables[table].columns) | _STANDARDIZER_COLUMNS  # noqa: SLF001 — fixture wiring
        self._orchestrator = _Orchestrator(state)

    def _seed(self) -> None:
        if self._seeded:
            return
        self._seeded = True
        scenario = self._scenario
        if scenario.baseline:
            self.put(scenario.baseline, OLD_RELEASE, BASELINE_AT)
        if scenario.recorded and not scenario.stale_first:
            self.put(scenario.target, RELEASE, CLOCK)

    def put(self, git_sha: str, release_id: str, deployed_at: str) -> None:
        """Write one record through the shipped record verb, as the seed of a scenario."""
        reply = self.record_deployment(solet_name=self._solet_name, release_id=release_id, git_sha=git_sha, deployed_at=deployed_at, trigger="tick", manifest_sha256="0" * 64, agent_instance_id="agi-seed", actor_model="seed", actor_effort="none")
        assert reply["action_status"] == "completed", f"FIXTURE_SEED_REFUSED: {reply}"

    def stored(self) -> list[dict[str, Any]]:
        """Every stored record, newest first, exactly as the history verb returns it."""
        self._seed()
        reply = self.read_deployments(solet_name=self._solet_name, mode="history", limit=100)
        return reply["data"]["rows"]

    def row_id(self, git_sha: str) -> str:
        """The id the store minted for the one record of this commit."""
        (row,) = [r for r in self.stored() if r["git_sha"] == git_sha]
        return row["deployment_id"]

    def serve_read(self, data: dict[str, Any]) -> dict[str, Any]:
        """Answer one read. A record written before the run but not yet visible appears right after the first read."""
        self._seed()
        scenario = self._scenario
        scenario.current_reads += 1
        reply = self.read_deployments(**data)
        if scenario.recorded and scenario.stale_first and scenario.current_reads == 1:
            self.put(scenario.target, RELEASE, CLOCK)
        return reply

    def serve_record(self, data: dict[str, Any]) -> dict[str, Any]:
        """Answer one record call; the scenario's recorded flag says a write reached the store."""
        self._seed()
        self._scenario.recorded = True
        return self.record_deployment(**data)


class Fixture:
    """All file, Git, process, clock, PID and lock observations are synthetic."""

    def __init__(self) -> None:
        self.m = load()
        self.args = argparse.Namespace(trigger="landing", solet_name="fixture-solet", repo=Path("/fixture/repo"), app_home=Path("/fixture/home"), releases_root=Path("/fixture/releases"), bridge="/fixture/bridge", agent_instance_id="agi-fixture", actor_model="fixture-model", actor_effort="medium", deadline=20)
        self.log: list[Any] = []
        self.s = SimpleNamespace(
            delta="M\0ananta/src/code.py\0",
            serving=OLD,
            baseline=OLD,
            target=TARGET,
            committed=False,
            recorded=False,
            occupied=False,
            reject=False,
            ambiguous=False,
            stuck=False,
            advance=False,
            bad_manifest=False,
            holder=True,
            receipt={"result": "passed", "pid": 42},
            receipt_live=False,
            receipt_lock_held=False,
            ticks=0,
            polls=0,
            current_reads=0,
            stale_first=False,
            apply_timeout=False,
            plugin_timeouts=0,
            plugin_not_ready=0,
            plugins_never_ready=False,
            plugin_reads=0,
            proof_change={},
            version_change={},
            notice_reply=envelope({"message_id": "arm-fixture", "delivery": "queued_watcher"}),
            notice_timeout=False,
            async_keys=(self.m.SWAP, self.m.APPLY),
            job_failure=None,
            job_timeouts=0,
            job_timeout_during_drain=False,
            full_battery_timeout=False,
            full_battery_reject=False,
            marker=None,
            marker_write_fails=False,
        )
        self.jobs: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.attested_manifest_sha256 = ""
        self.store = RecordStore(self.s, self.args.solet_name)
        self.s.flat_swap = False
        self.s.drain_consumes_deadline = False
        self.ports = self.m.Ports(self.command, self.read, self.resolve, self.receipts, lambda target: self.s.receipt_live if isinstance(target, int) else self.s.receipt_lock_held, self.lock, lambda: self.s.ticks, self.sleep, lambda: CLOCK, write=self.write)

    def resolve(self, path: Path) -> Path:
        return Path(str(path).replace("/current", f"/{RELEASE}"))

    def read(self, path: Path) -> bytes:
        if path.name == MARKER:
            if self.s.marker is None:
                raise FileNotFoundError("fixture marker absent")
            return self.s.marker
        if path.name == "manifest.yaml":
            return b"profile_name: fixture-solet\nplugins: []\n" if not self.s.bad_manifest else b"plugins: [changed]\n"
        if path.name == "receipt.json":
            return self.s.receipt if isinstance(self.s.receipt, bytes) else json.dumps(self.s.receipt).encode()
        if path.name.endswith(".lock"):
            return b""
        proof = {"head": self.s.serving, "state": "equal", "mismatches": [], **self.s.proof_change}
        raw = json.dumps({"artifact_to_head_equality": proof}).encode()
        if path.name == "VERSION":
            return json.dumps({"release_id": RELEASE, "git_sha": self.s.serving[:9], "selected_file_manifest": {"filename": "SELECTED_FILES.json", "sha256": sha256(raw).hexdigest()}, **self.s.version_change}).encode()
        assert path.name == "SELECTED_FILES.json", path
        self.attested_manifest_sha256 = sha256(raw).hexdigest()
        self.log.append("REACHED_VERIFY")
        print("REACHED_VERIFY", flush=True)
        return raw

    def write(self, path: Path, data: bytes) -> None:
        """The one file a run may write: the schema-route marker, under the runtime folder."""
        assert path == self.args.app_home / "data/runtime" / MARKER, f"UNEXPECTED_WRITE: {path}"
        self.log.append("REACHED_MARKER_WRITE")
        if self.s.marker_write_fails:
            raise OSError("fixture marker write failed")
        self.s.marker = data

    def refuse_schema(self) -> None:
        """The committing APPLY answers as the live Manager does when the cutover refuses the release's schema change.

        The refusal code is the cutover's own token, so a run only defers when the
        module under test speaks the same vocabulary as the orchestrator.
        """
        from macos_self_deployment_plugin.constants import RestartReasonCode  # noqa: PLC0415 — only this scenario needs the plugin

        orig = self.apply

        def apply(key: str, data: dict[str, Any]) -> str:
            if data["dry_run"]:
                return orig(key, data)
            return envelope(
                {
                    "status": "restart_failed_after_manifest_commit",
                    "current_etag": "fresh",
                    "restart_status": "failed",
                    "restart_reason_code": RestartReasonCode.SCHEMA_PREFLIGHT_REFUSED.value,
                    "rejection_reasons": [f"restart_failed_after_manifest_commit: deployment plugin returned status='failed': {SCHEMA_REFUSAL}", "manifest_rolled_back: restored the CAS-time pre-write snapshot"],
                    "message": "Deployment restart failed after the manifest write.",
                }
            )

        self.apply = apply

    def receipts(self) -> list[Path]:
        if self.s.receipt is None:
            raise FileNotFoundError("fixture receipts absent")
        return [Path("/fixture/receipt.json")]

    @contextmanager
    def lock(self, path: Path) -> Any:
        yield not self.s.occupied

    def sleep(self, seconds: float) -> None:
        self.s.ticks += seconds

    def status(self) -> dict[str, Any]:
        self.s.polls += int(self.s.committed)
        new = self.s.committed and not self.s.stuck
        color, instance = ("green", "new") if new else ("blue", "old")
        drains = [{"color": "blue"}] if new and self.s.polls == 1 else []
        if new and self.s.drain_consumes_deadline:
            drains = []
            self.s.ticks = self.args.deadline
        if new and not drains:
            self.s.serving = self.s.target
        return {"swap_in_progress": False, "router_status": {"active_color": color, "active_instance_id": instance, "colors": [{"color": color, "instance_id": instance, "status": "active"}], "drain_entries": drains}}

    def command(self, argv: list[str]) -> str:
        self.log.append(argv)
        if argv[0] == "git":
            return self.git(argv[3:])
        if argv[1] == "holds-role":
            if self.s.holder is None:
                raise OSError("fixture holder unavailable")
            return json.dumps({"result": {"success": True, "data": {"holds": self.s.holder}}})
        assert argv[0] == self.args.bridge, f"UNEXPECTED_COMMAND: only git and the bridge may run, never an external register executable: {argv}"
        return self.bridge_call(argv)

    def bridge_call(self, argv: list[str]) -> str:
        """Mirror call's queued admission and --wait's get_job record output.

        Admission alone must not execute the operation. A waited success keeps
        the original ActionResult under result, as both owner workers persist
        it. CLI failures carry the finished record on stdout; a wait timeout
        retains a processing job and reports its identity on stderr.
        """
        key, data = argv[2], json.loads(argv[3])
        if key not in self.s.async_keys:
            return self.process(key, data)
        job: dict[str, Any] = {"id": f"job-fixture-{len(self.jobs)}", "provider_name": key.split("::")[1], "status": "queued", "result": None, "error": None}
        self.jobs.append(job)
        if "--wait" not in argv:
            return envelope({"job_id": job["id"], "status": "queued"})
        if self.s.job_timeouts and (not self.s.job_timeout_during_drain or self.s.committed):
            self.s.job_timeouts -= 1
            job["status"] = "processing"
            raise subprocess.CalledProcessError(7, argv, stderr=f"job {job['id']} still processing; the job is still running")
        if key == self.s.job_failure:
            job.update(status="error", error={"message": "fixture job failed"})
            raise subprocess.CalledProcessError(6, argv, output=json.dumps(job), stderr="fixture job failed")
        job.update(status="completed", result=json.loads(self.process(key, data))["result"])
        return json.dumps(job)

    def _read_deployments(self, data: dict[str, Any]) -> str:
        """Serve the read verb from the store, and mark a read that shows the new record."""
        reply = self.store.serve_read(data)
        if any(row["git_sha"] == self.s.target and row["release_id"] == RELEASE for row in reply.get("data", {}).get("rows", [])):
            self.log.append("REACHED_VERIFIED_RECORD")
            print("REACHED_VERIFIED_RECORD", flush=True)
        return json.dumps({"status": "completed", "result": reply})

    def _record(self, key: str, data: dict[str, Any]) -> str:
        """Serve the record verb; an ambiguous write lands in the store but its reply is lost."""
        self.log.append("REACHED_RECORD")
        print("REACHED_RECORD", flush=True)
        reply = self.store.serve_record(data)
        if self.s.ambiguous:
            raise subprocess.TimeoutExpired([self.args.bridge, key], 30)
        return json.dumps({"status": "completed", "result": reply})

    def _full_battery(self, key: str, data: dict[str, Any]) -> str:
        """Serve the full-battery request: it starts a campaign, times out, or is refused."""
        self.log.append("REACHED_FULL_BATTERY_REQUEST")
        print("REACHED_FULL_BATTERY_REQUEST", flush=True)
        self.requests.append(data)
        if self.s.full_battery_timeout:
            raise subprocess.TimeoutExpired([self.args.bridge, key], 30)
        if self.s.full_battery_reject:
            return json.dumps({"status": "completed", "result": {"action_status": "failed", "success": False, "error": "fixture refused the request"}})
        return envelope({"status": "started", "campaign_id": "c" * 64})

    def git(self, argv: list[str]) -> str:
        if argv[0] == "diff":
            assert argv[4:6] == [self.s.baseline or self.s.serving, self.s.target]
            return self.s.delta
        if argv[0] == "ls-tree":
            return "ananta/src/code.py\0"
        name = argv[-1]
        if name == "master^{commit}":
            count = sum(isinstance(a, list) and a[-1] == name for a in self.log)
            return "c" * 40 if self.s.advance and count > 1 else self.s.target
        return {OLD[:9] + "^{commit}": OLD, TARGET[:9] + "^{commit}": TARGET}[name]

    def process(self, key: str, data: dict[str, Any]) -> str:
        if key == NOTICE:
            self.log.append("REACHED_NOTICE")
            print("REACHED_NOTICE", flush=True)
            if self.s.notice_timeout:
                raise subprocess.TimeoutExpired([self.args.bridge, key], 30)
            return self.s.notice_reply
        if key == FULL_BATTERY:
            return self._full_battery(key, data)
        if key == self.m.SWAP:
            if self.s.flat_swap:
                return json.dumps({"status": "completed", "result": {"action_status": "completed", "success": True, **self.status()}})
            return envelope(self.status())
        if key == self.m.PLUGINS:
            return self.plugins(key)
        if key == READ:
            return self._read_deployments(data)
        if key == RECORD:
            return self._record(key, data)
        return self.apply(key, data)

    def apply(self, key: str, data: dict[str, Any]) -> str:
        assert key == self.m.APPLY
        assert data["new_manifest"] == {"profile_name": "fixture-solet", "plugins": []}
        assert self.s.target in data["reason"]
        if data["dry_run"]:
            return envelope({"status": "dry_run", "current_etag": "fresh"})
        assert data["expected_etag"] == "fresh"
        self.log.append("REACHED_APPLY_COMMIT")
        print("REACHED_APPLY_COMMIT", flush=True)
        self.s.committed = True
        if self.s.apply_timeout:
            raise subprocess.TimeoutExpired([self.args.bridge, key], 30)
        return envelope({"status": "rejected" if self.s.reject else "applied", "current_etag": "fresh"})

    def plugins(self, key: str) -> str:
        self.s.plugin_reads += 1
        if self.s.plugin_timeouts:
            self.s.plugin_timeouts -= 1
            raise subprocess.TimeoutExpired([self.args.bridge, key], 30)
        if self.s.plugin_not_ready or self.s.plugins_never_ready:
            self.s.plugin_not_ready = max(0, self.s.plugin_not_ready - 1)
            return envelope({"plugins": [{"lifecycle_managed": True, "status": "starting", "is_running": False}]})
        return envelope({"plugins": [{"lifecycle_managed": True, "status": "ready", "is_running": True}]})

    def run(self) -> dict[str, Any]:
        return self.m.deploy(self.ports, self.args)


def writes(f: Fixture) -> list[Any]:
    return [x for x in f.log if x in ("REACHED_APPLY_COMMIT", "REACHED_RECORD")]


def calls(f: Fixture, key: str) -> int:
    """How many times the run called this bridge verb, whatever its arguments."""
    return sum(isinstance(x, list) and x[0] == f.args.bridge and x[1] == "call" and x[2] == key for x in f.log)


def notices(f: Fixture) -> list[dict[str, Any]]:
    """The content of every Main notice the run sent."""
    return [json.loads(json.loads(x[3])["content"]) for x in f.log if isinstance(x, list) and x[0] == f.args.bridge and x[1] == "call" and x[2] == NOTICE]
