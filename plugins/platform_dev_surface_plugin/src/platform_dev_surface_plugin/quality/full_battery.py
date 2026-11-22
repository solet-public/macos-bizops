"""Late-boot schedule retirement and a durable, single-slot full-battery producer.

No schedule executes tests on the action queue. A request is the master commit,
so one commit is one campaign and a commit with a result is not run again. The
worker drains the queue to its end; a newer commit replaces a queued older one.
A failed run remains in its campaign record, and a complete failure also reaches
the solet's main seat once: the notice is recorded as due in the same journal
write that closes the campaign, sent after it, and retried while it is due.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from platform_dev_surface_plugin.bounded_subprocess import run_bounded
from platform_dev_surface_plugin.quality.frozen_export import exports_root_for, frozen_export, sweep_exports
from quality_gates.full_smoke_campaign import CampaignStore, campaign_budget, utc_now
from quality_gates.gate_smoke_register import REGISTER_RELPATH as _REGISTER_RELPATH
from quality_gates.gate_smoke_register import load as _load_effective_register
from quality_gates.smoke_identity import digest, read_tree, verify_worktree
from quality_gates.smoke_pass_store import default_store_root
from quality_gates.smoke_proof import finish_full_proofs, prepare_full_proofs

SYSTEM_STATE = {"flow_id": "flow-quality-full-smoke-producer", "session_id": "sess-quality-full-smoke-producer"}
_READ_VERB = "service_interface::quality_service::list_full_smoke_campaigns"
_FAILURE_ADVISORY = f"failure recorded in this campaign only; read it with {_READ_VERB}"
PEER_SEND_BY_NAME = "plugin::agent_messaging_plugin::peer_send_by_name"
# A send that the queued route holds longer than this stays due and is retried; the worker holds the run exclusion meanwhile.
_NOTICE_TIMEOUT_SECONDS = 30

type Notifier = Callable[[str, str], Mapping[str, Any]]
"""One role-addressed send: (role name, message text) -> the receipt payload of the send. It raises when the send did not happen."""


def _data(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("error") or value.get("action_status") == "error":
        raise RuntimeError(f"scheduler operation failed: {value}")
    return value.get("data", value)


def retire_full_smoke_schedules(scheduler: Any) -> dict[str, Any]:
    """Clear observed full-battery schedules during late boot, recording uncertain tags."""
    if scheduler is None:
        return {"retired": 0, "advisory": "scheduler unavailable before late action-factory injection"}
    retired = 0
    uncertain: list[str] = []
    for tag in ("quality-full-smokes-six-hour", "quality-full-smokes-due-retry"):
        concerns: list[str] = []
        try:
            observed = _data(scheduler.get_schedules_by_tag(tag=tag))
            if observed.get("complete") is not True:
                concerns.append(f"enumeration complete={observed.get('complete')!r}")
            for row in observed.get("schedules", []):
                try:
                    _data(scheduler.clear_scheduled_action(schedule_id=row["schedule_id"], state=SYSTEM_STATE))
                    retired += 1
                except Exception as error:
                    concerns.append(f"clear {row.get('schedule_id')!r} failed: {error}")
        except Exception as error:
            concerns.append(f"read failed: {error}")
        if concerns:
            uncertain.append(tag)
            logging.getLogger(__name__).warning("full smoke schedule retirement uncertain for %s: %s", tag, "; ".join(concerns))
    return {"retired": retired, "uncertain": uncertain}


class FullBatteryProducer:
    """One background worker with restart-safe campaign requests."""

    def __init__(
        self,
        root: Path,
        solet_name: str,
        *,
        store: CampaignStore | None = None,
        runner: Callable[[dict[str, Any], Path], dict[str, Any]] | None = None,
        exports_root: Path | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self.root = root
        self.solet_name = solet_name
        self.store = store or CampaignStore()
        self.exports_root = exports_root or exports_root_for(self.store.root)
        self.runner = runner or self._run_battery
        self.notifier = notifier
        self._slot = threading.Lock()
        self.active = True

    def trigger(self, trigger: str = "manual") -> dict[str, Any]:
        if trigger not in ("deploy", "manual"):
            raise ValueError("unknown full-battery trigger")
        if not self.active:
            return {"status": "inactive", "campaign_id": None}
        campaign = self._enqueue(self._request())
        campaign_id = campaign["campaign_id"] if campaign else None
        if campaign and campaign["state"] == "closed":
            self._replay_due_notices()
            return {"status": "have_result", "campaign_id": campaign_id, "outcome": campaign["result"].get("result")}
        if campaign:
            self._supersede_other_queued(campaign)
        if not self._slot.acquire(blocking=False):
            return {"status": "already_running", "campaign_id": campaign_id}
        try:
            threading.Thread(target=self._background, name="quality-full-smoke-producer", daemon=True).start()
        except RuntimeError as error:
            self._slot.release()
            return {"status": "queued", "campaign_id": campaign_id, "advisory": f"worker launch unavailable: {error}; due work remains on disk for retry", "commissioned": False}
        return {"status": "started", "campaign_id": campaign_id, "commissioned": False}

    def _replay_due_notices(self) -> None:
        """A request that finds its result also retries a notice still due, on the worker: the caller never waits on a send."""
        if self.notifier is None or not any((row.get("notice") or {}).get("status") == "due" for row in self.store.all()):
            return
        if not self._slot.acquire(blocking=False):
            return
        try:
            threading.Thread(target=self._background, name="quality-full-smoke-producer", daemon=True).start()
        except RuntimeError as error:
            self._slot.release()
            logging.getLogger(__name__).warning("full battery notice replay not started: worker launch unavailable: %s", error)

    def _supersede_other_queued(self, successor: dict[str, Any]) -> None:
        for other in self.store.all():
            if other["state"] == "queued" and other["request"]["commit"] != successor["request"]["commit"]:
                self._supersede(other, successor)

    def _enqueue(self, request: dict[str, Any]) -> dict[str, Any]:
        """Requeue incomplete terminal work; keep its linked attempts/history."""
        campaign = self.store.enqueue(request)
        if campaign["state"] == "closed" and campaign.get("result", {}).get("result") != "superseded" and not _full_sampled(campaign.get("result", {})):
            campaign.update(state="queued")
            return self.store.write(campaign["campaign_id"], campaign, expected_version=campaign["version"], reason="retry incomplete campaign; terminal attempt retained")
        return campaign

    def _request(self) -> dict[str, Any]:
        """The request is the master commit: one commit, one campaign."""
        tree = read_tree(self.root, "master")
        return {"commit": tree.commit, "tree": tree.tree, "manifest_sha256": tree.manifest_sha256}

    def _last_success(self) -> dict[str, Any] | None:
        successes = [row for row in self.store.all() if _full_clean(row.get("result", {})) and row["state"] == "closed"]
        return max(successes, key=lambda row: row["finished_at"]) if successes else None

    def list_campaigns(self, finished_after: object = None, limit: object = 50) -> dict[str, Any]:
        """Read closed campaign records, oldest first by finish time; changes nothing.

        Both arguments arrive straight from a caller's JSON, so they are checked
        here rather than trusted to be a string and an integer.
        ``finished_after`` is an exclusive ISO-8601 cursor with a timezone. A page
        that stops short sets ``truncated`` and returns the cursor for the next
        call. The cursor is a timestamp, so two campaigns that finish in the same
        microsecond could straddle a page boundary; the stored timestamps carry
        microseconds, which makes that a theoretical case.
        """
        count = _page_limit(limit)
        after = _cursor(finished_after)
        rows = [row for row in _closed_rows(self.store.all()) if after is None or datetime.fromisoformat(row["finished_at"]) > after]
        page = rows[:count]
        truncated = len(rows) > count
        return {"campaigns": page, "truncated": truncated, "next_finished_after": page[-1]["finished_at"] if truncated else None}

    def _background(self) -> None:
        """Drain the queue: after each run take the next campaign; after the slot is released, look once more."""
        while True:
            try:
                ran = self.run_due()
            finally:
                self._slot.release()
            if ran == [{"status": "already_running"}]:
                return
            if not (self._queued() and self._slot.acquire(blocking=False)):
                return

    def _queued(self) -> bool:
        return any(row["state"] in ("queued", "running") for row in self.store.all())

    def run_due(self) -> list[dict[str, Any]]:
        """Replay due notices, then run at most one queued or interrupted campaign; injectable in tests."""
        with self.store.exclusion() as acquired:
            if not acquired:
                return [{"status": "already_running"}]
            sweep_exports(self.exports_root)
            campaigns = self.store.all()
            for row in campaigns:
                if row["state"] == "closed":
                    self._deliver_notice(row)
            queued = [row for row in campaigns if row["state"] in ("queued", "running")]
            return [self._execute(min(queued, key=lambda row: row["queued_at"]))] if queued else []

    def _execute(self, campaign: dict[str, Any]) -> dict[str, Any]:
        campaign = self._refresh_request(campaign)
        if campaign["state"] == "closed":
            return campaign
        campaign = self._begin_attempt(campaign)
        try:
            result = self.runner(campaign, self.store.root)
        except Exception as error:
            result = {"result": "interrupted", "exit_code": None, "advisory": str(error)}
        if not _full_clean(result):
            prior = result.get("advisory")
            result = {**result, "advisory": f"{prior}; {_FAILURE_ADVISORY}" if prior else _FAILURE_ADVISORY}
        campaign.update(state="closed", finished_at=utc_now(), result=result)
        if _failed_complete(result):
            # Due is part of the closing write, so a crash before the send leaves work to replay, never a silent gap.
            campaign["notice"] = {"status": "due", "recipient": self._recipient(), "attempts": 0}
        campaign["attempts"][-1].update(result=result, finished_at=campaign["finished_at"])
        closed = self.store.write(campaign["campaign_id"], campaign, expected_version=campaign["version"], reason="terminal full battery observation")
        return self._deliver_notice(closed)

    def _recipient(self) -> str:
        """The solet's main seat, named as ananta's deploy-by-rule names it for its own notices."""
        return f"{self.solet_name.title()}-Main"

    def _deliver_notice(self, campaign: dict[str, Any]) -> dict[str, Any]:
        """Send a due notice and journal the outcome. Only the notice is rewritten: the campaign's verdict never is.

        Delivery is at-least-once: a crash between a stored send and its journal write sends again on replay,
        which is why the campaign id travels in the message as its dedupe key.
        """
        notice = campaign.get("notice")
        if self.notifier is None or notice is None or notice.get("status") != "due":
            return campaign
        tried_at = utc_now()
        try:
            receipt = self.notifier(notice["recipient"], _notice_text(self.solet_name, campaign))
            message_id = receipt.get("message_id")
            if not isinstance(message_id, str) or not message_id.strip():
                raise ValueError("send receipt carried no durable message_id")
        except Exception as error:
            logging.getLogger(__name__).warning("full battery notice for %s stays due: %s", campaign["campaign_id"], error)
            campaign["notice"] = {**notice, "attempts": notice["attempts"] + 1, "last_attempt_at": tried_at, "advisory": f"{type(error).__name__}: {error}"[:240]}
        else:
            sent = {key: value for key, value in notice.items() if key != "advisory"}
            campaign["notice"] = {**sent, "status": "sent", "attempts": notice["attempts"] + 1, "last_attempt_at": tried_at, "sent_at": utc_now(), "message_id": message_id, "delivery": receipt.get("delivery"), "send_status": receipt.get("send_status")}
        return self.store.write(campaign["campaign_id"], campaign, expected_version=campaign["version"], reason="full battery failure notice outcome")

    def _refresh_request(self, campaign: dict[str, Any]) -> dict[str, Any]:
        """Queue delay may advance master; retain the original as superseded."""
        tree = read_tree(self.root, "master")
        if campaign["request"]["commit"] == tree.commit:
            return campaign
        successor = self._enqueue(self._request())
        self._supersede(campaign, successor)
        return successor

    def _supersede(self, old: dict[str, Any], successor: dict[str, Any]) -> None:
        """Close a campaign whose commit is no longer master; the link to its successor stays."""
        old.update(state="closed", finished_at=utc_now(), result={"result": "superseded", "successor_campaign": successor["campaign_id"], "reason": "master advanced while work was queued"})
        if old["attempts"]:
            old["attempts"][-1].update(finished_at=old["finished_at"], result={"result": "interrupted", "reason": "prior worker no longer holds exclusion"})
        self.store.write(old["campaign_id"], old, expected_version=old["version"], reason="superseded queued identity, retained successor link")

    def _begin_attempt(self, campaign: dict[str, Any]) -> dict[str, Any]:
        if campaign["state"] == "running":
            campaign["attempts"][-1].update(result={"result": "interrupted", "advisory": "prior worker no longer holds exclusion"}, finished_at=utc_now())
        previous = self._last_success()
        campaign.update(state="running", attribution={}, attribution_since=previous["finished_at"] if previous else None)
        campaign["attempts"].append({"number": len(campaign["attempts"]) + 1, "started_at": utc_now(), "previous_attempt": len(campaign["attempts"]) or None})
        return self.store.write(campaign["campaign_id"], campaign, expected_version=campaign["version"], reason="start linked full battery attempt")

    def _run_battery(self, campaign: dict[str, Any], directory: Path) -> dict[str, Any]:
        """Run the frozen commit in a detached export: a landing in the live checkout cannot change what executes."""
        label = f"{campaign['campaign_id'][:12]}-{len(campaign['attempts'])}"
        with frozen_export(self.root, campaign["request"]["commit"], self.exports_root, label) as export:
            return self._run_in_export(campaign, directory, export)

    def _run_in_export(self, campaign: dict[str, Any], directory: Path, export: Path) -> dict[str, Any]:
        request = campaign["request"]
        tree = read_tree(export, request["commit"])
        verify_worktree(export, tree, list(tree.entries))
        # The EFFECTIVE register: gate_smokes.txt plus its fragments, so the
        # campaign's entry list and budget cover a fragment-registered smoke
        # that the runner below is going to run (prb_ae6d81ea).
        effective = _load_effective_register(export / _REGISTER_RELPATH)
        entries = effective.entries
        for advisory in effective.advisories:
            logging.getLogger(__name__).warning("%s", advisory)
        # Historical estimates are advisory, never a subprocess kill deadline.
        budget = campaign_budget(default_store_root().parent, registered_count=len(entries), entries=entries)
        output = directory / f"{campaign['campaign_id']}.{len(campaign['attempts'])}.results.json"
        name = f"scheduled:{campaign['campaign_id']}:{len(campaign['attempts'])}"
        principal = "system:quality-full-smoke-producer"
        context = prepare_full_proofs(export, tree.commit, principal=principal, attribution=campaign["attribution"])
        # The runner resolves the fragment directory beside this register path,
        # so naming the main register still runs the whole effective register.
        argv = [str(export / ".venv/bin/python3"), str(export / "quality_gates/run_smokes.py"), "--register", str(export / _REGISTER_RELPATH), "--campaign", name, "--results-output", str(output)]
        bounded = run_bounded(argv, cwd=export, timeout=None, extra_env={"SOLET_NAME": self.solet_name})
        verify_worktree(export, tree, list(tree.entries))
        # Without a published results file the runner's harness defects are
        # unknown: null, never an empty list that would claim none occurred.
        result = json.loads(output.read_bytes()) if output.exists() else {"result": "interrupted", "exit_code": bounded.exit_code, "harness_defects": None}
        import_environment = _campaign_import_environment(default_store_root().parent, name)
        proofs = finish_full_proofs(export, context, result, import_environment, principal=principal)
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
            "proofs": [{"smoke": row["smoke"], "key": row["proof"]["key"], "event_id": row["proof"]["observation_event_id"]} for row in proofs if row["proof"] is not None],
            "cache_ignored": True,
        }


def _full_sampled(result: dict[str, Any]) -> bool:
    """Completed physical samples bound cadence without becoming clean proof."""
    summary = result.get("summary", {})
    total = summary.get("total", 0)
    return result.get("result") in ("passed", "failed", "passed_with_retries") and total > 0 and result.get("registered_count") == total and sum(summary.get(key, 0) for key in ("passed", "failed", "skipped")) == total and not summary.get("missing") and not summary.get("not_run") and not result.get("timed_out")


def _full_clean(result: dict[str, Any]) -> bool:
    summary = result.get("summary", {})
    return result.get("result") == "passed" and result.get("exit_code") == 0 and summary.get("total", 0) > 0 and summary.get("total") == summary.get("passed") and not summary.get("retried_count") and not result.get("timed_out")


def _failed_complete(result: dict[str, Any]) -> bool:
    """A failure worth announcing: every registered smoke was counted, so the failing list is the whole list."""
    return result.get("result") == "failed" and _full_sampled(result)


def _page_limit(limit: object) -> int:
    """A page size from 1 to 100; a boolean is an integer to Python but not a count."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 to 100")
    return limit


def _closed_rows(campaigns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every closed campaign as a row, oldest first; each row learns the finish of the clean one before it.

    A campaign put back in the queue keeps its old ``finished_at`` but is no longer closed, so the state is
    read, not the timestamp alone.
    """
    closed = [row for row in campaigns if row.get("state") == "closed" and row.get("finished_at")]
    closed.sort(key=lambda row: (datetime.fromisoformat(row["finished_at"]), row["campaign_id"]))
    rows: list[dict[str, Any]] = []
    previous_clean: str | None = None
    for campaign in closed:
        rows.append(_campaign_row(campaign, previous_clean))
        if rows[-1]["clean"]:
            previous_clean = campaign["finished_at"]
    return rows


def _cursor(finished_after: object) -> datetime | None:
    """Parse the exclusive page cursor; a timestamp without a timezone has no single meaning."""
    if finished_after is None:
        return None
    if not isinstance(finished_after, str):
        raise ValueError("finished_after must be an ISO-8601 timestamp string")
    try:
        moment = datetime.fromisoformat(finished_after)
    except ValueError as error:
        raise ValueError(f"finished_after is not an ISO-8601 timestamp: {finished_after!r}") from error
    if moment.tzinfo is None:
        raise ValueError("finished_after must carry a timezone")
    return moment


def _campaign_row(campaign: dict[str, Any], previous_clean: str | None) -> dict[str, Any]:
    """One closed campaign as the read verb reports it; ``filing_status`` is only what the outbox last recorded.

    ``notice`` is present only on a campaign that carries one: a complete failure's same-day notice to the
    main seat, ``due`` until a send is stored (its message id and time then show here), ``sent`` after.
    """
    result = campaign.get("result") or {}
    request = campaign.get("request") or {}
    row = {
        "campaign_id": campaign["campaign_id"],
        "marker": f"smoke-campaign-{campaign['campaign_id']}",
        "request": {key: request.get(key) for key in ("commit", "tree")},
        "finished_at": campaign["finished_at"],
        "outcome": result.get("result"),
        "clean": _full_clean(result),
        "superseded": result.get("result") == "superseded",
        "summary": result.get("summary"),
        "results_sha256": result.get("results_sha256"),
        "previous_clean_finished_at": previous_clean,
        "filing_status": (campaign.get("filing") or {}).get("status"),
    }
    if campaign.get("notice") is not None:
        row["notice"] = campaign["notice"]
    return row


def _campaign_import_environment(directory: Path, campaign: str) -> dict[str, Any]:
    for path in directory.glob("*.json"):
        try:
            receipt = json.loads(path.read_bytes())
        except (OSError, ValueError):
            continue
        if receipt.get("campaign") == campaign:
            return receipt.get("import_environment", {})
    return {"state": "advisory", "advisory": "physical runner import-environment receipt unavailable"}


def _notice_text(solet_name: str, campaign: dict[str, Any]) -> str:
    """The message both transports carry. A failing list the results do not hold is null, never rebuilt."""
    result = campaign["result"]
    failing = result.get("summary", {}).get("failing_smokes")
    return json.dumps(
        {
            "event": "full_battery_failed",
            "solet": solet_name,
            "campaign_id": campaign["campaign_id"],
            "commit": campaign["request"].get("commit"),
            "finished_at": campaign["finished_at"],
            "failing_smokes": failing if isinstance(failing, list) else None,
            "results_sha256": result.get("results_sha256"),
            "read_with": _READ_VERB,
            "duplicate_semantics": "campaign_id is the dedupe key: a repeat of this campaign_id is the same failure, not a new one.",
        },
        sort_keys=True,
    )


def bridge_notifier(repo_root: Path, solet_name: str) -> Notifier:
    """The installed transport: the registered peer_send_by_name process, called through this checkout's bridge.

    It is the same queued route the scheduler and deploy-by-rule use for their notices, so the sender is the
    platform's system sender: a producer thread is not a session and holds no session identity. A deploy can hold
    that route for minutes; a send it holds past the timeout stays due and is retried.
    """
    bridge = repo_root / ".venv" / "bin" / "solet-bridge"

    def notify(recipient: str, text: str) -> dict[str, Any]:
        argv = [str(bridge), "call", PEER_SEND_BY_NAME, json.dumps({"name": recipient, "content": text}), "--wait"]
        done = subprocess.run(argv, cwd=repo_root, env={**os.environ, "SOLET_NAME": solet_name}, capture_output=True, text=True, timeout=_NOTICE_TIMEOUT_SECONDS, check=False)
        return _peer_receipt(done)

    return notify


def _peer_receipt(done: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """The send's receipt from the bridge's outer envelope. Only stdout is read: stderr carries progress lines."""
    try:
        outer = json.loads(done.stdout)
        result = outer["result"]
    except (ValueError, KeyError, TypeError) as error:
        raise RuntimeError(f"bridge exited {done.returncode} with no readable envelope: {(done.stderr or done.stdout)[-160:]!r}") from error
    if done.returncode != 0 or outer.get("status") != "completed" or result.get("error") or result.get("success") is False:
        code = (result.get("error") or {}).get("code")
        raise RuntimeError(f"peer_send_by_name not completed (exit {done.returncode}): {code or outer.get('status')}")
    return result["data"]
