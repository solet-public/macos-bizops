"""Statically registered transfer processes, grouped by vendor family."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from ananta.core.actions.action_metadata import ContextHandling, MergeErrorProcessorCustomizations, platform_process
from ananta.core.domain.enums import ProcessorPolicyCategory

from . import bulk_api, file_api, transfer_metadata
from .completion_templates import ResultKind
from .lead_database_process_metadata import JOB_HANDLE_SCHEMA


class TransferDispatch(ABC):
    @abstractmethod
    def _export_path_gate(self, output_tsv_path: str) -> str: ...

    @abstractmethod
    def _dispatch_async_job(
        self,
        *,
        action_name: str,
        verb_label: str,
        notes: str,
        prepare: Callable[[], dict[str, Any]],
        state: dict[str, Any],
        params: dict[str, Any],
        result_kind: ResultKind = "export",
    ) -> dict[str, Any]: ...

    def _dispatch_transfer_verb(self, verb: str, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        is_file = verb in file_api.ENDPOINTS
        return self._dispatch_async_job(
            action_name=verb,
            verb_label=verb.replace("_", " "),
            notes=f"Marketo {verb}",
            prepare=lambda: file_api.prepare(verb, params, self._export_path_gate) if is_file else bulk_api.prepare(verb, params),
            state=state,
            params=params,
            result_kind=("asset" if transfer_metadata.is_write(verb) else "export") if is_file else "bulk",
        )


class FileProcesses(TransferDispatch):
    """FileProcesses caller surface; shared worker dispatch is supplied by the plugin."""

    @platform_process(
        name="create_asset_file",
        display_name="Marketo: Create Asset File",
        description=transfer_metadata.description("create_asset_file"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("create_asset_file"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("create_asset_file")),
        context_handling=ContextHandling.NONE,
    )
    def create_asset_file(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("create_asset_file", params, state)

    @platform_process(
        name="get_asset_file",
        display_name="Marketo: Get Asset File",
        description=transfer_metadata.description("get_asset_file"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("get_asset_file"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("get_asset_file")),
        context_handling=ContextHandling.NONE,
    )
    def get_asset_file(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("get_asset_file", params, state)

    @platform_process(
        name="get_asset_file_by_name",
        display_name="Marketo: Get Asset File By Name",
        description=transfer_metadata.description("get_asset_file_by_name"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("get_asset_file_by_name"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("get_asset_file_by_name")),
        context_handling=ContextHandling.NONE,
    )
    def get_asset_file_by_name(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("get_asset_file_by_name", params, state)

    @platform_process(
        name="list_asset_files",
        display_name="Marketo: List Asset Files",
        description=transfer_metadata.description("list_asset_files"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("list_asset_files"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("list_asset_files")),
        context_handling=ContextHandling.NONE,
    )
    def list_asset_files(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("list_asset_files", params, state)

    @platform_process(
        name="update_asset_file_content",
        display_name="Marketo: Update Asset File Content",
        description=transfer_metadata.description("update_asset_file_content"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("update_asset_file_content"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("update_asset_file_content")),
        context_handling=ContextHandling.NONE,
    )
    def update_asset_file_content(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("update_asset_file_content", params, state)


class ImportProcesses(TransferDispatch):
    """ImportProcesses caller surface; shared worker dispatch is supplied by the plugin."""

    @platform_process(
        name="import_leads",
        display_name="Marketo: Import Leads",
        description=transfer_metadata.description("import_leads"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("import_leads"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("import_leads")),
        context_handling=ContextHandling.NONE,
    )
    def import_leads(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("import_leads", params, state)

    @platform_process(
        name="get_lead_import_status",
        display_name="Marketo: Get Lead Import Status",
        description=transfer_metadata.description("get_lead_import_status"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("get_lead_import_status"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("get_lead_import_status")),
        context_handling=ContextHandling.NONE,
    )
    def get_lead_import_status(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("get_lead_import_status", params, state)

    @platform_process(
        name="get_lead_import_failures",
        display_name="Marketo: Get Lead Import Failures",
        description=transfer_metadata.description("get_lead_import_failures"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("get_lead_import_failures"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("get_lead_import_failures")),
        context_handling=ContextHandling.NONE,
    )
    def get_lead_import_failures(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("get_lead_import_failures", params, state)

    @platform_process(
        name="get_lead_import_warnings",
        display_name="Marketo: Get Lead Import Warnings",
        description=transfer_metadata.description("get_lead_import_warnings"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("get_lead_import_warnings"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("get_lead_import_warnings")),
        context_handling=ContextHandling.NONE,
    )
    def get_lead_import_warnings(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("get_lead_import_warnings", params, state)

    @platform_process(
        name="import_custom_objects",
        display_name="Marketo: Import Custom Objects",
        description=transfer_metadata.description("import_custom_objects"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("import_custom_objects"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("import_custom_objects")),
        context_handling=ContextHandling.NONE,
    )
    def import_custom_objects(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("import_custom_objects", params, state)

    @platform_process(
        name="get_custom_object_import_status",
        display_name="Marketo: Get Custom Object Import Status",
        description=transfer_metadata.description("get_custom_object_import_status"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("get_custom_object_import_status"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("get_custom_object_import_status")),
        context_handling=ContextHandling.NONE,
    )
    def get_custom_object_import_status(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("get_custom_object_import_status", params, state)

    @platform_process(
        name="get_custom_object_import_failures",
        display_name="Marketo: Get Custom Object Import Failures",
        description=transfer_metadata.description("get_custom_object_import_failures"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("get_custom_object_import_failures"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("get_custom_object_import_failures")),
        context_handling=ContextHandling.NONE,
    )
    def get_custom_object_import_failures(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("get_custom_object_import_failures", params, state)

    @platform_process(
        name="get_custom_object_import_warnings",
        display_name="Marketo: Get Custom Object Import Warnings",
        description=transfer_metadata.description("get_custom_object_import_warnings"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("get_custom_object_import_warnings"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("get_custom_object_import_warnings")),
        context_handling=ContextHandling.NONE,
    )
    def get_custom_object_import_warnings(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("get_custom_object_import_warnings", params, state)

    @platform_process(
        name="import_program_members",
        display_name="Marketo: Import Program Members",
        description=transfer_metadata.description("import_program_members"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("import_program_members"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("import_program_members")),
        context_handling=ContextHandling.NONE,
    )
    def import_program_members(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("import_program_members", params, state)

    @platform_process(
        name="get_program_member_import_status",
        display_name="Marketo: Get Program Member Import Status",
        description=transfer_metadata.description("get_program_member_import_status"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("get_program_member_import_status"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("get_program_member_import_status")),
        context_handling=ContextHandling.NONE,
    )
    def get_program_member_import_status(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("get_program_member_import_status", params, state)

    @platform_process(
        name="get_program_member_import_failures",
        display_name="Marketo: Get Program Member Import Failures",
        description=transfer_metadata.description("get_program_member_import_failures"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("get_program_member_import_failures"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("get_program_member_import_failures")),
        context_handling=ContextHandling.NONE,
    )
    def get_program_member_import_failures(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("get_program_member_import_failures", params, state)

    @platform_process(
        name="get_program_member_import_warnings",
        display_name="Marketo: Get Program Member Import Warnings",
        description=transfer_metadata.description("get_program_member_import_warnings"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("get_program_member_import_warnings"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("get_program_member_import_warnings")),
        context_handling=ContextHandling.NONE,
    )
    def get_program_member_import_warnings(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("get_program_member_import_warnings", params, state)


class LeadExtractProcesses(TransferDispatch):
    """LeadExtractProcesses caller surface; shared worker dispatch is supplied by the plugin."""

    @platform_process(
        name="list_bulk_lead_extract_jobs",
        display_name="Marketo: List Bulk Lead Extract Jobs",
        description=transfer_metadata.description("list_bulk_lead_extract_jobs"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("list_bulk_lead_extract_jobs"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("list_bulk_lead_extract_jobs")),
        context_handling=ContextHandling.NONE,
    )
    def list_bulk_lead_extract_jobs(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("list_bulk_lead_extract_jobs", params, state)

    @platform_process(
        name="create_bulk_lead_extract",
        display_name="Marketo: Create Bulk Lead Extract",
        description=transfer_metadata.description("create_bulk_lead_extract"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("create_bulk_lead_extract"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("create_bulk_lead_extract")),
        context_handling=ContextHandling.NONE,
    )
    def create_bulk_lead_extract(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("create_bulk_lead_extract", params, state)

    @platform_process(
        name="enqueue_bulk_lead_extract",
        display_name="Marketo: Enqueue Bulk Lead Extract",
        description=transfer_metadata.description("enqueue_bulk_lead_extract"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("enqueue_bulk_lead_extract"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("enqueue_bulk_lead_extract")),
        context_handling=ContextHandling.NONE,
    )
    def enqueue_bulk_lead_extract(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("enqueue_bulk_lead_extract", params, state)

    @platform_process(
        name="cancel_bulk_lead_extract",
        display_name="Marketo: Cancel Bulk Lead Extract",
        description=transfer_metadata.description("cancel_bulk_lead_extract"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("cancel_bulk_lead_extract"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("cancel_bulk_lead_extract")),
        context_handling=ContextHandling.NONE,
    )
    def cancel_bulk_lead_extract(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("cancel_bulk_lead_extract", params, state)

    @platform_process(
        name="get_bulk_lead_extract_status",
        display_name="Marketo: Get Bulk Lead Extract Status",
        description=transfer_metadata.description("get_bulk_lead_extract_status"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("get_bulk_lead_extract_status"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("get_bulk_lead_extract_status")),
        context_handling=ContextHandling.NONE,
    )
    def get_bulk_lead_extract_status(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("get_bulk_lead_extract_status", params, state)

    @platform_process(
        name="download_bulk_lead_extract",
        display_name="Marketo: Download Bulk Lead Extract",
        description=transfer_metadata.description("download_bulk_lead_extract"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("download_bulk_lead_extract"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("download_bulk_lead_extract")),
        context_handling=ContextHandling.NONE,
    )
    def download_bulk_lead_extract(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("download_bulk_lead_extract", params, state)


class ActivityExtractProcesses(TransferDispatch):
    """ActivityExtractProcesses caller surface; shared worker dispatch is supplied by the plugin."""

    @platform_process(
        name="list_bulk_activity_extract_jobs",
        display_name="Marketo: List Bulk Activity Extract Jobs",
        description=transfer_metadata.description("list_bulk_activity_extract_jobs"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("list_bulk_activity_extract_jobs"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("list_bulk_activity_extract_jobs")),
        context_handling=ContextHandling.NONE,
    )
    def list_bulk_activity_extract_jobs(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("list_bulk_activity_extract_jobs", params, state)

    @platform_process(
        name="create_bulk_activity_extract",
        display_name="Marketo: Create Bulk Activity Extract",
        description=transfer_metadata.description("create_bulk_activity_extract"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("create_bulk_activity_extract"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("create_bulk_activity_extract")),
        context_handling=ContextHandling.NONE,
    )
    def create_bulk_activity_extract(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("create_bulk_activity_extract", params, state)

    @platform_process(
        name="enqueue_bulk_activity_extract",
        display_name="Marketo: Enqueue Bulk Activity Extract",
        description=transfer_metadata.description("enqueue_bulk_activity_extract"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("enqueue_bulk_activity_extract"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("enqueue_bulk_activity_extract")),
        context_handling=ContextHandling.NONE,
    )
    def enqueue_bulk_activity_extract(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("enqueue_bulk_activity_extract", params, state)

    @platform_process(
        name="cancel_bulk_activity_extract",
        display_name="Marketo: Cancel Bulk Activity Extract",
        description=transfer_metadata.description("cancel_bulk_activity_extract"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("cancel_bulk_activity_extract"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("cancel_bulk_activity_extract")),
        context_handling=ContextHandling.NONE,
    )
    def cancel_bulk_activity_extract(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("cancel_bulk_activity_extract", params, state)

    @platform_process(
        name="get_bulk_activity_extract_status",
        display_name="Marketo: Get Bulk Activity Extract Status",
        description=transfer_metadata.description("get_bulk_activity_extract_status"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("get_bulk_activity_extract_status"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("get_bulk_activity_extract_status")),
        context_handling=ContextHandling.NONE,
    )
    def get_bulk_activity_extract_status(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("get_bulk_activity_extract_status", params, state)

    @platform_process(
        name="download_bulk_activity_extract",
        display_name="Marketo: Download Bulk Activity Extract",
        description=transfer_metadata.description("download_bulk_activity_extract"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("download_bulk_activity_extract"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("download_bulk_activity_extract")),
        context_handling=ContextHandling.NONE,
    )
    def download_bulk_activity_extract(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("download_bulk_activity_extract", params, state)


class ProgramMemberExtractProcesses(TransferDispatch):
    """ProgramMemberExtractProcesses caller surface; shared worker dispatch is supplied by the plugin."""

    @platform_process(
        name="list_bulk_program_member_extract_jobs",
        display_name="Marketo: List Bulk Program Member Extract Jobs",
        description=transfer_metadata.description("list_bulk_program_member_extract_jobs"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("list_bulk_program_member_extract_jobs"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("list_bulk_program_member_extract_jobs")),
        context_handling=ContextHandling.NONE,
    )
    def list_bulk_program_member_extract_jobs(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("list_bulk_program_member_extract_jobs", params, state)

    @platform_process(
        name="create_bulk_program_member_extract",
        display_name="Marketo: Create Bulk Program Member Extract",
        description=transfer_metadata.description("create_bulk_program_member_extract"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("create_bulk_program_member_extract"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("create_bulk_program_member_extract")),
        context_handling=ContextHandling.NONE,
    )
    def create_bulk_program_member_extract(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("create_bulk_program_member_extract", params, state)

    @platform_process(
        name="enqueue_bulk_program_member_extract",
        display_name="Marketo: Enqueue Bulk Program Member Extract",
        description=transfer_metadata.description("enqueue_bulk_program_member_extract"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("enqueue_bulk_program_member_extract"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("enqueue_bulk_program_member_extract")),
        context_handling=ContextHandling.NONE,
    )
    def enqueue_bulk_program_member_extract(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("enqueue_bulk_program_member_extract", params, state)

    @platform_process(
        name="cancel_bulk_program_member_extract",
        display_name="Marketo: Cancel Bulk Program Member Extract",
        description=transfer_metadata.description("cancel_bulk_program_member_extract"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("cancel_bulk_program_member_extract"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("cancel_bulk_program_member_extract")),
        context_handling=ContextHandling.NONE,
    )
    def cancel_bulk_program_member_extract(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("cancel_bulk_program_member_extract", params, state)

    @platform_process(
        name="get_bulk_program_member_extract_status",
        display_name="Marketo: Get Bulk Program Member Extract Status",
        description=transfer_metadata.description("get_bulk_program_member_extract_status"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("get_bulk_program_member_extract_status"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("get_bulk_program_member_extract_status")),
        context_handling=ContextHandling.NONE,
    )
    def get_bulk_program_member_extract_status(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("get_bulk_program_member_extract_status", params, state)

    @platform_process(
        name="download_bulk_program_member_extract",
        display_name="Marketo: Download Bulk Program Member Extract",
        description=transfer_metadata.description("download_bulk_program_member_extract"),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=transfer_metadata.parameters("download_bulk_program_member_extract"),
        return_value_schema=JOB_HANDLE_SCHEMA,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=not transfer_metadata.is_write("download_bulk_program_member_extract")),
        context_handling=ContextHandling.NONE,
    )
    def download_bulk_program_member_extract(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_transfer_verb("download_bulk_program_member_extract", params, state)
