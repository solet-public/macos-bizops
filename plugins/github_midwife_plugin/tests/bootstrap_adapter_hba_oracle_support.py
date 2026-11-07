"""Focused pg_hba SCRAM-oracle checks used by the adapter contract smoke."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import TextIO
from unittest.mock import patch

Check = Callable[[object, str], None]


def _completed() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, "", "")


def _no_op_runner(
    _command: list[str], **_kwargs: object,
) -> subprocess.CompletedProcess[str]:
    return _completed()


class _PartialWriteStream:
    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def __enter__(self) -> _PartialWriteStream:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self._stream.close()

    def write(self, value: str) -> int:
        self._stream.write(value[: max(1, len(value) // 2)])
        raise OSError("injected partial write")

    def flush(self) -> None:
        self._stream.flush()

    def fileno(self) -> int:
        return self._stream.fileno()


def _interruption_fixture(path: Path, lines: list[str]) -> tuple[bytes, bytes]:
    prior_text = (
        "\n".join(f"# {line}" for line in lines)
        + "\nlocal all all trust\n"
        + "host all all 127.0.0.1/32 trust\n"
        + "host all all ::1/128 trust\n"
    )
    path.write_text(prior_text, encoding="utf-8")
    path.chmod(0o600)
    from bootstrap_adapter.postgres import _hba_with_default_scram

    return prior_text.encode("utf-8"), _hba_with_default_scram(prior_text).encode("utf-8")


def _assert_interrupted_write(
    path: Path,
    *,
    check: Check,
    interrupt: Callable[[], None],
    expected: bytes,
    expected_facts: tuple[bool, bool, bool],
    label: str,
) -> None:
    from bootstrap_adapter.postgres import _inspect_pg_hba

    try:
        interrupt()
    except OSError:
        check(True, f"RED-FIRST {label}: injected exception propagates")
    else:
        check(False, f"RED-FIRST {label}: injected exception propagates")
    check(path.read_bytes() == expected, f"RED-FIRST {label}: pg_hba.conf is the expected complete byte sequence")
    check(_inspect_pg_hba(path) == expected_facts, f"RED-FIRST {label}: pg_hba.conf inspection matches the expected complete state")
    check(
        not list(path.parent.glob(f".{path.name}.solet-*")),
        f"RED-FIRST {label}: no temporary sibling remains after failure",
    )


def _check_atomic_write_interruptions(
    root: Path, *, check: Check, lines: list[str],
) -> None:
    import bootstrap_adapter.postgres as postgres
    from bootstrap_adapter.models import AdapterError

    writer = postgres.atomic_write_scram_hba
    old_facts = (True, True, False)
    new_facts = (True, True, True)

    def make_case(name: str) -> tuple[Path, bytes, bytes]:
        path = root / name
        prior, complete = _interruption_fixture(path, lines)
        return path, prior, complete

    path, prior, _complete = make_case("temp-fsync-pg_hba.conf")
    with patch("bootstrap_adapter.postgres.os.fsync", side_effect=OSError("temp fsync")):
        _assert_interrupted_write(
            path,
            check=check,
            interrupt=lambda: writer(path),
            expected=prior,
            expected_facts=old_facts,
            label="temporary-file fsync failure preserves prior bytes",
        )

    path, prior, _complete = make_case("replace-pg_hba.conf")
    with patch("bootstrap_adapter.postgres.os.replace", side_effect=OSError("replace")):
        _assert_interrupted_write(
            path,
            check=check,
            interrupt=lambda: writer(path),
            expected=prior,
            expected_facts=old_facts,
            label="replace failure preserves prior bytes",
        )

    path, _prior, complete = make_case("directory-fsync-pg_hba.conf")
    fsync_calls = 0

    def fail_directory_fsync(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError("directory fsync")

    with patch("bootstrap_adapter.postgres.os.fsync", side_effect=fail_directory_fsync):
        _assert_interrupted_write(
            path,
            check=check,
            interrupt=lambda: writer(path),
            expected=complete,
            expected_facts=new_facts,
            label="parent-directory fsync failure preserves complete new bytes",
        )
    check(fsync_calls == 2, "RED-FIRST directory fsync interruption reached after replace")

    path, prior, _complete = make_case("partial-write-pg_hba.conf")
    real_fdopen = os.fdopen

    def fail_after_partial_write(
        descriptor: int, mode: str, *, encoding: str,
    ) -> _PartialWriteStream:
        return _PartialWriteStream(real_fdopen(descriptor, mode, encoding=encoding))

    with patch("bootstrap_adapter.postgres.os.fdopen", side_effect=fail_after_partial_write):
        _assert_interrupted_write(
            path,
            check=check,
            interrupt=lambda: writer(path),
            expected=prior,
            expected_facts=old_facts,
            label="partial stream write failure preserves prior bytes",
        )

    path, _prior, complete = make_case("other-pid-pg_hba.conf")
    other_pid_sibling = path.with_name(f".{path.name}.solet-{os.getpid() + 1}")
    other_pid_sibling.write_text("left by another process\n", encoding="utf-8")
    writer(path)
    check(
        path.read_bytes() == complete,
        "RED-FIRST leftover sibling from another pid permits a complete write",
    )
    check(
        postgres._inspect_pg_hba(path) == new_facts,
        "RED-FIRST other-pid sibling result is recognized as complete SCRAM",
    )

    path, prior, _complete = make_case("same-pid-pg_hba.conf")
    same_pid_sibling = path.with_name(f".{path.name}.solet-{os.getpid()}")
    same_pid_sibling.write_text("left by this process\n", encoding="utf-8")
    try:
        writer(path)
    except AdapterError as exc:
        check(
            str(exc) == "pg_hba.conf atomic-write sibling already exists",
            "RED-FIRST same-pid leftover sibling raises the named AdapterError",
        )
    else:
        check(False, "RED-FIRST same-pid leftover sibling raises the named AdapterError")
    check(path.read_bytes() == prior, "RED-FIRST same-pid leftover sibling leaves prior bytes unchanged")


def check_pg_hba_scram_oracle(root: Path, *, check: Check, now: datetime) -> None:
    """Prove comments, transport-specific rules, and trust cannot satisfy SCRAM."""

    from bootstrap_adapter.models import AdapterError, AdapterRuntime, RolePolicyObservation
    from bootstrap_adapter.postgres import (
        _default_scram_lines,
        _inspect_pg_hba,
        apply_postgres_configuration,
        atomic_write_scram_hba,
    )

    _check_atomic_write_interruptions(
        root, check=check, lines=_default_scram_lines(),
    )

    commented_scram = root / "commented-pg_hba.conf"
    commented_scram.write_text(
        "\n".join(f"# {line}" for line in _default_scram_lines())
        + "\nlocal all all trust\n"
        + "host all all 127.0.0.1/32 trust\n"
        + "host all all ::1/128 trust\n"
    )
    commented_scram.chmod(0o600)
    check(
        _inspect_pg_hba(commented_scram) == (True, True, False),
        "RED-FIRST pg_hba oracle ignores commented-out SCRAM strings",
    )
    atomic_write_scram_hba(commented_scram)
    check(
        _inspect_pg_hba(commented_scram) == (True, True, True),
        "RED-FIRST success control: comment-only SCRAM no longer makes the writer skip a real fix",
    )

    unverified_scram = root / "unverified-pg_hba.conf"
    unverified_scram.write_text("local all all trust\n")
    unverified_scram.chmod(0o600)
    runtime = AdapterRuntime(
        run=_no_op_runner,
        which=lambda _name: None,
        now=lambda: now,
        name="adaptertest",
        target=root,
    )
    policy = RolePolicyObservation(
        False, False, None, None, False, None, False, False,
        unverified_scram, True, True, False,
    )
    with patch("bootstrap_adapter.postgres.atomic_write_scram_hba"):
        try:
            apply_postgres_configuration(
                runtime, policy, [{"id": "postgres.edit_pg_hba_scram"}]
            )
        except AdapterError as exc:
            check(
                "verification failed after apply" in str(exc),
                "RED-FIRST no-op pg_hba write is rejected by the post-write re-read",
            )
        else:
            check(False, "pg_hba apply did not verify its written result")

    preempted_scram = root / "preempted-pg_hba.conf"
    preempted_scram.write_text(
        "local all all trust\n"
        "host all all 127.0.0.1/32 trust\n"
        "host all all ::1/128 trust\n"
        + "\n".join(_default_scram_lines())
        + "\n"
    )
    preempted_scram.chmod(0o600)
    check(
        _inspect_pg_hba(preempted_scram) == (True, False, False),
        "RED-FIRST pg_hba oracle rejects first-match trust before SCRAM",
    )

    ssl_only_scram = root / "ssl-only-scram-pg_hba.conf"
    ssl_only_scram.write_text(
        "local all all scram-sha-256\n"
        "hostssl all all 127.0.0.1/32 scram-sha-256\n"
        "hostssl all all ::1/128 scram-sha-256\n"
        "hostnossl all all 127.0.0.1/32 trust\n"
        "hostnossl all all ::1/128 trust\n"
    )
    ssl_only_scram.chmod(0o600)
    check(
        _inspect_pg_hba(ssl_only_scram) == (True, False, False),
        "RED-FIRST hostssl SCRAM cannot verify passwordless hostnossl traffic",
    )

    for connection_type in ("hostgssenc", "hostnogssenc"):
        gss_preempted_scram = root / f"{connection_type}-preempted-pg_hba.conf"
        gss_preempted_scram.write_text(
            "local all all scram-sha-256\n"
            f"{connection_type} all all 127.0.0.1/32 trust\n"
            "hostssl all all 127.0.0.1/32 scram-sha-256\n"
            "hostnossl all all 127.0.0.1/32 scram-sha-256\n"
            "hostssl all all ::1/128 scram-sha-256\n"
            "hostnossl all all ::1/128 scram-sha-256\n"
        )
        gss_preempted_scram.chmod(0o600)
        check(
            _inspect_pg_hba(gss_preempted_scram) == (True, False, False),
            f"RED-FIRST {connection_type} trust cannot hide behind later hostssl SCRAM",
        )
