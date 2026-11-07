#!/usr/bin/env python3
"""Observe session-cron attempts without blocking: success is always unknown.

Plan amendment uev_c545d677: the strict landed backend owns the schema.
ScheduleWakeup wake_time carries requested delay text (delaySeconds=<n>),
not a computed instant. Prompt and CronCreate cron are verbatim input.
recurring/durable/noop/stop/reason have no backend slot and are not recorded.
Even noop/stop or an error response is an observed attempt. No response shape
is inferred. Fixtures are synthetic-input-schema-derived, never native output.
No local state, history, credential, log or cache reads/writes; no promotion.
One disclosed exception: an env-less session (no launcher identity) reads its
clone coordination receipt and its identity record through the read-only
_session_identity.py, to learn its own instance id.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_HOOK_DIRECTORY = str(Path(__file__).resolve().parent)
if _HOOK_DIRECTORY not in sys.path:
    sys.path.insert(0, _HOOK_DIRECTORY)
import _session_identity as session_identity  # noqa: E402 -- sibling helper beside this hook

_PROCESS_KEY = "plugin::fleet_maintenance_plugin::session_cron_observation"
# Documented environment settings: RPC default 5 seconds; input default 65536
# Unicode characters. These are RPC/input bounds, never battery deadlines.
_DEFAULT_BRIDGE_TIMEOUT_SECONDS = 5.0
_DEFAULT_MAX_INPUT_CHARS = 65536


def _settings() -> tuple[float, int]:
    timeout = float(os.environ.get("SESSION_CRON_BRIDGE_TIMEOUT_SECONDS", _DEFAULT_BRIDGE_TIMEOUT_SECONDS))
    limit = int(os.environ.get("SESSION_CRON_MAX_INPUT_CHARS", _DEFAULT_MAX_INPUT_CHARS))
    if not math.isfinite(timeout) or timeout <= 0 or limit <= 0:
        raise ValueError("invalid session-cron input/RPC configuration")
    return timeout, limit


def _warn(message: str) -> None:
    print(f"[session-cron-sentinel] {message}", file=sys.stderr)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing or unsupported {field}")
    return value


def _observation(event: dict[str, Any]) -> dict[str, str]:
    inputs = event.get("tool_input")
    if not isinstance(inputs, dict):
        raise ValueError("unsupported tool_input")
    observation = {
        "observation_kind": "capture",
        "tool_name": event["tool_name"],
        "agent_instance_id": _text(os.environ.get("AGENT_INSTANCE_ID"), "agent_instance_id"),
        "session_id": _text(event.get("session_id"), "session_id"),
        "created_at": datetime.now(UTC).isoformat(),
        "prompt": _text(inputs.get("prompt"), "prompt"),
    }
    if event["tool_name"] == "CronCreate":
        observation["cron_expression"] = _text(inputs.get("cron"), "cron")
    else:
        delay = inputs.get("delaySeconds")
        if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not math.isfinite(delay):
            raise ValueError("missing or unsupported delaySeconds")
        observation["wake_time"] = f"delaySeconds={delay}"
    return observation


def _persist(observation: dict[str, str], timeout: float, limit: int) -> None:
    payload = json.dumps({"operation": "record", "observation": observation}, ensure_ascii=False)
    result = subprocess.run(
        ["solet-bridge", "call", _PROCESS_KEY, payload],
        shell=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0 or len(result.stdout) > limit:
        raise ValueError("bridge returned no verified receipt")
    receipt = json.loads(result.stdout)
    data = receipt.get("result", {}).get("data", {})
    if receipt.get("status") != "completed" or receipt.get("result", {}).get("success") is not True:
        raise ValueError("bridge returned no verified receipt")
    _text(data.get("pass_id"), "stored pass id")
    if data.get("append_only") is not True:
        raise ValueError("bridge returned no append-only receipt")
    # Never relay bridge diagnostics or input-derived receipt text to the model.
    _warn("attempt observation persisted (append-only receipt); success unknown")


def main() -> int:
    try:
        timeout, limit = _settings()
        raw = sys.stdin.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("unsupported oversized event")
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise ValueError("unsupported event object")
        if event.get("tool_name") not in ("CronCreate", "ScheduleWakeup"):
            return 0
        _warn("session-scoped scheduling attempt observed; success unknown; not a durable duty")
        # Env-less session: its persisted identity supplies AGENT_INSTANCE_ID;
        # a no-op whenever a launcher exported one.
        session_identity.apply(__file__, payload_session_id=str(event.get("session_id") or ""))
        _persist(_observation(event), timeout, limit)
    except (ValueError, TypeError, AttributeError, OverflowError, OSError, subprocess.TimeoutExpired):
        _warn("unable to persist session-cron attempt; input/provenance or bridge receipt unavailable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
