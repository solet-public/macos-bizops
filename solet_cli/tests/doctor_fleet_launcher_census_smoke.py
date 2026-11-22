"""Fleet-launcher doctor census coverage.

The census answers one question: does ``client/<name>.zsh`` source a fleet file that is not there?  Every
case here is paired with its opposite, because a test that only asserts the quiet half cannot tell a working
detector from one that never fires.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.doctor import _doctor_result  # pyright: ignore[reportPrivateUsage]  # noqa: E402
from solet_manager.doctor_fleet_launcher_census import collect_fleet_launcher_advisories  # noqa: E402
from solet_manager.models import CheckpointStatus, InstanceRecord, JsonValue  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.transaction import Transaction, canonical_sha256  # noqa: E402

_CHECK_ID = "doctor::fleet_launcher_file_v1"
_PASSED: list[bool] = []


def _check(condition: bool, message: str) -> None:
    _PASSED.append(condition)
    if not condition:
        raise AssertionError(f"red: {message}")


def _record(target: Path) -> InstanceRecord:
    return InstanceRecord(
        name="fixture",
        target=str(target),
        launcher=f"{target}/client/bin/fixture",
        seed_repository="https://github.com/solet-public/macos-bizops.git",
        seed_tag="release-2026-08-20",
        seed_commit="a" * 40,
        seed_tree_hash="b" * 40,
        profile="macos-bizops",
        flow_id="fixture-flow",
        flow_source_revision="a" * 40,
        flow_contract_digest="sha256:" + "c" * 64,
        created_at="2026-09-01T00:00:00+00:00",
        updated_at="2026-09-01T00:00:00+00:00",
        expected_router_name="fixture",
        expected_router_socket="/fixture/fixture.router.sock",
        expected_router_port_range="8800-8999",
    )


def _transaction() -> Transaction:
    seed = SeedLock(
        "https://github.com/solet-public/macos-bizops.git",
        "release-2026-08-20",
        "a" * 40,
        "b" * 40,
        None,
        "macos-bizops",
    )
    return Transaction.create(
        name="fixture",
        target=Path("/fixture/target"),
        input_fingerprint=canonical_sha256({"name": "fixture"}),
        answers={"setup_profile": "macos-bizops"},
        seed=seed,
        flow_id="fixture-flow",
        flow_source_revision="a" * 40,
        flow_contract_digest="sha256:" + "c" * 64,
        stage_ids=("fixture-stage",),
        completion_probe_ids=("fixture-check",),
    ).with_statuses(
        stages={"fixture-stage": CheckpointStatus.VERIFIED},
        completion={"fixture-check": CheckpointStatus.VERIFIED},
    )


def _rows(root: Path) -> list[dict[str, JsonValue]]:
    rows = collect_fleet_launcher_advisories(_record(root))
    found: list[dict[str, JsonValue]] = []
    for item in rows:
        assert isinstance(item, dict)
        found.append(item)
    return found


def _only_row(root: Path) -> dict[str, JsonValue]:
    rows = _rows(root)
    _check(len(rows) == 1, f"the census must return exactly one row, got {len(rows)}")
    _check(rows[0]["check_id"] == _CHECK_ID, "the row must carry the fleet launcher check id")
    return rows[0]


def _write_client(root: Path, text: str) -> Path:
    client = root / "client"
    client.mkdir(parents=True, exist_ok=True)
    path = client / "fixture.zsh"
    path.write_text(text, encoding="utf-8")
    return client


_SOURCE_LINE = 'source "$(dirname "$0")/fixture-fleet.zsh"\n'


def _assert_t2_no_client_file_is_quiet() -> None:
    """T2 -- no client file means no fleet-use evidence, so nothing new is said."""

    with tempfile.TemporaryDirectory() as raw:
        row = _only_row(Path(raw))
        _check(row["status"] == "verified", "T2: a missing client file must read as no evidence, verified")


def _assert_t2b_client_without_source_line_is_quiet() -> None:
    """T2b -- a client file that never sources the fleet file is not fleet use."""

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _write_client(root, '# fixture launcher\nexport PATH="$PATH:/opt/fixture"\n# source fixture-fleet.zsh\n')
        row = _only_row(root)
        _check(row["status"] != "warn", "T2b: a client file without a source line must not warn")
        _check(row["status"] == "verified", "T2b: a client file without a source line must be verified")


def _assert_t3_sourced_but_missing_is_named() -> None:
    """T3 -- the client file sources the fleet file and the fleet file is absent."""

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _write_client(root, "# fixture launcher\n    " + _SOURCE_LINE)
        row = _only_row(root)
        _check(row["status"] == "warn", "T3: a sourced but missing fleet file must be named, not green")
        _check(
            row["reason_code"] == "fleet_file_missing_but_sourced",
            "T3: the warn must carry its specific reason code",
        )
        repair = str(row["repair"])
        _check("Step 4a" in repair, "T3: the repair must point at runbook Step 4a")
        _check("fleet_functions.zsh.template" in repair, "T3: the repair must name the template to render from")
        _check(row["blocking"] is False, "T3: the warn must never block")
        result = _doctor_result(
            "fixture",
            _transaction(),
            [{"id": "fixture-check", "status": "verified"}],
            advisories=[row],
        )
        _check(result.exit_code == 0, "T3: the warn must not change doctor acceptance")
        _check(result.data["advisories"] == [row], "T3: doctor must surface the fleet launcher row")


def _assert_t3b_sourced_and_present_is_verified() -> None:
    """T3b -- the pass path: the same client file with the fleet file present."""

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        client = _write_client(root, _SOURCE_LINE)
        (client / "fixture-fleet.zsh").write_text("# rendered\n", encoding="utf-8")
        row = _only_row(root)
        _check(row["status"] == "verified", "T3b: a sourced fleet file that exists must be verified")


def _assert_t5_unreadable_client_file_is_unknown() -> None:
    """T5 -- an unreadable client path is unknown, never an exception and never a pass."""

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "client" / "fixture.zsh").mkdir(parents=True)
        row = _only_row(root)
        _check(row["status"] == "unknown", "T5: a client path that cannot be read must be unknown")
        _check(row["reason_code"] == "client_file_unreadable", "T5: the unknown must name its cause")

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _write_client(root, "")
        (root / "client" / "fixture.zsh").write_bytes(b"\xff\xfe\x00source fixture-fleet.zsh\n")
        row = _only_row(root)
        _check(row["status"] == "unknown", "T5: undecodable bytes must be unknown")


def main() -> int:
    _assert_t2_no_client_file_is_quiet()
    _assert_t2b_client_without_source_line_is_quiet()
    _assert_t3_sourced_but_missing_is_named()
    _assert_t3b_sourced_and_present_is_verified()
    _assert_t5_unreadable_client_file_is_unknown()
    print(f"doctor_fleet_launcher_census_smoke OK: {len(_PASSED)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
