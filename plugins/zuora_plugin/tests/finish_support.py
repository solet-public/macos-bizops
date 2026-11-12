"""Run one Zuora action through the plugin's single finish step, as the worker does.

Actions return finished content; ``ZuoraPlugin._finish`` wraps it, as
received, in the protocol envelope. Tests that drive an action directly read
its outward form through ``finished``.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zuora_plugin.http_client import ZuoraClient  # noqa: E402
from zuora_plugin.plugin import ZuoraPlugin  # noqa: E402


def finished(client: ZuoraClient, produce: Callable[[ZuoraClient], dict[str, Any]], params: dict[str, Any] | None = None, *, name: str = "request") -> dict[str, Any]:
    """The envelope the plugin publishes for one job: a fresh ledger, the action, then the finish step."""
    plugin = ZuoraPlugin()
    plugin.initialize({})
    plugin._client = client  # noqa: SLF001
    plugin._app_config_loader = MagicMock()  # noqa: SLF001
    return plugin._run(produce, name, params)  # noqa: SLF001
