#!/usr/bin/env python3
"""Killing tests for the two yaml fragment surfaces (prb_ae6d81ea, chg_46d4755e).

Run:

    .venv/bin/python3 quality_gates/tests/shipped_contract_fragment_union_smoke.py

WHAT THIS PROVES. Declaring a checkout-only smoke used to require editing THREE
shared files -- the ``# not shipped:`` marker in ``quality_gates/gate_smokes.txt``,
the ``checkout_only`` entry in ``shipped_smoke_contract.yaml`` and the
``exclude_paths`` line in ``seed_manifest.yaml`` -- so two candidates declaring
one each invalidated each other's frozen base entries.
``quality_gates/gate_smoke_register.py`` removed that collision for the register;
this candidate removes it for the two yaml surfaces. Each case here measures the
outcome that makes that true:

* a contract row that exists ONLY in a fragment is parsed and honoured;
* an ``exclude_paths`` entry that exists ONLY in a fragment is honoured by
  ``assemble``'s OWN exclusion predicate, so the prune agrees with the agreement;
* the plugin's fragment reader and ``quality_gates``' agree file for file --
  PROVEN here rather than asserted in prose, because a shipped plugin cannot
  import the repository-root dev package and therefore has a reader of its own;
* the union digest that replaced ``toolchain.contract_hash``'s plain blob hash is
  bit-identical for a fragment-free tree and changes when a fragment does;
* a duplicate ``exclude_paths`` entry is a union no-op and NOT a refusal, which
  is the deliberate difference from a duplicate contract ENTRY (that one carries
  a reason and a prerequisite that can disagree, and raises).

The three-surface agreement itself -- every one-surface-missing combination
across main files and fragments -- is measured where the predicate's other cases
already live: ``plugins/seed_factory_plugin/tests/shipped_smoke_contract_smoke.py``
at parser scope and ``quality_gates/tests/smoke_contract_lint_gate_smoke.py``
through the real gate.

Every case writes real yaml and register files to a temporary tree and uses the
production parsers. The aggregate integration fixture pins its environment and
runs the real source-citation gate in a subprocess, with no scanner stub.
Every fixture is a ``TemporaryDirectory``; Git writes are confined to disposable
fixture repositories, with no shared-checkout or live-solet mutation.

DISCLOSED DEPENDENCY. This smoke imports ``seed_factory_plugin``, which ships in
no capability bundle, so it is declared checkout-only -- and it declares itself
through FRAGMENT files, which is this candidate's outcome demonstrating itself.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
# Bind every package this smoke measures to THIS tree: the repository venv is
# shared with the main checkout, so an editable install would resolve the plugin
# to the MAIN checkout's src/ and silently measure code this candidate has not
# changed (the same wrong-tree defect smoke_contract_lint_gate.py defends).
sys.path.insert(0, str(_REPO_ROOT / "plugins/seed_factory_plugin/src"))
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "quality_gates"))

from seed_factory_plugin import knowledge_fragments  # noqa: E402
from seed_factory_plugin.assemble import _is_excluded  # noqa: E402
from seed_factory_plugin.shipped_smoke_contract import (  # noqa: E402
    ShippedSmokeContractError,
    checkout_only_consistency_violations,
    contract_union_digest,
    load_contract,
)
from seed_factory_plugin.shipped_surface import load_seed_manifest  # noqa: E402

from quality_gates import code_quality_check, gate_smoke_register  # noqa: E402
from quality_gates.code_quality_scope import ClassPlan  # noqa: E402

_FAILURES: list[str] = []

_CHECKOUT_ONLY_ROW = """checkout_only:
  - entry: {entry}
    reason_category: fleet_tooling
    prerequisite:
      kind: missing_path
      path: deployment/scripts/fixture.py
"""


def _check(ok: bool, label: str) -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        _FAILURES.append(label)


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _manifest_body(exclude_paths: tuple[str, ...]) -> str:
    return "copy:\n  - quality_gates/\nexclude_paths:\n" + "".join(f"  - {entry}\n" for entry in exclude_paths)


def _build_tree(
    root: Path,
    *,
    contract: str,
    markers: tuple[str, ...] = (),
    exclude_paths: tuple[str, ...] = (),
    contract_fragments: dict[str, str] | None = None,
    manifest_fragments: dict[str, str] | None = None,
    register_fragments: dict[str, str] | None = None,
) -> Path:
    """Write all three real surfaces plus any fragments; return the contract path.

    Exactly the layout ``load_contract`` resolves on a real checkout: the
    manifest beside the contract, the register at the repository root above it,
    and each fragment directory beside its own main file.
    """
    _write(root, "plugins/seed_factory_plugin/knowledge_base/seed_manifest.yaml", _manifest_body(exclude_paths))
    _write(root, "quality_gates/gate_smokes.txt", "".join(f"# not shipped: {entry} fixture marker\n" for entry in markers))
    for name, body in (contract_fragments or {}).items():
        _write(root, f"plugins/seed_factory_plugin/knowledge_base/shipped_smoke_contract.d/{name}", body)
    for name, body in (manifest_fragments or {}).items():
        _write(root, f"plugins/seed_factory_plugin/knowledge_base/seed_manifest.d/{name}", body)
    for name, body in (register_fragments or {}).items():
        _write(root, f"quality_gates/gate_smokes.d/{name}", body)
    return _write(root, "plugins/seed_factory_plugin/knowledge_base/shipped_smoke_contract.yaml", contract)


def _unreadable(path: Path) -> bool:
    """Make ``path`` unreadable; False when the running user can read it anyway.

    A root-owned run can read a 0000 file, so the advisory cases that need a
    genuinely unreadable fragment say so and skip rather than asserting a
    condition the environment cannot produce.
    """
    path.chmod(0o000)
    try:
        path.read_bytes()
    except OSError:
        return True
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return False


def case_fragment_only_contract_row() -> None:
    """Case 1 — a checkout_only row that exists ONLY in a fragment is honoured."""
    print("\nCase 1 — fragment-only contract row")
    entry = "quality_gates/tests/fragment_only_row_fixture_smoke.py"
    with TemporaryDirectory() as raw:
        contract_path = _build_tree(
            Path(raw) / "checkout",
            contract="shipped: []\n",
            markers=(entry,),
            exclude_paths=(entry,),
            contract_fragments={"candidate-a.yaml": _CHECKOUT_ONLY_ROW.format(entry=entry)},
        )
        contract = load_contract(contract_path)
        _check(
            contract.checkout_only_paths() == frozenset({entry}),
            f"the fragment's row is in the effective contract (got {sorted(contract.checkout_only_paths())!r})",
        )
        _check(contract.advisories == (), f"a readable fragment raises no advisory (got {contract.advisories!r})")
        violations = checkout_only_consistency_violations(contract_path)
        _check(violations == [], f"the three surfaces agree with the row in a fragment (got {violations!r})")


def case_fragment_only_marker() -> None:
    """Case 2 — a ``# not shipped:`` marker that exists ONLY in a register fragment."""
    print("\nCase 2 — fragment-only marker")
    entry = "quality_gates/tests/fragment_only_marker_fixture_smoke.py"
    with TemporaryDirectory() as raw:
        contract_path = _build_tree(
            Path(raw) / "checkout",
            contract=_CHECKOUT_ONLY_ROW.format(entry=entry),
            markers=(),
            exclude_paths=(entry,),
            register_fragments={"candidate-b.txt": f"# not shipped: {entry} fixture marker\n{entry}\n"},
        )
        contract = load_contract(contract_path)
        _check(
            contract.checkout_only_markers == frozenset({entry}),
            f"the fragment's marker is in the effective marker surface (got {sorted(contract.checkout_only_markers)!r})",
        )
        violations = checkout_only_consistency_violations(contract_path)
        _check(violations == [], f"the agreement accepts a marker that lives in a fragment (got {violations!r})")


def case_fragment_only_exclusion_prunes() -> None:
    """Case 3 — a fragment ``exclude_paths`` entry is honoured by the REAL prune.

    Not merely by the agreement: ``assemble._is_excluded`` is the single source
    of truth for both the on-disk prune and the manifest-versus-archive diff, so
    the fragment exclusion has to reach THAT predicate or a fragment-declared
    checkout-only smoke would still ship and be red in every born clone.
    """
    print("\nCase 3 — fragment-only exclusion reaches assemble's own predicate")
    entry = "quality_gates/tests/fragment_only_exclusion_fixture_smoke.py"
    other = "quality_gates/tests/not_excluded_fixture_smoke.py"
    with TemporaryDirectory() as raw:
        root = Path(raw) / "checkout"
        contract_path = _build_tree(
            root,
            contract=_CHECKOUT_ONLY_ROW.format(entry=entry),
            markers=(entry,),
            exclude_paths=(),
            manifest_fragments={"candidate-c.yaml": f"exclude_paths:\n  - {entry}\n"},
        )
        manifest = load_seed_manifest(root / "plugins/seed_factory_plugin/knowledge_base/seed_manifest.yaml")
        _check(entry in manifest.exclude_paths, f"the fragment's exclusion is in the effective set (got {sorted(manifest.exclude_paths)!r})")
        _check(_is_excluded(PurePosixPath(entry), manifest), "assemble's own exclusion predicate prunes the fragment-excluded path")
        _check(not _is_excluded(PurePosixPath(other), manifest), "a path no surface excludes is still shipped")
        _check(manifest.advisories == (), f"a readable manifest fragment raises no advisory (got {manifest.advisories!r})")
        violations = checkout_only_consistency_violations(contract_path)
        _check(violations == [], f"the agreement accepts an exclusion that lives in a fragment (got {violations!r})")


def case_duplicate_exclusion_is_a_union_no_op() -> None:
    """Case 4 — a repeated ``exclude_paths`` entry is a no-op, deliberately NOT a refusal.

    The exclusion surface is a SET, so there is no last-wins ambiguity to
    resolve and nothing a candidate could disagree about; refusing here would be
    a gate a candidate excluding an already-excluded path could not pass
    (rul_becb4bed). A duplicate contract ENTRY is the opposite case and raises:
    case 7.
    """
    print("\nCase 4 — duplicate exclusion across fragments is a union no-op")
    entry = "quality_gates/tests/duplicate_exclusion_fixture_smoke.py"
    with TemporaryDirectory() as raw:
        root = Path(raw) / "checkout"
        _build_tree(
            root,
            contract=_CHECKOUT_ONLY_ROW.format(entry=entry),
            markers=(entry,),
            exclude_paths=(entry,),
            manifest_fragments={
                "candidate-d.yaml": f"exclude_paths:\n  - {entry}\n",
                "candidate-e.yaml": f"exclude_paths:\n  - {entry}\n",
            },
        )
        manifest = load_seed_manifest(root / "plugins/seed_factory_plugin/knowledge_base/seed_manifest.yaml")
        _check(manifest.exclude_paths == frozenset({entry}), f"the repeated path appears once (got {sorted(manifest.exclude_paths)!r})")
        _check(manifest.advisories == (), f"a duplicate exclusion is not even an advisory (got {manifest.advisories!r})")


def case_manifest_fragment_extra_key_is_advisory() -> None:
    """Case 5 — a manifest fragment carrying any other key is an ADVISORY, not a refusal.

    A fragment exists so a candidate can exclude its own smoke; widening ``copy``
    would change what every bundle ships, so it is not applied -- but it is
    REPORTED, because silently ignoring it would let a candidate believe it had
    added a copy root.
    """
    print("\nCase 5 — unsupported manifest-fragment key is reported and not applied")
    entry = "quality_gates/tests/extra_key_fixture_smoke.py"
    with TemporaryDirectory() as raw:
        root = Path(raw) / "checkout"
        _build_tree(
            root,
            contract="shipped: []\n",
            manifest_fragments={"candidate-f.yaml": f"copy:\n  - workbench/\nexclude_paths:\n  - {entry}\n"},
        )
        manifest = load_seed_manifest(root / "plugins/seed_factory_plugin/knowledge_base/seed_manifest.yaml")
        _check(manifest.copy == ("quality_gates/",), f"the fragment did not widen the copy allowlist (got {manifest.copy!r})")
        _check(entry not in manifest.exclude_paths, "a fragment the gate refused to apply contributes no exclusion either")
        _check(
            any("candidate-f.yaml" in advisory and "exclude_paths" in advisory for advisory in manifest.advisories),
            f"the advisory names the file and the only supported key (got {manifest.advisories!r})",
        )


def case_missing_fragment_directory_is_silent() -> None:
    """Case 6 — no fragment directory is normal: identical parse, zero advisories."""
    print("\nCase 6 — a missing fragment directory is normal and silent")
    entry = "quality_gates/tests/no_fragments_fixture_smoke.py"
    with TemporaryDirectory() as raw:
        root = Path(raw) / "checkout"
        contract_path = _build_tree(
            root,
            contract=_CHECKOUT_ONLY_ROW.format(entry=entry),
            markers=(entry,),
            exclude_paths=(entry,),
        )
        contract = load_contract(contract_path)
        _check(contract.checkout_only_paths() == frozenset({entry}), "the main file alone parses exactly as before")
        _check(contract.advisories == (), f"no advisory for an absent directory (got {contract.advisories!r})")
        _check(
            knowledge_fragments.fragment_files(contract_path, knowledge_fragments.CONTRACT_FRAGMENT_DIRNAME, ".yaml") == ((), ()),
            "the reader returns no files and no advisories",
        )
        _check(checkout_only_consistency_violations(contract_path) == [], "the agreement is unchanged for a fragment-free tree")


def case_duplicate_contract_entry_across_fragments() -> None:
    """Case 7 — the same entry in two fragments raises and names BOTH files.

    A silent last-wins merge would let two candidates disagree about what ships,
    which is a data-integrity boundary rather than unknown data. A candidate
    writing only its own fragment satisfies this by construction -- case 1 is the
    pass path.
    """
    print("\nCase 7 — duplicate entry across fragments is a named error")
    entry = "quality_gates/tests/duplicate_entry_fixture_smoke.py"
    for label, fragments, main in (
        (
            "two fragments",
            {"candidate-g.yaml": _CHECKOUT_ONLY_ROW.format(entry=entry), "candidate-h.yaml": _CHECKOUT_ONLY_ROW.format(entry=entry)},
            "shipped: []\n",
        ),
        (
            "main file and a fragment",
            {"candidate-i.yaml": _CHECKOUT_ONLY_ROW.format(entry=entry)},
            _CHECKOUT_ONLY_ROW.format(entry=entry),
        ),
    ):
        with TemporaryDirectory() as raw:
            contract_path = _build_tree(
                Path(raw) / "checkout",
                contract=main,
                markers=(entry,),
                exclude_paths=(entry,),
                contract_fragments=fragments,
            )
            try:
                load_contract(contract_path)
                _check(False, f"{label}: a duplicate entry was accepted")
            except ShippedSmokeContractError as error:
                message = str(error)
                _check(entry in message, f"{label}: the refusal names the duplicated entry")
                _check(
                    all(name in message for name in (sorted(fragments)[0], "first in")) or "shipped_smoke_contract" in message,
                    f"{label}: the refusal names where both declarations live (got {message!r})",
                )


def case_unreadable_contract_fragment_is_advisory() -> None:
    """Case 8 — an unreadable or malformed contract fragment is an ADVISORY.

    The rows that COULD be read stay in effect and the battery proceeds; only the
    main file is required.
    """
    print("\nCase 8 — unreadable and malformed contract fragments are advisories")
    good = "quality_gates/tests/readable_fragment_fixture_smoke.py"
    with TemporaryDirectory() as raw:
        root = Path(raw) / "checkout"
        contract_path = _build_tree(
            root,
            contract="shipped: []\n",
            markers=(good,),
            exclude_paths=(good,),
            contract_fragments={
                "candidate-j.yaml": _CHECKOUT_ONLY_ROW.format(entry=good),
                "candidate-k.yaml": "checkout_only: [  # unterminated flow sequence\n",
            },
        )
        contract = load_contract(contract_path)
        _check(good in contract.checkout_only_paths(), "the readable fragment's row is still in effect")
        _check(
            any("candidate-k.yaml" in advisory for advisory in contract.advisories),
            f"the malformed fragment is an advisory naming the file (got {contract.advisories!r})",
        )
        _check(checkout_only_consistency_violations(contract_path) == [], "a malformed fragment is not a finding")

        broken = root / "plugins/seed_factory_plugin/knowledge_base/shipped_smoke_contract.d/candidate-k.yaml"
        if _unreadable(broken):
            reread = load_contract(contract_path)
            _check(
                any("candidate-k.yaml" in advisory and "unreadable" in advisory for advisory in reread.advisories),
                f"an unreadable fragment is an advisory naming the file (got {reread.advisories!r})",
            )
            _check(good in reread.checkout_only_paths(), "its readable sibling's row is still in effect")
            broken.chmod(stat.S_IRUSR | stat.S_IWUSR)
        else:
            print("    NOTE: this user can read a 0000 file; the unreadable-fragment half is not measurable here")


def case_union_digest_definition() -> None:
    """Case 9 — the union digest that replaced ``toolchain.contract_hash``'s blob hash.

    Bit-identical with no fragment, so every signature already issued keeps its
    exact meaning; sensitive to a fragment's bytes, its name and the set itself,
    so a fragment cannot change what the gate measured without changing the key.
    """
    print("\nCase 9 — union digest: unchanged without fragments, sensitive with them")
    entry = "quality_gates/tests/digest_fixture_smoke.py"
    row = _CHECKOUT_ONLY_ROW.format(entry=entry)
    with TemporaryDirectory() as raw:
        root = Path(raw) / "checkout"
        contract_path = _build_tree(root, contract="shipped: []\n", markers=(entry,), exclude_paths=(entry,))
        plain = hashlib.sha256(contract_path.read_bytes()).hexdigest()
        bare = contract_union_digest(contract_path)
        _check(bare == plain, "with no fragment the digest IS the plain blob hash of the main file")
        _check(bare == contract_union_digest(contract_path), "the digest is deterministic across calls")

        fragments = root / "plugins/seed_factory_plugin/knowledge_base/shipped_smoke_contract.d"
        fragments.mkdir(parents=True, exist_ok=True)
        (fragments / "candidate-l.yaml").write_text(row, encoding="utf-8")
        with_one = contract_union_digest(contract_path)
        _check(with_one != bare, "adding a fragment changes the digest")
        (fragments / "candidate-l.yaml").write_text(row + "\n", encoding="utf-8")
        _check(contract_union_digest(contract_path) != with_one, "changing a fragment's bytes changes the digest")
        (fragments / "candidate-l.yaml").write_text(row, encoding="utf-8")
        _check(contract_union_digest(contract_path) == with_one, "restoring the bytes restores the digest")
        (fragments / "candidate-l.yaml").rename(fragments / "candidate-m.yaml")
        _check(contract_union_digest(contract_path) != with_one, "the same bytes under a different fragment NAME is a different digest")
        (fragments / "ignored.txt").write_text(row, encoding="utf-8")
        (fragments / "candidate-m.yaml").rename(fragments / "candidate-l.yaml")
        _check(contract_union_digest(contract_path) == with_one, "a non-yaml sibling is not part of the contract")


def case_reader_parity_with_quality_gates() -> None:
    """Case 10 — the plugin's fragment reader and ``quality_gates``' agree.

    The plugin CANNOT import ``quality_gates`` (it is a repository-root dev
    package, not on a shipped plugin's path), so it owns a reader of its own.
    That is only safe if the two agree, and this is where that is measured
    instead of asserted: same files, same order, same selection rules, and an
    advisory from both where either cannot read.
    """
    print("\nCase 10 — plugin reader == quality_gates loader, file for file")
    with TemporaryDirectory() as raw:
        root = Path(raw) / "checkout"
        register = _write(root, "quality_gates/gate_smokes.txt", "quality_gates/tests/main_fixture_smoke.py\n")
        for name, body in (
            ("b-second.txt", "quality_gates/tests/b_fixture_smoke.py\n"),
            ("a-first.txt", "quality_gates/tests/a_fixture_smoke.py\n"),
            ("ignored.md", "not a register\n"),
        ):
            _write(root, f"quality_gates/gate_smokes.d/{name}", body)
        _write(root, "quality_gates/gate_smokes.d/nested/deep.txt", "quality_gates/tests/nested_fixture_smoke.py\n")

        mine, my_advisories = knowledge_fragments.fragment_files(register, knowledge_fragments.REGISTER_FRAGMENT_DIRNAME, knowledge_fragments.REGISTER_FRAGMENT_SUFFIX)
        theirs, their_advisories = gate_smoke_register.fragment_files(register)
        _check(mine == theirs, f"the same fragment files in the same order (mine={mine!r} theirs={theirs!r})")
        _check([path.name for path in mine] == ["a-first.txt", "b-second.txt"], f"ascending filename order, non-.txt and nested excluded (got {[p.name for p in mine]!r})")
        _check(my_advisories == their_advisories == (), "neither reader raises an advisory on a readable directory")
        _check(
            knowledge_fragments.fragment_directory(register, knowledge_fragments.REGISTER_FRAGMENT_DIRNAME) == gate_smoke_register.fragment_directory(register),
            "both resolve the same fragment directory beside the register",
        )

        unreadable = root / "quality_gates/gate_smokes.d/a-first.txt"
        if _unreadable(unreadable):
            my_bodies = knowledge_fragments.read_fragments(register, knowledge_fragments.REGISTER_FRAGMENT_DIRNAME, knowledge_fragments.REGISTER_FRAGMENT_SUFFIX)
            their_register = gate_smoke_register.load(register)
            _check(
                len(my_bodies.advisories) == len(their_advisories) + 1 and len(their_register.advisories) == 1,
                f"both report exactly one advisory for the unreadable fragment (mine={my_bodies.advisories!r} theirs={their_register.advisories!r})",
            )
            _check(
                all("a-first.txt" in advisory for advisory in my_bodies.advisories + their_register.advisories),
                "both advisories name the unreadable file",
            )
            _check(
                [path.name for path, _ in my_bodies.bodies] == ["b-second.txt"] and "quality_gates/tests/b_fixture_smoke.py" in their_register.entries,
                "both still deliver the readable sibling's content",
            )
            unreadable.chmod(stat.S_IRUSR | stat.S_IWUSR)
        else:
            print("    NOTE: this user can read a 0000 file; the advisory-parity half is not measurable here")


def case_real_repo_surfaces_agree() -> None:
    """Case 11 — this checkout's own three effective surfaces are self-consistent.

    The repository carries real fragments of all three kinds after this
    candidate, so this is the outcome measured on the tree that ships, not only
    on fixtures.
    """
    print("\nCase 11 — the real repo's effective surfaces agree")
    contract_path = _REPO_ROOT / "plugins/seed_factory_plugin/knowledge_base/shipped_smoke_contract.yaml"
    contract = load_contract(contract_path)
    violations = checkout_only_consistency_violations(contract_path)
    _check(violations == [], f"the real repo's three unions agree (got {violations!r})")
    _check(contract.advisories == (), f"no surface fragment is unreadable in this checkout (got {contract.advisories!r})")
    own = "quality_gates/tests/shipped_contract_fragment_union_smoke.py"
    _check(own in contract.checkout_only_paths(), "this smoke's own checkout-only row is in the effective contract")
    _check(own in contract.checkout_only_markers, "this smoke's own marker is in the effective marker surface")
    _check(own in contract.manifest_exclude_paths, "this smoke's own exclusion is in the effective exclusion set")
    fragment_dir = _REPO_ROOT / "plugins/seed_factory_plugin/knowledge_base/shipped_smoke_contract.d"
    _check(fragment_dir.is_dir() and any(fragment_dir.glob("*.yaml")), "the declaration lives in fragment files, which is the outcome demonstrating itself")


def _source_citation_result(root: Path) -> tuple[list[str], str]:
    results = code_quality_check._CheckResults()
    results.plan = {name: ClassPlan(name, "full" if name == "shipped_doc" else "inherited", None, "source-citation integration fixture") for name in results.plan}
    output = io.StringIO()
    with patch.dict(os.environ, {"SOLET_NAME": "fixture", "PYTHONPATH": str(_REPO_ROOT / "plugins/seed_factory_plugin/src")}), contextlib.redirect_stdout(output):
        code_quality_check._run_blocking_gates(root, Path(sys.executable), results)
    _check(not results.crashed_gates, f"source-citation gate produced a verdict: {results.crashed_gates!r}")
    return results.failed_blocking_gates, output.getvalue()


def case_local_aggregate_checks_source_citations() -> None:
    """The landing aggregate reaches the real per-profile source scanner.

    Pass condition: every shipped source has no untolerated never-shipped path
    citation. The native landing battery invokes this aggregate on each candidate.
    This fixture proves that schedule's local wiring, both clean and blocking.
    """
    print("\nCase 12 — real source-citation gate through the landing aggregate")
    with TemporaryDirectory() as raw:
        root = Path(raw)
        subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
        for relpath, content in {
            "plugins/seed_factory_plugin/knowledge_base/seed_manifest.yaml": "copy:\n  - quality_gates/\nexclude: []\nexclude_paths:\n  - quality_gates/shipped_doc_gate.py\n",
            "plugins/seed_factory_plugin/knowledge_base/capability_bundles.yaml": "bundles:\n  small:\n    profile_template: macos-free-solet\n    plugins: [plugin_alpha]\n  large:\n    profile_template: macos-free-solet\n    plugins: [plugin_alpha, plugin_beta]\n",
            "quality_gates/cited_path_gate_allowlist.txt": "# no tolerated debt\n",
            "quality_gates/shipped_doc_baseline.txt": "# no tolerated debt\n",
            "AGENTS.md": "Fixture instructions.\n",
            "CLAUDE.md": "Fixture instructions.\n",
            "plugins/plugin_alpha/src/clean.py": '"""A self-contained fixture."""\n',
        }.items():
            _write(root, relpath, content)
        shutil.copyfile(_REPO_ROOT / "quality_gates/shipped_doc_gate.py", root / "quality_gates/shipped_doc_gate.py")
        failed, output = _source_citation_result(root)
        _check(failed == [], f"clean source passes the aggregate ({failed!r}: {output})")
        citation = "work" + "bench/manifest.yaml"
        _write(root, "plugins/plugin_alpha/src/clean.py", f'"""See `{citation}`."""\n')
        failed, output = _source_citation_result(root)
        _check(failed == ["shipped_doc"], f"dead source citation blocks the landing aggregate ({failed!r}: {output})")
        _check("small" in output and "large" in output and "sources=   1" in output, "every profile reports the shipped source scan")
        for profile in ("small", "large"):
            _check(f"[{profile}] DEAD plugins/plugin_alpha/src/clean.py::{citation}" in output, f"source citation is attributed to profile {profile}")


def main() -> int:
    print(f"shipped_contract_fragment_union_smoke (pid {os.getpid()})")
    case_fragment_only_contract_row()
    case_fragment_only_marker()
    case_fragment_only_exclusion_prunes()
    case_duplicate_exclusion_is_a_union_no_op()
    case_manifest_fragment_extra_key_is_advisory()
    case_missing_fragment_directory_is_silent()
    case_duplicate_contract_entry_across_fragments()
    case_unreadable_contract_fragment_is_advisory()
    case_union_digest_definition()
    case_reader_parity_with_quality_gates()
    case_real_repo_surfaces_agree()
    case_local_aggregate_checks_source_citations()
    if _FAILURES:
        print(f"\nFAIL: {len(_FAILURES)} check(s) failed:")
        for label in _FAILURES:
            print(f"  - {label}")
        return 1
    print("\nshipped_contract_fragment_union_smoke: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
