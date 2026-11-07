#!/usr/bin/env python3
"""Run one command with nine private PostgreSQL databases, then retire the run."""

from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import TextIO

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from quality_gates.private_test_pg import (  # noqa: E402
    MANIFEST_ENV,
    MARKER,
    FixtureIntegrityError,
    FixtureManifest,
    RunOwner,
    database_mapping,
    pg_binary,
    process_start,
    read_owned_json,
    validate_endpoint,
    without_libpq,
)

_LEASE_PATH = Path("/tmp/private-test-pg.lock")
_TOOL_TIMEOUT = 40


class StopFailedError(RuntimeError):
    """Stop custody is uncertain; keep every run-owned data byte."""


def receipt(stage: str, **fields: object) -> None:
    print(json.dumps({"private_pg": stage, **fields}, sort_keys=True), flush=True)


@contextmanager
def host_lease() -> Generator[None]:
    """All invocations on this host serialize on one persistent advisory lock."""
    descriptor = os.open(_LEASE_PATH, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        if os.fstat(handle.fileno()).st_uid != os.getuid():
            raise FixtureIntegrityError("foreign provisioner lease")
        _acquire_lease(handle)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _acquire_lease(handle: TextIO) -> None:
    deadline = time.monotonic() + 30
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise FixtureIntegrityError("another private-PG invocation holds the host lease") from None
            time.sleep(0.1)


def _port_available(port: int) -> None:
    validate_endpoint("127.0.0.1", port)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise FixtureIntegrityError("private port occupied; no server was contacted") from exc


def _write_private(path: Path, value: Mapping[str, object]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        path.chmod(0o600)
        json.dump(value, handle, sort_keys=True)
        handle.write("\n")


class Cluster:
    """Custody of only this invocation's fresh directory and postmaster."""

    def __init__(self, directory: Path, port: int) -> None:
        self.directory = directory
        self.data = directory / "data"
        self.port = port
        self.nonce = secrets.token_hex(16)
        self.user = getpass.getuser()
        self.owner: RunOwner = {
            "nonce": self.nonce,
            "owner_pid": os.getpid(),
            "owner_start": process_start(os.getpid()),
            "owner_uid": os.getuid(),
        }
        self.created = False
        self.removed = False
        self.retained = False
        self.inode: int | None = None
        self.postmaster: tuple[str, str, str] | None = None
        self.log = directory.parent / f"{directory.name}-{self.nonce}.postgres.log"

    @property
    def environment(self) -> dict[str, str]:
        return without_libpq(os.environ) | {"HOME": str(self.directory)}

    def _tool(self, name: str, args: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [str(pg_binary(name)), *args],
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=_TOOL_TIMEOUT,
            check=False,
        )
        if check and result.returncode:
            receipt("tool_failed", tool=name, exit=result.returncode, log=str(self.log))
            raise RuntimeError(f"private PostgreSQL {name} failed (exit {result.returncode}); retain log {self.log}")
        return result

    def create(self) -> Path:
        if not self.directory.is_absolute() or self.directory.resolve() != self.directory:
            raise FixtureIntegrityError("require an absolute directory without symlinks")
        _port_available(self.port)
        self.directory.mkdir(mode=0o700, parents=False, exist_ok=False)
        self.created = True
        self.inode = self.directory.stat().st_ino
        _write_private(self.directory / MARKER, self.owner)
        receipt("created", run=self.nonce, port=self.port, root=str(self.directory))
        self._tool("initdb", ["-D", str(self.data), "--auth-local=reject", "--auth-host=trust", "--username", self.user, "--encoding=UTF8", "--no-locale"])
        options = f"-h 127.0.0.1 -p {self.port} -k '' -c max_connections=80 -c shared_buffers=16MB"
        self._tool("pg_ctl", ["-D", str(self.data), "-l", str(self.log), "-o", options, "-w", "-t", "25", "start"])
        self.postmaster = self._read_postmaster()
        for database in database_mapping(self.nonce).values():
            if self._read_postmaster() != self.postmaster:
                raise FixtureIntegrityError("postmaster identity changed before database creation")
            self._tool("createdb", ["-h", "127.0.0.1", "-p", str(self.port), "-U", self.user, "--maintenance-db=postgres", database])
        return self._manifest()

    def _read_postmaster(self) -> tuple[str, str, str]:
        lines = (self.data / "postmaster.pid").read_text(encoding="utf-8").splitlines()
        if len(lines) < 6 or lines[1] != str(self.data) or lines[3] != str(self.port) or lines[5] != "127.0.0.1":
            raise FixtureIntegrityError("owned postmaster endpoint could not be proved")
        os.kill(int(lines[0]), 0)
        return lines[0], lines[2], process_start(int(lines[0]))

    def _manifest(self) -> Path:
        if self.postmaster is None:
            raise FixtureIntegrityError("postmaster identity was not captured")
        path = self.directory / "manifest.json"
        value: FixtureManifest = {
            **self.owner,
            "version": 1,
            "root": str(self.directory),
            "host": "127.0.0.1",
            "port": self.port,
            "user": self.user,
            "databases": database_mapping(self.nonce),
            "postmaster_pid": int(self.postmaster[0]),
            "postmaster_start": self.postmaster[1],
            "postmaster_process_start": self.postmaster[2],
        }
        _write_private(path, value)
        receipt("ready", run=self.nonce, databases=len(database_mapping(self.nonce)))
        return path

    def _confirm_owner(self) -> None:
        if self.directory.is_symlink() or self.directory.stat().st_ino != self.inode:
            raise FixtureIntegrityError("run directory custody changed")
        if read_owned_json(self.directory / MARKER) != self.owner:
            raise FixtureIntegrityError("foreign or stale run marker; retain directory")

    def _stop_owned_postmaster(self) -> None:
        pidfile = self.data / "postmaster.pid"
        if pidfile.exists():
            actual = self._read_postmaster()
            if self.postmaster is not None and self.postmaster != actual:
                raise StopFailedError("postmaster identity changed; retained")
            stopped = self._tool("pg_ctl", ["-D", str(self.data), "-w", "-t", "25", "-m", "fast", "stop"], check=False)
            if stopped.returncode:
                raise StopFailedError("pg_ctl stop failed; retained")
        if (self.data / "PG_VERSION").exists():
            status = self._tool("pg_ctl", ["-D", str(self.data), "status"], check=False)
            if status.returncode != 3 or pidfile.exists():
                raise StopFailedError("postmaster stop could not be confirmed; retained")
        receipt("stopped", run=self.nonce)

    def _remove_owned_directory(self) -> None:
        self._confirm_owner()
        shutil.rmtree(self.directory)
        self.removed = True
        receipt("removed", run=self.nonce, log=str(self.log))

    def finalize(self) -> None:
        if not self.created or self.removed:
            return
        if self.retained:
            raise StopFailedError("previous stop uncertainty; directory remains retained")
        try:
            self._confirm_owner()
            self._stop_owned_postmaster()
            self._remove_owned_directory()
        except Exception:
            self.retained = True
            receipt("retained", run=self.nonce, root=str(self.directory), log=str(self.log))
            raise


def _terminate_child(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is not None:
        return
    os.killpg(child.pid, signal.SIGTERM)
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=5)


def run_child(command: Sequence[str], manifest: Path, timeout: float) -> int:
    environment = without_libpq(os.environ) | {MANIFEST_ENV: str(manifest)}
    child = subprocess.Popen(command, env=environment, start_new_session=True)
    try:
        status = child.wait(timeout=timeout)
        receipt("child_exited", exit=status)
        return status if status >= 0 else 128 - status
    except subprocess.TimeoutExpired:
        receipt("child_timeout")
        return 124
    finally:
        _terminate_child(child)


def _interrupted(signum: int, frame: FrameType | None) -> None:
    del frame
    raise InterruptedError(signum)


@contextmanager
def signal_scope() -> Generator[None]:
    previous = {number: signal.signal(number, _interrupted) for number in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", required=True, type=Path)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--timeout", default=900.0, type=float)
    parser.add_argument("--exec", dest="execute", required=True, action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or args.timeout <= 0:
        parser.error("--exec -- COMMAND is required with a positive timeout")
    cluster = Cluster(args.dir, args.port)
    try:
        with host_lease(), signal_scope():
            try:
                manifest = cluster.create()
                return run_child(command, manifest, args.timeout)
            finally:
                cluster.finalize()
    except InterruptedError as exc:
        receipt("interrupted", signal=exc.args[0])
        return 128 + int(exc.args[0])
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        receipt("failed", reason=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
