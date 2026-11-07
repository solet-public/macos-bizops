"""Find the files a shipped payload's own code reads at installed locations.

A hand-maintained packaging list can only agree with other lists; it cannot
know what the installed code opens.  This module reads the payload's Python
sources and follows every path expression rooted at an installed-location
anchor -- ``Path(sys.prefix)``, ``Path(__file__)`` and
``importlib.resources.files`` -- to the leaf it reaches.  The staging adapter
maps each leaf to the archive member that must provide it and checks the
archive; this module only says what is read, where, and what it could not
explain.

Resolution is deliberately narrow.  It follows constant joins, module and
imported constants, local assignments, loops over constant tuples,
no-argument providers and same-module call sites: the forms the payload
actually uses.  An anchored expression it cannot finish becomes an advisory
finding, never a requirement and never coverage.  A read of a derived leaf
through a path that is not production-anchored (an explicit directory, a
development-relative path) is reported as uncovered, because it can pass in a
development checkout and still fail in the installed layout.

Requirements are immutable values.  A changed reader is a new derivation, a
removed reader retires its requirement, and restoring the reader restores
it; nothing here is persisted.  Stdlib only, so a staging script can load it
by path from the checkout it stages.
"""

from __future__ import annotations

import ast
import posixpath
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final

# Requirement coverage: open until checked, then exactly one terminal value.
COVERAGE_REQUIRED: Final[str] = "required"
COVERAGE_ARCHIVE_MEMBER: Final[str] = "archive_member"
COVERAGE_FORMULA_RENDERED: Final[str] = "formula_rendered"
COVERAGE_FORMULA_LINKED: Final[str] = "formula_linked"
COVERAGE_NOT_INSTALLED: Final[str] = "not_installed"
COVERAGE_MISSING: Final[str] = "missing"
FAILED_COVERAGE: Final[frozenset[str]] = frozenset({COVERAGE_NOT_INSTALLED, COVERAGE_MISSING})

# Findings: what an expression is when it is not a requirement.
FINDING_DIRECTORY: Final[str] = "directory_reference"
FINDING_ANCHOR_ROOT: Final[str] = "anchor_root"
FINDING_UNRESOLVED: Final[str] = "unresolved_anchor"
FINDING_UNCOVERED_READ: Final[str] = "uncovered_read"
FINDING_CALLER_ALTERNATIVE: Final[str] = "caller_alternative"
ADVISORY_FINDINGS: Final[frozenset[str]] = frozenset({FINDING_UNRESOLVED, FINDING_UNCOVERED_READ})

# Anchors: below ``sys.prefix`` (a keg), or below a module's own source file.
ANCHOR_PREFIX: Final[str] = "sys.prefix"
ANCHOR_SOURCE: Final[str] = "package_source"

_PATH_METHODS: Final[frozenset[str]] = frozenset({"joinpath", "with_name", "resolve", "absolute", "expanduser"})
_PATH_ATTRIBUTES: Final[frozenset[str]] = _PATH_METHODS | {"parent"}
_PATH_CONSTRUCTORS: Final[frozenset[str]] = frozenset({"Path", "PurePath", "PosixPath"})
_MAX_DEPTH: Final[int] = 12
_MAX_ALTERNATIVES: Final[int] = 64


@dataclass(frozen=True, slots=True)
class Requirement:
    """One leaf the installed payload needs, and the reader that needs it.

    ``reader`` is ``<path>:<line>`` of the reading expression (or of the
    installer statement); ``mapping`` names the installer rule that puts
    ``source_leaf`` at ``installed_path``.
    """

    reader: str
    installed_path: str
    source_leaf: str | None
    mapping: str
    coverage: str

    def describe(self) -> str:
        """One line naming the leaf, its source, its reader and its coverage."""
        source = self.source_leaf or "no archive source"
        return f"{self.installed_path} <- {source} read at {self.reader} via {self.mapping} [{self.coverage}]"


@dataclass(frozen=True, slots=True)
class Finding:
    """An anchored or leaf-reading expression that is not a requirement."""

    kind: str
    location: str
    expression: str
    reason: str


@dataclass(frozen=True, slots=True)
class MirrorRecord:
    """A directory whose files are all or part of a required bundle."""

    directory: str
    present: tuple[str, ...]
    missing: tuple[str, ...]
    canonical: bool


@dataclass(frozen=True, slots=True)
class AnchoredRead:
    """A maximal path expression that resolved below an installed anchor.

    ``path`` is relative to ``sys.prefix`` for :data:`ANCHOR_PREFIX` and an
    archive path for :data:`ANCHOR_SOURCE`; it is empty for a bare anchor.
    """

    location: str
    expression: str
    anchor: str
    path: str


@dataclass(frozen=True, slots=True)
class RawRead:
    """A path expression with no anchor, and the leaf names it may read."""

    location: str
    expression: str
    names: frozenset[str]


@dataclass(frozen=True, slots=True)
class ReaderCensus:
    """Everything one scan of the payload's modules found."""

    reads: tuple[AnchoredRead, ...]
    findings: tuple[Finding, ...]
    raw_reads: tuple[RawRead, ...]
    modules: tuple[str, ...]


def census_readers(members: Mapping[str, bytes], source_roots: Sequence[str]) -> ReaderCensus:
    """Scan every ``.py`` member below ``source_roots`` for anchored path expressions.

    ``members`` maps archive paths to regular-file bytes.  A module that does
    not parse raises ``SyntaxError``: shipped code that cannot be read cannot
    be explained.
    """
    modules = _modules(members, source_roots)
    resolver = _Resolver(modules)
    scan = _Scan()
    for module in modules.values():
        scan.module(module, resolver)
    return ReaderCensus(
        reads=tuple(sorted(set(scan.reads), key=lambda item: (item.location, item.path))),
        findings=tuple(sorted(set(scan.findings), key=lambda item: (item.location, item.kind, item.reason))),
        raw_reads=tuple(scan.raw_reads),
        modules=tuple(sorted(module.path for module in modules.values())),
    )


def uncovered_reads(raw_reads: Iterable[RawRead], leaf_names: frozenset[str]) -> tuple[Finding, ...]:
    """Raw reads of a derived leaf name: never production coverage."""
    return tuple(
        Finding(
            FINDING_UNCOVERED_READ,
            raw.location,
            raw.expression,
            f"reads {name} through a path that is not production-anchored; a development-layout pass here is not installed-layout coverage",
        )
        for raw in raw_reads
        for name in sorted(raw.names & leaf_names)
    )


def verify_archive(requirements: Iterable[Requirement], regular_members: frozenset[str]) -> tuple[Requirement, ...]:
    """Stamp each still-open requirement with whether the archive holds its leaf.

    Only the leaf's own regular member satisfies it.  An existing parent
    directory, or any other member below it, never does.
    """
    return tuple(replace(item, coverage=COVERAGE_ARCHIVE_MEMBER if item.source_leaf in regular_members else COVERAGE_MISSING) if item.coverage == COVERAGE_REQUIRED else item for item in requirements)


def census_mirrors(paths: Iterable[str], leaf_names: frozenset[str], canonical: frozenset[str]) -> tuple[MirrorRecord, ...]:
    """Every directory holding at least one bundle leaf, found by directory, not by a list.

    A mirror that no list names -- a fixture directory whose contents ARE the
    set -- is found exactly like a listed one.
    """
    by_directory: dict[str, set[str]] = {}
    for path in paths:
        directory, name = posixpath.split(path)
        if name in leaf_names:
            by_directory.setdefault(directory, set()).add(name)
    return tuple(MirrorRecord(directory, tuple(sorted(names)), tuple(sorted(leaf_names - names)), directory in canonical) for directory, names in sorted(by_directory.items()))


@dataclass(slots=True)
class _Module:
    """One parsed payload module, indexed for narrow resolution.

    ``parents`` links every node to its parent, to find enclosing loops and
    functions; ``constants`` and ``imports`` resolve module-level names;
    ``functions`` and ``calls`` (bare-name call sites) let a parameter be
    resolved from the arguments its same-module callers pass.
    """

    path: str
    package: str
    tree: ast.Module
    parents: dict[ast.AST, ast.AST]
    constants: dict[str, ast.expr]
    imports: dict[str, tuple[str, str]]
    functions: dict[str, ast.FunctionDef]
    calls: dict[str, list[ast.Call]]


def _modules(members: Mapping[str, bytes], source_roots: Sequence[str]) -> dict[str, _Module]:
    """Parse the ``.py`` members below each source root, keyed by dotted name."""
    modules: dict[str, _Module] = {}
    for root in source_roots:
        prefix = f"{root}/"
        for path in sorted(name for name in members if name.startswith(prefix) and name.endswith(".py")):
            dotted = path.removeprefix(prefix).removesuffix(".py").replace("/", ".")
            name = dotted.removesuffix(".__init__")
            package = name if dotted.endswith(".__init__") else name.rpartition(".")[0]
            modules[name] = _parse_module(path, package, members[path])
    return modules


def _parse_module(path: str, package: str, raw: bytes) -> _Module:
    """Index one module's constants, imports, functions and call sites."""
    tree = ast.parse(raw.decode("utf-8"), filename=path)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    module = _Module(path, package, tree, parents, {}, {}, {}, {})
    for statement in tree.body:
        _index_statement(module, statement)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            module.calls.setdefault(node.func.id, []).append(node)
    return module


def _index_statement(module: _Module, statement: ast.stmt) -> None:
    """Record a top-level function, ``from`` import or single-name assignment."""
    if isinstance(statement, ast.FunctionDef):
        module.functions[statement.name] = statement
    elif isinstance(statement, ast.ImportFrom):
        base = _import_base(module.package, statement.module, statement.level)
        for alias in statement.names:
            module.imports[alias.asname or alias.name] = (base, alias.name)
    elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
        target = statement.targets[0] if isinstance(statement, ast.Assign) else statement.target
        if isinstance(target, ast.Name) and statement.value is not None:
            module.constants[target.id] = statement.value


def _import_base(package: str, module: str | None, level: int) -> str:
    """The dotted module a ``from`` import names, relative imports resolved."""
    if level == 0:
        return module or ""
    parts = package.split(".")[: len(package.split(".")) - (level - 1)]
    return ".".join([*parts, module] if module else parts)


def _package_directories(modules: Mapping[str, _Module]) -> dict[str, tuple[str, ...]]:
    """Archive directory parts of every package (a module that is an ``__init__``)."""
    return {name: tuple(posixpath.dirname(module.path).split("/")) for name, module in modules.items() if module.path.endswith("/__init__.py")}


@dataclass(frozen=True, slots=True)
class _Anchored:
    """A path below one anchor, as its parts.

    Abstract values are plain strings (segments), anchored paths, or unknowns.
    """

    anchor: str
    parts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Unknown:
    """A value narrow resolution could not finish.

    ``anchored`` records whether it arose while extending an anchored path:
    only those are advisories, because an unanchored unknown is ordinary
    runtime input such as a caller's directory.
    """

    reason: str
    anchored: bool


type _Value = str | _Anchored | _Unknown


def _join(left: _Value, right: _Value) -> _Value:
    """``left / right`` over abstract values; only an anchored left side is extended."""
    if isinstance(left, _Unknown):
        return left
    if not isinstance(left, _Anchored):
        return _Unknown("unanchored join", False)
    if not isinstance(right, str):
        reason = right.reason if isinstance(right, _Unknown) else "an anchored path used as a segment"
        return _Unknown(f"unresolved segment ({reason})", True)
    return _Anchored(left.anchor, (*left.parts, *(piece for piece in right.split("/") if piece not in ("", "."))))


def _parent(value: _Value) -> _Value:
    """``.parent`` of an anchored path; anything else is left as it was."""
    if not isinstance(value, _Anchored) or not value.parts:
        return value if isinstance(value, _Unknown) else _Unknown("parent of an unanchored path", False)
    return _Anchored(value.anchor, value.parts[:-1])


def _with_name(value: _Value, name: _Value) -> _Value:
    """``.with_name(name)`` of an anchored path."""
    if not isinstance(value, _Anchored):
        return value if isinstance(value, _Unknown) else _Unknown("with_name of an unanchored path", False)
    if not isinstance(name, str):
        return _Unknown("unresolved with_name argument", True)
    return _Anchored(value.anchor, (*value.parts[:-1], name))


def _constant(node: ast.Constant) -> _Value:
    """A string literal is a path segment; any other literal is not."""
    return node.value if isinstance(node.value, str) else _Unknown("non-string constant", False)


def _limit(values: tuple[_Value, ...]) -> tuple[_Value, ...]:
    """Collapse a blown-up alternative set into one honest unknown."""
    if len(values) <= _MAX_ALTERNATIVES:
        return values
    return (_Unknown("too many alternatives", any(isinstance(value, _Anchored) for value in values)),)


def _binds(target: ast.expr, name: str) -> bool:
    """Whether an assignment or loop target is exactly the plain name ``name``."""
    return isinstance(target, ast.Name) and target.id == name


def _loop_iterable(parent: ast.AST, child: ast.AST, name: str) -> ast.expr | None:
    """The iterable a ``for`` statement or comprehension binds ``name`` from, if any."""
    if isinstance(parent, ast.For) and child in parent.body and _binds(parent.target, name):
        return parent.iter
    generators = parent.generators if isinstance(parent, (ast.GeneratorExp, ast.ListComp, ast.SetComp)) else []
    return next((item.iter for item in generators if _binds(item.target, name)), None)


def _enclosing_function(module: _Module, node: ast.AST) -> ast.FunctionDef | None:
    """The innermost function containing ``node``; ``None`` at module level."""
    current = module.parents.get(node)
    while current is not None and not isinstance(current, ast.FunctionDef):
        current = module.parents.get(current)
    return current


def _local_assignments(function: ast.FunctionDef, name: str) -> list[ast.expr]:
    """Every value assigned to ``name`` anywhere inside ``function``."""
    return [statement.value for statement in ast.walk(function) if isinstance(statement, ast.Assign) and any(_binds(target, name) for target in statement.targets)]


def _call_argument(call: ast.Call, index: int, name: str) -> ast.expr | None:
    """The argument a call passes for positional parameter ``index`` named ``name``."""
    keyword = next((item.value for item in call.keywords if item.arg == name), None)
    if keyword is not None or index >= len(call.args):
        return keyword
    return call.args[index]


def _is_sys_prefix(node: ast.expr) -> bool:
    """``sys.prefix``: the installed environment's root, a Homebrew keg's ``libexec``."""
    return isinstance(node, ast.Attribute) and node.attr == "prefix" and isinstance(node.value, ast.Name) and node.value.id == "sys"


def _is_path_constructor(node: ast.expr) -> bool:
    """A bare ``Path``-family constructor name, as the payload imports it."""
    return isinstance(node, ast.Name) and node.id in _PATH_CONSTRUCTORS


def _is_resources_files(module: _Module, node: ast.expr) -> bool:
    """``resources.files`` after ``from importlib import resources``, or an imported ``files``."""
    if isinstance(node, ast.Name):
        return module.imports.get(node.id) == ("importlib.resources", "files")
    owner = node.value if isinstance(node, ast.Attribute) and node.attr == "files" else None
    return isinstance(owner, ast.Name) and module.imports.get(owner.id) == ("importlib", "resources")


class _Resolver:
    """Abstract values of path expressions, narrowed to the forms the payload uses.

    Memoized per node; a recursive or too-deep chain is an unknown, never a
    guess.  A tuple or list resolves to its elements as alternatives, which is
    how a loop over a constant tuple binds its variable.
    """

    def __init__(self, modules: Mapping[str, _Module]) -> None:
        """Resolve within ``modules`` only; a name from anywhere else is an unknown."""
        self._modules = modules
        self._packages = _package_directories(modules)
        self._memo: dict[tuple[str, int], tuple[_Value, ...]] = {}
        self._active: set[tuple[str, int]] = set()

    def values(self, module: _Module, node: ast.expr, depth: int = 0) -> tuple[_Value, ...]:
        """Every value ``node`` may take, as far as narrow resolution can tell."""
        key = (module.path, id(node))
        cached = self._memo.get(key)
        if cached is not None:
            return cached
        if depth > _MAX_DEPTH or key in self._active:
            return (_Unknown("recursive or too deep", False),)
        self._active.add(key)
        try:
            result = _limit(self._dispatch(module, node, depth + 1))
        finally:
            self._active.discard(key)
        self._memo[key] = result
        return result

    def _dispatch(self, module: _Module, node: ast.expr, depth: int) -> tuple[_Value, ...]:
        """Route one expression to the rule for its node type.

        Anything outside these forms is an unknown, which only becomes an
        advisory if it was reached while extending an anchored path.
        """
        if isinstance(node, ast.Constant):
            return (_constant(node),)
        if isinstance(node, ast.Name):
            return self._name(module, node, depth)
        if isinstance(node, ast.BinOp):
            return self._divide(module, node, depth)
        if isinstance(node, ast.Call):
            return self._call(module, node, depth)
        if isinstance(node, ast.Attribute):
            return self._attribute(module, node, depth)
        if isinstance(node, (ast.Tuple, ast.List, ast.Starred, ast.IfExp)):
            return self._alternatives(module, node, depth)
        return (_Unknown(f"unsupported {type(node).__name__}", False),)

    def _divide(self, module: _Module, node: ast.BinOp, depth: int) -> tuple[_Value, ...]:
        """``left / right`` for every pair of alternatives; any other operator is not a path."""
        if not isinstance(node.op, ast.Div):
            return (_Unknown("arithmetic, not a path join", False),)
        lefts, rights = self.values(module, node.left, depth), self.values(module, node.right, depth)
        return tuple(_join(left, right) for left in lefts for right in rights)

    def _attribute(self, module: _Module, node: ast.Attribute, depth: int) -> tuple[_Value, ...]:
        """``.parent`` drops one part; any other attribute is runtime state, not a path."""
        if node.attr != "parent":
            return (_Unknown(f"attribute {ast.unparse(node)}", False),)
        return tuple(_parent(value) for value in self.values(module, node.value, depth))

    def _alternatives(self, module: _Module, node: ast.Tuple | ast.List | ast.Starred | ast.IfExp, depth: int) -> tuple[_Value, ...]:
        """Elements of a literal sequence, a starred sequence, or both conditional branches."""
        if isinstance(node, ast.IfExp):
            branches = [node.body, node.orelse]
        elif isinstance(node, ast.Starred):
            branches = [node.value]
        else:
            branches = node.elts
        return tuple(value for branch in branches for value in self.values(module, branch, depth))

    def _name(self, module: _Module, node: ast.Name, depth: int) -> tuple[_Value, ...]:
        """A name: ``__file__``, a loop variable, a local, a parameter, a constant, or an import."""
        if node.id == "__file__":
            return (_Anchored(ANCHOR_SOURCE, tuple(module.path.split("/"))),)
        iterable = self._loop_iterable(module, node)
        if iterable is not None:
            return self.values(module, iterable, depth)
        function = _enclosing_function(module, node)
        local = self._local_values(module, function, node.id, depth) if function is not None else None
        if local is not None:
            return local
        binding = self._global(module, node.id)
        return self.values(binding[0], binding[1], depth) if binding is not None else (_Unknown(f"name {node.id}", False),)

    def _loop_iterable(self, module: _Module, node: ast.Name) -> ast.expr | None:
        """The iterable of the nearest enclosing loop that binds ``node``, inside its function."""
        current: ast.AST = node
        while (parent := module.parents.get(current)) is not None and not isinstance(parent, ast.FunctionDef):
            iterable = _loop_iterable(parent, current, node.id)
            if iterable is not None:
                return iterable
            current = parent
        return None

    def _local_values(self, module: _Module, function: ast.FunctionDef, name: str, depth: int) -> tuple[_Value, ...] | None:
        """Values from local assignments, else from every same-module caller's argument.

        ``None`` when ``name`` is neither assigned nor a positional parameter
        of ``function``, so the caller falls through to module scope.
        """
        assigned = _local_assignments(function, name)
        if assigned:
            return tuple(value for item in assigned for value in self.values(module, item, depth))
        positional = [argument.arg for argument in (*function.args.posonlyargs, *function.args.args)]
        if name not in positional:
            return None
        values: list[_Value] = []
        for call in module.calls.get(function.name, ()):
            argument = _call_argument(call, positional.index(name), name)
            values.extend(self.values(module, argument, depth) if argument is not None else (_Unknown(f"call omits {name}", False),))
        return tuple(values) or (_Unknown(f"parameter {name} of {function.name}() has no local caller", False),)

    def _global(self, module: _Module, name: str) -> tuple[_Module, ast.expr] | None:
        """A module constant, or the constant an imported name refers to."""
        if name in module.constants:
            return module, module.constants[name]
        base, attribute = module.imports.get(name, ("", ""))
        target = self._modules.get(base)
        if target is None or attribute not in target.constants:
            return None
        return target, target.constants[attribute]

    def _call(self, module: _Module, node: ast.Call, depth: int) -> tuple[_Value, ...]:
        """Path construction, ``resources.files``, a path method, or a no-argument provider."""
        func = node.func
        if _is_path_constructor(func):
            return self._construct(module, node, depth)
        if _is_resources_files(module, func):
            return (self._package_anchor(module, node),)
        if isinstance(func, ast.Attribute) and func.attr in _PATH_METHODS:
            return self._method(module, node, func, depth)
        provider = self._provider(module, func) if not node.args else None
        if provider is not None:
            return self.values(provider[0], provider[1], depth)
        return (_Unknown(f"call {ast.unparse(func)}", False),)

    def _construct(self, module: _Module, node: ast.Call, depth: int) -> tuple[_Value, ...]:
        """``Path(sys.prefix)`` is the prefix anchor; ``Path(x)`` is ``x``."""
        if not node.args:
            return (_Unknown("Path()", False),)
        if _is_sys_prefix(node.args[0]):
            return (_Anchored(ANCHOR_PREFIX, ()),)
        if any(isinstance(item, ast.Name) and item.id == "__import__" for item in ast.walk(node.args[0])):
            return (_Unknown("module file of a dynamically chosen module", True),)
        return self.values(module, node.args[0], depth)

    def _method(self, module: _Module, node: ast.Call, func: ast.Attribute, depth: int) -> tuple[_Value, ...]:
        """``joinpath`` folds its arguments; ``with_name`` replaces; the rest keep the path."""
        current = self.values(module, func.value, depth)
        if func.attr == "with_name" and node.args:
            names = self.values(module, node.args[0], depth)
            return tuple(_with_name(value, name) for value in current for name in names)
        for segment in node.args if func.attr == "joinpath" else ():
            parts = self.values(module, segment, depth)
            current = _limit(tuple(_join(value, part) for value in current for part in parts))
        return current

    def _provider(self, module: _Module, func: ast.expr) -> tuple[_Module, ast.expr] | None:
        """The single ``return`` expression of a same-module or imported no-argument function."""
        if not isinstance(func, ast.Name):
            return None
        owner: _Module | None = module
        name = func.id
        if name not in module.functions:
            base, name = module.imports.get(func.id, ("", ""))
            owner = self._modules.get(base)
        function = owner.functions.get(name) if owner is not None else None
        if owner is None or function is None:
            return None
        returns = [item.value for item in ast.walk(function) if isinstance(item, ast.Return)]
        if len(returns) != 1 or returns[0] is None:
            return None
        return owner, returns[0]

    def _package_anchor(self, module: _Module, node: ast.Call) -> _Value:
        """``resources.files("pkg")`` is the package's directory in the archive."""
        argument = node.args[0] if node.args else None
        package = argument.value if isinstance(argument, ast.Constant) else None
        directory = self._packages.get(package) if isinstance(package, str) else None
        if directory is None:
            return _Unknown(f"resources.files() of a package no payload distribution ships ({ast.unparse(node)})", True)
        return _Anchored(ANCHOR_SOURCE, directory)


def _is_path_expression(module: _Module, node: ast.AST) -> bool:
    """A division, a path constructor, ``resources.files``, a path method or ``.parent``.

    Only MAXIMAL path expressions are resolved -- ``a / b / c`` once, never
    its prefixes -- so each read is reported at the expression that reads.
    """
    if isinstance(node, ast.BinOp):
        return isinstance(node.op, ast.Div)
    if isinstance(node, ast.Attribute):
        return node.attr == "parent"
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return _is_path_constructor(func) or _is_resources_files(module, func) or (isinstance(func, ast.Attribute) and func.attr in _PATH_METHODS)


def _extends_parent(module: _Module, node: ast.AST) -> bool:
    """Whether ``node`` is only the left part of a larger path expression."""
    parent = module.parents.get(node)
    if isinstance(parent, ast.BinOp):
        return parent.left is node and isinstance(parent.op, ast.Div)
    if isinstance(parent, ast.Attribute):
        return parent.value is node and parent.attr in _PATH_ATTRIBUTES
    return isinstance(parent, ast.Call) and _is_path_constructor(parent.func) and node in parent.args


class _Scan:
    """Collects reads, findings and raw reads across one census.

    What resolves to an anchor is a read; what does not is kept with the
    leaf names it may read, so the adapter can mark raw reads of derived
    leaves as uncovered.
    """

    def __init__(self) -> None:
        """Start an empty census."""
        self.reads: list[AnchoredRead] = []
        self.findings: list[Finding] = []
        self.raw_reads: list[RawRead] = []

    def module(self, module: _Module, resolver: _Resolver) -> None:
        """Classify every maximal path expression in ``module``."""
        for node in ast.walk(module.tree):
            if isinstance(node, ast.expr) and _is_path_expression(module, node) and not _extends_parent(module, node):
                self._expression(module, resolver, node)

    def _expression(self, module: _Module, resolver: _Resolver, node: ast.expr) -> None:
        """Record what one maximal expression reads, or what it could not resolve.

        An expression with no anchored alternative is a raw read; one with
        both anchored and other alternatives also records that the other
        alternatives (an explicit or caller-supplied path) are not coverage.
        """
        location, expression = f"{module.path}:{node.lineno}", ast.unparse(node)[:200]
        values = resolver.values(module, node)
        anchored, unresolved = _partition(values)
        if not anchored and not unresolved:
            self.raw_reads.append(RawRead(location, expression, _leaf_names(resolver, module, node)))
            return
        self.reads.extend(AnchoredRead(location, expression, value.anchor, "/".join(value.parts)) for value in anchored)
        self.findings.extend(Finding(FINDING_UNRESOLVED, location, expression, value.reason) for value in unresolved)
        if len(anchored) + len(unresolved) < len(values):
            reason = "a caller-supplied alternative path is not production coverage"
            self.findings.append(Finding(FINDING_CALLER_ALTERNATIVE, location, expression, reason))


def _partition(values: tuple[_Value, ...]) -> tuple[list[_Anchored], list[_Unknown]]:
    """Split alternatives into anchored paths and anchored-but-unresolved unknowns."""
    anchored = [value for value in values if isinstance(value, _Anchored)]
    unresolved = [value for value in values if isinstance(value, _Unknown) and value.anchored]
    return anchored, unresolved


def _leaf_names(resolver: _Resolver, module: _Module, node: ast.expr) -> frozenset[str]:
    """Final-segment names an unanchored path expression may read."""
    tail = node.right if isinstance(node, ast.BinOp) else (node.args[-1] if isinstance(node, ast.Call) and node.args else None)
    if tail is None:
        return frozenset()
    return frozenset(posixpath.basename(value) for value in resolver.values(module, tail) if isinstance(value, str))
