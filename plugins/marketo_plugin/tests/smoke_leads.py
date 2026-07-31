#!/usr/bin/env python3
"""Lead verb + envelope-classification smoke tests for marketo_plugin.

Hermetic — a faked client returning canned decoded envelope dicts (the shape
``http_client.MarketoClient.get_json``/``post_json``/``delete_json`` already
return), no live instance.

Exercises:
  1. describe_lead_fields — inline vs spill envelope shape plus the
     instance-specific searchable_fields catalog
  2. get_leads — instance-specific filter_type forwarding, paging fields
     carried, spill trigger
  3. create_or_update_leads — one describe preflight per batch, whole-batch
     refusal naming every REST read-only non-key field and its records,
     lookup-key anti-brick controls, absent-metadata fail-open, plus vendor
     results/reasons/tallies unchanged after an accepted write
  4. delete_leads — batch cap enforced, tallies shape
  5. merge_leads — losing_lead_ids cap (25, or 1 when merge_in_crm=true),
     mergeInCRM query param only sent when explicitly set, success/request_id
     shape
  6. check_setup — all-pass shape (reads_verified=true, writes_unverified
     names the 6 unprobed write/execute verbs), and a mixed pass/fail shape
     where a 603 on one probe surfaces marketo.permission_denied + guidance
     naming the exact missing Access API permission
  7. classify_marketo_envelope — the sourced code -> our-error-code map for a
     representative spread (601 auth, 603 permission_denied, 606
     rate-limited, 702 not-found, 1005 validation-failed, 1008
     partition_access_denied), and that the message is built from Marketo's
     own errors[].message text
  8. list_activity_types and get_api_usage — per-instance activity ids and
     current-day API consumption are exposed through pure reads
  9. EDGE parity: validate_edge_process_provider raises nothing

Run:
    HOMUNCULUS_NAME=<name> .venv/bin/python3 \
        plugins/marketo_plugin/tests/smoke_leads.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "marketo_plugin" / "src"))

from marketo_plugin import marketing_actions  # noqa: E402
from marketo_plugin.constants import GET_LEADS_DEFAULT_FIELDS  # noqa: E402
from marketo_plugin.errors import MarketoEnvelopeError, classify_marketo_envelope  # noqa: E402
from marketo_plugin.plugin import MarketoPlugin  # noqa: E402

_passed = 0
_failed: list[str] = []


def _assert(label: str, cond: bool, msg: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}: {msg or 'assertion failed'}")


def _fake_client(**method_returns: dict[str, Any]) -> Any:
    client = MagicMock()
    for method, payload in method_returns.items():
        getattr(client, method).return_value = payload
    return client


def _no_blob_writer(_content: bytes, _filename: str, _mime: str) -> str:
    raise AssertionError("blob_writer should not be called for small results")


def test_describe_lead_fields_inline() -> None:
    client = _fake_client(
        get_json={
            "success": True,
            "result": [
                {
                    "id": 1,
                    "displayName": "Email Address",
                    "dataType": "string",
                    "rest": {"name": "email", "readOnly": False},
                },
            ],
            "searchableFields": ["email", "externalDedupeKey"],
        },
    )
    result = marketing_actions.describe_lead_fields(client, {}, _no_blob_writer)
    _assert("describe inline REST field name carried", result["records"][0]["rest"]["name"] == "email")
    _assert("describe inline REST readOnly marker carried", result["records"][0]["rest"]["readOnly"] is False)
    _assert(
        "describe searchable_fields carried",
        result["searchable_fields"] == ["email", "externalDedupeKey"],
        str(result),
    )
    _assert("describe row_count", result["row_count"] == 1)
    _assert("describe not spilled", result["spilled"] is False)


def test_get_leads_filter_forwarding() -> None:
    client = _fake_client(
        get_json={
            "success": True,
            "result": [{"id": 1, "email": "a@b.com"}],
            "nextPageToken": "tok-2",
            "moreResult": False,
        }
    )
    result = marketing_actions.get_leads(
        client,
        {
            "filter_type": "externalDedupeKey",
            "filter_values": ["a@b.com"],
        },
        _no_blob_writer,
    )
    _assert("get_leads records carried", result["records"][0]["email"] == "a@b.com")
    _assert("get_leads next_page_token carried", result["next_page_token"] == "tok-2")
    _assert("get_leads token overrides false vendor moreResult", result["more_result"] is True)
    _assert(
        "instance-specific filter_type forwarded",
        client.get_json.call_args.kwargs["params"]["filterType"]
        == "externalDedupeKey",
        str(client.get_json.call_args),
    )

    terminal_client = _fake_client(
        get_json={"success": True, "result": [{"id": 2}], "moreResult": True}
    )
    terminal = marketing_actions.get_leads(
        terminal_client,
        {"filter_type": "id", "filter_values": ["2"]},
        _no_blob_writer,
    )
    _assert("get_leads missing token overrides true vendor moreResult", terminal["more_result"] is False)

    raised = False
    try:
        marketing_actions.get_leads(client, {"filter_type": "id", "filter_values": [str(i) for i in range(301)]}, _no_blob_writer)
    except ValueError:
        raised = True
    _assert("over-cap filter_values rejected", raised)


def test_create_or_update_leads_partial_batch_is_not_an_error() -> None:
    client = _fake_client(
        get_json={"success": True, "result": []},
        post_json={
            "success": True,
            "result": [
                {"id": 111, "status": "updated"},
                {
                    "status": "skipped",
                    "reasons": [{"code": "1006", "message": "Field 'emali' not found"}],
                },
            ],
        }
    )
    result = marketing_actions.create_or_update_leads(
        client,
        {
            "records": [
                {"email": "a@b.com"},
                {"email": "c@d.com", "emali": "c@d.com"},
            ],
            "action": "createOnly",
        },
    )
    _assert(
        "results array passed through vendor-verbatim",
        result["results"] == client.post_json.return_value["result"],
        str(result),
    )
    _assert(
        "unknown-field reasons survive vendor-verbatim",
        result["results"][1]["reasons"]
        == [{"code": "1006", "message": "Field 'emali' not found"}],
        str(result),
    )
    _assert("result row_count preserved", result["row_count"] == 2)
    _assert("tallies computed", result["tallies"] == {"updated": 1, "skipped": 1})
    _assert(
        "accepted result has no synthesized warning channel",
        set(result) == {"results", "row_count", "tallies"},
        str(result),
    )
    _assert("accepted write makes one describe call", client.get_json.call_count == 1)
    _assert("accepted write makes one post call", client.post_json.call_count == 1)

    raised = False
    try:
        marketing_actions.create_or_update_leads(client, {"records": [{"email": "a@b.com"}], "action": "notARealAction"})
    except ValueError:
        raised = True
    _assert("invalid action rejected", raised)

    raised = False
    try:
        marketing_actions.create_or_update_leads(client, {"records": []})
    except ValueError:
        raised = True
    _assert("empty records rejected", raised)


def test_documented_get_leads_default_fields_contract() -> None:
    """Pin our exclusion contract; passing is not evidence Marketo marks members read-only."""
    _assert(
        "documented Get Leads default field membership is exact",
        GET_LEADS_DEFAULT_FIELDS
        == frozenset(
            {
                "id",
                "email",
                "updatedAt",
                "createdAt",
                "firstName",
                "lastName",
            }
        ),
        str(sorted(GET_LEADS_DEFAULT_FIELDS)),
    )


def test_create_or_update_leads_read_only_preflight() -> None:
    refused_client = _fake_client(
        get_json={
            "success": True,
            "result": [
                {"rest": {"name": "id", "readOnly": True}},
                {"rest": {"name": "readOnlyPersonSource", "readOnly": True}},
            ],
        },
        post_json={"success": True, "result": []},
    )
    refusal = ""
    try:
        marketing_actions.create_or_update_leads(
            refused_client,
            {
                "records": [{"id": 101, "readOnlyPersonSource": "partner"}],
                "action": "updateOnly",
                "lookup_field": "id",
            },
        )
    except ValueError as exc:
        refusal = str(exc)
    _assert("read-only non-key field refused", bool(refusal), refusal)
    _assert(
        "refusal names only the offending field and record",
        "readOnlyPersonSource (records 1)" in refusal and "id (" not in refusal,
        refusal,
    )
    _assert("refusal makes exactly one describe call", refused_client.get_json.call_count == 1)
    _assert("refusal happens before the write", refused_client.post_json.call_count == 0)

    writable_client = _fake_client(
        get_json={
            "success": True,
            "result": [
                {"rest": {"name": "id", "readOnly": True}},
                {"rest": {"name": "firstName", "readOnly": False}},
            ],
        },
        post_json={"success": True, "result": [{"id": 101, "status": "updated"}]},
    )
    accepted = marketing_actions.create_or_update_leads(
        writable_client,
        {
            "records": [{"id": 101, "firstName": "Example"}],
            "action": "updateOnly",
            "lookup_field": "id",
        },
    )
    _assert("lookup_field=id writable batch accepted", accepted["tallies"] == {"updated": 1})
    _assert("lookup_field=id accepted after one describe", writable_client.get_json.call_count == 1)
    _assert("lookup_field=id reaches write once", writable_client.post_json.call_count == 1)

    echoed_defaults_client = _fake_client(
        get_json={
            "success": True,
            "result": [
                {"rest": {"name": "id", "readOnly": True}},
                {"rest": {"name": "email", "readOnly": False}},
                {"rest": {"name": "updatedAt", "readOnly": True}},
                {"rest": {"name": "createdAt", "readOnly": True}},
                {"rest": {"name": "firstName", "readOnly": False}},
                {"rest": {"name": "lastName", "readOnly": False}},
            ],
        },
        post_json={"success": True, "result": [{"id": 101, "status": "updated"}]},
    )
    echoed_record = {
        "id": 101,
        "email": "example@example.com",
        "updatedAt": "2026-07-30T00:00:00Z",
        "createdAt": "2026-07-29T00:00:00Z",
        "firstName": "Example",
        "lastName": "Lovelace",
    }
    echoed_defaults = marketing_actions.create_or_update_leads(
        echoed_defaults_client,
        {"records": [echoed_record], "action": "updateOnly"},
    )
    echoed_body = echoed_defaults_client.post_json.call_args.kwargs["json"]
    _assert(
        "live-read-only documented default echoes do not brick read-modify-write",
        echoed_defaults["tallies"] == {"updated": 1},
        str(echoed_defaults),
    )
    _assert(
        "default echoes remain vendor-controlled in the outbound record",
        echoed_body["input"] == [echoed_record],
        str(echoed_body),
    )
    _assert(
        "default-echo acceptance makes one describe call",
        echoed_defaults_client.get_json.call_count == 1,
    )
    _assert(
        "default-echo acceptance reaches the write once",
        echoed_defaults_client.post_json.call_count == 1,
    )

    heterogeneous_client = _fake_client(
        get_json={
            "success": True,
            "result": [
                {"rest": {"name": "readOnlyAlpha", "readOnly": True}},
                {"rest": {"name": "readOnlyBeta", "readOnly": True}},
            ],
        },
        post_json={"success": True, "result": []},
    )
    heterogeneous_refusal = ""
    try:
        marketing_actions.create_or_update_leads(
            heterogeneous_client,
            {
                "records": [
                    {"id": 101, "readOnlyAlpha": "a"},
                    {"id": 202, "readOnlyBeta": "b"},
                ],
                "action": "updateOnly",
                "lookup_field": "id",
            },
        )
    except ValueError as exc:
        heterogeneous_refusal = str(exc)
    _assert(
        "heterogeneous refusal names every field",
        "readOnlyAlpha" in heterogeneous_refusal and "readOnlyBeta" in heterogeneous_refusal,
        heterogeneous_refusal,
    )
    _assert(
        "heterogeneous refusal identifies carrying records",
        "readOnlyAlpha (records 1)" in heterogeneous_refusal
        and "readOnlyBeta (records 2)" in heterogeneous_refusal,
        heterogeneous_refusal,
    )
    _assert("heterogeneous refusal prevents the write", heterogeneous_client.post_json.call_count == 0)

    absent_rest_client = _fake_client(
        get_json={
            "success": True,
            "result": [
                {"id": 7, "displayName": "Legacy Field", "dataType": "string"},
                {"id": 8, "rest": {"name": "restWithoutMarker"}},
            ],
        },
        post_json={"success": True, "result": [{"id": 101, "status": "updated"}]},
    )
    absent_rest = marketing_actions.create_or_update_leads(
        absent_rest_client,
        {
            "records": [{"id": 101, "legacyField": "value", "restWithoutMarker": "value"}],
            "action": "updateOnly",
            "lookup_field": "id",
        },
    )
    _assert("absent rest or readOnly marker is treated writable", absent_rest["row_count"] == 1)
    _assert("absent-metadata case reaches the write", absent_rest_client.post_json.call_count == 1)

    implicit_keys_client = _fake_client(
        get_json={
            "success": True,
            "result": [
                {"rest": {"name": "email", "readOnly": True}},
                {"rest": {"name": "id", "readOnly": True}},
                {"rest": {"name": "firstName", "readOnly": False}},
            ],
        },
        post_json={"success": True, "result": [{"id": 303, "status": "updated"}]},
    )
    implicit_keys = marketing_actions.create_or_update_leads(
        implicit_keys_client,
        {
            "records": [{"email": "example@example.com", "id": 303, "firstName": "Example"}],
            "action": "updateOnly",
        },
    )
    body = implicit_keys_client.post_json.call_args.kwargs["json"]
    _assert("default email and bare id are identifier fields", implicit_keys["row_count"] == 1)
    _assert("omitted lookup_field stays omitted from vendor body", "lookupField" not in body, str(body))


def test_delete_leads_batch_cap() -> None:
    client = _fake_client(post_json={"success": True, "result": [{"id": 1, "status": "deleted"}]})
    result = marketing_actions.delete_leads(client, {"lead_ids": [1]})
    _assert("delete tallies shape", result["tallies"] == {"deleted": 1})

    raised = False
    try:
        marketing_actions.delete_leads(client, {"lead_ids": list(range(301))})
    except ValueError:
        raised = True
    _assert("over-cap lead_ids rejected", raised)


def test_merge_leads() -> None:
    client = _fake_client(post_json={"success": True, "requestId": "req-merge-1"})
    result = marketing_actions.merge_leads(client, {"winning_lead_id": "1", "losing_lead_ids": [2, 3]})
    _assert("merge success carried", result["success"] is True)
    _assert("merge request_id carried", result["request_id"] == "req-merge-1")
    query = client.post_json.call_args.kwargs.get("params")
    _assert("leadIds comma-joined, no mergeInCRM when unset", query == {"leadIds": "2,3"}, str(query))

    client.post_json.reset_mock()
    marketing_actions.merge_leads(client, {"winning_lead_id": "1", "losing_lead_ids": [2], "merge_in_crm": True})
    query = client.post_json.call_args.kwargs.get("params")
    _assert("mergeInCRM sent as lowercase string when explicitly set", query == {"leadIds": "2", "mergeInCRM": "true"}, str(query))

    raised = False
    try:
        marketing_actions.merge_leads(client, {"winning_lead_id": "1", "losing_lead_ids": [2, 3], "merge_in_crm": True})
    except ValueError:
        raised = True
    _assert("merge_in_crm=true caps losing_lead_ids at 1", raised)

    raised = False
    try:
        marketing_actions.merge_leads(client, {"winning_lead_id": "1", "losing_lead_ids": list(range(26))})
    except ValueError:
        raised = True
    _assert("over-cap losing_lead_ids (>25) rejected", raised)

    raised = False
    try:
        marketing_actions.merge_leads(client, {"winning_lead_id": "1", "losing_lead_ids": [2], "merge_in_crm": "not-a-bool"})
    except ValueError:
        raised = True
    _assert("non-boolean merge_in_crm rejected", raised)


def test_check_setup_all_pass() -> None:
    client = _fake_client(
        get_json={
            "success": True,
            "result": [],
            "searchableFields": ["id"],
        },
    )
    result = marketing_actions.check_setup(client, _no_blob_writer)
    _assert("check_setup all-pass reads_verified", result["reads_verified"] is True)
    _assert("check_setup ran all 6 read probes", len(result["checks"]) == 6, str(result["checks"]))
    _assert(
        "check_setup names the 6 unverified write verbs",
        set(result["writes_unverified"])
        == {"create_or_update_leads", "delete_leads", "merge_leads", "add_leads_to_list", "remove_leads_from_list", "trigger_campaign"},
        str(result["writes_unverified"]),
    )


def test_check_setup_mixed_pass_fail() -> None:
    # The real MarketoClient._request_json raises MarketoEnvelopeError on a
    # success:false envelope (see http_client.py) — marketing_actions functions
    # never inspect "success" themselves, so the fake client must raise here
    # too, exactly like the real one would, not just return a falsy dict.
    call_count = {"n": 0}

    def get_json_side_effect(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        call_count["n"] += 1
        if call_count["n"] == 1:  # describe_lead_fields fails with 603
            raise MarketoEnvelopeError({"success": False, "errors": [{"code": "603", "message": "Access denied"}]})
        return {
            "success": True,
            "result": [],
            "searchableFields": ["id"],
        }

    client = MagicMock()
    client.get_json.side_effect = get_json_side_effect
    result = marketing_actions.check_setup(client, _no_blob_writer)
    _assert("check_setup mixed reads_verified false", result["reads_verified"] is False)
    failed = [c for c in result["checks"] if c["status"] == "failed"]
    _assert("exactly one failed probe", len(failed) == 1, str(failed))
    _assert("failed probe classified as permission_denied", failed[0]["error_code"] == "marketo.permission_denied", str(failed[0]))
    _assert("guidance names the Read-Only Person permission", "Read-Only Person" in failed[0]["guidance"], failed[0]["guidance"])


def test_classify_marketo_envelope() -> None:
    auth_exc = MarketoEnvelopeError({"success": False, "errors": [{"code": "601", "message": "Access token invalid"}]})
    code, message = classify_marketo_envelope(auth_exc)
    _assert("601 -> marketo.auth_failed", code == "marketo.auth_failed")
    _assert("601 message carries Marketo detail", "Access token invalid" in message, message)

    permission_exc = MarketoEnvelopeError({"success": False, "errors": [{"code": "603", "message": "Access denied"}]})
    code, message = classify_marketo_envelope(permission_exc)
    _assert("603 -> marketo.permission_denied (NOT auth_failed)", code == "marketo.permission_denied")

    partition_exc = MarketoEnvelopeError({"success": False, "errors": [{"code": "1008", "message": "No access to partition"}]})
    code, message = classify_marketo_envelope(partition_exc)
    _assert("1008 -> marketo.partition_access_denied (NOT auth_failed)", code == "marketo.partition_access_denied")

    rate_exc = MarketoEnvelopeError({"success": False, "errors": [{"code": "606", "message": "Max rate limit exceeded"}]})
    code, message = classify_marketo_envelope(rate_exc)
    _assert("606 -> marketo.rate_limited", code == "marketo.rate_limited")

    not_found_exc = MarketoEnvelopeError({"success": False, "errors": [{"code": "702", "message": "No lead found"}]})
    code, message = classify_marketo_envelope(not_found_exc)
    _assert("702 -> marketo.object_not_found", code == "marketo.object_not_found")

    validation_exc = MarketoEnvelopeError({"success": False, "errors": [{"code": "1005", "message": "Lead already exists"}]})
    code, message = classify_marketo_envelope(validation_exc)
    _assert("1005 -> marketo.validation_failed", code == "marketo.validation_failed")

    merge_cap_exc = MarketoEnvelopeError({"success": False, "errors": [{"code": "1080", "message": "Too many leadIds"}]})
    code, message = classify_marketo_envelope(merge_cap_exc)
    _assert("1080 -> marketo.invalid_params", code == "marketo.invalid_params")

    unknown_exc = MarketoEnvelopeError({"success": False, "errors": [{"code": "9999", "message": "something new"}]})
    code, message = classify_marketo_envelope(unknown_exc)
    _assert("unmapped code falls back to marketo.api_error", code == "marketo.api_error")


def test_get_activities() -> None:
    """§20.2 — the read that verifies what a destructive write actually caused."""
    # since_datetime path: mints a paging token, THEN reads activities.
    client = MagicMock()
    client.get_json.side_effect = [
        {"success": True, "nextPageToken": "tok-mint"},
        {
            "success": True,
            "result": [{"id": 9, "leadId": 42, "activityTypeId": 6, "activityDate": "2026-07-28T12:00:00Z"}],
            "nextPageToken": "tok-next",
            "moreResult": True,
        },
    ]
    result = marketing_actions.get_activities(
        client,
        {"since_datetime": "2026-07-28T00:00:00-07:00", "lead_ids": [42], "activity_type_ids": [6, 7]},
        _no_blob_writer,
    )
    _assert("activities records carried", result["records"][0]["activityTypeId"] == 6)
    _assert("activities next_page_token carried", result["next_page_token"] == "tok-next")
    _assert("activities more_result carried", result["more_result"] is True)
    token_call, activities_call = client.get_json.call_args_list
    _assert("paging token minted from since_datetime", token_call.kwargs["params"]["sinceDatetime"] == "2026-07-28T00:00:00-07:00")
    _assert("minted token used for the activities read", activities_call.kwargs["params"]["nextPageToken"] == "tok-mint")
    _assert("lead_ids filter sent server-side", activities_call.kwargs["params"]["leadIds"] == "42")
    _assert("activity_type_ids filter sent server-side", activities_call.kwargs["params"]["activityTypeIds"] == "6,7")

    # next_page_token path: continues WITHOUT re-minting (one call, not two).
    client = _fake_client(
        get_json={
            "success": True,
            "result": [],
            "nextPageToken": "tok-bookmark",
            "moreResult": False,
        }
    )
    result = marketing_actions.get_activities(
        client,
        {"next_page_token": "tok-carry", "activity_type_ids": [6]},
        _no_blob_writer,
    )
    _assert("continuation makes exactly one call", client.get_json.call_count == 1)
    _assert("continuation reuses caller token", client.get_json.call_args.kwargs["params"]["nextPageToken"] == "tok-carry")
    _assert("empty page still reports row_count 0", result["row_count"] == 0)
    _assert("activity final-page bookmark is surfaced", result["next_page_token"] == "tok-bookmark")
    _assert("activity false moreResult stops despite bookmark", result["more_result"] is False)

    empty_midstream_client = _fake_client(
        get_json={
            "success": True,
            "result": [],
            "nextPageToken": "tok-after-empty",
            "moreResult": True,
        }
    )
    empty_midstream = marketing_actions.get_activities(
        empty_midstream_client,
        {"next_page_token": "tok-before-empty", "activity_type_ids": [6]},
        _no_blob_writer,
    )
    _assert("activity empty mid-stream page remains incomplete", empty_midstream["more_result"] is True)

    # activityTypeIds is mandatory in Marketo, not an optional filter.
    raised = False
    try:
        marketing_actions.get_activities(
            _fake_client(get_json={"success": True}),
            {"next_page_token": "tok-carry"},
            _no_blob_writer,
        )
    except ValueError:
        raised = True
    _assert("missing mandatory activity_type_ids rejected", raised)

    # Neither param -> refuse rather than call an endpoint that cannot work.
    raised = False
    try:
        marketing_actions.get_activities(_fake_client(get_json={"success": True}), {}, _no_blob_writer)
    except ValueError:
        raised = True
    _assert("missing since_datetime AND next_page_token rejected", raised)

    # Marketo's own server-side caps enforced here, not left to a 1003.
    raised = False
    try:
        marketing_actions.get_activities(
            _fake_client(get_json={"success": True}),
            {"next_page_token": "t", "lead_ids": list(range(31))},
            _no_blob_writer,
        )
    except ValueError:
        raised = True
    _assert("over-cap lead_ids (>30) rejected", raised)

    raised = False
    try:
        marketing_actions.get_activities(
            _fake_client(get_json={"success": True}),
            {"next_page_token": "t", "activity_type_ids": list(range(11))},
            _no_blob_writer,
        )
    except ValueError:
        raised = True
    _assert("over-cap activity_type_ids (>10) rejected", raised)

    # A pagingtoken response with no token must fail loudly, not read page 1 of nothing.
    raised = False
    try:
        marketing_actions.get_activities(
            _fake_client(get_json={"success": True}), {"since_datetime": "nonsense"}, _no_blob_writer
        )
    except ValueError:
        raised = True
    _assert("missing minted token rejected", raised)


def test_get_activities_fractional_seconds_contract() -> None:
    """Pin OUR whole-second floor contract, not Marketo's runtime behaviour."""
    cases = (
        (
            "2026-07-30T18:00:00.999999+00:00",
            "2026-07-30T18:00:00+00:00",
        ),
        (
            "2026-07-30T18:00:00.123456Z",
            "2026-07-30T18:00:00Z",
        ),
    )
    for supplied_since_datetime, expected_wire_datetime in cases:
        client = MagicMock()
        client.get_json.side_effect = [
            {"success": True, "nextPageToken": "tok-mint"},
            {
                "success": True,
                "result": [],
                "nextPageToken": "tok-bookmark",
                "moreResult": False,
            },
        ]

        marketing_actions.get_activities(
            client,
            {
                "since_datetime": supplied_since_datetime,
                "activity_type_ids": [1],
            },
            _no_blob_writer,
        )

        mint_call = client.get_json.call_args_list[0]
        actual_wire_datetime = mint_call.kwargs["params"]["sinceDatetime"]
        _assert(
            f"fractional since_datetime floors to its whole second: {supplied_since_datetime}",
            actual_wire_datetime == expected_wire_datetime,
            str(actual_wire_datetime),
        )


def test_list_activity_types_and_api_usage() -> None:
    client = _fake_client(
        get_json={
            "success": True,
            "result": [
                {
                    "id": 6,
                    "name": "Send Email",
                    "primaryAttribute": {
                        "name": "Mailing ID",
                        "dataType": "integer",
                    },
                    "attributes": [],
                },
            ],
        },
    )
    result = marketing_actions.list_activity_types(
        client,
        {},
        _no_blob_writer,
    )
    _assert("activity type records carried", result["records"][0]["id"] == 6)
    _assert(
        "activity types use the authoritative endpoint",
        client.get_json.call_args.args[0] == "/rest/v1/activities/types.json",
        str(client.get_json.call_args),
    )
    usage_client = _fake_client(
        get_json={
            "success": True,
            "result": [
                {
                    "date": "2026-07-29",
                    "total": 15232,
                    "users": [
                        {
                            "userId": "integration@example.com",
                            "count": 15232,
                        },
                    ],
                },
            ],
        },
    )
    result = marketing_actions.get_api_usage(usage_client, {})
    _assert("usage records carried", result["records"][0]["total"] == 15232)
    _assert("usage calls_today surfaced", result["calls_today"] == 15232)
    _assert(
        "usage reads the current-day stats endpoint",
        usage_client.get_json.call_args.args[0] == "/rest/v1/stats/usage.json",
        str(usage_client.get_json.call_args),
    )


def test_list_paging_fields_surfaced() -> None:
    """§20.3 — list_* verbs must expose truncation instead of silently slicing."""
    client = _fake_client(
        get_json={
            "success": True,
            "result": [{"id": 1, "name": "c1"}],
            "nextPageToken": "camp-2",
            "moreResult": False,
        }
    )
    result = marketing_actions.list_campaigns(client, {}, _no_blob_writer)
    _assert("list_campaigns next_page_token surfaced", result["next_page_token"] == "camp-2")
    _assert("list_campaigns token overrides false vendor moreResult", result["more_result"] is True)

    result = marketing_actions.list_campaigns(client, {"next_page_token": "camp-2"}, _no_blob_writer)
    _assert("list_campaigns forwards caller token", client.get_json.call_args.kwargs["params"]["nextPageToken"] == "camp-2")

    lists_client = _fake_client(
        get_json={
            "success": True,
            "result": [{"id": 5}],
            "nextPageToken": "l-2",
            "moreResult": False,
        }
    )
    result = marketing_actions.list_static_lists(lists_client, {}, _no_blob_writer)
    _assert("list_static_lists next_page_token surfaced", result["next_page_token"] == "l-2")
    _assert("list_static_lists token overrides false vendor moreResult", result["more_result"] is True)

    # Without a usable token, the collection is terminal even when the raw
    # vendor flag claims otherwise.
    bare = _fake_client(
        get_json={"success": True, "result": [{"id": 1}], "moreResult": True}
    )
    result = marketing_actions.list_campaigns(bare, {}, _no_blob_writer)
    _assert("absent collection token surfaces None", result["next_page_token"] is None)
    _assert("absent collection token overrides true vendor moreResult", result["more_result"] is False)


def test_edge_parity() -> None:
    from ananta.core.plugins.action_discovery import discover_actions
    from ananta.core.process_registry.plugin_registration_validator import (
        PluginRegistrationValidator,
    )

    plugin = MarketoPlugin()
    actions = discover_actions(plugin)
    raised = None
    try:
        PluginRegistrationValidator().validate_edge_process_provider("marketo_plugin", plugin, actions)
    except Exception as exc:  # FrameworkError on mismatch
        raised = exc
    _assert("EDGE parity: validator raises nothing", raised is None, str(raised))
    _assert("all 15 verbs discovered", len(actions) == 15, str(len(actions)))
    _assert("get_activities registered as a verb", "get_activities" in {a.name for a in actions})
    _assert(
        "list_activity_types registered as a verb",
        "list_activity_types" in {a.name for a in actions},
    )
    _assert(
        "get_api_usage registered as a verb",
        "get_api_usage" in {a.name for a in actions},
    )


def main() -> int:
    print("\nmarketo_plugin lead verb + envelope-classification smoke tests")
    print("=" * 63)
    test_describe_lead_fields_inline()
    test_get_leads_filter_forwarding()
    test_create_or_update_leads_partial_batch_is_not_an_error()
    test_documented_get_leads_default_fields_contract()
    test_create_or_update_leads_read_only_preflight()
    test_delete_leads_batch_cap()
    test_merge_leads()
    test_check_setup_all_pass()
    test_check_setup_mixed_pass_fail()
    test_classify_marketo_envelope()
    test_get_activities()
    test_get_activities_fractional_seconds_contract()
    test_list_activity_types_and_api_usage()
    test_list_paging_fields_surfaced()
    test_edge_parity()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All lead verb + envelope-classification smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
