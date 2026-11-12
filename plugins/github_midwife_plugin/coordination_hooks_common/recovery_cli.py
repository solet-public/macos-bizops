"""Recovery cli: canonical stdlib Python 3.11 recovery component.

Part of one shared engine; working_tree_recovery is its public facade.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import custody_store as store
from command_walk import Discard
from recovery_capture import save
from recovery_lifecycle import correct, forget, purge, unforget
from recovery_metadata import prepare_root
from recovery_packets import Recovery
from recovery_restore import restore
from recovery_retention import delivered, prune
from recovery_types import HookEvent, RecoveryError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    subparsers = parser.add_subparsers(dest="verb", required=True)
    save_parser = subparsers.add_parser("save")
    save_parser.add_argument("--folder", type=Path, default=Path.cwd())
    save_parser.add_argument("--trigger", choices=("manual", "retire"), default="manual")
    listing = subparsers.add_parser("list")
    listing.add_argument("--folder", type=Path)
    listing.add_argument("--undelivered", action="store_true")
    show = subparsers.add_parser("show")
    show.add_argument("save_id")
    correction = subparsers.add_parser("correct")
    correction.add_argument("save_id")
    correction.add_argument("--note", required=True)
    for name, expected in (("forget", "present"), ("unforget", "trashed")):
        command = subparsers.add_parser(name)
        command.add_argument("save_id")
        command.add_argument("--expected-state", required=True, choices=("present", "trashed", "absent"), help=f"show must currently report {expected}")
    pruning = subparsers.add_parser("prune")
    apply = pruning.add_mutually_exclusive_group(required=True)
    apply.add_argument("--dry-run", action="store_true")
    apply.add_argument("--apply", action="store_true")
    purging = subparsers.add_parser("purge")
    purging.add_argument("save_id", nargs="?")
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("save_id")
    restore_parser.add_argument("--into", type=Path)
    restore_parser.add_argument("--stage", action="store_true")
    restore_parser.add_argument("--refs", action="store_true")
    return parser


def _listing(recovery: Recovery, arguments: argparse.Namespace) -> list[dict[str, object]]:
    deadline = time.monotonic() + 60.0
    result: list[dict[str, object]] = []
    for parts, manifest, state in recovery.packets(include_trash=True):
        if arguments.folder is not None and Path(manifest["binding"]["folder"]) != arguments.folder.resolve():
            continue
        if arguments.undelivered and delivered(recovery, manifest, deadline):
            continue
        result.append({"save_id": parts[-1], "state": state, "folder": manifest["binding"]["folder"], "trigger": manifest["trigger"]})
    return result


def _dispatch(recovery: Recovery, arguments: argparse.Namespace, advisory: tuple[str, ...]) -> object:
    result: object
    if arguments.verb == "save":
        event = HookEvent(event="manual", session_id="manual", cwd=arguments.folder, tool="cli")
        save_id, _manifest = save(recovery, event, Discard(arguments.folder, ("tree",)), arguments.trigger, time.monotonic() + 60.0)
        result = {"save_id": save_id, "advisories": advisory}
    elif arguments.verb == "list":
        result = _listing(recovery, arguments)
    elif arguments.verb == "show":
        result = recovery.show(arguments.save_id)
    elif arguments.verb == "correct":
        result = {"save_id": correct(recovery, arguments.save_id, arguments.note)}
    elif arguments.verb in {"forget", "unforget"}:
        operation = forget if arguments.verb == "forget" else unforget
        result = {"save_id": operation(recovery, arguments.save_id, arguments.expected_state)}
    elif arguments.verb == "prune":
        result = prune(recovery, apply=arguments.apply)
    elif arguments.verb == "purge":
        result = {"purged": purge(recovery, arguments.save_id)}
    else:
        result = restore(recovery, arguments.save_id, into=arguments.into, stage=arguments.stage, refs=arguments.refs)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Run the explicit user CLI; hook output remains runner neutral."""
    arguments = _parser().parse_args(argv)
    try:
        root, advisory = prepare_root(arguments.root, Path.cwd(), time.monotonic() + 60.0, read_only=arguments.verb in {"list", "show"})
        result = _dispatch(Recovery(root), arguments, advisory)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, RecoveryError, store.CustodyStoreError) as exc:
        print(f"[recovery] {exc}", file=sys.stderr)
        return 1
