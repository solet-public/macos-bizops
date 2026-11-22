#!/usr/bin/env python3
"""Tier U: a clean pass is reused across trees only while its recorded inputs hold.

Every case runs the real runner and the real read worker on a scratch git
repository whose successive commits are the successive trees, all at one
resolved root. Only the tool profile and the runner's launch environment are
fixed so the scratch tree needs no checkout of the platform.
"""

# ruff: noqa: E402
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
from quality_gates import run_smokes, smoke_proof
from quality_gates.smoke_identity import FrozenTree, digest, freeze_request, recorded_key
from quality_gates.smoke_pass_store import CAP_SECONDS, SmokePassStore
from quality_gates.smoke_proof import execute_registered, finish_full_proofs, main, prepare_full_proofs, run_selected_smokes, select_smokes
from quality_gates.smoke_read_store import MAX_CERTIFICATES, Context, Misses, RecordedReadStore, RecordedReuse
from quality_gates.tests._focused_smoke_selector_fixture import FrozenProofFixture, git_environment

# A stand-in for the solet's work-folder verbs, so scratch-folder custody (the runner's and the worker's) completes
# quietly: a registration failure prints a line with a random path, which the worker's root probe reads as a difference.
_BRIDGE = """#!/usr/bin/env python3
import hashlib, json, os, shutil, sys
from pathlib import Path

verb, args = sys.argv[2].rsplit('::', 1)[1], json.loads(sys.argv[3])
records = Path(__file__).parent / 'work-folder-records'
records.mkdir(exist_ok=True)
if verb == 'read_work_folders' and args['mode'] == 'list':
    data = {'folders': [], 'next': None}
elif verb == 'open_work_folder':
    info = Path(args['path']).lstat()
    identity = 'wkf_' + hashlib.sha256(os.fsencode(args['path'])).hexdigest()
    data = dict(folder_id=identity, path=args['path'], state='open', version=1, dev=str(info.st_dev), ino=str(info.st_ino), harvest_kind=args['harvest'])
    (records / (identity + '.json')).write_text(json.dumps(data))
else:
    identity = args['folder_id']
    data = json.loads((records / (identity + '.json')).read_text())
    if verb == 'read_work_folders':
        data = {'folders': [data]}
    elif verb == 'transition_work_folder':
        data['version'] += 1
        data['state'] = 'due' if args['action'] == 'close' else data['state']
        data['harvest_kind'] = args.get('harvest_kind', data['harvest_kind'])
        (records / (identity + '.json')).write_text(json.dumps(data))
    elif verb == 'sweep_work_folders':
        shutil.rmtree(data['path'])
        data = {'advisories': []}
print(json.dumps({'status': 'completed', 'result': {'success': True, 'data': data}}))
"""
_REPOSITORY = "fixture-repository"
_A, _B, _STAMP = "suite/read_a_smoke.py", "suite/read_b_smoke.py", "suite/stamp_smoke.py"
_FIRST = "suite/check_smoke.py"  # the static fixture's own smoke
_GIT = ("git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null")
# The worker is launched from the scratch root, so the platform's own roots are named explicitly.
_PYTHONPATH = os.pathsep.join([str(_ROOT), *map(str, run_smokes._import_roots(_ROOT))])
_CHILD_ENVIRONMENT = run_smokes._child_environment


def _reader(name: str) -> str:
    """A smoke that reads data/<name>.txt through a name no static scan can see.

    It stays inside the calls the recorder classifies (the read worker marks any other native call incomplete).
    """
    return f"from pathlib import Path\nroot = Path(__file__).resolve().parents[1]\nname = ''.join(({name!r}, '.txt'))\nassert len((root / 'data' / name).read_bytes()) > 0\n"


_STAMP_SOURCE = "import os\nfrom pathlib import Path\nroot = Path(__file__).resolve().parents[1]\ninfo = os.stat(root / 'data' / 'stamp.txt')\nassert info.st_size > 0\nprint('even' if info.st_mtime_ns % 2 == 0 else 'odd')\n"
_BRANCH, _PROBE, _LISTED, _SHADOW, _LINKED = "suite/branch_smoke.py", "suite/probe_smoke.py", "suite/listed_smoke.py", "suite/shadow_smoke.py", "suite/linked_smoke.py"
_NATIVE, _RED, _SKIP, _HARNESS = "suite/native_smoke.py", "suite/red_smoke.py", "suite/skip_smoke.py", "suite/harness_smoke.py"
_PRELUDE = "from pathlib import Path\nroot = Path(__file__).resolve().parents[1]\n"
_SOURCES: dict[str, str] = {
    _A: _reader("a"),
    _B: _reader("b"),
    _STAMP: _STAMP_SOURCE,
    # The file it reads next depends on the data it read first.
    _BRANCH: _PRELUDE + "flag = (root / 'data' / 'flag.txt').read_bytes()\nname = 'a' if flag.rfind(b'a') >= 0 else 'b'\nassert len((root / 'data' / ''.join((name, '.txt'))).read_bytes()) > 0\n",
    # It reads an optional flag only when the flag exists.
    _PROBE: "import os\n" + _PRELUDE + "flag = root / 'data' / 'optional.flag'\nif os.path.exists(flag):\n    assert len(flag.read_bytes()) > 0\n",
    _LISTED: "import os\n" + _PRELUDE + "assert len(os.listdir(root / 'plug')) > 0\n",
    # The module is found on a later search directory; a copy beside the smoke would shadow it.
    _SHADOW: "import os, sys\n" + _PRELUDE + "sys.path.append(os.fspath(root / 'lib'))\nimport helper_mod\nassert helper_mod.VALUE > 0\n",
    _LINKED: _PRELUDE + "assert len((root / 'data' / 'link.txt').read_bytes()) > 0\n",
    _NATIVE: "import subprocess, sys\nsubprocess.run([sys.executable, '-c', 'pass'], check=True)\n",
    _RED: "raise SystemExit(1)\n",
    _SKIP: "raise SystemExit(77)\n",
    _HARNESS: "raise SystemExit(124)\n",
}
_BASE: dict[str, str] = {
    **_SOURCES,
    "data/a.txt": "alpha\n",
    "data/b.txt": "beta\n",
    "data/flag.txt": "a\n",
    "data/stamp.txt": "stamp\n",
    "plug/one.txt": "one\n",
    "lib/helper_mod.py": "VALUE = 3\n",
    "notes.txt": "notes\n",
    "quality_gates/lane_source_roots.json": '{"version": 1, "roots": []}\n',
    "quality_gates/gate_smokes.txt": f"{_A}\n{_B}\n{_STAMP}\n",
}


def _seed(*smokes: str) -> dict[str, str]:
    """The base tree registering exactly ``smokes``: a registered smoke can be selected by a rule of its own (a listing is a scan), so each case registers what it uses."""
    return {**_BASE, "quality_gates/gate_smokes.txt": "".join(f"{smoke}\n" for smoke in smokes)}


class _Repository:
    """A real scratch repository: each commit is one tree, checked out at the same resolved root."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        if not (self.root / ".git").exists():
            self.root.mkdir(parents=True, exist_ok=True)
            self.git("init", "-q")

    def git(self, *arguments: str) -> str:
        done = subprocess.run([*_GIT, *arguments], cwd=self.root, env=git_environment(), check=True, capture_output=True, text=True)
        return done.stdout.strip()

    def commit(self, changes: Mapping[str, str | None], *, links: Mapping[str, str] | None = None, modes: Mapping[str, int] | None = None) -> str:
        """Write (or, for None, delete) files, point symlinks, set modes, and commit every change, returning the new commit."""
        for relative, body in changes.items():
            path = self.root / relative
            if body is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        for relative, target in (links or {}).items():
            (self.root / relative).unlink(missing_ok=True)
            (self.root / relative).symlink_to(target)
        for relative, mode in (modes or {}).items():
            (self.root / relative).chmod(mode)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", "tree")
        return self.git("rev-parse", "HEAD")

    def tree(self, commit: str) -> str:
        return self.git("rev-parse", f"{commit}^{{tree}}")


class _ReuseCase(unittest.TestCase):
    """One scratch repository, one proof store and the real runner pointed at them."""

    seed: Mapping[str, str] = _BASE

    def setUp(self) -> None:
        self.area = Path(tempfile.mkdtemp(prefix="smoke-read-reuse-")).resolve()
        self.addCleanup(shutil.rmtree, self.area, True)
        self.repo = _Repository(self.area / "repo")
        self.store = SmokePassStore(self.area / "proofs")
        self.variant = 0
        self.stderr = io.StringIO()
        self.bridge = self.area / "bridge"
        self.bridge.write_text(_BRIDGE)
        self.bridge.chmod(0o700)
        self.after_run: Callable[[], None] = lambda: None
        self.first = self.repo.commit(dict(self.seed), links={"data/link.txt": "a.txt"})

    @contextmanager
    def runner(self, root: Path) -> Generator[None]:
        """The real runner, launch path and worker, aimed at the scratch root."""

        def child_environment(python: Path) -> dict[str, str]:
            return {**_CHILD_ENVIRONMENT(python), "PYTHONPATH": _PYTHONPATH, "PYTHONDONTWRITEBYTECODE": "1", "SOLET_BRIDGE": str(self.bridge)}

        def suite(entries: list[str], args: Any, serial_only: frozenset[str], *, smoke_results: Any = None, harness: Any = None) -> Any:
            args.import_environment = {"state": "verified"}
            ran = run_smokes._run_selected_suite(Path(sys.executable), entries, args, serial_only, smoke_results=smoke_results, harness=harness)
            self.after_run()
            return ran

        environment = {"RUN_SMOKES_BATTERY_RECEIPT_DIR": str(self.area / "receipts"), "SOLET_NAME": os.environ.get("SOLET_NAME", "fixture-solet"), "SOLET_BRIDGE": str(self.bridge), "AGENT_WAKE_CLI": str(self.bridge)}
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, environment))
            stack.enter_context(patch.object(run_smokes, "_REPO_ROOT", root))
            stack.enter_context(patch.object(run_smokes, "_DEFAULT_REGISTER", root / "quality_gates" / "gate_smokes.txt"))
            stack.enter_context(patch.object(run_smokes, "_child_environment", child_environment))
            stack.enter_context(patch.object(run_smokes, "_run_suite_with_environment", suite))
            stack.enter_context(patch("quality_gates.smoke_proof.tool_profile", side_effect=lambda *_: self.profile()))
            yield

    def run_tree(self, base: str, candidate: str, *, repo: _Repository | None = None, named: Sequence[str] = (_A, _B), executor: Any = None) -> dict[str, Any]:
        """Run the named smokes for one frozen request through the real proof path."""
        target = repo or self.repo
        request = freeze_request(target.root, base, candidate, repository_id=_REPOSITORY, named_smokes=named)
        with self.runner(target.root), redirect_stdout(io.StringIO()), redirect_stderr(self.stderr):
            return run_selected_smokes(target.root, request, principal="fixture", store=self.store, executor=executor, record_reads=True)

    def counts(self, result: Mapping[str, Any]) -> tuple[int, int]:
        """(executed, reused), after proving every selected row is one or the other."""
        self.assertEqual(result["selected_count"], result["executed_count"] + result["reused_count"], result)
        return result["executed_count"], result["reused_count"]

    @staticmethod
    def executed(result: Mapping[str, Any]) -> list[str]:
        return [row["smoke"] for row in result["proofs"] if not row["reused"]]

    @staticmethod
    def row(result: Mapping[str, Any], smoke: str) -> dict[str, Any]:
        """The proof row of one smoke; its ``recorded`` entry says what became of its certificate."""
        return next(row for row in result["proofs"] if row["smoke"] == smoke)

    @staticmethod
    def selected(result: Mapping[str, Any], smoke: str) -> dict[str, Any]:
        return next(row for row in result["selection"]["selected"] if row["smoke"] == smoke)

    def certificates(self, smoke: str) -> list[dict[str, Any]]:
        """The certificates the store holds for a smoke under the current profile."""
        found, _ = RecordedReadStore.beside(self.store).candidates(smoke, digest(self.profile()))
        return [candidate.trace for candidate in found]

    def profile(self) -> dict[str, Any]:
        """The fixed tool profile in force; a changed ``variant`` stands for a changed code, recorder or dependency version."""
        return {"rule_version": "fixture-v1", "inputs": {}, "variant": self.variant}


class SequenceTests(_ReuseCase):
    def test_an_unrelated_change_reuses_both_and_one_consumed_change_executes_one(self) -> None:
        self.assertEqual(self.counts(self.run_tree(self.first, self.first)), (2, 0))
        unrelated = self.repo.commit({"notes.txt": "changed\n"})
        second = self.run_tree(self.first, unrelated)
        self.assertEqual(self.counts(second), (0, 2))
        for row in second["proofs"]:
            observed = row["proof"]["observation"]
            self.assertEqual((observed["candidate_commit"], observed["candidate_tree"]), (self.first, self.repo.tree(self.first)))
        self.assertEqual({row["key_scope"] for row in second["selection"]["selected"]}, {"recorded"})
        consumed = self.repo.commit({"data/a.txt": "alpha two\n"})
        third = self.run_tree(unrelated, consumed)
        self.assertEqual(self.counts(third), (1, 1))
        self.assertEqual(self.executed(third), [_A])
        self.assertEqual(third["proofs"][0]["proof"]["observation"]["candidate_commit"], consumed)


class RootAndMetadataTests(_ReuseCase):
    def test_identical_bytes_at_a_distinct_root_execute(self) -> None:
        self.run_tree(self.first, self.first)
        clone = self.area / "clone"
        subprocess.run(["git", "clone", "-q", str(self.repo.root), str(clone)], env=git_environment(), check=True, capture_output=True)
        other = _Repository(clone)
        moved = other.commit({"notes.txt": "moved\n"})
        elsewhere = self.run_tree(self.first, moved, repo=other)
        self.assertEqual(self.counts(elsewhere), (2, 0))
        home = self.repo.commit({"notes.txt": "moved\n"})
        self.assertEqual(self.counts(self.run_tree(self.first, home)), (0, 2))

    def test_a_metadata_only_change_is_never_consumed(self) -> None:
        stamp = self.repo.root / "data" / "stamp.txt"
        nanoseconds = 1_700_000_000_000_000_000
        os.utime(stamp, ns=(nanoseconds, nanoseconds))
        first = self.run_tree(self.first, self.first, named=(_STAMP,))
        self.assertEqual(self.counts(first), (1, 0))
        os.utime(stamp, ns=(nanoseconds, nanoseconds + 1))
        moved = self.repo.commit({"notes.txt": "moved\n"})
        second = self.run_tree(self.first, moved, named=(_STAMP,))
        self.assertEqual(self.counts(second), (1, 0))


def _passing(entries: list[str]) -> dict[str, Any]:
    """What a runner that records no reads reports for smokes that all pass."""
    return {"exit_code": 0, "import_environment": {"state": "verified"}, "smoke_results": {smoke: {"verdict": "passed", "seconds": 0.01, "attempts": [{"output": "3 passed", "verdict": "passed"}]} for smoke in entries}}


class BranchTests(_ReuseCase):
    seed = _seed(_BRANCH, _PROBE)

    def test_changed_branch_data_an_added_flag_and_changed_source_execute_and_report_the_new_read(self) -> None:
        named = (_BRANCH, _PROBE)
        self.assertEqual(self.counts(self.run_tree(self.first, self.first, named=named)), (2, 0))
        flipped = self.repo.commit({"data/flag.txt": "b\n"})
        flagged = self.run_tree(self.first, flipped, named=named)
        # The data that selects the branch changed: its certificate no longer holds and its old pass is not consumed.
        self.assertEqual((self.counts(flagged), self.executed(flagged), self.selected(flagged, _BRANCH)["recorded"]["status"]), ((1, 1), [_BRANCH], "invalid"))
        self.assertEqual(self.row(flagged, _BRANCH)["recorded"]["outside_recorded_list"]["new_paths"], ["data/b.txt"])
        self.assertIn("outside_recorded_list", self.stderr.getvalue())
        # The flag that was absent appears: the absence was recorded, so the probing smoke runs and its old pass is not consumed.
        # (Its own new trace stats an existing file, which differs under the worker's root probe: the recorder's measured limit, so it leaves no certificate.)
        added = self.repo.commit({"data/optional.flag": "set\n"})
        appeared = self.run_tree(flipped, added, named=named)
        self.assertEqual((self.executed(appeared), self.selected(appeared, _PROBE)["recorded"]["status"]), ([_PROBE], "invalid"))
        edited = self.repo.commit({_BRANCH: _SOURCES[_BRANCH] + "# edited\n"})
        self.assertEqual(self.executed(self.run_tree(added, edited, named=(_BRANCH,))), [_BRANCH])


class ListingTests(_ReuseCase):
    seed = _seed(_LISTED, _SHADOW)

    def test_a_new_directory_member_and_a_module_shadow_execute_their_consumers(self) -> None:
        named = (_LISTED, _SHADOW)
        self.assertEqual(self.counts(self.run_tree(self.first, self.first, named=named)), (2, 0))
        member = self.repo.commit({"plug/two.txt": "two\n"})
        listed = self.run_tree(self.first, member, named=named)
        self.assertEqual((self.counts(listed), self.executed(listed)), ((1, 1), [_LISTED]))
        shadow = self.repo.commit({"suite/helper_mod.py": "VALUE = 9\n"})
        self.assertEqual(self.executed(self.run_tree(member, shadow, named=named)), [_SHADOW])


class LinkTests(_ReuseCase):
    seed = _seed(_LINKED, _B)

    def test_a_retargeted_link_a_mode_change_a_deletion_and_a_rename_execute_the_consumer(self) -> None:
        named = (_LINKED, _B)
        self.assertEqual(self.counts(self.run_tree(self.first, self.first, named=named)), (2, 0))
        retarget = self.repo.commit({}, links={"data/link.txt": "b.txt"})
        self.assertEqual(self.executed(self.run_tree(self.first, retarget, named=named)), [_LINKED])
        mode = self.repo.commit({}, modes={"data/b.txt": 0o755})
        self.assertEqual(sorted(self.executed(self.run_tree(retarget, mode, named=named))), sorted([_B, _LINKED]))
        renamed = self.repo.commit({"data/b.txt": None, "data/c.txt": "beta\n"})
        gone = self.run_tree(mode, renamed, named=named)
        self.assertEqual((self.counts(gone), {self.row(gone, smoke)["recorded"]["status"] for smoke in named}), ((2, 0), {"not_clean"}))


class EdgeTests(_ReuseCase):
    def test_a_recorded_read_selects_a_consumer_that_static_selection_missed(self) -> None:
        self.run_tree(self.first, self.first)
        changed = self.repo.commit({"data/a.txt": "alpha two\n"})
        result = self.run_tree(self.first, changed, named=())
        self.assertEqual([row["smoke"] for row in result["selection"]["selected"]], [_A])
        self.assertEqual({reason["rule"] for reason in self.selected(result, _A)["reasons"]}, {"recorded_read"})
        self.assertEqual((self.counts(result), result["selection"]["unmapped"]), ((1, 0), []))


class UncleanRunTests(_ReuseCase):
    seed = _seed(_A, _NATIVE, _RED, _SKIP, _HARNESS)

    def test_unsupported_and_unclean_runs_leave_no_certificate_and_run_every_time(self) -> None:
        named = (_NATIVE, _RED, _SKIP, _HARNESS)
        first = self.run_tree(self.first, self.first, named=named)
        statuses = {smoke: self.row(first, smoke)["recorded"]["status"] for smoke in named}
        self.assertEqual(statuses, {_NATIVE: "incomplete", _RED: "not_clean", _SKIP: "not_clean", _HARNESS: "not_clean"})
        self.assertTrue(self.row(first, _NATIVE)["recorded"]["reasons"])
        self.assertEqual({smoke: self.certificates(smoke) for smoke in named}, {smoke: [] for smoke in named})
        second = self.run_tree(self.first, self.repo.commit({"notes.txt": "moved\n"}), named=named)
        self.assertEqual(self.counts(second), (4, 0))

    def test_an_executor_that_records_nothing_cannot_mint_a_certificate(self) -> None:
        first = self.run_tree(self.first, self.first, named=(_A,), executor=_passing)
        self.assertEqual((self.counts(first), self.row(first, _A)["recorded"]["status"], self.certificates(_A)), ((1, 0), "no_trace", []))
        moved = self.repo.commit({"notes.txt": "moved\n"})
        self.assertEqual(self.counts(self.run_tree(self.first, moved, named=(_A,), executor=_passing)), (1, 0))

    def test_inputs_that_change_during_a_run_publish_no_certificate(self) -> None:
        def mutate() -> None:
            (self.repo.root / "data" / "a.txt").write_text("mutated\n")

        self.after_run = mutate
        with self.assertRaisesRegex(ValueError, "differs from frozen candidate"):
            self.run_tree(self.first, self.first, named=(_A,))
        self.assertEqual(self.certificates(_A), [])


class StaleEvidenceTests(_ReuseCase):
    def test_a_changed_profile_never_consumes_a_certificate_recorded_under_another(self) -> None:
        self.run_tree(self.first, self.first, named=(_A,))
        self.variant = 1  # a changed code, recorder or dependency version moves the profile
        moved = self.repo.commit({"notes.txt": "moved\n"})
        result = self.run_tree(self.first, moved, named=(_A,))
        self.assertEqual((self.counts(result), "recorded" in self.selected(result, _A)), ((1, 0), False))
        self.variant = 0
        self.assertEqual(self.counts(self.run_tree(self.first, moved, named=(_A,))), (0, 1))

    def test_an_unreadable_tampered_stale_or_proofless_certificate_costs_an_execution(self) -> None:
        self.run_tree(self.first, self.first, named=(_A,))
        certificate = self.certificates(_A)[0]
        path = self.store.root / "read-certificates" / "certificates" / f"{certificate['trace_sha256']}.json"
        previous = self.first
        damages: dict[str, Callable[[], object]] = {
            "unreadable": lambda: path.write_text("{"),
            "tampered": lambda: path.write_text(json.dumps({**certificate, "reads": certificate["reads"][:-1]})),
            "proofless": lambda: shutil.rmtree(self.store.root / recorded_key(_A, certificate["trace_sha256"], self.profile())),
        }
        for label, damage in damages.items():
            with self.subTest(label):
                damage()
                moved = self.repo.commit({"notes.txt": f"{label}\n"})
                self.stderr.seek(0)
                self.stderr.truncate()
                result = self.run_tree(previous, moved, named=(_A,))
                previous = moved
                self.assertEqual(self.counts(result), (1, 0))
                self.assertNotEqual(self.selected(result, _A)["reuse_reason"], "recorded inputs unchanged")
                if label == "unreadable":
                    self.assertIn("unreadable", self.stderr.getvalue())
        future = time.time() + CAP_SECONDS + 60

        def beside(cls: type[RecordedReadStore], proofs: SmokePassStore, **_: object) -> RecordedReadStore:
            return cls(proofs.root / "read-certificates", clock=lambda: future)

        with patch.object(RecordedReadStore, "beside", classmethod(beside)):
            stale = self.run_tree(previous, self.repo.commit({"notes.txt": "stale\n"}), named=(_A,))
        self.assertEqual((self.counts(stale), "recorded" in self.selected(stale, _A)), ((1, 0), False))


class RaceTests(_ReuseCase):
    def test_a_proof_retired_before_the_claim_or_before_publication_is_not_consumed(self) -> None:
        self.run_tree(self.first, self.first, named=(_A,))
        moved = self.repo.commit({"notes.txt": "moved\n"})
        request = freeze_request(self.repo.root, self.first, moved, repository_id=_REPOSITORY, named_smokes=(_A,))
        with self.runner(self.repo.root):
            key = select_smokes(self.repo.root, request, store=self.store)["selected"][0]["key"]
        select = smoke_proof._select

        def select_then_retire(root: Path, request: Mapping[str, Any], store: SmokePassStore, only: Any) -> Any:
            selection = select(root, request, store, only)
            self.store.retire(key, expected_version=self.store.get(key)["version"], principal="operator", reason="retired between selection and claim")
            return selection

        with patch("quality_gates.smoke_proof._select", side_effect=select_then_retire):
            claimed = self.run_tree(self.first, moved, named=(_A,))
        self.assertEqual((self.counts(claimed), self.selected(claimed, _A)["key_scope"], self.store.get(key)["reusable"]), ((1, 0), "recorded", True))
        self.assertIn("key changed before claim", self.stderr.getvalue())

        def operator_retires(entries: list[str]) -> dict[str, Any]:
            self.store.retire(key, expected_version=self.store.get(key)["version"], principal="operator", reason="retired while consumed")
            return _passing(entries)

        with self.assertRaisesRegex(ValueError, "proof changed during consumption"):
            self.run_tree(self.first, self.repo.commit({"notes.txt": "again\n"}), named=(_A,), executor=operator_retires)


class FullSamplingTests(_ReuseCase):
    seed = _seed(_A, _B)

    def test_physical_full_sampling_executes_every_row_and_a_captured_row_certifies(self) -> None:
        self.run_tree(self.first, self.first)
        moved = self.repo.commit({"notes.txt": "moved\n"})
        with self.runner(self.repo.root), patch("quality_gates.smoke_proof.SmokePassStore", return_value=self.store), redirect_stdout(io.StringIO()), redirect_stderr(self.stderr):
            context = prepare_full_proofs(self.repo.root, moved, principal="campaign", attribution={}, capture_dir=self.area / "capture")
            self.assertEqual([(row["reusable"], row["key_scope"]) for row in context["runtime_rows"]], [(False, "tree"), (False, "tree")])
            self.assertEqual(sorted(context["capture"]), [_A, _B])
            ran = execute_registered(Misses([_A, _B], context["capture"]), run_smokes)
            rows = [{**row, "smoke": smoke} for smoke, row in ran["smoke_results"].items()]
            published = finish_full_proofs(self.repo.root, context, {"smoke_results": rows}, {"state": "verified"}, principal="campaign")
        self.assertEqual(sorted(ran["smoke_results"]), [_A, _B])
        self.assertEqual([row["recorded"]["status"] for row in published], ["certified", "certified"])


def _trace_digest(trace: Mapping[str, Any]) -> str:
    excluded = {"trace_sha256", "attribution", "complete", "incomplete_reasons"}
    value = {key: data for key, data in trace.items() if key not in excluded}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class ForgedCertificateTests(_ReuseCase):
    """A certificate no real run wrote, even with a clean proof at its own key, licenses no skip."""

    def forge(self, mutate: Callable[[dict[str, Any]], None], *, key: str | None = None) -> None:
        real = self.certificates(_A)[0]
        forged = copy.deepcopy(real)
        mutate(forged)
        forged["trace_sha256"] = _trace_digest(forged)
        own = key or recorded_key(_A, forged["trace_sha256"], self.profile())
        clean = {"key": own, "smoke": _A, "verdict": "passed", "exit_code": 0, "complete": True, "identity_complete": True, "attempt_count": 1, "candidate_commit": "f" * 40, "candidate_tree": "e" * 40}
        self.store.observe(own, clean, expected_version=self.store.get(own)["version"], principal="forger")
        # Only the forgery is on offer: a genuine certificate that still holds would license its own skip, which is right.
        shutil.rmtree(self.store.root / "read-certificates")
        RecordedReadStore.beside(self.store).add(forged, own, principal="forger")

    def test_a_certificate_without_its_root_a_stat_one_nanosecond_off_and_a_wrong_key_execute(self) -> None:
        self.run_tree(self.first, self.first, named=(_A,))
        info = os.stat(self.repo.root / "data" / "a.txt")
        fields: dict[str, dict[str, Any]] = {name: {"type": type(getattr(info, name)).__name__, "value": getattr(info, name)} for name in dir(info) if name.startswith("st_")}
        fields["st_mtime_ns"]["value"] += 1  # the 10-item tuple and the bytes are exactly the file's own

        def without_root(trace: dict[str, Any]) -> None:
            trace["runtime_inputs"] = [row for row in trace["runtime_inputs"] if row["path"] != "@repository_root"]

        def off_by_a_nanosecond(trace: dict[str, Any]) -> None:
            trace["metadata"].append({"path": "data/a.txt", "operation": "stat", "follow_symlinks": True, "result": {"tuple": list(info), "fields": fields}})

        previous = self.first
        for label, mutate, expected in (("root", without_root, "root-digest-mismatch"), ("nanosecond", off_by_a_nanosecond, "observation-changed:data/a.txt")):
            with self.subTest(label):
                self.forge(mutate)
                moved = self.repo.commit({"notes.txt": f"{label}\n"})
                result = self.run_tree(previous, moved, named=(_A,))
                previous = moved
                self.assertEqual(self.counts(result), (1, 0))
                self.assertEqual((self.selected(result, _A)["recorded"]["status"], expected in self.selected(result, _A)["recorded"]["reasons"]), ("invalid", True))
        with self.subTest("key"):
            self.forge(lambda trace: None, key=digest({"smoke": _A, "scope": "recorded", "certificate": self.certificates(_A)[0]["trace_sha256"]}))
            result = self.run_tree(previous, self.repo.commit({"notes.txt": "key\n"}), named=(_A,))
            self.assertEqual((self.counts(result), self.selected(result, _A)["recorded"]["reasons"]), ((1, 0), ["recorded-key-mismatch"]))


@contextmanager
def _plain_directory(_record: bool) -> Generator[Path]:
    """A directory for traces without work-folder custody, for cases whose runner records nothing."""
    with tempfile.TemporaryDirectory() as directory:
        yield Path(directory)


class RecordedScopeTests(unittest.TestCase):
    """What recorded reads add to a selection, a run and a claim on the static fixture; the reads themselves are measured by the real worker above."""

    def test_recording_is_a_choice_and_an_executor_gets_the_misses_as_a_plain_list_that_carries_the_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("quality_gates.smoke_proof.capture_directory", _plain_directory):
            fixture = FrozenProofFixture(Path(directory))
            seen: list[Any] = []

            def execute(entries: list[str]) -> dict[str, Any]:
                seen.append(entries)
                return _passing(entries)

            with fixture.patches():
                plain = run_selected_smokes(fixture.root, fixture.request(), store=SmokePassStore(fixture.root / "plain"), principal="fixture", executor=execute)
                chosen = run_selected_smokes(fixture.root, fixture.request(), store=SmokePassStore(fixture.root / "chosen"), principal="fixture", executor=execute, record_reads=True)
            self.assertEqual((dict(seen[0].capture), "recorded" in plain["proofs"][0]), ({}, False))
            entries = seen[1]
            self.assertEqual((entries, type(entries), sorted(entries.capture)), ([_FIRST], Misses, [_FIRST]))
            record = entries.capture[_FIRST]
            self.assertEqual((len(record.attempt), len(record.profile_sha256), Path(record.trace_path).name), (32, 64, "reads.json"))
            self.assertEqual(chosen["proofs"][0]["recorded"]["status"], "no_trace")

    def test_the_two_commands_that_run_a_proof_pass_the_choice_on_and_neither_records_unless_asked(self) -> None:
        for flag, expected in (([], False), (["--record-reads"], True)):
            with self.subTest(flag=flag), patch.object(sys, "argv", ["run_smokes.py", "--proof-request", "request.json", *flag]):
                self.assertEqual(run_smokes._parse_args().record_reads, expected)
            with self.subTest(command="proof run", flag=flag), tempfile.TemporaryDirectory() as directory, patch("quality_gates.smoke_proof.run_selected_smokes", return_value={"run": {"exit_code": 0}}) as runner, redirect_stdout(io.StringIO()):
                request_file = Path(directory) / "request.json"
                request_file.write_text("{}")
                main(["run", "--repo-root", directory, "--store-root", directory, "--request-json", str(request_file), "--principal", "fixture", *flag])
                self.assertEqual(runner.call_args.kwargs["record_reads"], expected)

    def test_a_recorded_observation_of_a_changed_path_or_a_directory_above_it_adds_its_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            store = SmokePassStore(fixture.root / "proofs")
            profile = {"rule_version": "fixture-v1", "inputs": {}}
            reads = RecordedReadStore.beside(store)
            seen = {"suite/reads.py": ("reads", "data/input.csv"), "suite/probes.py": ("absent", "data/new.flag"), "suite/lists.py": ("directories", "plug"), "suite/metadata.py": ("metadata", "gone/deleted.txt"), "suite/unrelated.py": ("reads", "notes.txt")}
            for number, (smoke, (section, path)) in enumerate(seen.items()):
                trace = {"smoke": smoke, "profile_sha256": digest(profile), "trace_sha256": f"{number:064x}", "reads": [], "metadata": [], "directories": [], "absent": [], "code_inputs": [], section: [{"path": path}]}
                reads.add(trace, "1" * 64, principal="fixture")
            reuse = RecordedReuse(reads, store, Context(fixture.root, {}, profile, digest(profile)))
            reasons: dict[str, list[dict[str, str]]] = {"suite/static.py": [{"rule": "named", "detail": "named"}]}
            reuse.widen(reasons, [*seen, "suite/static.py", "suite/never_recorded.py"], ["data/input.csv", "data/new.flag", "plug/deep/new.txt", "gone/deleted.txt"])
            self.assertEqual(sorted(reasons), ["suite/lists.py", "suite/metadata.py", "suite/probes.py", "suite/reads.py", "suite/static.py"])
            self.assertEqual(reasons["suite/static.py"], [{"rule": "named", "detail": "named"}])
            self.assertEqual({(reason["recorded_path"], reason["changed_path"]) for reason in reasons["suite/lists.py"]}, {("plug", "plug/deep/new.txt")})

    def test_a_path_the_certificate_recorded_forbids_the_closure_pass_it_would_have_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = FrozenProofFixture(Path(directory))
            fixture.old_sources["notes.txt"] = b"older\n"
            fixture.base = FrozenTree(fixture.base.commit, fixture.base.tree, fixture._entries(fixture.old_sources))
            fixture.delta = [{"path": path, "status": "M", "base": fixture.base.entries[path], "candidate": fixture.candidate.entries[path]} for path in ("data/input.csv", "notes.txt")]
            store = SmokePassStore(fixture.root / "proofs")
            calls: list[list[str]] = []

            def execute(entries: list[str]) -> dict[str, Any]:
                calls.append(list(entries))
                return _passing(entries)

            with fixture.patches():
                first = run_selected_smokes(fixture.root, fixture.request(), store=store, principal="fixture", executor=execute)
                # The same closure under the same key is reused: nothing recorded says a path it read has changed.
                second = run_selected_smokes(fixture.root, fixture.request(), store=store, principal="fixture", executor=execute)
                self.assertEqual((first["selection"]["selected"][0]["key_scope"], first["executed_count"], second["reused_count"]), ("closure", 1, 1))
                # A certificate that read notes.txt, which this delta changes, forbids that reuse though the closure key still holds.
                profile = {"rule_version": "fixture-v1", "inputs": {}}
                trace = {"smoke": _FIRST, "profile_sha256": digest(profile), "trace_sha256": "2" * 64, "reads": [{"path": "notes.txt"}], "metadata": [], "directories": [], "absent": [], "code_inputs": []}
                RecordedReadStore.beside(store).add(trace, "3" * 64, principal="fixture")
                third = run_selected_smokes(fixture.root, fixture.request(), store=store, principal="fixture", executor=execute)
                ignored = select_smokes(fixture.root, fixture.request(), store=store, recorded=False)
            row = third["selection"]["selected"][0]
            self.assertEqual((third["executed_count"], row["reuse_reason"], row["recorded"]["status"]), (1, "a path its certificate recorded changed", "invalid"))
            self.assertIn("recorded_read", {reason["rule"] for reason in row["reasons"]})
            self.assertEqual(calls, [[_FIRST], [], [_FIRST]])
            # Physical full sampling and any caller that ignores certificates see only the closure key.
            self.assertEqual((ignored["selected"][0]["reusable"], "recorded" in ignored["selected"][0]), (True, False))


def _synthetic(number: int, smoke: str, profile: str) -> dict[str, Any]:
    return {"schema": "fixture", "smoke": smoke, "profile_sha256": profile, "trace_sha256": hashlib.sha256(str(number).encode()).hexdigest(), "reads": [{"path": f"data/{number}.txt"}], "complete": True}


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.area = Path(tempfile.mkdtemp(prefix="smoke-read-store-")).resolve()
        self.addCleanup(shutil.rmtree, self.area, True)
        self.proofs = SmokePassStore(self.area / "proofs")
        self.now = 1_700_000_000.0
        self.reads = RecordedReadStore.beside(self.proofs, clock=lambda: self.now)
        self.profile = "a" * 64

    def keep(self, number: int) -> dict[str, Any]:
        """Add one certificate with a clean proof at its key, a second later than the last."""
        self.now += 1
        trace = _synthetic(number, _A, self.profile)
        key = recorded_key(_A, trace["trace_sha256"], {"number": number})
        self.proofs.observe(key, {"key": key, "smoke": _A, "verdict": "passed", "exit_code": 0, "complete": True, "identity_complete": True, "attempt_count": 1}, expected_version=0, principal="fixture")
        self.reads.add(trace, key, principal="fixture")
        return trace

    def test_the_index_is_bounded_ages_out_and_a_missing_certificate_costs_only_itself(self) -> None:
        traces = [self.keep(number) for number in range(MAX_CERTIFICATES + 3)]
        found, advisories = self.reads.candidates(_A, self.profile)
        self.assertEqual(([candidate.trace for candidate in found], advisories), (list(reversed(traces[-MAX_CERTIFICATES:])), []))
        self.assertEqual(len(list((self.reads.root / "certificates").glob("*.json"))), MAX_CERTIFICATES)
        (self.reads.root / "certificates" / f"{found[2].trace['trace_sha256']}.json").unlink()
        found, advisories = self.reads.candidates(_A, self.profile)
        self.assertEqual((len(found), len(advisories)), (MAX_CERTIFICATES - 1, 1))
        # Reading does not refresh a certificate's age: past the cap nothing is offered, and pruning ends all of it.
        self.now += CAP_SECONDS + 1
        self.assertEqual(self.reads.candidates(_A, self.profile), ([], []))
        self.assertEqual(self.reads.prune(self.proofs, principal="fixture"), {"kept": 0, "removed": MAX_CERTIFICATES})
        self.assertEqual(list((self.reads.root / "certificates").glob("*.json")) + list((self.reads.root / "indexes").glob("*.json")), [])

    def test_a_certificate_without_its_proof_is_pruned_and_the_store_is_private(self) -> None:
        kept, orphaned = self.keep(1), self.keep(2)
        shutil.rmtree(self.proofs.root / recorded_key(_A, orphaned["trace_sha256"], {"number": 2}))
        self.assertEqual(self.reads.prune(self.proofs, principal="fixture"), {"kept": 1, "removed": 1})
        self.assertEqual([candidate.trace for candidate in self.reads.candidates(_A, self.profile)[0]], [kept])
        for path in (self.reads.root, self.reads.root / "certificates", *(self.reads.root / "certificates").glob("*.json")):
            self.assertEqual(path.stat().st_mode & 0o077, 0, path)
        with self.assertRaises(PermissionError):
            self.reads.add(kept, "0" * 64, principal="")
        (self.reads.root / "certificates").chmod(0o770)
        with self.assertRaises(PermissionError):
            self.reads.add(kept, "0" * 64, principal="fixture")


if __name__ == "__main__":
    unittest.main()
