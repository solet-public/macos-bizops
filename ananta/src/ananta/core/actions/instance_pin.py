"""Instance-pinned action rows: a row only the instance that wrote it may claim.

Under a blue-green deploy two instances poll ONE ``core__action_events`` queue.
Before ``unt_37b85986`` the old colour's poller was blocked inline for the whole
deploy, so a booting candidate was the only claimant of its own starting
actions. With the deploy detached, both directions broke (iss_faf5802c):

- the old colour claimed the candidate's ``start_interface`` and answered
  ``bridge.already_running``, so the candidate never bound a bridge and the
  swap ended ``register_timeout``;
- the candidate, not yet router-active and with no bridge, claimed the live
  colour's ``peer_inbox`` / ``deliver_result`` rows and failed them
  ``bridge.not_running``.

## The pin

A row is pinned by an ``instance:<id>`` entry in its existing
``excluded_versions`` JSON list, so no DDL is needed. ``<id>`` is
:func:`process_instance_id`: unique per process (pid plus a random suffix),
prefixed with the router's ``SOLET_INSTANCE_ID`` when the deploy plugin set
one, so log lines name the colour. It is never a version: two colours of one
version (a same-manifest redeploy, a rollback) still have different ids.

A pinned row also lists the writer's own ``SOLET_VERSION``. That entry is a
shield for pollers that predate pins: they read only the version entries, so
an old colour of the same version skips the row instead of claiming it. A
poller that knows pins ignores the version entries of a pinned row; the pin
alone decides.

## Who claims what

- A pinned row: only the instance it names.
- An unpinned row: any instance not restricted to its own rows, subject to the
  version exclusion as before.
- An instance restricted to its own rows (a router-backed instance until the
  router names it active) claims only rows pinned to itself.
"""

from __future__ import annotations

import json
import os
import uuid
from functools import cache
from typing import Final

#: Runtime-action key carrying the pin from the submitter to the recorder.
CLAIM_PIN_KEY: Final[str] = "claim_pin"

#: Prefix of the ``excluded_versions`` entry that pins a row to an instance.
PIN_ENTRY_PREFIX: Final[str] = "instance:"

_ENV_SOLET_INSTANCE_ID: Final[str] = "SOLET_INSTANCE_ID"
_ENV_SOLET_VERSION: Final[str] = "SOLET_VERSION"
_DEFAULT_SOLET_VERSION: Final[str] = "local"
_UNROUTED: Final[str] = "unrouted"


def solet_version() -> str:
    """This process's ``SOLET_VERSION`` lineage marker (``local`` when unset)."""
    return os.environ.get(_ENV_SOLET_VERSION) or _DEFAULT_SOLET_VERSION


@cache
def process_instance_id() -> str:
    """This process's claim identity, fixed for its lifetime."""
    router_id = os.environ.get(_ENV_SOLET_INSTANCE_ID, "").strip() or _UNROUTED
    return f"{router_id}/{os.getpid()}-{uuid.uuid4().hex[:8]}"


def exclusion_entries(raw: object) -> list[object]:
    """The ``excluded_versions`` column as a list.

    The JSON column reads back as a list or, defensively, a JSON string;
    ``NULL``, empty and malformed values are an empty list.
    """
    if raw is None:
        return []
    value: object = raw
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return value if isinstance(value, list) else []


def pinned_entries(instance_id: str) -> list[str]:
    """The entries a row pinned to ``instance_id`` carries: version shield, then pin."""
    return [solet_version(), f"{PIN_ENTRY_PREFIX}{instance_id}"]


def pinned_instance(entries: list[object]) -> str | None:
    """The instance a row is pinned to, or ``None`` for an unpinned row."""
    for entry in entries:
        if isinstance(entry, str) and entry.startswith(PIN_ENTRY_PREFIX):
            return entry[len(PIN_ENTRY_PREFIX):]
    return None
