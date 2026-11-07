#!/usr/bin/env python3
"""The effective gate-smoke register: ``gate_smokes.txt`` plus per-candidate fragments.

WHY THIS MODULE EXISTS (prb_ae6d81ea, iss_6ec80ecb). ``quality_gates/gate_smokes.txt``
is one shared append-only register, and about one commit in eight touches it.
Landing readiness compares a candidate's frozen base entries with master path by
path, so any landing that touches that path invalidates every other queued
candidate that touches it -- a replay plus a delta review plus a landing battery
each. Six queued candidates measured three to four hours of strictly serial
landings for that reason alone.

WHAT CHANGES. A candidate may now register a smoke by adding a file of its own
under the fragment directory (``gate_smokes.d/`` beside the register) that no
other candidate writes to. The effective register is the UNION of the existing
file and every fragment, read through this one loader that every reader calls.

WHAT DOES NOT CHANGE. This module changes where entries may live, not what the
register means. The line grammar is the existing one, the register is still an
allowlist, the existing ``gate_smokes.txt`` keeps working unchanged and is not
migrated, and a tree with no fragment directory parses to exactly the entries it
parses to today. ``ORDERING`` and ``DE-DUPLICATION`` below are the only added
semantics, and both are no-ops for a tree without fragments.

SCOPE NOTE. The ``# not shipped:`` checkout-only marker surface is NOT read
through this module. That surface belongs to
``seed_factory_plugin.shipped_smoke_contract``, and a shipped plugin cannot
import this repository-root dev package. It reads the same union through its own
``seed_factory_plugin.knowledge_fragments``, which follows the convention below
file for file -- ``quality_gates/tests/shipped_contract_fragment_union_smoke.py``
proves the two readers agree rather than leaving the equality asserted here --
so a ``# not shipped:`` marker may live in a fragment and the three-surface
agreement compares the unions (prb_ae6d81ea, chg_46d4755e).

GRAMMAR, the single copy. Text from the first ``#`` onwards is a comment, the
remainder is stripped, and a blank result is skipped. That is exactly
``run_smokes._read_register``'s historical grammar;
``focused_smoke_selector.parse_register_text`` re-exports
:func:`parse_register_text` from here so there is one function and one grammar
rather than the three hand-written copies that preceded this module.

ORDERING, deterministic. The main register's rows in file order, then each
fragment in ascending filename order, each fragment's rows in file order. Two
candidates therefore never reorder each other's entries.

DE-DUPLICATION. First occurrence wins, over the whole ordered sequence. Callers
that treat a repeat as an error (``run_smokes``) still see every row with its own
provenance through :attr:`EffectiveRegister.rows` and can report the exact file
and line of both the first and the repeat.

PASS CONDITION (rul_becb4bed). This module ADDS NO REFUSAL. A missing fragment
directory is normal and silent. An unreadable or malformed fragment is an
``ADVISORY`` line naming the file and the problem: the entries that could be read
are used and the battery proceeds. Only the main register stays required, exactly
as it is today, and this module leaves that ``OSError`` to its caller so each
caller keeps its current behaviour -- ``run_smokes`` and
``gate_smoke_registration_gate`` fail loud on a missing main register,
``focused_smoke_selector`` turns it into an advisory.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

#: The canonical register's repo-relative path. This module owns the constant;
#: ``focused_smoke_selector.REGISTER_RELPATH`` re-exports it under the name its
#: importers already use.
REGISTER_RELPATH: Final[str] = "quality_gates/gate_smokes.txt"

#: Fragment directory name, resolved beside whichever register file is in use so
#: that ``RUN_SMOKES_DEFAULT_REGISTER_PATH`` and ``--register`` still get a
#: coherent (register, fragments) pair instead of the real fragments bleeding
#: into a disposable fixture register.
FRAGMENT_DIRNAME: Final[str] = "gate_smokes.d"

#: Only ``*.txt`` fragments are read, so a README or an editor backup beside them
#: is not mistaken for a register.
FRAGMENT_SUFFIX: Final[str] = ".txt"

ADVISORY_PREFIX: Final[str] = "ADVISORY gate_smoke_register:"


@dataclass(frozen=True, slots=True)
class RegisterRow:
    """One parsed entry with the provenance a caller needs for an error message."""

    path: str
    """The repo-relative smoke path the row names."""

    source: str
    """The register or fragment the row came from, as a caller-displayable string."""

    line: int
    """1-based line number within :attr:`source`."""


@dataclass(frozen=True, slots=True)
class EffectiveRegister:
    """The union, its row provenance, and any advisories raised while reading it."""

    rows: tuple[RegisterRow, ...]
    """Every parsed row in canonical order, repeats included, for provenance."""

    advisories: tuple[str, ...]
    """``ADVISORY`` lines for fragments that could not be read or parsed."""

    @property
    def entries(self) -> tuple[str, ...]:
        """The de-duplicated union in canonical order; first occurrence wins."""
        seen: dict[str, None] = {}
        for row in self.rows:
            seen.setdefault(row.path, None)
        return tuple(seen)


def parse_register_text(text: str) -> tuple[str, ...]:
    """Rows in register grammar: text after ``#`` and blank lines ignored, first occurrence kept."""
    rows: dict[str, None] = {}
    for raw in text.splitlines():
        row = raw.split("#", 1)[0].strip()
        if row:
            rows.setdefault(row, None)
    return tuple(rows)


def _parse_rows(text: str, source: str) -> list[RegisterRow]:
    """Parse one register body into rows, keeping each row's own line number."""
    rows: list[RegisterRow] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if line:
            rows.append(RegisterRow(path=line, source=source, line=number))
    return rows


def fragment_directory(register: Path) -> Path:
    """The fragment directory for ``register``, resolved beside it."""
    return register.parent / FRAGMENT_DIRNAME


def fragment_files(register: Path) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    """Fragments beside ``register`` in filename order, plus any advisories.

    A missing directory is normal and yields ``((), ())``. A directory that
    cannot be listed is an advisory, never a refusal.
    """
    directory = fragment_directory(register)
    try:
        if not directory.is_dir():
            return (), ()
        names = sorted(entry.name for entry in directory.iterdir() if entry.name.endswith(FRAGMENT_SUFFIX) and entry.is_file())
    except OSError as error:
        return (), (f"{ADVISORY_PREFIX} fragment directory {directory} cannot be listed ({error}); only {register} was read",)
    return tuple(directory / name for name in names), ()


def load(register: Path) -> EffectiveRegister:
    """Load the effective register for ``register`` from the filesystem.

    The main register is required and its ``OSError`` propagates, which is what
    every current caller already does with it. Each fragment is advisory.
    """
    rows = _parse_rows(register.read_text(encoding="utf-8"), str(register))
    advisories: list[str] = []
    fragments, listing_advisories = fragment_files(register)
    advisories.extend(listing_advisories)
    for fragment in fragments:
        try:
            body = fragment.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            advisories.append(f"{ADVISORY_PREFIX} fragment {fragment} is unreadable ({error}); its entries are not registered and the rest of the register is used")
            continue
        rows.extend(_parse_rows(body, str(fragment)))
    return EffectiveRegister(rows=tuple(rows), advisories=tuple(advisories))


def is_fragment_relpath(register_relpath: str, path: str) -> bool:
    """Whether ``path`` is a fragment of the register at ``register_relpath``.

    Only a direct child of the fragment directory with the fragment suffix
    counts, so a nested path cannot smuggle in an entry.
    """
    prefix = f"{PurePosixPath(register_relpath).parent / FRAGMENT_DIRNAME}/"
    if not path.startswith(prefix) or not path.endswith(FRAGMENT_SUFFIX):
        return False
    return "/" not in path[len(prefix) :]


def fragment_relpaths(register_relpath: str, available: Iterable[str]) -> tuple[str, ...]:
    """Fragment repo-relative paths among ``available``, in filename order.

    The source-map counterpart of :func:`fragment_files`, for callers that read a
    frozen git tree rather than the working tree.
    """
    return tuple(sorted(path for path in available if is_fragment_relpath(register_relpath, path)))


def load_from_sources(sources: Mapping[str, bytes], register_relpath: str) -> EffectiveRegister:
    """Load the effective register out of a repo-relative path to bytes mapping.

    ``KeyError`` for the main register propagates, matching the filesystem
    loader's treatment of a missing main register.
    """
    rows = _parse_rows(sources[register_relpath].decode("utf-8"), register_relpath)
    advisories: list[str] = []
    for relpath in fragment_relpaths(register_relpath, sources):
        try:
            body = sources[relpath].decode("utf-8")
        except UnicodeError as error:
            advisories.append(f"{ADVISORY_PREFIX} fragment {relpath} is undecodable ({error}); its entries are not registered and the rest of the register is used")
            continue
        rows.extend(_parse_rows(body, relpath))
    return EffectiveRegister(rows=tuple(rows), advisories=tuple(advisories))
