#!/usr/bin/env python3
"""Unit smoke for per-spawn inference-provider selection (2026-08-10).

The spawning session directs which provider its worker runs on — "bedrock"
or "anthropic" — independent of the platform daemon's own provider. This
smoke covers the three seams that carry that choice, all with fakes (no real
process, no real vault, no real ``claude`` binary):

  1. ``_coerce_provider_env`` / ``_apply_provider_env`` — the pure overlay
     helpers in ``headless_adapter`` (set-for-bedrock, strip-for-anthropic,
     no-op-when-absent).
  2. ``AgentMessagingPlugin._resolve_provider_env`` — the ONLY vault-reading
     seam (fake vault): inherit / anthropic-strip / bedrock-resolve /
     fail-loud-on-missing-credential / fail-loud-on-unknown-provider.
  3. The headless + tmux adapters' ``spawn`` env wiring — a records-only
     ``popen_fn`` asserts a ``provider_env`` on the dispatch spec actually
     reaches the spawned worker's environment, and that an omitted provider
     leaves the inherited environment untouched (backward compatible).

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/spawn_provider_env_smoke.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin.headless_adapter import (  # noqa: E402
    _PROVIDER_ENV_KEYS,
    _apply_provider_env,
    _coerce_provider_env,
)
from agent_messaging_plugin.plugin import (  # noqa: E402
    AgentMessagingPlugin,
    _ProviderResolutionError,
)

# Reuse the headless smoke's fake-process + driver-config helpers so this
# smoke exercises the SAME spawn path the identity-wiring smoke does.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from headless_adapter_smoke import (  # noqa: E402
    _configured_driver,
    _FakeProc,
    _pop_env_family,
    _restore_env_family,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _FakeVault:
    """Records-only stand-in for the injected VaultServiceProxy — returns the
    REAL ActionResult envelope ``macos_vault_plugin.retrieve`` produces:
    ``action_status == 'completed'`` with ``data.value`` on a hit, and the
    same ``completed`` status with no ``value`` on a not-found (its
    ``_success``/``_not_found`` shape). Keying on the wrong field here is
    exactly the bug this smoke's live counterpart caught (2026-08-10): a
    fake that returned ``{"status": "success"}`` would have passed a
    _vault_retrieve_value that never matched the real envelope."""

    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = secrets

    def retrieve(self, key: str) -> dict[str, Any]:
        value = self._secrets.get(key)
        if value is None:
            return {"action_status": "completed", "data": {"found": False, "key": key}}
        return {"action_status": "completed", "data": {"key": key, "value": value}}


def test_coerce_provider_env() -> None:
    _check(_coerce_provider_env({}) == {}, "coerce: absent provider_env -> {}")
    _check(
        _coerce_provider_env({"provider_env": None}) == {},
        "coerce: None provider_env -> {}",
    )
    _check(
        _coerce_provider_env({"provider_env": ["not", "a", "map"]}) == {},
        "coerce: wrong-shaped provider_env -> {} (tolerant, never raises)",
    )
    _check(
        _coerce_provider_env({"provider_env": {"A": 1}}) == {"A": "1"},
        "coerce: dict values stringified",
    )


def test_apply_provider_env_noop() -> None:
    base = {"ANTHROPIC_BEDROCK_BASE_URL": "inherited", "FOO": "bar"}
    env = dict(base)
    _apply_provider_env(env, None)
    _check(env == base, "apply: None overlay is a no-op (inherit daemon env)")
    env = dict(base)
    _apply_provider_env(env, {})
    _check(env == base, "apply: empty-dict overlay is a no-op (inherit)")


def test_apply_provider_env_bedrock() -> None:
    env = {"ANTHROPIC_API_KEY": "leftover_from_daemon", "FOO": "keep"}
    _apply_provider_env(
        env,
        {
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
            "ANTHROPIC_BEDROCK_BASE_URL": "http://gw/bedrock",
            "ANTHROPIC_AUTH_TOKEN": "tok",
        },
    )
    _check(env.get("CLAUDE_CODE_USE_BEDROCK") == "1", "apply bedrock: USE_BEDROCK set")
    _check(
        env.get("ANTHROPIC_BEDROCK_BASE_URL") == "http://gw/bedrock",
        "apply bedrock: base URL set",
    )
    _check(env.get("ANTHROPIC_AUTH_TOKEN") == "tok", "apply bedrock: token set")
    _check(
        "ANTHROPIC_API_KEY" not in env,
        "apply bedrock: an inherited ANTHROPIC_API_KEY is STRIPPED (switch is not additive)",
    )
    _check(env.get("FOO") == "keep", "apply bedrock: unrelated vars untouched")


def test_apply_provider_env_anthropic_strip() -> None:
    env = {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "ANTHROPIC_BEDROCK_BASE_URL": "http://gw",
        "ANTHROPIC_AUTH_TOKEN": "tok",
        "KEEP": "1",
    }
    _apply_provider_env(env, dict.fromkeys(_PROVIDER_ENV_KEYS, ""))
    _check("CLAUDE_CODE_USE_BEDROCK" not in env, "apply anthropic: USE_BEDROCK stripped")
    _check("ANTHROPIC_BEDROCK_BASE_URL" not in env, "apply anthropic: base URL stripped")
    _check("ANTHROPIC_AUTH_TOKEN" not in env, "apply anthropic: token stripped")
    _check(env.get("KEEP") == "1", "apply anthropic: unrelated vars untouched")


def _bare_plugin(vault: object | None) -> AgentMessagingPlugin:
    plugin = AgentMessagingPlugin.__new__(AgentMessagingPlugin)
    plugin._vault_service = vault  # type: ignore[attr-defined]
    return plugin


def test_resolve_provider_env_inherit() -> None:
    plugin = _bare_plugin(_FakeVault({}))
    _check(plugin._resolve_provider_env("") is None, "resolve: '' -> None (inherit)")
    _check(plugin._resolve_provider_env("   ") is None, "resolve: whitespace -> None")


def test_resolve_provider_env_unknown() -> None:
    plugin = _bare_plugin(_FakeVault({}))
    try:
        plugin._resolve_provider_env("openai")
        _check(False, "resolve: unknown provider raises")
    except _ProviderResolutionError as exc:
        _check(exc.code == "unknown_provider", "resolve: unknown provider -> unknown_provider")


def test_resolve_provider_env_anthropic() -> None:
    plugin = _bare_plugin(None)
    env = plugin._resolve_provider_env("anthropic")
    _check(
        env
        == {
            "CLAUDE_CODE_USE_BEDROCK": "",
            "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "",
            "ANTHROPIC_BEDROCK_BASE_URL": "",
            "ANTHROPIC_AUTH_TOKEN": "",
        },
        "resolve anthropic: explicit strip map, no vault needed",
    )
    _check(
        plugin._resolve_provider_env("Anthropic") == env,
        "resolve: provider name is case-insensitive",
    )


def test_resolve_provider_env_bedrock_ok() -> None:
    plugin = _bare_plugin(
        _FakeVault(
            {
                "dax.agent_messaging_plugin.bedrock_auth_token": "tok123",
                "dax.agent_messaging_plugin.bedrock_base_url": "http://gw/bedrock",
            },
        ),
    )
    prior = _restore_solet_name("dax")
    try:
        env = plugin._resolve_provider_env("bedrock")
    finally:
        _restore_solet_name_value(prior)
    _check(
        env is not None and env.get("ANTHROPIC_AUTH_TOKEN") == "tok123",
        "resolve bedrock: token",
    )
    _check(
        env is not None and env.get("ANTHROPIC_BEDROCK_BASE_URL") == "http://gw/bedrock",
        "resolve bedrock: base URL",
    )
    _check(
        env is not None and env.get("CLAUDE_CODE_USE_BEDROCK") == "1",
        "resolve bedrock: USE_BEDROCK=1",
    )


def test_resolve_provider_env_bedrock_missing_creds() -> None:
    plugin = _bare_plugin(_FakeVault({}))
    prior = _restore_solet_name("dax")
    try:
        plugin._resolve_provider_env("bedrock")
        _check(False, "resolve bedrock: missing creds fails loud")
    except _ProviderResolutionError as exc:
        _check(
            exc.code == "provider_env_unresolved",
            "resolve bedrock: missing vault creds -> provider_env_unresolved (never a "
            "silent wrong-provider spawn)",
        )
    finally:
        _restore_solet_name_value(prior)


def test_resolve_provider_env_bedrock_no_vault() -> None:
    plugin = _bare_plugin(None)
    prior = _restore_solet_name("dax")
    try:
        plugin._resolve_provider_env("bedrock")
        _check(False, "resolve bedrock: no vault fails loud")
    except _ProviderResolutionError as exc:
        _check(
            exc.code == "provider_env_unresolved",
            "resolve bedrock: no bound vault -> provider_env_unresolved",
        )
    finally:
        _restore_solet_name_value(prior)


def _restore_solet_name(value: str) -> str | None:
    import os

    prior = os.environ.get("SOLET_NAME")
    os.environ["SOLET_NAME"] = value
    return prior


def _restore_solet_name_value(prior: str | None) -> None:
    import os

    if prior is None:
        os.environ.pop("SOLET_NAME", None)
    else:
        os.environ["SOLET_NAME"] = prior


def test_headless_spawn_applies_bedrock_overlay() -> None:
    calls: list[dict[str, Any]] = []

    def _capture(cmd: list[str], **kwargs: Any) -> _FakeProc:
        calls.append({"cmd": cmd, **kwargs})
        return _FakeProc(pid=71001)

    prior = _pop_env_family()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            driver = _configured_driver(Path(tmp), popen_fn=_capture)
            driver.spawn(
                {
                    "agent_instance_id": "agi-bedrock",
                    "lane_id": "lane-b",
                    "provider": "bedrock",
                    "provider_env": {
                        "CLAUDE_CODE_USE_BEDROCK": "1",
                        "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
                        "ANTHROPIC_BEDROCK_BASE_URL": "http://gw/bedrock",
                        "ANTHROPIC_AUTH_TOKEN": "tok123",
                    },
                },
            )
            env = calls[0]["env"]
            _check(
                env.get("CLAUDE_CODE_USE_BEDROCK") == "1",
                "headless spawn: bedrock provider_env reaches the worker env",
            )
            _check(
                env.get("ANTHROPIC_AUTH_TOKEN") == "tok123",
                "headless spawn: bedrock token reaches the worker env",
            )
    finally:
        _restore_env_family(prior)


def test_headless_spawn_no_provider_inherits() -> None:
    import os

    calls: list[dict[str, Any]] = []

    def _capture(cmd: list[str], **kwargs: Any) -> _FakeProc:
        calls.append({"cmd": cmd, **kwargs})
        return _FakeProc(pid=71002)

    prior = _pop_env_family()
    sentinel_prior = os.environ.get("_PROVIDER_SMOKE_SENTINEL")
    os.environ["_PROVIDER_SMOKE_SENTINEL"] = "inherited-value"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            driver = _configured_driver(Path(tmp), popen_fn=_capture)
            driver.spawn({"agent_instance_id": "agi-inherit", "lane_id": "lane-i"})
            env = calls[0]["env"]
            _check(
                env.get("_PROVIDER_SMOKE_SENTINEL") == "inherited-value",
                "headless spawn: no provider directed -> daemon env inherited "
                "untouched (backward compatible)",
            )
    finally:
        if sentinel_prior is None:
            os.environ.pop("_PROVIDER_SMOKE_SENTINEL", None)
        else:
            os.environ["_PROVIDER_SMOKE_SENTINEL"] = sentinel_prior
        _restore_env_family(prior)


def test_tmux_omits_dev_channels_flag_on_bedrock() -> None:
    """The tmux driver must NOT pass --dangerously-load-development-channels
    when the provider will ignore it (Bedrock): the flag is inert there, and
    passing it makes the PTY-confirm expect loop wait for a prompt that never
    comes, then kill a fully-booted worker. Omitting it keeps
    _needs_dev_channels_confirmation false so the loop is skipped. An
    Anthropic / no-provider spawn is unchanged: flag present, loop runs."""
    from agent_messaging_plugin.tmux_adapter import (  # noqa: PLC0415
        _DEV_CHANNELS_FLAG,
        TmuxHostDriver,
        _needs_dev_channels_confirmation,
    )

    driver = TmuxHostDriver(
        tmux_bin="tmux", solet_name="dax", permission_mode="bypassPermissions",
    )
    cmd_bedrock = driver._spawn_command(
        {"provider": "bedrock", "provider_env": {"CLAUDE_CODE_USE_BEDROCK": "1"}},
        transport="watch", label="smoke-bedrock",
    )
    _check(
        _DEV_CHANNELS_FLAG not in cmd_bedrock,
        "tmux + bedrock: --dangerously-load-development-channels is OMITTED "
        "(inert on a third-party provider; passing it would hang the confirm loop)",
    )
    _check(
        _needs_dev_channels_confirmation(cmd_bedrock) is False,
        "tmux + bedrock: confirm expect loop is skipped (no flag = no prompt to wait for)",
    )
    cmd_default = driver._spawn_command({}, transport="watch", label="smoke-default")
    _check(
        _DEV_CHANNELS_FLAG in cmd_default,
        "tmux + no provider: dev-channels flag still present (Anthropic path unchanged)",
    )
    _check(
        _needs_dev_channels_confirmation(cmd_default) is True,
        "tmux + no provider: confirm expect loop still runs (unchanged)",
    )


def main() -> int:
    test_coerce_provider_env()
    test_apply_provider_env_noop()
    test_apply_provider_env_bedrock()
    test_apply_provider_env_anthropic_strip()
    test_resolve_provider_env_inherit()
    test_resolve_provider_env_unknown()
    test_resolve_provider_env_anthropic()
    test_resolve_provider_env_bedrock_ok()
    test_resolve_provider_env_bedrock_missing_creds()
    test_resolve_provider_env_bedrock_no_vault()
    test_headless_spawn_applies_bedrock_overlay()
    test_headless_spawn_no_provider_inherits()
    test_tmux_omits_dev_channels_flag_on_bedrock()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
