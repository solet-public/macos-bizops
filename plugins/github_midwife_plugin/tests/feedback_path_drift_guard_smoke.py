"""Smoke: no shipped surface tells a user to give feedback in a way a read-only account cannot follow.

An adopter's own GitHub account is an ordinary account on the seed repository. It can open
an issue, comment, and edit the issue it opened, and nothing more: issue creation through
the API silently drops a label it may not set, the sub-issues endpoint answers 404, and
``gh issue create --web`` needs a browser a headless solet does not have. The feedback
runbook and the ``/feedback`` skill template are the canonical text for the one path that
works. Every other shipped surface either points at them or matches them, and this smoke
fails if any of them reintroduces one of the contradictory steps.

Coverage:

1. The surfaces are DISCOVERED, not listed by hand: every Markdown, template and YAML file
   under ``plugins/github_midwife_plugin/knowledge_base/`` (retrieval-test companions
   excepted), ``CONTRIBUTING.md``, and every ``.github/ISSUE_TEMPLATE/*.yml``.
2. Each detector fires on a synthetic bad line and stays quiet on the synthetic allowed one
   (anti-vacuity): the ``--label`` flag, the sub-issues API path, "attach ... as a
   sub-issue", ``--web``, and an un-negated "submit a PR".
3. No discovered surface trips a detector. The canonical runbook and skill template may name
   the forbidden steps only inside their marked ``not-attempt`` block, which is stripped
   before scanning; an unbalanced marker fails.
4. Both canonical files carry the same required anchors: the ``Class:`` line, the
   ``Round: #`` line, ``--body-file``, the new-issue chooser URL for the no-``gh`` route, the
   parent-edit command and its comment fallback, and a ``not-attempt`` block that names
   every forbidden step.
5. The item forms tell a web filer how to name the round, and the round form tells the
   author to edit a checklist of item links into its own body.

Run directly: ``.venv/bin/python3 plugins/github_midwife_plugin/tests/feedback_path_drift_guard_smoke.py``.
Pass ``--root <tree>`` to run it against another checkout of the same layout, for example a
``git archive`` extraction of an earlier commit.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from github_midwife_plugin.feedback_draft import DraftInput

import jsonschema
import yaml

_DEFAULT_ROOT = Path(__file__).resolve().parents[3]
_KB_RELDIR = Path("plugins") / "github_midwife_plugin" / "knowledge_base"
_RUNBOOK_RELPATH = _KB_RELDIR / "07_upstream_feedback_runbook.md"
_SKILL_RELPATH = _KB_RELDIR / "hydration_templates" / "feedback_skill_SKILL.md.template"
_FORMS_RELDIR = Path(".github") / "ISSUE_TEMPLATE"
_SURFACE_SUFFIXES = (".md", ".template", ".yml", ".yaml")
_RETRIEVAL_COMPANION_SUFFIX = ".retrieval_test.yaml"

_EXEMPT_START = "<!-- not-attempt:start -->"
_EXEMPT_END = "<!-- not-attempt:end -->"

_NEGATION_RE = re.compile(
    r"\b(?:not|never|no|removed|declin\w*|refus\w*|without|cannot|instead|whether)\b|n't\b",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# name -> pattern; a hit anywhere outside an exempt block is a violation
_PLAIN_DETECTORS: dict[str, re.Pattern[str]] = {
    "label flag": re.compile(r"(?<![\w-])--(?:add-)?label\b"),
    "sub-issues API": re.compile(r"sub_issues"),
    "sub-issue attach instruction": re.compile(
        r"\bas (?:a )?sub[- ]issues?\b"
        r"|\battach\w*\b[^.\n]{0,80}\bsub[- ]issues?\b"
        r"|\bsub[- ]issues? (?:of|under)\b",
        re.IGNORECASE,
    ),
    "web flag": re.compile(r"(?<![\w-])--web\b"),
}
# a sentence hit is a violation only when the same sentence carries no negation
_PULL_REQUEST_RE = re.compile(
    r"\b(?:submit|send|open|create|file|raise|push)\w*\s+(?:a\s+|the\s+|your\s+)?(?:pull requests?|PRs?)\b",
    re.IGNORECASE,
)
_PULL_REQUEST_DETECTOR = "pull-request instruction"

_ANCHORS = (
    "Class: ",
    "Round: #",
    "--body-file",
    "issues/new/choose",
    'gh issue edit "$PARENT_NUMBER"',
    "gh issue comment",
    _EXEMPT_START,
)
# each forbidden step must be NAMED inside the exempt block, so the filer is told explicitly
_EXEMPT_MUST_NAME = ("--label", "sub_issues", "--web", "pull request")

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


@dataclass(frozen=True)
class Finding:
    path: str
    detector: str
    excerpt: str


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}" if detail else label)


def _split_exempt(text: str) -> tuple[str, list[str]]:
    """Return (text outside exempt blocks, the exempt block bodies); unbalanced markers fail."""
    outside: list[str] = []
    inside: list[str] = []
    rest = text
    while _EXEMPT_START in rest:
        head, _, tail = rest.partition(_EXEMPT_START)
        _check("exempt block is closed", _EXEMPT_END in tail, "a not-attempt:start has no matching end")
        body, _, rest = tail.partition(_EXEMPT_END)
        outside.append(head)
        inside.append(body)
    _check("no stray exempt end marker", _EXEMPT_END not in rest, "a not-attempt:end has no matching start")
    outside.append(rest)
    return "".join(outside), inside


def _sentences(text: str) -> list[str]:
    return _SENTENCE_SPLIT_RE.split(re.sub(r"\s+", " ", text))


def _scan_text(path: str, text: str) -> list[Finding]:
    outside, _ = _split_exempt(text)
    findings: list[Finding] = []
    for name, pattern in _PLAIN_DETECTORS.items():
        for match in pattern.finditer(outside):
            findings.append(Finding(path, name, match.group(0)))
    for sentence in _sentences(outside):
        if _PULL_REQUEST_RE.search(sentence) and not _NEGATION_RE.search(sentence):
            findings.append(Finding(path, _PULL_REQUEST_DETECTOR, sentence.strip()[:120]))
    return findings


def _discover(root: Path) -> list[Path]:
    kb = root / _KB_RELDIR
    found = [p for p in sorted(kb.rglob("*")) if p.is_file() and p.suffix in _SURFACE_SUFFIXES and not p.name.endswith(_RETRIEVAL_COMPANION_SUFFIX)]
    contributing = root / "CONTRIBUTING.md"
    if contributing.is_file():
        found.append(contributing)
    forms = root / _FORMS_RELDIR
    if forms.is_dir():
        found.extend(sorted(p for p in forms.glob("*.yml") if p.is_file()))
    return found


def _check_detectors_are_not_vacuous() -> None:
    bad = {
        "label flag": "gh issue create --repo R --label defect --body-file x.md",
        "sub-issues API": "gh api repos/R/issues/1/sub_issues -F sub_issue_id=2",
        "sub-issue attach instruction": "then attach each item's issue to it as a sub-issue",
        "web flag": "gh issue create --web",
        _PULL_REQUEST_DETECTOR: "For a design, open a pull request with the document.",
    }
    for name, line in bad.items():
        hit = [f.detector for f in _scan_text("synthetic", line)]
        _check(f"detector fires: {name}", name in hit, f"no {name!r} finding for {line!r}: {hit}")
    allowed = "Do not open a pull request. A label or a sub-issue link needs write access."
    _check("negated sentence is not flagged", not _scan_text("synthetic", allowed), allowed)
    question = "You are wondering whether to open a pull request or send a patch."
    _check("a whether-question is not an instruction", not _scan_text("synthetic", question), question)
    exempt = f"ok\n{_EXEMPT_START}\nDo not pass --label or --web.\n{_EXEMPT_END}\nok"
    _check("exempt block is not scanned", not _scan_text("synthetic", exempt), exempt)
    unbalanced_failed = False
    try:
        _scan_text("synthetic", f"{_EXEMPT_START} --label")
    except SmokeFailureError:
        unbalanced_failed = True
    _check("unbalanced exempt marker fails", unbalanced_failed)


def _check_no_surface_trips_a_detector(root: Path) -> list[Path]:
    surfaces = _discover(root)
    _check("surfaces discovered", len(surfaces) >= 8, f"only {len(surfaces)} shipped feedback surfaces found under {root}")
    findings: list[Finding] = []
    for path in surfaces:
        findings.extend(_scan_text(str(path.relative_to(root)), path.read_text(encoding="utf-8")))
    detail = "; ".join(f"{f.path} [{f.detector}] {f.excerpt!r}" for f in findings[:12])
    _check(
        "no shipped surface reintroduces a contradictory feedback step",
        not findings,
        f"{len(findings)} finding(s): {detail}",
    )
    return surfaces


def _check_canonical_anchors(root: Path) -> None:
    for relpath in (_RUNBOOK_RELPATH, _SKILL_RELPATH):
        path = root / relpath
        _check(f"canonical file present: {relpath.name}", path.is_file(), str(path))
        text = path.read_text(encoding="utf-8")
        missing = [anchor for anchor in _ANCHORS if anchor not in text]
        _check(f"{relpath.name} carries every canonical anchor", not missing, f"missing {missing}")
        _, blocks = _split_exempt(text)
        _check(f"{relpath.name} has exactly one not-attempt block", len(blocks) == 1, f"found {len(blocks)}")
        unnamed = [token for token in _EXEMPT_MUST_NAME if token not in blocks[0]]
        _check(f"{relpath.name} not-attempt block names every forbidden step", not unnamed, f"unnamed {unnamed}")


def _check_forms(root: Path) -> None:
    forms = root / _FORMS_RELDIR
    for name in ("01_defect.yml", "02_question.yml", "03_feature_request.yml", "04_closure_confirmation.yml"):
        text = (forms / name).read_text(encoding="utf-8")
        _check(f"{name} tells a web filer how to name the round", "(round #" in text, "no '(round #' in the item number field")
    round_form = (forms / "05_feedback_round.yml").read_text(encoding="utf-8")
    _check(
        "05_feedback_round.yml tells the author to edit a checklist into the round body",
        "- [ ] #<issue number>" in round_form and "you can edit it" in round_form,
        "the checklist instruction is missing",
    )


def _envelope_module(root: Path) -> ModuleType:
    path = root / "plugins/github_midwife_plugin/src/github_midwife_plugin/feedback_envelope.py"
    _check("envelope callable exists", path.is_file(), str(path))
    spec = importlib.util.spec_from_file_location("_feedback_envelope_under_test", path)
    if spec is None or spec.loader is None:
        raise SmokeFailureError("envelope import spec unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _check("callable imported from requested root", Path(module.__file__).resolve() == path.resolve())
    return module


def _example_envelope(feedback_class: str = "defect") -> dict[str, object]:
    return {
        "version": 1,
        "class": feedback_class,
        "item": "Part 60" if feedback_class == "feedback-round" else "§60.1",
        "round": None if feedback_class == "feedback-round" else 92,
        "seed": "solet-public/macos-bizops",
        "release_tag": "release-example",
        "release_commit": "a" * 40,
        "manager": "solet 69_40",
        "surface": "plugin:example_plugin",
        "content_gate": "passed",
    }


def _example_body(envelope: dict[str, object], headings: list[str]) -> str:
    block = yaml.safe_dump(envelope, allow_unicode=True, sort_keys=False)
    return "```solet-feedback\n" + block + "```\n\n" + "".join(f"### {heading}\nLiteral data $(echo unsafe) `text`\n\n" for heading in headings)


def _form_headings(root: Path) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for name, feedback_class in (("01_defect.yml", "defect"), ("02_question.yml", "question"), ("03_feature_request.yml", "feature-request"), ("04_closure_confirmation.yml", "closure-confirmation"), ("05_feedback_round.yml", "feedback-round")):
        form = yaml.safe_load((root / _FORMS_RELDIR / name).read_text())
        result[feedback_class] = [field["attributes"]["label"] for field in form["body"] if field.get("validations", {}).get("required") or field["type"] == "checkboxes"]
    return result


def _check_schema_parity(root: Path, module: ModuleType, facts: object) -> None:
    schema_path = root / ".github/feedback-envelope.v1.json"
    _check("source envelope schema exists", schema_path.is_file())
    schema = json.loads(schema_path.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    mutations: list[tuple[str, object]] = [
        ("version", 2),
        ("version", True),
        ("version", "1"),
        ("version", 1.0),
        ("class", "design"),
        ("item", "§60.10"),
        ("item", "§0.1"),
        ("item", "§60.1\n"),
        ("round", True),
        ("round", 0),
        ("round", 2.0),
        ("round", 2.5),
        ("seed", "wrong-shape"),
        ("seed", "solet-public/macos-bizops\n"),
        ("release_commit", "a" * 12),
        ("release_commit", "A" * 40),
        ("release_tag", "space tag"),
        ("manager", "  "),
        ("manager", "solet\n1"),
        ("surface", "plugin:"),
        ("surface", "core\n"),
        ("surface", "unknown"),
        ("surface", "seed:macos-bizops"),
        ("content_gate", "pending"),
        ("content_gate", True),
    ]
    fixtures = [_example_envelope()]
    fixtures.extend({**_example_envelope(), key: value} for key, value in mutations)
    fixtures.extend({key: value for key, value in _example_envelope().items() if key != omitted} for omitted in _example_envelope())
    fixtures.extend([{**_example_envelope(), "extra": "value"}, _example_envelope("feedback-round"), {**_example_envelope("feedback-round"), "round": 92}])
    for index, envelope in enumerate(fixtures):
        # Fact binding is a separate contextual rule; these fixtures keep valid facts fixed.
        diagnostics = module.validate_envelope(envelope, facts)
        _check(f"schema/callable parity fixture {index}", validator.is_valid(envelope) == (not diagnostics), str(diagnostics))


def _check_invalid_drafts(module: ModuleType, facts: object, body: str, headings: list[str]) -> None:
    malformed = [
        "prefix\n" + body,
        body.replace("```solet-feedback", "```yaml", 1),
        body + "```solet-feedback\nversion: 1\n```",
        body + "  ````solet-feedback \nversion: 1\n````",
        body.replace("version: 1", "version: 1\nversion: 1", 1),
        body.replace("version: 1", "version: [1]", 1),
        body.replace("version: 1", "version: &v 1", 1),
        body.replace("version: 1", "version: !!python/object:builtins.object {}", 1),
        body.replace("version: 1", "version: *missing", 1),
        body.replace("version: 1", "version: 1\n---\nversion: 1", 1),
        body.replace("version: 1", "version: !!int 1", 1),
    ]
    for index, draft in enumerate(malformed):
        result = module.validate_feedback(body=draft, title="§60.1 — test", facts=facts)
        _check(f"unsafe/malformed/duplicate YAML rejected {index}", not result.valid and result.diagnostics[0].code == "invalid_envelope_yaml")
    for heading in headings:
        draft = body.replace(f"### {heading}\nLiteral data $(echo unsafe) `text`", f"### {heading}\n")
        result = module.validate_feedback(body=draft, title="§60.1 — test", facts=facts)
        _check(f"required non-empty heading: {heading}", any(d.code == "required_heading" for d in result.diagnostics))
    for draft in (body + "### Expected behavior\nOther\n", body.replace("### Expected behavior", "```text\n### Expected behavior") + "```\n"):
        _check("duplicate or code-fenced heading rejected", not module.validate_feedback(body=draft, title="§60.1 — test", facts=facts).valid)
    for title in ("§60.10 — wrong item", "§60.1suffix", "wrong", "é" * 513):
        _check("title exact item boundary and byte limit", not module.validate_feedback(body=body, title=title, facts=facts).valid)
    huge = body + "é" * (module.MAX_BODY_BYTES // 2)
    result = module.validate_feedback(body=huge, title="§60.1 — test", facts=facts)
    _check("body UTF-8 byte limit before YAML parsing", result.diagnostics[0].code == "input_too_large")


def _check_markdown_fence_boundaries(module: ModuleType, facts: object, body: str) -> None:
    envelope, separator, sections = body.partition("```\n\n")
    _check("fence regression fixture has envelope and sections", bool(separator))
    prefix = envelope + separator
    invalid_closers = (
        ("```text", "```still-code"),
        ("~~~text", "~~~still-code"),
        ("````text", "```"),
        ("```text", "~~~"),
        ("```text", "    ```"),
        ("```text", "```\u00a0"),
    )
    for opening, false_close in invalid_closers:
        for ending in ("", "\n" + opening[:3] + "\n"):
            draft = prefix + opening + "\n" + false_close + "\n" + sections + ending
            result = module.validate_feedback(body=draft, title="§60.1 — test", facts=facts)
            _check(f"headings behind invalid closer stay fenced: {opening!r}/{false_close!r}/{ending!r}", not result.valid and any(d.code == "required_heading" for d in result.diagnostics))
    for opening, closing in (("```text", "```"), ("~~~text", "  ~~~~ \t"), ("````text", "`````\t")):
        draft = prefix + opening + "\nordinary code\n" + closing + "\n" + sections
        _check(f"valid closer restores real headings: {closing!r}", module.validate_feedback(body=draft, title="§60.1 — test", facts=facts).valid)


def _check_release_binding(module: ModuleType, facts: object, headings: list[str]) -> None:
    for field, value, code in (
        ("seed", "elsewhere/macos-bizops", "release_fact_mismatch"),
        ("release_tag", "other-tag", "release_fact_mismatch"),
        ("release_commit", "b" * 40, "release_fact_mismatch"),
        ("surface", "plugin:missing_plugin", "plugin_not_in_release"),
        ("surface", "seed:other-seed", "seed_surface_mismatch"),
    ):
        envelope = {**_example_envelope(), field: value}
        result = module.validate_feedback(body=_example_body(envelope, headings), title="§60.1 — test", facts=facts)
        _check(f"authoritative binding: {field}/{value}", any(d.code == code for d in result.diagnostics))
    for surface in ("unknown", "core", "manager", "seed", "seed:macos-bizops"):
        body = _example_body({**_example_envelope(), "surface": surface}, headings)
        _check(f"supported surface {surface}", module.validate_feedback(body=body, title="§60.1 — test", facts=facts).valid)


def _check_envelope_cli(root: Path, body: str) -> None:
    path = root / "plugins/github_midwife_plugin/src/github_midwife_plugin/feedback_envelope.py"
    facts = {"repository": "solet-public/macos-bizops", "release_tag": "release-example", "release_commit": "a" * 40, "plugins": ["example_plugin"]}
    request = json.dumps({"title": "§60.1 — test", "body": body, "facts": facts})
    for raw, code in ((request, 0), (request.replace("Literal data", "\\ud800", 1), 1), (request.replace("release-example", "other-tag", 1), 1), ('{"title":"x","title":"y"}', 2), (request.replace('"plugins": ["example_plugin"]', '"plugins": [1]'), 2), (" " * 131_073, 2)):
        run = subprocess.run([sys.executable, str(path)], input=raw, text=True, capture_output=True, check=False, timeout=10)
        _check(f"CLI structured exit {code}", run.returncode == code, run.stderr + run.stdout)
        output = json.loads(run.stdout)
        _check("CLI diagnostics and valid flag", output["valid"] == (code == 0) and isinstance(output["diagnostics"], list))
        if code == 0:
            _check("CLI preserves body literal newlines", output["body"] == body)


def _check_envelope_lifecycle(module: ModuleType, facts: object, body: str, headings: list[str]) -> None:
    original = module.validate_feedback(body=body, title="§60.1 — test", facts=facts)
    corrected = {**_example_envelope(), "round": None, "surface": "unknown"}
    revised_body = _example_body(corrected, headings)
    revised = module.validate_feedback(body=revised_body, title="§60.1 — corrected", facts=facts)
    _check("correct/clear optional parent and revalidate", revised.valid and revised.envelope["round"] is None)
    _check("original validation retains original draft", original.body == body and original.envelope["round"] == 92)
    _check("parent association can be set again", module.validate_feedback(body=body, title="§60.1 — test", facts=facts).valid)
    crlf = body.replace("\n", "\r\n")
    result = module.validate_feedback(body=crlf, title="§60.1 — test", facts=facts)
    _check("CRLF input preserved", result.valid and result.body == crlf)
    result = module.validate_feedback(body=body + "\ud800", title="§60.1 — test", facts=facts)
    _check("invalid UTF-8 callable input diagnosed", result.diagnostics[0].code == "invalid_utf8")
    for commit, roster in (("short", ("example_plugin",)), ("a" * 40, ("example_plugin", "example_plugin")), ("a" * 40, ("bad/name",))):
        refused = False
        try:
            module.ReleaseFacts("solet-public/macos-bizops", "release-example", commit, roster)
        except ValueError:
            refused = True
        _check("malformed caller facts refused", refused)


def _check_feedback_envelope(root: Path) -> None:
    module = _envelope_module(root)
    facts = module.ReleaseFacts("solet-public/macos-bizops", "release-example", "a" * 40, ("example_plugin",))
    headings = _form_headings(root)
    for feedback_class, required in headings.items():
        envelope = _example_envelope(feedback_class)
        body = _example_body(envelope, required)
        title = str(envelope["item"]) + " — test"
        result = module.validate_feedback(body=body, title=title, facts=facts)
        _check(f"valid {feedback_class} using current form headings", result.valid, str(result.diagnostics))
        _check("literal body and title preserved", result.body == body and result.title == title)
    body = _example_body(_example_envelope(), headings["defect"])
    _check_schema_parity(root, module, facts)
    _check_invalid_drafts(module, facts, body, headings["defect"])
    _check_markdown_fence_boundaries(module, facts, body)
    _check_release_binding(module, facts, headings["defect"])
    _check_envelope_cli(root, body)
    _check_envelope_lifecycle(module, facts, body, headings["defect"])


def _draft_module(root: Path) -> ModuleType:
    source = root / "plugins/github_midwife_plugin/src"
    sys.path.insert(0, str(source))
    import github_midwife_plugin.feedback_draft as module

    _check("real draft import origin", Path(module.__file__).resolve() == source / "github_midwife_plugin/feedback_draft.py")
    _check("draft uses real validator origin", Path(sys.modules[module.validate_feedback.__module__].__file__).resolve() == source / "github_midwife_plugin/feedback_envelope.py")
    return module


def _draft_fixture(module: ModuleType, feedback_class: str, headings: list[str]) -> DraftInput:
    item = "Part 60" if feedback_class == "feedback-round" else "§60.1"
    sections = dict.fromkeys(headings, "Literal evidence $(echo unsafe) `text`\nsecond evidence line")
    sections[headings[0]] = item
    sections["Content gate"] = "- [x] I re-read this issue for private data and secrets."
    return module.DraftInput(item + " — test", feedback_class, item, None if feedback_class == "feedback-round" else 92, "solet 69_40", "plugin:example_plugin", "passed", sections)


def _check_draft_classes(root: Path, module: ModuleType, facts: object) -> DraftInput:
    for feedback_class, headings in _form_headings(root).items():
        _check(f"producer headings equal actual form: {feedback_class}", tuple(headings) == module.REQUIRED_SECTIONS[feedback_class])
        draft = _draft_fixture(module, feedback_class, headings)
        result = module.produce_feedback(draft=draft, facts=facts)
        _check(f"producer generates valid {feedback_class}", result.valid, str(result.diagnostics))
        _check("producer final bytes pass existing validator", module.validate_feedback(body=result.body, title=result.title, facts=facts).valid)
        _check("one leading envelope before Class anchor", result.body.startswith("```solet-feedback\n") and result.body.count("```solet-feedback") == 1 and "\nClass: " in result.body)
        _check("literal evidence retained as data", "$(echo unsafe) `text`\nsecond evidence line" in result.body)
    return _draft_fixture(module, "defect", _form_headings(root)["defect"])


def _check_draft_corrections(module: ModuleType, facts: object, draft: DraftInput) -> None:
    for changes in (
        {"title": "§60.10 — wrong"},
        {"title": "§60.1 — bad\nheading"},
        {"item": "§0.1"},
        {"round_number": True},
        {"round_number": 2.5},
        {"round_number": 0},
        {"surface": "plugin:missing"},
        {"surface": "seed:other"},
        {"manager": ""},
        {"manager": "one\ntwo"},
        {"content_gate": "pending"},
        {"feedback_class": "design"},
    ):
        result = module.produce_feedback(draft=replace(draft, **changes), facts=facts)
        _check(f"producer rejects invalid input {changes!r}", not result.valid and bool(result.diagnostics))


def _check_draft_sections(module: ModuleType, facts: object, draft: DraftInput) -> None:
    for heading in module.REQUIRED_SECTIONS["defect"]:
        for value in (None, "", " \n", "_No response_"):
            sections = dict(draft.sections)
            if value is None:
                sections.pop(heading)
            else:
                sections[heading] = value
            _check(f"producer required section missing/clear {heading}/{value!r}", not module.produce_feedback(draft=replace(draft, sections=sections), facts=facts).valid)
    for text in ("first\n## Expected behavior\nspoof", "first\n```solet-feedback\nspoof", "~~~text\nspoof", "first\rspoof", "first\u2028## Expected behavior", "first\n---", "\x00value"):
        sections = {**draft.sections, "Observed output": text}
        result = module.produce_feedback(draft=replace(draft, sections=sections), facts=facts)
        _check(f"producer unsafe delimiters rejected {text!r}", any(d.code == "unsafe_delimiter" for d in result.diagnostics))
    sections = {**draft.sections, "Unexpected heading": "data"}
    _check("producer rejects unknown section", not module.produce_feedback(draft=replace(draft, sections=sections), facts=facts).valid)


def _check_draft_lifecycle(module: ModuleType, facts: object, draft: DraftInput) -> None:
    original = module.produce_feedback(draft=draft, facts=facts)
    corrected = replace(draft, round_number=None, surface="unknown", title="§60.1 — corrected")
    revised = module.produce_feedback(draft=corrected, facts=facts)
    _check("producer correct/clear/revalidate", revised.valid and revised.envelope["round"] is None and "Round: #" not in revised.body)
    _check("producer old result unchanged", original.envelope["round"] == 92 and original.title == draft.title)
    _check("producer reversal/set parent", module.produce_feedback(draft=draft, facts=facts) == original)
    _check("producer rejects excessive final body", not module.produce_feedback(draft=replace(draft, sections={**draft.sections, "Observed output": "é" * 33_000}), facts=facts).valid)
    _check("producer diagnoses invalid Unicode", not module.produce_feedback(draft=replace(draft, sections={**draft.sections, "Observed output": "\ud800"}), facts=facts).valid)
    for changes in ({"manager": "\ud800"}, {"title": "§60.1 — \ud800"}):
        result = module.produce_feedback(draft=replace(draft, **changes), facts=facts)
        _check("producer rejects Unicode hidden by JSON quoting", result.diagnostics[0].code == "invalid_utf8")
    invalid_facts = module.ReleaseFacts("solet-public/macos-bizops", "tag-\ud800", "a" * 40, ("example_plugin",))
    result = module.produce_feedback(draft=draft, facts=invalid_facts)
    _check("producer rejects invalid Unicode in explicit facts", result.diagnostics[0].code == "invalid_utf8")


def _draft_cli(root: Path, raw: str, exit_code: int) -> dict[str, Any]:
    source = root / "plugins/github_midwife_plugin/src"
    run = subprocess.run([sys.executable, "-m", "github_midwife_plugin.feedback_draft"], cwd=source, input=raw, text=True, capture_output=True, check=False, timeout=10)
    _check(f"real producer CLI exit {exit_code}", run.returncode == exit_code, run.stderr + run.stdout)
    output = json.loads(run.stdout)
    _check("producer CLI structured diagnostics", output["valid"] == (exit_code == 0) and isinstance(output["diagnostics"], list))
    return output


def _check_draft_examples(root: Path, module: ModuleType) -> dict[str, Any]:
    requests: list[dict[str, Any]] = []
    for relative in (_RUNBOOK_RELPATH, _SKILL_RELPATH):
        text = (root / relative).read_text()
        marker = "<!-- feedback-draft-request:start -->\n```json\n"
        _check("docs include executable JSON request", marker in text)
        raw = text.split(marker, 1)[1].split("\n```", 1)[0]
        request = json.loads(raw)
        requests.append(request)
        output = _draft_cli(root, raw, 0)
        facts = module.ReleaseFacts(**{**request["facts"], "plugins": tuple(request["facts"]["plugins"])})
        _check("documented producer output passes actual validator", module.validate_feedback(body=output["body"], title=output["title"], facts=facts).valid)
        for anchor in ("produce_feedback", "DraftInput", "python -m github_midwife_plugin.feedback_draft", "independently", "HEAD", "^{commit}", "ls-tree", "forms/intake", "leading"):
            _check(f"doc actual API/CLI/provenance anchor: {anchor}", anchor in text)
        _check("docs do not instruct Class first", "Start every draft with a first line `Class:" not in text)
        if relative == _SKILL_RELPATH:
            hydrated = text.replace("{{SOLET_NAME}}", "example")
            frontmatter = yaml.safe_load(hydrated.split("---", 2)[1])
            _check("hydrated skill frontmatter valid", frontmatter["name"] == "feedback" and bool(frontmatter["description"]) and "{{" not in hydrated)
    _check("runbook and skill request examples agree", requests[0] == requests[1])
    return requests[0]


def _check_draft_cli_negatives(root: Path, request: dict[str, Any]) -> None:
    raw = json.dumps(request)
    for malformed in (
        raw[:-1] + ', "title": "duplicate"}',
        raw.replace('"release_tag":', '"release_tag":"duplicate", "release_tag":'),
        raw.replace('"Observed output":', '"Observed output":"duplicate", "Observed output":'),
        raw[:-1] + ', "unknown": 1}',
        raw.replace('"plugins": ["example_plugin"]', '"plugins": [1]'),
        raw.replace('"round": null', '"round": true'),
        " " * 131_073,
        "{",
        "[]",
        raw.replace('"release_commit": "' + "a" * 40, '"release_commit": "bad'),
        raw.replace('"plugins": ["example_plugin"]', '"plugins": ["example_plugin", "example_plugin"]'),
    ):
        _draft_cli(root, malformed, 2)
    for changes in ({"title": "wrong"}, {"content_gate": "pending"}, {"surface": "plugin:missing"}, {"round": 0}):
        _draft_cli(root, json.dumps({**request, **changes}), 1)
    for facts in ({**request["facts"], "extra": "unknown"}, {**request["facts"], "repository": "not-a-repository"}, {**request["facts"], "release_tag": "bad tag"}):
        _draft_cli(root, json.dumps({**request, "facts": facts}), 2)
    limited = _draft_cli(root, "not json" * 20_000, 2)
    _check("producer bounds bytes before JSON parse", limited["diagnostics"][0]["message"] == "request exceeds UTF-8 byte limit")
    # A parent is produced before its remote issue number exists. Only the
    # caller can select the next Part N and later bind that real issue number.
    parent = {
        **request,
        "class": "feedback-round",
        "item": "Part 61",
        "title": "Part 61 — test",
        "round": None,
        "sections": {"Part number": "Part 61", "Release this round was measured against": "Example release", "What this round is about": "Example findings", "Items in this round": "Items will be linked after filing.", "Content gate": "- [x] Re-read for private data."},
    }
    _draft_cli(root, json.dumps(parent), 0)
    _draft_cli(root, json.dumps({**parent, "round": 92}), 1)
    child = {**request, "item": "§61.1", "title": "§61.1 — test", "round": 123, "sections": {**request["sections"], "Item number": "§61.1"}}
    output = _draft_cli(root, json.dumps(child), 0)
    _check("real parent association in generated child", "Round: #123" in output["body"])
    _draft_cli(root, json.dumps({**child, "sections": {**child["sections"], "Item number": "§60.1"}}), 1)


def _check_feedback_draft(root: Path) -> None:
    module = _draft_module(root)
    facts = module.ReleaseFacts("solet-public/macos-bizops", "release-example", "a" * 40, ("example_plugin",))
    draft = _check_draft_classes(root, module, facts)
    _check_draft_corrections(module, facts, draft)
    _check_draft_sections(module, facts, draft)
    _check_draft_lifecycle(module, facts, draft)
    request = _check_draft_examples(root, module)
    _check_draft_cli_negatives(root, request)


def main(argv: list[str]) -> int:
    root = Path(argv[argv.index("--root") + 1]).resolve() if "--root" in argv else _DEFAULT_ROOT
    try:
        _check_detectors_are_not_vacuous()
        surfaces = _check_no_surface_trips_a_detector(root)
        _check_canonical_anchors(root)
        _check_forms(root)
        _check_feedback_envelope(root)
        _check_feedback_draft(root)
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1

    print(f"feedback_path_drift_guard_smoke OK: {len(_CHECKS_RUN)} checks passed ({len(surfaces)} surfaces scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
