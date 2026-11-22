"""The measured host platform an update's host-gated closure pieces are decided on (iss_6d26db73, rul_385dac24).

Queried, never inferred: ``/usr/bin/sw_vers -productVersion`` for the macOS
version and ``/usr/bin/uname -m`` for the machine, each bounded.  A probe that
fails or answers in an unexpected shape raises :class:`HostPlatformError`; the
caller blocks the plan rather than guessing which pieces the host can take.
The threshold a release applies lives in its transition bundle's
``host_profiles``, not here.

A Mac that can run Apple's SystemLanguageModel (macOS 26 or later on arm64, the
model's own floor) also carries :class:`ModelAvailability`, read by a bounded
one-line Swift probe (prb_66bfd36b).  That fact is advisory data
(rul_becb4bed): a probe that cannot run, times out or answers in an unexpected
shape leaves ``HostPlatform.model`` as ``None`` (unknown), never an exception
and never a guess, so a missing Swift toolchain cannot stop a setup.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, replace

__all__ = ["HostPlatform", "HostPlatformError", "ModelAvailability", "read_host_platform"]

_VERSION = re.compile(r"^(\d+)(?:\.\d+){0,2}$")
_MACHINE = re.compile(r"^[a-z0-9_]{1,32}$")
_TIMEOUT_SECONDS = 5
# SystemLanguageModel exists from macOS 26 on Apple silicon; elsewhere there is nothing to measure.
_MODEL_MIN_MACOS = 26
_MODEL_MACHINE = "arm64"
# A ceiling, not the expected time. Measured 2026-11-21 on the r74 macOS 27 guest, idle host: a cold
# `swift -e` probe took 12.4 s and a warm one 1.1 s; the bound leaves room for a loaded host.
_MODEL_TIMEOUT_SECONDS = 60
_MODEL_PROBE = """import FoundationModels
switch SystemLanguageModel.default.availability {
case .available: print("AVAILABLE")
case .unavailable(let reason): print("UNAVAILABLE", reason)
}"""
_MODEL_REASON = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}$")
_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


class HostPlatformError(RuntimeError):
    """The host platform could not be measured."""


@dataclass(frozen=True, slots=True)
class ModelAvailability:
    """Whether Apple's SystemLanguageModel can answer now; ``reason`` is ``AVAILABLE`` or why not (``DEVICE_NOT_ELIGIBLE``)."""

    available: bool
    reason: str


@dataclass(frozen=True, slots=True)
class HostPlatform:
    product_version: str
    machine: str
    model: ModelAvailability | None = None  # None: not measured or unknown

    def __post_init__(self) -> None:
        if _VERSION.fullmatch(self.product_version) is None:
            raise HostPlatformError(f"macOS product version is not a version: {self.product_version!r}")
        if _MACHINE.fullmatch(self.machine) is None:
            raise HostPlatformError(f"machine is not a machine name: {self.machine!r}")

    @property
    def macos_major(self) -> int:
        return int(self.product_version.split(".", 1)[0])


def read_host_platform() -> HostPlatform:
    """Measure this host; raises :class:`HostPlatformError` instead of answering from a guess.

    The Apple model is read only where it can exist and never raises: see :func:`_read_model`.
    """
    host = HostPlatform(_probe(("/usr/bin/sw_vers", "-productVersion")), _probe(("/usr/bin/uname", "-m")))
    if host.macos_major < _MODEL_MIN_MACOS or host.machine != _MODEL_MACHINE:
        return host
    return replace(host, model=_read_model())


def _read_model() -> ModelAvailability | None:
    """The Apple model's availability now, or ``None`` when it cannot be measured."""
    try:
        # /usr/bin/swift is a shim that opens a GUI install prompt when no developer tools are set: ask first.
        _probe(("/usr/bin/xcode-select", "-p"))
        answer = _probe(("/usr/bin/swift", "-e", _MODEL_PROBE), _MODEL_TIMEOUT_SECONDS)
    except HostPlatformError:
        return None
    return _parse_model(answer)


def _parse_model(answer: str) -> ModelAvailability | None:
    words = answer.splitlines()[-1].split() if answer else []
    if words == ["AVAILABLE"]:
        return ModelAvailability(True, "AVAILABLE")
    if words[:1] != ["UNAVAILABLE"]:
        return None
    token = words[1] if len(words) == 2 else ""
    reason = _CASE_BOUNDARY.sub("_", token).upper() if _MODEL_REASON.fullmatch(token) else "UNSPECIFIED"
    return ModelAvailability(False, reason)


def _probe(argv: tuple[str, ...], timeout: int = _TIMEOUT_SECONDS) -> str:
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HostPlatformError(f"{argv[0]} could not run: {exc}") from exc
    if completed.returncode != 0:
        raise HostPlatformError(f"{argv[0]} exited {completed.returncode}: {completed.stderr.strip()[:200]}")
    return completed.stdout.strip()
