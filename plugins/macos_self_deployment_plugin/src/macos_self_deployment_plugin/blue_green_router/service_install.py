"""Shared helpers for install_router.py and uninstall_router.py.

Path conventions, name validation, and template-rendering used by both
the install and the uninstall side of Slice H. Kept in one module so
install/uninstall agree on label format + file locations without
drifting.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from string import Template
from typing import Final

from macos_self_deployment_plugin.constants import AUTOSTART_LABEL_PREFIX

# The router's launchd label and systemd unit share the operator-neutral
# `local.homunculus.<name>` family used by the main autostart plist
# (single-sourced from ``constants.AUTOSTART_LABEL_PREFIX``). A born
# homunculus's router therefore carries ONLY its own name — never the
# birther's. (The former origin-specific service prefix stamped the birther identity
# into every clone's infrastructure; removed 2026-07-18, SEED-06 D2.)
RUNTIME_DIR: Final[Path] = Path.home() / ".ananta" / "runtime"
LOG_DIR: Final[Path] = Path.home() / ".ananta" / "logs"
LAUNCHD_AGENTS_DIR: Final[Path] = Path.home() / "Library" / "LaunchAgents"
SYSTEMD_USER_UNIT_DIR: Final[Path] = Path.home() / ".config" / "systemd" / "user"
LAUNCHD_TEMPLATE_NAME: Final[str] = "router_launchd.plist.template"
SYSTEMD_TEMPLATE_NAME: Final[str] = "router_systemd.service.template"

# Reverse-DNS Label component constraint. The full label is
# `local.homunculus.<homunculus>.router`; the `<homunculus>` component must
# be a single DNS-safe lowercase token. Reject empties, dots, slashes,
# uppercase, anything that breaks launchd Label parsing or path quoting.
_HOMUNCULUS_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class InstallError(RuntimeError):
    """Install or uninstall refused to proceed."""


def validate_homunculus_name(name: str) -> None:
    if not _HOMUNCULUS_NAME_RE.match(name):
        raise InstallError(
            f"invalid homunculus_name {name!r}: must match "
            f"{_HOMUNCULUS_NAME_RE.pattern} (lowercase, starts with letter, "
            "≤32 chars, no dots/slashes)",
        )


def launchd_label(homunculus: str) -> str:
    return f"{AUTOSTART_LABEL_PREFIX}.{homunculus}.router"


def systemd_unit_name(homunculus: str) -> str:
    return f"{AUTOSTART_LABEL_PREFIX}.{homunculus}.router.service"


def default_launchd_plist_path(homunculus: str) -> Path:
    return LAUNCHD_AGENTS_DIR / f"{launchd_label(homunculus)}.plist"


def default_systemd_unit_path(homunculus: str) -> Path:
    return SYSTEMD_USER_UNIT_DIR / systemd_unit_name(homunculus)


def default_socket_path(homunculus: str) -> Path:
    return RUNTIME_DIR / f"{homunculus}.router.sock"


def render_template(template_name: str, context: dict[str, str]) -> str:
    template_path = Path(__file__).parent / template_name
    text = template_path.read_text(encoding="utf-8")
    try:
        return Template(text).substitute(context)
    except KeyError as exc:
        raise InstallError(
            f"template {template_name} references {exc.args[0]!r} but context "
            "did not provide it",
        ) from None


def python_bin() -> str:
    """Active interpreter — captured at install time so the plist points
    at the same venv the operator ran install from."""

    return sys.executable
