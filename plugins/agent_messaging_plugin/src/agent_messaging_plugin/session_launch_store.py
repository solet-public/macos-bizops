"""One durable launch owner per solet, including overlapping runtime colors."""

import os
import subprocess

from ananta.core.domain.types import ActionResult
from ananta.interfaces.state_service_protocol import StateServiceProtocol
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.types.column_types import ColumnType
from ananta.types.schema_types import ColumnDefinition, SchemaDefinition, TableSchema

LAUNCH_TABLE = "session_launch_reservation"
LAUNCH_RESOURCE = "session_launch"


class LaunchBusyError(RuntimeError):
    """The existing owner must finish or be explicitly reconciled."""

    def __init__(self, owner: dict[str, object]) -> None:
        super().__init__(f"Session launch is reserved by {owner.get('operation_id')}")
        self.owner = owner


def _data(result: ActionResult) -> dict[str, object]:
    data = result.get("data")
    if result.get("action_status") != "completed" or not isinstance(data, dict):
        raise RuntimeError(f"Launch reservation storage failed: {result!r}")
    return data


def process_start_token(pid: int) -> str:
    """The host's start time for pid, which a recycled pid does not share; "" if unreadable."""
    completed = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, check=False)
    return completed.stdout.strip()


_OWN_START_TOKENS: dict[int, str] = {}


def _own_start_token(pid: int) -> str:
    """This process's own start token, read from the host at most once per process.

    Keyed by pid, so a forked child reads its own token. Only a non-empty token is
    cached; another pid's token is never cached, because a recycled pid would look alive.
    """
    cached = _OWN_START_TOKENS.get(pid)
    if cached is not None:
        return cached
    token = process_start_token(pid)
    if token:
        _OWN_START_TOKENS[pid] = token
    return token


def recorded_owner_pid(owner: dict[str, object]) -> int | None:
    pid = owner.get("owner_pid")
    return pid if isinstance(pid, int) and not isinstance(pid, bool) else None


def owner_alive(owner: dict[str, object]) -> bool | None:
    """Whether the recorded owner process still runs; None when its identity is unproven.

    Both colors run on this one host, so a pid and its start token identify the owner.
    A row written before owner identity was recorded has no pid and is never judged dead.
    """
    pid = recorded_owner_pid(owner)
    recorded = owner.get("owner_started")
    if pid is None or not isinstance(recorded, str) or not recorded:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # Another user's live process: compare its start token below.
    current = process_start_token(pid)
    return current == recorded if current else None


def reserve_launch(state: StateServiceProtocol, operation_id: str, job_id: str = "") -> None:
    """No waiting, lease expiry, or automatic replay; the row records its owner process."""
    pid = os.getpid()
    record: dict[str, object] = {
        "resource_key": LAUNCH_RESOURCE,
        "operation_id": operation_id,
        "job_id": job_id,
        "owner_pid": pid,
        "owner_started": _own_start_token(pid),
    }
    data = _data(
        state.upsert_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": LAUNCH_TABLE,
                "record": record,
                "conflict_columns": ["resource_key"],
                "on_conflict": "do_nothing",
            },
        )
    )
    result = data.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("inserted"), bool):
        raise RuntimeError("Launch reservation did not return an inserted boolean")
    if result["inserted"]:
        return
    rows = _data(
        state.read_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": LAUNCH_TABLE,
                "filters": {"resource_key": LAUNCH_RESOURCE},
                "limit": 1,
            },
        )
    ).get("records")
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        raise RuntimeError("Launch reservation conflicted but its owner is unreadable; retry admission")
    raise LaunchBusyError(rows[0])


def bind_launch_job(state: StateServiceProtocol, operation_id: str, job_id: str) -> None:
    data = _data(
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": LAUNCH_TABLE,
                "filters": {"resource_key": LAUNCH_RESOURCE, "operation_id": operation_id, "job_id": ""},
            },
            {"job_id": job_id},
        )
    )
    result = data.get("result")
    if not isinstance(result, dict) or result.get("updated") != 1:
        raise RuntimeError("Launch job binding lost its reservation owner")


def release_launch(state: StateServiceProtocol, operation_id: str, owner_pid: int | None) -> None:
    """Only the measured owner may release; hard deletion frees the unique key.

    The row must still carry the observed operation and owner pid, so a release
    decided on a stale read never frees a slot that a new owner has since taken.
    """
    pid_filter: object = {"op": "is_null"} if owner_pid is None else owner_pid
    _data(
        state.delete_records(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": LAUNCH_TABLE,
                "filters": {"resource_key": LAUNCH_RESOURCE, "operation_id": operation_id, "owner_pid": pid_filter},
                "soft_delete": False,
            },
        )
    )


def get_session_launch_schema_definition() -> SchemaDefinition:
    """Fresh provider-owned table, installed by the normal schema lifecycle."""
    return SchemaDefinition(
        namespace=AGENT_ROLE_BINDING_NAMESPACE,
        version="1.0.0",
        description="Durable session-launch admission; a provably gone owner is released on the next launch.",
        tables={
            LAUNCH_TABLE: TableSchema(
                table_name=LAUNCH_TABLE,
                id_prefix="slr",
                description="One session-launch owner per solet, with its canonical job id and owner process.",
                columns={
                    "resource_key": ColumnDefinition(type=ColumnType.TEXT, not_null=True, unique=True),
                    "operation_id": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
                    "job_id": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
                    "owner_pid": ColumnDefinition(type=ColumnType.INTEGER),
                    "owner_started": ColumnDefinition(type=ColumnType.TEXT),
                },
            )
        },
    )
