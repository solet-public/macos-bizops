"""Minimal synchronous client for a homunculus's localhost bridge surface.

Speaks the `/api/v1/bridge/*` contract the stdio MCP bridge forwards to,
but as one-shot request/response rather than a persistent MCP session.

Deliberately NOT the `Forwarder` from ``mcp_bridge/``: that owns a
persistent bridge session (peer-registry registration, a long-poll event
loop, an MCP notification write-stream). A one-shot call needs none of
that — ``process/call`` only requires the bridge to exist — so this client
opens a bridge, calls, polls the result, and closes, never registering a
peer identity and never polluting the registry.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import TracebackType
from typing import Any, Final

import httpx
from ananta.core.config.environment_config import EnvironmentConfig
from ananta.core.root_manifest import MANIFEST_FILENAME, load_manifest
from ananta.core.runtime.port_manager import read_port_file

BRIDGE_SERVICE_NAME: Final[str] = "bridge"
API_PREFIX: Final[str] = "/api/v1/bridge"

DEFAULT_REQUEST_TIMEOUT_S: Final[float] = 30.0
DEFAULT_POLL_TIMEOUT_S: Final[float] = 120.0
POLL_INTERVAL_S: Final[float] = 0.5

# A genesis-born clone rewrites root_manifest.yaml's homunculus_name to its own
# name, but an unmaterialized source tree still carries this literal
# placeholder -- so it is the signal to use the clone-dir basename.
_NAME_PLACEHOLDER: Final[str] = "homunculus"

# Action statuses that mean "still working". Anything else is terminal.
NON_TERMINAL_STATUSES: Final[frozenset[str]] = frozenset(
    {"queued", "pending", "dispatched", "processing", "running"},
)


class HomunculusNotRunningError(RuntimeError):
    """The homunculus bridge is not reachable (no port file present)."""


class HomunculusIdentityError(RuntimeError):
    """The CLI could not determine WHICH homunculus it is installed under — its
    clone's ``root_manifest.yaml`` is present but unreadable, so falling back to
    the clone-dir basename could silently retarget a corrupt/mis-seeded clone.
    """


class BridgeCallError(RuntimeError):
    """A bridge HTTP call returned an error status or an unusable body."""


class BridgeResultTimeoutError(RuntimeError):
    """A dispatched action did not reach a terminal state within the budget."""


def _clone_root() -> Path | None:
    """The homunculus clone root: the nearest ancestor of this module carrying
    ``root_manifest.yaml``. ``None`` when the CLI runs outside a clone.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / MANIFEST_FILENAME).is_file():
            return parent
    return None


def resolve_homunculus_name() -> str:
    """The homunculus this CLI is installed under — derived from the CLI's OWN
    resolved install location, never a caller flag or ambient env, so a bare
    per-homunculus symlink (``~/.local/bin/<name>`` -> ``<clone>/.venv/bin/...``)
    pins its own homunculus and reaches no sibling.

    Precedence: the clone's genesis-rewritten ``root_manifest.yaml`` name; else
    the clone-dir basename (the birth/clone convention is ``~/Workspace/<name>/``);
    else ``$HOMUNCULUS_NAME`` (when run outside any clone). A root_manifest that
    is PRESENT but unreadable (malformed / schema-invalid) fails loud rather than
    silently retargeting a corrupt clone by its directory name.
    """
    root = _clone_root()
    if root is None:
        return EnvironmentConfig.homunculus_name()
    manifest_path = root / MANIFEST_FILENAME
    manifest, error = load_manifest(manifest_path)
    if manifest is not None:
        # Genesis rewrites this to the newborn's name; an unmaterialized source
        # tree keeps the literal placeholder -> use the clone-dir basename.
        if manifest.homunculus_name != _NAME_PLACEHOLDER:
            return manifest.homunculus_name
        return root.name
    if manifest_path.is_file():
        raise HomunculusIdentityError(
            f"root_manifest at {manifest_path} is present but unreadable ({error}) "
            "— refusing to guess the homunculus identity by clone-dir basename."
        )
    # ABSENT root_manifest -> the clone-dir basename is the name.
    return root.name


def resolve_base_url(homunculus_name: str | None = None) -> str:
    """Discover the running homunculus's bridge base URL from its port file.

    Args:
        homunculus_name: Test-only override. Production resolves identity from
            the CLI's install location (:func:`resolve_homunculus_name`) — no
            ``-H`` flag, no ambient env — so each installed command (and any
            symlink to it) reaches ONLY its own homunculus.

    Returns:
        ``http://127.0.0.1:<port>`` for the discovered bridge port.

    Raises:
        HomunculusNotRunningError: No bridge port file exists for the homunculus.
    """
    name = homunculus_name or resolve_homunculus_name()
    port = read_port_file(BRIDGE_SERVICE_NAME, name)
    if port is None:
        raise HomunculusNotRunningError(
            f"no bridge port file for homunculus '{name}' "
            f"(~/.ananta/runtime/{name}.{BRIDGE_SERVICE_NAME}.port). "
            "Is the homunculus running?",
        )
    return f"http://127.0.0.1:{port}"


class BridgeClient:
    """One-shot synchronous client over a homunculus's bridge HTTP surface."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=request_timeout_s,
            transport=transport,
        )
        self._bridge_id: str | None = None

    def __enter__(self) -> BridgeClient:
        self.open()
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        self.close()

    def open(self) -> str:
        """Open a bridge session and remember its id."""
        payload = self._post(f"{API_PREFIX}/open", {"parent_pid": os.getpid()})
        bridge_id = payload.get("bridge_id")
        if not isinstance(bridge_id, str):
            raise BridgeCallError(f"bridge open returned no bridge_id: {payload!r}")
        self._bridge_id = bridge_id
        return bridge_id

    def close(self) -> None:
        """Close the bridge (best-effort) and dispose the HTTP client.

        Teardown never masks a result already obtained by the caller, so an
        HTTP error while closing is swallowed — the httpx client is closed
        regardless.
        """
        bridge_id, self._bridge_id = self._bridge_id, None
        try:
            if bridge_id is not None:
                self._post(f"{API_PREFIX}/{bridge_id}/close", {})
        except httpx.HTTPError:
            pass
        finally:
            self._client.close()

    def call_and_wait(
        self,
        process_key: str,
        arguments: dict[str, Any],
        *,
        reason: str | None = None,
        poll_timeout_s: float = DEFAULT_POLL_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Dispatch a process and poll until its result is terminal."""
        dispatch = self.process_call(process_key, arguments, reason=reason)
        action_id = dispatch.get("action_id")
        if not isinstance(action_id, str):
            raise BridgeCallError(
                f"process_call returned no action_id: {dispatch!r}",
            )
        return self.wait_for_result(action_id, poll_timeout_s=poll_timeout_s)

    def wait_for_result(
        self,
        action_id: str,
        *,
        poll_timeout_s: float = DEFAULT_POLL_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Poll ``process/result`` until the action leaves a working state."""
        deadline = time.monotonic() + poll_timeout_s
        while True:
            payload = self.process_result(action_id)
            status = str(payload.get("status", ""))
            if status not in NON_TERMINAL_STATUSES:
                return payload
            if time.monotonic() >= deadline:
                raise BridgeResultTimeoutError(
                    f"action {action_id} still '{status}' "
                    f"after {poll_timeout_s:.0f}s",
                )
            time.sleep(POLL_INTERVAL_S)

    def process_call(
        self,
        process_key: str,
        arguments: dict[str, Any],
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Zero-inference dispatch; returns the correlation envelope."""
        body: dict[str, Any] = {"process_key": process_key, "arguments": arguments}
        if reason is not None:
            body["reason"] = reason
        return self._post(f"{API_PREFIX}/{self._require_bridge()}/process/call", body)

    def process_result(self, action_id: str) -> dict[str, Any]:
        """Snapshot-read a dispatched action's stored result."""
        bridge_id = self._require_bridge()
        return self._get(f"{API_PREFIX}/{bridge_id}/process/result/{action_id}")

    def process_search(self, query: str, max_results: int) -> dict[str, Any]:
        """Discover process keys by semantic query."""
        bridge_id = self._require_bridge()
        return self._post(
            f"{API_PREFIX}/{bridge_id}/process/search",
            {"query": query, "max_results": max_results},
        )

    def process_schema(self, process_key: str) -> dict[str, Any]:
        """Fetch a process's argument schema."""
        bridge_id = self._require_bridge()
        return self._post(
            f"{API_PREFIX}/{bridge_id}/process/schema",
            {"process_key": process_key},
        )

    def health(self) -> dict[str, Any]:
        """Read the bridge health endpoint (no open bridge required)."""
        return self._get(f"{API_PREFIX}/health")

    def _require_bridge(self) -> str:
        if self._bridge_id is None:
            raise BridgeCallError("bridge not open; call open() first")
        return self._bridge_id

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._unwrap(self._client.post(path, json=body), path)

    def _get(self, path: str) -> dict[str, Any]:
        return self._unwrap(self._client.get(path), path)

    @staticmethod
    def _unwrap(response: httpx.Response, path: str) -> dict[str, Any]:
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise BridgeCallError(
                f"{path} failed ({response.status_code}): {response.text}",
            )
        body = response.json()
        if not isinstance(body, dict):
            raise BridgeCallError(f"{path} returned a non-object body: {body!r}")
        return body
