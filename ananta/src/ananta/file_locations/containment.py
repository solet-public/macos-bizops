"""Shared location admissibility and advisory-bearing values.

Residuals of the path guard (_confirm_chain): a change after the guard returns
(the follow-up fd-bound handle contract); a pending target's empty deepest
directory replaced between reads; and, only where the final check reads
components one at a time by path (no one-call no-link read), an ancestor
relocated with a link left at its name between its own read and the final
read. An existing target itself is always read last.
"""

import errno
import json
import os
import stat
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from . import no_link_stat
from .resolution import PathAddress, SoletTree, overlaps_solet_tree


@dataclass(frozen=True, slots=True)
class Located[T]:
    """A computed value together with every advisory from obtaining it."""

    value: T
    advisories: tuple[str, ...]


def assert_location_admissible(path: Path, tree: SoletTree) -> None:
    """Require a folder outside the solet's tree on every path-entry write.

    Register, add_entry and update_entry call this guard. Their pass-path
    smoke writes a Documents location outside repo_root and app_home.
    """
    _confirm_chain(_admissible_chain(Path(os.path.realpath(path)), tree))


class PathRefusedError(RuntimeError):
    """A connector path violates the shared file-location boundary."""

    code: str = "file_locations.path_refused"


class PathIdentityRefusedError(PathRefusedError):
    """Filesystem identity could not prove this security boundary."""

    code: str = "file_locations.identity_unavailable"


class PathIdentityChangedError(PathRefusedError):
    """The path no longer has the ancestry used for its admission decision."""

    code: str = "file_locations.identity_changed"


class _IdentityUnavailableError(ValueError):
    """An existing path or its unique ancestry cannot be established."""


class _IdentityChangedError(ValueError):
    """An observed path changed before the guard could return it."""


class _SingleResolutionUnavailableError(RuntimeError):
    """One no-follow resolution of a whole path is unavailable for this path."""


# A directory component is opened for search only (macOS O_SEARCH, Linux
# O_PATH), so a searchable directory without read permission still yields a
# descriptor; elsewhere it is opened read-only.
_SEARCH: int = getattr(os, "O_SEARCH", 0) or getattr(os, "O_PATH", 0) or os.O_RDONLY
_CLOEXEC: int = getattr(os, "O_CLOEXEC", 0)
_COMPONENT_FLAGS: int = _SEARCH | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | _CLOEXEC
# The walk opens each component under its parent's descriptor without following a link.
_DESCRIPTOR_WALK: bool = hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd
# macOS resolves a whole path in one open that refuses a link at any component.
_NO_FOLLOW_ANY: int | None = getattr(os, "O_NOFOLLOW_ANY", None)


@dataclass(frozen=True, slots=True)
class _Chain:
    """One link-free path as a single descriptor chain from the filesystem root.

    identities holds (st_dev, st_ino) from the root directory down to the
    deepest existing component. Each directory is opened under its parent's
    descriptor and read with fstat; the last component is read with fstatat
    under its parent's descriptor. missing names the pending components: the
    first was absent from the deepest opened directory when the walk read it.
    """

    path: Path
    identities: tuple[tuple[int, int], ...]
    missing: tuple[str, ...]

    @property
    def deepest(self) -> Path:
        return Path(self.path.anchor).joinpath(*self.path.parts[1 : len(self.identities)])


def _checked(information: os.stat_result, path: Path, *, leaf: bool) -> tuple[int, int]:
    if stat.S_ISLNK(information.st_mode):
        raise _IdentityChangedError(f"Path component {path} became a link after the path was resolved.")
    if not leaf and not stat.S_ISDIR(information.st_mode):
        raise _IdentityUnavailableError(f"Existing ancestor {path} is not a directory.")
    if stat.S_ISREG(information.st_mode) and information.st_nlink > 1:
        raise _IdentityUnavailableError(f"File {path} has multiple hard links; a unique containing directory cannot be proven.")
    return information.st_dev, information.st_ino


def _open_under(parent: int, name: str, path: Path) -> int | None:
    """Open one directory component under its parent's descriptor; None when it is absent."""
    try:
        return os.open(name, _COMPONENT_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno not in (errno.ENOTDIR, errno.ELOOP, errno.EMLINK):
            raise _IdentityUnavailableError(f"Cannot establish filesystem identity for {path}: {exc}.") from exc
        refused = exc
    # A link or a non-directory refuses the open; one no-follow read names which.
    try:
        information = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError as exc:
        raise _IdentityChangedError(f"Path component {path} changed while it was opened.") from exc
    _checked(information, path, leaf=False)
    raise _IdentityChangedError(f"Path component {path} changed while it was opened.") from refused


def _leaf(parent: int, name: str, path: Path) -> tuple[int, int] | None:
    try:
        return _checked(os.stat(name, dir_fd=parent, follow_symlinks=False), path, leaf=True)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _IdentityUnavailableError(f"Cannot establish filesystem identity for {path}: {exc}.") from exc


def _descriptor_walk(real: Path) -> _Chain:
    names = real.parts[1:]
    try:
        parent = os.open(real.anchor, _COMPONENT_FLAGS)
    except OSError as exc:
        raise _IdentityUnavailableError(f"Cannot open the filesystem root {real.anchor}: {exc}.") from exc
    try:
        identities = [_checked(os.fstat(parent), Path(real.anchor), leaf=False)]
        for depth, name in enumerate(names, start=1):
            path = Path(real.anchor).joinpath(*names[:depth])
            if depth == len(names):
                leaf = _leaf(parent, name, path)
                return _Chain(real, (*identities, leaf) if leaf else tuple(identities), () if leaf else (name,))
            child = _open_under(parent, name, path)
            if child is None:
                return _Chain(real, tuple(identities), names[depth - 1 :])
            os.close(parent)
            parent = child
            identities.append(_checked(os.fstat(parent), path, leaf=False))
        return _Chain(real, tuple(identities), ())
    finally:
        os.close(parent)


def _path_reads(real: Path) -> _Chain:
    """Read each component by path where descriptor-relative opens are unavailable."""
    retain_location_advisories(("file_locations: this platform cannot open a path component under its parent's descriptor; components were read one at a time by path.",))
    names = real.parts[1:]
    identities: list[tuple[int, int]] = []
    for depth in range(len(names) + 1):
        path = Path(real.anchor).joinpath(*names[:depth])
        try:
            information = os.lstat(path)
        except FileNotFoundError:
            return _Chain(real, tuple(identities), names[depth - 1 :])
        except OSError as exc:
            raise _IdentityUnavailableError(f"Cannot establish filesystem identity for {path}: {exc}.") from exc
        identities.append(_checked(information, path, leaf=depth == len(names)))
    return _Chain(real, tuple(identities), ())


def _walk(real: Path) -> _Chain:
    """Open an already link-free spelling as one descriptor chain; a link found now is a change."""
    return _descriptor_walk(real) if _DESCRIPTOR_WALK else _path_reads(real)


def _resolved_chain(path: Path) -> _Chain:
    return _walk(Path(os.path.realpath(path)))


def _contains(parent: _Chain, child: _Chain) -> bool:
    if parent.missing:
        # An uncreated root grants only its exact pending components, anchored
        # to the same opened directory, never that whole existing directory.
        return parent.identities[-1] == child.identities[-1] and child.missing[: len(parent.missing)] == parent.missing
    return parent.identities[-1] in child.identities


def _admissible_chain(real: Path, tree: SoletTree) -> _Chain:
    """Walk a request whose links were resolved once, after its spelling is checked against the tree.

    The spelling check comes first and the request is not resolved again, so
    a link that appears during the walk is reported as a change.
    """
    message = f"Location {real} overlaps the solet's own tree; choose a directory outside repo_root and app_home."
    if overlaps_solet_tree(real, tree):
        raise ValueError(message)
    protected = tuple(_resolved_chain(own) for own in (tree.repo_root, tree.app_home))
    candidate = _walk(real)
    for own in protected:
        if _contains(own, candidate) or _contains(candidate, own):
            raise ValueError(message)
    return candidate


def _open_once(path: Path) -> int | None:
    """Open the whole spelling in one resolution that refuses a link at any component; None when absent."""
    if _NO_FOLLOW_ANY is None or not _DESCRIPTOR_WALK:
        raise _SingleResolutionUnavailableError("this platform has no single no-follow path resolution")
    try:
        return os.open(path, os.O_RDONLY | os.O_NONBLOCK | _CLOEXEC | _NO_FOLLOW_ANY)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise _IdentityChangedError(f"Path {path} no longer resolves without links before return.") from exc
        raise _SingleResolutionUnavailableError(f"{path} cannot be opened for one no-follow resolution ({exc.strerror})") from exc


def _read_without_links(path: Path) -> no_link_stat.NoLinkStat | None:
    """Read the whole spelling in one call that refuses a link at any component; None when absent."""
    try:
        return no_link_stat.stat_without_links(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise _IdentityChangedError(f"Path {path} no longer resolves without links before return.") from exc
        raise _SingleResolutionUnavailableError(f"{path} cannot be read in one no-link resolution ({exc.strerror})") from exc


def _confirm_by_identity(chain: _Chain) -> None:
    """Read the deepest existing component's identity and links in one call, then a pending name's absence."""
    deepest = _read_without_links(chain.deepest)
    if deepest is None or stat.S_ISLNK(deepest.st_mode) or (deepest.st_dev, deepest.st_ino) != chain.identities[-1]:
        raise _IdentityChangedError(f"Path {chain.deepest} names a different object than the walk opened.")
    if stat.S_ISREG(deepest.st_mode) and deepest.st_nlink > 1:
        raise _IdentityUnavailableError(f"File {chain.deepest} gained a hard link; a unique containing directory cannot be proven.")
    if chain.missing and _read_without_links(chain.deepest / chain.missing[0]) is not None:
        raise _IdentityChangedError(f"Pending component {chain.deepest / chain.missing[0]} appeared before return.")


def _confirm_by_open(chain: _Chain) -> None:
    deepest = _open_once(chain.deepest)
    if deepest is None:
        raise _IdentityChangedError(f"Path {chain.deepest} disappeared before return.")
    try:
        information = os.fstat(deepest)
    finally:
        os.close(deepest)
    if (information.st_dev, information.st_ino) != chain.identities[-1]:
        raise _IdentityChangedError(f"Path {chain.deepest} names a different object than the walk opened.")
    final = chain.deepest / chain.missing[0] if chain.missing else chain.deepest
    descriptor = _open_once(final)
    if descriptor is not None:
        os.close(descriptor)
    if (descriptor is None) != bool(chain.missing):
        raise _IdentityChangedError(f"Path {final} {'appeared' if chain.missing else 'disappeared'} before return.")


def _recheck_components(chain: _Chain) -> None:
    """Read each existing component of the returned spelling by path, top-down, after the repeated walk.

    Without a one-call no-link read, a component changed after the walk read
    it is seen only by a later path read of it. The last read here is the
    existing target itself (for a pending target, its deepest existing
    ancestor): not a link, the object the walk opened, and a regular file
    still with one link, so a replaced, moved or newly hard-linked target
    refuses. An ancestor relocated with a link left at its name between its
    own read and the last read stays possible on such platforms, as their
    advisory says; this is the fallback residual in _confirm_chain.
    """
    last = len(chain.identities) - 1
    for depth, identity in enumerate(chain.identities):
        path = Path(chain.path.anchor).joinpath(*chain.path.parts[1 : depth + 1])
        try:
            information = os.lstat(path)
        except OSError as exc:
            raise _IdentityChangedError(f"Component {path} became unavailable before return.") from exc
        if stat.S_ISLNK(information.st_mode) or (information.st_dev, information.st_ino) != identity:
            raise _IdentityChangedError(f"Component {path} changed before return.")
        if depth == last and not chain.missing and stat.S_ISREG(information.st_mode) and information.st_nlink > 1:
            raise _IdentityUnavailableError(f"File {path} gained a hard link; a unique containing directory cannot be proven.")


def _confirm_chain(chain: _Chain) -> None:
    """Require the returned spelling to name the opened chain at the guard's last reads.

    Pass condition: the deepest existing component still resolves, with no
    link at any component, to the identity the walk opened (and a regular file
    still has one link); then, for a pending target, the last read finds the
    first pending component still absent through a spelling with no link. The
    guard runs this on every admitted path. Pass test:
    AncestryChangeSmoke.test_unchanged_ancestry_returns_existing_and_missing_targets.

    The containment decision reads only the descriptor chain, so moving a
    directory after the walk opened it cannot change what the walk measured.
    The guard returns a path string, though, and the descriptors cannot see a
    later link, move or replacement; only a whole-path read can. On macOS the
    last read is one fstatat with AT_SYMLINK_NOFOLLOW_ANY (no_link_stat), so a
    single instant answers both "no link anywhere" and "the walked object".
    Without it the guard uses its whole-path no-follow open, and without that
    it repeats the walk and rechecks each component by path, reading an
    existing target last; an advisory says so. For a pending target the
    deepest identity and the pending name's absence are two reads. A change
    after return is the follow-up fd-bound handle contract. The sweep in
    containment_sweep_support changes each element before each metadata call
    and checks the tree at return.

    Fallback residual: when the final check reads components one at a time,
    an ancestor relocated with a link left at its old name between its own
    path read and the final read is not seen, so the returned spelling can
    resolve through that link. Path reads made one at a time cannot cover
    every component at one instant. The one-call read closes this on macOS;
    the advisory names every run that took the fallback.
    """
    try:
        if no_link_stat.AVAILABLE and _NO_FOLLOW_ANY is not None and _DESCRIPTOR_WALK:
            _confirm_by_identity(chain)
        else:
            _confirm_by_open(chain)
    except _SingleResolutionUnavailableError as exc:
        retain_location_advisories((f"file_locations: {exc}; the final check walked path components one at a time.",))
        if _walk(chain.path) != chain:
            raise _IdentityChangedError(f"Path {chain.path} changed its components before return.") from exc
        _recheck_components(chain)


def derive_allowed_roots(addresses: Sequence[PathAddress], launch_directory: str | None, tree: SoletTree, solet_home: Path) -> Located[tuple[Path, ...]]:
    """Use all live addresses, a safe launch directory, and missing home defaults.

    Legacy parent addresses may overlap the tree: the destination guard still
    protects every write beneath them. Malformed inputs are advisory only.
    """
    advisories: list[str] = []
    roots: list[Path] = []
    by_name: dict[str, Path] = {}
    for address in addresses:
        if not address.directory.is_absolute():
            advisories.append(f"Skipping malformed path address {address.name!r}: directory must be absolute.")
            continue
        directory = address.directory.resolve()
        roots.append(directory)
        by_name[address.name] = directory
    if launch_directory:
        launch = Path(launch_directory)
        if launch.is_absolute() and launch.is_dir() and not overlaps_solet_tree(launch, tree):
            roots.append(launch.resolve())
        else:
            advisories.append(f"Launch directory {launch} is unavailable or overlaps the solet tree; using path addresses and solet_home.")
    else:
        advisories.append("No launch directory capture is available; using path addresses and solet_home. Refresh the launcher and launch from your project folder.")
    home = by_name.get("solet_home", solet_home.resolve())
    if overlaps_solet_tree(home, tree):
        advisories.append(f"Registered solet_home {home} overlaps the tree; using computed home {solet_home}.")
        home = solet_home.resolve()
    for name, directory in (("solet_home", home), ("solet_home_exports", home / "Exports.nosync")):
        if name not in by_name:
            roots.append(directory)
            advisories.append(f"Path address {name!r} is missing; using computed folder {directory}. Register it as a path address to choose another folder.")
    return Located(tuple(dict.fromkeys(roots)), tuple(advisories))


def _validated_path(path: str, suffix: str | None, param_name: str, plugin_name: str) -> Path:
    if not path or "\x00" in path or not os.path.isabs(path):
        raise PathRefusedError(f"{plugin_name}: {param_name} must be an ABSOLUTE filesystem path without NUL bytes.")
    if suffix and not path.endswith(suffix):
        raise PathRefusedError(f"{plugin_name}: {param_name} must end in '{suffix}'.")
    return Path(os.path.realpath(path))


def assert_path_allowed(path: str, roots: Located[tuple[Path, ...]], tree: SoletTree, *, suffix: str | None, param_name: str, plugin_name: str) -> Located[str]:
    """Prove every connector I/O boundary with physical filesystem ancestry.

    Pass condition: register the folder as a path address or launch from it,
    outside repo_root and app_home, with ancestry unchanged through the
    immediate pre-return verification. Connectors run this on every file I/O;
    the unchanged-ancestry smoke exercises existing/new-target pass paths.
    """
    candidate = _validated_path(path, suffix, param_name, plugin_name)
    try:
        chain = _admissible_chain(candidate, tree)
        allowed = tuple(_resolved_chain(root) for root in roots.value if root.is_absolute())
        if any(_contains(root, chain) for root in allowed):
            _confirm_chain(chain)
            return Located(str(chain.path), roots.advisories)
    except _IdentityChangedError as exc:
        raise PathIdentityChangedError(f"{plugin_name}: {param_name}: file_locations.identity_changed: {exc}") from exc
    except _IdentityUnavailableError as exc:
        raise PathIdentityRefusedError(f"{plugin_name}: {param_name}: file_locations.identity_unavailable: {exc}") from exc
    except ValueError as exc:
        raise PathRefusedError(f"{plugin_name}: {param_name}: {exc}") from exc
    raise PathRefusedError(f"{plugin_name}: {param_name} {path!r} is outside every allowed root; register the folder as a path address or launch from it, outside the solet tree.")


@dataclass(slots=True)
class _LocationContext:
    session_id: str | None
    advisories: list[str] = field(default_factory=list)


_LOCATION_CONTEXT: ContextVar[_LocationContext | None] = ContextVar("file_location_context", default=None)


@contextmanager
def location_session(session_id: str | None) -> Generator[None]:
    """Bind one job's origin and reset it even when its worker raises.

    This is ephemeral execution context, never a database or caller parameter.
    Worker adapters supply the session already present in job metadata.
    """
    token = _LOCATION_CONTEXT.set(_LocationContext(session_id))
    try:
        yield
    finally:
        _LOCATION_CONTEXT.reset(token)


def location_session_id() -> str | None:
    context = _LOCATION_CONTEXT.get()
    return context.session_id if context is not None else None


def retain_location_advisories(advisories: Sequence[str]) -> None:
    context = _LOCATION_CONTEXT.get()
    if context is not None:
        context.advisories.extend(advisories)


def location_advisories() -> tuple[str, ...]:
    context = _LOCATION_CONTEXT.get()
    return tuple(dict.fromkeys(context.advisories)) if context is not None else ()


def job_location_session(result: object) -> str | None:
    """Decode the existing AsyncJobManager get_job result, without I/O."""
    if not isinstance(result, dict) or result.get("action_status") != "completed":
        return None
    data = result.get("data")
    job = data.get("job") if isinstance(data, dict) else None
    metadata = job.get("metadata") if isinstance(job, dict) else None
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (ValueError, TypeError):
            return None
    session = metadata.get("session_id") if isinstance(metadata, dict) else None
    return session if isinstance(session, str) and session else None
