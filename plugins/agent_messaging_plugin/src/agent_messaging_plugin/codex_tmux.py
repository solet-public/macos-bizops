"""Detached-tmux host driver and verified TUI channel for managed Codex."""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codex_app_server import CodexAppServerHostDriver
from .codex_common import (
    _ANSI_ESCAPE_RE,
    _CODEX_AGENT_ID,
    _DEFAULT_TMUX_POLL_INTERVAL_SECONDS,
    _DEFAULT_TMUX_STABLE_SAMPLES,
    _DEFAULT_TMUX_VERIFY_TIMEOUT_SECONDS,
    _MIN_TMUX_VERSION,
    _TMUX_BUSY_MARKERS,
    _codex_config_overrides,
    _codex_home,
    _command_succeeded,
    _identity_env,
    _read_codex_config,
    _refuse_claude_provider_overlay,
    _resolve_solet_bin,
    _toml_string,
    _without_parent_runtime_env,
)
from .headless_adapter import (
    _authority_system_prompt,
    _pid_alive,
    _resolve_default_cwd,
    _sigterm_then_kill,
)
from .tmux_adapter import (
    DEFAULT_PANE_HEIGHT,
    DEFAULT_PANE_WIDTH,
    _emit_role_tag_path,
    _parse_tmux_version,
    _sanitize_session_name,
    _sigterm_then_kill_process_group,
)


@dataclass(frozen=True, slots=True)
class _TmuxSpawnIdentity:
    agent_instance_id: str
    agent_session_id: str
    label: str


class _CodexTmuxDriverChannel:
    """Verified send-keys channel for the interactive Codex TUI."""

    def __init__(
        self,
        *, tmux_bin: str, session: str, run_fn: Callable[..., Any],
        sleep_fn: Callable[[float], None] = time.sleep,
        now_fn: Callable[[], float] = time.monotonic,
        poll_interval_seconds: float = _DEFAULT_TMUX_POLL_INTERVAL_SECONDS,
        stable_samples: int = _DEFAULT_TMUX_STABLE_SAMPLES,
        verify_timeout_seconds: float = _DEFAULT_TMUX_VERIFY_TIMEOUT_SECONDS,
    ) -> None:
        self._tmux_bin = tmux_bin
        self._session = session
        self._run_fn = run_fn
        self._sleep_fn = sleep_fn
        self._now_fn = now_fn
        self._poll_interval_seconds = poll_interval_seconds
        self._stable_samples = stable_samples
        self._verify_timeout_seconds = verify_timeout_seconds

    def send(self, text: str) -> None:
        from .session_hosts import DriverChannelSendError  # noqa: PLC0415

        self._wait_until_ready()
        literal = self._run(
            [self._tmux_bin, "send-keys", "-t", self._session, "-l", "--", text],
        )
        if not _command_succeeded(literal):
            raise DriverChannelSendError(
                f"tmux literal send failed for Codex session {self._session!r}",
            )
        composed = self._wait_until_stable()
        if self._submit_and_observe_change(composed):
            return
        # A same-burst Enter can be absorbed by the TUI.  One separately
        # timed retry is allowed; two no-op Enters are evidence of no pickup.
        if self._submit_and_observe_change(composed):
            return
        raise DriverChannelSendError(
            f"Codex tmux session {self._session!r} showed no styled pane-state "
            "change after two separate Enter submissions; refusing to call "
            "ghost/composed text a delivered turn.",
        )

    def _run(self, argv: list[str]) -> Any:
        try:
            return self._run_fn(
                argv, capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            from .session_hosts import DriverChannelSendError  # noqa: PLC0415

            raise DriverChannelSendError(
                f"tmux command failed for Codex session {self._session!r}: {exc}",
            ) from exc

    def _capture_styled(self) -> str | None:
        result = self._run(
            [self._tmux_bin, "capture-pane", "-e", "-p", "-t", self._session],
        )
        return str(getattr(result, "stdout", "") or "") if _command_succeeded(result) else None

    @staticmethod
    def _visible_text(styled: str) -> str:
        return _ANSI_ESCAPE_RE.sub("", styled)

    def _wait_until_ready(self) -> str:
        """Wait for an idle Codex prompt before putting text in its composer."""
        deadline = self._now_fn() + self._verify_timeout_seconds
        while self._now_fn() <= deadline:
            current = self._capture_styled()
            if current is not None:
                visible = self._visible_text(current)
                if (
                    "OpenAI Codex" in visible
                    and "›" in visible
                    and not any(marker in visible for marker in _TMUX_BUSY_MARKERS)
                ):
                    return current
            self._sleep_fn(self._poll_interval_seconds)
        from .session_hosts import DriverChannelSendError  # noqa: PLC0415

        raise DriverChannelSendError(
            f"Codex tmux pane {self._session!r} never reached an idle prompt; "
            "text was not pasted.",
        )

    def _wait_until_stable(self) -> str:
        deadline = self._now_fn() + self._verify_timeout_seconds
        previous: str | None = None
        count = 0
        while self._now_fn() <= deadline:
            current = self._capture_styled()
            if current is not None and current == previous:
                count += 1
                if count >= self._stable_samples:
                    return current
            else:
                count = 0
            previous = current
            self._sleep_fn(self._poll_interval_seconds)
        from .session_hosts import DriverChannelSendError  # noqa: PLC0415

        raise DriverChannelSendError(
            f"Codex tmux pane {self._session!r} did not stabilize before Enter; "
            "submission was not attempted.",
        )

    def _submit_and_observe_change(self, composed: str) -> bool:
        enter = self._run(
            [self._tmux_bin, "send-keys", "-t", self._session, "Enter"],
        )
        if not _command_succeeded(enter):
            return False
        deadline = self._now_fn() + self._verify_timeout_seconds
        while self._now_fn() <= deadline:
            current = self._capture_styled()
            if (
                current is not None
                and current != composed
                and self._visible_text(current) != self._visible_text(composed)
            ):
                return True
            self._sleep_fn(self._poll_interval_seconds)
        return False


class CodexTmuxHostDriver:
    """The ``("codex", "tmux")`` swap-durable interactive driver."""

    def __init__(
        self,
        *, codex_bin: str | None = None,
        tmux_bin: str | None = None,
        solet_bin: str | None = None,
        solet_name: str | None = None,
        codex_home: Path | None = None,
        cwd: Path | None = None,
        transport: str | None = None,
        run_fn: Callable[..., Any] = subprocess.run,
        pane_width: int = DEFAULT_PANE_WIDTH,
        pane_height: int = DEFAULT_PANE_HEIGHT,
        grace_seconds: float = 10.0,
    ) -> None:
        self._codex_bin = codex_bin if codex_bin is not None else shutil.which("codex") or ""
        self._tmux_bin = tmux_bin if tmux_bin is not None else shutil.which("tmux") or ""
        self._solet_bin = _resolve_solet_bin(solet_bin)
        self._solet_name = (
            solet_name if solet_name is not None
            else os.environ.get("SOLET_NAME", "")
        )
        self._codex_home = _codex_home(codex_home)
        self._cwd = cwd if cwd is not None else _resolve_default_cwd()
        self._transport = transport if transport is not None else ""
        self._run_fn = run_fn
        self._pane_width = pane_width
        self._pane_height = pane_height
        self._grace_seconds = grace_seconds

    @property
    def _config_path(self) -> Path:
        return self._codex_home / "config.toml"

    def _resolve_transport(self, spec: Mapping[str, object]) -> str:
        return str(spec.get("transport") or "") or self._transport or "watch"

    def verify_config(self, *, transport: str | None = None) -> list[str]:
        base = CodexAppServerHostDriver(
            codex_bin=self._codex_bin,
            solet_bin=self._solet_bin,
            solet_name=self._solet_name,
            codex_home=self._codex_home,
            cwd=self._cwd,
            transport=self._transport,
        ).verify_config(transport=transport)
        if not (self._tmux_bin and os.access(self._tmux_bin, os.X_OK)):
            base.append("no executable tmux binary found — install tmux>=3.3.")
            return base
        try:
            result = self._run_fn(
                [self._tmux_bin, "-V"], capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            base.append(f"tmux -V failed: {exc}")
            return base
        version = _parse_tmux_version(str(getattr(result, "stdout", "") or ""))
        if not _command_succeeded(result) or version is None or version < _MIN_TMUX_VERSION:
            base.append(
                f"tmux>=3.3 is required; observed {getattr(result, 'stdout', '')!r}.",
            )
        return base

    def capability_report(self) -> dict[str, object]:
        return {
            "host": "tmux",
            "agent_runtime": _CODEX_AGENT_ID,
            "topology": "detached-pty",
            "inspectable_via": ["tmux", "codex_thread", "transcript"],
            "driver_channel": "verified-send-keys",
        }

    def spawn(self, spec: Mapping[str, object]) -> str:
        _refuse_claude_provider_overlay(spec)
        transport = self._resolve_transport(spec)
        self._require_ready(transport)
        identity = self._spawn_identity(spec)
        env = self._spawn_env(identity, transport)
        codex_cmd = self._codex_command(spec, identity, transport)
        session_name = _sanitize_session_name(
            f"fleet-{identity.label}-{identity.agent_instance_id[-8:]}",
        )
        command = self._new_session_command(
            session_name=session_name,
            pane_command=self._pane_command(
                codex_cmd, label=identity.label, transport=transport,
            ),
            env=env,
        )
        self._launch_tmux(command)
        self._run_fn(
            [self._tmux_bin, "set", "-g", "allow-passthrough", "on"],
            capture_output=True, text=True, timeout=5,
        )
        return session_name

    def _require_ready(self, transport: str) -> None:
        from .session_hosts import HostCannotSpawnError  # noqa: PLC0415

        remedies = self.verify_config(transport=transport)
        if remedies:
            raise HostCannotSpawnError("; ".join(remedies))

    @staticmethod
    def _spawn_identity(spec: Mapping[str, object]) -> _TmuxSpawnIdentity:
        from .session_hosts import HostCannotSpawnError  # noqa: PLC0415

        agent_instance_id = str(spec.get("agent_instance_id") or "")
        if not agent_instance_id:
            raise HostCannotSpawnError("spawn spec is missing agent_instance_id for Codex tmux.")
        return _TmuxSpawnIdentity(
            agent_instance_id=agent_instance_id,
            agent_session_id=f"ases-{agent_instance_id}",
            label=str(spec.get("lane_id") or "") or agent_instance_id,
        )

    def _spawn_env(
        self, identity: _TmuxSpawnIdentity, transport: str,
    ) -> dict[str, str]:
        return _identity_env(
            agent_instance_id=identity.agent_instance_id,
            agent_session_id=identity.agent_session_id,
            label=identity.label,
            solet_name=self._solet_name,
            solet_bin=self._solet_bin,
            transport=transport,
        )

    def _codex_command(
        self, spec: Mapping[str, object], identity: _TmuxSpawnIdentity,
        transport: str,
    ) -> list[str]:
        config = _read_codex_config(self._config_path)
        overrides = _codex_config_overrides(
            config=config,
            solet_name=self._solet_name,
            transport=transport,
            agent_instance_id=identity.agent_instance_id,
            agent_session_id=identity.agent_session_id,
            label=identity.label,
            solet_bin=self._solet_bin,
        )
        effort = str(spec.get("effort") or "")
        if effort:
            overrides.append(f"model_reasoning_effort={_toml_string(effort)}")
        overrides.append(
            f"developer_instructions={_toml_string(_authority_system_prompt(spec))}",
        )
        codex_cmd = [
            self._codex_bin,
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
            "-C", str(self._cwd),
        ]
        model = str(spec.get("model") or "")
        if model:
            codex_cmd += ["-m", model]
        for override in overrides:
            codex_cmd += ["-c", override]
        return codex_cmd

    def _new_session_command(
        self, *, session_name: str, pane_command: str, env: Mapping[str, str],
    ) -> list[str]:
        new_session_cmd = [
            self._tmux_bin, "new-session", "-d", "-s", session_name,
            "-x", str(self._pane_width), "-y", str(self._pane_height),
        ]
        for key, value in env.items():
            if key in {
                "SOLET_NAME", "AGENT_IDENTITY", "AGENT_INSTANCE_ID",
                "AGENT_SESSION_ID", "AGENT_SESSION_LABEL", "AGENT_WAKE_CLI",
                "FLEET_TRANSPORT", "PATH",
            }:
                new_session_cmd += ["-e", f"{key}={value}"]
        new_session_cmd += ["-c", str(self._cwd), "sh", "-c", pane_command]
        return new_session_cmd

    def _launch_tmux(self, new_session_cmd: list[str]) -> None:
        from .session_hosts import HostCannotSpawnError  # noqa: PLC0415

        try:
            result = self._run_fn(
                new_session_cmd, capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HostCannotSpawnError(f"tmux new-session raised: {exc}") from exc
        if not _command_succeeded(result):
            raise HostCannotSpawnError(
                f"tmux new-session failed: {getattr(result, 'stderr', '')}",
            )

    def _pane_command(self, codex_cmd: list[str], *, label: str, transport: str) -> str:
        emit = _emit_role_tag_path()
        parts = [
            f"sh {shlex.quote(str(emit))} {shlex.quote(label)}; " if emit.exists() else "",
        ]
        if transport == "watch":
            # codex-0147-dead-spool-retirement (2026-08-13): --no-spool disables
            # this watcher's own wake-hook spool tee. Stock Codex's Stop hook
            # cannot consume it (async command hooks do not execute on stock
            # Codex), so an armed spool here would just accumulate an unread
            # file for the pane's lifetime.
            watch_cmd = shlex.join(
                _without_parent_runtime_env(
                    [
                        self._solet_bin,
                        "watch",
                        "--agent-id", _CODEX_AGENT_ID,
                        "--no-claim",
                        "--no-spool",
                    ],
                ),
            )
            # $$ is deliberately shell-expanded before exec; the shell pid is
            # retained by the exec'd Codex process, giving the watcher a true
            # parent-liveness target without adding a second identity mint.
            parts.append(
                f"{watch_cmd} --exit-with-parent $$ >/dev/null 2>&1 & ",
            )
        parts.append(f"exec {shlex.join(_without_parent_runtime_env(codex_cmd))}")
        return "".join(parts)

    def alive(self, host_ref: str) -> bool:
        try:
            result = self._run_fn(
                [self._tmux_bin, "has-session", "-t", host_ref],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return _command_succeeded(result)

    def _pane_pid(self, host_ref: str) -> int | None:
        try:
            result = self._run_fn(
                [self._tmux_bin, "list-panes", "-t", host_ref, "-F", "#{pane_pid}"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if not _command_succeeded(result):
            return None
        try:
            return int(str(getattr(result, "stdout", "") or "").strip().splitlines()[0])
        except (IndexError, ValueError):
            return None

    def terminate(self, host_ref: str, grace_seconds: int) -> None:
        pane_pid = self._pane_pid(host_ref)
        grace = grace_seconds if grace_seconds > 0 else self._grace_seconds
        if pane_pid is not None and _pid_alive(pane_pid):
            try:
                pgid = os.getpgid(pane_pid)
            except ProcessLookupError:
                pgid = None
            if pgid is not None:
                _sigterm_then_kill_process_group(pgid, grace)
            else:
                _sigterm_then_kill(pane_pid, None, grace)
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            self._run_fn(
                [self._tmux_bin, "kill-session", "-t", host_ref],
                capture_output=True, text=True, timeout=5,
            )

    def driver_channel(self, host_ref: str) -> _CodexTmuxDriverChannel | None:
        if not self.alive(host_ref):
            return None
        return _CodexTmuxDriverChannel(
            tmux_bin=self._tmux_bin,
            session=host_ref,
            run_fn=self._run_fn,
        )
