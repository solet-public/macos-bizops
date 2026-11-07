"""Typed Snowflake REST families over the account-bound request transport.

Each family is one verb with an ``operation`` parameter. Every operation is one
documented vendor method and path, taken from the Snowflake REST reference and
the published OpenAPI files (snowflakedb/snowflake-rest-api-specs: common,
result, task, stream, dynamic-table, database, schema, warehouse, stage, table,
view, sequence and pipe; SQL API: developer-guide/sql-api; Snowpipe ingestion:
user-guide/data-load-snowpipe-rest-apis). Endpoints the specs mark deprecated
(databases/{name}:from_share, warehouses/{name}:use and the underscore table
actions such as tables/{name}:as_select) are left to the generic request verb. Input checks mirror those documents: identifier pattern, query
names, types and ranges, createMode values and required body members. Nothing
here classifies reads against writes: the registered role's vendor grants
decide every operation, including deletes. Transport, JWT and credential redaction are rest_client's.

Pagination is surfaced, never followed: a list's last name for ``fromName``,
the Link header's relations, a 202 result handler for ``fetch_result``, the
next SQL API partition, a Snowpipe insert report's ``nextBeginMark`` and an
incomplete load-history scan's next start time. Derived hints are read from the already-redacted
result, so they cannot carry a credential the result itself does not.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from urllib.parse import quote

from . import rest_client
from .constants import (
    ERROR_API_ERROR,
    ERROR_AUTH_FAILED,
    ERROR_INVALID_PARAMS,
    ERROR_OBJECT_NOT_FOUND,
    ERROR_TIMEOUT,
    REST_ERROR_CONFLICT,
    REST_ERROR_STATEMENT_FAILED,
)

QueryKind = Literal["bool", "int", "str", "kind", "create_mode", "result_limit", "show_limit", "timestamp"]
BodyKind = Literal["none", "object", "optional_object", "tag_assignments", "tag_references"]

# common.yaml Identifier; a double-quoted identifier may hold any character.
_IDENTIFIER = re.compile(r'"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*|\$[0-9]+')
# Statement handles and result handlers are opaque vendor tokens.
_HANDLE = re.compile(r"[A-Za-z0-9_\-]{1,256}")
_CREATE_MODES = frozenset({"errorIfExists", "orReplace", "ifNotExists"})
_HANDLE_PARAMS = frozenset({"statement_handle", "result_handler"})
# stage.yaml filePath: "The full stage path of the file", a plain string.
_FILE_PATH = "file_path"
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_LINK = re.compile(r'<([^>]*)>\s*;\s*rel="?([A-Za-z]+)"?')
# Vendor statuses with a more specific meaning than rest_client's generic map.
_STATUS_ERRORS = {404: ERROR_OBJECT_NOT_FOUND, 408: ERROR_TIMEOUT, 409: REST_ERROR_CONFLICT}
# SQL API: 422 carries a QueryFailureStatus for a statement that ran and failed.
_SQL_API_STATUS_ERRORS = {**_STATUS_ERRORS, 422: REST_ERROR_STATEMENT_FAILED}


class RestFamilyError(ValueError):
    """Caller input that the vendor document would reject; no I/O was attempted."""


@dataclass(frozen=True)
class Operation:
    method: str
    path: str
    query: Mapping[str, QueryKind] = field(default_factory=dict)
    required_query: frozenset[str] = frozenset()
    body: BodyKind = "none"
    body_required: tuple[str, ...] = ()
    listing: bool = False
    # The vendor tracks a requestId (SQL API retries, Snowpipe reports); one is generated when omitted.
    request_id: bool = False
    body_check: Callable[[dict[str, Any]], None] | None = None


_SCHEMA = "/api/v2/databases/{database}/schemas/{schema}"
_LIST_QUERY: Mapping[str, QueryKind] = {"like": "str", "startsWith": "str", "showLimit": "show_limit", "fromName": "str"}
_IF_EXISTS: Mapping[str, QueryKind] = {"ifExists": "bool"}
_CREATE_MODE: Mapping[str, QueryKind] = {"createMode": "create_mode"}
_RESULT = Operation("GET", "/api/v2/results/{result_handler}", {"page": "int"})


def _tags(base: str, query: Mapping[str, QueryKind]) -> dict[str, Operation]:
    return {
        "set_tags": Operation("POST", base + "/{name}:set-tags", query, body="tag_assignments"),
        "unset_tags": Operation("POST", base + "/{name}:unset-tags", query, body="tag_references"),
        "get_tags": Operation("GET", base + "/{name}:get-tags", {"withLineage": "bool"}),
    }


def _sql_api() -> dict[str, Operation]:
    statements = "/api/v2/statements"
    return {
        "submit": Operation("POST", statements, {"requestId": "str", "async": "bool", "nullable": "bool", "retry": "bool"}, body="object", body_required=("statement",), request_id=True),
        "status": Operation("GET", statements + "/{statement_handle}", {"requestId": "str", "partition": "int"}),
        "cancel": Operation("POST", statements + "/{statement_handle}/cancel", {"requestId": "str"}),
    }


def _task() -> dict[str, Operation]:
    base = _SCHEMA + "/tasks"
    graphs: Mapping[str, QueryKind] = {"resultLimit": "result_limit", "errorOnly": "bool"}
    return {
        "list": Operation("GET", base, {**_LIST_QUERY, "rootOnly": "bool"}, listing=True),
        "create": Operation("POST", base, {"createMode": "create_mode"}, body="object", body_required=("name", "definition")),
        "get": Operation("GET", base + "/{name}"),
        "create_or_alter": Operation("PUT", base + "/{name}", body="object", body_required=("name", "definition")),
        "delete": Operation("DELETE", base + "/{name}", {"ifExists": "bool"}),
        "execute": Operation("POST", base + "/{name}:execute", {"asyncExec": "bool", "retryLast": "bool"}),
        "resume": Operation("POST", base + "/{name}:resume"),
        "suspend": Operation("POST", base + "/{name}:suspend"),
        "dependents": Operation("GET", base + "/{name}/dependents", {"recursive": "bool"}),
        "current_graphs": Operation("GET", base + "/{name}/current-graphs", {"resultLimit": "result_limit"}),
        "complete_graphs": Operation("GET", base + "/{name}/complete-graphs", graphs),
        **_tags(base, {"ifExists": "bool"}),
        "fetch_result": _RESULT,
    }


def _stream() -> dict[str, Operation]:
    base = _SCHEMA + "/streams"
    clone_query: Mapping[str, QueryKind] = {"createMode": "create_mode", "copyGrants": "bool", "targetDatabase": "str", "targetSchema": "str"}
    return {
        "list": Operation("GET", base, _LIST_QUERY, listing=True),
        "create": Operation("POST", base, {"createMode": "create_mode", "copyGrants": "bool"}, body="object", body_required=("name", "stream_source")),
        "get": Operation("GET", base + "/{name}"),
        "delete": Operation("DELETE", base + "/{name}", {"ifExists": "bool"}),
        "clone": Operation("POST", base + "/{name}:clone", clone_query, body="object", body_required=("name",)),
        **_tags(base, {"ifExists": "bool"}),
        "fetch_result": _RESULT,
    }


def _dynamic_table() -> dict[str, Operation]:
    base = _SCHEMA + "/dynamic-tables"
    if_exists: Mapping[str, QueryKind] = {"ifExists": "bool"}
    clone_query: Mapping[str, QueryKind] = {"createMode": "create_mode", "copyGrants": "bool", "targetDatabase": "str", "targetSchema": "str"}
    actions = {action.replace("-", "_"): Operation("POST", base + "/{name}:" + action, if_exists) for action in ("suspend", "resume", "refresh", "suspend-recluster", "resume-recluster")}
    return {
        "list": Operation("GET", base, {**_LIST_QUERY, "deep": "bool"}, listing=True),
        "create": Operation("POST", base, {"createMode": "create_mode"}, body="object", body_required=("name", "target_lag", "warehouse", "query")),
        "get": Operation("GET", base + "/{name}"),
        "delete": Operation("DELETE", base + "/{name}", if_exists),
        "clone": Operation("POST", base + "/{name}:clone", clone_query, body="object", body_required=("name",)),
        "undrop": Operation("POST", base + "/{name}:undrop"),
        **actions,
        "swap_with": Operation("POST", base + "/{name}:swap-with", {"targetName": "str", "ifExists": "bool", "targetDatabase": "str", "targetSchema": "str"}, required_query=frozenset({"targetName"})),
        **_tags(base, if_exists),
        "fetch_result": _RESULT,
    }


def _database() -> dict[str, Operation]:
    base = "/api/v2/databases"
    create: Mapping[str, QueryKind] = {**_CREATE_MODE, "kind": "kind"}
    replication = base + "/{name}/replication:"
    failover = base + "/{name}/failover:"
    return {
        "list": Operation("GET", base, {**_LIST_QUERY, "history": "bool"}, listing=True),
        "create": Operation("POST", base, create, body="object", body_required=("name",)),
        "create_from_share": Operation("POST", base + ":from-share", {**_CREATE_MODE, "share": "str"}, body="object"),
        "clone": Operation("POST", base + "/{name}:clone", create, body="object", body_required=("name",)),
        "get": Operation("GET", base + "/{name}"),
        "create_or_alter": Operation("PUT", base + "/{name}", body="object", body_required=("name",)),
        "delete": Operation("DELETE", base + "/{name}", {**_IF_EXISTS, "restrict": "bool"}),
        "undrop": Operation("POST", base + "/{name}:undrop"),
        "enable_replication": Operation("POST", replication + "enable", {"ignore_edition_check": "bool"}, body="object", body_required=("accounts",)),
        "disable_replication": Operation("POST", replication + "disable", body="optional_object"),
        "refresh_replication": Operation("POST", replication + "refresh"),
        "enable_failover": Operation("POST", failover + "enable", body="object", body_required=("accounts",)),
        "disable_failover": Operation("POST", failover + "disable", body="optional_object"),
        "primary_failover": Operation("POST", failover + "primary"),
        **_tags(base, _IF_EXISTS),
        "fetch_result": _RESULT,
    }


def _schema() -> dict[str, Operation]:
    base = "/api/v2/databases/{database}/schemas"
    create: Mapping[str, QueryKind] = {**_CREATE_MODE, "kind": "kind"}
    return {
        "list": Operation("GET", base, {**_LIST_QUERY, "history": "bool"}, listing=True),
        "create": Operation("POST", base, create, body="object", body_required=("name",)),
        "clone": Operation("POST", base + "/{name}:clone", {**create, "targetDatabase": "str"}, body="object", body_required=("name",)),
        "undrop": Operation("POST", base + "/{name}:undrop"),
        "get": Operation("GET", base + "/{name}"),
        "create_or_alter": Operation("PUT", base + "/{name}", {"kind": "kind"}, body="object", body_required=("name",)),
        "delete": Operation("DELETE", base + "/{name}", {**_IF_EXISTS, "restrict": "bool"}),
        **_tags(base, _IF_EXISTS),
        "fetch_result": _RESULT,
    }


def _warehouse() -> dict[str, Operation]:
    base = "/api/v2/warehouses"
    actions = {action: Operation("POST", base + "/{name}:" + action, _IF_EXISTS) for action in ("resume", "suspend", "abort", "enable", "disable")}
    return {
        "list": Operation("GET", base, {"like": "str"}),
        "create": Operation("POST", base, _CREATE_MODE, body="object", body_required=("name",)),
        "get": Operation("GET", base + "/{name}"),
        "create_or_alter": Operation("PUT", base + "/{name}", body="object", body_required=("name",)),
        "delete": Operation("DELETE", base + "/{name}", _IF_EXISTS),
        **actions,
        "rename": Operation("POST", base + "/{name}:rename", _IF_EXISTS, body="object", body_required=("name",)),
        **_tags(base, _IF_EXISTS),
        "fetch_result": _RESULT,
    }


def _stage() -> dict[str, Operation]:
    base = _SCHEMA + "/stages"
    return {
        "list": Operation("GET", base, {"like": "str"}),
        "create": Operation("POST", base, _CREATE_MODE, body="object", body_required=("name",)),
        "get": Operation("GET", base + "/{name}"),
        "delete": Operation("DELETE", base + "/{name}", _IF_EXISTS),
        "list_files": Operation("GET", base + "/{name}/files", {"pattern": "str"}),
        "presigned_url": Operation("POST", base + "/{name}/files/{file_path}:presigned-url", body="optional_object"),
        "fetch_result": _RESULT,
    }


def _table() -> dict[str, Operation]:
    base = _SCHEMA + "/tables"
    create: Mapping[str, QueryKind] = {**_CREATE_MODE, "copyGrants": "bool"}
    from_query: Mapping[str, QueryKind] = {**create, "query": "str"}
    targets: Mapping[str, QueryKind] = {"targetDatabase": "str", "targetSchema": "str"}
    return {
        "list": Operation("GET", base, {**_LIST_QUERY, "history": "bool", "deep": "bool"}, listing=True),
        "create": Operation("POST", base, create, body="object", body_required=("name",)),
        "create_as_select": Operation("POST", base + ":as-select", from_query, required_query=frozenset({"query"}), body="object"),
        "create_using_template": Operation("POST", base + ":using-template", from_query, required_query=frozenset({"query"}), body="optional_object"),
        "get": Operation("GET", base + "/{name}"),
        "create_or_alter": Operation("PUT", base + "/{name}", body="object", body_required=("name",)),
        "delete": Operation("DELETE", base + "/{name}", _IF_EXISTS),
        "clone": Operation("POST", base + "/{name}:clone", {**create, **targets}, body="object", body_required=("name",)),
        "create_like": Operation("POST", base + "/{name}:create-like", create, body="object"),
        "undrop": Operation("POST", base + "/{name}:undrop"),
        "suspend_recluster": Operation("POST", base + "/{name}:suspend-recluster", _IF_EXISTS),
        "resume_recluster": Operation("POST", base + "/{name}:resume-recluster", _IF_EXISTS),
        "swap_with": Operation("POST", base + "/{name}:swap-with", {**_IF_EXISTS, "targetName": "str", **targets}, required_query=frozenset({"targetName"})),
        **_tags(base, _IF_EXISTS),
        "fetch_result": _RESULT,
    }


def _view() -> dict[str, Operation]:
    base = _SCHEMA + "/views"
    return {
        "list": Operation("GET", base, {**_LIST_QUERY, "deep": "bool"}, listing=True),
        "create": Operation("POST", base, {**_CREATE_MODE, "copyGrants": "bool"}, body="object", body_required=("name", "columns", "query")),
        "get": Operation("GET", base + "/{name}"),
        "delete": Operation("DELETE", base + "/{name}", _IF_EXISTS),
        **_tags(base, _IF_EXISTS),
        "fetch_result": _RESULT,
    }


def _sequence() -> dict[str, Operation]:
    base = _SCHEMA + "/sequences"
    targets: Mapping[str, QueryKind] = {"targetDatabase": "str", "targetSchema": "str"}
    return {
        "list": Operation("GET", base, {"like": "str"}),
        "create": Operation("POST", base, _CREATE_MODE, body="object", body_required=("name",)),
        "get": Operation("GET", base + "/{name}"),
        "delete": Operation("DELETE", base + "/{name}", _IF_EXISTS),
        "clone": Operation("POST", base + "/{name}:clone", {**_CREATE_MODE, **targets}, body="object", body_required=("name",)),
        "rename": Operation("POST", base + "/{name}:rename", {**_IF_EXISTS, **targets, "targetName": "str"}, required_query=frozenset({"targetName"})),
        "fetch_result": _RESULT,
    }


# Snowpipe insertFiles: "at most 5,000 files", each path "at most 1,024 bytes long when encoded as UTF-8".
_SNOWPIPE_MAX_FILES = 5000
_SNOWPIPE_MAX_PATH_BYTES = 1024


def _insert_file(item: object) -> None:
    """One insertFiles entry: a required path and an optional byte size."""
    path = item.get("path") if isinstance(item, dict) else None
    if not isinstance(path, str) or not path or len(path.encode("utf-8")) > _SNOWPIPE_MAX_PATH_BYTES:
        raise RestFamilyError(f"every file needs a path of 1 to {_SNOWPIPE_MAX_PATH_BYTES} UTF-8 bytes")
    size = item.get("size") if isinstance(item, dict) else None
    if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size < 0):
        raise RestFamilyError("a file size must be a non-negative integer number of bytes")


def _insert_files(body: dict[str, Any]) -> None:
    files = body.get("files")
    if not isinstance(files, list) or not files or len(files) > _SNOWPIPE_MAX_FILES:
        raise RestFamilyError(f"body.files must be an array of 1 to {_SNOWPIPE_MAX_FILES} file objects")
    for item in files:
        _insert_file(item)


def _pipe() -> dict[str, Operation]:
    base = _SCHEMA + "/pipes"
    # Snowpipe ingestion addresses the pipe by its fully qualified name, database.schema.pipe.
    ingest = "/v1/data/pipes/{database}.{schema}.{name}/"
    return {
        "list": Operation("GET", base, {"like": "str"}),
        "create": Operation("POST", base, _CREATE_MODE, body="object", body_required=("name", "copy_statement")),
        "get": Operation("GET", base + "/{name}"),
        "delete": Operation("DELETE", base + "/{name}", _IF_EXISTS),
        "refresh": Operation("POST", base + "/{name}:refresh", {**_IF_EXISTS, "prefix": "str", "modified_after": "timestamp"}),
        **_tags(base, _IF_EXISTS),
        "insert_files": Operation("POST", ingest + "insertFiles", {"requestId": "str", "showSkippedFiles": "bool"}, body="object", body_required=("files",), request_id=True, body_check=_insert_files),
        "insert_report": Operation("GET", ingest + "insertReport", {"requestId": "str", "beginMark": "str"}, request_id=True),
        "load_history_scan": Operation("GET", ingest + "loadHistoryScan", {"startTimeInclusive": "timestamp", "endTimeExclusive": "timestamp", "requestId": "str"}, required_query=frozenset({"startTimeInclusive"}), request_id=True),
        "fetch_result": _RESULT,
    }


FAMILIES: Mapping[str, Mapping[str, Operation]] = {
    "sql_api": _sql_api(),
    "task": _task(),
    "stream": _stream(),
    "dynamic_table": _dynamic_table(),
    "database": _database(),
    "schema": _schema(),
    "warehouse": _warehouse(),
    "stage": _stage(),
    "table": _table(),
    "view": _view(),
    "sequence": _sequence(),
    "pipe": _pipe(),
}


def operation_names(family: str) -> str:
    """Operation names for verb parameter descriptions."""
    return ", ".join(FAMILIES[family])


def path_parameters(family: str) -> list[str]:
    """Path placeholders the family's operations use, in first-use order."""
    names = (name for operation in FAMILIES[family].values() for name in _PLACEHOLDER.findall(operation.path))
    return list(dict.fromkeys(names))


def takes_tags(family: str) -> bool:
    return any(operation.body in _TAG_MEMBERS for operation in FAMILIES[family].values())


def _operation(family: str, params: Mapping[str, Any]) -> tuple[str, Operation]:
    name = params.get("operation")
    if not isinstance(name, str) or name not in FAMILIES[family]:
        raise RestFamilyError(f"operation must be one of: {operation_names(family)}")
    return name, FAMILIES[family][name]


def _path(operation: Operation, params: Mapping[str, Any]) -> str:
    def segment(match: re.Match[str]) -> str:
        key = match.group(1)
        value = params.get(key)
        if key == _FILE_PATH:
            return _file_path(value)
        pattern = _HANDLE if key in _HANDLE_PARAMS else _IDENTIFIER
        if not isinstance(value, str) or pattern.fullmatch(value) is None:
            kind = "an opaque vendor handle" if key in _HANDLE_PARAMS else "a Snowflake identifier (double-quote names with other characters)"
            raise RestFamilyError(f"{key} must be {kind}")
        return quote(value, safe="$")

    return _PLACEHOLDER.sub(segment, operation.path)


def _file_path(value: object) -> str:
    """One encoded segment: a nested stage path keeps its slashes as %2F."""
    if not isinstance(value, str) or not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise RestFamilyError("file_path must be a non-empty stage file path without control characters")
    return quote(value, safe="")


def _boolean(key: str, value: object) -> object:
    if not isinstance(value, bool):
        raise RestFamilyError(f"{key} must be a boolean")
    return "true" if value else "false"


def _integer(key: str, value: object, low: int, high: int | None) -> object:
    valid = isinstance(value, int) and not isinstance(value, bool) and value >= low and (high is None or value <= high)
    if not valid:
        limits = f"between {low} and {high}" if high is not None else f"at least {low}"
        raise RestFamilyError(f"{key} must be an integer {limits}")
    return value


def _text(key: str, value: object, choices: frozenset[str] | None = None) -> object:
    if not isinstance(value, str) or not value:
        raise RestFamilyError(f"{key} must be a non-empty string")
    if choices is not None and value not in choices:
        raise RestFamilyError(f"{key} must be one of: {', '.join(sorted(choices))}")
    return value


def _timestamp(key: str, value: object) -> object:
    """An ISO-8601 timestamp string, the format the vendor documents for these parameters."""
    if not isinstance(value, str) or not value:
        raise RestFamilyError(f"{key} must be an ISO-8601 timestamp string")
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise RestFamilyError(f"{key} must be an ISO-8601 timestamp, for example 2026-11-07T10:00:00Z") from None
    return value


def _kind(key: str, value: object) -> object:
    """database/schema kind: a string where the empty string means permanent."""
    if not isinstance(value, str):
        raise RestFamilyError(f"{key} must be a string: transient, permanent or '' (permanent)")
    return value


# resultLimit (task graphs) and showLimit (common.yaml) are both documented as 1 to 10000.
_QUERY_CHECKS: Mapping[QueryKind, Callable[[str, object], object]] = {
    "bool": _boolean,
    "int": lambda key, value: _integer(key, value, 0, None),
    "result_limit": lambda key, value: _integer(key, value, 1, 10000),
    "show_limit": lambda key, value: _integer(key, value, 1, 10000),
    "str": _text,
    "kind": _kind,
    "create_mode": lambda key, value: _text(key, value, _CREATE_MODES),
    "timestamp": _timestamp,
}


def _query(operation: Operation, params: Mapping[str, Any]) -> dict[str, object]:
    raw = params.get("query") or {}
    if not isinstance(raw, dict):
        raise RestFamilyError("query must be an object of vendor query parameters")
    unknown = sorted(set(raw) - set(operation.query))
    if unknown:
        allowed = ", ".join(operation.query) or "none"
        raise RestFamilyError(f"query parameters {unknown} are not documented for this operation (allowed: {allowed})")
    missing = sorted(operation.required_query - set(raw))
    if missing:
        raise RestFamilyError(f"query parameters {missing} are required for this operation")
    return {key: _QUERY_CHECKS[operation.query[key]](key, value) for key, value in raw.items()}


def _tag_list(tags: object, required: tuple[str, ...]) -> list[Any]:
    """The vendor body is a JSON array (TagAssignment or TagReference items)."""
    if not isinstance(tags, list) or not tags or not all(isinstance(item, dict) for item in tags):
        raise RestFamilyError("tags must be a non-empty array of tag objects")
    for item in tags:
        for member in required:
            if not isinstance(item.get(member), str) or not item[member]:
                raise RestFamilyError(f"every tag needs a non-empty string {member}")
    return tags


# Required members of each tag item: TagAssignment needs a value, TagReference only a name.
_TAG_MEMBERS: Mapping[BodyKind, tuple[str, ...]] = {"tag_assignments": ("tag_name", "tag_value"), "tag_references": ("tag_name",)}


def _body(operation: Operation, params: Mapping[str, Any]) -> object | None:
    body = params.get("body")
    tags = params.get("tags")
    if operation.body not in _TAG_MEMBERS and tags is not None:
        raise RestFamilyError("tags is only accepted by set_tags and unset_tags")
    if operation.body not in ("object", "optional_object") and body is not None:
        raise RestFamilyError("this operation takes no body object")
    if operation.body == "none" or (operation.body == "optional_object" and body is None):
        return None
    if operation.body in _TAG_MEMBERS:
        return _tag_list(tags, _TAG_MEMBERS[operation.body])
    return _object_body(operation, body)


def _object_body(operation: Operation, body: object) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise RestFamilyError("body must be a JSON object")
    missing = [member for member in operation.body_required if body.get(member) in (None, "", [], {})]
    if missing:
        raise RestFamilyError(f"body is missing required members: {', '.join(missing)}")
    if operation.body_check is not None:
        operation.body_check(body)
    return body


def build_request(family: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """Validate typed input and return rest_client.perform_request parameters."""
    _, operation = _operation(family, params)
    query = _query(operation, params)
    body = _body(operation, params)
    if operation.request_id:
        # SQL API retry=true reuses it to avoid re-running a statement; Snowpipe echoes it for tracing.
        query.setdefault("requestId", str(uuid.uuid4()))
    request: dict[str, Any] = {"method": operation.method, "path": _path(operation, params), "query": query, "headers": {"Accept": "application/json"}}
    if body is not None:
        request["body"] = body
    return request


def _links(headers: Mapping[str, Any]) -> dict[str, str]:
    return {rel.lower(): target for target, rel in _LINK.findall(str(headers.get("link", "")))}


def _partition_hint(body: Mapping[str, Any], query: Mapping[str, Any]) -> dict[str, Any]:
    meta = body.get("resultSetMetaData")
    partitions = meta.get("partitionInfo") if isinstance(meta, dict) else None
    if not isinstance(partitions, list):
        return {}
    current = query.get("partition", 0)
    current = current if isinstance(current, int) else 0
    hint: dict[str, Any] = {"partition": current, "partition_count": len(partitions)}
    if current + 1 < len(partitions):
        hint["next_partition"] = current + 1
    return hint


def _header_hints(headers: Mapping[str, Any]) -> dict[str, Any]:
    hint: dict[str, Any] = {}
    links = _links(headers)
    if links:
        hint["links"] = links
    if headers.get("location"):
        hint["location"] = headers["location"]
    return hint


def _handle_hints(body: Mapping[str, Any], query: Mapping[str, Any]) -> dict[str, Any]:
    hint = _partition_hint(body, query)
    for source, target in (("resultHandler", "result_handler"), ("statementHandle", "statement_handle"), ("statementHandles", "statement_handles")):
        if body.get(source):
            hint[target] = body[source]
    return hint


def _snowpipe_hints(body: Mapping[str, Any]) -> dict[str, Any]:
    """insertReport continues from nextBeginMark; an incomplete loadHistoryScan from rangeEndTime."""
    hint: dict[str, Any] = {}
    if isinstance(body.get("nextBeginMark"), str):
        hint["next_begin_mark"] = body["nextBeginMark"]
    # completeResult is documented as the string "true" or "false".
    if str(body.get("completeResult")).lower() == "false" and isinstance(body.get("rangeEndTime"), str):
        hint["next_start_time_inclusive"] = body["rangeEndTime"]
    return hint


def _from_name_hint(operation: Operation, body: object, query: Mapping[str, Any]) -> dict[str, Any]:
    """A list page that filled showLimit continues after its last name (fromName)."""
    if not operation.listing or not isinstance(body, list) or not body or len(body) != query.get("showLimit"):
        return {}
    last = body[-1]
    if isinstance(last, dict) and isinstance(last.get("name"), str):
        return {"next_from_name": last["name"]}
    return {}


def pagination(operation: Operation, request: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    """Explicit next-call hints derived from the redacted vendor result."""
    body = result.get("body")
    hint = _header_hints(result.get("headers") or {})
    if isinstance(body, dict):
        hint.update(_handle_hints(body, request["query"]))
        hint.update(_snowpipe_hints(body))
    hint.update(_from_name_hint(operation, body, request["query"]))
    return hint


def run_family(family: str, plugin: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Worker entry: one vendor call, typed result, no exception text in history."""
    if plugin._app_config_loader is None:
        return plugin._error(ERROR_AUTH_FAILED, "Snowflake account is not configured")
    try:
        name, operation = _operation(family, params)
        request = build_request(family, params)
        config = plugin._app_config_loader.resolve()
        result = rest_client.perform_request(config, request, timeout_seconds=plugin._login_timeout_seconds())
    except (RestFamilyError, rest_client.RestRequestError) as exc:
        return plugin._error(ERROR_INVALID_PARAMS, str(exc))
    except Exception:
        return plugin._error(ERROR_API_ERROR, "Snowflake REST request failed; vendor state may have changed")
    status_errors = _SQL_API_STATUS_ERRORS if family == "sql_api" else _STATUS_ERRORS
    if result["status"] in status_errors:
        result["error_code"] = status_errors[result["status"]]
    data = {**result, "family": family, "operation": name, "method": operation.method}
    if "requestId" in request["query"]:
        data["request_id"] = request["query"]["requestId"]
    hint = pagination(operation, request, result)
    if hint:
        data["pagination"] = hint
    return plugin._success(data)
