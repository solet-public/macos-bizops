"""Documented sf CLI commands and their flags, checked before the CLI runs.

Each ``CliCommand`` is one ``sf`` command with every flag the CLI's command
manifest documents (``sf commands --json``, ``@salesforce/cli`` 2.142.7),
except the global ``--json``, ``--flags-dir`` and ``--loglevel``. A flag is
either one the caller passes in the verb's ``flags`` object, by the CLI's own
kebab-case name and with the CLI's own value type and choices, or one the
plugin sets itself, listed in ``managed`` with the reason. Caller flags are
not reinterpreted: the CLI receives them unchanged, and the vendor decides
everything the manifest does not state (ranges, exclusive pairs, permissions).

Values are passed as ``--name=value`` so that a value beginning with ``-`` can
never be read as another flag; a boolean is ``--name`` when true and, for a
flag the CLI lets negate, ``--no-name`` when false. A file flag is admitted
through the plugin's read containment (registered path addresses) at the
moment the CLI runs.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from .errors import TypedFamilyError

FlagKind = Literal["text", "boolean", "integer", "choice", "file"]
ReadGate = Callable[[str], str]


@dataclass(frozen=True)
class Flag:
    kind: FlagKind
    options: tuple[str, ...] = ()
    required: bool = False
    # oclif ``allowNo``: false is sent as --no-<name>.
    negatable: bool = False


TEXT = Flag("text")
BOOLEAN = Flag("boolean")
INTEGER = Flag("integer")
FILE = Flag("file")


def choice(*options: str) -> Flag:
    return Flag("choice", options=options)


@dataclass(frozen=True)
class CliCommand:
    words: tuple[str, ...]
    flags: Mapping[str, Flag] = field(default_factory=dict)
    managed: Mapping[str, str] = field(default_factory=dict)

    def label(self) -> str:
        return "sf " + " ".join(self.words)

    def documented(self) -> frozenset[str]:
        return frozenset(self.flags) | frozenset(self.managed)


def flags_object(given: object) -> dict[str, object]:
    if given is None:
        return {}
    if not isinstance(given, dict):
        raise TypedFamilyError("flags must be an object of sf CLI flag names (without the leading --) and their values")
    return {str(name): value for name, value in given.items()}


def check_flags(command: CliCommand, given: object, *, also_required: tuple[str, ...] = ()) -> dict[str, object]:
    """Names, required flags and value types, as documented; no I/O."""
    flags = flags_object(given)
    _refuse_names(command, flags)
    missing = sorted(name for name, flag in command.flags.items() if (flag.required or name in also_required) and name not in flags)
    if missing:
        raise TypedFamilyError(f"{command.label()} requires flags {missing}")
    for name, value in flags.items():
        _check_value(name, command.flags[name], value)
    return flags


def _refuse_names(command: CliCommand, flags: dict[str, object]) -> None:
    """A plugin-set flag is refused with its reason; an undocumented one with the documented list."""
    managed = sorted(set(flags) & set(command.managed))
    if managed:
        raise TypedFamilyError("; ".join(f"--{name} is set by the plugin for {command.label()}: {command.managed[name]}" for name in managed))
    unknown = sorted(set(flags) - set(command.flags))
    if unknown:
        raise TypedFamilyError(f"flags {unknown} are not documented for {command.label()} (documented: {', '.join(sorted(command.flags)) or 'none'})")


def command_argv(command: CliCommand, given: object, *, read_gate: ReadGate, also_required: tuple[str, ...] = ()) -> list[str]:
    """The command words and the caller's flags as CLI arguments; file flags pass the read gate."""
    flags = check_flags(command, given, also_required=also_required)
    argv = list(command.words)
    for name, value in flags.items():
        argv.extend(_flag_argv(name, command.flags[name], value, read_gate))
    return argv


def _boolean_problem(value: object, _flag: Flag) -> str:
    return "" if isinstance(value, bool) else "true or false"


def _integer_problem(value: object, _flag: Flag) -> str:
    return "" if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else "a whole number"


def _text_problem(value: object, flag: Flag) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        return "a non-empty string"
    return f"one of: {', '.join(flag.options)}" if flag.kind == "choice" and value not in flag.options else ""


_VALUE_PROBLEMS: Mapping[FlagKind, Callable[[object, Flag], str]] = {"boolean": _boolean_problem, "integer": _integer_problem, "text": _text_problem, "choice": _text_problem, "file": _text_problem}


def _check_value(name: str, flag: Flag, value: object) -> None:
    problem = _VALUE_PROBLEMS[flag.kind](value, flag)
    if problem:
        raise TypedFamilyError(f"--{name} must be {problem}")


def _flag_argv(name: str, flag: Flag, value: object, read_gate: ReadGate) -> list[str]:
    if flag.kind == "boolean":
        if value is True:
            return [f"--{name}"]
        return [f"--no-{name}"] if flag.negatable else []
    if flag.kind == "file":
        return [f"--{name}={read_gate(str(value))}"]
    return [f"--{name}={value}"]
