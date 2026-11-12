"""Standalone contracts and provisioned regressions for connector fixtures.

quality_gates/tests/fast_return_census_connectors_smoke.py

The pure legs verify the owned external-vault lifecycle, input population,
synthetic values and export admission. Directory descriptors may traverse
ancestors of owned roots; host file reads and directory listings stay denied.
Those checks run without PostgreSQL and never open a real credential store.
First-call configuration and explicit MIME resolution run under the real
setup audit before any other fixture; the MIME database must stay uninitialized.
The external-app executable is an owned fail-closed sentinel used only for
path admission; the real caller reaches the guarded keychain subprocess
before its content could execute. External CLI results remain unmeasured.
S3 multipart upload retains a static scheduling remainder: its SDK transfer
future would wait for parked executor threads. No generic executor is admitted.

The optional real leg invokes the public census with discovered preparers.
It uses the provisioner's real state, address book, jobs and memory services.
Workers stay parked, so persisted enqueue proves dispatch, not vendor work.
Every newly supplied prerequisite has a representative classification check.
Known plugin-side defects remain named rows rather than becoming fake successes.

Run with quality_gates/provision_test_pg.py --exec for real-context coverage.
Without that private fixture, the pure contracts run and the real leg prints
its explicit skip. CENSUS_CONNECTOR_REPORT optionally retains the full report
outside the disposable homes, including causes and timing for slow handlers.
Imports bind this tree before installing the audit: editable installations may
otherwise expose another checkout while importing guarded dependencies.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import pkgutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(Path(__file__).resolve().parent), str(ROOT), str(ROOT / "ananta/src"), str(ROOT / "solet_setup_contracts/src"), *(str(path) for path in sorted(ROOT.glob("plugins/*/src")))]


COUNTS = {
    "marketo_plugin": 37,
    "snowflake_plugin": 11,
    "macos_self_deployment_plugin": 6,
    "s3_blob_storage_plugin": 8,
    "salesforce_plugin": 5,
    "seed_factory_plugin": 6,
    "aws_self_deployment_plugin": 6,
    "external_app_client_plugin": 4,
    "fleet_maintenance_plugin": 3,
    "jira_plugin": 21,
    "aws_undertaker_plugin": 3,
    "schwab_market_data_plugin": 3,
    "soundcloud_artist_studio_plugin": 3,
    "aws_account_admin_plugin": 2,
    "aws_midwife_plugin": 2,
    "external_postgres_plugin": 1,
    "github_midwife_plugin": 1,
    "macos_midwife_plugin": 1,
    "actr_memory_plugin": 1,
    "default_blob_storage_plugin": 1,
    "default_address_book_plugin": 4,
    "titanv2_embeddings_plugin": 3,
    "claude_code_filesystem_session_source_plugin": 1,
    "codex_filesystem_session_source_plugin": 1,
    "claude_code_cloud_session_source_plugin": 1,
    "codex_cloud_session_source_plugin": 1,
}
_SKIPPED_OPTIONAL_LEGS: set[str] = set()


def _module_or_skip(name: str) -> Any | None:
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name.split(".")[0]:
            raise
        if name not in _SKIPPED_OPTIONAL_LEGS:
            print(f"SKIP {name} contract leg: plugin absent from this seed")
            _SKIPPED_OPTIONAL_LEGS.add(name)
        return None


class _AddressBook:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def register(self, **kwargs: Any) -> dict[str, str]:
        self.entries.append(kwargs)
        return {"action_status": "completed"}

    def list_path_addresses(self) -> dict[str, object]:
        return {
            "action_status": "completed",
            "data": {"addresses": [{**entry, "tags": [], "updated_at": "2026-11-09T00:00:00+00:00"} for entry in self.entries if entry["address_type"] == "path"]},
        }

    def resolve(self, name: str) -> dict[str, Any]:
        entry = next(item for item in self.entries if item["name"] == name)
        return {"action_status": "completed", "data": {**entry, "entries": [{**part, "id": f"census-entry-{index}"} for index, part in enumerate(entry["entries"])]}}


class _Context:
    def __init__(self, home: Path, export_root: Path) -> None:
        self.app_home = home
        self.export_root = export_root
        self.address_book = _AddressBook()

    def get_service(self, name: str) -> object | None:
        return self.address_book if name == "address_book_service" else None

    def store_blob(self, namespace: str, filename: str, content: bytes, mime_type: str) -> str:
        raise AssertionError("connector preparer must not seed a blob")


def _values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for nested in value.values() for item in _values(nested)]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [item for nested in value for item in _values(nested)]
    return []


def _safe(value: object) -> bool:
    for item in _values(value):
        if "://" in item and not (".invalid" in item or item.startswith("http://127.0.0.1:0")):
            return False
        if any(token in item for token in ("AKIA", "ghp_", "sk-", "-----BEGIN", "@gmail.com")):
            return False
    return True


def _compared_literals(source: ast.AST, variable: str) -> frozenset[str]:
    return frozenset(
        comparator.value
        for node in ast.walk(source)
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == variable and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq)
        for comparator in node.comparators
        if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str)
    )


def _marketo_choices(verb: str, params: dict[str, object]) -> None:
    asset = _module_or_skip("marketo_plugin.asset_api")
    if asset is None:
        return
    if verb == "set_asset_token":
        assert params["token_type"] in asset.TOKEN_TYPES
    folder = params.get("folder")
    if isinstance(folder, dict):
        assert folder["type"] in asset.FOLDER_TYPES


def _snowflake_choices(verb: str, params: dict[str, object]) -> None:
    if verb == "stage_files":
        stage = _module_or_skip("snowflake_plugin.stage_sql")
        if stage is not None:
            assert params["operation"] in stage.OPERATIONS


def _seed_choices(verb: str, params: dict[str, object]) -> None:
    if verb == "publish_seed":
        publish = _module_or_skip("seed_factory_plugin.publish")
        if publish is not None:
            assert params["visibility"] in publish._VALID_VISIBILITIES


def _soundcloud_choices(verb: str, params: dict[str, object]) -> None:
    del verb
    constants = _module_or_skip("soundcloud_artist_studio_plugin.constants")
    if constants is not None:
        assert params["sharing"] in constants.SHARING_VALUES


def _fleet_choices(verb: str, params: dict[str, object]) -> None:
    if verb != "session_cron_observation":
        return
    source = Path(__file__).resolve().parents[2] / "plugins/fleet_maintenance_plugin/src/fleet_maintenance_plugin/plugin.py"
    if not source.is_file():
        print("SKIP fleet_maintenance_plugin enum leg: source absent from this seed")
        return
    tree = ast.parse(source.read_text())
    method = next(node for cls in ast.walk(tree) if isinstance(cls, ast.ClassDef) and cls.name == "FleetMaintenancePlugin" for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "session_cron_observation")
    assert params["operation"] in _compared_literals(method, "operation")


def _s3_choices(verb: str, params: dict[str, object]) -> None:
    if verb != "file_command":
        return
    file_commands = _module_or_skip("s3_blob_storage_plugin.file_commands")
    if file_commands is None:
        return
    source = ast.parse(Path(file_commands.__file__).read_text())
    function = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "execute_file_command")
    assert params["command"] in _compared_literals(function, "cmd")


_CHOICE_CHECKS = {
    "marketo_plugin": _marketo_choices,
    "snowflake_plugin": _snowflake_choices,
    "seed_factory_plugin": _seed_choices,
    "soundcloud_artist_studio_plugin": _soundcloud_choices,
    "fleet_maintenance_plugin": _fleet_choices,
    "s3_blob_storage_plugin": _s3_choices,
}


def _check_choices(plugin: str, verb: str, params: dict[str, object]) -> None:
    """Derive each supplied choice from the owning plugin's live validator."""
    check = _CHOICE_CHECKS.get(plugin)
    if check is not None:
        check(verb, params)


def _check_population() -> Any:
    package = importlib.import_module("fast_return_census_preparers")
    modules = [importlib.import_module(f"{package.__name__}.{item.name}") for item in pkgutil.iter_modules(package.__path__)]
    module = next(item for item in modules if item.PREPARER_NAME == "connectors-v1")
    assert module.PLUGINS == frozenset(COUNTS)
    assert {name: len(verbs) for name, verbs in module.VERBS.items()} == COUNTS
    assert sum(COUNTS.values()) == 136
    assert len(module.MARKETO_EXPORT_VERBS) == 29
    _check_safety_declarations(module)
    print("PASS connector fixture ownership: 26 plugins, 136 verbs, 29 exports")
    return module


def _check_safety_declarations(module: Any) -> None:
    assert module.MEASURE_EXCLUDED == {("macos_midwife_plugin", "birth_solet")}
    assert module.EXCLUSION_REASONS[("macos_midwife_plugin", "birth_solet")]
    assert set(module.RESIDUAL) == {("s3_blob_storage_plugin", "store_blob_from_file")}


def _check_marketo_export_gate(module: Any, context: _Context, home: Path, export_root: Path) -> None:
    marketo_module = _module_or_skip("marketo_plugin.plugin")
    if marketo_module is None:
        return
    MarketoPlugin = marketo_module.MarketoPlugin
    PathRefusedError = importlib.import_module("ananta.file_locations.containment").PathRefusedError

    consumer = MarketoPlugin()
    consumer.initialize({})
    consumer.orchestrator_ref = _MarketoOrchestrator(home, _AddressBook())
    first_path = str(export_root / f"{sorted(module.MARKETO_EXPORT_VERBS)[0]}.tsv")
    try:
        consumer._export_path_gate(first_path)
    except PathRefusedError:
        pass
    else:
        raise AssertionError("unregistered export root must be refused by the real consumer")
    consumer.orchestrator_ref = _MarketoOrchestrator(home, context.address_book)
    for verb in module.MARKETO_EXPORT_VERBS:
        path = str(export_root / f"{verb}.tsv")
        assert Path(consumer._export_path_gate(path)) == Path(path).resolve(), verb


def _check_plugin(module: Any, plugin: str, context: _Context, home: Path, export_root: Path) -> None:
    from fast_return_census_fixtures import audited_setup

    if _module_or_skip(plugin) is None:
        return
    with audited_setup(ROOT, home.parent.parent):
        config = module.configure(plugin, home, export_root)
    seeded = module.seed(plugin, context)
    assert config["export_allowed_roots"] == [str(export_root)]
    assert _safe(config) and _safe(seeded)
    for verb in sorted(module.VERBS[plugin]):
        params = module.parameters(plugin, verb, {}, seeded, home)
        assert params and _safe(params), (plugin, verb)
        _check_choices(plugin, verb, params)
        if plugin == "marketo_plugin" and verb in module.MARKETO_EXPORT_VERBS:
            assert Path(params["output_tsv_path"]).parent == export_root
    if plugin == "marketo_plugin":
        _check_marketo_export_gate(module, context, home, export_root)


class _MarketoOrchestrator:
    def __init__(self, home: Path, address_book: _AddressBook) -> None:
        self.APP_HOME = str(home)
        self.address_book = address_book
        self.plugin_manager = None

    def get_service(self, name: str) -> object | None:
        return self.address_book if name == "address_book_service" else None


def _check_fixture(module: Any) -> None:
    from fast_return_census_fixtures import smoke_scratch

    with smoke_scratch(ROOT) as scratch:
        home, export_root = scratch / "application" / "profile", scratch / "export"
        home.mkdir(parents=True)
        export_root.mkdir()
        context = _Context(home, export_root)
        with patch.dict("os.environ", {"SOLET_NAME": "census-fixture", "HOME": str(scratch)}, clear=True), patch.object(socket.socket, "connect", side_effect=AssertionError("network egress")) as connect, patch.object(subprocess, "run", side_effect=AssertionError("subprocess")) as run:
            for plugin in sorted(module.PLUGINS):
                _check_plugin(module, plugin, context, home, export_root)
            assert connect.call_count == run.call_count == 0
        expected_addresses = {
            "marketo_plugin": {"census_export_root", "marketo_instance"},
            "jira_plugin": {"jira_site"},
            "aws_account_admin_plugin": {"aws_admin_role"},
            "aws_midwife_plugin": {"aws_midwife_role"},
            "aws_self_deployment_plugin": {"aws_self_deployment_role"},
            "aws_undertaker_plugin": {"aws_undertaker_role"},
            "titanv2_embeddings_plugin": {"titanv2_embeddings"},
            "schwab_market_data_plugin": {"schwab_oauth_app"},
        }
        expected = {name for plugin, names in expected_addresses.items() if plugin not in _SKIPPED_OPTIONAL_LEGS for name in names}
        assert {entry["name"] for entry in context.address_book.entries} == expected
        assert _safe(context.address_book.entries)
        assert all(path.is_relative_to(scratch) for path in scratch.rglob("*"))
        assert not (home / "synthetic-unsealed").exists()
    print("PASS contract paths, synthetic values, guarded boundaries and no publish preparation")


def _check_real_context() -> None:
    """Exercise the discovered preparer and real export gate in the census layout."""
    from quality_gates.private_test_pg import MissingFixtureError, load_fixture

    try:
        load_fixture("fast_return_census")
    except MissingFixtureError:
        print("SKIP connector real-context leg: private PostgreSQL absent; run quality_gates/provision_test_pg.py --exec")
        return
    if _module_or_skip("marketo_plugin.plugin") is None:
        return
    import fast_return_census as census
    from fast_return_census_fixtures import smoke_scratch

    with smoke_scratch(ROOT) as scratch:
        report = census.run_census(ROOT, scratch, census.Settings(), measure=True, only=set(COUNTS))
        _write_real_report(report)
        _check_export_layout(report)
        _check_new_measurements(report)
    print("PASS real-context Marketo export containment and persisted enqueue")


def _check_export_layout(report: dict[str, Any]) -> None:
    plugin = next(item for item in report["plugins"] if item["plugin"] == "marketo_plugin")
    row = next(item for item in plugin["verbs"] if item["verb"] == "get_leads")
    assert row["succeeded"] and row["class"] != "NOT_MEASURABLE", row["missing_piece"]
    assert row["worker_reachable"] and row["job_readback"], "accepted export must reach the real persisted job"
    assert not Path(row["parameters"]["output_tsv_path"]).is_relative_to(Path(row["fixture_app_home"]).parent)
    assert plugin["context_run"]["fixture_removed"] and report["fixture_removed"]


def _write_real_report(report: dict[str, Any]) -> None:
    output = os.environ.get("CENSUS_CONNECTOR_REPORT")
    if output:
        Path(output).write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    for item in report["plugins"]:
        if item["plugin"] in COUNTS:
            print("CONNECTOR_REAL_COUNTS=" + json.dumps({"plugin": item["plugin"], **item["counts"]}, sort_keys=True))


def _check_new_measurements(report: dict[str, Any]) -> None:
    expected = {
        "marketo_plugin": {"create_asset_file", "create_asset_folder", "import_leads", "update_asset_file_content", "check_marketo_job_status", "get_custom_objects", "get_opportunity_roles"},
        "jira_plugin": {"get_issue", "sites", "oauth"},
        "salesforce_plugin": {"bulk_jobs", "composite", "content_files", "metadata", "tooling"},
        "actr_memory_plugin": {"import_memories_action"},
        "default_blob_storage_plugin": {"store_blob_from_file"},
        "default_address_book_plugin": {"add_entry", "delete", "delete_entry", "update_entry"},
        "titanv2_embeddings_plugin": {"get_embedding_dimension_action"},
        "schwab_market_data_plugin": {"get_options_chain", "start_oauth_bootstrap"},
        "soundcloud_artist_studio_plugin": {"list_artists"},
        "claude_code_filesystem_session_source_plugin": {"qualify"},
        "codex_filesystem_session_source_plugin": {"qualify"},
        "claude_code_cloud_session_source_plugin": {"backfill_status"},
        "codex_cloud_session_source_plugin": {"backfill_status"},
        "external_app_client_plugin": {"issue_list", "issue_show", "repository_list", "unit_who_has_work"},
        "s3_blob_storage_plugin": {"delete_blob", "get_blob", "get_blob_metadata", "update_blob_metadata", "store_blob_from_file"},
        "seed_factory_plugin": {"release_run_status", "reproduce_release"},
    }
    measured = {item["plugin"]: {row["verb"]: row for row in item["verbs"]} for item in report["plugins"]}
    for name, verbs in expected.items():
        if name not in measured:
            print(f"SKIP {name} real-context leg: plugin absent from this seed")
            continue
        for verb in verbs:
            row = measured[name][verb]
            _check_measured_row(name, verb, row)


def _check_measured_row(name: str, verb: str, row: dict[str, Any]) -> None:
    assert row["class"] != "NOT_MEASURABLE", (name, verb, row["missing_piece"])
    if (name, verb) == ("default_blob_storage_plugin", "store_blob_from_file"):
        assert row["parameters"]["mime_type"] == "text/plain"
    if (name, verb) == ("s3_blob_storage_plugin", "store_blob_from_file"):
        assert row["class"] == "RESIDUAL" and "workers are parked" in row["residual_reason"]
        scheduling = row["proof"]["fixture_scheduling"]
        assert scheduling["default_threadpool"] and scheduling["crt_available"] is False
    if verb == "backfill_status":
        outcome = ast.literal_eval(row["outcome"])
        assert outcome["data"]["status"] == "completed" and outcome["data"]["execution_measured"] is False


def _check_vault_contract() -> None:
    from ananta.interfaces.vault_service_interface import VaultServiceInterface
    from fast_return_census_context import CensusVault

    vault = CensusVault("census-contract")
    key, renamed = "census-contract.jira_plugin.site", "census-contract.jira_plugin.renamed"
    assert vault.exists(key)["data"] == {"key": key, "exists": False}
    assert vault.retrieve(key)["action_status"] == "error", "missing keys must not manufacture a value"
    assert all(callable(getattr(vault, name, None)) for name in VaultServiceInterface.__abstractmethods__)
    _check_vault_reads(vault, key)
    _check_vault_lifecycle(vault, key, renamed)
    _check_vault_oauth(vault)
    print("PASS owned vault exists/list/retrieve, corrections, rename, deletion and lock lifecycle")


def _check_vault_reads(vault: Any, key: str) -> None:
    assert vault.store(key, "synthetic-before", tags=["census"], metadata={"fixture": "owned"})["data"]["version"] == 1
    assert vault.exists(key)["data"]["exists"]
    assert vault.retrieve(key)["data"]["value"] == "synthetic-before"
    listing = vault.list(tag="census")["data"]
    assert listing["count"] == 1 and listing["secrets"][0]["key"] == key
    assert "synthetic-before" not in json.dumps(listing) and "value" not in listing["secrets"][0]
    listing["secrets"][0]["metadata"]["fixture"] = "mutated copy"
    assert vault.list(tag="census")["data"]["secrets"][0]["metadata"]["fixture"] == "owned"
    assert not vault.list(tag="absent")["data"]["secrets"]


def _check_vault_lifecycle(vault: Any, key: str, renamed: str) -> None:
    cases = (
        ("store", (key, "duplicate"), ("action_status",), "error"),
        ("rotate", (key, "synthetic-after"), ("data", "version"), 2),
        ("retrieve", (key,), ("data", "value"), "synthetic-after"),
        ("rename", (key, renamed), ("action_status",), "completed"),
        ("exists", (key,), ("data", "exists"), False),
        ("exists", (renamed,), ("data", "exists"), True),
        ("delete", (renamed,), ("action_status",), "completed"),
        ("delete", (renamed,), ("action_status",), "error"),
        ("rotate", (renamed, "absent"), ("action_status",), "error"),
        ("store_random", (key, 16), ("data", "byte_length"), 16),
        ("lock", (), ("data", "unlocked"), False),
        ("retrieve", (key,), ("action_status",), "error"),
        ("unlock", ("wrong-passphrase",), ("action_status",), "error"),
        ("unlock", ("census-synthetic-passphrase",), ("data", "unlocked"), True),
        ("status", (), ("data", "initialized"), True),
        ("delete", (key,), ("action_status",), "completed"),
    )
    for method, arguments, path, expected in cases:
        value = getattr(vault, method)(*arguments)
        for part in path:
            value = value[part]
        assert value == expected, (method, value, expected)


def _check_vault_oauth(vault: Any) -> None:
    client = vault.oauth_client_register("synthetic", redirect_uris=["https://census.invalid/callback"])["data"]
    assert client["operator_approved"] and "client_secret" not in json.dumps(vault.oauth_client_list())
    assert vault.oauth_client_add_redirect_uri(client["client_id"], "https://census.invalid/callback")["data"]["added"] is False
    assert vault.oauth_client_add_redirect_uri(client["client_id"], "https://census.invalid/second")["data"]["added"] is True
    assert vault.oauth_client_revoke(client["client_id"])["data"]["removed"] == 1
    assert vault.oauth_client_revoke(client["client_id"])["data"]["removed"] == 0


def _check_directory_traversal() -> None:
    import fast_return_census as census
    from fast_return_census_fixtures import UnsafeAttempt, isolated_boundaries, smoke_scratch

    with smoke_scratch(ROOT) as scratch, isolated_boundaries(ROOT, scratch, census.Probe(census.Settings())) as guard:
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            refused = (
                lambda: os.open("/", os.O_RDONLY),
                lambda: os.open("/etc/passwd", os.O_RDONLY),
                lambda: os.listdir("/"),
                lambda: os.listdir(descriptor),
                lambda: os.scandir(descriptor),
            )
            for operation in refused:
                try:
                    operation()
                except UnsafeAttempt:
                    continue
                raise AssertionError("directory traversal must not permit host content reads or listing")
        finally:
            os.close(descriptor)
        assert any(row["allowed"] and row.get("traversal_only") and row["paths"] == ["/"] for row in guard.files)
        assert len(guard.blocked) == len(refused)
    print("PASS audited ancestor directory traversal; host file reads and path/fd listings refused")


def _check_first_blob_configuration() -> None:
    """The first configure call must not prime MIME state from host files."""
    import mimetypes

    from fast_return_census_context import discover_preparers
    from fast_return_census_fixtures import audited_setup, smoke_scratch

    module = next(item for item in discover_preparers() if item.PREPARER_NAME == "connectors-v1")
    ingestion = _module_or_skip("default_blob_storage_plugin.ingestion")
    assert vars(mimetypes)["_db"] is None, "run this smoke in a fresh interpreter, without MIME cache priming"
    print("COLD_MIME_DB_NONE: first discovered configure runs under audited_setup", flush=True)
    with smoke_scratch(ROOT) as scratch:
        home, exports = scratch / "application" / "profile", scratch / "export"
        home.mkdir(parents=True)
        exports.mkdir()
        with audited_setup(ROOT, scratch) as guard:
            config = module.configure("default_blob_storage_plugin", home, exports)
            params = module.parameters("default_blob_storage_plugin", "store_blob_from_file", {}, {}, home)
            if ingestion is not None:
                assert ingestion.resolve_mime_type(params["mime_type"], params["filename"]) == "text/plain"
        assert config["export_allowed_roots"] == [str(exports)] and params["mime_type"] == "text/plain"
        assert vars(mimetypes)["_db"] is None and not guard.blocked, "explicit MIME must leave host tables unopened"
    print("PASS first audited blob configuration uses explicit MIME without global initialization")


def main() -> None:
    from fast_return_census_fixtures import library_roots

    allowed = (ROOT, Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), *library_roots())
    sys.path[:] = [value for value in sys.path if any(Path(value).resolve().is_relative_to(base) for base in allowed)]
    _check_first_blob_configuration()
    _check_directory_traversal()
    _check_vault_contract()
    _check_fixture(_check_population())
    _check_real_context()


if __name__ == "__main__":
    main()
