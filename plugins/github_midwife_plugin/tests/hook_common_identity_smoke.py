"""Self-contained identity, stdlib and Python-floor proofs for common hooks."""

from __future__ import annotations

import ast
import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE = Path("plugins/github_midwife_plugin/coordination_hooks_common/custody_store.py")
CONSUMER = Path("plugins/agent_messaging_plugin/src/agent_messaging_plugin/custody_store.py")
TOOL = Path("plugins/github_midwife_plugin/tools/materialize_hook_common.py")
COMMON = Path("plugins/github_midwife_plugin/coordination_hooks_common")


def _modules() -> tuple[str, ...]:
    tree = ast.parse((REPO_ROOT / COMMON / "working_tree_recovery.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "MODULES" for target in node.targets):
            value = ast.literal_eval(node.value)
            assert isinstance(value, tuple) and all(isinstance(name, str) for name in value)
            return value
    raise AssertionError("engine MODULES inventory missing")


def _tool(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(root / TOOL), *args], capture_output=True, text=True, check=False)


def _check_bulk(root: Path, modules: tuple[str, ...], destination: Path) -> None:
    written = _tool(root, "--write", "--dest", str(destination))
    assert written.returncode == 0 and destination.is_dir(), written
    for name in modules:
        source = root / COMMON / name
        copy = destination / name
        assert copy.read_bytes() == source.read_bytes(), name
        assert copy.stat().st_mode & 0o777 == source.stat().st_mode & 0o777, name
    clean = _tool(root, "--check", "--dest", str(destination))
    assert clean.returncode == 0 and "differs" not in clean.stdout and "missing" not in clean.stdout, clean
    assert clean.stdout.count("identical") == len(modules), clean.stdout


def _check_subset_and_reports(root: Path, modules: tuple[str, ...], destination: Path) -> None:
    subset = root / "subset"
    chosen = (modules[0], modules[-1])
    selected = _tool(root, "--write", "--dest", str(subset), "--module", chosen[0], "--module", chosen[1])
    assert selected.returncode == 0 and {path.name for path in subset.iterdir()} == set(chosen), selected

    mutation = destination / modules[0]
    original = mutation.read_bytes()
    mutation.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    changed = _tool(root, "--check", "--dest", str(destination), "--module", modules[0])
    assert changed.returncode == 0 and modules[0] in changed.stdout and ("---" in changed.stdout or "differs" in changed.stdout), changed
    assert mutation.read_bytes() != original, "--check wrote the destination"

    (root / COMMON / modules[-1]).unlink()
    missing = _tool(root, "--check", "--dest", str(destination), "--module", modules[-1])
    assert missing.returncode == 0 and modules[-1] in missing.stdout and "missing" in missing.stdout.lower(), missing


def _check_materializer() -> None:
    modules = _modules()
    assert len(modules) == 23, modules
    with tempfile.TemporaryDirectory(prefix="hook-common-identity-") as temporary:
        root = Path(temporary)
        for relative in (TOOL, CONSUMER, *(COMMON / name for name in modules)):
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO_ROOT / relative, destination)
        golden = _tool(root, "--check")
        assert (golden.returncode, golden.stdout, golden.stderr) == (0, "custody_store: identical\n", ""), golden
        golden_write = _tool(root, "--write")
        assert (golden_write.returncode, golden_write.stdout, golden_write.stderr) == (0, golden.stdout, ""), golden_write
        assert hashlib.sha256((root / CONSUMER).read_bytes()).digest() == hashlib.sha256((root / SOURCE).read_bytes()).digest()
        destination = root / "materialized" / "hooks"
        _check_bulk(root, modules, destination)
        _check_subset_and_reports(root, modules, destination)


def _check_imports(source: bytes) -> None:
    tree = ast.parse(source, feature_version=(3, 11))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] in sys.stdlib_module_names for alias in node.names), node.lineno
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and (node.module or "").split(".")[0] in sys.stdlib_module_names, node.lineno


def _check_floor_runtime() -> None:
    interpreter = shutil.which("python3.11")
    proof = """import hashlib, pathlib, runpy, sys, tempfile
s = runpy.run_path(sys.argv[1])
with tempfile.TemporaryDirectory() as temporary:
    root = pathlib.Path(temporary)
    payload = s['Payload'].from_bytes(b'floor proof', 'sha1')
    raw = s['canonical_bytes']({'floor': '3.11'})
    packet = s['publish_packet'](root, ('packets',), raw, {payload.sha256: payload}, pool=('pool',))
    parts = ('packets', packet.packet_id)
    assert s['read_verified'](root, parts, packet_id=packet.packet_id, object_format='sha1') == raw
    s['verify_payload_set'](root, parts, {payload.sha256: (payload.oid, 11)}, object_format='sha1')
    current = s['observe_packet'](root, parts)
    retired = s['trash'](root, parts, expected=current)
    current = s['untrash'](root, parts, expected=retired)
    retired = s['trash'](root, parts, expected=current)
    assert s['purge'](root, parts, expected=retired).state == 'absent'
    assert s['sweep_pool'](root) == (payload.sha256,)
print(sys.version.split()[0], 'standalone custody lifecycle PASS')
"""
    if interpreter is None:
        print("ADVISORY: Python 3.11 interpreter absent; runtime floor proof unmeasured here")
    else:
        result = subprocess.run([interpreter, "-I", "-c", proof, str(REPO_ROOT / SOURCE)], capture_output=True, text=True, check=False)
        assert result.returncode == 0, (result.stdout, result.stderr)
        print(result.stdout.strip())


def main() -> int:
    source = (REPO_ROOT / SOURCE).read_bytes()
    assert source == (REPO_ROOT / CONSUMER).read_bytes(), "materialized custody_store must be byte-identical"
    _check_imports(source)
    _check_floor_runtime()
    if (REPO_ROOT / TOOL).is_file():
        _check_materializer()
    else:
        print(f"ADVISORY: {TOOL} absent; CLI materialization proof skipped in this installed profile")
    print("PASS: identity, stdlib imports, Python 3.11 grammar and advisory runtime check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
