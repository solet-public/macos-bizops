"""XDG and ``SOLET_HOME`` path resolution."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError


def _absolute_env_path(value: str, variable: str, home: Path) -> Path:
    expanded = Path(value).expanduser()
    if not expanded.is_absolute():
        raise ConfigError(f"{variable} must be an absolute path, got {value!r}")
    resolved = expanded.resolve(strict=False)
    if resolved == Path("/"):
        raise ConfigError(f"{variable} may not resolve to the filesystem root")
    if resolved == home.parent and resolved != home:
        raise ConfigError(f"{variable} is too broad: {resolved}")
    return resolved


@dataclass(frozen=True)
class ManagerPaths:
    """All manager-owned locations for one invocation."""

    config_dir: Path
    state_dir: Path
    cache_dir: Path

    @property
    def registry_path(self) -> Path:
        return self.config_dir / "instances.json"

    @property
    def transactions_dir(self) -> Path:
        return self.state_dir / "transactions"

    @property
    def locks_dir(self) -> Path:
        return self.state_dir / "locks"

    @property
    def reconciliations_dir(self) -> Path:
        """Return manager-private durable reconciliation receipts."""

        return self.state_dir / "contract-reconciliations"

    @property
    def adapter_reconciliations_dir(self) -> Path:
        """Return manager-private receipts for snapshot-only code refreshes."""

        return self.state_dir / "adapter-reconciliations"

    @property
    def identity_reconciliations_dir(self) -> Path:
        """Return manager-private receipts for in-field identity migrations."""

        return self.state_dir / "identity-reconciliations"

    @property
    def acquisition_dir(self) -> Path:
        return self.cache_dir / "acquisition"

    def transaction_path(self, name: str) -> Path:
        return self.transactions_dir / f"{name}.json"

    def lock_path(self, name: str) -> Path:
        return self.locks_dir / f"{name}.lock"

    @classmethod
    def resolve(
        cls,
        *,
        environ: Mapping[str, str] | None = None,
        home: Path | None = None,
        explicit_home: Path | None = None,
    ) -> ManagerPaths:
        env = os.environ if environ is None else environ
        user_home = (Path.home() if home is None else home).expanduser().resolve(strict=False)
        selected_home = explicit_home
        if selected_home is None and env.get("SOLET_HOME", "").strip():
            selected_home = _absolute_env_path(env["SOLET_HOME"], "SOLET_HOME", user_home)
        if selected_home is not None:
            base = selected_home.expanduser().resolve(strict=False)
            if not base.is_absolute() or base == Path("/"):
                raise ConfigError(f"manager home must be an absolute, non-root path: {base}")
            return cls(base / "config", base / "state", base / "cache")

        config_root = _root_from_env(env, "XDG_CONFIG_HOME", user_home / ".config", user_home)
        state_root = _root_from_env(env, "XDG_STATE_HOME", user_home / ".local" / "state", user_home)
        cache_root = _root_from_env(
            env,
            "XDG_CACHE_HOME",
            user_home / "Library" / "Caches",
            user_home,
        )
        return cls(config_root / "solet", state_root / "solet", cache_root / "solet")


def _root_from_env(env: Mapping[str, str], key: str, default: Path, home: Path) -> Path:
    raw = env.get(key, "").strip()
    return _absolute_env_path(raw, key, home) if raw else default.resolve(strict=False)
