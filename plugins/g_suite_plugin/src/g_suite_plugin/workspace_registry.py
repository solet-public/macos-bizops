"""Pinned vendor contracts for the Drive, People, Tasks, Docs, Sheets and Slides processes.

Discovery revision 20261106, fetched from Google's discovery endpoints. Keeping
the full documents also preserves schemas, media support and newer methods
which older installations of google-api-python-client cannot otherwise build.
"""

from __future__ import annotations

import json
import re
from functools import cache
from importlib.resources import files
from typing import Any

from ananta.core.actions.action_metadata import ParameterMetadata, ParameterType

# Build 3 products, then the build 4 editor APIs (Docs v1, Sheets v4, Slides v1).
WORKSPACE_PRODUCTS: tuple[str, ...] = ("drive", "people", "tasks", "docs", "sheets", "slides")


@cache
def discovery_document(product: str) -> dict[str, Any]:
    if product not in WORKSPACE_PRODUCTS:
        raise ValueError(f"Unknown Workspace API: {product}")
    return json.loads(files("g_suite_plugin").joinpath(f"discovery/{product}.json").read_text())


def _methods(node: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = list(node.get("methods", {}).values())
    for resource in node.get("resources", {}).values():
        result.extend(_methods(resource))
    return result


def verb_name(method_id: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", method_id.replace(".", "_")).lower()


WORKSPACE_METHODS: dict[str, tuple[str, dict[str, Any]]] = {verb_name(method["id"]): (product, method) for product in WORKSPACE_PRODUCTS for method in _methods(discovery_document(product))}

_TYPES = {"string": ParameterType.STRING, "integer": ParameterType.INTEGER, "boolean": ParameterType.BOOLEAN, "number": ParameterType.FLOAT}


def parameter_schemas(verb: str) -> dict[str, dict[str, Any]]:
    product, method = WORKSPACE_METHODS[verb]
    schemas = {**discovery_document(product).get("parameters", {}), **method.get("parameters", {})}
    # Authentication belongs to the resolved vaulted account, not query overrides.
    return {name: schema for name, schema in schemas.items() if name not in {"access_token", "oauth_token", "key"}}


def process_parameters(verb: str) -> dict[str, ParameterMetadata]:
    """Typed vendor query/path arguments; JSON request bodies keep vendor names."""
    from .account_processes import ACCOUNT_PARAM

    _, method = WORKSPACE_METHODS[verb]
    result = {"account": ACCOUNT_PARAM}
    for key in ("if_match", "if_none_match"):
        result[key] = ParameterMetadata(type=ParameterType.STRING, required=False, description="Conditional HTTP etag header; stale writes are refused by Google where supported. Read the current etag before correcting an object.")
    for name, schema in parameter_schemas(verb).items():
        kind = ParameterType.LIST if schema.get("repeated") else _TYPES.get(schema.get("type", "string"), ParameterType.STRING)
        result[name] = ParameterMetadata(type=kind, required=schema.get("required", False), description=schema.get("description", name))
    if "request" in method:
        result["body"] = ParameterMetadata(type=ParameterType.OBJECT, required=False, description=f"Vendor {method['request']['$ref']} JSON body; preserve vendor field names, etags and update masks. See the API coverage article.")
    if method.get("supportsMediaUpload"):
        result["blob_key"] = ParameterMetadata(type=ParameterType.STRING, required=False, description="Blob containing file content; optional for metadata-only create/update. Use blob storage to ingest local files first.")
        result["mime_type"] = ParameterMetadata(type=ParameterType.STRING, required=False, description="Upload MIME type; defaults to the stored blob MIME type.")
    return result
