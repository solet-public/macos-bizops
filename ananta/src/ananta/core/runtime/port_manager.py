"""Dynamic port allocation and discovery for Ananta services.

This module provides utilities for:

- ``find_available_port`` — OS-assigned port via ``bind(0)`` (with an
  optional ``preferred`` fast-path that tries a specific port first).
- ``write_port_file`` / ``read_port_file`` / ``remove_port_file`` —
  on-disk service-discovery for non-bridge services (REST, JSONRPC,
  Schwab HTTP, …).
- ``PortManager`` — thin wrapper that ``allocate``-then-``write_port_file``
  for the common case.

Slice 3 of the bridge-port-routing-and-session-lifecycle design record
(dev-checkout workbench — not part of the shipped tree; invariant I1.A
— no hardcoded port bands) eliminated the per-color
allocation bands (blue=8101-8149 / green=8150-8198 / probe=8200-8299
plus the legacy unset band). Allocation is now ``bind(0)`` against the
OS ephemeral range; the spawn-path guarantee from invariant I2 ensures
no two children try to register the same instance_id, so band
partitioning is unnecessary.

``<name>.bridge.port`` names the solet's bridge front door and has
exactly ONE writer — whatever component owns that front door (D11 ruling,
dev-checkout workbench record — not part of the shipped tree).

In router topology, the router owns it:
``plugins/macos_self_deployment_plugin/src/macos_self_deployment_plugin/blue_green_router/install_router.py``
writes ``<name>.bridge.port`` directly (via ``Path.write_text``) at
install time. the solet children do NOT maintain a bridge port file — the
bridge HTTP server's bound port lives in-process on the
``agent_messaging_plugin``'s ``bridge_port`` property, and the
``macos_self_deployment_plugin`` heartbeat reads it via cross-plugin
lookup. To prevent regression, ``write_port_file`` and
``remove_port_file`` raise ``ValueError`` when called with
``service_name='bridge'``.

In router-less topology (minimal-bundle solets that never install the
router), ``agent_messaging_plugin`` IS the front door, so it is the
sanctioned writer of its own bridge port file — via
``write_routerless_bridge_port_file``, the one narrow, explicitly-named
exception to the guard above. Its sole caller is
``agent_messaging_plugin.start_interface``, gated on a manifest-declared
"is the router in this solet's topology" predicate (R1 of the D11
ruling): never runtime-probed, so both colors of a router topology agree
and neither writes. Callers must never use this function when a router
is present.
"""

import asyncio
import logging
import os
import socket
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ananta.core.config.environment_config import EnvironmentConfig

DEFAULT_CLIENT_IO_WORKERS: Final[int] = 2
DEFAULT_CLIENT_IO_PENDING: Final[int] = 32


@dataclass(eq=False)
class _ClientIOWaiter:
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[None]
    granted: bool = False


class ClientIOExecutor:
    """A helper-owned pool with a bounded submitted queue and async FIFO admission.

    This is not a shared runtime pool. Standalone clients construct their own
    named instance. Configuration is immutable; ``close`` drains accepted work
    and retires the instance. Cancellation retains a slot until the physical
    operation finishes, including when its original caller's loop has exited.

    ``SOLET_CLIENT_IO_WORKERS`` (default 2) and ``SOLET_CLIENT_IO_PENDING``
    (default 32) configure helper-local instances at construction. Explicit
    constructor settings take precedence, including a zero pending queue.
    """

    def __init__(self, name: str, *, workers: int | None = None, pending: int | None = None) -> None:
        workers = int(os.environ.get("SOLET_CLIENT_IO_WORKERS", str(DEFAULT_CLIENT_IO_WORKERS))) if workers is None else workers
        pending = int(os.environ.get("SOLET_CLIENT_IO_PENDING", str(DEFAULT_CLIENT_IO_PENDING))) if pending is None else pending
        if workers < 1 or pending < 0:
            raise ValueError("workers must be positive and pending must be nonnegative")
        self._name = name
        self._workers = workers
        self._capacity = workers + pending
        self._available = self._capacity
        self._pool: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        self._waiters: deque[_ClientIOWaiter] = deque()
        self._closed = False

    async def _admit(self) -> ThreadPoolExecutor:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._closed:
                raise RuntimeError(f"{self._name} is closed")
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix=self._name)
                logging.getLogger(__name__).info("created %s executor: workers=%d pending=%d", self._name, self._workers, self._capacity - self._workers)
            pool = self._pool
            if self._available:
                self._available -= 1
                return pool
            waiter = _ClientIOWaiter(loop, loop.create_future())
            self._waiters.append(waiter)
        try:
            await waiter.future
        except BaseException:
            with self._lock:
                if not waiter.granted:
                    self._waiters.remove(waiter)
            if waiter.granted:
                self._release()
            raise
        return pool

    @staticmethod
    def _wake(future: asyncio.Future[None]) -> None:
        if not future.done():
            future.set_result(None)

    def _release(self) -> None:
        with self._lock:
            if not self._waiters:
                self._available += 1
                self._close_if_drained()
                return
            waiter = self._waiters.popleft()
            waiter.granted = True
        try:
            waiter.loop.call_soon_threadsafe(self._wake, waiter.future)
        except RuntimeError:
            # A closed caller loop cannot receive its reserved slot.
            self._release()

    async def run[T](self, function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        pool = await self._admit()
        try:
            job = pool.submit(function, *args, **kwargs)
        except BaseException:
            self._release()
            raise
        job.add_done_callback(lambda _job: self._release())
        future = asyncio.wrap_future(job)
        # A cancelled caller must neither free a running slot nor cancel a
        # queued work item before the worker has physically dequeued it.
        future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return await asyncio.shield(future)

    def _close_if_drained(self) -> None:
        if self._closed and self._available == self._capacity and self._pool is not None:
            self._pool.shutdown(wait=False)

    def close(self) -> None:
        """Retire without joining a worker on the calling event loop."""
        with self._lock:
            self._closed = True
            self._close_if_drained()


# Filename suffix for the router-port discovery file written by
# install_router.py. The router's own management port (NOT the public
# bridge port, which the router also writes to ``<name>.bridge.port``).
ROUTER_PORT_FILENAME_SUFFIX: Final[str] = ".router.port"

# Sentinel used to fail-loud on regression: nothing in the platform
# should write or remove ``<name>.bridge.port`` through port_manager.
# install_router.py writes it directly; nothing else touches it.
_FORBIDDEN_PORT_FILE_SERVICE_NAMES: Final[frozenset[str]] = frozenset({"bridge"})


def get_runtime_dir(solet_name: str | None = None) -> Path:
    """Get the runtime directory for a solet.

    Uses XDG_RUNTIME_DIR if available, otherwise ~/.ananta/runtime.

    Args:
        solet_name: Optional solet name. If not provided,
            resolved via EnvironmentConfig.solet_name() (hard-errors
            if SOLET_NAME is unset).

    Returns:
        Path to the runtime directory (created if it doesn't exist)
    """
    if solet_name is None:
        solet_name = EnvironmentConfig.solet_name()

    # Prefer XDG_RUNTIME_DIR (standard on Linux)
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        runtime_dir = Path(xdg_runtime) / "ananta"
    else:
        # Fallback: user home directory
        runtime_dir = Path.home() / ".ananta" / "runtime"

    # Create with user-only permissions (rwx------)
    runtime_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    return runtime_dir


def find_available_port(preferred: int | None = None) -> int:
    """Return an OS-assigned port via ``bind(0)``.

    Slice 3 eliminated band-iteration in favor of ``bind(0)``. Race
    window: between this function returning and the caller binding the
    same port, another process could grab it. Same race as the prior
    iterate-and-test approach — acceptable.

    Args:
        preferred: If provided and the port is currently available,
            return it. Otherwise fall through to ``bind(0)``.

    Returns:
        The allocated port number.
    """
    if preferred is not None and _is_port_available(preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("", 0))
        return sock.getsockname()[1]


def _is_port_available(port: int, host: str = "0.0.0.0") -> bool:
    """Check if a port is available for binding.

    Args:
        port: Port number to check
        host: Host address to bind to

    Returns:
        True if port is available, False otherwise
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            return True
    except OSError:
        return False


def _port_file_path(runtime_dir: Path, solet_name: str, service_name: str) -> Path:
    """Resolve the on-disk port file path for a non-bridge service.

    Returns ``<runtime_dir>/<solet_name>.<service_name>.port``.
    The bridge service is router-owned (see module docstring); the
    write/remove paths refuse ``service_name='bridge'`` so a
    regression cannot reintroduce per-color or per-child bridge port
    files that would race with ``install_router.py``'s canonical
    write.
    """
    filename = f"{solet_name}.{service_name}.port"
    return runtime_dir / filename


def write_port_file(port: int, service_name: str = "rest", solet_name: str | None = None) -> Path:
    """Write a port number to a runtime file for service discovery.

    Creates a file at: ``{runtime_dir}/{solet_name}.{service_name}.port``.

    The bridge service file ``<name>.bridge.port`` is router-owned —
    ``install_router.py`` writes it directly via ``Path.write_text`` at
    install time. Calling this function with ``service_name='bridge'``
    raises ``ValueError`` to prevent a regression where a solet-side
    component overwrites the router's canonical file (the historical
    split-brain root cause documented in §1 of the bridge-port-routing
    design).

    Args:
        port: Port number to write
        service_name: Name of the service (default: 'rest')
        solet_name: Solet name (resolved via
            EnvironmentConfig.solet_name() if not provided)

    Returns:
        Path to the created port file

    Raises:
        ValueError: If ``service_name == 'bridge'`` — fail-fast guard.
    """
    if service_name in _FORBIDDEN_PORT_FILE_SERVICE_NAMES:
        msg = (
            f"write_port_file(service_name={service_name!r}, ...) is forbidden: "
            f"<name>.bridge.port has exactly one writer — install_router.py in "
            f"router topology, or write_routerless_bridge_port_file (via "
            f"agent_messaging_plugin.start_interface, R1-gated) in router-less "
            f"topology. This generic path is never that writer (D11 ruling)."
        )
        raise ValueError(msg)
    if solet_name is None:
        solet_name = EnvironmentConfig.solet_name()

    runtime_dir = get_runtime_dir(solet_name)
    port_file = _port_file_path(runtime_dir, solet_name, service_name)

    # Write atomically (write to temp, then rename)
    temp_file = port_file.with_suffix(".port.tmp")
    temp_file.write_text(str(port))
    temp_file.rename(port_file)

    # Set user-only permissions (rw-------)
    port_file.chmod(0o600)

    return port_file


def write_routerless_bridge_port_file(port: int, solet_name: str | None = None) -> Path:
    """Write ``<name>.bridge.port`` when this solet has no router (D11).

    The one narrow, explicitly-named exception to the ``write_port_file``
    bridge guard above. ``<name>.bridge.port`` has exactly one writer per
    solet topology; in router-less topology ``agent_messaging_plugin``
    IS the front door, so it is that writer (D11 ruling, dev-checkout
    workbench record — not part of the shipped tree).

    Contract (binding, do not weaken without a fresh Architect ruling):

    - **Sole caller:** ``agent_messaging_plugin.start_interface``, gated on
      the manifest-declared router-presence predicate (R1) — never called
      when the router is present, even if the file happens to be missing
      (R4; a missing file in router topology is the pre-install bootstrap
      window, not a gap this function fills).
    - **Call only after bind is confirmed** (R3) — never publish a port
      this process does not yet own.
    - **Rewrite on every start** (R3) — self-heals port re-roll staleness;
      callers do not need to check for an existing file first.
    - **Never called on shutdown** — there is no routerless counterpart to
      ``remove_port_file('bridge')``; a stale file yields a diagnosable
      connection-refused, the same failure mode router topology has when
      the platform is down.

    Args:
        port: The already-bound bridge server port.
        solet_name: Solet name (resolved via
            EnvironmentConfig.solet_name() if not provided).

    Returns:
        Path to the written port file.
    """
    if solet_name is None:
        solet_name = EnvironmentConfig.solet_name()

    runtime_dir = get_runtime_dir(solet_name)
    port_file = _port_file_path(runtime_dir, solet_name, "bridge")

    # Write atomically (write to temp, then rename) — same shape as
    # write_port_file, deliberately duplicated rather than shared so the
    # bridge guard above can never be bypassed by refactoring this
    # function to call write_port_file internally.
    temp_file = port_file.with_suffix(".port.tmp")
    temp_file.write_text(str(port))
    temp_file.rename(port_file)

    # Set user-only permissions (rw-------)
    port_file.chmod(0o600)

    return port_file


def read_port_file(service_name: str = "rest", solet_name: str | None = None) -> int | None:
    """Read a port number from a runtime file.

    Looks for file at: ``{runtime_dir}/{solet_name}.{service_name}.port``.

    Reading the bridge port file IS allowed and returns the front door's
    canonical port — written by ``install_router.py`` in router topology,
    or by ``write_routerless_bridge_port_file`` in router-less topology
    (D11). The write path is gated per-topology; the read path is the
    same legitimate operator-side MCP-bridge discovery surface either way.

    Args:
        service_name: Name of the service (default: 'rest')
        solet_name: Solet name (resolved via
            EnvironmentConfig.solet_name() if not provided)

    Returns:
        Port number if file exists and is valid, None otherwise
    """
    if solet_name is None:
        solet_name = EnvironmentConfig.solet_name()

    runtime_dir = get_runtime_dir(solet_name)
    port_file = _port_file_path(runtime_dir, solet_name, service_name)

    if not port_file.exists():
        return None

    try:
        content = port_file.read_text().strip()
        return int(content)
    except (ValueError, OSError):
        return None


def remove_port_file(service_name: str = "rest", solet_name: str | None = None) -> bool:
    """Remove a port file (typically on shutdown).

    Same fail-fast guard as ``write_port_file`` — refusing
    ``service_name='bridge'`` prevents accidental deletion of the
    router-owned canonical file.

    Args:
        service_name: Name of the service (default: 'rest')
        solet_name: Solet name (resolved via
            EnvironmentConfig.solet_name() if not provided)

    Returns:
        True if file was removed, False if it didn't exist

    Raises:
        ValueError: If ``service_name == 'bridge'`` — fail-fast guard.
    """
    if service_name in _FORBIDDEN_PORT_FILE_SERVICE_NAMES:
        msg = f"remove_port_file(service_name={service_name!r}, ...) is forbidden: <name>.bridge.port is never removed by its writer — R3 of the D11 ruling keeps a stale file diagnosable (connection-refused) rather than racing a restart for zero benefit."
        raise ValueError(msg)
    if solet_name is None:
        solet_name = EnvironmentConfig.solet_name()

    runtime_dir = get_runtime_dir(solet_name)
    port_file = _port_file_path(runtime_dir, solet_name, service_name)

    if port_file.exists():
        port_file.unlink()
        return True
    return False


class PortManager:
    """Manages dynamic port allocation for a service.

    Usage:
        manager = PortManager("rest")
        port = manager.allocate()  # Finds available port, writes port file
        # ... use port ...
        manager.release()  # Removes port file

    Not appropriate for ``service_name='bridge'`` — the bridge port is
    router-owned. Plugins that need an in-process bridge port (the
    agent_messaging_plugin) call ``find_available_port`` directly and
    skip the file-write entirely.
    """

    def __init__(self, service_name: str = "rest", solet_name: str | None = None) -> None:
        """Initialize port manager.

        Args:
            service_name: Name of the service (e.g., 'rest', 'jsonrpc')
            solet_name: Solet name (resolved via
            EnvironmentConfig.solet_name() if not provided)
        """
        self.service_name = service_name
        self.solet_name = solet_name or EnvironmentConfig.solet_name()
        self._allocated_port: int | None = None
        self._port_file: Path | None = None

    def allocate(self, preferred_port: int | None = None) -> int:
        """Allocate a port and write the port file.

        Args:
            preferred_port: If provided, try this port first. If unavailable,
                fall back to ``find_available_port()``.

        Returns:
            The allocated port number
        """
        port = find_available_port(preferred=preferred_port)
        self._port_file = write_port_file(port, self.service_name, self.solet_name)
        self._allocated_port = port
        return port

    def release(self) -> None:
        """Release the allocated port (remove port file)."""
        if self._port_file and self._port_file.exists():
            self._port_file.unlink()
        self._allocated_port = None
        self._port_file = None

    @property
    def port(self) -> int | None:
        """Get the currently allocated port."""
        return self._allocated_port

    @staticmethod
    def discover(service_name: str = "rest", solet_name: str | None = None) -> int | None:
        """Discover a service's port from its port file.

        Args:
            service_name: Name of the service
            solet_name: Solet name (resolved via
            EnvironmentConfig.solet_name() if not provided)

        Returns:
            Port number if found, None otherwise
        """
        return read_port_file(service_name, solet_name)
