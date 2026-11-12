"""Fake-only command proof, loading the shipped sibling by its exact path."""

from __future__ import annotations

import fcntl
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "ananta" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _deploy_by_rule_fixture_support import (  # noqa: E402
    NOTICE,
    OLD,
    TARGET,
    Fixture,
    envelope,
    load,
    writes,
)


def test_command_available() -> None:
    load()


def test_docs_landing_no_swap() -> None:
    f = Fixture()
    f.s.delta = "".join(f"M\0{p}\0" for p in ("docs/x", "quality_gates/x", "ananta/tests/x", "plugins/p/knowledge_base/x", ".claude/x", "workbench/x"))
    result = f.run()
    assert result["decision"] == "skipped_nonproduction" and not writes(f) and "notice" not in result and "REACHED_NOTICE" not in f.log, "DOCS_LANDING_NO_SWAP"
    f = Fixture()
    f.s.delta = ""
    result = f.run()
    assert result["decision"] == "skipped_nonproduction" and not writes(f) and "notice" not in result and "REACHED_NOTICE" not in f.log, "EMPTY_DELTA_LANDING_NO_SWAP"
    for path in ("ananta/src/x", "plugins/p/src/x", "profile/config/x"):
        assert f.m.production(path)
        assert path in f.m.changed_paths(f"R100\0{path}\0docs/x\0")
        assert f.m.changed_paths(f"D\0{path}\0") == [path]


def test_production_deploy_recorded() -> None:
    f = Fixture()
    result = f.run()
    assert result["decision"] == "deployed" and writes(f) == ["REACHED_APPLY_COMMIT", "REACHED_RECORD"], "PRODUCTION_DEPLOY_RECORDED"
    assert f.log.index("REACHED_VERIFY") < f.log.index("REACHED_RECORD")
    assert f.s.polls == 2, "DRAIN_BEFORE_RECORD"
    recorded = next(a for a in f.log if isinstance(a, list) and a[1] == "deployed")
    assert recorded[recorded.index("--commit") + 1] == TARGET
    assert recorded[-6:] == ["--agent-instance-id", "agi-fixture", "--actor-model", "fixture-model", "--actor-effort", "medium"]


def test_bridge_jobs_are_awaited_at_the_shared_process_seam() -> None:
    """Reach a verified deploy through the same CLI boundary as the seat.

    Both swap observations and manifest submissions require the second hop.
    Checking every invocation also protects the synchronous readiness/notice
    calls if their registration later changes to an admitted operation.
    """
    f = Fixture()
    result = f.run()
    assert result["decision"] == "deployed", f"ASYNC_SWAP_FINAL_RECORD: {result}"
    calls = [call for call in f.log if isinstance(call, list) and call[0] == f.args.bridge and call[1] == "call"]
    assert calls and all("--wait" in call for call in calls), "EVERY_PROCESS_CALL_REQUESTS_FINAL_RESULT"
    assert writes(f) == ["REACHED_APPLY_COMMIT", "REACHED_RECORD"], "ASYNC_APPLY_NOT_REPLAYED"


def test_finished_job_records_preserve_the_original_result() -> None:
    """Assert the actual get_job record shape, then change one key's policy.

    A future asynchronous readiness read must work through the same process()
    decoder. No deployment-specific key wrapper should be needed for it.
    """
    f = Fixture()
    assert f.run()["decision"] == "deployed", "JOB_RECORD_FIXTURE_REACHES_DEPLOYMENT"
    assert {job["provider_name"] for job in f.jobs} == {"local_self_deployment_service", "lifecycle_management_service"}, "SWAP_AND_APPLY_JOB_RESULTS_DECODED"
    assert all(job["status"] == "completed" and job["result"] for job in f.jobs), "FINISHED_JOBS_SUPPLY_ORIGINAL_ACTION_ENVELOPE"
    f = Fixture()
    f.s.async_keys = (*f.s.async_keys, f.m.PLUGINS)
    assert f.run()["decision"] == "deployed", "PROCESS_SEAM_AWAITS_ANOTHER_KEY_WITHOUT_A_WRAPPER"


def test_synchronous_bridge_results_are_unchanged() -> None:
    """Keep the immediate outer-envelope contract for unselected keys.

    The explicit process() assertion verifies its returned Row, so a decoder
    that blindly strips an extra result/data layer cannot pass by coincidence.
    """
    f = Fixture()
    f.s.async_keys = ()
    assert f.run()["decision"] == "deployed" and not f.jobs, "SYNC_PROCESS_RESULT_UNCHANGED"
    assert f.m.process(f.ports, f.args, f.m.PLUGINS, {}) == {"plugins": [{"lifecycle_managed": True, "status": "ready", "is_running": True}]}, "SYNC_DATA_NOT_UNWRAPPED_TWICE"


def test_failed_bridge_job_is_an_advisory() -> None:
    """Preserve CLI failure details through each existing decision path.

    A failed swap read defers, a failed dry-run fails the run, and failed
    readiness reads retry to their deadline. None may record a deployment.
    The captured job record must retain its id and original worker error.
    """
    for key, decision in ((load().SWAP, "deferred_swap"), (load().APPLY, "failed"), (load().PLUGINS, "deferred_readiness")):
        f = Fixture()
        f.s.async_keys = (*f.s.async_keys, f.m.PLUGINS)
        f.s.job_failure = key
        result = f.run()
        assert result["decision"] == decision and not f.s.recorded, "FAILED_JOB_NEVER_RECORDS_DEPLOYMENT"
        detail = str(result)
        assert "fixture job failed" in detail and "job-fixture-" in detail, "FAILED_JOB_ID_AND_ERROR_ARE_VISIBLE"


def test_bridge_job_timeout_keeps_running_and_drain_retries() -> None:
    """A timeout ends the wait and never cancels the admitted operation.

    Before a swap it uses the existing deferral. During drain the existing
    observation loop retries safely; the manifest submission still occurs
    exactly once and the earlier processing job remains available to read.
    """
    f = Fixture()
    f.s.job_timeouts = 1
    result = f.run()
    assert result["decision"] == "deferred_swap" and f.jobs[0]["status"] == "processing", "JOB_TIMEOUT_DEFERS_WITHOUT_CANCELLING"
    assert "still running" in str(result["advisories"]) and "job-fixture-0" in str(result["advisories"]), "JOB_TIMEOUT_RETAINED_IN_NOTES"
    f = Fixture()
    f.s.job_timeouts, f.s.job_timeout_during_drain = 1, True
    result = f.run()
    assert result["decision"] == "deployed" and any(job["status"] == "processing" for job in f.jobs), "DRAIN_JOB_TIMEOUT_RETRIES_READ_WITHOUT_CANCELLING"
    assert any(note.startswith("swap_read_retry") and "still running" in note for note in result["advisories"]), "DRAIN_JOB_TIMEOUT_IS_ADVISORY"
    assert writes(f) == ["REACHED_APPLY_COMMIT", "REACHED_RECORD"], "JOB_TIMEOUT_NEVER_REPLAYS_APPLY"


def test_deploy_notice_after_verified_record() -> None:
    """Success is complete at verified record readback and sends no notice.

    Keep setup/readback checks before the named no-notice assertion so a broken
    fixture cannot masquerade as proof that the production notice is absent.
    """
    f = Fixture()
    result = f.run()
    assert result["decision"] == "deployed" and "REACHED_RECORD" in f.log, "DEPLOY_NOTICE_FIXTURE_REACHED_RECORD"
    assert "REACHED_VERIFIED_RECORD" in f.log and result["measurement_id"] == "cme-fixture", "DEPLOY_NOTICE_FIXTURE_VERIFIED_READBACK"
    calls = [a for a in f.log if isinstance(a, list) and len(a) > 2 and a[2] == NOTICE]
    assert not calls and "notice" not in result, "DEPLOY_NOTICE_AFTER_VERIFIED_RECORD: success is silent"


def _assert_exception_notice(result: dict[str, object], log: list[object], expected_decision: str, expected_target: str | None = None) -> None:
    """Bind one queued exception receipt to the terminal outcome identity."""
    values, content = _exception_notice_payload(log)
    notice = result["notice"]
    assert isinstance(notice, dict), "DEPLOY_NOTICE_RECEIPT_MAPPING"
    _assert_exception_content(values, content, expected_decision, expected_target)
    _assert_exception_receipt(notice, content)


def _exception_notice_payload(log: list[object]) -> tuple[dict[str, object], dict[str, object]]:
    calls = [a for a in log if isinstance(a, list) and len(a) > 2 and a[2] == NOTICE]
    assert len(calls) == 1, "EXCEPTION_NOTICE_EXACTLY_ONCE"
    values = json.loads(calls[0][3])
    content = json.loads(str(values["content"]))
    return values, content


def _assert_exception_content(values: dict[str, object], content: dict[str, object], expected_decision: str, expected_target: str | None) -> None:
    assert values["name"] == "Fixture-Solet-Main", "DEPLOY_NOTICE_SCOPED_RECIPIENT"
    assert content["decision"] == expected_decision, "EXCEPTION_NOTICE_DECISION"
    assert content["outcome"] == expected_decision, "EXCEPTION_NOTICE_OUTCOME"
    assert content["target"] == expected_target, "EXCEPTION_NOTICE_TARGET_NULLABILITY"


def _assert_exception_receipt(notice: dict[str, object], content: dict[str, object]) -> None:
    assert notice["message_id"] == "arm-fixture", "DEPLOY_NOTICE_RECEIPT_MESSAGE_ID"
    assert notice["status"] == "queued", "DEPLOY_NOTICE_RECEIPT_STATUS"
    assert notice["notice_key"] == content["notice_key"], "DEPLOY_NOTICE_DUPLICATE_KEY"
    assert "same terminal outcome" in str(content["duplicate_semantics"]), "DEPLOY_NOTICE_DUPLICATE_SEMANTICS"


def test_exception_notices_for_failure_and_holds() -> None:
    """Failure, proof-red and deferred holds each attempt one exception notice."""
    cases = (
        (lambda state: setattr(state, "reject", True), "failed", None),
        (lambda state: setattr(state, "proof_change", {"head": OLD}), "failed", None),
        (lambda state: setattr(state, "occupied", True), "deferred_lock", None),
        (lambda state: (setattr(state, "receipt", {"result": "running", "pid": 1}), setattr(state, "receipt_lock_held", True)), "deferred_gate", TARGET),
        (lambda state: setattr(state, "plugins_never_ready", True), "deferred_readiness", TARGET),
    )
    for setup, decision, target in cases:
        f = Fixture()
        setup(f.s)
        result = f.run()
        assert result["decision"] == decision, f"EXCEPTION_NOTICE_REACHES_{decision}"
        _assert_exception_notice(result, f.log, decision, target)

    f = Fixture()
    f.s.job_failure = f.m.SWAP
    result = f.run()
    assert result["decision"] == "deferred_swap", "EXCEPTION_NOTICE_REACHES_deferred_swap"
    _assert_exception_notice(result, f.log, "deferred_swap", TARGET)


def test_notice_failures_are_not_read_on_success() -> None:
    """Success does not send, so transport failure cannot alter deployment."""

    replies = ["not JSON", "[]", envelope({}), envelope({"message_id": ""}), envelope({"message_id": 7}), json.dumps({"status": "completed", "result": {"data": []}}), json.dumps({"status": "completed", "result": {"success": False, "error": "role_unbound"}}), json.dumps({"status": "failed"})]
    for reply in replies:
        f = Fixture()
        f.s.notice_reply = reply
        result = f.run()
        assert result["decision"] == "deployed" and result["measurement_id"] == "cme-fixture", "SUCCESS_PRESERVES_DEPLOYMENT"
        assert "notice" not in result and f.log.count("REACHED_NOTICE") == 0, "SUCCESS_HAS_NO_NOTICE_TRANSPORT"
    f = Fixture()
    f.s.notice_timeout = True
    result = f.run()
    assert result["decision"] == "deployed" and "notice" not in result and f.log.count("REACHED_NOTICE") == 0, "NOTICE_TIMEOUT_NOT_READ_ON_SUCCESS"


def test_exception_notice_transport_uncertainty_preserves_hold() -> None:
    """Malformed, vacant and timed-out notice sends preserve the original hold."""
    replies = ["not JSON", "[]", envelope({}), envelope({"message_id": ""}), envelope({"message_id": 7}), json.dumps({"status": "completed", "result": {"data": []}}), json.dumps({"status": "completed", "result": {"success": False, "error": "role_unbound"}}), json.dumps({"status": "failed"})]
    for reply in replies:
        f = Fixture()
        f.s.reject = True
        f.s.notice_reply = reply
        result = f.run()
        assert result["decision"] == "failed" and result["notice"]["status"] == "uncertain" and result["notice"]["message_id"] is None, "FAILED_NOTICE_UNCERTAINTY_PRESERVES_DECISION"
        assert f.log.count("REACHED_NOTICE") == 1 and writes(f) == ["REACHED_APPLY_COMMIT"], "FAILED_NOTICE_NO_RETRY"
    f = Fixture()
    f.s.occupied = True
    f.s.notice_timeout = True
    result = f.run()
    assert result["decision"] == "deferred_lock" and result["notice"]["status"] == "uncertain" and f.log.count("REACHED_NOTICE") == 1, "HOLD_NOTICE_TIMEOUT_PRESERVES_DECISION"


def test_notice_identity_recurrence_and_reversal() -> None:
    """Verified success, recovery and reversal are all complete without notices.

    Early exits have no verified completion branch and must send nothing.
    """
    f = Fixture()
    first = f.run()
    assert first["decision"] == "deployed" and "notice" not in first and f.log.count("REACHED_NOTICE") == 0, "SUCCESS_NO_NOTICE"
    for measurement in ("cme-fixture", "cme-reversal"):
        f = Fixture()
        f.s.serving, f.s.recorded, f.s.stale_first = TARGET, True, True
        f.s.measurement_id = measurement
        repeat = f.run()
        assert repeat["decision"] == "already_recorded" and not writes(f) and f.log.count("REACHED_NOTICE") == 0 and "notice" not in repeat, "VERIFIED_ALREADY_RECORDED_NO_NOTICE"
    _assert_reversal_no_notice()
    for field, value in (("baseline", TARGET), ("occupied", True), ("stuck", True), ("reject", True), ("delta", "M\0docs/x\0"), ("plugins_never_ready", True)):
        f = Fixture()
        setattr(f.s, field, value)
        result = f.run()
        assert result["decision"] not in ("deployed", "already_recorded"), "NO_UNVERIFIED_DEPLOY_NOTICE_SETUP"
        if result["decision"] in ("already_current", "skipped_nonproduction"):
            assert "REACHED_NOTICE" not in f.log and "notice" not in result, "BENIGN_TERMINAL_NO_NOTICE"
        else:
            assert f.log.count("REACHED_NOTICE") == 1 and "notice" in result, "EXCEPTION_TERMINAL_ONE_NOTICE"


def _assert_reversal_no_notice() -> None:
    """A later run verifies the opposite target with no completion notice."""
    f = Fixture()
    f.s.target, f.s.baseline, f.s.serving = OLD, TARGET, TARGET
    f.s.measurement_id = "cme-rollback"
    result = f.run()
    assert result["decision"] == "deployed" and result["target"] == OLD and result["measurement_id"] == "cme-rollback", "REVERSAL_VERIFIED_SEPARATELY"
    assert "notice" not in result and f.log.count("REACHED_NOTICE") == 0, "REVERSAL_NO_NOTICE"


def test_restart_recovery_after_apply_commit_timeout() -> None:
    f = Fixture()
    f.args.deadline = 25
    f.s.apply_timeout = True
    f.s.plugin_timeouts = 2
    f.s.plugin_not_ready = 1
    result = f.run()
    assert result["decision"] == "deployed" and f.s.recorded and writes(f).count("REACHED_APPLY_COMMIT") == 1, "RESTART_RECOVERY_DEPLOYS_AND_RECORDS"
    assert result["advisories"] and f.s.plugin_reads == 4, "RESTART_READ_FAILURES_ARE_RETRIED"


def test_readiness_deadline_defers_then_next_run_records() -> None:
    f = Fixture()
    f.args.deadline = 10
    f.s.plugins_never_ready = True
    result = f.run()
    assert result["decision"] == "deferred_readiness" and result["advisories"] and not f.s.recorded, "READINESS_EXPIRY_DEFERS_WITHOUT_RECORD"
    f.s.plugins_never_ready = False
    f.s.serving = TARGET
    recovered = f.run()
    assert recovered["decision"] == "deployed" and f.s.recorded and writes(f).count("REACHED_APPLY_COMMIT") == 1, "NEXT_SERVING_TARGET_RUN_RECORDS"
    deadline_fixture = Fixture()
    deadline_fixture.args.deadline = 5
    deadline_fixture.s.plugins_never_ready = True
    deadline_fixture.s.drain_consumes_deadline = True
    at_deadline = deadline_fixture.run()
    assert at_deadline["decision"] == "deferred_readiness" and deadline_fixture.s.plugin_reads == 1, "READINESS_READS_ONCE_AFTER_DRAIN_DEADLINE"


def test_serving_target_retries_readiness() -> None:
    f = Fixture()
    f.s.serving = TARGET
    f.s.plugin_timeouts = 2
    f.s.plugin_not_ready = 1
    result = f.run()
    assert result["decision"] == "deployed" and f.s.recorded and writes(f).count("REACHED_APPLY_COMMIT") == 0, "SERVING_TARGET_RESTART_RECOVERY_RECORDS"
    assert result["advisories"] and f.s.plugin_reads == 4, "SERVING_TARGET_READ_FAILURES_ARE_RETRIED"


def test_flat_swap_status_reaches_swap() -> None:
    f = Fixture()
    f.s.flat_swap = True
    result = f.run()
    assert result["decision"] != "deferred_swap", "FLAT_SWAP_REACHES_SWAP"
    assert "REACHED_APPLY_COMMIT" in f.log


def test_receipt_lock_companion_is_ignored() -> None:
    f = Fixture()
    f.s.receipt = {"result": "running", "pid": 1}
    f.s.receipt_live = True
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        (directory / "receipt.lock").touch()
        (directory / "receipt.json").write_text(json.dumps(f.s.receipt))
        with patch.dict(os.environ, RUN_SMOKES_BATTERY_RECEIPT_DIR=temp):
            receipts = f.m.native_ports().receipts
        ports = f.m.Ports(f.command, f.read, f.resolve, receipts, lambda path: True, f.lock, lambda: f.s.ticks, f.sleep, lambda: "fixture")
        notes: list[str] = []
        observed = f.m.busy(ports, notes)
    assert observed and observed["pid"] == 1, "LIVE_JSON_RECEIPT_STILL_DEFERS"
    assert not any("receipt_unknown" in note for note in notes), "LOCK_COMPANION_NO_ADVISORY"


def test_tick_and_no_change() -> None:
    f = Fixture()
    f.s.delta, f.args.trigger = "M\0docs/x\0", "tick"
    assert f.run()["decision"] == "deployed"
    f = Fixture()
    f.s.baseline = TARGET
    result = f.run()
    assert result["decision"] == "already_current" and not writes(f) and "notice" not in result and "REACHED_NOTICE" not in f.log


def test_busy_deferral_and_stale_receipt() -> None:
    for receipt in ({"result": "passed", "pid": 42}, {"result": "running", "pid": 42}, None, b"bad"):
        f = Fixture()
        f.s.receipt = receipt
        result = f.run()
        assert result["decision"] == "deployed", "IDLE_GATE_DEPLOYS"
        assert receipt not in (None, b"bad") or result["advisories"]
    f = Fixture()
    f.s.occupied = True
    assert f.run()["decision"] == "deferred_lock" and not writes(f)
    f.s.occupied = False
    assert f.run()["decision"] == "deployed"


def test_stale_running_receipt_with_live_pid_continues() -> None:
    f = Fixture()
    f.s.receipt = {"result": "running", "pid": 1}
    f.s.receipt_live = True
    f.s.receipt_lock_held = False
    result = f.run()
    assert result["decision"] != "deferred_gate", "STALE_RUNNING_RECEIPT_DOES_NOT_DEFER"
    assert "receipt_stale_running:/fixture/receipt.json" in result["advisories"], "STALE_RUNNING_RECEIPT_ADVISORY"
    f = Fixture()
    f.s.receipt = {"result": "running"}
    result = f.run()
    assert result["decision"] != "deferred_gate", "PIDLESS_RUNNING_RECEIPT_DOES_NOT_DEFER"
    assert not any("receipt_unknown" in note for note in result["advisories"]), "PIDLESS_RUNNING_RECEIPT_IS_READABLE"


def test_held_running_receipt_defers() -> None:
    f = Fixture()
    f.s.receipt = {"result": "running", "pid": 1}
    f.s.receipt_lock_held = True
    result = f.run()
    assert result["decision"] == "deferred_gate", "HELD_RUNNING_RECEIPT_DEFERS"
    assert result["receipt"] == "/fixture/receipt.json", "HELD_RUNNING_RECEIPT_PATH"


def test_production_receipt_lock_port() -> None:
    f = Fixture()
    f.s.receipt = {"result": "running"}
    with tempfile.TemporaryDirectory() as temp:
        receipt_path = Path(temp) / "receipt.json"
        lock_path = receipt_path.with_suffix(".lock")
        receipt_path.write_text(json.dumps(f.s.receipt))
        lock_path.touch()
        ports = replace(
            f.ports,
            receipts=lambda: [receipt_path],
            receipt_lock_held=f.m.receipt_lock_held,
        )
        with lock_path.open("rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = f.m.deploy(ports, f.args)
            assert result["decision"] == "deferred_gate", "PRODUCTION_HELD_RECEIPT_DEFERS"
            assert f.m.receipt_lock_held(receipt_path), "PRODUCTION_LOCK_PORT_SEES_HELD"
        f.s.receipt = {"result": "running", "pid": os.getpid()}
        receipt_path.write_text(json.dumps(f.s.receipt))
        assert not f.m.receipt_lock_held(receipt_path), "PRODUCTION_LOCK_PORT_SEES_RELEASED"
        result = f.m.deploy(ports, f.args)
        assert result["decision"] != "deferred_gate", "PRODUCTION_RELEASED_RECEIPT_CONTINUES"


def test_attestation_and_etag() -> None:
    f = Fixture()
    f.s.reject = True
    assert f.run()["decision"] == "failed" and "REACHED_RECORD" not in f.log
    for change in ({"head": OLD}, {"state": "different"}, {"mismatches": ["x"]}):
        f = Fixture()
        f.s.proof_change = change
        assert f.run()["decision"] == "failed" and "REACHED_RECORD" not in f.log
    for change in ({"selected_file_manifest": {"filename": "SELECTED_FILES.json", "sha256": "bad"}}, {"selected_file_manifest": {"filename": "/escaped", "sha256": "bad"}}, {"git_sha": OLD[:9]}):
        f = Fixture()
        f.s.serving = TARGET
        f.s.version_change = change
        assert f.run()["decision"] == "failed" and "REACHED_RECORD" not in f.log
    f = Fixture()
    with patch.object(f, "sleep", side_effect=lambda seconds: setattr(f.s, "bad_manifest", True)):
        ports = f.m.Ports(f.command, f.read, f.resolve, f.receipts, lambda pid: False, f.lock, lambda: f.s.ticks, f.sleep, lambda: "fixture")
        assert f.m.deploy(ports, f.args)["decision"] == "failed"


def test_record_retry() -> None:
    f = Fixture()
    f.s.serving, f.s.ambiguous, f.s.advance = TARGET, True, True
    result = f.run()
    assert result["decision"] == "deployed" and writes(f) == ["REACHED_RECORD"]
    assert result["target"] == TARGET and "master_advanced" in result["advisories"]
    f.s.advance = False
    assert f.run()["decision"] == "already_current" and writes(f) == ["REACHED_RECORD"]
    f = Fixture()
    f.s.serving, f.s.recorded, f.s.stale_first = TARGET, True, True
    assert f.run()["decision"] == "already_recorded" and not writes(f)
    f = Fixture()
    f.s.stuck = True
    assert f.run()["decision"] == "failed" and "REACHED_RECORD" not in f.log
    f = Fixture()
    f.s.baseline = ""
    assert f.run()["decision"] == "deployed"


def test_identity_and_cli_adapters() -> None:
    for holder in (True, False, None):
        f = Fixture()
        f.s.holder = holder
        result = f.run()
        assert result["decision"] == "deployed"
        assert holder is True or "identity_not_current_holder" in result["advisories"]
    assert f.m.bridge_data(json.loads(envelope({"ok": True}))) == {"ok": True}
    try:
        f.m.bridge_swap_status({"status": "completed", "result": {"action_status": "completed", "success": True}})
    except ValueError:
        pass
    else:
        raise AssertionError("SWAP_UNKNOWN_SHAPE")
    cli_adapters(f)


def test_malformed_swap_row_during_drain_is_retried() -> None:
    f = Fixture()
    command = f.command
    malformed: list[list[str]] = []

    def reply(argv: list[str]) -> str:
        if argv[0] == f.args.bridge and argv[2] == f.m.SWAP and f.s.committed and not malformed:
            malformed.append(argv)
            return envelope({"swap_in_progress": False})
        return command(argv)

    result = f.m.deploy(replace(f.ports, command=reply), f.args)
    assert result["decision"] == "deployed", "malformed_swap_row_must_not_fail_a_committed_swap"
    assert malformed and any(note.startswith("swap_shape_retry") for note in result["advisories"]), "malformed_swap_row_is_retried_as_a_note"


def test_holds_role_reply_of_any_shape_is_advisory() -> None:
    f = Fixture()
    command = f.command

    def list_reply(argv: list[str]) -> str:
        if argv[1] == "holds-role":
            return json.dumps({"result": [{"holds": True}]})
        return command(argv)

    result = f.m.deploy(replace(f.ports, command=list_reply), f.args)
    assert result["decision"] == "deployed" and "identity_not_current_holder" in result["advisories"], "holds_list_reply_is_advisory"
    assert len(result["holder_checks"]) == 2, "holder_checks_recorded"
    f = Fixture()

    def string_reply(argv: list[str]) -> str:
        if argv[1] == "holds-role":
            return json.dumps({"result": {"data": {"holds": "yes"}}})
        return f.command(argv)

    result = f.m.deploy(replace(f.ports, command=string_reply), f.args)
    assert result["decision"] == "deployed" and "identity_not_current_holder" in result["advisories"], "holds_must_be_true_exactly"


def cli_adapters(f: Fixture) -> None:
    """Exercise JSON and argv boundaries without launching any external adapter."""
    for payload in ({"status": "failed"}, {"status": "completed", "result": {"action_status": "failed"}}):
        try:
            f.m.bridge_data(payload)
        except ValueError:
            pass
        else:
            raise AssertionError("CLI_ERROR_ENVELOPE")
    f = Fixture()
    argv = ["--trigger", "landing"]
    for key in ("repo", "app_home", "releases_root", "psolet", "bridge", "agent_instance_id", "actor_model", "actor_effort"):
        argv.extend(["--" + key.replace("_", "-"), str(getattr(f.args, key))])
    f.s.notice_timeout = True
    output = io.StringIO()
    with patch.dict(os.environ, SOLET_NAME="fixture-solet"), patch.object(f.m, "native_ports", return_value=f.ports), redirect_stdout(output):
        assert f.m.main(argv) == 0
    terminal = json.loads(output.getvalue().splitlines()[-1])
    assert terminal["decision"] == "deployed" and "notice" not in terminal, "SUCCESS_TERMINAL_JSON_HAS_NO_NOTICE"
    with patch.object(f.m.subprocess, "run", return_value=SimpleNamespace(stdout="fixture")) as run:
        assert f.m.command(["fixture"]) == "fixture"
        assert run.call_args.kwargs["timeout"] == 30


def main() -> None:
    failures: list[AssertionError] = []
    for test in (
        test_command_available,
        test_bridge_jobs_are_awaited_at_the_shared_process_seam,
        test_finished_job_records_preserve_the_original_result,
        test_synchronous_bridge_results_are_unchanged,
        test_failed_bridge_job_is_an_advisory,
        test_bridge_job_timeout_keeps_running_and_drain_retries,
        test_deploy_notice_after_verified_record,
        test_exception_notices_for_failure_and_holds,
        test_notice_failures_are_not_read_on_success,
        test_exception_notice_transport_uncertainty_preserves_hold,
        test_notice_identity_recurrence_and_reversal,
        test_restart_recovery_after_apply_commit_timeout,
        test_readiness_deadline_defers_then_next_run_records,
        test_serving_target_retries_readiness,
        test_docs_landing_no_swap,
        test_production_deploy_recorded,
        test_flat_swap_status_reaches_swap,
        test_receipt_lock_companion_is_ignored,
        test_tick_and_no_change,
        test_busy_deferral_and_stale_receipt,
        test_stale_running_receipt_with_live_pid_continues,
        test_held_running_receipt_defers,
        test_production_receipt_lock_port,
        test_attestation_and_etag,
        test_record_retry,
        test_identity_and_cli_adapters,
        test_malformed_swap_row_during_drain_is_retried,
        test_holds_role_reply_of_any_shape_is_advisory,
    ):
        try:
            test()
        except AssertionError as error:
            failures.append(error)
    if failures:
        raise AssertionError("; ".join(map(str, failures)))


if __name__ == "__main__":
    main()
