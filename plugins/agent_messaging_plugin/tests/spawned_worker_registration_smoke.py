#!/usr/bin/env python3
"""Offline smoke for spawned CLAUDE-worker peer registration.

RCA of record: workbench/2026-08-13_registration_loss_rca_lane_r_report.md.
Two spawned tmux workers ran multi-hour productive lives on 2026-08-13/14 and
never appeared in ``peer_list`` — unreachable by ``peer_send``, contributing
zero liveness, reaped mid-programme. Root cause was an asymmetry: the managed
Codex adapters arm a presence watcher and resolve an absolute CLI, the Claude
adapters did neither.

Named mutations this suite must catch:

* a Claude adapter exporting the BARE command name ``solet-bridge`` as
  ``AGENT_WAKE_CLI`` again (dies FileNotFoundError, silently, under the
  minimal PATH a tmux pane or a materialized release actually runs with);
* the tmux ``-e`` allowlist dropping ``PATH`` again (52edfb559's lesson,
  learned on the Codex path only);
* either Claude adapter shipping NO presence watcher on the ``watch``
  transport — the registration loss itself;
* a Claude sidecar copying Codex's ``--no-spool`` verbatim, which would
  re-deafen the very Stop-hook wake the sidecar exists to enable;
* a sidecar CLAIMING a role at launch instead of registering presence only
  (``--no-claim`` is what keeps role ownership model-initiated);
* arming the sidecar on the ``mcp`` transport, where the bridge registers;
* the ``/rename`` skill and its seed template drifting out of lock-step, or
  either one deriving ``WATCH_ID`` when a managed ``AGENT_INSTANCE_ID`` is
  available; bare watches must retain the derived ``agi-watch-*`` fallback;
* an existing watcher and an existing application bridge being confused
  before a second watcher is armed.

No tmux server, bridge, database, or model turn is used.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin.headless_adapter import (  # noqa: E402
    HeadlessHostDriver,
    _arm_watcher,
)
from agent_messaging_plugin.local_cli.spool import (  # noqa: E402
    resolve_watch_instance_id,
    watch_instance_digest,
)
from agent_messaging_plugin.models import (  # noqa: E402
    WATCH_AGENT_INSTANCE_PREFIX,
)
from agent_messaging_plugin.solet_cli import (  # noqa: E402
    expose_worker_cli,
    resolve_solet_bin,
    watch_sidecar_argv,
)
from agent_messaging_plugin.tmux_adapter import (  # noqa: E402
    _env_pairs,
    _pane_command,
)

_RENAME_SKILL = REPO_ROOT / ".claude" / "commands" / "rename.md"
_RENAME_TEMPLATE = (
    REPO_ROOT / "plugins" / "github_midwife_plugin" / "knowledge_base"
    / "hydration_templates" / "rename_skill_SKILL.md.template"
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _executable(directory: Path, name: str) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o755)
    return str(target)


def _env_map(pairs: list[str]) -> dict[str, str]:
    """``["-e", "K=V", ...]`` -> ``{"K": "V"}``, asserting the -e pairing."""
    out: dict[str, str] = {}
    for flag, assignment in zip(pairs[::2], pairs[1::2], strict=True):
        assert flag == "-e", f"expected -e flag, got {flag!r}"
        key, _, value = assignment.partition("=")
        out[key] = value
    return out


def _tmux_env(solet_bin: str, *, transport: str = "watch") -> dict[str, str]:
    return _env_map(_env_pairs(
        agent_instance_id="agi-test", agent_session_id="ases-agi-test",
        label="lane-x", solet_name="testsolet", solet_bin=solet_bin,
        allowed_tools=(), transport=transport,
        context_gauge_reporter_path=(
            Path(solet_bin).parent / "rotation_due_watch.py"
        ),
        # The spawn's RESOLVED lane root (2026-09-07, iss_2236d297) -- these
        # CLI/PATH assertions are indifferent to which root it is, so this
        # passes the same directory the fixture already built.
        cwd=Path(solet_bin).parent,
    ))


def _managed_identity_precedence_is_documented(body: str) -> bool:
    return all(
        fragment in body
        for fragment in (
            'WATCH_ID="${AGENT_INSTANCE_ID:-}"',
            'if [[ -z "$WATCH_ID" ]]; then',
            'shasum -a 256 | cut -c1-24',
        )
    )


def _rename_recipe(body: str, cli_name: str) -> str:
    for block in body.split("```bash")[1:]:
        if 'WATCH_CLI=' in block and 'PEER_LIST=' in block and 'WATCH_BRANCH=' in block:
            return block.split("```", 1)[0].replace("{{SOLET_NAME}}", cli_name)
    return ""


def _run_rename_recipe(
    recipe: str, *, cli_name: str, case: str, session_id: str, managed_id: str,
) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bindir = root / "bin"
        bindir.mkdir()
        call_log = root / "calls.log"
        peer_file = root / "peer.json"
        process_file = root / "process.txt"
        resolved_id = managed_id if case != "bare-watcher" else resolve_watch_instance_id(session_id, None)
        has_row = case != "absence"
        peer_data = {"result": {"success": True, "data": {"instances": {
            "claude_code": ([{"agent_instance_id": resolved_id, "parent_pid": 4242}] if has_row else [])
        }}}}
        peer_file.write_text(__import__("json").dumps(peer_data), encoding="utf-8")
        process_command = f"{cli_name} watch --no-claim" if case in {"managed-watcher", "bare-watcher"} else "claude mcp application-bridge"
        process_file.write_text(process_command, encoding="utf-8")
        cli = bindir / cli_name
        cli.write_text(
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$*" >> "$CALL_LOG"\n'
            'if [ "$1" = call ] && [ "$2" = plugin::agent_messaging_plugin::peer_list ]; then cat "$PEER_FILE"; fi\n',
            encoding="utf-8",
        )
        cli.chmod(0o755)
        ps = bindir / "ps"
        ps.write_text('#!/bin/sh\ncat "$PROCESS_FILE"\n', encoding="utf-8")
        ps.chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
            "CALL_LOG": str(call_log),
            "PEER_FILE": str(peer_file),
            "PROCESS_FILE": str(process_file),
            "AGENT_SESSION_ID": session_id,
            "AGENT_IDENTITY": "claude_code",
            "ARGUMENTS": "role-fixture",
            "ROLE": "role-fixture",
        }
        if case != "bare-watcher":
            env["AGENT_INSTANCE_ID"] = managed_id
        else:
            env.pop("AGENT_INSTANCE_ID", None)
        result = subprocess.run(["bash", "-c", recipe], env=env, capture_output=True, text=True, check=False)
        calls = call_log.read_text(encoding="utf-8").splitlines() if call_log.exists() else []
        return result, calls, resolved_id

def test_tmux_env_exports_absolute_cli_and_path() -> None:
    print("\ntmux adapter exports an absolute CLI and a PATH that finds it")
    with tempfile.TemporaryDirectory() as tmp:
        bin_dir = Path(tmp) / "venv" / "bin"
        bin_dir.mkdir(parents=True)
        solet_bin = _executable(bin_dir, "solet-bridge")
        env = _tmux_env(solet_bin)
    _check(
        env.get("AGENT_WAKE_CLI") == solet_bin,
        "AGENT_WAKE_CLI is the ABSOLUTE binary, not the bare name 'solet-bridge'",
    )
    _check(
        env.get("AGENT_WAKE_CLI") != "solet-bridge",
        "AGENT_WAKE_CLI is not the bare command name (the silent-death shape)",
    )
    _check("PATH" in env, "PATH crosses the tmux -e allowlist boundary")
    _check(
        env.get("PATH", "").split(os.pathsep)[0] == str(bin_dir),
        "the CLI's directory is PREPENDED to PATH, so a bare `solet-bridge` resolves",
    )
    _check(
        env.get("AGENT_WAKE_CLI") != env.get("SOLET_NAME"),
        "AGENT_WAKE_CLI is the binary, never the solet INSTANCE name",
    )


def test_tmux_pane_command_arms_registration_sidecar_with_spool() -> None:
    print("\ntmux pane command arms the presence sidecar (watch transport)")
    with tempfile.TemporaryDirectory() as tmp:
        bin_dir = Path(tmp)
        solet_bin = _executable(bin_dir, "solet-bridge")
        pane = _pane_command(
            ["claude", "--print"], label="lane-x",
            solet_bin=solet_bin, transport="watch",
        )
        mcp_pane = _pane_command(
            ["claude", "--print"], label="lane-x",
            solet_bin=solet_bin, transport="mcp",
        )
        no_cli_pane = _pane_command(
            ["claude", "--print"], label="lane-x",
            solet_bin="", transport="watch",
        )
    _check(
        f"{shlex.quote(solet_bin)} watch" in pane or f"{solet_bin} watch" in pane,
        "a `solet-bridge watch` sidecar is armed at all — THE registration step",
    )
    _check("--no-claim" in pane, "the sidecar registers presence WITHOUT claiming a role")
    _check(
        "--no-spool" not in pane,
        "the Claude sidecar keeps its spool (wake_waiter.py consumes it) — "
        "copying Codex's --no-spool would re-deafen the wake",
    )
    _check("--exit-with-parent $$" in pane, "the sidecar dies with the pane's shell")
    # find(), not index(): a mutation that removes the sidecar must RED this
    # check, not raise ValueError out of the suite (a crash reports no verdict
    # for the remaining legs at all).
    _check(
        0 <= pane.find(" watch ") < pane.find("exec "),
        "the sidecar is backgrounded BEFORE exec replaces the shell",
    )
    _check(
        pane.rstrip().endswith(shlex.join(["claude", "--print"])),
        "the pane still execs the worker command last",
    )
    _check("watch" not in mcp_pane, "no sidecar on the mcp transport (the bridge registers)")
    _check(
        "watch" not in no_cli_pane and no_cli_pane.endswith(
            f"exec {shlex.join(['claude', '--print'])}",
        ),
        "an unresolvable CLI degrades to the pre-fix pane, never a broken one",
    )


def test_headless_env_and_watcher() -> None:
    print("\nheadless adapter: absolute CLI, PATH, and a tracked sidecar")
    calls: list[dict[str, Any]] = []

    class _FakeProc:
        pid = 4242
        stdin = None
        stdout = None
        stderr = None

        def poll(self) -> int | None:
            return None

    def _popen(argv: list[str], **kwargs: Any) -> Any:
        calls.append({"argv": argv, "env": dict(kwargs.get("env") or {})})
        return _FakeProc()

    with tempfile.TemporaryDirectory() as tmp:
        bin_dir = Path(tmp) / "venv" / "bin"
        bin_dir.mkdir(parents=True)
        solet_bin = _executable(bin_dir, "solet-bridge")
        driver = HeadlessHostDriver(
            claude_bin="/bin/true", solet_name="testsolet", solet_bin=solet_bin,
            transport="watch", cwd=Path(tmp),
            python_executable=str(bin_dir / "python3"), popen_fn=_popen,
        )
        env = driver._spawn_env(  # noqa: SLF001 -- offline shape assertion
            agent_instance_id="agi-test", agent_session_id="ases-agi-test",
            label="lane-x", allowed_tools=(), transport="watch",
            context_gauge_reporter_path=(
                Path(tmp) / ".claude" / "hooks" / "rotation_due_watch.py"
            ),
        )
        watcher = _arm_watcher(
            _popen, solet_bin, Path(tmp), 999, env, "watch",
        )
        none_on_mcp = _arm_watcher(
            _popen, solet_bin, Path(tmp), 999, env, "mcp",
        )

    _check(env.get("AGENT_WAKE_CLI") == solet_bin, "headless AGENT_WAKE_CLI is absolute")
    _check(
        env.get("AGENT_CONTEXT_GAUGE_REPORTER_PATH", "").endswith(
            ".claude/hooks/rotation_due_watch.py"
        ),
        "headless exports its designated gauge reporter path",
    )
    _check(
        env.get("PATH", "").split(os.pathsep)[0] == str(bin_dir),
        "headless PATH is prepended with the CLI's directory",
    )
    _check(watcher is not None, "headless arms a presence watcher on the watch transport")
    _check(none_on_mcp is None, "headless arms no watcher on the mcp transport")
    watch_calls = [c for c in calls if "watch" in c["argv"]]
    _check(len(watch_calls) == 1, "exactly one watcher spawned")
    # Every downstream leg reads watch_calls[0]; a mutation that arms NO
    # watcher must RED each of them rather than raising IndexError and
    # leaving the rest of the suite unreported.
    argv: list[str] = watch_calls[0]["argv"] if watch_calls else []
    _check(argv[:1] == [solet_bin], "the watcher runs the ABSOLUTE binary")
    _check("--no-claim" in argv, "headless watcher registers presence WITHOUT a role claim")
    _check(
        bool(argv) and "--no-spool" not in argv,
        "headless watcher keeps its spool for wake_waiter.py",
    )
    _check(
        "--exit-with-parent" in argv
        and argv[argv.index("--exit-with-parent") + 1] == "999",
        "the watcher is bound to the worker's pid, not its own",
    )


def test_resolver_falls_back_to_the_active_venv() -> None:
    print("\nCLI resolution survives a minimal PATH")
    with tempfile.TemporaryDirectory() as tmp:
        bin_dir = Path(tmp) / "venv" / "bin"
        bin_dir.mkdir(parents=True)
        solet_bin = _executable(bin_dir, "solet-bridge")
        fake_python = str(bin_dir / "python3")
        original = os.environ.get("PATH", "")
        try:
            os.environ["PATH"] = "/nonexistent-for-this-smoke"
            resolved = resolve_solet_bin(None, python_executable=fake_python)
        finally:
            os.environ["PATH"] = original
    _check(
        resolved == solet_bin,
        "resolves the venv-sibling CLI when PATH cannot (the release/pane case)",
    )
    _check(
        resolve_solet_bin(solet_bin, python_executable=fake_python) == solet_bin,
        "owned explicit path wins",
    )
    _check(
        resolve_solet_bin(None, python_executable="/nope/python3") == ""
        or Path(resolve_solet_bin(None, python_executable="/nope/python3")).is_absolute(),
        "unresolvable returns empty rather than a bogus relative path",
    )
    degraded: dict[str, str] = {"PATH": "/usr/bin"}
    expose_worker_cli(degraded, "")
    _check(
        degraded["AGENT_WAKE_CLI"] == "solet-bridge" and degraded["PATH"] == "/usr/bin",
        "an unresolved CLI degrades to the bare name and leaves PATH untouched",
    )
    absent = {"PATH": "/usr/bin:/bin"}
    expose_worker_cli(absent, "/a/solet-bridge")
    _check(
        absent["PATH"] == f"/a{os.pathsep}/usr/bin{os.pathsep}/bin",
        "an absent CLI directory is prepended to PATH",
    )
    idempotent = {"PATH": f"/a{os.pathsep}/usr/bin{os.pathsep}/bin"}
    expose_worker_cli(idempotent, "/a/solet-bridge")
    expose_worker_cli(idempotent, "/a/solet-bridge")
    _check(
        idempotent["PATH"] == f"/a{os.pathsep}/usr/bin{os.pathsep}/bin",
        "an already-leading CLI directory remains leading and unique across re-exposure",
    )
    later_and_duplicate = {"PATH": f"/usr/bin{os.pathsep}/a{os.pathsep}/bin{os.pathsep}/a"}
    expose_worker_cli(later_and_duplicate, "/a/solet-bridge")
    _check(
        later_and_duplicate["PATH"] == f"/a{os.pathsep}/usr/bin{os.pathsep}/bin",
        "later and duplicate CLI-directory entries are removed before one leading prepend",
    )


def test_sidecar_argv_contract() -> None:
    print("\nwatch_sidecar_argv encodes the presence-not-ownership contract")
    # Neither actual Codex host driver (codex_tmux.py, codex_app_server.py)
    # calls this shared helper -- they build their watch argv inline (see
    # codex_managed_session_smoke.py's spool-parity legs for the real,
    # CDX-06-updated Codex call-site behavior). This leg only pins the
    # helper's OWN mechanics: it must still honor whatever `spool` value a
    # future caller passes, in both directions -- not a claim about which
    # value any current caller uses.
    claude = watch_sidecar_argv("/x/solet-bridge", agent_id="claude_code", spool=True)
    codex = watch_sidecar_argv("/x/solet-bridge", agent_id="codex", spool=False)
    _check("--no-claim" in claude and "--no-claim" in codex, "--no-claim on BOTH runtimes")
    _check("--no-spool" not in claude, "spool=True never emits --no-spool")
    _check("--no-spool" in codex, "spool=False still emits --no-spool (helper mechanics only)")
    _check("--role" not in claude, "the sidecar never passes --role: presence, not ownership")
    _check(
        claude[claude.index("--agent-id") + 1] == "claude_code",
        "claude sidecar registers under the claude_code agent id",
    )


def test_presence_and_liveness_regress_independently() -> None:
    """RCA of record (Lane R, 2026-08-14): PRESENCE (the watch sidecar
    ``_pane_command`` arms) and LIVENESS (the ``AGENT_WAKE_CLI``/``PATH``
    export ``_env_pairs``/``expose_worker_cli`` build, which
    ``heartbeat_report_alive.py``'s bare ``subprocess.run(["solet-bridge", ...])``
    depends on to resolve) are two separate mechanisms built by two
    functions that never call each other or share mutable state. A future
    change could restore one without the other and still look green if
    nothing asserts them side by side. This pins that they redden
    independently: proof-by-mutation recorded in
    ``workbench/2026-08-13_presence_liveness_independence_smoke_report_lane_s.md``
    (each mutation applied by hand to src, one at a time, smoke run,
    reverted — never left in the committed tree).
    """
    print("\npresence (sidecar arm) and liveness (PATH/CLI export) regress independently")
    with tempfile.TemporaryDirectory() as tmp:
        bin_dir = Path(tmp) / "venv" / "bin"
        bin_dir.mkdir(parents=True)
        solet_bin = _executable(bin_dir, "solet-bridge")
        pane = _pane_command(
            ["claude", "--print"], label="lane-x",
            solet_bin=solet_bin, transport="watch",
        )
        env = _tmux_env(solet_bin)
        # Resolved while the stub `solet-bridge` still exists on disk -- shutil.which
        # stats the candidate, so this must run inside the tempdir's lifetime.
        liveness_ok = shutil.which("solet-bridge", path=env.get("PATH", "")) == solet_bin
    presence_ok = "watch" in pane and "--no-claim" in pane
    # The literal dependency heartbeat_report_alive.py's bare
    # `subprocess.run(["solet-bridge", ...])` has on PATH -- not a proxy for it.
    _check(
        presence_ok,
        "LEG 1 baseline: presence assertion is green (sidecar armed in the pane) -- "
        "named mutation: neuter the `if transport == 'watch' and solet_bin:` block "
        "in tmux_adapter.py::_pane_command; must redden ONLY this assertion",
    )
    _check(
        liveness_ok,
        "LEG 2 baseline: liveness assertion is green (PATH resolves the bare "
        "`solet-bridge` heartbeat_report_alive.py shells out to) -- named mutation: "
        "skip the PATH-prepend in solet_cli.py::expose_worker_cli; must redden "
        "ONLY this assertion",
    )
    _check(
        presence_ok and liveness_ok,
        "LEG 3: both assertions are green from the SAME baseline build, so a "
        "mutation applied to only one of _pane_command/expose_worker_cli has "
        "exactly one of these two checks available to redden -- see the "
        "report for the two separate single-mutation runs",
    )


def _rename_claim_contract(calls: list[str], identity: str, live: bool) -> bool:
    claims = [call for call in calls if "peer_claim_role" in call]
    if len(claims) != (1 if live else 0):
        return False
    return not live or f'"agent_instance_id": "{identity}"' in claims[0]


def _rename_arm_contract(calls: list[str], live: bool) -> bool:
    arms = [call for call in calls if call.startswith("watch ")]
    return len(arms) == (0 if live else 1)


def _rename_case_contract_holds(
    result: subprocess.CompletedProcess[str], calls: list[str],
    expected_branch: str, identity: str, expected_live: bool,
) -> bool:
    branch = f"WATCH_BRANCH={expected_branch} WATCH_ID={identity}"
    return (
        result.returncode == 0
        and branch in result.stdout
        and _rename_claim_contract(calls, identity, expected_live)
        and _rename_arm_contract(calls, expected_live)
    )


def _unsafe_bridge_mutant_violates_contract(
    recipe: str, cli_name: str, session_id: str, managed_id: str, identity: str,
) -> bool:
    mutant = recipe.replace("*) BRANCH=1c ;;", "*) BRANCH=1b ;;")
    result, calls, _ = _run_rename_recipe(
        mutant, cli_name=cli_name, case="managed-bridge",
        session_id=session_id, managed_id=managed_id,
    )
    return not _rename_case_contract_holds(result, calls, "1c", identity, True)


def _qualify_rename_case(
    name: str, recipe: str, cli_name: str, case: str,
    expected_branch: str, session_id: str, managed_id: str,
) -> None:
    result, calls, identity = _run_rename_recipe(
        recipe, cli_name=cli_name, case=case,
        session_id=session_id, managed_id=managed_id,
    )
    live = case != "absence"
    _check(
        _rename_case_contract_holds(result, calls, expected_branch, identity, live),
        f"{name}: {case} selects {expected_branch}, claims the resolved id, and "
        f"{'never arms a live incumbent' if live else 'arms only when absent'}",
    )
    if case == "managed-bridge":
        _check(
            _unsafe_bridge_mutant_violates_contract(recipe, cli_name, session_id, managed_id, identity),
            f"{name}: unsafe-bridge-arm mutant is killed",
        )


def test_rename_skill_and_template_stay_in_lockstep() -> None:
    print("\n/rename recipes execute incumbent reuse in origin-present and template-only modes")
    template = _RENAME_TEMPLATE.read_text(encoding="utf-8")
    origin = _RENAME_SKILL.read_text(encoding="utf-8") if _RENAME_SKILL.is_file() else None
    modes: list[tuple[str, list[tuple[str, str, str]]]] = []
    if origin is not None:
        modes.append(("origin-present", [("skill", origin, "solet-bridge"), ("template", template, "solet-fixture")]))
    modes.append(("template-only", [("template", template, "solet-fixture")]))
    session_id = "ases-agi-574487c6c5c922c8533b70e39e981031"
    managed_id = "agi-managed-watch-fixture-01"
    for mode, surfaces in modes:
        _check(bool(surfaces), f"{mode}: surface set is populated")
        for name, body, cli_name in surfaces:
            label = f"{mode}/{name}"
            role_variable = "$ARGUMENTS" if name == "skill" else "$ROLE"
            _check(role_variable in body, f"{label}: uses its render-specific role variable")
            _check(_managed_identity_precedence_is_documented(body), f"{label}: managed id wins; digest is bare-watch fallback")
            _check("A watcher does NOT register under `$AGENT_INSTANCE_ID`." not in body, f"{label}: does not contradict managed-id precedence")
            _check("{{SOLET_NAME}}" in body if name == "template" else "solet call" not in body, f"{label}: uses the appropriate client executable")
            recipe = _rename_recipe(body, cli_name)
            _check(bool(recipe), f"{label}: complete lookup/decision/claim/arm recipe is executable")
            for case, branch in (("managed-watcher", "1a"), ("managed-bridge", "1c"), ("bare-watcher", "1a"), ("absence", "1b")):
                _qualify_rename_case(label, recipe, cli_name, case, branch, session_id, managed_id)


def test_watch_id_recipe_matches_the_code() -> None:
    print("\nrename managed and bare identities match the Python resolver")
    session_id = "ases-agi-574487c6c5c922c8533b70e39e981031"
    managed_id = "agi-managed-watch-fixture-01"
    derived = "agi-watch-" + hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
    _check(
        resolve_watch_instance_id(session_id, None) == derived
        == f"{WATCH_AGENT_INSTANCE_PREFIX}{watch_instance_digest(session_id)}",
        "bare-watch fixture matches the Python digest implementation",
    )
    _check(
        resolve_watch_instance_id(session_id, managed_id) == managed_id,
        "managed identity takes precedence in the Python resolver",
    )


def main() -> int:
    tests = [
        test_tmux_env_exports_absolute_cli_and_path,
        test_tmux_pane_command_arms_registration_sidecar_with_spool,
        test_headless_env_and_watcher,
        test_resolver_falls_back_to_the_active_venv,
        test_sidecar_argv_contract,
        test_presence_and_liveness_regress_independently,
        test_rename_skill_and_template_stay_in_lockstep,
        test_watch_id_recipe_matches_the_code,
    ]
    for test in tests:
        test()
    print(f"\nPASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
