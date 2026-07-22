"""LLM session ledger — durable transcript persistence across LLM tools.

Stores normalized session events from heterogeneous sources (Claude Code,
Codex, ChatGPT export, agent_messaging) behind a stable schema. Events
land with full content; the 2026-06-14 eradication PR1a removed the
pre-rip ingest-time scan surface per
``workbench/2026-06-14_secretgate_full_eradication_design.md``. See
``workbench/2026-05-24_llm_session_ledger_implementation_spec.md`` for
the original spec.
"""
