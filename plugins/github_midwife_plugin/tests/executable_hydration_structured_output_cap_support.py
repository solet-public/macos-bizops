"""Structured bridge-output cap regressions for executable hydration."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any


def _public_observed_values(evidence: Any) -> dict[str, Any] | None:
    if not isinstance(evidence, list) or not evidence or not isinstance(evidence[0], dict):
        return None
    observed = evidence[0].get("observed")
    if not isinstance(observed, list) or not all(isinstance(item, str) for item in observed):
        return None
    return {
        key: json.loads(value)
        for item in observed
        if "=" in item
        for key, value in [item.split("=", maxsplit=1)]
    }


def run_knowledge_output_cap(
    target: Path,
    runtime: Any,
    *,
    request: Callable[..., Any],
    check: Callable[[object, str], None],
    command_outcome: Any,
    structured_output_limit: int,
) -> None:
    """Prove a measured over-cap knowledge response remains readable."""

    from github_midwife_plugin.setup_adapter import dispatch_request

    probe = request(
        target,
        operation_id="knowledge_retrieval_succeeds",
        operation_ref="service_interface::knowledge_service.search",
        probe_purpose="completion",
    )
    vector = (
        str(target / ".venv/bin/solet-bridge"),
        "call",
        "service_interface::knowledge_service::search",
        '{"query":"session start orientation","top_k":1}',
    )
    runtime.responses[vector] = command_outcome(
        0,
        False,
        1,
        json.dumps(
            {
                "result": {
                    "success": True,
                    "error": None,
                    "data": {"count": 1, "results": [{"content": "x" * 25_022}]},
                }
            }
        ),
        "",
    )
    result = dispatch_request(probe, runtime)
    check(
        result["checkpoint_status"] == "verified",
        "M-KNOWLEDGE-CAP: over-cap structured knowledge output is green",
    )
    check(
        runtime.command_output_limits[-1] == structured_output_limit,
        "structured knowledge output_limit reaches the production cap",
    )


def run_plugin_roster_output_cap(
    target: Path,
    runtime: Any,
    *,
    request: Callable[..., Any],
    check: Callable[[object, str], None],
    command_outcome: Any,
    default_output_limit: int,
    structured_output_limit: int,
) -> None:
    """Exercise roster capture, truncation, and evidence through FakeRuntime."""

    from github_midwife_plugin.setup_adapter import dispatch_request

    manifest = target / "profile/config/manifest.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    probe = request(
        target,
        operation_id="plugin_roster_matches_plan",
        operation_ref="service_interface::lifecycle_management_service.list_plugins",
        probe_purpose="stage_exit",
    )
    vector = (
        str(target / ".venv/bin/solet-bridge"),
        "call",
        "service_interface::lifecycle_management_service::list_plugins",
        "{}",
    )

    def row(name: str, *, status: str = "ready", padding: str = "") -> dict[str, object]:
        value: dict[str, object] = {
            "name": name,
            "status": status,
            "enabled": True,
            "lifecycle_managed": False,
            "is_running": False,
            "version": "1.2.3",
            "priority": 100,
            "process_count": 1,
        }
        if padding:
            value["metadata"] = padding
        return value

    def roster(rows: list[dict[str, object]]) -> Any:
        payload = {"result": {"success": True, "error": None, "data": {"plugins": rows}}}
        return command_outcome(0, False, 1, json.dumps(payload), "")

    production_rows = [row(f"fixture_plugin_{index:02d}") for index in range(34)]
    manifest.write_text(
        "profile_name: fixture\nplugins:\n"
        + "".join(f"- {entry['name']}\n" for entry in production_rows),
        encoding="utf-8",
    )
    check(
        len(roster(production_rows).stdout.encode("utf-8")) > default_output_limit,
        "production-shaped roster exceeds the default command cap",
    )

    runtime.responses[vector] = roster(production_rows[:33])
    check(
        dispatch_request(probe, runtime)["checkpoint_status"] != "verified",
        "strict-subset roster is non-green",
    )
    runtime.responses[vector] = roster(production_rows + [row("unexpected_plugin")])
    check(
        dispatch_request(probe, runtime)["checkpoint_status"] != "verified",
        "strict-superset roster is non-green",
    )
    runtime.responses[vector] = roster(production_rows)
    exact = dispatch_request(probe, runtime)
    check(
        exact["checkpoint_status"] == "verified",
        "M-ROSTER-STRUCTURED-CAP-4096: exact production roster is green",
    )
    check(
        runtime.command_output_limits[-1] == structured_output_limit,
        "roster output_limit reaches _cap_stream",
    )

    dormant_rows = list(production_rows)
    dormant_rows[-1] = row(str(production_rows[-1]["name"]), status="uninitialized")
    runtime.responses[vector] = roster(dormant_rows)
    check(
        dispatch_request(probe, runtime)["checkpoint_status"] != "verified",
        "unconfigured roster member is non-green",
    )

    runtime.responses[vector] = roster(
        [row("oversized_plugin", padding="x" * structured_output_limit)]
    )
    truncated = dispatch_request(probe, runtime)
    check(
        truncated["error_kind"] == "output_truncated",
        "M-ROSTER-DROP-TRUNCATION-BRANCH: truncated roster is explicit",
    )
    evidence = truncated["evidence"]
    observed_values = _public_observed_values(evidence)
    check(
        observed_values is not None
        and observed_values.get("stdout_bytes", 0) > structured_output_limit,
        "truncated roster carries byte-count evidence",
    )
