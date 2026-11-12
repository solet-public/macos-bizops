#!/usr/bin/env python3
"""Real-shaped multipart/import/extract transcripts through the job ledger."""

from __future__ import annotations

import base64
import json
import sys
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins/marketo_plugin/src"))
sys.path.insert(0, str(Path(__file__).parent))

from lead_database_objects_smoke import STATE, _VendorTranscriptCase  # noqa: E402  # pyright: ignore[reportMissingImports]
from marketo_plugin import bulk_api, completion_templates, file_api, transfer_metadata  # noqa: E402
from marketo_plugin.constants import CHECK_SETUP_UNVERIFIED_WRITE_VERBS  # noqa: E402
from marketo_plugin.multipart import decode_files, prepare_files  # noqa: E402
from marketo_plugin.plugin import MarketoPlugin  # noqa: E402
from marketo_plugin.transfer_processes import ActivityExtractProcesses, FileProcesses, ImportProcesses, LeadExtractProcesses, ProgramMemberExtractProcesses, TransferDispatch  # noqa: E402

EXPORT_ID = "ce45a7a1-f19d-4ce2-882c-a3c795940a7d"
DATE_RANGE = {"startAt": "2026-11-01T00:00:00Z", "endAt": "2026-11-02T00:00:00Z"}


def _envelope(result: list[dict[str, Any]], **extras: Any) -> dict[str, Any]:
    return {"requestId": "e42b#14272d07d78", "success": True, "result": result, **extras}


def _file_part(content: bytes = b"email,firstName\r\nperson@example.test,Alex\r\n", filename: str = "leads.csv", content_type: str = "text/csv") -> dict[str, str]:
    return {"filename": filename, "content_type": content_type, "content_base64": base64.b64encode(content).decode()}


def _multipart(request: httpx.Request) -> dict[str, Any]:
    raw = b"Content-Type: " + request.headers["Content-Type"].encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + request.content
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert message.is_multipart()
    parts: dict[str, Any] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        parts[str(name)] = {"filename": part.get_filename(), "content_type": part.get_content_type(), "content": part.get_payload(decode=True)}
    return parts


class BulkFileTests(_VendorTranscriptCase):
    def test_thirty_five_operations_have_all_three_registration_artifacts(self) -> None:
        from ananta.core.plugins.action_discovery import discover_actions
        from ananta.core.process_registry.plugin_registration_validator import PluginRegistrationValidator

        plugin = MarketoPlugin()
        actions = {action.name: action for action in discover_actions(plugin)}
        PluginRegistrationValidator().validate_edge_process_provider("marketo_plugin", plugin, discover_actions(plugin))
        self.assertEqual(len(transfer_metadata.ALL_VERBS), 35)
        for verb in transfer_metadata.ALL_VERBS:
            with self.subTest(verb=verb):
                self.assertTrue(callable(getattr(plugin, verb)))
                self.assertIn(verb, plugin.get_edge_process_definitions())
                kb = json.loads((REPO_ROOT / "plugins/marketo_plugin/knowledge_base/processes" / f"{verb}.json").read_text())
                self.assertEqual(kb["process_key"], f"plugin::marketo_plugin::{verb}")
                self.assertEqual(kb["result_processor_customizations"]["result_type"], transfer_metadata.result_type(verb))
                self.assertTrue(200 <= len(kb["embedding_description"]) <= 400)
                self.assertEqual(set(actions[verb].parameters), set(transfer_metadata.parameters(verb)))
                if transfer_metadata.is_write(verb):
                    self.assertIn(verb, CHECK_SETUP_UNVERIFIED_WRITE_VERBS)
        # These static mixins are discovered by the ordinary runtime action census.
        for cls in (FileProcesses, ImportProcesses, LeadExtractProcesses, ActivityExtractProcesses, ProgramMemberExtractProcesses):
            self.assertIn(TransferDispatch, cls.__mro__)
        self.assertEqual(len(file_api.ENDPOINTS), 5)
        self.assertEqual(len(bulk_api.ENDPOINTS), 30)

    def test_file_and_image_upload_update_and_reversal_use_real_multipart(self) -> None:
        content = b"\x89PNG\r\n\x00\xffbinary-image"
        current: dict[str, Any] = {}

        def vendor(request: httpx.Request) -> httpx.Response:
            parts = _multipart(request)
            if parts.get("insertOnly", {}).get("content") == b"true" and current:
                return httpx.Response(200, json={"success": False, "errors": [{"code": "1003", "message": "File already exists"}]})
            current["content"] = parts["file"]["content"]
            return httpx.Response(
                200, json=_envelope([{"id": 46960, "name": "hero.png", "mimeType": "image/png", "size": len(current["content"]), "folder": {"id": 436, "type": "Image"}, "url": "https://pages.example.test/hero.png", "createdAt": "2026-11-01T00:00:00Z+0000", "updatedAt": "2026-11-07T00:00:00Z+0000"}], warnings=[])
            )

        self.vendor = vendor
        params = {"file": _file_part(content, "hero.png", "image/png"), "folder": {"id": 436, "type": "Folder"}, "name": "hero.png", "description": "Image & launch", "insert_only": True}
        result, _ = self._run_job("create_asset_file", params)
        self.assertEqual(result["status"], "completed")
        call = self._vendor_calls()[-1]
        self.assertEqual((call.method, call.url.path), ("POST", "/rest/asset/v1/files.json"))
        parts = _multipart(call)
        self.assertEqual(parts["file"], {"filename": "hero.png", "content_type": "image/png", "content": content})
        self.assertEqual({key: part["content"] for key, part in parts.items() if key != "file"}, {"folder": b'{"id":436,"type":"Folder"}', "name": b"hero.png", "description": b"Image & launch", "insertOnly": b"true"})
        failed, _ = self._run_job("create_asset_file", params)
        self.assertEqual(failed["error"]["code"], "marketo.validation_failed")
        # Upsert permits correction and explicit description clear; content replacement is reversible from saved bytes.
        result, _ = self._run_job("create_asset_file", {**params, "description": "", "insert_only": False})
        self.assertEqual(_multipart(self._vendor_calls()[-1])["description"]["content"], b"")
        for value in (b"corrected", content):
            result, _ = self._run_job("update_asset_file_content", {"file_id": 46960, "file": _file_part(value, "hero.png", "image/png")})
            call = self._vendor_calls()[-1]
            self.assertEqual((call.method, call.url.path), ("POST", "/rest/asset/v1/file/46960/content.json"))
            self.assertEqual(set(_multipart(call)), {"file"})
            self.assertEqual(current["content"], value)

    def test_file_reads_page_with_all_documented_parameters_and_resume_hint(self) -> None:
        self.vendor = lambda _: httpx.Response(200, json=_envelope([{"id": 46960, "name": "hero.png", "size": 100, "mimeType": "image/png", "folder": {"id": 436, "type": "Image"}}]))
        for verb, params, path, query in (
            ("get_asset_file", {"file_id": 46960}, "/rest/asset/v1/file/46960.json", {}),
            ("get_asset_file_by_name", {"name": "hero.png"}, "/rest/asset/v1/file/byName.json", {"name": "hero.png"}),
        ):
            result, _ = self._run_job(verb, {**params, "output_tsv_path": self._tsv(verb + ".tsv")})
            self.assertEqual(result["result"]["row_count"], 1)
            self.assertEqual((self._vendor_calls()[-1].url.path, dict(self._vendor_calls()[-1].url.params)), (path, query))

        def vendor(request: httpx.Request) -> httpx.Response:
            offset = int(request.url.params["offset"])
            return httpx.Response(200, json=_envelope([{"id": offset, "name": f"file-{offset}"}] if offset < 3 else []))

        self.vendor = vendor
        result, _ = self._run_job("list_asset_files", {"folder": {"id": 436, "type": "Program"}, "offset": 1, "max_return": 1, "output_tsv_path": self._tsv("files.tsv")})
        calls = self._vendor_calls()[-3:]
        self.assertEqual([call.url.params["offset"] for call in calls], ["1", "2", "3"])
        self.assertEqual(dict(calls[0].url.params), {"folder": '{"id":436,"type":"Program"}', "offset": "1", "maxReturn": "1"})
        self.assertEqual(result["result"]["row_count"], 2)
        self.vendor = lambda _: httpx.Response(200, json=_envelope([{"id": n} for n in range(200)]))
        result, _ = self._run_job("list_asset_files", {"output_tsv_path": self._tsv("cap.tsv")})
        self.assertEqual((result["result"]["truncated"], result["result"]["next_offset"]), (True, 500))
        result, _ = self._run_job("list_asset_files", {"row_limit": 2, "acknowledge_default_limit_override": True, "output_tsv_path": self._tsv("override.tsv")})
        self.assertEqual((result["result"]["row_count"], result["result"]["next_offset"]), (2, 2))

    def test_all_import_families_submit_status_failures_warnings_and_historical_readback(self) -> None:
        families = (
            ("lead", "import_leads", "/bulk/v1/leads.json", "/bulk/v1/leads/batch/3404", {"lookup_field": "id", "partition_name": "Default", "list_id": 1204}, {"lookupField": "id", "partitionName": "Default", "listId": "1204"}),
            ("custom_object", "import_custom_objects", "/bulk/v1/customobjects/car_c/import.json", "/bulk/v1/customobjects/car_c/import/3404", {"api_name": "car_c"}, {}),
            ("program_member", "import_program_members", "/bulk/v1/program/1204/members/import.json", "/bulk/v1/program/members/import/3404", {"program_id": "1204", "program_member_status": "On List"}, {"programMemberStatus": "On List"}),
        )
        for family, submit, submit_path, status_base, extras, query in families:
            with self.subTest(family=family):
                plugin, _ = self._plugin()
                handles: list[str] = []

                def vendor(request: httpx.Request, family: str = family) -> httpx.Response:
                    if request.method == "POST":
                        identity = {"objectApiName": "car_c"} if family == "custom_object" else {"importId": "3404"}
                        return httpx.Response(200, json=_envelope([{"batchId": 3404, "status": "Queued", **identity}]))
                    if request.url.path.endswith(("failures.json", "warnings.json")):
                        return httpx.Response(200, text='email,firstName,Reason\r\nperson@example.test,Alex,"Invalid value"\r\n', headers={"Content-Type": "text/csv"})
                    counts = {"numOfObjectsProcessed": 2, "objectApiName": "car_c", "operation": "import"} if family == "custom_object" else {"numOfLeadsProcessed": 2}
                    return httpx.Response(200, json=_envelope([{"batchId": 3404, "status": "Complete", "numOfRowsFailed": 1, "numOfRowsWithWarning": 1, "message": "Import completed with errors", **counts}]))

                self.vendor = vendor
                formats = ("csv", "tsv", "ssv", "CSV", "TSV", "SSV") if family == "program_member" else ("csv", "tsv", "ssv")
                for format_name in formats:
                    delimiter = {"csv": ",", "tsv": "\t", "ssv": ";"}[format_name.lower()]
                    headers, values = {"lead": (["email", "firstName"], ["person@example.test", "Alex"]), "program_member": (["email", "firstName"], ["person@example.test", "Alex"]), "custom_object": (["vin", "color"], ["WBA4R7C55HK895912", "red"])}[family]
                    part = _file_part((delimiter.join(headers) + "\r\n" + delimiter.join(values) + "\r\n").encode())
                    queued = getattr(plugin, submit)({"file": part, "format": format_name, **extras}, STATE)
                    self.assertEqual(queued["action_status"], "completed", queued)
                    handles.append(queued["data"]["job_id"])
                    before = len(self._vendor_calls())
                    plugin._process_pending_jobs()  # pyright: ignore[reportPrivateUsage]
                    call = self._vendor_calls()[-1]
                    self.assertEqual(len(self._vendor_calls()), before + 1)
                    self.assertEqual((call.method, call.url.path, dict(call.url.params)), ("POST", submit_path, {"format": format_name, **query}))
                    self.assertEqual(_multipart(call)["file"]["content"], base64.b64decode(part["content_base64"]))
                    self.assertEqual(plugin.check_marketo_job_status({"job_id": handles[-1]}, {})["data"]["result"]["result"][0]["status"], "Queued")
                status_params = {"batch_id": 3404, **({"api_name": "car_c"} if family == "custom_object" else {})}
                for operation in ("status", "failures", "warnings"):
                    result, _ = self._run_job(f"get_{family}_import_{operation}", status_params)
                    path = status_base + (".json" if family == "lead" else "/status.json") if operation == "status" else status_base + f"/{operation}.json"
                    self.assertEqual((self._vendor_calls()[-1].method, self._vendor_calls()[-1].url.path), ("GET", path))
                    self.assertEqual(result["status"], "completed", result)
                    if operation == "status":
                        self.assertEqual(result["result"]["result"][0]["numOfRowsFailed"], 1)
                    else:
                        self.assertIn("Invalid value", result["result"]["body"])
                        self.assertEqual(base64.b64decode(result["result"]["body_base64"]), result["result"]["body"].encode())
                historical = plugin.check_marketo_job_status({"job_id": handles[0]}, {})
                self.assertEqual(historical["data"]["result"]["result"][0]["status"], "Queued")

    def test_all_extract_families_create_enqueue_status_download_cancel_and_list_continuation(self) -> None:
        for family, resource, filters in (
            ("lead", "leads", {"createdAt": DATE_RANGE}),
            ("activity", "activities", {"createdAt": DATE_RANGE, "activityTypeIds": [2], "primaryAttributeValueIds": [16]}),
            ("program_member", "program/members", {"programIds": [1204, 1205], "isExhausted": False, "updatedAt": DATE_RANGE}),
        ):
            with self.subTest(family=family):
                vendor_state = "Created"
                current_id = EXPORT_ID
                creations = 0
                full_bytes = "ÉRecord Id,Value\r\n5414087,2026-11-01T00:00:00Z\r\n".encode()

                def vendor(request: httpx.Request, full_bytes: bytes = full_bytes) -> httpx.Response:
                    nonlocal vendor_state, current_id, creations
                    if request.url.path.endswith("/create.json"):
                        creations += 1
                        current_id = EXPORT_ID if creations == 1 else "fad2cd1b-e822-4025-be1e-9caa9cf1d4b8"
                        vendor_state = "Created"
                    if request.url.path.endswith("/enqueue.json"):
                        vendor_state = "Queued"
                    if request.url.path.endswith("/cancel.json"):
                        vendor_state = "Canceled"
                    if request.url.path.endswith("/file.json"):
                        if request.headers.get("Range"):
                            return httpx.Response(206, content=b"\xc3", headers={"Content-Range": f"bytes 0-0/{len(full_bytes)}"})
                        return httpx.Response(200, content=full_bytes, headers={"Content-Type": "text/csv; charset=utf-8"})
                    if request.url.path.endswith("/export.json"):
                        return httpx.Response(200, json=_envelope([{"exportId": EXPORT_ID, "status": "Completed"}], nextPageToken="next-2" if request.url.params.get("nextPageToken") != "next-2" else None))
                    return httpx.Response(200, json=_envelope([{"exportId": current_id, "status": vendor_state, "format": "CSV", "createdAt": "2026-11-07T00:00:00Z", "numberOfRecords": 1}]))

                self.vendor = vendor
                field_names = {"lead": ["id", "email"], "activity": ["leadId", "activityDate"], "program_member": ["leadId", "email"]}[family]
                column_headers = {field_names[0]: "ÉRecord Id", field_names[1]: "Value"}
                create_params = {"fields": field_names, "filter": filters, "format": "CSV", "column_header_names": column_headers}
                created, _ = self._run_job(f"create_bulk_{family}_extract", create_params)
                self.assertEqual(created["result"]["result"][0]["status"], "Created")
                self.assertEqual((self._vendor_calls()[-1].method, self._vendor_calls()[-1].url.path), ("POST", f"/bulk/v1/{resource}/export/create.json"))
                self.assertEqual(json.loads(self._vendor_calls()[-1].content), {"fields": field_names, "filter": filters, "format": "CSV", "columnHeaderNames": column_headers})
                for operation, suffix, expected in (("enqueue", "enqueue", "Queued"), ("get", "status", "Completed")):
                    if operation == "get":
                        vendor_state = "Completed"
                    verb = f"{operation}_bulk_{family}_extract" + ("_status" if operation == "get" else "")
                    result, _ = self._run_job(verb, {"export_id": EXPORT_ID})
                    call = self._vendor_calls()[-1]
                    self.assertEqual((call.method, call.url.path), ("GET" if suffix == "status" else "POST", f"/bulk/v1/{resource}/export/{EXPORT_ID}/{suffix}.json"))
                    self.assertEqual(result["result"]["result"][0]["status"], expected)
                result, _ = self._run_job(f"download_bulk_{family}_extract", {"export_id": EXPORT_ID})
                self.assertEqual(result["result"]["status"], 200)
                self.assertIn("5414087", result["result"]["body"])
                result, _ = self._run_job(f"download_bulk_{family}_extract", {"export_id": EXPORT_ID, "range": "bytes=0-0"})
                self.assertEqual(self._vendor_calls()[-1].headers["Range"], "bytes=0-0")
                self.assertEqual(result["result"]["content_range"], f"bytes 0-0/{len(full_bytes)}")
                self.assertEqual(base64.b64decode(result["result"]["body_base64"]), b"\xc3")
                replacement, _ = self._run_job(f"create_bulk_{family}_extract", create_params)
                cancel_id = replacement["result"]["result"][0]["exportId"]
                cancelled, _ = self._run_job(f"cancel_bulk_{family}_extract", {"export_id": cancel_id})
                self.assertEqual(cancelled["result"]["result"][0]["status"], "Canceled")
                self.assertEqual((self._vendor_calls()[-1].method, self._vendor_calls()[-1].url.path), ("POST", f"/bulk/v1/{resource}/export/{cancel_id}/cancel.json"))
                for page in (None, "next-2"):
                    params = {"status": ["created", "completed"], "batch_size": 1, **({"next_page_token": page} if page else {})}
                    result, _ = self._run_job(f"list_bulk_{family}_extract_jobs", params)
                    call = self._vendor_calls()[-1]
                    self.assertEqual(call.url.path, f"/bulk/v1/{resource}/export.json")
                    self.assertEqual(call.url.params.get_list("status"), ["created", "completed"])
                    self.assertEqual(call.url.params["batchSize"], "1")
                    self.assertEqual(call.url.params.get("nextPageToken"), page)
                    self.assertEqual(result["result"]["nextPageToken"], None if page else "next-2")

    def test_all_documented_extract_filter_variants_and_optional_activity_fields_are_forwarded(self) -> None:
        self.vendor = lambda _: httpx.Response(200, json=_envelope([{"exportId": EXPORT_ID, "status": "Created"}]))
        filters = {
            "lead": [{name: value} for name, value in (("createdAt", DATE_RANGE), ("updatedAt", DATE_RANGE), ("staticListName", "Launch"), ("staticListId", 1204), ("smartListName", "Active"), ("smartListId", 1205))],
            "activity": [{"createdAt": DATE_RANGE}, {"createdAt": DATE_RANGE, "activityTypeIds": [2], "primaryAttributeValues": ["Campaign.Form"]}],
            "program_member": [{"programId": 1204}, {"programIds": [1204, 1205], "isExhausted": False, "updatedAt": DATE_RANGE, "nurtureCadence": "norm", "statusNames": ["On List", "Member"]}],
        }
        for family, values in filters.items():
            for value in values:
                for format_name in ("CSV", "TSV", "SSV"):
                    params = {"filter": value, "format": format_name, **({} if family == "activity" else {"fields": ["email"]})}
                    result, _ = self._run_job(f"create_bulk_{family}_extract", params)
                    self.assertEqual(result["status"], "completed", result)
                    body = json.loads(self._vendor_calls()[-1].content)
                    self.assertEqual((body["filter"], body["format"]), (value, format_name))
                    self.assertEqual("fields" in body, family != "activity")

    def test_multipart_generic_route_replays_binary_parts_after_explicit_token_expiry(self) -> None:
        content = b"\x00\xffbinary-file"

        def vendor(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"success": False, "errors": [{"code": "602", "message": "Expired"}]}) if self.mints == 1 else httpx.Response(200, json=_envelope([{"id": 46960}]))

        self.vendor = vendor
        result, _ = self._run_job("request", {"method": "POST", "path": "/rest/asset/v1/files.json", "form_body": {"name": "image.png", "folder": '{"id":436,"type":"Folder"}'}, "multipart_files": {"file": _file_part(content, "image.png", "image/png")}, "headers": {"Content-Type": "wrong-boundary"}})
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(self.mints, 2)
        self.assertEqual(len(self._vendor_calls()), 2)
        for call in self._vendor_calls():
            self.assertIn("multipart/form-data; boundary=", call.headers["Content-Type"])
            self.assertEqual(_multipart(call)["file"]["content"], content)
        self.assertEqual(decode_files(prepare_files({"file": _file_part(content)}))["file"][1], content)  # type: ignore[index]

    def test_documented_http_range_forms_reach_vendor_without_a_narrower_local_grammar(self) -> None:
        # A server may ignore Range and send the whole representation (HTTP 200).
        self.vendor = lambda _: httpx.Response(200, text="leadId\r\n5414087\r\n", headers={"Content-Type": "text/csv"})
        for value in ("bytes= 0-999, 4500-5499, -1000", "BYTES=0-499", "bytes=-500", "bytes=9500-"):
            result, _ = self._run_job("download_bulk_activity_extract", {"export_id": EXPORT_ID, "range": value})
            self.assertEqual(self._vendor_calls()[-1].headers["Range"], value)
            self.assertEqual(result["result"]["status"], 200)

    def test_vendor_terminal_failure_and_auth_validation_transport_errors_stay_distinct(self) -> None:
        self.vendor = lambda _: httpx.Response(200, json=_envelope([{"batchId": 3404, "status": "Failed", "message": "Import failed"}]))
        result, _ = self._run_job("get_lead_import_status", {"batch_id": 3404})
        self.assertEqual(result["result"]["result"][0]["status"], "Failed")
        for status, body, code in (
            (200, {"requestId": "e42b#14272d07d78", "success": False, "errors": [{"code": "1003", "message": "Invalid job state"}]}, "marketo.validation_failed"),
            (200, {"success": False, "errors": [{"code": "603", "message": "Access denied"}]}, "marketo.permission_denied"),
            (401, {"message": "Unauthorized"}, "marketo.auth_failed"),
            (403, {"message": "Forbidden"}, "marketo.permission_denied"),
            (503, {"message": "Unavailable"}, "marketo.api_error"),
        ):
            self.vendor = lambda _, status=status, body=body: httpx.Response(status, json=body)
            for verb, params in (("import_leads", {"file": _file_part(), "format": "csv"}), ("cancel_bulk_activity_extract", {"export_id": EXPORT_ID}), ("get_asset_file", {"file_id": 46960, "output_tsv_path": self._tsv("error.tsv")})):
                before = len(self._vendor_calls())
                result, _ = self._run_job(verb, params)
                self.assertEqual(result["error"]["code"], code, result)
                self.assertEqual(len(self._vendor_calls()), before + 1)
        prompt = json.dumps(completion_templates.build_completion_template("result", "bulk extract", result_kind="bulk"))
        self.assertIn("Platform completion is not vendor bulk-job completion", prompt)
        self.assertIn("Failed", prompt)

    def test_input_integrity_refusals_create_neither_job_nor_http_call(self) -> None:
        cases = [
            ("create_asset_file", {"name": "hero.png", "folder": {"id": 436, "type": "Folder"}, "file": {"filename": "x", "content_type": "image/png", "content_base64": "%%%"}}),
            ("import_leads", {"file": _file_part(), "format": "wrong"}),
            ("get_lead_import_status", {"batch_id": True}),
            ("create_bulk_lead_extract", {"fields": [], "filter": {"createdAt": DATE_RANGE}}),
            ("create_bulk_activity_extract", {"filter": {}, "format": "CSV"}),
            ("create_bulk_lead_extract", {"fields": ["email"], "filter": {"createdAt": DATE_RANGE}, "column_header_names": []}),
            ("list_bulk_activity_extract_jobs", {"batch_size": 301}),
            ("download_bulk_activity_extract", {"export_id": EXPORT_ID, "range": "bytes=0-1\r\nX: 1"}),
            ("request", {"method": "POST", "path": "https://foreign.test/", "multipart_files": {"file": _file_part()}}),
            ("request", {"method": "POST", "path": "/files", "json_body": {}, "multipart_files": {"file": _file_part()}}),
        ]
        for verb, params in cases:
            plugin, manager = self._plugin()
            result = getattr(plugin, verb)(params, STATE)
            self.assertEqual(result["action_status"], "error", (verb, result))
            self.assertEqual(manager._jobs, {})  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.seen, [])


if __name__ == "__main__":
    unittest.main()
