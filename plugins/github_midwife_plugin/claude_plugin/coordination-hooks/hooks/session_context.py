#!/usr/bin/env python3
"""SessionStart / UserPromptSubmit additionalContext emitter for memory-passthrough.

A hook subprocess has no MCP bridge, so drain and hydrate are AGENT-mediated
(design §4.4-4.5). This emitter is how the loop reaches the agent: it prints a
``hookSpecificOutput.additionalContext`` block carrying the EXACT instruction
sequence (not left to convention) — the pending-journal count, the drain
process_call recipe, the SessionStart hydrate sequence, and the local-delete-vs-
canonical-forget rule.

  * SessionStart  -> the full hydrate + drain instruction sequence.
  * UserPromptSubmit -> a short drain nudge, emitted ONLY when the journal has
                        pending entries (silent otherwise, so it is not noise).

Bare-session guard: if the memory dir does not exist (a session not working in a
project with a memory dir), emit nothing and exit 0 — mirrors the shipped
SessionStart guard. Any failure exits 0 with no output (never disrupt a session).

Stdlib-only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# ruff: noqa: E402
# pyright: reportMissingImports=false
import _journal

# Rendered ABSOLUTE (this hook always has CLAUDE_PROJECT_DIR) and re-exported
# inline in every suggested command: the agent's Bash shell does NOT inherit
# hook env, and _journal.project_dir() fails fast rather than fall back to a
# cwd that can mint a wrong origin (claude_code.scratchpad, 2026-08-05).
_PROJECT_DIR = _journal.project_dir()
# Self-relative (R4 seed-packaging audit, Package B, 2026-08-10) rather than
# hardcoded to this checkout's own `.claude/hooks/memory_passthrough` layout
# -- the vendored plugin copy of this file lives at a different path
# (`${CLAUDE_PLUGIN_ROOT}/hooks/`), and this must always resolve to wherever
# THIS running copy actually is, so its printed Bash commands invoke the
# right sibling scripts regardless of which copy fired.
_HOOKS_DIR = str(Path(__file__).resolve().parent)
_ENV = f"CLAUDE_PROJECT_DIR={_PROJECT_DIR}"


def _spool_path() -> Path:
    return _journal.state_dir() / "spool" / "export.json"


def _hydrate_instructions(pending: int, origin_tag: str, spool: str) -> str:
    return (
        "MEMORY PASSTHROUGH (this local memory dir is a disposable projection of the homunculus's "
        "memory_service; see workbench/2026-07-16_unified_memory_passthrough_design_v2.md).\n"
        "\n"
        "HYDRATE (run once, early, before editing memory files) — regenerate the local "
        "projection from the canonical store, in THIS order:\n"
        f"  1. process_call service_interface::memory_service::export_memories with arguments "
        f'{{"tags": ["agent_memory", "{origin_tag}"], "file_path": "{spool}"}} '
        "(ALL-tag semantics keeps this to YOUR origin's records only).\n"
        f"  2. Bash: {_ENV} python3 {_HOOKS_DIR}/hydrate_render.py {spool}  "
        "(writes per-fact files + MEMORY.md, VERBATIM, and stamps hydrated hashes BEFORE any "
        "capture can fire so it does not echo back).\n"
        "  If the homunculus is DOWN (export fails): SKIP hydrate entirely — the last projection stays "
        "untouched; do not delete or half-write anything.\n"
        "\n"
        + _drain_instructions(pending)
    )


def _drain_instructions(pending: int) -> str:
    return (
        f"DRAIN ({pending} local memory edit(s) pending write-through to the homunculus) — flush the "
        "capture journal to the canonical store:\n"
        f"  1. Bash: {_ENV} python3 {_HOOKS_DIR}/drain.py  -> prints {{\"pending\", \"upserts\":[{{path, "
        'process_key, arguments}}], "skipped_deleted"}}.\n'
        "  2. For EACH upsert, issue process_call with its process_key "
        "(service_interface::memory_service::upsert_memory_by_tag) and its arguments verbatim "
        "(content + slot tag + provenance tags — one small call per entry, bridge-safe).\n"
        f"  3. After the upserts succeed, Bash: {_ENV} python3 {_HOOKS_DIR}/drain.py --advance "
        "(marks them drained; captures during the drain are picked up next time).\n"
        "\n"
        "DELETE vs FORGET: deleting a local memory file is a CACHE CLEAR — hydrate regenerates "
        "it, and drain SKIPS a deleted file (never upserts an empty one). To FORGET a fact "
        "canonically, call process_call service_interface::memory_service::delete_memories_by_tag "
        "with the fact's slot tag (agent_memory:slot:<origin>:<file-slug>); then it will not "
        "come back on hydrate.\n"
    )


def _emit(event: str, context: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {"hookEventName": event, "additionalContext": context}
    }))


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        event = payload.get("hook_event_name") if isinstance(payload, dict) else None
        if not _journal.memory_dir().exists():
            return 0  # bare/no-memory session: emit nothing
        pending = _journal.pending_count()
        if event == "SessionStart":
            _emit("SessionStart", _hydrate_instructions(
                pending, _journal.origin_tag(), str(_spool_path())
            ))
        elif event == "UserPromptSubmit" and pending > 0:
            _emit("UserPromptSubmit", _drain_instructions(pending))
    except Exception:  # noqa: BLE001 — never disrupt a session.
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
