"""Claude and Codex plugin installation operations."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import cast

from .setup_adapter_contract import AdapterRequest, JsonObject, JsonValue, planned_action, result
from .setup_adapter_runtime import CommandOutcome, Runtime, read_json_object, resolve_executable
from .setup_operations import (
    _CLAUDE_HOOKS,
    _CODEX_HOOKS,
    _apply_outcome,
    _blocked,
    _failed_outcome,
    _verified,
)


def plugin_install(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    is_claude = request.operation_ref == "hydration::claude.install_plugin"
    cli = "claude" if is_claude else "codex"
    executable = resolve_executable(runtime, cli)
    if executable is None:
        return _missing_cli(request, cli)
    marketplace = request.name.replace("_", "-")
    manifest = request.target / (_CLAUDE_HOOKS if is_claude else _CODEX_HOOKS)
    selector = f"coordination-hooks@{marketplace}"
    if request.phase == "probe":
        return _plugin_install_probe(
            request, runtime, cli, executable, marketplace, selector, manifest
        )
    patch_error = _patch_hook_manifest(manifest, request.target, runtime)
    if patch_error is not None:
        return _blocked(request, "hook_manifest_invalid", patch_error)
    add = runtime.run(
        (executable, "plugin", "marketplace", "add", str(request.target)),
        timeout_seconds=request.timeout_seconds,
        cwd=request.target,
    )
    if not add.ok:
        return _failed_outcome(request, add, f"{cli}_marketplace_failed")
    install_vector = (
        (executable, "plugin", "install", selector)
        if is_claude
        else (executable, "plugin", "add", selector)
    )
    installed = runtime.run(
        install_vector, timeout_seconds=request.timeout_seconds, cwd=request.target
    )
    return _apply_outcome(request, installed, f"{cli}_plugin_install_failed")


def _plugin_install_probe(
    request: AdapterRequest,
    runtime: Runtime,
    cli: str,
    executable: str,
    marketplace: str,
    selector: str,
    manifest: Path,
) -> JsonObject:
    list_vector = plugin_list_vector(cli, marketplace, executable)
    listed = runtime.run(list_vector, timeout_seconds=10)
    if command_output_truncated(listed):
        return _blocked(request, "output_truncated", truncated_output_repair(list_vector, listed))
    rows = plugin_list_rows(cli, listed)
    visible = rows is not None and any(plugin_row_visible(cli, row, selector) for row in rows)
    interpreter_ok = _manifest_has_absolute_python(manifest, request.target)
    if visible and interpreter_ok:
        return _verified(request, f"{cli}_plugin", f"{cli} plugin is visible and bound", str(manifest))
    if request.probe_purpose == "post_apply":
        return _blocked(
            request,
            "plugin_not_visible" if not visible else "hook_manifest_invalid",
            post_apply_repair(list_vector, listed, selector, visible),
        )
    return result(
        request,
        status="pending",
        actions=_plugin_actions(request, cli, selector, manifest),
        repair=f"Approve the {cli} CLI changes.",
    )


def _plugin_actions(
    request: AdapterRequest,
    cli: str,
    selector: str,
    manifest: Path,
) -> list[JsonObject]:
    return [
        planned_action(
            action_id=f"{cli}.patch_hook_interpreter",
            title=f"Bind every {cli} Python hook to target Python 3.13",
            mutation_kind="plugin_manifest_write",
            target=str(manifest),
            evidence_ref="hook_interpreter_not_absolute",
        ),
        planned_action(
            action_id=f"{cli}.register_marketplace",
            title=f"Register the {cli} coordination marketplace",
            mutation_kind="plugin_marketplace_write",
            target=str(request.target),
            evidence_ref="plugin_not_visible",
        ),
        planned_action(
            action_id=f"{cli}.install_coordination_hooks",
            title=f"Install and enable {selector}",
            mutation_kind="plugin_install",
            target=selector,
            evidence_ref="plugin_not_visible",
        ),
    ]


def plugin_list_vector(cli: str, marketplace: str, executable: str) -> tuple[str, ...]:
    """Return the narrowest JSON list query each coding-agent CLI supports."""
    if cli == "codex":
        return (executable, "plugin", "list", "-m", marketplace, "--json")
    return (executable, "plugin", "list", "--json")


def _missing_cli(request: AdapterRequest, cli: str) -> JsonObject:
    return result(
        request,
        status="blocked",
        error_kind=f"{cli}_cli_missing",
        retry_safe=True,
        reason={
            "outcome_class": "executable_missing",
            "exit_code": None,
            "duration_ms": 0,
            "timed_out": False,
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "stdout_truncated": False,
            "stderr_truncated": False,
        },
        repair=(
            f"Install the selected {cli} CLI through its provisioning operation, then run a fresh preview."
        ),
    )


def command_output_truncated(outcome: CommandOutcome) -> bool:
    return outcome.stdout_truncated or outcome.stderr_truncated


def truncated_output_repair(vector: tuple[str, ...], outcome: CommandOutcome) -> str:
    command = " ".join(vector)
    return (
        f"{command} output was truncated: stdout {outcome.stdout_bytes} bytes "
        f"(truncated={outcome.stdout_truncated}), stderr {outcome.stderr_bytes} bytes "
        f"(truncated={outcome.stderr_truncated})."
    )


def post_apply_repair(
    vector: tuple[str, ...],
    outcome: CommandOutcome,
    selector: str,
    visible: bool,
) -> str:
    command = " ".join(vector)
    observation = (
        f"plugin {selector!r} not listed by {command}"
        if not visible
        else f"plugin {selector!r} listed by {command}, but its hook interpreter is not bound"
    )
    return (
        f"Post-apply probe observed {observation}; stdout {outcome.stdout_bytes} bytes, "
        f"truncated={outcome.stdout_truncated}; stderr {outcome.stderr_bytes} bytes, "
        f"truncated={outcome.stderr_truncated}."
    )


def plugin_list_rows(cli: str, outcome: CommandOutcome) -> list[JsonObject] | None:
    """Parse a complete coding-agent JSON list without accepting partial output."""
    if not outcome.ok or command_output_truncated(outcome):
        return None
    try:
        payload: object = json.loads(outcome.stdout)
    except json.JSONDecodeError:
        return None
    raw_rows = payload.get("installed") if cli == "codex" and isinstance(payload, dict) else payload
    if not isinstance(raw_rows, list):
        return None
    return [cast(JsonObject, row) for row in raw_rows if isinstance(row, dict)]


def plugin_row_visible(cli: str, row: JsonObject, selector: str) -> bool:
    if cli == "codex":
        return (
            row.get("pluginId") == selector
            and row.get("installed") is True
            and row.get("enabled") is True
        )
    return row.get("id") == selector and row.get("enabled") is True


def _manifest_has_absolute_python(path: Path, target: Path) -> bool:
    manifest = read_json_object(path)
    if manifest is None:
        return False
    expected = str(target / ".venv/bin/python3")
    commands = _hook_commands(manifest)
    python_commands = [
        command for command in commands if command.endswith("python3") or "python3 " in command
    ]
    return bool(python_commands) and all(
        command.startswith(expected) for command in python_commands
    )


def _patch_hook_manifest(path: Path, target: Path, runtime: Runtime) -> str | None:
    manifest = read_json_object(path)
    if manifest is None:
        return f"hook manifest is absent or invalid: {path}"
    expected = str(target / ".venv/bin/python3")
    if not isinstance(manifest.get("hooks"), dict):
        return "hook manifest lacks the hooks object"
    changed = False
    for hook in _hook_records(manifest):
        command = cast(str, hook["command"])
        replacement = _absolute_python_command(command, expected)
        if replacement is not None:
            hook["command"] = replacement
            changed = True
    if changed:
        runtime.atomic_write(path, json.dumps(manifest, indent=2) + "\n", mode=0o644)
    return None


def _hook_commands(manifest: JsonObject) -> list[str]:
    return [cast(str, hook["command"]) for hook in _hook_records(manifest)]


def _hook_records(manifest: JsonObject) -> list[JsonObject]:
    hooks = manifest.get("hooks")
    records: list[JsonObject] = []
    if not isinstance(hooks, dict):
        return records
    for event_entries in hooks.values():
        if not isinstance(event_entries, list):
            continue
        for entry in event_entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                continue
            for hook in cast(list[JsonValue], entry["hooks"]):
                if isinstance(hook, dict) and isinstance(hook.get("command"), str):
                    records.append(hook)
    return records


def _absolute_python_command(command: str, expected: str) -> str | None:
    if command == "python3":
        return expected
    if command.startswith("python3 "):
        return shlex.quote(expected) + command[len("python3") :]
    return None
