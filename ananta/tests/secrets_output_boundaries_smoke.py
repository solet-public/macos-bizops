"""Public address resolution keeps references; secret expansion stays internal."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
for directory in (ROOT / "ananta/src", ROOT / "plugins/default_address_book_plugin/src"):
    sys.path.insert(0, str(directory))

from ananta.core.process_registry.invocation_schema_generator import InvocationSchemaGenerator  # noqa: E402
from ananta.core.process_registry.service_interface_scanner import ServiceInterfaceScanner  # noqa: E402
from ananta.services.address_book_service.interfaces.public import AddressBookServiceAPI  # noqa: E402
from default_address_book_plugin import address_ops  # noqa: E402
from default_address_book_plugin.plugin import DefaultAddressBookPlugin  # noqa: E402


def main() -> None:
    processes: dict[str, Any] = {}
    ServiceInterfaceScanner(InvocationSchemaGenerator())._scan_class_methods(AddressBookServiceAPI, processes)
    assert "service_interface::address_book_service::resolve_with_secrets" not in processes, "public registry still exposes decrypted values"  # wint:negative-fixture
    assert "service_interface::address_book_service::resolve" in processes
    cards = ROOT / "ananta/knowledge_base/processes/address_book_service"
    assert all(card.stem != "resolve_with_secrets" for card in cards.glob("*.json")), "plaintext process card remains"
    assert callable(DefaultAddressBookPlugin.resolve_with_secrets), "internal resolver must remain"
    reference = "vault::owned.resolver.fake"
    result: Any = {"action_status": "completed", "data": {"entries": [{"field_type": "token", "value": reference}]}}

    class Vault:
        def retrieve(self, key: str) -> dict[str, Any]:
            assert key == reference[7:]
            return {"action_status": "completed", "data": {"value": "fake-only-secret"}}

    with patch.object(address_ops, "resolve_impl", return_value=result):
        import logging

        expanded = address_ops.resolve_with_secrets_impl(None, "owned", None, False, Vault(), logging.getLogger(__name__), "owned")  # type: ignore[arg-type]
    expanded_data: Any = expanded.get("data")
    assert expanded_data["entries"][0]["value"] == "fake-only-secret"
    print("PASS real registry retirement, card removal and internal secret expansion")


def _response_data(response: Any) -> dict[str, Any]:
    assert response["action_status"] == "completed"
    return response["data"]


def registered_git_failure(harness: Any) -> None:
    """Real install admission and native Git failure must persist only public facts."""
    fake = "fake-only-git-credential"
    source = (harness.root / "missing-owned-source").as_uri()

    def address(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return {"action_status": "completed", "data": {"entries": [{"field_type": "url", "value": source}, {"field_type": "token", "value": fake}]}}

    def public_address(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return {"action_status": "completed", "data": {"entries": [{"field_type": "url", "value": source}, {"field_type": "token", "value": "vault::owned.resolver.git"}]}}

    harness.plugin._address_book_service = SimpleNamespace(resolve=public_address, resolve_with_secrets=address)
    job = harness.final("install", {"name": "credential-failure"}, status="error")
    assert fake not in json.dumps(job), "Git argv credential reached public persisted job error"
    assert "clone" in json.dumps(job["error"]) and "128" in json.dumps(job["error"]), "failure must reach native Git clone"
    identity = job["id"]
    payload = harness.manager.get_job_payload(identity, "request")
    assert fake not in json.dumps(payload)
    reread = harness.jobs.get_job(identity)
    assert fake not in json.dumps(reread), "historical failure readback exposed credential"
    history = _response_data(harness.state.read_state("core", {"table": "job_payload", "filters": {"job_id": identity}, "limit": 100}))["records"]
    assert history and fake not in json.dumps(history), "append-only job payload history exposed credential"
    source_path = harness.root / "owned-git-source"
    source_path.mkdir()
    (source_path / "a.md").write_text("# Owned Git\n\nPublic content.\n")
    for arguments in (["init", "-b", "main"], ["add", "a.md"], ["-c", "user.name=owned", "-c", "user.email=owned@example.invalid", "commit", "-m", "owned fixture"]):
        subprocess.run(["git", *arguments], cwd=source_path, capture_output=True, check=True)
    source = source_path.as_uri()
    harness.final("install", {"name": "credential-update"})
    source_path.rename(harness.root / "retired-owned-git-source")
    failed_update = harness.final("update", {"name": "credential-update"}, status="error")
    assert "fetch" in json.dumps(failed_update["error"]) and fake not in json.dumps(failed_update)
    update_history = _response_data(harness.state.read_state("core", {"table": "job_payload", "filters": {"job_id": failed_update["id"]}, "limit": 100}))["records"]
    assert update_history and fake not in json.dumps(update_history)
    assert fake not in (harness.root / "credential-update/.git/config").read_text()
    harness.final("uninstall", {"name": "credential-update"})
    harness.plugin._address_book_service = None
    print("PASS actual registered install/native Git failure, durable request and repeated job readback contain public facts only")


if __name__ == "__main__":
    main()
