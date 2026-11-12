"""``dump_database`` / ``restore_database`` — pg_dump and pg_restore.

The vendor surface is the PostgreSQL 17 client programs. Every documented
``pg_dump`` option and every restore-into-database ``pg_restore`` option is a
parameter named after its long flag (``--exclude-table-data`` ->
``exclude_table_data``), declared in :data:`PG_DUMP_OPTIONS` /
:data:`PG_RESTORE_OPTIONS`. Connection options are not parameters: the target
is always a registered ``external_pg::<name>`` connection, and the platform's
own instance is refused before any program starts (the plugin calls
``connection.assert_foreign_target``).

Credentials never touch argv or the inherited environment. The program gets a
clean environment (every inherited ``PG*`` variable dropped), the connection
literals as ``PGHOST``/``PGPORT``/``PGUSER``/``PGSSLMODE``, and the password
through a 0600 password file in a private temporary directory, removed when
the run ends. ``--no-password`` is always passed, so a missing password fails
instead of prompting.

Every file path is contained (``file_gate``: absolute, under an
``registered path addresses`` entry). Restore accepts pg_dump ARCHIVES only (custom,
directory, tar) and runs them through ``pg_restore``, which has no client-side
commands; with ``create`` the archive's own database name is read
(``pg_restore --list``) and platform-guarded first, because pg_restore
reconnects to it. A plain-text SQL file is refused with
``external_pg.plain_sql_restore_refused``: replaying it would need ``psql``, whose
backslash meta-commands run programs and read files on this host where vendor
RBAC does not reach (seat decision on review BLOCK uev_19f22651: no hand-rolled
psql lexer). Plain SQL runs through ``run_script`` instead. Program stderr is returned as
received with the host removed; connection, authentication and permission
failures carry the plugin's fixed topology-safe messages instead.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, Literal

from .app_config import ExternalDsn
from .connection import assert_foreign_target
from .constants import (
    CLIENT_BIN_FALLBACK_DIRS,
    CLIENT_TOOL_STDERR_TAIL_CHARS,
    CONFIG_KEY_CLIENT_BIN_DIR,
    CONFIG_KEY_CLIENT_TOOL_TIMEOUT_SECONDS,
    CONNECT_TIMEOUT_SECONDS,
    ERROR_API_ERROR,
    ERROR_AUTH_FAILED,
    ERROR_CLIENT_TOOL_FAILED,
    ERROR_CLIENT_TOOL_MISSING,
    ERROR_PERMISSION_DENIED,
    ERROR_PLAIN_SQL_RESTORE_REFUSED,
    ERROR_TIMEOUT,
    GENERIC_MESSAGE_AUTH_FAILED,
    GENERIC_MESSAGE_CONNECTION_FAILED,
    GENERIC_MESSAGE_PERMISSION_DENIED,
    PLUGIN_NAME,
)

FileGate = Callable[[str, str], str]
OptionKind = Literal["bool", "str", "int", "list", "path"]

# pg_dump --help (PostgreSQL 17): every option except connection options,
# --file/--format (output_path/format parameters), --version/--help, and the
# deprecated --blobs/--no-blobs aliases of --large-objects/--no-large-objects.
PG_DUMP_OPTIONS: dict[str, OptionKind] = {
    "jobs": "int",
    "verbose": "bool",
    "compress": "str",
    "lock_wait_timeout": "str",
    "no_sync": "bool",
    "sync_method": "str",
    "data_only": "bool",
    "large_objects": "bool",
    "no_large_objects": "bool",
    "clean": "bool",
    "create": "bool",
    "extension": "list",
    "encoding": "str",
    "schema": "list",
    "exclude_schema": "list",
    "no_owner": "bool",
    "schema_only": "bool",
    "superuser": "str",
    "table": "list",
    "exclude_table": "list",
    "no_privileges": "bool",
    "binary_upgrade": "bool",
    "column_inserts": "bool",
    "disable_dollar_quoting": "bool",
    "disable_triggers": "bool",
    "enable_row_security": "bool",
    "exclude_extension": "list",
    "exclude_table_and_children": "list",
    "exclude_table_data": "list",
    "exclude_table_data_and_children": "list",
    "extra_float_digits": "int",
    "filter": "path",
    "if_exists": "bool",
    "include_foreign_data": "list",
    "inserts": "bool",
    "load_via_partition_root": "bool",
    "no_comments": "bool",
    "no_publications": "bool",
    "no_security_labels": "bool",
    "no_subscriptions": "bool",
    "no_table_access_method": "bool",
    "no_tablespaces": "bool",
    "no_toast_compression": "bool",
    "no_unlogged_table_data": "bool",
    "on_conflict_do_nothing": "bool",
    "quote_all_identifiers": "bool",
    "restrict_key": "str",
    "rows_per_insert": "int",
    "section": "list",
    "serializable_deferrable": "bool",
    "snapshot": "str",
    "strict_names": "bool",
    "table_and_children": "list",
    "use_set_session_authorization": "bool",
    "role": "str",
}
# pg_restore --help (PostgreSQL 17): every option that restores into a database;
# --list and --file (archive listing / script conversion) are build 2.
PG_RESTORE_OPTIONS: dict[str, OptionKind] = {
    "verbose": "bool",
    "data_only": "bool",
    "clean": "bool",
    "create": "bool",
    "exit_on_error": "bool",
    "index": "list",
    "jobs": "int",
    "use_list": "path",
    "schema": "list",
    "exclude_schema": "list",
    "no_owner": "bool",
    "function": "list",
    "schema_only": "bool",
    "superuser": "str",
    "table": "list",
    "trigger": "list",
    "no_privileges": "bool",
    "single_transaction": "bool",
    "disable_triggers": "bool",
    "enable_row_security": "bool",
    "filter": "path",
    "if_exists": "bool",
    "no_comments": "bool",
    "no_data_for_failed_tables": "bool",
    "no_publications": "bool",
    "no_security_labels": "bool",
    "no_subscriptions": "bool",
    "no_table_access_method": "bool",
    "no_tablespaces": "bool",
    "restrict_key": "str",
    "section": "list",
    "strict_names": "bool",
    "transaction_size": "int",
    "use_set_session_authorization": "bool",
    "role": "str",
}
DUMP_FORMATS = ("plain", "custom", "directory", "tar")
ARCHIVE_FORMATS = ("custom", "directory", "tar")

_AUTH_MARKERS = ("password authentication failed", "no password supplied", "authentication failed")
_HOST_MASK = "[REDACTED]"  # topology-safe: a client program's stderr never names the host
_CONNECTION_MARKERS = ("could not connect", "connection to server", "could not translate host")


class ClientToolError(RuntimeError):
    """A client program was missing, failed, or timed out — typed, with the host removed from stderr."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ToolSettings:
    """Where the client programs live and how long one may run."""

    bin_dir: str
    timeout_seconds: int
    platform_pg_port: int


def dump_database(
    dsn: ExternalDsn,
    params: dict[str, Any],
    file_gate: FileGate,
    settings: ToolSettings,
) -> dict[str, Any]:
    """Run pg_dump against the registered connection into a contained output path."""
    fmt = params.get("format", "plain")
    if fmt not in DUMP_FORMATS:
        raise ValueError(f"'format' must be one of {list(DUMP_FORMATS)}")
    output_path = file_gate(_require_str(params, "output_path"), "output_path")
    parent = os.path.dirname(output_path)
    if not os.path.isdir(parent):
        raise ValueError(f"the parent directory of 'output_path' does not exist ({parent})")
    args = [
        "--no-password",
        f"--format={fmt}",
        f"--file={output_path}",
        *option_args(params, PG_DUMP_OPTIONS, file_gate),
        f"--dbname={_conninfo_dbname(dsn.dbname)}",
    ]
    stderr = _run_tool("pg_dump", args, dsn, settings)
    return {
        "output_path": output_path,
        "format": fmt,
        "bytes": _size_of(output_path),
        "messages": stderr,
    }


def restore_database(
    dsn: ExternalDsn,
    params: dict[str, Any],
    file_gate: FileGate,
    settings: ToolSettings,
) -> dict[str, Any]:
    """Restore a contained pg_dump archive into the registered connection with pg_restore."""
    input_path = file_gate(_require_str(params, "input_path"), "input_path")
    if not os.path.exists(input_path):
        raise ValueError(f"'input_path' does not exist ({input_path})")
    detected = detect_dump_format(input_path)
    requested = params.get("format")
    if requested is not None and requested != detected:
        raise ValueError(f"'format' is {requested!r} but {input_path} is a {detected} dump; omit 'format' to restore it as detected")
    if detected == "plain":
        raise ClientToolError(
            ERROR_PLAIN_SQL_RESTORE_REFUSED,
            f"{input_path} is a plain-text SQL file; restore_database accepts pg_dump archives only "
            "(format custom, directory or tar), restored through pg_restore, which runs no "
            "client-side commands. Replaying plain SQL would need psql, whose backslash "
            "meta-commands run programs and read files on this host, outside the database's own "
            "grants. Dump again with format custom, or run plain SQL with run_script (one "
            "transaction with a savepoint per statement, through the driver, never psql).",
        )
    if params.get("create") is True:
        _guard_archive_database(input_path, detected, dsn, settings)
    args = [
        "--no-password",
        f"--format={detected}",
        *option_args(params, PG_RESTORE_OPTIONS, file_gate),
        f"--dbname={_conninfo_dbname(dsn.dbname)}",
        input_path,
    ]
    stderr = _run_tool("pg_restore", args, dsn, settings)
    return {"input_path": input_path, "format": detected, "tool": "pg_restore", "messages": stderr}


def detect_dump_format(path: str) -> str:
    """``directory``, ``custom`` (``PGDMP`` magic), ``tar`` (``ustar`` at 257) or ``plain``."""
    if os.path.isdir(path):
        return "directory"
    with open(path, "rb") as handle:
        head = handle.read(512)
    if head.startswith(b"PGDMP"):
        return "custom"
    if head[257:262] == b"ustar":
        return "tar"
    return "plain"


def option_args(
    params: dict[str, Any],
    spec: dict[str, OptionKind],
    file_gate: FileGate,
) -> list[str]:
    """Render the declared options present in ``params`` as ``--long-flag`` arguments."""
    args: list[str] = []
    for name, kind in spec.items():
        value = params.get(name)
        if value is None:
            continue
        flag = "--" + name.replace("_", "-")
        args.extend(_render(flag, name, kind, value, file_gate))
    return args


def resolve_tool(name: str, bin_dir: str) -> str:
    """The client program: client_bin_dir if configured, else PATH, else standard dirs."""
    if bin_dir:
        candidate = os.path.join(os.path.expanduser(bin_dir), name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
        raise ClientToolError(
            ERROR_CLIENT_TOOL_MISSING,
            f"{name} was not found in {CONFIG_KEY_CLIENT_BIN_DIR} ({bin_dir}) in the {PLUGIN_NAME} config; point it at a PostgreSQL bin directory",
        )
    found = shutil.which(name)
    if found:
        return found
    for directory in CLIENT_BIN_FALLBACK_DIRS:
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise ClientToolError(
        ERROR_CLIENT_TOOL_MISSING,
        f"{name} is not installed (not on PATH or in the standard PostgreSQL directories); install the PostgreSQL client programs or set {CONFIG_KEY_CLIENT_BIN_DIR} in the {PLUGIN_NAME} config",
    )


def classify_tool_failure(tool: str, returncode: int, stderr: str) -> ClientToolError:
    """Typed, topology-safe error for a non-zero exit; ``stderr`` already has the host removed."""
    lowered = stderr.lower()
    if any(marker in lowered for marker in _AUTH_MARKERS):
        return ClientToolError(ERROR_AUTH_FAILED, GENERIC_MESSAGE_AUTH_FAILED)
    if any(marker in lowered for marker in _CONNECTION_MARKERS):
        return ClientToolError(ERROR_API_ERROR, GENERIC_MESSAGE_CONNECTION_FAILED)
    if "permission denied" in lowered:
        return ClientToolError(ERROR_PERMISSION_DENIED, GENERIC_MESSAGE_PERMISSION_DENIED)
    return ClientToolError(
        ERROR_CLIENT_TOOL_FAILED,
        f"{tool} exited with status {returncode}: {stderr}",
    )


def _render(flag: str, name: str, kind: OptionKind, value: Any, file_gate: FileGate) -> list[str]:
    """One option's argv entries, validated by its declared kind."""
    if kind == "bool":
        return _render_bool(flag, name, value)
    if kind == "int":
        return _render_int(flag, name, value)
    if kind == "list":
        return _render_list(flag, name, value)
    text = _non_empty_str(name, value)
    return [f"{flag}={file_gate(text, name) if kind == 'path' else text}"]


def _render_bool(flag: str, name: str, value: Any) -> list[str]:
    if not isinstance(value, bool):
        raise ValueError(f"'{name}' must be a boolean")
    return [flag] if value else []


def _render_int(flag: str, name: str, value: Any) -> list[str]:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"'{name}' must be a non-negative integer")
    return [f"{flag}={value}"]


def _render_list(flag: str, name: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f"'{name}' must be a list of non-empty strings")
    return [f"{flag}={item}" for item in value]


def _non_empty_str(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"'{name}' must be a non-empty string")
    return value


def archive_dbname(listing: str) -> str | None:
    """The database name recorded in a ``pg_restore --list`` header (``;     dbname: NAME``)."""
    match = re.search(r"^;\s+dbname: (.+)$", listing, re.MULTILINE)
    return match.group(1).strip() if match else None


def _guard_archive_database(input_path: str, fmt: str, dsn: ExternalDsn, settings: ToolSettings) -> None:
    """``create`` makes pg_restore reconnect to the ARCHIVE's database name: guard that target.

    With ``create`` (and ``clean``, a DROP DATABASE first) pg_restore connects to the
    database named inside the archive on the registered server, so that name gets the
    same platform-database refusal as the registered one. The header is read with
    ``pg_restore --list``, which opens no connection.
    """
    completed = subprocess.run(
        [resolve_tool("pg_restore", settings.bin_dir), "--list", f"--format={fmt}", input_path],
        env={key: value for key, value in os.environ.items() if not key.startswith("PG")},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=settings.timeout_seconds,
        check=False,
    )
    name = archive_dbname(completed.stdout.decode("utf-8", errors="replace"))
    if completed.returncode != 0 or name is None:
        raise ValueError(f"could not read the database name recorded in {input_path}, which create needs to check; restore without create, or check the archive")
    assert_foreign_target(replace(dsn, dbname=name), settings.platform_pg_port)


def _run_tool(tool: str, args: list[str], dsn: ExternalDsn, settings: ToolSettings) -> str:
    """Run one client program with a private passfile; return its stderr tail with the host removed."""
    executable = resolve_tool(tool, settings.bin_dir)
    with _password_file(dsn.password) as passfile:
        try:
            completed = subprocess.run(
                [executable, *args],
                env=_tool_environment(dsn, passfile),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=settings.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ClientToolError(
                ERROR_TIMEOUT,
                f"{tool} exceeded {CONFIG_KEY_CLIENT_TOOL_TIMEOUT_SECONDS} ({settings.timeout_seconds} s) and was stopped",
            ) from exc
    stderr = _scrub(completed.stderr.decode("utf-8", errors="replace"), dsn)
    if completed.returncode != 0:
        raise classify_tool_failure(tool, completed.returncode, stderr)
    return stderr


def _tool_environment(dsn: ExternalDsn, passfile: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    env.update(
        {
            "PGPORT": str(dsn.port),
            "PGUSER": dsn.user,
            "PGSSLMODE": dsn.sslmode,
            "PGPASSFILE": passfile,
            "PGSERVICEFILE": os.devnull,
            "PGCONNECT_TIMEOUT": str(CONNECT_TIMEOUT_SECONDS),
            "PGAPPNAME": PLUGIN_NAME,
        }
    )
    if dsn.host:
        env["PGHOST"] = dsn.host
    return env


@contextmanager
def _password_file(password: str) -> Generator[str]:
    """A 0600 libpq password file in a private 0700 directory, removed afterwards.

    Created even for a password-less registration (then empty): libpq warns
    about a non-regular file such as /dev/null, and leaving PGPASSFILE unset
    would let the operator's own ~/.pgpass supply a credential the registration
    does not hold.
    """
    directory = tempfile.mkdtemp(prefix="epg_pass_")
    path = os.path.join(directory, "pgpass")
    escaped = password.replace("\\", "\\\\").replace(":", "\\:")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            if password:
                handle.write(f"*:*:*:*:{escaped}\n")
        yield path
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _scrub(stderr: str, dsn: ExternalDsn) -> str:
    """The stderr tail with the host removed (topology-safe), otherwise as received."""
    text = stderr
    if dsn.host:
        text = text.replace(dsn.host, _HOST_MASK)
    text = text.strip()
    if len(text) > CLIENT_TOOL_STDERR_TAIL_CHARS:
        text = "…" + text[-CLIENT_TOOL_STDERR_TAIL_CHARS:]
    return text


def _conninfo_dbname(dbname: str) -> str:
    """``dbname='…'`` conninfo, so a name containing ``=`` is never read as conninfo."""
    escaped = dbname.replace("\\", "\\\\").replace("'", "\\'")
    return f"dbname='{escaped}'"


def _size_of(path: str) -> int:
    if os.path.isdir(path):
        return sum(os.path.getsize(os.path.join(root, name)) for root, _dirs, files in os.walk(path) for name in files)
    return os.path.getsize(path) if os.path.exists(path) else 0


def _require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{key}' is required and must be a non-empty string")
    return value
