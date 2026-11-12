"""Caller contracts for Files, Bulk Import and Bulk Extract operations."""

from __future__ import annotations

from typing import Final

from ananta.core.actions.action_metadata import ParameterMetadata, ParameterType

from . import bulk_api, file_api

ALL_VERBS: Final[tuple[str, ...]] = (*file_api.ENDPOINTS, *bulk_api.ENDPOINTS)
_FILE_DESCRIPTION: Final[str] = "File part object with filename, content_type (e.g. image/png or text/csv), and content_base64 (base64-encoded bytes); the file bytes are uploaded as multipart/form-data."


def _parameter(kind: ParameterType, description: str, required: bool = False) -> ParameterMetadata:
    return ParameterMetadata(type=kind, description=description, required=required)


def is_write(verb: str) -> bool:
    return (file_api.ENDPOINTS[verb] if verb in file_api.ENDPOINTS else bulk_api.ENDPOINTS[verb])[0] == "POST"


def result_type(verb: str) -> str:
    return f"marketo_{verb}_result"


def description(verb: str) -> str:
    name = verb.replace("_", " ")
    if verb in file_api.ENDPOINTS:
        return f"Marketo {name} through the typed Files API, including images. Dispatches a platform job; poll check_marketo_job_status for the vendor envelope on writes or metadata TSV on reads. Upload parts carry filename, content_type and content_base64; vendor Assets RBAC and workspace access apply."
    return f"Marketo {name} through the registered credential and vendor RBAC. Dispatches a platform job; poll check_marketo_job_status for its vendor response. Preserve batchId/exportId for vendor status calls; platform completion does not mean the vendor bulk job is complete."


def _file_parameters(verb: str) -> dict[str, ParameterMetadata]:
    method, path, paged = file_api.ENDPOINTS[verb]
    fields: dict[str, ParameterMetadata] = {}
    if "{id}" in path:
        fields["file_id"] = _parameter(ParameterType.INTEGER, "Vendor file id (id); positive integer.", True)
    if verb in {"get_asset_file_by_name", "create_asset_file"}:
        fields["name"] = _parameter(ParameterType.STRING, "Vendor file name.", True)
    if verb in {"list_asset_files", "create_asset_file"}:
        fields["folder"] = _parameter(ParameterType.OBJECT, 'Vendor folder reference {"id": 436, "type": "Folder"} (or Program).', verb == "create_asset_file")
    if verb == "create_asset_file":
        fields["description"] = _parameter(ParameterType.STRING, "Vendor description; empty string clears on upsert.")
        fields["insert_only"] = _parameter(ParameterType.BOOLEAN, "Vendor insertOnly: true refuses overwriting an existing file of the same name.")
    if method == "POST":
        fields["file"] = _parameter(ParameterType.OBJECT, _FILE_DESCRIPTION, True)
    else:
        fields["output_tsv_path"] = _parameter(ParameterType.STRING, "Absolute .tsv metadata export under registered path addresses; existing parent required.", True)
    if paged:
        fields.update(
            offset=_parameter(ParameterType.INTEGER, "Vendor initial offset, default 0. Continue from next_offset if truncated."),
            max_return=_parameter(ParameterType.INTEGER, "Vendor maxReturn, 1-200, default 200 per page."),
            row_limit=_parameter(ParameterType.INTEGER, "Total row limit; default 500, maximum 5000 with explicit override acknowledgement."),
            acknowledge_default_limit_override=_parameter(ParameterType.BOOLEAN, "True with row_limit to override the default row limit."),
        )
    return fields


def _bulk_parameters(verb: str) -> dict[str, ParameterMetadata]:
    _, path, operation = bulk_api.ENDPOINTS[verb]
    fields: dict[str, ParameterMetadata] = {}
    for key, wire, kind in (("batch_id", "batchId", ParameterType.INTEGER), ("program_id", "programId", ParameterType.STRING), ("api_name", "apiName", ParameterType.STRING), ("export_id", "exportId", ParameterType.STRING)):
        if "{" + key + "}" in path:
            fields[key] = _parameter(kind, f"Vendor {wire} path parameter.", True)
    if operation == "import":
        fields["file"] = _parameter(ParameterType.OBJECT, _FILE_DESCRIPTION, True)
        fields["format"] = _parameter(ParameterType.STRING, "Vendor format: csv, tsv, ssv (program members also CSV, TSV, SSV).", True)
        if verb == "import_leads":
            fields["lookup_field"] = _parameter(ParameterType.STRING, "Vendor lookupField; defaults to email; id selects update-only.")
            fields["partition_name"] = _parameter(ParameterType.STRING, "Vendor partitionName to import into.")
            fields["list_id"] = _parameter(ParameterType.INTEGER, "Vendor listId: static list membership to add.")
        if verb == "import_program_members":
            fields["program_member_status"] = _parameter(ParameterType.STRING, "Vendor programMemberStatus assigned to imported members.", True)
    if operation == "create":
        fields["filter"] = _parameter(ParameterType.OBJECT, "Entire vendor filter object, including nested options. Leads: one date/list filter; activities: createdAt plus optional type/primary-attribute filters; program members: programId or programIds plus optional filters.", True)
        fields["fields"] = _parameter(ParameterType.LIST, "Vendor field-name array; optional for activity extracts, required otherwise.", "activity" not in verb)
        fields["format"] = _parameter(ParameterType.STRING, "Vendor file format CSV, TSV or SSV; default CSV.")
        fields["column_header_names"] = _parameter(ParameterType.OBJECT, "Vendor columnHeaderNames mapping REST field names to output headers.")
    if operation == "list":
        fields["status"] = _parameter(ParameterType.LIST, "Vendor status filter array: created, queued, processing, cancelled, completed, failed.")
        fields["batch_size"] = _parameter(ParameterType.INTEGER, "Vendor batchSize, 1-300; default 300. One page is returned with vendor nextPageToken when more remain.")
        fields["next_page_token"] = _parameter(ParameterType.STRING, "Vendor nextPageToken from the previous list response.")
    if operation == "download" and "/export/" in path:
        fields["range"] = _parameter(ParameterType.STRING, "Optional vendor HTTP Range header, e.g. bytes=0-1023; response retains content_range and body_base64 bytes.")
    return fields


def parameters(verb: str) -> dict[str, ParameterMetadata]:
    return _file_parameters(verb) if verb in file_api.ENDPOINTS else _bulk_parameters(verb)
