"""Late-boot schedules and a durable, single-slot full-battery producer.

No schedule executes tests on the action queue. Busy work stays queued; a
five-minute trigger retries it and reconciles the failure outbox. Production
uses native register attribution; tests inject only these external boundaries.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from platform_dev_surface_plugin.bounded_subprocess import run_bounded
from platform_dev_surface_plugin.quality.battery_register import BatteryRegister
from quality_gates.full_smoke_campaign import CampaignStore, battery_busy, campaign_budget, utc_now
from quality_gates.gate_smoke_register import REGISTER_RELPATH as _REGISTER_RELPATH
from quality_gates.gate_smoke_register import load as _load_effective_register
from quality_gates.smoke_identity import digest, git, read_tree, verify_worktree
from quality_gates.smoke_pass_store import default_store_root
from quality_gates.smoke_proof import _hub_changed, finish_full_proofs, prepare_full_proofs

TARGET = "service_interface::quality_service::trigger_full_smoke_battery"
SYSTEM_STATE = {"flow_id": "quality-full-smoke-producer", "session_id": "quality-full-smoke-producer"}
_SCHEDULES = (("quality-full-smokes-six-hour", "0 */6 * * *", "schedule"), ("quality-full-smokes-due-retry", "*/5 * * * *", "due_retry"))


def _data(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("error") or value.get("action_status") == "error":
        raise RuntimeError(f"scheduler operation failed: {value}")
    return value.get("data", value)


def ensure_full_smoke_schedule(scheduler: Any) -> dict[str, Any]:
    """Ensure exact target, cadence and system state through scheduler lifecycle."""
    if scheduler is None:
        return {"installed": False, "advisory": "scheduler unavailable before late action-factory injection", "schedules": []}
    installed = [_ensure_one(scheduler, tag, cron, trigger) for tag, cron, trigger in _SCHEDULES]
    result: dict[str, Any] = {"installed": all(row["verified"] for row in installed), "schedules": installed, "commissioned": False}
    if not result["installed"]:
        result["advisory"] = "schedule installation unverified; retry explicit ensure or late action-factory injection: " + "; ".join(row["advisory"] for row in installed if not row["verified"])
        logging.getLogger(__name__).warning("%s", result["advisory"])
    return result


def _ensure_one(scheduler: Any, tag: str, cron: str, trigger: str) -> dict[str, Any]:
    observed = _data(scheduler.get_schedules_by_tag(tag=tag))
    matches = observed.get("schedules", [])
    if observed.get("complete") is not True:
        return {
            "verified": False,
            "status": "uncertain",
            "tag": tag,
            "source_complete": observed.get("complete"),
            "observed_schedule_ids": [row["schedule_id"] for row in matches],
            "advisory": f"{tag}: scheduler enumeration incomplete or unknown; observed schedules retained and unknown absence not used to create; {observed.get('advisories', [])}",
        }
    desired = [{"process_key": TARGET, "arguments": {"trigger": trigger}, "result_processor": None, "result_processor_kind": None}]
    correct = [row for row in matches if _matching_schedule(row, cron, desired)]
    if len(matches) == len(correct) == 1:
        return {"schedule_id": correct[0]["schedule_id"], "verified": True, "status": "already_present", "cron_expression": cron, "process_key": TARGET, "source_complete": True}
    for row in matches:
        _data(scheduler.clear_scheduled_action(schedule_id=row["schedule_id"], state=SYSTEM_STATE))
    result = _data(scheduler.create_cron_schedule(cron_expression=cron, action_definitions=desired, label=tag, tags=[tag], state=SYSTEM_STATE))
    if not result.get("schedule_id"):
        raise RuntimeError("schedule creation has no measured schedule id")
    return _created_readback(scheduler, tag, cron, desired, result["schedule_id"])


def _created_readback(scheduler: Any, tag: str, cron: str, desired: list[dict[str, Any]], schedule_id: str) -> dict[str, Any]:
    """Only a complete exact observation certifies the just-created identity."""
    readback = _data(scheduler.get_schedules_by_tag(tag=tag))
    rows = readback.get("schedules", [])
    verified = readback.get("complete") is True and len(rows) == 1 and rows[0].get("schedule_id") == schedule_id and _matching_schedule(rows[0], cron, desired)
    outcome = {"schedule_id": schedule_id, "verified": verified, "status": "created" if verified else "unverified", "cron_expression": cron, "process_key": TARGET, "source_complete": readback.get("complete")}
    if not verified:
        outcome["advisory"] = f"{tag}: created identity, exact contract and uniqueness not proven by complete readback; {readback.get('advisories', [])}"
    return outcome


def _matching_schedule(row: dict[str, Any], cron: str, desired: list[dict[str, Any]]) -> bool:
    return row.get("cron_expression") == cron and row.get("action_definitions") == desired and all(row.get(key) == value for key, value in SYSTEM_STATE.items()) and row.get("status") in ("scheduled", "running")


class FullBatteryProducer:
    """One background worker, with restart-safe requests and filing retries."""

    def __init__(self, root: Path, solet_name: str, *, store: CampaignStore | None = None, register: Any = None, runner: Callable[[dict[str, Any], Path], dict[str, Any]] | None = None) -> None:
        self.root = root
        self.solet_name = solet_name
        self.store = store or CampaignStore()
        self.register = register or BatteryRegister(solet_name, "quality-full-smoke-producer")
        self.runner = runner or self._run_battery
        self._slot = threading.Lock()
        self._receipt_cursor = 0
        self.active = True

    def trigger(self, trigger: str = "schedule") -> dict[str, Any]:
        if trigger not in ("schedule", "due_retry", "hub", "manual"):
            raise ValueError("unknown full-battery trigger")
        if not self.active:
            return {"status": "inactive", "campaign_id": None}
        request = self._request(trigger)
        campaign = self._enqueue(request) if request else None
        if not self._slot.acquire(blocking=False):
            return {"status": "already_running", "campaign_id": campaign["campaign_id"] if campaign else None}
        try:
            threading.Thread(target=self._background, name="quality-full-smoke-producer", daemon=True).start()
        except RuntimeError as error:
            self._slot.release()
            return {"status": "queued", "campaign_id": campaign["campaign_id"] if campaign else None, "advisory": f"worker launch unavailable: {error}; due work remains on disk for retry", "commissioned": False}
        return {"status": "started", "campaign_id": campaign["campaign_id"] if campaign else None, "commissioned": False}

    def _enqueue(self, request: dict[str, Any]) -> dict[str, Any]:
        """Requeue incomplete terminal work; keep its linked attempts/history."""
        campaign = self.store.enqueue(request)
        if campaign["state"] == "closed" and campaign.get("result", {}).get("result") != "superseded" and not _full_sampled(campaign.get("result", {})):
            campaign.update(state="queued", next_due_at=utc_now())
            return self.store.write(campaign["campaign_id"], campaign, expected_version=campaign["version"], reason="retry incomplete campaign; terminal attempt retained")
        return campaign

    def _request(self, trigger: str) -> dict[str, Any] | None:
        tree = read_tree(self.root, "master")
        previous = self._last_sample()
        now = datetime.now(UTC)
        if trigger == "due_retry" and previous:
            age = (now - datetime.fromisoformat(previous["finished_at"])).total_seconds()
            changed = git(self.root, "diff", "--name-only", "--no-renames", previous["request"]["commit"], tree.commit, "--").decode().splitlines()
            if age >= 6 * 3600:
                trigger = "schedule"
            elif _hub_changed(changed):
                trigger = "hub"
            else:
                return None
        window = int(now.timestamp()) // (6 * 3600)
        return {"commit": tree.commit, "tree": tree.tree, "manifest_sha256": tree.manifest_sha256, "trigger": trigger, "window": window}

    def _last_sample(self) -> dict[str, Any] | None:
        samples = [row for row in self.store.all() if _full_sampled(row.get("result", {})) and row["state"] == "closed"]
        return max(samples, key=lambda row: row["finished_at"]) if samples else None

    def _last_success(self) -> dict[str, Any] | None:
        successes = [row for row in self.store.all() if _full_clean(row.get("result", {})) and row["state"] == "closed"]
        return max(successes, key=lambda row: row["finished_at"]) if successes else None

    def _background(self) -> None:
        try:
            self.run_due()
        finally:
            self._slot.release()

    def run_due(self) -> list[dict[str, Any]]:
        """Bounded one-campaign pass plus pending outbox; injectable in tests."""
        with self.store.exclusion() as acquired:
            if not acquired:
                return [{"status": "already_running"}]
            campaigns = self.store.all()
            for index, row in enumerate(campaigns):
                if row["filing"]["status"] in ("pending", "uncertain", "submitted"):
                    campaigns[index] = self._file_failure(row)
            queued = [row for row in campaigns if row["state"] in ("queued", "running")]
            return [self._execute(min(queued, key=lambda row: row["queued_at"]))] if queued else []

    def _busy(self) -> dict[str, Any]:
        if battery_busy(default_store_root().parent):
            return {"busy": True, "basis": "held canonical battery receipt lock"}
        try:
            return self.register.controller_busy()
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            return {"busy": False, "advisory": f"Controller queue UNKNOWN: {error}; host receipt exclusion still checked"}

    def _execute(self, campaign: dict[str, Any]) -> dict[str, Any]:
        busy = self._busy()
        if busy["busy"]:
            campaign.update(state="queued", next_due_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(), queue_evidence=busy)
            return self.store.write(campaign["campaign_id"], campaign, expected_version=campaign["version"], reason="yield to Controller/battery exclusion")
        campaign = self._refresh_request(campaign)
        campaign = self._begin_attempt(campaign)
        try:
            result = self.runner(campaign, self.store.root)
        except Exception as error:
            result = {"result": "interrupted", "exit_code": None, "advisory": str(error)}
        campaign.update(state="closed", finished_at=utc_now(), result=result, queue_evidence=busy)
        campaign["attempts"][-1].update(result=result, finished_at=campaign["finished_at"])
        if not _full_clean(result):
            campaign["filing"] = {"status": "pending", "marker": f"smoke-campaign-{campaign['campaign_id']}", "owner": "quality-full-smoke-producer"}
        closed = self.store.write(campaign["campaign_id"], campaign, expected_version=campaign["version"], reason="terminal full battery observation")
        return self._file_failure(closed) if closed["filing"]["status"] == "pending" else closed

    def _refresh_request(self, campaign: dict[str, Any]) -> dict[str, Any]:
        """Queue delay may advance master; retain the original as superseded."""
        tree = read_tree(self.root, "master")
        if campaign["request"]["commit"] == tree.commit:
            return campaign
        request = {**campaign["request"], "commit": tree.commit, "tree": tree.tree, "manifest_sha256": tree.manifest_sha256, "supersedes_campaign": campaign["campaign_id"]}
        successor = self.store.enqueue(request)
        campaign.update(state="closed", finished_at=utc_now(), result={"result": "superseded", "successor_campaign": successor["campaign_id"], "reason": "master advanced while work was queued"})
        if campaign["attempts"]:
            campaign["attempts"][-1].update(finished_at=campaign["finished_at"], result={"result": "interrupted", "reason": "prior worker no longer holds exclusion"})
        self.store.write(campaign["campaign_id"], campaign, expected_version=campaign["version"], reason="superseded queued identity, retained successor link")
        return successor

    def _begin_attempt(self, campaign: dict[str, Any]) -> dict[str, Any]:
        if campaign["state"] == "running":
            campaign["attempts"][-1].update(result={"result": "interrupted", "advisory": "prior worker no longer holds exclusion"}, finished_at=utc_now())
        previous = self._last_success()
        try:
            attribution = self.register.landings(previous["finished_at"] if previous else None)
        except Exception as error:
            attribution = {"landings": [], "change_ids": [], "advisory": f"native attribution UNKNOWN: {error}"}
        campaign.update(state="running", attribution=attribution, attribution_since=previous["finished_at"] if previous else None)
        self._retain_native(campaign)
        campaign["attempts"].append({"number": len(campaign["attempts"]) + 1, "started_at": utc_now(), "previous_attempt": len(campaign["attempts"]) or None})
        return self.store.write(campaign["campaign_id"], campaign, expected_version=campaign["version"], reason="start linked full battery attempt")

    def _run_battery(self, campaign: dict[str, Any], directory: Path) -> dict[str, Any]:
        request = campaign["request"]
        tree = read_tree(self.root, request["commit"])
        verify_worktree(self.root, tree, list(tree.entries))
        # The EFFECTIVE register: gate_smokes.txt plus its fragments, so the
        # campaign's entry list and budget cover a fragment-registered smoke
        # that the runner below is going to run (prb_ae6d81ea).
        effective = _load_effective_register(self.root / _REGISTER_RELPATH)
        entries = effective.entries
        for advisory in effective.advisories:
            logging.getLogger(__name__).warning("%s", advisory)
        # Historical estimates are advisory, never a subprocess kill deadline.
        budget = campaign_budget(default_store_root().parent, registered_count=len(entries), entries=entries)
        output = directory / f"{campaign['campaign_id']}.{len(campaign['attempts'])}.results.json"
        name = f"scheduled:{campaign['campaign_id']}:{len(campaign['attempts'])}"
        principal = "system:quality-full-smoke-producer"
        context = prepare_full_proofs(self.root, tree.commit, principal=principal, attribution=campaign["attribution"])
        # The runner resolves the fragment directory beside this register path,
        # so naming the main register still runs the whole effective register.
        argv = [str(self.root / ".venv/bin/python3"), str(self.root / "quality_gates/run_smokes.py"), "--register", str(self.root / _REGISTER_RELPATH), "--campaign", name, "--results-output", str(output)]
        bounded = run_bounded(argv, cwd=self.root, timeout=None, extra_env={"SOLET_NAME": self.solet_name})
        verify_worktree(self.root, tree, list(tree.entries))
        # Without a published results file the runner's harness defects are
        # unknown: null, never an empty list that would claim none occurred.
        result = json.loads(output.read_bytes()) if output.exists() else {"result": "interrupted", "exit_code": bounded.exit_code, "harness_defects": None}
        import_environment = _campaign_import_environment(default_store_root().parent, name)
        proofs = finish_full_proofs(self.root, context, result, import_environment, principal=principal)
        return {
            **result,
            "budget": budget,
            "budget_advisory": "Estimate only; no outer subprocess deadline. Inner smoke limits remain independent.",
            "argv": argv,
            "runner_exit_code": bounded.exit_code,
            "timed_out": bounded.timed_out,
            "output": bounded.output,
            "output_truncated": bounded.truncated,
            "output_chars_total": bounded.output_chars_total,
            "results_path": str(output),
            "results_sha256": digest(result),
            "registered_count": len(entries),
            "proofs": [{"smoke": row["smoke"], "key": row["proof"]["key"], "event_id": row["proof"]["observation_event_id"]} for row in proofs],
            "cache_ignored": True,
        }

    def _file_failure(self, campaign: dict[str, Any]) -> dict[str, Any]:
        filing = campaign["filing"]
        try:
            self._refresh_attribution(campaign)
            detail = self._failure_detail(campaign)
            problem = self.register.reconcile(filing["marker"])
            problem = problem or self.register.file_problem(filing["marker"], detail)
            filing.update(status="submitted", **problem)
            campaign = self.store.write(campaign["campaign_id"], campaign, expected_version=campaign["version"], reason="native issue and Problem read back")
            if campaign["attribution"].get("advisory"):
                raise RuntimeError("Problem retained; attribution remains UNKNOWN and links are pending")
            evidence = self.register.attribute_problem(problem, filing["marker"], detail, campaign["attribution"]["change_ids"])
            filing.update(status="confirmed", evidence=evidence)
        except Exception as error:
            filing.update(status="uncertain", advisory=str(error), next_due_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat())
        campaign["filing"] = filing
        self._retain_native(campaign)
        return self.store.write(campaign["campaign_id"], campaign, expected_version=campaign["version"], reason="failure outbox reconciliation")

    def _refresh_attribution(self, campaign: dict[str, Any]) -> None:
        if campaign["attribution"].get("advisory"):
            try:
                campaign["attribution"] = self.register.landings(campaign.get("attribution_since"))
            except Exception as error:
                campaign["attribution"]["advisory"] = f"native attribution UNKNOWN: {error}"

    def _failure_detail(self, campaign: dict[str, Any]) -> str:
        payload = {key: campaign.get(key) for key in ("campaign_id", "request", "result", "attempts", "attribution", "attribution_since")}
        artifact = self.store.journal.retain_attempt(payload, principal="system:quality-full-smoke-producer")
        detail = json.dumps(
            {
                "marker": campaign["filing"]["marker"],
                "campaign_id": campaign["campaign_id"],
                "request": campaign["request"],
                "evidence_path": artifact,
                "evidence_sha256": digest(payload),
                "change_ids_count": len(campaign["attribution"]["change_ids"]),
                "change_ids_sha256": digest(campaign["attribution"]["change_ids"]),
                "change_ids_in_evidence": True,
                "attribution_kind": "landing exposure, causal root cause unproven",
            },
            sort_keys=True,
        )
        if len(detail.encode()) > 4096:
            raise ValueError("native failure pointer exceeds event limit")
        return detail

    def _retain_native(self, campaign: dict[str, Any]) -> None:
        receipts = getattr(self.register, "receipts", [])
        if len(receipts) > self._receipt_cursor:
            body = {"campaign_id": campaign["campaign_id"], "native_receipts": receipts[self._receipt_cursor :]}
            path = self.store.journal.retain_attempt(body, principal="system:quality-full-smoke-producer")
            campaign.setdefault("native_receipts", []).append({"path": path, "sha256": digest(body)})
            self._receipt_cursor = len(receipts)


def _full_sampled(result: dict[str, Any]) -> bool:
    """Completed physical samples bound cadence without becoming clean proof."""
    summary = result.get("summary", {})
    total = summary.get("total", 0)
    return result.get("result") in ("passed", "failed", "passed_with_retries") and total > 0 and result.get("registered_count") == total and sum(summary.get(key, 0) for key in ("passed", "failed", "skipped")) == total and not summary.get("missing") and not summary.get("not_run") and not result.get("timed_out")


def _full_clean(result: dict[str, Any]) -> bool:
    summary = result.get("summary", {})
    return result.get("result") == "passed" and result.get("exit_code") == 0 and summary.get("total", 0) > 0 and summary.get("total") == summary.get("passed") and not summary.get("retried_count") and not result.get("timed_out")


def _campaign_import_environment(directory: Path, campaign: str) -> dict[str, Any]:
    for path in directory.glob("*.json"):
        try:
            receipt = json.loads(path.read_bytes())
        except (OSError, ValueError):
            continue
        if receipt.get("campaign") == campaign:
            return receipt.get("import_environment", {})
    return {"state": "advisory", "advisory": "physical runner import-environment receipt unavailable"}
