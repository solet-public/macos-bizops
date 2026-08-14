# pyright: reportUnusedFunction=false
"""Shared configuration and identity helpers for managed Codex drivers."""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# Hoisted to the runner-neutral module (2026-08-14, registration-loss RCA):
# these were never Codex-specific, and every spawned CLAUDE worker needed the
# identical treatment -- that asymmetry is what left Claude workers
# unregistered. Aliased to the established private names so no Codex call site
# moves; `resolve_solet_bin` is now imported from `solet_cli` directly by the
# drivers that need it, since a re-export through here reads as ownership it
# no longer has.
from .solet_cli import expose_worker_cli as _expose_worker_cli
from .solet_cli import worker_path as _worker_path

_CODEX_AGENT_ID = "codex"
_COORDINATION_PLUGIN_PREFIX = "coordination-hooks@"
_DEFAULT_RPC_TIMEOUT_SECONDS = 30.0
_DEFAULT_TMUX_VERIFY_TIMEOUT_SECONDS = 10.0
_DEFAULT_TMUX_POLL_INTERVAL_SECONDS = 0.25
_DEFAULT_TMUX_STABLE_SAMPLES = 3
_MIN_TMUX_VERSION = (3, 3)
_MCP_ROLE_AUTOBIND_ENV = "AGENT_ROLE_AUTOBIND"
_CODEX_PARENT_SESSION_ENV = "CODEX_THREAD_ID"
_CLAUDE_ONLY_ENV_PREFIXES = (
    "ANTHROPIC_",
    "AWS_",
    "CLAUDE_CODE_",
    "CLOUD_ML_",
    "GOOGLE_",
    "VERTEX_",
)
_CLAUDE_ONLY_ENV_NAMES = {
    "CLAUDECODE",
    "FLEET_HEADLESS_PERMISSION_MODE",
    "FLEET_HEADLESS_TOOL_ALLOWLIST",
}
_CLAUDE_PROVIDER_ENV_NAMES = {
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_USE_VERTEX",
}
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_TOML_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
_TMUX_BUSY_MARKERS = (
    "Starting MCP servers",
    "Working",
    "esc to interrupt",
)


def _toml_string(value: str) -> str:
    """JSON string syntax is valid TOML basic-string syntax for this input."""
    return json.dumps(value)


def _codex_home(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    configured = os.environ.get("CODEX_HOME", "").strip()
    return Path(configured) if configured else Path.home() / ".codex"


def _read_codex_config(config_path: Path) -> dict[str, Any]:
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data


def _coordination_plugin_enabled(config: Mapping[str, object]) -> bool:
    plugins = config.get("plugins")
    if not isinstance(plugins, Mapping):
        return False
    return any(
        str(name).startswith(_COORDINATION_PLUGIN_PREFIX)
        and isinstance(settings, Mapping)
        and settings.get("enabled") is True
        for name, settings in plugins.items()
    )


def _mcp_server_config(
    config: Mapping[str, object], solet_name: str,
) -> Mapping[str, object] | None:
    servers = config.get("mcp_servers")
    if not isinstance(servers, Mapping):
        return None
    server = servers.get(solet_name)
    return server if isinstance(server, Mapping) else None


def _identity_env(
    *, agent_instance_id: str, agent_session_id: str, label: str,
    solet_name: str, solet_bin: str, transport: str,
) -> dict[str, str]:
    env = dict(os.environ)
    # A managed worker is a new logical Codex session, never a continuation of
    # the operator process that happened to launch it.  Claude-only provider
    # and host policy variables are equally outside the Codex runtime contract.
    for key in tuple(env):
        if (
            key == _CODEX_PARENT_SESSION_ENV
            or key in _CLAUDE_ONLY_ENV_NAMES
            or key.startswith(_CLAUDE_ONLY_ENV_PREFIXES)
        ):
            env.pop(key)
    env.update(
        {
            "SOLET_NAME": solet_name,
            "AGENT_IDENTITY": _CODEX_AGENT_ID,
            "AGENT_INSTANCE_ID": agent_instance_id,
            "AGENT_SESSION_ID": agent_session_id,
            "AGENT_SESSION_LABEL": label,
            "FLEET_TRANSPORT": transport,
        },
    )
    _expose_worker_cli(env, solet_bin)
    # role_name is intentionally absent.  spawn_session.role_name authorizes
    # a later claim; it does not itself bind a role or grant AGENT_ROLE.
    env.pop("AGENT_ROLE", None)
    return env


def _parent_runtime_env_names_to_unset() -> list[str]:
    """Names that a pre-existing tmux server must not leak into Codex."""
    inherited = {
        key
        for key in os.environ
        if key in _CLAUDE_ONLY_ENV_NAMES
        or key.startswith(_CLAUDE_ONLY_ENV_PREFIXES)
    }
    inherited.update(_CLAUDE_PROVIDER_ENV_NAMES)
    inherited.update({_CODEX_PARENT_SESSION_ENV, "AGENT_ROLE"})
    return sorted(inherited)


def _without_parent_runtime_env(argv: list[str]) -> list[str]:
    command = ["env"]
    for key in _parent_runtime_env_names_to_unset():
        command += ["-u", key]
    return [*command, *argv]


def _codex_config_overrides(
    *, config: Mapping[str, object], solet_name: str, transport: str,
    agent_instance_id: str, agent_session_id: str, label: str, solet_bin: str,
) -> list[str]:
    """Return Codex-native ``-c`` overrides for the selected transport."""
    overrides = [
        "shell_environment_policy.set.PATH="
        f"{_toml_string(_worker_path(solet_bin))}",
    ]
    server = _mcp_server_config(config, solet_name)
    if server is None:
        return overrides
    prefix = f"mcp_servers.{solet_name}"
    if transport == "watch":
        return [*overrides, f"{prefix}.enabled=false"]
    identity = {
        "SOLET_NAME": solet_name,
        "AGENT_IDENTITY": _CODEX_AGENT_ID,
        "AGENT_INSTANCE_ID": agent_instance_id,
        "AGENT_SESSION_ID": agent_session_id,
        "AGENT_SESSION_LABEL": label,
        "AGENT_WAKE_CLI": solet_bin or "solet",
        "FLEET_TRANSPORT": transport,
        # A managed-session lane label is cosmetic.  Role ownership remains a
        # model-initiated peer_claim_role action after the bootstrap turn.
        _MCP_ROLE_AUTOBIND_ENV: "0",
    }
    overrides.append(f"{prefix}.enabled=true")
    overrides.extend(
        f"{prefix}.env.{key}={_toml_string(value)}"
        for key, value in identity.items()
    )
    return overrides


def _refuse_claude_provider_overlay(spec: Mapping[str, object]) -> None:
    from .session_hosts import HostCannotSpawnError  # noqa: PLC0415

    if str(spec.get("provider") or "").strip() or spec.get("provider_env"):
        raise HostCannotSpawnError(
            "provider_unsupported_for_runtime: agent_runtime='codex' does not "
            "accept Claude provider selection or provider_env overlays; omit "
            "provider, or add a separately reviewed Codex-native provider contract.",
        )


def _command_succeeded(result: object) -> bool:
    return int(getattr(result, "returncode", 1)) == 0
