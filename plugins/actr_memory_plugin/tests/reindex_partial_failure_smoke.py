#!/usr/bin/env python3
"""Pure real-backend reindex: per-record refusal, no lost text or rebuild loop."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[3]
for source in ("ananta/src", "plugins/actr_memory_plugin/src"):
    sys.path.insert(0, str(ROOT / source))

from actr_memory_plugin import backend  # noqa: E402
from ananta.error_handling import FrameworkError  # noqa: E402
from ananta.interfaces.embedding_service_interface import TokenBudget  # noqa: E402


class ReindexTests(unittest.TestCase):
    def make_backend(self) -> backend.ACTRMemoryBackend:
        service = backend.ACTRMemoryBackend.__new__(backend.ACTRMemoryBackend)
        service.actr_enabled = True
        service.embedding_service = Mock()
        service.embedding_service.input_token_budget.return_value = TokenBudget(2048, len)
        service.vector_service = Mock()
        return service

    def test_fit_indexed_oversize_reported_exactly_without_truncation(self) -> None:
        service = self.make_backend()
        text = "x" * 3074
        rows = [{"id": "large", "content": text}, {"id": "fit", "content": "fits"}]
        with patch.object(service, "_find_orphaned_memories", return_value=rows), \
             patch.object(service, "_generate_embedding", return_value=[1.0]) as generate, \
             patch.object(backend, "store_memory_vector") as store, \
             self.assertLogs(backend.logger, level="ERROR") as logs:
            result = service.reindex_orphaned_vectors()
        generate.assert_called_once_with("fits")
        self.assertEqual(store.call_count, 1)
        self.assertEqual(store.call_args.args[1], "fit")
        self.assertEqual(result["data"]["reindex_count"], 1)
        failed = result["data"]["failed_records"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["memory_id"], "large")
        self.assertEqual(failed[0]["token_count"], 3074)
        self.assertIn("2048", failed[0]["error"])
        self.assertIn("large", " ".join(logs.output))
        self.assertIn("3074", " ".join(logs.output))
        self.assertEqual(rows[0]["content"], text)

    def test_generation_and_storage_errors_do_not_abort_next_record(self) -> None:
        for stage in ("generate", "store"):
            with self.subTest(stage=stage):
                service = self.make_backend()
                rows = [{"id": "bad", "content": "bad"}, {"id": "good", "content": "good"}]
                error = RuntimeError("injected " + stage)
                with patch.object(service, "_find_orphaned_memories", return_value=rows), \
                     patch.object(service, "_generate_embedding",
                                  side_effect=[error, [1.0]] if stage == "generate" else [[1.0], [1.0]]), \
                     patch.object(backend, "store_memory_vector",
                                  side_effect=[error, None] if stage == "store" else None), \
                     self.assertLogs(backend.logger, level="ERROR"):
                    data = service.reindex_orphaned_vectors()["data"]
                self.assertEqual(data["reindex_count"], 1)
                self.assertEqual(data["failed_records"][0]["memory_id"], "bad")
                self.assertEqual(data["failed_records"][0]["error"], str(error))

    def test_rebuild_partial_failure_stops_loudly_not_forever(self) -> None:
        service = self.make_backend()
        with patch.object(service, "_find_orphaned_memories", return_value=[
            {"id": "large", "content": "x" * 3074},
        ]) as find, self.assertLogs(backend.logger, level="ERROR"), \
             self.assertRaises(FrameworkError):
            service._rebuild_all_vectors()
        find.assert_called_once()

    def test_success_keeps_count_and_empty_reports(self) -> None:
        service = self.make_backend()
        with patch.object(service, "_find_orphaned_memories", return_value=[{"id": "fit", "content": "fits"}]), \
             patch.object(service, "_generate_embedding", return_value=[1.0]), \
             patch.object(backend, "store_memory_vector"):
            self.assertEqual(service.reindex_orphaned_vectors()["data"], {
                "reindex_count": 1, "failed_records": [],
            })


if __name__ == "__main__":
    print("reindex module:", backend.__file__, flush=True)
    unittest.main(verbosity=2)
