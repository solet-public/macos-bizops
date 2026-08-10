"""Fleet session-management Phase B, D1+D2 (§5) — the ``HostDriver`` Protocol
+ the ``operator`` driver + the ``headless`` driver (``headless_adapter.py``,
the universal floor, hardening WS-6 prior art) + the ``tmux`` driver
(``tmux_adapter.py``, D2, hardening the R1 spike). All three ship REGISTERED
in this build. Host drivers bind through a declared, registered Protocol
(per §1.1 point 2), never duck-typed discovery.

NAMING (A3 ruling, `workbench/2026-08-03_phase_c_brief_and_d4_sitting_agenda_
coordinator_dawn.md`): the L2 mechanism is called "host driver" in prose from
this point on — "adapter" is retired terminology. The rename is vocabulary
only (this module's Protocol/classes/functions, docstrings, error text); the
§5 contract (spawn/alive/terminate/driver_channel/capability_report/
verify_config) and every module/file NAME are unchanged (``session_hosts.py``,
``headless_adapter.py``, ``tmux_adapter.py`` keep their existing paths — only
the identifiers and prose inside them moved).

Selection is DECLARED, never probed (C3): ``FLEET_SESSION_HOST`` env var (or
a per-spawn override), default ``headless`` per §5. Every registered driver
is still config-gated fail-closed — an unconfigured environment (missing
binary, no ``HOMUNCULUS_NAME``, no permission mode, no ``.mcp.json``, or for
``tmux`` an unsupported tmux version) gets ``host_cannot_spawn`` with the
exact remedies from that driver's ``verify_config``, never
``host_mechanism_missing`` (that token now means ONLY "no driver registered
at all in this build" — there is no such name left among ``operator``/
``headless``/``tmux``; a caller must request one of those or wait for the
next driver to land).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

_ENV_FLEET_SESSION_HOST = "FLEET_SESSION_HOST"
DEFAULT_HOST = "headless"

OPERATOR_HOST = "operator"


class DriverChannel(Protocol):
    """A live stdin/stream-json handle for clear/compact/drive (§5).
    ``clear_session``/``compact_session`` (AMEND 5b) send a slash-command and
    return — fire-and-forget, they do not read/await the resulting turn (the
    running session processes it on its own next event-loop pass; consuming
    the turn is a separate, later concern). ``send()`` is the whole contract
    for this slice; only the ``headless`` and ``tmux`` host drivers provide a
    real one (the ``operator`` driver's ``driver_channel()`` still returns
    ``None`` — ``unsupported_on_host``, never a silent degradation)."""

    def send(self, text: str) -> None: ...


class HostDriver(Protocol):
    """Per-§5: each host implements this; the core layer never knows
    terminal specifics."""

    def spawn(self, spec: Mapping[str, object]) -> str:
        """Returns ``host_ref`` (tmux session name / driver pid / ...)."""
        ...

    def alive(self, host_ref: str) -> bool: ...

    def terminate(self, host_ref: str, grace_seconds: int) -> None: ...

    def driver_channel(self, host_ref: str) -> DriverChannel | None: ...

    def capability_report(self) -> dict[str, object]: ...

    def verify_config(self) -> list[str]:
        """Config-time, fail-closed remedies; empty = ready to spawn."""
        ...


class HostNotDeclaredError(Exception):
    """No host resolved at all: no per-spawn override, no
    ``FLEET_SESSION_HOST`` env, and no built-in default — cannot happen with
    :data:`DEFAULT_HOST` set, but the token exists for an explicit blank
    override (a caller passing ``host=""`` on purpose)."""

    def __init__(self) -> None:
        super().__init__(
            "host_not_declared: no host resolved (no per-spawn override, no "
            f"{_ENV_FLEET_SESSION_HOST} env, no default).",
        )


class HostMechanismMissingError(Exception):
    """The resolved host name has no host driver registered in THIS build —
    a known-missing mechanism (a future D-step driver, or a typo'd host
    name), never a silent substitute."""

    def __init__(self, host: str, remedy: str) -> None:
        self.host = host
        self.remedy = remedy
        super().__init__(f"host_mechanism_missing: {host!r} — {remedy}")


class HostCannotSpawnError(Exception):
    """The resolved host driver is registered but DEGENERATE — it observes
    via registration only, never spawns (the ``operator`` driver, §5)."""

    def __init__(self, remedy: str) -> None:
        self.remedy = remedy
        super().__init__(f"host_cannot_spawn: {remedy}")


class OperatorHostDriver:
    """The degenerate v1 host driver (§5): explicit "no automation" state.
    Observes via registration (the normal ``peer_register`` flow already
    gives an operator-launched session a ``managed_session`` row, §3.2) —
    it is never dispatched THROUGH ``spawn_session``."""

    def spawn(self, spec: Mapping[str, object]) -> str:
        del spec
        raise HostCannotSpawnError(
            "the operator host driver cannot spawn; launch manually: "
            "HOMUNCULUS_NAME=<name> python -m ananta.cli --app-home <profile>",
        )

    def alive(self, host_ref: str) -> bool:
        del host_ref
        return True

    def terminate(self, host_ref: str, grace_seconds: int) -> None:
        del host_ref, grace_seconds
        raise HostCannotSpawnError(
            "the operator host driver cannot terminate a session it did not "
            "spawn; stop the process manually.",
        )

    def driver_channel(self, host_ref: str) -> DriverChannel | None:
        del host_ref
        return None

    def capability_report(self) -> dict[str, object]:
        return {"host": OPERATOR_HOST, "topology": "manual", "inspectable_via": []}

    def verify_config(self) -> list[str]:
        return []


def _build_registry() -> dict[str, HostDriver]:
    # Deferred import: headless_adapter/tmux_adapter import HostCannotSpawnError
    # from THIS module, so a top-level import here would be circular.
    from .headless_adapter import HeadlessHostDriver  # noqa: PLC0415
    from .tmux_adapter import TmuxHostDriver  # noqa: PLC0415

    return {
        OPERATOR_HOST: OperatorHostDriver(),
        "headless": HeadlessHostDriver(),
        "tmux": TmuxHostDriver(),
    }


_REGISTRY: dict[str, HostDriver] = _build_registry()


def shutdown_all_drivers() -> None:
    """Terminate every tracked worker across every registered host driver
    that has a ``shutdown`` method (currently just ``headless``) — wired
    into ``plugin.py``'s ``stop_services`` so a graceful shutdown/restart
    never leaves an orphaned worker process burning tokens with nothing
    tracking it."""
    for driver in _REGISTRY.values():
        shutdown = getattr(driver, "shutdown", None)
        if callable(shutdown):
            shutdown()


def resolve_host_driver(requested_host: str | None) -> tuple[HostDriver, str]:
    """Resolve ``requested_host`` (per-spawn override) or the
    ``FLEET_SESSION_HOST`` env default to a registered host driver.

    An explicitly BLANK override (``""``/whitespace — distinct from
    ``None``, "no override supplied") raises :class:`HostNotDeclaredError`
    rather than silently falling through to the env/default chain: a caller
    that explicitly declared "no host" gets refused, never a substitute it
    never asked for (C3). ``None`` (the ordinary case — no per-spawn
    override) falls through to the env var, then :data:`DEFAULT_HOST`.
    Raises :class:`HostMechanismMissingError` when the resolved name has no
    driver registered in this build.
    """
    if requested_host is not None and not requested_host.strip():
        raise HostNotDeclaredError()
    host = (requested_host or os.environ.get(_ENV_FLEET_SESSION_HOST) or DEFAULT_HOST).strip()
    driver = _REGISTRY.get(host)
    if driver is None:
        registered = sorted(_REGISTRY)
        raise HostMechanismMissingError(
            host,
            f"no host driver registered for host {host!r} in this build "
            f"(registered: {registered}) — pass one of those or wait for a "
            "new host driver to land.",
        )
    return driver, host


__all__ = [
    "DEFAULT_HOST",
    "OPERATOR_HOST",
    "DriverChannel",
    "HostCannotSpawnError",
    "HostDriver",
    "HostMechanismMissingError",
    "HostNotDeclaredError",
    "OperatorHostDriver",
    "resolve_host_driver",
    "shutdown_all_drivers",
]
