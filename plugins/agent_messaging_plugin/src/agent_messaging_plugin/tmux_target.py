"""Build the exact tmux target for a recorded session reference.

A plain ``-t NAME`` is a prefix match, so it can reach a session the reference never named.
Session verbs (has-session, set-environment, show-environment, kill-session) take the exact form
``=NAME``; list-panes and the pane verbs (send-keys, capture-pane, display-message, paste-buffer)
need ``=NAME:``, and the colonless form gives them a prefix match or a silent wrong answer.
"""

from __future__ import annotations


def _require_exact_name(ref: str) -> None:
    """Refuse a reference that cannot be one session's exact name.

    tmux turns ':' and '.' in a created session's name into '_', so such a reference can never
    match; a leading '=' would double the exact marker.
    """
    if not ref or ":" in ref or "." in ref or ref.startswith("="):
        raise ValueError(f"tmux session reference {ref!r} is not an exact session name")


def session_target(ref: str) -> str:
    """Return ``=NAME``, the exact target for has-session, set-environment, show-environment and kill-session.

    Not for list-panes: on tmux 3.7b ``list-panes -t =tt-beta`` still prefix-matches and lists the
    panes of ``tt-beta-77``. Use ``pane_target`` for list-panes.
    """
    _require_exact_name(ref)
    return "=" + ref


def pane_target(ref: str) -> str:
    """Return ``=NAME:``, the exact target for list-panes, display-message, send-keys, capture-pane and paste-buffer.

    The colon is required. Without it ``list-panes -t =tt-beta`` reaches ``tt-beta-77`` and
    ``display-message -t =NAME`` prints an empty line. With a reference that names no session,
    display-message still exits 0 and prints an empty line, so read the output, not the exit code.
    """
    _require_exact_name(ref)
    return "=" + ref + ":"
