#!/usr/bin/env python3
"""Exercise battery admission against neutral leases with controlled time."""

from __future__ import annotations

import argparse
import fcntl
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable, Generator
from contextlib import contextmanager, nullcontext, redirect_stderr
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from quality_gates import host_compute_budget, run_smokes  # noqa: E402
from quality_gates.tests import host_compute_budget_smoke  # noqa: E402

admission = run_smokes.battery_admission
_SELF = "quality_gates/tests/run_smokes_admission_smoke.py"


class _PeerLeaseFixture:
    """An independent writer and reader of the shared on-disk lease format."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.pid = os.getpid()
        birth = host_compute_budget.process_birth(self.pid)
        if birth is None:
            raise AssertionError("lease fixture owner has no live process identity")
        self.birth = birth
        host_compute_budget_smoke.write_host_snapshot(root)
        self.leases = root / "leases"
        self.leases.mkdir(mode=0o700)

    @contextmanager
    def _locked(self) -> Generator[None]:
        descriptor = os.open(self.root / "settings.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "r+b") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def try_acquire(self, token: str, cpu_slots: int) -> bool:
        with self._locked():
            snapshot = json.loads((self.root / "settings.json").read_text())
            capacity = snapshot["capacities"]["cpu_slots"]["value"]
            running = 0
            for path in self.leases.glob("*.json"):
                row = json.loads(path.read_text())
                if path.stem != token and row["schema"] == 1 and row["state"] == "running" and (row["pid"], row["owner_start"]) == (self.pid, self.birth):
                    running += row["demand"]["cpu_slots"]
            admitted = running + cpu_slots <= capacity
            row = {
                "schema": 1,
                "token": token,
                "job_id": token,
                "pid": self.pid,
                "owner_start": self.birth,
                "started_at": time.time(),
                "launcher": "fixture-peer",
                "job_kind": "landing_gate",
                "workers": None,
                "demand": {"cpu_slots": cpu_slots, "pg_segments": 0},
                "state": "running" if admitted else "waiting",
                "reason": "admitted" if admitted else f"cpu_slots running={running} candidate={cpu_slots} capacity={capacity}",
            }
            pending = self.leases / f".{token}.pending"
            pending.write_text(json.dumps(row))
            pending.replace(self.leases / f"{token}.json")
            return admitted

    def release(self, token: str) -> None:
        with self._locked():
            (self.leases / f"{token}.json").unlink()


class _AdmissionFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "tickets"
        home = self.root / "home"
        home.mkdir()
        host = self.root / "host"
        environment = patch.dict(os.environ, {"HOME": str(home), admission.DIRECTORY_ENV: str(self.directory), "RUN_SMOKES_PRIORITY": "normal", "HOST_HEAVY_LEASE_DIR": str(host)})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(admission.INHERITED_ENV, None)
        self.peer = _PeerLeaseFixture(host)

    def _seed(self, ticket: admission.Ticket) -> None:
        with admission._locked(self.directory):
            admission._publish(self.directory, ticket)

    def _holder(self) -> admission.Ticket:
        holder = admission.Ticket("holder", os.getpid(), 4, "normal", 0, "fixture", "running")
        self._seed(holder)
        return holder

    def _rows(self) -> list[dict[str, object]]:
        return [json.loads(path.read_text()) for path in self.directory.glob("*.json")]


class AdmissionSmoke(_AdmissionFixture):
    def test_peer_lease_holds_local_battery(self) -> None:
        self.assertTrue(self.peer.try_acquire("peer-holder", 18))
        sleeps: list[float] = []

        def release_after_wait(seconds: float) -> None:
            sleeps.append(seconds)
            waits = [row for row in self._rows() if row["state"] == "waiting"]
            self.assertEqual(len(waits), 1)
            self.assertIn("cpu_slots running=18 candidate=8 capacity=24", str(waits[0]["reason"]))
            self.peer.release("peer-holder")

        with admission.admit(8, job_kind="full_battery", reference_workers=8, sleep=release_after_wait, report=lambda _line: None):
            self.assertEqual(len(sleeps), 1, "PEER_LEASE_18_PLUS_LOCAL_8_EXCEEDS_24")

    def test_local_battery_holds_peer_lease(self) -> None:
        with admission.admit(8, job_kind="full_battery", reference_workers=8, report=lambda _line: None):
            self.assertTrue(self.peer.try_acquire("peer-fits", 16))
            self.peer.release("peer-fits")
            self.assertFalse(self.peer.try_acquire("peer-waits", 18), "LOCAL_LEASE_8_PLUS_PEER_18_EXCEEDS_24")
        self.assertTrue(self.peer.try_acquire("peer-waits", 18))
        self.peer.release("peer-waits")

    def test_first_local_ticket_waits_when_running_lease_exceeds_capacity(self) -> None:
        self.assertTrue(self.peer.try_acquire("only-holder", 18))
        waits: list[float] = []

        def release_after_wait(seconds: float) -> None:
            waits.append(seconds)
            self.peer.release("only-holder")

        with admission.admit(8, job_kind="landing_battery", reference_workers=8, sleep=release_after_wait, report=lambda _line: None):
            self.assertEqual(len(waits), 1, "FIRST_TICKET_RESPECTS_RUNNING_LEASE")

    def test_wait_does_not_expire_after_1200_seconds(self) -> None:
        self.assertTrue(self.peer.try_acquire("long-holder", 18))
        now = [0.0]
        waits: list[float] = []

        def advance(_seconds: float) -> None:
            now[0] += 601
            waits.append(now[0])
            if len(waits) == 3:
                self.peer.release("long-holder")

        with admission.admit(8, job_kind="full_battery", reference_workers=8, clock=lambda: now[0], sleep=advance, report=lambda _line: None):
            self.assertEqual(len(waits), 3, "KNOWN_CAPACITY_CANNOT_AGE_OUT")

    def test_priority_default(self) -> None:
        with patch.dict(os.environ), patch.object(sys, "argv", ["run_smokes"]):
            os.environ.pop("RUN_SMOKES_PRIORITY", None)
            args = run_smokes._parse_args()
        self.assertEqual(getattr(args, "priority", None), "normal", "RUN_SMOKES_PRIORITY_DEFAULT")

    def test_proof_runner_without_register_uses_landing_demand(self) -> None:
        args = argparse.Namespace(jobs=8, priority="normal")
        expected: tuple[list[str], list[str], list[str], list[str], float] = ([], [], [], [], 0.0)
        with (
            patch.object(admission, "admit", return_value=nullcontext()) as admit,
            patch.object(run_smokes, "_run_admitted_suite", return_value=expected),
        ):
            self.assertEqual(run_smokes._run_suite_with_environment([], args, frozenset()), expected)
        admit.assert_called_once_with(8, "normal", job_kind="landing_battery", reference_workers=run_smokes._DEFAULT_JOBS)

    def test_priority_environment_and_cli(self) -> None:
        with patch.dict(os.environ, {"RUN_SMOKES_PRIORITY": "critical"}):
            with patch.object(sys, "argv", ["run_smokes"]):
                self.assertEqual(run_smokes._parse_args().priority, "critical")
            with patch.object(sys, "argv", ["run_smokes", "--priority", "normal"]):
                self.assertEqual(run_smokes._parse_args().priority, "normal")

    def test_overload_waits_with_quoted_reason_and_refreshes(self) -> None:
        self.assertTrue(self.peer.try_acquire("overload", 22))
        messages: list[str] = []
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            self.assertEqual(len([row for row in self._rows() if row["state"] == "waiting"]), 1)
            self.peer.release("overload")

        with admission.admit(4, sleep=sleep, report=messages.append) as ticket:
            self.assertEqual(ticket.state if ticket else None, "running")
        waiting = [line for line in messages if ": waiting " in line]
        self.assertEqual(len(waiting), 1)
        self.assertIn("cpu_slots running=22 candidate=4 capacity=24", waiting[0])
        self.assertIn("priority normal; ticket ", waiting[0])
        self.assertIn("waiting since ", waiting[0])
        self.assertTrue(waiting[0].endswith("'"), "QUOTED_WAIT_REASON")
        self.assertEqual(len(sleeps), 1)
        self.assertEqual(self._rows(), [])

    def test_under_capacity_starts_without_sleep(self) -> None:
        def no_sleep(_seconds: float) -> None:
            self.fail("UNDER_CAPACITY_MUST_START_IMMEDIATELY")

        with admission.admit(4, sleep=no_sleep, report=lambda _line: None) as ticket:
            self.assertIsNotNone(ticket)
            rows = self._rows()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["state"], "running")
            self.assertEqual(rows[0]["demand"], 4)
        self.assertEqual(self._rows(), [], "RELEASE_ON_SUCCESS")

    def test_priority_precedes_older_normal_and_fifo_within_priority(self) -> None:
        self._seed(admission.Ticket("critical-head", os.getpid(), 2, "critical", 0, "fixture"))
        waits: list[float] = []

        def release_head(seconds: float) -> None:
            waits.append(seconds)
            (self.directory / "critical-head.json").unlink()

        with admission.admit(2, "normal", sleep=release_head, report=lambda _line: None):
            self.assertEqual(len(waits), 1, "HIGH_PRIORITY_AHEAD")

    def test_no_running_head_can_start_under_pressure(self) -> None:
        with patch("os.getloadavg", return_value=(80.0, 80.0, 80.0)), admission.admit(4, report=lambda _line: None) as ticket:
            self.assertIsNotNone(ticket)
            self.assertEqual(ticket.state if ticket else None, "running")
            self.assertIn("ADVISORY host load", ticket.reason if ticket else "")

    def test_reservation_covers_load_average_startup_lag(self) -> None:
        self.assertTrue(self.peer.try_acquire("reserved", 22))
        try:
            observed = host_compute_budget.try_acquire("full_battery", 4, token="candidate")
            self.assertFalse(observed.admitted, "RUNNING_LEASE_PREVENTS_STARTUP_OVERSUBSCRIPTION")
            self.assertIn("running=22", observed.reason)
            host_compute_budget.release(observed)
        finally:
            self.peer.release("reserved")


class TicketLifecycleSmoke(_AdmissionFixture):
    def test_proof_priority_reaches_real_file_read_and_restores_environment(self) -> None:
        request = self.root / "invalid-proof.json"
        request.write_text("{}")
        observed: list[str | None] = []

        class ObservedRequest:
            def read_bytes(self) -> bytes:
                observed.append(os.environ.get(admission.PRIORITY_ENV))
                return request.read_bytes()

        args = argparse.Namespace(proof_request=ObservedRequest(), proof_output=None, priority="critical")
        for previous in (None, "normal"):
            with patch.dict(os.environ), redirect_stderr(io.StringIO()):
                if previous is None:
                    os.environ.pop(admission.PRIORITY_ENV, None)
                else:
                    os.environ[admission.PRIORITY_ENV] = previous
                with self.assertRaises((ValueError, KeyError)):
                    run_smokes._run_proof_request(args)
                self.assertEqual(os.environ.get(admission.PRIORITY_ENV), previous, "PRIORITY_ENV_REVERSED_ON_PROOF_ERROR")
        self.assertEqual(observed, ["critical", "critical"], "CLI_PRIORITY_REACHES_PROOF_REQUEST_EXECUTION")

    def test_dead_pid_swept_without_removing_live_ticket(self) -> None:
        self._holder()
        code = "import os, sys\nfrom pathlib import Path\nfrom quality_gates import battery_admission as a\nroot = Path(sys.argv[1])\nwith a._locked(root):\n    a._publish(root, a.Ticket('dead', os.getpid(), 4, 'normal', 0, 'fixture', 'running'))\n"
        subprocess.run([sys.executable, "-c", code, str(self.directory)], check=True)
        self.assertTrue((self.directory / "dead.json").is_file(), "REAL_EXITED_PID_FIXTURE_EXISTS")
        messages: list[str] = []
        with admission.admit(2, report=messages.append):
            self.assertFalse((self.directory / "dead.json").exists(), "DEAD_PID_SWEPT")
            self.assertTrue((self.directory / "holder.json").exists(), "LIVE_PID_RETAINED")
        self.assertTrue(any("swept dead pid" in line for line in messages))

    def test_known_capacity_wait_does_not_age_out(self) -> None:
        self.assertTrue(self.peer.try_acquire("age-holder", 22))
        now = [0.0]
        waits: list[float] = []

        def sleep(_seconds: float) -> None:
            now[0] += 601
            waits.append(now[0])
            if len(waits) == 3:
                self.peer.release("age-holder")

        with admission.admit(4, "normal", clock=lambda: now[0], sleep=sleep, report=lambda _line: None) as ticket:
            self.assertEqual(ticket.state if ticket else None, "running")
        self.assertEqual(len(waits), 3, "CAPACITY_WAIT_HAS_NO_AGING_BYPASS")

    def test_two_processes_run_concurrently_under_capacity(self) -> None:
        code = "import json, sys\nfrom pathlib import Path\nfrom quality_gates import battery_admission as a\nroot = Path(sys.argv[1])\nwith a.admit(2, directory=root, report=lambda line: None):\n    print(json.dumps([json.loads(p.read_text()) for p in root.glob('*.json')]))\n"
        with admission.admit(2, report=lambda _line: None):
            environment = os.environ.copy()
            environment.pop(admission.INHERITED_ENV, None)
            completed = subprocess.run([sys.executable, "-c", code, str(self.directory)], env=environment, capture_output=True, text=True, check=True)
            rows = json.loads(completed.stdout)
            self.assertEqual(len(rows), 2, "TWO_SIMULTANEOUS_RUNNING_TICKETS")
            self.assertEqual({row["state"] for row in rows}, {"running"})
            self.assertEqual(len({row["pid"] for row in rows}), 2, "DISTINCT_LIVE_PROCESSES")
        self.assertEqual(self._rows(), [])

    def test_nested_runner_reuses_and_preserves_parent_ticket(self) -> None:
        with admission.admit(4, report=lambda _line: None) as parent:
            with admission.admit(4, report=lambda _line: None) as child:
                self.assertEqual(child, parent, "NESTED_RUNNER_CANNOT_QUEUE_BEHIND_ITS_PARENT")
            self.assertEqual(len(self._rows()), 1, "CHILD_MUST_NOT_RELEASE_PARENT")
        self.assertEqual(self._rows(), [])

    def test_unknown_measurement_and_unavailable_storage_advise(self) -> None:
        messages: list[str] = []
        (self.peer.root / "settings.json").unlink()
        with admission.admit(4, report=messages.append) as ticket:
            self.assertIn("capacity_unknown", ticket.reason if ticket else "")
        bad_directory = self.root / "file"
        bad_directory.write_text("fixture")
        with admission.admit(4, directory=bad_directory, report=messages.append) as ticket:
            self.assertIsNone(ticket, "UNAVAILABLE_STORAGE_MUST_NOT_REFUSE")
        self.assertTrue(any("ADVISORY" in line and "proceeding" in line for line in messages))

    def test_malformed_ticket_preserved_and_advised(self) -> None:
        self.directory.mkdir()
        malformed = self.directory / "unknown.json"
        malformed.write_text("{}")
        messages: list[str] = []
        with admission.admit(4, report=messages.append):
            self.assertEqual(malformed.read_text(), "{}", "UNKNOWN_TICKET_HAS_NO_CLEANUP_AUTHORITY")
        self.assertEqual(sum("unreadable ticket" in line for line in messages), 1)

    def test_exception_and_interruption_release_waiting_and_running_tickets(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "fixture"):
            with admission.admit(4, report=lambda _line: None):
                raise RuntimeError("fixture")
        self.assertEqual(self._rows(), [], "RELEASE_ON_BODY_EXCEPTION")
        self.assertTrue(self.peer.try_acquire("interrupt-holder", 22))

        def interrupt(_seconds: float) -> None:
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            with admission.admit(4, sleep=interrupt, report=lambda _line: None):
                self.fail("OVERLOAD_CANNOT_START")
        self.assertEqual(self._rows(), [], "RELEASE_ON_WAIT_INTERRUPTION")
        self.peer.release("interrupt-holder")

    def test_invalid_environment_priority_advises_and_directory_is_per_user(self) -> None:
        diagnostics = io.StringIO()
        with patch.dict(os.environ, {"RUN_SMOKES_PRIORITY": "unknown"}), redirect_stderr(diagnostics):
            self.assertEqual(admission.priority_value(), "normal")
        self.assertIn("ADVISORY", diagnostics.getvalue())
        with patch.dict(os.environ):
            os.environ.pop(admission.DIRECTORY_ENV, None)
            self.assertEqual(admission.ticket_directory(), Path.home() / ".ananta" / "locks" / "run_smokes_admission")

    def test_real_runner_publishes_cli_demand_and_priority(self) -> None:
        register = self.root / "register.txt"
        register.write_text(_SELF + "\n")
        observation = self.root / "observation.json"
        code = "from quality_gates import run_smokes as r; r.battery_admission._capacity = lambda: (8, 0); raise SystemExit(r.main())"
        environment = {**os.environ, "SOLET_NAME": "admission-fixture", "RUN_SMOKES_NESTED_SELF_TEST": "1", "RUN_SMOKES_ADMISSION_FIXTURE": str(observation)}
        completed = subprocess.run([sys.executable, "-c", code, "--register", str(register), "--jobs", "2", "--priority", "critical"], env=environment, capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertTrue(observation.is_file(), "REAL_CHILD_OBSERVED_ADMISSION")
        row = json.loads(observation.read_text())
        self.assertTrue(row, "NONEMPTY_RUNNING_TICKET")
        self.assertEqual((row["state"], row["demand"], row["priority"]), ("running", 2, "critical"))
        self.assertEqual(self._rows(), [], "REAL_RUNNER_RELEASED_TICKET")


class SerialOrderingSmoke(_AdmissionFixture):
    def test_distinct_lock_resources_do_not_share_priority_queue(self) -> None:
        other = admission.Ticket("other-resource", os.getpid(), 1, "critical", 0, "fixture", kind="serial", resource="other.lock")
        self._seed(other)
        calls: list[str] = []

        def attempt() -> bool:
            calls.append("acquired")
            return True

        admission.wait_for_serial_lock(attempt, resource="this.lock", report=lambda _line: None)
        self.assertEqual(calls, ["acquired"], "DISTINCT_PHYSICAL_LOCKS_CANNOT_BLOCK_EACH_OTHER")
        self.assertEqual([row["name"] for row in self._rows()], ["other-resource"], "OTHER_RESOURCE_TICKET_PRESERVED")

    def test_serial_priority_and_waiting_ticket_lifecycle(self) -> None:
        head = admission.Ticket("priority-head", os.getpid(), 1, "critical", 0, "fixture", kind="serial")
        self._seed(head)
        calls: list[str] = []
        messages: list[str] = []

        def attempt() -> bool:
            calls.append("attempt")
            return True

        def sleep(_seconds: float) -> None:
            self.assertEqual(calls, [], "LOWER_PRIORITY_CANNOT_TRY_PHYSICAL_LOCK")
            rows = self._rows()
            self.assertEqual(len(rows), 2, "SERIAL_WAITING_TICKET_VISIBLE")
            (self.directory / "priority-head.json").unlink()

        admission.wait_for_serial_lock(attempt, clock=lambda: 1, sleep=sleep, report=messages.append)
        self.assertEqual(calls, ["attempt"])
        self.assertEqual(self._rows(), [], "SERIAL_TICKET_CONSUMED_ON_PHYSICAL_ACQUISITION")
        self.assertTrue(any("waiting 'serial slot:" in line and "priority ticket" in line for line in messages))

    def test_serial_aging_bypasses_priority_but_preserves_physical_lock(self) -> None:
        head = admission.Ticket("critical", os.getpid(), 1, "critical", 0, "fixture", kind="serial")
        waiter = replace(head, name="old-normal", priority="normal")
        self._seed(head)
        self._seed(waiter)
        acquired, reason = admission._serial_attempt(self.directory, waiter, lambda: False, 1200, lambda _line: None)
        self.assertFalse(acquired, "AGING_CANNOT_BREAK_PHYSICAL_MUTEX")
        self.assertIn("ADVISORY aging limit 1200s reached", reason)
        acquired, _reason = admission._serial_attempt(self.directory, waiter, lambda: True, 1200, lambda _line: None)
        self.assertTrue(acquired, "AGED_TICKET_CAN_TAKE_NEXT_FREE_SLOT")

    def _wait_for(self, predicate: Callable[[], bool], label: str) -> None:
        deadline = time.monotonic() + 10
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail(label)
            time.sleep(0.01)

    def _waiting_priority(self, priority: str) -> bool:
        return any(row["kind"] == "serial" and row["priority"] == priority for row in self._rows())

    def test_real_serial_phases_interleave_and_priority_takes_next_slot(self) -> None:
        fixture = self.root / "fixture"
        (fixture / "ananta" / "src").mkdir(parents=True)
        events = self.root / "events.jsonl"
        release = self.root / "release-a"
        stub = (
            "import json, time\nfrom pathlib import Path\n"
            "events = Path({events!r})\nname = {name!r}\n"
            "with events.open('a') as f:\n    f.write(json.dumps([name, 'start', time.monotonic()]) + '\\n')\n"
            "if name == 'a':\n"
            "    while not Path({release!r}).exists():\n        time.sleep(0.01)\n"
            "time.sleep(0.03)\n"
            "with events.open('a') as f:\n    f.write(json.dumps([name, 'end', time.monotonic()]) + '\\n')\n"
        )
        for name in "abcde":
            (fixture / f"{name}.py").write_text(stub.format(events=str(events), name=name, release=str(release)))
        code = (
            "import json, sys\nfrom pathlib import Path\nfrom quality_gates import run_smokes as r\n"
            "r._REPO_ROOT = Path(sys.argv[1])\n"
            "supervisor = r._SmokeProcessSupervisor(r._MAX_SMOKE_STARTS_PER_WINDOW, r._SMOKE_START_WINDOW_SECONDS)\n"
            "results, waited = r._run_serial_phase(Path(sys.executable), sys.argv[2:], supervisor)\n"
            "print(json.dumps({'results': results, 'waited': waited}))\n"
        )
        processes: list[subprocess.Popen[str]] = []

        def launch(priority: str, entries: list[str]) -> None:
            env = {**os.environ, "SOLET_NAME": "admission-fixture", "RUN_SMOKES_PRIORITY": priority, "RUN_SMOKES_HOST_SERIAL_LOCK_PATH": str(self.root / "host.lock")}
            processes.append(subprocess.Popen([sys.executable, "-c", code, str(fixture), *entries], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))

        try:
            launch("normal", ["a.py", "b.py"])
            self._wait_for(events.exists, "FIRST_SERIAL_SMOKE_DID_NOT_START")
            launch("normal", ["c.py"])
            self._wait_for(lambda: self._waiting_priority("normal"), "OLDER_NORMAL_TICKET_NOT_VISIBLE")
            launch("critical", ["d.py", "e.py"])
            self._wait_for(lambda: self._waiting_priority("critical"), "PRIORITY_TICKET_NOT_VISIBLE")
            release.touch()
            for process in processes:
                stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, stdout + stderr)
                result = json.loads(stdout)
                self.assertTrue(result["results"], "REAL_SERIAL_RESULTS_NONEMPTY")
                self.assertTrue(all(row[0] == "passed" for row in result["results"].values()), result)
        finally:
            release.touch()
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)
        rows = [json.loads(line) for line in events.read_text().splitlines()]
        self.assertEqual(len(rows), 10, "ALL_FIVE_REAL_SMOKES_STARTED_AND_FINISHED")
        starts = [name for name, event, _when in rows if event == "start"]
        self.assertEqual(starts[:2], ["a", "d"], "HIGHER_PRIORITY_TAKES_NEXT_SLOT_AHEAD_OF_OLDER_NORMAL")
        self.assertLess(starts.index("d"), starts.index("b"), "PHASES_INTERLEAVE_BETWEEN_SMOKES")
        for (first, event, ended), (second, next_event, started) in zip(rows[1::2], rows[2::2], strict=False):
            self.assertEqual((event, next_event), ("end", "start"), f"SERIAL_SMOKES_OVERLAP: {first}/{second}")
            self.assertGreaterEqual(started, ended, "PHYSICAL_SERIAL_MUTEX_PRESERVED")
        self.assertEqual(self._rows(), [], "ALL_SERIAL_WAITING_TICKETS_RETIRED")


def load_tests(loader: unittest.TestLoader, tests: unittest.TestSuite, _pattern: str | None) -> unittest.TestSuite:
    """Run the host protocol cases through the registered admission smoke."""
    tests.addTests(loader.loadTestsFromModule(host_compute_budget_smoke))
    return tests


if __name__ == "__main__":
    if target := os.environ.get("RUN_SMOKES_ADMISSION_FIXTURE"):
        Path(target).write_text(Path(os.environ[admission.INHERITED_ENV]).read_text())
    elif os.environ.get("RUN_SMOKES_NESTED_SELF_TEST") == "1":
        raise SystemExit("ADMISSION_FIXTURE_MARKER_MUST_BE_FORWARDED")
    else:
        unittest.main()
