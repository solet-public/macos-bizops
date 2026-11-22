"""Report-only doctor advisory for a fleet launcher file that is sourced but missing.

The fleet launcher (``client/<name>-fleet.zsh``) is rendered once, by Step 4a of the hydration runbook, and
that step also adds a ``source`` line for it to ``client/<name>.zsh``.  The Manager update refreshes a fleet
file that exists and never creates one that does not.  So an operator whose client file sources a fleet file
that is absent has a shell that errors at start and role sessions that are not tmux-hosted, and nothing else
tells them.  This census does, as an advisory.

The signal is the source line.  It is clone-local, deterministic, and written by exactly Step 4a.  A role
binding would also show fleet use, but it lives in the running solet's database, which a Manager doctor on a
stopped solet cannot read.  A solet with no such line gets a verified row and nothing new.  An unreadable
client file is ``unknown``, not a verdict: this census returns rows only and never refuses.
"""

from __future__ import annotations

from pathlib import Path

from .doctor_advisory_result import advisory_unknown, advisory_verified, advisory_warn
from .models import InstanceRecord, JsonValue

_CHECK_ID = "doctor::fleet_launcher_file_v1"
_MISSING_REASON = "fleet_file_missing_but_sourced"
_UNREADABLE_REASON = "client_file_unreadable"


def collect_fleet_launcher_advisories(
    record: InstanceRecord,
    *,
    target_root: Path | None = None,
) -> list[JsonValue]:
    """Return one row: is a fleet file that the client file sources actually present?"""

    root = Path(record.target) if target_root is None else target_root
    client_path = root / "client" / f"{record.name}.zsh"
    fleet_name = f"{record.name}-fleet.zsh"
    fleet_path = root / "client" / fleet_name
    source = str(client_path)
    expected: dict[str, JsonValue] = {
        "sourced_fleet_file": fleet_name,
        "fleet_file_present_when_sourced": True,
    }

    try:
        text = client_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [_no_evidence(expected, source, client_file_present=False)]
    except (OSError, UnicodeDecodeError) as exc:
        return [
            advisory_unknown(
                _CHECK_ID,
                "The client launcher file could not be read, so fleet use is unknown.",
                expected,
                {"client_file_readable": False},
                source,
                _UNREADABLE_REASON,
                f"{type(exc).__name__}: {exc}",
            )
        ]

    if not _sources_fleet_file(text, fleet_name):
        return [_no_evidence(expected, source, client_file_present=True)]

    present = fleet_path.is_file()
    observed: dict[str, JsonValue] = {
        "client_file_sources_fleet_file": True,
        "fleet_file_present": present,
    }
    if present:
        return [
            advisory_verified(
                _CHECK_ID,
                "The fleet launcher file the client file sources is present.",
                expected,
                observed,
                source,
            )
        ]
    return [
        advisory_warn(
            _CHECK_ID,
            "The client launcher file sources a fleet launcher file that does not exist.",
            expected,
            observed,
            source,
            _MISSING_REASON,
            f"client/{record.name}.zsh sources {fleet_name} but the file does not exist, so no role session is tmux-hosted; re-render it from fleet_functions.zsh.template (runbook Step 4a) or remove the source line; the update never creates it.",
        )
    ]


def _sources_fleet_file(text: str, fleet_name: str) -> bool:
    """True when a line, after optional whitespace, starts with ``source`` and names the fleet file."""

    for line in text.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0] == "source" and fleet_name in parts[1]:
            return True
    return False


def _no_evidence(
    expected: dict[str, JsonValue],
    source: str,
    *,
    client_file_present: bool,
) -> dict[str, JsonValue]:
    return advisory_verified(
        _CHECK_ID,
        "The client launcher file does not source a fleet launcher file, so there is no fleet use to check.",
        expected,
        {"client_file_present": client_file_present, "client_file_sources_fleet_file": False},
        source,
    )
