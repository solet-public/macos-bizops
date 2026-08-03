#!/usr/bin/env python3
"""Assert the repository marketplace resolves to the reviewed plugin bytes."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from _harness import PLUGIN_ROOT, Results, preflight  # noqa: E402

PLUGIN_NAME = "coordination-hooks"
# The marketplace name is per-homunculus (derived from HOMUNCULUS_NAME at
# hydration time, TEMPLATE_VARS.md {{MARKETPLACE_NAME}}) — never a fixed
# literal a shipped smoke can pin, so this checks shape, not a specific value.
MARKETPLACE_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?$")


def _object(path: Path) -> dict[str, Any]:
    value: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected object in {path}")
    return value


def _check_entry_against_manifest(
    entry: dict[str, Any], manifest: dict[str, Any], repo_root: Path, res: Results,
) -> None:
    """Cross-check one marketplace entry's source/policy against the plugin manifest."""
    source = entry.get("source")
    res.check(isinstance(source, dict), "plugin source is an object")
    if isinstance(source, dict):
        res.check(source.get("source") == "local", "marketplace source kind is local")
        path = source.get("path")
        res.check(isinstance(path, str), "marketplace local path is a string")
        if isinstance(path, str):
            resolved = (repo_root / path).resolve()
            res.check(resolved == PLUGIN_ROOT.resolve(), "marketplace resolves to reviewed plugin bytes", str(resolved))

    res.check(
        entry.get("policy") == {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "marketplace policy is the reviewed opt-in policy",
    )
    res.check(entry.get("category") == "Productivity", "marketplace category matches metadata")
    res.check(manifest.get("name") == PLUGIN_NAME, "plugin manifest identity matches marketplace")
    version = manifest.get("version")
    res.check(isinstance(version, str) and SEMVER.fullmatch(version) is not None, "plugin version is semver")
    interface = manifest.get("interface")
    res.check(isinstance(interface, dict), "plugin interface metadata is an object")
    if isinstance(interface, dict):
        res.check(interface.get("category") == entry.get("category"), "plugin category matches marketplace")
        res.check(interface.get("capabilities") == ["Hooks"], "plugin declares only the Hooks capability")


def main() -> int:
    preflight()
    res = Results("Codex marketplace consistency")
    repo_root = PLUGIN_ROOT.parents[3]
    marketplace_path = repo_root / ".agents" / "plugins" / "marketplace.json"
    marketplace = _object(marketplace_path)
    manifest = _object(PLUGIN_ROOT / ".codex-plugin" / "plugin.json")

    name = marketplace.get("name")
    res.check(
        isinstance(name, str) and MARKETPLACE_NAME_RE.fullmatch(name) is not None,
        "marketplace identity is a valid kebab-case name",
        str(name),
    )
    plugins = marketplace.get("plugins")
    res.check(isinstance(plugins, list), "marketplace plugins is a list")
    if not isinstance(plugins, list):
        return res.finish()
    matches = [entry for entry in plugins if isinstance(entry, dict) and entry.get("name") == PLUGIN_NAME]
    res.check(len(matches) == 1, "marketplace names the plugin exactly once", f"found {len(matches)}")
    if len(matches) != 1:
        return res.finish()

    _check_entry_against_manifest(matches[0], manifest, repo_root, res)
    return res.finish()


if __name__ == "__main__":
    raise SystemExit(main())
