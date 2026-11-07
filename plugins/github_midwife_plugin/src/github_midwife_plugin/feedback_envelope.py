"""Validate feedback data locally against explicit, caller-resolved release facts.

No resolver, GitHub consumer, persistence, or public write is installed here.
See feedback_envelope_contract.md for the wire format and caller obligations.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from typing import cast

import yaml
from yaml.nodes import MappingNode, ScalarNode
from yaml.tokens import AliasToken, AnchorToken, TagToken

MAX_BODY_BYTES = 65_536
MAX_REQUEST_BYTES = 131_072
MAX_TITLE_BYTES = 1_024
_FIELDS = frozenset(("version", "class", "item", "round", "seed", "release_tag", "release_commit", "manager", "surface", "content_gate"))
_CLASSES = ("defect", "question", "feature-request", "closure-confirmation", "feedback-round")
_REQUIRED_HEADINGS: dict[str, tuple[str, ...]] = {
    "defect": ("Item number", "The exact command or action", "Observed output", "Expected behavior", "Release measured against", "Content gate"),
    "question": ("Item number", "The question", "What you are trying to do", "Where you already looked", "Release you are on", "Content gate"),
    "feature-request": ("Item number", "The outcome you need", "The workflow that hit the gap", "What it costs you today", "Your current workaround", "Content gate"),
    "closure-confirmation": ("Item number", "The item being confirmed", "Verified outcome", "How you verified it", "Release you verified on", "Content gate"),
    "feedback-round": ("Part number", "Release this round was measured against", "What this round is about", "Items in this round", "Content gate"),
}
_PATTERNS = {
    "seed": r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
    "release_tag": r"[^\s\x00-\x1f\x7f]+",
    "release_commit": r"[0-9a-f]{40}",
    "manager": r"(?=.*\S)[^\r\n]+",
    "surface": r"(?:core|manager|seed|unknown|seed:[A-Za-z0-9_.-]+|plugin:[A-Za-z0-9_][A-Za-z0-9_.-]*)",
}
_MAX_LENGTHS = {"item": 64, "seed": 256, "release_tag": 256, "release_commit": 40, "manager": 256, "surface": 256}
type EnvelopeValue = str | int | float | None


@dataclass(frozen=True)
class ReleaseFacts:
    """Trusted input resolved outside this module, never from the issue itself."""

    repository: str
    release_tag: str
    release_commit: str
    plugins: tuple[str, ...]

    def __post_init__(self) -> None:
        for field, value in (("seed", self.repository), ("release_tag", self.release_tag), ("release_commit", self.release_commit)):
            if not re.fullmatch(_PATTERNS[field], value) or len(value) > _MAX_LENGTHS[field]:
                raise ValueError(f"invalid authoritative {field}")
        if any(not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", plugin) for plugin in self.plugins):
            raise ValueError("invalid authoritative plugin roster")
        if len(set(self.plugins)) != len(self.plugins):
            raise ValueError("duplicate authoritative plugin roster entry")


@dataclass(frozen=True)
class Diagnostic:
    """Stable code and field, with a human-readable explanation."""

    code: str
    field: str
    message: str


@dataclass(frozen=True)
class ValidationResult:
    """Result record; raw body and title retain their literal input text."""

    diagnostics: tuple[Diagnostic, ...]
    envelope: dict[str, EnvelopeValue] | None
    title: str
    body: str

    @property
    def valid(self) -> bool:
        return not self.diagnostics


def _problem(code: str, field: str, message: str) -> Diagnostic:
    return Diagnostic(code, field, message)


def _parse_mapping(block: str) -> dict[str, object]:
    """A flat safe YAML mapping, without aliases, explicit tags, or duplicate keys."""
    if any(isinstance(token, (AliasToken, AnchorToken, TagToken)) for token in yaml.scan(block)):
        raise ValueError("aliases, anchors and explicit tags are not supported")
    node = yaml.compose(block, Loader=yaml.SafeLoader)
    if not isinstance(node, MappingNode):
        raise ValueError("envelope must be one mapping")
    keys: set[str] = set()
    for key, value in node.value:
        if not isinstance(key, ScalarNode) or key.tag != "tag:yaml.org,2002:str":
            raise ValueError("field names must be strings")
        if key.value in keys:
            raise ValueError(f"duplicate field: {key.value}")
        keys.add(key.value)
        if not isinstance(value, ScalarNode):
            raise ValueError("envelope values must be scalars")
    return cast("dict[str, object]", yaml.safe_load(block))


def _leading_envelope(body: str) -> tuple[dict[str, object], str]:
    lines = body.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "```solet-feedback":
        raise ValueError("body must start with a solet-feedback fence")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.rstrip("\r\n") == "```"), None)
    if end is None:
        raise ValueError("envelope fence is not closed")
    rest = "".join(lines[end + 1 :])
    if re.search(r"(?m)^ {0,3}`{3,}solet-feedback[ \t]*\r?$", rest):
        raise ValueError("exactly one solet-feedback block is allowed")
    return _parse_mapping("".join(lines[1:end])), rest


def _is_round(value: object) -> bool:
    return value is None or (not isinstance(value, bool) and isinstance(value, (int, float)) and value >= 1 and value % 1 == 0)


def _value_diagnostic(key: str, value: object, feedback_class: object) -> Diagnostic | None:
    if key == "version":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != 1:
            return _problem("unsupported_version", key, "version must be integer 1")
    elif key == "round":
        if not _is_round(value):
            return _problem("invalid_round", key, "round must be a positive integer or null")
    elif not isinstance(value, str):
        return _problem("invalid_type", key, "field must be a string")
    elif not _valid_string(key, value, feedback_class):
        return _problem("invalid_value", key, "field does not match the version 1 contract")
    return None


def _field_diagnostics(envelope: dict[str, object]) -> list[Diagnostic]:
    errors = [_problem("missing_field", key, "required field is absent") for key in sorted(_FIELDS - envelope.keys())]
    errors.extend(_problem("unknown_field", key, "field is not in version 1") for key in sorted(envelope.keys() - _FIELDS))
    for key in sorted(_FIELDS & envelope.keys()):
        error = _value_diagnostic(key, envelope[key], envelope.get("class"))
        if error is not None:
            errors.append(error)
    if envelope.get("class") == "feedback-round" and envelope.get("round") is not None:
        errors.append(_problem("invalid_round", "round", "a parent feedback round has no parent"))
    return errors


def _valid_string(key: str, value: str, feedback_class: object) -> bool:
    if key == "class":
        return value in _CLASSES
    if key == "content_gate":
        return value == "passed"
    if key == "item":
        pattern = r"Part [1-9][0-9]*" if feedback_class == "feedback-round" else r"§[1-9][0-9]*\.[1-9][0-9]*"
    else:
        pattern = _PATTERNS[key]
    return len(value) <= _MAX_LENGTHS[key] and re.fullmatch(pattern, value) is not None


def _fact_diagnostics(envelope: dict[str, object], facts: ReleaseFacts) -> list[Diagnostic]:
    errors: list[Diagnostic] = []
    for key, expected in (("seed", facts.repository), ("release_tag", facts.release_tag), ("release_commit", facts.release_commit)):
        if envelope[key] != expected:
            errors.append(_problem("release_fact_mismatch", key, "does not equal the authoritative release fact"))
    surface = cast("str", envelope["surface"])
    if surface.startswith("plugin:") and surface.removeprefix("plugin:") not in facts.plugins:
        errors.append(_problem("plugin_not_in_release", "surface", "plugin is absent from the authoritative release roster"))
    if surface.startswith("seed:") and surface.removeprefix("seed:") != facts.repository.split("/")[1]:
        errors.append(_problem("seed_surface_mismatch", "surface", "seed surface names another repository"))
    return errors


def validate_envelope(envelope: dict[str, object], facts: ReleaseFacts) -> tuple[Diagnostic, ...]:
    """Check schema-equivalent scalar rules, then bind the release facts.

    This lower-level check does not inspect a title or Markdown headings.
    Use validate_feedback for a complete issue draft check.
    """
    errors = _field_diagnostics(envelope)
    if not errors:
        errors.extend(_fact_diagnostics(envelope, facts))
    return tuple(errors)


def _closes_markdown_fence(line: str, token: str, fence: str) -> bool:
    return token[0] == fence[0] and len(token) >= len(fence) and re.fullmatch(r" {0,3}(?:`+|~+)[ \t]*", line) is not None


def _heading_sections(markdown: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    fence: str | None = None
    for line in markdown.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif _closes_markdown_fence(line, token, fence):
                fence = None
            continue
        heading = re.fullmatch(r"#{2,6} +(.+?) *#* *", line) if fence is None else None
        if heading:
            current = cast("str", heading.group(1))
            sections.setdefault(current, []).append("")
        elif current is not None:
            sections[current][-1] += line + "\n"
    return sections


def _draft_diagnostics(envelope: dict[str, object], title: str, markdown: str) -> list[Diagnostic]:
    errors: list[Diagnostic] = []
    item = cast("str", envelope["item"])
    if not title.startswith(item) or (len(title) > len(item) and title[len(item)] not in " \t—:-"):
        errors.append(_problem("title_item_mismatch", "title", "title must start with the exact item followed by a separator"))
    sections = _heading_sections(markdown)
    for heading in _REQUIRED_HEADINGS[cast("str", envelope["class"])]:
        values = sections.get(heading, [])
        if len(values) != 1 or not values[0].strip() or values[0].strip() == "_No response_":
            errors.append(_problem("required_heading", heading, "required heading must occur once with non-empty content"))
    return errors


def validate_feedback(*, body: str, title: str, facts: ReleaseFacts) -> ValidationResult:
    """Validate exactly one leading block and a draft without changing literal text."""
    errors: list[Diagnostic] = []
    try:
        body_bytes, title_bytes = len(body.encode("utf-8")), len(title.encode("utf-8"))
    except UnicodeError:
        return ValidationResult((_problem("invalid_utf8", "body/title", "input must encode as UTF-8"),), None, title, body)
    if body_bytes > MAX_BODY_BYTES or title_bytes > MAX_TITLE_BYTES:
        errors.append(_problem("input_too_large", "body/title", "UTF-8 input exceeds the documented byte limit"))
        return ValidationResult(tuple(errors), None, title, body)
    try:
        envelope, markdown = _leading_envelope(body)
    except (ValueError, yaml.YAMLError, RecursionError) as exc:
        return ValidationResult((_problem("invalid_envelope_yaml", "body", str(exc)),), None, title, body)
    errors.extend(validate_envelope(envelope, facts))
    if not errors:
        errors.extend(_draft_diagnostics(envelope, title, markdown))
    return ValidationResult(tuple(errors), cast("dict[str, EnvelopeValue]", envelope) if not errors else None, title, body)


def _request_facts(value: object) -> ReleaseFacts:
    if not isinstance(value, dict) or set(value) != {"repository", "release_tag", "release_commit", "plugins"}:
        raise ValueError("facts must contain repository, release_tag, release_commit, plugins")
    data = cast("dict[str, object]", value)
    if not all(isinstance(data[key], str) for key in ("repository", "release_tag", "release_commit")):
        raise ValueError("authoritative release facts must be strings")
    plugins = data["plugins"]
    if not isinstance(plugins, list) or not all(isinstance(plugin, str) for plugin in cast("list[object]", plugins)):
        raise ValueError("plugins must be an array of strings")
    return ReleaseFacts(cast("str", data["repository"]), cast("str", data["release_tag"]), cast("str", data["release_commit"]), tuple(cast("list[str]", plugins)))


def _json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def main() -> int:
    """Read one bounded UTF-8 JSON request on stdin; emit JSON, exit 0/1/2."""
    argparse.ArgumentParser(description=__doc__).parse_args()
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    try:
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("request exceeds UTF-8 byte limit")
        request: object = json.loads(raw.decode("utf-8"), object_pairs_hook=_json_pairs)
        if not isinstance(request, dict) or set(request) != {"title", "body", "facts"}:
            raise ValueError("request must contain title, body, facts only")
        data = cast("dict[str, object]", request)
        if not isinstance(data["title"], str) or not isinstance(data["body"], str):
            raise ValueError("title and body must be strings")
        result = validate_feedback(body=data["body"], title=data["title"], facts=_request_facts(data["facts"]))
    except (ValueError, UnicodeError, RecursionError) as exc:
        print(json.dumps({"valid": False, "diagnostics": [asdict(_problem("invalid_request", "request", str(exc)))]}))
        return 2
    print(json.dumps({"valid": result.valid, "diagnostics": [asdict(d) for d in result.diagnostics], "envelope": result.envelope, "title": result.title, "body": result.body}, ensure_ascii=True))
    return 0 if result.valid else 1


if __name__ == "__main__":
    sys.exit(main())
