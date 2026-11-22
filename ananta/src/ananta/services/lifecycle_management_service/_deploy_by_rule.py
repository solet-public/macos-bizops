"""External deploy orchestration, never an action-queue handler.

The solet's run_deploy_rule verb starts it as a detached process, and so can a
person or seat by hand.

Receipt observations are advisory and host-wide, with an accepted check/start
race. A running full-campaign receipt is noted, not a hold. Provenance consumes the builder attestation, not a second file rehash.
Every invocation emits an immutable terminal receipt; correction is a new run.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))
from quality_gates import host_compute_budget  # noqa: E402

APPLY = "service_interface::lifecycle_management_service::apply_manifest"
SWAP = "service_interface::local_self_deployment_service::swap_status"
PLUGINS = "service_interface::lifecycle_management_service::list_plugins"
NOTICE = "plugin::agent_messaging_plugin::peer_send_by_name"
RECORD = "service_interface::lifecycle_management_service::record_deployment"
READ = "service_interface::lifecycle_management_service::read_deployments"
FULL_BATTERY = "service_interface::quality_service::trigger_full_smoke_battery"
# full_battery.py names each campaign's receipt f"scheduled:{campaign_id}:{attempt}".
CAMPAIGN_RECEIPT_PREFIX = "scheduled:"
# The cutover's own refusal code (macOS RestartReasonCode.SCHEMA_PREFLIGHT_REFUSED), carried by apply_manifest as restart_reason_code.
SCHEMA_REFUSED = "schema_preflight_refused"
ROUTE = "plugins/default_thinking_plugin/knowledge_base_joseki/deploy_local_blue_green_release.md#Installing a release the cutover refuses"
SCHEMA_ROUTE_MARKER = "data/runtime/deploy-by-rule.schema-route.json"
type Row = dict[str, Any]


class ActionFailedError(ValueError):
    """The verb answered, and the answer says it did not do the work."""


class SchemaRefusedError(Exception):
    """The cutover refused the release's schema change; only the hand route installs it."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class Ports:
    """All external effects, replaced in offline tests."""

    command: Callable[[list[str]], str]
    read: Callable[[Path], bytes]
    resolve: Callable[[Path], Path]
    receipts: Callable[[], list[Path]]
    receipt_lock_held: Callable[[Path], bool]
    lock: Callable[[Path], AbstractContextManager[bool]]
    clock: Callable[[], float]
    sleep: Callable[[float], None]
    utc: Callable[[], str]
    host_acquire: Callable[[str], host_compute_budget.LeaseOutcome] | None = None
    host_release: Callable[[host_compute_budget.LeaseOutcome], None] | None = None
    write: Callable[[Path, bytes], None] | None = None


def command(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, errors="surrogateescape", check=True, timeout=30).stdout


def receipt_lock_held(path: Path) -> bool:
    """Return whether a battery holds the receipt's exclusive sibling lock."""
    with path.with_suffix(".lock").open("rb") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False


@contextmanager
def lock(path: Path) -> Generator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            try:
                yield True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def write_atomic(path: Path, data: bytes) -> None:
    """Replace a file whole: a reader sees the old bytes or the new, never a part."""
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        scratch.write_bytes(data)
        os.replace(scratch, path)
    except OSError:
        scratch.unlink(missing_ok=True)
        raise


def native_ports() -> Ports:
    directory = Path(os.environ.get("RUN_SMOKES_BATTERY_RECEIPT_DIR", "~/.ananta/runtime/gate_battery_receipts")).expanduser()
    return Ports(command, Path.read_bytes, Path.resolve, lambda: [path for path in directory.iterdir() if path.suffix == ".json"], receipt_lock_held, lock, time.monotonic, time.sleep, lambda: datetime.now(UTC).isoformat(), host_compute_budget.try_acquire, host_compute_budget.release, write_atomic)


def bridge_data(payload: Row) -> Row:
    """Decode both CLI outer envelopes and action result/data, rejecting errors."""
    if payload.get("status") != "completed":
        raise ValueError(f"bridge_outer_failed: {payload}")
    result = payload["result"]
    if result.get("success") is False or result.get("error") or result.get("action_status", "completed") != "completed":
        raise ActionFailedError(f"bridge_action_failed: {result}")
    return result["data"]


def bridge_swap_status(payload: Row) -> Row:
    """Decode swap_status whether its action fields are flat or under data."""
    if payload.get("status") != "completed":
        raise ValueError(f"bridge_outer_failed: {payload}")
    result = payload["result"]
    if result.get("success") is False or result.get("error") or result.get("action_status", "completed") != "completed":
        raise ActionFailedError(f"bridge_action_failed: {result}")
    if "data" in result:
        return result["data"]
    if "swap_in_progress" not in result or "router_status" not in result:
        raise ValueError(f"bridge_swap_shape_unknown: {result}")
    return {key: value for key, value in result.items() if key not in {"action_status", "success", "error"}}


def process(p: Ports, args: argparse.Namespace, key: str, values: Row) -> Row:
    """Await admitted jobs and restore their original action result envelope.

    The piped CLI returns only admission unless --wait is explicit. With it,
    synchronous keys retain their outer envelope, while jobs return the exact
    get_job record with the original action response under result. A failed
    CLI wait leaves the job running or terminal; callers retain its diagnostics
    through the existing advisory/reconciliation paths, without replaying APPLY.
    """
    try:
        payload = json.loads(p.command([args.bridge, "call", key, json.dumps(values), "--wait"]))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"bridge_call_unresolved: {error}; stdout={error.stdout!r}; stderr={error.stderr!r}") from error
    if "id" in payload:
        if payload.get("status") != "completed" or payload.get("error") or not isinstance(payload.get("result"), dict):
            raise ValueError(f"bridge_job_unresolved: {payload}")
        payload = {"status": "completed", "result": payload["result"]}
    return bridge_swap_status(payload) if key == SWAP else bridge_data(payload)


def git(p: Ports, args: argparse.Namespace, *values: str) -> str:
    return p.command(["git", "-C", str(args.repo), *values]).rstrip("\n")


def current(p: Ports, args: argparse.Namespace) -> Row:
    rows = process(p, args, READ, {"solet_name": args.solet_name, "mode": "current"})["rows"]
    valid = [r for r in rows if re.fullmatch(r"[0-9a-f]{40}", r.get("git_sha") or "")]
    return max(valid, key=lambda r: datetime.fromisoformat(r["deployed_at"]), default={})


def advisory_read(read: Callable[[], Any], notes: list[str], label: str) -> Any:
    try:
        return read()
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        notes.append(f"{label}: {error}")
        return None


def production(path: str) -> bool:
    return bool(re.match(r"^(ananta/src/|plugins/[^/]+/src/|profile/config/)", path))


def changed_paths(raw: str) -> list[str]:
    """Consume status-specific NUL records; retain old and new rename paths."""
    fields, paths = iter(raw.rstrip("\0").split("\0")) if raw else iter(()), []
    for status in fields:
        paths.append(next(fields))
        if status.startswith(("R", "C")):
            paths.append(next(fields))
    return paths


def release_version(p: Ports, args: argparse.Namespace) -> tuple[Path, Row]:
    release = p.resolve(args.releases_root / "current")
    version_path = p.resolve(release / "VERSION")
    if not version_path.is_relative_to(release):
        raise ValueError("escaped VERSION")
    return release, json.loads(p.read(version_path))


def attest(p: Ports, args: argparse.Namespace, target: str) -> Row:
    release, version = release_version(p, args)
    manifest = version["selected_file_manifest"]
    path = p.resolve(release / manifest["filename"])
    if not path.is_relative_to(release) or version["release_id"] != release.name:
        raise ValueError("release_path_mismatch")
    raw = p.read(path)
    proof = json.loads(raw)["artifact_to_head_equality"]
    digest = hashlib.sha256(raw).hexdigest()
    if digest != manifest["sha256"] or proof != {**proof, "head": target, "state": "equal", "mismatches": []}:
        raise ValueError("builder_attestation_mismatch")
    if git(p, args, "rev-parse", "--verify", f"{version['git_sha']}^{{commit}}") != target:
        raise ValueError("release_commit_mismatch")
    return {"release": release.name, "attestation_head": target, "attestation_state": "equal", "manifest_sha256": digest}


def holds(reply: Any) -> bool:
    """Read holds only as result -> data -> holds, each level a mapping; any other shape is not holding."""
    result = reply.get("result") if isinstance(reply, dict) else None
    data = result.get("data") if isinstance(result, dict) else None
    return isinstance(data, dict) and data.get("holds") is True


def identity(p: Ports, args: argparse.Namespace, notes: list[str]) -> list[Any]:
    results = [advisory_read(lambda role=role: json.loads(p.command([args.bridge, "holds-role", "--name", role, "--instance-id", args.agent_instance_id])), notes, "identity_unknown") for role in (f"{args.solet_name.title()}-Main", "Git-Controller")]
    if not any(holds(r) for r in results):
        notes.append("identity_not_current_holder")
    return results


def busy(p: Ports, notes: list[str]) -> Row:
    paths = advisory_read(p.receipts, notes, "receipts_unknown") or []
    for path in paths:
        row = advisory_read(lambda path=path: receipt(p.read(path)), notes, f"receipt_unknown:{path}")
        if row and row.get("result") == "running":
            held = advisory_read(lambda path=path: p.receipt_lock_held(path), notes, f"receipt_lock_unknown:{path}")
            if held:
                if str(row.get("campaign", "")).startswith(CAMPAIGN_RECEIPT_PREFIX):
                    notes.append(f"full_campaign_running_not_a_hold:{path}")
                    continue
                return {"receipt": str(path), "pid": row.get("pid"), "candidate_digest": row.get("candidate_digest")}
            notes.append(f"receipt_stale_running:{path}")
    return {}


def receipt(raw: bytes) -> Row:
    row = json.loads(raw)
    if not isinstance(row, dict) or not isinstance(row.get("result"), str):
        raise ValueError("malformed_receipt")
    return row


def stable(row: Row) -> tuple[str, str] | None:
    router = row["router_status"]
    active = [r for r in router["colors"] if r["status"] == "active"]
    if row["swap_in_progress"] or router["drain_entries"] or len(active) != 1:
        return None
    color, instance = router["active_color"], router["active_instance_id"]
    return (color, instance) if (color, instance) == (active[0]["color"], active[0]["instance_id"]) else None


def healthy(p: Ports, args: argparse.Namespace) -> None:
    rows = process(p, args, PLUGINS, {})["plugins"]
    if not rows or any(r["lifecycle_managed"] and (r["status"] != "ready" or r["is_running"] is not True) for r in rows):
        raise ValueError("plugins_not_ready")


def wait_for_readiness(p: Ports, args: argparse.Namespace, notes: list[str], deadline: float) -> bool:
    """Retry restart-sensitive readiness reads until the shared deadline."""
    attempted = False
    while not attempted or p.clock() < deadline:
        attempted = True
        try:
            healthy(p, args)
            return True
        except ValueError as error:
            label = "plugins_not_ready_retry" if str(error) == "plugins_not_ready" else "plugins_readiness_retry"
            notes.append(f"{label}:{error}")
        except (OSError, KeyError, TypeError, StopIteration, subprocess.SubprocessError) as error:
            notes.append(f"plugins_readiness_retry:{error}")
        remaining = deadline - p.clock()
        if remaining <= 0:
            return False
        p.sleep(min(5, remaining))
    return False


def require_applied(committed: Row, etag: str) -> None:
    """Read the commit's answer: a cutover refusal is its own outcome, any other non-applied answer a rejection."""
    if committed.get("restart_reason_code") == SCHEMA_REFUSED:
        detail = (committed.get("rejection_reasons") or [committed.get("message")])[0]
        raise SchemaRefusedError(_bounded(detail) or "the cutover gave no detail")
    if (committed.get("status"), committed.get("current_etag")) != ("applied", etag):
        raise ValueError(f"apply_rejected: {committed}")


def swap(p: Ports, args: argparse.Namespace, target: str, old: tuple[str, str], notes: list[str]) -> bool:
    """Submit at most once; an ambiguous commit is reconciled only by reads."""
    manifest_path = args.app_home / "config/manifest.yaml"
    manifest = yaml.safe_load(p.read(manifest_path))
    if not isinstance(manifest, dict):
        raise ValueError("manifest_not_mapping")
    values = {"new_manifest": manifest, "reason": f"deploy-by-rule target={target}", "dry_run": True}
    dry = process(p, args, APPLY, values)
    if dry["status"] != "dry_run" or not dry["current_etag"]:
        raise ValueError("dry_run_without_etag")
    try:
        committed = process(p, args, APPLY, {**values, "dry_run": False, "expected_etag": dry["current_etag"]})
    except ActionFailedError as error:
        raise ValueError(f"apply_rejected: {error}") from error
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        notes.append(f"apply_unresolved: {error}")
        committed = None
    if committed is not None:
        require_applied(committed, dry["current_etag"])
    deadline = p.clock() + args.deadline
    wait_for_drain(p, args, old, notes, deadline)
    if yaml.safe_load(p.read(manifest_path)) != manifest:
        raise ValueError("manifest_changed")
    return wait_for_readiness(p, args, notes, deadline)


def wait_for_drain(p: Ports, args: argparse.Namespace, old: tuple[str, str], notes: list[str], deadline: float) -> None:
    """Read retries are bounded; the router finisher supplies normal convergence."""
    while p.clock() < deadline:
        row = advisory_read(lambda: process(p, args, SWAP, {}), notes, "swap_read_retry")
        new = advisory_read(lambda row=row: stable(row), notes, "swap_shape_retry") if row else None
        if new and new[0] != old[0] and new[1] != old[1]:
            return
        p.sleep(5)
    raise ValueError("swap_deadline_unresolved")


def record(p: Ports, args: argparse.Namespace, target: str, proof: Row, notes: list[str]) -> Row:
    def matches(row: Row) -> bool:
        return row.get("git_sha") == target and row.get("release_id") == proof["release"]

    observed = current(p, args)
    if matches(observed):
        return {"decision": "already_recorded", "deployment_id": observed["deployment_id"]}
    advisory_read(
        lambda: process(
            p,
            args,
            RECORD,
            {
                "solet_name": args.solet_name,
                "release_id": proof["release"],
                "git_sha": target,
                "deployed_at": p.utc(),
                "trigger": args.trigger,
                "manifest_sha256": proof["manifest_sha256"],
                "agent_instance_id": args.agent_instance_id,
                "actor_model": args.actor_model,
                "actor_effort": args.actor_effort,
            },
        ),
        notes,
        "record_write_unresolved",
    )
    observed = current(p, args)
    if not matches(observed):
        raise ValueError("record_unresolved")
    return {"decision": "deployed", "deployment_id": observed["deployment_id"]}


def request_full_battery(p: Ports, args: argparse.Namespace, notes: list[str]) -> Row:
    """Ask the solet for one full battery run of the commit it just deployed.

    One advisory attempt: a failed ask never changes the deployment decision and
    is not retried, because the next deployment asks again and the verb runs a
    commit at most once.
    """
    return advisory_read(lambda: process(p, args, FULL_BATTERY, {"trigger": "deploy"}), notes, "full_battery_request_unresolved") or {"status": "unresolved"}


def _bounded(value: Any, limit: int = 240) -> str | None:
    """Keep exception notices useful without forwarding unbounded diagnostics."""
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def notify_main(p: Ports, args: argparse.Namespace, terminal: Row, notes: list[str]) -> Row:
    """Attempt one advisory exception notice for a non-success terminal outcome.

    A receipt is queued delivery, not model acceptance. The exception identity
    is stable for the same invocation identity; no deployment effect is retried.
    """
    decision = terminal.get("decision")
    notice_key = hashlib.sha256(
        json.dumps(
            [
                args.solet_name,
                args.trigger,
                decision,
                terminal.get("target"),
                terminal.get("release"),
                terminal.get("measurement_id"),
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    recipient = f"{args.solet_name.title()}-Main"
    notice: Row = {"notice_key": notice_key, "recipient": recipient, "status": "uncertain", "message_id": None}
    content = {
        "solet": args.solet_name,
        "outcome": decision,
        "decision": decision,
        "trigger": args.trigger,
        "target": terminal.get("target"),
        "release": terminal.get("release"),
        "measurement_id": terminal.get("measurement_id"),
        "error": _bounded(terminal.get("error")),
        "advisories": [_bounded(note) for note in notes[:8]],
        "notice_key": notice_key,
        "duplicate_semantics": "Repeated exception notices with this key refer to the same terminal outcome.",
    }
    try:
        receipt = process(p, args, NOTICE, {"name": recipient, "content": json.dumps(content, sort_keys=True)})
        message_id = receipt.get("message_id")
        if not isinstance(message_id, str) or not message_id.strip():
            raise ValueError("notice_receipt_missing_message_id")
        notice.update(status="queued", message_id=message_id, delivery=receipt.get("delivery"))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as error:
        notice["advisory"] = f"notice_delivery_uncertain:{type(error).__name__}:{error}"
        notes.append(notice["advisory"])
    return notice


def terminal_notice(p: Ports, args: argparse.Namespace, terminal: Row, notes: list[str]) -> Row:
    """Attach one notice only to failures and deferred terminal holds.

    A deferral that built nothing repeats one already sent, so it sends none.
    """
    decision = terminal.get("decision")
    if terminal.get("built") is False:
        return terminal
    if decision == "failed" or (isinstance(decision, str) and decision.startswith("deferred_")):
        return {**terminal, "notice": notify_main(p, args, terminal, notes)}
    return terminal


@dataclass
class AdmissionWait:
    """One invocation's admission episode, frozen before execution starts."""

    started: float | None = None
    seconds: float = 0.0
    reason: str | None = None
    notice: Row | None = None

    def observe(self, now: float, reason: str) -> None:
        if self.reason is None:
            self.started, self.reason = now, _bounded(reason)

    def elapsed(self, now: float) -> float:
        return self.seconds if self.started is None else max(0.0, now - self.started)

    def finish(self, now: float) -> None:
        self.seconds = self.elapsed(now)
        self.started = None

    def summary(self) -> Row:
        row: Row = {"wait_seconds": self.seconds, "wait_reason": self.reason}
        if self.notice is not None:
            row["notice"] = self.notice
        return row


def wait_notice(p: Ports, args: argparse.Namespace, wait: AdmissionWait, notes: list[str]) -> None:
    """Attempt once after 300 seconds, outside the deploy lock, even if uncertain."""
    seconds = wait.elapsed(p.clock())
    if wait.notice is not None or seconds < 300:
        return
    recipient = f"{args.solet_name.title()}-Main"
    wait.notice = {"recipient": recipient, "status": "uncertain", "message_id": None}
    content = {"decision": "host_compute_wait", "solet": args.solet_name, "trigger": args.trigger, "agent_instance_id": args.agent_instance_id, "wait_seconds": seconds, "wait_reason": wait.reason}
    try:
        receipt = process(p, args, NOTICE, {"name": recipient, "content": json.dumps(content, sort_keys=True)})
        message_id = receipt.get("message_id")
        if not isinstance(message_id, str) or not message_id.strip():
            raise ValueError("notice_receipt_missing_message_id")
        wait.notice.update(status="queued", message_id=message_id, delivery=receipt.get("delivery"))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as error:
        notes.append(f"wait_notice_delivery_uncertain:{_bounded(error)}")


def selected(p: Ports, args: argparse.Namespace, target: str, notes: list[str]) -> tuple[Row, list[str]]:
    previous = advisory_read(lambda: current(p, args), notes, "version_unknown") or {}
    baseline = previous.get("git_sha")
    if not baseline:
        notes.append("baseline_unknown_using_release_or_inventory")
        version = advisory_read(lambda: release_version(p, args)[1], notes, "release_unknown")
        baseline = advisory_read(lambda: git(p, args, "rev-parse", "--verify", f"{version['git_sha']}^{{commit}}"), notes, "baseline_unresolved") if version else None
    if baseline == target:
        return previous, []
    raw = git(p, args, "diff", "--name-status", "-z", "--find-renames", baseline, target, "--") if baseline else ""
    paths = changed_paths(raw) if baseline else git(p, args, "ls-tree", "-rz", "--name-only", target).rstrip("\0").split("\0")
    return previous, paths


def admitted_execute(p: Ports, args: argparse.Namespace, notes: list[str], wait: AdmissionWait) -> Row | None:
    """Run under the deploy lock, or release a refused lease for another poll."""
    if p.host_acquire is None and p.host_release is None:
        return execute(p, args, notes)
    if p.host_acquire is None or p.host_release is None:
        raise ValueError("incomplete_host_admission_ports")
    lease = p.host_acquire("deploy")
    if not lease.admitted:
        wait.observe(p.clock(), lease.reason)
        p.host_release(lease)
        return None
    wait.finish(p.clock())
    lease = host_compute_budget.with_wait(lease, wait.seconds, wait.reason)
    try:
        return execute(p, args, notes)
    finally:
        p.host_release(lease)


def deploy(p: Ports, args: argparse.Namespace) -> Row:
    notes: list[str] = []
    result: Row = {"trigger": args.trigger, "advisories": notes, "identity": args.agent_instance_id}
    wait = AdmissionWait()
    try:
        while True:
            wait_notice(p, args, wait, notes)
            with p.lock(args.app_home / "data/runtime/deploy-by-rule.lock") as acquired:
                if not acquired:
                    terminal = {**result, "decision": "deferred_lock"}
                    break
                outcome = admitted_execute(p, args, notes, wait)
                if outcome is not None:
                    terminal = {**result, **outcome}
                    break
            p.sleep(1)
    except (OSError, ValueError, KeyError, TypeError, StopIteration, subprocess.SubprocessError) as error:
        terminal = {**result, "decision": "failed", "error": str(error)}
    wait.finish(p.clock())
    terminal["host_compute_wait"] = wait.summary()
    if wait.reason is not None:
        notes.append(f"host_compute_wait:wait_seconds={wait.seconds};wait_reason={wait.reason}")
    return terminal_notice(p, args, terminal, notes)


def schema_route(built: bool, detail: str) -> Row:
    """The terminal fields of a release only the hand route installs."""
    return {"decision": "deferred_schema_route", "route": ROUTE, "refused": SCHEMA_REFUSED, "built": built, "detail": detail}


def serving_release(p: Ports, args: argparse.Namespace) -> str:
    return p.resolve(args.releases_root / "current").name


def mark_refused_release(p: Ports, args: argparse.Namespace, target: str) -> None:
    """Record the serving release that met the refusal, so later runs defer before they build."""
    if p.write is None:
        raise ValueError("no_write_port")
    marker = {"target": target, "current_release": serving_release(p, args), "refused_at": p.utc()}
    p.write(args.app_home / SCHEMA_ROUTE_MARKER, json.dumps(marker, sort_keys=True).encode())


def deferred_by_marker(p: Ports, args: argparse.Namespace, notes: list[str]) -> Row | None:
    """Defer, without building, while the serving release is the one that met the refusal.

    Taking the hand route moves current, and that clears the deferral by itself.
    An absent marker is the ordinary case. A marker or a serving release that
    cannot be read is an advisory and the run goes on: unknown data never blocks.
    """
    try:
        marker = json.loads(p.read(args.app_home / SCHEMA_ROUTE_MARKER))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        notes.append(f"schema_route_marker_unreadable:{_bounded(error)}")
        return None
    refused = marker.get("current_release") if isinstance(marker, dict) else None
    if not isinstance(refused, str):
        notes.append("schema_route_marker_unreadable:no current_release")
        return None
    if advisory_read(lambda: serving_release(p, args), notes, "schema_route_release_unknown") != refused:
        return None
    return schema_route(False, f"current is still {refused}, where {marker.get('target')} was refused at {marker.get('refused_at')}")


def refused_by_cutover(p: Ports, args: argparse.Namespace, result: Row, refusal: SchemaRefusedError, notes: list[str]) -> Row:
    """End the run as the hand route, and mark the serving release; a marker that cannot be written is an advisory."""
    advisory_read(lambda: mark_refused_release(p, args, result["target"]), notes, "schema_route_marker_unwritten")
    return {**result, **schema_route(True, refusal.detail), "error": f"{SCHEMA_REFUSED}: hand route {ROUTE}; {refusal.detail}"}


def serve(p: Ports, args: argparse.Namespace, target: str, old: tuple[str, str], notes: list[str]) -> bool:
    """Wait for readiness when the target already serves, otherwise swap to it."""
    version = advisory_read(lambda: release_version(p, args)[1], notes, "release_unknown")
    serving = advisory_read(lambda: git(p, args, "rev-parse", "--verify", f"{version['git_sha']}^{{commit}}"), notes, "serving_unknown") if version else None
    if serving == target:
        return wait_for_readiness(p, args, notes, p.clock() + args.deadline)
    return swap(p, args, target, old, notes)


def execute(p: Ports, args: argparse.Namespace, notes: list[str]) -> Row:
    target = git(p, args, "rev-parse", "--verify", "master^{commit}")
    previous, paths = selected(p, args, target, notes)
    result: Row = {"target": target, "previous": previous, "paths": paths}
    if previous.get("git_sha") == target:
        return {**result, "decision": "already_current"}
    if args.trigger == "landing" and not any(map(production, paths)):
        return {**result, "decision": "skipped_nonproduction", "full_battery": request_full_battery(p, args, notes)}
    held = deferred_by_marker(p, args, notes)
    if held:
        return {**result, **held}
    observed = busy(p, notes)
    if observed:
        return {**result, **observed, "decision": "deferred_gate"}
    old = advisory_read(lambda: stable(process(p, args, SWAP, {})), notes, "swap_state_unknown")
    if not old:
        return {**result, "decision": "deferred_swap"}
    result["holder_checks"] = identity(p, args, notes)
    try:
        served = serve(p, args, target, old, notes)
    except SchemaRefusedError as refusal:
        return refused_by_cutover(p, args, result, refusal, notes)
    if not served:
        return {**result, "decision": "deferred_readiness"}
    return conclude(p, args, result, target, notes)


def conclude(p: Ports, args: argparse.Namespace, result: Row, target: str, notes: list[str]) -> Row:
    """The target serves: attest it, record the deployment and ask for one full battery run."""
    proof = attest(p, args, target)
    if git(p, args, "rev-parse", "--verify", "master^{commit}") != target:
        notes.append("master_advanced")
    recorded = record(p, args, target, proof, notes)
    verified = {**result, **proof, **recorded}
    if recorded["decision"] == "deployed":
        verified["full_battery"] = request_full_battery(p, args, notes)
    return verified


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trigger", choices=("landing", "tick"), required=True)
    for name in ("repo", "app-home", "releases-root", "bridge"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    for name in ("agent-instance-id", "actor-model", "actor-effort"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--deadline", type=float, default=900)
    args = parser.parse_args(argv)
    args.solet_name = os.environ.get("SOLET_NAME", "").strip()
    if not args.solet_name:
        parser.error("SOLET_NAME is required")
    if not all(getattr(args, n).is_absolute() for n in ("repo", "app_home", "releases_root", "bridge")) or args.deadline <= 0:
        parser.error("absolute paths and a positive deadline are required")
    args.bridge = str(args.bridge)
    result = deploy(native_ports(), args)
    print(json.dumps(result, sort_keys=True))
    return int(result["decision"] == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
