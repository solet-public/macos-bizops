"""External, seat-invoked deploy orchestration; never an action-queue handler.

Receipt observations are advisory and host-wide, with an accepted check/start
race. Provenance consumes the builder attestation, not a second file rehash.
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
import time
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

APPLY = "service_interface::lifecycle_management_service::apply_manifest"
SWAP = "service_interface::local_self_deployment_service::swap_status"
PLUGINS = "service_interface::lifecycle_management_service::list_plugins"
NOTICE = "plugin::agent_messaging_plugin::peer_send_by_name"
REPOSITORY = "rep_7111540c-ac4a-468c-8f84-b67d8756d1d0"
type Row = dict[str, Any]


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


def native_ports() -> Ports:
    directory = Path(os.environ.get("RUN_SMOKES_BATTERY_RECEIPT_DIR", "~/.ananta/runtime/gate_battery_receipts")).expanduser()
    return Ports(command, Path.read_bytes, Path.resolve, lambda: [path for path in directory.iterdir() if path.suffix == ".json"], receipt_lock_held, lock, time.monotonic, time.sleep, lambda: datetime.now(UTC).isoformat())


def bridge_data(payload: Row) -> Row:
    """Decode both CLI outer envelopes and action result/data, rejecting errors."""
    if payload.get("status") != "completed":
        raise ValueError(f"bridge_outer_failed: {payload}")
    result = payload["result"]
    if result.get("success") is False or result.get("error") or result.get("action_status", "completed") != "completed":
        raise ValueError(f"bridge_action_failed: {result}")
    return result["data"]


def bridge_swap_status(payload: Row) -> Row:
    """Decode swap_status whether its action fields are flat or under data."""
    if payload.get("status") != "completed":
        raise ValueError(f"bridge_outer_failed: {payload}")
    result = payload["result"]
    if result.get("success") is False or result.get("error") or result.get("action_status", "completed") != "completed":
        raise ValueError(f"bridge_action_failed: {result}")
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
    rows = json.loads(p.command([args.psolet, "db", "version", "current", "--solet", args.solet_name, "--component", args.solet_name, "--limit", "5"]))
    valid = [r for r in rows if r.get("repository_id") == REPOSITORY and re.fullmatch(r"[0-9a-f]{40}", r.get("value_sha256") or "")]
    return max(valid, key=lambda r: datetime.fromisoformat(r["measured_at"]), default={})


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
    committed = advisory_read(lambda: process(p, args, APPLY, {**values, "dry_run": False, "expected_etag": dry["current_etag"]}), notes, "apply_unresolved")
    if committed is not None and (committed.get("status"), committed.get("current_etag")) != ("applied", dry["current_etag"]):
        raise ValueError(f"apply_rejected: {committed}")
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
        return row.get("value_sha256") == target and row.get("value_text") == proof["release"]

    observed = current(p, args)
    if matches(observed):
        return {"decision": "already_recorded", "measurement_id": observed["measurement_id"]}
    advisory_read(
        lambda: p.command([args.psolet, "deployed", args.solet_name, proof["release"], "--commit", target, "--deployed-at", p.utc(), "--repo", str(args.repo), "--solet", args.solet_name, "--agent-instance-id", args.agent_instance_id, "--actor-model", args.actor_model, "--actor-effort", args.actor_effort]),
        notes,
        "record_write_unresolved",
    )
    observed = current(p, args)
    if not matches(observed):
        raise ValueError("record_unresolved")
    return {"decision": "deployed", "measurement_id": observed["measurement_id"]}


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
    """Attach one notice only to failures and deferred terminal holds."""
    decision = terminal.get("decision")
    if decision == "failed" or (isinstance(decision, str) and decision.startswith("deferred_")):
        return {**terminal, "notice": notify_main(p, args, terminal, notes)}
    return terminal


def selected(p: Ports, args: argparse.Namespace, target: str, notes: list[str]) -> tuple[Row, list[str]]:
    previous = advisory_read(lambda: current(p, args), notes, "version_unknown") or {}
    baseline = previous.get("value_sha256")
    if not baseline:
        notes.append("baseline_unknown_using_release_or_inventory")
        version = advisory_read(lambda: release_version(p, args)[1], notes, "release_unknown")
        baseline = advisory_read(lambda: git(p, args, "rev-parse", "--verify", f"{version['git_sha']}^{{commit}}"), notes, "baseline_unresolved") if version else None
    if baseline == target:
        return previous, []
    raw = git(p, args, "diff", "--name-status", "-z", "--find-renames", baseline, target, "--") if baseline else ""
    paths = changed_paths(raw) if baseline else git(p, args, "ls-tree", "-rz", "--name-only", target).rstrip("\0").split("\0")
    return previous, paths


def deploy(p: Ports, args: argparse.Namespace) -> Row:
    notes: list[str] = []
    result: Row = {"trigger": args.trigger, "advisories": notes, "identity": args.agent_instance_id}
    try:
        with p.lock(args.app_home / "data/runtime/deploy-by-rule.lock") as acquired:
            if not acquired:
                terminal = {**result, "decision": "deferred_lock"}
            else:
                terminal = {**result, **execute(p, args, notes)}
    except (OSError, ValueError, KeyError, TypeError, StopIteration, subprocess.SubprocessError) as error:
        terminal = {**result, "decision": "failed", "error": str(error)}
    return terminal_notice(p, args, terminal, notes)


def execute(p: Ports, args: argparse.Namespace, notes: list[str]) -> Row:
    target = git(p, args, "rev-parse", "--verify", "master^{commit}")
    previous, paths = selected(p, args, target, notes)
    result: Row = {"target": target, "previous": previous, "paths": paths}
    if previous.get("value_sha256") == target:
        return {**result, "decision": "already_current"}
    if args.trigger == "landing" and not any(map(production, paths)):
        return {**result, "decision": "skipped_nonproduction"}
    observed = busy(p, notes)
    if observed:
        return {**result, **observed, "decision": "deferred_gate"}
    old = advisory_read(lambda: stable(process(p, args, SWAP, {})), notes, "swap_state_unknown")
    if not old:
        return {**result, "decision": "deferred_swap"}
    result["holder_checks"] = identity(p, args, notes)
    version = advisory_read(lambda: release_version(p, args)[1], notes, "release_unknown")
    serving = advisory_read(lambda: git(p, args, "rev-parse", "--verify", f"{version['git_sha']}^{{commit}}"), notes, "serving_unknown") if version else None
    if serving == target:
        ready = wait_for_readiness(p, args, notes, p.clock() + args.deadline)
    else:
        ready = swap(p, args, target, old, notes)
    if not ready:
        return {**result, "decision": "deferred_readiness"}
    proof = attest(p, args, target)
    if git(p, args, "rev-parse", "--verify", "master^{commit}") != target:
        notes.append("master_advanced")
    verified = {**result, **proof, **record(p, args, target, proof, notes)}
    return verified


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trigger", choices=("landing", "tick"), required=True)
    for name in ("repo", "app-home", "releases-root", "psolet", "bridge"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    for name in ("agent-instance-id", "actor-model", "actor-effort"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--deadline", type=float, default=900)
    args = parser.parse_args(argv)
    args.solet_name = os.environ.get("SOLET_NAME", "").strip()
    if not args.solet_name:
        parser.error("SOLET_NAME is required")
    if not all(getattr(args, n).is_absolute() for n in ("repo", "app_home", "releases_root", "psolet", "bridge")) or args.deadline <= 0:
        parser.error("absolute paths and a positive deadline are required")
    args.psolet, args.bridge = str(args.psolet), str(args.bridge)
    result = deploy(native_ports(), args)
    print(json.dumps(result, sort_keys=True))
    return int(result["decision"] == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
