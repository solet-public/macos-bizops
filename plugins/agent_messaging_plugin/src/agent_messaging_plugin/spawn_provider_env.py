"""Per-spawn Claude provider overlay: the host-applicability rule and the host spec entry.

``spawn_session`` accepts an optional ``provider_env`` object that selects the Claude
provider backend for one worker. This module holds the rule that decides whether a
resolved host can apply it, and the single helper that places it on the host spawn
spec. Nothing here touches the ledger: the overlay may carry credentials, so it is
never written to ManagedSessionSpec, the ledger row or the spawn detail.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from .register_unit_client import VerbError

if TYPE_CHECKING:
    from .session_lifecycle_verbs import SpawnSessionRequest

# Host drivers that apply the overlay to the worker's own environment. The tmux
# driver threads it onto the pane via `new-session -e` (tmux_adapter's
# _provider_env_pairs). headless does not read it, so a headless spawn with an
# overlay is refused rather than launched on the wrong provider.
HOSTS_APPLYING_PROVIDER_ENV: Final[frozenset[str]] = frozenset({"tmux"})


def refuse_unapplied_provider_env(req: SpawnSessionRequest, resolved_host: str) -> None:
    """Refuse a per-spawn provider overlay the resolved host cannot apply.

    Called after host resolution and before any worktree, ledger or dispatch side
    effect. A non-empty overlay passes only when the runtime is ``claude_code`` and
    the resolved host is in ``HOSTS_APPLYING_PROVIDER_ENV``. The refusal names the
    overlay KEYS and the host only, never values, which may be credentials.
    """
    if not req.provider_env:
        return
    keys = sorted(req.provider_env)
    if req.agent_runtime != "claude_code":
        raise VerbError(
            "provider_unsupported_for_runtime",
            f"provider_env is a Claude provider overlay; agent_runtime={req.agent_runtime!r} does not accept it (keys={keys}).",
        )
    if resolved_host not in HOSTS_APPLYING_PROVIDER_ENV:
        raise VerbError(
            "provider_selection_not_applied_by_host",
            f"provider_env (keys={keys}) is not applied by host {resolved_host!r}; spawn on a host that applies it (tmux), or omit provider_env.",
        )


def provider_env_host_spec(req: SpawnSessionRequest) -> Mapping[str, object]:
    """The host spawn spec entry carrying the overlay, or nothing for an ordinary spawn.

    The driver reads this key from the host spec only. It is never placed on the
    ledger row, the spawn detail or any register record.
    """
    if not req.provider_env:
        return {}
    return {"provider_env": dict(req.provider_env)}
