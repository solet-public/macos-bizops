"""Owned, synthetic connector inputs for the NM-2 fast-return census.

This module follows the preparer discovery contract in NM-2 design section 4.2.
The real census owns the plugin instances, services, audit, and transport guard.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol, cast

PREPARER_NAME = "connectors-v1"

# Exactly the Appendix A U3 allocation. Other verbs in these plugins retain
# their U1/U4 owner and pass through parameters() unchanged.
VERBS: dict[str, frozenset[str]] = {
    "marketo_plugin": frozenset(
        {
            "create_asset_file",
            "create_asset_folder",
            "describe_companies",
            "describe_custom_objects",
            "describe_lead_fields",
            "describe_named_accounts",
            "describe_opportunities",
            "describe_opportunity_roles",
            "describe_sales_persons",
            "get_activities",
            "get_asset_file",
            "get_asset_file_by_name",
            "get_asset_folder",
            "get_asset_folder_by_name",
            "get_asset_folder_content",
            "get_asset_tokens",
            "get_companies",
            "get_custom_objects",
            "get_leads",
            "get_named_account_list_members",
            "get_named_account_lists",
            "get_named_accounts",
            "get_opportunities",
            "get_opportunity_roles",
            "get_sales_persons",
            "import_custom_objects",
            "import_leads",
            "import_program_members",
            "list_activity_types",
            "list_asset_files",
            "list_asset_folders",
            "list_campaigns",
            "list_custom_object_types",
            "list_static_lists",
            "set_asset_token",
            "update_asset_file_content",
            "check_marketo_job_status",
        }
    ),
    "snowflake_plugin": frozenset({"dynamic_table", "pipe", "schema", "sequence", "sql_api", "stage", "stage_files", "stream", "table", "task", "view"}),
    "macos_self_deployment_plugin": frozenset({"attest_runtime_code", "cutover_release", "restart_with_manifest", "rollback_release", "swap_rollback", "swap_status"}),
    "s3_blob_storage_plugin": frozenset({"delete_blob", "file_command", "get_blob", "get_blob_metadata", "search_blobs", "store_blob", "store_blob_from_file", "update_blob_metadata"}),
    "salesforce_plugin": frozenset({"bulk_jobs", "composite", "content_files", "metadata", "tooling"}),
    "seed_factory_plugin": frozenset({"assemble_seed", "publish_seed", "publish_release", "release_run_status", "reproduce_release", "validate_and_seal_seed_bundle"}),
    "aws_self_deployment_plugin": frozenset({"complete_deploy", "deploy_rollback", "deploy_self", "deploy_status", "restart_with_manifest", "stop_self"}),
    "external_app_client_plugin": frozenset({"issue_list", "issue_show", "repository_list", "unit_who_has_work"}),
    "fleet_maintenance_plugin": frozenset({"maintenance_duty_run", "maintenance_duty_status", "session_cron_observation"}),
    "jira_plugin": frozenset(
        {"add_attachment", "download_attachment", "oauth", "sites", "add_comment", "agile_issues", "backlog", "boards", "create_issue", "delete_issue", "get_issue", "jql_search", "list_comments", "list_transitions", "projects", "sprints", "test_connection", "transition_issue", "update_issue", "versions", "worklogs"}
    ),
    "aws_undertaker_plugin": frozenset({"delete_rds_subnet_group", "delete_snapshot", "teardown_solet"}),
    "schwab_market_data_plugin": frozenset({"get_options_chain", "start_interface", "start_oauth_bootstrap"}),
    "soundcloud_artist_studio_plugin": frozenset({"list_artists", "set_playlist_sharing", "set_track_sharing"}),
    "aws_account_admin_plugin": frozenset({"audit_lifecycle_roles", "provision_lifecycle_roles"}),
    "aws_midwife_plugin": frozenset({"birth_solet", "build_and_push"}),
    "external_postgres_plugin": frozenset({"list_connections"}),
    "github_midwife_plugin": frozenset({"birth_solet"}),
    "macos_midwife_plugin": frozenset({"birth_solet"}),
    "actr_memory_plugin": frozenset({"import_memories_action"}),
    "default_blob_storage_plugin": frozenset({"store_blob_from_file"}),
    "default_address_book_plugin": frozenset({"add_entry", "delete", "delete_entry", "update_entry"}),
    "titanv2_embeddings_plugin": frozenset({"generate_embeddings_action", "get_embedding_dimension_action", "list_models_action"}),
    "claude_code_filesystem_session_source_plugin": frozenset({"qualify"}),
    "codex_filesystem_session_source_plugin": frozenset({"qualify"}),
    "claude_code_cloud_session_source_plugin": frozenset({"backfill_status"}),
    "codex_cloud_session_source_plugin": frozenset({"backfill_status"}),
}
PLUGINS = frozenset(VERBS)
_MARKETO_K2 = frozenset({"create_asset_file", "create_asset_folder", "import_custom_objects", "import_leads", "import_program_members", "set_asset_token", "update_asset_file_content", "check_marketo_job_status"})
MARKETO_EXPORT_VERBS = VERBS["marketo_plugin"] - _MARKETO_K2
MEASURE_EXCLUDED = frozenset({("macos_midwife_plugin", "birth_solet")})
EXCLUSION_REASONS = {
    ("macos_midwife_plugin", "birth_solet"): "host installer and clone side effects cannot be contained by the census transport guard",
}
RUN_THREADS: frozenset[str] = frozenset()
LOOPBACK_LISTENER = False
RESIDUAL = {
    ("s3_blob_storage_plugin", "store_blob_from_file"): "s3transfer workers are parked by exact-name fixture admission while upload_file synchronously waits for their future; executor-scoped admission and idle-executor teardown remain unmeasured",
}


def prove(root: Path, plugin_class: type[object], function_name: str) -> dict[str, object]:
    """Retain real handler reachability and the installed SDK's missing premise."""
    from concurrent.futures import ThreadPoolExecutor

    from boto3.s3.transfer import HAS_CRT
    from s3transfer.futures import BoundedExecutor

    from . import residual

    return residual.prove(root, plugin_class, function_name) | {
        "fixture_scheduling": {
            "default_threadpool": BoundedExecutor.EXECUTOR_CLS is ThreadPoolExecutor,
            "crt_available": HAS_CRT,
            "admission": "exact thread names for the plugin context; no executor ownership or per-verb selector",
            "qualification": "handler not invoked; no future wait, SDK monkeypatch, provider replacement or real network",
        }
    }


_SYNTHETIC = "census-synthetic-placeholder"
_ENDPOINT = "https://census.invalid"


class CensusContextView(Protocol):
    app_home: Path
    export_root: Path

    def get_service(self, name: str) -> object | None: ...

    def store_blob(self, namespace: str, filename: str, content: bytes, mime_type: str) -> str: ...


def configure(plugin: str, home: Path, export_root: Path) -> dict[str, object]:
    """Supply only synthetic endpoints and an owned export directory."""
    if plugin not in PLUGINS:
        return {}
    if export_root.resolve().is_relative_to(home.resolve()):
        raise ValueError("export root must be outside the application home")
    config: dict[str, object] = {"export_allowed_roots": [str(export_root)], "base_url": _ENDPOINT}
    config.update(_configuration_details(plugin, home))
    return config


def _configuration_details(plugin: str, home: Path) -> dict[str, object]:
    config: dict[str, object] = {}
    if plugin == "marketo_plugin":
        config.update(client_id=_SYNTHETIC, client_secret=_SYNTHETIC)
    elif plugin == "snowflake_plugin":
        config.update(account="CENSUS", user="CENSUS", warehouse="CENSUS", database="CENSUS", schema="PUBLIC", host="census.invalid")
    elif plugin == "s3_blob_storage_plugin":
        config.update(region="us-east-1", endpoint_url=_ENDPOINT, bucket="census-synthetic", solet_name="census", kms_key_id="alias/census-synthetic", cache_dir=str(home / "s3-cache"))
    elif plugin == "aws_self_deployment_plugin":
        config.update(solet_name="census", region="us-east-1", cluster="census-synthetic", shared_alb_arn="arn:aws:elasticloadbalancing:us-east-1:000000000000:loadbalancer/app/census/0000000000000000", shared_vpc="vpc-census")
    elif plugin == "aws_undertaker_plugin":
        config.update(region="us-east-1", account_id="000000000000", hosted_zone_id="ZCENSUS", shared_alb_arn="arn:aws:elasticloadbalancing:us-east-1:000000000000:loadbalancer/app/census/0000000000000000")
    elif plugin in {"aws_midwife_plugin", "aws_account_admin_plugin"}:
        config.update(region="us-east-1", endpoint_url=_ENDPOINT)
    elif plugin in {"jira_plugin", "salesforce_plugin", "schwab_market_data_plugin", "soundcloud_artist_studio_plugin"}:
        config.update(api_url=_ENDPOINT, client_id=_SYNTHETIC, client_secret=_SYNTHETIC)
    elif plugin == "external_app_client_plugin":
        config["instances"] = {"census": {"executable_root": str(home / "external-app"), "keychain_service": "census-synthetic", "keychain_account": "census-synthetic", "deployment_name": "census", "enabled_commands": sorted(VERBS[plugin])}}
    return config


def seed(plugin: str, context: CensusContextView) -> dict[str, str]:
    """Create local inputs only; never create a publishable bundle or call a vendor."""
    if plugin not in PLUGINS:
        return {}
    seeded = {"export_root": str(context.export_root)}
    if plugin == "marketo_plugin" and not _register_address(
        context,
        "census_export_root",
        {"path": str(context.export_root)},
        address_type="path",
    ):
        seeded["export_root_address_seed"] = "unavailable; retain the named prerequisite"
    address = _ADDRESSES.get(plugin)
    if address is not None and not _register_address(context, *address):
        seeded["address_book_seed"] = "unavailable; retain the named prerequisite"
    if plugin in {"aws_midwife_plugin", "github_midwife_plugin", "macos_midwife_plugin"}:
        target = context.app_home / "census-owned-clone"
        target.mkdir(exist_ok=True)
        seeded["clone_target"] = str(target)
    _seed_owned_inputs(plugin, context, seeded)
    return seeded


def _seed_owned_inputs(plugin: str, context: CensusContextView, seeded: dict[str, str]) -> None:
    if plugin.startswith("aws_") or plugin in {"s3_blob_storage_plugin", "titanv2_embeddings_plugin"}:
        _seed_aws(context)
    if plugin == "jira_plugin":
        _seed_jira(context)
    _seed_connector_files(plugin, context, seeded)
    if plugin in {"marketo_plugin", "claude_code_cloud_session_source_plugin", "codex_cloud_session_source_plugin"}:
        _seed_status_job(plugin, context, seeded)
    if plugin in {"claude_code_filesystem_session_source_plugin", "codex_filesystem_session_source_plugin"}:
        _seed_source(plugin, context)
    if plugin == "schwab_market_data_plugin":
        _seed_schwab(context)
    if plugin == "seed_factory_plugin":
        _seed_release_status(context, seeded)


def _seed_connector_files(plugin: str, context: CensusContextView, seeded: dict[str, str]) -> None:
    if plugin == "actr_memory_plugin":
        path = context.export_root / "memories.json"
        path.write_text(json.dumps({"memories": []}), encoding="utf-8")
        seeded["import_path"] = str(path)
    if plugin == "s3_blob_storage_plugin":
        path = context.export_root / "census.txt"
        path.write_bytes(b"census synthetic fixture\n")
        seeded["file_path"] = str(path)
        _seed_s3_metadata(context, seeded)
    if plugin == "external_app_client_plugin":
        # Existence is a dispatch prerequisite. The guarded keychain subprocess
        # is reached first; this sentinel is never a substitute CLI result.
        executable = context.app_home / "external-app" / ".venv" / "bin" / "psolet"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"#!/bin/sh\nexit 97\n")
        executable.chmod(0o700)
        seeded["external_executable"] = str(executable)


def _seed_s3_metadata(context: CensusContextView, seeded: dict[str, str]) -> None:
    state = context.get_service("state_service")
    if state is None:
        return
    service = cast(Any, state)
    for verb in ("delete_blob", "get_blob", "get_blob_metadata", "update_blob_metadata"):
        record = service.prepare_record("s3_blob_storage_plugin", "metadata", {"plugin_namespace": "census", "name": f"census-{verb}", "filename": f"census-{verb}.txt", "mime_type": "text/plain", "size": 25, "s3_key": f"census/census/{verb}"})
        record["blob_id"] = record["id"]
        result = service.write_state("s3_blob_storage_plugin", {"table": "metadata", "record": record})
        if result.get("action_status") != "completed":
            raise RuntimeError(f"synthetic S3 metadata seed failed: {result}")
        seeded[f"{verb}_blob_id"] = str(record["id"])


def _seed_aws(context: CensusContextView) -> None:
    """Use the SDK's supported shared files, entirely inside the owned home."""
    import boto3

    directory = context.app_home.parent.parent / ".aws"
    directory.mkdir(exist_ok=True)
    (directory / "credentials").write_text("[default]\naws_access_key_id = census-synthetic-access\naws_secret_access_key = census-synthetic-secret\n", encoding="utf-8")
    (directory / "config").write_text(f"[default]\nregion = us-east-1\nendpoint_url = {_ENDPOINT}\n", encoding="utf-8")
    boto3.setup_default_session()


def _seed_jira(context: CensusContextView) -> None:
    from jira_plugin.sites import INDEX_KEY, SiteStore

    vault = context.get_service("vault_service")
    if vault is None:
        return
    key = SiteStore.key("census") + "__synthetic"
    value = {"base_url": _ENDPOINT, "auth_type": "api_token", "email": "census@example.invalid", "api_token": _SYNTHETIC}
    index = {"version": 1, "default": "census", "sites": {"census": {"base_url": _ENDPOINT, "auth_type": "api_token", "credential_key": key}}, "history": [], "retired": {}}
    for name, payload in ((key, value), (INDEX_KEY, index)):
        result = cast(Any, vault).store(name, json.dumps(payload), tags=["jira_site"], metadata={})
        if result.get("action_status") != "completed":
            raise RuntimeError(f"synthetic Jira vault seed failed: {name}")


def _seed_status_job(plugin: str, context: CensusContextView, seeded: dict[str, str]) -> None:
    manager = context.get_service("async_job_manager")
    if manager is None:
        return
    action = "get_leads" if plugin == "marketo_plugin" else "backfill_from_cloud"
    result = cast(Any, manager).create_job(plugin_name=plugin, action_name=action, request_data={"notes": "owned synthetic status fixture", "verb": action})
    if result.get("action_status") != "completed":
        raise RuntimeError(f"synthetic status seed failed: {result}")
    seeded["status_job_id"] = str(result["data"]["job_id"])
    if plugin != "marketo_plugin":
        _complete_status_job(cast(Any, manager), plugin, seeded["status_job_id"])


def _complete_status_job(manager: Any, plugin: str, job_id: str) -> None:
    result = manager.update_job(job_id, {"status": "completed", "result": {"events_persisted": 0}})
    if result.get("action_status") != "completed" and result.get("data", {}).get("outcome_committed") is not True:
        raise RuntimeError(f"synthetic cloud status update failed: {result}")
    readback = manager.get_job(job_id)
    job = readback.get("data", {}).get("job", {})
    payload = manager.get_job_payload(job_id, "result")
    if readback.get("action_status") != "completed" or job.get("status") != "completed" or job.get("provider_name") != f"{plugin}.backfill_from_cloud" or payload.get("data", {}).get("payload") != {"events_persisted": 0}:
        raise RuntimeError(f"synthetic cloud status readback disagrees: {readback}, {payload}")
    if result.get("action_status") != "completed":
        print(f"ADVISORY cloud fixture outcome committed and read back; continuation delivery unmeasured: {result['error']}")


def _seed_source(plugin: str, context: CensusContextView) -> None:
    from ananta.core.services.call_context import CallContext

    ledger = context.get_service("session_ledger_service")
    if ledger is not None:
        kind = "codex_local" if plugin.startswith("codex_") else "claude_code_local"
        # The production qualifier explicitly reports an unpopulated source.
        # Keep that safe path visible; it does not claim content ingestion.
        cast(Any, ledger).register_source(kind, str(context.app_home / "unpopulated-source"), call_context=CallContext.for_operator())


def _seed_schwab(context: CensusContextView) -> None:
    from schwab_market_data_plugin.app_config import AppConfigLoader
    from schwab_market_data_plugin.token_store import TokenStore

    vault = context.get_service("vault_service")
    address = context.get_service("address_book_service")
    if vault is not None and address is not None:
        TokenStore(vault, cast(Any, AppConfigLoader)(address)).store_initial_tokens(_SYNTHETIC, _SYNTHETIC, 3600)


def _seed_release_status(context: CensusContextView, seeded: dict[str, str]) -> None:
    state = context.get_service("state_service")
    if state is None:
        return
    from seed_factory_plugin import release_run

    request = release_run.ReleaseRunRequest(source_ref="0" * 40, seed_target=release_run.SeedTarget("census", "synthetic", "private"), manager_target=release_run.RepoTarget("census", "synthetic"), bundle_name="census-synthetic")
    run = release_run.ReleaseRun(run_id=release_run.new_run_id(), request=request, state="refused", refusal={"code": "census_fixture", "message": "Synthetic terminal status; no release stage ran"}, created_at=release_run.now_iso(), finished_at=release_run.now_iso())
    release_run.save_run(cast(Any, state), run)
    seeded["release_run_id"] = run.run_id


def _register_address(context: CensusContextView, name: str, fields: Mapping[str, str], *, address_type: str = "service") -> bool:
    address_book = context.get_service("address_book_service")
    if address_book is None:
        return False
    result = cast(Any, address_book).register(
        name=name,
        address_type=address_type,
        description="Synthetic census instance",
        entries=[{"field_type": field, "description": "Synthetic census value", "value": value} for field, value in fields.items()],
    )
    return isinstance(result, dict) and result.get("action_status") == "completed"


_ROLE_ARN = "arn:aws:iam::000000000000:role/census-synthetic"
_ADDRESSES: dict[str, tuple[str, Mapping[str, str]]] = {
    "marketo_plugin": ("marketo_instance", {"base_url": _ENDPOINT, "client_id": _SYNTHETIC, "client_secret": _SYNTHETIC}),
    "jira_plugin": ("jira_site", {"base_url": _ENDPOINT, "email": "census@example.invalid", "api_token": _SYNTHETIC, "expires_at": "2099-01-01T00:00:00+00:00"}),
    "aws_account_admin_plugin": ("aws_admin_role", {"role_arn": _ROLE_ARN, "region": "us-east-1"}),
    "aws_midwife_plugin": ("aws_midwife_role", {"role_arn": _ROLE_ARN, "region": "us-east-1"}),
    "aws_self_deployment_plugin": ("aws_self_deployment_role", {"role_arn": _ROLE_ARN, "region": "us-east-1"}),
    "aws_undertaker_plugin": ("aws_undertaker_role", {"role_arn": _ROLE_ARN, "region": "us-east-1"}),
    "titanv2_embeddings_plugin": ("titanv2_embeddings", {"region": "us-east-1", "model": "amazon.titan-embed-text-v2:0", "dimensions": "1024"}),
    "schwab_market_data_plugin": ("schwab_oauth_app", {"client_id": _SYNTHETIC, "client_secret": _SYNTHETIC, "redirect_uri": "https://census.invalid/oauth/schwab/callback"}),
}


def parameters(plugin: str, verb: str, params: dict[str, object], seeded: Mapping[str, str], home: Path) -> dict[str, object]:
    """Fill the U3 rows through the public preparer hook, before timing."""
    result = dict(params)
    if verb not in VERBS.get(plugin, frozenset()):
        return result
    result.setdefault("notes", "synthetic census observation")
    handler = _PARAMETER_HANDLERS.get(plugin)
    if handler is not None:
        handler(verb, result, seeded, home)
    return result


def _marketo_parameters(verb: str, result: dict[str, object], seeded: Mapping[str, str], home: Path) -> None:
    if verb in MARKETO_EXPORT_VERBS:
        root = Path(seeded.get("export_root", home.parent / "export"))
        result["output_tsv_path"] = str(root / f"{verb}.tsv")
    if verb in {"create_asset_file", "create_asset_folder"}:
        result.update(id=1, folder={"id": 1, "type": "Folder"}, parent={"id": 1, "type": "Folder"})
    if verb.startswith("import_") or verb in {"update_asset_file_content", "create_asset_file"}:
        result["file"] = {"filename": "census.csv", "content_type": "text/csv", "content_base64": "YQ=="}
    if verb == "check_marketo_job_status":
        result["job_id"] = seeded.get("status_job_id", "owned-job-not-provisioned")
    if verb == "set_asset_token":
        result["token_type"] = "text"
    if verb in {"get_custom_objects", "get_opportunity_roles"}:
        result.update(filter_type="id", filter_values=["0"])


def _snowflake_parameters(verb: str, result: dict[str, object], seeded: Mapping[str, str], home: Path) -> None:
    del seeded, home
    result.update(database="CENSUS", schema="PUBLIC", name="CENSUS", warehouse="CENSUS")
    if verb == "sql_api":
        result["body"] = {"statement": "SELECT 1"}
    if verb == "stage_files":
        result.update(operation="list", stage_location="@CENSUS.PUBLIC.STAGE")


def _midwife_parameters(verb: str, result: dict[str, object], seeded: Mapping[str, str], home: Path) -> None:
    del verb
    result.update(name="census-synthetic", clone_target=seeded.get("clone_target", str(home / "census-owned-clone")), profile_template="local")
    result["environment_config"] = {"target": result["clone_target"]}


def _aws_midwife_parameters(verb: str, result: dict[str, object], seeded: Mapping[str, str], home: Path) -> None:
    _midwife_parameters(verb, result, seeded, home)
    result["environment_config"] = {
        "target": result["clone_target"],
        "dns_root_domain": "census.invalid",
        "hosted_zone_id": "ZCENSUS",
        "alb_listener_arn": "arn:aws:elasticloadbalancing:us-east-1:000000000000:listener/app/census/0000000000000000/0000000000000000",
        "alb_dns_name": "alb.census.invalid",
        "alb_hosted_zone_id": "ZCENSUS",
        "shared_vpc": "vpc-census",
        "subnets": ["subnet-census"],
        "security_groups": ["sg-census"],
        "private_endpoint_sg": "sg-census",
        "image_uri": "registry.census.invalid/synthetic:fixture",
    }


def _seed_factory_parameters(verb: str, result: dict[str, object], seeded: Mapping[str, str], home: Path) -> None:
    values: dict[str, dict[str, object]] = {
        "assemble_seed": {"bundle_name": "census-synthetic"},
        "reproduce_release": {"manager_target": {"owner": "census", "repo": "synthetic"}, "release_tag": "census-synthetic"},
        "validate_and_seal_seed_bundle": {"output_dir": str(home / "synthetic-unsealed")},
        # No bundle is created: the user gate forbids publish preparation.
        # The census guards subprocess execution; missing seal remains named.
        "publish_seed": {"repo_path": str(home / "synthetic-unsealed"), "sealed_commit_sha": "0" * 40, "repo_name": "census-synthetic", "visibility": "private"},
        "publish_release": {"seed_target": {"owner": "census", "repo": "synthetic", "visibility": "private"}, "manager_target": {"owner": "census", "repo": "synthetic"}, "source_ref": "0" * 40, "bundle_name": "census-synthetic"},
        "release_run_status": {"run_id": seeded.get("release_run_id", "rrun_" + "0" * 32)},
    }
    result.update(values[verb])


def _other_parameters(verb: str, result: dict[str, object], seeded: Mapping[str, str], home: Path) -> None:
    del seeded, home
    result.pop("body", None)
    result.pop("query", None)
    values: dict[str, dict[str, object]] = {
        "bulk_jobs": {"operation": "query_list"},
        "composite": {"operation": "composite", "body": {"compositeRequest": [{"method": "GET", "url": "/services/data/v62.0/limits", "referenceId": "census"}]}},
        "tooling": {"operation": "query", "query": {"q": "SELECT Id FROM ApexClass LIMIT 1"}},
        "metadata": {"operation": "deploy_status", "deploy_request_id": "0Af000000000001AAA"},
        "content_files": {"operation": "delete_document", "content_document_id": "069000000000001AAA"},
    }
    result.update(values[verb])


def _jira_parameters(verb: str, result: dict[str, object], seeded: Mapping[str, str], home: Path) -> None:
    del seeded, home
    result["site"] = "census"
    if verb == "sites":
        result["operation"] = "list"
    if verb == "oauth":
        result["operation"] = "cancel"


def _memory_parameters(verb: str, result: dict[str, object], seeded: Mapping[str, str], home: Path) -> None:
    del verb
    result["file_path"] = seeded.get("import_path", str(home / "not-provisioned.json"))


def _status_parameters(verb: str, result: dict[str, object], seeded: Mapping[str, str], home: Path) -> None:
    del verb, home
    result["job_id"] = seeded.get("status_job_id", "owned-job-not-provisioned")


def _s3_parameters(verb: str, result: dict[str, object], seeded: Mapping[str, str], home: Path) -> None:
    result.update(namespace="census", command="ls", blob_id=seeded.get(f"{verb}_blob_id", "bmd-0000000000000"))
    result["file_path"] = seeded.get("file_path", str(home / "census.txt"))
    if verb == "get_blob":
        result["name"] = result["blob_id"]


def _external_parameters(verb: str, result: dict[str, object], seeded: Mapping[str, str], home: Path) -> None:
    del seeded, home
    result.clear()
    result["instance"] = "census"
    if verb == "issue_show":
        result["issue"] = "iss_00000000-0000-0000-0000-000000000000"


def _fixed_parameters(values: dict[str, object]) -> Callable[[str, dict[str, object], Mapping[str, str], Path], None]:
    def apply(verb: str, result: dict[str, object], seeded: Mapping[str, str], home: Path) -> None:
        del verb, seeded, home
        result.update(values)

    return apply


_PARAMETER_HANDLERS: dict[str, Callable[[str, dict[str, object], Mapping[str, str], Path], None]] = {
    "marketo_plugin": _marketo_parameters,
    "snowflake_plugin": _snowflake_parameters,
    "salesforce_plugin": _other_parameters,
    "jira_plugin": _jira_parameters,
    "actr_memory_plugin": _memory_parameters,
    "claude_code_cloud_session_source_plugin": _status_parameters,
    "codex_cloud_session_source_plugin": _status_parameters,
    "external_app_client_plugin": _external_parameters,
    "s3_blob_storage_plugin": _s3_parameters,
    "default_blob_storage_plugin": _fixed_parameters({"filename": "census-upload.txt", "mime_type": "text/plain"}),
    "fleet_maintenance_plugin": _fixed_parameters({"operation": "read", "duty_name": "census-synthetic"}),
    "soundcloud_artist_studio_plugin": _fixed_parameters({"sharing": "private"}),
    "aws_account_admin_plugin": _fixed_parameters({"role_name": "CensusSyntheticRole"}),
    "aws_midwife_plugin": _aws_midwife_parameters,
    "github_midwife_plugin": _midwife_parameters,
    "macos_midwife_plugin": _midwife_parameters,
    "seed_factory_plugin": _seed_factory_parameters,
    "macos_self_deployment_plugin": _fixed_parameters({"reconciliation_id": "rec_census", "verification_modules": ["quality_gates.tests.fast_return_census_smoke"]}),
}
