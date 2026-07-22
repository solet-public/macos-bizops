"""Stable identifiers for ``agent_messaging_plugin``."""

from typing import Final

PLUGIN_NAME: str = "agent_messaging_plugin"

# The runner process the channel plugin submits per turn.
RUN_TURN_NAME: str = "run_turn"

# The synthetic identity ``PermissiveBearerVerifier`` stamps on every caller
# when ``streamable_no_auth=True`` — authenticated by NO bearer, so both the
# ``agent_id`` and ``agent_instance_id`` carry this sentinel. Lives here (the
# leaf constants module) rather than in ``mcp_streamable.auth`` so the auth,
# dispatch, and platform-surface layers can all import it without a package
# import cycle. Keyed off by the B1 Slice-A untrusted-principal gate.
TUNNEL_PASSTHROUGH_SENTINEL: Final[str] = "tunnel_passthrough"
