"""Real subprocess support cases for the owned smoke collector.

Run directly for focused proof, or incorporate run_cases into the dependent
registered containment smoke. This support file adds no gate registration.
Every process is a short hermetic fixture; explicit cleanup touches only its
new session and the detached writer it recorded in its own private directory.
There is no test-battery timeout. Small sleeps pace fixture output/observations,
and never stand in for an execution deadline in the collector.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from quality_gates import smoke_process  # noqa: E402
from quality_gates.smoke_process import (  # noqa: E402
    Attention,
    CleanupReason,
    CollectorConfig,
    OwnedProcess,
    collect_owned_process,
)

type Check = Callable[[object, str], None]

_CONFIG = CollectorConfig(attention_seconds=0.025, poll_seconds=0.005, tail_bytes=4096, read_bytes=4096)


def _signal_owned(target: int, *, group: bool = False) -> None:
    try:
        if group:
            os.killpg(target, signal.SIGKILL)
        else:
            os.kill(target, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _live(pid: int) -> bool:
    """A dead, unreaped orphan is not a running pipe writer."""
    result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False)
    status = result.stdout.strip()
    return bool(status) and not status.startswith("Z")


@dataclass
class _Owner:
    process: OwnedProcess
    child_record: Path | None = None
    reasons: list[CleanupReason] = field(default_factory=list)
    finalizations: list[bool] = field(default_factory=list)

    def child_pid(self) -> int | None:
        if self.child_record is None or not self.child_record.exists():
            return None
        return int(self.child_record.read_text(encoding="utf-8"))

    def cleanup(self, reason: CleanupReason) -> None:
        self.reasons.append(reason)
        _signal_owned(self.process.pid, group=True)
        child = self.child_pid()
        if child is not None:
            _signal_owned(child)

    def finalized(self) -> None:
        pipes = (self.process.stdout, self.process.stderr)
        self.finalizations.append(self.process.returncode is not None and all(pipe is None or pipe.closed for pipe in pipes))

    def emergency_cleanup(self) -> None:
        """Fixture owner teardown is explicit, including on a failed check."""
        if self.process.poll() is None:
            _signal_owned(self.process.pid, group=True)
        child = self.child_pid()
        if child is not None and _live(child):
            _signal_owned(child)
        self.process.wait()
        for pipe in (self.process.stdout, self.process.stderr):
            if pipe is not None:
                pipe.close()


@contextmanager
def _owned(code: str, child_record: Path | None = None, *, text: bool = False) -> Iterator[_Owner]:
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        start_new_session=True,
    )
    owner = _Owner(process, child_record)
    try:
        yield owner
    finally:
        owner.emergency_cleanup()


def _success_and_attention(check: Check) -> None:
    with tempfile.TemporaryDirectory(prefix="smoke-attention-") as temporary:
        _attention_until_released(check, Path(temporary) / "release")


def _attention_until_released(check: Check, release: Path) -> None:
    code = f"import os,time; from pathlib import Path\nos.write(1,b'first\\n')\nwhile not Path({str(release)!r}).exists(): time.sleep(0.001)\nos.write(1,b'last\\n'); os.write(2,b'error stream\\n')"
    events: list[Attention] = []
    with _owned(code, text=True) as owner:

        def observe(event: Attention) -> None:
            events.append(event)
            if len(events) <= 3:
                check(owner.process.poll() is None, "attention observes a still-running root")
            check(not owner.reasons, "repeated attention never invokes cleanup")
            if len(events) == 3:
                release.write_text("attention observed", encoding="utf-8")

        result = collect_owned_process(owner.process, cleanup_owned=owner.cleanup, on_reaped=owner.finalized, config=_CONFIG, on_attention=observe)
        check(len(events) >= 3, "multiple attention intervals precede eventual success")
        check(result.returncode == 0 and not result.cancelled, "eventual success retains real return code")
        check(bool(result.stdout.tail) and result.stdout.tail == b"first\nlast\n", "stdout is exact with no duplicate bytes")
        check(bool(result.stderr.tail) and result.stderr.tail == b"error stream\n", "stderr is exact and separate")
        check(result.stdout.total_bytes == 11 and not result.stdout.truncated, "untruncated stdout count is exact")
        check(all(event.tests_done is None for event in events), "unknown tests-done facts stay unknown")
        check(all(event.elapsed_seconds >= event.since_output_seconds >= 0 for event in events), "attention elapsed/quiet durations are truthful")
        check(all(event.host_load is None or len(event.host_load) == 3 for event in events), "host load is available data or explicitly unknown")
        check(owner.reasons == [CleanupReason.ROOT_EXIT] and owner.finalizations == [True], "natural exit cleanup precedes closed-pipe finalization exactly once")


def _nonzero_and_closed_pipes(check: Check) -> None:
    for code in (7, 77):
        with _owned(f"import os; os.write(1,b'nonzero'); os.write(2,b'detail'); raise SystemExit({code})") as owner:
            result = collect_owned_process(owner.process, cleanup_owned=owner.cleanup, on_reaped=owner.finalized, config=_CONFIG)
            check(result.returncode == code and result.returncode == owner.process.returncode, f"real nonzero {code} is retained without verdict classification")
            check(not result.cancelled and result.stdout.tail == b"nonzero" and result.stderr.tail == b"detail", f"nonzero {code} retains output")
    with _owned("import os,time; os.close(1); os.close(2); time.sleep(0.08); raise SystemExit(3)") as owner:
        result = collect_owned_process(owner.process, cleanup_owned=owner.cleanup, on_reaped=owner.finalized, config=_CONFIG)
        check(result.returncode == 3 and result.stdout.total_bytes == result.stderr.total_bytes == 0, "EOF before root exit still waits for the actual root result")


def _large_streams(check: Check) -> None:
    code = "import os,threading\ndef emit(fd,data):\n while data:\n  data=data[os.write(fd,data):]\na=threading.Thread(target=emit,args=(1,bytes(range(256))*4096+b'OUT-END'))\nb=threading.Thread(target=emit,args=(2,bytes(reversed(range(256)))*4096+b'ERR-END'))\na.start(); b.start(); a.join(); b.join()\n"
    config = CollectorConfig(attention_seconds=1, poll_seconds=0.005, tail_bytes=257, read_bytes=4096)
    with _owned(code, text=True) as owner:
        result = collect_owned_process(owner.process, cleanup_owned=owner.cleanup, on_reaped=owner.finalized, config=config)
        expected = (bytes(range(256)) * 4096 + b"OUT-END", bytes(reversed(range(256))) * 4096 + b"ERR-END")
        check(result.returncode == 0, "simultaneous large stdout/stderr complete without deadlock")
        for name, output, full in (("stdout", result.stdout, expected[0]), ("stderr", result.stderr, expected[1])):
            check(output.total_bytes == len(full) and output.total_bytes > 1_000_000, f"{name} retains true byte count")
            check(output.truncated and len(output.tail) == config.tail_bytes, f"{name} explicitly reports bounded truncation")
            check(bool(output.tail) and output.tail == full[-config.tail_bytes :], f"{name} exact binary tail has no decoding/duplicate loss")
    with _owned("import os; os.write(1,b'discarded'); os.write(2,b'also discarded')") as owner:
        result = collect_owned_process(owner.process, cleanup_owned=owner.cleanup, on_reaped=owner.finalized, config=CollectorConfig(tail_bytes=0))
        check(result.stdout.total_bytes == 9 and result.stderr.total_bytes == 14 and result.stdout.tail == result.stderr.tail == b"", "zero-byte retention still counts both streams")
        check(result.stdout.truncated and result.stderr.truncated, "zero-byte retention discloses discarded output")


def _writer_source(record: Path, *, root_exits: bool) -> str:
    child = f"import os,time; from pathlib import Path; os.write(1,b'child-ready\\n'); Path({str(record)!r}).write_text(str(os.getpid()),encoding='utf-8'); time.sleep(2)"
    return f"import subprocess,sys,time; from pathlib import Path\nsubprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True)\nwhile not Path({str(record)!r}).exists(): time.sleep(0.001)\nprint('root-ready',flush=True)\n" + ("" if root_exits else "time.sleep(2)\n")


def _owned_descendants(check: Check, *, cancel: bool) -> None:
    with tempfile.TemporaryDirectory(prefix="smoke-collector-") as temporary, _owned("import time; time.sleep(2)") as foreign:
        record = Path(temporary) / "owned-child.pid"
        events: list[Attention] = []
        with _owned(_writer_source(record, root_exits=not cancel), record) as owner:

            def cancelled() -> bool:
                return _ready_to_cancel(cancel, events, owner)

            def observe(event: Attention) -> None:
                events.append(event)

            result = collect_owned_process(owner.process, cleanup_owned=owner.cleanup, on_reaped=owner.finalized, config=_CONFIG, on_attention=observe, cancellation_requested=cancelled, tests_done=lambda: 4)
            _check_child_cleanup(check, owner, foreign)
            check(b"child-ready\n" in result.stdout.tail and b"root-ready\n" in result.stdout.tail, "descendant-held descriptors drain their exact final bytes")
            expected_reason = CleanupReason.CANCELLED if cancel else CleanupReason.ROOT_EXIT
            check(owner.reasons == [expected_reason] and owner.finalizations == [True], "owned cleanup and finalization run once in terminal order")
            check(result.cancelled is cancel and result.returncode == owner.process.returncode, "cancellation fact and real OS exit code remain distinct")
            if cancel:
                check(result.returncode == -signal.SIGKILL and len(events) >= 3, "only explicit cancellation stops the root after repeated attention")
                check(all(event.tests_done == 4 for event in events), "attention uses caller-supplied tests-done facts")
            else:
                check(result.returncode == 0 and result.elapsed_seconds < 1.5, "natural root exit releases inherited pipes before descendant's own exit")


def _ready_to_cancel(cancel: bool, events: list[Attention], owner: _Owner) -> bool:
    return cancel and len(events) >= 3 and events[-1].stdout_bytes >= len(b"child-ready\nroot-ready\n") and owner.child_pid() is not None


def _check_child_cleanup(check: Check, owner: _Owner, foreign: _Owner) -> None:
    child = owner.child_pid()
    check(child is not None, "real detached descendant recorded its owned identity")
    if child is not None:
        check(not _live(child), "owner cleanup stopped the detached inherited-pipe writer")
    check(foreign.process.poll() is None and not foreign.reasons, "cleanup leaves unrelated session alive")


def _callback_errors(check: Check) -> None:
    with _owned("import time; time.sleep(2)") as owner:
        failure = RuntimeError("attention fixture error")

        def fail_attention(_event: Attention) -> None:
            raise failure

        try:
            collect_owned_process(owner.process, cleanup_owned=owner.cleanup, on_reaped=owner.finalized, config=_CONFIG, on_attention=fail_attention)
        except RuntimeError as exc:
            check(exc is failure, "attention exception is preserved verbatim")
        else:
            check(False, "attention exception must propagate")
        check(owner.reasons == [CleanupReason.ERROR] and owner.finalizations == [True], "callback error cleans and reaps before finalization")
    with _owned("import time; time.sleep(2)") as owner:

        def cleanup(reason: CleanupReason) -> None:
            owner.cleanup(reason)
            owner.process.wait()
            raise ValueError("cleanup fixture error")

        def finalized() -> None:
            owner.finalized()
            raise OSError("finalization fixture error")

        try:
            collect_owned_process(owner.process, cleanup_owned=cleanup, on_reaped=finalized, config=_CONFIG, on_attention=fail_attention)
        except BaseExceptionGroup as exc:
            check([str(error) for error in exc.exceptions] == [str(failure), "cleanup fixture error", "finalization fixture error"], "all primary/cleanup/finalization failures survive together")
        else:
            check(False, "multiple callback errors must propagate together")
        check(owner.reasons == [CleanupReason.ERROR] and owner.finalizations == [True], "raising cleanup is attempted once without hiding finalization")


def _failed_cleanup_retains_ownership(check: Check) -> None:
    with _owned("import time; time.sleep(2)") as owner:
        failure = OSError("owner cannot clean")

        def fail_cleanup(reason: CleanupReason) -> None:
            owner.reasons.append(reason)
            raise failure

        try:
            collect_owned_process(owner.process, cleanup_owned=fail_cleanup, on_reaped=owner.finalized, config=_CONFIG, cancellation_requested=lambda: True)
        except OSError as exc:
            check(exc is failure, "failed owner cleanup is surfaced without timer fallback")
        else:
            check(False, "failed cleanup must propagate")
        check(owner.process.poll() is None and not owner.finalizations, "failed cleanup leaves live-root registration with the owner")
        check(owner.reasons == [CleanupReason.CANCELLED], "failed cleanup is not repeated under a different reason")


def _configuration(check: Check) -> None:
    defaults = CollectorConfig.from_environment({})
    check(defaults == CollectorConfig(), "documented defaults load through the real configuration path")
    configured = CollectorConfig.from_environment({"SMOKE_PROCESS_ATTENTION_SECONDS": "0.2", "SMOKE_PROCESS_POLL_SECONDS": "0.01", "SMOKE_PROCESS_TAIL_BYTES": "0", "SMOKE_PROCESS_READ_BYTES": "128"})
    check(configured == CollectorConfig(0.2, 0.01, 0, 128), "all documented environment knobs are actually consumed")
    invalid = (
        ("SMOKE_PROCESS_ATTENTION_SECONDS", "nan"),
        ("SMOKE_PROCESS_ATTENTION_SECONDS", "inf"),
        ("SMOKE_PROCESS_POLL_SECONDS", "0"),
        ("SMOKE_PROCESS_TAIL_BYTES", "-1"),
        ("SMOKE_PROCESS_READ_BYTES", "0"),
        ("SMOKE_PROCESS_READ_BYTES", "1.5"),
    )
    for key, value in invalid:
        try:
            CollectorConfig.from_environment({key: value})
        except ValueError:
            check(True, f"malformed {key}={value} is visible")
        else:
            check(False, f"malformed {key}={value} must fail")


def run_cases(check: Check) -> None:
    """Exercise the candidate module; callable from the integration smoke."""
    check(Path(smoke_process.__file__).resolve() == _REPO_ROOT / "quality_gates" / "smoke_process.py", "test origin is this candidate's collector module")
    _configuration(check)
    _success_and_attention(check)
    _nonzero_and_closed_pipes(check)
    _large_streams(check)
    _owned_descendants(check, cancel=True)
    _owned_descendants(check, cancel=False)
    _callback_errors(check)
    _failed_cleanup_retains_ownership(check)


def main() -> int:
    """House check harness stays active under Python -O."""
    passed = 0
    failures: list[str] = []

    def check(condition: object, label: str) -> None:
        nonlocal passed
        if condition:
            passed += 1
            print(f"PASS {label}", flush=True)
        else:
            failures.append(label)
            print(f"FAIL {label}", flush=True)

    run_cases(check)
    print(f"{passed} passed, {len(failures)} failed", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
