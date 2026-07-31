"""Slice A smoke — validate the `macos-free-homunculus` genesis profile.

Loads `knowledge_base/profile_templates/macos-free-homunculus.yaml` and the
`knowledge_base/profile_baseline/*.json` per-plugin config templates with
plain `yaml`/`json` parsing (github_midwife_plugin ships no installable
code yet — Slice A is data-only, and the plugin must not cross-import
macos_midwife_plugin's loader per the own-copy-per-plugin convention).

Checks:
  1. Binding-satisfaction: every `service_bindings` value names a plugin in
     the `plugins:` allowlist; `inference_service` and
     `self_deployment_service` are asserted ABSENT (the declared-vacant /
     opt-in shape the design mandates), not merely unchecked.
  2. No orphan starting_actions: every `plugin::<name>::...` starting-action
     process_key names a plugin in the allowlist.
  3. Public-safe: no operator-identity path (`/Users/...`) or secret-shaped
     key (`api_key`, `password`, `secret`) appears anywhere in the profile
     YAML or any checked-in `profile_baseline/*.json` file — every
     `profile_baseline` file also parses as valid JSON.

Run directly: ``.venv/bin/python3
plugins/github_midwife_plugin/tests/macos_free_profile_smoke.py``.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_KB_ROOT = _REPO_ROOT / "plugins" / "github_midwife_plugin" / "knowledge_base"
_PROFILE_PATH = _KB_ROOT / "profile_templates" / "macos-free-homunculus.yaml"
_BASELINE_DIR = _KB_ROOT / "profile_baseline"

_OPERATOR_PATH_PATTERN = re.compile(r"/Users/[A-Za-z0-9_.-]+")
_SECRET_KEY_PATTERN = re.compile(r'"(api_key|password|secret)"\s*:', re.IGNORECASE)

# Service bindings the design explicitly mandates ABSENT from this profile
# (declared-vacant inference; opt-in self-deployment) rather than merely
# unchecked — an orphan-plugin check alone would silently pass a profile
# that never declared them at all, which is the point, but would equally
# silently pass a profile that bound them to something bogus with no
# allowlist entry required. Assert absence explicitly.
_MUST_BE_ABSENT_BINDINGS = ("inference_service", "self_deployment_service")

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str) -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _load_profile() -> dict[str, Any]:
    if not _PROFILE_PATH.is_file():
        raise SmokeFailureError(f"profile template missing: {_PROFILE_PATH}")
    raw = yaml.safe_load(_PROFILE_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SmokeFailureError(f"profile template did not parse to a mapping: {_PROFILE_PATH}")
    return raw


def _check_binding_satisfaction(profile: dict[str, Any], plugins: set[str]) -> None:
    bindings = profile.get("service_bindings") or {}
    _check(
        "service_bindings is a mapping",
        isinstance(bindings, dict),
        f"got {type(bindings).__name__}",
    )
    for service_name, plugin_name in bindings.items():
        _check(
            f"binding-satisfaction[{service_name}]",
            plugin_name in plugins,
            f"{service_name} -> {plugin_name!r} not in plugins allowlist",
        )
    for absent_binding in _MUST_BE_ABSENT_BINDINGS:
        _check(
            f"declared-absent[{absent_binding}]",
            absent_binding not in bindings,
            f"{absent_binding} must be absent (declared-vacant / opt-in shape) "
            f"but found bound to {bindings.get(absent_binding)!r}",
        )


def _check_no_orphan_starting_actions(profile: dict[str, Any], plugins: set[str]) -> None:
    starting_actions = profile.get("starting_actions") or []
    _check(
        "starting_actions is a list",
        isinstance(starting_actions, list),
        f"got {type(starting_actions).__name__}",
    )
    for action in starting_actions:
        name = action.get("name", "<unnamed>")
        process_key = action.get("process_key", "")
        if process_key.startswith("plugin::"):
            owner = process_key.split("::")[1]
            _check(
                f"no-orphan-starting-action[{name}]",
                owner in plugins,
                f"process_key {process_key!r} references plugin {owner!r} "
                "not in the plugins allowlist",
            )
        else:
            raise SmokeFailureError(
                f"starting_action {name!r} has an unrecognized process_key shape "
                f"{process_key!r} — this profile only expects plugin::<name>::... "
                "starting actions; extend the check before adding a service_interface:: one"
            )


def _check_public_safe(text: str, source: str) -> None:
    operator_hit = _OPERATOR_PATH_PATTERN.search(text)
    _check(
        f"no-operator-path[{source}]",
        operator_hit is None,
        f"found operator-identity path {operator_hit.group(0)!r}" if operator_hit else "",
    )
    secret_hit = _SECRET_KEY_PATTERN.search(text)
    _check(
        f"no-secret-key[{source}]",
        secret_hit is None,
        f"found secret-shaped key {secret_hit.group(1)!r}" if secret_hit else "",
    )


def _check_baseline_files_are_valid_json() -> list[Path]:
    if not _BASELINE_DIR.is_dir():
        raise SmokeFailureError(f"profile_baseline directory missing: {_BASELINE_DIR}")
    baseline_files = sorted(_BASELINE_DIR.glob("*.json"))
    _check(
        "profile_baseline has at least one file",
        len(baseline_files) > 0,
        f"no *.json files found under {_BASELINE_DIR}",
    )
    for path in baseline_files:
        text = path.read_text(encoding="utf-8")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SmokeFailureError(f"{path.name} is not valid JSON: {exc}") from exc
        _check(
            f"baseline-is-mapping[{path.name}]",
            isinstance(parsed, dict),
            f"{path.name} did not parse to a JSON object",
        )
    return baseline_files


def main() -> int:
    try:
        profile = _load_profile()
        plugins_raw = profile.get("plugins") or []
        _check(
            "plugins is a non-empty list of strings",
            isinstance(plugins_raw, list)
            and len(plugins_raw) > 0
            and all(isinstance(p, str) for p in plugins_raw),
            f"got {plugins_raw!r}",
        )
        plugins = set(plugins_raw)

        _check_binding_satisfaction(profile, plugins)
        _check_no_orphan_starting_actions(profile, plugins)

        profile_text = _PROFILE_PATH.read_text(encoding="utf-8")
        _check_public_safe(profile_text, _PROFILE_PATH.name)

        baseline_files = _check_baseline_files_are_valid_json()
        for path in baseline_files:
            _check_public_safe(path.read_text(encoding="utf-8"), path.name)

    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1

    print(
        f"macos_free_profile_smoke OK: {len(_CHECKS_RUN)} checks passed "
        f"({len(plugins)} plugins, {len(baseline_files)} baseline files)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
