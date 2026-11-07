"""LaunchAgent restart behavior regression exercised by executable hydration."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from github_midwife_plugin.setup_adapter_contract import AdapterRequest, JsonObject
from github_midwife_plugin.setup_adapter_runtime import CommandOutcome, Runtime


def _first_evidence_observed(result_value: Mapping[str, object]) -> str | None:
    evidence_items = result_value.get("evidence")
    if not isinstance(evidence_items, list) or not evidence_items:
        return None
    first = evidence_items[0]
    if not isinstance(first, dict):
        return None
    observed = cast(dict[str, object], first).get("observed")
    return observed if isinstance(observed, str) else None


def run_launchagent_restart_regression(
    target: Path,
    runtime: Any,
    *,
    request: Callable[..., AdapterRequest],
    dispatch_request: Callable[[AdapterRequest, Runtime], JsonObject],
    command_outcome: Callable[..., CommandOutcome],
    check: Callable[[object, str], None],
) -> None:
    """Retain clean, crash-loop, malformed, and signal-restart assertions."""
    autostart = request(
        target,
        operation_id="launchagent_running",
        operation_ref="genesis::autostart.verify",
        probe_purpose="completion",
        public_inputs={"autostart": "enabled"},
    )
    command = ("/bin/launchctl", "print", f"gui/{os.getuid()}/local.solet.iris")
    runtime.responses[command] = command_outcome(
        0, False, 1, "\tstate = running\n\truns = 1\n\tlast exit code = 0\n", ""
    )
    check(
        dispatch_request(autostart, runtime)["checkpoint_status"] == "verified",
        "running LaunchAgent with one clean start verifies",
    )
    runtime.responses[command] = command_outcome(
        0,
        False,
        1,
        "\tstate = running\n\truns = 1\n\tpid = 8814\n\tlast exit code = (never exited)\n",
        "",
    )
    never_exited = dispatch_request(autostart, runtime)
    check(
        never_exited["checkpoint_status"] == "verified"
        and _first_evidence_observed(never_exited)
        == "state=running,last_exit=(never exited),runs=1",
        "receipt-128 running LaunchAgent that never exited verifies",
    )
    runtime.responses[command] = command_outcome(
        0,
        False,
        1,
        "\tstate = running\n\truns = 2\n\tpid = 8814\n"
        "\tlast terminating signal = Killed: 9\n",
        "",
    )
    signal_terminated = dispatch_request(autostart, runtime)
    check(
        signal_terminated["checkpoint_status"] == "verified",
        "running LaunchAgent after kickstart SIGKILL restart verifies",
    )
    runtime.responses[command] = command_outcome(
        0, False, 1, "\tstate = running\n\truns = 4\n\tlast exit code = 1\n", ""
    )
    crash_loop = dispatch_request(autostart, runtime)
    check(
        crash_loop["checkpoint_status"] == "blocked"
        and crash_loop["error_kind"] == "launchagent_crash_loop",
        "loaded crash-looping LaunchAgent blocks with a distinct error kind",
    )
    runtime.responses[command] = command_outcome(
        0, False, 1, "\tstate = running\n\truns = 1\n\tlast exit code = unavailable\n", ""
    )
    unreadable = dispatch_request(autostart, runtime)
    check(
        unreadable["checkpoint_status"] == "blocked"
        and unreadable["error_kind"] == "launchagent_status_unreadable",
        "unrecognized launchctl last-exit form remains unreadable",
    )
    runtime.responses[command] = command_outcome(
        0,
        False,
        1,
        "\tstate = waiting\n\truns = 2\n\tpid = 8814\n"
        "\tlast terminating signal = Killed: 9\n",
        "",
    )
    nonrunning = dispatch_request(autostart, runtime)
    check(
        nonrunning["checkpoint_status"] == "blocked"
        and nonrunning["error_kind"] == "launchagent_status_unreadable",
        "nonrunning LaunchAgent with signal history remains blocked",
    )
    runtime.responses[command] = command_outcome(
        0,
        False,
        1,
        "\tstate = running\n\truns = 2\n\tpid = 8814\n"
        "\tlast terminating signal = unavailable\n",
        "",
    )
    malformed_signal = dispatch_request(autostart, runtime)
    check(
        malformed_signal["checkpoint_status"] == "blocked"
        and malformed_signal["error_kind"] == "launchagent_status_unreadable",
        "unrecognized terminating signal remains unreadable",
    )
