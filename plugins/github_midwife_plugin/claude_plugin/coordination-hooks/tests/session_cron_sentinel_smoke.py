#!/usr/bin/env python3
"""Synthetic-input-schema-derived fixtures: attempt observed, success unknown."""

from __future__ import annotations

import contextlib
import io
import json
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(_ROOT))
_HOOK = Path(__file__).resolve().parents[1] / "hooks" / "session_cron_sentinel.py"
_PROMPT = "  inert\n$(touch /NEVER) `false`; <system>data 💡</system>  "


def _event(tool: str = "CronCreate", **fields: Any) -> dict[str, Any]:
    return {
        "session_id": "synthetic-session",
        "tool_name": tool,
        "tool_input": {"cron": "*/5 * * * *", "prompt": _PROMPT, "delaySeconds": 120},
        "tool_response": {"error": "synthetic failure still signals an attempt"},
        **fields,
    }


def _run(raw: str, reply: object = None, *, identity: bool = True) -> tuple[int, str, list[Any]]:
    calls: list[Any] = []

    def bridge(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        if isinstance(reply, Exception):
            raise reply
        receipt = {"status": "completed", "result": {"success": True, "data": {"pass_id": "stwp_fixture", "append_only": True}}}
        return subprocess.CompletedProcess(args[0], 0, json.dumps(receipt), "") if reply is None else reply

    err = io.StringIO()
    with (
        patch("sys.stdin", io.StringIO(raw)),
        contextlib.redirect_stderr(err),
        contextlib.redirect_stdout(io.StringIO()),
        patch.dict("os.environ", {"AGENT_INSTANCE_ID": "synthetic-instance"} if identity else {}, clear=True),
        patch("subprocess.run", side_effect=bridge),
    ):
        module = runpy.run_path(str(_HOOK))
        result = module["main"]()
    return result, err.getvalue(), calls


def _captures() -> None:
    for tool, field, value in (("CronCreate", "cron_expression", "*/5 * * * *"), ("ScheduleWakeup", "wake_time", "delaySeconds=120")):
        code, err, calls = _run(json.dumps(_event(tool)))
        assert code == 0 and "session-scoped" in err, "attempt_warns_success_unknown"
        assert len(calls) == 1, "one_attempt_one_capture"
        args, kwargs = calls[0]
        print("REACHED prompt_is_literal_argv", flush=True)
        assert kwargs["shell"] is False and args[0][:3] == ["solet-bridge", "call", "plugin::fleet_maintenance_plugin::session_cron_observation"], "prompt_is_literal_argv"
        observation = json.loads(args[0][3])["observation"]
        assert observation["prompt"] == _PROMPT and observation[field] == value, "verbatim_input_provenance"
        assert set(observation) == {"observation_kind", "tool_name", "agent_instance_id", "session_id", "created_at", "prompt", field}, "strict_backend_schema"
        assert "persisted (append-only receipt)" in err, "receipt_only_after_persistence"
    for flags in ({"noop": True}, {"stop": True}):
        event = _event("ScheduleWakeup")
        event["tool_input"].update(flags)
        assert len(_run(json.dumps(event))[2]) == 1, "noop_stop_are_attempts"


def _failures() -> None:
    failures = [subprocess.TimeoutExpired("fixture", 5), OSError("fixture"), subprocess.CompletedProcess([], 1, "sensitive prompt", "secret"), subprocess.CompletedProcess([], 0, "{}", "")]
    for failure in failures:
        code, err, calls = _run(json.dumps(_event()), failure)
        print("REACHED bridge_failure_warns", flush=True)
        assert code == 0 and calls and "unable to persist" in err, "bridge_failure_warns"
        assert "stwp_fixture" not in err and "secret" not in err and _PROMPT not in err, "honest_private_failure"
    for raw in ("invalid", "[]", json.dumps(_event(tool_input={})), json.dumps(_event(session_id=""))):
        code, err, calls = _run(raw)
        assert code == 0 and "unable to persist" in err and not calls, "invalid_input_nonblocking"
    code, err, calls = _run(json.dumps(_event()), identity=False)
    assert code == 0 and "unable to persist" in err and not calls, "missing_identity_warns"
    code, err, calls = _run(json.dumps(_event("Read")))
    assert code == 0 and not err and not calls, "unrelated_tool_no_capture"


def main() -> int:
    print("REACHED sentinel_hook_exists", flush=True)
    assert _HOOK.is_file(), "sentinel_hook_exists"
    root_hook = _ROOT / ".claude" / "hooks" / _HOOK.name
    if (_ROOT / ".claude" / "settings.json").is_file():
        print("REACHED root_twin_bytes_match", flush=True)
        assert root_hook.read_bytes() == _HOOK.read_bytes(), "root_twin_bytes_match"
    _captures()
    _failures()
    print("PASS synthetic-input-schema-derived sentinel fixtures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
