#!/usr/bin/env python3
"""Registered vault admission with private PostgreSQL and synthetic credentials.

The factory, recorder, registry, processor, poller, bound macOS provider, job
manager and stores are real. Keychain bytes stay in an owned memory fixture;
wrapping and sealed-box crypto use their production implementations. No live
credential or application database is selected. Missing private PG is advisory.

Proof boundaries:

* Park an actual credential write before admitting unrelated ordinary work.
  A fast validation refusal cannot satisfy the leaf-entry witness.
* Park file reads, sealed-box decryption and the metadata/OAuth population
  leaves separately. A fast sibling cannot establish another operation's
  placement. The poller's existing transport preflight is outside this timing.
* Keep direct caller-bound operations synchronous under the provider guard;
  nested S1 reference use and metadata cannot create another result-bearing job.
* Preserve the sealed-bootstrap caller's immediate public ciphertext and zero
  job contract alongside the three existing bridge carve-outs. The encrypted
  import proof parks real sealed-box decryption on the admitted worker instead.
* Exercise credential correction, reversal, rename, deletion and recreation.
  The existing Keychain-only substrate has no credential CAS or historical
  version readback; this admission change does not invent either lifecycle.
* Read exact job ids and all request/result/error payloads. Synthetic plaintext
  deliberately appears in a throwing provider and a failed envelope, so absence
  checks can distinguish sanitization from an unexercised failure branch.
* Interrupt a worker after its effect and refuse a terminal write separately.
  Uncertain effects remain visible; a successor may only enter untouched work.
* Fail the terminal notification after the public outcome commits. Transport
  failure cannot erase that result or authorize repeating the credential effect.

Queue transport and completion destinations are local recording fixtures. The
real poller still drains ordinary actions and resolves each original action
through its production registered-completion lookup. Private PostgreSQL owns
the action, flow-token, job and payload records; a missing manifest never falls
back to an installed application's database. Host Keychain prompts, real user
credentials and production workload percentiles are deliberately unmeasured.
"""

from __future__ import annotations

import asyncio
import importlib
import io
import json
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
for directory in (ROOT, ROOT / "ananta/src", ROOT / "plugins/macos_vault_plugin/src", ROOT / "plugins/postgres_state_management_plugin/src", ROOT / "plugins/macos_vault_plugin/tests"):
    sys.path.insert(0, str(directory))
for source in ("plugins/macos_vault_plugin/src/macos_vault_plugin", "plugins/postgres_state_management_plugin/src/postgres_state_management_plugin"):
    if not (ROOT / source).is_dir():
        print(f"ADVISORY optional source absent: {source}; private vault proof skipped")
        raise SystemExit(77)
if not (ROOT / "plugins/macos_vault_plugin/tests/fake_keychain.py").is_file():
    print("ADVISORY optional synthetic Keychain fixture absent; private vault proof skipped")
    raise SystemExit(77)
os.environ["SOLET_NAME"] = "g1vaultfixture"

from ananta.config.core_schemas import CoreSchemaDefinitions  # noqa: E402
from ananta.core.actions.action_factory import ActionFactory  # noqa: E402
from ananta.core.actions.action_processor import ActionProcessor  # noqa: E402
from ananta.core.actions.action_queue_poller import ActionQueuePoller, QueuedAction  # noqa: E402
from ananta.core.event_orchestrator import EventOrchestrator  # noqa: E402
from ananta.core.orchestration.execution_context import ExecutionContextManager  # noqa: E402
from ananta.core.orchestration.managers.action_event_recorder import ActionEventRecorder  # noqa: E402
from ananta.core.orchestration.managers.flow_manager import FlowManager  # noqa: E402
from ananta.core.orchestration.service_bindings import BindingSource, ServiceBinding, ServiceBindings, ServiceName  # noqa: E402
from ananta.core.plugins.plugin_manager import PluginManager  # noqa: E402
from ananta.core.process_registry.invocation_schema_generator import InvocationSchemaGenerator  # noqa: E402
from ananta.core.process_registry.service_interface_scanner import ServiceInterfaceScanner  # noqa: E402
from ananta.core.runtime.loop_executors import close_loop_executors  # noqa: E402
from ananta.core.services.call_context import CallContext, VaultAccessDeniedError  # noqa: E402
from ananta.core.state.async_job_manager import AsyncJobManager  # noqa: E402
from ananta.core.state.flow_runtime_graph import FlowRuntimeGraph  # noqa: E402
from ananta.interfaces.credential_contracts import MetadataRequest, SignRequest, VaultReference  # noqa: E402
from ananta.services.discovery_service.service import DiscoveryService  # noqa: E402
from ananta.services.job_service.service import JobService  # noqa: E402
from ananta.services.schema_manager import SchemaManager  # noqa: E402
from ananta.services.vault_service.vault_service_proxy import VaultServiceProxy  # noqa: E402
from fake_keychain import FakeKeychain  # noqa: E402
from macos_vault_plugin.constants import PLUGIN_NAME, passphrase_env_var  # noqa: E402
from macos_vault_plugin.key_manager import VaultKeyManager  # noqa: E402
from macos_vault_plugin.plugin import MacosVaultPlugin  # noqa: E402

from quality_gates.private_test_pg import MANIFEST_ENV, LedgerFixture, ledger_fixture, skip_fixture  # noqa: E402

OPERATIONS = frozenset(
    {
        "delete",
        "ensure_encryption_keypair",
        "get_public_key",
        "import_encrypted",
        "list",
        "oauth_client_list",
        "qualify_keychain",
        "qualify_round_trip",
        "rename",
        "status",
        "store_from_env",
        "store_from_file",
        "store_from_keychain",
        "store_from_kv_file",
        "store_random",
        "unlock",
        "vault_create_recovery",
        "vault_init",
        "vault_rotate_passphrase",
    }
)
SECRET = "owned-vault-operand-42d6f1"
CORRECTED = "owned-vault-correction-19bc8a"
ENV_KEY = "G1_VAULT_OWNED_INPUT"
OPERATOR = CallContext.for_operator()


def data(response: Any) -> dict[str, Any]:
    """Read only successful fixture state envelopes without printing operands."""
    assert response.get("action_status") == "completed", "fixture state operation failed"
    result = response.get("data")
    assert isinstance(result, dict), "fixture state response has no data"
    return cast(dict[str, Any], result)


def registry() -> dict[str, Any]:
    processes: dict[str, Any] = {}
    scanner = ServiceInterfaceScanner(InvocationSchemaGenerator())
    for provider in ("vault_service", "job_service"):
        module = importlib.import_module(f"ananta.services.{provider}.interfaces.public")
        scanner._scan_module_classes(module, module.__name__, processes)
    return processes


class Keychain(FakeKeychain):
    """Owned credential and wrapped-key substrates, with a named parked leaf."""

    def __init__(self) -> None:
        super().__init__()
        self.wrapped: dict[str, bytes] = {}
        self.park = False
        self.entered, self.release = threading.Event(), threading.Event()
        self.writers: list[int] = []

    def exists(self, account: str) -> bool:
        return account in self.wrapped

    def retrieve(self, account: str) -> bytes | None:
        return self.wrapped.get(account)

    def store(self, account: str, value: bytes) -> None:
        self.wrapped[account] = value

    def store_credential(self, plugin_name: str, credential: str, value: bytes) -> None:
        self.writers.append(threading.get_ident())
        if self.park:
            self.park = False
            self.entered.set()
            assert self.release.wait(10), "owned keychain write must be released"
        super().store_credential(plugin_name, credential, value)


class NoTemplates:
    def resolve_templates(self, action_def: dict[str, object], context: dict[str, object]) -> dict[str, object]:
        del action_def, context
        raise AssertionError("owned factory inputs have no templates")


@dataclass
class Observed:
    receipts: dict[str, dict[str, Any]] = field(default_factory=dict)
    terminals: list[tuple[str, str]] = field(default_factory=list)
    seconds: dict[str, float] = field(default_factory=dict)
    dispatch_threads: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)


class Harness:
    """Real registered caller and persistence stack; only transport is local."""

    def __init__(self, fixture: LedgerFixture, keychain: Keychain) -> None:
        self.state, self.keychain = fixture.state, keychain
        self.plugin = MacosVaultPlugin()
        schemas = SchemaManager(self.state, fixture.plugin)
        definitions = [
            CoreSchemaDefinitions.get_sessions_schema(),
            CoreSchemaDefinitions.get_flows_schema(),
            CoreSchemaDefinitions.get_flow_tokens_schema(),
            CoreSchemaDefinitions.get_action_events_schema(),
            CoreSchemaDefinitions.get_job_schema(),
            CoreSchemaDefinitions.get_job_payload_schema(),
            *self.plugin.get_schema_definitions(),
        ]
        schemas.initialize_schemas(definitions)
        self.state.set_schema_manager(schemas)
        plugins = PluginManager()
        plugins.plugins[PLUGIN_NAME] = self.plugin
        graph = FlowRuntimeGraph(self.state)
        self.manager = AsyncJobManager(self.state, graph)
        self.jobs = JobService(self.state, self.manager)
        bindings = ServiceBindings(str(fixture.app_home))
        bindings._bindings = {ServiceName("vault_service"): ServiceBinding(ServiceName("vault_service"), PLUGIN_NAME, BindingSource.CONFIG)}
        bindings._loaded = True
        orchestrator: Any = EventOrchestrator.__new__(EventOrchestrator)
        orchestrator.APP_HOME, orchestrator.plugin_manager, orchestrator.service_bindings = str(fixture.app_home), plugins, bindings
        orchestrator.async_job_manager, orchestrator.action_recorder = self.manager, None
        orchestrator.state_service = self.state
        orchestrator.service_manager = SimpleNamespace(job_service=self.jobs)
        self.plugin.set_orchestrator_ref(orchestrator)
        self.plugin.prepare_for_readiness()
        self.plugin.set_ready()
        processes = registry()
        discovery: Any = DiscoveryService.__new__(DiscoveryService)
        discovery.processes = processes
        self.contexts = ExecutionContextManager()
        self.processor = ActionProcessor(plugins, state_service=self.state, orchestrator=orchestrator, process_registry={"processes": processes}, execution_context_manager=self.contexts, discovery_service=discovery)
        self.flows = FlowManager(self.state)
        recorder = ActionEventRecorder(self.state, self.flows, graph)
        self.factory = ActionFactory({"processes": processes}, NoTemplates(), cast(Any, self.state), recorder, str(fixture.app_home))
        self.session = data(self.state.write_state("core", {"table": "sessions", "record": {"context_type": "owned_vault_fixture", "status": "active"}}))["result"]["generated_id"]
        self.queue: list[QueuedAction] = []
        self.observed = Observed()
        self.poller = self.make_poller()
        self.manager.set_registered_completion_handler(self.poller._complete_registered_job)

    def action(self, verb: str, values: dict[str, object], *, caller: str | None = None, provider: str = "vault_service") -> QueuedAction:
        trigger: dict[str, object] = {"authenticated_principal": {"client_id": caller}} if caller else {}
        flow = self.flows.create_flow(self.session, "owned_fixture", trigger)
        self.contexts.create_context(flow)
        key = f"service_interface::{provider}::{verb}"
        identity = self.factory.submit_action_definition({"name": f"owned_{verb}", "process_key": key, "arguments": values, "notes": "owned vault admission proof", "flow_id": flow, "session_id": self.session, "result_processor_kind": None, "error_processor_kind": None})
        row = data(self.state.read_state("core", {"table": "action_events", "filters": {"id": identity}, "limit": 1}))["records"][0]
        assert row["flow_token_id"], "factory must persist a real execution token"
        return QueuedAction(identity, key, row["parameters"], row["notes"], str(row["sequence"]), session_id=self.session, flow_id=flow, flow_token_id=row["flow_token_id"])

    def dispatch(self, action: QueuedAction) -> dict[str, Any]:
        before = time.perf_counter()
        self.observed.dispatch_threads[action.id] = threading.get_ident()
        result = self.processor.execute_action(cast(Any, action))
        self.observed.seconds[action.id] = time.perf_counter() - before
        return cast(dict[str, Any], result)

    def completed(self, identity: str, result: dict[str, Any], _token: str | None = None, on_status_written: Any = None, *, owned_job_completion: bool = False) -> None:
        if owned_job_completion:
            self.observed.terminals.append((identity, "completed"))
        else:
            self.observed.receipts[identity] = result
        if on_status_written is not None:
            on_status_written()

    def failed(self, identity: str, _message: str, *_args: object, owned_job_completion: bool = False, **_kwargs: object) -> None:
        if owned_job_completion:
            self.observed.terminals.append((identity, "error"))
        else:
            self.observed.failures.append(identity)

    def make_poller(self) -> Any:
        poller: Any = ActionQueuePoller.__new__(ActionQueuePoller)

        async def queued() -> list[QueuedAction]:
            return list(self.queue)

        def claim(identity: str) -> bool:
            self.queue[:] = [action for action in self.queue if action.id != identity]
            return True

        poller._get_queued_actions, poller._mark_action_processing = queued, claim
        poller._is_terminated_flow_sibling = lambda _action: False
        poller._prepare_action_for_execution = lambda _action: True
        poller._resolve_io_context = lambda _action: None
        poller._mark_action_completed, poller._mark_action_failed = self.completed, self.failed
        poller.state_service = self.state
        poller.action_processor = SimpleNamespace(execute_action=self.dispatch)
        poller.total_actions_processed = poller.total_poll_cycles = 0
        poller._last_observed_queue_depth = 0
        return poller

    def join(self) -> None:
        for owner in list(self.manager._owners.values()):
            thread = owner._thread
            if thread is not None:
                thread.join(10)
                assert not thread.is_alive(), "owned vault worker must drain"

    def job(self, identity: str) -> dict[str, Any]:
        return cast(dict[str, Any], self.jobs.get_job(identity)["data"]["result"]["job"])

    def job_count(self) -> int:
        return len(data(self.state.read_state("core", {"table": "job", "limit": 100}))["records"])

    def final(self, verb: str, values: dict[str, object], *, status: str = "completed", caller: str | None = None) -> dict[str, Any]:
        before = self.job_count()
        action = self.action(verb, values, caller=caller)
        response = self.dispatch(action)
        assert response.get("success") is True, "registered vault admission failed"
        assert {"actions", "error", "timestamp"} <= response.keys(), "receipt lacks its ActionResult envelope"
        identity = response["data"].get("job_id")
        assert isinstance(identity, str), "selected vault operation returned no job"
        self.join()
        job = self.job(identity)
        assert self.job_count() == before + 1, "one outer invocation must create exactly one job"
        assert job["status"] == status, f"unexpected public terminal status for {verb}"
        assert self.observed.terminals.count((action.id, status)) == 1, "exactly one original-action completion is required"
        assert_lineage(self, job, action)
        return job


def assert_lineage(harness: Harness, job: dict[str, Any], action: QueuedAction) -> None:
    metadata = json.loads(job["metadata"]) if isinstance(job["metadata"], str) else job["metadata"]
    assert job["flow_token_id"] == action.flow_token_id and job["group_id"] == f"registered:{action.id}", "job lost its actual action token"
    assert metadata["registered_action_id"] == action.id and metadata["flow_id"] == action.flow_id and metadata["session_id"] == action.session_id, "job lost server-derived lineage"
    request = data(harness.manager.get_job_payload(job["id"], "request"))["payload"]
    assert len(json.dumps(request)) < 512, "vault request must be a bounded public descriptor"
    assert request["parameters"].get("volatile_arguments") is True, "vault operands must remain volatile"


async def parked_admission(harness: Harness) -> None:
    """The first real Keychain write blocks; receipt and unrelated work progress."""
    control_id = data(harness.manager.create_job(PLUGIN_NAME, "status", {"notes": "owned point-reader control"}))["job_id"]
    first = harness.action("store_from_env", {"key": "g1vaultfixture.owner.first", "env_var": ENV_KEY})
    control = harness.action("get_job", {"job_id": control_id}, provider="job_service")
    harness.keychain.writers.clear()
    harness.keychain.park = True
    harness.queue[:] = [first, control]
    polling = asyncio.create_task(harness.poller._poll_once())
    entered = await asyncio.to_thread(harness.keychain.entered.wait, 5)
    print("WITNESS actual bound vault store_from_env reached synthetic Keychain write:", entered)
    try:
        assert entered, "valid registered setup must reach the credential leaf"
        await asyncio.sleep(0.08)
        returned_while_parked = first.id in harness.observed.receipts and control.id in harness.observed.receipts
        with ThreadPoolExecutor(max_workers=1) as executor:
            direct = executor.submit(harness.plugin.store, "g1vaultfixture.owner.direct", SECRET, call_context=OPERATOR)
            try:
                await asyncio.sleep(0.03)
                direct_waited = not direct.done()
            finally:
                harness.keychain.release.set()
                direct_result = direct.result(5)
        await polling
    finally:
        harness.keychain.release.set()
        await polling
        harness.join()
        await close_loop_executors()
    assert returned_while_parked, "persisted vault admission must return while its actual credential leaf is parked"
    assert direct_waited and data(direct_result)["key"] == "g1vaultfixture.owner.direct", "internal full result must share the provider fence"
    assert not harness.observed.failures, "ordinary poller dispatch failed"
    response = harness.observed.receipts[first.id]
    assert response["data"]["status"] == "queued" and harness.observed.seconds[first.id] < 0.05, "complete registered vault admission exceeded 50 ms"
    assert harness.observed.dispatch_threads[first.id] not in harness.keychain.writers, "credential work reached the caller thread"
    job = harness.job(response["data"]["job_id"])
    assert job["status"] == "completed" and harness.observed.terminals.count((first.id, "completed")) == 1
    assert_lineage(harness, job, first)
    assert harness.job_count() == 2, "direct calls and the point reader cannot admit extra jobs"
    print("PASS parked Keychain admission <50ms; unrelated action progresses; direct caller retains final result")


def parked_leaf(harness: Harness, verb: str, values: dict[str, object], owner: object, name: str, *, selected_path: Path | None = None) -> None:
    """Park a real file/crypto/metadata leaf after valid registered admission."""
    original: Callable[..., Any] = getattr(owner, name)
    entered, release = threading.Event(), threading.Event()
    callers: list[int] = []

    def body(*args: Any, **kwargs: Any) -> Any:
        if selected_path is None or args[0] == selected_path:
            callers.append(threading.get_ident())
            entered.set()
            assert release.wait(10), "owned operation leaf must be released"
        return original(*args, **kwargs)

    action = harness.action(verb, values)
    before = harness.job_count()
    with patch.object(owner, name, new=body):
        try:
            receipt = harness.dispatch(action)
            assert entered.wait(5), "actual registered operation must reach its parked leaf"
            assert harness.observed.seconds[action.id] < 0.05, "complete admission exceeded 50 ms"
            identity = receipt["data"]["job_id"]
            assert harness.job(identity)["status"] == "processing", "operation completed before its parked leaf was released"
            assert harness.job_count() == before + 1 and threading.get_ident() not in callers
        finally:
            release.set()
            harness.join()
    job = harness.job(identity)
    assert job["status"] == "completed" and harness.observed.terminals.count((action.id, "completed")) == 1
    assert_lineage(harness, job, action)
    print(f"PASS {verb}: real leaf parked, receipt <50ms, one job and one public completion")


def remaining_parked_leaves(harness: Harness, root: Path, manager: VaultKeyManager) -> None:
    from nacl.public import SealedBox

    path = root / "parked-credential.txt"
    path.write_text(SECRET, encoding="utf-8")
    parked_leaf(harness, "store_from_file", {"key": "g1vaultfixture.owner.parked_file", "file_path": str(path)}, Path, "read_text", selected_path=path)
    public = data(harness.plugin.get_public_key(call_context=OPERATOR))["public_key"]
    exported = synchronous_export(harness, public)
    parked_leaf(harness, "import_encrypted", {"name": "g1vaultfixture.owner.parked_crypto", "ciphertext": exported["ciphertext"]}, SealedBox, "decrypt")
    parked_leaf(harness, "status", {}, manager, "get_status")
    parked_leaf(harness, "oauth_client_list", {}, harness.plugin._oauth_registry, "list_clients")


def declarations() -> None:
    processes = registry()
    for name in OPERATIONS:
        entry = processes[f"service_interface::vault_service::{name}"]
        assert entry["is_async"] is True and entry["action_blueprint"]["metadata"]["is_async"] is True, name
        assert set(entry["output"]["properties"]) == {"job_id", "status"}, name
    for name in ("store", "rotate", "exists", "export_encrypted", "lock", "oauth_client_register", "oauth_client_revoke", "oauth_client_add_redirect_uri"):
        assert processes[f"service_interface::vault_service::{name}"]["is_async"] is False, name
    removed = "service_interface::vault_service::" + "retrieve"
    assert removed not in processes and not (ROOT / "ananta/knowledge_base/processes/vault_service" / "retrieve.json").exists(), "removed session retrieval was resurrected"
    print("PASS 19 real receipt declarations; four caller carve-outs and bounded controls unchanged; retrieve absent")


def synchronous_export(harness: Harness, public: str) -> dict[str, Any]:
    """Keep the sealed-bootstrap caller's immediate public ciphertext contract."""
    before = harness.job_count()
    result = harness.dispatch(harness.action("export_encrypted", {"secret_name": "g1vaultfixture.owner.first", "recipient_pubkey": public}))
    assert result["action_status"] == "completed" and "ciphertext" in result["data"], "sealed bootstrap requires immediate ciphertext, never a job receipt"
    assert harness.job_count() == before, "synchronous export carve-out created a job"
    return data(result)


def credential_lifecycle(harness: Harness, root: Path) -> None:
    """Create, correct, reverse, rename and retire using synthetic operands."""
    key = "g1vaultfixture.owner.lifecycle"
    path = root / "credential.txt"
    path.write_text(SECRET, encoding="utf-8")
    harness.final("store_from_file", {"key": key, "file_path": str(path)})
    duplicate = harness.final("store_from_file", {"key": key, "file_path": str(path)}, status="error")
    assert duplicate["error"]["code"] == "vault.key_exists", "create collision changed category"
    assert data(harness.plugin.rotate(key, CORRECTED, call_context=OPERATOR))["version"] == 1
    assert data(harness.plugin.retrieve(key, call_context=OPERATOR))["value"] == CORRECTED
    harness.plugin.rotate(key, SECRET, call_context=OPERATOR)
    renamed = "g1vaultfixture.owner.renamed"
    harness.final("rename", {"old_key": key, "new_key": renamed})
    assert data(harness.plugin.exists(key, call_context=OPERATOR))["exists"] is False
    harness.final("rename", {"old_key": renamed, "new_key": key})
    listing = harness.final("list", {})["result"]["data"]["secrets"]
    assert any(row["key"] == key for row in listing) and all("value" not in row for row in listing)
    harness.final("delete", {"key": key})
    assert data(harness.plugin.retrieve(key, call_context=OPERATOR))["found"] is False
    harness.final("store_from_env", {"key": key, "env_var": ENV_KEY})
    credential_sources(harness, root)
    caller_bound_lifecycle(harness, key)
    print("PASS credential lifecycle, collision, reversal, S1 metadata/use and unchanged principal/namespace denial")


def credential_sources(harness: Harness, root: Path) -> None:
    """Resolve owned environment/file/Keychain inputs entirely in the worker."""
    harness.final("store_random", {"key": "g1vaultfixture.owner.random", "byte_length": 32})
    kv_path = root / "credential.json"
    kv_path.write_text(json.dumps({"token": SECRET}), encoding="utf-8")
    harness.final("store_from_kv_file", {"key": "g1vaultfixture.owner.kv", "file_path": str(kv_path), "field": "token"})
    with patch.object(harness.plugin, "_lookup_keychain_password", return_value=SECRET):
        harness.final("store_from_keychain", {"key": "g1vaultfixture.owner.copied", "service": "owned-source", "account": "owned-account"})
    for verb in ("qualify_keychain", "qualify_round_trip", "status"):
        harness.final(verb, {})


def caller_bound_lifecycle(harness: Harness, key: str) -> None:
    """S1 use and metadata retain their full typed synchronous results."""
    proxy = VaultServiceProxy(harness.plugin, "owner")
    assert proxy.metadata(MetadataRequest("exists", VaultReference(key))).exists is True
    assert proxy.use(SignRequest(VaultReference(key), b"public signing input")).status == "completed"
    try:
        proxy.retrieve("g1vaultfixture.foreign.credential")
    except VaultAccessDeniedError:
        pass
    else:
        raise AssertionError("caller-bound S1 retrieval allowed a foreign namespace")
    denied = harness.final("delete", {"key": key}, status="error", caller="foreign")
    assert denied["error"]["code"] == "vault.access_denied", "namespace denial lost typing"
    assert proxy.metadata(MetadataRequest("exists", VaultReference(key))).exists is True
    harness.final("get_public_key", {}, status="error", caller="owner")


def crypto_and_admin(harness: Harness, root: Path, manager: VaultKeyManager) -> None:
    """Use real wrapping/sealed-box crypto against only the owned Keychain."""
    harness.final("ensure_encryption_keypair", {})
    public = harness.final("get_public_key", {})["result"]["data"]["public_key"]
    exported = synchronous_export(harness, public)
    harness.final("import_encrypted", {"name": "g1vaultfixture.owner.imported", "ciphertext": exported["ciphertext"]})
    assert data(harness.plugin.retrieve("g1vaultfixture.owner.imported", call_context=OPERATOR))["value"] == SECRET
    harness.final("import_encrypted", {"name": "g1vaultfixture.owner.invalid", "ciphertext": "invalid"}, status="error")
    manager.lock()
    harness.final("unlock", {"passphrase": SECRET})
    harness.final("vault_create_recovery", {"recovery_passphrase": CORRECTED})
    manager.lock()
    manager.recover_with_recovery_key(CORRECTED)
    assert manager.is_unlocked()
    harness.final("vault_rotate_passphrase", {"old_passphrase": SECRET, "new_passphrase": CORRECTED})
    manager.lock()
    harness.final("unlock", {"passphrase": CORRECTED})
    assert (root / "config/plugins/macos_vault_plugin/passphrase").read_text() == CORRECTED
    business = harness.final("unlock", {"passphrase": SECRET})["result"]["data"]
    assert business["authenticated"] is False, "invalid passphrase changed its business outcome"
    harness.final("vault_init", {"passphrase": SECRET}, status="error")
    print("PASS encrypted transfer, initialization refusal, unlock, recovery and persistent passphrase rotation")


def oauth_lifecycle(harness: Harness) -> None:
    """Raw registration output stays local; only public list metadata is durable."""
    registered = harness.plugin.oauth_client_register("owned-client", redirect_uris=["https://example.invalid/callback"], call_context=OPERATOR)
    client_id = data(registered)["client_id"]
    assert isinstance(client_id, str)
    listed = harness.final("oauth_client_list", {})["result"]["data"]["clients"]
    assert any(client["client_id"] == client_id for client in listed)
    assert all("client_secret" not in client and "client_secret_hash" not in client for client in listed)
    harness.plugin.oauth_client_add_redirect_uri(client_id, "https://example.invalid/corrected", call_context=OPERATOR)
    corrected = harness.final("oauth_client_list", {})["result"]["data"]["clients"]
    assert any("https://example.invalid/corrected" in client["redirect_uris"] for client in corrected)
    harness.plugin.oauth_client_revoke(client_id, call_context=OPERATOR)
    assert all(client["client_id"] != client_id for client in harness.final("oauth_client_list", {})["result"]["data"]["clients"])
    print("PASS OAuth public list create/correct/retire lifecycle without durable credential output")


def failure_confinement(harness: Harness) -> None:
    """Provider exceptions and uncertain terminal writes cannot persist operands."""
    with patch.object(harness.keychain, "list_credentials_under_solet", side_effect=RuntimeError(SECRET)):
        failure = harness.final("list", {}, status="error")
    assert failure["error"]["code"] == "vault.operation_failed"
    unsafe = {"action_status": "error", "data": {"value": SECRET}, "actions": [{"arguments": {"value": SECRET}}], "error": {"code": "vault.encryption_failed", "message": SECRET, "details": {"operand": SECRET}}}
    with patch.object(harness.plugin, "_list_impl", return_value=unsafe):
        projected = harness.final("list", {}, status="error")
    assert projected["error"]["code"] == "vault.encryption_failed" and projected["result"]["data"] == {}
    original_update = harness.manager.update_job
    writes: list[str] = []

    def refuse(identity: str, updates: dict[str, object]) -> dict[str, object]:
        if updates.get("status") in {"completed", "error"}:
            writes.append(identity)
            return {"action_status": "error", "error": "owned terminal write unavailable"}
        return original_update(identity, updates)

    action = harness.action("list", {})
    with patch.object(harness.manager, "update_job", side_effect=refuse), patch.object(harness.keychain, "list_credentials_under_solet", side_effect=RuntimeError(SECRET)) as leaf:
        receipt = harness.dispatch(action)
        harness.join()
    identity = receipt["data"]["job_id"]
    assert writes == [identity] and leaf.call_count == 1, "uncertain terminal persistence replayed the effect"
    assert harness.job(identity)["status"] == "processing" and not any(item[0] == action.id for item in harness.observed.terminals)
    harness.final("status", {})
    print("PASS sanitized provider exception; terminal-write uncertainty remains visible without replay")


def notification_failure(harness: Harness) -> None:
    """A failed terminal notification preserves the committed public outcome."""
    action = harness.action("store_from_env", {"key": "g1vaultfixture.owner.notified", "env_var": ENV_KEY})
    attempts: list[str] = []

    def unavailable(identity: str, _status: str, _result: dict[str, object] | None, _error: dict[str, object] | None) -> None:
        attempts.append(identity)
        raise RuntimeError("owned notification unavailable")

    harness.manager.set_registered_completion_handler(unavailable)
    try:
        with patch.object(harness.keychain, "store_credential", wraps=harness.keychain.store_credential) as leaf:
            receipt = harness.dispatch(action)
            harness.join()
    finally:
        harness.manager.set_registered_completion_handler(harness.poller._complete_registered_job)
    job = harness.job(receipt["data"]["job_id"])
    assert job["status"] == "completed" and job["result"]["data"]["key"] == "g1vaultfixture.owner.notified"
    assert attempts == [action.id] and leaf.call_count == 1, "notification failure cannot rerun the provider operation"
    print("PASS committed public result remains readable after one failed terminal notification")


def worker_loss(harness: Harness) -> None:
    """A worker can die after an effect; its successor must not replay it."""
    entered: list[str] = []
    original_store = harness.keychain.store_credential

    def lost(plugin: str, credential: str, value: bytes) -> None:
        original_store(plugin, credential, value)
        entered.append(credential)
        raise SystemExit(SECRET)

    with patch.object(harness.keychain, "store_credential", side_effect=lost):
        failure = harness.final("store_from_env", {"key": "g1vaultfixture.owner.lost", "env_var": ENV_KEY}, status="error")
    assert failure["error"]["code"] == "registered_owner.worker_lost"
    assert entered == ["lost"] and harness.keychain.exists_credential("owner", "lost")
    harness.final("status", {})
    print("PASS worker loss records uncertainty; untouched successors progress; entered effect executes once")


def no_secret_persistence(harness: Harness, captured: io.StringIO) -> None:
    jobs = data(harness.state.read_state("core", {"table": "job", "limit": 100}))["records"]
    for job in jobs:
        for kind in ("request", "result", "error"):
            payload = harness.manager.get_job_payload(job["id"], kind)
            encoded = json.dumps(payload, default=str)
            assert SECRET not in encoded and CORRECTED not in encoded, "credential escaped into a durable job payload"
        encoded_job = json.dumps(job, default=str)
        assert SECRET not in encoded_job and CORRECTED not in encoded_job, "credential escaped into job metadata"
    assert SECRET not in captured.getvalue() and CORRECTED not in captured.getvalue(), "credential escaped into logs"
    assert len({job["id"] for job in jobs}) == len(jobs)
    print(f"PASS secret absence across {len(jobs)} jobs, every request/result/error, and captured logs")


def main() -> int:
    if not os.environ.get(MANIFEST_ENV):
        declarations()
        return skip_fixture()
    captured = io.StringIO()
    handler = logging.StreamHandler(captured)
    logging.getLogger().addHandler(handler)
    try:
        with ledger_fixture("fast_return_census") as fixture, ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"APP_HOME": str(fixture.app_home), ENV_KEY: SECRET}))
            os.environ.pop(passphrase_env_var(), None)
            path = fixture.app_home / "config/plugins/macos_vault_plugin/passphrase"
            path.parent.mkdir(parents=True)
            path.write_text(SECRET, encoding="utf-8")
            keychain, manager = Keychain(), VaultKeyManager()
            stack.enter_context(patch("macos_vault_plugin.plugin.SystemKeychain", return_value=keychain))
            stack.enter_context(patch("macos_vault_plugin.key_manager.get_keychain", return_value=keychain))
            stack.enter_context(patch("macos_vault_plugin.key_manager.get_backend_name", return_value="owned memory Keychain"))
            stack.enter_context(patch("macos_vault_plugin.plugin.get_key_manager", return_value=manager))
            harness = Harness(fixture, keychain)
            try:
                asyncio.run(parked_admission(harness))
                declarations()
                remaining_parked_leaves(harness, fixture.app_home, manager)
                credential_lifecycle(harness, fixture.app_home)
                crypto_and_admin(harness, fixture.app_home, manager)
                oauth_lifecycle(harness)
                failure_confinement(harness)
                notification_failure(harness)
                worker_loss(harness)
                no_secret_persistence(harness, captured)
            finally:
                keychain.release.set()
                harness.join()
    finally:
        logging.getLogger().removeHandler(handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
