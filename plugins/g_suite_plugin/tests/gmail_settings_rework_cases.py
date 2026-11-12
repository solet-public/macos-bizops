"""Gmail settings query and discovery regressions, run by the registered smoke."""

from __future__ import annotations

import json
import unittest
from typing import Any

from smoke_gmail_settings import (
    BASE,
    DISCOVERY,
    GMAIL_SETTINGS_VERBS,
    KB_DIR,
    STANDARD_QUERY_FIELDS,
    _call,
    _fidelity_problems,
    _sample,
    settings_process,
)


class GmailSettingsReworkTests(unittest.TestCase):
    """Query and discovery coverage for the settings verbs."""

    def test_chain_validation_and_common_queries_reach_real_http_request(self) -> None:
        body = {"pkcs7": "public-certificate", "privateKeyMetadata": [{"hardwareKeyMetadata": {"description": "historical key"}}]}
        for validation in ("all", "none", None):
            params: dict[str, Any] = {"pkcs7": body["pkcs7"], "private_key_metadata": body["privateKeyMetadata"], "fields": "keyPairId,enablementState", "pretty_print": False, "quota_user": "work-user"}
            if validation is not None:
                params["chain_validation"] = validation
            result, requests = _call("gmail_create_cse_key_pair", params, (200, {"keyPairId": "kp-historical", "enablementState": "disabled"}))
            request = requests[0]
            self.assertEqual((request["method"], request["path"], request["body"]), ("POST", f"{BASE}/settings/cse/keypairs", body))
            query = {"fields": ["keyPairId,enablementState"], "prettyPrint": ["false"], "quotaUser": ["work-user"]}
            if validation is not None:
                query["chainValidation"] = [validation]
            self.assertEqual(request["query"], query)
            self.assertEqual(result["key_pair_id"], "kp-historical")
        with self.assertRaisesRegex(ValueError, "'chain_validation' must be one of"):
            _call("gmail_create_cse_key_pair", {"chain_validation": "invalid", "pkcs7": "certificate", "private_key_metadata": [{}]})

    def test_live_discovery_snapshot_and_api_wide_metadata(self) -> None:
        self.assertEqual(DISCOVERY["revision"], "20261102")
        for verb, spec in GMAIL_SETTINGS_VERBS.items():
            with self.subTest(verb=verb):
                self.assertEqual(_fidelity_problems(spec), [])
                declared = settings_process(verb)["parameters"]
                for field in STANDARD_QUERY_FIELDS:
                    self.assertIn(field.param, declared)
                    self.assertFalse(declared[field.param].required)
                    self.assertEqual(DISCOVERY["parameters"][field.api]["location"], "query")
                for param, value in (("fields", False), ("pretty_print", "false"), ("quota_user", 1)):
                    with self.assertRaises(ValueError):
                        _call(verb, {**{f.param: _sample(f) for f in spec.fields}, param: value})
        declared = settings_process("gmail_create_cse_key_pair")["parameters"]
        self.assertIn("chain_validation", declared)
        self.assertIn("none", declared["chain_validation"].description)
        doc = json.loads((KB_DIR / "gmail_create_cse_key_pair.json").read_text())
        self.assertIn("chain_validation", doc["description"])
