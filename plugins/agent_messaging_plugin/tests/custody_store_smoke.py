"""Public-entry-point fault proofs for the shared, materialized custody store."""

from __future__ import annotations

import errno
import hashlib
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
        source = self.root / "source"
        source.write_bytes(DATA)
        (self.root / "link").symlink_to("source")
        entry = store.read_entry(self.root, "link", object_format="sha1", digest=True)
        self.assertEqual((entry.mode, entry.oid), (store.SYMLINK_MODE, store.blob_oid(b"source", "sha1")))
        self.assertEqual(store.read_entry(self.root, "../source", object_format="sha1", digest=True), store.UNREADABLE_ENTRY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
