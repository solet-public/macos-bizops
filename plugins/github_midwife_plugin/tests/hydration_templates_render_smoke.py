"""Hydration-templates render smoke.

Nothing renders `hydration_templates/*.template` in code — the runbook is
explicit that hydration is driving-agent-performed literal string
substitution, not genesis code. That means a bad template (a token that
doesn't survive substitution, a shell file that's syntactically broken
once rendered, a JSON file that isn't valid JSON once rendered) has no
other gate catching it before an agent renders it live into someone's
`~/.zshrc` or `.claude/settings.json`. This smoke simulates the
documented render contract (literal `{{TOKEN}}` replacement, per
`TEMPLATE_VARS.md`) against every template file and checks the result:

1. No `{{...}}`-shaped token survives substitution (an undocumented or
   misspelled token would otherwise render literally into the user's
   file).
2. Every `.zsh`-shaped template (and every `client/bin/*` script
   template) parses as valid zsh once rendered (`zsh -n`).
3. `claude_settings.json.template` is valid JSON once rendered, and each
   rendered command hook parses as shell.
4. `TEMPLATE_VARS.md`'s file-map table lists every `*.template` file on
   disk, and lists no file that isn't on disk (catches map drift).
5. The directory stays FLAT (the KB-ingestion exclude pattern relies on
   this, per `TEMPLATE_VARS.md`'s own note).
6. The generated CLAUDE.md and prompt hook lead with the no-MCP local
   `<name>` command, and never reintroduce the old "skip Step Zero until
   MCP is registered" guidance.

Run directly: ``.venv/bin/python3 plugins/github_midwife_plugin/tests/hydration_templates_render_smoke.py``.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "knowledge_base" / "hydration_templates"
_TEMPLATE_VARS = _TEMPLATES_DIR / "TEMPLATE_VARS.md"

_RENDER_TOKENS = {
    "{{HOMUNCULUS_NAME}}": "iris",
    "{{CLONE_DIR}}": "/Users/example/Workspace/iris",
    "{{HYDRATION_DATE}}": "2026-07-13",
    "{{BACKUP_PATH}}": "/Users/example/.zshrc.pre-iris-hydration-20260713",
}

_ZSH_SHAPED = {
    "zshrc.template",
    "homunculus.zsh.template",
    "claude_launcher.template",
    "launch.template",
    "fleet_functions.zsh.template",
}

_TOKEN_RE = re.compile(r"\{\{[A-Z_]+\}\}")

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str) -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _render(content: str) -> str:
    for token, value in _RENDER_TOKENS.items():
        content = content.replace(token, value)
    return content


def _check_directory_is_flat() -> None:
    entries = list(_TEMPLATES_DIR.iterdir())
    subdirs = [e for e in entries if e.is_dir()]
    _check("directory stays flat (no subdirectories)", not subdirs, f"found subdirectories: {subdirs}")


def _check_file_map_matches_disk() -> None:
    on_disk = {p.name for p in _TEMPLATES_DIR.glob("*.template")}
    documented = set(re.findall(r"`([a-zA-Z0-9_.]+\.template)`", _TEMPLATE_VARS.read_text(encoding="utf-8")))
    _check(
        "every on-disk template is documented in TEMPLATE_VARS.md's file map",
        on_disk <= documented,
        f"undocumented templates: {on_disk - documented}",
    )
    _check(
        "TEMPLATE_VARS.md documents no template absent from disk",
        documented <= on_disk,
        f"documented-but-missing templates: {documented - on_disk}",
    )


def _check_no_surviving_tokens(name: str, rendered: str) -> None:
    leftover = _TOKEN_RE.findall(rendered)
    _check(f"{name}: no undocumented {{...}} token survives render", not leftover, f"leftover tokens: {leftover}")


def _check_zsh_syntax(name: str, rendered: str) -> None:
    result = subprocess.run(
        ["zsh", "-n", "/dev/stdin"], input=rendered, capture_output=True, text=True, timeout=10,
    )
    _check(f"{name}: rendered content is valid zsh (zsh -n)", result.returncode == 0, result.stderr.strip())


def _check_json_syntax(name: str, rendered: str) -> None:
    try:
        json.loads(rendered)
        ok, detail = True, ""
    except json.JSONDecodeError as exc:
        ok, detail = False, str(exc)
    _check(f"{name}: rendered content is valid JSON", ok, detail)


def _iter_entry_commands(
    event_name: str, entry_index: int, entry: object,
) -> list[tuple[str, str]]:
    entry_hooks = entry.get("hooks") if isinstance(entry, dict) else None
    if not isinstance(entry_hooks, list):
        return []
    commands: list[tuple[str, str]] = []
    for hook_index, hook in enumerate(entry_hooks):
        if (
            isinstance(hook, dict)
            and hook.get("type") == "command"
            and isinstance(hook.get("command"), str)
        ):
            commands.append(
                (f"{event_name}[{entry_index}].hooks[{hook_index}]", hook["command"]),
            )
    return commands


def _iter_claude_hook_commands(config: object) -> list[tuple[str, str]]:
    if not isinstance(config, dict):
        return []
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        return []
    commands: list[tuple[str, str]] = []
    for event_name, event_entries in hooks.items():
        if not isinstance(event_entries, list):
            continue
        for entry_index, entry in enumerate(event_entries):
            commands.extend(_iter_entry_commands(event_name, entry_index, entry))
    return commands


def _check_claude_hook_commands_parse(name: str, rendered: str) -> None:
    config = json.loads(rendered)
    for label, command in _iter_claude_hook_commands(config):
        result = subprocess.run(
            ["zsh", "-n", "/dev/stdin"], input=command, capture_output=True, text=True, timeout=10,
        )
        _check(
            f"{name}: rendered hook command parses as zsh ({label})",
            result.returncode == 0,
            result.stderr.strip(),
        )


def _check_all_templates() -> None:
    for path in sorted(_TEMPLATES_DIR.glob("*.template")):
        rendered = _render(path.read_text(encoding="utf-8"))
        _check_no_surviving_tokens(path.name, rendered)
        if path.name in _ZSH_SHAPED:
            _check_zsh_syntax(path.name, rendered)
        if path.name == "claude_settings.json.template":
            _check_json_syntax(path.name, rendered)
            _check_claude_hook_commands_parse(path.name, rendered)


def _check_no_mcp_first_generated_guidance() -> None:
    _check_generated_agent_instruction_files()
    _check_generated_hook_surfaces()


def _check_generated_agent_instruction_files() -> None:
    rendered = {
        name: _render((_TEMPLATES_DIR / f"{name}.template").read_text(encoding="utf-8"))
        for name in ("AGENTS.md", "CLAUDE.md")
    }
    shared_snippets = (
        "iris call service_interface::knowledge_service::search",
        '"top_k": 8',
        "iris call service_interface::session_ledger_service::search_event_content",
        '"limit": 8',
        "<!-- BEGIN HOMUNCULUS HYDRATION -->",
        "<!-- END HOMUNCULUS HYDRATION -->",
        "MCP is optional and exists only for the operator who explicitly asks for it",
        "the default.",
        "There is no offline query script shipped in",
        "Before editing code, changing config, or explaining a subsystem",
        "search iris's knowledge base",
        "router bridge blue-green local deployment architecture",
    )
    stale_markers = (
        "Pre-boot exception",
        "until iris's MCP bridge is registered",
        "Once the bridge is up",
        "skip everything in this section",
        "plugins/default_knowledge_plugin/tools/query_knowledge_base.py",
        "Codex homunculus wake runbook",
    )
    for name, content in rendered.items():
        for snippet in shared_snippets:
            _check(
                f"{name}: shared bootstrap contains {snippet!r}",
                snippet in content,
                "paired instruction templates drifted",
            )
        for marker in stale_markers:
            _check(
                f"{name}: stale generated-guidance marker absent: {marker!r}",
                marker not in content,
                f"found {marker!r}",
            )

    _check(
        "AGENTS.md: runner-specific audience is generic",
        "AGENTS.md-convention\nsession" in rendered["AGENTS.md"],
        "must not hardcode one AGENTS.md-reading runner",
    )
    _check(
        "CLAUDE.md: runner-specific audience is Claude Code",
        "Claude Code session" in rendered["CLAUDE.md"],
        "missing Claude Code audience",
    )


def _check_generated_hook_surfaces() -> None:
    settings = _render(
        (_TEMPLATES_DIR / "claude_settings.json.template").read_text(encoding="utf-8"),
    )
    _check_settings_hook_content(settings)
    _check_rename_and_fleet_templates()
    _check_settings_hook_kb_guidance(settings)


_SETTINGS_HOOK_CHECKS: tuple[tuple[str, tuple[str, ...], tuple[str, ...], str], ...] = (
    (
        "settings hook: Step Zero names local command",
        ("use the local iris command to ask iris",),
        (),
        "missing local-command instruction",
    ),
    (
        "settings hook: carries Step Zero merge marker",
        ("HOMUNCULUS_STEP_ZERO_HOOK=iris",),
        (),
        "missing Step Zero merge marker",
    ),
    (
        "settings hook: Stop waker runs iris wake in a rewake-capable shape",
        (
            "HOMUNCULUS_WAKE_HOOK=iris",
            "exec iris wake",
            '[ \\"${FLEET_TRANSPORT:-watch}\\" = \\"watch\\" ]',
            '"asyncRewake": true',
            '"timeout": 86400',
            "exit 0",
        ),
        (),
        "Stop hook must arm the iris wake waker (asyncRewake, label-guarded, "
        "transport-guarded with the watch default, silent exit-0 no-op for "
        "unlabeled sessions)",
    ),
    (
        "settings hook: carries role reclaim merge marker",
        ("HOMUNCULUS_ROLE_RECLAIM_HOOK=iris",),
        (),
        "missing role reclaim merge marker",
    ),
    (
        "settings hook: does not skip Step Zero when MCP is absent",
        (),
        ("skip this instruction", "MCP bridge is not registered yet"),
        "found stale skip-on-no-MCP wording",
    ),
    (
        "settings hook: role reclaim follows the declared fleet transport",
        (
            "follows the declared fleet transport",
            "unset on this machine means watch, the iris watch registered-presence watcher",
            "peer_holds_role",
        ),
        ("skip role re-claim silently",),
        "SessionStart hook must route role reclaim through the rename skill's "
        "declared transport, with the iris watch watcher as the unset default",
    ),
)


def _missing_and_forbidden(
    text: str, required: tuple[str, ...], forbidden: tuple[str, ...],
) -> tuple[list[str], list[str]]:
    missing = [snippet for snippet in required if snippet not in text]
    present = [snippet for snippet in forbidden if snippet in text]
    return missing, present


def _check_settings_label_guards(settings: str) -> None:
    _check(
        "settings hooks: ALL THREE guard on the session label (user-scope safety)",
        settings.count('[ -n \\"$AGENT_SESSION_LABEL\\" ] &&') == 3
        and settings.count("|| true") == 2,
        "user-scope install requires every hook to no-op cleanly in unlabeled sessions",
    )


def _check_settings_hook_content(settings: str) -> None:
    _check_settings_label_guards(settings)
    for label, required, forbidden, detail in _SETTINGS_HOOK_CHECKS:
        missing, present = _missing_and_forbidden(settings, required, forbidden)
        _check(
            label,
            not missing and not present,
            f"{detail} (missing snippets: {missing}; forbidden snippets present: {present})",
        )


def _check_rename_and_fleet_templates() -> None:
    rename_skill = _render(
        (_TEMPLATES_DIR / "rename_skill_SKILL.md.template").read_text(encoding="utf-8"),
    )
    _check(
        "rename skill: watch is the declared default, transports never silently cross",
        "iris watch --role" in rename_skill
        and "declared, never probed, never silently crossed" in rename_skill
        and "FLEET_TRANSPORT:-watch" in rename_skill
        and "suggest MCP to make renaming work" in rename_skill
        and "peer_holds_role" in rename_skill,
        "rename skill must lead with the declared-transport contract "
        "(watch default, fail-loud, no silent transport crossing)",
    )
    fleet = _render(
        (_TEMPLATES_DIR / "fleet_functions.zsh.template").read_text(encoding="utf-8"),
    )
    _check(
        "fleet launcher: MCP development channel is opt-in, default off",
        "iris_FLEET_MCP_CHANNELS" in fleet
        and "exec claude \\\n      --dangerously-load-development-channels" not in fleet,
        "fleet template must not hardcode the MCP development-channel flag",
    )
    _check(
        "fleet launcher: exports the declared transport with the watch default",
        'FLEET_TRANSPORT="${iris_FLEET_TRANSPORT:-watch}"' in fleet,
        "fleet launcher must export FLEET_TRANSPORT from the per-name "
        "operator knob, defaulting to watch",
    )


def _check_settings_hook_kb_guidance(settings: str) -> None:
    _check(
        "settings hook: implementation/debugging requires KB search",
        "For implementation/debugging/design work" in settings
        and "search the iris knowledge base for the current design record" in settings,
        "missing implementation/debugging KB-first hook guidance",
    )
    _check(
        "settings hook: router/bridge confusion requires KB search",
        "router/bridge/blue-green questions especially require a KB search" in settings,
        "missing router/bridge hook guidance",
    )


def main() -> int:
    try:
        _check_directory_is_flat()
        _check_file_map_matches_disk()
        _check_all_templates()
        _check_no_mcp_first_generated_guidance()
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1

    print(f"hydration_templates_render_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
