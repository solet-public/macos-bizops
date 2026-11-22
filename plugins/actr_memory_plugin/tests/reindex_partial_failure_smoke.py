#!/usr/bin/env python3
"""Pure real-backend smoke for the memory reindex and for budgeted segmentation.

``ReindexTests`` pin the per-record reindex contract: an oversize memory is indexed in segments, a failing
record does not abort the next, and a failing rebuild stops loudly instead of looping.

``SegmentationTests`` pin the behavior a memory over the embedding budget needs end to end. The provider
budget comes from ``embedding_service.input_token_budget()`` (here a word counter, no network). A memory
that fits keeps exactly one vector under its own id; one that does not is split into windows that each fit,
every window is embedded, and each is stored under ``<memory id>:<index>`` (index 0 is the memory id itself,
written last so it marks the memory complete). Search maps any segment hit back to its memory once,
coverage needs every segment, and every delete path removes every segment. They use only the backend's
existing public surface and literal vector ids, so they run (and fail) the same way on a tree without
segmentation.
"""

from __future__ import annotations

import math
import sys
import unittest
import zlib
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[3]
for source in ("ananta/src", "plugins/actr_memory_plugin/src"):
    sys.path.insert(0, str(ROOT / source))

from actr_memory_plugin import backend  # noqa: E402
from ananta.error_handling import FrameworkError  # noqa: E402
from ananta.interfaces.embedding_service_interface import TokenBudget  # noqa: E402

BUDGET_WORDS = 40
DIMENSION = 256
# "common" opens every ten-word group, so every segment of the big memory matches a query for it.
BIG_TEXT = " ".join("common" if index % 10 == 0 else f"w{index:03d}" for index in range(130))
SHORT_TEXT = "a short note about segments"
OK = "completed"


class ReindexTests(unittest.TestCase):
    def make_backend(self) -> backend.ACTRMemoryBackend:
        service = backend.ACTRMemoryBackend.__new__(backend.ACTRMemoryBackend)
        service.actr_enabled = True
        service.embedding_service = Mock()
        service.embedding_service.input_token_budget.return_value = TokenBudget(2048, len)
        service.vector_service = Mock()
        return service

    def test_oversize_memory_is_indexed_in_budgeted_segments_without_truncation(self) -> None:
        service = self.make_backend()
        text = "x" * 3074
        rows = [{"id": "large", "content": text}, {"id": "fit", "content": "fits"}]
        with patch.object(service, "_find_orphaned_memories", return_value=rows), patch.object(service, "_generate_embedding", return_value=[1.0]) as generate, patch.object(service, "_replace_memory_vectors") as replace:
            result = service.reindex_orphaned_vectors()
        self.assertEqual(result["data"], {"reindex_count": 2, "failed_records": []})
        sent = [call.args[0] for call in generate.call_args_list]
        self.assertEqual(len(sent), 3)
        self.assertTrue(all(len(window) <= 2048 for window in sent[:2]))
        self.assertEqual("".join(sent[:2]), text, "the whole text is embedded, none of it dropped")
        self.assertEqual(sent[2], "fits")
        self.assertEqual([call.args[0] for call in replace.call_args_list], ["large", "fit"])
        self.assertEqual(len(replace.call_args_list[0].args[1]), 2, "one embedding per segment")
        self.assertEqual(len(replace.call_args_list[1].args[1]), 1, "a memory that fits has one")

    def test_generation_and_storage_errors_do_not_abort_next_record(self) -> None:
        for stage in ("generate", "store"):
            with self.subTest(stage=stage):
                service = self.make_backend()
                rows = [{"id": "bad", "content": "bad"}, {"id": "good", "content": "good"}]
                error = RuntimeError("injected " + stage)
                with (
                    patch.object(service, "_find_orphaned_memories", return_value=rows),
                    patch.object(service, "_generate_embedding", side_effect=[error, [1.0]] if stage == "generate" else [[1.0], [1.0]]),
                    patch.object(service, "_replace_memory_vectors", side_effect=[error, None] if stage == "store" else None),
                    self.assertLogs(backend.logger, level="ERROR"),
                ):
                    data = service.reindex_orphaned_vectors()["data"]
                self.assertEqual(data["reindex_count"], 1)
                self.assertEqual(data["failed_records"][0]["memory_id"], "bad")
                self.assertEqual(data["failed_records"][0]["error"], str(error))

    def test_rebuild_partial_failure_stops_loudly_not_forever(self) -> None:
        service = self.make_backend()
        with (
            patch.object(service, "_find_orphaned_memories", return_value=[{"id": "bad", "content": "bad"}]) as find,
            patch.object(service, "_generate_embedding", side_effect=RuntimeError("injected")),
            self.assertLogs(backend.logger, level="ERROR"),
            self.assertRaises(FrameworkError),
        ):
            service._rebuild_all_vectors()
        find.assert_called_once()

    def test_success_keeps_count_and_empty_reports(self) -> None:
        service = self.make_backend()
        with patch.object(service, "_find_orphaned_memories", return_value=[{"id": "fit", "content": "fits"}]), patch.object(service, "_generate_embedding", return_value=[1.0]), patch.object(service, "_replace_memory_vectors"):
            self.assertEqual(
                service.reindex_orphaned_vectors()["data"],
                {
                    "reindex_count": 1,
                    "failed_records": [],
                },
            )


def _word_vector(text: str) -> list[float]:
    vector = [0.0] * DIMENSION
    for word in text.split():
        vector[zlib.crc32(word.encode()) % DIMENSION] += 1.0
    return vector


def _cosine_distance(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return 1.0 - (dot / norm if norm else 0.0)


class FakeEmbedder:
    """Counts in words and, like the real provider, refuses any input over its budget."""

    def __init__(self) -> None:
        self.inputs: list[str] = []
        self.budget = TokenBudget(BUDGET_WORDS, lambda text: len(text.split()))

    def input_token_budget(self) -> TokenBudget:
        return self.budget

    def generate_embeddings(self, inputs: list[str], **_options: Any) -> dict[str, Any]:
        for text in inputs:
            if not self.budget.fits(text):
                return {"action_status": "error", "error": {"message": f"input over budget: {self.budget.count(text)} words"}}
        self.inputs.extend(inputs)
        return {"action_status": OK, "data": {"result": {"embeddings": [_word_vector(text) for text in inputs]}}}


class FakeVectors:
    """The vector verbs the memory plugin calls, with the real store's unique external_id."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.last_hits: list[str] = []

    def ids(self) -> list[str]:
        return sorted(self.rows)

    def store_vectors(self, namespace: str, vectors: list[dict[str, Any]]) -> dict[str, Any]:
        del namespace
        taken = [str(v["external_id"]) for v in vectors if str(v["external_id"]) in self.rows]
        if taken:
            return {"action_status": "error", "data": None, "error": {"message": f"duplicate external_id {taken}"}}
        for vector in vectors:
            self.rows[str(vector["external_id"])] = vector
        return {"action_status": OK, "data": {"result": {"count": len(vectors)}}, "error": None}

    def find_missing_external_ids(self, namespace: str, candidate_external_ids: list[str]) -> dict[str, Any]:
        del namespace
        missing = [key for key in candidate_external_ids if key not in self.rows]
        return {"action_status": OK, "data": {"result": {"missing": missing}}, "error": None}

    def delete_by_external_ids(self, namespace: str, external_ids: list[str]) -> dict[str, Any]:
        del namespace
        removed = [key for key in external_ids if self.rows.pop(key, None) is not None]
        return {"action_status": OK, "data": {"result": {"deleted_count": len(removed)}}, "error": None}

    def search_similar(self, namespace: str, query_vector: list[float], top_k: int = 10, filters: dict[str, Any] | None = None, **_options: Any) -> dict[str, Any]:
        del namespace
        eligible = [(key, row) for key, row in self.rows.items() if all(row["metadata"].get(name) == value for name, value in (filters or {}).items())]
        ranked = sorted(((_cosine_distance(query_vector, row["vector"]), key) for key, row in eligible))[:top_k]
        self.last_hits = [key for _, key in ranked]
        results = [{"external_id": key, "distance": distance} for distance, key in ranked]
        return {"action_status": OK, "data": {"result": {"results": results}}, "error": None}


def _memory(memory_id: str, content: str) -> dict[str, Any]:
    return {"id": memory_id, "content": content, "tags": [], "status": "active", "memory_type": "episodic", "is_deleted": 0, "created_at": "2026-11-12T00:00:00+00:00"}


class SegmentationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.memories: dict[str, dict[str, Any]] = {}
        self.embedder = FakeEmbedder()
        self.vectors = FakeVectors()
        service = backend.ACTRMemoryBackend.__new__(backend.ACTRMemoryBackend)
        service.actr_enabled = True
        service.embedding_service = self.embedder
        service.vector_service = self.vectors
        service.state_service = Mock()
        self.service = service
        patches = (
            patch.object(backend, "get_all_memories", side_effect=lambda *_a, **_k: list(self.memories.values())),
            patch.object(backend, "get_memory", side_effect=lambda _state, memory_id: self.memories.get(memory_id)),
            patch.object(backend, "save_memory", side_effect=lambda _state, memory: memory["id"]),
            patch.object(backend, "delete_memory_records", side_effect=lambda _state, ids: len(ids)),
            patch.object(backend, "find_memories_by_tag", side_effect=lambda _state, _tag: list(self.memories.values())),
        )
        for each in patches:
            each.start()
            self.addCleanup(each.stop)

    def add(self, memory_id: str, content: str) -> None:
        self.memories[memory_id] = _memory(memory_id, content)

    def reindex(self) -> dict[str, Any]:
        data: dict[str, Any] = self.service.reindex_orphaned_vectors()["data"]
        return data

    def seed_segments(self, memory_id: str, count: int) -> None:
        for index in range(count):
            external_id = memory_id if index == 0 else f"{memory_id}:{index}"
            self.vectors.rows[external_id] = {"external_id": external_id, "vector": [1.0], "metadata": {"status": "active"}}

    def test_oversize_memory_is_indexed_in_budgeted_segments_without_loss(self) -> None:
        self.add("mem-big", BIG_TEXT)
        data = self.reindex()
        self.assertEqual(data["failed_records"], [])
        self.assertEqual(data["reindex_count"], 1)
        self.assertEqual(self.vectors.ids(), ["mem-big", "mem-big:1", "mem-big:2", "mem-big:3"])
        self.assertEqual(len(self.embedder.inputs), 4)
        self.assertTrue(all(self.embedder.budget.fits(text) for text in self.embedder.inputs))
        self.assertEqual("".join(self.embedder.inputs), BIG_TEXT, "every character is embedded, none truncated")

    def test_short_memory_keeps_exactly_one_vector_under_its_own_id(self) -> None:
        self.add("mem-short", SHORT_TEXT)
        data = self.reindex()
        self.assertEqual(data, {"reindex_count": 1, "failed_records": []})
        self.assertEqual(self.vectors.ids(), ["mem-short"])
        self.assertEqual(self.embedder.inputs, [SHORT_TEXT])

    def test_covered_memories_are_not_reembedded(self) -> None:
        self.add("mem-big", BIG_TEXT)
        self.add("mem-short", SHORT_TEXT)
        self.reindex()
        embedded = len(self.embedder.inputs)
        self.assertEqual(self.service._find_orphaned_memories(), [])
        self.assertEqual(self.service.reindex_orphaned_vectors()["data"]["message"], "No orphaned memories")
        self.assertEqual(len(self.embedder.inputs), embedded)

    def test_a_memory_missing_any_one_segment_is_reported_and_repaired(self) -> None:
        self.add("mem-big", BIG_TEXT)
        self.reindex()
        for lost in ("mem-big:2", "mem-big:3", "mem-big"):
            with self.subTest(lost=lost):
                del self.vectors.rows[lost]
                self.assertEqual([row["id"] for row in self.service._find_orphaned_memories()], ["mem-big"])
                data = self.reindex()
                self.assertEqual(data["failed_records"], [])
                self.assertEqual(self.vectors.ids(), ["mem-big", "mem-big:1", "mem-big:2", "mem-big:3"])
                self.assertEqual(self.service._find_orphaned_memories(), [])

    def test_search_by_tail_text_returns_the_memory_once(self) -> None:
        self.add("mem-big", BIG_TEXT)
        self.add("mem-short", SHORT_TEXT)
        self.reindex()
        tail = [m["id"] for m in self.service.recall("w125 w126 w127", top_k=5, score_by_similarity=True)["memories"]]
        self.assertEqual(tail[0], "mem-big", "text from the tail finds the memory first")
        self.assertEqual(tail.count("mem-big"), 1)
        shared = [m["id"] for m in self.service.recall("common", top_k=5, score_by_similarity=True)["memories"]]
        segment_hits = [hit for hit in self.vectors.last_hits if hit == "mem-big" or hit.startswith("mem-big:")]
        self.assertGreater(len(segment_hits), 1, "the query matched several segments of the same memory")
        self.assertEqual(shared.count("mem-big"), 1, "several segment hits come back as one result")

    def test_every_delete_path_removes_every_segment_and_only_that_memorys(self) -> None:
        for name, call in (
            ("forget", lambda: self.service.forget("mem-big")),
            ("delete_memory", lambda: self.service.delete_memory("mem-big")),
            ("delete_memories_by_ids", lambda: self.service.delete_memories_by_ids(["mem-big"])),
            ("delete_memories_by_tag", lambda: self.service.delete_memories_by_tag("slot")),
        ):
            with self.subTest(path=name):
                self.memories = {"mem-big": _memory("mem-big", BIG_TEXT)}
                self.vectors.rows.clear()
                self.seed_segments("mem-big", 4)
                self.seed_segments("mem-b", 1)  # a lookalike prefix: "mem-b" is not "mem-big"
                self.seed_segments("mem-big2", 2)  # and so is "mem-big2:1"
                call()
                self.assertEqual(self.vectors.ids(), ["mem-b", "mem-big2", "mem-big2:1"])

    def test_a_failed_segment_lookup_fails_the_delete_loudly_with_a_complete_envelope(self) -> None:
        self.memories = {"mem-big": _memory("mem-big", BIG_TEXT)}
        self.seed_segments("mem-big", 4)
        with patch.object(self.vectors, "find_missing_external_ids", return_value={"action_status": "error", "data": None, "error": "lookup down"}):
            with self.assertRaises(FrameworkError) as raised, self.assertLogs("actr_memory_plugin.vector_segments", level="ERROR"):
                self.service.delete_memory("mem-big")
            with patch.object(backend.logger, "warning") as warned, self.assertLogs("actr_memory_plugin.vector_segments", level="ERROR"):
                with patch.object(self.service, "remember", return_value={"memory_id": "mem-new"}):
                    self.service.upsert_memory_by_tag("replacement", "slot")
        self.assertIn("lookup down", str(raised.exception))
        self.assertTrue(warned.called, "upsert keeps its policy: a failed old-vector delete is a warning, not an abort")
        self.assertEqual(self.vectors.ids(), ["mem-big", "mem-big:1", "mem-big:2", "mem-big:3"], "nothing is deleted when the segments cannot be listed")

    def test_upsert_by_tag_removes_every_segment_of_the_replaced_memory(self) -> None:
        self.memories = {"mem-big": _memory("mem-big", BIG_TEXT)}
        self.seed_segments("mem-big", 4)
        with patch.object(self.service, "remember", return_value={"memory_id": "mem-new"}):
            self.service.upsert_memory_by_tag("replacement", "slot")
        self.assertEqual(self.vectors.ids(), [])

    def test_rebuild_after_a_namespace_clear_terminates_with_a_segmented_memory(self) -> None:
        self.add("mem-big", BIG_TEXT)
        self.add("mem-short", SHORT_TEXT)
        self.assertEqual(self.service._rebuild_all_vectors(), 2)
        self.assertEqual(self.vectors.ids(), ["mem-big", "mem-big:1", "mem-big:2", "mem-big:3", "mem-short"])

    def test_import_stores_an_oversize_memory_in_segments(self) -> None:
        self.assertTrue(self.service._import_single_memory(_memory("mem-big", BIG_TEXT)))
        self.assertEqual(self.vectors.ids(), ["mem-big", "mem-big:1", "mem-big:2", "mem-big:3"])


if __name__ == "__main__":
    print("reindex module:", backend.__file__, flush=True)
    unittest.main(verbosity=2)
