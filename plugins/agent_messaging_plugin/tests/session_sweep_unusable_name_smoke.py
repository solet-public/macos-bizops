#!/usr/bin/env python3
"""Unusable solet identity keeps rotation notices appended and driven with an advisory."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from session_sweep_smoke import (  # noqa: E402
    HolderClaim,
    _check,
    _failed,
    _NoticeChannel,
    _notify_rotation_due,
    _wired,
    claim_role_binding_v4,
)


def test_unusable_solet_name_advises_and_keeps_driver() -> None:
    for value in (None, "", "   "):
        state, reg, mgr, bridge_id = _wired("ases-steward")
        claim_role_binding_v4(state, name="-Main", claim=HolderClaim(
            holder_kind="session", holder_identity={"agent_id": "claude_code"},
            agent_instance_id="agi-steward", agent_session_id="ases-steward", session_label="display-only",
        ))
        channel = _NoticeChannel()
        with patch.dict(os.environ):
            os.environ.pop("SOLET_NAME", None)
            if value is not None:
                os.environ["SOLET_NAME"] = value
            with patch("agent_messaging_plugin.session_lifecycle_control._resolve_driver_channel", return_value=channel):
                with patch("agent_messaging_plugin.session_sweep.logger.warning") as advisory:
                    delivered = _notify_rotation_due(
                        state=state, peer_registry=reg, bridge_manager=mgr, row={"current_tokens": 900_000},
                        agent_instance_id="agi-worker", spawner_instance_id="agi-steward",
                    )
        _check((delivered, len(channel.sent), len(mgr.get(bridge_id).events_after(-1)[1])) == (True, 1, 1),
               f"unusable SOLET_NAME={value!r}: native driver retained and event readable")
        _check(any("ADVISORY" in call.args[0] for call in advisory.call_args_list),
               f"unusable SOLET_NAME={value!r}: visible ADVISORY despite matching suffix-only binding")


def main() -> int:
    test_unusable_solet_name_advises_and_keeps_driver()
    print(f"FAILED: {len(_failed)}")
    return int(bool(_failed))


if __name__ == "__main__":
    sys.exit(main())
