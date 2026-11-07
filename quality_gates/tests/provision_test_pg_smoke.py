#!/usr/bin/env python3
"""Offline private-PG custody, routing and disclosed-absence regression proof."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from quality_gates import provision_test_pg as provision  # noqa: E402
from quality_gates import run_smokes  # noqa: E402
from quality_gates.private_test_pg import (  # noqa: E402
    MANIFEST_ENV,
    MARKER,
    FixtureConfig,
    FixtureIntegrityError,
    database_mapping,
    load_fixture,
    process_start,
    validate_endpoint,
)


def test_forwarded() -> None:
    """A real runner child must enter its fixture body, rather than SKIP."""
    with tempfile.TemporaryDirectory() as raw:
        child = Path(raw) / "fixture_body.py"
        child.write_text(
            "import os, sys\nif not os.environ.get('PRIVATE_TEST_PG_MANIFEST'):\n    print('SKIP missing fixture'); sys.exit(77)\nprint('PRIVATE_PG_REACHES_REAL_BODY')\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"PRIVATE_TEST_PG_MANIFEST": str(Path(raw) / "manifest.json")}):
            before = dict(os.environ)
            verdict, output, _exit_code = run_smokes._run_one(Path(sys.executable), child)
            assert dict(os.environ) == before, "CALLER_ENVIRONMENT_UNCHANGED"
        print("RUNNER_BODY_OBSERVATION", verdict, output.strip(), flush=True)
        assert verdict == "passed", ("RUNNER_PRIVATE_PG_FORWARDED", verdict, output)
        assert verdict == "passed" and "PRIVATE_PG_REACHES_REAL_BODY" in output, (
            "PRIVATE_PG_REACHES_REAL_BODY",
            verdict,
            output,
        )


def test_absence() -> None:
    """Exercise the existing entry point without permitting any profile read."""
    children = [
        "ananta/tests/llm/agent_messaging/list_threads_live_smoke.py",
        "ananta/tests/llm/agent_messaging/repository_write_migration_live_smoke.py",
        "ananta/tests/llm/session_ledger/ingest_migration_live_smoke.py",
        "ananta/tests/llm/session_ledger/polling_driver_migration_live_smoke.py",
        "ananta/tests/llm/session_ledger/summarize_migration_live_smoke.py",
        "ananta/tests/llm/session_ledger/reset_ingest_state_live_db_smoke.py",
        "plugins/external_postgres_plugin/tests/smoke_readonly.py",
        "plugins/external_postgres_plugin/tests/smoke_multistatement.py",
        "plugins/external_postgres_plugin/tests/smoke_write.py",
    ]
    for relative in children:
        child = _ROOT / relative
        if not child.is_file():
            print(f"SKIP optional plugin smoke absent: {relative}")
            continue
        _absent_child(child)


def _absent_child(child: Path) -> None:
    source = (
        "import runpy, socket\nfrom pathlib import Path\n"
        "original = Path.open\n"
        "def guarded(self, *args, **kwargs):\n"
        "    assert 'profile' not in self.parts, 'NO_PROFILE_READ'\n"
        "    return original(self, *args, **kwargs)\n"
        "Path.open = guarded\n"
        "def no_connection(*args, **kwargs):\n"
        "    raise AssertionError('NO_CONNECTION_WITHOUT_FIXTURE')\n"
        "socket.create_connection = no_connection\n"
        f"runpy.run_path({str(child)!r}, run_name='__main__')\n"
    )
    environment = {k: v for k, v in os.environ.items() if not k.startswith(("PG", "PRIVATE_TEST_PG_", "AGENT_MESSAGING_"))}
    environment |= {"AGENT_MESSAGING_LIST_THREADS_LIVE_SMOKE": "1", "AGENT_MESSAGING_WRITE_LIVE_SMOKE": "1", "LEDGER_INGEST_LIVE_SMOKE": "1", "LEDGER_POLLING_LIVE_SMOKE": "1", "LEDGER_SUMMARIZE_LIVE_SMOKE": "1", "LEDGER_RESET_LIVE_SMOKE": "1"}
    result = subprocess.run(
        [sys.executable, "-c", source],
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    print("ABSENCE_OBSERVATION", child.name, result.returncode, result.stdout.strip(), result.stderr, flush=True)
    assert "NO_PROFILE_READ" not in result.stderr, result.stderr
    assert "SKIP" in result.stdout, ("ABSENCE_VALID_SETUP", result.stdout, result.stderr)
    assert result.returncode == 77, ("ABSENT_FIXTURE_IS_77", result.returncode, result.stdout, result.stderr)
    with patch.dict(os.environ, environment, clear=True):
        verdict, output, _exit_code = run_smokes._run_one(Path(sys.executable), child)
    assert verdict == "skipped", ("ABSENT_FIXTURE_RUNNER_IS_SKIPPED", verdict, output)


@contextmanager
def owned_fixture() -> Generator[tuple[Path, provision.Cluster]]:
    """Valid filesystem/process custody with no database connection or tools."""
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve() / "cluster"
        cluster = provision.Cluster(root, 58983)
        root.mkdir(mode=0o700)
        cluster.created = True
        cluster.inode = root.stat().st_ino
        provision._write_private(root / MARKER, cluster.owner)
        yield root, cluster


def _expect_integrity(operation: Callable[[], object], label: str) -> None:
    try:
        operation()
    except (FixtureIntegrityError, FileExistsError, provision.StopFailedError):
        return
    raise AssertionError(label)


def test_endpoint() -> None:
    validate_endpoint("127.0.0.1", 58983)
    for host, port in (("localhost", 5432), ("127.0.0.1", 5432), ("example.invalid", 58983)):
        _expect_integrity(lambda host=host, port=port: validate_endpoint(host, port), "PRIVATE_ENDPOINT_ONLY")
    with owned_fixture() as (root, cluster):
        data = root / "data"
        data.mkdir()
        (data / "postmaster.pid").write_text(f"{os.getpid()}\n{data}\n1\n58983\n\n127.0.0.1\n")
        cluster.postmaster = str(os.getpid()), "1", process_start(os.getpid())
        manifest = cluster._manifest()
        with patch.dict(os.environ, {MANIFEST_ENV: str(manifest)}):
            observed = load_fixture("ledger_ingest")
            assert observed.database == database_mapping(cluster.nonce)["ledger_ingest"], "OWNED_CLUSTER_ONLY"
            value = json.loads(manifest.read_text())
            value["databases"]["ledger_ingest"] = "shared"
            manifest.write_text(json.dumps(value))
            _expect_integrity(lambda: load_fixture("ledger_ingest"), "OWNED_CLUSTER_ONLY")
            value["databases"] = database_mapping(cluster.nonce)
            value["postmaster_process_start"] = "stale"
            manifest.write_text(json.dumps(value))
            _expect_integrity(lambda: load_fixture("ledger_ingest"), "POSTMASTER_PROCESS_IDENTITY_REFUSED")
            value["postmaster_process_start"] = process_start(os.getpid())
            value["owner_start"] = "stale"
            manifest.write_text(json.dumps(value))
            _expect_integrity(lambda: load_fixture("ledger_ingest"), "STALE_PROCESS_REFUSED")


def test_lifecycle() -> None:
    with owned_fixture() as (root, cluster):
        events: list[str] = []
        remove = cluster._remove_owned_directory

        def stopped() -> None:
            assert root.exists(), "STOP_BEFORE_REMOVE"
            events.append("stop")

        def removed() -> None:
            assert events == ["stop"], "STOP_BEFORE_REMOVE"
            remove()
            events.append("remove")

        with patch.object(cluster, "_stop_owned_postmaster", stopped), patch.object(cluster, "_remove_owned_directory", removed):
            cluster.finalize()
            cluster.finalize()
        assert events == ["stop", "remove"] and not root.exists(), "FINALIZATION_IDEMPOTENT"
    with owned_fixture() as (root, cluster):
        with patch.object(cluster, "_stop_owned_postmaster", side_effect=provision.StopFailedError("injected stop failure")):
            _expect_integrity(cluster.finalize, "STOP_FAILURE_RETAINS")
        assert root.exists() and cluster.retained, "STOP_FAILURE_RETAINS"
        _expect_integrity(cluster.finalize, "REPEAT_UNCERTAINTY_RETAINS")
        assert root.exists(), "REPEAT_UNCERTAINTY_RETAINS"
    with owned_fixture() as (root, cluster):
        (root / MARKER).write_text(json.dumps(cluster.owner | {"nonce": "foreign"}))
        _expect_integrity(cluster.finalize, "STALE_MARKER_REFUSED")
        assert root.exists(), "OWNED_CLUSTER_ONLY"
    with owned_fixture() as (root, cluster):
        data = root / "data"
        data.mkdir()
        (data / "postmaster.pid").write_text(f"{os.getpid()}\n{data}\n1\n58983\n\n127.0.0.1\n")
        cluster.postmaster = str(os.getpid()), "1", "stale process start"
        with patch.object(cluster, "_tool") as tool:
            _expect_integrity(cluster.finalize, "STALE_POSTMASTER_NEVER_STOPPED")
            tool.assert_not_called()
        assert root.exists() and cluster.retained, "STALE_POSTMASTER_RETAINED"


def test_partial_start() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve() / "partial"
        cluster = provision.Cluster(root, 58983)
        with patch.object(provision, "_port_available"), patch.object(cluster, "_tool", side_effect=RuntimeError("partial initdb failure")):
            try:
                cluster.create()
            except RuntimeError:
                cluster.finalize()
        assert cluster.created and cluster.removed and not root.exists(), "PARTIAL_START_RETIRED"
        foreign = Path(raw).resolve() / "foreign"
        foreign.mkdir()
        sentinel = foreign / "sentinel"
        sentinel.write_text("foreign")
        cluster = provision.Cluster(foreign, 58983)
        with patch.object(provision, "_port_available"):
            _expect_integrity(cluster.create, "OWNED_CLUSTER_ONLY")
        cluster.finalize()
        assert sentinel.read_text() == "foreign", "FOREIGN_DIRECTORY_UNTOUCHED"
        link = Path(raw).resolve() / "link"
        link.symlink_to(foreign, target_is_directory=True)
        _expect_integrity(lambda: provision.Cluster(link, 58983).create(), "SYMLINK_REFUSED")
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        _expect_integrity(lambda: provision._port_available(occupied.getsockname()[1]), "OCCUPIED_PORT_REFUSED")


def test_child() -> None:
    with tempfile.TemporaryDirectory() as raw:
        manifest = Path(raw).resolve() / "manifest.json"
        observed = Path(raw) / "child.json"
        source = f"import json,os\nfrom pathlib import Path\nPath({str(observed)!r}).write_text(json.dumps(dict(os.environ)))\nraise SystemExit(23)"
        with patch.dict(os.environ, {"PGHOST": "foreign.invalid", "PGPASSWORD": "sentinel", "PROJECT_SOLET_TEST_DSN": "unchanged"}):
            before = dict(os.environ)
            status = provision.run_child([sys.executable, "-c", source], manifest, 10)
            assert dict(os.environ) == before, "CALLER_ENVIRONMENT_UNCHANGED"
        assert status == 23, "CHILD_STATUS_PROPAGATED"
        child_environment = json.loads(observed.read_text())
        assert child_environment[MANIFEST_ENV] == str(manifest), "RUNNER_PRIVATE_PG_FORWARDED"
        assert child_environment["PROJECT_SOLET_TEST_DSN"] == "unchanged", "PROJECT_SOLET_VALUES_PRESERVED"
        assert "PGHOST" not in child_environment and "PGPASSWORD" not in child_environment, "LIBPQ_ENVIRONMENT_EXCLUDED"
        assert provision.run_child([sys.executable, "-c", "import time; time.sleep(10)"], manifest, 0.05) == 124, "CHILD_TIMEOUT_BOUNDED"
        assert provision.run_child([sys.executable, "-c", "import os,signal; os.kill(os.getpid(), signal.SIGTERM)"], manifest, 10) == 143, "CHILD_SIGNAL_PROPAGATED"


def test_external() -> None:
    """Observe every real external smoke's subprocess argv without executing it."""
    for name in ("readonly", "multistatement", "write"):
        path = _ROOT / f"plugins/external_postgres_plugin/tests/smoke_{name}.py"
        if not path.is_file():
            print(f"SKIP optional external PostgreSQL plugin absent: {path.relative_to(_ROOT)}")
            continue
        spec = importlib.util.spec_from_file_location(f"private_epg_{name}", path)
        assert spec and spec.loader, "EXTERNAL_VALID_SETUP"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        config = FixtureConfig(Path("/fixture"), "a" * 32, "127.0.0.1", 58983, module._USER, "private_test_external", f"external_{name}")
        with patch.object(module, "load_fixture", return_value=config), patch.object(module, "pg_binary", side_effect=lambda tool: Path("/fixture/bin") / tool), patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "1\n", "")) as run:
            with patch.dict(os.environ, {"PGHOST": "localhost", "PGPORT": "5432", "PGPASSWORD": "sentinel"}):
                module._configure_fixture()
                assert module._setup(), "EXTERNAL_VALID_SETUP"
                module._psql("unchanged probe")
                if name == "write":
                    module._psql_scalar("unchanged scalar probe")
        assert len(run.call_args_list) >= 3, "EXTERNAL_SUBPROCESS_PATH_REACHED"
        for call in run.call_args_list:
            _assert_external_call(name, config, call)
        dsn = module._dsn()
        assert (dsn.host, dsn.port, dsn.dbname) == (config.host, config.port, config.database), "EXTERNAL_CONNECTOR_SAME_ENDPOINT"


def _assert_external_call(name: str, config: FixtureConfig, call: Any) -> None:
    argv = call.args[0]
    print("EXTERNAL_ARGV_OBSERVATION", name, argv[:10], flush=True)
    assert "-h" in argv and argv[argv.index("-h") + 1] == config.host and "-p" in argv and argv[argv.index("-p") + 1] == str(config.port), "EXTERNAL_PSQL_USES_PRIVATE_ENDPOINT"
    assert "-U" in argv and argv[argv.index("-U") + 1] == config.user, "EXTERNAL_EXPLICIT_USER"
    assert "PGHOST" not in call.kwargs["env"] and "PGPASSWORD" not in call.kwargs["env"], "EXTERNAL_LIBPQ_ENVIRONMENT_EXCLUDED"


def main() -> int:
    cases = {"forwarded": test_forwarded, "absence": test_absence, "endpoint": test_endpoint, "lifecycle": test_lifecycle, "partial": test_partial_start, "child": test_child, "external": test_external}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=cases)
    args = parser.parse_args()
    for name in [args.case] if args.case else cases:
        cases[name]()
        print(f"PASS {name}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
