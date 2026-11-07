"""Explicit, owned private PostgreSQL configuration for the nine routed smokes.

There is no profile route. A missing manifest is a disclosed skip; corrupt
custody is an integrity error. The immutable manifest is retired with its run.
"""

from __future__ import annotations

import builtins
import json
import os
import re
import stat
import subprocess
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, TypedDict, cast

if TYPE_CHECKING:
    from ananta.core.domain.types import ActionResult
    from ananta.core.services.call_context import CallContext
    from ananta.interfaces.plugin_schema_service_interface import PluginSchemaServiceInterface
    from ananta.interfaces.vault_service_interface import VaultServiceInterface
    from ananta.services.state_service import StateService
    from postgres_state_management_plugin.plugin import PostgresStatePlugin

MANIFEST_ENV = "PRIVATE_TEST_PG_MANIFEST"
MARKER = ".private-test-pg-owner.json"
PG_SCHEMA = "private_test_fixture"
FIXTURE_KEYS = (
    "messaging_threads",
    "messaging_write",
    "ledger_ingest",
    "ledger_polling",
    "ledger_summarize",
    "ledger_reset",
    "external_readonly",
    "external_multistatement",
    "external_write",
)
PG_BIN_DIRECTORIES = (
    "/opt/homebrew/opt/postgresql@17/bin",
    "/usr/local/opt/postgresql@17/bin",
    "/usr/lib/postgresql/17/bin",
    "/usr/local/bin",
    "/usr/bin",
)


class MissingFixtureError(RuntimeError):
    """No explicit fixture was supplied; never resolve application defaults."""


class FixtureIntegrityError(RuntimeError):
    """The supplied fixture cannot establish private endpoint custody."""


class ConnectionConfig(TypedDict):
    host: str
    port: int
    user: str
    database: str
    pg_schema: str
    pool_size: int
    connection_timeout: int


class RunOwner(TypedDict):
    nonce: str
    owner_pid: int
    owner_start: str
    owner_uid: int


class FixtureManifest(RunOwner):
    version: int
    root: str
    host: str
    port: int
    user: str
    databases: dict[str, str]
    postmaster_pid: int
    postmaster_start: str
    postmaster_process_start: str


def skip_fixture() -> int:
    print(f"SKIP private PostgreSQL absent: {MANIFEST_ENV}; run quality_gates/provision_test_pg.py --exec")
    print('BORN_CLONE_SKIP_WITNESS={"reason":"local_postgres_fixture_unreachable"}')
    return 77


def pg_binary(name: str) -> Path:
    """Resolve a shipped PostgreSQL tool from fixed directories, never PATH."""
    if name not in {"initdb", "pg_ctl", "createdb", "psql"}:
        raise ValueError("unexpected PostgreSQL tool")
    for directory in PG_BIN_DIRECTORIES:
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    raise FileNotFoundError(f"PostgreSQL tool unavailable: {name}")


def without_libpq(environment: Mapping[str, str]) -> dict[str, str]:
    """Preserve caller values including PROJECT_SOLET_*, exclude libpq inputs."""
    return {key: value for key, value in environment.items() if not key.startswith("PG")} | {
        "PGPASSFILE": os.devnull,
        "PGSERVICEFILE": os.devnull,
        "PSQLRC": os.devnull,
    }


def process_start(pid: int) -> str:
    if pid <= 1:
        raise FixtureIntegrityError("invalid fixture process identity")
    result = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "lstart="],
        env={"LC_ALL": "C", "TZ": "UTC"},
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    if result.returncode or not result.stdout.strip():
        raise FixtureIntegrityError("fixture owner process is absent")
    return result.stdout.strip()


def database_mapping(nonce: str) -> dict[str, str]:
    return {key: f"private_test_{nonce[:12]}_{key}" for key in FIXTURE_KEYS}


def read_owned_json(path: Path) -> dict[str, object]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise FixtureIntegrityError("fixture file must be an owned private regular file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise FixtureIntegrityError("fixture file must contain an object")
    return cast("dict[str, object]", value)


def validate_endpoint(host: object, port: object) -> None:
    if host != "127.0.0.1" or type(port) is not int or not 1024 < port < 65536 or port == 5432:
        raise FixtureIntegrityError("PRIVATE_ENDPOINT_ONLY: require 127.0.0.1 and a non5432 port")


@dataclass(frozen=True, slots=True)
class FixtureConfig:
    root: Path
    nonce: str
    host: str
    port: int
    user: str
    database: str
    key: str

    def connection_config(self, schema: str = PG_SCHEMA) -> ConnectionConfig:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
            raise FixtureIntegrityError("invalid fixture schema")
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "database": self.database,
            "pg_schema": schema,
            "pool_size": 2,
            "connection_timeout": 10,
        }

    def endpoint_args(self) -> list[str]:
        return ["-h", self.host, "-p", str(self.port), "-U", self.user]


def load_fixture(key: str) -> FixtureConfig:
    """Validate every endpoint and ownership premise before returning fields."""
    path, value, nonce = _read_manifest()
    root = path.parent
    _validate_owner(root, value)
    _validate_postmaster(root, value)
    databases = value.get("databases")
    if databases != database_mapping(nonce) or key not in FIXTURE_KEYS:
        raise FixtureIntegrityError("fixture database mapping is not the exact owned set")
    user = value.get("user")
    if not isinstance(user, str) or not user:
        raise FixtureIntegrityError("missing explicit fixture user")
    return FixtureConfig(root, nonce, "127.0.0.1", cast("int", value["port"]), user, database_mapping(nonce)[key], key)


def _read_manifest() -> tuple[Path, dict[str, object], str]:
    raw = os.environ.get(MANIFEST_ENV)
    if not raw:
        raise MissingFixtureError(MANIFEST_ENV)
    path = Path(raw)
    if not path.is_absolute() or path.resolve() != path:
        raise FixtureIntegrityError("manifest path must be absolute without symlinks")
    value = read_owned_json(path)
    if value.get("version") != 1:
        raise FixtureIntegrityError("unknown fixture manifest version")
    validate_endpoint(value.get("host"), value.get("port"))
    nonce = str(value.get("nonce", ""))
    if not re.fullmatch(r"[0-9a-f]{32}", nonce):
        raise FixtureIntegrityError("invalid run nonce")
    root = path.parent
    if value.get("root") != str(root) or path.name != "manifest.json":
        raise FixtureIntegrityError("manifest is outside its run root")
    return path, value, nonce


def _validate_owner(root: Path, value: dict[str, object]) -> None:
    owner = read_owned_json(root / MARKER)
    expected_owner = {name: value.get(name) for name in ("nonce", "owner_pid", "owner_start", "owner_uid")}
    if owner != expected_owner or owner.get("owner_uid") != os.getuid():
        raise FixtureIntegrityError("foreign or stale fixture marker")
    pid = owner.get("owner_pid")
    if type(pid) is not int or process_start(pid) != owner.get("owner_start"):
        raise FixtureIntegrityError("stale fixture process identity")


def _validate_postmaster(root: Path, value: dict[str, object]) -> None:
    lines = (root / "data/postmaster.pid").read_text(encoding="utf-8").splitlines()
    if len(lines) < 6:
        raise FixtureIntegrityError("incomplete postmaster identity")
    observed = (lines[0], lines[1], lines[2], lines[3], lines[5])
    expected = (str(value.get("postmaster_pid")), str(root / "data"), str(value.get("postmaster_start")), str(value["port"]), "127.0.0.1")
    if observed != expected:
        raise FixtureIntegrityError("postmaster identity or endpoint changed")
    if process_start(int(lines[0])) != value.get("postmaster_process_start"):
        raise FixtureIntegrityError("postmaster process identity changed")
    os.kill(int(lines[0]), 0)


def _unexpected_vault() -> NoReturn:
    raise AssertionError("unexpected operation on fixture-only vault")


def fixture_vault(expected_key: str, retrieved: list[str]) -> VaultServiceInterface:
    """Instantiate the complete interface lazily, under the fixture identity."""
    from ananta.interfaces.vault_service_interface import VaultServiceInterface

    class FixtureVault(VaultServiceInterface):
        def retrieve(self, key: str, *, call_context: CallContext | None = None) -> ActionResult:
            retrieved.append(key)
            if key != expected_key:
                _unexpected_vault()
            return {"action_status": "completed", "data": {"value": ""}, "actions": [], "error": None}

        def store(self, key: str, value: str, tags: builtins.list[str] | None = None, metadata: dict[str, str] | None = None, *, call_context: CallContext | None = None) -> ActionResult:
            _unexpected_vault()

        def delete(self, key: str, *, call_context: CallContext | None = None) -> ActionResult:
            _unexpected_vault()

        def list(self, tag: str | None = None, *, call_context: CallContext | None = None) -> ActionResult:
            _unexpected_vault()

        def exists(self, key: str, *, call_context: CallContext | None = None) -> ActionResult:
            _unexpected_vault()

        def rotate(self, key: str, new_value: str, *, call_context: CallContext | None = None) -> ActionResult:
            _unexpected_vault()

        def rename(self, old_key: str, new_key: str, *, call_context: CallContext | None = None) -> ActionResult:
            _unexpected_vault()

        def store_random(self, key: str, byte_length: int = 32, tags: builtins.list[str] | None = None, metadata: dict[str, str] | None = None, *, call_context: CallContext | None = None) -> ActionResult:
            _unexpected_vault()

        def status(self, *, call_context: CallContext | None = None) -> ActionResult:
            _unexpected_vault()

        def unlock(self, passphrase: str, *, call_context: CallContext | None = None) -> ActionResult:
            _unexpected_vault()

        def lock(self, *, call_context: CallContext | None = None) -> ActionResult:
            _unexpected_vault()

        def oauth_client_register(self, client_name: str, scopes: builtins.list[str] | None = None, redirect_uris: builtins.list[str] | None = None, grant_types: builtins.list[str] | None = None, *, call_context: CallContext | None = None) -> ActionResult:
            _unexpected_vault()

        def oauth_client_revoke(self, client_id: str, *, call_context: CallContext | None = None) -> ActionResult:
            _unexpected_vault()

        def oauth_client_list(self, *, call_context: CallContext | None = None) -> ActionResult:
            _unexpected_vault()

        def oauth_client_add_redirect_uri(self, client_id: str, redirect_uri: str, *, call_context: CallContext | None = None) -> ActionResult:
            _unexpected_vault()

    return FixtureVault()


class _FixturePluginManager:
    def __init__(self, plugin: object) -> None:
        self.plugin = plugin

    def get_plugin(self, plugin_name: str) -> object:
        if plugin_name != "postgres_state_management_plugin":
            raise AssertionError("unexpected fixture plugin lookup")
        return self.plugin

    def execute_action(self, plugin_name: str, params: dict[str, object], state: dict[str, object], APP_HOME: str, plugin_config: dict[str, object]) -> dict[str, object]:
        raise AssertionError("fixture must not dispatch application actions")


@dataclass(frozen=True)
class LedgerFixture:
    state: StateService
    plugin: PluginSchemaServiceInterface
    app_home: Path
    vault_keys: list[str]
    expected_vault_key: str


@contextmanager
def ledger_fixture(key: str, schema: str = PG_SCHEMA) -> Generator[LedgerFixture]:
    """Install full production declarations through the public plugin lifecycle."""
    import asyncio
    import secrets

    config = load_fixture(key)
    app_home = config.root / f"app-{key}-{secrets.token_hex(6)}"
    app_home.mkdir(mode=0o700)
    previous = os.environ.get("SOLET_NAME")
    identity = f"private_fixture_{config.nonce[:12]}"
    os.environ["SOLET_NAME"] = identity
    started = False
    plugin: PostgresStatePlugin | None = None
    try:
        from ananta.core.config.config_manager import initialize_config
        from ananta.llm.session_ledger.schema import get_session_ledger_schema
        from ananta.services.schema_manager import SchemaManager
        from ananta.services.state_service import StateService
        from postgres_state_management_plugin.plugin import PostgresStatePlugin

        manager = initialize_config(str(app_home))
        connection = dict(config.connection_config(schema))
        if not manager.save_plugin_config("postgres_state_management_plugin", connection):
            raise RuntimeError("could not save private fixture configuration")
        plugin = PostgresStatePlugin()
        keys: list[str] = []
        expected = f"{identity}.postgres_state_management_plugin.db_password"
        plugin.set_vault_service(fixture_vault(expected, keys))
        plugin.prepare_for_readiness()
        result = asyncio.run(plugin.start_services())
        if result.get("action_status") != "completed":
            raise RuntimeError(f"private plugin startup failed: {result!r}")
        started = True
        state = StateService(_FixturePluginManager(plugin), app_home=str(app_home), state_plugin_name=plugin.name)
        schemas = SchemaManager(state, plugin)
        schemas.initialize_schemas([get_session_ledger_schema()])
        state.set_schema_manager(schemas)
        _restore_identity(previous)
        yield LedgerFixture(state, plugin, app_home, keys, expected)
    finally:
        _restore_identity(previous)
        if started and plugin is not None:
            stopped = asyncio.run(plugin.stop_services())
            if stopped.get("action_status") != "completed":
                raise RuntimeError(f"private plugin stop failed: {stopped!r}")


def _restore_identity(previous: str | None) -> None:
    if previous is None:
        os.environ.pop("SOLET_NAME", None)
    else:
        os.environ["SOLET_NAME"] = previous
