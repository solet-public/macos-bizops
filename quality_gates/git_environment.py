"""Construct per-operation Git environments without inherited caller steering.

Non-Git values are copied afresh on every call. Only operation-owned overrides
may route Git; caller index, object, repository and config selectors are
deliberately excluded. Global and system configuration remain disabled even
when overrides are supplied. The caller's environment is never mutated.
"""

from __future__ import annotations

import os
from collections.abc import Mapping


def scrubbed_git_environment(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a fresh environment, with explicit routing and isolated config."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(overrides or {})
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_CONFIG_SYSTEM"] = os.devnull
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    return environment
