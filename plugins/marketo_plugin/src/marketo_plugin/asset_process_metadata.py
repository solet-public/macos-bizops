"""Caller contracts for all Folder and My Token Asset API endpoints."""

from __future__ import annotations

from typing import Final

from ananta.core.actions.action_metadata import ParameterMetadata, ParameterType

from .asset_api import ASSET_PAGE_CAP, ENDPOINTS, FOLDER_DESCRIPTION_CAP, TOKEN_NAME_CAP, is_write
from .constants import DEFAULT_ROW_LIMIT, MARKETO_LIST_ROW_LIMIT_CAP

DESCRIPTIONS: Final[dict[str, str]] = {
    "get_asset_folder": "Read a Marketo Folder or Program by its numeric folder_id and folder_type. Returns the current folder metadata, including parent, path and archive state, in a workspace TSV. Vendor RBAC and workspace permissions apply; poll check_marketo_job_status for the finished export.",
    "get_asset_folder_by_name": "Find Marketo folders by exact name, optionally scoped to a root reference, folder_type and workspace. The result can contain several matching folders. Async read writes the metadata to output_tsv_path; poll check_marketo_job_status. All vendor parameters are forwarded.",
    "list_asset_folders": "Browse Marketo asset folders under an optional root reference, with max_depth and workspace filters. Pages internally using vendor offset/maxReturn up to row_limit, returning a TSV, truncated flag and next_offset when incomplete. Poll check_marketo_job_status for completion.",
    "get_asset_folder_content": "Read the assets contained by a Marketo Folder or Program. Pages internally using offset/maxReturn up to row_limit and exports the vendor records to a workspace TSV. A truncated result includes next_offset for continuation; poll check_marketo_job_status to retrieve the export.",
    "create_asset_folder": "Create a Marketo asset folder with a name, parent reference and optional description. Sends an application/x-www-form-urlencoded request, serializing parent as embedded JSON. This changes vendor state; poll check_marketo_job_status for the vendor success envelope and new folder id.",
    "update_asset_folder": "Correct a Marketo folder name or description, clear its description with an empty string, or archive/unarchive with is_archive. The vendor determines writable folder types and permissions. Async form write returns the vendor envelope; read back before retrying an uncertain write.",
    "delete_asset_folder": "Permanently delete a Marketo asset folder by folder_id and folder_type. Marketo refuses system folders, programs and nonempty folders; no plugin permission prompt is added. Async form write: poll check_marketo_job_status and inspect the vendor envelope before reporting deletion.",
    "get_asset_tokens": "Read the My Tokens local to a Marketo Folder or Program. folder_type defaults to the vendor's Folder type when omitted. Exports the vendor folder/tokens structure to a workspace TSV; poll check_marketo_job_status for the result. This is asset content, not OAuth credential retrieval.",
    "set_asset_token": "Create or correct a local My Token on a Marketo Folder or Program by name and token_type, including an empty value to clear it. The same endpoint upserts existing tokens. Async form write returns the vendor envelope; RBAC controls access and concurrent updates are last-write-wins.",
    "delete_asset_token": "Delete a local My Token from a Marketo Folder or Program by name and token_type. This changes vendor state and can be reversed by set_asset_token using the prior value. Poll check_marketo_job_status for the vendor envelope, and read back before retrying an uncertain deletion.",
}


def _parameter(kind: ParameterType, description: str, required: bool = False) -> ParameterMetadata:
    return ParameterMetadata(type=kind, required=required, description=description)


def _identity_parameters(verb: str) -> dict[str, ParameterMetadata]:
    _, path, _ = ENDPOINTS[verb]
    fields: dict[str, ParameterMetadata] = {}
    if "{id}" in path:
        fields["folder_id"] = _parameter(ParameterType.INTEGER, "Positive numeric folder or program id.", True)
    if verb not in {"list_asset_folders", "create_asset_folder"}:
        fields["folder_type"] = _parameter(ParameterType.STRING, "Folder or Program (vendor type / folderType).", verb not in {"get_asset_folder_by_name", "get_asset_tokens"})
    if verb in {"create_asset_folder", "get_asset_folder_by_name", "update_asset_folder", "set_asset_token", "delete_asset_token"}:
        fields["name"] = _parameter(ParameterType.STRING, f"Folder name or My Token name (token maximum {TOKEN_NAME_CAP} characters).", verb != "update_asset_folder")
    return fields


def _folder_parameters(verb: str) -> dict[str, ParameterMetadata]:
    fields: dict[str, ParameterMetadata] = {}
    if verb in {"create_asset_folder", "update_asset_folder"}:
        fields["description"] = _parameter(ParameterType.STRING, f"Folder description, maximum {FOLDER_DESCRIPTION_CAP} characters; empty string clears it.")
    if verb == "create_asset_folder":
        fields["parent"] = _parameter(ParameterType.OBJECT, 'Parent reference {"id": 14, "type": "Folder"} (or Program).', True)
    if verb == "update_asset_folder":
        fields["is_archive"] = _parameter(ParameterType.BOOLEAN, "Vendor isArchive: true archives; false unarchives.")
    if verb == "list_asset_folders":
        fields["root"] = _parameter(ParameterType.OBJECT, 'Vendor root reference {"id": 14, "type": "Folder"} (or Program).')
        fields["max_depth"] = _parameter(ParameterType.INTEGER, "Nonnegative maximum depth to traverse; vendor default 2, 0 returns root only.")
    if verb == "get_asset_folder_by_name":
        fields["root"] = _parameter(ParameterType.STRING, "Vendor root reference string (folder id); folder_type is required with root.")
    if verb in {"get_asset_folder_by_name", "list_asset_folders"}:
        fields["workspace"] = _parameter(ParameterType.STRING, "Vendor workSpace: workspace name to search.")
    return fields


def _token_parameters(verb: str) -> dict[str, ParameterMetadata]:
    fields: dict[str, ParameterMetadata] = {}
    if verb in {"set_asset_token", "delete_asset_token"}:
        fields["token_type"] = _parameter(ParameterType.STRING, "Vendor type. Creation: date, number, rich text, score, sfdc campaign, or text. Deletion: existing type.", True)
    if verb == "set_asset_token":
        fields["value"] = _parameter(ParameterType.STRING, "Vendor value as a string; an empty string clears the token value.", True)
    return fields


def parameters(verb: str) -> dict[str, ParameterMetadata]:
    """Expose every documented parameter with vendor names mapped to snake case."""
    method, _, paged = ENDPOINTS[verb]
    fields = {**_identity_parameters(verb), **_folder_parameters(verb), **_token_parameters(verb)}
    if method == "GET":
        fields["output_tsv_path"] = _parameter(ParameterType.STRING, "Absolute .tsv path under export_allowed_roots; parent directory must exist.", True)
    if paged:
        fields["offset"] = _parameter(ParameterType.INTEGER, "Vendor initial nonnegative offset, default 0; also accepts next_offset from a truncated export.")
        fields["max_return"] = _parameter(ParameterType.INTEGER, f"Vendor maxReturn, 1 through {ASSET_PAGE_CAP}; default {ASSET_PAGE_CAP} per internal page.")
        fields["acknowledge_default_limit_override"] = _parameter(ParameterType.BOOLEAN, f"True with row_limit to exceed the default {DEFAULT_ROW_LIMIT} rows.")
        fields["row_limit"] = _parameter(ParameterType.INTEGER, f"Total export row ceiling up to {MARKETO_LIST_ROW_LIMIT_CAP}, with acknowledge_default_limit_override=true.")
    return fields


def guidance(verb: str) -> str:
    return "Poll check_marketo_job_status. Report vendor result, requestId and warnings; a transport failure may follow a mutation. Read back before retrying." if is_write(verb) else "Poll check_marketo_job_status. Report the TSV path and row_count. If truncated, continue from next_offset with the same filters."
