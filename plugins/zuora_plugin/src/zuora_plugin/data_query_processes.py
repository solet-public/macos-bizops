"""``@platform_process`` declarations for Zuora SQL Data Query jobs and Files (mixed into ``ZuoraPlugin``).

Same contract as ``workflow_processes``: EDGE, deferred completion, one vendor
operation per job, vendor RBAC the only boundary. Downloads write to an
absolute ``output_path`` under the operator's ``registered path addresses``.
"""

from __future__ import annotations

from typing import Any, Final

from ananta.core.actions.action_metadata import (
    ContextHandling,
    MergeErrorProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    platform_process,
)
from ananta.core.domain.enums import ProcessorPolicyCategory

from .commerce_params import IDEMPOTENT_HEADER_PARAMS, V1_HEADER_PARAMS
from .workflow_processes import QUEUED

_QUEUED_NOTE: Final[str] = " Returns job_id/status queued at once; dispatch is not completion."
_JOB_ID = ParameterMetadata(type=ParameterType.STRING, required=True, description="Data query job id.")
_FILE_ID = ParameterMetadata(type=ParameterType.STRING, required=True, description="Zuora file id.")
_OUTPUT_PATH = ParameterMetadata(type=ParameterType.STRING, required=True, description="Absolute destination path under a registered path address.")
# Request headers the Data Queries reference documents on every route (submit adds Idempotency-Key).
_QUERY_HEADERS: Final[dict[str, ParameterMetadata]] = {key: V1_HEADER_PARAMS[key] for key in ("zuora_entity_ids", "zuora_org_ids", "zuora_track_id")}
_SUBMIT_HEADERS: Final[dict[str, ParameterMetadata]] = {"idempotency_key": IDEMPOTENT_HEADER_PARAMS["idempotency_key"], **_QUERY_HEADERS}
_READ: Final[MergeErrorProcessorCustomizations] = MergeErrorProcessorCustomizations(retryable=True)
_WRITE: Final[MergeErrorProcessorCustomizations] = MergeErrorProcessorCustomizations(retryable=False)
_EDGE: Final[ProcessorPolicyCategory] = ProcessorPolicyCategory.EDGE
_NONE: Final[ContextHandling] = ContextHandling.NONE


class DataQueryFileProcesses:
    """Typed SQL Data Query and Files verbs. ``ZuoraPlugin`` supplies ``_dispatch_typed``."""

    def _dispatch_typed(self, action_name: str, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @platform_process(
        name="submit_data_query",
        display_name="Zuora: Submit SQL Data Query",
        description="Submit a SQL Data Query job (POST /query/jobs). output_format JSON, CSV (default), TSV or DSV with a one-character column_separator; compression NONE (default), GZIP or ZIP; optional read_deleted, source_data LIVE or WAREHOUSE, warehouse_size, use_index_join and encryption_key. Completion carries the job id and queryStatus."
        + _QUEUED_NOTE,
        processor_policy_category=_EDGE,
        parameters={
            "query": ParameterMetadata(type=ParameterType.STRING, required=True, description="The SQL statement Zuora Data Query runs."),
            "output_format": ParameterMetadata(type=ParameterType.STRING, required=False, description="JSON, CSV, TSV or DSV."),
            "compression": ParameterMetadata(type=ParameterType.STRING, required=False, description="NONE, GZIP or ZIP."),
            "column_separator": ParameterMetadata(type=ParameterType.STRING, required=False, description="One character; DSV only."),
            "read_deleted": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="True reads only deleted records (30-day retention)."),
            "source_data": ParameterMetadata(type=ParameterType.STRING, required=False, description="LIVE (default) or WAREHOUSE."),
            "warehouse_size": ParameterMetadata(type=ParameterType.STRING, required=False, description="xsmall; WAREHOUSE only."),
            "use_index_join": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="Use Zuora's index join."),
            "encryption_key": ParameterMetadata(type=ParameterType.STRING, required=False, description="Base64 public RSA key to encrypt results."),
            **_SUBMIT_HEADERS,
        },
        return_value_schema=QUEUED,
        error_processor_customizations=_WRITE,
        context_handling=_NONE,
    )
    def submit_data_query(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("submit_data_query", params, state)

    @platform_process(
        name="list_data_query_jobs",
        display_name="Zuora: List SQL Data Query Jobs",
        description="List SQL Data Query jobs (GET /query/jobs), optionally filtered by query_status (submitted, accepted, in_progress, completed, failed, cancelled) and sized by page_size. Zuora returns one response with no continuation; completion's pagination says whether the response filled page_size (may_have_more) and how to see more."
        + _QUEUED_NOTE,
        processor_policy_category=_EDGE,
        parameters={
            "query_status": ParameterMetadata(type=ParameterType.STRING, required=False, description="Status filter."),
            "page_size": ParameterMetadata(type=ParameterType.INTEGER, required=False, description="Jobs per response."),
            **_QUERY_HEADERS,
        },
        return_value_schema=QUEUED,
        error_processor_customizations=_READ,
        context_handling=_NONE,
    )
    def list_data_query_jobs(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_data_query_jobs", params, state)

    @platform_process(
        name="get_data_query_job",
        display_name="Zuora: Get SQL Data Query Job",
        description="Read one SQL Data Query job (GET /query/jobs/{job_id}): queryStatus, outputRows, processingTime and, when completed, the dataFile location." + _QUEUED_NOTE,
        processor_policy_category=_EDGE,
        parameters={"job_id": _JOB_ID, **_QUERY_HEADERS},
        return_value_schema=QUEUED,
        error_processor_customizations=_READ,
        context_handling=_NONE,
    )
    def get_data_query_job(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_data_query_job", params, state)

    @platform_process(
        name="cancel_data_query_job",
        display_name="Zuora: Cancel SQL Data Query Job",
        description="Cancel an accepted or in-progress SQL Data Query job (DELETE /query/jobs/{job_id}); Zuora sets queryStatus cancelled, the job's terminal state." + _QUEUED_NOTE,
        processor_policy_category=_EDGE,
        parameters={"job_id": _JOB_ID, **_QUERY_HEADERS},
        return_value_schema=QUEUED,
        error_processor_customizations=_WRITE,
        context_handling=_NONE,
    )
    def cancel_data_query_job(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("cancel_data_query_job", params, state)

    @platform_process(
        name="download_data_query_result",
        display_name="Zuora: Download SQL Data Query Result",
        description="Download a completed SQL Data Query job's result file to output_path. Reads the job, then fetches Zuora's own dataFile URL without the bearer token; a job not yet completed returns downloaded false with its queryStatus." + _QUEUED_NOTE,
        processor_policy_category=_EDGE,
        parameters={"job_id": _JOB_ID, "output_path": _OUTPUT_PATH, **_QUERY_HEADERS},
        return_value_schema=QUEUED,
        error_processor_customizations=_READ,
        context_handling=_NONE,
    )
    def download_data_query_result(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("download_data_query_result", params, state)

    @platform_process(
        name="download_file",
        display_name="Zuora: Download File",
        description="Download one tenant file, such as an export result, invoice PDF or accounting report (GET /v1/files/{file_id}), to output_path; returns path, bytes, sha256 and content_type." + _QUEUED_NOTE,
        processor_policy_category=_EDGE,
        parameters={"file_id": _FILE_ID, "output_path": _OUTPUT_PATH, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=_READ,
        context_handling=_NONE,
    )
    def download_file(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("download_file", params, state)

    @platform_process(
        name="get_file_status",
        display_name="Zuora: Get File Status",
        description="Read a tenant file's status and whether it is accessible or archived (GET /v1/files/{file_id}/status)." + _QUEUED_NOTE,
        processor_policy_category=_EDGE,
        parameters={"file_id": _FILE_ID, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=_READ,
        context_handling=_NONE,
    )
    def get_file_status(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_file_status", params, state)

    @platform_process(
        name="restore_file",
        display_name="Zuora: Restore File",
        description="Restore an archived tenant file so it can be downloaded again (PUT /v1/files/{file_id}/restore)." + _QUEUED_NOTE,
        processor_policy_category=_EDGE,
        parameters={"file_id": _FILE_ID, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=_WRITE,
        context_handling=_NONE,
    )
    def restore_file(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("restore_file", params, state)
