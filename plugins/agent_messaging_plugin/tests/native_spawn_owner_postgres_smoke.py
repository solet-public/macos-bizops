#!/usr/bin/env python3
"""Native CLI ownership/start proof on provision_test_pg's private desk DB.

No real worker is launched: only the HostDriver is a double. Register reads,
actor identity observations, CAS ownership writes and child uptake use psolet.
Run under project-solet/scripts/provision_test_pg.py --exec, never a live DSN.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO_ROOT / "ananta/src"), str(REPO_ROOT / "plugins/agent_messaging_plugin/src"), str(Path(__file__).parent)]

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402
from _register_unit_double import FIXTURE_UNIT_ID, NativeOwnerCLIDouble, fixture_assignment  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402

import agent_messaging_plugin.session_hosts as hosts  # noqa: E402
import agent_messaging_plugin.session_lifecycle_verbs as verbs  # noqa: E402
from agent_messaging_plugin.lane_worktrees import LaneWorktree  # noqa: E402
from agent_messaging_plugin.register_unit_client import (  # noqa: E402
    PsoletRegisterUnitClient,
    RegisterActor,
    RegisterUnitError,
    UnitAssignment,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    LIFECYCLE_LIVE,
    LIFECYCLE_OVERDUE,
    LIFECYCLE_RETIRED,
    LIFECYCLE_SPAWNING,
    LIFECYCLE_TERMINATED,
    TABLE_MANAGED_SESSION,
)
from agent_messaging_plugin.session_lifecycle_store import read_managed_session, transition_lifecycle_state  # noqa: E402
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    SpawnSessionRequest,
    VerbError,
)
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    spawn_session as native_spawn_session,
)

CLI = str(Path.home() / "Workspace/project-solet/.venv/bin/psolet")
SOLET = "native-spawn-owner-test"


class FixtureEmbeddingHandler(BaseHTTPRequestHandler):
    """Local deterministic document-registration fixture, never a live embedder."""

    def do_POST(self) -> None:  # noqa: N802
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        data = []
        for index, text in enumerate(request["input"]):
            vector = [0.0] * 768
            vector[int("basalt lighthouse" in text)] = 1.0
            data.append({"index": index, "embedding": vector})
        body = json.dumps({"model": request["model"], "data": data}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        del format, args


class OwnerDriver:
    def __init__(self, case: NativeOwnerCase) -> None:
        self.case = case
        self.spec: dict[str, object] = {}
        self.spawns = 0
        self.turns = 0
        self.fail_host = False
        self.inject_cas = False

    def spawn(self, spec: Mapping[str, object]) -> str:
        self.spawns += 1
        self.spec = dict(spec)
        if self.fail_host:
            raise hosts.HostCannotSpawnError("named host failure")
        if self.inject_cas:
            self.case.cli("db", "unit", "record-event", self.case.unit, "--event-kind", "observation", "--detail", "concurrent write", *self.case.main_flags)
        return f"fixture-host-{self.spawns}"

    def alive(self, host_ref: str) -> bool:
        return host_ref.startswith("fixture-host-")

    def terminate(self, host_ref: str, grace_seconds: int) -> None:
        del host_ref, grace_seconds

    def driver_channel(self, host_ref: str) -> OwnerDriver:
        del host_ref
        return self

    def insert(self, text: str) -> None:
        del text

    def submit(self) -> None:
        pass

    def send(self, text: str) -> None:
        self.case.order_events.append("driver.send")
        instance = str(self.spec["agent_instance_id"])
        events = self.case.client._owner_snapshot(self.case.unit)[0]  # noqa: SLF001
        event = next(row for row in events if instance in str(row["detail"]) and "platform_spawn_identity_not_worker_uptake" in str(row["detail"]))
        expected = str(event["observed_by_actor_id"])
        # O1/O2/O3 die here, before child CLI start or any unrelated assertion.
        self.case.assertEqual(self.case.owner(), expected, "OWNER_TRANSFER_BEFORE_FIRSTTURN: actual child actor owns pinned Unit")
        self.case.assertNotEqual(expected, self.case.main_actor, "OWNER_TRANSFER_CHILD_NOT_MAIN: spawned actor identity")
        self.case.assertIn(self.case.assignment.revision.revision_id, text)
        child_flags = ["--solet", SOLET, "--agent-instance-id", instance, "--agent-session-id", f"ases-{instance}", "--agent-id", "codex", "--actor-model", "gpt-6.1-sol", "--actor-effort", "medium"]
        self.case.cli("db", "unit", "read-back", self.case.unit, "--text", "Child reads its pinned brief", *child_flags)
        if self.turns == 0:
            self.case.cli("db", "unit", "start", self.case.unit, *child_flags)
        self.turns += 1

    def capability_report(self) -> dict[str, object]:
        return {}

    def verify_config(self) -> list[str]:
        return []


class OwnerFirstGoPort:
    def __init__(self, case: NativeOwnerCase) -> None:
        self.case = case
        self.calls = 0

    def peer_send(self, request: object) -> Any:
        del request
        self.calls += 1
        self.case.order_events.append("first_go")
        return type("PeerSendReceipt", (), {"message_id": f"agm-native-first-go-{self.calls}"})()


class NativeOwnerCase(unittest.TestCase):
    def cli(self, *args: str) -> dict[str, Any]:
        result = subprocess.run((CLI, *args), env=self.environment, capture_output=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return cast("dict[str, Any]", json.loads(result.stdout))

    def owner(self) -> str:
        payload = self.cli("db", "unit", "show", self.unit)
        owners = [row["actor_id"] for row in payload["unit_assignment"] if row["assignment_kind"] == "owner" and row["released_at"] is None and not row["is_deleted"]]
        self.assertEqual(len(owners), 1, "OWNER_TRANSFER_SINGLE_OWNER")
        return str(owners[0])

    def _exercise_spawn(self) -> None:
        state = cast("StateManagementInterface", RealShapeState())
        driver = OwnerDriver(self)
        self.order_events: list[str] = []
        first_go_port = OwnerFirstGoPort(self)
        actor = RegisterActor(self.main_flags[3], "ases-main", "", "native-owner-lane")
        req = verbs.SpawnSessionRequest(
            role_class="ephemeral",
            lane_id="native-owner-lane",
            unit_id=self.unit,
            assignment=self.assignment,
            work_class="analysis_deliverable",
            budget_line="native-owner-test",
            host="native-owner-fixture",
            agent_runtime="codex",
            model="gpt-6.1-sol",
            effort="medium",
            dispatch_kind="infrastructure",
            spawned_by_instance_id=actor.agent_instance_id,
            first_go="GO native owner",
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            worktree = LaneWorktree(root, root, root, "fixture")
            with (
                patch.dict(hosts._REGISTRY, {("codex", "native-owner-fixture"): driver}),  # noqa: SLF001
                patch.object(verbs, "_provision_worktree_for_request", return_value=worktree),
                patch.object(verbs, "remove_lane_worktree"),
            ):
                result = verbs.spawn_session(state, req, register=self.client, actor=actor, messaging=first_go_port)
                self.assertEqual(self.order_events[:2], ["first_go", "driver.send"], "FIRST_GO_BEFORE_NATIVE_OWNER_FIRSTTURN")
                self.assertTrue(result["first_turn_delivered"], f"OWNER_TRANSFER_BEFORE_FIRSTTURN: {result['first_turn_error']}")
                self.assertEqual(driver.turns, 1, "OWNER_TRANSFER_CHILD_START_WITHOUT_MAIN_REPAIR")
                self.assertEqual(result["ownership_receipt"]["owner_actor_id"], self.owner())
                receipt = result["ownership_receipt"]
                version = self.client._owner_snapshot(self.unit)[1]  # noqa: SLF001
                replay = self.client.transfer_spawn_owner(receipt, actor)
                self.assertEqual(replay["ownership_status"], "already_assigned", "OWNER_TRANSFER_REPLAY_NO_WRITE")
                self.assertEqual(self.client._owner_snapshot(self.unit)[1], version)  # noqa: SLF001
                self.assertEqual(driver.spawns, 1, "OWNER_TRANSFER_REPLAY_NO_SPAWN")
                driver.fail_host = True
                prior_owner = self.owner()
                with self.assertRaisesRegex(verbs.VerbError, "host_cannot_spawn"):
                    verbs.spawn_session(state, replace(req, lane_id="failed-host"), register=self.client, actor=actor, messaging=first_go_port)
                self.assertEqual(self.owner(), prior_owner, "OWNER_TRANSFER_HOST_FAILURE_PRESERVES_OWNER")
                driver.fail_host = False
                driver.inject_cas = True
                concurrent = verbs.spawn_session(state, replace(req, lane_id="cas-host"), register=self.client, actor=actor, messaging=first_go_port)
                self.assertEqual(concurrent["ownership_receipt"]["owner_actor_id"], self.owner(), "OWNER_TRANSFER_UNRELATED_EVENT_SUCCEEDS")
                self.assertTrue(concurrent["first_turn_delivered"], "OWNER_TRANSFER_UNRELATED_EVENT_FIRSTTURN")
                self.assertEqual(driver.turns, 2)
                driver.inject_cas = False
                prior_owner = self.owner()
                failed_req = replace(req, lane_id="boundary-host")
                with patch.object(self.client, "transfer_spawn_owner", side_effect=RegisterUnitError("unit_register_unavailable", "explicit transport failure")), self.assertRaises(verbs.VerbError) as failure:
                    verbs.spawn_session(state, failed_req, register=self.client, actor=actor, messaging=first_go_port)
                self.assertEqual(failure.exception.code, "spawn_owner_transfer_failed", "OWNER_TRANSFER_BOUNDARY_TYPED_FAILURE")
                self.assertIn("explicit transport failure", failure.exception.message)
                self.assertEqual(driver.turns, 2, "OWNER_TRANSFER_BOUNDARY_NO_FIRSTTURN")
                self.assertEqual(self.owner(), prior_owner, "OWNER_TRANSFER_BOUNDARY_PRESERVES_OWNER")
                row = read_managed_session(state, str(driver.spec["agent_instance_id"]))
                partial = row["capability_report"]["native_spawn_owner"]
                self.assertEqual(partial["host_ref"], "fixture-host-4", "OWNER_TRANSFER_EXACT_PARTIAL_HOST")
                self.assertIsNone(row.get("first_turn_at"), "OWNER_TRANSFER_PENDING_NO_FIRSTTURN_AT")
                for source, target in ((LIFECYCLE_SPAWNING, LIFECYCLE_LIVE), (LIFECYCLE_LIVE, LIFECYCLE_OVERDUE)):
                    transition_lifecycle_state(state, agent_instance_id=str(row["agent_instance_id"]), from_state=source, to_state=target, directed_by="fixture", reason="registered retained host becomes overdue")
                pending_version = self.client._owner_snapshot(self.unit)[1]  # noqa: SLF001
                recovered = verbs.spawn_session(state, failed_req, register=self.client, actor=actor, messaging=first_go_port)
                self.assertEqual(recovered["agent_instance_id"], row["agent_instance_id"], "OWNER_TRANSFER_RECOVERY_SAME_IDENTITY")
                self.assertEqual(driver.spawns, 4, "OWNER_TRANSFER_RECOVERY_NO_SECOND_HOST")
                self.assertEqual(driver.turns, 3, "OWNER_TRANSFER_RECOVERY_CHILD_READBACK")
                self.assertTrue(recovered["first_turn_delivered"], "OWNER_TRANSFER_OVERDUE_SAME_HOST_FIRSTTURN")
                self.assertEqual(recovered["host_ref"], partial["host_ref"])
                self.assertEqual(recovered["ownership_receipt"]["predecessor_owner_actor_ids"], partial["predecessor_owner_actor_ids"])
                self.assertEqual(recovered["ownership_receipt"]["history_version"], pending_version + 1, "OWNER_TRANSFER_RECOVERY_NO_NEW_IDENTITY_OBSERVATION")
                with self.assertRaisesRegex(RegisterUnitError, "spawn_owner_superseded"):
                    self.client.transfer_spawn_owner(receipt, actor)
                self.assertEqual(self.owner(), recovered["ownership_receipt"]["owner_actor_id"], "OWNER_TRANSFER_DELAYED_REPLAY_PRESERVES_NEW_OWNER")
                print("PASS native owner CLI lifecycle: fresh CAS, pre-firstturn, child start, replay, host failure, boundary failure, overdue same-host recovery, delayed replay")


class NativeSpawnOwnerTests(NativeOwnerCase):
    def test_native_spawn_owner_lifecycle(self) -> None:
        dsn = os.environ["PROJECT_SOLET_DESK_TEST_DSN"]
        address = dict(item.split("=", 1) for item in shlex.split(dsn))
        self.assertEqual(address["host"], "127.0.0.1")
        self.assertNotEqual(int(address["port"]), 5432)
        self.assertEqual(address["dbname"], "project_solet_desk_test")
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith(("GIT_", "AGENT_")) and key != "SOLET_NAME"}
        self.environment["PROJECT_SOLET_DATABASE_DSN"] = dsn
        self.main_flags = ["--solet", SOLET, "--agent-instance-id", f"agi-main-{uuid4().hex}", "--agent-session-id", "ases-main", "--agent-id", "codex", "--actor-model", "gpt-6.1-sol", "--actor-effort", "medium"]
        fixture = tempfile.TemporaryDirectory(prefix="native-owner-repository-")
        self.addCleanup(fixture.cleanup)
        fixture_root = Path(fixture.name) / "repository"
        fixture_root.mkdir()
        (fixture_root / "AGENTS.md").write_text(
            "Private native ownership fixture: transfer the existing pinned Unit to the actual child actor before its first turn, preserving same-host recovery and append-only ownership history.\n",
            encoding="utf-8",
        )
        for command in (
            ("git", "init", "--quiet", "-b", "master", str(fixture_root)),
            ("git", "-C", str(fixture_root), "add", "--", "AGENTS.md"),
            ("git", "-C", str(fixture_root), "-c", "user.name=NativeFixture", "-c", "user.email=native-fixture@example.invalid", "commit", "--quiet", "-m", "Native fixture source"),
            ("git", "-C", str(fixture_root), "remote", "add", "origin", "https://github.com/dwestgate/native-spawn-owner-fixture.git"),
        ):
            subprocess.run(command, env=self.environment, capture_output=True, check=True, timeout=10)
        server = HTTPServer(("127.0.0.1", 0), FixtureEmbeddingHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            repository = self.cli("db", "repository", "register", "--host", "github.com", "--owner", "dwestgate", "--name", "native-spawn-owner-fixture")
            self._configure_repository_convention(str(repository["repository_id"]))
            source = subprocess.run(
                ("git", "-C", str(fixture_root), "log", "-1", "--format=%H", "--", "AGENTS.md"),
                env=self.environment,
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout.strip()
            document = self.cli(
                "document",
                "register",
                "AGENTS.md",
                "--repo-root",
                str(fixture_root),
                "--source-commit",
                source,
                "--name",
                "Native spawn fixture",
                "--embedding-description",
                "Private register fixture for native lane ownership: an existing Unit starts with a Main grooming owner; the spawned child actor must own that same pinned Unit before any first turn, with CAS recovery and replay evidence.",
                "--base-url",
                f"http://127.0.0.1:{server.server_port}",
                *self.main_flags,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        dispatched = self.cli(
            "db",
            "unit",
            "dispatch",
            "--unit-key",
            f"native-owner-{uuid4().hex}",
            "--kind",
            "infrastructure",
            "--repository-id",
            str(repository["repository_id"]),
            "--repo-root",
            str(fixture_root),
            "--brief-document-revision-id",
            str(document["revision_id"]),
            "--model",
            "gpt-6.1-sol",
            "--effort",
            "medium",
            *self.main_flags,
        )
        self.unit = str(dispatched["unit_id"])
        observation = self.cli("db", "unit", "record-event", self.unit, "--event-kind", "observation", "--detail", "Main grooming reservation", *self.main_flags)
        self.main_actor = str(self.cli("db", "unit", "event-show", observation["unit_event_id"])["observed_by_actor_id"])
        self.cli("db", "unit", "set-owner", self.unit, "--actor-id", self.main_actor, "--reason", "fixture grooming owner", "--evidence", "private fixture", *self.main_flags)
        with patch.dict(os.environ, self.environment, clear=True):
            self.client = PsoletRegisterUnitClient(CLI, solet_name=SOLET)
            unit = self.client.show_unit(self.unit)
            self.assignment = UnitAssignment(unit.unit_id, unit.unit_key, unit.repository_id, self.client.read_brief(unit))
            self._exercise_spawn()

    def _configure_repository_convention(self, repository_id: str) -> None:
        """Install the retained repository convention policy for the private fixture id."""
        directory = tempfile.TemporaryDirectory(prefix="native-owner-convention-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "sitecustomize.py").write_text(
            "from dataclasses import replace\n"
            "from project_solet import reference_service as refs\n"
            f"repository = {repository_id!r}\n"
            "policy = replace(refs.REPOSITORY_CONVENTIONS['rep_7111540c-ac4a-468c-8f84-b67d8756d1d0'], repository_id=repository)\n"
            "refs.REPOSITORY_CONVENTIONS[repository] = policy\n"
            "refs.REPOSITORY_CONVENTION_REVISIONS[(repository, policy.version, policy.sha256)] = policy\n",
            encoding="utf-8",
        )
        self.environment["PYTHONPATH"] = os.pathsep.join(part for part in (str(root), self.environment.get("PYTHONPATH", "")) if part)


class NativeOwnerBoundaryDoubleTests(NativeOwnerCase):
    def cli(self, *args: str) -> dict[str, Any]:
        return cast("dict[str, Any]", json.loads(self.client._run(*args).stdout))  # noqa: SLF001

    def test_native_cli_double_owner_outcomes(self) -> None:
        self.client = NativeOwnerCLIDouble()
        self.assignment = fixture_assignment()
        self.unit = self.assignment.unit_id
        self.main_actor = "act-agi-main"
        self.main_flags = ["--solet", SOLET, "--agent-instance-id", "agi-main"]
        self._exercise_spawn()


def _check(condition: object, label: str) -> None:
    """The lifecycle smoke replaces this callback to collect named pass/fail totals."""
    if not condition:
        raise AssertionError(label)


def _state() -> StateManagementInterface:
    return cast("StateManagementInterface", RealShapeState())


def _spawn_req(**overrides: Any) -> SpawnSessionRequest:
    base: dict[str, Any] = {"role_class": "ephemeral", "lane_id": "lane-1", "unit_id": fixture_assignment().unit_id, "assignment": fixture_assignment(), "work_class": "analysis_deliverable", "budget_line": "budget-1", "host": "operator", "directed_by": "operator:none", "dispatch_kind": "infrastructure"}
    return SpawnSessionRequest(**{**base, **overrides})


class _FakeChannel:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


class _FakeDriverWithChannel:
    def __init__(self) -> None:
        self.channel = _FakeChannel()

    def terminate(self, host_ref: str, grace_seconds: int) -> None:
        del host_ref, grace_seconds

    def driver_channel(self, host_ref: str) -> _FakeChannel:
        del host_ref
        return self.channel

    def capability_report(self) -> dict[str, object]:
        return {}

    def verify_config(self) -> list[str]:
        return []


def _owner_detail() -> dict[str, Any]:
    """Seed the complete caller identity used by production preparation.

    This is the same pinned assignment supplied to native spawn. Dispatch and
    attempt are explicit so the later-attempt guard can be exercised without
    inventing a predecessor field in the test. The production receipt supplies
    that anchor from the CLI double's active owner snapshot.
    """
    assignment = fixture_assignment()
    return {
        "unit_id": assignment.unit_id,
        "revision_id": assignment.revision.revision_id,
        "assignment_sha256": assignment.sha256,
        "agent_instance_id": "agi-child",
        "lane_id": "owner-lane",
        "local_name": "owner-child",
        "agent_runtime": "codex",
        "model": "gpt-6.1-sol",
        "effort": "medium",
        "dispatch_id": "mdp-owner",
        "attempt_number": 1,
        "spawned_by_instance_id": "agi-main",
        "observation_kind": "platform_spawn_identity_not_worker_uptake",
    }


def _owner_event(client: NativeOwnerCLIDouble, detail: str = "unrelated event") -> None:
    """Append a genuine event through the existing CLI boundary double.

    Its frontier advances exactly as a native unrelated observation does.
    Using the command path also records argv for the explicit CAS assertion;
    changing a receipt ordinal alone would not reproduce the reported defect.
    """
    client._run(
        "db",
        "unit",
        "record-event",
        FIXTURE_UNIT_ID,
        "--event-kind",
        "observation",  # noqa: SLF001
        "--detail",
        detail,
        "--agent-instance-id",
        "agi-main",
    )


def _active_owner(client: NativeOwnerCLIDouble) -> str:
    """Read the unique active fixture owner, with ambiguity represented empty.

    Released assignments remain available to production delayed-replay checks.
    The double keeps append-only events and historical assignment rows, so
    these assertions measure current ownership independently of the receipt.
    """
    owners = [str(row["actor_id"]) for row in client.assignments if not row.get("released_at") and not row.get("is_deleted")]
    return owners[0] if len(owners) == 1 else ""


def _transfer_result(client: NativeOwnerCLIDouble, receipt: Mapping[str, Any]) -> tuple[RegisterUnitError | None, dict[str, Any]]:
    """Reach production transfer while keeping the named assertion reachable.

    Expected boundary errors are returned locally, rather than terminating the
    whole smoke before its killer label. Unexpected setup errors still escape.
    This matters for both the red reproduction and the three mutation runs.
    """
    try:
        return None, client.transfer_spawn_owner(receipt, RegisterActor("agi-main", "ases-main", "", "owner-lane"))
    except RegisterUnitError as exc:
        return exc, {}


def _set_owner_calls(client: NativeOwnerCLIDouble) -> list[tuple[str, ...]]:
    return [argv for argv in client.commands if argv[2] == "set-owner"]


def _exercise_current_history() -> tuple[RegisterUnitError | None, dict[str, Any], str, dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
    """An unrelated event advances CAS without changing the immutable predecessor."""
    client = NativeOwnerCLIDouble()
    receipt = client.prepare_spawn_owner(_owner_detail())
    _owner_event(client)
    latest = len(client.native_events)
    error, result = _transfer_result(client, receipt)
    current_owner = _active_owner(client)
    argv = _set_owner_calls(client)[-1]
    _check(int(argv[argv.index("--expected-history-version") + 1]) == latest, "OWNER_TRANSFER_FRESH_EXPLICIT_CAS")
    if error is None:
        before = len(client.native_events)
        replay_error, replay = _transfer_result(client, receipt)
        _check((replay_error, replay.get("ownership_status"), len(client.native_events)) == (None, "already_assigned", before), "OWNER_TRANSFER_REPLAY_APPENDS_NOTHING")
    return error, result, current_owner, receipt


def _replace_owner(client: NativeOwnerCLIDouble, owner: str) -> None:
    for row in client.assignments:
        row["released_at"] = "released"
    if owner:
        client.assignments.append({"actor_id": owner, "assignment_kind": "owner", "released_at": None, "is_deleted": False})
    _owner_event(client, "legitimate owner replacement")


def _check_predecessor_replacements() -> None:
    """Exercise replaced and cleared ownership with fresh and old receipts.

    Aligning one receipt ordinal isolates the missing predecessor guard on
    base code. Repeating preparation after the replacement must reuse the
    original identity event and predecessor, never authorize the new owner.
    Both variants assert that refusal happens before any set-owner command.
    """
    for replacement_owner in ("act-replacement", ""):
        for align_version in (True, False):
            client = NativeOwnerCLIDouble()
            receipt = client.prepare_spawn_owner(_owner_detail())
            _replace_owner(client, replacement_owner)
            reused = client.prepare_spawn_owner(_owner_detail())
            _check((reused.get("predecessor_owner_actor_ids"), reused["identity_event_id"]) == (receipt.get("predecessor_owner_actor_ids"), receipt["identity_event_id"]), "OWNER_TRANSFER_PREPARATION_REUSES_ANCHOR")
            if align_version:
                receipt["expected_history_version"] = len(client.native_events)
            before = len(_set_owner_calls(client))
            error, _ = _transfer_result(client, receipt)
            active_owner_after = _active_owner(client)
            set_owner_calls_after = len(_set_owner_calls(client))
            _check(error is not None and active_owner_after == replacement_owner and set_owner_calls_after == before, "OWNER_TRANSFER_PREDECESSOR_REPLACEMENT_VARIANTS")


def _check_prepared_evidence_integrity() -> None:
    """Keep missing, fabricated and ambiguous predecessor evidence distinct.

    The missing case edits the private historical observation, while the
    inconsistent case edits only the proposed receipt. Neither is an owner
    change that a fresh history ordinal could legitimately authorize.
    Multiple current owners must also be refused before the CLI write.
    """
    for defect in ("multiple", "missing", "inconsistent"):
        client = NativeOwnerCLIDouble()
        receipt = client.prepare_spawn_owner(_owner_detail())
        if defect == "multiple":
            client.assignments.append({"actor_id": "act-extra", "assignment_kind": "owner", "released_at": None, "is_deleted": False})
        elif defect == "missing":
            event = client.native_events[0]
            detail = json.loads(event["detail"])
            del detail["predecessor_owner_actor_ids"]
            event["detail"] = json.dumps(detail)
        else:
            receipt["predecessor_owner_actor_ids"] = ["act-fabricated"]
        error, _ = _transfer_result(client, receipt)
        _check((error is not None, _set_owner_calls(client)) == (True, []), f"OWNER_TRANSFER_EVIDENCE_REFUSES_{defect.upper()}")


class _PostSnapshotRaceClient(NativeOwnerCLIDouble):
    race = True

    def _set_owner(self, arguments: tuple[str, ...]) -> dict[str, Any]:
        if self.race:
            self.race = False
            _owner_event(self, "post-snapshot race")
        return super()._set_owner(arguments)


def _exercise_prepared_owner() -> tuple[RegisterUnitError | None, str, str, int, int]:  # pyright: ignore[reportUnusedFunction]
    """Changed/released owners and superseded attempts cannot be overwritten."""
    client = NativeOwnerCLIDouble()
    receipt = client.prepare_spawn_owner(_owner_detail())
    replacement_owner = "act-replacement"
    _replace_owner(client, replacement_owner)
    receipt["expected_history_version"] = len(client.native_events)
    set_owner_calls_before = len(_set_owner_calls(client))
    changed_error, _ = _transfer_result(client, receipt)
    active_owner_after = _active_owner(client)
    set_owner_calls_after = len(_set_owner_calls(client))
    _check_predecessor_replacements()
    _check_prepared_evidence_integrity()
    client = NativeOwnerCLIDouble()
    receipt = client.prepare_spawn_owner(_owner_detail())
    _, assigned = _transfer_result(client, receipt)
    before = len(client.native_events)
    error, replay = _transfer_result(client, receipt)
    _check((error, replay.get("ownership_status"), len(client.native_events)) == (None, "already_assigned", before), "OWNER_TRANSFER_EXACT_CHILD_NO_WRITE")
    _replace_owner(client, "act-replacement")
    error, _ = _transfer_result(client, assigned or receipt)
    _check((getattr(error, "code", None), _active_owner(client)) == ("spawn_owner_superseded", "act-replacement"), "OWNER_TRANSFER_RELEASED_CHILD_REFUSED")
    client = NativeOwnerCLIDouble()
    receipt = client.prepare_spawn_owner(_owner_detail())
    client.prepare_spawn_owner({**_owner_detail(), "agent_instance_id": "agi-newer", "attempt_number": 2})
    error, _ = _transfer_result(client, receipt)
    _check((getattr(error, "code", None), _set_owner_calls(client)) == ("spawn_owner_superseded", []), "OWNER_TRANSFER_HIGHER_ATTEMPT_REFUSED")
    client = _PostSnapshotRaceClient()
    receipt = client.prepare_spawn_owner(_owner_detail())
    error, _ = _transfer_result(client, receipt)
    _check((error is not None, _active_owner(client)) == (True, "act-agi-main"), "OWNER_TRANSFER_POST_SNAPSHOT_RACE_REFUSED")
    error, recovered = _transfer_result(client, receipt)
    _check((error, recovered.get("ownership_status")) == (None, "assigned"), "OWNER_TRANSFER_POST_SNAPSHOT_RACE_RECOVERABLE")
    return changed_error, active_owner_after, replacement_owner, set_owner_calls_after, set_owner_calls_before


class _PendingOwnerClient(NativeOwnerCLIDouble):
    fail_transfer = True

    def transfer_spawn_owner(self, receipt: Mapping[str, Any], actor: RegisterActor) -> dict[str, Any]:
        if self.fail_transfer:
            raise RegisterUnitError("unit_register_unavailable", "explicit injected transfer failure")
        return super().transfer_spawn_owner(receipt, actor)


class _OwnerFirstTurnChannel(_FakeChannel):
    def __init__(self, driver: _PendingOwnerDriver) -> None:
        super().__init__()
        self.driver = driver

    def send(self, text: str) -> None:
        _check(_active_owner(self.driver.client) == "act-" + str(self.driver.spec["agent_instance_id"]), "OWNER_TRANSFER_BEFORE_CHANNEL_SEND")
        super().send(text)


class _PendingOwnerDriver(_FakeDriverWithChannel):
    def __init__(self, client: _PendingOwnerClient) -> None:
        super().__init__()
        self.client = client
        self.spawns = 0
        self.spec: dict[str, object] = {}
        self.host_alive = True
        self.channel = _OwnerFirstTurnChannel(self)

    def spawn(self, spec: object) -> str:
        self.spawns += 1
        self.spec = dict(cast("Mapping[str, object]", spec))
        return "fake-host-ref"

    def alive(self, host_ref: str) -> bool:
        del host_ref
        return self.host_alive


def _pending_owner_fixture() -> tuple[StateManagementInterface, _PendingOwnerClient, _PendingOwnerDriver, SpawnSessionRequest, RegisterActor]:
    """Retain a real native-spawn fixture row after one explicit boundary error.

    No stale ordinal or lifecycle transition is fabricated to cause failure.
    The host has been launched once and has received no first turn. Removing
    the injected failure permits exact-repeat recovery against the same client,
    whose original actor observation and predecessor remain in its history.
    """
    client = _PendingOwnerClient()
    driver = _PendingOwnerDriver(client)
    state = _state()
    req = _spawn_req(host="owner-fixture", agent_runtime="codex", model="gpt-6.1-sol", effort="medium", local_name="pending-owner")
    actor = RegisterActor("agi-main", "ases-main", "", req.lane_id)
    with patch.dict(hosts._REGISTRY, {("codex", "owner-fixture"): driver}):  # noqa: SLF001
        try:
            native_spawn_session(state, req, register=client, actor=actor)
        except VerbError as exc:
            _check((exc.code, driver.spawns, driver.channel.sent) == ("spawn_owner_transfer_failed", 1, []), "OWNER_TRANSFER_EXPLICIT_FAILURE_RETAINS_HOST")
    client.fail_transfer = False
    return state, client, driver, req, actor


def _recover_owner(state: StateManagementInterface, client: _PendingOwnerClient, driver: _PendingOwnerDriver, req: SpawnSessionRequest, actor: RegisterActor) -> tuple[VerbError | None, dict[str, Any]]:
    with patch.dict(hosts._REGISTRY, {("codex", "owner-fixture"): driver}):  # noqa: SLF001
        try:
            return None, native_spawn_session(state, req, register=client, actor=actor)
        except VerbError as exc:
            return exc, {}


def _owner_transition(state: StateManagementInterface, instance: str, source: str, target: str) -> None:
    transition_lifecycle_state(state, agent_instance_id=instance, from_state=source, to_state=target, directed_by="operator:none", reason="pending ownership fixture")


def _check_pending_owner_exclusions() -> None:
    """Exercise recovery exclusions in independent retained-host fixtures.

    Terminal states may admit an ordinary replacement, but cannot resume the
    old identity. Other exclusions must leave name/identity guards effective.
    A dead host requires explicit lifecycle recovery. Dispatch, caller and
    attempt mismatches each alter only their own field in the private receipt.
    """
    for exclusion in ("terminated", "retired", "dead", "dispatch", "caller", "attempt", "first_turn"):
        state, client, driver, req, actor = _pending_owner_fixture()
        instance = str(driver.spec["agent_instance_id"])
        row = read_managed_session(state, instance)
        report = row["capability_report"]
        if exclusion in {"terminated", "retired"}:
            _owner_transition(state, instance, LIFECYCLE_SPAWNING, LIFECYCLE_TERMINATED)
            if exclusion == "retired":
                _owner_transition(state, instance, LIFECYCLE_TERMINATED, LIFECYCLE_RETIRED)
        elif exclusion == "dead":
            driver.host_alive = False
        elif exclusion == "first_turn":
            state.update_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_MANAGED_SESSION, "filters": {"agent_instance_id": instance}}, {"first_turn_at": datetime.now(UTC)})
        else:
            field, value = {"dispatch": ("dispatch_id", "mdp-other"), "caller": ("spawned_by_instance_id", "agi-other"), "attempt": ("attempt_number", 2)}[exclusion]
            report["native_spawn_owner"][field] = value
            state.update_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_MANAGED_SESSION, "filters": {"agent_instance_id": instance}}, {"capability_report": report})
        error, recovered = _recover_owner(state, client, driver, req, actor)
        _check(recovered.get("agent_instance_id") != instance and (error is not None or driver.spawns == 2), f"OWNER_TRANSFER_PENDING_EXCLUDES_{exclusion.upper()}")


def _check_pending_predecessor_preserved() -> None:
    """An overdue exact repeat cannot overwrite an intervening owner.

    The persisted receipt is captured before the replacement, not regenerated
    from its current owner. Recovery must preserve that identity and predecessor
    in the failure report while sending nothing and launching no second host.
    The unrelated owner-change event remains the fixture's final history row.
    """
    state, client, driver, req, actor = _pending_owner_fixture()
    instance = str(driver.spec["agent_instance_id"])
    original = dict(read_managed_session(state, instance)["capability_report"]["native_spawn_owner"])
    _owner_transition(state, instance, LIFECYCLE_SPAWNING, LIFECYCLE_LIVE)
    _owner_transition(state, instance, LIFECYCLE_LIVE, LIFECYCLE_OVERDUE)
    _replace_owner(client, "act-replacement")
    before = len(client.native_events)
    error, _ = _recover_owner(state, client, driver, req, actor)
    retained = read_managed_session(state, instance)["capability_report"]["native_spawn_owner"]
    _check((getattr(error, "code", None), _active_owner(client), len(client.native_events), driver.spawns, driver.channel.sent) == ("spawn_owner_transfer_failed", "act-replacement", before, 1, []), "OWNER_TRANSFER_RECOVERY_CHANGED_PREDECESSOR_REFUSED")
    _check((retained["identity_event_id"], retained["predecessor_owner_actor_ids"]) == (original["identity_event_id"], original["predecessor_owner_actor_ids"]), "OWNER_TRANSFER_RECOVERY_RETAINS_ORIGINAL_ANCHOR")


def _exercise_overdue_owner() -> tuple[VerbError | None, dict[str, Any], str, _PendingOwnerDriver]:  # pyright: ignore[reportUnusedFunction]
    """Overdue recovery retains host, assignment, attempt and predecessor before send."""
    state, client, driver, req, actor = _pending_owner_fixture()
    retained_id = str(driver.spec["agent_instance_id"])
    row = read_managed_session(state, retained_id)
    receipt = dict(row["capability_report"]["native_spawn_owner"])
    _check((bool(row["host_ref"]), row.get("first_turn_at"), _active_owner(client)) == (True, None, "act-agi-main"), "OWNER_TRANSFER_PENDING_NO_FIRSTTURN_PRIOR_OWNER")
    _owner_transition(state, retained_id, LIFECYCLE_SPAWNING, LIFECYCLE_LIVE)
    _owner_transition(state, retained_id, LIFECYCLE_LIVE, LIFECYCLE_OVERDUE)
    before = len(client.native_events)
    error, recovered = _recover_owner(state, client, driver, req, actor)
    after = read_managed_session(state, retained_id)
    _check((after["host_ref"], after["assignment_sha256"], len(client.native_events), after["capability_report"]["native_spawn_owner"]["attempt_number"]) == (row["host_ref"], row["assignment_sha256"], before + 1, receipt["attempt_number"]), "OWNER_TRANSFER_RECOVERY_PINNED_NO_NEW_OBSERVATION")
    _check_pending_owner_exclusions()
    _check_pending_predecessor_preserved()
    return error, recovered, retained_id, driver


if __name__ == "__main__":
    # Disclose fixture absence at process entry; importing this module remains safe.
    if "PROJECT_SOLET_DESK_TEST_DSN" not in os.environ:
        print("SKIP native spawn owner PostgreSQL fixture absent: PROJECT_SOLET_DESK_TEST_DSN; run under project-solet/scripts/provision_test_pg.py --exec")
        print('BORN_CLONE_SKIP_WITNESS={"reason":"local_postgres_fixture_unreachable"}')
        raise SystemExit(77)
    unittest.main()
