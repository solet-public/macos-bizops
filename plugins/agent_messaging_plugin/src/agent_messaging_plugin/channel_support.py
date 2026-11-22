"""Where Claude Code surfaces development-channel events.

One home for the third-party-provider predicate, so the tmux spawn path (which
omits ``--dangerously-load-development-channels`` where it is inert) and the
MCP bridge (which declares ``wake_capable`` at registration) read the same
fact. Standard library only: the bridge starts under a client startup timeout.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import BridgeBinding

# Third-party-provider fix (§39.1/§40.1, reported and field-verified by a seed
# adopter): on a third-party inference provider, Claude Code IGNORES
# --dangerously-load-development-channels ("... ignored (server:<name>)" /
# "Channels are not available on third-party providers") and never raises the
# interactive confirmation. The expect loop in tmux_adapter has exactly two
# branches -- prompt-appeared -> confirm, no-prompt -> assume-hung -> KILL -- so
# a fully-booted worker was killed after DEFAULT_CONFIRM_TIMEOUT_SECONDS on
# every such spawn, making the swap-durable tmux fleet unspawnable on exactly
# the provider configuration an enterprise adopter without Anthropic-API access
# is forced onto.
#
# The fix is to omit the flag when it is already inert, so no prompt is ever
# expected: with the flag absent, _needs_dev_channels_confirmation returns False
# and the loop is skipped entirely. ONE mechanism, not two -- deliberately NOT
# also adding a pane-text success marker for the "ignored" lines, because a
# second guard would make this guard's smoke legs vacuous. The Anthropic /
# no-provider path is byte-for-byte unchanged (flag present, prompt-and-confirm
# exactly as before).
#
# EVIDENCE BOUND ON THIS TUPLE: CLAUDE_CODE_USE_BEDROCK is the ONLY marker with
# live evidence behind it (the adopter's verified spawn). Other third-party
# providers plausibly have their own switches, but we have no third-party
# endpoint in this checkout and no field report naming one, so adding a marker
# here on inference would be an unmarked guess -- a wrong name is silently inert
# and a right-name-wrong-semantics entry would omit the flag on an Anthropic
# spawn. Extend this tuple ONLY with a measured spawn or an adopter report.
THIRD_PARTY_PROVIDER_ENV_MARKERS = ("CLAUDE_CODE_USE_BEDROCK",)


def provider_ignores_dev_channels(env: Mapping[str, str]) -> bool:
    """True when ``env`` selects a third-party provider that ignores
    ``--dangerously-load-development-channels``. Adopter's shape, bound to OUR
    effective-env computation rather than to their overlay."""
    return any(str(env.get(marker, "")).strip() == "1" for marker in THIRD_PARTY_PROVIDER_ENV_MARKERS)


def bridge_wake_capable(*, is_claude_code: bool, env: Mapping[str, str]) -> bool:
    """Whether a bridge's session receives channel events, so a delivery wakes it.

    Declared from the bridge's own environment, never probed from another
    process: true only for Claude Code on a provider that surfaces development
    channels.
    """
    return is_claude_code and not provider_ignores_dev_channels(env)


def channel_carries_message(binding: BridgeBinding, *, native_wake_adapter: bool) -> bool:
    """Whether a send that just queued a message for ``binding`` has put it on a
    channel the session surfaces, so a "delivery waiting" pane notice would only
    repeat it (prb_57c118ad).

    True only when the send went through the native wake adapter (the same
    bridge event queue the session reads as a channel notification), the
    bridge declared ``wake_capable`` (see :func:`bridge_wake_capable`), and the
    holder is not a watcher: a watcher is a pull consumer, so the queued event
    surfaces nothing until it polls and its pane notice is its only wake.
    ``native_wake_adapter`` is the caller's own fact (it knows which delivery
    path it took); this function never probes the session.
    """
    return native_wake_adapter and binding.wake_capable and not binding.is_watcher
