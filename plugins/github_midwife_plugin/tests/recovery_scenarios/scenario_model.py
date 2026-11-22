"""Scenario model: constants, errors, hooks.json registrations and the scratch world of one scenario variant."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol, cast

Json = dict[str, Any]

HERE: Final[Path] = Path(__file__).resolve().parent
PLUGIN_DIR: Final[Path] = HERE.parents[1]
TOOLS: Final[tuple[str, ...]] = ("claude", "codex")
DEFAULT_ROOTS: Final[dict[str, Path]] = {"claude": PLUGIN_DIR / "claude_plugin" / "coordination-hooks", "codex": PLUGIN_DIR / "codex_plugin" / "coordination-hooks"}
ADAPTERS: Final[dict[str, str]] = {"claude": "recovery_claude.py", "codex": "recovery_codex.py"}
ROOT_PLACEHOLDER: Final[dict[str, str]] = {"claude": "${CLAUDE_PLUGIN_ROOT}", "codex": "${PLUGIN_ROOT}"}
FORBIDDEN_KEYS: Final[frozenset[str]] = frozenset({"decision", "permissionDecision", "continue", "stopReason"})
ALLOWED_TOP: Final[frozenset[str]] = frozenset({"systemMessage", "hookSpecificOutput"})
ALLOWED_SPECIFIC: Final[frozenset[str]] = frozenset({"hookEventName", "additionalContext"})
SILENT_EVENTS: Final[frozenset[str]] = frozenset({"PostToolUse", "Stop", "SessionEnd"})
TOOL_EVENTS: Final[frozenset[str]] = frozenset({"PreToolUse", "PostToolUse"})
OLD_PYTHONS: Final[tuple[str, ...]] = ("python3.10", "python3.9", "python3.8", "/usr/bin/python3")
STOP_AND_TELL: Final[str] = "If the user did not mean to discard these changes"
ADVISORY_LINE: Final[str] = "ADVISORY: recovery adapters not installed in this tree; 0 of {cells} cells run"
VARIABLE: Final[re.Pattern[str]] = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
DIRECTORY_NAME: Final[re.Pattern[str]] = re.compile(r"[A-Z][A-Z0-9]*")
SYSTEM_PROBE_COMMANDS: Final[tuple[tuple[str, ...], ...]] = (("uptime",), ("sysctl", "vm.swapusage"), ("ps", "-axo", "command"))
# Heavy host work the live run must not compete with, matched against a process's invocation (its first words) so that a resident
# service, or a lane whose prompt merely mentions a landing, is not counted: a check-in, a smoke battery, a landing run, a restore,
# a private database being provisioned, or a deployment swap or cutover script.
ADMISSION_PROCESS: Final[re.Pattern[str]] = re.compile(r"scoped_checkin\.py|run_smokes\.py|\blanding run\b|\bpg_restore\b|provision_test_pg|deployment/scripts/\S*(?:swap|cutover|deploy)\S*")
ADMISSION_INVOCATION_WORDS: Final[int] = 6
ADMISSION_LOAD: Final[float] = 16.0
ADMISSION_SWAP_GB: Final[float] = 1.0
TURN_CAP: Final[int] = 40


class CellError(Exception):
    """A measured difference between the expected and the observed outcome."""


class UnmeasuredError(Exception):
    """A condition the host cannot produce; reported with its reason, never a FAIL."""


class LiveStopError(Exception):
    """A live-stage stop condition of the plan: report it, do not work around it."""


@dataclass
class HookRun:
    """What the recovery hook commands of one event produced."""

    event: str
    registered: bool
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    seconds: float = 0.0
    timed_out: bool = False


@dataclass(frozen=True)
class Registration:
    """One recovery entry of a plugin's hooks.json, with its command line fully expanded."""

    event: str
    matcher: str
    argv: tuple[str, ...]
    timeout: float


PAYLOAD_NO_RECORD: Final[str] = "n/a (payload mode feeds the hook commands directly; no tool ran)"


@dataclass
class CellResult:
    """One cell's outcome. `hook_log` and `reply_artifact` name the raw record the cell kept: the file of every hook call the tool made
    (payload in, output out) and the file of everything the model and the tool said. A live cell always names both, even when the file
    records that no turn ran; a payload cell names neither because no tool ran."""

    status: str
    seconds: float
    notes: list[str] = field(default_factory=lambda: cast("list[str]", []))
    failures: list[str] = field(default_factory=lambda: cast("list[str]", []))
    hook_log: str = PAYLOAD_NO_RECORD
    reply_artifact: str = PAYLOAD_NO_RECORD


@dataclass(frozen=True)
class Options:
    roots: dict[str, Path]
    bulk_files: int | None
    explicit: frozenset[str]


# --------------------------------------------------------------------------------------
# hooks.json: the recovery entries exactly as each tool's manifest declares them
# --------------------------------------------------------------------------------------


def adapter_present(tool: str, root: Path) -> bool:
    return (root / "hooks" / ADAPTERS[tool]).is_file() and (root / "hooks" / "hooks.json").is_file()


def _entry_argv(tool: str, root: Path, hook: Json) -> tuple[str, ...]:
    command = str(hook.get("command", ""))
    if tool == "claude":
        pieces = [command, *[str(item) for item in cast("list[Any]", hook.get("args", []))]]
        return tuple(piece.replace(ROOT_PLACEHOLDER[tool], str(root)) for piece in pieces)
    return tuple(shlex.split(command.replace(ROOT_PLACEHOLDER[tool], shlex.quote(str(root)))))


def recovery_registrations(tool: str, root: Path) -> list[Registration]:
    path = root / "hooks" / "hooks.json"
    if not path.is_file():
        return []
    registrations: list[Registration] = []
    declared = cast(Json, json.loads(path.read_text(encoding="utf-8"))).get("hooks", {})
    for event, groups in cast(Json, declared).items():
        for group in cast("list[Json]", groups):
            for hook in cast("list[Json]", group.get("hooks", [])):
                argv = _entry_argv(tool, root, hook)
                if any(ADAPTERS[tool] in token for token in argv):
                    registrations.append(Registration(event, str(group.get("matcher", "")), argv, float(hook.get("timeout", 60))))
    return registrations


def registration_matches(registration: Registration, key: str) -> bool:
    return not registration.matcher or re.fullmatch(registration.matcher, key) is not None


def short_path(path: Path) -> str:
    """A path as its last four components, so a failure line stays readable."""
    return ".../" + "/".join(path.parts[-4:])


def _inside_repository(path: Path) -> bool:
    probe = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path, capture_output=True, text=True, check=False)
    return probe.returncode == 0


def _make_base() -> Path:
    for parent in (None, "/tmp"):  # noqa: S108 -- a fallback outside any repository when TMPDIR sits inside one
        base = Path(tempfile.mkdtemp(prefix="recovery-scenarios-", dir=parent)).resolve()
        if not _inside_repository(base):
            return base
        shutil.rmtree(base, ignore_errors=True)
    raise UnmeasuredError("no scratch location outside a git repository (the store would be placed in the default root)")


class Env:
    """The scratch repositories, the store and the variables of one scenario variant."""

    def __init__(self, scenario: Json, tool: str, root: Path, options: Options, tmp_case: str, base: Path | None = None) -> None:
        self.scenario, self.tool, self.root, self.options = scenario, tool, root, options
        self.base = base or _make_base()
        self.home, self.store = self.base / "home", self.base / "store"
        self.driver: Driver
        self.home.mkdir()
        self.tmp = self._tmp_dir(tmp_case)
        self.registrations = recovery_registrations(tool, root)
        self.variables: dict[str, str] = {}
        self.known: set[str] = set()
        self.applied: set[str] = set()
        self.notes: list[str] = []
        self.search_counts: list[int] = []
        self.last_dir: dict[str, Path] = {}
        self.hook_runs = 0
        self.counter = 0
        self.scripts = self.base / "scripts"
        bulk = scenario.get("bulk_files")
        if bulk is not None:
            self.variables["bulk_files"] = str(options.bulk_files if options.bulk_files is not None else bulk)

    def _tmp_dir(self, tmp_case: str) -> Path:
        if tmp_case == "inside_store":
            (self.store / "tmp").mkdir(parents=True, mode=0o700)
            return self.store / "tmp"
        if tmp_case == "symlink_into_store":
            (self.store / "tmp").mkdir(parents=True, mode=0o700)
            link = self.base / "tmp-link"
            link.symlink_to(self.store / "tmp")
            return link
        outside = self.base / "tmp-outside"
        outside.mkdir()
        return outside

    def dir_path(self, name: str) -> Path:
        return self.base / name

    def resolve_variable(self, name: str) -> str:
        if name in self.variables:
            return self.variables[name]
        if DIRECTORY_NAME.fullmatch(name):
            return str(self.dir_path(name))
        raise CellError(f"scenario refers to an unknown variable ${{{name}}}")

    def expand(self, value: Any) -> Any:
        if isinstance(value, str):
            return VARIABLE.sub(lambda match: self.resolve_variable(match.group(1)), value)
        if isinstance(value, list):
            return [self.expand(item) for item in cast("list[Any]", value)]
        if isinstance(value, dict):
            return {key: self.expand(item) for key, item in cast(Json, value).items()}
        return value

    def shell_env(self) -> dict[str, str]:
        identity = {"GIT_AUTHOR_NAME": "Scenario", "GIT_AUTHOR_EMAIL": "scenario@example.invalid", "GIT_COMMITTER_NAME": "Scenario", "GIT_COMMITTER_EMAIL": "scenario@example.invalid"}
        return {"PATH": os.environ.get("PATH", ""), "HOME": str(self.home), "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C", **identity}

    def hook_env(self, fault: str) -> dict[str, str]:
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.home), "SOLET_RECOVERY_ROOT": str(self.store), "TMPDIR": str(self.tmp), "PYTHONDONTWRITEBYTECODE": "1"}
        if fault == "git_missing":
            env["PATH"] = str(self._interpreter_directory(Path(os.path.realpath(sys.executable)), ""))
        elif fault == "python_below_3_11":
            env["PATH"] = str(self._interpreter_directory(self._old_python(), os.environ.get("PATH", "")))
        elif fault == "readonly_store":
            env["SOLET_RECOVERY_ROOT"] = str(self._read_only_store())
        elif fault:
            raise CellError(f"scenario names an unknown fault {fault!r}")
        return env

    def _interpreter_directory(self, interpreter: Path, rest: str) -> str:
        directory = self.base / f"python-{abs(hash(str(interpreter) + rest))}"
        if not directory.exists():
            directory.mkdir()
            (directory / "python3").symlink_to(interpreter)
        return str(directory) + (os.pathsep + rest if rest else "")

    @staticmethod
    def _old_python() -> Path:
        for candidate in OLD_PYTHONS:
            found = shutil.which(candidate)
            if found is None:
                continue
            probe = subprocess.run([found, "-c", "import sys; print(sys.version_info >= (3, 11))"], capture_output=True, text=True, check=False)
            if probe.returncode == 0 and probe.stdout.strip() == "False":
                return Path(os.path.realpath(found))
        raise UnmeasuredError("no interpreter below 3.11 found (tried " + ", ".join(OLD_PYTHONS) + ")")

    def _read_only_store(self) -> Path:
        if os.geteuid() == 0:
            raise UnmeasuredError("running as root: a read-only directory does not stop root, so the fault cannot be produced")
        parent = self.base / "read-only"
        parent.mkdir(exist_ok=True)
        parent.chmod(0o500)
        return parent / "store"

    def run_cli(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        script = self.root / "hooks" / "working_tree_recovery.py"
        if not script.is_file():
            raise CellError(f"the recovery CLI is not installed: {short_path(script)} is missing")
        return subprocess.run([sys.executable, "-B", str(script), "--root", str(self.store), *args], cwd=self.base, env=self.hook_env(""), capture_output=True, text=True, timeout=300, check=False)

    def list_saves(self) -> dict[str, Json]:
        if not self.store.exists():
            return {}
        result = self.run_cli(["list"])
        if result.returncode != 0:
            raise CellError(f"recovery_cli list exited {result.returncode}: {result.stderr.strip()[:300]}")
        return {str(row["save_id"]): row for row in cast("list[Json]", json.loads(result.stdout))}

    def new_saves(self, trigger: str = "") -> list[str]:
        """The saves made since the last call. Every one is absorbed; `trigger` only selects which are returned."""
        saves = self.list_saves()
        new = sorted(set(saves) - self.known)
        self.known |= set(saves)
        return [save_id for save_id in new if not trigger or self.manifest(save_id)["trigger"] == trigger]

    def manifest(self, save_id: str) -> Json:
        result = self.run_cli(["show", save_id])
        if result.returncode != 0:
            raise CellError(f"recovery_cli show {save_id[:8]} exited {result.returncode}: {result.stderr.strip()[:300]}")
        return cast(Json, cast(Json, json.loads(result.stdout))["manifest"])

    def cleanup(self) -> None:
        for path in self.base.glob("read-only"):
            path.chmod(0o700)
        shutil.rmtree(self.base, ignore_errors=True)


class Driver(Protocol):
    """What a scenario step needs from a mode: make the actor act, and report what the hook said and which saves appeared."""

    auto_turn_end: bool

    def edit(self, op: Json) -> tuple[HookRun, list[str]]: ...

    def shell(self, op: Json, command: str) -> tuple[HookRun, list[str]]: ...

    def lifecycle(self, op: Json, event: str) -> tuple[HookRun, list[str]]: ...

    def raw(self, op: Json) -> tuple[HookRun, list[str]]: ...

    def install(self, directory: Path) -> None: ...
