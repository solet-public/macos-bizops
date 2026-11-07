"""Read-only permission observations; never alter launch policy or lifecycle."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Literal

from .session_hosts import resolve_host_driver
from .tmux_adapter import TmuxHostDriver

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)
type PermissionPromptState = Literal["blocked", "not_detected", "unknown", "not_applicable"]
PROMPT_TAIL_LINES = 15
PROMPT_QUESTION = "Do you want to proceed?"
PROMPT_OPTION_ONE = re.compile(r"^\s*(?:❯\s*)?1\.\s+Yes\b")


def _prompt_shape_visible(text: str) -> bool:
    """A live prompt is the question line, alone, in the pane tail, with option 1 after it."""
    tail = [line for line in text.splitlines() if line.strip()][-PROMPT_TAIL_LINES:]
    for index, line in enumerate(tail):
        if line.strip() == PROMPT_QUESTION:
            return any(PROMPT_OPTION_ONE.match(later) for later in tail[index + 1 :])
    return False


def permission_prompt_state(row: Mapping[str, object]) -> PermissionPromptState:
    """Observe the visible Claude tmux pane; unreadable never means clear."""
    if row.get("agent_runtime") != "claude_code" or row.get("host") != "tmux":
        return "not_applicable"
    host_ref = str(row.get("host_ref") or "")
    if not host_ref:
        return "unknown"
    try:
        driver, _host = resolve_host_driver("tmux", "claude_code")
        if not isinstance(driver, TmuxHostDriver):
            return "unknown"
        text = driver.capture_pane_text(host_ref)
    except Exception:  # noqa: BLE001 -- optional observation must not block ledger reads
        logger.warning("Permission prompt observation unavailable for %s", host_ref, exc_info=True)
        return "unknown"
    if not text.strip():
        return "unknown"
    return "blocked" if _prompt_shape_visible(text) else "not_detected"


def permission_mode_advisory(agent_runtime: str, permission_mode: str) -> str:
    """Describe a resolved managed-launch mode without changing its value."""
    if agent_runtime != "claude_code" or permission_mode == "bypassPermissions":
        return ""
    return f"Managed Claude permission_mode={permission_mode!r}; unattended work may wait for permission approval. Launch mode is unchanged."
