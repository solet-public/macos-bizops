"""Source-only resolution for the service-interface signature gate.

Only explicit imports, class definitions, constructor assignments and selected
profile bindings establish coverage. Unsupported expressions raise a coverage
reason; no runtime module, constructor or configuration expression is executed.
"""

from __future__ import annotations

import ast
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast


class StaticResolutionError(Exception):
    """A route cannot be established from the selected source tree."""


@dataclass(frozen=True)
class ClassRef:
    path: Path
    node: ast.ClassDef


@dataclass
class SourceIndex:
    root: Path
    trees: dict[Path, ast.Module] = field(default_factory=dict)

    def tree(self, path: Path) -> ast.Module:
        """Cache parsed source; unavailable input carries its original reason."""
        if path not in self.trees:
            try:
                self.trees[path] = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError, UnicodeError) as exc:
                raise StaticResolutionError(f"source {path.relative_to(self.root)}: {exc}") from exc
        return self.trees[path]

    def module(self, name: str) -> Path:
        """Resolve only checkout module roots, never installed runtime code."""
        relative = Path(*name.split("."))
        roots = [self.root / "ananta/src", *sorted(self.root.glob("plugins/*/src"))]
        candidates = [path for root in roots for path in (root / relative.with_suffix(".py"), root / relative / "__init__.py") if path.is_file()]
        if len(candidates) != 1:
            raise StaticResolutionError(f"import {name}: expected one source, found {len(candidates)}")
        return candidates[0]

    def module_name(self, path: Path) -> str:
        """Recover a package name for relative imports within indexed roots."""
        for root in [self.root / "ananta/src", *sorted(self.root.glob("plugins/*/src"))]:
            if path.is_relative_to(root):
                relative = path.relative_to(root).with_suffix("")
                parts = relative.parts[:-1] if path.name == "__init__.py" else relative.parts
                return ".".join(parts)
        raise StaticResolutionError(f"source outside module roots: {path}")


def _statements(nodes: list[ast.stmt]) -> list[ast.stmt]:
    """Flatten control blocks, retaining lexical class/function boundaries."""
    result: list[ast.stmt] = []
    for node in nodes:
        result.append(node)
        if isinstance(node, ast.If | ast.Try | ast.With | ast.For | ast.While):
            result.extend(_statements(node.body))
            if isinstance(node, ast.If | ast.Try | ast.For | ast.While):
                result.extend(_statements(node.orelse))
            if isinstance(node, ast.Try):
                result.extend(_statements(node.finalbody))
                for handler in node.handlers:
                    result.extend(_statements(handler.body))
    return result


def _scopes(tree: ast.Module, line: int) -> list[list[ast.stmt]]:
    enclosing = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.lineno <= line <= (node.end_lineno or node.lineno)]
    enclosing.sort(key=lambda node: node.lineno, reverse=True)
    return [*[_statements(node.body) for node in enclosing], _statements(tree.body)]


def _dotted(expr: ast.expr) -> list[str]:
    if isinstance(expr, ast.Name):
        return [expr.id]
    if isinstance(expr, ast.Attribute):
        return [*_dotted(expr.value), expr.attr]
    raise StaticResolutionError(f"dynamic expression {ast.unparse(expr)}")


def _import_module(index: SourceIndex, path: Path, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    package = index.module_name(path).split(".")
    if path.name != "__init__.py":
        package.pop()
    if node.level > len(package):
        raise StaticResolutionError("relative import escapes package")
    prefix = package[: len(package) - node.level + 1]
    return ".".join([*prefix, *([node.module] if node.module else [])])


def _binding_value(node: ast.stmt, name: str) -> ast.expr | None:
    if isinstance(node, ast.Assign):
        if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return node.value
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        if node.target.id == name:
            return node.value
    return None


def _symbol(
    index: SourceIndex,
    path: Path,
    parts: list[str],
    line: int,
    seen: frozenset[tuple[Path, str]] = frozenset(),
) -> ClassRef:
    """Resolve nearest lexical bindings, then module bindings.

    Every definition at the selected scope must establish the same class;
    conditional alternatives cannot silently pick whichever appears first.
    Visited symbol identity bounds recursion through import/assignment cycles.
    """
    key = (path, ".".join(parts))
    if key in seen:
        raise StaticResolutionError(f"cyclic symbol {key[1]}")
    seen = seen | {key}
    for scope in _scopes(index.tree(path), line):
        matches = [node for node in scope if _defines(node, parts[0])]
        if not matches:
            continue
        refs = [_resolve_definition(index, path, node, parts, seen) for node in matches]
        if len({(ref.path, ref.node.name) for ref in refs}) != 1:
            raise StaticResolutionError(f"ambiguous symbol {parts[0]}")
        return refs[0]
    raise StaticResolutionError(f"unresolved symbol {'.'.join(parts)} in {path.relative_to(index.root)}")


def _defines(node: ast.stmt, name: str) -> bool:
    if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
        return node.name == name
    if isinstance(node, ast.ImportFrom | ast.Import):
        return any((alias.asname or alias.name.split(".")[0]) == name for alias in node.names)
    return _binding_value(node, name) is not None


def _resolve_definition(
    index: SourceIndex,
    path: Path,
    node: ast.stmt,
    parts: list[str],
    seen: frozenset[tuple[Path, str]],
) -> ClassRef:
    if isinstance(node, ast.ClassDef) and len(parts) == 1:
        return ClassRef(path, node)
    if isinstance(node, ast.ImportFrom | ast.Import):
        return _resolve_import(index, path, node, parts, seen)
    value = _binding_value(node, parts[0])
    if value is not None and len(parts) == 1:
        expr = value.func if isinstance(value, ast.Call) else value
        return _symbol(index, path, _dotted(expr), node.lineno, seen)
    raise StaticResolutionError(f"{parts[0]} is not a statically resolved class")


def _resolve_import(
    index: SourceIndex,
    path: Path,
    node: ast.ImportFrom | ast.Import,
    parts: list[str],
    seen: frozenset[tuple[Path, str]],
) -> ClassRef:
    """Follow aliases through a module or package's explicit reexports.

    A dotted imported module can consume successive package components;
    the last component must still resolve to a source class definition.
    Ambiguous modules and cyclic reexports remain coverage advisories.
    """
    alias = next(a for a in node.names if (a.asname or a.name.split(".")[0]) == parts[0])
    if isinstance(node, ast.ImportFrom):
        module = _import_module(index, path, node)
        target = [alias.name, *parts[1:]]
    else:
        module = alias.name if alias.asname else parts[0]
        target = parts[1:]
    while len(target) > 1:
        try:
            child = index.module(module + "." + target[0])
        except StaticResolutionError:
            break
        module = index.module_name(child)
        target = target[1:]
    if not target:
        raise StaticResolutionError(f"module {module} is not a concrete class")
    return _symbol(index, index.module(module), target, 0, seen)


def _class_for(index: SourceIndex, path: Path, expr: ast.expr, line: int) -> ClassRef:
    target = expr.func if isinstance(expr, ast.Call) else expr
    return _symbol(index, path, _dotted(target), line)


def _assignment_scopes(tree: ast.Module, receiver: str) -> list[ast.stmt]:
    if receiver == "self":
        classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ServiceManager"]
        if len(classes) != 1:
            raise StaticResolutionError("ServiceManager construction class absent or ambiguous")
        return classes[0].body
    return tree.body


def _assignments(tree: ast.Module, receiver: str, provider: str) -> list[ast.expr]:
    values: list[ast.expr] = []
    for function in _assignment_scopes(tree, receiver):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if receiver not in {arg.arg for arg in function.args.args}:
            continue
        for node in _statements(function.body):
            value = _service_assignment(node, receiver, provider)
            if value is not None:
                values.append(value)
    return values


def _service_assignment(node: ast.stmt, receiver: str, provider: str) -> ast.expr | None:
    """Read one explicit attribute assignment, ignoring uninitialized None."""
    if isinstance(node, ast.Assign):
        targets, value = node.targets, node.value
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        targets, value = [node.target], node.value
    else:
        return None
    if isinstance(value, ast.Constant) and value.value is None:
        return None
    expected = f"{receiver}.{provider}"
    return value if any(ast.unparse(target) == expected for target in targets) else None


def _constructed(
    index: SourceIndex,
    path: Path,
    receiver: str,
    provider: str,
    seen: frozenset[tuple[Path, str]] = frozenset(),
) -> ClassRef:
    """Resolve every non-None source construction for one routed attribute.

    None is a documented optional-service initialization state. Different
    class constructions or dynamic values cannot establish one signature.
    Aliases may refer to the same receiver or its constructed service manager.
    """
    key = (path, provider)
    if key in seen:
        raise StaticResolutionError(f"cyclic service construction for {provider}")
    values = _assignments(index.tree(path), receiver, provider)
    if not values:
        raise StaticResolutionError(f"no explicit {receiver}.{provider} construction in {path.name}")
    refs = [_construction_value(index, path, receiver, value, seen | {key}) for value in values]
    if len({(ref.path, ref.node.name) for ref in refs}) != 1:
        raise StaticResolutionError(f"multiple concrete constructions for {provider}")
    return refs[0]


def _construction_value(
    index: SourceIndex,
    path: Path,
    receiver: str,
    value: ast.expr,
    seen: frozenset[tuple[Path, str]],
) -> ClassRef:
    if isinstance(value, ast.Attribute):
        parts = _dotted(value)
        if len(parts) == 2 and parts[0] == receiver:
            return _constructed(index, path, receiver, parts[1], seen)
        if len(parts) == 3 and parts[:2] == [receiver, "service_manager"]:
            manager = path.with_name("service_manager.py")
            return _constructed(index, manager, "self", parts[2], seen)
    return _class_for(index, path, value, value.lineno)


def _strings(value: ast.expr) -> set[str]:
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
        if value.func.id == "frozenset" and len(value.args) == 1:
            return _strings(value.args[0])
    if isinstance(value, ast.Set | ast.List | ast.Tuple):
        if all(isinstance(item, ast.Constant) and isinstance(item.value, str) for item in value.elts):
            return {cast(str, cast(ast.Constant, item).value) for item in value.elts}
    raise StaticResolutionError("dynamic routing membership")


def _routing_class(index: SourceIndex) -> ast.ClassDef:
    """Anchor routing to the orchestrator, excluding unrelated classes."""
    path = index.root / "ananta/src/ananta/core/event_orchestrator.py"
    classes = [node for node in index.tree(path).body if isinstance(node, ast.ClassDef) and node.name == "EventOrchestrator"]
    if len(classes) != 1:
        raise StaticResolutionError("EventOrchestrator routing class absent or ambiguous")
    return classes[0]


def _memberships(cls: ast.ClassDef) -> dict[str, set[str]]:
    """Read literal membership sets; dynamic membership cannot prove a route."""
    names = ("_DIRECT_ATTR_SERVICES", "_SERVICE_MANAGER_SERVICES")
    return {name: _strings(value) for node in cls.body for name in names if (value := _binding_value(node, name)) is not None}


def _service_comparison(test: ast.expr) -> bool:
    """Recognize only simple comparisons against the routed service name."""
    return isinstance(test, ast.Compare) and len(test.comparators) == 1 and isinstance(test.left, ast.Name) and test.left.id == "service_name"


def _routing_test(
    test: ast.expr,
    memberships: dict[str, set[str]],
    provider: str,
) -> str | None:
    """Interpret the measured service-name membership and wrapper tests."""
    if not _service_comparison(test):
        return None
    assert isinstance(test, ast.Compare)
    right = test.comparators[0]
    if isinstance(test.ops[0], ast.In) and isinstance(right, ast.Attribute):
        if provider in memberships.get(right.attr, set()):
            return "manager" if right.attr == "_SERVICE_MANAGER_SERVICES" else "direct"
    if isinstance(test.ops[0], ast.Eq) and isinstance(right, ast.Constant):
        return "wrapper" if right.value == provider else None
    return None


def _route(index: SourceIndex, provider: str) -> str:
    """Preserve source test order so plugin bindings never shadow wrappers."""
    cls = _routing_class(index)
    memberships = _memberships(cls)
    method = next((node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "get_service"), None)
    if method is None or len(memberships) != 2:
        raise StaticResolutionError("get_service routing/membership unavailable")
    for node in method.body:
        if isinstance(node, ast.If):
            route = _routing_test(node.test, memberships, provider)
            if route is not None:
                _verify_route_body(node, route, provider)
                return route
    return "plugin"


def _verify_route_body(node: ast.If, route: str, provider: str) -> None:
    """Do not infer a concrete route if the measured return shape changes."""
    returned = ast.unparse(node.body[-1])
    if route in {"direct", "manager"}:
        receiver = "self.service_manager" if route == "manager" else "self"
        if returned == f"return getattr({receiver}, service_name, None)":
            return
    if route == "wrapper":
        expected = f"wrapper = getattr(self, {provider!r}, None)"
        fallback = "return wrapper if wrapper is not None else self._get_bound_plugin_service(service_name)"
        if ast.unparse(node.body[0]) == expected and returned == fallback:
            return
    raise StaticResolutionError(f"unsupported {route} routing body for {provider}")


def _entry_point(data: dict[str, object], plugin: str) -> object:
    """Read the metadata mapping without executing or trusting malformed shapes."""
    value: object = data
    for name in ("project", "entry-points", "ananta.plugins", plugin):
        if not isinstance(value, dict):
            raise StaticResolutionError(f"malformed source entrypoint metadata for {plugin}")
        value = value.get(name)
    return value


def _plugin(index: SourceIndex, provider: str) -> ClassRef:
    """Use the selected profile's explicit source entrypoint for plugin routes.

    This deliberately avoids installed metadata and environment overrides:
    the gate measures the checkout configuration, not a running instance.
    Missing profile data in an archive discloses incomplete coverage.
    """
    config = index.root / "profile/config/service_bindings.json"
    try:
        bindings: object = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StaticResolutionError(f"selected binding file {config.relative_to(index.root)}: {exc}") from exc
    if not isinstance(bindings, dict):
        raise StaticResolutionError("selected binding file is not a mapping")
    plugin = cast(dict[str, object], bindings).get(provider)
    if not isinstance(plugin, str) or not plugin or Path(plugin).name != plugin:
        raise StaticResolutionError(f"no explicit selected binding for {provider}")
    metadata = index.root / "plugins" / plugin / "pyproject.toml"
    try:
        data = tomllib.loads(metadata.read_text(encoding="utf-8"))
        entry = _entry_point(data, plugin)
    except (OSError, ValueError) as exc:
        raise StaticResolutionError(f"plugin entrypoint {plugin}: {exc}") from exc
    if not isinstance(entry, str) or entry.count(":") != 1:
        raise StaticResolutionError(f"no explicit source entrypoint for {plugin}")
    module, name = entry.split(":")
    return _symbol(index, index.module(module), name.split("."), 0)


def concrete_class(index: SourceIndex, provider: str) -> ClassRef:
    """Resolve direct/manager/wrapper routes before selected plugin entrypoints."""
    route = _route(index, provider)
    core = index.root / "ananta/src/ananta/core/orchestration"
    if route == "manager":
        return _constructed(index, core / "service_manager.py", "self", provider)
    if route in {"direct", "wrapper"}:
        return _constructed(index, core / "startup_sequence.py", "orch", provider)
    return _plugin(index, provider)


def _mro(index: SourceIndex, ref: ClassRef, seen: frozenset[tuple[Path, str]]) -> list[ClassRef]:
    """Build a source C3 linearization; unresolved external bases stay unknown."""
    key = (ref.path, ref.node.name)
    if key in seen:
        raise StaticResolutionError(f"cyclic base {ref.node.name}")
    bases: list[ClassRef] = []
    for base in ref.node.bases:
        if isinstance(base, ast.Name) and base.id == "object":
            continue
        bases.append(_class_for(index, ref.path, base, ref.node.lineno))
    sequences = [_mro(index, base, seen | {key}) for base in bases] + [bases.copy()]
    return [ref, *_merge_bases(sequences)]


def _merge_bases(sequences: list[list[ClassRef]]) -> list[ClassRef]:
    """C3 merge preserves diamonds and ordered local mixins.

    A candidate head must not occur in any other remaining tail. Removing
    that head from all sequences preserves Python's monotonic precedence.
    Unknown or inconsistent bases are never replaced with guessed matches.
    """
    result: list[ClassRef] = []
    while any(sequences):
        sequences = [seq for seq in sequences if seq]
        head = _merge_head(sequences)
        result.append(head)
        for seq in sequences:
            if seq[0] == head:
                seq.pop(0)
    return result


def _merge_head(sequences: list[list[ClassRef]]) -> ClassRef:
    """Select an eligible C3 head, or disclose inconsistent inheritance."""
    for seq in sequences:
        if all(seq[0] not in other[1:] for other in sequences):
            return seq[0]
    raise StaticResolutionError("inconsistent local inheritance")


def concrete_method(
    index: SourceIndex,
    ref: ClassRef,
    name: str,
) -> tuple[ClassRef, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Select the actual owner under C3 inheritance, never a same-name class."""
    own = [node for node in ref.node.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name]
    if own:
        return ref, own[-1]
    for owner in _mro(index, ref, frozenset())[1:]:
        for node in owner.node.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
                return owner, node
    raise StaticResolutionError(f"method {ref.node.name}.{name} absent from resolved bases")
