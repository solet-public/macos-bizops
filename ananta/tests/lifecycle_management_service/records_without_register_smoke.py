#!/usr/bin/env python3
"""Prove the solet keeps work-folder and deployment records without the external register.

Offline: the work-folder half uses A4's schema-enforced state fake and the
deploy half uses A3's fixture ports over the real deployment-record store.

Run from the repository root:
    PYTHONPATH=src .venv/bin/python ananta/tests/lifecycle_management_service/records_without_register_smoke.py
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _deploy_by_rule_fixture_support import TARGET, Fixture  # noqa: E402
from ananta.services.lifecycle_management_service.work_folder_records import (  # noqa: E402
    get,
    history,
)
from ananta.work_folders import register_work_folder  # noqa: E402
from work_folder_records_smoke import _store  # noqa: E402


def _assert_isolated_environment(home: Path) -> None:
    assert os.environ.get("HOME") == str(home), "HOME_IS_THE_EMPTY_FIXTURE_DIRECTORY"
    assert not any(name.startswith("PROJECT_" + "SOLET") for name in os.environ), "NO_EXTERNAL_REGISTER_ENVIRONMENT"
    assert not any(home.iterdir()), "NOTHING_EXISTS_UNDER_HOME"


def _register_free_environment(home: Path) -> dict[str, str]:
    environment = {name: value for name, value in os.environ.items() if not name.startswith("PROJECT_" + "SOLET")}
    environment["HOME"] = str(home)
    return environment


def _work_folder_lifecycle(root: Path) -> None:
    store = _store()
    path = root / "work"
    path.mkdir()
    (path / "scratch.txt").write_text("scratch", encoding="utf-8")

    folder = register_work_folder(
        path,
        purpose="records-without-register smoke",
        bound="release_run:records-without-register",
        state=store,
    )
    assert folder.folder_id is not None, "WORK_FOLDER_REGISTERED_IN_SOLET_STATE"
    folder.close()

    row = get(store, folder_id=folder.folder_id)
    assert row is not None and row["state"] == "deleted", "WORK_FOLDER_ROW_IS_TERMINAL"
    assert not path.exists(), "WORK_FOLDER_WAS_REMOVED_BY_CLOSE"
    events = history(store, folder_id=folder.folder_id, limit=100).rows
    assert [(event["ordinal"], event["kind"]) for event in events] == [
        (1, "opened"),
        (2, "closed"),
        (3, "harvested"),
        (4, "deleted"),
    ], "WORK_FOLDER_HISTORY_IS_COMPLETE"


def _deployment_lifecycle() -> None:
    fixture = Fixture()
    assert fixture.ports.host_acquire is None and fixture.ports.host_release is None, "OFFLINE_FIXTURE_HAS_NO_HOST_LEASE_PORTS"
    deployed = fixture.run()
    assert deployed["decision"] == "deployed", "DEPLOY_BY_RULE_COMPLETED"

    current = fixture.store.read_deployments(
        solet_name=fixture.args.solet_name,
        mode="current",
    )["data"]["rows"]
    assert len(current) == 1 and current[0]["git_sha"] == TARGET, "DEPLOYMENT_IS_CURRENT_IN_SOLET_STATE"
    assert fixture.run()["decision"] == "already_current", "SECOND_DEPLOY_IS_ALREADY_CURRENT"

    commands = [entry for entry in fixture.log if isinstance(entry, list)]
    assert commands and all(("p" + "solet") not in Path(str(command[0])).name.lower() for command in commands), "NO_REGISTER_COMMAND"


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="records-without-register-") as raw:
        root = Path(raw).resolve()
        home = root / "home"
        home.mkdir()
        environment = _register_free_environment(home)
        stderr = io.StringIO()

        def unexpected_subprocess(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise AssertionError("NO_REAL_SUBPROCESS")

        with (
            patch.dict(os.environ, environment, clear=True),
            patch("subprocess.run", side_effect=unexpected_subprocess) as subprocess_run,
            redirect_stderr(stderr),
        ):
            _assert_isolated_environment(home)
            _work_folder_lifecycle(root)
            _deployment_lifecycle()
            assert not any(home.iterdir()), "NOTHING_WAS_CREATED_UNDER_HOME"
            assert subprocess_run.call_count == 0, "NO_REAL_SUBPROCESS_WAS_STARTED"

        assert "ADVISORY" not in stderr.getvalue(), f"NO_TRACKING_ADVISORY: {stderr.getvalue()}"

    print("records without the external register: work folders and deployments PASS")


if __name__ == "__main__":
    main()
