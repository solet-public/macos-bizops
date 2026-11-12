"""Consistency matrix for the file-location guard's metadata reads.

containment_smoke exposes the test classes defined here. Each matrix cell is
one guard case (pending, existing file or existing directory target), one
change (replace the element with a link into the protected repo, relocate it
into the repo leaving a link at the old path, remove it from the path, or put a
hard link to the protected file at a file element's name) and one fixture element from the allowed root down to the leaf. A cell's test runs
the guard once per metadata call index k of the unchanged case, each time on a
fresh tree, making that change immediately before call k.

Every os metadata function the guard reaches is wrapped (WRAPPED; os.stat with
dir_fd is fstatat), and so is no_link_stat.stat_without_links, the one-call
macOS fstatat with AT_SYMLINK_NOFOLLOW_ANY. A profiler census records any other
os function the guard calls, and any call into no_link_stat that bypasses the
wrapper; outside NOT_METADATA that fails the run, so a new kind of metadata
read cannot escape the sweep. The oracle never consults the guard: a refusal
passes, and a return passes only when, on the tree as it stands at return, no
component of the returned path is a link, every existing component is the
object the fixture created at that name, and none is the protected repo, app
home or protected file. A change after the final call is after return: the
follow-up fd-bound handle.

FallbackLeafMatrixSmoke repeats every cell that changes an existing target
itself under each fallback condition, where the final check reads components
one at a time by path. An ancestor relocated between its own read and the
final read there is the guard's documented fallback residual.
"""

from __future__ import annotations

import ast
import errno
import os
import stat
import sys
import tempfile
import unittest
from collections.abc import Callable, Generator, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta/src"))

from ananta.file_locations import containment, no_link_stat  # noqa: E402
from ananta.file_locations.resolution import SoletTree  # noqa: E402

type Read = tuple[str, str]
type Identity = tuple[int, int]

CHANGES = ("link", "relocate", "remove", "hardlink")
EXISTING = frozenset({"Project", "Project/current", "Project/current/existing.tsv"})
PROTECTED = ("Repo", "Repo/profile", "Repo/protected.tsv")
CREATED = (*sorted(EXISTING), *PROTECTED)
WRAPPED = ("stat", "lstat", "fstat", "open", "listdir", "scandir", "readlink", "access", "stat_without_links")
# Wrapped entries that are not os functions, by the module the guard calls them through.
_OWNERS: dict[str, object] = {"stat_without_links": no_link_stat}
# os functions that read no filesystem metadata: closing a descriptor and path-string parsing.
NOT_METADATA = frozenset({"close", "fspath", "_path_splitroot_ex"})
_OS_MODULE = os.stat.__module__


@dataclass(frozen=True, slots=True)
class SweepCase:
    """One guard entry on one fixture target; a root of None selects the location entry."""

    name: str
    kind: str
    target: str
    root: str | None


SWEEP_CASES = (
    SweepCase("existing file", "existing file", "Project/current/existing.tsv", "Project"),
    SweepCase("existing file in root", "existing file", "Project/current/existing.tsv", "Project/current"),
    SweepCase("existing location", "existing directory", "Project/current", None),
    SweepCase("existing directory path", "existing directory", "Project/current", "Project"),
    SweepCase("new file", "pending", "Project/current/out.tsv", "Project/current"),
    SweepCase("new folder", "pending", "Project/current/pending/out.tsv", "Project"),
    SweepCase("nested new folders", "pending", "Project/current/pending/nested/out.tsv", "Project"),
    SweepCase("uncreated allowed root", "pending", "Project/current/pending/nested/out.tsv", "Project/current/pending"),
    SweepCase("new location", "pending", "Project/current/pending", None),
)


@dataclass(frozen=True, slots=True)
class Cell:
    """One (case, change, element) cell of the matrix."""

    case: SweepCase
    change: str
    element: str

    @property
    def name(self) -> str:
        parts = (self.case.name, self.change, self.element)
        return "__".join(part.replace(" ", "_").replace("/", "_").replace(".", "_") for part in parts)


def _skip_reason(element: str, change: str, first_pending: bool) -> str | None:
    """Why (change, element) is not a cell, or None when it is one."""
    if change == "hardlink" and not element.endswith(".tsv"):
        return "a directory cannot be hard-linked"
    if element in EXISTING or (first_pending and change in ("link", "hardlink")):
        return None
    if first_pending:
        return "a pending component cannot be relocated or removed; its link cell covers its appearance"
    return "a component below a pending one cannot appear before its pending parent does"


def matrix() -> tuple[tuple[Cell, ...], tuple[tuple[str, str], ...]]:
    """Every applicable cell, and every other (case, change, element) with its reason."""
    cells: list[Cell] = []
    skipped: list[tuple[str, str]] = []
    for case in SWEEP_CASES:
        parts = Path(case.target).parts
        first_pending = True
        for depth in range(1, len(parts) + 1):
            element = "/".join(parts[:depth])
            for change in CHANGES:
                reason = _skip_reason(element, change, first_pending)
                if reason is None:
                    cells.append(Cell(case, change, element))
                else:
                    skipped.append((f"{case.name} / {change} / {element}", reason))
            first_pending = first_pending and element in EXISTING
    return tuple(cells), tuple(skipped)


@dataclass(slots=True)
class MetadataTrace:
    """Each wrapped call in order, named by absolute path, and every unwrapped os call."""

    reads: list[Read] = field(default_factory=list)
    unwrapped: list[str] = field(default_factory=list)


def _named(opened: dict[int, str], args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """The absolute path a call reads, resolving descriptors to the path they were opened for."""
    target: Any = args[0] if args else kwargs.get("path", kwargs.get("fd"))
    if isinstance(target, int):
        return opened.get(target, f"<fd {target}>")
    parent = kwargs.get("dir_fd")
    name = os.fspath(target)
    return name if parent is None else os.path.join(opened.get(parent, f"<fd {parent}>"), name)


@contextmanager
def counted_metadata_reads(before_read: Callable[[int, Read], None], wrapped: Sequence[str] = WRAPPED) -> Generator[MetadataTrace]:
    """Wrap each named os function, calling before_read(k, read) before call k runs."""
    trace = MetadataTrace()
    real = {name: getattr(_OWNERS.get(name, os), name) for name in wrapped}
    opened: dict[int, str] = {}
    quiet = [False]

    def counted(kind: str) -> Callable[..., Any]:
        def measured(*args: Any, **kwargs: Any) -> Any:
            read = (kind, _named(opened, args, kwargs))
            trace.reads.append(read)
            quiet[0] = True
            try:
                before_read(len(trace.reads), read)
                result = real[kind](*args, **kwargs)
            finally:
                quiet[0] = False
            if kind == "open":
                opened[result] = read[1]
            return result

        return measured

    def census(frame: FrameType, event: str, arg: Any) -> None:
        if quiet[0]:
            return
        if event == "c_call" and getattr(arg, "__module__", None) == _OS_MODULE:
            name = getattr(arg, "__name__", "")
            if name not in real and name not in NOT_METADATA:
                trace.unwrapped.append(name)
        elif event == "call" and frame.f_code.co_filename == no_link_stat.__file__:
            trace.unwrapped.append(f"no_link_stat.{frame.f_code.co_name}")

    with ExitStack() as stack:
        for name in wrapped:
            stack.enter_context(patch.object(_OWNERS.get(name, os), name, counted(name)))
        previous = sys.getprofile()
        sys.setprofile(census)
        try:
            yield trace
        finally:
            sys.setprofile(previous)


def _identity(information: os.stat_result) -> Identity:
    return information.st_dev, information.st_ino


def _fixture(base: Path) -> SoletTree:
    (base / "Repo/profile").mkdir(parents=True)
    (base / "Repo/protected.tsv").write_text("protected fixture")
    (base / "Project/current").mkdir(parents=True)
    (base / "Project/current/existing.tsv").write_text("ordinary fixture")
    return SoletTree(base / "Repo", base / "Repo/profile")


def _apply(cell: Cell, base: Path, repo: Path) -> None:
    """Make the cell's change in one step; only os.rename, os.symlink and os.unlink run."""
    element = base / cell.element
    is_file = cell.element.endswith(".tsv")
    if cell.change == "remove":
        if is_file:
            os.unlink(element)
        else:
            os.rename(element, element.with_name(element.name + "-removed"))
        return
    if cell.change == "hardlink":
        replacement = element.with_name(element.name + "-hardlink")
        os.link(repo / "protected.tsv", replacement)
        os.replace(replacement, element)
        return
    if cell.change == "relocate":
        moved = repo / (element.name + "-relocated")
        os.rename(element, moved)
        os.symlink(moved, element, target_is_directory=not is_file)
        return
    if cell.element in EXISTING:
        os.rename(element, element.with_name(element.name + "-prior"))
    os.symlink(repo / "protected.tsv" if is_file else repo, element, target_is_directory=not is_file)


def _admit(case: SweepCase, base: Path, tree: SoletTree) -> str | None:
    """Run the guard entry; return the admitted path, or None for the location entry."""
    target = base / case.target
    if case.root is None:
        containment.assert_location_admissible(target, tree)
        return None
    return containment.assert_path_allowed(str(target), containment.Located((base / case.root,), ()), tree, suffix=None, param_name="path", plugin_name="sweep").value


def _refusal(case: SweepCase) -> type[Exception]:
    return ValueError if case.root is None else containment.PathRefusedError


def _outcome(case: SweepCase, base: Path, tree: SoletTree) -> tuple[bool, str | None]:
    """(refused, returned path) for one guard run."""
    try:
        return False, _admit(case, base, tree)
    except _refusal(case):
        return True, None


def _inconsistency(case: SweepCase, base: Path, created: dict[str, Identity], returned: str | None) -> str | None:
    """Why a return disagrees with the tree as it stands now, or None when it agrees; never runs the guard."""
    target = base / case.target
    if returned is not None and returned != str(target):
        return f"returned {returned} for a request of {target}"
    current = Path(target.anchor)
    for name in target.parts[1:]:
        current = current / name
        try:
            information = os.lstat(current)
        except FileNotFoundError:
            return None
        problem = _component_problem(current, information, base, created)
        if problem:
            return problem
    return None


def _component_problem(current: Path, information: os.stat_result, base: Path, created: dict[str, Identity]) -> str | None:
    if stat.S_ISLNK(information.st_mode):
        return f"returned while {current} is a link"
    if _identity(information) in {created[name] for name in PROTECTED}:
        return f"returned while {current} is the solet's own tree"
    relative = current.relative_to(base).as_posix() if current.is_relative_to(base) else None
    if relative in created and created[relative] != _identity(information):
        return f"returned while {current} is not the object the fixture created there"
    return None


def _modules_importing(package: Path, module: str) -> set[str]:
    """Names of the package's Python files that import the given module."""
    found: set[str] = set()
    for path in package.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = [node.module or ""] if isinstance(node, ast.ImportFrom) else [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
            if module in names:
                found.add(path.name)
    return found


class MetadataSweep:
    """Run one cell at every call index, each run on its own fresh fixture tree."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.trees = 0

    def fixture(self) -> tuple[Path, SoletTree, dict[str, Identity]]:
        self.trees += 1
        base = self.base / f"run{self.trees:05d}"
        tree = _fixture(base)
        return base, tree, {name: _identity(os.lstat(base / name)) for name in CREATED}

    def reference(self, case: SweepCase) -> list[Read]:
        """Measure the unchanged pass path; its call count is the cell's K."""
        base, tree, created = self.fixture()
        with counted_metadata_reads(lambda _index, _read: None) as trace:
            refused, returned = _outcome(case, base, tree)
        if refused or trace.unwrapped or not trace.reads or _inconsistency(case, base, created, returned):
            raise AssertionError(f"{case.name}: unchanged tree did not pass (refused={refused}, unwrapped={trace.unwrapped})")
        return [(kind, path.removeprefix(f"{base}/")) for kind, path in trace.reads]

    def changed_run(self, cell: Cell, k: int) -> tuple[bool, str | None]:
        """Return (refused, failure) for one change made immediately before call k."""
        base, tree, created = self.fixture()
        applied: list[int] = []

        def change(index: int, _read: Read) -> None:
            if index == k:
                _apply(cell, base, tree.repo_root)
                applied.append(index)

        with counted_metadata_reads(change) as trace:
            refused, returned = _outcome(cell.case, base, tree)
        if not applied:
            return refused, "change was not reached"
        if trace.unwrapped:
            return refused, f"guard called unwrapped os functions {sorted(set(trace.unwrapped))}"
        return refused, None if refused else _inconsistency(cell.case, base, created, returned)

    def sweep_cell(self, cell: Cell) -> list[str]:
        reads = self.reference(cell.case)
        failures: list[str] = []
        for k in range(1, len(reads) + 1):
            _refused, failure = self.changed_run(cell, k)
            if failure:
                failures.append(f"k={k} of {len(reads)} before {reads[k - 1]}: {failure}")
        return failures


class NoLinkStatSmoke(unittest.TestCase):
    """The one-call no-link read agrees with os.stat, refuses a path through a link, and falls back with one advisory."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_unavailable_read_logs_one_advisory_and_keeps_the_open_path(self) -> None:
        with patch.object(no_link_stat, "_load", side_effect=OSError("fixture: symbol absent")), self.assertLogs(no_link_stat.logger, "WARNING") as logged:
            self.assertIsNone(no_link_stat._start())  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(len(logged.output), 1)
        self.assertIn("ADVISORY", logged.output[0])
        (self.root / "Repo/profile").mkdir(parents=True)
        (self.root / "Project").mkdir()
        target = self.root / "Project/existing.tsv"
        target.write_text("ordinary fixture")
        tree = SoletTree(self.root / "Repo", self.root / "Repo/profile")
        with patch.object(no_link_stat, "AVAILABLE", False):
            result = containment.assert_path_allowed(str(target), containment.Located((self.root / "Project",), ()), tree, suffix=None, param_name="path", plugin_name="fallback")
        self.assertEqual(result.value, str(target))

    def test_matches_os_stat_and_refuses_links(self) -> None:
        if not no_link_stat.AVAILABLE:
            advisory = f"ADVISORY: one-call no-link stat is unavailable on {sys.platform}; its comparison with os.stat is skipped."
            print(advisory)
            self.skipTest(advisory)
        regular, linked, directory = self.root / "regular.tsv", self.root / "linked.tsv", self.root / "directory"
        regular.write_text("fixture")
        linked.write_text("fixture")
        os.link(linked, self.root / "second-name.tsv")
        directory.mkdir()
        target = directory / "target.tsv"
        target.write_text("fixture")
        (self.root / "link").symlink_to(directory, target_is_directory=True)
        for path in (regular, linked, directory, target):
            with self.subTest(path=path.name):
                expected, observed = os.stat(path), no_link_stat.stat_without_links(path)
                self.assertEqual((observed.st_dev, observed.st_ino, observed.st_mode, observed.st_nlink), (expected.st_dev, expected.st_ino, expected.st_mode, expected.st_nlink))
        self.assertEqual(no_link_stat.stat_without_links(linked).st_nlink, 2)
        link, described = os.lstat(self.root / "link"), no_link_stat.stat_without_links(self.root / "link")
        self.assertEqual((described.st_dev, described.st_ino, described.st_mode), (link.st_dev, link.st_ino, link.st_mode), "a final link is described, never followed")
        with self.assertRaises(OSError) as caught:
            no_link_stat.stat_without_links(self.root / "link/target.tsv")
        self.assertEqual(caught.exception.errno, errno.ELOOP)
        with self.assertRaises(FileNotFoundError):
            no_link_stat.stat_without_links(self.root / "missing")


class MetadataChangeMatrixSmoke(unittest.TestCase):
    """One test per matrix cell; each changes its element before every metadata call."""

    def test_matrix_is_the_full_product(self) -> None:
        cells, skipped = matrix()
        product = sum(len(Path(case.target).parts) * len(CHANGES) for case in SWEEP_CASES)
        self.assertEqual(len(cells) + len(skipped), product)
        self.assertEqual({case.kind for case in SWEEP_CASES}, {"pending", "existing file", "existing directory"})
        self.assertEqual(len({cell.name for cell in cells}), len(cells))

    def test_guard_calls_only_wrapped_metadata_functions(self) -> None:
        self.assertEqual(WRAPPED, ("stat", "lstat", "fstat", "open", "listdir", "scandir", "readlink", "access", "stat_without_links"))
        foreign = _modules_importing(ROOT / "ananta/src/ananta/file_locations", "ctypes")
        self.assertEqual(foreign, {"no_link_stat.py", "documents.py"}, "metadata uses no_link_stat; documents uses only the Windows known-folder API")
        documents = ast.parse((ROOT / "ananta/src/ananta/file_locations/documents.py").read_text())
        windows_apis = {node.func.attr for node in ast.walk(documents) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id in {"shell", "ole"}}
        self.assertEqual(windows_apis, {"SHGetKnownFolderPath", "CoTaskMemFree"}, "known-folder lookup and its allocation release are the only allowed foreign calls in documents")
        with tempfile.TemporaryDirectory() as temporary:
            sweep = MetadataSweep(Path(temporary).resolve())
            for case in SWEEP_CASES:
                with self.subTest(case=case.name):
                    self.assertLessEqual({kind for kind, _path in sweep.reference(case)}, set(WRAPPED))
            base, tree, _created = sweep.fixture()
            with counted_metadata_reads(lambda _index, _read: None, wrapped=("stat", "lstat", "open")) as trace:
                _admit(SWEEP_CASES[0], base, tree)
        self.assertIn("fstat", trace.unwrapped, "the census must name a metadata function left out of the wrapped set")
        if no_link_stat.AVAILABLE:
            self.assertIn("no_link_stat.stat_without_links", trace.unwrapped, "the census must name an unwrapped call into no_link_stat")


def _cell_test(cell: Cell) -> Callable[[MetadataChangeMatrixSmoke], None]:
    def test(self: MetadataChangeMatrixSmoke) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            failures = MetadataSweep(Path(temporary).resolve()).sweep_cell(cell)
        self.assertEqual(failures, [], f"cell {cell.name}: {len(failures)} inconsistent outcomes; first: {failures[:3]}")

    return test


for _cell in matrix()[0]:
    setattr(MetadataChangeMatrixSmoke, f"test_{_cell.name}", _cell_test(_cell))


# Platform conditions under which the guard's final check reads components one at a time by path.
FALLBACKS: dict[str, Callable[[], AbstractContextManager[Any]]] = {
    "unreadable_single_read": lambda: patch.object(no_link_stat, "stat_without_links", side_effect=PermissionError(errno.EACCES, "fixture permission unavailable")),
    "no_single_resolution": lambda: patch.object(containment, "_NO_FOLLOW_ANY", None),
    "no_descriptor_walk": lambda: patch.object(containment, "_DESCRIPTOR_WALK", False),
}


def leaf_cells() -> tuple[Cell, ...]:
    """Matrix cells that change an existing target itself: the file, or the directory a directory case names."""
    return tuple(cell for cell in matrix()[0] if cell.case.kind != "pending" and cell.element == cell.case.target)


class FallbackLeafMatrixSmoke(unittest.TestCase):
    """Each existing-target cell swept at every call index under each fallback condition.

    Without the one-call no-link read, the final recheck reads each component
    by path and reads the existing target last, so a change to the target
    itself before any metadata call is refused or leaves a consistent tree.
    An ancestor relocated with a link between its own read and that last read
    is the documented fallback residual and is not a cell here.
    """

    def test_leaf_cells_cover_every_existing_target_change(self) -> None:
        cells = leaf_cells()
        self.assertEqual({cell.case.kind for cell in cells}, {"existing file", "existing directory"})
        self.assertEqual(len(cells), 14, [cell.name for cell in cells])


def _fallback_cell_test(fallback: str, cell: Cell) -> Callable[[FallbackLeafMatrixSmoke], None]:
    def test(self: FallbackLeafMatrixSmoke) -> None:
        if fallback == "unreadable_single_read" and not no_link_stat.AVAILABLE:
            advisory = f"ADVISORY: one-call no-link stat is unavailable on {sys.platform}; its unreadable-read fallback cell is skipped."
            print(advisory)
            self.skipTest(advisory)
        with FALLBACKS[fallback](), tempfile.TemporaryDirectory() as temporary:
            failures = MetadataSweep(Path(temporary).resolve()).sweep_cell(cell)
        self.assertEqual(failures, [], f"{fallback} cell {cell.name}: {len(failures)} inconsistent outcomes; first: {failures[:3]}")

    return test


for _fallback in FALLBACKS:
    for _cell in leaf_cells():
        setattr(FallbackLeafMatrixSmoke, f"test_{_fallback}__{_cell.name}", _fallback_cell_test(_fallback, _cell))


class FinalReadFixtureSmoke(unittest.TestCase):
    """Named review fixtures, and platforms without one no-follow resolution or descriptor opens."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.sweep = MetadataSweep(self.base / "sweep")

    def refusal_code(self, cell: Cell, at: Callable[[int, Read], bool]) -> tuple[str | None, list[int]]:
        """Apply the cell's change before the first read that `at` selects; the refusal code and where it ran."""
        base, tree, _created = self.sweep.fixture()
        applied: list[int] = []

        def change(index: int, read: Read) -> None:
            if not applied and at(index, (read[0], read[1].removeprefix(f"{base}/"))):
                _apply(cell, base, tree.repo_root)
                applied.append(index)

        with counted_metadata_reads(change):
            try:
                _admit(cell.case, base, tree)
            except containment.PathRefusedError as error:
                return error.code, applied
        return None, applied

    def test_existing_file_parent_relocated_during_final_read_is_refused(self) -> None:
        cell = Cell(SWEEP_CASES[0], "relocate", "Project/current")
        final = len(self.sweep.reference(cell.case))
        code, applied = self.refusal_code(cell, lambda index, _read: index == final)
        self.assertEqual(applied, [final], "fixture must relocate the parent before the guard's last metadata read")
        self.assertEqual(code, "file_locations.identity_changed")

    def test_existing_file_ancestor_change_during_final_fstat_is_refused(self) -> None:
        reads = self.sweep.reference(SWEEP_CASES[0])
        fstats = [index for index, (kind, _path) in enumerate(reads, start=1) if kind == "fstat"]
        for element in ("Project/current", "Project"):
            for label, index in (("first fstat", fstats[0]), ("final fstat", fstats[-1])):
                with self.subTest(element=element, at=label):
                    code, applied = self.refusal_code(Cell(SWEEP_CASES[0], "relocate", element), lambda k, _read, index=index: k == index)
                    self.assertEqual(applied, [index], "the fstat must be reached")
                    self.assertEqual(code, "file_locations.identity_changed")

    def test_pending_segment_link_after_root_recapture_is_refused(self) -> None:
        cell = Cell(SWEEP_CASES[5], "link", "Project/current/pending")
        reads = self.sweep.reference(cell.case)
        absent = reads.index(("open", "Project/current/pending")) + 1
        for k in range(1, len(reads) + 1):
            code, applied = self.refusal_code(cell, lambda index, _read, k=k: index == k)
            self.assertEqual(applied, [k], f"k={k}")
            self.assertIsNotNone(code, f"k={k}: the guard returned while the pending segment links into the repo")
            if k >= absent:
                self.assertEqual(code, "file_locations.identity_changed", f"k={k} {reads[k - 1]}")

    def test_pending_target_ancestor_link_before_final_read_is_refused(self) -> None:
        for change in ("link", "relocate"):
            cell = Cell(SWEEP_CASES[5], change, "Project/current")
            final = len(self.sweep.reference(cell.case))
            self.assertEqual(self.sweep.changed_run(cell, final), (True, None), change)
        removed = Cell(SWEEP_CASES[5], "remove", "Project/current")
        self.assertEqual(self.sweep.changed_run(removed, len(self.sweep.reference(removed.case))), (False, None))

    def single_read(self) -> None:
        if not (no_link_stat.AVAILABLE and containment._NO_FOLLOW_ANY is not None):  # pyright: ignore[reportPrivateUsage]
            advisory = f"ADVISORY: one-call no-link stat is unavailable on {sys.platform}; this final-read fixture is skipped."
            print(advisory)
            self.skipTest(advisory)

    def test_hard_link_appearance_before_final_metadata_read_is_refused(self) -> None:
        self.single_read()
        case = SWEEP_CASES[0]
        reads = self.sweep.reference(case)
        final = len(reads)
        self.assertEqual(reads[-1], ("stat_without_links", case.target))
        code, applied = self.refusal_code(Cell(case, "hardlink", case.target), lambda index, _read: index == final)
        self.assertEqual(applied, [final], "the leaf must change before the final metadata read")
        self.assertIsNotNone(code, "a leaf changed to a protected-file hard link before the final metadata read must not be admitted")

    def parent_relocated_before_its_own_final_read(self, fallback: AbstractContextManager[Any]) -> None:
        """Relocate the target's parent immediately before the fallback's final path read of that parent.

        The fallback's final recheck reads the existing target last, so this
        cell changes the parent before its own read, not before the last call:
        a parent relocated between its own read and the target read is the
        documented fallback residual (containment module docstring).
        """
        cell = Cell(SWEEP_CASES[0], "relocate", "Project/current")
        with fallback:
            reads = self.sweep.reference(cell.case)
            own = len(reads) - reads[::-1].index(("lstat", "Project/current"))
            self.assertEqual(reads[-1], ("lstat", cell.case.target), "the fallback's final read is the existing target")
            self.assertEqual(self.sweep.changed_run(cell, own), (True, None), reads[own - 1])

    def test_no_single_resolution_keeps_final_parent_change_consistent(self) -> None:
        """Without one no-follow resolution, a parent relocated before its own final read is refused."""
        self.parent_relocated_before_its_own_final_read(patch.object(containment, "_NO_FOLLOW_ANY", None))

    def test_no_descriptor_support_keeps_final_parent_change_consistent(self) -> None:
        """Without descriptor-relative opens, a parent relocated before its own final read is refused."""
        self.parent_relocated_before_its_own_final_read(patch.object(containment, "_DESCRIPTOR_WALK", False))

    def test_unreadable_single_read_refuses_leaf_hard_link_before_final_read(self) -> None:
        """Review cell B1-R5: the one-call read reports EACCES and the existing leaf becomes a hard link to the protected file before the last metadata call."""
        self.single_read()
        case = SWEEP_CASES[0]
        with patch.object(no_link_stat, "stat_without_links", side_effect=PermissionError(errno.EACCES, "fixture permission unavailable")):
            final = len(self.sweep.reference(case))
            code, applied = self.refusal_code(Cell(case, "hardlink", case.target), lambda index, _read: index == final)
        self.assertEqual(applied, [final], "the leaf must change before the final metadata read")
        self.assertEqual(code, "file_locations.identity_changed", "an existing leaf changed to a protected-file hard link before the fallback's final read must be refused")

    def test_unreadable_single_read_falls_back_with_advisory(self) -> None:
        self.single_read()
        tree = _fixture(self.base)
        target = self.base / "Project/current/existing.tsv"
        refused: list[Read] = []

        def unreadable(_index: int, read: Read) -> None:
            if read[0] == "stat_without_links":
                refused.append(read)
                raise PermissionError(errno.EACCES, "fixture permission unavailable")

        with counted_metadata_reads(unreadable), containment.location_session("permission"):
            result = containment.assert_path_allowed(str(target), containment.Located((self.base / "Project",), ()), tree, suffix=None, param_name="path", plugin_name="fixture")
            advisories = containment.location_advisories()
        self.assertTrue(refused, "fixture must refuse the one-call read")
        self.assertEqual(result.value, str(target))
        self.assertTrue(any("one at a time" in text for text in advisories), advisories)

    def test_component_reads_where_no_single_resolution_exists(self) -> None:
        tree = _fixture(self.base)
        roots = containment.Located((self.base / "Project",), ())
        with patch.object(containment, "_NO_FOLLOW_ANY", None), containment.location_session("flagless"):
            result = containment.assert_path_allowed(str(self.base / "Project/current/out.tsv"), roots, tree, suffix=None, param_name="path", plugin_name="fixture")
            advisories = containment.location_advisories()
        self.assertEqual(result.value, str(self.base / "Project/current/out.tsv"))
        self.assertTrue(any("one at a time" in text for text in advisories), advisories)

    def test_path_reads_where_descriptor_opens_are_unavailable(self) -> None:
        tree = _fixture(self.base)
        roots = containment.Located((self.base / "Project",), ())
        for target in (self.base / "Project/current/existing.tsv", self.base / "Project/current/pending/out.tsv"):
            with self.subTest(target=target), patch.object(containment, "_DESCRIPTOR_WALK", False), containment.location_session("pathwise"):
                result = containment.assert_path_allowed(str(target), roots, tree, suffix=None, param_name="path", plugin_name="fixture")
                advisories = containment.location_advisories()
            self.assertEqual(result.value, str(target))
            self.assertTrue(any("by path" in text for text in advisories), advisories)
        with patch.object(containment, "_DESCRIPTOR_WALK", False), self.assertRaises(containment.PathRefusedError):
            containment.assert_path_allowed(str(tree.repo_root / "out.tsv"), containment.Located((self.base,), ()), tree, suffix=None, param_name="path", plugin_name="fixture")
