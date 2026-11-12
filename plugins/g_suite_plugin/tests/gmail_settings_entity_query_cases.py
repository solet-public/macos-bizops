"""API-wide Gmail query regressions, run by the Gmail settings smoke."""

from __future__ import annotations

import unittest
from typing import Any
from urllib.parse import parse_qs, urlsplit

from googleapiclient.discovery import build
from smoke_gmail_settings import _WORKSPACE_FACTS, GMAIL_SETTINGS_VERBS, WORKSPACE, RecordingHttp, _call, _sample, run_settings_verb, settings_process


class WireHttp(RecordingHttp):
    """Retain the unfiltered wire query, including the client's alt parameter."""

    query: dict[str, list[str]]

    def request(self, uri: str, method: str = "GET", body: Any = None, headers: Any = None, **kwargs: Any) -> tuple[Any, bytes]:
        self.query = parse_qs(urlsplit(uri).query)
        return super().request(uri, method=method, body=body, headers=headers, **kwargs)


class GmailSettingsEntityQueryTests(unittest.TestCase):
    def test_xgafv_and_json_reach_the_wire_for_all_verbs(self) -> None:
        for verb, spec in GMAIL_SETTINGS_VERBS.items():
            for version in ("1", "2", None):
                with self.subTest(verb=verb, version=version):
                    params = {f.param: _sample(f) for f in spec.fields}
                    params.update(fields="id", pretty_print=False, quota_user="test-user", alt="json", xgafv=version)
                    http = WireHttp([(200, {})])
                    gmail = build("gmail", "v1", http=http, cache_discovery=False, static_discovery=True)
                    run_settings_verb(spec, lambda gmail=gmail: gmail, params, _WORKSPACE_FACTS, WORKSPACE)
                    self.assertEqual(http.query.get("$.xgafv"), [version] if version else None)
                    self.assertEqual(http.query.get("alt"), ["json"])
                    self.assertEqual(http.query["fields"], ["id"])
                    self.assertEqual(http.query["prettyPrint"], ["false"])
                    self.assertEqual(http.query["quotaUser"], ["test-user"])
                    self.assertIn("xgafv", settings_process(verb)["parameters"])

    def test_non_json_alt_and_invalid_xgafv_fail_before_http(self) -> None:
        for verb, spec in GMAIL_SETTINGS_VERBS.items():
            params = {f.param: _sample(f) for f in spec.fields}
            params.update({name: "private-material-7qz" for name in ("smtp_password", "pkcs12", "encrypted_key_password") if name in params})
            for alt in ("proto", "media"):
                with self.subTest(verb=verb, alt=alt), self.assertRaisesRegex(ValueError, "alt.*json"):
                    run_settings_verb(spec, lambda: self.fail("non-JSON format reached the client"), {**params, "alt": alt}, _WORKSPACE_FACTS, WORKSPACE)
            with self.subTest(verb=verb), self.assertRaisesRegex(ValueError, "xgafv.*1.*2"):
                _call(verb, {**params, "xgafv": "3"})
