#!/usr/bin/env python3
"""Migrate a deployed solet's machine surface from homunculus-era names.

The P2 rename (2026-08-13) cut every live identifier over to solet forms:
the ``solet`` CLI console script, the ``SOLET_*`` environment family, the
``solet_name`` root-manifest key, launchd labels ``local.solet.<name>``, and
the ``--solet`` flag on the router/ingress/tunnel entry points. Deployed
machines carry the OLD names in persistent state that no release cutover
rewrites. This script migrates exactly that persistent surface, fail-loud:

  1. LaunchAgent plists in ~/Library/LaunchAgents whose Label / env /
     ProgramArguments carry homunculus-era names (label ``local.homunculus.*``
     and friends). The codex-wake toolchain (``com.openai.codex-*``) is a
     separate deployed system and is deliberately NOT touched (P3).
  2. ``~/.zshrc`` launcher functions: ``AGENT_WAKE_CLI="homunculus"`` values.
  3. The repo checkout's editable venv: reinstall agent_messaging_plugin so
     the ``solet`` console script replaces the ``homunculus`` shim (no alias
     is kept — no-shims culture).
  4. The checkout's root_manifest.yaml ``homunculus_name:`` key (a git-tracked
     file — on the origin checkout this lands with P2 itself; on an adopter's
     seed clone the seed update delivers it, and this step is a no-op guard).

Loaded agents are booted out before their plist is rewritten and bootstrapped
back afterwards under the new label/filename. Old plist files are removed,
not kept.

Run without flags for a DRY RUN (prints the full plan, mutates nothing).
Run with ``--apply`` to execute. Idempotent: already-migrated pieces are
reported and skipped. Any un-migratable state aborts before mutation.

Residual guard (manual, platform must be up): after relaunch, enumerate the
plugin-config store and fail loud on any ``homunculus_name`` key — see the
seed-update runbook step this script ships with.
"""
from __future__ import annotations

import argparse
import os
import plistlib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
ZSHRC = Path.home() / ".zshrc"

# Deployed systems that mention homunculus but migrate on their own tracks.
PLIST_SKIP_PREFIXES = ("com.openai.",)

ENV_RENAMES = re.compile(r"^HOMUNCULUS_(?=[A-Z_]+$)")
LABEL_RENAME = re.compile(r"^local\.homunculus\.")
ARG_FLAG = "--homunculus"


@dataclass
class PlistPlan:
    old_path: Path
    new_path: Path
    old_label: str
    new_label: str
    env_renames: list[tuple[str, str]] = field(default_factory=list)
    arg_renames: list[tuple[int, str, str]] = field(default_factory=list)
    was_loaded: bool = False


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _gui_domain() -> str:
    return f"gui/{os.getuid()}"


def _is_loaded(label: str) -> bool:
    probe = _run(["launchctl", "print", f"{_gui_domain()}/{label}"], check=False)
    return probe.returncode == 0


def _env_renames(payload: dict[str, object]) -> list[tuple[str, str]]:
    env_keys = [str(k) for k in payload.get("EnvironmentVariables", {})]  # type: ignore[union-attr]
    return [(k, ENV_RENAMES.sub("SOLET_", k)) for k in env_keys if ENV_RENAMES.match(k)]


def _arg_renames(payload: dict[str, object]) -> list[tuple[int, str, str]]:
    args = [str(a) for a in payload.get("ProgramArguments", [])]  # type: ignore[union-attr]
    return [(i, a, "--solet") for i, a in enumerate(args) if a == ARG_FLAG]


def _plan_one_plist(path: Path) -> PlistPlan | None:
    """Plan a single plist's migration; ``None`` when it needs nothing."""
    try:
        payload = plistlib.loads(path.read_bytes())
    except Exception as exc:  # noqa: BLE001 — refuse loudly below
        raise SystemExit(f"REFUSING: cannot parse {path}: {exc}") from exc
    label = str(payload.get("Label", ""))
    env_renames = _env_renames(payload)
    arg_renames = _arg_renames(payload)
    new_label = LABEL_RENAME.sub("local.solet.", label)
    if not (env_renames or arg_renames or new_label != label):
        return None
    return PlistPlan(
        old_path=path,
        new_path=path.with_name(path.name.replace("local.homunculus.", "local.solet.")),
        old_label=label,
        new_label=new_label,
        env_renames=env_renames,
        arg_renames=arg_renames,
        was_loaded=_is_loaded(label),
    )


def plan_plists() -> list[PlistPlan]:
    candidates = (
        path for path in sorted(LAUNCH_AGENTS.glob("*.plist"))
        if not path.name.startswith(PLIST_SKIP_PREFIXES)
    )
    return [plan for plan in map(_plan_one_plist, candidates) if plan is not None]


def apply_plist(plan: PlistPlan) -> None:
    payload = plistlib.loads(plan.old_path.read_bytes())
    payload["Label"] = plan.new_label
    env = payload.get("EnvironmentVariables")
    if env:
        for old_key, new_key in plan.env_renames:
            env[new_key] = env.pop(old_key)
    args = payload.get("ProgramArguments")
    if args:
        for idx, _, new in plan.arg_renames:
            args[idx] = new
    if plan.was_loaded:
        _run(["launchctl", "bootout", f"{_gui_domain()}/{plan.old_label}"], check=False)
    with plan.new_path.open("wb") as f:
        plistlib.dump(payload, f)
    if plan.new_path != plan.old_path:
        plan.old_path.unlink()
    if plan.was_loaded:
        _run(["launchctl", "bootstrap", _gui_domain(), str(plan.new_path)])


def plan_zshrc() -> int:
    if not ZSHRC.exists():
        return 0
    return ZSHRC.read_text(encoding="utf-8").count('AGENT_WAKE_CLI="homunculus"')


def apply_zshrc() -> None:
    text = ZSHRC.read_text(encoding="utf-8")
    ZSHRC.write_text(
        text.replace('AGENT_WAKE_CLI="homunculus"', 'AGENT_WAKE_CLI="solet"'),
        encoding="utf-8",
    )


def plan_manifest() -> bool:
    manifest = REPO / "root_manifest.yaml"
    return bool(re.search(r"^homunculus_name:", manifest.read_text(encoding="utf-8"), re.M))


def apply_manifest() -> None:
    manifest = REPO / "root_manifest.yaml"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(re.sub(r"^homunculus_name:", "solet_name:", text, count=1, flags=re.M),
                        encoding="utf-8")


def shim_state() -> tuple[bool, bool]:
    bin_dir = REPO / ".venv" / "bin"
    return (bin_dir / "homunculus").exists(), (bin_dir / "solet").exists()


def apply_reinstall() -> None:
    pip = REPO / ".venv" / "bin" / "pip"
    _run([str(pip), "install", "-e", str(REPO / "plugins" / "agent_messaging_plugin")])
    old_shim, new_shim = shim_state()
    if not new_shim:
        raise SystemExit("REFUSING to finish: reinstall did not create .venv/bin/solet")
    if old_shim:
        (REPO / ".venv" / "bin" / "homunculus").unlink()
        print("  removed lingering .venv/bin/homunculus shim (no-shims)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="execute (default: dry run)")
    args = parser.parse_args()
    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"== migrate_to_solet [{mode}] ==")

    plans = plan_plists()
    zsh_hits = plan_zshrc()
    manifest_old = plan_manifest()
    old_shim, new_shim = shim_state()

    print(f"LaunchAgents to migrate: {len(plans)}")
    for p in plans:
        loaded = "loaded" if p.was_loaded else "not loaded"
        print(f"  {p.old_path.name} → {p.new_path.name} [{loaded}]")
        print(f"    label {p.old_label} → {p.new_label}")
        for old_key, new_key in p.env_renames:
            print(f"    env {old_key} → {new_key}")
        for idx, old, new in p.arg_renames:
            print(f"    arg[{idx}] {old} → {new}")
    print(f"~/.zshrc AGENT_WAKE_CLI values to flip: {zsh_hits}")
    print(f"root_manifest.yaml old key present: {manifest_old}")
    print(f"venv shims: homunculus={old_shim} solet={new_shim}")

    if not args.apply:
        print("dry run complete — re-run with --apply to execute")
        return 0

    for p in plans:
        apply_plist(p)
        print(f"migrated {p.new_path.name}")
    if zsh_hits:
        apply_zshrc()
        print("migrated ~/.zshrc launcher functions")
    if manifest_old:
        apply_manifest()
        print("migrated root_manifest.yaml key")
    apply_reinstall()
    print("reinstalled agent_messaging_plugin; `solet` CLI active")
    print("NEXT (manual, from the runbook): refresh installed coordination-hooks "
          "plugin caches, wait for the platform log to go stable, then run the "
          "config-store residual check (fail loud on any homunculus_name key).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
