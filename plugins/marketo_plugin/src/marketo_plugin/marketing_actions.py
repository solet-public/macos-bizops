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
    ACTIVITIES_PATH,
    ACTIVITIES_SPILL_FILENAME,
    ACTIVITY_PAGING_TOKEN_PATH,
    CHECK_SETUP_PROBES,
    CHECK_SETUP_UNVERIFIED_WRITE_VERBS,
    DEFAULT_LEAD_ACTION,
    ERROR_AUTH_FAILED,
    ERROR_PARTITION_ACCESS_DENIED,
    ERROR_PERMISSION_DENIED,
    INLINE_BYTE_CAP,
    LEAD_ACTIONS,
    LEAD_FILTER_TYPES,
    MAX_ACTIVITY_LEAD_IDS,
    MAX_ACTIVITY_TYPE_IDS,
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
    _apply_next_page_token(query, params)
    payload = client.get_json("/rest/v1/leads.json", params=query)
    leads = payload.get("result") or []
    envelope = _spill_envelope(leads if isinstance(leads, list) else [], blob_writer, "get_leads_results.json")
    return _with_paging(envelope, payload)


def get_activities(client: Any, params: dict[str, Any], blob_writer: BlobWriter) -> dict[str, Any]:
    """Read the Marketo activity log — what a lead actually DID, or had done to it.

    This is the read that lets a caller verify what a destructive write caused
    (e.g. whether a ``merge_leads`` call resulted in a Send Email / Send Alert
    activity). Two shapes:

      * ``since_datetime`` (ISO-8601) — mints a paging token for that instant
        and reads the first page from it (two HTTP calls).
      * ``next_page_token`` — continues a prior page (one HTTP call).

    Exactly one is required; Marketo's ``activities.json`` cannot be called
    without a token.

    HONEST SCOPE — this verb is an AFTER-THE-FACT audit, not a pre-flight
    guarantee. It answers "did anything notify a human since T", and it makes a
    dry run on one sacrificial record possible. It CANNOT prove that a future
    merge will stay silent: that depends on which trigger campaigns are active
    and how their filters match, which no activity read can predict. Do not let
    a clean result here be reported as "merges are safe".

    ``more_result`` true means KEEP PAGING even when this page's ``records`` is
    empty — Marketo streams activities in ~300-item pages and an empty page
    mid-stream is normal, not the end of the data.
    """
    query = _activity_query(client, params)
    payload = client.get_json(ACTIVITIES_PATH, params=query)
    activities = payload.get("result") or []
    envelope = _spill_envelope(
        activities if isinstance(activities, list) else [], blob_writer, ACTIVITIES_SPILL_FILENAME
    )
    return _with_paging(envelope, payload)


def _activity_query(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Build activities.json's query: the required token plus optional filters."""
    query: dict[str, Any] = {"nextPageToken": _activity_token(client, params)}
    if params.get("lead_ids") is not None:
        lead_ids = _require_list(params, "lead_ids", max_len=MAX_ACTIVITY_LEAD_IDS)
        query["leadIds"] = ",".join(str(lead_id) for lead_id in lead_ids)
    if params.get("activity_type_ids") is not None:
        type_ids = _require_list(params, "activity_type_ids", max_len=MAX_ACTIVITY_TYPE_IDS)
        query["activityTypeIds"] = ",".join(str(type_id) for type_id in type_ids)
    return query


def _activity_token(client: Any, params: dict[str, Any]) -> str:
    """Resolve the paging token: carried from the caller, or minted from an instant."""
    next_page_token = params.get("next_page_token")
    if isinstance(next_page_token, str) and next_page_token:
        return next_page_token
    since_datetime = params.get("since_datetime")
    if isinstance(since_datetime, str) and since_datetime.strip():
        return _mint_activity_paging_token(client, since_datetime.strip())
    raise ValueError(
        "'get_activities' requires either 'since_datetime' (ISO-8601, mints a "
        "paging token) or 'next_page_token' (continues a prior page)"
    )


def _mint_activity_paging_token(client: Any, since_datetime: str) -> str:
    """Exchange an ISO-8601 instant for the paging token activities.json requires."""
    payload = client.get_json(ACTIVITY_PAGING_TOKEN_PATH, params={"sinceDatetime": since_datetime})
    token = payload.get("nextPageToken")
    if not isinstance(token, str) or not token:
        raise ValueError(
            f"Marketo returned no nextPageToken for since_datetime={since_datetime!r} "
            "(it must be ISO-8601, e.g. 2026-07-28T00:00:00-07:00)"
        )
    return token


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
    """List campaigns, optionally filtered by name and/or program name.

    Marketo caps a single page at 300 campaigns. This verb previously DISCARDED
    the response's own paging fields, so an instance with more than 300
    campaigns silently returned an arbitrary 300-campaign slice — and repeat
    calls could return DIFFERENT slices, which reads as data rather than as
    truncation (Dax Part 20 §20.3: 300 of 19,919). The paging fields are now
    surfaced verbatim, so a caller can both detect truncation (``more_result``)
    and continue (``next_page_token``).
    """
    query: dict[str, Any] = {}
    names = params.get("names")
    if isinstance(names, list) and names:
        query["name"] = [str(n) for n in names]
    program_names = params.get("program_names")
    if isinstance(program_names, list) and program_names:
        query["programName"] = [str(n) for n in program_names]
    _apply_next_page_token(query, params)
    payload = client.get_json("/rest/v1/campaigns.json", params=query or None)
    campaigns = payload.get("result") or []
    envelope = _spill_envelope(
        campaigns if isinstance(campaigns, list) else [], blob_writer, "list_campaigns_results.json"
    )
    return _with_paging(envelope, payload)


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
    """List static lists, optionally filtered by name.

    Same page-cap exposure as :func:`list_campaigns` — Dax framed §20.3 as a
    class, not one verb — so the response's paging fields are surfaced here too.
    """
    query: dict[str, Any] = {}
    names = params.get("names")
    if isinstance(names, list) and names:
        query["name"] = [str(n) for n in names]
    _apply_next_page_token(query, params)
    payload = client.get_json("/rest/v1/lists.json", params=query or None)
    lists_ = payload.get("result") or []
    envelope = _spill_envelope(
        lists_ if isinstance(lists_, list) else [], blob_writer, "list_static_lists_results.json"
    )
    return _with_paging(envelope, payload)


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


def _apply_next_page_token(query: dict[str, Any], params: dict[str, Any]) -> None:
    """Forward a caller-supplied ``next_page_token`` onto the outgoing query.

    Deliberately SELF-GATING: the token is sent only when the caller passes one,
    and a caller can only have one because a prior response returned it. That
    keeps this safe on any endpoint whose paging contract is unconfirmed — we
    echo the vendor's own field back rather than inventing a request parameter
    that might not be supported.
    """
    next_page_token = params.get("next_page_token")
    if isinstance(next_page_token, str) and next_page_token:
        query["nextPageToken"] = next_page_token


def _with_paging(envelope: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Surface Marketo's own paging fields verbatim on a result envelope.

    ``next_page_token`` is None and ``more_result`` False when the endpoint did
    not return them — which is honest ("no continuation offered") and is still
    strictly more information than dropping the fields entirely.
    """
    envelope["next_page_token"] = payload.get("nextPageToken")
    envelope["more_result"] = bool(payload.get("moreResult", False))
    return envelope


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
