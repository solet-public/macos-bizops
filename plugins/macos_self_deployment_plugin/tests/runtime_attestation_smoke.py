#!/usr/bin/env python3
"""T1 runtime-attestation smoke; uses only a temporary immutable release."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
for _path in (_ROOT / "ananta" / "src", _ROOT / "plugins" / "macos_self_deployment_plugin" / "src"):
    sys.path.insert(0, str(_path))

from macos_self_deployment_plugin.plugin import MacosSelfDeploymentPlugin  # noqa: E402


class _Manager:
    def __init__(self, root: Path) -> None:
        self.releases_root = root
        self.current_release = "rel-smoke"


class _Router:
    def status(self) -> dict[str, str]:
        return {"active_instance_id": "instance-smoke", "active_color": "blue"}


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        releases = Path(directory)
        code = releases / "rel-smoke" / "code"
        for relative, content in (
            ("ananta/a.py", "a = 1\n"),
            ("plugins/example/src/example/loaded.py", "value = 1\n"),
            ("plugins/example/src/example/unimported.py", "value = 2\n"),
            ("solet_cli/a.py", "a = 1\n"),
            ("solet_setup_contracts/src/a.py", "a = 1\n"),
        ):
            target = code / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        version = releases / "rel-smoke" / "VERSION"
        version.write_text(json.dumps({"manifest_etag": "etag-smoke"}), encoding="utf-8")
        loaded_path = code / "plugins/example/src/example/loaded.py"
        loaded = types.ModuleType("example.loaded")
        loaded.__file__ = str(loaded_path)
        sys.modules["example.loaded"] = loaded
        original_release = os.environ.get("SOLET_RELEASE_ID")
        os.environ["SOLET_RELEASE_ID"] = "rel-smoke"
        try:
            plugin = MacosSelfDeploymentPlugin()
            plugin._release_manager = _Manager(releases)  # noqa: SLF001
            plugin._router_client = _Router()  # noqa: SLF001
            plugin._self_instance_id = "instance-smoke"  # noqa: SLF001
            plugin._self_color = "blue"  # noqa: SLF001
            result = plugin.attest_runtime_code(
                reconciliation_id="rec-smoke",
                verification_modules=("example.loaded", "example.unimported"),
            )
            checks = [
                (result["served_by_self"] is True, "router-served identity"),
                (result["release_id"] == "rel-smoke", "immutable release identity"),
                (str(result["source_surface_sha256"]).startswith("sha256:"), "source digest"),
                (str(result["release_surface_sha256"]).startswith("sha256:"), "release digest"),
                (result["modules"][0]["loaded"] is True, "loaded module observed"),
                (result["modules"][1]["loaded"] is False, "unimported module not imported"),
            ]
            action_result = plugin.attest_runtime_code_action(
                {
                    "reconciliation_id": "rec-smoke",
                    "verification_modules": ["example.loaded"],
                },
                {},
            )
            checks.extend(
                [
                    (
                        action_result["action_status"] == "completed",
                        "platform-process wrapper resolves and runs",
                    ),
                    (
                        action_result["data"]["served_by_self"] is True,
                        "platform-process wrapper returns served proof",
                    ),
                    (
                        "attest_runtime_code" in plugin.get_edge_process_definitions(),
                        "edge process definition registered",
                    ),
                ]
            )
            plugin._router_client = type("_WrongRouter", (), {"status": lambda self: {"active_instance_id": "other", "active_color": "green"}})()  # noqa: E501, SLF001
            checks.append((plugin.attest_runtime_code(reconciliation_id="", verification_modules=())["served_by_self"] is False, "non-self route is exposed"))
        finally:
            sys.modules.pop("example.loaded", None)
            if original_release is None:
                os.environ.pop("SOLET_RELEASE_ID", None)
            else:
                os.environ["SOLET_RELEASE_ID"] = original_release
    for passed, label in checks:
        print(f"{'PASS' if passed else 'FAIL'} {label}")
    return 0 if all(passed for passed, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
