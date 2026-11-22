#!/usr/bin/env python3
"""Smoke pin: a Claude Code session's project_path comes from the transcript cwd.

Problem ``prb_a1d9f5db``: the source plugin rebuilt ``project_path`` from the
encoded project-directory name (``name[1:].replace('-', '/')``), which turns a
real hyphen into a slash and cannot recover a dot. The transcript records the
real ``cwd`` on its lines; the decode is now the flagged last resort.

Fixture: cwd ``/Users/alice/my-repo.v2`` inside a project directory named
``-Users-alice-my-repo.v2`` (the lossy decode would give
``/Users/alice/my/repo.v2``).

* T1 vendor capture: first-seen cwd wins, even behind metadata-type lines.
* T2 vendor defaults: no usable cwd, or a missing file, gives ``cwd is None``.
* T3 discovery path: ``discover_sessions`` yields the transcript cwd.
* T4 poll path: ``_poll_ref`` yields the transcript cwd.
* T5 flagged fallback: no cwd gives the decode AND exactly one WARNING naming
  the session. The ref is still produced; nothing is excluded.

No database, no live filesystem state; every fixture lives in a tempdir.

Run::

    .venv/bin/python3 plugins/claude_code_filesystem_session_source_plugin/tests/claude_code_session_cwd_smoke.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0,
    str(REPO_ROOT / "plugins" / "claude_code_filesystem_session_source_plugin" / "src"),
)

from ananta.llm.session_ledger.vendor import claude_code as claude_code_vendor  # noqa: E402

_SESSION_ID = "471a05a5-d18c-4dc0-87c3-e96a02b852ff"
_PROJECT_DIR_NAME = "-Users-alice-my-repo.v2"
_REAL_CWD = "/Users/alice/my-repo.v2"
_LOSSY_DECODE = "/Users/alice/my/repo.v2"
_PLUGIN_LOGGER = "claude_code_filesystem_session_source_plugin.plugin"


def _write_fixture(path: Path, lines: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for entry in lines:
            fh.write(json.dumps(entry) + "\n")


def _user_line(uuid: str, cwd: str | None) -> dict[str, object]:
    line: dict[str, object] = {
        "type": "user",
        "uuid": uuid,
        "timestamp": "2026-11-21T17:00:00.000Z",
        "sessionId": _SESSION_ID,
        "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]},
    }
    if cwd is not None:
        line["cwd"] = cwd
    return line


def _lines_with_cwd() -> list[dict[str, object]]:
    return [
        {"type": "agent-name", "agentName": "Claude-A", "sessionId": _SESSION_ID},
        {"type": "custom-title", "customTitle": "A title", "sessionId": _SESSION_ID},
        _user_line("evt-1", _REAL_CWD),
        # A later, different cwd (the session moved): first-seen must win.
        _user_line("evt-2", "/Users/alice/elsewhere"),
    ]


def _lines_without_cwd() -> list[dict[str, object]]:
    return [
        {"type": "agent-name", "agentName": "Claude-A", "sessionId": _SESSION_ID},
        _user_line("evt-1", None),
    ]


class _StubConfigProvider:
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def get(self, name: str, default: object | None = None) -> object:
        return self._values.get(name, default)


def _make_plugin() -> object:
    from claude_code_filesystem_session_source_plugin.plugin import (  # noqa: PLC0415
        ClaudeCodeFilesystemSessionSourcePlugin,
    )

    plugin = ClaudeCodeFilesystemSessionSourcePlugin()
    plugin.set_config_provider(_StubConfigProvider({"glob": "*.jsonl"}))  # type: ignore[arg-type]
    plugin.prepare_for_readiness()
    return plugin


def _project_with_rollout(root: Path, lines: list[dict[str, object]]) -> Path:
    project_dir = root / _PROJECT_DIR_NAME
    project_dir.mkdir()
    rollout = project_dir / f"{_SESSION_ID}.jsonl"
    _write_fixture(rollout, lines)
    return rollout


class SessionCwdSmoke(unittest.TestCase):
    def test_t1_vendor_captures_first_seen_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"{_SESSION_ID}.jsonl"
            _write_fixture(path, _lines_with_cwd())
            metadata = claude_code_vendor.read_session_metadata(path)
        self.assertEqual(metadata.cwd, _REAL_CWD)
        # The existing last-write-wins fields are untouched by the new pass.
        self.assertEqual(metadata.agent_name, "Claude-A")
        self.assertEqual(metadata.custom_title, "A title")

    def test_t2_vendor_defaults_to_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            no_cwd = Path(tmp) / "no-cwd.jsonl"
            _write_fixture(no_cwd, _lines_without_cwd())
            unusable = Path(tmp) / "unusable-cwd.jsonl"
            _write_fixture(
                unusable,
                [_user_line("evt-1", ""), _user_line("evt-2", "relative/path")],
            )
            self.assertIsNone(claude_code_vendor.read_session_metadata(no_cwd).cwd)
            self.assertIsNone(claude_code_vendor.read_session_metadata(unusable).cwd)
            missing = claude_code_vendor.read_session_metadata(Path(tmp) / "absent.jsonl")
        self.assertIsNone(missing.cwd)

    def test_t3_discovery_path_uses_transcript_cwd(self) -> None:
        plugin = _make_plugin()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _project_with_rollout(root, _lines_with_cwd())
            with self.assertNoLogs(_PLUGIN_LOGGER, level="WARNING"):
                refs = list(plugin.discover_sessions(str(root), None))  # type: ignore[attr-defined]
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].project_path, _REAL_CWD)

    def test_t4_poll_path_uses_transcript_cwd(self) -> None:
        from claude_code_filesystem_session_source_plugin.plugin import (  # noqa: PLC0415
            observe_candidate,
        )

        plugin = _make_plugin()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rollout = _project_with_rollout(root, _lines_with_cwd())
            candidate = observe_candidate(
                rollout,
                external_id=_SESSION_ID,
                relative_path=rollout.relative_to(root).as_posix(),
                json_lines=True,
            )
            self.assertIsNotNone(candidate)
            with self.assertNoLogs(_PLUGIN_LOGGER, level="WARNING"):
                ref = plugin._poll_ref(str(root), candidate)  # type: ignore[attr-defined]
        self.assertEqual(ref.project_path, _REAL_CWD)

    def test_t5_no_cwd_falls_back_to_decode_with_one_warning(self) -> None:
        plugin = _make_plugin()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _project_with_rollout(root, _lines_without_cwd())
            with self.assertLogs(_PLUGIN_LOGGER, level="WARNING") as captured:
                refs = list(plugin.discover_sessions(str(root), None))  # type: ignore[attr-defined]
        self.assertEqual(len(refs), 1, "the ref is still produced: nothing is excluded")
        self.assertEqual(refs[0].project_path, _LOSSY_DECODE)
        warnings = [r for r in captured.records if r.levelname == "WARNING"]
        self.assertEqual(len(warnings), 1)
        self.assertIn(_SESSION_ID, warnings[0].getMessage())


def main() -> int:
    print("=== claude_code_session_cwd_smoke ===")
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(SessionCwdSmoke)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
