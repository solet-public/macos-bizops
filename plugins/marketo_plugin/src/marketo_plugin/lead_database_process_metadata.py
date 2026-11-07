"""Process metadata shared by the typed Lead Database object verbs.

The plugin declares each verb with a literal ``@platform_process`` call (the
registry and AST gates read those); this module supplies the descriptions and
parameter maps so the declarations stay identical in shape.
"""

from __future__ import annotations

from typing import Final

from ananta.core.actions.action_metadata import ParameterMetadata, ParameterType, ReturnValueSchema

from .constants import (
    DEFAULT_ROW_LIMIT,
    MARKETO_LIST_PAGE_ROW_CAP,
    MARKETO_LIST_ROW_LIMIT_CAP,
    MAX_BATCH_RECORDS,
    MAX_FILTER_VALUES,
    PARAM_ACKNOWLEDGE_OVERRIDE,
    PARAM_ROW_LIMIT,
)
from .lead_database_objects import (
    API_NAME_PARAM,
    CUSTOM_OBJECT_TYPES_PATH,
    LIST_ID_PARAM,
    NAMED_ACCOUNT_LIST_MEMBERS_PATH,
    ObjectFamily,
)

JOB_HANDLE_SCHEMA: Final[ReturnValueSchema] = ReturnValueSchema(
    type=ParameterType.OBJECT,
    description="A job handle; poll check_marketo_job_status(job_id) for the finished result.",
    properties={
        "job_id": ParameterMetadata(type=ParameterType.STRING, description="Pass to check_marketo_job_status to retrieve the result."),
        "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'; dispatch never waits for the vendor call."),
    },
)

_OUTPUT_TSV_PATH: Final[ParameterMetadata] = ParameterMetadata(
    type=ParameterType.STRING,
    required=True,
    description="ABSOLUTE .tsv destination path, contained under an export_allowed_roots entry.",
)
_API_NAME: Final[ParameterMetadata] = ParameterMetadata(
    type=ParameterType.STRING,
    required=True,
    description="The custom object type's API name (e.g. car_c), as listed by list_custom_object_types.",
)
_LIST_ID: Final[ParameterMetadata] = ParameterMetadata(
    type=ParameterType.STRING,
    required=True,
    description="The named account list's marketoGUID, as returned by get_named_account_lists.",
)
_NAMED_ACCOUNT_IDS: Final[ParameterMetadata] = ParameterMetadata(
    type=ParameterType.LIST,
    required=True,
    description=f"Up to {MAX_BATCH_RECORDS} named account marketoGUID strings.",
)


def _row_limit_parameters() -> dict[str, ParameterMetadata]:
    return {
        PARAM_ACKNOWLEDGE_OVERRIDE: ParameterMetadata(
            type=ParameterType.BOOLEAN,
            required=False,
            description=f"Must be exactly true, together with row_limit, to fetch more than the default {DEFAULT_ROW_LIMIT} records.",
        ),
        PARAM_ROW_LIMIT: ParameterMetadata(
            type=ParameterType.INTEGER,
            required=False,
            description=f"Explicit record ceiling up to {MARKETO_LIST_ROW_LIMIT_CAP}; only with acknowledge_default_limit_override=true.",
        ),
    }


def _type_parameter(family: ObjectFamily) -> dict[str, ParameterMetadata]:
    return {API_NAME_PARAM: _API_NAME} if family.typed_by_api_name else {}


def describe_parameters(family: ObjectFamily) -> dict[str, ParameterMetadata]:
    return {**_type_parameter(family), "output_tsv_path": _OUTPUT_TSV_PATH}


def get_parameters(family: ObjectFamily) -> dict[str, ParameterMetadata]:
    filters_required = not family.compound_query
    filter_type_hint = f"a searchable or dedupe field from describe_{family.key}, e.g. {family.id_field}" if "describe" in family.operations else f"dedupeFields ({family.dedupe_text}) or idField ({family.id_field})"
    parameters = {
        **_type_parameter(family),
        "filter_type": ParameterMetadata(type=ParameterType.STRING, required=filters_required, description=f"Field to filter on: {filter_type_hint}."),
        "filter_values": ParameterMetadata(
            type=ParameterType.LIST,
            required=filters_required,
            description=f"Up to {MAX_FILTER_VALUES} values to match against filter_type.",
        ),
    }
    if family.query_fields:
        parameters["fields"] = ParameterMetadata(type=ParameterType.LIST, required=False, description=f"Optional {family.label} field API names to return.")
    parameters["output_tsv_path"] = _OUTPUT_TSV_PATH
    parameters.update(_row_limit_parameters())
    if family.compound_query:
        parameters["compound_keys"] = ParameterMetadata(
            type=ParameterType.LIST,
            required=False,
            description=(f"Instead of filter_values: up to {MAX_FILTER_VALUES} objects each carrying {family.dedupe_text}; sent as Marketo's POST ?_method=GET compound-key query (filter_type defaults to dedupeFields) and paged on nextPageToken."),
        )
    return parameters


def _default_action(family: ObjectFamily) -> str:
    return "createOrUpdate" if "createOrUpdate" in family.sync_actions else "createOnly"


def _other_actions(family: ObjectFamily) -> list[str]:
    return sorted(family.sync_actions - {_default_action(family)})


def sync_parameters(family: ObjectFamily) -> dict[str, ParameterMetadata]:
    others = _other_actions(family)
    action_text = f"{_default_action(family)} (vendor default), {', or '.join(others)}." if len(others) > 1 else f"{_default_action(family)} (vendor default) or {others[0]}."
    return {
        **_type_parameter(family),
        "records": ParameterMetadata(
            type=ParameterType.LIST,
            required=True,
            description=f"Up to {MAX_BATCH_RECORDS} {family.label} objects of field values, keyed by {family.dedupe_text} or {family.id_field}.",
        ),
        "action": ParameterMetadata(type=ParameterType.STRING, required=False, description=action_text),
        "dedupe_by": ParameterMetadata(type=ParameterType.STRING, required=False, description="dedupeFields (vendor default) or idField."),
    }


def delete_parameters(family: ObjectFamily) -> dict[str, ParameterMetadata]:
    return {
        **_type_parameter(family),
        "keys": ParameterMetadata(
            type=ParameterType.LIST,
            required=True,
            description=f"Up to {MAX_BATCH_RECORDS} objects naming each {family.label} by {family.dedupe_text} or by {family.id_field}.",
        ),
        "delete_by": ParameterMetadata(type=ParameterType.STRING, required=False, description="dedupeFields (vendor default) or idField."),
    }


def list_types_parameters() -> dict[str, ParameterMetadata]:
    return {
        "names": ParameterMetadata(type=ParameterType.LIST, required=False, description="Optional custom object API names to restrict the listing to."),
        "output_tsv_path": _OUTPUT_TSV_PATH,
    }


def get_members_parameters() -> dict[str, ParameterMetadata]:
    return {
        LIST_ID_PARAM: _LIST_ID,
        "fields": ParameterMetadata(type=ParameterType.LIST, required=False, description="Optional named account field API names to return."),
        "output_tsv_path": _OUTPUT_TSV_PATH,
        **_row_limit_parameters(),
    }


def change_members_parameters() -> dict[str, ParameterMetadata]:
    return {LIST_ID_PARAM: _LIST_ID, "named_account_ids": _NAMED_ACCOUNT_IDS}


def describe_description(family: ObjectFamily) -> str:
    noun = family.label if family.label.endswith("object") else f"{family.label} object"
    return f"Dispatch an async read of Marketo's {noun} description (GET {family.describe_path}). The finished job writes the field descriptors to output_tsv_path and returns id_field, dedupe_fields and searchable_fields, which are the valid filter_type values for get_{family.key}. Poll check_marketo_job_status."


def get_description(family: ObjectFamily) -> str:
    compound = " or by compound_keys (POST ?_method=GET)" if family.compound_query else ""
    return (
        f"Dispatch an async query of Marketo {family.label} records (GET {family.collection_path}) by filter_type and up to "
        f"{MAX_FILTER_VALUES} filter_values{compound}. Pages internally at the vendor's {MARKETO_LIST_PAGE_ROW_CAP}-per-call "
        f"ceiling up to {DEFAULT_ROW_LIMIT} rows by default ({MARKETO_LIST_ROW_LIMIT_CAP} with acknowledge_default_limit_override "
        "plus row_limit). The finished job writes the records to output_tsv_path and reports truncated."
    )


def sync_description(family: ObjectFamily) -> str:
    separator = ", " if len(_other_actions(family)) > 1 else " or "
    actions = f"{_default_action(family)} (default){separator}{' or '.join(_other_actions(family))}"
    return (
        f"Create and/or update up to {MAX_BATCH_RECORDS} Marketo {family.label} records in one batch "
        f"(POST {family.collection_path}). action is {actions}; dedupe_by is "
        f"dedupeFields ({family.dedupe_text}) or idField ({family.id_field}). Async write: poll "
        "check_marketo_job_status for per-record status and reasons. Vendor RBAC decides what the API user may write."
    )


def delete_description(family: ObjectFamily) -> str:
    return f"Permanently delete up to {MAX_BATCH_RECORDS} Marketo {family.label} records (POST {family.delete_path}). delete_by is dedupeFields ({family.dedupe_text}, default) or idField ({family.id_field}). Destructive async write: poll check_marketo_job_status for per-record deleted/skipped status and reasons."


LIST_TYPES_DESCRIPTION: Final[str] = (
    f"Dispatch an async listing of the instance's Marketo custom object types (GET {CUSTOM_OBJECT_TYPES_PATH}, optional names filter). "
    "The finished job writes each type's name, displayName, idField, dedupeFields, searchableFields and relationships to "
    "output_tsv_path and returns api_names, the api_name values the custom object record verbs take."
)
GET_MEMBERS_DESCRIPTION: Final[str] = (
    f"Dispatch an async read of the named accounts in one Marketo named account list (GET {NAMED_ACCOUNT_LIST_MEMBERS_PATH}.json). "
    f"Pages internally at {MARKETO_LIST_PAGE_ROW_CAP} per call up to {DEFAULT_ROW_LIMIT} rows by default "
    f"({MARKETO_LIST_ROW_LIMIT_CAP} with the override pair); writes the members to output_tsv_path and reports truncated."
)
ADD_MEMBERS_DESCRIPTION: Final[str] = (
    f"Add up to {MAX_BATCH_RECORDS} named accounts, by marketoGUID, to one Marketo named account list (POST {NAMED_ACCOUNT_LIST_MEMBERS_PATH}.json). Async write: poll check_marketo_job_status for per-record added/skipped status and reasons. Vendor RBAC decides what the API user may write."
)
REMOVE_MEMBERS_DESCRIPTION: Final[str] = f"Remove up to {MAX_BATCH_RECORDS} named accounts, by marketoGUID, from one Marketo named account list (POST {NAMED_ACCOUNT_LIST_MEMBERS_PATH}/remove.json). The accounts themselves are not deleted. Async write: poll check_marketo_job_status for per-record status and reasons."
