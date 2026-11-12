"""Adopter wording and complete base-pinned process-document population."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / relative) for relative in ("plugins/github_midwife_plugin/src", "solet_setup_contracts/src", "ananta/src")]

from github_midwife_plugin.setup_adapter_contract import AdapterRequest  # noqa: E402
from github_midwife_plugin.setup_shell_operations import _rendered_files  # noqa: E402
from solet_setup_contracts.agent_instruction_block import (  # noqa: E402
    AGENT_INSTRUCTION_FILES,
    HYDRATION_BEGIN,
    HYDRATION_END,
    extract_hydration_block,
)

RUNBOOKS = (
    "plugins/github_midwife_plugin/knowledge_base/01_hydration_runbook.md",
    "plugins/github_midwife_plugin/knowledge_base/05_seed_update_runbook.md",
    "plugins/github_midwife_plugin/knowledge_base/hydration_templates/AGENTS.md.template",
    "plugins/github_midwife_plugin/knowledge_base/hydration_templates/CLAUDE.md.template",
)
PROCESS_DOCS = (
    "plugins/external_postgres_plugin/knowledge_base/processes/copy.json",
    "plugins/external_postgres_plugin/knowledge_base/processes/dump_database.json",
    "plugins/external_postgres_plugin/knowledge_base/processes/export_query.json",
    "plugins/external_postgres_plugin/knowledge_base/processes/restore_database.json",
    "plugins/external_postgres_plugin/knowledge_base/processes/run_query.json",
    "plugins/external_postgres_plugin/knowledge_base/processes/run_statement.json",
    "plugins/marketo_plugin/knowledge_base/processes/describe_companies.json",
    "plugins/marketo_plugin/knowledge_base/processes/describe_lead_fields.json",
    "plugins/marketo_plugin/knowledge_base/processes/describe_opportunities.json",
    "plugins/marketo_plugin/knowledge_base/processes/describe_opportunity_roles.json",
    "plugins/marketo_plugin/knowledge_base/processes/describe_sales_persons.json",
    "plugins/marketo_plugin/knowledge_base/processes/get_activities.json",
    "plugins/marketo_plugin/knowledge_base/processes/get_companies.json",
    "plugins/marketo_plugin/knowledge_base/processes/get_custom_objects.json",
    "plugins/marketo_plugin/knowledge_base/processes/get_leads.json",
    "plugins/marketo_plugin/knowledge_base/processes/get_named_account_list_members.json",
    "plugins/marketo_plugin/knowledge_base/processes/get_named_account_lists.json",
    "plugins/marketo_plugin/knowledge_base/processes/get_named_accounts.json",
    "plugins/marketo_plugin/knowledge_base/processes/get_opportunities.json",
    "plugins/marketo_plugin/knowledge_base/processes/get_opportunity_roles.json",
    "plugins/marketo_plugin/knowledge_base/processes/get_sales_persons.json",
    "plugins/marketo_plugin/knowledge_base/processes/list_activity_types.json",
    "plugins/marketo_plugin/knowledge_base/processes/list_campaigns.json",
    "plugins/marketo_plugin/knowledge_base/processes/list_custom_object_types.json",
    "plugins/marketo_plugin/knowledge_base/processes/list_static_lists.json",
    "plugins/salesforce_plugin/knowledge_base/processes/bulk_ingest_submit.json",
    "plugins/salesforce_plugin/knowledge_base/processes/bulk_job_results.json",
    "plugins/salesforce_plugin/knowledge_base/processes/bulk_jobs.json",
    "plugins/salesforce_plugin/knowledge_base/processes/content_files.json",
    "plugins/salesforce_plugin/knowledge_base/processes/export_soql.json",
    "plugins/salesforce_plugin/knowledge_base/processes/metadata.json",
    "plugins/salesforce_plugin/knowledge_base/processes/soql_query.json",
    "plugins/snowflake_plugin/knowledge_base/processes/export_query.json",
    "plugins/snowflake_plugin/knowledge_base/processes/run_query.json",
    "plugins/snowflake_plugin/knowledge_base/processes/run_statement.json",
    "plugins/snowflake_plugin/knowledge_base/processes/stage_files.json",
    "plugins/zuora_plugin/knowledge_base/processes/bulk_export.json",
    "plugins/zuora_plugin/knowledge_base/processes/cancel_data_query_job.json",
    "plugins/zuora_plugin/knowledge_base/processes/create_workflow_auth_provider.json",
    "plugins/zuora_plugin/knowledge_base/processes/data_query.json",
    "plugins/zuora_plugin/knowledge_base/processes/delete_workflow.json",
    "plugins/zuora_plugin/knowledge_base/processes/delete_workflow_auth_provider.json",
    "plugins/zuora_plugin/knowledge_base/processes/delete_workflow_version.json",
    "plugins/zuora_plugin/knowledge_base/processes/download_data_query_result.json",
    "plugins/zuora_plugin/knowledge_base/processes/download_file.json",
    "plugins/zuora_plugin/knowledge_base/processes/export_workflow.json",
    "plugins/zuora_plugin/knowledge_base/processes/get_data_query_job.json",
    "plugins/zuora_plugin/knowledge_base/processes/get_file_status.json",
    "plugins/zuora_plugin/knowledge_base/processes/get_workflow.json",
    "plugins/zuora_plugin/knowledge_base/processes/get_workflow_run.json",
    "plugins/zuora_plugin/knowledge_base/processes/get_workflow_task.json",
    "plugins/zuora_plugin/knowledge_base/processes/get_workflow_usage.json",
    "plugins/zuora_plugin/knowledge_base/processes/import_usage.json",
    "plugins/zuora_plugin/knowledge_base/processes/import_workflow.json",
    "plugins/zuora_plugin/knowledge_base/processes/import_workflow_version.json",
    "plugins/zuora_plugin/knowledge_base/processes/list_data_query_jobs.json",
    "plugins/zuora_plugin/knowledge_base/processes/list_invoices.json",
    "plugins/zuora_plugin/knowledge_base/processes/list_subscriptions.json",
    "plugins/zuora_plugin/knowledge_base/processes/list_workflow_tasks.json",
    "plugins/zuora_plugin/knowledge_base/processes/list_workflow_versions.json",
    "plugins/zuora_plugin/knowledge_base/processes/list_workflows.json",
    "plugins/zuora_plugin/knowledge_base/processes/rerun_workflow_task.json",
    "plugins/zuora_plugin/knowledge_base/processes/restore_file.json",
    "plugins/zuora_plugin/knowledge_base/processes/run_workflow.json",
    "plugins/zuora_plugin/knowledge_base/processes/stop_workflow_run.json",
    "plugins/zuora_plugin/knowledge_base/processes/submit_data_query.json",
    "plugins/zuora_plugin/knowledge_base/processes/update_workflow.json",
    "plugins/zuora_plugin/knowledge_base/processes/update_workflow_auth_provider.json",
    "plugins/zuora_plugin/knowledge_base/processes/update_workflow_tasks.json",
    "plugins/zuora_plugin/knowledge_base/processes/upload_credit_memo_file.json",
    "plugins/zuora_plugin/knowledge_base/processes/upload_custom_object_job_file.json",
    "plugins/zuora_plugin/knowledge_base/processes/upload_debit_memo_file.json",
    "plugins/zuora_plugin/knowledge_base/processes/upload_invoice_file.json",
)


ARTICLES = (
    "ananta/knowledge_bases/ananta_platform/24_operator_communication/09_maintenance_verbs_joseki_cards.md",
    "plugins/external_postgres_plugin/knowledge_base/01_external_postgres_overview.md",
    "plugins/jira_plugin/knowledge_base/01_jira_overview.md",
    "plugins/marketo_plugin/knowledge_base/01_marketo_overview.md",
    "plugins/marketo_plugin/knowledge_base/vendor_api_coverage.md",
    "plugins/salesforce_plugin/knowledge_base/01_salesforce_overview.md",
    "plugins/salesforce_plugin/knowledge_base/hydration_guidance.md",
    "plugins/salesforce_plugin/knowledge_base/vendor_api_coverage.md",
    "plugins/snowflake_plugin/knowledge_base/01_snowflake_overview.md",
    "plugins/snowflake_plugin/knowledge_base/hydration_guidance.md",
    "plugins/snowflake_plugin/knowledge_base/vendor_api_coverage.md",
    "plugins/zuora_plugin/knowledge_base/01_zuora_overview.md",
    "plugins/zuora_plugin/knowledge_base/vendor_api_coverage.md",
)

ADOPTER_TEXT = 'Your solet keeps its own files in a folder in your Documents folder named after it, and large exports go into the `Exports.nosync` folder inside it so iCloud does not copy them. When you start a session from a project folder, files you ask for without naming a place are saved in that project folder. To set a place for a project, say "save `<kind of file>` for `<project>` in `<folder>`", for example "save reports for Acme in ~/Clients/Acme/Reports". To check, ask "where will you save this?". Your solet never saves into its own installation folder; if you start it from there, it saves to its Documents folder and tells you.'
FACTS = ("nothing is moved or deleted", "Files and Folders", "old export folder", "System Settings", "refresh")


def _retired_roots_absent(contents: str, relative: str) -> None:
    for retired in ("export_allowed_roots", "import_allowed_roots", "export_containment"):
        assert retired not in contents, f"{relative}: retired root wording {retired}"


def verify_docs(root: Path, emit: Callable[[str], None] = print) -> tuple[int, int]:
    """Check required shipped guidance and the connector documents this profile has."""
    checked = 0
    missing = 0
    for relative in RUNBOOKS:
        contents = (root / relative).read_text()
        assert ADOPTER_TEXT in contents, f"{relative}: adopter paragraph changed"
        for phrase in FACTS:
            assert phrase.casefold() in contents.casefold(), f"{relative}: missing {phrase}"
        _retired_roots_absent(contents, relative)
        checked += 1
    for relative in (*ARTICLES, *PROCESS_DOCS):
        path = root / relative
        if not path.is_file():
            emit(f"ADVISORY file-location document absent from this profile: {relative}")
            missing += 1
            continue
        contents = path.read_text()
        if relative.endswith(".json"):
            json.loads(contents)
        _retired_roots_absent(contents, relative)
        checked += 1
    return checked, missing


def _profile_fixture(root: Path, connectors: bool) -> None:
    wording = ADOPTER_TEXT + "\n" + "\n".join(FACTS)
    for relative in RUNBOOKS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(wording)
    for relative in (*ARTICLES, *PROCESS_DOCS) if connectors else ():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}" if relative.endswith(".json") else "Saved locations.")


class FileLocationsDocsSmoke(unittest.TestCase):
    def test_rendered_root_instructions_keep_adopter_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            request = AdapterRequest(
                request_id="11111111-1111-4111-8111-111111111111",
                operation_id="install_shell_integration",
                operation_ref="hydration::shell.install",
                phase="probe",
                probe_purpose="preview",
                attempt=1,
                name="fixture",
                target=target,
                flow_source_revision="a" * 40,
                answers_fingerprint="sha256:" + "b" * 64,
                approval_fingerprint=None,
                dry_run=True,
                timeout_seconds=30,
                public_inputs={"selected_coding_agents": ["claude_code", "codex"]},
            )
            for existing in (
                "",
                f"Operator preface\n{HYDRATION_BEGIN}\nOld guidance\n{HYDRATION_END}\nOperator tail\n",
            ):
                for runner_file in AGENT_INSTRUCTION_FILES:
                    (target / runner_file).write_text(existing)
                rendered = _rendered_files(request)
                for runner_file in AGENT_INSTRUCTION_FILES:
                    with self.subTest(runner_file=runner_file, replacing=bool(existing)):
                        content, mode = rendered[target / runner_file]
                        block = extract_hydration_block(content)
                        assert block is not None, f"{runner_file}: missing managed block"
                        self.assertIn(ADOPTER_TEXT, block)
                        for phrase in FACTS:
                            self.assertIn(phrase.casefold(), block.casefold())
                        self.assertNotIn("{{SOLET_NAME}}", block)
                        self.assertNotIn("{{CLONE_DIR}}", block)
                        self.assertEqual(mode, 0o644)
                        self.assertEqual(
                            content.replace(block, ""),
                            existing.replace(extract_hydration_block(existing) or "", "") if existing else "\n",
                        )
                        (target / runner_file).write_text(content)
                self.assertEqual(_rendered_files(request), rendered)

    def test_shipped_documents(self) -> None:
        self.assertEqual(len(PROCESS_DOCS), 73)
        checked, missing = verify_docs(ROOT)
        self.assertEqual(checked + missing, len(RUNBOOKS) + len(ARTICLES) + len(PROCESS_DOCS))

    def test_complete_profile_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _profile_fixture(root, True)
            self.assertEqual(verify_docs(root), (len(RUNBOOKS) + len(ARTICLES) + len(PROCESS_DOCS), 0))

    def test_absent_connector_docs_advise_and_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _profile_fixture(root, False)
            advisories: list[str] = []
            self.assertEqual(verify_docs(root, advisories.append), (len(RUNBOOKS), len(ARTICLES) + len(PROCESS_DOCS)))
            self.assertEqual(len(advisories), len(ARTICLES) + len(PROCESS_DOCS))
            self.assertTrue(all(line.startswith("ADVISORY") for line in advisories))

    def test_present_stale_doc_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _profile_fixture(root, True)
            (root / PROCESS_DOCS[0]).write_text('{"description": "export_allowed_roots"}')
            with self.assertRaisesRegex(AssertionError, "retired root wording"):
                verify_docs(root)


if __name__ == "__main__":
    unittest.main()
