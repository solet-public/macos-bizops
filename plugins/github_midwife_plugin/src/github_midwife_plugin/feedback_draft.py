"""Produce local feedback drafts from explicit facts; never discover or publish.

Run ``python -m github_midwife_plugin.feedback_draft`` with one JSON request
on stdin. ReleaseFacts provenance and the content privacy audit belong to the
caller; a successful result grants no publication authority.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import cast

from github_midwife_plugin.feedback_envelope import MAX_REQUEST_BYTES, Diagnostic, ReleaseFacts, ValidationResult, validate_feedback

REQUIRED_SECTIONS: dict[str, tuple[str, ...]] = {
    "defect": ("Item number", "The exact command or action", "Observed output", "Expected behavior", "Release measured against", "Content gate"),
    "question": ("Item number", "The question", "What you are trying to do", "Where you already looked", "Release you are on", "Content gate"),
    "feature-request": ("Item number", "The outcome you need", "The workflow that hit the gap", "What it costs you today", "Your current workaround", "Content gate"),
    "closure-confirmation": ("Item number", "The item being confirmed", "Verified outcome", "How you verified it", "Release you verified on", "Content gate"),
    "feedback-round": ("Part number", "Release this round was measured against", "What this round is about", "Items in this round", "Content gate"),
}
_REQUEST_FIELDS = frozenset(("title", "class", "item", "round", "manager", "surface", "content_gate", "sections", "facts"))
_UNSAFE_LINE = re.compile(r"^ {0,3}(?:`{3,}|~{3,}|#{1,6}(?:\s|$)|(?:=+|-+)[ \t]*$)", re.MULTILINE)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x85\u2028\u2029]")


@dataclass(frozen=True)
class DraftInput:
    """Caller-owned values; correct or clear them by creating a new input.

    Sections use the exact form labels. Their literal text is never evaluated.
    ``round_number=None`` clears a parent; a parent round must use None.
    ``content_gate`` is the caller's explicit attestation, never an audit here.
    """

    title: str
    feedback_class: str
    item: str
    round_number: int | None
    manager: str
    surface: str
    content_gate: str
    sections: Mapping[str, str]


def _input_diagnostics(draft: DraftInput) -> tuple[Diagnostic, ...]:
    headings = REQUIRED_SECTIONS.get(draft.feedback_class)
    if headings is None:
        return (Diagnostic("invalid_class", "class", "unsupported feedback class"),)
    errors = _metadata_diagnostics(draft)
    errors.extend(_sections_diagnostics(draft, headings))
    return tuple(errors)


def _metadata_diagnostics(draft: DraftInput) -> list[Diagnostic]:
    errors: list[Diagnostic] = []
    for field, text in (("title", draft.title), ("manager", draft.manager), ("item", draft.item)):
        if _CONTROL.search(text) or "\n" in text or "\r" in text:
            errors.append(Diagnostic("unsafe_delimiter", field, "single-line text is required"))
    if draft.round_number is not None and (type(draft.round_number) is not int or draft.round_number < 1):
        errors.append(Diagnostic("invalid_round", "round", "round must be a positive integer or null"))
    return errors


def _sections_diagnostics(draft: DraftInput, headings: tuple[str, ...]) -> list[Diagnostic]:
    errors: list[Diagnostic] = []
    errors.extend(Diagnostic("missing_section", key, "required section is absent") for key in sorted(set(headings) - draft.sections.keys()))
    errors.extend(Diagnostic("unknown_section", key, "section is not in this class") for key in sorted(draft.sections.keys() - set(headings)))
    for key in headings:
        if key in draft.sections:
            errors.extend(_section_diagnostics(key, draft.sections[key]))
    number_heading = "Part number" if draft.feedback_class == "feedback-round" else "Item number"
    if number_heading in draft.sections and draft.sections[number_heading] != draft.item:
        errors.append(Diagnostic("section_item_mismatch", number_heading, "section must equal the exact item"))
    return errors


def _section_diagnostics(key: str, text: str) -> list[Diagnostic]:
    if not text.strip() or text.strip() == "_No response_":
        return [Diagnostic("empty_section", key, "required section must contain evidence")]
    if _CONTROL.search(text) or "\r" in text or _UNSAFE_LINE.search(text):
        return [Diagnostic("unsafe_delimiter", key, "control, fence and heading delimiters are not section data; describe or quote them inline")]
    return []


def _utf8_diagnostics(draft: DraftInput, facts: ReleaseFacts) -> tuple[Diagnostic, ...]:
    """Check source strings before JSON quoting can hide invalid Unicode."""
    values = (draft.title, draft.feedback_class, draft.item, draft.manager, draft.surface, draft.content_gate, *draft.sections.keys(), *draft.sections.values(), facts.repository, facts.release_tag, facts.release_commit, *facts.plugins)
    try:
        for value in values:
            value.encode("utf-8")
    except UnicodeError:
        return (Diagnostic("invalid_utf8", "draft/facts", "all supplied strings must encode as UTF-8"),)
    return ()


def produce_feedback(*, draft: DraftInput, facts: ReleaseFacts) -> ValidationResult:
    """Build one leading envelope, then validate the exact final title/body.

    Facts are independently resolved by the caller. No history, discovery,
    optimistic write, close/reopen or persistent lifecycle exists here. Keep
    earlier inputs/results as evidence and re-run after every correction;
    discard caller-owned values to clear a draft. Required sections cannot be
    cleared in an accepted result. Constants and fields evolve through source
    review, not database operations.
    """
    errors = _utf8_diagnostics(draft, facts) or _input_diagnostics(draft)
    if errors:
        return ValidationResult(errors, None, draft.title, "")
    envelope = {
        "version": 1,
        "class": draft.feedback_class,
        "item": draft.item,
        "round": draft.round_number,
        "seed": facts.repository,
        "release_tag": facts.release_tag,
        "release_commit": facts.release_commit,
        "manager": draft.manager,
        "surface": draft.surface,
        "content_gate": draft.content_gate,
    }
    # JSON is a flat YAML mapping too; quoting every string prevents scalar
    # typing or newline/fence injection through any envelope value.
    block = json.dumps(envelope, ensure_ascii=True, indent=2)
    anchors = f"Class: {draft.feedback_class}\n"
    if draft.round_number is not None:
        anchors += f"Round: #{draft.round_number}\n"
    sections = "".join(f"## {key}\n{draft.sections[key]}\n\n" for key in REQUIRED_SECTIONS[draft.feedback_class])
    body = f"```solet-feedback\n{block}\n```\n\n{anchors}\n{sections}"
    return validate_feedback(body=body, title=draft.title, facts=facts)


def _object(value: object, fields: frozenset[str], name: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError(f"{name} must contain exactly: {', '.join(sorted(fields))}")
    return cast("dict[str, object]", value)


def _strings(value: object, name: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object of strings")
    data = cast("dict[str, object]", value)
    if not all(isinstance(item, str) for item in data.values()):
        raise ValueError(f"{name} must contain strings only")
    return cast("dict[str, str]", data)


def _facts(value: object) -> ReleaseFacts:
    data = _object(value, frozenset(("repository", "release_tag", "release_commit", "plugins")), "facts")
    strings = _strings({key: data[key] for key in ("repository", "release_tag", "release_commit")}, "facts")
    plugins = data["plugins"]
    if not isinstance(plugins, list) or not all(isinstance(item, str) for item in cast("list[object]", plugins)):
        raise ValueError("facts.plugins must be an array of strings")
    return ReleaseFacts(strings["repository"], strings["release_tag"], strings["release_commit"], tuple(cast("list[str]", plugins)))


def _request(value: object) -> tuple[DraftInput, ReleaseFacts]:
    data = _object(value, _REQUEST_FIELDS, "request")
    strings = _strings({key: data[key] for key in ("title", "class", "item", "manager", "surface", "content_gate")}, "draft")
    parent = data["round"]
    if parent is not None and type(parent) is not int:
        raise ValueError("round must be an integer or null")
    draft = DraftInput(strings["title"], strings["class"], strings["item"], parent, strings["manager"], strings["surface"], strings["content_gate"], _strings(data["sections"], "sections"))
    return draft, _facts(data["facts"])


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def main() -> int:
    """Read at most MAX_REQUEST_BYTES+1 before parsing; emit JSON, exit 0/1/2."""
    argparse.ArgumentParser(description=__doc__).parse_args()
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    try:
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("request exceeds UTF-8 byte limit")
        value: object = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
        draft, facts = _request(value)
        result = produce_feedback(draft=draft, facts=facts)
    except (ValueError, UnicodeError, RecursionError) as exc:
        print(json.dumps({"valid": False, "diagnostics": [asdict(Diagnostic("invalid_request", "request", str(exc)))]}))
        return 2
    print(json.dumps({"valid": result.valid, "diagnostics": [asdict(item) for item in result.diagnostics], "envelope": result.envelope, "title": result.title, "body": result.body}, ensure_ascii=True))
    return 0 if result.valid else 1


if __name__ == "__main__":
    sys.exit(main())
