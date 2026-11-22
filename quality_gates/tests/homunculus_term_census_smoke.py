"""Closed terminology census pass, refusal, correction and wiring proof."""

from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "quality_gates"))

from quality_gates import code_quality_check as quality  # noqa: E402
from quality_gates import code_quality_scope as scope  # noqa: E402
from quality_gates.homunculus_term_census import run  # noqa: E402


def _measure(root: Path, allowlist: Path) -> tuple[int, str]:
    output = io.StringIO()
    with redirect_stdout(output):
        code = run(["--repo-root", str(root), "--allowlist", str(allowlist)])
    return code, output.getvalue()


def _shared_gate_wiring() -> None:
    """T7: preserve exact argv, deadline and blocking versus crash behavior."""
    callback = dict(quality._BLOCKING_GATE_CHECKS)["homunculus_term_census"]
    assert "homunculus_term_census" in scope.CLASS_NAMES
    for code in (0, 2, 70):
        results = quality._CheckResults()
        with patch.object(quality.subprocess, "run", return_value=CompletedProcess(["fixture"], code, "measured", "")) as child:
            blocked = callback(ROOT, Path(sys.executable), results)
        assert child.call_args.args[0] == [str(Path(sys.executable)), str(ROOT / "quality_gates/homunculus_term_census.py"), "--repo-root", str(ROOT), "--allowlist", str(ROOT / "quality_gates/homunculus_term_dispositions.json")]
        assert child.call_args.kwargs["timeout"] == 300
        assert blocked is (code == 2)
        assert bool(results.crashed_gates) is (code == 70)


def _fixture_cases(root: Path) -> None:
    """T2-T6: exercise new hits, count correction, advisories and malformed data."""
    source = root / "ananta/src/new.py"
    source.parent.mkdir(parents=True)
    source.write_text("# Homunculus\n", encoding="utf-8")
    allowlist = root / "dispositions.json"
    allowlist.write_text(json.dumps({"policy": "Fixture policy.", "entries": []}), encoding="utf-8")
    code, output = _measure(root, allowlist)
    assert code == 2 and "UNCLASSIFIED ananta/src/new.py 1" in output, "T2 unclassified"
    row: dict[str, object] = {"path": "ananta/src/new.py", "class": "legacy_detection", "count": 1, "reason": "Recognize an older installation."}

    def write(entries: list[dict[str, object]]) -> None:
        allowlist.write_text(json.dumps({"policy": "Fixture policy.", "entries": entries}), encoding="utf-8")

    write([row])
    source.write_text("# Homunculus\n# HOMUNCULUS homunculus\n", encoding="utf-8")
    code, output = _measure(root, allowlist)
    assert code == 2 and "INCREASED ananta/src/new.py recorded 1 measured 2" in output, "T3 increased"
    row["count"] = 2
    write([row])
    assert _measure(root, allowlist)[0] == 0, "T3 raised count passes"
    source.write_text("\n\n" + source.read_text(encoding="utf-8"), encoding="utf-8")
    assert _measure(root, allowlist)[0] == 0, "T3 line shifts pass"
    row["count"] = 3
    write([row])
    source.write_text("# homunculus\n", encoding="utf-8")
    code, output = _measure(root, allowlist)
    assert code == 0 and "ADVISORY stale ananta/src/new.py recorded 3 measured 1" in output, "T4 stale"
    source.unlink()
    code, output = _measure(root, allowlist)
    assert code == 0 and "ADVISORY obsolete entry ananta/src/new.py" in output, "T4 gone"
    source.write_text("# solet\n", encoding="utf-8")
    assert "ADVISORY obsolete entry" in _measure(root, allowlist)[1], "T4 no hits"
    tests = root / "plugins/x/src/x/tests/fixture.py"
    tests.parent.mkdir(parents=True)
    tests.write_text("# homunculus\n", encoding="utf-8")
    source.with_suffix(".txt").write_text("homunculus\n", encoding="utf-8")
    write([])
    assert _measure(root, allowlist) == (0, "homunculus-term census: 0 files, 0 lines, 0 unclassified, 0 increased\n"), "T5 exclusions"
    _malformed_cases(root, allowlist, row)


def _malformed_cases(root: Path, allowlist: Path, row: dict[str, object]) -> None:
    """T6: malformed data stays unmeasured instead of passing or blocking."""

    def write(entries: list[dict[str, object]]) -> None:
        allowlist.write_text(json.dumps({"policy": "Fixture policy.", "entries": entries}), encoding="utf-8")

    for key, value in (("class", "unknown"), ("reason", ""), ("reason", "two\nlines"), ("count", 0), ("count", "2"), ("count", True), ("extra", "wrong")):
        write([{**row, key: value}])
        code, output = _measure(root, allowlist)
        assert code == 70 and output.startswith("GATE CRASH"), f"T6 malformed {key}={value!r}"
    write([row, row])
    assert _measure(root, allowlist)[0] == 70, "T6 duplicate"
    for document in ([], {"policy": 2, "entries": []}, {"policy": "p", "entries": [], "extra": 1}):
        allowlist.write_text(json.dumps(document), encoding="utf-8")
        code, output = _measure(root, allowlist)
        assert code == 70 and output.startswith("GATE CRASH"), "T6 document shape"


def main() -> int:
    """Run T1-T7 against real production bytes and isolated temporary fixtures."""
    assert run(["--repo-root", str(ROOT)]) == 0, "T1 real-tree pass path"
    with tempfile.TemporaryDirectory() as temporary:
        _fixture_cases(Path(temporary))
    _shared_gate_wiring()
    print("homunculus-term census smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
