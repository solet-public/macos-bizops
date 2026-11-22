"""Live smoke: create a scratch org from a real Dev Hub through the plugin, then delete it.

Off unless asked for. A run creates a real scratch org and spends one of the
Dev Hub's DailyScratchOrgs, so it runs only when ``SALESFORCE_LIVE_DEV_HUB``
names a Dev Hub the local sf CLI is logged in to (its alias or username).
``quality_gates/run_smokes.py`` builds each smoke's environment from a fixed
allowlist that does not carry that variable, so inside a battery this smoke
always exits 77, the runner's disclosed skip, after printing why. To run it:

    SALESFORCE_LIVE_DEV_HUB=<dev hub alias> SALESFORCE_LIVE_SF_CLI=<path to sf> \\
        SOLET_NAME=<name> .venv/bin/python plugins/salesforce_plugin/tests/live_scratch_org_smoke.py

It drives the plugin's own verbs on its job worker with the real sf CLI and an
in-memory vault: bind_org on the Dev Hub (which must report is_dev_hub),
org_lifecycle create_scratch (an inline Developer definition, one day),
resume_scratch until the org is ready (checked every 20 seconds, at most 15
minutes), display (the org is Active), then delete_scratch. Whatever happens,
a scratch org that was bound is deleted, and an unfinished request is
removed from the Dev Hub. Exit 0 when every step passed, 1 on a failure.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import NonCallableMagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from org_fixtures import STATE, FakeJobs, FakeVault  # noqa: E402
from salesforce_plugin import async_jobs  # noqa: E402
from salesforce_plugin.plugin import SalesforcePlugin  # noqa: E402

SKIP = 77
RESUME_EVERY_SECONDS = 20.0
RESUME_FOR_SECONDS = 900.0


def build_plugin(workspace: Path, sf_cli: str) -> tuple[SalesforcePlugin, FakeJobs]:
    address_book = NonCallableMagicMock()
    address_book.resolve.return_value = {"action_status": "error", "error": {"code": "address_book.not_found"}}
    address_book.list_path_addresses.return_value = {"action_status": "completed", "data": {"addresses": []}}
    orchestrator = NonCallableMagicMock()
    orchestrator.APP_HOME = str(workspace / "profile")
    orchestrator.get_service.return_value = address_book
    orchestrator.config_manager.get_plugin_config.return_value = {"sf_cli_path": sf_cli}
    orchestrator.plugin_manager.get_plugin.return_value = None
    plugin = SalesforcePlugin()
    plugin.orchestrator_ref = orchestrator
    plugin.set_vault_service(FakeVault())
    plugin.prepare_for_readiness()
    jobs = FakeJobs()
    plugin._async_job_manager = jobs  # noqa: SLF001
    return plugin, jobs


def run(plugin: SalesforcePlugin, jobs: FakeJobs, verb: str, params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a verb and run its job on the worker; returns the final job update."""
    queued = getattr(plugin, verb)(params, STATE)
    if queued.get("action_status") != "completed":
        return {"status": "error", "error": queued.get("error")}
    job_id = queued["data"]["job_id"]
    async_jobs._process_job(plugin, jobs, job_id, verb)  # noqa: SLF001
    return jobs.updates[job_id][-1]


def check(label: str, condition: bool, detail: object = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + ("" if condition else f": {detail}"))
    if not condition:
        raise AssertionError(label)


def main() -> int:
    hub = os.environ.get("SALESFORCE_LIVE_DEV_HUB", "").strip()
    if not hub:
        print("SKIP live scratch org smoke: SALESFORCE_LIVE_DEV_HUB is not set (the alias or username of a Dev Hub the local sf CLI is logged in to). No scratch org was created.")
        return SKIP
    sf_cli = os.environ.get("SALESFORCE_LIVE_SF_CLI", "sf").strip() or "sf"
    with tempfile.TemporaryDirectory(prefix="sf_live_scratch_") as temporary:
        plugin, jobs = build_plugin(Path(temporary), sf_cli)
        try:
            return _scratch_round_trip(plugin, jobs, hub)
        except AssertionError:
            return 1


def _scratch_round_trip(plugin: SalesforcePlugin, jobs: FakeJobs, hub: str) -> int:
    bound = run(plugin, jobs, "bind_org", {"target_org": hub, "alias": "live_hub"})
    check("the Dev Hub binds", bound.get("status") == "completed", bound)
    check("the bound org is a Dev Hub", bound["result"]["binding"]["is_dev_hub"] is True, bound["result"]["binding"])
    alias = f"b6live{uuid.uuid4().hex[:8]}"
    created = run(plugin, jobs, "org_lifecycle", {"operation": "create_scratch", "org": "live_hub", "definition": {"orgName": "Build 6 live smoke", "edition": "Developer"}, "flags": {"duration-days": 1, "alias": alias}})
    check("create_scratch is accepted", created.get("status") == "completed", created)
    job_id = created["result"]["job_id"]
    print(f"  INFO  ScratchOrgInfo {job_id} requested from {hub}")
    ready = False
    resumed: dict[str, Any] = {}
    try:
        deadline = time.monotonic() + RESUME_FOR_SECONDS
        while not ready:
            resumed = run(plugin, jobs, "org_lifecycle", {"operation": "resume_scratch", "org": "live_hub", "alias": alias, "flags": {"job-id": job_id}})
            check("resume_scratch answers", resumed.get("status") == "completed", resumed)
            ready = resumed["result"]["ready"] is True
            if not ready:
                check("the org is ready within 15 minutes", time.monotonic() < deadline, resumed["result"])
                time.sleep(RESUME_EVERY_SECONDS)
        check("the ready scratch org is bound as a scratch org", resumed["result"]["binding"]["org_type"] == "scratch", resumed["result"]["binding"])
        shown = run(plugin, jobs, "org_lifecycle", {"operation": "display", "org": alias})
        check("display reports the scratch org Active", shown.get("status") == "completed" and shown["result"]["org_display"].get("status") == "Active", shown)
    finally:
        _clean_up(plugin, jobs, alias, job_id, ready)
    print("All live scratch org steps passed.")
    return 0


def _clean_up(plugin: SalesforcePlugin, jobs: FakeJobs, alias: str, job_id: str, ready: bool) -> None:
    if ready:
        deleted = run(plugin, jobs, "org_lifecycle", {"operation": "delete_scratch", "org": alias})
        check("delete_scratch removes the scratch org and its binding", deleted.get("status") == "completed" and deleted["result"]["deleted"] is True, deleted)
        return
    cancelled = run(plugin, jobs, "authenticated_request", {"org": "live_hub", "method": "DELETE", "path": f"/services/data/v62.0/sobjects/ScratchOrgInfo/{job_id}"})
    print(f"  INFO  removed the unfinished ScratchOrgInfo request: {cancelled.get('result', {}).get('status')}")


if __name__ == "__main__":
    raise SystemExit(main())
