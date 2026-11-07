#!/usr/bin/env python3
"""Meta-smoke for the whole-tree integration gate (W-INT Cycle 1).

Exercises the gate's collaborator functions against synthetic surfaces
(in-memory `ServiceSurface` / `PluginSurface` / `CallSiteRef` objects with
hand-crafted decorator/JSON mappings) and confirms each check family
emits the expected findings for the documented failure modes.

Cases 1-6 drive the real catalog parity check (whole_tree_integration_catalog)
over in-memory catalogs; cases 19-28 drive it through the gate's own `main`
against synthetic plugin trees, and through the real tree.

Cases:
  1. C1.5 — registrable service-interface key has no companion KB JSON (the
     canonical vault `write_state` regression that motivated the gate).
  2. C1.7 — payload whose full key nothing registrable matches.
  3. C1.6 — the JSON the runtime overlay reads carries another key.
  4. C2.2 — registrable plugin key has no companion JSON.
  5. C2.4 — plugin payload no imported class registers.
  6. C3.1 — call-site references a non-existent process_key.
  7. C3.2 — call-site references a bound-plugin namespace (Task #21 surface).
  8. C5.1 — scheduling action literal omits `result_processor_kind`.
  9. Allowlist coverage — wildcard + exact-match.
 10. End-to-end — the live gate against the real tree returns exit 0
     with the shipped allowlist (proves wiring + allowlist remain valid).
 11. Negative-fixture marker — `# wint:negative-fixture` is honored in a
     tests/ path (line-scoped suppression) but IGNORED in a src/ path
     (red-first: the convention must not weaken src detection).
 12. C3.* executable-context matching (iss_3dc2ecc3) — a process_key quoted
     inside PROSE (a comment, a docstring, a `description=` format example)
     is not a reference, while a whole-literal key still is. Also pins the
     assembled-key boundary: f-strings and implicit adjacent-literal
     concatenation are deliberately NOT reported.
 13. Bindings fail-loud (iss_06a5d953) — a tree with no materialized
     `service_bindings.json` derives them from the shipped profile template
     genesis uses; with no provenance to say which profile, the gate exits 2
     (harness error) instead of silently scanning against an empty map.
 17. C6.1 git-awareness (iss_ce45713b) — an undeclared root entry that Git
     does not track is reported, not blocking; the same entry staged or
     committed blocks exactly as before, and so does every unknown entry
     when no Git directory exists to ask.
 18. Shared registration metadata — completed classes expose generated and
     inherited metadata without construction; instance discovery, signatures,
     provider-qualified keys, binding policy and every registry field stay fixed.
 19-21. Red-first: a class-decorator-generated action with its JSON is not a
     C2.4 orphan; deleting the JSON is C2.2; an unregistered payload is C2.4.
 22-24. Inherited actions register; the provider is the entry-point name, so
     one verb in two providers stays two keys and `name=` (not the attribute)
     picks the overlay path; wrong-provider and duplicate payloads are named.
 25. A disabled service action is absent from the real scanner's catalog.
 26. An optional import failure or a dynamic get_available_actions is an
     advisory carrying its raw reason, never a finding, and the CATALOG line
     says INCOMPLETE; bound providers keep JSON parity and are disclosed.
 27. Allowance lifecycle on a git fixture: a satisfied entry is detected and
     retired, unrelated debt stays byte-identical, a reason correction keeps
     the key and its history, a reintroduction covers a fresh finding, and a
     write against a stale base blob is refused before it touches the file.
 28. The real audio class's generated ffmpeg_a3dscope key resolves to its
     shipped JSON; case 13 also asserts no satisfied C1/C2 allowance remains.

Project policy: no pytest. Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import ast
import contextlib
import importlib
import importlib.util
import io
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, cast

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
REPO_ROOT = _ROOT
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "quality_gates"))

import whole_tree_integration_gate as wint  # noqa: E402  # pyright: ignore[reportMissingImports]
from ananta.core.actions.action_metadata import (  # noqa: E402
    ActionMetadata,
    platform_process,
)
from ananta.core.plugins.action_discovery import discover_actions  # noqa: E402
from ananta.core.process_registry.plugin_process_scanner import (  # noqa: E402
    PluginProcessScanner,
)
from ananta.error_handling import PluginError  # noqa: E402
from whole_tree_integration_gate import (  # noqa: E402  # pyright: ignore[reportMissingImports]
    Allowlist,
    AllowlistEntry,
    BindingsResolutionError,
    CallSiteRef,
    Finding,
    PluginSurface,
    _bindings_from_profile_template,
    _classify_call_site_ref,
    _collect_service_interface_decorated,
    _is_test_path,
    _scan_single_file_for_refs,
    check_scheduling_rpk_propagation,
    load_allowlist,
    satisfied_allowances,
    split_unshipped_references,
)

from quality_gates import whole_tree_integration_catalog as catalog_mod  # noqa: E402
from quality_gates.whole_tree_integration_catalog import (  # noqa: E402
    PLUGIN_SPEC,
    SERVICE_SPEC,
    CatalogSpec,
    ProcessCatalog,
    check_parity,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _synthetic_func(name: str = "verb") -> ast.FunctionDef:
    """Return a minimal FunctionDef AST node for use in synthetic surfaces."""
    node = ast.FunctionDef(
        name=name,
        args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=[ast.Pass()],
        decorator_list=[],
        returns=None,
        lineno=1,
    )
    return node


_SERVICE_KB = Path("/catalog-tree/ananta/knowledge_base/processes/example_service")
_PLUGIN_KB = Path("/catalog-tree/plugins/example_plugin/knowledge_base/processes")


def _parity(spec: CatalogSpec, entries: dict[str, Path], payloads: dict[Path, str | None]) -> list[tuple[str, str, str]]:
    """Run the real parity check over an in-memory catalog and payload corpus."""
    catalog = ProcessCatalog(spec, Path("/catalog-tree"), entries=entries, providers={key.split("::")[1] for key in entries})
    return sorted((f.check_id, f.scope_qualifier, f.specifier) for f in check_parity(catalog, payloads))


def _case_c1_5_registered_without_json() -> None:
    print("\nCase 1: C1.5 — registrable service-interface key without KB JSON")
    key = "service_interface::example_service::write_state"
    found = _parity(SERVICE_SPEC, {key: _SERVICE_KB / "write_state.json"}, {})
    _check(found == [("C1.5", "example_service", "write_state")], f"C1.5 names the key the overlay cannot find (vault write_state precedent; got {found})")


def _case_c1_5_disabled_decorator_surface() -> None:
    print("\nCase 1b: disabled decorators stay off the AST surface C3 resolves against")
    module = ast.parse("""
class API:
    @service_interface_process(name="disabled_verb", is_enabled=False)
    def disabled_verb(self):
        pass
    @service_interface_process(name="enabled_verb", is_enabled=True)
    def enabled_verb(self):
        pass
""")
    decorated = _collect_service_interface_decorated(module)
    _check("disabled_verb" not in decorated and "enabled_verb" in decorated, "shared enabled predicate omits disabled decorators from the AST surface")


def _case_c1_7_orphan_kb_json() -> None:
    print("\nCase 2: C1.7 — payload whose key no registrable service-interface key matches")
    found = _parity(SERVICE_SPEC, {}, {_SERVICE_KB / "orphan.json": "service_interface::example_service::orphan"})
    _check(found == [("C1.7", "example_service", "orphan")], f"C1.7 names the unknown full key (got {found})")


def _case_c1_6_wrong_process_key_in_json() -> None:
    print("\nCase 3: C1.6 — the overlay's own file carries another key")
    key = "service_interface::example_service::verb"
    found = _parity(SERVICE_SPEC, {key: _SERVICE_KB / "verb.json"}, {_SERVICE_KB / "verb.json": "service_interface::WRONG_PROVIDER::verb"})
    _check(found == [("C1.6", "example_service", "verb")], f"C1.6 when the runtime path carries a different key (got {found})")


def _case_c2_2_plugin_registered_without_json() -> None:
    print("\nCase 4: C2.2 — registrable plugin key without KB JSON")
    found = _parity(PLUGIN_SPEC, {"plugin::example_plugin::upload_track": _PLUGIN_KB / "upload_track.json"}, {})
    _check(found == [("C2.2", "example_plugin", "upload_track")], f"C2.2 names the key the overlay cannot find (got {found})")


def _case_c2_4_orphan_plugin_json() -> None:
    print("\nCase 5: C2.4 — plugin payload whose key no imported class registers")
    found = _parity(PLUGIN_SPEC, {}, {_PLUGIN_KB / "orphan.json": "plugin::example_plugin::orphan"})
    _check(found == [("C2.4", "example_plugin", "orphan")], f"C2.4 names the unknown full key (got {found})")


def _case_c2_3_wrong_plugin_process_key() -> None:
    print("\nCase 6: C2.3 — plugin payload that names no key of its namespace")
    key = "plugin::example_plugin::upload_track"
    found = _parity(PLUGIN_SPEC, {key: _PLUGIN_KB / "upload_track.json"}, {_PLUGIN_KB / "upload_track.json": "service_interface::WRONG::upload_track"})
    _check(found == [("C2.3", "example_plugin", "upload_track")], f"C2.3 when the overlay's file carries a non-plugin key (got {found})")


def _case_c3_1_nonexistent_call_site() -> None:
    print("\nCase 7: C3.1 — call-site references a non-existent process_key")
    ref = CallSiteRef(
        namespace="service_interface",
        provider="ghost_service",
        verb="nope",
        file_path=REPO_ROOT / "ananta/src/ananta/example.py",
        lineno=42,
    )
    findings = _classify_call_site_ref(ref, set(), {}, {})
    _check(any(f.check_id == "C3.1" for f in findings), "emits C3.1 when call-site references unknown service")


def _case_c3_2_bound_plugin_namespace() -> None:
    print("\nCase 8: C3.2 — call-site references a bound-plugin namespace (Task #21)")
    plugin_surface = PluginSurface(
        name="actr_memory_plugin",
        plugin_py=REPO_ROOT / "plugins/actr_memory_plugin/src/actr_memory_plugin/plugin.py",
        decorated={"remember": _synthetic_func("remember")},
        kb_jsons={"remember": Path("/tmp/fake.json")},
    )
    ref = CallSiteRef(
        namespace="plugin",
        provider="actr_memory_plugin",
        verb="remember",
        file_path=REPO_ROOT / "ananta/src/ananta/example.py",
        lineno=100,
    )
    bound = {"actr_memory_plugin"}
    findings = _classify_call_site_ref(
        ref,
        bound,
        {},
        {"actr_memory_plugin": plugin_surface},
    )
    _check(any(f.check_id == "C3.2" for f in findings), "emits C3.2 when call-site references bound-plugin namespace")


def _case_c5_1_scheduling_rpk_missing() -> None:
    print("\nCase 9: C5.1 — scheduling action literal omits result_processor_kind")
    with tempfile.TemporaryDirectory() as tmpdir:
        src = Path(tmpdir) / "fixture.py"
        src.write_text("self._scheduling_service.create_cron_schedule(\n    cron_expression='*/5 * * * *',\n    actions=[{\n        'process_key': 'service_interface::x::y',\n        'arguments': {},\n    }],\n)\n")
        findings = check_scheduling_rpk_propagation([Path(tmpdir)])
        _check(any(f.check_id == "C5.1" for f in findings), "emits C5.1 when scheduling action dict omits result_processor_kind")


def _case_c5_1_scheduling_rpk_present_is_clean() -> None:
    print("\nCase 10: C5.1 — scheduling action with rpk emits no finding")
    with tempfile.TemporaryDirectory() as tmpdir:
        src = Path(tmpdir) / "fixture.py"
        src.write_text("self._scheduling_service.create_cron_schedule(\n    cron_expression='*/5 * * * *',\n    actions=[{\n        'process_key': 'service_interface::x::y',\n        'arguments': {},\n        'result_processor_kind': 'inference',\n    }],\n)\n")
        findings = check_scheduling_rpk_propagation([Path(tmpdir)])
        _check(not findings, "no C5.1 finding when result_processor_kind is set")


def _case_allowlist_wildcard_and_exact() -> None:
    print("\nCase 11: Allowlist coverage — wildcard + exact-match semantics")
    allowlist = Allowlist(
        frozenset(
            {
                AllowlistEntry("C2.4", "example_plugin", "*"),
                AllowlistEntry("C3.1", "ananta/src/x.py", "plugin::y::z"),
            }
        )
    )
    wildcard_finding = Finding("C2.4", "example_plugin", "anything", "msg")
    exact_finding = Finding("C3.1", "ananta/src/x.py", "plugin::y::z", "msg")
    miss_finding = Finding("C3.1", "ananta/src/x.py", "plugin::OTHER::z", "msg")
    _check(allowlist.covers(wildcard_finding), "wildcard `*` covers any specifier under same (check, scope)")
    _check(allowlist.covers(exact_finding), "exact specifier match is covered")
    _check(not allowlist.covers(miss_finding), "exact specifier mismatch is NOT covered")


def _case_allowlist_file_round_trip() -> None:
    print("\nCase 12: Allowlist file parses and round-trips correctly")
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "allow.txt"
        _tag = "  # owner: smoke-fixture reason: tracked test debt expires: 2099-01-01\n"
        path.write_text("# comment\n\nC1.5::svc::method" + _tag + "C2.4::plugin::*" + _tag + "C3.1::a/b/c.py::plugin::x::y" + _tag)
        loaded = load_allowlist(path)
        _check(len(loaded.entries) == 3, "parses 3 entries, skipping comment + blank line")
        _check(any(e.check_id == "C1.5" for e in loaded.entries), "C1.5 entry parsed")
        _check(any(e.specifier == "plugin::x::y" for e in loaded.entries), "specifier with embedded `::` parsed verbatim")


def _case_negative_fixture_marker() -> None:
    print("\nCase 14: C3.* negative-fixture marker — honored in tests/, IGNORED in src/")
    marked = 'KEY = "plugin::ghost_plugin::no_such_verb"  # wint:negative-fixture\n'
    plain = 'OTHER = "plugin::ghost_plugin::also_missing"\n'
    with tempfile.TemporaryDirectory() as tmpdir:
        # A tests/ file: the marked line's ref is suppressed; a plain line on
        # the SAME file is still scanned (line-scoped, not a blanket wildcard).
        test_file = Path(tmpdir) / "plugins" / "x" / "tests" / "y_smoke.py"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text(marked + plain, encoding="utf-8")
        verbs = {r.verb for r in _scan_single_file_for_refs(test_file)}
        _check("no_such_verb" not in verbs, "marker suppresses the marked line's ref in a tests/ file")
        _check("also_missing" in verbs, "unmarked line on the same file STILL scanned (line-scoped)")

        # A src/ file: the marker is IGNORED — the ref is still detected. This
        # is the red-first guarantee that the convention cannot weaken src.
        src_file = Path(tmpdir) / "plugins" / "x" / "src" / "y.py"
        src_file.parent.mkdir(parents=True, exist_ok=True)
        src_file.write_text(marked, encoding="utf-8")
        src_verbs = {r.verb for r in _scan_single_file_for_refs(src_file)}
        _check("no_such_verb" in src_verbs, "RED-FIRST: marker IGNORED in src/ — the ref is STILL detected")

    _check(_is_test_path(Path("plugins/x/tests/y_smoke.py")), "_is_test_path True for plugins/<x>/tests/…")
    _check(_is_test_path(Path("ananta/tests/platform/z_smoke.py")), "_is_test_path True for ananta/tests/…")
    _check(not _is_test_path(Path("plugins/x/src/x/y.py")), "_is_test_path False for a src/ path")


_C6_MANIFEST = """\
schema_version: 1
solet_name: smokebot
universal:
  files: [root_manifest.yaml]
  directories: []
platform_managed:
  directories: [.git]
sanctioned: []
overrides: []
diagnostic:
  report_categories: [unknown_root_entries]
  ignore_patterns: []
"""


def _fixture_git(root: Path, *arguments: str) -> None:
    import os

    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="smoke",
        GIT_AUTHOR_EMAIL="smoke@example.invalid",
        GIT_COMMITTER_NAME="smoke",
        GIT_COMMITTER_EMAIL="smoke@example.invalid",
    )
    subprocess.run(("git", *arguments), cwd=root, env=env, check=True, capture_output=True)


def _case_c6_1_untracked_root_entry_does_not_block() -> None:
    """iss_ce45713b: C6.1 blocks on drift the repository CARRIES, never on a
    neighbouring lane's untracked scratch file — and the discriminator is that
    the SAME entry, once tracked, blocks exactly as before."""
    print("\nCase 17: C6.1 — untracked root entry reported, not blocking; tracked one blocks")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        (root / "root_manifest.yaml").write_text(_C6_MANIFEST)
        _fixture_git(root, "init", "-q")
        _fixture_git(root, "add", "-A")
        _fixture_git(root, "commit", "-qm", "base")
        _check(wint.check_root_manifest_drift(root) == [], "declared-only tree yields no C6 findings")

        (root / "HANDOVER_NOTE.md").write_text("another lane's note\n")
        findings = wint.check_root_manifest_drift(root)
        _check(findings == [], f"untracked root entry yields no blocking finding (got {findings})")

        _fixture_git(root, "add", "HANDOVER_NOTE.md")
        findings = wint.check_root_manifest_drift(root)
        _check([(f.check_id, f.scope_qualifier, f.specifier) for f in findings] == [("C6.1", "root", "HANDOVER_NOTE.md")], f"the same entry, staged, is C6.1::root::HANDOVER_NOTE.md (got {findings})")

        _fixture_git(root, "commit", "-qm", "undeclared root entry")
        findings = wint.check_root_manifest_drift(root)
        _check([(f.check_id, f.scope_qualifier, f.specifier) for f in findings] == [("C6.1", "root", "HANDOVER_NOTE.md")], f"the same entry, committed, is still C6.1 (got {findings})")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        (root / "root_manifest.yaml").write_text(_C6_MANIFEST)
        (root / "HANDOVER_NOTE.md").write_text("no git here at all\n")
        findings = wint.check_root_manifest_drift(root)
        _check([(f.check_id, f.specifier) for f in findings] == [("C6.1", "HANDOVER_NOTE.md")], f"with no git directory every unknown entry still blocks (got {findings})")


def _case_e2e_live_gate_clean() -> None:
    print("\nCase 13: end-to-end — live gate against the real tree returns exit 0")
    gate = REPO_ROOT / "quality_gates" / "whole_tree_integration_gate.py"
    allowlist = REPO_ROOT / "quality_gates" / "whole_tree_integration_gate_allowlist.txt"
    venv = REPO_ROOT / ".venv" / "bin" / "python3"
    if not venv.exists():
        _check(True, "skipped (no venv at .venv/bin/python3)")
        return
    result = subprocess.run(
        [str(venv), str(gate), "--allowlist", str(allowlist)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    lines = result.stdout.splitlines()
    blocking = [line for line in lines if re.match(r"C\d+\.\d+::", line) and not line.endswith("[allowlisted]")]
    _check(result.returncode == 0 and blocking == [], f"live gate exits 0 with shipped allowlist (rc={result.returncode}, unallowlisted {blocking})")
    _check("OK:" in result.stdout, "live gate prints OK: summary line")
    _check_no_satisfied_allowance(lines)


def _check_no_satisfied_allowance(lines: list[str]) -> None:
    """Every shipped C1/C2 allowance still covers a live finding (decidable only on a complete catalog)."""
    catalog = next((line for line in lines if line.startswith("CATALOG:")), "")
    if "coverage complete" not in catalog:
        print(f"ADVISORY: live catalog incomplete, satisfied-allowance check not decidable: {catalog}")
        return
    satisfied = [line for line in lines if line.startswith("ADVISORY: satisfied allowance")]
    _check(satisfied == [], f"satisfied_allowance_retired (got {satisfied})")


def _case_c3_executable_context_only() -> None:
    print("\nCase 15: C3.* — a key quoted in PROSE is not a reference")
    # Every shape below carries a real, correctly-spelled process key. What
    # separates them is CONTEXT, which is exactly what the pre-2026-09-05
    # raw-line detector could not see (iss_3dc2ecc3).
    source = (
        '''"""Module docstring mentioning \'plugin::ghost_plugin::doc_verb\'."""
'''
        "\n"
        "# A comment naming 'plugin::ghost_plugin::comment_verb' as an example.\n"
        "\n"
        "def f() -> None:\n"
        '    """Returns: Process key string (e.g., \'plugin::ghost_plugin::inner_doc_verb\').\n'
        '    """\n'
        "\n"
        "META = Param(\n"
        "    description=(\n"
        '        "The fully-qualified process identifier. "\n'
        "        \"Format: 'provider_type::provider::function_name' \"\n"
        "        \"(e.g., 'plugin::ghost_plugin::kwarg_verb')\"\n"
        "    ),\n"
        ")\n"
        "\n"
        "# Positive controls — whole-literal keys in executable position.\n"
        'ROUTE = {"target": "plugin::ghost_plugin::dict_value_verb"}\n'
        'call("plugin::ghost_plugin::call_arg_verb")\n'
        "\n"
        "# Assembled keys — deliberately OUT of scope.\n"
        'FSTR = f"plugin::ghost_plugin::fstring_{suffix}"\n'
        'IMPL = ("plugin::ghost_plugin::"\n'
        '        "implicit_concat_verb")\n'
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        mod = Path(tmpdir) / "plugins" / "x" / "src" / "prose.py"
        mod.parent.mkdir(parents=True, exist_ok=True)
        mod.write_text(source, encoding="utf-8")
        verbs = {r.verb for r in _scan_single_file_for_refs(mod)}

    # Prose — each of these was a Group A/C born-clone false positive.
    _check("doc_verb" not in verbs, "module docstring key is NOT a reference")
    _check("comment_verb" not in verbs, "comment key is NOT a reference")
    _check("inner_doc_verb" not in verbs, "function docstring key is NOT a reference")
    _check("kwarg_verb" not in verbs, "`description=` format example is NOT a reference")
    # Positive controls — these MUST still fire, or the fix over-pruned and the
    # gate has quietly stopped detecting the drift it exists to catch.
    _check("dict_value_verb" in verbs, "POSITIVE CONTROL: whole-literal key as a dict value still detected")
    _check("call_arg_verb" in verbs, "POSITIVE CONTROL: whole-literal key as a call argument still detected")
    # Assembled — documented boundary.
    _check("fstring_" not in verbs, "f-string prefix is NOT reported as a whole key")
    _check("implicit_concat_verb" not in verbs, "implicit adjacent-literal concatenation is NOT reported")


def _write_fixture_clone(root: Path, *, bundle: str | None, template: str | None) -> None:
    """Materialize the minimum a born clone carries for bindings resolution."""
    if bundle is not None:
        (root / "PROVENANCE.json").write_text(
            json.dumps({"bundle": {"name": bundle, "platform": "macos"}}),
            encoding="utf-8",
        )
    if template is not None:
        tdir = root / "plugins" / "github_midwife_plugin" / "knowledge_base" / "profile_templates"
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / f"{template}.yaml").write_text(
            "service_bindings:\n  state_service: postgres_state_management_plugin\n  vault_service: macos_vault_plugin\n",
            encoding="utf-8",
        )


def _case_bindings_fail_loud() -> None:
    print("\nCase 16: bindings — derived from the shipped template, never a silent {}")
    with tempfile.TemporaryDirectory() as tmpdir:
        # (a) No bindings file AND no provenance: the gate cannot know which
        # profile to measure against. Before this fix it returned {} and every
        # bound-plugin ref reclassified C3.2 -> C3.1 (iss_06a5d953).
        bare = Path(tmpdir) / "bare"
        bare.mkdir()
        try:
            _bindings_from_profile_template(bare)
            _check(False, "no bindings + no provenance raises (got a value instead)")
        except BindingsResolutionError:
            _check(True, "no bindings + no provenance raises BindingsResolutionError")
        _check(issubclass(BindingsResolutionError, RuntimeError), "BindingsResolutionError is a RuntimeError, so `main` renders it as exit 2")

        # (b) Provenance present: bindings equal the shipped template's mapping.
        clone = Path(tmpdir) / "clone"
        clone.mkdir()
        _write_fixture_clone(clone, bundle="macos-bizops", template="macos-bizops")
        derived = _bindings_from_profile_template(clone)
        _check(derived == {"state_service": "postgres_state_management_plugin", "vault_service": "macos_vault_plugin"}, "bindings derived from the profile template genesis materializes from")

        # (c) Provenance present but the template is missing: still loud.
        headless = Path(tmpdir) / "headless"
        headless.mkdir()
        _write_fixture_clone(headless, bundle="macos-bizops", template=None)
        try:
            _bindings_from_profile_template(headless)
            _check(False, "missing template raises (got a value instead)")
        except RuntimeError:
            _check(True, "missing profile template raises (harness error, not {})")

        # (d) End-to-end: the gate process really exits 2, not just the helper.
        # REPO_ROOT/_DEFAULT_BINDINGS are module constants, so point them at a
        # tree with neither bindings nor provenance and drive `main` itself.
        empty = Path(tmpdir) / "empty"
        empty.mkdir()
        saved_root, saved_bindings = wint.REPO_ROOT, wint._DEFAULT_BINDINGS
        try:
            wint.REPO_ROOT = empty
            wint._DEFAULT_BINDINGS = empty / "profile" / "config" / "service_bindings.json"
            rc = wint.main(["--skip-call-sites", "--skip-scheduling"])
        finally:
            wint.REPO_ROOT, wint._DEFAULT_BINDINGS = saved_root, saved_bindings
        _check(rc == 2, f"live gate exits 2 when bindings are unresolvable (got {rc})")


def _generated_fixture(cls: type[Any]) -> type[Any]:
    """Model a class decorator attaching metadata after the class body."""

    def generated(self: object, params: dict[str, object], state: dict[str, object]) -> None:
        pass

    setattr(cls, "generated_attribute", platform_process(name="generated")(generated))  # noqa: B010
    return cls


class _CensusParent:
    @platform_process(name="inherited")
    def inherited(self, params: dict[str, object], state: dict[str, object]) -> None:
        pass


@_generated_fixture
class _CensusFixture(_CensusParent):
    name = "fixture"

    @platform_process(name="literal")
    def literal_attribute(self, params: dict[str, object], state: dict[str, object]) -> None:
        pass

    @platform_process(name="evaluate_input")
    def evaluate_input(self, params: dict[str, object], state: dict[str, object]) -> None:
        pass

    @platform_process(name="private")
    def _private(self, params: dict[str, object], state: dict[str, object]) -> None:
        pass

    @platform_process(name="skipped")
    def cleanup(self, params: dict[str, object], state: dict[str, object]) -> None:
        pass

    @property
    def dangerous(self) -> object:
        raise AssertionError("property getter must never execute")

    def get_available_actions(self) -> list[ActionMetadata]:
        return discover_actions(cast(Any, self))


class _CensusValidator:
    def validate_edge_process_provider(self, *args: object) -> None:
        pass


class _CensusMetadata:
    def generate_input_contract(self, action: ActionMetadata) -> dict[str, object]:
        return {"input": action.name}

    def generate_action_blueprint(self, key: str, action: ActionMetadata) -> dict[str, object]:
        return {"key": key, "function": action.function}


class _CensusSchema:
    def generate(self, *, process_key: str, parameters_dict: object) -> dict[str, object]:
        return {"key": process_key, "parameters": parameters_dict}


class _CensusBindings:
    def __init__(self, bound: bool) -> None:
        self.bound = bound

    def is_plugin_bound_to_service(self, name: str) -> bool:
        return self.bound


class _CensusManager:
    def __init__(self, fixture: _CensusFixture, bound: bool) -> None:
        self.plugins = {fixture.name: fixture}
        self.orchestrator_ref = self
        self.service_bindings = _CensusBindings(bound)


def _fixture_registry(fixture: _CensusFixture, bound: bool = False) -> dict[str, object]:
    scanner = PluginProcessScanner(
        cast(Any, _CensusManager(fixture, bound)),
        cast(Any, _CensusValidator()),
        cast(Any, _CensusMetadata()),
        cast(Any, _CensusSchema()),
    )
    registry: dict[str, object] = {"processes": {}}
    scanner.scan_and_register(registry)
    return cast(dict[str, object], registry["processes"])


def _expected_fixture_entry(name: str, function: str) -> dict[str, object]:
    """Frozen pre-extraction entry payload, including derived documentation."""
    key = f"plugin::fixture::{name}"
    return {
        "provider_type": "plugin",
        "provider": "fixture",
        "function_name": name,
        "name": name,
        "display_name": "",
        "description": "",
        "embedding_description": "",
        "is_discoverable": True,
        "parameters": {},
        "parameter_schema": {},
        "return_value_schema": None,
        "is_inference_capable": False,
        "is_enabled": True,
        "is_long_running": False,
        "is_async": False,
        "input_contract": {"input": name},
        "action_blueprint": {"key": key, "function": function},
        "requires_result_processor": False,
        "processor_policy_category": None,
        "chaining_guidance": [],
        "work_count_impact": 1,
        "include_in_system_prompt": False,
        "planning_docs": {"process_key": key, "summary": "", "description": "", "parameters": {"type": "object", "required": [], "properties": {}}, "is_inference_capable": False},
        "error_handling_docs": {"process_key": key, "summary": ""},
        "response_handling_docs": {"process_key": key, "summary": ""},
        "invocation_schema": {"key": key, "parameters": {}},
    }


def check_runtime_same() -> None:
    """Pin all entry fields and real service-binding policy with fake collaborators."""
    expected = {f"plugin::fixture::{name}": _expected_fixture_entry(name, function) for name, function in (("generated", "generated"), ("inherited", "inherited"), ("literal", "literal_attribute"))}
    registry = _fixture_registry(_CensusFixture())
    _check("plugin::fixture::evaluate_input" not in registry, "evaluate_input_absent")  # wint:negative-fixture
    _check(registry == expected, "runtime_entries_same")

    class Provider(_CensusFixture):
        service_interfaces: tuple[type[object], ...] = ()
        supported_interface_versions: dict[type[object], str] = {}

    _check(_fixture_registry(Provider(), bound=True) == {}, "bound_provider_skipped")
    _check(_fixture_registry(Provider()) == expected, "unbound_provider_kept")


def check_instance_surface() -> None:
    fixture = _CensusFixture()

    @platform_process(name="instance_only")
    def instance_action(params: dict[str, object], state: dict[str, object]) -> None:
        pass

    setattr(fixture, "instance_action", instance_action)  # noqa: B010
    actions = discover_actions(cast(Any, fixture))  # pyright: ignore[reportUnnecessaryCast]
    _check("instance_only" in {a.name for a in actions}, "instance_surface_kept")
    original = getattr(_CensusFixture.literal_attribute, "_platform_process_metadata")  # noqa: B009
    copied = next(a for a in actions if a.name == "literal")
    _check(copied is not original and copied.plugin == "fixture" and original.plugin == "", "metadata_copy_attached")

    class Invalid:
        name = "invalid"

        @platform_process(name="invalid")
        def invalid(self, wrong: object) -> None:
            pass

    try:
        discover_actions(cast(Any, Invalid()))
    except PluginError as error:
        _check(error.error_code == "invalid.invalid_action_signature", "invalid_signature_rejected")
    else:
        _check(False, "invalid_signature_rejected")


def check_class_projection() -> None:
    module_name = "ananta.core.process_registry.registration_census"
    available = importlib.util.find_spec(module_name) is not None
    _check(available, "shared_projection_available")
    if not available:
        return
    census = importlib.import_module(module_name)
    projection = census.class_registration_census(_CensusFixture, "fixture")
    names = {action.name for action in projection.actions}
    _check("generated" in names, "generated_metadata_present")
    _check(names == {"generated", "inherited", "literal"}, "class_projection_filtered")
    runtime = [a for a in discover_actions(cast(Any, _CensusFixture())) if a.name != "evaluate_input"]  # pyright: ignore[reportUnnecessaryCast]
    _check(list(projection.actions) == runtime, "class_runtime_metadata_same")
    _check("instance_only_actions_unmeasured" in projection.coverage_limits and "get_available_actions_overrides_unmeasured" in projection.coverage_limits, "class_coverage_limits_explicit")
    check_inherited(census)
    check_provider_key(census)
    _check_real_audio_projection(census)


def check_inherited(census: Any) -> None:
    projection = census.class_registration_census(_CensusFixture, "fixture")
    _check("inherited" in {a.name for a in projection.actions}, "inherited_metadata_present")
    literal = next(a for a in projection.actions if a.name == "literal")
    _check(literal.function == "literal_attribute" and literal.plugin == "fixture", "metadata_name_preserved")

    class Descriptor:
        def __get__(self, instance: object, owner: type[object]) -> object:
            raise AssertionError("class descriptor must never execute")

    class DescriptorFixture(_CensusFixture):
        descriptor = Descriptor()

    safe = census.class_registration_census(DescriptorFixture, "fixture")
    _check(safe.actions == projection.actions, "class_descriptor_not_executed")


def check_provider_key(census: Any) -> None:
    actions = census.class_registration_census(_CensusFixture, "fixture").actions
    action = next(a for a in actions if a.name == "literal")
    keys = {census.plugin_process_key(provider, action) for provider in ("one", "two")}
    _check(keys == {"plugin::one::literal", "plugin::two::literal"}, "two_provider_keys_distinct")  # wint:negative-fixture


def _check_real_audio_projection(census: Any) -> None:
    sys.path.insert(0, str(REPO_ROOT / "plugins" / "audio_processing_plugin" / "src"))
    try:
        audio = importlib.import_module("audio_processing_plugin.plugin")
    except ImportError as error:
        print(f"ADVISORY: real audio class projection unavailable: {type(error).__name__}: {error}")
        return
    projection = census.class_registration_census(audio.AudioProcessingPlugin, "audio_processing_plugin")
    _check("ffmpeg_a3dscope" in {a.name for a in projection.actions}, "real_audio_generated_metadata")


_GENERATED_PLUGIN = '''
from ananta.core.actions.action_metadata import platform_process
from ananta.core.plugins.plugin_base import PluginBase


def _generate(cls):
    def handler(self, params, state):
        return None

    setattr(cls, "generated_attribute", platform_process(name="generated_verb")(handler))
    return cls


@_generate
class FixturePlugin(PluginBase):
    """The class decorator adds the only action after the class body."""
'''


def _write_plugin(root: Path, provider: str, package: str, source: str, payloads: dict[str, str]) -> Path:
    """Declare `provider` as an ananta.plugins entry point with KB JSON payloads by stem."""
    plugin_dir = root / "plugins" / package
    (plugin_dir / "src" / package).mkdir(parents=True)
    (plugin_dir / "pyproject.toml").write_text(f'[project.entry-points."ananta.plugins"]\n{provider} = "{package}.plugin:FixturePlugin"\n')
    (plugin_dir / "src" / package / "__init__.py").write_text("")
    (plugin_dir / "src" / package / "plugin.py").write_text(source)
    kb_dir = plugin_dir / "knowledge_base" / "processes"
    kb_dir.mkdir(parents=True)
    for stem, key in payloads.items():
        (kb_dir / f"{stem}.json").write_text(json.dumps({"process_key": key}))
    return kb_dir


def _tree_gate(root: Path) -> tuple[int, dict[str, Any]]:
    """Run the real gate entry point (C1/C2 only) against a synthetic tree."""
    bindings = root / "profile" / "config" / "service_bindings.json"
    bindings.parent.mkdir(parents=True, exist_ok=True)
    bindings.write_text("{}")
    names = ("REPO_ROOT", "_PLUGINS_DIR", "_SERVICES_DIR", "_SERVICE_KB_DIR", "_DEFAULT_BINDINGS")
    saved = [getattr(wint, name) for name in names]
    patched = (root, root / "plugins", root / "ananta/src/ananta/services", root / "ananta/knowledge_base/processes", bindings)
    out = io.StringIO()
    try:
        for name, value in zip(names, patched, strict=True):
            setattr(wint, name, value)
        with contextlib.redirect_stdout(out):
            rc = wint.main(["--json", "--skip-call-sites", "--skip-scheduling"])
    finally:
        for name, value in zip(names, saved, strict=True):
            setattr(wint, name, value)
    return rc, json.loads(out.getvalue())


def _plugin_findings(payload: dict[str, Any]) -> list[tuple[str, str, str]]:
    return sorted((f["check_id"], f["scope_qualifier"], f["specifier"]) for f in payload["findings"] if f["check_id"].startswith("C2."))


_INHERITED_BASE = """
from ananta.core.actions.action_metadata import platform_process
from ananta.core.plugins.plugin_base import PluginBase


class Parent(PluginBase):
    @platform_process(name="inherited_verb")
    def inherited_verb(self, params, state):
        return None
"""

_INHERITED_PLUGIN = '''
from .base import Parent


class FixturePlugin(Parent):
    """Every action comes from the parent class."""
'''

_RENAMED_PLUGIN = """
from ananta.core.actions.action_metadata import platform_process
from ananta.core.plugins.plugin_base import PluginBase


class FixturePlugin(PluginBase):
    @platform_process(name="shared_verb")
    def attribute_name(self, params, state):
        return None
"""

_OPTIONAL_PLUGIN = 'raise ImportError("optional dependency wint_missing_codec is not installed")\n'

_DYNAMIC_PLUGIN = """
from ananta.core.plugins.plugin_base import PluginBase


class FixturePlugin(PluginBase):
    def get_available_actions(self):
        return []
"""


def check_generated_pair() -> None:
    print("\nCase 19: generated class-decorator action with its JSON is registered, not orphaned")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        kb_dir = _write_plugin(root, "wint_generated", "wint_generated_plugin", _GENERATED_PLUGIN, {"generated_verb": "plugin::wint_generated::generated_verb"})
        rc, payload = _tree_gate(root)
        findings = _plugin_findings(payload)
        _check(rc == 0 and findings == [], f"generated_is_registered (rc={rc}, got {findings})")
        entries = catalog_mod.build_plugin_catalog(root).entries
        _check(entries == {"plugin::wint_generated::generated_verb": kb_dir / "generated_verb.json"}, f"generated_key_and_overlay_path_exact (got {entries})")
        check_missing_json(root, kb_dir)
        check_unknown_json(root, kb_dir)


def check_missing_json(root: Path, kb_dir: Path) -> None:
    print("\nCase 20: deleting the generated action's JSON is a forward finding")
    payload_file = kb_dir / "generated_verb.json"
    saved = payload_file.read_text()
    payload_file.unlink()
    rc, payload = _tree_gate(root)
    payload_file.write_text(saved)
    messages = " ".join(f["message"] for f in payload["findings"])
    _check(rc == 1 and _plugin_findings(payload) == [("C2.2", "wint_generated", "generated_verb")] and "wint_generated_plugin/knowledge_base/processes/generated_verb.json" in messages, f"missing_generated_json (got {_plugin_findings(payload)})")


def check_unknown_json(root: Path, kb_dir: Path) -> None:
    print("\nCase 21: a payload no imported class registers is a reverse finding")
    (kb_dir / "ghost_verb.json").write_text(json.dumps({"process_key": "plugin::wint_generated::ghost_verb"}))
    rc, payload = _tree_gate(root)
    _check(rc == 1 and _plugin_findings(payload) == [("C2.4", "wint_generated", "ghost_verb")], f"unknown_json_reported (got {_plugin_findings(payload)})")


def check_inherited_action() -> None:
    print("\nCase 22: an action inherited from a parent class is registered")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        _write_plugin(root, "wint_inherited", "wint_inherited_plugin", _INHERITED_PLUGIN, {"inherited_verb": "plugin::wint_inherited::inherited_verb"})
        (root / "plugins/wint_inherited_plugin/src/wint_inherited_plugin/base.py").write_text(_INHERITED_BASE)
        rc, payload = _tree_gate(root)
    _check(rc == 0 and _plugin_findings(payload) == [], f"inherited_is_registered (rc={rc}, got {_plugin_findings(payload)})")


def check_provider_identity() -> None:
    print("\nCase 23: provider = entry-point name; one verb name in two providers stays two keys")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        one = _write_plugin(root, "wint_one", "wint_identity_one_plugin", _RENAMED_PLUGIN, {"shared_verb": "plugin::wint_one::shared_verb"})
        two = _write_plugin(root, "wint_two", "wint_identity_two_plugin", _RENAMED_PLUGIN, {"shared_verb": "plugin::wint_two::shared_verb"})
        entries = catalog_mod.build_plugin_catalog(root).entries
        rc, payload = _tree_gate(root)
    expected = {"plugin::wint_one::shared_verb": one / "shared_verb.json", "plugin::wint_two::shared_verb": two / "shared_verb.json"}
    _check(entries == expected, f"gate_two_provider_keys_distinct (got {entries})")
    _check(rc == 0 and _plugin_findings(payload) == [], f"metadata_name_keys_the_overlay_path (attribute 'attribute_name', name 'shared_verb'; got {_plugin_findings(payload)})")


def check_payload_key() -> None:
    print("\nCase 24: wrong-provider payload and duplicate identities name exact keys and paths")
    alpha = Path("/catalog-tree/plugins/alpha_plugin/knowledge_base/processes")
    beta = Path("/catalog-tree/plugins/beta_plugin/knowledge_base/processes")
    entries = {"plugin::alpha::verb": alpha / "verb.json", "plugin::beta::other": beta / "other.json"}
    payloads: dict[Path, str | None] = {alpha / "verb.json": "plugin::beta::verb", beta / "verb.json": "plugin::beta::verb", beta / "other.json": "plugin::beta::other"}
    catalog = ProcessCatalog(PLUGIN_SPEC, Path("/catalog-tree"), entries=entries, providers={"alpha", "beta"})
    found = {(f.check_id, f.scope_qualifier, f.specifier): f.message for f in check_parity(catalog, payloads)}
    _check(sorted(found) == [("C2.3", "alpha", "verb"), ("C2.4", "beta", "verb"), ("C2.5", "beta", "verb")], f"payload_mismatch_and_duplicate_named (got {sorted(found)})")
    duplicate = found.get(("C2.5", "beta", "verb"), "")
    _check("plugins/alpha_plugin/knowledge_base/processes/verb.json" in duplicate and "plugins/beta_plugin/knowledge_base/processes/verb.json" in duplicate, f"duplicate_names_both_paths ({duplicate})")


def check_disabled() -> None:
    print("\nCase 25: a disabled service action is not registrable (real scanner, real tree)")
    from ananta.services.io_interface_service.interfaces.public import IOInterfaceServiceAPI

    disabled = sorted(name for name, member in vars(IOInterfaceServiceAPI).items() if getattr(getattr(member, "_service_interface_metadata", None), "is_enabled", True) is False)
    catalog = catalog_mod.build_service_catalog(REPO_ROOT)
    registered = sorted(key for key in catalog.entries if key.startswith("service_interface::io_interface_service::"))
    _check(bool(disabled) and registered == [] and catalog.is_complete(), f"disabled_action_absent (disabled {disabled}, registered {registered}, advisories {catalog.advisories})")


def check_coverage() -> None:
    print("\nCase 26: unmeasurable providers are disclosed with their reason, never findings, never complete")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        _write_plugin(root, "wint_optional", "wint_optional_plugin", _OPTIONAL_PLUGIN, {"codec_verb": "plugin::wint_optional::codec_verb"})
        _write_plugin(root, "wint_dynamic", "wint_dynamic_plugin", _DYNAMIC_PLUGIN, {"dynamic_verb": "plugin::wint_dynamic::dynamic_verb"})
        _write_plugin(root, "wint_bound", "wint_bound_plugin", _RENAMED_PLUGIN, {"shared_verb": "plugin::wint_bound::shared_verb"})
        catalog = catalog_mod.build_plugin_catalog(root)
        rc, payload = _tree_gate(root)
    advisories = " | ".join(payload["advisories"])
    _check(rc == 0 and _plugin_findings(payload) == [], f"unmeasured_providers_are_not_findings (got {_plugin_findings(payload)})")
    _check("wint_missing_codec is not installed" in advisories and "wint_optional_plugin/knowledge_base/processes/codec_verb.json" in advisories, f"lost_coverage_disclosed ({advisories})")
    _check("overrides get_available_actions" in advisories and "wint_dynamic_plugin/knowledge_base/processes/dynamic_verb.json" in advisories, f"dynamic_override_unmeasured ({advisories})")
    _check("coverage INCOMPLETE" in payload["catalog"] and "instance-only actions" in payload["catalog"], f"no_completeness_claim ({payload['catalog']})")
    summary = catalog_mod.catalog_summary([catalog], {"wint_bound"})
    _check("plugin::wint_bound::shared_verb" in catalog.entries and "1 bound plugin providers" in summary, f"bound_provider_catalog_artifact ({summary})")


_FIXTURE_TAG = "  # owner: smoke-fixture reason: tracked test debt expires: 2099-01-01"


def _git_out(root: Path, *arguments: str) -> str:
    return subprocess.run(("git", *arguments), cwd=root, capture_output=True, text=True, check=True).stdout.strip()


def _write_if_unchanged(root: Path, path: Path, expected_blob: str, text: str) -> bool:
    """Optimistic concurrency for a source edit: write only onto the blob the edit was made against."""
    if _git_out(root, "hash-object", str(path)) != expected_blob:
        return False
    path.write_text(text)
    return True


def check_allowance_lifecycle() -> None:
    print("\nCase 27: allowance lifecycle — retire, retain, correct, reverse, refuse a stale write")
    satisfied, unrelated = "C2.4::wint_p::gone_verb", "C3.1::x.py::plugin::y::z"
    live = [Finding("C3.1", "x.py", "plugin::y::z", "msg")]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        path = root / "allow.txt"
        path.write_text(f"{satisfied}{_FIXTURE_TAG}\n{unrelated}{_FIXTURE_TAG}\n")
        for step in (("init", "-q"), ("add", "-A"), ("commit", "-qm", "base")):
            _fixture_git(root, *step)
        base_blob = _git_out(root, "rev-parse", "HEAD:allow.txt")
        _check(satisfied_allowances(load_allowlist(path), live) == [satisfied], "satisfied_allowance_detected")
        wildcard = Allowlist(frozenset({AllowlistEntry("C2.4", "wint_p", "*")}))
        _check(satisfied_allowances(wildcard, [Finding("C2.4", "wint_p", "live_verb", "msg")]) == [], "wildcard_covering_a_finding_is_not_satisfied")
        retired = f"{unrelated}{_FIXTURE_TAG}\n"
        _check(_write_if_unchanged(root, path, base_blob, retired) and satisfied_allowances(load_allowlist(path), live) == [], "satisfied_entry_retired")
        _fixture_git(root, "commit", "-qam", "retire")
        _check(path.read_text().splitlines() == [f"{unrelated}{_FIXTURE_TAG}"], "unrelated_debt_byte_identical")
        path.write_text(retired.replace("tracked test debt", "corrected reason"))
        _fixture_git(root, "commit", "-qam", "correct")
        _check(load_allowlist(path).covers(live[0]) and "tracked test debt" in _git_out(root, "show", "HEAD~1:allow.txt"), "correction_keeps_key_and_history")
        path.write_text(path.read_text() + f"{satisfied}  # owner: smoke-fixture reason: fresh measured reintroduction expires: 2099-02-01\n")
        _check(load_allowlist(path).covers(Finding("C2.4", "wint_p", "gone_verb", "msg")), "reversal_covers_fresh_finding")
        current = path.read_text()
        _check(not _write_if_unchanged(root, path, base_blob, "clobbered\n") and path.read_text() == current, "stale_base_write_refused")


def check_born_seed_unshipped_plugins() -> None:
    print("\nCase 29: a born seed discloses references to plugins it does not ship; the origin tree still blocks them")
    absent = Finding("C3.1", "core.py", "plugin::seed_absent_plugin::verb", "msg", "core.py", 7)
    shipped = Finding("C3.1", "core.py", "plugin::seed_shipped_plugin::gone_verb", "msg", "core.py", 8)
    service = Finding("C3.1", "core.py", "service_interface::seed_absent_plugin::verb", "msg", "core.py", 9)
    allowance = Allowlist(frozenset({AllowlistEntry("C2.2", "seed_absent_plugin", "verb"), AllowlistEntry("C2.2", "seed_shipped_plugin", "gone")}))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        (root / "plugins" / "seed_shipped_plugin").mkdir(parents=True)
        kept, advisories = split_unshipped_references([absent, shipped, service], root)
        _check(kept == [absent, shipped, service] and advisories == [], f"origin_tree_keeps_every_c3_1 (got {kept}, {advisories})")
        _check(satisfied_allowances(allowance, [], root) == ["C2.2::seed_absent_plugin::verb", "C2.2::seed_shipped_plugin::gone"], "origin_tree_reports_both_satisfied_allowances")
        (root / "PROVENANCE.json").write_text("{}\n")
        kept, advisories = split_unshipped_references([absent, shipped, service], root)
        _check(kept == [shipped, service], f"seed_keeps_shipped_plugin_and_service_findings (got {kept})")
        _check(advisories == ["seed does not ship seed_absent_plugin: core.py:7 references plugin::seed_absent_plugin::verb (measured as C3.1 on the origin tree)"], f"seed_discloses_unshipped_reference ({advisories})")
        _check(satisfied_allowances(allowance, [], root) == ["C2.2::seed_shipped_plugin::gone"], "seed_unshipped_allowance_is_not_satisfied")


def check_real_audio_catalog() -> None:
    print("\nCase 28: the real audio class's generated ffmpeg_a3dscope key matches its shipped JSON")
    catalog = catalog_mod.build_plugin_catalog(REPO_ROOT)
    key = "plugin::audio_processing_plugin::ffmpeg_a3dscope"
    if not (REPO_ROOT / "plugins/audio_processing_plugin").is_dir() or "audio_processing_plugin" in catalog.unmeasured:
        print(f"ADVISORY: real audio catalog unavailable in this tree: {[a for a in catalog.advisories if 'audio_processing_plugin' in a]}")
        return
    path = catalog.entries.get(key)
    expected = REPO_ROOT / "plugins/audio_processing_plugin/knowledge_base/processes/ffmpeg_a3dscope.json"
    _check(path is not None and path == expected and catalog_mod.collect_payload_keys([path]).get(path) == key, f"real_audio_generated_key_matches_json (got {path})")


def main() -> int:
    print("Whole-tree integration gate meta-smoke (W-INT Cycle 1)")
    print("=" * 60)
    check_generated_pair()
    check_inherited_action()
    check_provider_identity()
    check_payload_key()
    check_disabled()
    check_coverage()
    check_allowance_lifecycle()
    check_born_seed_unshipped_plugins()
    check_runtime_same()
    check_instance_surface()
    check_class_projection()
    _case_c1_5_registered_without_json()
    _case_c1_5_disabled_decorator_surface()
    _case_c1_7_orphan_kb_json()
    _case_c1_6_wrong_process_key_in_json()
    _case_c2_2_plugin_registered_without_json()
    _case_c2_4_orphan_plugin_json()
    _case_c2_3_wrong_plugin_process_key()
    _case_c3_1_nonexistent_call_site()
    _case_c3_2_bound_plugin_namespace()
    _case_c5_1_scheduling_rpk_missing()
    _case_c5_1_scheduling_rpk_present_is_clean()
    _case_allowlist_wildcard_and_exact()
    _case_allowlist_file_round_trip()
    _case_negative_fixture_marker()
    _case_c3_executable_context_only()
    _case_bindings_fail_loud()
    _case_c6_1_untracked_root_entry_does_not_block()
    check_real_audio_catalog()
    _case_e2e_live_gate_clean()

    print("\n" + "-" * 60)
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    if _failed:
        print("\nFailures:")
        for label in _failed:
            print(f"  - {label}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
