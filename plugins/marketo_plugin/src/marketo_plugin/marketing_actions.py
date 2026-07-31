"""Marketo verb implementations — pure functions over a built ``MarketoClient``.

Each function takes an already-built :class:`http_client.MarketoClient` and a
``params`` dict, returning a plain result dict. Blob I/O is injected
(``blob_writer``) for the five verbs whose result can be large (describe,
get_leads, list_activity_types, list_campaigns, list_static_lists). Invalid parameters raise
``ValueError`` (mapped to ``marketo.invalid_params``); ``success: false``
envelopes raise ``MarketoEnvelopeError`` (carrying the payload for the
plugin's classifier).

Batch verbs (create_or_update_leads, delete_leads, add/remove_leads_from_list)
return Marketo's own per-record ``result`` array UNCHANGED plus a computed
tally — a per-record ``status: "skipped"``/``"failed"`` entry is normal data,
not a plugin-level fault, because Marketo's overall ``success`` stays true for
those calls. ``create_or_update_leads`` first refuses a whole batch that names
any intended REST read-only field other than identifiers or documented
default-read echoes, before calling the write endpoint. Only a ``success:
false`` envelope (structural fault — bad batch shape, auth, access) raises
after a write is attempted.

No bulk-extract verb exists (v1 scope): Marketo's async Bulk Extract job API
(create -> enqueue -> poll status -> download file) is a materially different
control-flow shape from every other verb here and is explicitly deferred —
``get_leads`` covers ad-hoc reads with the same inline-or-spill envelope the
other connectors use.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from .constants import (
    ACTIVITIES_PATH,
    ACTIVITIES_SPILL_FILENAME,
    ACTIVITY_PAGING_TOKEN_PATH,
    ACTIVITY_TYPES_PATH,
    ACTIVITY_TYPES_SPILL_FILENAME,
    API_USAGE_PATH,
    CHECK_SETUP_PROBES,
    CHECK_SETUP_UNVERIFIED_WRITE_VERBS,
    DEFAULT_LEAD_ACTION,
    DEFAULT_LEAD_LOOKUP_FIELD,
    DESCRIBE_SPILL_FILENAME,
    ERROR_AUTH_FAILED,
    ERROR_PARTITION_ACCESS_DENIED,
    ERROR_PERMISSION_DENIED,
    GET_LEADS_DEFAULT_FIELDS,
    INLINE_BYTE_CAP,
    LEAD_ACTIONS,
    LEAD_ID_FIELD,
    LEADS_DESCRIBE_PATH,
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

_ISO_8601_FRACTIONAL_SECONDS = re.compile(r"(T\d{2}:\d{2}:\d{2})\.\d+")


def describe_lead_fields(client: Any, _params: dict[str, Any], blob_writer: BlobWriter) -> dict[str, Any]:
    """Fetch the full lead field metadata list (id, displayName, name, dataType, ...)."""
    payload = client.get_json(LEADS_DESCRIBE_PATH)
    fields = payload.get("result") or []
    searchable_fields = payload.get("searchableFields")
    if not isinstance(searchable_fields, list):
        raise ValueError("Marketo describe response omitted searchableFields")
    envelope = _spill_envelope(
        fields if isinstance(fields, list) else [],
        blob_writer,
        DESCRIBE_SPILL_FILENAME,
    )
    envelope["searchable_fields"] = searchable_fields
    return envelope


def list_activity_types(
    client: Any,
    _params: dict[str, Any],
    blob_writer: BlobWriter,
) -> dict[str, Any]:
    """Return the configured instance's authoritative activity type catalog."""
    payload = client.get_json(ACTIVITY_TYPES_PATH)
    records = payload.get("result") or []
    return _spill_envelope(
        records if isinstance(records, list) else [],
        blob_writer,
        ACTIVITY_TYPES_SPILL_FILENAME,
    )


def get_api_usage(client: Any, _params: dict[str, Any]) -> dict[str, Any]:
    """Return current-day REST API call totals and per-user usage."""
    payload = client.get_json(API_USAGE_PATH)
    records = payload.get("result") or []
    if not isinstance(records, list):
        raise ValueError("Marketo API usage response result must be a list")
    if not records:
        return {
            "records": [],
            "row_count": 0,
            "date": None,
            "calls_today": None,
            "users": [],
        }
    current = records[0]
    if not isinstance(current, dict):
        raise ValueError("Marketo API usage response contains a non-object record")
    date = current.get("date")
    total = current.get("total")
    users = current.get("users")
    if not isinstance(date, str) or not date:
        raise ValueError("Marketo API usage response omitted date")
    if not isinstance(total, int) or isinstance(total, bool):
        raise ValueError("Marketo API usage response omitted integer total")
    if not isinstance(users, list):
        raise ValueError("Marketo API usage response omitted users")
    return {
        "records": records,
        "row_count": len(records),
        "date": date,
        "calls_today": total,
        "users": users,
    }


def get_leads(client: Any, params: dict[str, Any], blob_writer: BlobWriter) -> dict[str, Any]:
    """Query leads by filterType/filterValues; optionally restrict returned fields.

    A non-empty ``next_page_token`` is the authoritative continuation signal.
    Marketo can return ``moreResult: false`` on a full page that still carries
    a usable token, so ``more_result`` is normalized from token presence.
    """
    filter_type = _require_str(params, "filter_type")
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
    return _with_token_authoritative_paging(envelope, payload)


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

    PAGING — ``more_result`` is the ONLY USABLE continuation signal here, which
    is not the same as a verified reliable one. Adobe documents that this
    endpoint ALWAYS returns ``nextPageToken`` (Developer Guide → REST → Lead
    Database → Activities), so token presence cannot terminate the loop — the
    exact inverse of :func:`_with_token_authoritative_paging`'s verbs, which
    ignore the vendor flag because there the token IS the honest signal. Page
    until ``more_result`` is false. Adobe also states the endpoint "can return
    fewer than 300 activity items while setting ``moreResult`` to true", so a
    SHORT page — including an empty one — does not mean the end of the data.

    ⚠ UNMEASURED, and this is the open risk: the flag's reliability on THIS
    endpoint has never been checked against a live instance. The only live
    measurement of ``moreResult`` anywhere found it VIOLATED on
    ``list_campaigns`` — a full 300-record page reporting false while carrying
    a usable token (field-verified against a live instance). On the
    token-authoritative verbs that
    is survivable because token presence is a valid fallback; here there is NO
    fallback, so if the flag under-reports, an activity read truncates
    silently and no caller-side rule can detect it. The hermetic smokes assert
    that this code implements the documented rule; they cannot and do not show
    that Marketo honours it.
    """
    query = _activity_query(client, params)
    payload = client.get_json(ACTIVITIES_PATH, params=query)
    activities = payload.get("result") or []
    envelope = _spill_envelope(
        activities if isinstance(activities, list) else [], blob_writer, ACTIVITIES_SPILL_FILENAME
    )
    return _with_activity_paging(envelope, payload)


def _activity_query(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Build activities.json's query with both server-required arguments."""
    type_ids = _require_list(
        params,
        "activity_type_ids",
        max_len=MAX_ACTIVITY_TYPE_IDS,
    )
    query: dict[str, Any] = {
        "nextPageToken": _activity_token(client, params),
        "activityTypeIds": ",".join(str(type_id) for type_id in type_ids),
    }
    if params.get("lead_ids") is not None:
        lead_ids = _require_list(params, "lead_ids", max_len=MAX_ACTIVITY_LEAD_IDS)
        query["leadIds"] = ",".join(str(lead_id) for lead_id in lead_ids)
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
    """Exchange a whole-second ISO-8601 instant for an activity paging token."""
    wire_since_datetime = _ISO_8601_FRACTIONAL_SECONDS.sub(
        r"\1",
        since_datetime,
        count=1,
    )
    payload = client.get_json(
        ACTIVITY_PAGING_TOKEN_PATH,
        params={"sinceDatetime": wire_since_datetime},
    )
    token = payload.get("nextPageToken")
    if not isinstance(token, str) or not token:
        raise ValueError(
            f"Marketo returned no nextPageToken for since_datetime={since_datetime!r} "
            "(it must be ISO-8601, e.g. 2026-07-28T00:00:00-07:00)"
        )
    return token


def create_or_update_leads(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Create/update leads after refusing intended REST read-only fields."""
    action = params.get("action") or DEFAULT_LEAD_ACTION
    if action not in LEAD_ACTIONS:
        raise ValueError(f"'action' must be one of {sorted(LEAD_ACTIONS)}, got {action!r}")
    records = _require_lead_records(params)
    lookup_field = _optional_lookup_field(params)
    _refuse_read_only_lead_fields(
        client,
        records,
        lookup_field or DEFAULT_LEAD_LOOKUP_FIELD,
    )
    body: dict[str, Any] = {"action": action, "input": records}
    if lookup_field is not None:
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
    """Merge up to 25 losing leads into one winning lead.

    ``merge_in_crm=True`` also merges the natively-synced CRM records — Marketo
    itself restricts a CRM merge to exactly ONE losing lead per call (not 25),
    so that cap is enforced here rather than left to a server-side 1080.
    Read-only fields retain the winner's value even when it is empty; they are
    not populated from a losing record under the general merge-precedence rule.
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
    truncation across tens of thousands of campaigns. A non-empty
    ``next_page_token`` is the authoritative continuation signal:
    ``more_result`` is normalized to true whenever that token is present,
    including when Marketo's raw ``moreResult`` flag incorrectly says false.
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
    return _with_token_authoritative_paging(envelope, payload)


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

    Same page-cap exposure as :func:`list_campaigns` — it is a class of
    defect, not one verb. A non-empty ``next_page_token`` is authoritative, so
    ``more_result`` is normalized from token presence rather than Marketo's
    unreliable raw flag.
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
    return _with_token_authoritative_paging(envelope, payload)


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

    Runs the six safe, side-effect-free read probes in :data:`CHECK_SETUP_PROBES`
    (lead describe/query, activity-type listing, API usage, campaign listing,
    and static-list listing) and classifies any failure via
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
    elif verb == "list_activity_types":
        list_activity_types(client, {}, blob_writer)
    elif verb == "get_api_usage":
        get_api_usage(client, {})
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


def _with_token_authoritative_paging(
    envelope: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Normalize collection paging from the usable continuation token.

    Used by ``get_leads``, ``list_campaigns``, and ``list_static_lists``:
    Marketo's raw ``moreResult`` can be false while ``nextPageToken`` still
    continues to another page, so token presence wins for these verbs.
    """
    next_page_token = payload.get("nextPageToken")
    envelope["next_page_token"] = next_page_token
    envelope["more_result"] = bool(next_page_token)
    return envelope


def _with_activity_paging(
    envelope: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Surface activity paging with Marketo's ``moreResult`` flag authoritative.

    The activity token is a resumable bookmark that can be returned on the last
    page too. Token presence therefore cannot determine whether to continue;
    callers must keep paging through empty pages while ``more_result`` is true
    and stop when it is false, even if ``next_page_token`` remains populated.
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


def _require_lead_records(params: dict[str, Any]) -> list[dict[str, Any]]:
    """Return validated lead-record objects with their value types preserved."""
    records = _require_list(params, "records", max_len=MAX_BATCH_RECORDS)
    if any(not isinstance(record, dict) or not record for record in records):
        raise ValueError("every entry in 'records' must be a non-empty object")
    return records


def _optional_lookup_field(params: dict[str, Any]) -> str | None:
    """Validate the optional lookup field; omission selects Marketo's email default."""
    lookup_field = params.get("lookup_field")
    if lookup_field is None:
        return None
    if isinstance(lookup_field, str) and (normalized := lookup_field.strip()):
        return normalized
    raise ValueError("'lookup_field' must be a non-empty string if provided")


def _refuse_read_only_lead_fields(
    client: Any,
    records: list[dict[str, Any]],
    effective_lookup_field: str,
) -> None:
    """Refuse intended read-only writes while allowing keys and default-read echoes."""
    read_only_fields = _read_only_rest_field_names(client)
    key_fields = {effective_lookup_field, LEAD_ID_FIELD}
    echoed_read_only_fields = read_only_fields & (GET_LEADS_DEFAULT_FIELDS - key_fields)
    excluded_fields = key_fields | echoed_read_only_fields
    offending_records: dict[str, list[int]] = {}
    for record_number, record in enumerate(records, start=1):
        for field_name in record:
            if field_name in read_only_fields and field_name not in excluded_fields:
                offending_records.setdefault(field_name, []).append(record_number)
    if not offending_records:
        return
    details = "; ".join(
        f"{field_name} (records {', '.join(str(number) for number in offending_records[field_name])})"
        for field_name in sorted(offending_records)
    )
    raise ValueError(
        "Marketo REST read-only fields cannot be written: "
        f"{details}. Remove every listed field and retry; no write was attempted."
    )


def _read_only_rest_field_names(client: Any) -> set[str]:
    """Read one describe page and return fields explicitly marked REST read-only.

    Missing ``rest`` or ``rest.readOnly`` metadata is deliberately treated as
    writable: absence is not evidence that Marketo will refuse the field.
    Present-but-malformed metadata still fails loudly.
    """
    payload = client.get_json(LEADS_DESCRIBE_PATH)
    fields = payload.get("result")
    if not isinstance(fields, list):
        raise ValueError("Marketo describe response omitted the lead field list")
    read_only_fields: set[str] = set()
    for entry in fields:
        field_name = _read_only_rest_field_name(entry)
        if field_name is not None:
            read_only_fields.add(field_name)
    return read_only_fields


def _read_only_rest_field_name(entry: Any) -> str | None:
    """Validate one describe entry and return its read-only REST name, if any."""
    if not isinstance(entry, dict):
        raise ValueError("Marketo describe response contained a non-object field entry")
    rest = entry.get("rest")
    if rest is None:
        return None
    if not isinstance(rest, dict):
        raise ValueError("Marketo describe response contained malformed REST field metadata")
    read_only = rest.get("readOnly")
    if read_only is None:
        return None
    if not isinstance(read_only, bool):
        raise ValueError("Marketo describe response contained a non-boolean rest.readOnly marker")
    if not read_only:
        return None
    field_name = rest.get("name")
    if not isinstance(field_name, str) or not field_name:
        raise ValueError("Marketo describe response marked an unnamed REST field read-only")
    return field_name


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
