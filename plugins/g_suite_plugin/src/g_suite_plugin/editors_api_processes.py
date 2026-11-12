"""Typed Docs v1, Sheets v4 and Slides v1 methods from Google's discovery revision 20261106.

Each process has explicit named, typed vendor parameters (API-wide ones
included). It is checked by editors_contract before its job is queued, and the
job runs through the shared Workspace producer as the selected account; results
are returned as received. See workspace_registry for schemas.
"""

from __future__ import annotations

from typing import Any

from ananta.core.actions.action_metadata import ContextHandling, MergeErrorProcessorCustomizations, ParameterType, ReturnValueSchema, platform_process
from ananta.core.domain.enums import ProcessorPolicyCategory

from .accounts import AccountStore
from .constants import ERROR_INVALID_PARAMS
from .editors_contract import check_editor_call, editor_verbs
from .gmail_actions import GmailIO
from .workspace_registry import process_parameters

EDITOR_VERB_SET: frozenset[str] = frozenset(editor_verbs())


class EditorsAPIProcesses:
    """Complete Docs, Sheets and Slides APIs; the plugin supplies dispatch and errors."""

    def _run_deferred(self, params: dict[str, Any], state: dict[str, Any], verb: str) -> dict[str, Any]:
        raise NotImplementedError

    def _error(self, code: str, message: str) -> dict[str, Any]:
        raise NotImplementedError

    def _require_factory(self) -> Any:
        raise NotImplementedError

    def _gmail_io(self) -> GmailIO:
        raise NotImplementedError

    def _require_accounts(self) -> AccountStore:
        raise NotImplementedError

    def _run_editor_deferred(self, params: dict[str, Any], state: dict[str, Any], verb: str) -> dict[str, Any]:
        """Refuse credential overrides and off-schema arguments before queueing the job."""
        try:
            check_editor_call(verb, params)
        except ValueError as exc:
            return self._error(ERROR_INVALID_PARAMS, str(exc))
        return self._run_deferred(params, state, verb)

    @platform_process(
        name="docs_documents_get",
        display_name="Docs Documents Get",
        description="docs.documents.get: GET v1/documents/{documentId}. Gets the latest version of the specified document. Runs as a background job; completion preserves the vendor response and names the selected account. key, access_token and oauth_token are refused: the connected account authenticates.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("docs_documents_get"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries account, method, response and response_headers."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def docs_documents_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_editor_deferred(params, state, "docs_documents_get")

    @platform_process(
        name="docs_documents_batch_update",
        display_name="Docs Documents Batch Update",
        description="docs.documents.batchUpdate: POST v1/documents/{documentId}:batchUpdate. Applies one or more updates to the document. Each request is validated before being applied. If any request is not valid, then the entire request will fail and nothing will be applied. Some requests have replies to give you some information about how they are applied. Other requests do not need to return information; these each return an empty reply. The order of replies matches that of the requests. For example, suppose you call batchUpdate with four updates, and only the third one returns information. Body: vendor BatchUpdateDocumentRequest JSON, checked against the pinned discovery schema (each batchUpdate entry sets exactly one request kind). Runs as a background job; completion preserves the vendor response and names the selected account. key, access_token and oauth_token are refused: the connected account authenticates.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("docs_documents_batch_update"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries account, method, response and response_headers."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def docs_documents_batch_update(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_editor_deferred(params, state, "docs_documents_batch_update")

    @platform_process(
        name="docs_documents_create",
        display_name="Docs Documents Create",
        description="docs.documents.create: POST v1/documents. Creates a blank document using the title given in the request. Other fields in the request, including any provided content, are ignored. Returns the created document. Body: vendor Document JSON, checked against the pinned discovery schema. Runs as a background job; completion preserves the vendor response and names the selected account. key, access_token and oauth_token are refused: the connected account authenticates.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("docs_documents_create"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries account, method, response and response_headers."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def docs_documents_create(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_editor_deferred(params, state, "docs_documents_create")

    @platform_process(
        name="sheets_spreadsheets_get_by_data_filter",
        display_name="Sheets Spreadsheets Get By Data Filter",
        description="sheets.spreadsheets.getByDataFilter: POST v4/spreadsheets/{spreadsheetId}:getByDataFilter. Returns the spreadsheet at the given ID. The caller must specify the spreadsheet ID. For more information, see Read, write, and search metadata. This method differs from GetSpreadsheet in that it allows selecting which subsets of spreadsheet data to return by specifying a dataFilters parameter. Multiple DataFilters can be specified. Specifying one or more data filters returns the portions of the spreadsheet that intersect ranges matched by any of the filters. By default, data within grids is not returned. Body: vendor GetSpreadsheetByDataFilterRequest JSON, checked against the pinned discovery schema. Runs as a background job; completion preserves the vendor response and names the selected account. key, access_token and oauth_token are refused: the connected account authenticates.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("sheets_spreadsheets_get_by_data_filter"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries account, method, response and response_headers."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def sheets_spreadsheets_get_by_data_filter(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_editor_deferred(params, state, "sheets_spreadsheets_get_by_data_filter")

    @platform_process(
        name="sheets_spreadsheets_batch_update",
        display_name="Sheets Spreadsheets Batch Update",
        description="sheets.spreadsheets.batchUpdate: POST v4/spreadsheets/{spreadsheetId}:batchUpdate. Applies one or more updates to the spreadsheet. Each request is validated before being applied. If any request is not valid then the entire request will fail and nothing will be applied. Some requests have replies to give you some information about how they are applied. The replies will mirror the requests. For example, if you applied 4 updates and the 3rd one had a reply, then the response will have 2 empty replies, the actual reply, and another empty reply, in that order. Body: vendor BatchUpdateSpreadsheetRequest JSON, checked against the pinned discovery schema (each batchUpdate entry sets exactly one request kind). Runs as a background job; completion preserves the vendor response and names the selected account. key, access_token and oauth_token are refused: the connected account authenticates.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("sheets_spreadsheets_batch_update"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries account, method, response and response_headers."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def sheets_spreadsheets_batch_update(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_editor_deferred(params, state, "sheets_spreadsheets_batch_update")

    @platform_process(
        name="sheets_spreadsheets_get",
        display_name="Sheets Spreadsheets Get",
        description="sheets.spreadsheets.get: GET v4/spreadsheets/{spreadsheetId}. Returns the spreadsheet at the given ID. The caller must specify the spreadsheet ID. By default, data within grids is not returned. You can include grid data in one of 2 ways: Specify a field mask listing your desired fields using the fields URL parameter in HTTP Set the includeGridData URL parameter to true. If a field mask is set, the includeGridData parameter is ignored For large spreadsheets, as a best practice, retrieve only the specific spreadsheet fields that you want. Runs as a background job; completion preserves the vendor response and names the selected account. key, access_token and oauth_token are refused: the connected account authenticates.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("sheets_spreadsheets_get"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries account, method, response and response_headers."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def sheets_spreadsheets_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_editor_deferred(params, state, "sheets_spreadsheets_get")

    @platform_process(
        name="sheets_spreadsheets_create",
        display_name="Sheets Spreadsheets Create",
        description="sheets.spreadsheets.create: POST v4/spreadsheets. Creates a spreadsheet, returning the newly created spreadsheet. Body: vendor Spreadsheet JSON, checked against the pinned discovery schema. Runs as a background job; completion preserves the vendor response and names the selected account. key, access_token and oauth_token are refused: the connected account authenticates.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("sheets_spreadsheets_create"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries account, method, response and response_headers."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def sheets_spreadsheets_create(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_editor_deferred(params, state, "sheets_spreadsheets_create")

    @platform_process(
        name="sheets_spreadsheets_developer_metadata_get",
        display_name="Sheets Spreadsheets Developer Metadata Get",
        description="sheets.spreadsheets.developerMetadata.get: GET v4/spreadsheets/{spreadsheetId}/developerMetadata/{metadataId}. Returns the developer metadata with the specified ID. The caller must specify the spreadsheet ID and the developer metadata's unique metadataId. For more information, see Read, write, and search metadata. Runs as a background job; completion preserves the vendor response and names the selected account. key, access_token and oauth_token are refused: the connected account authenticates.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("sheets_spreadsheets_developer_metadata_get"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries account, method, response and response_headers."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def sheets_spreadsheets_developer_metadata_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_editor_deferred(params, state, "sheets_spreadsheets_developer_metadata_get")

    @platform_process(
        name="sheets_spreadsheets_developer_metadata_search",
        display_name="Sheets Spreadsheets Developer Metadata Search",
        description="sheets.spreadsheets.developerMetadata.search: POST v4/spreadsheets/{spreadsheetId}/developerMetadata:search. Returns all developer metadata matching the specified DataFilter. For more information, see Read, write, and search metadata. If the provided DataFilter represents a DeveloperMetadataLookup object, this will return all DeveloperMetadata entries selected by it. If the DataFilter represents a location in a spreadsheet, this will return all developer metadata associated with locations intersecting that region. Body: vendor SearchDeveloperMetadataRequest JSON, checked against the pinned discovery schema. Runs as a background job; completion preserves the vendor response and names the selected account. key, access_token and oauth_token are refused: the connected account authenticates.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("sheets_spreadsheets_developer_metadata_search"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries account, method, response and response_headers."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def sheets_spreadsheets_developer_metadata_search(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_editor_deferred(params, state, "sheets_spreadsheets_developer_metadata_search")

    @platform_process(
        name="sheets_spreadsheets_values_clear",
        display_name="Sheets Spreadsheets Values Clear",
        description="sheets.spreadsheets.values.clear: POST v4/spreadsheets/{spreadsheetId}/values/{range}:clear. Clears values from a spreadsheet. The caller must specify the spreadsheet ID and range. Only values are cleared -- all other properties of the cell (such as formatting, data validation, etc..) are kept. Body: vendor ClearValuesRequest JSON, checked against the pinned discovery schema. Runs as a background job; completion preserves the vendor response and names the selected account. key, access_token and oauth_token are refused: the connected account authenticates.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("sheets_spreadsheets_values_clear"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries account, method, response and response_headers."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def sheets_spreadsheets_values_clear(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_editor_deferred(params, state, "sheets_spreadsheets_values_clear")

    @platform_process(
        name="sheets_spreadsheets_values_append",
        display_name="Sheets Spreadsheets Values Append",
        description='sheets.spreadsheets.values.append: POST v4/spreadsheets/{spreadsheetId}/values/{range}:append. Appends values to a spreadsheet. The input range is used to search for existing data and find a "table" within that range. Values will be appended to the next row of the table, starting with the first column of the table. See the guide and sample code for specific details of how tables are detected and data is appended. The caller must specify the spreadsheet ID, range, and a valueInputOption. Body: vendor ValueRange JSON, checked against the pinned discovery schema. Runs as a background job; completion preserves the vendor response and names the selected account. key, access_token and oauth_token are refused: the connected account authenticates.',
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("sheets_spreadsheets_values_append"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries account, method, response and response_headers."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def sheets_spreadsheets_values_append(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_editor_deferred(params, state, "sheets_spreadsheets_values_append")

    @platform_process(
        name="sheets_spreadsheets_values_update",
        display_name="Sheets Spreadsheets Values Update",
        description="sheets.spreadsheets.values.update: PUT v4/spreadsheets/{spreadsheetId}/values/{range}. Sets values in a range of a spreadsheet. The caller must specify the spreadsheet ID, range, and a valueInputOption. Body: vendor ValueRange JSON, checked against the pinned discovery schema. Runs as a background job; completion preserves the vendor response and names the selected account. key, access_token and oauth_token are refused: the connected account authenticates.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("sheets_spreadsheets_values_update"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries account, method, response and response_headers."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def sheets_spreadsheets_values_update(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_editor_deferred(params, state, "sheets_spreadsheets_values_update")

    @platform_process(
        name="sheets_spreadsheets_values_batch_get_by_data_filter",
        display_name="Sheets Spreadsheets Values Batch Get By Data Filter",
        description="sheets.spreadsheets.values.batchGetByDataFilter: POST v4/spreadsheets/{spreadsheetId}/values:batchGetByDataFilter. Returns one or more ranges of values that match the specified data filters. For more information, see Read, write, and search metadata. The caller must specify the spreadsheet ID and one or more DataFilters. Ranges that match any of the data filters in the request will be returned. Body: vendor BatchGetValuesByDataFilterRequest JSON, checked against the pinned discovery schema. Runs as a background job; completion preserves the vendor response and names the selected account. key, access_token and oauth_token are refused: the connected account authenticates.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("sheets_spreadsheets_values_batch_get_by_data_filter"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries account, method, response and response_headers."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def sheets_spreadsheets_values_batch_get_by_data_filter(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_editor_deferred(params, state, "sheets_spreadsheets_values_batch_get_by_data_filter")

    @platform_process(
        name="sheets_spreadsheets_values_get",
        display_name="Sheets Spreadsheets Values Get",
        description="sheets.spreadsheets.values.get: GET v4/spreadsheets/{spreadsheetId}/values/{range}. Returns a range of values from a spreadsheet. The caller must specify the spreadsheet ID and a range. Runs as a background job; completion preserves the vendor response and names the selected account. key, access_token and oauth_token are refused: the connected account authenticates.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("sheets_spreadsheets_values_get"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries account, method, response and response_headers."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def sheets_spreadsheets_values_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_editor_deferred(params, state, "sheets_spreadsheets_values_get")

    @platform_process(
        name="sheets_spreadsheets_values_batch_clear",
        display_name="Sheets Spreadsheets Values Batch Clear",
        description="sheets.spreadsheets.values.batchClear: POST v4/spreadsheets/{spreadsheetId}/values:batchClear. Clears one or more ranges of values from a spreadsheet. The caller must specify the spreadsheet ID and one or more ranges. Only values are cleared -- all other properties of the cell (such as formatting and data validation) are kept. Body: vendor BatchClearValuesRequest JSON, checked against the pinned discovery schema. Runs as a background job; completion preserves the vendor response and names the selected account. key, access_token and oauth_token are refused: the connected account authenticates.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("sheets_spreadsheets_values_batch_clear"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries account, method, response and response_headers."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def sheets_spreadsheets_values_batch_clear(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_editor_deferred(params, state, "sheets_spreadsheets_values_batch_clear")

    @platform_process(
        name="sheets_spreadsheets_values_batch_get",
        display_name="Sheets Spreadsheets Values Batch Get",
        description="sheets.spreadsheets.values.batchGet: GET v4/spreadsheets/{spreadsheetId}/values:batchGet. Returns one or more ranges of values from a spreadsheet. The caller must specify the spreadsheet ID and one or more ranges. Runs as a background job; completion preserves the vendor response and names the selected account. key, access_token and oauth_token are refused: the connected account authenticates.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("sheets_spreadsheets_values_batch_get"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries account, method, response and response_headers."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def sheets_spreadsheets_values_batch_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_editor_deferred(params, state, "sheets_spreadsheets_values_batch_get")

    @platform_process(
        name="sheets_spreadsheets_values_batch_update",
        display_name="Sheets Spreadsheets Values Batch Update",
        description="sheets.spreadsheets.values.batchUpdate: POST v4/spreadsheets/{spreadsheetId}/values:batchUpdate. Sets values in one or more ranges of a spreadsheet. The caller must specify the spreadsheet ID, a valueInputOption, and one or more ValueRanges. Body: vendor BatchUpdateValuesRequest JSON, checked against the pinned discovery schema. Runs as a background job; completion preserves the vendor response and names the selected account. key, access_token and oauth_token are refused: the connected account authenticates.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("sheets_spreadsheets_values_batch_update"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries account, method, response and response_headers."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def sheets_spreadsheets_values_batch_update(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_editor_deferred(params, state, "sheets_spreadsheets_values_batch_update")

    @platform_process(
        name="sheets_spreadsheets_values_batch_update_by_data_filter",
        display_name="Sheets Spreadsheets Values Batch Update By Data Filter",
        description="sheets.spreadsheets.values.batchUpdateByDataFilter: POST v4/spreadsheets/{spreadsheetId}/values:batchUpdateByDataFilter. Sets values in one or more ranges of a spreadsheet. For more information, see Read, write, and search metadata. The caller must specify the spreadsheet ID, a valueInputOption, and one or more DataFilterValueRanges. Body: vendor BatchUpdateValuesByDataFilterRequest JSON, checked against the pinned discovery schema. Runs as a background job; completion preserves the vendor response and names the selected account. key, access_token and oauth_token are refused: the connected account authenticates.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("sheets_spreadsheets_values_batch_update_by_data_filter"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries account, method, response and response_headers."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def sheets_spreadsheets_values_batch_update_by_data_filter(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_editor_deferred(params, state, "sheets_spreadsheets_values_batch_update_by_data_filter")

    @platform_process(
        name="sheets_spreadsheets_values_batch_clear_by_data_filter",
        display_name="Sheets Spreadsheets Values Batch Clear By Data Filter",
        description="sheets.spreadsheets.values.batchClearByDataFilter: POST v4/spreadsheets/{spreadsheetId}/values:batchClearByDataFilter. Clears one or more ranges of values from a spreadsheet. For more information, see Read, write, and search metadata. The caller must specify the spreadsheet ID and one or more DataFilters. Ranges matching any of the specified data filters will be cleared. Only values are cleared -- all other properties of the cell (such as formatting, data validation, etc.) are kept. Body: vendor BatchClearValuesByDataFilterRequest JSON, checked against the pinned discovery schema. Runs as a background job; completion preserves the vendor response and names the selected account. key, access_token and oauth_token are refused: the connected account authenticates.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("sheets_spreadsheets_values_batch_clear_by_data_filter"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries account, method, response and response_headers."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def sheets_spreadsheets_values_batch_clear_by_data_filter(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_editor_deferred(params, state, "sheets_spreadsheets_values_batch_clear_by_data_filter")

    @platform_process(
        name="sheets_spreadsheets_sheets_copy_to",
        display_name="Sheets Spreadsheets Sheets Copy To",
        description="sheets.spreadsheets.sheets.copyTo: POST v4/spreadsheets/{spreadsheetId}/sheets/{sheetId}:copyTo. Copies a single sheet from a spreadsheet to another spreadsheet. Returns the properties of the newly created sheet. Body: vendor CopySheetToAnotherSpreadsheetRequest JSON, checked against the pinned discovery schema. Runs as a background job; completion preserves the vendor response and names the selected account. key, access_token and oauth_token are refused: the connected account authenticates.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("sheets_spreadsheets_sheets_copy_to"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries account, method, response and response_headers."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def sheets_spreadsheets_sheets_copy_to(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_editor_deferred(params, state, "sheets_spreadsheets_sheets_copy_to")

    @platform_process(
        name="slides_presentations_batch_update",
        display_name="Slides Presentations Batch Update",
        description="slides.presentations.batchUpdate: POST v1/presentations/{presentationId}:batchUpdate. Applies one or more updates to the presentation. Each request is validated before being applied. If any request is not valid, then the entire request will fail and nothing will be applied. Some requests have replies to give you some information about how they are applied. Other requests do not need to return information; these each return an empty reply. The order of replies matches that of the requests. For example, suppose you call batchUpdate with four updates, and only the third one returns information. Body: vendor BatchUpdatePresentationRequest JSON, checked against the pinned discovery schema (each batchUpdate entry sets exactly one request kind). Runs as a background job; completion preserves the vendor response and names the selected account. key, access_token and oauth_token are refused: the connected account authenticates.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("slides_presentations_batch_update"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries account, method, response and response_headers."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def slides_presentations_batch_update(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_editor_deferred(params, state, "slides_presentations_batch_update")

    @platform_process(
        name="slides_presentations_create",
        display_name="Slides Presentations Create",
        description="slides.presentations.create: POST v1/presentations. Creates a blank presentation using the title given in the request. If a presentationId is provided, it is used as the ID of the new presentation. Otherwise, a new ID is generated. Other fields in the request, including any provided content, are ignored. Returns the created presentation. Body: vendor Presentation JSON, checked against the pinned discovery schema. Runs as a background job; completion preserves the vendor response and names the selected account. key, access_token and oauth_token are refused: the connected account authenticates.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("slides_presentations_create"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries account, method, response and response_headers."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def slides_presentations_create(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_editor_deferred(params, state, "slides_presentations_create")

    @platform_process(
        name="slides_presentations_get",
        display_name="Slides Presentations Get",
        description="slides.presentations.get: GET v1/presentations/{+presentationId}. Gets the latest version of the specified presentation. Runs as a background job; completion preserves the vendor response and names the selected account. key, access_token and oauth_token are refused: the connected account authenticates.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("slides_presentations_get"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries account, method, response and response_headers."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def slides_presentations_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_editor_deferred(params, state, "slides_presentations_get")

    @platform_process(
        name="slides_presentations_pages_get",
        display_name="Slides Presentations Pages Get",
        description="slides.presentations.pages.get: GET v1/presentations/{presentationId}/pages/{pageObjectId}. Gets the latest version of the specified page in the presentation. Runs as a background job; completion preserves the vendor response and names the selected account. key, access_token and oauth_token are refused: the connected account authenticates.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("slides_presentations_pages_get"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries account, method, response and response_headers."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def slides_presentations_pages_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_editor_deferred(params, state, "slides_presentations_pages_get")

    @platform_process(
        name="slides_presentations_pages_get_thumbnail",
        display_name="Slides Presentations Pages Get Thumbnail",
        description="slides.presentations.pages.getThumbnail: GET v1/presentations/{presentationId}/pages/{pageObjectId}/thumbnail. Generates a thumbnail of the latest version of the specified page in the presentation and returns a URL to the thumbnail image. This request counts as an expensive read request for quota purposes. Runs as a background job; completion preserves the vendor response and names the selected account. key, access_token and oauth_token are refused: the connected account authenticates.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("slides_presentations_pages_get_thumbnail"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries account, method, response and response_headers."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def slides_presentations_pages_get_thumbnail(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_editor_deferred(params, state, "slides_presentations_pages_get_thumbnail")
