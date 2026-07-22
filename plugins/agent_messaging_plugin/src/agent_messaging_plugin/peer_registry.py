"""Peer registry backed by the platform :class:`Store` abstraction.

A peer entry maps ``(agent_id, agent_instance_id) -> BridgeBinding``.
The registry is the authoritative routing table for ``peer_send``:
recipients are resolved by ``agent_id`` (with optional
``agent_instance_id`` disambiguation) and either delivered through a
registered ``NativeWakeAdapter`` (e.g., Claude Code's chat-style wake
path) or — when no adapter is registered — surfaced as a long-poll
channel event by the caller.

Backed by an in-memory :class:`Store` over
:func:`get_peer_binding_schema`.  The store owns the lock and the
``created_at`` / ``updated_at`` timestamps; the registry contributes
the routing semantics (cross-bucket sweep on register, single-row
lookup on resolve, group-by-agent_id on list).  The wake-adapter map
is unchanged — it's a transport-adapter table, not a row store, so
it stays as a bespoke dict-with-lock.

Ported from the now-deleted ``agent_channel_plugin`` (2026-05-16) during
the bridge-consolidation work — see
``workbench/2026-05-16_codex_mcp_channel_and_inter_agent_outstanding_work.md``
Phase 2d for the pickup contract.  Migrated to :class:`Store` (Phase 2
of ``workbench/2026-05-19_unified_store_abstraction.md``,
2026-05-19) so ``last_active_at`` semantics fall out of the platform's
``updated_at`` convention.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from ananta.services.store import Store, open_store

from .schema import (
    PEER_BINDING_NAMESPACE,
    get_peer_binding_schema,
)

if TYPE_CHECKING:
    from .models import BridgeBinding, NativeWakeAdapter

logger = logging.getLogger(__name__)


class PeerAmbiguousError(LookupError):
    """Raised when multiple bindings match a peer_id without a hint.

    ``candidate_instance_ids`` lists every ``agent_instance_id`` that
    could have been targeted; ``candidate_session_labels`` is the
    matching parallel list of human-facing labels so the caller can
    render a disambiguation prompt without another registry walk.
    """

    def __init__(
        self,
        peer_agent_id: str,
        candidate_instance_ids: list[str],
        candidate_session_labels: list[str],
    ) -> None:
        self.peer_agent_id = peer_agent_id
        self.candidate_instance_ids = candidate_instance_ids
        self.candidate_session_labels = candidate_session_labels
        rendered = ", ".join(
            f"{inst} (label={label!r})"
            for inst, label in zip(
                candidate_instance_ids, candidate_session_labels, strict=True,
            )
        )
        super().__init__(
            f"peer_ambiguous: multiple {peer_agent_id!r} instances "
            f"registered; pass peer_agent_instance_id to disambiguate. "
            f"Candidates: {rendered}",
        )


class PeerUnreachableError(LookupError):
    """Raised when a peer_id (or specific instance) has no live binding."""


class PeerSessionAmbiguousError(LookupError):
    """Raised when >1 live binding shares one ``agent_session_id`` (fail-loud dup).

    A stable ``agent_session_id`` identifies exactly one logical session, which
    holds at most one live bridge at a time — so a session-id resolving to
    multiple bindings is a registry-hygiene invariant violation, not a
    disambiguation prompt. The §5.4 handover-notify path catches this (its resolve
    is best-effort and never gates a claim), but the primitive itself fails loud
    so the invariant breach surfaces rather than a silent arbitrary pick.
    """

    def __init__(self, agent_session_id: str, candidate_instance_ids: list[str]) -> None:
        self.agent_session_id = agent_session_id
        self.candidate_instance_ids = candidate_instance_ids
        super().__init__(
            f"peer_session_ambiguous: {len(candidate_instance_ids)} live bindings "
            f"share agent_session_id {agent_session_id!r}: "
            f"{', '.join(candidate_instance_ids)}",
        )


class PeerRegistry:
    """Owns the per-agent_id peer registry + native wake adapters.

    Threading: the store owns the per-row mutation lock, so no
    registry-level lock is needed for bindings.  A separate mutex
    still guards the wake-adapter table (which is a transport-adapter
    map, not a row store) so a wake registration during dispatch
    can't deadlock against an in-flight resolve.
    """

    def __init__(
        self,
        *,
        bindings_store: Store | None = None,
        state_service: object | None = None,
    ) -> None:
        if bindings_store is None:
            if state_service is None:
                raise ValueError(
                    "PeerRegistry requires either bindings_store (tests) or "
                    "state_service (production) to open its persistent backend",
                )
            # Load-order dependency: the ``"postgres"`` factory is registered
            # by whichever vault plugin imports its ``postgres_backend``
            # module first. agent_messaging's ``start_interface`` runs after
            # vault's ``prepare_for_readiness``, so the registration is
            # already in place by the time we get here. A future profile
            # shipping without any vault plugin would surface this gap; see
            # ``workbench/2026-06-01_local_reconnect_ux_design.md`` §4 +
            # the pause-and-ping discussion 2026-06-01.
            bindings_store = open_store(
                get_peer_binding_schema(),
                namespace=PEER_BINDING_NAMESPACE,
                backend="postgres",
                state_service=state_service,
            )
        self._bindings: Store = bindings_store
        self._wake_adapters: dict[str, NativeWakeAdapter] = {}
        self._wake_adapters_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Binding lifecycle
    # ------------------------------------------------------------------

    def register(self, binding: BridgeBinding) -> str:
        """Add or replace ``binding`` under its ``agent_id`` bucket.

        Returns the **effective** ``session_label`` that landed in the
        store, which may differ from ``binding.session_label`` when
        preserve-on-empty restores a previously-stored label (see
        below). Callers (the HTTP ``peer/register`` route, smokes) use
        the return value to echo the persisted state back to the
        client rather than the incoming value.

        Cross-bucket sweep: any existing row sharing this
        ``bridge_id`` OR this ``agent_instance_id`` OR this
        ``session_label`` (when non-empty) is hard-deleted first.
        This preserves the pre-Store semantics where a relabel or
        manual reassignment of a live bridge — a row that moved to
        a different ``agent_id`` bucket — gets cleaned up cleanly,
        AND ensures the **single-active-session-per-name** invariant
        (operator directive 2026-06-09): when a new session claims a
        name via ``/rename`` (which fires ``peer_register`` with the
        new session_label), the previous holder of that label is
        evicted from the registry. Without the session_label sweep
        the prior holder's row would persist across the rename
        because both ``bridge_id`` and ``agent_instance_id`` are
        new on the claiming session.  Then insert the fresh row.

        Preserve-on-empty (2026-06-01 §4.2): if the incoming
        ``binding.session_label`` is empty and a stored row for this
        ``agent_instance_id`` already carries a non-empty label, the
        stored label flows into the new row. The auto-reconnect path's
        ``_register_identity`` always sends the subprocess's stale
        empty cache; the operator's last ``/rename`` is the authority.
        A non-empty incoming label overwrites (explicit beats
        implicit; matches address_book most-recent-claim semantics).

        The read + two deletes + one insert are not atomic at the
        store level. Two threads racing to register the same bridge
        can interleave; this matches the prior in-memory implementation
        under fast reconnect storms, and is acceptable at our scale.
        A future iteration can add a store-level ``replace_by``
        primitive if profiling shows it.
        """
        effective_label = binding.session_label
        if not effective_label:
            existing = self._bindings.read_one(
                {"agent_instance_id": binding.agent_instance_id},
            )
            if existing is not None:
                stored = str(existing.get("session_label") or "")
                if stored:
                    effective_label = stored
        self._bindings.delete(
            {"bridge_id": binding.bridge_id}, soft_delete=False,
        )
        self._bindings.delete(
            {"agent_instance_id": binding.agent_instance_id},
            soft_delete=False,
        )
        # Single-active-session-per-name invariant: evict any prior
        # holder of this session_label so the registry never carries
        # two rows answering to the same display name. Skipped when
        # the effective label is empty (a transient pre-rename state
        # that legitimately leaves the label slot vacant).
        if effective_label:
            self._bindings.delete(
                {"session_label": effective_label}, soft_delete=False,
            )
        self._bindings.insert(
            {
                "agent_id": binding.agent_id,
                "agent_instance_id": binding.agent_instance_id,
                "bridge_id": binding.bridge_id,
                "session_label": effective_label,
                "parent_pid": binding.parent_pid,
                "agent_session_id": binding.agent_session_id,
            },
        )
        logger.info(
            "peer registered: %s/%s (label=%r, parent_pid=%s) -> bridge %s",
            binding.agent_id,
            binding.agent_instance_id,
            effective_label,
            binding.parent_pid,
            binding.bridge_id,
        )
        return effective_label

    def unregister(self, bridge_id: str) -> int:
        """Remove every binding owned by ``bridge_id``; return count removed."""
        removed = self._bindings.delete(
            {"bridge_id": bridge_id}, soft_delete=False,
        )
        if removed:
            logger.info(
                "peer unregistered: bridge %s gone (%d binding(s))",
                bridge_id, removed,
            )
        return removed

    def touch_binding(self, agent_instance_id: str) -> int:
        """Bump ``updated_at`` on the binding for ``agent_instance_id``.

        Called from the dispatch path (``peer_send``,
        ``peer_inbox``, native-wake delivery) so ``updated_at``
        carries "last active" semantics that the canonical platform
        timestamp convention already wires through every query.
        Returns the count of rows touched (0 if no binding matches).
        """
        return self._bindings.touch({"agent_instance_id": agent_instance_id})

    def stamp_model_activity(self, agent_instance_id: str, stamp: str) -> int:
        """Mirror ``last_model_activity_at`` onto the binding for ``agent_instance_id``.

        REL-05: the durable half of the in-memory ``BridgeSessionState`` stamp,
        so the server-side sweep can read model-activity liveness without the
        live session. Distinct from ``touch_binding`` (which is ``updated_at``,
        bumped by every dispatch/inbox/register); this is the model-activity-only
        signal that the consumption reconciler and the deaf-wake census key on.
        Returns the count of rows updated (0 if no binding matches).
        """
        return self._bindings.update(
            {"agent_instance_id": agent_instance_id},
            {"last_model_activity_at": stamp},
        )

    def list_agent_ids(self) -> dict[str, list[BridgeBinding]]:
        """Snapshot the registry keyed by ``agent_id`` for peer_list responses."""
        rows = self._bindings.read()
        grouped: dict[str, list[BridgeBinding]] = {}
        for row in rows:
            agent_id = str(row["agent_id"])
            grouped.setdefault(agent_id, []).append(_binding_from_row(row))
        return {
            agent_id: sorted(
                bindings,
                key=lambda b: b.agent_instance_id,
            )
            for agent_id, bindings in grouped.items()
        }

    def list_by_bridge(self, bridge_id: str) -> list[BridgeBinding]:
        """Snapshot every binding owned by ``bridge_id``.

        Per the 2026-06-13 coding-agent-inference design v4 §4: callers
        that need the per-bridge binding set BEFORE the bridge closes
        (e.g. to clean up a per-binding sidecar inside the same
        ``close_bridge`` route handler) snapshot through this helper
        rather than scanning ``list_agent_ids`` linearly.
        """
        rows = self._bindings.read({"bridge_id": bridge_id})
        return [_binding_from_row(row) for row in rows]

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve(
        self,
        peer_id: str,
        peer_agent_instance_id: str | None = None,
    ) -> BridgeBinding:
        """Pick the recipient ``BridgeBinding`` for a ``peer_send`` call.

        Three rules:

        1. ``peer_agent_instance_id`` supplied → return that exact
           binding; raise ``PeerUnreachableError`` if no match.
        2. Exactly one binding under ``peer_id`` → return it.
        3. Multiple bindings → raise ``PeerAmbiguousError`` carrying
           the candidate ``agent_instance_id`` and ``session_label``
           lists so the caller can prompt for disambiguation.
        """
        if peer_agent_instance_id is not None:
            row = self._bindings.read_one(
                {
                    "agent_id": peer_id,
                    "agent_instance_id": peer_agent_instance_id,
                },
            )
            if row is None:
                raise PeerUnreachableError(
                    f"peer_unreachable: no binding for "
                    f"{peer_id!r}/{peer_agent_instance_id!r}",
                )
            return _binding_from_row(row)
        rows = self._bindings.read({"agent_id": peer_id})
        if not rows:
            raise PeerUnreachableError(
                f"peer agent_id {peer_id!r} is not currently "
                f"registered (no live bindings)",
            )
        if len(rows) == 1:
            return _binding_from_row(rows[0])
        candidates = sorted(
            (_binding_from_row(r) for r in rows),
            key=lambda b: b.agent_instance_id,
        )
        raise PeerAmbiguousError(
            peer_agent_id=peer_id,
            candidate_instance_ids=[c.agent_instance_id for c in candidates],
            candidate_session_labels=[c.session_label for c in candidates],
        )

    def resolve_by_agent_session_id(
        self, agent_session_id: str,
    ) -> BridgeBinding | None:
        """Reverse-lookup the live binding for a stable ``agent_session_id``.

        The §5.4 handover-notify path uses this to route a displaced-holder notice
        to the prior holder's CURRENT bridge (found via its stable session id)
        rather than the stale ``agent_instance_id`` recorded on the role binding
        — which has rotated by the time a reconnect displaces it. Returns ``None``
        for an empty session id or no match (best-effort: nothing to notify);
        raises :class:`PeerSessionAmbiguousError` on >1 match (fail-loud dup — a
        session holds at most one live bridge).
        """
        if not agent_session_id:
            return None
        rows = self._bindings.read({"agent_session_id": agent_session_id})
        if not rows:
            return None
        if len(rows) > 1:
            raise PeerSessionAmbiguousError(
                agent_session_id,
                [str(row.get("agent_instance_id") or "") for row in rows],
            )
        return _binding_from_row(rows[0])

    def agent_session_id_for_instance(self, agent_instance_id: str) -> str:
        """The stable ``agent_session_id`` bound to ``agent_instance_id`` (or "").

        The claim path sources the claimant's session id from its OWN live
        ``peer_binding`` row here (REL-07 finding 1) — NEVER from claim args, which
        never carry it — so the written role binding gets a non-empty
        ``agent_session_id`` and the reconnect CAS (keyed on it alone) can re-point
        the role. Empty when the instance is not registered (the caller falls back
        to leaving the column empty — no worse than the pre-fix state).
        """
        if not agent_instance_id:
            return ""
        row = self._bindings.read_one({"agent_instance_id": agent_instance_id})
        return str(row.get("agent_session_id") or "") if row is not None else ""

    # ------------------------------------------------------------------
    # Native wake adapters
    # ------------------------------------------------------------------

    def register_native_wake_adapter(
        self, agent_id: str, adapter: NativeWakeAdapter,
    ) -> None:
        """Store ``adapter`` keyed by ``agent_id``; re-register replaces silently.

        Adapters are typically singleton per agent kind (e.g., the
        plugin self-registers a ``claude_code`` adapter when it boots
        its IO surface), so last-write-wins is the right semantics.
        """
        with self._wake_adapters_lock:
            self._wake_adapters[agent_id] = adapter
        logger.info(
            "native wake adapter registered for agent_id=%r", agent_id,
        )

    def wake_adapter_for(self, agent_id: str) -> NativeWakeAdapter | None:
        """Return the registered ``NativeWakeAdapter`` for ``agent_id`` or None."""
        with self._wake_adapters_lock:
            return self._wake_adapters.get(agent_id)


def _binding_from_row(row: dict[str, object]) -> BridgeBinding:
    """Build a typed :class:`BridgeBinding` view over one store row."""
    from .models import BridgeBinding  # noqa: PLC0415 — break import cycle
    parent_pid_raw = row.get("parent_pid")
    parent_pid = (
        int(parent_pid_raw)
        if isinstance(parent_pid_raw, int)
        else None
    )
    return BridgeBinding(
        bridge_id=str(row["bridge_id"]),
        agent_id=str(row["agent_id"]),
        agent_instance_id=str(row["agent_instance_id"]),
        session_label=str(row.get("session_label") or ""),
        parent_pid=parent_pid,
        created_at=str(row.get("created_at") or ""),
        updated_at=str(row.get("updated_at") or ""),
        agent_session_id=str(row.get("agent_session_id") or ""),
    )


__all__ = [
    "PeerAmbiguousError",
    "PeerRegistry",
    "PeerSessionAmbiguousError",
    "PeerUnreachableError",
]
