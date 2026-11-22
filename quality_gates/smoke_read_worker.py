"""Execute a Python smoke once and atomically write its read observations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pkgutil
import runpy
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ananta.work_folders import process_binding, register_work_folder

from quality_gates.smoke_reads import TraceRecorder


@contextmanager
def _scratch(prefix: str, parent: Path | None = None) -> Generator[Path]:
    """Give disposable worker state process-bound work-folder custody."""
    path = (parent or Path(tempfile.gettempdir())) / (prefix + uuid.uuid4().hex)
    path.mkdir(mode=0o700)
    folder = register_work_folder(path, purpose="smoke_read_worker", bound=process_binding())
    try:
        yield path
    finally:
        folder.close()


def _write_trace(path: Path, trace: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".reads-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(trace, stream, sort_keys=True, separators=(",", ":"), allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """Keep the original script's arguments, exit, cwd and output semantics."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--trace-out", type=Path, required=True)
    parser.add_argument("--profile-sha256", required=True)
    parser.add_argument("--_once", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command: list[str] = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("expected SMOKE [args...] after --")
    if len(args.profile_sha256) != 64 or any(char not in "0123456789abcdef" for char in args.profile_sha256):
        parser.error("--profile-sha256 requires 64 lowercase hex characters")
    args.command = command
    return args


def _record_once(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    script = Path(args.command[0]).absolute()
    relative = script.relative_to(root).as_posix()
    # Resolve runpy's lazy dependency before installing the trace.
    _ = pkgutil.get_importer(str(script))
    sys.argv = args.command
    sys.path[0] = str(script.parent)
    load_code = runpy.__dict__["_get_code_from_file"]
    temp_module = runpy.__dict__["_TempModule"]
    run_code = runpy.__dict__["_run_code"]
    with _scratch("recorded-smoke-") as private:
        recorder = TraceRecorder(root, relative, args.profile_sha256, private)
        try:
            with recorder:
                # run_path rewrites argv[0] to its filename. The file launcher
                # instead keeps the caller's argv and exposes an absolute
                # __file__; use the same runpy loader/executor with that argv.
                code = load_code(str(script))
                with temp_module("__main__") as temporary:
                    run_code(code, temporary.module.__dict__, mod_name="__main__", script_name=str(script))
        finally:
            try:
                _write_trace(args.trace_out, recorder.seal().to_dict())
            except (OSError, ValueError, TypeError) as exc:
                print(f"ADVISORY smoke read trace unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)


def _worker_command(args: argparse.Namespace, root: Path, output: Path, command: list[str]) -> list[str]:
    return [sys.executable, "-m", "quality_gates.smoke_read_worker", "--_once", "--root", str(root), "--trace-out", str(output), "--profile-sha256", args.profile_sha256, "--", *command]


def _observations(trace: dict[str, Any]) -> dict[str, Any]:
    """Compare the measured inputs, omitting the deliberately distinct root."""
    excluded = {"trace_sha256", "attribution", "complete", "incomplete_reasons"}
    value = {key: item for key, item in trace.items() if key not in excluded}
    value["runtime_inputs"] = [row for row in trace["runtime_inputs"] if row["path"] not in ("@repository_root", "@root_probe")]
    return value


def _copy_input(root: Path, destination: Path, relative: str) -> None:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("unsafe probe input")
    source = root / path
    target = destination / path
    if target.exists() or target.is_symlink():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        target.symlink_to(os.readlink(source))
    elif source.is_dir():
        target.mkdir(exist_ok=True)
    else:
        shutil.copy2(source, target)


def _copy_probe(root: Path, destination: Path, trace: dict[str, Any]) -> None:
    """Copy the closure's bytes and directory membership in recorded order."""
    for row in trace["directories"]:
        if "result" not in row:
            continue
        _copy_input(root, destination, row["path"])
        for name in row["result"]:
            _copy_input(root, destination, str(Path(row["path"]) / name))
    for row in trace["metadata"]:
        if row["operation"] == "readlink":
            _copy_input(root, destination, row["path"])
    for row in trace["reads"]:
        _copy_input(root, destination, row["path"])
    # Bootstrap after membership copying to preserve observed creation order.
    # A real tree supplies these same recorder bytes; generated fixture roots
    # retain their original caller-provided module search path instead.
    for name in ("__init__.py", "smoke_reads.py", "smoke_read_hooks.py", "smoke_read_worker.py"):
        relative = "quality_gates/" + name
        if (root / relative).is_file():
            _copy_input(root, destination, relative)


def _probe(args: argparse.Namespace, trace: dict[str, Any], original: subprocess.CompletedProcess[bytes]) -> str:
    root = args.root.resolve()
    destination = args.trace_out.parent.resolve() / ".smoke-root-probe"
    if destination.is_relative_to(root):
        return "unavailable"
    # Exclusive creation makes cleanup authority local to this invocation.
    try:
        destination.mkdir(mode=0o700)
    except OSError:
        return "unavailable"
    try:
        _copy_probe(root, destination, trace)
        return _run_probe(args, trace, original, root, destination)
    except (OSError, ValueError, KeyError, TypeError):
        return "unavailable"
    finally:
        shutil.rmtree(destination)


def _run_probe(args: argparse.Namespace, trace: dict[str, Any], original: subprocess.CompletedProcess[bytes], root: Path, destination: Path) -> str:
    cwd = Path.cwd().resolve()
    if not cwd.is_relative_to(root):
        return "unavailable"
    probe_cwd = destination / cwd.relative_to(root)
    probe_cwd.mkdir(parents=True, exist_ok=True)
    command = list(args.command)
    if Path(command[0]).is_absolute():
        command[0] = str(destination / trace["smoke"])
    with _scratch(".smoke-probe-state-", args.trace_out.parent) as private:
        sidecar = private / "diagnostic.json"
        result = subprocess.run(_worker_command(args, destination, sidecar, command), cwd=probe_cwd, capture_output=True, check=False)
        if not sidecar.is_file():
            return "unavailable"
        observed = json.loads(sidecar.read_text())
        same_result = (result.returncode, result.stdout, result.stderr) == (original.returncode, original.stdout, original.stderr)
        same_inputs = observed.get("complete") is True and _observations(observed) == _observations(trace)
        return "matched" if same_result and same_inputs else "diverged"


def _certify(args: argparse.Namespace, trace: dict[str, Any], original: subprocess.CompletedProcess[bytes]) -> dict[str, Any]:
    if trace.get("complete") is True:
        status = _probe(args, trace, original)
        trace["runtime_inputs"].append({"path": "@root_probe", "status": status})
        trace["runtime_inputs"].sort(key=lambda row: json.dumps(row, sort_keys=True))
        if status != "matched":
            trace["complete"] = False
            trace["incomplete_reasons"].append("root_sensitive" if status == "diverged" else "root_probe_unavailable")
        identity = {key: value for key, value in trace.items() if key not in {"trace_sha256", "attribution", "complete", "incomplete_reasons"}}
        trace["trace_sha256"] = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return trace


def main(argv: list[str] | None = None) -> None:
    """Publish the production result, then one root-bound diagnostic receipt."""
    args = _arguments(argv)
    if args._once:
        _record_once(args)
        return
    args.trace_out = args.trace_out.absolute()
    try:
        args.trace_out.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # Production runs; the sidecar failure is disclosed after its result.
    with _scratch("smoke-production-") as private:
        sidecar = private / "production.json"
        original = subprocess.run(_worker_command(args, args.root, sidecar, args.command), capture_output=True, check=False)
        sys.stdout.buffer.write(original.stdout)
        sys.stderr.buffer.write(original.stderr)
        if sidecar.is_file():
            try:
                trace = _certify(args, json.loads(sidecar.read_text()), original)
                _write_trace(args.trace_out, trace)
            except (OSError, ValueError, TypeError) as exc:
                print(f"ADVISORY smoke read trace unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        if original.returncode < 0:
            os.kill(os.getpid(), -original.returncode)
        raise SystemExit(original.returncode)


if __name__ == "__main__":
    main()
