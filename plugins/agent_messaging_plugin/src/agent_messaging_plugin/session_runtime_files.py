"""The end of a session's watch and wake files in the runtime folder.

The watch and wake halves of a session write up to six files into the runtime
folder, each named ``<solet>.<agent instance id>`` plus one suffix (``SUFFIXES``;
``local_cli/spool.py`` is their writer). Nothing deleted them, so a folder meant to
hold one family per live session held one per session ever started. This module is
the one place that deletes them, and it is called at the two moments a family stops
being needed: a session's terminal event (``_fire_session_terminal_dependencies``
in ``session_lifecycle_control``) and the solet's start check
(``check_session_records`` in ``session_end``).

A family is the regular files directly in the folder whose names are one stem plus
one suffix, where the stem is ``<solet>.agi-<32 hex>``. The ``agi-watch-<24 hex>``
ids of bare watches are not session ids and are left alone, as is every name that is
not one of the six, every directory and every symlink.

A family is kept whole while a watcher holds the exclusive flock on its
``.watch.lock``: that is a live watcher, and the kernel drops the lock when the
watcher dies, so a free lock means nothing is running for the family. A family with
no lock file is free. A held family is listed in ``kept_locked`` and is picked up by
a later start check.

The folder is shared by every solet on the host and an instance id is unique across
them, so the solet in the stem is not part of the rule. The start-check sweep judges
families by this solet's own records, so a family another solet wrote, for a session
this solet has no record of, is removed when its lock is free.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from ananta.core.runtime.port_manager import get_runtime_dir

if TYPE_CHECKING:
    from collections.abc import Callable, Collection
    from pathlib import Path

logger = logging.getLogger(__name__)

SUFFIXES: Final[tuple[str, ...]] = (".watch.lock", ".watchpair", ".marks", ".spool", ".spool.lock", ".spool.offset")
_LOCK_SUFFIX: Final[str] = ".watch.lock"
_STEM: Final[re.Pattern[str]] = re.compile(r"^(?P<solet>[^/]+)\.(?P<instance>agi-[0-9a-f]{32})$")
_INSTANCE: Final[re.Pattern[str]] = re.compile(r"agi-[0-9a-f]{32}")


@dataclass(frozen=True)
class RuntimeFilesRemoval:
    """What one call did: ``removed`` and ``failed`` are file names, ``kept_locked`` are stems.

    ``failed`` also names a lock that could not be probed (the family is kept) and the
    folder itself when it could not be read.
    """

    removed: tuple[str, ...] = ()
    kept_locked: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


def _families(runtime_dir: Path, wanted: Callable[[str, str], bool]) -> dict[str, list[str]]:
    """Stem to the names of its regular files, for the stems ``wanted(stem, instance)`` accepts. One scan."""
    families: dict[str, list[str]] = {}
    with os.scandir(runtime_dir) as entries:
        for entry in entries:
            suffix = next((candidate for candidate in SUFFIXES if entry.name.endswith(candidate)), None)
            if suffix is None:
                continue
            stem = entry.name.removesuffix(suffix)
            match = _STEM.fullmatch(stem)
            if match is not None and wanted(stem, match["instance"]) and entry.is_file(follow_symlinks=False):
                families.setdefault(stem, []).append(entry.name)
    return families


def _lock_held(runtime_dir: Path, stem: str) -> bool:
    """True when a watcher holds ``<stem>.watch.lock``; no lock file means free."""
    try:
        descriptor = os.open(runtime_dir / f"{stem}{_LOCK_SUFFIX}", os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def _delete_family(runtime_dir: Path, names: list[str]) -> tuple[list[str], list[str]]:
    """Delete each named file; ``(removed, failed)``. A file already gone is neither."""
    removed: list[str] = []
    failed: list[str] = []
    for name in names:
        try:
            (runtime_dir / name).unlink()
        except FileNotFoundError:
            continue
        except OSError:
            logger.warning("runtime files: could not delete %s", name, exc_info=True)
            failed.append(name)
        else:
            removed.append(name)
    return removed, failed


def _remove(runtime_dir: Path, wanted: Callable[[str, str], bool]) -> RuntimeFilesRemoval:
    try:
        families = _families(runtime_dir, wanted)
    except OSError:
        logger.warning("runtime files: could not read %s", runtime_dir, exc_info=True)
        return RuntimeFilesRemoval(failed=(str(runtime_dir),))
    removed: list[str] = []
    kept_locked: list[str] = []
    failed: list[str] = []
    for stem, names in families.items():
        try:
            held = _lock_held(runtime_dir, stem)
        except OSError:
            logger.warning("runtime files: could not probe the watch lock of %s; its files are kept", stem, exc_info=True)
            failed.append(f"{stem}{_LOCK_SUFFIX}")
            continue
        if held:
            kept_locked.append(stem)
            continue
        gone, stuck = _delete_family(runtime_dir, names)
        removed.extend(gone)
        failed.extend(stuck)
    return RuntimeFilesRemoval(tuple(removed), tuple(kept_locked), tuple(failed))


def instance_watch_held(runtime_dir: Path, agent_instance_id: str) -> bool:
    """True when a watcher holds the lock of any family of this instance. Raises OSError if the folder cannot be read."""
    return any(_lock_held(runtime_dir, stem) for stem in _families(runtime_dir, lambda _stem, instance: instance == agent_instance_id))


def remove_instance_files(runtime_dir: Path, agent_instance_id: str, *, keep_stem: str | None = None) -> RuntimeFilesRemoval:
    """Remove every free family of this instance, under any solet, except the one named ``keep_stem``."""
    return _remove(runtime_dir, lambda stem, instance: instance == agent_instance_id and stem != keep_stem)


def remove_files_without_live_session(runtime_dir: Path, live_instance_ids: Collection[str]) -> RuntimeFilesRemoval:
    """Remove every free family whose instance is not in ``live_instance_ids``."""
    return _remove(runtime_dir, lambda _stem, instance: instance not in live_instance_ids)


def end_session_runtime_files(agent_instance_id: str) -> RuntimeFilesRemoval:
    """Remove this session's families from the runtime folder as its terminal event passes.

    Never raises: a terminal event is not failed by a file that could not be removed,
    and the next start check removes what this left. An id that cannot name a family
    never reads the folder.
    """
    if not _INSTANCE.fullmatch(agent_instance_id):
        return RuntimeFilesRemoval()
    try:
        return remove_instance_files(get_runtime_dir(), agent_instance_id)
    except Exception as exc:  # noqa: BLE001 — a runtime-file fault never fails a terminal event
        logger.warning("runtime files: could not end the files of %s: %s", agent_instance_id, exc, exc_info=True)
        return RuntimeFilesRemoval()
