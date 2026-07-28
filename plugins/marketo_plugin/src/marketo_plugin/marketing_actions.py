"""Marketo verb implementations — pure functions over a built ``MarketoClient``.

Each function takes an already-built :class:`http_client.MarketoClient` and a
``params`` dict, returning a plain result dict. Blob I/O is injected
(``blob_writer``) for the four verbs whose result can be large (describe,
get_leads, list_campaigns, list_static_lists). Invalid parameters raise
``ValueError`` (mapped to ``marketo.invalid_params``); ``success: false``
envelopes raise ``MarketoEnvelopeError`` (carrying the payload for the
plugin's classifier).

Batch verbs (create_or_update_leads, delete_leads, add/remove_leads_from_list)
return Marketo's own per-record ``result`` array UNCHANGED plus a computed
tally — a per-record ``status: "skipped"``/``"failed"`` entry is normal data,
not a plugin-level fault, because Marketo's overall ``success`` stays true for
those calls. Only a ``success: false`` envelope (structural fault — bad
batch shape, auth, access) raises.

No bulk-extract verb exists (v1 scope): Marketo's async Bulk Extract job API
(create -> enqueue -> poll status -> download file) is a materially different
control-flow shape from every other verb here and is explicitly deferred —
``get_leads`` covers ad-hoc reads with the same inline-or-spill envelope the
other connectors use.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .constants import (
    CHECK_SETUP_PROBES,
    CHECK_SETUP_UNVERIFIED_WRITE_VERBS,
    DEFAULT_LEAD_ACTION,
    ERROR_AUTH_FAILED,
    ERROR_PARTITION_ACCESS_DENIED,
    ERROR_PERMISSION_DENIED,
    INLINE_BYTE_CAP,
    LEAD_ACTIONS,
    LEAD_FILTER_TYPES,
    MAX_BATCH_RECORDS,
    MAX_FILTER_VALUES,
    MAX_MERGE_LOSING_LEADS,
    MAX_MERGE_LOSING_LEADS_CRM,
    MAX_TRIGGER_LEADS,
    MIME_JSON,
)
from .errors import MarketoEnvelopeError, classify_marketo_envelope

# blob_writer(content, filename, mime_type) -> blob_id (the returned *_blob_key)
BlobWriter = Callable[[bytes, str, str], str]


def describe_lead_fields(client: Any, _params: dict[str, Any], blob_writer: BlobWriter) -> dict[str, Any]:
    """Fetch the full lead field metadata list (id, displayName, name, dataType, ...)."""
    payload = client.get_json("/rest/v1/leads/describe.json")
    fields = payload.get("result") or []
    return _spill_envelope(fields if isinstance(fields, list) else [], blob_writer, "describe_lead_fields_results.json")


def get_leads(client: Any, params: dict[str, Any], blob_writer: BlobWriter) -> dict[str, Any]:
    """Query leads by filterType/filterValues; optionally restrict the returned fields."""
    filter_type = _require_str(params, "filter_type")
    if filter_type not in LEAD_FILTER_TYPES:
        raise ValueError(f"'filter_type' must be one of {sorted(LEAD_FILTER_TYPES)}, got {filter_type!r}")
    filter_values = _require_list(params, "filter_values", max_len=MAX_FILTER_VALUES)
    query: dict[str, Any] = {
        "filterType": filter_type,
        "filterValues": ",".join(str(v) for v in filter_values),
    }
    fields = params.get("fields")
    if isinstance(fields, list) and fields:
        query["fields"] = ",".join(str(f) for f in fields)
    next_page_token = params.get("next_page_token")
    if isinstance(next_page_token, str) and next_page_token:
        query["nextPageToken"] = next_page_token
    payload = client.get_json("/rest/v1/leads.json", params=query)
    leads = payload.get("result") or []
    envelope = _spill_envelope(leads if isinstance(leads, list) else [], blob_writer, "get_leads_results.json")
    envelope["next_page_token"] = payload.get("nextPageToken")
    envelope["more_result"] = bool(payload.get("moreResult", False))
    return envelope


def create_or_update_leads(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Create and/or update up to MAX_BATCH_RECORDS lead records in one batch."""
    action = params.get("action") or DEFAULT_LEAD_ACTION
    if action not in LEAD_ACTIONS:
        raise ValueError(f"'action' must be one of {sorted(LEAD_ACTIONS)}, got {action!r}")
    records = _require_list(params, "records", max_len=MAX_BATCH_RECORDS)
    for record in records:
        if not isinstance(record, dict) or not record:
            raise ValueError("every entry in 'records' must be a non-empty object")
    body: dict[str, Any] = {"action": action, "input": records}
    lookup_field = params.get("lookup_field")
    if isinstance(lookup_field, str) and lookup_field:
        body["lookupField"] = lookup_field
    payload = client.post_json("/rest/v1/leads.json", json=body)
    return _batch_envelope(payload)


def delete_leads(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Delete up to MAX_BATCH_RECORDS leads by id."""
    lead_ids = _require_list(params, "lead_ids", max_len=MAX_BATCH_RECORDS)
    body = {"input": [{"id": lead_id} for lead_id in lead_ids]}
    payload = client.post_json("/rest/v1/leads/delete.json", json=body)
    return _batch_envelope(payload)


def merge_leads(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Merge up to 25 losing leads into one winning lead (winner's fields take precedence).

    ``merge_in_crm=True`` also merges the natively-synced CRM records — Marketo
    itself restricts a CRM merge to exactly ONE losing lead per call (not 25),
    so that cap is enforced here rather than left to a server-side 1080.
    """
    winning_lead_id = _require_str(params, "winning_lead_id")
    merge_in_crm = params.get("merge_in_crm")
    if merge_in_crm is not None and not isinstance(merge_in_crm, bool):
        raise ValueError("'merge_in_crm' must be a boolean if provided")
    cap = MAX_MERGE_LOSING_LEADS_CRM if merge_in_crm else MAX_MERGE_LOSING_LEADS
    losing_lead_ids = _require_list(params, "losing_lead_ids", max_len=cap)
    query: dict[str, Any] = {"leadIds": ",".join(str(lead_id) for lead_id in losing_lead_ids)}
    if merge_in_crm is not None:
        query["mergeInCRM"] = "true" if merge_in_crm else "false"
    payload = client.post_json(f"/rest/v1/leads/{winning_lead_id}/merge.json", params=query)
    return {"success": bool(payload.get("success", True)), "request_id": payload.get("requestId")}


def list_campaigns(client: Any, params: dict[str, Any], blob_writer: BlobWriter) -> dict[str, Any]:
    """List campaigns, optionally filtered by name and/or program name."""
    query: dict[str, Any] = {}
    names = params.get("names")
    if isinstance(names, list) and names:
        query["name"] = [str(n) for n in names]
    program_names = params.get("program_names")
    if isinstance(program_names, list) and program_names:
        query["programName"] = [str(n) for n in program_names]
    payload = client.get_json("/rest/v1/campaigns.json", params=query or None)
    campaigns = payload.get("result") or []
    return _spill_envelope(campaigns if isinstance(campaigns, list) else [], blob_writer, "list_campaigns_results.json")


def trigger_campaign(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Trigger a campaign (Request Campaign) for up to MAX_TRIGGER_LEADS leads, with optional tokens."""
    campaign_id = _require_str(params, "campaign_id")
    lead_ids = _require_list(params, "lead_ids", max_len=MAX_TRIGGER_LEADS)
    input_body: dict[str, Any] = {"leads": [{"id": lead_id} for lead_id in lead_ids]}
    tokens = params.get("tokens")
    if isinstance(tokens, list) and tokens:
        for token in tokens:
            if not isinstance(token, dict) or "name" not in token or "value" not in token:
                raise ValueError("every entry in 'tokens' must be an object with 'name' and 'value'")
        input_body["tokens"] = tokens
    payload = client.post_json(f"/rest/v1/campaigns/{campaign_id}/trigger.json", json={"input": input_body})
    return {"success": bool(payload.get("success", True)), "request_id": payload.get("requestId")}


def list_static_lists(client: Any, params: dict[str, Any], blob_writer: BlobWriter) -> dict[str, Any]:
    """List static lists, optionally filtered by name."""
    query: dict[str, Any] = {}
    names = params.get("names")
    if isinstance(names, list) and names:
        query["name"] = [str(n) for n in names]
    payload = client.get_json("/rest/v1/lists.json", params=query or None)
    lists_ = payload.get("result") or []
    return _spill_envelope(lists_ if isinstance(lists_, list) else [], blob_writer, "list_static_lists_results.json")


def add_leads_to_list(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Add up to MAX_BATCH_RECORDS leads to a static list by id."""
    list_id = _require_str(params, "list_id")
    lead_ids = _require_list(params, "lead_ids", max_len=MAX_BATCH_RECORDS)
    body = {"input": [{"id": lead_id} for lead_id in lead_ids]}
    payload = client.post_json(f"/rest/v1/lists/{list_id}/leads.json", json=body)
    return _batch_envelope(payload)


def remove_leads_from_list(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Remove up to MAX_BATCH_RECORDS leads from a static list by id (DELETE-with-body)."""
    list_id = _require_str(params, "list_id")
    lead_ids = _require_list(params, "lead_ids", max_len=MAX_BATCH_RECORDS)
    body = {"input": [{"id": lead_id} for lead_id in lead_ids]}
    payload = client.delete_json(f"/rest/v1/lists/{list_id}/leads.json", json=body)
    return _batch_envelope(payload)


def check_setup(client: Any, blob_writer: BlobWriter) -> dict[str, Any]:
    """Probe the configured API user's READ-ONLY capabilities; report gaps by name.

    Runs the four safe, side-effect-free read probes in :data:`CHECK_SETUP_PROBES`
    (a trivial ``get_leads`` query, ``describe_lead_fields``, ``list_campaigns``,
    ``list_static_lists``) and classifies any failure via
    :func:`errors.classify_marketo_envelope`. Deliberately does NOT probe any
    write/execute verb (create_or_update_leads, delete_leads, add/remove_leads_
    from_list, trigger_campaign) — there is no way to test those permissions
    without performing the action itself, so ``reads_verified`` is a PARTIAL
    guarantee: it names ``writes_unverified`` explicitly rather than implying
    the whole setup is confirmed ready.
    """
    checks: list[dict[str, Any]] = []
    for verb, label, permission in CHECK_SETUP_PROBES:
        check: dict[str, Any] = {"capability": label, "verb": verb}
        try:
            _run_read_probe(client, verb, blob_writer)
            check["status"] = "ok"
        except MarketoEnvelopeError as exc:
            code, message = classify_marketo_envelope(exc)
            check["status"] = "failed"
            check["error_code"] = code
            check["error_message"] = message
            check["guidance"] = _permission_guidance(code, permission)
        checks.append(check)
    return {
        "reads_verified": all(c["status"] == "ok" for c in checks),
        "checks": checks,
        "writes_unverified": list(CHECK_SETUP_UNVERIFIED_WRITE_VERBS),
        "writes_unverified_note": (
            "These verbs perform a write/execute action, so their Access API "
            "permission cannot be probed without actually running them. A "
            "missing permission surfaces as marketo.permission_denied on "
            "first real use — re-run check_setup after fixing it to confirm "
            "the reads still pass, then retry the write."
        ),
    }


def _run_read_probe(client: Any, verb: str, blob_writer: BlobWriter) -> None:
    """Invoke one read-only probe verb; raises MarketoEnvelopeError on failure."""
    if verb == "describe_lead_fields":
        describe_lead_fields(client, {}, blob_writer)
    elif verb == "get_leads":
        get_leads(client, {"filter_type": "id", "filter_values": ["0"]}, blob_writer)
    elif verb == "list_campaigns":
        list_campaigns(client, {}, blob_writer)
    elif verb == "list_static_lists":
        list_static_lists(client, {}, blob_writer)
    else:  # pragma: no cover — CHECK_SETUP_PROBES is the only caller-controlled source
        raise ValueError(f"unknown check_setup probe verb {verb!r}")


def _permission_guidance(error_code: str, permission: str | None) -> str:
    """Human remediation text for one failed check_setup probe."""
    if error_code == ERROR_PERMISSION_DENIED:
        if permission is not None:
            return (
                f"Add '{permission}' under Admin > Users & Roles > Roles > "
                "<the API role> > Access API, then re-run check_setup."
            )
        return (
            "The Access API Role is missing a permission for this capability "
            "(the exact checkbox is unconfirmed for this operation) — open "
            "Admin > Users & Roles > Roles > <the API role> > Access API and "
            "compare against what similar read verbs need, then re-run "
            "check_setup."
        )
    if error_code == ERROR_PARTITION_ACCESS_DENIED:
        return (
            "The API user isn't assigned to the workspace/partition this "
            "request targets — check Admin > Users & Roles > Users (the "
            "API-only user's partition assignment), NOT the Access API Role "
            "permissions."
        )
    if error_code == ERROR_AUTH_FAILED:
        return (
            "The OAuth client_id/client_secret were rejected — verify the "
            "LaunchPoint custom service's credentials in the marketo_instance "
            "address-book entry; this is a credential problem, not a Role "
            "permission gap."
        )
    return "Unexpected error — inspect error_message for detail."


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _spill_envelope(records: list[Any], blob_writer: BlobWriter, filename: str) -> dict[str, Any]:
    payload = json.dumps(records, default=str).encode("utf-8")
    if len(payload) > INLINE_BYTE_CAP:
        blob_key = blob_writer(payload, filename, MIME_JSON)
        return {"result_blob_key": blob_key, "row_count": len(records), "spilled": True}
    return {"records": records, "row_count": len(records), "spilled": False}


def _batch_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    results = payload.get("result") or []
    results = results if isinstance(results, list) else []
    tallies: dict[str, int] = {}
    for entry in results:
        status = entry.get("status", "unknown") if isinstance(entry, dict) else "unknown"
        tallies[status] = tallies.get(status, 0) + 1
    return {"results": results, "row_count": len(results), "tallies": tallies}


def _require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if isinstance(value, (str, int)) and str(value).strip():
        return str(value)
    raise ValueError(f"'{key}' is required and must be a non-empty string")


def _require_list(params: dict[str, Any], key: str, *, max_len: int) -> list[Any]:
    value = params.get(key)
    if not isinstance(value, list) or not value:
        raise ValueError(f"'{key}' is required and must be a non-empty list")
    if len(value) > max_len:
        raise ValueError(f"'{key}' has {len(value)} entries; the Marketo API caps this call at {max_len}")
    return value
