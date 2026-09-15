"""Idempotent host-shared LM Studio login registration, never solet-owned."""

from __future__ import annotations

import os
import plistlib
import shlex
from pathlib import Path

from .lm_studio_models import ModelArtifact, cli_path, contained_regular_file
from .lm_studio_settings import settings_path
from .setup_adapter_runtime import Runtime

LABEL = "local.solet.lm-studio"


def login_paths(home: Path) -> tuple[Path, Path]:
    return (
        home / "Library/LaunchAgents" / f"{LABEL}.plist",
        home / "Library/Application Support/Solet/LM Studio/start.sh",
    )


def render_login_agent(home: Path, models: dict[str, ModelArtifact]) -> tuple[str, str]:
    """Render stable host paths and a fixed-vector helper independent of any target.

    Every solet renders identical bytes. Models already provisioned by another
    solet remain eligible on later embedding-only installs. No teardown API is
    provided: operator ruling rul_5d2b3a95 makes this shared host infrastructure.
    """

    _, helper = login_paths(home)
    lms = str(cli_path(home))
    read_jit = shlex.join(("/usr/bin/plutil", "-extract", "justInTimeModelLoading", "raw", "-expect", "bool", "-o", "-", str(settings_path(home))))
    require_jit_off = f'[ "$({read_jit})" = false ]'
    lines = ["#!/bin/sh", "set -eu", *_loaded_state_function(), require_jit_off, shlex.join((lms, "daemon", "up")), shlex.join((lms, "server", "start", "--port", "1234", "--bind", "127.0.0.1")), require_jit_off]
    for role in ("embeddings", "inference"):
        model = models[role]
        path = shlex.quote(str(model.path(home)))
        lines.extend((f'if [ -f {path} ] && [ "$(/usr/bin/stat -f %z {path})" = {model.size_bytes} ]; then', f'  state=$(model_state {shlex.quote(model.api_identifier)}) || exit 1', '  case "$state" in', "    loaded) ;;", "    not-loaded) " + shlex.join((lms, *model.load_argv)) + " ;;", "    *) exit 1 ;;", "  esac", "fi"))
    lines.extend(("attempt=0", "while [ \"$attempt\" -lt 30 ]; do", "  if /usr/bin/curl --fail --silent --max-time 2 http://127.0.0.1:1234/v1/models >/dev/null; then exit 0; fi", "  attempt=$((attempt + 1))", "  /bin/sleep 1", "done", "exit 1", ""))
    plist = {
        "Label": LABEL,
        "ProgramArguments": ["/bin/sh", str(helper)],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "AbandonProcessGroup": True,
        "ProcessType": "Background",
        "EnvironmentVariables": {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        "StandardOutPath": str(home / ".lmstudio/solet-startup.stdout.log"),
        "StandardErrorPath": str(home / ".lmstudio/solet-startup.stderr.log"),
    }
    return plistlib.dumps(plist, sort_keys=True).decode(), "\n".join(lines)


def _loaded_state_function() -> tuple[str, ...]:
    """A bounded passive read; exact response identity and state are mandatory."""

    return (
        "model_state() {",
        '  response=$(/usr/bin/curl --fail --silent --show-error --max-time 2 --max-filesize 1048576 "http://127.0.0.1:1234/api/v0/models/$1") || return 1',
        '  identifier=$(printf %s "$response" | /usr/bin/plutil -extract id raw -o - -) || return 1',
        '  [ "$identifier" = "$1" ] || return 1',
        '  printf %s "$response" | /usr/bin/plutil -extract state raw -o - -',
        "}",
    )


def login_definition_current(home: Path, models: dict[str, ModelArtifact]) -> bool:
    expected = render_login_agent(home, models)
    for path, content, mode in zip(login_paths(home), expected, (0o644, 0o700), strict=True):
        if not contained_regular_file(path, home):
            return False
        try:
            if path.read_text(encoding="utf-8") != content or path.stat().st_mode & 0o777 != mode:
                return False
        except (OSError, UnicodeError):
            return False
    return True


def login_loaded(runtime: Runtime) -> bool | None:
    """Distinguish an absent job from an unreadable launchctl state."""

    outcome = runtime.run(("/bin/launchctl", "print", f"gui/{os.getuid()}/{LABEL}"), timeout_seconds=5)
    if outcome.ok:
        return True if "state =" in outcome.stdout and LABEL in outcome.stdout else None
    if not outcome.timed_out and "Could not find service" in outcome.stderr and not outcome.stderr_truncated:
        return False
    return None


def login_classification(runtime: Runtime, models: dict[str, ModelArtifact]) -> str:
    plist, helper = login_paths(runtime.home)
    loaded = login_loaded(runtime)
    if loaded is None:
        return "unknown"
    if not plist.exists() and not helper.exists() and not loaded:
        return "absent"
    if not login_definition_current(runtime.home, models):
        return "present_but_stale"
    return "present_already_current" if loaded else "present_not_loaded"


def install_login_agent(runtime: Runtime, models: dict[str, ModelArtifact]) -> bool:
    """Upsert and enable the singleton without unload or per-solet ownership."""

    classification = login_classification(runtime, models)
    if classification == "unknown":
        return False
    if classification == "present_already_current":
        return True
    if classification == "present_but_stale" and login_loaded(runtime) is True:
        # Replacing a loaded definition needs host-level coordination. Never
        # unload a shared job merely because this solet's setup runs again.
        return False
    _write_login_files(runtime, models)
    domain = f"gui/{os.getuid()}"
    if not runtime.run(("/bin/launchctl", "enable", f"{domain}/{LABEL}"), timeout_seconds=10).ok:
        return False
    if not runtime.run(("/bin/launchctl", "bootstrap", domain, str(login_paths(runtime.home)[0])), timeout_seconds=10).ok:
        return False
    return login_loaded(runtime) is True


def _write_login_files(runtime: Runtime, models: dict[str, ModelArtifact]) -> None:
    for path, content, mode in zip(login_paths(runtime.home), render_login_agent(runtime.home, models), (0o644, 0o700), strict=True):
        if not path.is_file() or path.read_text(encoding="utf-8") != content or path.stat().st_mode & 0o777 != mode:
            runtime.atomic_write(path, content, mode=mode)
