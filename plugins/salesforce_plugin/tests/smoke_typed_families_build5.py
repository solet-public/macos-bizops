"""Offline smoke for build 5's typed Salesforce operations and families.

Covers Tooling composite and apexCompileResults, the Bulk API 2.0 parallel
``resultPages`` resource, the report instance delete, sObject relationship
update and delete, and the ``metadata`` (REST deployRequest) and
``content_files`` (ContentVersion upload, verified download, ContentDocument
delete) families. Vendor calls run through the real ``SalesforceCliExecutor``
with ``subprocess.run`` patched to print what ``sf api request rest --include``
prints (status line, lower-case headers, no blank separator, pretty JSON), or,
for ``--stream-to-file``, to write the response bytes to the named file and
print only ``File saved to``. Bodies follow the vendor guides: Tooling
compositeResponse, Bulk resultPages with nextRecordsUrl, Metadata deployResult
with component and test errors, ContentVersion SaveResult and record fields.
No CLI executable and no Salesforce org are needed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import NonCallableMagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "ananta/src"))
import base64  # noqa: E402
import hashlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
import zipfile  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402
from urllib.parse import parse_qs, urlsplit  # noqa: E402

from ananta.file_locations.containment import PathRefusedError, assert_path_allowed, derive_allowed_roots  # noqa: E402
from ananta.file_locations.resolution import PathAddress, SoletTree  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins/salesforce_plugin/src"))

from salesforce_plugin import async_jobs, request_actions, transfers, typed_families  # noqa: E402
from salesforce_plugin.client import SalesforceCliExecutor  # noqa: E402
from salesforce_plugin.errors import SalesforceServiceError  # noqa: E402
from salesforce_plugin.plugin import SalesforcePlugin  # noqa: E402

V = "/services/data/v62.0"
JOB_ID = "750R0000000zxr8IAA"
REPORT_ID = "00OD0000001ZbP7MAK"
INSTANCE_ID = "0LGD000000000IjOAI"
CONTACT_ID = "003D000000QV9n2IAD"
CLASS_ID = "01pD0000000Fhy9IAC"
DEPLOY_ID = "0AfR000000014UoKAI"
VALIDATED_ID = "0AfR000000014UjKAI"
VERSION_ID = "068R0000000ytvdIAA"
DOCUMENT_ID = "069R0000000yZ9CIAU"
OWNER_ID = "005D0000001KyEIIA0"
ORG_HEADERS = "date: Sat, 07 Nov 2026 15:30:00 GMT\nx-content-type-options: nosniff\nx-robots-tag: none\nsforce-limit-info: api-usage=44/15000\n"
FILE_BYTES = b"%PDF-1.7\n\x00\xff\xfe binary \r\n body \x80\x81 end\n"


_LOCATION_ROOT = Path(__file__).resolve().parents[3]
_LOCATION_TREE = SoletTree(_LOCATION_ROOT, _LOCATION_ROOT / "profile")


def _checked_path(path: str, directories: list[str], *, suffix: str | None = ".tsv", param_name: str = "output_tsv_path", reading: bool = False) -> str:
    """Exercise real address-derived containment, never a mocked boundary."""
    addresses = [PathAddress(f"project-{index}", Path(directory), frozenset(), frozenset(), datetime.now(UTC)) for index, directory in enumerate(directories)]
    roots = derive_allowed_roots(addresses, None, _LOCATION_TREE, _LOCATION_ROOT.parent / "test-solet-files")
    result = assert_path_allowed(path, roots, _LOCATION_TREE, suffix=suffix, param_name=param_name, plugin_name="test")
    if reading and not Path(result.value).is_file():
        raise PathRefusedError(f"{param_name} must name an existing regular file")
    return result.value


def _bind_locations(plugin: Any, directories: list[str]) -> NonCallableMagicMock:
    """Supply a live address-book service through the production binding."""
    orchestrator = NonCallableMagicMock()
    orchestrator.APP_HOME = str(_LOCATION_TREE.app_home)
    orchestrator.plugin_manager.get_plugin.return_value = None
    orchestrator.get_service.return_value.list_path_addresses.return_value = {
        "action_status": "completed",
        "data": {"addresses": [{"name": f"project-{index}", "tags": [], "updated_at": datetime.now(UTC).isoformat(), "entries": [{"field_type": "path", "value": directory}]} for index, directory in enumerate(directories)]},
    }
    plugin.orchestrator_ref = orchestrator
    return orchestrator


def transcript(status: int, body: Any) -> str:
    printed = json.dumps(body, indent=2)
    return f"HTTP/1.1 {status}\n{ORG_HEADERS}content-type: application/json;charset=UTF-8\n{printed}\n"


def no_content() -> str:
    return f"HTTP/1.1 204\n{ORG_HEADERS}\n"


def vendor_error(status: int, code: str, message: str) -> str:
    return transcript(status, [{"errorCode": code, "message": message}])


def deploy_result(status: str, component_errors: str = "0", test_errors: str = "0") -> dict[str, Any]:
    """The deployRequest response shape from the Metadata API guide (counts are strings there)."""
    return {
        "id": DEPLOY_ID,
        "url": f"https://host{V}/metadata/deployRequest/{DEPLOY_ID}?includeDetails=true",
        "deployResult": {
            "checkOnly": "false",
            "ignoreWarnings": "false",
            "rollbackOnError": "true",
            "status": status,
            "numberComponentsDeployed": "10",
            "numberComponentsTotal": "12",
            "numberComponentErrors": component_errors,
            "numberTestsCompleted": "45",
            "numberTestsTotal": "45",
            "numberTestErrors": test_errors,
            "details": {"componentFailures": [{"fileName": "classes/Broken.cls", "problem": "Unexpected token '}'.", "problemType": "Error", "success": False}] if component_errors != "0" else [], "componentSuccesses": [], "retrieveResult": None, "runTestResults": {"numRun": 45, "successes": [], "failures": []}},
            "createdDate": "2026-11-07T15:00Z",
            "startDate": "2026-11-07T15:00Z",
            "lastModifiedDate": "2026-11-07T15:04Z",
            "completedDate": "2026-11-07T15:04Z",
            "errorStatusCode": None,
            "errorMessage": None,
            "stateDetail": None,
            "createdBy": OWNER_ID,
            "createdByName": "Integration User",
            "canceledBy": None,
            "canceledByName": None,
            "isRunTestsEnabled": None,
        },
    }


def version_record(*, size: int | None, checksum: str | None) -> dict[str, Any]:
    record: dict[str, Any] = {"attributes": {"type": "ContentVersion", "url": f"{V}/sobjects/ContentVersion/{VERSION_ID}"}, "Id": VERSION_ID, "ContentDocumentId": DOCUMENT_ID, "Title": "Quote Q-1001", "PathOnClient": "Quote Q-1001.pdf", "FileExtension": "pdf", "VersionNumber": "2"}
    if size is not None:
        record["ContentSize"] = size
    if checksum is not None:
        record["Checksum"] = checksum
    return record


def deploy_zip(path: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("package.xml", '<?xml version="1.0" encoding="UTF-8"?><Package xmlns="http://soap.sforce.com/2006/04/metadata"><types><members>Hello</members><name>ApexClass</name></types><version>62.0</version></Package>')
        archive.writestr("classes/Hello.cls", "public class Hello {}")
    path.write_bytes(buffer.getvalue())
    return buffer.getvalue()


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.executor = SalesforceCliExecutor(MagicMock())
        self.executor._target_org = "bound-org"  # noqa: SLF001
        self.argvs: list[list[str]] = []
        self.bodies: list[bytes | None] = []
        self.outputs: list[str | tuple[bytes, int]] = []
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.imports = self.workspace / "in"
        self.exports = self.workspace / "out"
        self.imports.mkdir()
        self.exports.mkdir()

    def gates(self) -> transfers.TransferGates:
        def read(path: str) -> str:
            return _checked_path(path, [str(self.imports)], suffix="", param_name="file_path", reading=True)

        def write(path: str) -> str:
            return _checked_path(path, [str(self.exports)], suffix="", param_name="output_path", reading=False)

        return transfers.TransferGates(read=read, write=write)

    def zip_gate(self, path: str) -> str:
        return _checked_path(path, [str(self.imports)], suffix=".zip", param_name="zip_path", reading=True)

    def respond(self, *outputs: str | tuple[bytes, int]) -> None:
        """A str is an --include transcript; (bytes, exit code) is a --stream-to-file run."""
        self.outputs = list(outputs)

    def run_family(self, family: str, params: dict[str, Any], gate: Any = None) -> dict[str, Any]:
        def invoke(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.argvs.append(argv)
            self.bodies.append(Path(argv[argv.index("--body") + 1][1:]).read_bytes() if "--body" in argv else None)
            output = self.outputs.pop(0)
            if isinstance(output, tuple):
                self.assertIn("--stream-to-file", argv)
                self.assertNotIn("--include", argv)
                content, code = output
                Path(argv[argv.index("--stream-to-file") + 1]).write_bytes(content)
                return subprocess.CompletedProcess(argv, code, "File saved to the requested path\n" if code == 0 else "", "")
            return subprocess.CompletedProcess(argv, int(int(output.split()[1]) >= 400), output, "")

        with patch("salesforce_plugin.client.subprocess.run", side_effect=invoke):
            return typed_families.run_family(family, self.executor, params, gate)

    def sent(self, index: int = -1) -> tuple[str, str, dict[str, list[str]], dict[str, str]]:
        argv = self.argvs[index]
        target = urlsplit(argv[4])
        headers = dict(item.split(":", 1) for item in (argv[i + 1] for i, a in enumerate(argv) if a == "--header"))
        return argv[argv.index("--method") + 1], target.path, parse_qs(target.query), headers

    def sent_json(self) -> Any:
        body = self.bodies[-1]
        return json.loads(body) if body is not None else None


class ToolingComposite(Fixture):
    def test_composite_counts_failed_subrequests(self) -> None:
        body = {
            "allOrNone": False,
            "compositeRequest": [
                {"method": "POST", "url": f"{V}/tooling/sobjects/MetadataContainer", "referenceId": "container", "body": {"Name": "Deploy-2026-11-07"}},
                {"method": "POST", "url": f"{V}/tooling/sobjects/ApexClassMember", "referenceId": "member", "body": {"MetadataContainerId": "@{container.id}", "ContentEntityId": CLASS_ID, "Body": "public class Hello {"}},
            ],
        }
        response = {
            "compositeResponse": [
                {"body": {"id": "1dcR0000000PUzZIAW", "success": True, "errors": []}, "httpHeaders": {"Location": f"{V}/tooling/sobjects/MetadataContainer/1dcR0000000PUzZIAW"}, "httpStatusCode": 201, "referenceId": "container"},
                {"body": [{"errorCode": "FIELD_INTEGRITY_EXCEPTION", "message": "Body: unexpected token", "fields": ["Body"]}], "httpHeaders": {}, "httpStatusCode": 400, "referenceId": "member"},
            ]
        }
        self.respond(transcript(200, response))
        result = self.run_family("tooling", {"operation": "composite", "body": body})
        method, path, _query, headers = self.sent()
        self.assertEqual((method, path, headers["Content-Type"]), ("POST", V + "/tooling/composite", "application/json"))
        self.assertEqual(self.sent_json(), body)
        self.assertEqual(result["failures"], 1)
        self.assertNotIn("error_code", result)

    def test_composite_resources_listing_passes_through(self) -> None:
        # The Tooling guide documents GET /composite as listing the composite resources without printing a
        # body; this is the REST API guide's documented shape for the same listing (a name -> URI map).
        listing = {"tree": f"{V}/composite/tree", "batch": f"{V}/composite/batch", "sobjects": f"{V}/composite/sobjects", "graph": f"{V}/composite/graph"}
        self.respond(transcript(200, listing))
        result = self.run_family("tooling", {"operation": "composite_resources"})
        self.assertEqual(self.sent()[:2], ("GET", V + "/tooling/composite"))
        self.assertIsNone(self.bodies[-1])
        self.assertEqual(result["body"], listing)
        self.assertNotIn("failures", result)


class ApexCompileResults(Fixture):
    """Tooling guide, apexCompileResults (API 68.0+): the request body must be exactly {}."""

    V68 = "/services/data/v68.0"
    PARTIAL = {
        "status": "PARTIAL_FAILURE",
        "results": [
            {
                "name": "MyInvalidClass",
                "namespace": "MyNamespace",
                "success": False,
                "problems": [{"line": 14, "column": 9, "message": "Variable does not exist: var1"}],
                "warnings": [{"line": 0, "column": 0, "message": "Apex API version 18.0 is scheduled for retirement. Update to the latest API version to avoid compile failures."}],
            },
            {"name": "OtherInvalidTrigger", "namespace": "", "success": False, "problems": [{"line": 3, "column": 1, "message": "Unexpected token 'insert'."}], "warnings": []},
        ],
    }

    def setUp(self) -> None:
        super().setUp()
        self.executor = SalesforceCliExecutor(MagicMock(), api_version="68.0")
        self.executor._target_org = "bound-org"  # noqa: SLF001

    def test_sends_the_documented_empty_object(self) -> None:
        for params in ({"operation": "apex_compile_results"}, {"operation": "apex_compile_results", "body": {}}):
            with self.subTest(params=params):
                self.respond(transcript(200, {"status": "OK", "results": []}))
                result = self.run_family("tooling", params)
                method, path, _query, headers = self.sent()
                self.assertEqual((method, path, headers["Content-Type"]), ("POST", self.V68 + "/tooling/apexCompileResults", "application/json"))
                self.assertEqual(self.sent_json(), {})
                self.assertEqual((result["status"], result["body"]["status"]), (200, "OK"))
                self.assertNotIn("failures", result)

    def test_partial_failure_counts_each_failed_class_or_trigger(self) -> None:
        self.respond(transcript(200, self.PARTIAL))
        result = self.run_family("tooling", {"operation": "apex_compile_results", "body": {}})
        self.assertEqual(result["failures"], 2)
        self.assertEqual(result["body"]["results"][0]["problems"][0]["line"], 14)
        self.respond(transcript(200, {"status": "PARTIAL_FAILURE", "results": []}))
        self.assertEqual(self.run_family("tooling", {"operation": "apex_compile_results"})["failures"], 1)

    def test_any_field_is_refused_before_io(self) -> None:
        # "Specifying any fields returns an error": the vendor takes no input at all.
        with patch("salesforce_plugin.client.subprocess.run") as run, self.assertRaises(typed_families.TypedFamilyError):
            typed_families.run_family("tooling", self.executor, {"operation": "apex_compile_results", "body": {"classes": ["Hello"]}}, None)
        run.assert_not_called()

    def test_below_api_68_the_vendor_answers_not_found(self) -> None:
        self.executor = SalesforceCliExecutor(MagicMock())
        self.executor._target_org = "bound-org"  # noqa: SLF001
        self.respond(vendor_error(404, "NOT_FOUND", "The requested resource does not exist"))
        self.assertEqual(self.run_family("tooling", {"operation": "apex_compile_results"})["error_code"], "sf.not_found")


class BulkResultPages(Fixture):
    def pages(self, done: bool) -> dict[str, Any]:
        results = f"{V}/jobs/query/{JOB_ID}/results?locator="
        body: dict[str, Any] = {"resultPages": [{"resultUrl": results + locator} for locator in ("aBcDeFg4N", "HiJkLmN4N", "oPQrStU4N", "vWxYzz4N", "NiKmABC4N")], "done": done}
        if not done:
            body["nextRecordsUrl"] = f"{V}/jobs/query/{JOB_ID}/resultpages?locator=YcApWm4N"
        return body

    def test_parallel_pages_give_every_locator_and_the_next_page(self) -> None:
        self.respond(transcript(200, self.pages(done=False)))
        first = self.run_family("bulk_jobs", {"operation": "query_result_pages", "job_id": JOB_ID})
        self.assertEqual(self.sent()[:3], ("GET", V + f"/jobs/query/{JOB_ID}/resultPages", {}))
        self.assertEqual(first["pagination"]["result_locators"], ["aBcDeFg4N", "HiJkLmN4N", "oPQrStU4N", "vWxYzz4N", "NiKmABC4N"])
        next_url = first["pagination"]["next_records_url"]
        self.assertEqual(next_url, f"{V}/jobs/query/{JOB_ID}/resultpages?locator=YcApWm4N")
        # The follow-up is the vendor's nextRecordsUrl verbatim, lower-case path included.
        self.respond(transcript(200, self.pages(done=True)))
        last = self.run_family("bulk_jobs", {"operation": "query_result_pages_more", "next_records_url": next_url})
        self.assertEqual(self.sent()[:3], ("GET", V + f"/jobs/query/{JOB_ID}/resultpages", {"locator": ["YcApWm4N"]}))
        self.assertNotIn("next_records_url", last["pagination"])
        self.assertEqual(len(last["pagination"]["result_locators"]), 5)

    def test_result_pages_take_no_invented_input(self) -> None:
        for params in (
            {"operation": "query_result_pages", "job_id": JOB_ID, "query": {"locator": "YcApWm4N"}},
            {"operation": "query_result_pages_more", "next_records_url": f"{V}/jobs/query/{JOB_ID}/results?locator=YcApWm4N"},
            {"operation": "query_result_pages_more", "next_records_url": f"https://evil.example{V}/jobs/query/{JOB_ID}/resultpages?locator=Y"},
        ):
            with self.subTest(params=params), patch("salesforce_plugin.client.subprocess.run") as run:
                with self.assertRaises(typed_families.TypedFamilyError):
                    typed_families.run_family("bulk_jobs", self.executor, params, None)
                run.assert_not_called()

    def test_wrong_api_version_conflict(self) -> None:
        self.respond(vendor_error(409, "INVALIDJOBSTATE", "Use the API version that created the query job"))
        result = self.run_family("bulk_jobs", {"operation": "query_result_pages", "job_id": JOB_ID})
        self.assertEqual(result["error_code"], "sf.conflict")


class ReportsAndRelationships(Fixture):
    def test_report_instance_delete_and_already_gone(self) -> None:
        self.respond(no_content())
        deleted = self.run_family("reports", {"operation": "instance_delete", "report_id": REPORT_ID, "instance_id": INSTANCE_ID})
        self.assertEqual(self.sent()[:2], ("DELETE", V + f"/analytics/reports/{REPORT_ID}/instances/{INSTANCE_ID}"))
        self.assertEqual((deleted["status"], deleted["body"]), (204, ""))
        self.respond(vendor_error(404, "NOT_FOUND", "The requested resource does not exist"))
        self.assertEqual(self.run_family("reports", {"operation": "instance_delete", "report_id": REPORT_ID, "instance_id": INSTANCE_ID})["error_code"], "sf.not_found")

    def test_relationship_update_delete_and_stale_write(self) -> None:
        self.respond(no_content())
        self.run_family("rest_resources", {"operation": "relationship_update", "sobject": "Contact", "id": CONTACT_ID, "relationship": "Account", "body": {"Rating": "Hot"}, "headers": {"If-Unmodified-Since": "Sat, 07 Nov 2026 15:00:00 GMT"}})
        method, path, _query, headers = self.sent()
        self.assertEqual((method, path, headers["If-Unmodified-Since"]), ("PATCH", V + f"/sobjects/Contact/{CONTACT_ID}/Account", "Sat, 07 Nov 2026 15:00:00 GMT"))
        self.assertEqual(self.sent_json(), {"Rating": "Hot"})
        self.respond(vendor_error(412, "PRECONDITION_FAILED", "The record was modified after the If-Unmodified-Since time"))
        stale = self.run_family("rest_resources", {"operation": "relationship_update", "sobject": "Contact", "id": CONTACT_ID, "relationship": "Account", "body": {"Rating": "Hot"}})
        self.assertEqual(stale["error_code"], "sf.precondition_failed")
        self.respond(no_content())
        self.run_family("rest_resources", {"operation": "relationship_delete", "sobject": "Contact", "id": CONTACT_ID, "relationship": "Account"})
        self.assertEqual(self.sent()[:2], ("DELETE", V + f"/sobjects/Contact/{CONTACT_ID}/Account"))
        self.respond(vendor_error(404, "NOT_FOUND", "The requested resource does not exist"))
        self.assertEqual(self.run_family("rest_resources", {"operation": "relationship_delete", "sobject": "Contact", "id": CONTACT_ID, "relationship": "Account"})["error_code"], "sf.not_found")


class MetadataDeploy(Fixture):
    OPTIONS = {"deployOptions": {"checkOnly": True, "rollbackOnError": True, "singlePackage": True, "testLevel": "RunSpecifiedTests", "runTests": ["HelloTest"], "ignoreWarnings": False, "purgeOnDelete": False}}

    def test_deploy_sends_the_documented_multipart_package(self) -> None:
        package = deploy_zip(self.imports / "release.zip")
        self.respond(transcript(201, {"id": DEPLOY_ID, "url": f"https://host{V}/metadata/deployRequest/{DEPLOY_ID}", "deployOptions": self.OPTIONS["deployOptions"]}))
        result = self.run_family("metadata", {"operation": "deploy", "zip_path": str(self.imports / "release.zip"), "body": self.OPTIONS}, self.zip_gate)
        method, path, _query, headers = self.sent()
        self.assertEqual((method, path), ("POST", V + "/metadata/deployRequest"))
        content_type = headers["Content-Type"]
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        boundary = content_type.split("boundary=", 1)[1].encode()
        body = self.bodies[-1] or b""
        parts = body.split(b"--" + boundary)
        self.assertEqual(parts[0], b"")
        self.assertEqual(parts[-1], b"--\r\n")
        json_head, json_part = parts[1].split(b"\r\n\r\n", 1)
        self.assertIn(b'Content-Disposition: form-data; name="json"', json_head)
        self.assertEqual(json.loads(json_part.removesuffix(b"\r\n")), self.OPTIONS)
        file_head, file_part = parts[2].split(b"\r\n\r\n", 1)
        self.assertIn(b'name="file"; filename="deploy.zip"', file_head)
        self.assertIn(b"Content-Type: application/zip", file_head)
        self.assertEqual(file_part.removesuffix(b"\r\n"), package)
        self.assertEqual((result["status"], result["body"]["id"]), (201, DEPLOY_ID))
        self.assertNotIn("failures", result)

    def test_status_counts_component_and_test_errors(self) -> None:
        for status, components, tests, expected in (("InProgress", "0", "0", None), ("Succeeded", "0", "0", None), ("SucceededPartial", "2", "0", 2), ("Failed", "1", "3", 4), ("Failed", "0", "0", 1)):
            with self.subTest(status=status, components=components, tests=tests):
                self.respond(transcript(200, deploy_result(status, components, tests)))
                result = self.run_family("metadata", {"operation": "deploy_status", "deploy_request_id": DEPLOY_ID, "query": {"includeDetails": True}})
                self.assertEqual(self.sent()[:3], ("GET", V + f"/metadata/deployRequest/{DEPLOY_ID}", {"includeDetails": ["true"]}))
                self.assertEqual(result.get("failures"), expected)

    def test_cancel_and_quick_deploy(self) -> None:
        self.respond(transcript(202, deploy_result("Canceling")))
        canceled = self.run_family("metadata", {"operation": "deploy_cancel", "deploy_request_id": DEPLOY_ID})
        self.assertEqual(self.sent()[:2], ("PATCH", V + f"/metadata/deployRequest/{DEPLOY_ID}"))
        self.assertEqual(self.sent_json(), {"deployResult": {"status": "Canceling"}})
        self.assertEqual(canceled["status"], 202)
        self.respond(transcript(201, {"validatedDeployRequestId": VALIDATED_ID, "id": DEPLOY_ID, "url": f"https://host{V}/metadata/deployRequest/{DEPLOY_ID}", "deployOptions": {"checkOnly": True, "singlePackage": True}}))
        quick = self.run_family("metadata", {"operation": "deploy_quick", "deploy_request_id": VALIDATED_ID, "body": {"validatedDeployRequestId": VALIDATED_ID}})
        self.assertEqual(self.sent()[:2], ("POST", V + f"/metadata/deployRequest/{VALIDATED_ID}"))
        self.assertEqual(self.sent_json(), {"validatedDeployRequestId": VALIDATED_ID})
        self.assertEqual(quick["status"], 201)
        self.respond(vendor_error(404, "NOT_FOUND", "No validated deployment qualifies for a quick deploy"))
        self.assertEqual(self.run_family("metadata", {"operation": "deploy_quick", "deploy_request_id": VALIDATED_ID, "body": {"validatedDeployRequestId": VALIDATED_ID}})["error_code"], "sf.not_found")

    def test_refusals_before_io(self) -> None:
        deploy_zip(self.imports / "release.zip")
        (self.imports / "notzip.zip").write_bytes(b"plain text, not a zip archive")
        outside = self.workspace / "outside.zip"
        deploy_zip(outside)
        options = self.OPTIONS
        cases: list[tuple[dict[str, Any], type[Exception]]] = [
            ({"operation": "deploy", "zip_path": str(self.imports / "release.zip")}, typed_families.TypedFamilyError),
            ({"operation": "deploy", "zip_path": str(self.imports / "release.zip"), "body": {"deployOptions": {"checkOnly": "yes"}}}, typed_families.TypedFamilyError),
            ({"operation": "deploy", "zip_path": str(self.imports / "release.zip"), "body": {"deployOptions": {"testLevel": "RunEverything"}}}, typed_families.TypedFamilyError),
            ({"operation": "deploy", "zip_path": str(self.imports / "release.zip"), "body": {"deployOptions": {"runTests": "HelloTest"}}}, typed_families.TypedFamilyError),
            ({"operation": "deploy", "zip_path": str(self.imports / "release.tar"), "body": options}, typed_families.TypedFamilyError),
            ({"operation": "deploy", "body": options}, typed_families.TypedFamilyError),
            ({"operation": "deploy", "zip_path": str(self.imports / "notzip.zip"), "body": options}, typed_families.TypedFamilyError),
            ({"operation": "deploy", "zip_path": str(outside), "body": options}, PathRefusedError),
            ({"operation": "deploy_status", "deploy_request_id": DEPLOY_ID, "query": {"includeDetails": "true"}}, typed_families.TypedFamilyError),
            ({"operation": "deploy_cancel", "deploy_request_id": "0Af-not-an-id"}, typed_families.TypedFamilyError),
            ({"operation": "deploy_quick", "deploy_request_id": VALIDATED_ID, "body": {"id": VALIDATED_ID}}, typed_families.TypedFamilyError),
        ]
        for params, error in cases:
            with self.subTest(params=params), patch("salesforce_plugin.client.subprocess.run") as run:
                with self.assertRaises(error):
                    typed_families.run_family("metadata", self.executor, params, self.zip_gate)
                run.assert_not_called()


class ContentFiles(Fixture):
    def source(self) -> Path:
        path = self.imports / "Quote Q-1001.pdf"
        path.write_bytes(FILE_BYTES)
        return path

    def test_upload_sends_base64_version_data(self) -> None:
        body = {"PathOnClient": "Quote Q-1001.pdf", "Title": "Quote Q-1001", "FirstPublishLocationId": "006D000000CDn8bIAD"}
        self.respond(transcript(201, {"id": VERSION_ID, "success": True, "errors": []}))
        result = self.run_family("content_files", {"operation": "upload", "file_path": str(self.source()), "body": body}, self.gates())
        method, path, _query, headers = self.sent()
        self.assertEqual((method, path, headers["Content-Type"]), ("POST", V + "/sobjects/ContentVersion", "application/json"))
        sent = self.sent_json()
        self.assertEqual(base64.b64decode(sent.pop("VersionData")), FILE_BYTES)
        self.assertEqual(sent, body)
        self.assertEqual((result["status"], result["body"]["id"]), (201, VERSION_ID))
        self.assertNotIn("failures", result)
        # A refused insert is the REST error array with a 400, not a 2xx SaveResult.
        self.respond(vendor_error(400, "FIELD_CUSTOM_VALIDATION_EXCEPTION", "Files must be shared to a record"))
        refused = self.run_family("content_files", {"operation": "upload", "file_path": str(self.source()), "body": {"PathOnClient": "Quote Q-1001.pdf", "ContentDocumentId": DOCUMENT_ID, "ReasonForChange": "Revised"}}, self.gates())
        self.assertEqual((refused["status"], refused["error_code"]), (400, "sf.bad_request"))
        self.assertEqual(base64.b64decode(self.sent_json()["VersionData"]), FILE_BYTES)

    def test_download_keeps_bytes_that_match_size_and_checksum(self) -> None:
        output = self.exports / "quote.pdf"
        self.respond(transcript(200, version_record(size=len(FILE_BYTES), checksum=hashlib.md5(FILE_BYTES, usedforsecurity=False).hexdigest())), (FILE_BYTES, 0))
        result = self.run_family("content_files", {"operation": "download", "content_version_id": VERSION_ID, "output_path": str(output)}, self.gates())
        method, path, query, headers = self.sent(0)
        self.assertEqual((method, path, headers["Accept"]), ("GET", V + f"/sobjects/ContentVersion/{VERSION_ID}", "application/json"))
        self.assertEqual(query["fields"], ["Id,ContentDocumentId,Title,PathOnClient,FileExtension,VersionNumber,ContentSize,Checksum"])
        method, path, _query, headers = self.sent(1)
        self.assertEqual((method, path), ("GET", V + f"/sobjects/ContentVersion/{VERSION_ID}/VersionData"))
        self.assertNotIn("Accept", headers)
        self.assertEqual(output.read_bytes(), FILE_BYTES)
        self.assertEqual((result["output_path"], result["bytes_written"]), (str(output.resolve()), len(FILE_BYTES)))
        self.assertEqual(result["body"]["ContentDocumentId"], DOCUMENT_ID)
        self.assertNotIn("integrity_advisory", result)
        self.assertEqual(sorted(path.name for path in self.exports.iterdir()), ["quote.pdf"])

    def test_download_mismatch_or_cli_failure_keeps_nothing(self) -> None:
        output = self.exports / "quote.pdf"
        good = hashlib.md5(FILE_BYTES, usedforsecurity=False).hexdigest()
        for record, stream in ((version_record(size=len(FILE_BYTES), checksum=good), (FILE_BYTES[:-3], 0)), (version_record(size=len(FILE_BYTES), checksum="0" * 32), (FILE_BYTES, 0)), (version_record(size=len(FILE_BYTES), checksum=good), (b"", 1))):
            with self.subTest(stream_code=stream[1], checksum=record["Checksum"]):
                self.respond(transcript(200, record), stream)
                with self.assertRaises(SalesforceServiceError):
                    self.run_family("content_files", {"operation": "download", "content_version_id": VERSION_ID, "output_path": str(output)}, self.gates())
                self.assertEqual(list(self.exports.iterdir()), [])

    def test_download_without_vendor_checksum_is_advisory(self) -> None:
        output = self.exports / "quote.pdf"
        self.respond(transcript(200, version_record(size=None, checksum=None)), (FILE_BYTES, 0))
        result = self.run_family("content_files", {"operation": "download", "content_version_id": VERSION_ID, "output_path": str(output)}, self.gates())
        self.assertIn("integrity_advisory", result)
        self.assertEqual(output.read_bytes(), FILE_BYTES)

    def test_download_of_missing_version_streams_nothing(self) -> None:
        self.respond(vendor_error(404, "NOT_FOUND", "The requested resource does not exist"))
        result = self.run_family("content_files", {"operation": "download", "content_version_id": VERSION_ID, "output_path": str(self.exports / "x.pdf")}, self.gates())
        self.assertEqual(result["error_code"], "sf.not_found")
        self.assertEqual(len(self.argvs), 1)
        self.assertEqual(list(self.exports.iterdir()), [])

    def test_delete_document(self) -> None:
        self.respond(no_content())
        self.run_family("content_files", {"operation": "delete_document", "content_document_id": DOCUMENT_ID})
        self.assertEqual(self.sent()[:2], ("DELETE", V + f"/sobjects/ContentDocument/{DOCUMENT_ID}"))

    def test_refusals_before_io(self) -> None:
        source = self.source()
        outside = self.workspace / "outside.pdf"
        outside.write_bytes(FILE_BYTES)
        cases: list[tuple[dict[str, Any], type[Exception]]] = [
            ({"operation": "upload", "file_path": str(source), "body": {"Title": "x"}}, typed_families.TypedFamilyError),
            ({"operation": "upload", "file_path": str(source), "body": {"PathOnClient": "a.pdf", "VersionData": "AAAA"}}, typed_families.TypedFamilyError),
            ({"operation": "upload", "body": {"PathOnClient": "a.pdf"}}, typed_families.TypedFamilyError),
            ({"operation": "upload", "file_path": str(outside), "body": {"PathOnClient": "a.pdf"}}, PathRefusedError),
            ({"operation": "upload", "file_path": str(self.imports / "missing.pdf"), "body": {"PathOnClient": "a.pdf"}}, PathRefusedError),
            ({"operation": "download", "content_version_id": VERSION_ID}, typed_families.TypedFamilyError),
            ({"operation": "download", "content_version_id": "068../x", "output_path": str(self.exports / "x.pdf")}, typed_families.TypedFamilyError),
            ({"operation": "delete_document", "content_document_id": ""}, typed_families.TypedFamilyError),
        ]
        for params, error in cases:
            with self.subTest(params=params), patch("salesforce_plugin.client.subprocess.run") as run:
                with self.assertRaises(error):
                    typed_families.run_family("content_files", self.executor, params, self.gates())
                run.assert_not_called()

    def test_download_outside_export_roots_reads_metadata_but_writes_nothing(self) -> None:
        self.respond(transcript(200, version_record(size=len(FILE_BYTES), checksum="0" * 32)))
        with self.assertRaises(PathRefusedError):
            self.run_family("content_files", {"operation": "download", "content_version_id": VERSION_ID, "output_path": str(self.workspace / "elsewhere.pdf")}, self.gates())
        self.assertEqual(len(self.argvs), 1)


class AuthErrors(Fixture):
    CALLS: tuple[tuple[str, dict[str, Any]], ...] = (
        ("tooling", {"operation": "composite", "body": {"compositeRequest": [{}]}}),
        ("bulk_jobs", {"operation": "query_result_pages", "job_id": JOB_ID}),
        ("reports", {"operation": "instance_delete", "report_id": REPORT_ID, "instance_id": INSTANCE_ID}),
        ("rest_resources", {"operation": "relationship_delete", "sobject": "Contact", "id": CONTACT_ID, "relationship": "Account"}),
        ("metadata", {"operation": "deploy_status", "deploy_request_id": DEPLOY_ID}),
        ("content_files", {"operation": "delete_document", "content_document_id": DOCUMENT_ID}),
    )

    def test_401_and_403_map_in_every_build5_operation(self) -> None:
        for family, params in self.CALLS:
            for status, code, expected in ((401, "INVALID_SESSION_ID", "sf.session_expired"), (403, "INSUFFICIENT_ACCESS", "sf.permission_denied")):
                with self.subTest(family=family, status=status):
                    self.respond(vendor_error(status, code, "Session expired or invalid" if status == 401 else "insufficient access rights on object id"))
                    self.assertEqual(self.run_family(family, params)["error_code"], expected)


class TransportHelpers(unittest.TestCase):
    def test_local_path_parameters_per_transfer(self) -> None:
        self.assertEqual([transfers.local_path_parameter(kind) for kind in ("zip_package", "file_upload", "file_download")], ["zip_path", "file_path", "output_path"])
        self.assertEqual(typed_families.local_paths("metadata"), ["zip_path"])
        self.assertEqual(typed_families.local_paths("content_files"), ["file_path", "output_path"])
        self.assertEqual(typed_families.local_paths("reports"), [])
        transfers.validate("file_download", {"output_path": "/x/y.bin"}, None)
        with self.assertRaises(typed_families.TypedFamilyError):
            transfers.validate("zip_package", {"zip_path": "/x/p.zip"}, {"deployOptions": {"singlePackage": 1}})

    def test_stream_rest_argv_and_failed_download(self) -> None:
        executor = SalesforceCliExecutor(MagicMock())
        executor._target_org = "bound-org"  # noqa: SLF001
        seen: list[list[str]] = []

        def invoke(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 1, "", "Error (1): request to https://example.my.salesforce.com failed, reason: 404")

        with patch("salesforce_plugin.client.subprocess.run", side_effect=invoke), self.assertRaises(SalesforceServiceError) as raised:
            executor.stream_rest("GET", V + f"/sobjects/ContentVersion/{VERSION_ID}/VersionData", "/tmp/out.bin", headers={"X-Trace": "1"})
        self.assertEqual(seen[0][1:5], ["api", "request", "rest", V + f"/sobjects/ContentVersion/{VERSION_ID}/VersionData"])
        self.assertEqual(seen[0][seen[0].index("--stream-to-file") + 1], "/tmp/out.bin")
        self.assertIn("X-Trace:1", seen[0])
        self.assertNotIn("salesforce.com", str(raised.exception))

    def test_stream_to_file_refuses_credential_headers_before_io(self) -> None:
        executor = MagicMock()
        with self.assertRaises(ValueError):
            request_actions.stream_to_file(executor, {"path": V + "/sobjects/ContentVersion", "query": {}, "headers": {"Authorization": "Bearer x"}}, "/tmp/out.bin")
        executor.stream_rest.assert_not_called()


class Wiring(unittest.TestCase):
    def test_dispatch_validates_then_queues(self) -> None:
        plugin = SalesforcePlugin()
        manager = MagicMock()
        manager.create_job.return_value = {"action_status": "completed", "data": {"job_id": "j1", "status": "queued"}}
        plugin._async_job_manager = manager  # noqa: SLF001
        state = {"session_id": "s", "flow_id": "f"}
        with patch.object(async_jobs, "ensure_worker_started"):
            for family, bad in (("metadata", {"operation": "deploy", "zip_path": "/x/p.zip"}), ("content_files", {"operation": "upload", "file_path": "/x/a.pdf", "body": {"Title": "x"}})):
                with self.subTest(family=family):
                    self.assertEqual(getattr(plugin, family)(bad, state)["error"]["code"], "sf.invalid_params")
            queued = plugin.metadata({"operation": "deploy_status", "deploy_request_id": DEPLOY_ID}, state)
        self.assertEqual(queued["data"], {"job_id": "j1", "status": "queued"})
        self.assertEqual(manager.create_job.call_count, 1)

    def test_worker_gates_bind_the_configured_roots(self) -> None:
        with tempfile.TemporaryDirectory() as imports, tempfile.TemporaryDirectory() as exports:
            plugin = SalesforcePlugin()
            config = {}
            _bind_locations(plugin, [imports, exports])
            Path(imports, "p.zip").write_bytes(b"PK")
            Path(imports, "a.pdf").write_bytes(b"x")
            with patch.object(plugin, "_load_plugin_config", return_value=config):
                zip_gate = async_jobs.ACTION_HANDLERS["metadata"][1](plugin)  # type: ignore[misc]
                file_gates = async_jobs.ACTION_HANDLERS["content_files"][1](plugin)  # type: ignore[misc]
                self.assertEqual(zip_gate(os.path.join(imports, "p.zip")), os.path.realpath(os.path.join(imports, "p.zip")))
                with self.assertRaises(PathRefusedError):
                    zip_gate(os.path.join(imports, "a.pdf"))
                self.assertIsInstance(file_gates, transfers.TransferGates)
                self.assertEqual(file_gates.read(os.path.join(imports, "a.pdf")), os.path.realpath(os.path.join(imports, "a.pdf")))
                self.assertEqual(file_gates.write(os.path.join(exports, "b.bin")), os.path.realpath(os.path.join(exports, "b.bin")))
                self.assertEqual(file_gates.write(os.path.join(imports, "b.bin")), os.path.realpath(os.path.join(imports, "b.bin")))
                outside = Path(imports).parent / (Path(imports).name + "-unregistered")
                with self.assertRaises(PathRefusedError):
                    file_gates.write(str(outside / "b.bin"))
                escape = Path(imports) / "escape"
                escape.symlink_to(outside, target_is_directory=True)
                for path in (escape / "b.bin", Path(imports) / ".." / outside.name / "b.bin", escape / "missing" / "b.bin", _LOCATION_TREE.repo_root / "b.bin", _LOCATION_TREE.app_home / "b.bin"):
                    with self.subTest(path=path), self.assertRaises(PathRefusedError):
                        file_gates.write(str(path))
                shared = Path(exports) / "existing.bin"
                shared.write_bytes(b"shared read root")
                self.assertEqual(file_gates.read(str(shared)), str(shared.resolve()))
                with self.assertRaises(PathRefusedError):
                    file_gates.read(os.path.join(exports, "missing.bin"))


if __name__ == "__main__":
    unittest.main()
