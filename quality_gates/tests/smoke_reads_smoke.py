#!/usr/bin/env python3
"""Tier U: real-worker input observations and conservative coverage controls."""

# ruff: noqa: E402
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
from quality_gates.smoke_read_hooks import HookState, _file_call
from quality_gates.smoke_reads import READ_SCHEMA, ReadTrace, TraceRecorder, validate_certificate

_PROFILE = "a" * 64
_FOLDER_BRIDGE = """#!/usr/bin/env python3
import hashlib, json, os, shutil, sys
from pathlib import Path

verb, args = sys.argv[2].rsplit('::', 1)[1], json.loads(sys.argv[3])
records = Path(__file__).parent / 'work-folder-records'
records.mkdir(exist_ok=True)
if verb == 'read_work_folders' and args['mode'] == 'list':
    data = {'folders': [], 'next': None}
else:
    if verb == 'open_work_folder':
        path = Path(args['path'])
        info = path.lstat()
        identity = 'wkf_' + hashlib.sha256(os.fsencode(path)).hexdigest()
        row = dict(folder_id=identity, path=str(path), state='open', version=1,
                   dev=str(info.st_dev), ino=str(info.st_ino),
                   harvest_kind=args['harvest'])
        data = row
    else:
        identity = args['folder_id']
        row = json.loads((records / (identity + '.json')).read_text())
        if verb == 'read_work_folders':
            data = {'folders': [row]}
        elif verb == 'transition_work_folder':
            assert args['expected_version'] == row['version']
            row['version'] += 1
            if args['action'] == 'close':
                row['state'] = 'due'
            if args['action'] == 'correct':
                row['harvest_kind'] = args['harvest_kind']
            data = row
        elif verb == 'sweep_work_folders':
            path = Path(row['path'])
            info = path.lstat()
            assert row['state'] == 'due'
            assert (str(info.st_dev), str(info.st_ino)) == (row['dev'], row['ino'])
            shutil.rmtree(path)
            row['state'] = 'deleted'
            data = {'advisories': []}
        else:
            raise AssertionError(verb)
    (records / (identity + '.json')).write_text(json.dumps(row))
print(json.dumps({'status': 'completed', 'result': {'success': True, 'data': data}}))
"""


def _entries(root: Path) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    for path in root.rglob("*"):
        if path.is_file() or path.is_symlink():
            data = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
            mode = "120000" if path.is_symlink() else "100755" if path.stat().st_mode & 0o111 else "100644"
            rows[path.relative_to(root).as_posix()] = {"mode": mode, "blob": hashlib.sha1(f"blob {len(data)}\0".encode() + data, usedforsecurity=False).hexdigest()}
    return rows


def _trace_digest(trace: Mapping[str, Any]) -> str:
    excluded = {"trace_sha256", "attribution", "complete", "incomplete_reasons"}
    value = {key: data for key, data in trace.items() if key not in excluded}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class _ReadFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.area = tempfile.TemporaryDirectory(prefix="smoke-read-controls-")
        self.addCleanup(self.area.cleanup)
        self.root = Path(self.area.name).resolve() / "repo"
        self.root.mkdir()
        self.output = Path(self.area.name) / "trace.json"
        self.bridge = Path(self.area.name) / "work-folder-bridge"
        self.bridge.write_text(_FOLDER_BRIDGE)
        self.bridge.chmod(0o700)
        self.worker_temp = Path(self.area.name) / "worker-temp"
        self.worker_temp.mkdir(mode=0o700)

    def run_worker(self, source: str, *, args: tuple[str, ...] = (), relative: bool = False) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        smoke = self.root / "check.py"
        smoke.write_text(source, encoding="utf-8")
        self.output.unlink(missing_ok=True)
        python_path = os.pathsep.join(filter(None, (str(_ROOT), os.environ.get("PYTHONPATH", ""))))
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=python_path, SOLET_BRIDGE=str(self.bridge), TMPDIR=str(self.worker_temp), TEMP=str(self.worker_temp), TMP=str(self.worker_temp))
        proc = subprocess.run([sys.executable, "-m", "quality_gates.smoke_read_worker", "--root", str(self.root), "--trace-out", str(self.output), "--profile-sha256", _PROFILE, "--", "check.py" if relative else str(smoke), *args], cwd=self.root, env=environment, capture_output=True, text=True, check=False)
        trace = json.loads(self.output.read_text()) if self.output.exists() else {}
        return proc, trace

    def valid(self, trace: Mapping[str, Any]) -> tuple[bool, list[str]]:
        return validate_certificate(trace, root=self.root, entries=_entries(self.root), profile_sha256=_PROFILE)

    def assert_incomplete(self, trace: Mapping[str, Any]) -> None:
        self.assertFalse(trace.get("complete"), trace)
        self.assertTrue(trace.get("incomplete_reasons"), trace)
        self.assertFalse(self.valid(trace)[0])


class DescriptorBindingTests(_ReadFixture):
    def test_dup2_binds_target_when_result_is_none(self) -> None:
        state = HookState(root=str(self.root), capture=lambda path: None, observe=lambda *args, **kwargs: None, code=lambda code: None, relative=lambda path: None, reject=lambda reason: None, serialize_stat=lambda result: {}, trusted_files=frozenset())
        state.descriptors[10] = str(self.root / "data")
        result = _file_call(state, "dup2", lambda *args, **kwargs: None, (10, 11), {})
        self.assertIsNone(result)
        self.assertEqual(state.descriptors.get(11), str(self.root / "data"))


class RecordedReadsTests(_ReadFixture):
    def test_custom_opener_is_incomplete(self) -> None:
        (self.root / "decoy").write_bytes(b"decoy")
        (self.root / "hidden").write_bytes(b"good")
        for mode in ("rb", "r"):
            with self.subTest(mode=mode):
                source = f"import os\nfrom pathlib import Path\nroot = Path(__file__).parent\ndef opener(path, flags):\n return os.open({str(self.root / 'hidden')!r}, flags)\nwith open(root / 'decoy', {mode!r}, opener=opener) as stream:\n assert stream.read() in (b'good', 'good')\n"
                proc, trace = self.run_worker(source)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assert_incomplete(trace)
                self.assertIn("unsupported:custom-opener", trace["incomplete_reasons"])

    def test_public_api_and_digest_tampering(self) -> None:
        self.assertEqual(READ_SCHEMA, "smoke-reads/v1")
        with TraceRecorder(self.root, "check.py", _PROFILE, Path(self.area.name) / "private") as recorder:
            result = recorder.seal()
        self.assertIsInstance(result, ReadTrace)
        self.assertEqual(result.to_dict()["schema"], READ_SCHEMA)
        proc, trace = self.run_worker("print('OK')\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(trace["trace_sha256"], _trace_digest(trace))
        trace["smoke"] = "other.py"
        self.assertFalse(self.valid(trace)[0])

    def test_computed_resource_and_unread_file(self) -> None:
        (self.root / "data.txt").write_bytes(b"payload")
        (self.root / "unread.txt").write_bytes(b"one")
        proc, trace = self.run_worker("from pathlib import Path\np = Path(__file__).parent / ('da' + 'ta.txt')\nassert p.read_bytes() == b'payload'\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(trace["complete"], trace["incomplete_reasons"])
        self.assertIn("data.txt", {row["path"] for row in trace["reads"]})
        self.assertTrue(self.valid(trace)[0], self.valid(trace))
        (self.root / "unread.txt").write_bytes(b"two")
        self.assertTrue(self.valid(trace)[0], self.valid(trace))
        (self.root / "data.txt").write_bytes(b"changed")
        self.assertFalse(self.valid(trace)[0])

    def test_failed_open_and_absent_path_exists(self) -> None:
        proc, trace = self.run_worker("from pathlib import Path\np=Path(__file__).parent/'missing'\nassert not p.exists()\ntry:\n p.open('rb')\nexcept FileNotFoundError:\n pass\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(trace["absent"], trace)
        (self.root / "missing").write_text("new")
        self.assertFalse(self.valid(trace)[0])

    def test_directory_add_remove_and_order(self) -> None:
        directory = self.root / "items"
        directory.mkdir()
        (directory / "first").write_text("a")
        proc, trace = self.run_worker("import os\nfrom pathlib import Path\np=Path(__file__).parent/'items'\nassert os.listdir(p)==['first']\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(trace["directories"], trace)
        self.assertTrue(self.valid(trace)[0], self.valid(trace))
        (directory / "second").write_text("b")
        self.assertFalse(self.valid(trace)[0])
        (directory / "second").unlink()
        (directory / "first").unlink()
        self.assertFalse(self.valid(trace)[0])

    def test_stat_fields_and_mmap(self) -> None:
        (self.root / "data").write_bytes(b"abc")
        proc, trace = self.run_worker("import os, mmap\nfrom pathlib import Path\np=Path(__file__).parent/'data'\ns=os.stat(p)\nwith p.open('rb') as f:\n with mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as m:\n  assert m[:]==b'abc'\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = [row for row in trace["metadata"] if row["operation"] == "stat"]
        self.assertTrue(rows, trace)
        fields = rows[0]["result"]["fields"]
        self.assertIn("st_mtime_ns", fields)
        self.assertEqual(set(fields), {name for name in dir(os.stat(self.root / "data")) if name.startswith("st_")})

    def test_stat_nanoseconds_with_equal_tuple(self) -> None:
        path = self.root / "data"
        path.write_bytes(b"same")
        fixed = 1_700_000_000_000_000_000
        os.utime(path, ns=(fixed, fixed))
        proc, trace = self.run_worker("print('OK')\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(trace["complete"], trace.get("incomplete_reasons"))
        # Preserve the worker's read identities without opening the stat-only
        # input during index construction, which could change its access time.
        entries = {row["path"]: {key: row[key] for key in ("mode", "blob")} for row in trace["reads"]}
        self.assertNotIn("data", entries)
        self.assertEqual(validate_certificate(trace, root=self.root, entries=entries, profile_sha256=_PROFILE), (True, []), trace.get("incomplete_reasons"))
        # Stat recording is covered above. Supply a synthetic expected stat
        # observation here: a public worker's real stat identity is root-sensitive.
        # Keep its measured completeness and matched-probe bindings intact.
        info = path.stat()
        fields: dict[str, Any] = {}
        for name in dir(info):
            if name.startswith("st_"):
                value = getattr(info, name)
                fields[name] = {"type": type(value).__name__, "value": value}
        row: dict[str, Any] = {"path": "data", "operation": "stat", "follow_symlinks": True, "result": {"tuple": list(info), "fields": fields}}
        trace["metadata"].append(row)
        trace["trace_sha256"] = _trace_digest(trace)
        self.assertEqual(validate_certificate(trace, root=self.root, entries=entries, profile_sha256=_PROFILE), (True, []))
        original_tuple = tuple(row["result"]["tuple"])
        self.assertEqual(row["result"]["fields"]["st_mtime_ns"], {"type": "int", "value": fixed})
        # A controlled expected-observation change isolates ns comparison from
        # coarse ctime and worker duration while retaining the real stat tuple.
        row["result"]["fields"]["st_mtime_ns"]["value"] = fixed + 1
        self.assertEqual(tuple(row["result"]["tuple"]), original_tuple)
        trace["trace_sha256"] = _trace_digest(trace)
        self.assertEqual(validate_certificate(trace, root=self.root, entries=entries, profile_sha256=_PROFILE), (False, ["observation-changed:data"]))

    def test_alias_and_symlink_retarget(self) -> None:
        (self.root / "a").write_bytes(b"same")
        (self.root / "b").write_bytes(b"same")
        (self.root / "link").symlink_to("a")
        proc, trace = self.run_worker("from pathlib import Path\nassert (Path(__file__).parent/'link').read_bytes()==b'same'\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.valid(trace)[0], self.valid(trace))
        (self.root / "link").unlink()
        (self.root / "link").symlink_to("b")
        self.assertFalse(self.valid(trace)[0])

    def test_dir_fd_reuse_chdir_and_unknown_descriptor(self) -> None:
        (self.root / "data").write_bytes(b"abc")
        source = "import os\nfrom pathlib import Path\nd=os.open(Path(__file__).parent,os.O_RDONLY)\nf=os.open('data',os.O_RDONLY,dir_fd=d)\nassert os.read(f,3)==b'abc'\ng=os.dup(f)\nos.lseek(g,0,0)\nassert os.read(g,3)==b'abc'\nos.close(f)\nos.close(g)\nos.fchdir(d)\nos.close(d)\nf=os.open('data',os.O_RDONLY)\nassert os.read(f,3)==b'abc'\nos.close(f)\n"
        proc, trace = self.run_worker(source)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("data", {row["path"] for row in trace["reads"]})
        proc, trace = self.run_worker("import os\nos.fstat(1)\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assert_incomplete(trace)

    def test_file_mutation_during_read(self) -> None:
        (self.root / "data").write_bytes(b"one")
        proc, trace = self.run_worker("from pathlib import Path\np=Path(__file__).parent/'data'\np.read_bytes()\np.write_bytes(b'two')\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assert_incomplete(trace)

    def test_import_lookup_shadow_and_dynamic_exec(self) -> None:
        (self.root / "helper.py").write_text("VALUE=3\n")
        proc, trace = self.run_worker("import helper\nassert helper.VALUE==3\nexec(compile('value=4','<dynamic>','exec'))\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(trace["code_inputs"], trace)
        self.assertFalse(self.valid(trace)[0])
        proc, trace = self.run_worker("import helper\nassert helper.VALUE==3\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(trace["directories"] or trace["absent"], trace)
        (self.root / "helper.py").write_text("VALUE=4\n")
        self.assertFalse(self.valid(trace)[0])

    def test_native_network_clock_random_thread_and_hooks(self) -> None:
        cases = {
            "native-child": "import subprocess,sys\nsubprocess.run([sys.executable,'-c',\"open('child-input').read()\"],check=True)\n",
            "network": "import socket\ns=socket.socket()\ns.close()\n",
            "ctypes": "import ctypes\nctypes.CDLL(None)\n",
            "extension": "import math\nmath.sin(0)\n",
            "clock": "import time\ntime.time()\n",
            "random": "import random\nrandom.random()\n",
            "thread": "import threading\nt=threading.Thread(target=lambda:None)\nt.start()\nt.join()\n",
            "hook": "import sys\nsys.setprofile(None)\n",
            "audit-hook": "import sys\nsys.addaudithook(lambda *args:None)\n",
        }
        (self.root / "child-input").write_text("child")
        for name, source in cases.items():
            with self.subTest(name=name):
                proc, trace = self.run_worker(source)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assert_incomplete(trace)

    def test_partial_sidecar_and_abrupt_exit(self) -> None:
        self.output.write_text('{"schema":')
        self.assertFalse(self.valid({})[0])
        proc, trace = self.run_worker("import os\nos._exit(7)\n")
        self.assertEqual(proc.returncode, 7, proc.stderr)
        self.assertFalse(trace)
        self.assertFalse(self.valid(trace)[0])

    def test_original_argv_file_exit_and_output(self) -> None:
        proc, trace = self.run_worker("import sys\nassert __name__=='__main__'\nassert __file__==sys.argv[0]\nassert sys.argv[1:] == ['one','two']\nprint('OUT')\nprint('ERR',file=sys.stderr)\nraise SystemExit(3)\n", args=("one", "two"))
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(proc.stdout, "OUT\n")
        self.assertEqual(proc.stderr, "ERR\n")
        self.assertEqual(trace["schema"], READ_SCHEMA)

    def test_worker_scratch_closes_registered_folders(self) -> None:
        proc, trace = self.run_worker("print('PASS synthetic')\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((proc.stdout, proc.stderr), ("PASS synthetic\n", ""))
        self.assertTrue(trace["complete"], trace["incomplete_reasons"])
        rows = [json.loads(path.read_text()) for path in (self.bridge.parent / "work-folder-records").glob("*.json")]
        self.assertEqual(len(rows), 4, rows)
        self.assertTrue(all(row["state"] == "deleted" and not Path(row["path"]).exists() for row in rows), rows)


class RootBindingTests(_ReadFixture):
    def test_root_sensitive_counterexample(self) -> None:
        second = Path(self.area.name).resolve() / ".smoke-root-probe"
        source = f"if __file__ == {str(second / 'check.py')!r}:\n raise SystemExit(9)\nprint('PASS root observation')\n"
        proc, trace = self.run_worker(source)
        second.mkdir()
        (second / "check.py").write_text(source)
        direct = subprocess.run([sys.executable, str(second / "check.py")], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(direct.returncode, 9, direct.stderr)
        self.assertFalse(trace["complete"], trace)
        self.assertIn("root_sensitive", trace["incomplete_reasons"])
        self.assertFalse(self.valid(trace)[0])

    def test_independent_roots_and_root_digest(self) -> None:
        source = "from pathlib import Path\nassert (Path(__file__).parent/'data').read_bytes()==b'abc'\nprint('PASS independent')\n"
        (self.root / "data").write_bytes(b"abc")
        first_root = self.root
        first, trace = self.run_worker(source)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(trace["complete"], trace["incomplete_reasons"])
        self.assertTrue(self.valid(trace)[0], self.valid(trace))
        self.root = first_root.parent / "other"
        self.root.mkdir()
        (self.root / "data").write_bytes(b"abc")
        second, other = self.run_worker(source)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue(other["complete"], other["incomplete_reasons"])
        for key in ("reads", "metadata", "absent", "directories", "code_inputs"):
            self.assertEqual(trace[key], other[key], key)
        self.assertNotEqual(trace["trace_sha256"], other["trace_sha256"])
        self.assertIn("root-digest-mismatch", self.valid(trace)[1])
        forged = dict(other, runtime_inputs=[row for row in other["runtime_inputs"] if row["path"] != "@repository_root"])
        forged["trace_sha256"] = _trace_digest(forged)
        self.assertIn("root-digest-mismatch", self.valid(forged)[1])

    def test_third_root_branch_is_not_portable(self) -> None:
        third = self.root.parent / "third"
        source = f"if __file__ == {str(third / 'check.py')!r}:\n raise SystemExit(9)\nprint('PASS first two roots')\n"
        first, trace = self.run_worker(source)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(trace["complete"], trace["incomplete_reasons"])
        third.mkdir()
        (third / "check.py").write_text(source)
        actual = subprocess.run([sys.executable, str(third / "check.py")], capture_output=True, text=True, check=False)
        self.assertEqual(actual.returncode, 9)
        self.root = third
        self.assertIn("root-digest-mismatch", self.valid(trace)[1])

    def test_probe_unavailable_preserves_original(self) -> None:
        existing = Path(self.area.name) / ".smoke-root-probe"
        existing.mkdir()
        (existing / "keep").write_text("owned elsewhere")
        proc, trace = self.run_worker("print('OUT')\n")
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "OUT\n", ""))
        self.assertIn("root_probe_unavailable", trace["incomplete_reasons"])
        self.assertFalse(self.valid(trace)[0])
        self.assertEqual((existing / "keep").read_text(), "owned elsewhere")

    def test_relative_argv_and_absolute_file(self) -> None:
        proc, trace = self.run_worker("import sys\nassert sys.argv[0]=='check.py'\nassert __file__.endswith('/check.py')\nprint('RELATIVE')\n", relative=True)
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "RELATIVE\n", ""))
        self.assertTrue(trace["complete"], trace["incomplete_reasons"])


if __name__ == "__main__":
    unittest.main()
