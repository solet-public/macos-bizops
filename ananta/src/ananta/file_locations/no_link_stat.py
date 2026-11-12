"""One whole-path metadata read that refuses a symbolic link at any component.

macOS fstatat with AT_SYMLINK_NOFOLLOW_ANY resolves a path in one call. If any
component before the last is a link, the call fails with ELOOP; a link as the
last component is described itself, never followed. Otherwise the call returns
the object's identity, type and link count. The containment guard makes this read
its last one, so a single instant answers two questions: whether the returned
spelling still has no link, and which object it names. Python's os module
cannot pass that flag, so the call goes through ctypes.

The read is used only on darwin, only when the symbol resolves (fstatat on
arm64, fstatat$INODE64 on x86_64), and only after a start-up self-check on a
temporary fixture. The self-check must agree with os.stat and must refuse a
link. If any condition fails, AVAILABLE is False, one ADVISORY log line names
the reason, and the guard keeps its whole-path no-follow open.
"""

import ctypes
import errno
import logging
import os
import platform
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

logger = logging.getLogger(__name__)

_AT_FDCWD = -2
_AT_SYMLINK_NOFOLLOW_ANY = 0x0800
# The 64-bit-inode struct stat (sys/stat.h) is 144 bytes. Only the leading
# st_dev, st_mode, st_nlink and st_ino fields are read; the rest is reserved.
_STAT_SIZE = 144
_SYMBOLS = {"arm64": "fstatat", "x86_64": "fstatat$INODE64"}


class _DarwinStat(ctypes.Structure):
    _fields_ = [
        ("st_dev", ctypes.c_int32),
        ("st_mode", ctypes.c_uint16),
        ("st_nlink", ctypes.c_uint16),
        ("st_ino", ctypes.c_uint64),
        ("_reserved", ctypes.c_char * (_STAT_SIZE - 16)),
    ]


@dataclass(frozen=True, slots=True)
class NoLinkStat:
    """The fields of one whole-path read that the guard compares."""

    st_dev: int
    st_ino: int
    st_mode: int
    st_nlink: int


type _Fstatat = Callable[[int, bytes, object, int], int]


def _load() -> _Fstatat:
    if sys.platform != "darwin":
        raise OSError(f"platform {sys.platform} has no AT_SYMLINK_NOFOLLOW_ANY")
    symbol = _SYMBOLS.get(platform.machine())
    if symbol is None:
        raise OSError(f"no fstatat symbol is known for machine {platform.machine()}")
    try:
        function = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)[symbol]
    except (OSError, AttributeError) as exc:
        raise OSError(f"{symbol} does not resolve: {exc}") from exc
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_int]
    function.restype = ctypes.c_int
    return cast(_Fstatat, function)


def _read(function: _Fstatat, path: Path) -> NoLinkStat:
    result = _DarwinStat()
    if function(_AT_FDCWD, os.fsencode(path), ctypes.byref(result), _AT_SYMLINK_NOFOLLOW_ANY) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(path))
    return NoLinkStat(result.st_dev, result.st_ino, result.st_mode, result.st_nlink)


def _self_check(function: _Fstatat) -> None:
    """Require agreement with os.stat on a plain fixture and a refusal through a link."""
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary).resolve()
        (base / "directory").mkdir()
        fixture = base / "directory" / "fixture"
        fixture.write_text("self-check fixture")
        (base / "link").symlink_to(base / "directory", target_is_directory=True)
        expected = os.stat(fixture)
        observed = _read(function, fixture)
        if (observed.st_dev, observed.st_ino, observed.st_mode, observed.st_nlink) != (expected.st_dev, expected.st_ino, expected.st_mode, expected.st_nlink):
            raise OSError(f"fstatat returned {observed}, os.stat returned dev={expected.st_dev} ino={expected.st_ino}")
        try:
            _read(function, base / "link" / "fixture")
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                return
            raise OSError(f"a path through a link failed with {exc.strerror}, not ELOOP") from exc
        raise OSError("a path through a link was not refused")


def _start() -> _Fstatat | None:
    try:
        function = _load()
        _self_check(function)
    except OSError as exc:
        logger.warning("ADVISORY: file_locations: one-call no-link stat is unavailable (%s); the containment guard's final read uses its whole-path no-follow open", exc)
        return None
    return function


_FSTATAT = _start()
AVAILABLE: bool = _FSTATAT is not None


def stat_without_links(path: Path) -> NoLinkStat:
    """Read the whole path once: ELOOP for a link before the last component; a final link is described, not followed."""
    if _FSTATAT is None:
        raise RuntimeError("stat_without_links is unavailable; check AVAILABLE first")
    return _read(_FSTATAT, path)
