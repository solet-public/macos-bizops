"""``@platform_process`` declarations for Zuora Custom Objects and Object Query (mixed into ``ZuoraPlugin``).

Same contract as ``account_processes``: EDGE, deferred completion, one vendor
operation from ``custom_object_actions`` per job, vendor RBAC the only
boundary. Deletes of definitions, records and batches are first-class.
"""

from __future__ import annotations

from typing import Any, Final

from ananta.core.actions.action_metadata import ParameterMetadata, ParameterType, platform_process

from .commerce_params import EDGE, IDEMPOTENT_HEADER_PARAMS, NONE, QUEUED_NOTE, READ, V1_HEADER_PARAMS, WRITE, body, flag, integer, strings, text
from .workflow_processes import QUEUED

OBJECT: Final[ParameterMetadata] = text("Custom object API name, e.g. vehicle.", required=True)
RECORD_ID: Final[ParameterMetadata] = text("Custom object record id (a 36-character UUID).", required=True)
JOB_ID: Final[ParameterMetadata] = text("Custom object bulk job id.", required=True)
PAGE_SIZE: Final[ParameterMetadata] = integer("Records per page.")
CURSOR: Final[ParameterMetadata] = text("next_cursor from the previous page.")
QUERY_OBJECT: Final[ParameterMetadata] = text(
    "Object Query path name: a standard object in plural kebab-case (accounts, invoices, invoice-items, payments, payment-methods, subscriptions, usages, products and the rest) or a custom object as {namespace}__{object}s, e.g. default__cars.",
    required=True,
)
OBJECT_QUERY_PARAMS: Final[dict[str, ParameterMetadata]] = {
    "page_size": integer("Records per page, 1 to 99 (Zuora's default 10); also caps each expanded list."),
    "cursor": text("next_cursor from the previous page."),
    "sort": text("One sort field with direction, e.g. name.DESC or updateddate.ASC."),
    "expand": strings("Related objects to include, e.g. [billto, subscriptions]; sent as repeated expand[]."),
    "filter": strings("AND-combined conditions field.OPERATOR:value (EQ, NE, LT, GT, LE, GE, SW, IN), e.g. [currency.EQ:CAD]; sent as repeated filter[]."),
    "fields": strings("Fields of the base object to return; sent as one comma-separated fields[]."),
    "object_fields": ParameterMetadata(
        type=ParameterType.OBJECT,
        required=False,
        description="Per-object field lists {object: [field, ...]}, e.g. {account: [name, balance], billto: [firstname]}; sent as <object>.fields[]=a,b.",
    ),
    "include_null_fields": flag("Include fields whose value is null."),
    **IDEMPOTENT_HEADER_PARAMS,
}


class CustomObjectProcesses:
    """Typed Custom Object and Object Query verbs. ``ZuoraPlugin`` supplies ``_dispatch_typed``."""

    def _dispatch_typed(self, action_name: str, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    # -- Definitions -------------------------------------------------------

    @platform_process(
        name="create_custom_object_definitions",
        display_name="Zuora: Create Custom Object Definitions",
        description="Create one or more custom object definitions (POST /objects/definitions/default): schema fields, required and filterable fields, relationships and labels; body needs definitions." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": body("definitions: a map of object API name to its definition (schema, label, type, relationships)."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_custom_object_definitions(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_custom_object_definitions", params, state)

    @platform_process(
        name="list_custom_object_definitions",
        display_name="Zuora: List Custom Object Definitions",
        description="List the tenant's custom object definitions (GET /objects/definitions/default); select type returns only each object's type." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"select": text("type: return only each definition's type."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_custom_object_definitions(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_custom_object_definitions", params, state)

    @platform_process(
        name="get_custom_object_definition",
        display_name="Zuora: Get Custom Object Definition",
        description="Read one custom object definition (GET /objects/definitions/default/{object}): its schema, required and filterable fields, relationships and label." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"object": OBJECT, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_custom_object_definition(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_custom_object_definition", params, state)

    @platform_process(
        name="delete_custom_object_definition",
        display_name="Zuora: Delete Custom Object Definition",
        description="Delete a custom object definition (DELETE /objects/definitions/default/{object}); Zuora deletes it only when no record of the object exists." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"object": OBJECT, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def delete_custom_object_definition(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("delete_custom_object_definition", params, state)

    @platform_process(
        name="update_custom_object_definition",
        display_name="Zuora: Update Custom Object Definition",
        description="Update a custom object or custom field definition (POST /objects/migrations): add, change or remove a field, relabel or re-audit; one action per call, answered with a jobId to poll." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": body("actions: one action with type, namespace, object and the field, relationship or label to change."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def update_custom_object_definition(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_custom_object_definition", params, state)

    # -- Records -------------------------------------------------------------

    @platform_process(
        name="create_custom_object_records",
        display_name="Zuora: Create Custom Object Records",
        description="Create records of a custom object (POST /objects/records/default/{object}); body needs records, and allowPartialSuccess keeps the valid ones when some fail." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"object": OBJECT, "body": body("records (required) and allowPartialSuccess."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_custom_object_records(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_custom_object_records", params, state)

    @platform_process(
        name="list_custom_object_records",
        display_name="Zuora: List Custom Object Records",
        description="List records of a custom object (GET /objects/records/default/{object}) by query q or ids, one page per call; next_cursor is the cursor to pass back with page_size, null at the end." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "object": OBJECT,
            "q": text("Query string on the object's filterable fields, in Zuora's custom object record query syntax."),
            "ids": strings("Record ids to read; sent as repeated ids."),
            "page_size": PAGE_SIZE,
            "cursor": text("next_cursor from the previous page; needs page_size."),
            **V1_HEADER_PARAMS,
        },
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_custom_object_records(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_custom_object_records", params, state)

    @platform_process(
        name="get_custom_object_record",
        display_name="Zuora: Get Custom Object Record",
        description="Read one custom object record (GET /objects/records/default/{object}/{id})." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"object": OBJECT, "record_id": RECORD_ID, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_custom_object_record(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_custom_object_record", params, state)

    @platform_process(
        name="update_custom_object_record",
        display_name="Zuora: Update Custom Object Record",
        description="Replace a custom object record (PUT /objects/records/default/{object}/{id}); if_match sends If-Match so Zuora refuses a stale write. Empty strings are not stored; send null to clear." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"object": OBJECT, "record_id": RECORD_ID, "body": body("The record's field values."), "if_match": text("If-Match header: the version the update must still match."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def update_custom_object_record(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_custom_object_record", params, state)

    @platform_process(
        name="patch_custom_object_record",
        display_name="Zuora: Patch Custom Object Record",
        description="Partially update a custom object record (PATCH /objects/records/default/{object}/{id}) by JSON Merge Patch: only sent fields change, and a field set to null is cleared." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"object": OBJECT, "record_id": RECORD_ID, "body": body("The fields to change; null clears a field."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def patch_custom_object_record(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("patch_custom_object_record", params, state)

    @platform_process(
        name="delete_custom_object_record",
        display_name="Zuora: Delete Custom Object Record",
        description="Delete one custom object record (DELETE /objects/records/default/{object}/{id})." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"object": OBJECT, "record_id": RECORD_ID, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def delete_custom_object_record(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("delete_custom_object_record", params, state)

    @platform_process(
        name="batch_custom_object_records",
        display_name="Zuora: Batch Update Or Delete Custom Object Records",
        description="Update or delete up to 1,000 custom object records in one call (POST /objects/batch/default/{object}); body needs action with type update (records) or delete (ids)." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"object": OBJECT, "body": body("action: {type: update, records: {id: fields}, allowPartialSuccess} or {type: delete, ids: [...]}."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def batch_custom_object_records(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("batch_custom_object_records", params, state)

    # -- Bulk jobs -------------------------------------------------------------

    @platform_process(
        name="submit_custom_object_job",
        display_name="Zuora: Submit Custom Object Bulk Job",
        description="Submit a custom object bulk job (POST /objects/jobs) to create, update or delete records; body needs namespace, object and operation. Upload its file next." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": body("namespace (default or com_zuora), object, operation (create, update or delete) and an optional filter for deletes."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def submit_custom_object_job(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("submit_custom_object_job", params, state)

    @platform_process(
        name="list_custom_object_jobs",
        display_name="Zuora: List Custom Object Bulk Jobs",
        description="List custom object bulk jobs (GET /objects/jobs), optionally by status, one page per call; next_cursor is the cursor to pass back, null at the end." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"page_size": PAGE_SIZE, "cursor": CURSOR, "status": text("Only jobs in this status."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_custom_object_jobs(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_custom_object_jobs", params, state)

    @platform_process(
        name="get_custom_object_job",
        display_name="Zuora: Get Custom Object Bulk Job",
        description="Read a custom object bulk job (GET /objects/jobs/{id}): status, operation, object, records processed, processing time and error; also polls update_custom_object_definition jobs." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"job_id": JOB_ID, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_custom_object_job(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_custom_object_job", params, state)

    @platform_process(
        name="upload_custom_object_job_file",
        display_name="Zuora: Upload Custom Object Bulk Job File",
        description="Upload the one .csv file of a pending custom object bulk job (POST /objects/jobs/{id}/files) from input_path under registered path addresses; the job starts when the upload finishes." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"job_id": JOB_ID, "input_path": text("Absolute path of the .csv file under a registered path address; an id column is required for updates.", required=True), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def upload_custom_object_job_file(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("upload_custom_object_job_file", params, state)

    @platform_process(
        name="list_custom_object_job_errors",
        display_name="Zuora: List Custom Object Bulk Job Errors",
        description="List the row errors of a custom object bulk job (GET /objects/jobs/{id}/errors): row number, error code, message and the rejected record." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"job_id": JOB_ID, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_custom_object_job_errors(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_custom_object_job_errors", params, state)

    @platform_process(
        name="cancel_custom_object_job",
        display_name="Zuora: Cancel Custom Object Bulk Job",
        description="Cancel a custom object bulk job (PATCH /objects/jobs/{id}/cancel), its terminal transition." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"job_id": JOB_ID, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def cancel_custom_object_job(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("cancel_custom_object_job", params, state)

    # -- Object Query ----------------------------------------------------------

    @platform_process(
        name="query_objects",
        display_name="Zuora: Object Query List",
        description="List records of any standard or custom object through Object Query (GET /object-query/{object}) with filter[], expand[], sort[] and field lists, one page per call; next_cursor is the cursor to pass back, null at the end." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"object": QUERY_OBJECT, **OBJECT_QUERY_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def query_objects(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("query_objects", params, state)

    @platform_process(
        name="query_object",
        display_name="Zuora: Object Query Get",
        description="Read one record of any standard or custom object by id or key through Object Query (GET /object-query/{object}/{key}), with expanded related objects and field lists." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"object": QUERY_OBJECT, "key": text("Object id or number, e.g. an account number or invoice number.", required=True), **OBJECT_QUERY_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def query_object(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("query_object", params, state)
