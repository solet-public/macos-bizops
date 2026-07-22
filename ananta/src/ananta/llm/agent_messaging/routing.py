"""Resolve a logical backend name to a ``GuardedAgentInterface`` impl.

The router walks every loaded plugin and asks for its
``describe_capabilities()`` (the canonical advertise channel for
``GuardedAgentInterface`` plugins).  Backends are matched against
``AgentCapabilities.backend`` so the mapping survives plugin renames or
multi-plugin per-backend setups.

Validation is duck-typed (``hasattr`` + ``callable``) because
``GuardedAgentInterface`` is not decorated with ``@runtime_checkable``;
``isinstance`` against it raises ``TypeError`` at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:  # pragma: no cover — import-cycle break
    from ananta.core.plugins.plugin_base import PluginBase
    from ananta.core.plugins.plugin_manager import PluginManager
    from ananta.interfaces.guarded_agent_interface import GuardedAgentInterface


class _PluginRegistry(Protocol):
    """Subset of ``PluginManager`` the router actually needs."""

    plugins: dict[str, PluginBase]


_REQUIRED_METHODS: tuple[str, ...] = (
    "describe_capabilities",
    "execute_agent",
    "resume",
    "interrupt",
)


class BackendResolutionError(RuntimeError):
    """Raised when a backend cannot be resolved to a single plugin."""


@dataclass(frozen=True, slots=True)
class ResolvedBackend:
    """Successful resolution of a ``backend`` string."""

    plugin_name: str
    backend: str
    instance: GuardedAgentInterface


class BackendRouter:
    """Resolve a logical backend name to a ``GuardedAgentInterface`` instance."""

    def __init__(self, plugin_registry: _PluginRegistry) -> None:
        self._registry = plugin_registry

    def resolve(
        self, backend: str, *, plugin_name: str | None = None,
    ) -> ResolvedBackend:
        """Return the plugin that satisfies ``backend``.

        ``plugin_name`` (currently unused at the public HTTP surface) is
        reserved for the case where multiple plugins claim the same
        backend.  When supplied, only that plugin is considered.
        """
        candidates = self._collect_candidates(plugin_name)
        matches: list[ResolvedBackend] = []
        for name, instance in candidates:
            advertised = self._advertised_backend(name, instance)
            if advertised == backend:
                matches.append(
                    ResolvedBackend(
                        plugin_name=name,
                        backend=backend,
                        instance=instance,
                    ),
                )

        if not matches:
            raise BackendResolutionError(
                f"no GuardedAgentInterface plugin advertises backend "
                f"{backend!r}"
                + (f" (filter plugin_name={plugin_name!r})" if plugin_name else "")
                + ".  Loaded candidates: "
                + ", ".join(name for name, _ in candidates) or "<none>",
            )
        if len(matches) > 1:
            names = ", ".join(m.plugin_name for m in matches)
            raise BackendResolutionError(
                f"multiple plugins advertise backend {backend!r}: {names}. "
                "Pass an explicit plugin_name to disambiguate.",
            )
        return matches[0]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _collect_candidates(
        self, plugin_name: str | None,
    ) -> list[tuple[str, GuardedAgentInterface]]:
        plugins = self._registry.plugins
        if plugin_name is not None:
            instance = plugins.get(plugin_name)
            if instance is None:
                raise BackendResolutionError(
                    f"plugin_name={plugin_name!r} is not loaded",
                )
            self._verify_interface(plugin_name, instance)
            return [(plugin_name, cast("GuardedAgentInterface", instance))]

        out: list[tuple[str, GuardedAgentInterface]] = []
        for name, plugin in plugins.items():
            if all(
                callable(getattr(plugin, method, None))
                for method in _REQUIRED_METHODS
            ):
                out.append((name, cast("GuardedAgentInterface", plugin)))
        return out

    @staticmethod
    def _verify_interface(plugin_name: str, plugin: object) -> None:
        for method in _REQUIRED_METHODS:
            attr = getattr(plugin, method, None)
            if not callable(attr):
                raise BackendResolutionError(
                    f"{plugin_name} does not implement "
                    f"GuardedAgentInterface.{method}",
                )

    @staticmethod
    def _advertised_backend(
        plugin_name: str, instance: GuardedAgentInterface,
    ) -> str:
        try:
            capabilities = instance.describe_capabilities()
        except Exception as exc:  # noqa: BLE001 — surface as routing failure
            raise BackendResolutionError(
                f"{plugin_name}.describe_capabilities() raised: {exc}",
            ) from exc
        backend = getattr(capabilities, "backend", None)
        if backend is None:
            raise BackendResolutionError(
                f"{plugin_name}.describe_capabilities() returned no backend",
            )
        # AgentBackend is `class AgentBackend(str, Enum)` — instances are
        # str subclasses but ``str(member)`` returns 'AgentBackend.CODEX'.
        # Use the underlying value (or the bare string for non-Enum impls).
        value = getattr(backend, "value", backend)
        return str(value)


__all__ = [
    "BackendResolutionError",
    "BackendRouter",
    "ResolvedBackend",
]


def make_router(plugin_manager: PluginManager) -> BackendRouter:
    """Convenience builder for callers that already hold a PluginManager."""
    return BackendRouter(cast(_PluginRegistry, plugin_manager))
