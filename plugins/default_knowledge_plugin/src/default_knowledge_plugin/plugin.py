"""DefaultKnowledgePlugin — bound implementation of KnowledgeServiceInterface.

Concrete-via-MI composition of 5 W5.T sub-Mixins (Lifecycle / Search / FileOps /
Refresh / Maintenance) post-W5.T. The residual class retains the platform-protocol
surface (constructor, schema declaration, readiness lifecycle, start/stop services,
spawn-path auto-install hook) + 2 class attributes that satisfy the KSI binding
contract.

Implementation is decomposed across sibling modules:
- kb_git.py          — git subprocess operations
- kb_indexing.py     — source resolution, manifest, chunking, indexing
- kb_search.py       — search/recall/deduplication helpers
- kb_lifecycle.py    — install state, install/update/uninstall/activate/deactivate
- kb_file_ops.py     — browse/read/edit/create/delete
- kb_process_registry.py — process registry refresh
- _lifecycle_plugin_mixin.py     (W5.T) — KnowledgeLifecyclePluginMixin
- _search_plugin_mixin.py        (W5.T) — KnowledgeSearchPluginMixin
- _file_ops_plugin_mixin.py      (W5.T) — KnowledgeFileOpsPluginMixin
- _refresh_plugin_mixin.py       (W5.T) — KnowledgeRefreshPluginMixin
- _maintenance_plugin_mixin.py   (W5.T) — KnowledgeMaintenancePluginMixin
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any, Concatenate, Protocol, cast

from ananta.core.domain.enums import ActionStatus, ErrorSeverity
from ananta.core.domain.types import ActionResult, ErrorDetail
from ananta.core.plugins.plugin_base import ServicePlugin
from ananta.interfaces.embedding_service_interface import TokenBudget
from ananta.interfaces.knowledge_service_interface import KnowledgeServiceInterface
from ananta.services.session_ledger_service.summary_executor import BoundedSummaryExecutor
from ananta.types.schema_types import SchemaDefinition

from ._file_ops_plugin_mixin import KnowledgeFileOpsPluginMixin
from ._lifecycle_plugin_mixin import KnowledgeLifecyclePluginMixin
from ._maintenance_plugin_mixin import KnowledgeMaintenancePluginMixin
from ._refresh_plugin_mixin import KnowledgeRefreshPluginMixin
from ._search_plugin_mixin import KnowledgeSearchPluginMixin
from .constants import PLUGIN_NAME
from .kb_install_control import paced_install_control
from .kb_lifecycle import (
    auto_install_knowledge_bases as _run_auto_install,
)
from .schema import get_knowledge_schema

logger = logging.getLogger(__name__)


class _ShutdownEvent(Protocol):
    """The orchestrator's shutdown event; only its flag is read, from the install thread."""

    def is_set(self) -> bool: ...


def _lifecycle_completed() -> ActionResult:
    """Build the completed ActionResult every lifecycle hook returns."""
    return {
        "action_status": ActionStatus.COMPLETED.value,
        "data": {},
        "actions": [],
        "error": None,
        "timestamp": datetime.now(UTC).isoformat(),
    }


def _lifecycle_refusal() -> ActionResult:
    """Build the error ActionResult for a stop refused while this plugin is the active interface."""
    stamp = datetime.now(UTC).isoformat()
    error: ErrorDetail = {
        "type": "KnowledgeLifecycleError",
        "code": "default_knowledge.stop_refused_active_interface",
        "message": "Cannot stop services while this plugin is the active interface provider",
        "details": {"plugin_name": PLUGIN_NAME},
        "severity": ErrorSeverity.ERROR.value,
        "timestamp": stamp,
    }
    return {
        "action_status": ActionStatus.ERROR.value,
        "data": {},
        "actions": [],
        "error": error,
        "timestamp": stamp,
    }


class DefaultKnowledgePlugin(
    # Mixins FIRST so concrete methods resolve BEFORE KSI's @abstractmethod
    # markers; otherwise `TypeError: Can't instantiate abstract class` at construction.
    KnowledgeLifecyclePluginMixin,
    KnowledgeSearchPluginMixin,
    KnowledgeFileOpsPluginMixin,
    KnowledgeRefreshPluginMixin,
    KnowledgeMaintenancePluginMixin,
    ServicePlugin,
    KnowledgeServiceInterface,
):
    """Knowledge base management plugin.

    Concrete-via-MI composition of 5 sub-Mixins (Lifecycle / Search / FileOps /
    Refresh / Maintenance) per the W5.Q + W5.R + W5.S + W5.T 5-domain split.
    The 19 KSI verb implementations live on the 5 mixins; MRO resolution makes
    this class concrete (no abstract methods remain after the mixin contributions
    satisfy the KSI abstract markers).

    Acquires service dependencies in prepare_for_readiness():
    - state_service (required) — database operations
    - memory_service (required) — chunk storage via remember()/forget()
    - embedding_service (required) — embedding generation for search
    - address_book_service (optional) — source URL + credential resolution for remote KBs

    NOTE: vector_service is intentionally NOT a dependency. KB chunk deletion now
    flows through ``memory_service::delete_memories_by_ids`` (the owner verb cascades
    vectors internally — 2026-06-21 SQL-lockdown cohort), and search goes through
    ``memory_service`` recall, so the plugin never touches the vector service directly.

    Backward-compat: every existing ``from default_knowledge_plugin.plugin import
    DefaultKnowledgePlugin`` import continues to resolve. The class implements
    the full 19-verb KSI contract via MI from the 5 sub-Mixins.

    Forward-compat: ``isinstance(plugin, KnowledgeMaintenanceInterface)`` now
    returns True (and similarly for each of the 5 sub-Interfaces from W5.R).
    Per-domain dependency-injection becomes a real architectural pattern.
    """

    service_interfaces: tuple[type, ...] = (KnowledgeServiceInterface,)
    supported_interface_versions: dict[type, str] = {KnowledgeServiceInterface: KnowledgeServiceInterface.INTERFACE_VERSION}

    def __init__(self) -> None:
        super().__init__()
        self.name = PLUGIN_NAME
        self._state_service: Any = None
        self._memory_service: Any = None
        self._embedding_service: Any = None
        self._address_book_service: Any = None
        self._kb_root: Path | None = None
        # Registered workers, synchronous callers and startup share the complete
        # provider operation, including both sides of chunk/record replacement.
        self._activity_lock = threading.RLock()
        # Single-slot background executor for audit_retrieval_corpus_cron
        # (B-M6 fast-return-contract fix). See _search_plugin_mixin.py.
        self._kb_audit_executor = BoundedSummaryExecutor(name="kb-retrieval-audit")

    def get_schema_definitions(self) -> list[SchemaDefinition]:
        return [get_knowledge_schema()]

    def prepare_for_readiness(self) -> None:
        """Acquire service dependencies from orchestrator."""
        if not self.orchestrator_ref:
            raise RuntimeError(f"{PLUGIN_NAME}: orchestrator_ref not set")

        state_service = self.orchestrator_ref.get_service("state_service")
        if state_service is None:
            raise RuntimeError(f"{PLUGIN_NAME}: state_service not available")
        self._state_service = state_service

        memory_service = self.orchestrator_ref.get_service("memory_service")
        if memory_service is None:
            raise RuntimeError(f"{PLUGIN_NAME}: memory_service not available")
        self._memory_service = memory_service

        embedding_service = self.orchestrator_ref.get_service("embedding_service")
        if embedding_service is None:
            raise RuntimeError(f"{PLUGIN_NAME}: embedding_service not available")
        self._embedding_service = embedding_service

        self._address_book_service = self.orchestrator_ref.get_service("address_book_service")
        if self._address_book_service is None:
            logger.debug(f"{PLUGIN_NAME}: address_book_service not available (optional)")

        config_manager = getattr(self.orchestrator_ref, "config_manager", None)
        if config_manager is None:
            raise RuntimeError(f"{PLUGIN_NAME}: config_manager not available")
        config = config_manager.get_plugin_config_provider(self.name)
        if not config:
            raise RuntimeError(f"{PLUGIN_NAME}: Plugin configuration not found - ensure config file exists at profile/config/plugins/{self.name}.json")
        self.config_provider = config

        kb_root_str = str(config.get("knowledge_base_root", ""))
        if not kb_root_str:
            raise RuntimeError(f"{PLUGIN_NAME}: knowledge_base_root not configured")
        self._kb_root = Path(kb_root_str)

        logger.debug(f"{PLUGIN_NAME}: ready (kb_root={self._kb_root})")
        self.set_ready()

    async def start_services(self) -> ActionResult:
        """Start knowledge services."""
        if self._services_started:
            return _lifecycle_completed()
        self._services_started = True
        logger.debug(f"{PLUGIN_NAME}: services started")
        return _lifecycle_completed()

    def auto_install_knowledge_bases(
        self,
        manifest_plugin_set: set[str] | None = None,
    ) -> None:
        """Public entry point for startup-sequence to trigger KB auto-install.

        ``manifest_plugin_set`` is forwarded to the lifecycle helper so
        plugin-owned KBs whose plugin has left the manifest are
        auto-uninstalled symmetrically with the install pass. Passing
        ``None`` preserves prior behaviour (install only).

        Per W5.P §4.1: the spawn-path ``purge_orphaned_chunks`` call has
        been REMOVED. Orphan cleanup is now operator-fired via the
        ``service_interface::knowledge_service::purge_orphaned_chunks``
        verb (default dry-run, batched hard-delete on confirm). Removing
        the spawn-path purge brings cutover cost back to O(plugin count);
        the spawn-path purge accumulated 271,605 archived rows by
        2026-06-13 and blew the 600s router-registration timeout.

        With no install records, the startup pass defaults to full speed and
        smallest-first order so a small KB becomes searchable before larger
        corpora complete. Later passes use alphabetical order and duty 0.05.
        An explicit ``SOLET_KB_INSTALL_DUTY`` overrides either duty. The pass
        stops between chunks once the orchestrator's shutdown event is set;
        an interrupted install resumes at the next start (prb_2e7713d4).
        """
        if self._kb_root is None:
            return
        _run_auto_install(
            self._kb_root,
            self._state_service,
            self._memory_service,
            self._address_book_service,
            manifest_plugin_set=manifest_plugin_set,
            budget=self._embedding_budget(),
            control_factory=lambda first_install: paced_install_control(self._shutdown_check(), first_install=first_install),
        )

    def _shutdown_check(self) -> Callable[[], bool]:
        """True once the orchestrator's shutdown event is set; an orchestrator without one is reported, never refused."""
        event: _ShutdownEvent | None = getattr(self.orchestrator_ref, "shutdown_event", None)
        if event is None:
            logger.warning("%s: the orchestrator has no shutdown_event; the knowledge-base auto-install cannot stop early at shutdown", PLUGIN_NAME)
            return lambda: False
        return event.is_set

    def _embedding_budget(self) -> TokenBudget | None:
        """The bound embedding provider's per-input budget; every index path splits to it (iss_9166af93)."""
        if self._embedding_service is None:
            raise RuntimeError(f"{PLUGIN_NAME}: embedding_service not available")
        budget: TokenBudget | None = self._embedding_service.input_token_budget()
        return budget

    async def stop_services(self) -> ActionResult:
        """Stop knowledge services."""
        if not self._services_started:
            return _lifecycle_completed()
        if self.is_active_interface_provider():
            return _lifecycle_refusal()
        self._services_started = False
        logger.debug(f"{PLUGIN_NAME}: services stopped")
        return _lifecycle_completed()


def _serialized_activity[**P, T](operation: Callable[Concatenate[DefaultKnowledgePlugin, P], T]) -> Callable[Concatenate[DefaultKnowledgePlugin, P], T]:
    """One reentrant provider seam, preserving each synchronous signature/result.

    Composition such as test_retrieval -> search stays on the current thread.
    Knowledge's synchronous memory dependency must never call back from a
    different worker into this owner while the current body is waiting on it.
    """

    @wraps(operation)
    def run(plugin: DefaultKnowledgePlugin, *args: P.args, **kwargs: P.kwargs) -> T:
        with plugin._activity_lock:
            return operation(plugin, *args, **kwargs)

    return run


# Bind the shared adapter once; no per-verb worker or per-verb wrapper body.
# list_installed and the existing audit admissions remain bounded controls.
_SERIAL_KNOWLEDGE_ACTIVITIES = frozenset({"archive_file", "browse", "create_file", "delete_file", "edit_file", "ingest", "install", "purge_orphaned_chunks", "read_file", "refresh_plugin_process", "refresh_plugin_processes", "search", "search_planning_references", "test_retrieval", "uninstall", "update"})
for _activity_name in _SERIAL_KNOWLEDGE_ACTIVITIES:
    setattr(DefaultKnowledgePlugin, _activity_name, _serialized_activity(cast(Callable[..., Any], getattr(DefaultKnowledgePlugin, _activity_name))))
