"""Bounded native register client for full-battery attribution and Problems.

Every mutation has a readback. Campaign markers reconcile response loss before
another filing. Investigation links attribute exposure, not causal blame.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def _rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return value
    for field in ("items", "rows", "records", "memberships", "events", "links"):
        if isinstance(value.get(field), list):
            return value[field]
    raise ValueError("native list has no measured row collection")


_REPOSITORY_ID = "rep_7111540c-ac4a-468c-8f84-b67d8756d1d0"
_CONTROLLER_REPOSITORIES = (_REPOSITORY_ID, "rep_571bd4f0-51ae-47c6-9fe4-e399608b9e2a")
_PROBLEM_CAUSE_TEXT = "Unproven: the retained failed campaign records the measurement; investigation will establish the cause."
_INVESTIGATION_RATIONALE = "Campaign exposure requires investigation; causal root cause unproven"


class BatteryRegister:
    """Shell-free Project Solet reads/writes with retained native receipts."""

    def __init__(self, solet_name: str, agent_instance_id: str) -> None:
        self.cli = os.environ.get("PROJECT_SOLET_CLI", str(Path.home() / "Workspace/project-solet/.venv/bin/psolet"))
        self.solet_name = solet_name
        self.agent_instance_id = agent_instance_id
        self.receipts: list[dict[str, Any]] = []

    def _call(self, *arguments: str) -> Any:
        env = os.environ.copy()
        if not env.get("PROJECT_SOLET_DATABASE_DSN"):
            secret = subprocess.run(("security", "find-generic-password", "-s", "project-solet.client.dsn", "-a", "project_solet", "-w"), capture_output=True, check=False, timeout=10)
            if secret.returncode:
                raise RuntimeError("native register credential unavailable")
            env["PROJECT_SOLET_DATABASE_DSN"] = secret.stdout.decode().strip()
        try:
            result = subprocess.run((self.cli, *arguments), env=env, capture_output=True, text=True, check=False, timeout=60)
        except subprocess.TimeoutExpired as error:
            self.receipts.append({"argv": [self.cli, *arguments], "exit_code": None, "timed_out": True, "stdout": _output(error.stdout), "stderr": _output(error.stderr)})
            raise
        self.receipts.append({"argv": [self.cli, *arguments], "exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
        if result.returncode:
            raise RuntimeError(f"native register call failed: {arguments[:3]}: {result.stderr}")
        value, end = json.JSONDecoder().raw_decode(result.stdout.lstrip())
        suffix = result.stdout.lstrip()[end:]
        if suffix.strip():
            raise ValueError("unexpected native JSON suffix")
        return value

    def _actor(self) -> tuple[str, ...]:
        return ("--solet", self.solet_name, "--agent-instance-id", self.agent_instance_id, "--session-name", "quality-full-smoke-producer")

    def landings(self, since: str | None) -> dict[str, Any]:
        arguments = ("--since", since) if since else ()
        landings = self._call("db", "landing", "list", "--repository-id", _REPOSITORY_ID, *arguments)
        changes: set[str] = set()
        for landing in landings:
            detail = self._call("db", "landing", "show", landing["landing_id"])
            for unit in detail["unit_ids"]:
                cursor: str | None = None
                while True:
                    arguments = ("--change-cursor", cursor) if cursor else ()
                    evidence = self._call("db", "unit", "evidence", unit, "--limit", "100", *arguments)
                    changes.update(row["change_id"] for row in evidence["changes"]["rows"])
                    cursor = evidence["changes"]["next_cursor"]
                    if cursor is None:
                        break
        return {"landings": landings, "change_ids": sorted(changes)}

    def controller_busy(self) -> dict[str, Any]:
        """Yield scheduled F to native Controller-admitted work in either repo.

        Unknown queue evidence is advisory. Actual receipt locks are checked
        independently by the worker before it starts a full battery.
        """
        queues: list[dict[str, Any]] = []
        for repository in _CONTROLLER_REPOSITORIES:
            queue = self._call("db", "landing-queue", "show", "--repository-id", repository)
            queues.append(queue)
            admitted = [row["unit_id"] for row in queue["entries"] if row["eligible"]]
            if admitted:
                return {"busy": True, "admitted_units": admitted, "queues": queues, "basis": "positive eligible native landing-queue entry"}
        return {"busy": False, "admitted_units": [], "queues": queues, "basis": "current native landing-queue entries"}

    def reconcile(self, marker: str) -> dict[str, str] | None:
        matches = self._issue_matches(marker)
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError("campaign marker has duplicate issues; reconciliation required")
        issue_id = matches[0].get("issue_id") or matches[0]["id"]
        issue = self._call("db", "issue", "show", "--issue", issue_id)
        placement = issue.get("parent_state") or {}
        if placement.get("parent_kind") != "problem" or placement.get("state") != "open":
            raise ValueError("campaign issue has no active typed Problem")
        problem_id = placement["parent_id"]
        problem = self._call("db", "problem", "show", "--parent", problem_id)
        if problem["parent"]["id"] != problem_id or problem["parent"]["lifecycle_state"] != "open":
            raise ValueError("campaign issue typed Problem readback is not open")
        return {"issue_id": issue_id, "problem_id": problem_id}

    def _issue_matches(self, marker: str) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._call("db", "issue", "list", "--repository-id", _REPOSITORY_ID, "--search", marker, "--limit", "100", "--offset", str(offset))
            rows = _rows(page)
            matches.extend(row for row in rows if marker in row["title"])
            if len(rows) < 100:
                return matches
            offset += len(rows)

    def file_problem(self, marker: str, detail: str) -> dict[str, str]:
        existing = self.reconcile(marker)
        if existing:
            return existing
        self._call(
            "filed",
            f"Full smoke battery failure {marker}",
            "--repo",
            _REPOSITORY_ID,
            "--component",
            "gates",
            "--category",
            "icat_software_foundations",
            "--core",
            "--urgency",
            "high",
            "--impact",
            "medium",
            "--new-parent-kind",
            "problem",
            "--new-parent-title",
            f"Full smoke failure {marker}",
            "--new-parent-scope",
            "Investigate the retained failed campaign and exposed landing Changes",
            "--new-parent-problem-statement",
            "The retained full smoke campaign failed and exposed landing Changes require investigation",
            "--new-parent-cause-text",
            _PROBLEM_CAUSE_TEXT,
            "--evidence",
            detail,
            *self._actor(),
        )
        observed = self.reconcile(marker)
        if observed is None:
            raise RuntimeError("filing returned without confirmed native issue/Problem")
        return observed

    def attribute_problem(self, problem: Mapping[str, str], marker: str, detail: str, changes: list[str]) -> dict[str, Any]:
        evidence = self._issue_evidence(problem["issue_id"], marker, detail)
        linked: list[dict[str, Any]] = []
        for change in changes:
            linked.append(self._change_evidence(problem["problem_id"], change, marker, detail))
        return {"issue_evidence": evidence, "change_evidence": linked}

    def _issue_evidence(self, issue: str, marker: str, detail: str) -> Any:
        events = self._call("db", "issue", "events", "--issue", issue)
        if not any(detail == row.get("detail") for row in _rows(events)):
            current = self._call("db", "issue", "show", "--issue", issue)
            guard = ("--expected-history-version", str(current["history_version"]))
            self._call("db", "issue", "record-event", "--issue", issue, "--event-kind", "evidence", "--detail", detail, *guard, *self._actor())
        return self._call("db", "issue", "events", "--issue", issue)

    def _change_evidence(self, problem: str, change: str, marker: str, detail: str) -> Any:
        events, frontier = self._change_events(change)
        if not any(detail == row.get("detail") for row in events):
            self._call("db", "change", "event", change, "--detail", detail, "--expected-sequence", str(frontier), *self._actor())
        links, version = self._problem_relations(problem)
        if not any(row["change_id"] == change and row["kind"] == "investigated_by" and row["active"] for row in links):
            self._call(
                "db",
                "problem",
                "relation",
                "add",
                "--parent",
                problem,
                "--repository-id",
                _REPOSITORY_ID,
                "--expected-version",
                str(version),
                "--reason",
                "attribute failed full smoke campaign",
                "--evidence",
                detail,
                "--request-key",
                f"full-smoke-investigation:{marker}:{change}",
                "--key",
                f"full-smoke:{change}",
                "--change",
                change,
                "--kind",
                "investigated_by",
                "--rationale",
                _INVESTIGATION_RATIONALE,
                *self._actor(),
            )
        observed, _ = self._change_events(change)
        readback, _ = self._problem_relations(problem)
        return {"events": observed, "links": readback}

    def _problem_relations(self, problem: str) -> tuple[list[dict[str, Any]], int]:
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        version: int | None = None
        while True:
            arguments = ("--after-key", cursor) if cursor else ()
            page = self._call("db", "problem", "relation", "show", "--parent", problem, "--limit", "100", *arguments)
            if version is not None and page["as_of_version"] != version:
                raise ValueError("typed Problem relation changed during pagination")
            version = page["as_of_version"]
            rows.extend(_rows(page))
            cursor = page["next_cursor"]
            if cursor is None:
                return rows, int(page["as_of_version"])

    def _change_events(self, change: str) -> tuple[list[dict[str, Any]], int]:
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        frontier = 0
        while True:
            arguments = ("--cursor", cursor) if cursor else ()
            page = self._call("db", "change", "events", change, "--limit", "100", *arguments)
            rows.extend(_rows(page))
            frontier = page["frontier_sequence"]
            cursor = page["next_cursor"]
            if cursor is None:
                return rows, frontier


def _output(value: str | bytes | None) -> str:
    return value.decode(errors="replace") if isinstance(value, bytes) else value or ""
