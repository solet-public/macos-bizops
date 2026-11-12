#!/usr/bin/env python3
"""Folder/My Token transcripts through dispatch, real HTTP client, worker and job poll."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins/marketo_plugin/src"))
sys.path.insert(0, str(Path(__file__).parent))

from lead_database_objects_smoke import STATE, _VendorTranscriptCase  # noqa: E402  # pyright: ignore[reportMissingImports]
from marketo_plugin import asset_api, asset_process_metadata, completion_templates  # noqa: E402
from marketo_plugin.constants import CHECK_SETUP_UNVERIFIED_WRITE_VERBS  # noqa: E402
from marketo_plugin.plugin import MarketoPlugin  # noqa: E402


def _envelope(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {"requestId": "111be#14e1f193e31", "success": True, "warnings": [], "errors": [], "result": records}


def _folder(folder_id: int = 454, **changes: Any) -> dict[str, Any]:
    return {
        "id": folder_id,
        "name": "Launch",
        "description": "Campaign assets",
        "folderId": {"id": folder_id, "type": "Folder"},
        "parent": {"id": 14, "type": "Folder"},
        "folderType": "Marketing Folder",
        "path": "/Marketing Activities/Default/Launch",
        "isArchive": False,
        "isSystem": False,
        "workspace": "Default",
        "createdAt": "2015-06-23T06:27:04Z+0000",
        "updatedAt": "2015-06-23T06:27:04Z+0000",
        **changes,
    }


def _form(request: httpx.Request) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(request.content.decode(), keep_blank_values=True).items()}


class AssetApiTests(_VendorTranscriptCase):
    def test_all_ten_endpoints_have_three_registered_artifacts(self) -> None:
        from ananta.core.plugins.action_discovery import discover_actions
        from ananta.core.process_registry.plugin_registration_validator import PluginRegistrationValidator

        plugin = MarketoPlugin()
        actions = {action.name: action for action in discover_actions(plugin)}
        PluginRegistrationValidator().validate_edge_process_provider("marketo_plugin", plugin, discover_actions(plugin))
        self.assertEqual(len(asset_api.ENDPOINTS), 10)
        for verb, (method, _, _) in asset_api.ENDPOINTS.items():
            with self.subTest(verb=verb):
                self.assertTrue(callable(getattr(plugin, verb)))
                self.assertIn(verb, plugin.get_edge_process_definitions())
                kb = json.loads((REPO_ROOT / "plugins/marketo_plugin/knowledge_base/processes" / f"{verb}.json").read_text())
                self.assertEqual(kb["process_key"], f"plugin::marketo_plugin::{verb}")
                self.assertEqual(kb["result_processor_customizations"]["result_type"], asset_api.result_type(verb))
                self.assertTrue(200 <= len(kb["embedding_description"]) <= 400)
                self.assertEqual(asset_api.is_write(verb), method == "POST")
                self.assertEqual(set(asset_process_metadata.parameters(verb)), set(actions[verb].parameters))

    def test_folder_create_correct_clear_archive_reopen_and_delete(self) -> None:
        current: dict[str, Any] = {}

        def vendor(request: httpx.Request) -> httpx.Response:
            values = _form(request)
            if request.url.path.endswith("/delete.json"):
                current.clear()
                return httpx.Response(200, json=_envelope([{"id": 454}]))
            if request.method == "POST":
                if request.url.path.endswith("/folders.json"):
                    current.update(_folder(name=values["name"], parent=json.loads(values["parent"])))
                current.update({key: values[key] for key in ("name", "description") if key in values})
                if "isArchive" in values:
                    current["isArchive"] = values["isArchive"] == "true"
            return httpx.Response(200, json=_envelope([dict(current)] if current else []))

        self.vendor = vendor
        create, _ = self._run_job("create_asset_folder", {"name": "Launch & Learn", "parent": {"id": 14, "type": "Folder"}, "description": "Quarterly"})
        self.assertEqual(create["result"]["result"][0]["name"], "Launch & Learn")
        call = self._vendor_calls()[-1]
        self.assertEqual((call.method, call.url.path), ("POST", "/rest/asset/v1/folders.json"))
        self.assertEqual(call.headers["Content-Type"], "application/x-www-form-urlencoded")
        self.assertEqual(_form(call), {"name": "Launch & Learn", "parent": '{"id":14,"type":"Folder"}', "description": "Quarterly"})
        for archived in (True, False):
            result, _ = self._run_job("update_asset_folder", {"folder_id": 454, "folder_type": "Folder", "name": "Corrected", "description": "", "is_archive": archived})
            self.assertEqual(result["result"]["result"][0]["description"], "")
            self.assertEqual(result["result"]["result"][0]["isArchive"], archived)
            self.assertEqual(_form(self._vendor_calls()[-1])["isArchive"], str(archived).lower())
        read, _ = self._run_job("get_asset_folder", {"folder_id": 454, "folder_type": "Folder", "output_tsv_path": self._tsv("folder.tsv")})
        self.assertEqual(self._vendor_calls()[-1].url.params["type"], "Folder")
        self.assertIn("Corrected", Path(read["result"]["path"]).read_text())
        deleted, _ = self._run_job("delete_asset_folder", {"folder_id": 454, "folder_type": "Folder"})
        self.assertEqual(deleted["result"]["result"], [{"id": 454}])
        self.assertEqual(_form(self._vendor_calls()[-1]), {"type": "Folder"})
        self.assertEqual(current, {})

    def test_folder_name_query_forwards_all_vendor_parameters(self) -> None:
        self.vendor = lambda _: httpx.Response(200, json=_envelope([_folder()]))
        result, _ = self._run_job("get_asset_folder_by_name", {"name": "Launch", "folder_type": "Folder", "root": "14", "workspace": "Default", "output_tsv_path": self._tsv("name.tsv")})
        self.assertEqual(result["result"]["row_count"], 1)
        call = self._vendor_calls()[-1]
        self.assertEqual(call.url.path, "/rest/asset/v1/folder/byName.json")
        self.assertEqual(dict(call.url.params), {"name": "Launch", "type": "Folder", "root": "14", "workSpace": "Default"})

    def test_folder_browse_and_content_page_internally_with_all_parameters(self) -> None:
        def vendor(request: httpx.Request) -> httpx.Response:
            offset = int(request.url.params["offset"])
            return httpx.Response(200, json=_envelope([_folder(offset)] if offset < 2 else []))

        self.vendor = vendor
        for verb, extras, path in (
            ("list_asset_folders", {"root": {"id": 14, "type": "Folder"}, "workspace": "Default", "max_depth": 0}, "/rest/asset/v1/folders.json"),
            ("get_asset_folder_content", {"folder_id": 14, "folder_type": "Program"}, "/rest/asset/v1/folder/14/content.json"),
        ):
            with self.subTest(verb=verb):
                before = len(self._vendor_calls())
                result, _ = self._run_job(verb, {**extras, "offset": 0, "max_return": 1, "output_tsv_path": self._tsv(f"{verb}.tsv")})
                calls = self._vendor_calls()[before:]
                self.assertEqual([(c.url.path, c.url.params["offset"]) for c in calls], [(path, str(n)) for n in (0, 1, 2)])
                self.assertEqual((result["result"]["row_count"], result["result"]["truncated"]), (2, False))
                self.assertEqual(len(result["result"]["envelopes"]), 3)
                if verb == "list_asset_folders":
                    self.assertEqual(json.loads(calls[0].url.params["root"]), extras["root"])
                    self.assertEqual(calls[0].url.params["maxDepth"], "0")
                    self.assertEqual(calls[0].url.params["workSpace"], "Default")

    def test_truncation_hint_resumes_without_skipping_a_partial_page(self) -> None:
        self.vendor = lambda request: httpx.Response(200, json=_envelope([_folder(n) for n in range(int(request.url.params["offset"]), int(request.url.params["offset"]) + 200)]))
        result, _ = self._run_job("list_asset_folders", {"output_tsv_path": self._tsv("limited.tsv"), "offset": 20})
        self.assertEqual(len(self._vendor_calls()), 3)
        self.assertEqual((result["result"]["row_count"], result["result"]["truncated"], result["result"]["next_offset"]), (500, True, 520))

    def test_token_set_correct_clear_and_delete_for_both_parent_types(self) -> None:
        for parent_type in ("Folder", "Program"):
            for value in ("Original & value", "Correction", ""):
                self.vendor = lambda request, parent_type=parent_type: httpx.Response(200, json=_envelope([{"folder": {"type": parent_type, "value": 416}, "tokens": [{"name": _form(request)["name"], "type": "text", "value": _form(request)["value"]}]}]))
                result, _ = self._run_job("set_asset_token", {"folder_id": 416, "folder_type": parent_type, "name": "Launch", "token_type": "text", "value": value})
                call = self._vendor_calls()[-1]
                self.assertEqual(call.url.path, "/rest/asset/v1/folder/416/tokens.json")
                self.assertEqual(_form(call), {"folderType": parent_type, "name": "Launch", "type": "text", "value": value})
                self.assertEqual(result["result"]["result"][0]["tokens"][0]["value"], value)
            self.vendor = lambda _: httpx.Response(200, json=_envelope([{"id": 416}]))
            result, _ = self._run_job("delete_asset_token", {"folder_id": 416, "folder_type": parent_type, "name": "Launch", "token_type": "text"})
            self.assertEqual(result["result"]["result"], [{"id": 416}])
            self.assertEqual(self._vendor_calls()[-1].url.path, "/rest/asset/v1/folder/416/tokens/delete.json")
            self.assertEqual(_form(self._vendor_calls()[-1]), {"folderType": parent_type, "name": "Launch", "type": "text"})

    def test_last_write_wins_recreation_and_historical_job_readback(self) -> None:
        tokens: dict[str, str] = {}

        def vendor(request: httpx.Request) -> httpx.Response:
            values = _form(request)
            if request.url.path.endswith("/delete.json"):
                tokens.clear()
                return httpx.Response(200, json=_envelope([{"id": 416}]))
            if request.method == "POST":
                tokens["value"] = values["value"]
            return httpx.Response(200, json=_envelope([{"folder": {"type": "Folder", "value": 416}, "tokens": [{"name": "Launch", "type": "text", "value": tokens.get("value", "")}]}]))

        self.vendor = vendor
        plugin, _ = self._plugin()
        params = {"folder_id": 416, "folder_type": "Folder", "name": "Launch", "token_type": "text"}
        handles: list[str] = []
        for value in ("first", "competing correction", ""):
            queued = plugin.set_asset_token({**params, "value": value}, STATE)
            handles.append(queued["data"]["job_id"])
            plugin._process_pending_jobs()  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(tokens["value"], value)
            self.assertNotIn("If-Match", self._vendor_calls()[-1].headers)
        plugin.delete_asset_token(params, STATE)
        plugin._process_pending_jobs()  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(tokens, {})
        plugin.set_asset_token({**params, "value": "first"}, STATE)
        plugin._process_pending_jobs()  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(tokens, {"value": "first"})
        historical = plugin.check_marketo_job_status({"job_id": handles[1]}, {})
        self.assertEqual(historical["data"]["result"]["result"][0]["tokens"][0]["value"], "competing correction")

    def test_every_vendor_creation_token_type_and_export_containment(self) -> None:
        self.vendor = lambda _: httpx.Response(200, json=_envelope([{"folder": {"type": "Folder", "value": 416}, "tokens": []}]))
        for token_type in asset_api.TOKEN_TYPES:
            result, _ = self._run_job("set_asset_token", {"folder_id": 416, "folder_type": "Folder", "name": "Example", "token_type": token_type, "value": "1"})
            self.assertEqual(result["status"], "completed")
            self.assertEqual(_form(self._vendor_calls()[-1])["type"], token_type)
        plugin, manager = self._plugin()
        refused = plugin.get_asset_tokens({"folder_id": 416, "output_tsv_path": "/outside/roots.tsv"}, STATE)
        self.assertEqual(refused["action_status"], "error")
        self.assertEqual(manager._jobs, {})  # pyright: ignore[reportPrivateUsage]

    def test_tokens_read_exports_real_nested_envelope_and_vendor_default(self) -> None:
        self.vendor = lambda _: httpx.Response(200, json=_envelope([{"folder": {"type": "Folder", "value": 416}, "tokens": [{"name": "Launch", "type": "text", "value": "Fall"}]}]))
        for extras in ({}, {"folder_type": "Program"}):
            result, _ = self._run_job("get_asset_tokens", {"folder_id": 416, "output_tsv_path": self._tsv("tokens.tsv"), **extras})
            self.assertEqual(dict(self._vendor_calls()[-1].url.params), {"folderType": "Program"} if extras else {})
            self.assertIn("Fall", Path(result["result"]["path"]).read_text())

    def test_validation_refuses_malformed_vendor_inputs_before_job_or_http(self) -> None:
        cases = [
            ("get_asset_folder", {"folder_id": "../1", "folder_type": "Folder"}),
            ("get_asset_folder", {"folder_id": True, "folder_type": "Folder"}),
            ("get_asset_folder_by_name", {"name": "Launch", "root": "14"}),
            ("list_asset_folders", {"max_return": 201}),
            ("list_asset_folders", {"offset": -1}),
            ("list_asset_folders", {"max_depth": -1}),
            ("list_asset_folders", {"root": {"id": 14, "type": "Foreign"}}),
            ("create_asset_folder", {"name": "Launch", "parent": {"id": 14}, "description": "x"}),
            ("create_asset_folder", {"name": "Launch", "parent": {"id": 14, "type": "Folder"}, "description": "x" * 2001}),
            ("update_asset_folder", {"folder_id": 416, "folder_type": "Folder", "is_archive": "false"}),
            ("set_asset_token", {"folder_id": 416, "folder_type": "Folder", "name": "x" * 51, "token_type": "text", "value": ""}),
            ("set_asset_token", {"folder_id": 416, "folder_type": "Folder", "name": "x", "token_type": "unsupported", "value": "x"}),
        ]
        for verb, params in cases:
            plugin, manager = self._plugin()
            refused = getattr(plugin, verb)({"output_tsv_path": self._tsv("refused.tsv"), **params}, STATE)
            self.assertEqual(refused["action_status"], "error", (verb, refused))
            self.assertEqual(manager._jobs, {})  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.seen, [])

    def test_vendor_error_http_auth_and_nonempty_folder_refusal_are_classified(self) -> None:
        for status, body, code in (
            (200, {"requestId": "4180#14e1f3fc017", "success": False, "errors": [{"code": "1003", "message": "Folder is not empty"}]}, "marketo.validation_failed"),
            (200, {"success": False, "errors": [{"code": "603", "message": "Access denied"}]}, "marketo.permission_denied"),
            (401, {"message": "Unauthorized"}, "marketo.auth_failed"),
            (403, {"message": "Forbidden"}, "marketo.permission_denied"),
            (503, {"message": "Unavailable"}, "marketo.api_error"),
        ):
            self.vendor = lambda _, status=status, body=body: httpx.Response(status, json=body)
            result, _ = self._run_job("delete_asset_folder", {"folder_id": 416, "folder_type": "Folder"})
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["error"]["code"], code)

    def test_asset_completion_has_vendor_envelope_guidance_and_write_permissions_stay_unverified(self) -> None:
        for verb in asset_api.ENDPOINTS:
            if asset_api.is_write(verb):
                self.assertIn(verb, CHECK_SETUP_UNVERIFIED_WRITE_VERBS)
        prompt = json.dumps(completion_templates.build_completion_template("result", "asset set_asset_token", result_kind="asset"))
        self.assertIn("requestId", prompt)
        self.assertIn("no per-record tallies", prompt)


if __name__ == "__main__":
    unittest.main()
