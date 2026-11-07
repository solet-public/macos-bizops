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
    assert result["decision"] == "skipped_nonproduction" and not writes(f), "DOCS_LANDING_NO_SWAP"
    f = Fixture()
    f.s.delta = ""
    result = f.run()
    assert result["decision"] == "skipped_nonproduction" and not writes(f), "EMPTY_DELTA_LANDING_NO_SWAP"
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


def test_deploy_notice_after_verified_record() -> None:
    """The BASE red and ordering mutant must both reach a verified deployment.

    Keep setup/readback checks before the named notice assertion so a broken
    fixture cannot masquerade as proof that the production notice is absent.
    """
    f = Fixture()
    result = f.run()
    assert result["decision"] == "deployed" and "REACHED_RECORD" in f.log, "DEPLOY_NOTICE_FIXTURE_REACHED_RECORD"
    assert "REACHED_VERIFIED_RECORD" in f.log and result["measurement_id"] == "cme-fixture", "DEPLOY_NOTICE_FIXTURE_VERIFIED_READBACK"
    calls = [a for a in f.log if isinstance(a, list) and len(a) > 2 and a[2] == NOTICE]
    assert len(calls) == 1, "DEPLOY_NOTICE_AFTER_VERIFIED_RECORD: exactly one notice"
    assert f.log.index("REACHED_RECORD") < f.log.index("REACHED_VERIFIED_RECORD") < f.log.index(calls[0]), "DEPLOY_NOTICE_AFTER_VERIFIED_RECORD: ordering"
    _assert_notice_identity(result, json.loads(calls[0][3]))


def _assert_notice_identity(result: dict[str, object], values: dict[str, object]) -> None:
    """Bind the queued receipt to the exact solet and deployment observation."""
    content = json.loads(str(values["content"]))
    notice = result["notice"]
    assert isinstance(notice, dict), "DEPLOY_NOTICE_RECEIPT_MAPPING"
    assert values["name"] == "Fixture-Solet-Main", "DEPLOY_NOTICE_SCOPED_RECIPIENT"
    assert (content["target"], content["release"], content["measurement_id"], content["decision"]) == (TARGET, "rel-new", "cme-fixture", "deployed"), "DEPLOY_NOTICE_EXACT_IDENTITY"
    assert notice["message_id"] == "arm-fixture" and notice["status"] == "queued", "DEPLOY_NOTICE_RECEIPT_RETAINED"
    assert notice["notice_key"] == content["notice_key"] and "same deployment" in content["duplicate_semantics"], "DEPLOY_NOTICE_DUPLICATE_KEY"


def test_notice_failures_preserve_deployment() -> None:
    """Transport and envelope failures are receipt uncertainty, never a redeploy.

    Check both effects, because preserving the terminal decision alone could
    hide an accidental second APPLY or deployed-observation write.
    """
    replies = ["not JSON", "[]", envelope({}), envelope({"message_id": ""}), envelope({"message_id": 7}), json.dumps({"status": "completed", "result": {"data": []}}), json.dumps({"status": "completed", "result": {"success": False, "error": "role_unbound"}}), json.dumps({"status": "failed"})]
    for reply in replies:
        f = Fixture()
        f.s.notice_reply = reply
        result = f.run()
        assert result["decision"] == "deployed" and result["measurement_id"] == "cme-fixture", "NOTICE_FAILURE_PRESERVES_DEPLOYMENT"
        assert result["notice"]["status"] == "uncertain" and result["notice"]["message_id"] is None and result["advisories"], "NOTICE_FAILURE_DISCLOSED"
        assert writes(f) == ["REACHED_APPLY_COMMIT", "REACHED_RECORD"] and f.log.count("REACHED_NOTICE") == 1, "NOTICE_FAILURE_NO_RETRY"
    f = Fixture()
    f.s.notice_timeout = True
    result = f.run()
    assert result["decision"] == "deployed" and result["notice"]["status"] == "uncertain" and f.log.count("REACHED_NOTICE") == 1, "NOTICE_TIMEOUT_PRESERVES_DEPLOYMENT"


def test_notice_identity_recurrence_and_reversal() -> None:
    """Duplicate measurements reuse a key; a reversal gets its own identity.

    Early exits have no verified completion branch and must send nothing.
    Already-recorded recovery may send once without repeating any deployment.
    """
    f = Fixture()
    first = f.run()
    for measurement in ("cme-fixture", "cme-reversal"):
        f = Fixture()
        f.s.serving, f.s.recorded, f.s.stale_first = TARGET, True, True
        f.s.measurement_id = measurement
        repeat = f.run()
        assert repeat["decision"] == "already_recorded" and not writes(f) and f.log.count("REACHED_NOTICE") == 1, "VERIFIED_ALREADY_RECORDED_NOTICE"
        assert (repeat["notice"]["notice_key"] == first["notice"]["notice_key"]) == (measurement == "cme-fixture"), "NOTICE_MEASUREMENT_IDENTITY"
    _assert_reversal_notice(first["notice"]["notice_key"])
    for field, value in (("baseline", TARGET), ("occupied", True), ("stuck", True), ("reject", True), ("delta", "M\0docs/x\0"), ("plugins_never_ready", True)):
        f = Fixture()
        setattr(f.s, field, value)
        result = f.run()
        assert result["decision"] not in ("deployed", "already_recorded") and "REACHED_NOTICE" not in f.log and "notice" not in result, "NO_UNVERIFIED_DEPLOY_NOTICE"


def _assert_reversal_notice(prior_key: str) -> None:
    """A later run can verify the opposite target with a new measurement key."""
    f = Fixture()
    f.s.target, f.s.baseline, f.s.serving = OLD, TARGET, TARGET
    f.s.measurement_id = "cme-rollback"
    result = f.run()
    assert result["decision"] == "deployed" and result["target"] == OLD and result["measurement_id"] == "cme-rollback", "REVERSAL_VERIFIED_SEPARATELY"
    assert result["notice"]["notice_key"] != prior_key and f.log.count("REACHED_NOTICE") == 1, "REVERSAL_DISTINCT_NOTICE"


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
    assert f.run()["decision"] == "already_current" and not writes(f)


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
    assert terminal["decision"] == "deployed" and terminal["notice"]["status"] == "uncertain", "NOTICE_FAILURE_TERMINAL_JSON_SUCCESS"
    with patch.object(f.m.subprocess, "run", return_value=SimpleNamespace(stdout="fixture")) as run:
        assert f.m.command(["fixture"]) == "fixture"
        assert run.call_args.kwargs["timeout"] == 30


def main() -> None:
    failures: list[AssertionError] = []
    for test in (
        test_command_available,
        test_deploy_notice_after_verified_record,
        test_notice_failures_preserve_deployment,
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
