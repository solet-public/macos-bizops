"""Typed Lead Database object verbs.

Families: companies, opportunities, opportunity roles, sales persons (build 2),
named accounts, named account lists with their membership, and custom object
records (build 3). Marketo exposes them through one shared REST contract
(Adobe Marketo Engage REST API, Lead Database pages, verified 2026-11-07
against Adobe's published OpenAPI document ``swagger-mapi.json``):

- describe: ``GET <base>/describe.json`` -> ``result[0]`` carries ``idField``,
  ``dedupeFields``, ``searchableFields`` and ``fields``. Named account lists
  have no describe endpoint.
- query: ``GET <base>.json`` with ``filterType``/``filterValues``/``fields``/
  ``batchSize``/``nextPageToken`` (named account lists take no ``fields``).
  Opportunity roles and custom objects also accept a compound-key query as
  ``POST <base>.json?_method=GET`` whose JSON body carries ``filterType``,
  ``fields``, ``input``, ``batchSize`` and ``nextPageToken``.
- sync: ``POST <base>.json`` with ``action``, ``dedupeBy`` and ``input``.
  Named account lists define only ``createOnly`` and ``updateOnly``.
- delete: ``POST <base>/delete.json`` with ``deleteBy`` and ``input``.
- custom objects: ``<base>`` is ``/rest/v1/customobjects/{apiName}``; the
  instance's types are listed by ``GET /rest/v1/customobjects.json?names=``.
- named account list membership: ``GET``/``POST
  /rest/v1/namedAccountList/{id}/namedAccounts.json`` (read, add) and ``POST
  .../namedAccounts/remove.json`` (remove), each entry ``{marketoGUID}``.

Every verb is async (prepare on the dispatch path, execute in the plugin's
worker). Reads write a workspace TSV like ``get_leads`` and page internally on
``nextPageToken``; writes and deletes return Marketo's own per-record
``result`` array plus tallies. Writes and deletes are first-class: the vendor
Role decides what the API user may do, so no plugin-side permission prompt or
read-only gate exists here. Every value the worker returns or writes is
passed through the client's credential redaction, which covers raw,
percent-encoded and base64 forms of the client secret and of every bearer
token the client has minted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote

from .constants import MARKETO_LIST_PAGE_ROW_CAP, MAX_BATCH_RECORDS, MAX_FILTER_VALUES
from .marketing_actions import (
    PathGate,
    _batch_envelope,
    _gate_and_check_parent,
    _paginate_token_authoritative,
    _require_list,
    _require_str,
    _resolve_effective_limit,
    _write_records_tsv,
)

SYNC_ACTIONS: Final[frozenset[str]] = frozenset({"createOnly", "updateOnly", "createOrUpdate"})
LIST_SYNC_ACTIONS: Final[frozenset[str]] = frozenset({"createOnly", "updateOnly"})
MATCH_MODES: Final[frozenset[str]] = frozenset({"dedupeFields", "idField"})
COMPOUND_QUERY_METHOD_OVERRIDE: Final[dict[str, str]] = {"_method": "GET"}
RECORD_OPERATIONS: Final[tuple[str, ...]] = ("describe", "get", "sync", "delete")
API_NAME_PARAM: Final[str] = "api_name"
LIST_ID_PARAM: Final[str] = "list_id"
CUSTOM_OBJECT_TYPES_PATH: Final[str] = "/rest/v1/customobjects.json"
NAMED_ACCOUNT_LIST_MEMBERS_PATH: Final[str] = "/rest/v1/namedAccountList/{list_id}/namedAccounts"
# The page-call breaker matches marketing_actions' internal pagination loops.
_PAGE_CALL_BREAKER_MARGIN: Final[int] = 8


@dataclass(frozen=True, slots=True)
class ObjectFamily:
    """One Lead Database object type and its verified vendor paths."""

    key: str
    label: str
    base_path: str
    id_field: str
    dedupe_fields: tuple[str, ...]
    compound_query: bool = False
    operations: tuple[str, ...] = RECORD_OPERATIONS
    sync_actions: frozenset[str] = SYNC_ACTIONS
    query_fields: bool = True
    typed_by_api_name: bool = False

    @property
    def collection_path(self) -> str:
        return f"{self.base_path}.json"

    @property
    def describe_path(self) -> str:
        return f"{self.base_path}/describe.json"

    @property
    def delete_path(self) -> str:
        return f"{self.base_path}/delete.json"

    @property
    def dedupe_text(self) -> str:
        if self.dedupe_fields:
            return " + ".join(self.dedupe_fields)
        return f"the type's dedupeFields from describe_{self.key}"

    def resolve_base(self, params: dict[str, Any]) -> str:
        """The base path for one call: custom objects substitute the type's API name."""
        if not self.typed_by_api_name:
            return self.base_path
        return self.base_path.replace("{api_name}", _path_segment(params, API_NAME_PARAM))


COMPANIES: Final[ObjectFamily] = ObjectFamily("companies", "company", "/rest/v1/companies", "id", ("externalCompanyId",))
OPPORTUNITIES: Final[ObjectFamily] = ObjectFamily("opportunities", "opportunity", "/rest/v1/opportunities", "marketoGUID", ("externalOpportunityId",))
OPPORTUNITY_ROLES: Final[ObjectFamily] = ObjectFamily(
    "opportunity_roles",
    "opportunity role",
    "/rest/v1/opportunities/roles",
    "marketoGUID",
    ("externalOpportunityId", "leadId", "role"),
    compound_query=True,
)
SALES_PERSONS: Final[ObjectFamily] = ObjectFamily("sales_persons", "sales person", "/rest/v1/salespersons", "id", ("externalSalesPersonId",))
NAMED_ACCOUNTS: Final[ObjectFamily] = ObjectFamily("named_accounts", "named account", "/rest/v1/namedaccounts", "marketoGUID", ("name",))
NAMED_ACCOUNT_LISTS: Final[ObjectFamily] = ObjectFamily(
    "named_account_lists",
    "named account list",
    "/rest/v1/namedAccountLists",
    "marketoGUID",
    ("name",),
    operations=("get", "sync", "delete"),
    sync_actions=LIST_SYNC_ACTIONS,
    query_fields=False,
)
CUSTOM_OBJECTS: Final[ObjectFamily] = ObjectFamily(
    "custom_objects",
    "custom object",
    "/rest/v1/customobjects/{api_name}",
    "marketoGUID",
    (),
    compound_query=True,
    typed_by_api_name=True,
)
FAMILIES: Final[dict[str, ObjectFamily]] = {family.key: family for family in (COMPANIES, OPPORTUNITIES, OPPORTUNITY_ROLES, SALES_PERSONS, NAMED_ACCOUNTS, NAMED_ACCOUNT_LISTS, CUSTOM_OBJECTS)}

# Verbs beyond the four record operations: custom-object type discovery and
# named account list membership.
EXTRA_VERBS: Final[dict[str, tuple[ObjectFamily, str]]] = {
    "list_custom_object_types": (CUSTOM_OBJECTS, "list_types"),
    "get_named_account_list_members": (NAMED_ACCOUNT_LISTS, "get_members"),
    "add_named_account_list_members": (NAMED_ACCOUNT_LISTS, "add_members"),
    "remove_named_account_list_members": (NAMED_ACCOUNT_LISTS, "remove_members"),
}

# verb name -> (family, operation). The plugin registers one process per entry.
VERB_OPERATIONS: Final[dict[str, tuple[ObjectFamily, str]]] = {
    **{f"{operation}_{family.key}": (family, operation) for family in FAMILIES.values() for operation in family.operations},
    **EXTRA_VERBS,
}
WRITE_OPERATIONS: Final[frozenset[str]] = frozenset({"sync", "delete", "add_members", "remove_members"})


def result_type(verb: str) -> str:
    return f"marketo_{verb}_result"


def is_write_verb(verb: str) -> bool:
    return VERB_OPERATIONS[verb][1] in WRITE_OPERATIONS


def members_path(list_id: str) -> str:
    return NAMED_ACCOUNT_LIST_MEMBERS_PATH.replace("{list_id}", list_id)


# ---------------------------------------------------------------------------
# Dispatch-path preparation (validation only, no vendor I/O)
# ---------------------------------------------------------------------------


def prepare_describe(family: ObjectFamily, params: dict[str, Any], path_gate: PathGate) -> dict[str, Any]:
    output_tsv_path = _require_str(params, "output_tsv_path")
    base_path = family.resolve_base(params)
    return {"family": family.key, "operation": "describe", "base_path": base_path, "resolved_path": _gate_and_check_parent(path_gate, output_tsv_path)}


def prepare_get(family: ObjectFamily, params: dict[str, Any], path_gate: PathGate) -> dict[str, Any]:
    verb = f"get_{family.key}"
    base_path = family.resolve_base(params)
    if not family.query_fields and params.get("fields") is not None:
        raise ValueError(f"{verb}: Marketo's {family.label} query takes no 'fields' parameter")
    resolved_path = _gate_and_check_parent(path_gate, _require_str(params, "output_tsv_path"))
    request: dict[str, Any] = {
        "family": family.key,
        "operation": "get",
        "base_path": base_path,
        "resolved_path": resolved_path,
        "fields": _optional_string_list(params, "fields"),
        "effective_limit": _resolve_effective_limit(params, verb=verb),
    }
    compound_keys = params.get("compound_keys")
    if compound_keys is not None:
        if not family.compound_query:
            raise ValueError(f"{verb}: 'compound_keys' is accepted only by get_opportunity_roles and get_custom_objects")
        if params.get("filter_values") is not None:
            raise ValueError(f"{verb}: give either 'compound_keys' or 'filter_values', not both")
        request["compound_keys"] = _require_objects(params, "compound_keys", max_len=MAX_FILTER_VALUES, required_keys=family.dedupe_fields)
        request["filter_type"] = _optional_str(params, "filter_type") or "dedupeFields"
        return request
    request["filter_type"] = _require_str(params, "filter_type")
    request["filter_values"] = [str(value) for value in _require_list(params, "filter_values", max_len=MAX_FILTER_VALUES)]
    return request


def prepare_sync(family: ObjectFamily, params: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"input": _require_objects(params, "records", max_len=MAX_BATCH_RECORDS)}
    action = _optional_choice(params, "action", family.sync_actions)
    if action is not None:
        body["action"] = action
    dedupe_by = _optional_choice(params, "dedupe_by", MATCH_MODES)
    if dedupe_by is not None:
        body["dedupeBy"] = dedupe_by
    return {"family": family.key, "operation": "sync", "base_path": family.resolve_base(params), "body": body}


def prepare_delete(family: ObjectFamily, params: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"input": _require_objects(params, "keys", max_len=MAX_BATCH_RECORDS)}
    delete_by = _optional_choice(params, "delete_by", MATCH_MODES)
    if delete_by is not None:
        body["deleteBy"] = delete_by
    return {"family": family.key, "operation": "delete", "base_path": family.resolve_base(params), "body": body}


def prepare_list_types(params: dict[str, Any], path_gate: PathGate) -> dict[str, Any]:
    resolved_path = _gate_and_check_parent(path_gate, _require_str(params, "output_tsv_path"))
    return {"family": CUSTOM_OBJECTS.key, "operation": "list_types", "resolved_path": resolved_path, "names": _optional_string_list(params, "names")}


def prepare_get_members(params: dict[str, Any], path_gate: PathGate) -> dict[str, Any]:
    list_id = _path_segment(params, LIST_ID_PARAM)
    resolved_path = _gate_and_check_parent(path_gate, _require_str(params, "output_tsv_path"))
    return {
        "family": NAMED_ACCOUNT_LISTS.key,
        "operation": "get_members",
        "base_path": members_path(list_id),
        "resolved_path": resolved_path,
        "fields": _optional_string_list(params, "fields"),
        "effective_limit": _resolve_effective_limit(params, verb="get_named_account_list_members"),
    }


def prepare_change_members(operation: str, params: dict[str, Any]) -> dict[str, Any]:
    list_id = _path_segment(params, LIST_ID_PARAM)
    guids = _require_list(params, "named_account_ids", max_len=MAX_BATCH_RECORDS)
    for number, guid in enumerate(guids, start=1):
        if not isinstance(guid, str) or not guid.strip():
            raise ValueError(f"every entry in 'named_account_ids' must be a non-empty marketoGUID string (entry {number})")
    return {
        "family": NAMED_ACCOUNT_LISTS.key,
        "operation": operation,
        "base_path": members_path(list_id),
        "body": {"input": [{"marketoGUID": guid} for guid in guids]},
    }


def prepare(verb: str, params: dict[str, Any], path_gate: PathGate) -> dict[str, Any]:
    family, operation = VERB_OPERATIONS[verb]
    match operation:
        case "describe":
            return prepare_describe(family, params, path_gate)
        case "get":
            return prepare_get(family, params, path_gate)
        case "sync":
            return prepare_sync(family, params)
        case "delete":
            return prepare_delete(family, params)
        case "list_types":
            return prepare_list_types(params, path_gate)
        case "get_members":
            return prepare_get_members(params, path_gate)
        case _:
            return prepare_change_members(operation, params)


# ---------------------------------------------------------------------------
# Worker-side execution (vendor I/O)
# ---------------------------------------------------------------------------


def execute(client: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Run one prepared Lead Database object request and redact its output."""
    family = FAMILIES[request["family"]]
    operation = request["operation"]
    match operation:
        case "describe":
            return _execute_describe(client, family, request)
        case "get":
            return _execute_get(client, request)
        case "list_types":
            return _execute_list_types(client, request)
        case "get_members":
            return _execute_get_members(client, request)
        case _:
            payload = client.post_json(_write_path(request), json=request["body"])
            return client.redact(_batch_envelope(payload))


def _write_path(request: dict[str, Any]) -> str:
    base_path: str = request["base_path"]
    suffix = {"sync": ".json", "add_members": ".json", "delete": "/delete.json", "remove_members": "/remove.json"}
    return base_path + suffix[request["operation"]]


def _execute_describe(client: Any, family: ObjectFamily, request: dict[str, Any]) -> dict[str, Any]:
    payload = client.get_json(f"{request['base_path']}/describe.json")
    results = payload.get("result")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        raise ValueError(f"Marketo {family.label} describe response omitted the object description")
    description: dict[str, Any] = client.redact(results[0])
    fields = description.get("fields")
    if not isinstance(fields, list):
        raise ValueError(f"Marketo {family.label} describe response omitted the field list")
    handle = _write_records_tsv(fields, request["resolved_path"])
    handle.update(
        {
            "object_name": description.get("name"),
            "id_field": description.get("idField"),
            "dedupe_fields": description.get("dedupeFields"),
            "searchable_fields": description.get("searchableFields"),
        }
    )
    if "relationships" in description:
        handle["relationships"] = description["relationships"]
    return handle


def _execute_get(client: Any, request: dict[str, Any]) -> dict[str, Any]:
    fields: list[str] | None = request.get("fields")
    collection_path = f"{request['base_path']}.json"
    limit = request["effective_limit"]
    compound_keys = request.get("compound_keys")
    if compound_keys is not None:
        body: dict[str, Any] = {"filterType": request["filter_type"], "input": compound_keys, "batchSize": MARKETO_LIST_PAGE_ROW_CAP}
        if fields:
            body["fields"] = fields
        records, truncated = _paginate_compound(client, collection_path, body, limit)
    else:
        query: dict[str, Any] = {
            "filterType": request["filter_type"],
            "filterValues": ",".join(request["filter_values"]),
            "batchSize": MARKETO_LIST_PAGE_ROW_CAP,
        }
        if fields:
            query["fields"] = ",".join(fields)
        records, truncated = _paginate_token_authoritative(client, collection_path, query, limit)
    handle = _write_records_tsv(client.redact(records[:limit]), request["resolved_path"])
    handle["truncated"] = truncated or len(records) > limit
    return handle


def _execute_list_types(client: Any, request: dict[str, Any]) -> dict[str, Any]:
    names: list[str] | None = request.get("names")
    payload = client.get_json(CUSTOM_OBJECT_TYPES_PATH, params={"names": ",".join(names)} if names else None)
    page = payload.get("result")
    types = client.redact(page if isinstance(page, list) else [])
    handle = _write_records_tsv(types, request["resolved_path"])
    handle["api_names"] = [entry.get("name") for entry in types if isinstance(entry, dict)]
    return handle


def _execute_get_members(client: Any, request: dict[str, Any]) -> dict[str, Any]:
    query: dict[str, Any] = {"batchSize": MARKETO_LIST_PAGE_ROW_CAP}
    fields: list[str] | None = request.get("fields")
    if fields:
        query["fields"] = ",".join(fields)
    limit = request["effective_limit"]
    records, truncated = _paginate_token_authoritative(client, f"{request['base_path']}.json", query, limit)
    handle = _write_records_tsv(client.redact(records[:limit]), request["resolved_path"])
    handle["truncated"] = truncated or len(records) > limit
    return handle


def _paginate_compound(client: Any, path: str, base_body: dict[str, Any], cap: int) -> tuple[list[Any], bool]:
    """Page a compound-key ``POST ?_method=GET`` query; ``nextPageToken`` rides in the JSON body.

    Same contract as ``_paginate_token_authoritative``: token presence is the
    continuation signal, and truncated is True whenever more data may exist
    beyond what was returned.
    """
    records: list[Any] = []
    body = dict(base_body)
    max_calls = cap // MARKETO_LIST_PAGE_ROW_CAP + _PAGE_CALL_BREAKER_MARGIN
    for _ in range(max_calls):
        payload = client.post_json(path, params=dict(COMPOUND_QUERY_METHOD_OVERRIDE), json=body)
        page = payload.get("result") or []
        records.extend(page if isinstance(page, list) else [])
        next_token = payload.get("nextPageToken")
        has_more = isinstance(next_token, str) and bool(next_token)
        if len(records) >= cap:
            return records[:cap], has_more or len(records) > cap
        if not has_more:
            return records, False
        body = {**base_body, "nextPageToken": next_token}
    return records[:cap], True


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _path_segment(params: dict[str, Any], key: str) -> str:
    """One caller-supplied path segment, percent-encoded so it can never leave its segment."""
    value = _require_str(params, key).strip()
    if value in {".", ".."}:
        raise ValueError(f"'{key}' must name a Marketo object, not a relative path segment")
    return quote(value, safe="")


def _optional_str(params: dict[str, Any], key: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    return _require_str(params, key)


def _optional_choice(params: dict[str, Any], key: str, allowed: frozenset[str]) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"'{key}' must be one of {sorted(allowed)}")
    return value


def _optional_string_list(params: dict[str, Any], key: str) -> list[str] | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"'{key}' must be a non-empty list of names if provided")
    return [str(item) for item in value]


def _require_objects(params: dict[str, Any], key: str, *, max_len: int, required_keys: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    entries = _require_list(params, key, max_len=max_len)
    for number, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict) or not entry:
            raise ValueError(f"every entry in '{key}' must be a non-empty object (entry {number})")
        missing = [name for name in required_keys if entry.get(name) in (None, "")]
        if missing:
            raise ValueError(f"entry {number} in '{key}' is missing {', '.join(missing)}")
    return entries
