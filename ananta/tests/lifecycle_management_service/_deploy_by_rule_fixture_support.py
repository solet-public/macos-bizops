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

OLD, TARGET = "a" * 40, "b" * 40
NOTICE = "plugin::agent_messaging_plugin::peer_send_by_name"
SOURCE = Path(__file__).resolve().parents[2] / "src/ananta/services/lifecycle_management_service/_deploy_by_rule.py"


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


class Fixture:
    """All file, Git, process, clock, PID and lock observations are synthetic."""

    def __init__(self) -> None:
        self.m = load()
        self.args = argparse.Namespace(
            trigger="landing", solet_name="fixture-solet", repo=Path("/fixture/repo"), app_home=Path("/fixture/home"), releases_root=Path("/fixture/releases"), bridge="/fixture/bridge", psolet="/fixture/psolet", agent_instance_id="agi-fixture", actor_model="fixture-model", actor_effort="medium", deadline=20
        )
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
            measurement_id="cme-fixture",
            notice_reply=envelope({"message_id": "arm-fixture", "delivery": "queued_watcher"}),
            notice_timeout=False,
        )
        self.s.flat_swap = False
        self.s.drain_consumes_deadline = False
        self.ports = self.m.Ports(self.command, self.read, self.resolve, self.receipts, lambda target: self.s.receipt_live if isinstance(target, int) else self.s.receipt_lock_held, self.lock, lambda: self.s.ticks, self.sleep, lambda: "2026-10-15T00:00:00+00:00")

    def resolve(self, path: Path) -> Path:
        return Path(str(path).replace("/current", "/rel-new"))

    def read(self, path: Path) -> bytes:
        if path.name == "manifest.yaml":
            return b"profile_name: fixture-solet\nplugins: []\n" if not self.s.bad_manifest else b"plugins: [changed]\n"
        if path.name == "receipt.json":
            return self.s.receipt if isinstance(self.s.receipt, bytes) else json.dumps(self.s.receipt).encode()
        if path.name.endswith(".lock"):
            return b""
        proof = {"head": self.s.serving, "state": "equal", "mismatches": [], **self.s.proof_change}
        raw = json.dumps({"artifact_to_head_equality": proof}).encode()
        if path.name == "VERSION":
            return json.dumps({"release_id": "rel-new", "git_sha": self.s.serving[:9], "selected_file_manifest": {"filename": "SELECTED_FILES.json", "sha256": sha256(raw).hexdigest()}, **self.s.version_change}).encode()
        assert path.name == "SELECTED_FILES.json", path
        self.log.append("REACHED_VERIFY")
        print("REACHED_VERIFY", flush=True)
        return raw

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
        if argv[0] == self.args.bridge:
            return self.process(argv[2], json.loads(argv[3]))
        if argv[1] == "deployed":
            self.log.append("REACHED_RECORD")
            print("REACHED_RECORD", flush=True)
            self.s.recorded = True
            if self.s.ambiguous:
                raise subprocess.TimeoutExpired(argv, 30)
            return "{}"
        return self.current()

    def current(self) -> str:
        """Simulate an observation becoming visible after an ambiguous write."""
        self.s.current_reads += 1
        visible = self.s.recorded and not (self.s.stale_first and self.s.current_reads == 1)
        commit = self.s.target if visible else self.s.baseline
        if visible:
            self.log.append("REACHED_VERIFIED_RECORD")
            print("REACHED_VERIFIED_RECORD", flush=True)
        return json.dumps([{"repository_id": self.m.REPOSITORY, "value_sha256": commit, "value_text": "rel-new" if visible else "rel-old", "measurement_id": self.s.measurement_id, "measured_at": "2026-10-15T00:00:00+00:00"}] if commit else [])

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
        if key == self.m.SWAP:
            if self.s.flat_swap:
                return json.dumps({"status": "completed", "result": {"action_status": "completed", "success": True, **self.status()}})
            return envelope(self.status())
        if key == self.m.PLUGINS:
            return self.plugins(key)
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
