#!/usr/bin/env python3
"""Knowledge chunks are embedded without their indexer preamble; stored text is untouched.

The indexer prepends ``Source:`` / ``Article Layer:`` / ``Article Role:`` /
``Article Tags:`` lines to every chunk (about 150 characters, identical across
articles). They dilute a short chunk's embedding, so the backend sends the
embedding model the text without that leading block, ONLY for memories tagged
``knowledge:official``. Agent memories, query embeddings and chunks without a
preamble are embedded exactly as stored, and the stored content never changes.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[3]
for source in ("ananta/src", "plugins/actr_memory_plugin/src"):
    sys.path.insert(0, str(ROOT / source))

from actr_memory_plugin import backend  # noqa: E402

KNOWLEDGE = backend.KNOWLEDGE_OFFICIAL_TAG
PREAMBLE = "Source: kb — Title\nArticle Layer: 2\nArticle Role: guide\nArticle Tags: a, b"
BODY = "first body line\nSource: later line stays\nArticle Tags: later line stays\nlast line"
CHUNK = f"{PREAMBLE}\n{BODY}"
OK = {"action_status": "completed", "data": {"result": {"embeddings": [[1.0]]}}}


def make_backend() -> tuple[backend.ACTRMemoryBackend, list[str]]:
    service = backend.ACTRMemoryBackend.__new__(backend.ACTRMemoryBackend)
    service.actr_enabled = True
    sent: list[str] = []

    def generate(*, inputs: list[str]) -> dict[str, Any]:
        sent.extend(inputs)
        return OK

    service.embedding_service = Mock()
    service.embedding_service.generate_embeddings.side_effect = generate
    service.embedding_service.input_token_budget.return_value = None  # no declared budget: the whole text is one input
    service.vector_service = Mock()
    service.state_service = Mock()
    return service, sent


class RememberTests(unittest.TestCase):
    def remember(self, content: str, tags: list[str]) -> tuple[list[str], dict[str, Any]]:
        service, sent = make_backend()
        with patch.object(backend, "save_memory", return_value="mid") as save, patch.object(backend, "store_memory_vector"):
            service.remember(content=content, tags=tags)
        return sent, save.call_args.args[1]

    def test_knowledge_chunk_embeds_without_preamble_and_stores_every_line(self) -> None:
        sent, stored = self.remember(CHUNK, [KNOWLEDGE, "kb:x"])
        self.assertEqual(sent, [BODY])
        self.assertEqual(stored["content"], CHUNK)
        self.assertEqual(CHUNK, f"{PREAMBLE}\n{sent[0]}")

    def test_agent_memory_starting_with_source_is_not_stripped(self) -> None:
        text = "Source: my notes\nArticle Tags: x\nremember this"
        sent, stored = self.remember(text, ["agent_memory"])
        self.assertEqual(sent, [text])
        self.assertEqual(stored["content"], text)

    def test_knowledge_chunk_without_preamble_is_unchanged(self) -> None:
        sent, _ = self.remember(BODY, [KNOWLEDGE])
        self.assertEqual(sent, [BODY])

    def test_knowledge_chunk_that_is_only_preamble_embeds_as_stored(self) -> None:
        sent, _ = self.remember(PREAMBLE, [KNOWLEDGE])
        self.assertEqual(sent, [PREAMBLE])


class OtherEmbeddingPathTests(unittest.TestCase):
    def test_query_embedding_is_unchanged(self) -> None:
        service, sent = make_backend()
        query = f"{PREAMBLE}\nwhat is this"
        with patch.object(service, "_get_recall_matches", return_value=[]), patch.object(service, "_process_recall_matches", return_value=[]):
            service.recall(query=query, score_by_similarity=True)
        self.assertEqual(sent, [query])

    def test_orphan_reindex_strips_knowledge_only_and_leaves_stored_rows_alone(self) -> None:
        service, sent = make_backend()
        memories = [
            {"id": "kb", "content": CHUNK, "tags": [KNOWLEDGE], "is_deleted": 0},
            {"id": "agent", "content": CHUNK, "tags": ["agent_memory"], "is_deleted": 0},
        ]
        with (
            patch.object(backend, "get_all_memories", return_value=memories),
            patch.object(service, "_memory_ids_without_vector", return_value=["kb", "agent"]),
            patch.object(service, "_replace_memory_vectors"),
        ):
            result = service.reindex_orphaned_vectors()
        self.assertEqual(result["data"]["reindex_count"], 2)
        self.assertEqual(sent, [BODY, CHUNK])
        self.assertEqual([m["content"] for m in memories], [CHUNK, CHUNK])
        self.assertEqual(service.state_service.method_calls, [])

    def test_import_strips_knowledge_only(self) -> None:
        service, sent = make_backend()
        records = [
            {"id": "kb", "content": CHUNK, "tags": [KNOWLEDGE]},
            {"id": "agent", "content": CHUNK, "tags": ["agent_memory"]},
            {"id": "bare", "content": CHUNK},
        ]
        with patch.object(backend, "save_memory") as save:
            self.assertTrue(all(service._import_single_memory(record) for record in records))
        self.assertEqual(sent, [BODY, CHUNK, CHUNK])
        self.assertEqual([call.args[1]["content"] for call in save.call_args_list], [CHUNK, CHUNK, CHUNK])


if __name__ == "__main__":
    print("backend module:", backend.__file__, flush=True)
    unittest.main(verbosity=2)
