#!/usr/bin/env python3
"""Gmail build 2 smoke: 47 settings/push verbs through the REAL google-api-python-client.

Like smoke_gmail_transcripts.py, the Gmail service is built from the discovery
document bundled with google-api-python-client over a recording httplib2
stand-in, so each check asserts the exact HTTP method, REST path, query and
JSON body the client sent and feeds back a real-shaped Gmail response or a
real Google error body. Vendor fidelity uses a separately retained projection
of live discovery revision 20261102, including chainValidation absent from the
installed client. No network, no credentials.

Checks:
  1. Vendor fidelity: every row matches its discovery method (HTTP method,
     path, path and query parameters, every writable body field, enums and the
     narrowest scope), so no documented parameter is rejected.
  2. Transcripts: every verb's request with all of its parameters set, plus
     hand-written transcripts for the shapes that matter (vacation, filters,
     watch, send-as SMTP, S/MIME, CSE paging, key pair state changes).
  3. Validation refusals happen before any request.
  4. Account contract through the plugin: result names the account, the
     account argument picks it, an unknown account is gsuite.unknown_account.
  5. Tier: Workspace-only verbs refuse a consumer account with one message
     naming the tier and send nothing; the same verb runs on a Workspace
     account and on an account whose tier is unknown.
  6. Scope hints for gmail.settings.sharing and gmail.settings.basic 403s.
  7. Results as received, keys in snake_case; vendor keys that meet after
     normalisation keep every value under `` (N)`` suffixes.
  8. Registration: decorator + EDGE + producer + KB JSON for every verb.

Run:
    SOLET_NAME=<name> .venv/bin/python3 plugins/g_suite_plugin/tests/smoke_gmail_settings.py
"""

from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httplib2

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "g_suite_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from g_suite_plugin.gmail_settings_actions import ERROR_WORKSPACE_ONLY, GMAIL_SETTINGS_VERBS, SettingsVerbError, run_settings_verb  # noqa: E402
from g_suite_plugin.gmail_settings_processes import GmailSettingsProcesses, settings_process  # noqa: E402
from g_suite_plugin.gmail_settings_spec import BOOL, INT, INT64, OBJECT_LIST, STANDARD_QUERY_FIELDS, STR, STR_LIST, Field, SettingsVerb, paging_fields  # noqa: E402
from g_suite_plugin.gmail_sharing_processes import GmailSharingProcesses  # noqa: E402
from g_suite_plugin.plugin import GSuitePlugin  # noqa: E402
from googleapiclient.discovery import build  # noqa: E402
from gsuite_fakes import connect_accounts  # noqa: E402  # pyright: ignore[reportMissingImports]

KB_DIR = REPO_ROOT / "plugins" / "g_suite_plugin" / "knowledge_base" / "processes"
DISCOVERY = json.loads((Path(__file__).parent / "fixtures" / "gmail_settings_discovery.json").read_text())
BASE = "/gmail/v1/users/me"
CONSUMER = "ann@gmail.com"
WORKSPACE = "bob@acme.example"
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


class RecordingHttp:
    """httplib2.Http stand-in: records each request, replays scripted (status, body)."""

    def __init__(self, responses: list[tuple[int, Any]]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def request(self, uri: str, method: str = "GET", body: Any = None, headers: Any = None, **_: Any) -> tuple[httplib2.Response, bytes]:
        parts = urlsplit(uri)
        payload = json.loads(body) if isinstance(body, (str, bytes)) and body else None
        query = {k: v for k, v in parse_qs(parts.query).items() if k != "alt"}
        self.requests.append({"method": method, "path": unquote(parts.path), "query": query, "body": payload})
        status, content = self.responses.pop(0)
        raw = b"" if content is None else json.dumps(content).encode()
        return httplib2.Response({"status": str(status), "content-type": "application/json"}), raw


def _gmail(*responses: tuple[int, Any]) -> tuple[Any, RecordingHttp]:
    http = RecordingHttp(list(responses))
    return build("gmail", "v1", http=http, cache_discovery=False, static_discovery=True), http


_WORKSPACE_FACTS: dict[str, Any] = {"account_tier": "workspace_user", "granted_scopes": None}


def _call(verb: str, params: dict[str, Any], *responses: tuple[int, Any], facts: dict[str, Any] | None = None) -> tuple[Any, list[dict[str, Any]]]:
    gmail, http = _gmail(*responses)
    return run_settings_verb(GMAIL_SETTINGS_VERBS[verb], lambda: gmail, params, facts or _WORKSPACE_FACTS, WORKSPACE), http.requests


# ---------------------------------------------------------------------------
# 1. Vendor fidelity against the discovery document
# ---------------------------------------------------------------------------


def _discovery_method(chain: tuple[str, ...]) -> dict[str, Any]:
    node: dict[str, Any] = DISCOVERY["resources"]["users"]
    for name in chain[:-1]:
        node = node["resources"][name]
    return node["methods"][chain[-1]]


def _writable(ref: str, prefix: str = "") -> dict[str, tuple[str, ...]]:
    """Writable body paths of a schema -> their enum (empty when none)."""
    out: dict[str, tuple[str, ...]] = {}
    for key, prop in DISCOVERY["schemas"][ref].get("properties", {}).items():
        if prop.get("readOnly"):
            continue
        nested = prop.get("$ref")
        if nested and DISCOVERY["schemas"][nested].get("properties"):
            out.update(_writable(nested, f"{prefix}{key}."))
        else:
            out[prefix + key] = tuple(prop.get("enum") or ())
    return out


def _param_problems(spec: SettingsVerb, method: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    rest = f"{method['httpMethod']} " + method["path"].replace("gmail/v1/users/{userId}/", "")
    if rest != spec.rest:
        problems.append(f"rest {rest} != {spec.rest}")
    params = method.get("parameters", {})
    for location, fields in (("path", spec.path), ("query", spec.query)):
        documented = {k for k, v in params.items() if v["location"] == location and k != "userId"}
        if documented != {f.api for f in fields}:
            problems.append(f"{location} {sorted(documented)} != {[f.api for f in fields]}")
        problems.extend(_enum_problems(location, fields, params))
    return problems


def _enum_problems(location: str, fields: tuple[Field, ...], params: dict[str, Any]) -> list[str]:
    return [f"{location} enum {field.api}" for field in fields if field.enum != tuple(params.get(field.api, {}).get("enum") or ())]


def _body_problems(spec: SettingsVerb, method: dict[str, Any]) -> list[str]:
    request = method.get("request", {}).get("$ref")
    if bool(request) != spec.has_body:
        return [f"has_body {spec.has_body} but request {request}"]
    if not request:
        return []
    documented = _writable(request)
    declared = {f.api: f.enum for f in spec.body}
    problems = [f"body {sorted(documented)} != {sorted(declared)}"] if set(documented) != set(declared) else []
    return problems + [f"enum {api}" for api, enum in documented.items() if api in declared and declared[api] != enum]


def _fidelity_problems(spec: SettingsVerb) -> list[str]:
    method = _discovery_method(spec.chain)
    scope = [] if spec.scope in method.get("scopes", []) else [f"scope {spec.scope} not accepted"]
    return _param_problems(spec, method) + _body_problems(spec, method) + scope


def check_vendor_fidelity() -> None:
    _assert("47 build-2 verbs declared", len(GMAIL_SETTINGS_VERBS) == 47, str(len(GMAIL_SETTINGS_VERBS)))
    for verb, spec in GMAIL_SETTINGS_VERBS.items():
        problems = _fidelity_problems(spec)
        _assert(f"{verb}: matches Gmail discovery (method, path, params, body, enums, scope)", not problems, "; ".join(problems))
    covered = {".".join(spec.chain) for spec in GMAIL_SETTINGS_VERBS.values()}
    _assert("watch, stop and every users.settings method covered", covered >= {"watch", "stop"} and len([c for c in covered if c.startswith("settings")]) == 45)


# ---------------------------------------------------------------------------
# 2. Transcripts
# ---------------------------------------------------------------------------

_SAMPLES: dict[str, Any] = {STR: "v", BOOL: True, INT: 7, INT64: 1730000000000, STR_LIST: ["Label_1"], OBJECT_LIST: [{"hardwareKeyMetadata": {"description": "key"}}]}


def _sample(field: Field) -> Any:
    return field.enum[-1] if field.enum else _SAMPLES[field.kind]


def _policy_params(spec: SettingsVerb) -> dict[str, Any]:
    params = {field.param: _sample(field) for field in spec.fields}
    params.update({name: "private-material-7qz" for name in ("smtp_password", "pkcs12", "encrypted_key_password") if name in params})
    return params


def _expected_body(spec: SettingsVerb, params: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {}
    for field in spec.body:
        value = params[field.param]
        node = body
        *parents, leaf = field.api.split(".")
        for name in parents:
            node = node.setdefault(name, {})
        node[leaf] = str(value) if field.kind == INT64 else value
    return body


def _scripted_response(spec: SettingsVerb) -> tuple[int, Any]:
    if spec.empty_result:
        return 204, None
    if spec.list_key:
        return 200, {spec.list_key: [{"someField": 1}]}
    return 200, {"someField": {"nestedValue": 2}}


def _check_request(verb: str, spec: SettingsVerb, params: dict[str, Any], req: dict[str, Any]) -> None:
    method, path = spec.rest.split(" ", 1)
    for field in spec.path:
        path = path.replace("{" + field.api + "}", params[field.param])
    expected_query = {field.api: [str(params[field.param]).lower() if field.kind == BOOL else str(params[field.param])] for field in (*spec.query, *STANDARD_QUERY_FIELDS) if field.api != "alt"}
    ok = req["method"] == method and req["path"] == f"{BASE}/{path}" and req["query"] == expected_query
    _assert(f"{verb}: {method} {path} with every query parameter", ok, str(req))
    if spec.has_body:
        _assert(f"{verb}: every body field sent", req["body"] == _expected_body(spec, params), f"{req['body']} != {_expected_body(spec, params)}")


def _check_result(verb: str, spec: SettingsVerb, params: dict[str, Any], result: dict[str, Any]) -> None:
    if spec.list_key:
        _assert(f"{verb}: rows under {spec.rows_name}, snake_case", result[spec.rows_name] == [{"some_field": 1}] and result["count"] == 1, str(result))
    elif spec.empty_result:
        echoed = all(result[f.param] == params[f.param] for f in spec.path)
        _assert(f"{verb}: empty response -> {spec.empty_result}=true with path ids", result.get(spec.empty_result) is True and echoed, str(result))
    else:
        _assert(f"{verb}: resource returned snake_case", result.get("some_field") == {"nested_value": 2}, str(result))


def _check_every_verb(verb: str, spec: SettingsVerb) -> None:
    params = _policy_params(spec)
    result, reqs = _call(verb, params, _scripted_response(spec))
    _check_request(verb, spec, params, reqs[0])
    _check_result(verb, spec, params, result)


def check_every_verb() -> None:
    for verb, spec in GMAIL_SETTINGS_VERBS.items():
        _check_every_verb(verb, spec)


VACATION = {"enableAutoReply": True, "responseSubject": "Away", "responseBodyPlainText": "Back Monday", "restrictToContacts": False, "restrictToDomain": False, "startTime": "1730000000000", "endTime": "1730600000000"}
SEND_AS = {"sendAsEmail": "bob@acme.example", "displayName": "Bob", "replyToAddress": "", "signature": "<b>Bob</b>", "isPrimary": True, "isDefault": True, "treatAsAlias": False, "verificationStatus": "accepted"}


def _check_settings_transcripts() -> None:
    result, reqs = _call("gmail_get_vacation", {}, (200, VACATION))
    _assert("getVacation: GET .../settings/vacation", reqs[0]["method"] == "GET" and reqs[0]["path"] == f"{BASE}/settings/vacation")
    _assert("getVacation: snake_case settings", result["enable_auto_reply"] is True and result["start_time"] == "1730000000000" and result["response_body_plain_text"] == "Back Monday", str(result))
    _, reqs = _call("gmail_update_vacation", {"enable_auto_reply": True, "response_subject": "Away", "response_body_html": "<p>Back Monday</p>", "restrict_to_domain": True, "start_time": 1730000000000, "end_time": "1730600000000"}, (200, VACATION))
    expected = {"enableAutoReply": True, "responseSubject": "Away", "responseBodyHtml": "<p>Back Monday</p>", "restrictToDomain": True, "startTime": "1730000000000", "endTime": "1730600000000"}
    _assert("updateVacation: PUT with camelCase body, epoch ms as strings", reqs[0]["method"] == "PUT" and reqs[0]["body"] == expected, str(reqs[0]["body"]))
    _, reqs = _call("gmail_update_imap", {"enabled": True, "auto_expunge": False, "expunge_behavior": "archive", "max_folder_size": 1000}, (200, {"enabled": True}))
    _assert("updateImap: PUT .../settings/imap body", reqs[0]["path"] == f"{BASE}/settings/imap" and reqs[0]["body"] == {"enabled": True, "autoExpunge": False, "expungeBehavior": "archive", "maxFolderSize": 1000})
    _, reqs = _call("gmail_update_pop", {"access_window": "fromNowOn", "disposition": "archive"}, (200, {}))
    _assert("updatePop: PUT .../settings/pop body", reqs[0]["body"] == {"accessWindow": "fromNowOn", "disposition": "archive"})
    _, reqs = _call("gmail_update_language", {"display_language": "en-GB"}, (200, {"displayLanguage": "en-GB"}))
    _assert("updateLanguage: PUT .../settings/language", reqs[0]["path"] == f"{BASE}/settings/language" and reqs[0]["body"] == {"displayLanguage": "en-GB"})
    _, reqs = _call("gmail_update_auto_forwarding", {"enabled": True, "email_address": "archive@acme.example", "disposition": "markRead"}, (200, {}))
    _assert("updateAutoForwarding: PUT .../settings/autoForwarding", reqs[0]["path"] == f"{BASE}/settings/autoForwarding" and reqs[0]["body"] == {"enabled": True, "emailAddress": "archive@acme.example", "disposition": "markRead"})


def _check_filter_transcripts() -> None:
    params = {"from": "boss@acme.example", "query": "has:attachment", "size": 1000000, "size_comparison": "larger", "add_label_ids": ["Label_7"], "remove_label_ids": ["INBOX"], "forward": "archive@acme.example"}
    created = {"id": "ANe1Bmj", "criteria": {"from": "boss@acme.example", "query": "has:attachment", "size": 1000000, "sizeComparison": "larger"}, "action": {"addLabelIds": ["Label_7"], "removeLabelIds": ["INBOX"], "forward": "archive@acme.example"}}
    result, reqs = _call("gmail_create_filter", params, (200, created))
    expected = {"criteria": {"from": "boss@acme.example", "query": "has:attachment", "size": 1000000, "sizeComparison": "larger"}, "action": {"addLabelIds": ["Label_7"], "removeLabelIds": ["INBOX"], "forward": "archive@acme.example"}}
    _assert("filters.create: POST .../settings/filters with nested criteria/action", reqs[0]["method"] == "POST" and reqs[0]["path"] == f"{BASE}/settings/filters" and reqs[0]["body"] == expected, str(reqs[0]["body"]))
    _assert("filters.create: id + snake_case criteria returned", result["id"] == "ANe1Bmj" and result["criteria"]["size_comparison"] == "larger" and result["action"]["add_label_ids"] == ["Label_7"])
    result, reqs = _call("gmail_list_filters", {}, (200, {"filter": [created]}))
    _assert("filters.list: rows under 'filters'", reqs[0]["path"] == f"{BASE}/settings/filters" and result["count"] == 1 and result["filters"][0]["id"] == "ANe1Bmj")
    result, reqs = _call("gmail_delete_filter", {"id": "ANe1Bmj"}, (204, None))
    _assert("filters.delete: DELETE .../settings/filters/{id}", reqs[0]["method"] == "DELETE" and reqs[0]["path"] == f"{BASE}/settings/filters/ANe1Bmj" and result == {"id": "ANe1Bmj", "deleted": True})


def _check_watch_transcripts() -> None:
    result, reqs = _call("gmail_watch", {"topic_name": "projects/p/topics/gmail", "label_ids": ["INBOX"], "label_filter_behavior": "include"}, (200, {"historyId": "9020", "expiration": "1731000000000"}))
    _assert("watch: POST users/me/watch with topic + label filter", reqs[0]["path"] == f"{BASE}/watch" and reqs[0]["body"] == {"topicName": "projects/p/topics/gmail", "labelIds": ["INBOX"], "labelFilterBehavior": "include"})
    _assert("watch: history_id + expiration returned", result == {"history_id": "9020", "expiration": "1731000000000"}, str(result))
    result, reqs = _call("gmail_stop", {}, (204, None))
    _assert("stop: POST users/me/stop, no body", reqs[0]["path"] == f"{BASE}/stop" and reqs[0]["body"] is None and result == {"stopped": True})


def _check_send_as_transcripts() -> None:
    result, reqs = _call("gmail_list_send_as", {}, (200, {"sendAs": [SEND_AS]}))
    _assert("sendAs.list: rows under 'send_as'", reqs[0]["path"] == f"{BASE}/settings/sendAs" and result["send_as"][0]["is_primary"] is True)
    params = {"send_as_email": "bob@acme.example", "signature": "<b>Bob</b><br>CTO", "smtp_host": "smtp.acme.example", "smtp_port": 587, "smtp_username": "bob", "smtp_password": "s3cret-smtp-pass", "smtp_security_mode": "starttls"}
    _, reqs = _call("gmail_patch_send_as", params, (200, SEND_AS))
    body = {"sendAsEmail": "bob@acme.example", "signature": "<b>Bob</b><br>CTO", "smtpMsa": {"host": "smtp.acme.example", "port": 587, "username": "bob", "password": "s3cret-smtp-pass", "securityMode": "starttls"}}
    _assert("sendAs.patch: PATCH .../sendAs/{email} with nested smtpMsa", reqs[0]["method"] == "PATCH" and reqs[0]["path"] == f"{BASE}/settings/sendAs/bob@acme.example" and reqs[0]["body"] == body, str(reqs[0]))
    result, reqs = _call("gmail_verify_send_as", {"send_as_email": "alias@acme.example"}, (204, None))
    _assert("sendAs.verify: POST .../sendAs/{email}/verify", reqs[0]["path"] == f"{BASE}/settings/sendAs/alias@acme.example/verify" and result["verification_sent"] is True)
    pkcs12 = base64.urlsafe_b64encode(b"PKCS12-key-material-0001").decode()
    result, reqs = _call("gmail_insert_smime_info", {"send_as_email": "bob@acme.example", "pkcs12": pkcs12, "encrypted_key_password": "key-pass-0001", "is_default": True}, (200, {"id": "sm1", "issuerCn": "Acme CA", "isDefault": True, "expiration": "1790000000000"}))
    _assert("smimeInfo.insert: POST .../sendAs/{email}/smimeInfo", reqs[0]["path"] == f"{BASE}/settings/sendAs/bob@acme.example/smimeInfo" and reqs[0]["body"] == {"pkcs12": pkcs12, "encryptedKeyPassword": "key-pass-0001", "isDefault": True})
    _assert("smimeInfo.insert: config returned", result["issuer_cn"] == "Acme CA" and result["send_as_email"] == "bob@acme.example")
    result, reqs = _call("gmail_set_default_smime_info", {"send_as_email": "bob@acme.example", "id": "sm1"}, (204, None))
    _assert("smimeInfo.setDefault: POST .../smimeInfo/{id}/setDefault", reqs[0]["path"] == f"{BASE}/settings/sendAs/bob@acme.example/smimeInfo/sm1/setDefault" and result["default_set"] is True)


def _check_cse_transcripts() -> None:
    result, reqs = _call("gmail_list_cse_key_pairs", {"page_size": 10, "page_token": "k2"}, (200, {"cseKeyPairs": [{"keyPairId": "kp1", "enablementState": "enabled", "subjectEmailAddresses": ["bob@acme.example"]}], "nextPageToken": "k3"}))
    _assert("cse.keypairs.list: pageSize + pageToken sent", reqs[0]["path"] == f"{BASE}/settings/cse/keypairs" and reqs[0]["query"] == {"pageSize": ["10"], "pageToken": ["k2"]})
    _assert("cse.keypairs.list: rows + next_page_token", result["cse_key_pairs"][0]["key_pair_id"] == "kp1" and result["next_page_token"] == "k3")
    for verb, suffix in (("gmail_disable_cse_key_pair", "disable"), ("gmail_enable_cse_key_pair", "enable"), ("gmail_obliterate_cse_key_pair", "obliterate")):
        _, reqs = _call(verb, {"key_pair_id": "kp1"}, (200, {"keyPairId": "kp1"}))
        _assert(f"cse.keypairs.{suffix}: POST .../keypairs/kp1:{suffix} with empty body", reqs[0]["method"] == "POST" and reqs[0]["path"] == f"{BASE}/settings/cse/keypairs/kp1:{suffix}" and reqs[0]["body"] in (None, {}), str(reqs[0]))
    _, reqs = _call("gmail_patch_cse_identity", {"email_address": "bob@acme.example", "encryption_key_pair_id": "kp1", "signing_key_pair_id": "kp2"}, (200, {"emailAddress": "bob@acme.example"}))
    body = {"emailAddress": "bob@acme.example", "signAndEncryptKeyPairs": {"encryptionKeyPairId": "kp1", "signingKeyPairId": "kp2"}}
    _assert("cse.identities.patch: PATCH .../identities/{emailAddress} nested key pair ids", reqs[0]["method"] == "PATCH" and reqs[0]["path"] == f"{BASE}/settings/cse/identities/bob@acme.example" and reqs[0]["body"] == body, str(reqs[0]))
    result, reqs = _call("gmail_delete_cse_identity", {"cse_email_address": "bob@acme.example"}, (204, None))
    _assert("cse.identities.delete: DELETE .../identities/{cseEmailAddress}", reqs[0]["method"] == "DELETE" and result == {"cse_email_address": "bob@acme.example", "deleted": True})


def check_transcripts() -> None:
    _check_settings_transcripts()
    _check_filter_transcripts()
    _check_watch_transcripts()
    _check_send_as_transcripts()
    _check_cse_transcripts()


# ---------------------------------------------------------------------------
# 3. Validation
# ---------------------------------------------------------------------------


def check_validation() -> None:
    cases: list[tuple[str, dict[str, Any], str]] = [
        ("gmail_watch", {}, "'topic_name' is required"),
        ("gmail_update_pop", {"access_window": "always"}, "'access_window' must be one of"),
        ("gmail_update_imap", {"enabled": "yes"}, "'enabled' must be true or false"),
        ("gmail_update_imap", {"max_folder_size": True}, "'max_folder_size' must be an integer"),
        ("gmail_update_vacation", {"start_time": "tomorrow"}, "'start_time' must be milliseconds"),
        ("gmail_create_filter", {"add_label_ids": "Label_1"}, "'add_label_ids' must be a list"),
        ("gmail_get_filter", {}, "'id' is required"),
        ("gmail_create_cse_key_pair", {"pkcs7": "x", "private_key_metadata": ["not-an-object"]}, "'private_key_metadata' must be a list of objects"),
        ("gmail_create_forwarding_address", {"forwarding_email": ""}, "'forwarding_email' is required"),
    ]
    for verb, params, needle in cases:
        gmail, http = _gmail()
        try:
            run_settings_verb(GMAIL_SETTINGS_VERBS[verb], lambda gmail=gmail: gmail, params, _WORKSPACE_FACTS, WORKSPACE)
            message = ""
        except ValueError as exc:
            message = str(exc)
        _assert(f"{verb} refuses {sorted(params)} before any request ({needle!r})", needle in message and not http.requests, message)


# ---------------------------------------------------------------------------
# 4-7. Through the plugin: account contract, tier, scope hints, results as received
# ---------------------------------------------------------------------------


def _error(code: int, status: str, message: str, reason: str, detail_reason: str = "") -> tuple[int, Any]:
    error: dict[str, Any] = {"code": code, "message": message, "errors": [{"message": message, "domain": "global", "reason": reason}], "status": status}
    if detail_reason:
        error["details"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": detail_reason, "domain": "googleapis.com"}]
    return code, {"error": error}


def _plugin() -> GSuitePlugin:
    plugin = GSuitePlugin()
    plugin.initialize({})
    connect_accounts(plugin, [CONSUMER, WORKSPACE])
    return plugin


def _run(plugin: GSuitePlugin, verb: str, params: dict[str, Any], *responses: tuple[int, Any]) -> tuple[dict[str, Any], RecordingHttp]:
    gmail, http = _gmail(*responses)
    plugin._require_factory = lambda: SimpleNamespace(gmail=lambda: gmail)  # type: ignore[method-assign]
    return plugin._run(lambda: plugin._deferred_producers()[verb](params), verb, params), http  # noqa: SLF001


def check_account_contract() -> None:
    plugin = _plugin()
    result, _ = _run(plugin, "gmail_get_vacation", {}, (200, VACATION))
    _assert("default account answers and the result names it", result["data"].get("account") == CONSUMER and result["data"]["enable_auto_reply"] is True, str(result))
    result, _ = _run(plugin, "gmail_list_send_as", {"account": WORKSPACE}, (200, {"sendAs": [SEND_AS]}))
    _assert("account argument picks the named account", result["data"].get("account") == WORKSPACE and result["data"]["count"] == 1, str(result))
    result, http = _run(plugin, "gmail_list_filters", {"account": "nobody@example.com"})
    _assert("unknown account -> gsuite.unknown_account listing the accounts, no request", (result.get("error") or {}).get("code") == "gsuite.unknown_account" and CONSUMER in str(result["error"]["message"]) and not http.requests, str(result))
    dispatched = plugin.gmail_get_vacation({"account": "nobody@example.com"}, {"session_id": "s1", "flow_id": "f1"})
    _assert("dispatch refuses an unknown account before queueing", (dispatched.get("error") or {}).get("code") == "gsuite.unknown_account", str(dispatched))


def check_tier() -> None:
    plugin = _plugin()
    workspace_only = sorted(verb for verb, spec in GMAIL_SETTINGS_VERBS.items() if spec.workspace_only)
    _assert("delegates, S/MIME, CSE and sharing writes are Workspace only", {"gmail_list_delegates", "gmail_create_delegate", "gmail_list_smime_info", "gmail_list_cse_key_pairs", "gmail_create_forwarding_address", "gmail_create_send_as", "gmail_update_auto_forwarding"} <= set(workspace_only), str(workspace_only))
    _assert("send-as reads/patch, filters and vacation work on every tier", not {"gmail_list_send_as", "gmail_patch_send_as", "gmail_create_filter", "gmail_update_vacation"} & set(workspace_only))
    for verb in workspace_only:
        spec = GMAIL_SETTINGS_VERBS[verb]
        params = _policy_params(spec)
        result, http = _run(plugin, verb, params)
        error = result.get("error") or {}
        _assert(f"{verb}: consumer account refused naming the tier, nothing sent", error.get("code") == ERROR_WORKSPACE_ONLY and "tier 'consumer'" in str(error.get("message")) and CONSUMER in str(error.get("message")) and not http.requests, str(error))
    result, http = _run(plugin, "gmail_list_delegates", {"account": WORKSPACE}, (200, {"delegates": [{"delegateEmail": "eve@acme.example", "verificationStatus": "accepted"}]}))
    _assert("pass path: the same verb runs on a Workspace account", result["data"].get("delegates") == [{"delegate_email": "eve@acme.example", "verification_status": "accepted"}] and len(http.requests) == 1, str(result))
    gmail, _ = _gmail((200, {"delegates": []}))
    unknown = run_settings_verb(GMAIL_SETTINGS_VERBS["gmail_list_delegates"], lambda: gmail, {}, {"account_tier": "unknown", "granted_scopes": None}, "old@acme.example")
    _assert("unknown tier is never refused (advisory, rul_becb4bed)", unknown == {"delegates": [], "count": 0})


def check_direct_refusal() -> None:
    gmail, http = _gmail()
    try:
        run_settings_verb(GMAIL_SETTINGS_VERBS["gmail_list_cse_identities"], lambda: gmail, {}, {"account_tier": "consumer"}, CONSUMER)
        refused = ""
    except SettingsVerbError as exc:
        refused = exc.code
    _assert("direct executor raises SettingsVerbError(gsuite.workspace_only) for a consumer, nothing sent", refused == ERROR_WORKSPACE_ONLY and not http.requests, refused)


def check_scope_hints() -> None:
    plugin = _plugin()
    scope_403 = _error(403, "PERMISSION_DENIED", "Request had insufficient authentication scopes.", "insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT")
    result, _ = _run(plugin, "gmail_create_delegate", {"account": WORKSPACE, "delegate_email": "eve@acme.example"}, scope_403)
    message = str((result.get("error") or {}).get("message"))
    _assert("sharing verb 403 names gmail.settings.sharing and domain-wide delegation", "gmail.settings.sharing" in message and "domain-wide delegation" in message and result["error"]["code"] == "gsuite.missing_scope", message)
    forbidden = _error(403, "PERMISSION_DENIED", "Access restricted to service accounts that have been delegated domain-wide authority", "forbidden")
    result, _ = _run(plugin, "gmail_create_send_as", {"account": WORKSPACE, "send_as_email": "alias@acme.example"}, forbidden)
    message = str((result.get("error") or {}).get("message"))
    _assert("sharing verb plain 403 also explains the service-account requirement", result["error"]["code"] == "gsuite.permission_denied" and "domain-wide delegation" in message, message)
    result, _ = _run(plugin, "gmail_update_vacation", {"enable_auto_reply": False}, scope_403)
    message = str((result.get("error") or {}).get("message"))
    _assert("basic-scope write 403 names gmail.settings.basic and reconnect", result["error"]["code"] == "gsuite.missing_scope" and "gmail.settings.basic" in message and "connect_account" in message, message)
    result, _ = _run(plugin, "gmail_get_filter", {"id": "nope"}, _error(404, "NOT_FOUND", "Filter not found", "notFound"))
    _assert("404 -> gsuite.not_found", (result.get("error") or {}).get("code") == "gsuite.not_found", str(result))


def check_as_received() -> None:
    plugin = _plugin()
    result, _ = _run(plugin, "gmail_get_imap", {}, (200, {"enabled": True, "note": "vendor text"}))
    _assert("a Gmail result is returned as received, keys in snake_case", result["data"]["note"] == "vendor text" and result["data"]["enabled"] is True, str(result))
    # Distinct vendor keys that meet after snake_case normalisation keep every
    # value: later ones take `` (N)`` suffixes from one counter per mapping,
    # starting at 2 (the rule the result shaping has always applied).
    vendor = {"enabled": True, "extension": {"fieldKey": "first", "field_key": "second", "otherKey": "third", "other_key": "fourth"}}
    shaped, _ = _call("gmail_get_imap", {}, (200, vendor))
    expected = {"field_key": "first", "field_key (2)": "second", "other_key": "third", "other_key (3)": "fourth"}
    _assert("colliding snake_case keys keep every vendor value under (N) suffixes", shaped == {"enabled": True, "extension": expected}, str(shaped))


# ---------------------------------------------------------------------------
# 8. Registration
# ---------------------------------------------------------------------------


def _artifacts(plugin: GSuitePlugin, verb: str, spec: SettingsVerb) -> tuple[bool, str]:
    metadata = getattr(getattr(type(plugin), verb, None), "_platform_process_metadata", None)
    kb = KB_DIR / f"{verb}.json"
    doc = json.loads(kb.read_text()) if kb.exists() else {}
    embedding = str(doc.get("embedding_description", ""))
    result_type = (doc.get("result_processor_customizations") or {}).get("result_type")
    in_plugin = verb in plugin.get_edge_process_definitions() and verb in plugin._deferred_producers()  # noqa: SLF001
    kb_ok = doc.get("process_key") == f"plugin::g_suite_plugin::{verb}" and 200 <= len(embedding) <= 400 and result_type == spec.result_type
    ok = metadata is not None and getattr(metadata, "name", "") == verb and in_plugin and kb_ok
    return ok, f"decorated={metadata is not None} edge+producer={in_plugin} kb={bool(doc)} emb={len(embedding)} result_type={result_type}"


def check_declarations() -> None:
    plugin = GSuitePlugin()
    plugin.initialize({})
    _assert("GSuitePlugin mixes in GmailSettingsProcesses and GmailSharingProcesses", GmailSettingsProcesses in GSuitePlugin.__mro__ and GmailSharingProcesses in GSuitePlugin.__mro__)
    edges = plugin.get_edge_process_definitions()
    retry = {verb: edges[verb].error_processor_template_customizations.retryable for verb in GMAIL_SETTINGS_VERBS}  # pyright: ignore[reportOptionalMemberAccess]
    _assert("EDGE retryable exactly for reads (spec.write false)", all(retry[verb] is (not spec.write) for verb, spec in GMAIL_SETTINGS_VERBS.items()), str(retry))
    _assert("CSE lists page with paging_fields (page_size, page_token)", all(GMAIL_SETTINGS_VERBS[v].query == paging_fields() and GMAIL_SETTINGS_VERBS[v].paged for v in ("gmail_list_cse_identities", "gmail_list_cse_key_pairs")))
    declared = settings_process("gmail_update_vacation")
    _assert("settings_process declares the row's title, EDGE category and account param", declared["display_name"] == "Gmail: Update Vacation Responder Settings" and "account" in declared["parameters"] and declared["is_async"] is True)


def check_registration() -> None:
    plugin = GSuitePlugin()
    plugin.initialize({})
    for verb, spec in GMAIL_SETTINGS_VERBS.items():
        ok, detail = _artifacts(plugin, verb, spec)
        _assert(f"{verb}: decorator + EDGE + producer + KB json", ok, detail)
        metadata = getattr(getattr(type(plugin), verb, None), "_platform_process_metadata", None)
        parameters = getattr(metadata, "parameters", None) or {}
        account = parameters.get("account")
        declared = set(parameters) - {"account"}
        _assert(f"{verb}: optional account + exactly the row's parameters", account is not None and not account.required and declared == {f.param for f in spec.fields}, str(sorted(declared)))


def main() -> int:
    from gmail_settings_entity_query_cases import GmailSettingsEntityQueryTests  # pyright: ignore[reportMissingImports]
    from gmail_settings_rework_cases import GmailSettingsReworkTests  # pyright: ignore[reportMissingImports]

    print("\ng_suite_plugin Gmail build 2 smoke (settings, push; real client, recorded HTTP)")
    print("=" * 60)
    check_vendor_fidelity()
    check_every_verb()
    check_transcripts()
    check_validation()
    check_account_contract()
    check_tier()
    check_scope_hints()
    check_as_received()
    check_direct_refusal()
    check_declarations()
    check_registration()
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(case) for case in (GmailSettingsReworkTests, GmailSettingsEntityQueryTests))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        _failed.append("Gmail settings rework unittest regressions")
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All Gmail build-2 checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
