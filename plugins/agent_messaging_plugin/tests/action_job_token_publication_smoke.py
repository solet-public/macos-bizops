"""A queued action must publish its flow token before a poller can claim it."""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ananta.core.state.execution_token_context import action_execution_context  # noqa: E402
from ananta.core.state.registered_job_operations import registered_action_context  # noqa: E402
from ananta.error_handling import FrameworkError  # noqa: E402
from bridge_wait_fixture_support import DispatchRuntime, job_row  # noqa: E402


class ActionJobTokenPublicationTests(unittest.TestCase):
    def test_registered_job_without_token_fails_before_persistence(self) -> None:
        runtime = DispatchRuntime()
        with registered_action_context("ae-missing-token", "flow-missing-token", "sess-missing-token"):
            with self.assertRaisesRegex(FrameworkError, "ae-missing-token"):
                runtime.manager.submit_registered_operation("embedding_service", "generate_embeddings", {}, lambda _args: {})
        self.assertEqual(runtime.state.rows.get("job", []), [])

    def test_ordinary_job_without_token_remains_valid(self) -> None:
        runtime = DispatchRuntime()
        receipt = runtime.manager.create_job("ordinary", "work", {"notes": "ordinary job"})
        job = job_row(runtime.manager, cast(str, cast(dict[str, object], receipt["data"])["job_id"]))
        self.assertNotIn("flow_token_id", job)

    def test_queued_action_cannot_spawn_tokenless_registered_job(self) -> None:
        runtime = DispatchRuntime()
        flow_id = runtime.flow_manager.create_flow("sess-token-publication", "bridge_process_call", {})
        recorder = runtime.factory.action_event_recorder
        assert recorder is not None
        original_write = runtime.state.write_state
        early_jobs: list[dict[str, object]] = []

        def create_from_worker(row: dict[str, object]) -> dict[str, object]:
            receipts: list[dict[str, object]] = []

            def run() -> None:
                with (
                    action_execution_context(cast(str | None, row.get("flow_token_id"))),
                    registered_action_context(cast(str, row["id"]), flow_id, "sess-token-publication"),
                ):
                    receipts.append(runtime.manager.submit_registered_operation("embedding_service", "generate_embeddings", {}, lambda _args: {}))

            worker = threading.Thread(target=run, name="poller-without-context-copy")
            worker.start()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            return receipts[0]

        def observe_write(namespace: str, data: dict[str, Any]) -> dict[str, Any]:
            result = original_write(namespace, data)
            if data.get("table") == "action_events":
                row = runtime.state.rows["action_events"][-1]
                if row["status"] == "queued":
                    early_jobs.append(create_from_worker(row))
            return result

        with patch.object(runtime.state, "write_state", side_effect=observe_write):
            action_id = recorder.store_action_event(
                {
                    "flow_id": flow_id,
                    "session_id": "sess-token-publication",
                    "name": "token-publication",
                    "process_key": "service_interface::embedding_service::generate_embeddings",
                    "notes": "Publish a complete action before poller admission",
                    "parameters": {"inputs": ["unit"]},
                }
            )

        row = runtime.state.rows["action_events"][-1]
        self.assertEqual(row["id"], action_id)
        self.assertEqual(row["status"], "queued")
        self.assertEqual(early_jobs, [], "a poller must never see a queued action before its token is published")
        receipt = create_from_worker(row)
        job = job_row(runtime.manager, cast(str, cast(dict[str, object], receipt["data"])["job_id"]))
        self.assertEqual(job["flow_token_id"], row["flow_token_id"])
        self.assertEqual(job["group_id"], f"registered:{action_id}")


if __name__ == "__main__":
    unittest.main()
