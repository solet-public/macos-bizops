"""Real runner fixtures with private short HOME/.at leaves and measured children.

No production scratch parser or cleanup algorithm is reused as a test oracle.
Only namespaces and process groups created by this fixture are cleaned here.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import cast
from unittest.mock import patch

from ananta.work_folders import WorkFolder, process_binding, register_work_folder

__all__ = ["Fixture", "_group_alive", "_scratch_exception_and_advisory", "_wait_for_groups_exit", "interrupt_cleanup", "stale_scratch"]

_REPO = Path(__file__).resolve().parents[2]
_STUB = """import json, os, signal, stat, subprocess, sys, tempfile, time
from pathlib import Path
home = Path(HOME_VALUE)
root = os.environ.get('TMPDIR')
artifact = Path(root or str(home / 'unowned')) / 'artifact'
artifact.parent.mkdir(parents=True, exist_ok=True)
artifact.write_text('scratch')
child = None
if INTERRUPT_VALUE:
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    def finish(number, frame):
        child.wait(timeout=5)
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, finish)
owner = json.loads((Path(root) / '.owner.json').read_text()) if root else None
record = dict(TMPDIR=root, TEMP=os.environ.get('TEMP'), TMP=os.environ.get('TMP'),
              actual=tempfile.gettempdir(), artifact=str(artifact), owner=owner,
              mode=stat.S_IMODE(Path(root).stat().st_mode) if root else None,
              pid=os.getpid(), pgid=os.getpgrp(), runner=os.getppid(),
              descendant=child.pid if child else None,
              descendant_pgid=os.getpgid(child.pid) if child else None)
(home / 'ready.tmp').write_text(json.dumps(record))
(home / 'ready.tmp').rename(home / 'observed.json')
if child:
    time.sleep(60)
"""
_LAUNCH = """import json, signal, subprocess, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, 'quality_gates')
import run_smokes as r
import run_smokes_scratch as h
home, parent, case, serial = json.loads(sys.argv[1])
home = Path(home)
if case in ('long', 'platform', 'exception'):
    h._parent = lambda: home / '.ananta' / 's'
else:
    h._parent = lambda: Path(parent)
if case == 'long':
    h._short_parents = lambda: ()
r._HOST_SERIAL_LOCK_PATH = home / 'fixture.lock'
original = r._run_suite_with_environment
if case == 'exception':
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('SCRATCH_EXCEPTION_FIXTURE')
    r._run_suite_with_environment = fail
saved = [signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)]
sys.argv = ['run_smokes', '--register', str(home / 'register'), '--jobs', '2']
if serial:
    sys.argv += ['--serial-only-override', str(home / 'child.py')]
with patch.object(h.tempfile, 'mkdtemp', side_effect=OSError('fixture allocation')) if case == 'allocation' else patch.object(h, '_parent', h._parent):
    with patch.object(h.shutil, 'rmtree', side_effect=OSError('fixture removal')) if case == 'removal' else patch.object(h, '_identity', h._identity):
        try:
            status = r.main()
        except RuntimeError as exc:
            if case != 'exception' or str(exc) != 'SCRATCH_EXCEPTION_FIXTURE':
                raise
            status = 0
if case == 'race':
    owner = r._SmokeProcessSupervisor(100, 1)
    with h.BatteryRuntime(owner, r._terminate_process_group) as runtime:
        runtime.abort()
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        try:
            owner.register(process)
        except r.SmokeStartRateError:
            pass
        assert process.poll() is not None, 'LATE_REGISTER_REAPED'
        runtime._signal(signal.SIGINT, None)
        runtime._signal(signal.SIGTERM, None)
assert h._active is None, 'SCRATCH_ACTIVE_RESTORED'
assert saved == [signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)], 'SCRATCH_HANDLERS_RESTORED'
raise SystemExit(status)
"""


class Fixture:
    """Own disposable deep records and a separately short runtime-parent seam."""

    def __enter__(self) -> Fixture:
        ancestor = Path.home().resolve() / ".at"
        if len(os.fsencode(ancestor / "abcdefgh" / "b-abcdefgh")) > 40 or ancestor.is_relative_to("/private/tmp"):
            base = Path("/Users/Shared") if sys.platform == "darwin" else Path(tempfile.gettempdir())
            ancestor = base / f".t{os.getuid()}"
            print(f"fixture short scratch uses private platform parent {ancestor}", flush=True)
        ancestor.mkdir(mode=0o700, exist_ok=True)
        assert not ancestor.is_symlink() and ancestor.stat().st_uid == os.getuid(), "PRIVATE_FIXTURE_PARENT_OWNER"
        assert len(os.fsencode(ancestor / "abcdefgh" / "b-abcdefgh")) <= 40, "INVALID_SHORT_FIXTURE"
        self.short = tempfile.TemporaryDirectory(prefix="", dir=ancestor)
        self.parent = Path(self.short.name)
        self.short_folder: WorkFolder = register_work_folder(self.parent, purpose="smoke_scratch_fixture", bound=process_binding())
        self.deep = tempfile.TemporaryDirectory(prefix="runner-scratch-")
        self.home = Path(self.deep.name)
        self.process: subprocess.Popen[str] | None = None
        self.observed: dict[str, object] = {}
        return self

    def start(self, case: str = "normal", serial: bool = True) -> None:
        source = _STUB.replace("HOME_VALUE", repr(str(self.home))).replace("INTERRUPT_VALUE", repr(case == "interrupt"))
        (self.home / "child.py").write_text(source)
        (self.home / "register").write_text(str(self.home / "child.py") + "\n")
        env = {key: value for key, value in os.environ.items() if not key.startswith("AGENT_") and key != "HOMUNCULUS_NAME"}
        env.update(HOME=str(self.home), TMPDIR=str(self.home), TEMP=str(self.home), TMP=str(self.home))
        self.process = subprocess.Popen([str(_REPO / ".venv/bin/python3"), "-c", _LAUNCH, json.dumps([str(self.home), str(self.parent), case, serial])], cwd=_REPO, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def read(self) -> dict[str, object]:
        self.observed = json.loads((self.home / "observed.json").read_text())
        print(json.dumps(self.observed, sort_keys=True), flush=True)
        return self.observed

    def finish(self) -> tuple[str, str]:
        assert self.process is not None
        stdout, stderr = self.process.communicate(timeout=10)
        print(stdout, stderr, flush=True)
        assert self.process.returncode == 0, ("FIXTURE_RUNNER_FAILED", self.process.returncode)
        return stdout, stderr

    def __exit__(self, *_error: object) -> None:
        try:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                self.process.communicate(timeout=10)
            record = self.home / "observed.json"
            if record.exists():
                group = json.loads(record.read_text())["pgid"]
                try:
                    os.killpg(group, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        finally:
            self.deep.cleanup()
            self.short_folder.close()
            self.short.cleanup()


def _scratch_run(case: str = "normal") -> None:
    with Fixture() as fixture:
        fixture.start(case)
        _, stderr = fixture.finish()
        observed = fixture.read()
        root = observed["TMPDIR"]
        artifact = Path(str(observed["artifact"]))
        if case == "cleanup":
            assert not artifact.exists(), ("RUNNER_SCRATCH_REMOVED", observed)
        elif case == "exception":
            assert not artifact.exists(), ("RUNNER_SCRATCH_EXCEPTION_REMOVED", observed)
        elif case == "length":
            size = len(os.fsencode(Path(str(root)).resolve())) if root is not None else None
            print("resolved_root_bytes", size, root, flush=True)
            assert root is not None and size is not None and size <= 40, ("RUNNER_SCRATCH_ROOT_WITHIN40", observed)
        else:
            assert root is not None, ("RUNNER_TMPDIR_OWNED", observed)
        if case in ("platform", "exception"):
            expected_parent = _platform_parent(root, stderr)
        else:
            expected_parent = fixture.parent
        _owned_observation(observed, expected_parent)


def _platform_parent(root: object, stderr: str) -> Path:
    base = Path("/Users/Shared") if sys.platform == "darwin" else Path(tempfile.gettempdir())
    assert len(os.fsencode(Path(str(root)).resolve())) <= 40, ("PLATFORM_ROOT_WITHIN40", root)
    assert '"private": true' in stderr and "not_provisioned" not in stderr, stderr
    return (base / f".s{os.getuid()}").resolve()


def _owned_observation(observed: dict[str, object], parent: Path) -> None:
    root, artifact = observed["TMPDIR"], Path(str(observed["artifact"]))
    assert root == observed["TEMP"] == observed["TMP"] == observed["actual"], observed
    # The runner resolves its root; the verifier may spell HOME through /tmp.
    assert Path(str(root)).parent.resolve() == parent.resolve(), observed
    assert observed["mode"] == 0o700, ("PRIVATE_SCRATCH_LEAF", observed)
    owner = observed["owner"]
    assert isinstance(owner, dict), observed
    owner = cast(dict[str, object], owner)
    assert owner["schema"] == 1 and owner["pid"] == observed["runner"] and owner["uid"] == os.getuid(), observed
    started = owner["start_time_ns"]
    assert isinstance(started, int) and 0 < started <= time.time_ns(), observed
    assert not artifact.exists() and not Path(str(root)).exists(), observed


def _scratch_exception_and_advisory() -> None:
    _scratch_run("platform")
    _scratch_run("exception")
    with Fixture() as fixture:
        fixture.start("race")
        fixture.finish()
    for case in ("allocation", "removal", "long"):
        with Fixture() as fixture:
            fixture.start(case)
            _, stderr = fixture.finish()
            observed = fixture.read()
            assert stderr.count("ADVISORY run_smokes scratch") == 1, stderr
            if case == "removal":
                assert Path(str(observed["TMPDIR"])).exists(), observed
            elif case == "long":
                assert "no short private parent available" in stderr, stderr
                assert "not_provisioned" not in stderr, stderr
                _owned_observation(observed, fixture.home / ".ananta" / "s")
            else:
                assert observed["TMPDIR"] is observed["TEMP"] is observed["TMP"] is None, observed


def _group_alive(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Darwin reports EPERM for an unreaped, zombie-only group too.
        # Permission denial is never evidence that a group has disappeared.
        return True
    return True


def _wait_for_groups_exit(groups: tuple[int, ...], timeout: float = 2.0) -> None:
    """Require ESRCH after reaping; tolerate no live or inaccessible group."""
    deadline = time.monotonic() + timeout
    while any(_group_alive(group) for group in groups):
        assert time.monotonic() < deadline, "OWNED_PROCESS_GROUP_STILL_PRESENT_AFTER_REAP_WAIT"
        time.sleep(0.01)


def interrupt_cleanup(signum: int | None = signal.SIGINT, serial: bool | None = True) -> None:
    for number in (signal.SIGINT, signal.SIGTERM) if signum is None else (signum,):
        for mode in (True, False) if serial is None else (serial,):
            _interrupt_case(number, mode)


def _interrupt_case(number: int, mode: bool) -> None:
    with Fixture() as fixture:
        fixture.start("interrupt", mode)
        deadline = time.monotonic() + 5
        while not (fixture.home / "observed.json").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        observed = fixture.read()
        assert observed["pid"] == observed["pgid"] == observed["descendant_pgid"], observed
        assert fixture.process is not None
        fixture.process.send_signal(number)
        stdout, stderr = fixture.process.communicate(timeout=10)
        print(stdout, stderr, flush=True)
        group = observed["pgid"]
        assert isinstance(group, int), observed
        assert not _group_alive(group), "RUNNER_INTERRUPT_GROUP_REAPED"
        assert observed["TMPDIR"] is not None and not Path(str(observed["TMPDIR"])).exists(), "RUNNER_INTERRUPT_ROOT_REMOVED"
        assert fixture.process.returncode in (128 + number, -number), fixture.process.returncode


def stale_scratch() -> None:
    with Fixture() as fixture:
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        roots, decoy = _seed(fixture, dead.pid)
        for _entry in range(2):
            fixture.start()
            fixture.finish()
            assert not roots["stale"].exists(), "STALE_RUNNER_SCRATCH_SWEPT"
            assert roots["live"].exists(), "LIVE_RUNNER_SCRATCH_PRESERVED"
            assert all(roots[label].exists() for label in ("young", "malformed", "missing")), roots
            assert decoy.exists() and (fixture.parent / "b-xxxxxxxx").is_symlink()
        _pid_controls(dead.pid)


def _seed(fixture: Fixture, dead_pid: int) -> tuple[dict[str, Path], Path]:
    roots = {}
    for index, label in enumerate(("stale", "live", "young", "malformed", "missing")):
        root = fixture.parent / f"b-{index:08d}"
        root.mkdir(mode=0o700)
        roots[label] = root
        record = {"schema": 1, "uid": os.getuid(), "pid": os.getpid() if label == "live" else dead_pid, "start_time_ns": time.time_ns() - (0 if label == "young" else 172800 * 10**9)}
        if label != "missing":
            (root / ".owner.json").write_text("bad" if label == "malformed" else json.dumps(record))
    decoy = fixture.parent / "decoy"
    decoy.mkdir()
    (fixture.parent / "b-xxxxxxxx").symlink_to(decoy, target_is_directory=True)
    return roots, decoy


def _pid_controls(dead: int) -> None:
    sys.path.insert(0, str(_REPO / "quality_gates"))
    import contention_signal

    with patch.object(contention_signal.os, "kill", side_effect=PermissionError):
        assert contention_signal.holder_pid_is_alive(os.getpid())
    with patch.object(contention_signal.os, "kill", side_effect=ProcessLookupError):
        assert not contention_signal.holder_pid_is_alive(dead)
