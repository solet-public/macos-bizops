"""Regression smoke for OpenAI-compatible served-model substitution."""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import TCPServer
from typing import cast

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from github_midwife_plugin.installation_model_doctor import inference_qualification  # noqa: E402
from github_midwife_plugin.setup_adapter_contract import AdapterRequest, JsonObject  # noqa: E402
from github_midwife_plugin.setup_adapter_runtime import SystemRuntime  # noqa: E402


class _LoopbackServer(HTTPServer):
    """Avoid reverse-DNS lookup while binding the fixture server."""

    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        host, port = cast(tuple[str, int], self.server_address)
        self.server_name = host
        self.server_port = port


class _Handler(BaseHTTPRequestHandler):
    served_model = "requested-X"
    requests: list[JsonObject] = []

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler protocol
        size = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(size))
        if isinstance(payload, dict):
            type(self).requests.append(cast(JsonObject, payload))
        response = json.dumps(
            {
                "model": type(self).served_model,
                "choices": [{"message": {"content": '{"action":"qualify"}'}}],
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def _request(base_url: str) -> AdapterRequest:
    return AdapterRequest(
        request_id="00000000-0000-4000-8000-000000000006",
        operation_id="setup.models.qualify_structured_actions",
        operation_ref="setup::models.qualify_structured_actions",
        phase="probe",
        probe_purpose="decision_qualification",
        attempt=1,
        name="served-model-identity",
        target=Path("/private/tmp/served-model-identity"),
        flow_source_revision="0" * 40,
        answers_fingerprint="sha256:" + "0" * 64,
        approval_fingerprint=None,
        dry_run=True,
        timeout_seconds=20,
        public_inputs={"candidate_id": "requested-X", "lm_studio_base_url": base_url},
    )


def main() -> int:
    server = _LoopbackServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    try:
        request = _request(f"http://{host}:{port}/v1")
        _Handler.served_model = "requested-X"
        identity = inference_qualification(request, SystemRuntime())
        _Handler.served_model = "served-Y"
        substituted = inference_qualification(request, SystemRuntime())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    repair = str(substituted.get("repair"))
    passed = (
        identity.get("checkpoint_status") == "verified"
        and substituted.get("checkpoint_status") != "verified"
        and "requested-X" in repair
        and "served-Y" in repair
        and [payload.get("model") for payload in _Handler.requests] == ["requested-X", "requested-X"]
    )
    print("PASS served-model identity and substitution refusal" if passed else "FAIL served-model identity")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
