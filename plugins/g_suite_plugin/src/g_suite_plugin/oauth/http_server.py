"""FastAPI app construction and uvicorn lifecycle for the Google callback surface.

Builds a minimal FastAPI application that serves the OAuth callback route. The
server runs in a background daemon thread with its own asyncio event loop,
mirroring the pattern established by schwab_market_data_plugin.

The plugin's start_interface/stop_interface @platform_process methods manage
this module's lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from ananta.core.runtime import PortManager

from ..constants import OAUTH_CALLBACK_PATH
from .app_config import AppConfigLoader
from .oauth_routes import create_oauth_router
from .token_store import TokenStore

_logger = logging.getLogger(__name__)


class OAuthServer:
    """Encapsulates the FastAPI/uvicorn OAuth callback server lifecycle."""

    def __init__(self) -> None:
        self._port_manager: PortManager = PortManager("gsuite")
        self._server_loop: asyncio.AbstractEventLoop | None = None
        self._server_thread: threading.Thread | None = None
        self._started = threading.Event()

    @property
    def port(self) -> int | None:
        return self._port_manager.port

    def start(
        self,
        host: str,
        preferred_port: int | None,
        token_store: TokenStore,
        app_config_loader: AppConfigLoader,
        pending_states: dict[str, str],
    ) -> int:
        """Start the uvicorn server in a background thread. Returns allocated port."""
        self._port_manager.allocate(preferred_port)
        port = self._port_manager.port
        assert port is not None

        oauth_router = create_oauth_router(token_store, app_config_loader, pending_states)

        def run() -> None:
            import uvicorn
            from fastapi import FastAPI

            app = FastAPI(title="Google Workspace OAuth Callback", version="1.0.0")
            app.include_router(oauth_router)

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._server_loop = loop

            config = uvicorn.Config(app, host=host, port=port, log_level="warning", loop="asyncio")
            server = uvicorn.Server(config)
            self._started.set()
            try:
                loop.run_until_complete(server.serve())
            except Exception as exc:
                _logger.error("Google OAuth callback server error: %s", exc)
            finally:
                loop.close()

        self._server_thread = threading.Thread(target=run, name="gsuite-oauth-srv", daemon=True)
        self._server_thread.start()
        self._started.wait(timeout=5.0)
        return port

    def stop(self) -> None:
        """Gracefully stop the server and release the port."""
        if self._server_loop and self._server_loop.is_running():
            self._server_loop.call_soon_threadsafe(self._server_loop.stop)
        self._port_manager.release()
        self._server_loop = None
        self._started.clear()

    def is_running(self) -> bool:
        return self._server_loop is not None and self._server_loop.is_running()


def build_start_result(port: int, host: str) -> dict[str, Any]:
    return {
        "port": port,
        "host": host,
        "callback_url": f"http://{host}:{port}{OAUTH_CALLBACK_PATH}",
        "message": "OAuth callback server started. ALB routes /oauth/google/* to this port.",
    }
