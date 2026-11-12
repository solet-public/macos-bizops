"""Public-entry-point fault proofs for the shared, materialized custody store."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins/agent_messaging_plugin/src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_messaging_plugin import custody_store as store  # noqa: E402

DATA = b"\x00shared custody\xff\n"


class Faults:
    """Fail a specific filesystem invocation after recording that it was reached."""

    def __init__(self, target: int = 0, error: int = errno.EIO, *, link_error: int = 0) -> None:
        self.target, self.error, self.link_error = target, error, link_error
        self.trace: list[str] = []

    def _step(self, name: str) -> None:
        self.trace.append(name)
        if len(self.trace) == self.target:
            raise OSError(self.error, f"injected {name} #{self.target}")

    def write(self, descriptor: int, data: bytes) -> int:
        self._step("write")
        return os.write(descriptor, data)

    def fsync(self, descriptor: int) -> None:
        self._step("fsync")
        os.fsync(descriptor)

    def rename(self, source: str, destination: str, *, src_dir_fd: int, dst_dir_fd: int) -> None:
        self._step("rename")
        os.rename(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    def link(self, source: str, destination: str, *, src_dir_fd: int, dst_dir_fd: int, follow_symlinks: bool) -> None:
        self._step("link")
        if self.link_error:
            raise OSError(self.link_error, "injected unsupported hard link")
        os.link(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, follow_symlinks=follow_symlinks)

    def writer(self) -> store.PacketWriter:
        return store.PacketWriter(self.write, self.fsync, self.rename, self.link)


def _request(label: str) -> tuple[bytes, dict[str, store.Payload]]:
    payload = store.Payload.from_bytes(DATA, "sha1")
    return store.canonical_bytes({"label": label, "payload": payload.sha256}), {payload.sha256: payload}


def _publish(root: Path, label: str = "one", writer: store.PacketWriter = store.DEFAULT_WRITER) -> store.Publication:
    raw, payloads = _request(label)
    return store.publish_packet(root, ("packets", "folder"), raw, payloads, writer, dir_mode=0o700, file_mode=0o400, pool=("pool",))


def _layout(directory: Path) -> list[dict[str, str | int | None]]:
    return [{"path": str(path.relative_to(directory)), "mode": stat.S_IMODE(path.stat().st_mode), "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None} for path in [directory, *sorted(directory.rglob("*"))]]


class CustodyStoreSmoke(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="custody-store-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_pool_dedup_modes_and_verified_reads(self) -> None:
        first, second = _publish(self.root), _publish(self.root, "two")
        digest = hashlib.sha256(DATA).hexdigest()
        pool = self.root / "pool" / digest
        packets = [self.root / "packets/folder" / item.packet_id for item in (first, second)]
        self.assertEqual([path.name for path in pool.parent.iterdir()], [digest])
        self.assertEqual(pool.stat().st_nlink, 3, "two packets plus one pool name share one inode")
        for packet, label in zip(packets, ("one", "two"), strict=True):
            raw, payloads = _request(label)
            self.assertEqual(store.read_verified(self.root, ("packets", "folder", packet.name), packet_id=packet.name, object_format="sha1"), raw)
            store.verify_payload_set(self.root, ("packets", "folder", packet.name), {digest: (payloads[digest].oid, len(DATA))}, object_format="sha1")
            self.assertTrue(pool.samefile(packet / "payload" / digest))
        for path in [self.root, *self.root.rglob("*")]:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o400, path)

    def test_default_atomicity_each_writer_invocation(self) -> None:
        """Mutation: disable _Publication._rollback; the no-leftovers assertion kills it."""
        baseline = self.root / "baseline"
        baseline.mkdir(mode=0o700)
        observed = Faults()
        _publish(baseline, writer=observed.writer())
        self.assertEqual(set(observed.trace), {"write", "fsync", "rename", "link"})
        for target, step in enumerate(observed.trace, 1):
            with self.subTest(target=target, step=step):
                trial = self.root / f"fault-{target}"
                trial.mkdir(mode=0o700)
                fault = Faults(target)
                with self.assertRaises(store.CustodyStoreError) as raised:
                    _publish(trial, writer=fault.writer())
                self.assertEqual(raised.exception.code, "custody_publication_failed")
                self.assertEqual(fault.trace, observed.trace[:target], "the intended injected step was reached")
                self.assertEqual(raised.exception.staging, "")
                self.assertFalse(any(path.is_file() or path.is_symlink() for path in trial.rglob("*")), "default rollback removes staged bytes, pool bytes and publication")
                self.assertFalse(any(path.name.startswith(store.STAGING_PREFIX) for path in trial.rglob("*")), "default rollback removes every temporary name")

    def test_retained_staging_and_validation_failure(self) -> None:
        """Mutation: force retain=False in rollback; retained-staging assertion kills it."""
        raw, payloads = _request("retained")
        for failure in ("write", "validate"):
            with self.subTest(failure=failure):
                trial = self.root / failure
                trial.mkdir(mode=0o700)
                called: list[str] = []

                def validate(staging: str, called: list[str] = called) -> None:
                    called.append(staging)
                    raise store.CustodyStoreError("custody_inventory_changed", "injected source recheck failure")

                writer = Faults(1).writer() if failure == "write" else store.DEFAULT_WRITER
                with self.assertRaises(store.CustodyStoreError) as raised:
                    store.publish_packet(trial, ("packets",), raw, payloads, writer, pool=None, validate=validate, retain_failed_staging=True)
                staging = trial / raised.exception.staging
                self.assertTrue(staging.is_dir(), "the explicit lane policy retains and reports failed staging")
                self.assertTrue(staging.name.startswith(store.STAGING_PREFIX))
                self.assertFalse((trial / "packets" / hashlib.sha256(raw).hexdigest()).exists(), "failed publication is never visible")
                self.assertEqual(bool(called), failure == "validate", "the validation mutation reached its intended callback")

    def test_default_validation_failure_cleans(self) -> None:
        raw, payloads = _request("validation")
        called: list[str] = []

        def validate(staging: str) -> None:
            called.append(staging)
            raise store.CustodyStoreError("custody_inventory_changed", "injected validation")

        with self.assertRaises(store.CustodyStoreError):
            store.publish_packet(self.root, ("packets",), raw, payloads, pool=("pool",), validate=validate)
        self.assertEqual(len(called), 1)
        self.assertFalse(any(path.is_file() for path in self.root.rglob("*")))
        self.assertFalse(any(path.name.startswith(store.STAGING_PREFIX) for path in self.root.rglob("*")))

    def test_concurrent_same_packet_and_enospc(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: _publish(self.root), range(2)))
        self.assertEqual(len({result.packet_id for result in results}), 1)
        self.assertEqual(sum(result.created for result in results), 1)
        digest = hashlib.sha256(DATA).hexdigest()
        self.assertEqual((self.root / "pool" / digest).stat().st_nlink, 2)
        self.assertFalse(any(path.name.startswith(store.STAGING_PREFIX) for path in self.root.rglob("*")))
        full = self.root / "enospc"
        full.mkdir(mode=0o700)
        with self.assertRaises(store.CustodyStoreError) as raised:
            _publish(full, writer=Faults(1, errno.ENOSPC).writer())
        cause = raised.exception.__cause__
        assert isinstance(cause, OSError)
        self.assertEqual(cause.errno, errno.ENOSPC)
        self.assertFalse(any(path.is_file() for path in full.rglob("*")))

    def test_link_failure_is_a_reported_verified_copy(self) -> None:
        for error in (errno.EPERM, errno.EXDEV):
            trial = self.root / str(error)
            trial.mkdir(mode=0o700)
            result = _publish(trial, writer=Faults(link_error=error).writer())
            digest = hashlib.sha256(DATA).hexdigest()
            self.assertEqual(result.copied_payloads, (digest,))
            packet = trial / "packets/folder" / result.packet_id
            self.assertEqual((packet / "payload" / digest).read_bytes(), DATA)
            self.assertFalse((trial / "pool" / digest).samefile(packet / "payload" / digest))

    def test_stale_lifecycle_reversal_terminal_and_sweep(self) -> None:
        """Mutation: omit expected/current comparison in _transition; stale assertion kills it."""
        first, second = _publish(self.root), _publish(self.root, "two")
        parts = ("packets", "folder", first.packet_id)
        present = store.observe_packet(self.root, parts)
        retired = store.trash(self.root, parts, expected=present)
        self.assertEqual(retired.state, "trashed")
        for operation in (store.untrash, store.purge):
            with self.assertRaises(store.CustodyStoreError) as raised:
                operation(self.root, parts, expected=present)
            self.assertEqual(raised.exception.code, "recovery_stale_state", "stale observation must refuse before a transition")
        reopened = store.untrash(self.root, parts, expected=retired)
        self.assertEqual(reopened.state, "present")
        retired_again = store.trash(self.root, parts, expected=reopened)
        self.assertNotEqual(retired.identity, retired_again.identity, "valid ABA setup changed the observed physical state")
        with self.assertRaises(store.CustodyStoreError):
            store.untrash(self.root, parts, expected=retired)
        with self.assertRaises(store.CustodyStoreError):
            store.purge(self.root, parts, expected=retired)
        absent = store.purge(self.root, parts, expected=retired_again)
        self.assertEqual(absent.state, "absent")
        self.assertEqual(store.sweep_pool(self.root), (), "a live second packet keeps the shared pool inode")
        other = ("packets", "folder", second.packet_id)
        store.purge(self.root, other, expected=store.trash(self.root, other, expected=store.observe_packet(self.root, other)))
        self.assertEqual(store.sweep_pool(self.root), (hashlib.sha256(DATA).hexdigest(),))
        with self.assertRaises(store.CustodyStoreError):
            store.untrash(self.root, parts, expected=retired_again)

    def test_corrupt_existing_packet_is_not_replaced(self) -> None:
        raw, payloads = _request("corrupt")
        digest = hashlib.sha256(raw).hexdigest()
        existing = self.root / "packets" / digest
        existing.mkdir(parents=True)
        identity = existing.stat().st_ino
        with self.assertRaises(store.CustodyStoreError):
            store.publish_packet(self.root, ("packets",), raw, payloads)
        self.assertEqual((existing.stat().st_ino, list(existing.iterdir())), (identity, []))

    def test_reader_no_follow_and_no_lane_reexports(self) -> None:
        from agent_messaging_plugin import lane_worktree_evidence, lane_worktree_preservation

        source = self.root / "source"
        source.write_bytes(DATA)
        (self.root / "link").symlink_to("source")
        entry = store.read_entry(self.root, "link", object_format="sha1", digest=True)
        self.assertEqual((entry.mode, entry.oid), (store.SYMLINK_MODE, store.blob_oid(b"source", "sha1")))
        self.assertEqual(store.read_entry(self.root, "../source", object_format="sha1", digest=True), store.UNREADABLE_ENTRY)
        for module in (lane_worktree_evidence, lane_worktree_preservation):
            for name in ("LocalEntry", "read_entry", "PacketWriter", "DEFAULT_WRITER"):
                self.assertFalse(hasattr(module, name), (module.__name__, name))

    def test_golden_lane_packets_layout_modes_and_correction(self) -> None:
        import lane_worktrees_smoke as fixture

        from agent_messaging_plugin import lane_worktree_preservation as lane_engine

        root = self.root / "golden"
        environment = fixture._environment(root) | {"GIT_AUTHOR_DATE": "2001-01-01T00:00:00+00:00", "GIT_COMMITTER_DATE": "2001-01-01T00:00:00+00:00"}
        repo, _lane = fixture._custody_lane(root, environment, "lane-golden")
        context = fixture._custody_context(lane_engine, repo, "lane-golden")
        snapshot = lane_engine.snapshot_lane_custody(context, environment=environment)
        self.assertTrue(snapshot.success)
        assert snapshot.snapshot is not None
        captured = lane_engine.capture_lane_custody(context, expected_fingerprint=snapshot.snapshot.fingerprint, disposition=lane_engine.DISPOSITION_PRESERVED, rationale="golden fixture", environment=environment)
        self.assertTrue(captured.success, captured)
        assert captured.packet is not None
        corrected = lane_engine.correct_lane_custody(context, captured.packet.packet_id, disposition=lane_engine.DISPOSITION_SUPERSEDED, rationale="golden correction", environment=environment)
        self.assertTrue(corrected.success, corrected)
        assert corrected.packet is not None
        predecessor: str | None = None
        for actual, golden in zip((captured.packet, corrected.packet), GOLDEN_PACKETS, strict=True):
            expected = json.loads(json.dumps(golden["manifest"]))
            expected["binding"]["common_dir"] = str(repo / ".git")
            expected["provenance"]["predecessor"] = predecessor
            raw = store.canonical_bytes(expected)
            predecessor = hashlib.sha256(raw).hexdigest()
            self.assertEqual(actual.packet_id, predecessor, "golden packet id is byte-identical after substituting only the disposable root")
            self.assertEqual(actual.manifest.encode(), raw)
            layout = json.loads(json.dumps(golden["entries"]))
            for item in layout:
                if item["path"] == "manifest.json":
                    item["sha256"] = predecessor
            self.assertEqual(_layout(fixture._custody_store(repo, context.agent_instance_id, environment) / actual.packet_id), layout)


# Captured by the original engine before extraction; only the disposable binding is substituted.
GOLDEN_PACKETS = json.loads(
    '[{"entries":[{"mode":493,"path":".","sha256":null},{"mode":292,"path":"manifest.json","sha256":"c9718c14b0d4798e1c0b10c43c9a5689294bcbfe5c3106b6dea9a0e095113b44"},{"mode":493,"path":"payload","sha256":null},{"mode":292,"path":"payload/01b88d89292ebca9477ef5242b9b1d7c61e884b173f4065144b389d3eda44960","sha256":"01b88d89292ebca9477ef5242b9b1d7c61e884b173f4065144b389d3eda44960"},{"mode":292,"path":"payload/09bf907bc70edbe94c4a9187d585423efd8a831614145ee1b620ab173a96af75","sha256":"09bf907bc70edbe94c4a9187d585423efd8a831614145ee1b620ab173a96af75"},{"mode":292,"path":"payload/306c6ca7407560340797866e077e053627ad409277d1b9da58106fce4cf717cb","sha256":"306c6ca7407560340797866e077e053627ad409277d1b9da58106fce4cf717cb"},{"mode":292,"path":"payload/3428719b7688c78a0cc8ba4b9e80b4e464c815fbccfd4b20695a15ffcefc22af","sha256":"3428719b7688c78a0cc8ba4b9e80b4e464c815fbccfd4b20695a15ffcefc22af"},{"mode":292,"path":"payload/4a47144eeaae2690f607ff501ff3b2c73a3fffcfbacdce1b4740270859ac6146","sha256":"4a47144eeaae2690f607ff501ff3b2c73a3fffcfbacdce1b4740270859ac6146"},{"mode":292,"path":"payload/56f82f1f6e911b711f507016bc5e48eef1fdc7b97e1afb9facb5b98602dfb5c1","sha256":"56f82f1f6e911b711f507016bc5e48eef1fdc7b97e1afb9facb5b98602dfb5c1"},{"mode":292,"path":"payload/58eab51b655a15f6ebf823bfc3de1372b44b8040112ca8657c2f0d83aadc7114","sha256":"58eab51b655a15f6ebf823bfc3de1372b44b8040112ca8657c2f0d83aadc7114"},{"mode":292,"path":"payload/5c4704ede4d7e6586804965694fc18561a793aea5dc9bce59c4b6b725081d9ec","sha256":"5c4704ede4d7e6586804965694fc18561a793aea5dc9bce59c4b6b725081d9ec"},{"mode":292,"path":"payload/6ac11c4f13237f7a51d7ade32a554f1a4652ac6c006b2f8186d419b86f63e5c6","sha256":"6ac11c4f13237f7a51d7ade32a554f1a4652ac6c006b2f8186d419b86f63e5c6"},{"mode":292,"path":"payload/8944e854e3f4447241f05a4624cc41c6f9872232251870fc8183743f05d49a2e","sha256":"8944e854e3f4447241f05a4624cc41c6f9872232251870fc8183743f05d49a2e"},{"mode":292,"path":"payload/9ac007af3de930baf647288da0c843b26a5f046a3fe1351f1bb039b242d22cdf","sha256":"9ac007af3de930baf647288da0c843b26a5f046a3fe1351f1bb039b242d22cdf"},{"mode":292,"path":"payload/a8076d3d28d21e02012b20eaf7dbf75409a6277134439025f282e368e3305abf","sha256":"a8076d3d28d21e02012b20eaf7dbf75409a6277134439025f282e368e3305abf"}],"manifest":{"binding":{"agent_instance_id":"agi-lane-golden","assignment_sha256":"4007adad092eddda39b5d215aa10e3c7fa027720229985ac89a4ce02c443eeef","branch":"lane/lane-golden/agi-lane-golden","common_dir":"/private/tmp/gitsafety-u1-agi-5cce54019abede4b537f2fd71d2452be/golden-before/fixture-repo/.git","lane_name":"lane-golden","repository_id":"rep_fixture","revision_id":"drv_fixture","unit_id":"unt_fixture","unit_key":"unt_fixture-key"},"build_output":[".venv","__pycache__/"],"fingerprint":"a9038c84aa1ec7402e2eb69f67c03a9a921d83b481bf06a14978960b4e2f7bd2","head_commit":"f949430ffbc5ca13215afba82a23f85cd174a691","head_tree":"922002a763607908b02e0396f23fb1d502a72b03","index":[{"flags":[],"mode":33188,"oid":"d5f7fc3f74f7dec08280f370a975b112e8f60818","path":"added.txt","sha256":"3428719b7688c78a0cc8ba4b9e80b4e464c815fbccfd4b20695a15ffcefc22af","size":6,"stage":0},{"flags":[],"mode":33188,"oid":"7d91f6f988b49dd27865a8132bc2f3bee990ef1f","path":"staged-only.txt","sha256":"5c4704ede4d7e6586804965694fc18561a793aea5dc9bce59c4b6b725081d9ec","size":12,"stage":0},{"flags":[],"mode":33188,"oid":"19d9cc8584ac2c7dcf57d2680375e80f099dc481","path":"tracked.txt","sha256":"9ac007af3de930baf647288da0c843b26a5f046a3fe1351f1bb039b242d22cdf","size":7,"stage":0}],"object_format":"sha1","provenance":{"actor":"fixture-actor","disposition":"preserved","predecessor":null,"rationale":"golden fixture"},"schema":"lane-custody-packet/v1","worktree":[{"kind":"directory","mode":null,"oid":null,"path":"empty/nested","sha256":null,"size":null},{"kind":"absent","mode":null,"oid":null,"path":"gone.txt","sha256":null,"size":null},{"kind":"symlink","mode":40960,"oid":"420af0fe53353fa73fd0ca2dea0ea0e7cbfec184","path":"link","sha256":"01b88d89292ebca9477ef5242b9b1d7c61e884b173f4065144b389d3eda44960","size":17},{"kind":"file","mode":33188,"oid":"636316d6157bef6080fd5ff1cb301a75ea5b5065","path":"notes/crlf.txt","sha256":"56f82f1f6e911b711f507016bc5e48eef1fdc7b97e1afb9facb5b98602dfb5c1","size":11},{"kind":"file","mode":33188,"oid":"e2c66c464b2e4de91c9ae64fcedd6db2752d2494","path":"notes/debug.log","sha256":"58eab51b655a15f6ebf823bfc3de1372b44b8040112ca8657c2f0d83aadc7114","size":19},{"kind":"file","mode":33188,"oid":"677db2875fc84ef95de614067156054a438059ef","path":"notes/deep/data.bin","sha256":"4a47144eeaae2690f607ff501ff3b2c73a3fffcfbacdce1b4740270859ac6146","size":10},{"kind":"directory","mode":null,"oid":null,"path":"notes/empty-inner","sha256":null,"size":null},{"kind":"file","mode":33188,"oid":"7329aed2145f024f4ab1b42676d111d5b1deed7b","path":"notes/tail.txt","sha256":"6ac11c4f13237f7a51d7ade32a554f1a4652ac6c006b2f8186d419b86f63e5c6","size":16},{"kind":"file","mode":33261,"oid":"039e4d0069c5c26909f86c505b9de66182e6d1f3","path":"notes/tool","sha256":"306c6ca7407560340797866e077e053627ad409277d1b9da58106fce4cf717cb","size":17},{"kind":"file","mode":33261,"oid":"1a2485251c33a70432394c93fb89330ef214bfc9","path":"run.sh","sha256":"a8076d3d28d21e02012b20eaf7dbf75409a6277134439025f282e368e3305abf","size":10},{"kind":"file","mode":33188,"oid":"564b12f45becba5fb2f70e270af067c1f13b3aab","path":"staged-only.txt","sha256":"8944e854e3f4447241f05a4624cc41c6f9872232251870fc8183743f05d49a2e","size":5},{"kind":"file","mode":33188,"oid":"b8d041e39da713592693a6e2d6af4a02a9c7a265","path":"tracked.txt","sha256":"09bf907bc70edbe94c4a9187d585423efd8a831614145ee1b620ab173a96af75","size":9}]},"packet_id":"c9718c14b0d4798e1c0b10c43c9a5689294bcbfe5c3106b6dea9a0e095113b44"},{"entries":[{"mode":493,"path":".","sha256":null},{"mode":292,"path":"manifest.json","sha256":"c1c929163a41af5ec83955fede89592b44c6a4b8b3d492a200d764c4f0ea55a6"},{"mode":493,"path":"payload","sha256":null},{"mode":292,"path":"payload/01b88d89292ebca9477ef5242b9b1d7c61e884b173f4065144b389d3eda44960","sha256":"01b88d89292ebca9477ef5242b9b1d7c61e884b173f4065144b389d3eda44960"},{"mode":292,"path":"payload/09bf907bc70edbe94c4a9187d585423efd8a831614145ee1b620ab173a96af75","sha256":"09bf907bc70edbe94c4a9187d585423efd8a831614145ee1b620ab173a96af75"},{"mode":292,"path":"payload/306c6ca7407560340797866e077e053627ad409277d1b9da58106fce4cf717cb","sha256":"306c6ca7407560340797866e077e053627ad409277d1b9da58106fce4cf717cb"},{"mode":292,"path":"payload/3428719b7688c78a0cc8ba4b9e80b4e464c815fbccfd4b20695a15ffcefc22af","sha256":"3428719b7688c78a0cc8ba4b9e80b4e464c815fbccfd4b20695a15ffcefc22af"},{"mode":292,"path":"payload/4a47144eeaae2690f607ff501ff3b2c73a3fffcfbacdce1b4740270859ac6146","sha256":"4a47144eeaae2690f607ff501ff3b2c73a3fffcfbacdce1b4740270859ac6146"},{"mode":292,"path":"payload/56f82f1f6e911b711f507016bc5e48eef1fdc7b97e1afb9facb5b98602dfb5c1","sha256":"56f82f1f6e911b711f507016bc5e48eef1fdc7b97e1afb9facb5b98602dfb5c1"},{"mode":292,"path":"payload/58eab51b655a15f6ebf823bfc3de1372b44b8040112ca8657c2f0d83aadc7114","sha256":"58eab51b655a15f6ebf823bfc3de1372b44b8040112ca8657c2f0d83aadc7114"},{"mode":292,"path":"payload/5c4704ede4d7e6586804965694fc18561a793aea5dc9bce59c4b6b725081d9ec","sha256":"5c4704ede4d7e6586804965694fc18561a793aea5dc9bce59c4b6b725081d9ec"},{"mode":292,"path":"payload/6ac11c4f13237f7a51d7ade32a554f1a4652ac6c006b2f8186d419b86f63e5c6","sha256":"6ac11c4f13237f7a51d7ade32a554f1a4652ac6c006b2f8186d419b86f63e5c6"},{"mode":292,"path":"payload/8944e854e3f4447241f05a4624cc41c6f9872232251870fc8183743f05d49a2e","sha256":"8944e854e3f4447241f05a4624cc41c6f9872232251870fc8183743f05d49a2e"},{"mode":292,"path":"payload/9ac007af3de930baf647288da0c843b26a5f046a3fe1351f1bb039b242d22cdf","sha256":"9ac007af3de930baf647288da0c843b26a5f046a3fe1351f1bb039b242d22cdf"},{"mode":292,"path":"payload/a8076d3d28d21e02012b20eaf7dbf75409a6277134439025f282e368e3305abf","sha256":"a8076d3d28d21e02012b20eaf7dbf75409a6277134439025f282e368e3305abf"}],"manifest":{"binding":{"agent_instance_id":"agi-lane-golden","assignment_sha256":"4007adad092eddda39b5d215aa10e3c7fa027720229985ac89a4ce02c443eeef","branch":"lane/lane-golden/agi-lane-golden","common_dir":"/private/tmp/gitsafety-u1-agi-5cce54019abede4b537f2fd71d2452be/golden-before/fixture-repo/.git","lane_name":"lane-golden","repository_id":"rep_fixture","revision_id":"drv_fixture","unit_id":"unt_fixture","unit_key":"unt_fixture-key"},"build_output":[".venv","__pycache__/"],"fingerprint":"a9038c84aa1ec7402e2eb69f67c03a9a921d83b481bf06a14978960b4e2f7bd2","head_commit":"f949430ffbc5ca13215afba82a23f85cd174a691","head_tree":"922002a763607908b02e0396f23fb1d502a72b03","index":[{"flags":[],"mode":33188,"oid":"d5f7fc3f74f7dec08280f370a975b112e8f60818","path":"added.txt","sha256":"3428719b7688c78a0cc8ba4b9e80b4e464c815fbccfd4b20695a15ffcefc22af","size":6,"stage":0},{"flags":[],"mode":33188,"oid":"7d91f6f988b49dd27865a8132bc2f3bee990ef1f","path":"staged-only.txt","sha256":"5c4704ede4d7e6586804965694fc18561a793aea5dc9bce59c4b6b725081d9ec","size":12,"stage":0},{"flags":[],"mode":33188,"oid":"19d9cc8584ac2c7dcf57d2680375e80f099dc481","path":"tracked.txt","sha256":"9ac007af3de930baf647288da0c843b26a5f046a3fe1351f1bb039b242d22cdf","size":7,"stage":0}],"object_format":"sha1","provenance":{"actor":"fixture-actor","disposition":"superseded","predecessor":"c9718c14b0d4798e1c0b10c43c9a5689294bcbfe5c3106b6dea9a0e095113b44","rationale":"golden correction"},"schema":"lane-custody-packet/v1","worktree":[{"kind":"directory","mode":null,"oid":null,"path":"empty/nested","sha256":null,"size":null},{"kind":"absent","mode":null,"oid":null,"path":"gone.txt","sha256":null,"size":null},{"kind":"symlink","mode":40960,"oid":"420af0fe53353fa73fd0ca2dea0ea0e7cbfec184","path":"link","sha256":"01b88d89292ebca9477ef5242b9b1d7c61e884b173f4065144b389d3eda44960","size":17},{"kind":"file","mode":33188,"oid":"636316d6157bef6080fd5ff1cb301a75ea5b5065","path":"notes/crlf.txt","sha256":"56f82f1f6e911b711f507016bc5e48eef1fdc7b97e1afb9facb5b98602dfb5c1","size":11},{"kind":"file","mode":33188,"oid":"e2c66c464b2e4de91c9ae64fcedd6db2752d2494","path":"notes/debug.log","sha256":"58eab51b655a15f6ebf823bfc3de1372b44b8040112ca8657c2f0d83aadc7114","size":19},{"kind":"file","mode":33188,"oid":"677db2875fc84ef95de614067156054a438059ef","path":"notes/deep/data.bin","sha256":"4a47144eeaae2690f607ff501ff3b2c73a3fffcfbacdce1b4740270859ac6146","size":10},{"kind":"directory","mode":null,"oid":null,"path":"notes/empty-inner","sha256":null,"size":null},{"kind":"file","mode":33188,"oid":"7329aed2145f024f4ab1b42676d111d5b1deed7b","path":"notes/tail.txt","sha256":"6ac11c4f13237f7a51d7ade32a554f1a4652ac6c006b2f8186d419b86f63e5c6","size":16},{"kind":"file","mode":33261,"oid":"039e4d0069c5c26909f86c505b9de66182e6d1f3","path":"notes/tool","sha256":"306c6ca7407560340797866e077e053627ad409277d1b9da58106fce4cf717cb","size":17},{"kind":"file","mode":33261,"oid":"1a2485251c33a70432394c93fb89330ef214bfc9","path":"run.sh","sha256":"a8076d3d28d21e02012b20eaf7dbf75409a6277134439025f282e368e3305abf","size":10},{"kind":"file","mode":33188,"oid":"564b12f45becba5fb2f70e270af067c1f13b3aab","path":"staged-only.txt","sha256":"8944e854e3f4447241f05a4624cc41c6f9872232251870fc8183743f05d49a2e","size":5},{"kind":"file","mode":33188,"oid":"b8d041e39da713592693a6e2d6af4a02a9c7a265","path":"tracked.txt","sha256":"09bf907bc70edbe94c4a9187d585423efd8a831614145ee1b620ab173a96af75","size":9}]},"packet_id":"c1c929163a41af5ec83955fede89592b44c6a4b8b3d492a200d764c4f0ea55a6"}]'
)


if __name__ == "__main__":
    unittest.main(verbosity=2)
