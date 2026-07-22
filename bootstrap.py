#!/usr/bin/env python3
"""Layer 0 — stdlib-only genesis bootstrap shim.

STRICTLY stdlib. No third-party imports, not even transitively (verified
by `tests/... bootstrap_stdlib_only_smoke.py`'s clean-interpreter AST
check). Lives outside any platform-importing package, at the repo root,
because it runs BEFORE the venv exists — it cannot import anything that
imports `ananta` or a plugin.

Three-layer architecture (design doc §2 — the two boundary lines are
load-bearing):
  * Layer -1 (README + the driving agent's shell) confirms/installs git
    and Python 3.13 — the two things needed to even reach this file.
  * Layer 0 (THIS FILE) confirms/prepares HOST prerequisites: Homebrew,
    Postgres server + pgvector extension, this homunculus's OWN
    non-superuser role AND its own database (both named after it, from
    HOMUNCULUS_NAME) + PUBLIC-connect revoke on that db + localhost
    default-scram auth (NO credential VALUE — the scram password is
    generated and vault-stored by Layer 1, in-venv, because the vault
    substrate needs the venv to exist), and the local LM Server + `nomic`
    embeddings endpoint. Then creates the venv and installs the MINIMAL
    SEED (`ananta` + `github_midwife_plugin`), fail-loud. Then hands off
    in-venv.
  * Layer 1 (`github_midwife_plugin`, in-venv) completes the
    profile-driven allowlist install, materializes the profile, seeds
    the scram password, launches, and installs autostart.

No layer installs the dependency it needs to exist to run.

Adaptive host-state (operator ruling 2026-07-09, build spec §10.2): each
probe below reports one of a SMALL, NAMED set of states (an `enum.Enum`)
rather than a boolean or free-text message, so a driving agent (and
Slice H's README ladders) can branch on named states instead of parsing
prose. Every step is: probe first (read-only) → if already healthy,
skip → else print the exact command(s) it is about to run and confirm
via the injectable `confirm` callback → act → re-probe to verify.
Divergent, non-golden-path states (wrong Postgres version, a
non-Homebrew install, a pre-existing role in an inconsistent state,
Homebrew itself absent) are surfaced as `needs_user_action` — a
SANCTIONED stop-and-ask, not a failure — never auto-"fixed" by force
(no uninstall, no initdb over existing data, no role/db drops; reuse a
healthy compatible install rather than force a parallel one).

Invocation: `python3.13 bootstrap.py`. Layer -1 already confirmed the
interpreter is 3.13 before this file ever runs; per CLAUDE.md ("always
assume Python 3.13; no version checks or compatibility code for older
Python"), this file does not re-check or re-exec for that precondition.
"""

from __future__ import annotations

import enum
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The homunculus-name grammar, inlined from
# `github_midwife_plugin.constants.NAME_PATTERN` (== `is_valid_homunculus_name`).
# Layer 0 is STRICTLY stdlib and runs BEFORE the venv exists, so it cannot import
# the plugin -- this duplicate is deliberate (same rationale as the inlined
# BUILD_BACKEND_PACKAGES list below); keep the two in exact sync. `fullmatch`
# (not `match`): `$` matches just before a trailing newline, so `match` would let
# `"x\nhost=evil"` through -- but this name is interpolated straight into the
# admin psql catalog probes / `REVOKE` SQL / `createuser`/`createdb` argv below,
# BEFORE the advertised Layer-1 validation, so it must fail closed here first.
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,62}$")


def _require_homunculus_name() -> str:
    """This homunculus's name IS its Postgres database name (operator ruling
    2026-07-11: "database per homunculus, named after it"). Layer -1 sets
    HOMUNCULUS_NAME for the whole bootstrap->genesis chain; bootstrap CONSUMES
    it and fails loud rather than defaulting -- a silent default would create a
    mis-named database the newborn's state plugin never connects to. Resolved
    at import (like _ADMIN_ROLE below) so every db-touching step in this file
    sees one identity, and the process refuses to start without a name.

    Validated against `_NAME_PATTERN` HERE, at the sole derivation boundary, so
    every downstream SQL/argv site (the pg_roles/pg_database probes, the ACL
    probe, the `REVOKE` on the quoted db identifier, createuser/createdb) sees an
    already-safe name -- a malformed or malicious HOMUNCULUS_NAME (quotes,
    semicolons, spaces, a leading hyphen) can never reach the admin psql layer.
    This is the Layer-0 half of the fix for the same injection class the Layer-1
    validator already closed; the pattern forbids the quote/space/semicolon
    metacharacters those sinks would otherwise be vulnerable to.
    """
    name = os.environ.get("HOMUNCULUS_NAME", "").strip()
    if not name:
        raise RuntimeError(
            "HOMUNCULUS_NAME env var is required -- it is this homunculus's "
            "database name (database per homunculus, named after it). The "
            "driving agent must export it for the bootstrap->genesis chain."
        )
    if not _NAME_PATTERN.fullmatch(name):
        raise RuntimeError(
            f"HOMUNCULUS_NAME {name!r} is not a valid homunculus name: it must "
            f"match {_NAME_PATTERN.pattern} (a lowercase letter, then 1-62 chars "
            "from [a-z0-9_-]). This name is used verbatim as a Postgres role, "
            "database, and schema identifier -- names with quotes, semicolons, "
            "spaces, or a leading hyphen are refused before any database call."
        )
    return name


_DATABASE = _require_homunculus_name()
# This homunculus's OWN Postgres role. db = schema = role = HOMUNCULUS_NAME
# (operator per-homunculus-isolation ruling, 2026-07-12): a non-superuser role
# named after the homunculus, owning its own database. The same single identity
# as _DATABASE -- no second derivation, no shared cluster role.
_ROLE_NAME = _DATABASE
# The Postgres admin/superuser role. Homebrew Postgres initdb's the superuser
# as the OS login user (KB 20/03), so resolve it dynamically rather than
# hardcoding an operator identity -- getpass.getuser() is env-first
# (LOGNAME/USER/...) then the pwd database, and raises OSError if none resolve
# (that raise IS the fail-loud; genesis has no sudo path). This is what lets a
# newborn bootstrap on any machine, not just the operator's.
_ADMIN_ROLE = getpass.getuser()
_SEED_PACKAGE_NAME = "github_midwife_plugin"
# The seed plugin's pyproject.toml pins macos_vault_plugin (an HONEST pin:
# genesis imports its keychain at module level, before profile_install's
# allowlist pass could ever supply it), so Layer 0 must editable-install the
# local copy BEFORE the seed plugin -- pip otherwise tries to resolve the pin
# from PyPI and fails ("no versions"). macos_vault_plugin itself pins only
# `ananta` locally, so the three-package order below is the full closure.
# Cold-agent acceptance finding F-5, 2026-07-12.
_VAULT_PACKAGE_NAME = "macos_vault_plugin"
_MIN_POSTGRES_MAJOR = 14  # pgvector's practical floor; keep in sync with the README (Slice H)
_LM_SERVER_BASE_URL = "http://localhost:1234/v1"
_NOMIC_MODEL_SUBSTRING = "nomic"
_PROBE_TIMEOUT_S = 10
_INSTALL_TIMEOUT_S = 300
_ASSUME_YES_ENV = "HOMUNCULUS_ASSUME_YES"

# The default-scram pg_hba block (KB 20/03; per-role isolation R3,
# 2026-07-12), inserted immediately ABOVE the blanket `trust` block
# (pg_hba is first-match-wins). Two halves:
#   * admin-trust: the OS login superuser (_ADMIN_ROLE) stays on `trust`
#     so `psql -U <admin>` needs no password -- re-asserted here because
#     the `all all scram` lines below would otherwise catch it too.
#   * all-databases scram: EVERY other role (each per-homunculus role) is
#     password-gated over localhost, with NO per-role and NO per-db line.
#     A per-role or per-db line would leave the NEXT homunculus's role/db
#     un-gated (silently passwordless) -- the fall-through class this
#     structurally kills. Any per-homunculus db is covered with zero
#     per-birth hba edits. An existing machine's prior lines (e.g. a previous
#     own `ananta` scram lines) are left byte-identical -- this only
#     INSERTS, never rewrites.
_DEFAULT_SCRAM_LINES: tuple[str, ...] = (
    f"local   all     {_ADMIN_ROLE}                                 trust",
    f"host    all     {_ADMIN_ROLE}         127.0.0.1/32            trust",
    f"host    all     {_ADMIN_ROLE}         ::1/128                 trust",
    "local   all     all                                     scram-sha-256",
    "host    all     all             127.0.0.1/32            scram-sha-256",
    "host    all     all             ::1/128                 scram-sha-256",
)


class BootstrapError(RuntimeError):
    """Raised when a step cannot complete safely (a hard failure, not a stop-and-ask)."""


class HomebrewState(enum.Enum):
    PRESENT = "present"
    ABSENT = "absent"


class PostgresState(enum.Enum):
    ABSENT = "absent"
    RUNNING_HEALTHY_COMPATIBLE = "running_healthy_compatible"
    RUNNING_WRONG_VERSION = "running_wrong_version"
    PRESENT_NOT_RUNNING = "present_not_running"
    NON_HOMEBREW_INSTALL = "non_homebrew_install"


class PgvectorState(enum.Enum):
    AVAILABLE = "available"
    NOT_INSTALLED = "not_installed"


class RoleDbState(enum.Enum):
    ABSENT = "absent"
    PRESENT_HEALTHY = "present_healthy"
    ROLE_EXISTS_UNKNOWN_PASSWORD = "role_exists_unknown_password"


class LMServerState(enum.Enum):
    RUNNING_CORRECT_MODEL = "running_correct_model"
    RUNNING_NO_MATCHING_MODEL = "running_no_matching_model"
    ABSENT = "absent"


class VenvState(enum.Enum):
    ABSENT = "absent"
    PRESENT = "present"


Runner = Callable[..., subprocess.CompletedProcess[str]]
Confirmer = Callable[[str], bool]


def confirm_interactive(message: str) -> bool:
    """Real confirmer: print the message, prompt on stdin. Default for a real run.

    Agent-driven runs that have already inspected the printed action plan can
    opt in explicitly with ``HOMUNCULUS_ASSUME_YES=1``. This is intentionally an
    environment flag rather than blind ``yes |`` piping: the transcript records
    that the driver meant to approve bootstrap's named, probe-derived actions.

    A non-interactive stdin (an agent-driven run with no live TTY) raises
    EOFError from input(); that is a DECLINE, not a crash — the calling step
    surfaces its own `needs_user_action` naming what was declined, and a
    re-run with a terminal or ``HOMUNCULUS_ASSUME_YES=1`` resumes at the same
    step. Caught live by the 2026-07-12 cold-agent seed acceptance test.
    """
    print(message)
    if os.environ.get(_ASSUME_YES_ENV, "").strip().lower() in {"1", "true", "yes", "y"}:
        print(f"{_ASSUME_YES_ENV}=1 set -- proceeding without stdin prompt.")
        return True
    try:
        reply = input("Proceed? [y/N] ").strip().lower()
    except EOFError:
        print("stdin is not interactive -- treating as decline. Re-run from a "
              "terminal, or pipe an explicit 'y' to confirm this step.")
        return False
    return reply in ("y", "yes")


def _default_http_get(url: str, timeout: int) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        return response.read()


HttpGetter = Callable[[str, int], bytes]


@dataclass
class BootstrapContext:
    """Mutable orchestration ledger threaded through each step.

    `target`/`venv_dir` are caller-supplied -- no cwd assumption.
    `run`/`confirm`/`http_get` are injectable so every step is testable
    offline against a fixture tree with a mocked subprocess, an
    auto-answering confirmer, and a fake embeddings-endpoint response --
    no real brew/psql/venv/network touched in a smoke.
    """

    target: Path
    run: Runner
    confirm: Confirmer
    http_get: HttpGetter = _default_http_get
    venv_dir: Path = field(init=False)
    steps: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.venv_dir = self.target / ".venv"


def _print_command(cmd: Sequence[str]) -> None:
    print(f"$ {' '.join(cmd)}")


# ── Homebrew ─────────────────────────────────────────────────────────


def probe_homebrew() -> HomebrewState:
    return HomebrewState.PRESENT if shutil.which("brew") else HomebrewState.ABSENT


def ensure_homebrew(_ctx: BootstrapContext) -> dict[str, Any]:
    """Homebrew absence is ALWAYS a stop-and-ask -- bootstrap.py never
    pipes Homebrew's installer script itself (the exact curl|bash trust
    boundary genesis exists to avoid, per the design doc's framing).
    """
    if probe_homebrew() is HomebrewState.PRESENT:
        return {"step_name": "homebrew", "status": "skipped", "state": "present"}
    return {
        "step_name": "homebrew", "status": "needs_user_action", "state": "absent",
        "detail": (
            "Homebrew not found on PATH. Install it yourself from "
            "https://brew.sh (bootstrap.py does not auto-run installer "
            "scripts fetched from the internet), then re-run bootstrap.py."
        ),
    }


# ── Postgres + pgvector ─────────────────────────────────────────────


def _psql_version_major(ctx: BootstrapContext) -> int | None:
    if not shutil.which("psql"):
        return None
    result = ctx.run(["psql", "--version"], capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S)
    match = re.search(r"(\d+)(?:\.\d+)*", result.stdout)
    return int(match.group(1)) if match else None


def _postgres_accepting_connections(ctx: BootstrapContext) -> bool:
    if not shutil.which("pg_isready"):
        return False
    result = ctx.run(
        ["pg_isready", "-h", "localhost", "-p", "5432"],
        capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S,
    )
    return result.returncode == 0


def _postgres_is_homebrew_managed(ctx: BootstrapContext) -> bool:
    result = ctx.run(
        ["brew", "list", "--formula"], capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S,
    )
    return any(line.startswith("postgresql") for line in result.stdout.splitlines())


def probe_postgres(ctx: BootstrapContext) -> tuple[PostgresState, str]:
    major = _psql_version_major(ctx)
    if major is None:
        return PostgresState.ABSENT, "no `psql` on PATH"
    if not _postgres_is_homebrew_managed(ctx):
        return PostgresState.NON_HOMEBREW_INSTALL, (
            f"psql {major}.x found but not Homebrew-managed (Postgres.app / "
            "EDB / other install channel) -- adaptive handling for that "
            "channel is out of scope for the golden path."
        )
    if not _postgres_accepting_connections(ctx):
        return PostgresState.PRESENT_NOT_RUNNING, "Homebrew postgresql installed but not accepting connections"
    if major < _MIN_POSTGRES_MAJOR:
        return PostgresState.RUNNING_WRONG_VERSION, f"running major version {major} < floor {_MIN_POSTGRES_MAJOR}"
    return PostgresState.RUNNING_HEALTHY_COMPATIBLE, f"running Homebrew postgresql {major}.x"


def ensure_postgres(ctx: BootstrapContext) -> dict[str, Any]:
    state, detail = probe_postgres(ctx)
    if state is PostgresState.RUNNING_HEALTHY_COMPATIBLE:
        return {"step_name": "postgres", "status": "skipped", "state": state.value, "detail": detail}
    if state in (PostgresState.NON_HOMEBREW_INSTALL, PostgresState.RUNNING_WRONG_VERSION):
        return {
            "step_name": "postgres", "status": "needs_user_action", "state": state.value,
            "detail": (
                f"{detail}. Reuse a healthy compatible install rather than force a "
                "parallel one -- present the upgrade-vs-parallel-install decision "
                "to the user; bootstrap.py never uninstalls or overwrites an "
                "existing install."
            ),
        }
    if state is PostgresState.PRESENT_NOT_RUNNING:
        cmd = ["brew", "services", "start", "postgresql"]
        message = f"Homebrew postgresql is installed but not running. Will run: {' '.join(cmd)}"
        if not ctx.confirm(message):
            return {"step_name": "postgres", "status": "needs_user_action", "state": state.value, "detail": "user declined to start postgresql"}
        _print_command(cmd)
        result = ctx.run(cmd, capture_output=True, text=True, timeout=_INSTALL_TIMEOUT_S)
        if result.returncode != 0:
            raise BootstrapError(f"`brew services start postgresql` failed (exit {result.returncode})")
        return {"step_name": "postgres", "status": "completed", "state": "started"}

    # ABSENT: install via Homebrew.
    install_cmd = ["brew", "install", "postgresql"]
    start_cmd = ["brew", "services", "start", "postgresql"]
    message = f"Postgres not found. Will run:\n  $ {' '.join(install_cmd)}\n  $ {' '.join(start_cmd)}"
    if not ctx.confirm(message):
        return {"step_name": "postgres", "status": "needs_user_action", "state": "absent", "detail": "user declined to install postgresql"}
    for cmd in (install_cmd, start_cmd):
        _print_command(cmd)
        result = ctx.run(cmd, capture_output=True, text=True, timeout=_INSTALL_TIMEOUT_S)
        if result.returncode != 0:
            raise BootstrapError(f"`{' '.join(cmd)}` failed (exit {result.returncode})")
    return {"step_name": "postgres", "status": "completed", "state": "installed_and_started"}


def probe_pgvector(ctx: BootstrapContext) -> PgvectorState:
    result = ctx.run(
        ["psql", "-U", _ADMIN_ROLE, "-d", "postgres", "-tAc",
         "SELECT 1 FROM pg_available_extensions WHERE name='vector'"],
        capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S,
    )
    return PgvectorState.AVAILABLE if result.stdout.strip() == "1" else PgvectorState.NOT_INSTALLED


def ensure_pgvector(ctx: BootstrapContext) -> dict[str, Any]:
    state = probe_pgvector(ctx)
    if state is PgvectorState.AVAILABLE:
        return {"step_name": "pgvector", "status": "skipped", "state": state.value}
    cmd = ["brew", "install", "pgvector"]
    if not ctx.confirm(f"pgvector extension not available. Will run: {' '.join(cmd)}"):
        return {"step_name": "pgvector", "status": "needs_user_action", "state": "not_installed", "detail": "user declined to install pgvector"}
    _print_command(cmd)
    result = ctx.run(cmd, capture_output=True, text=True, timeout=_INSTALL_TIMEOUT_S)
    if result.returncode != 0:
        raise BootstrapError(f"`brew install pgvector` failed (exit {result.returncode})")
    state_after = probe_pgvector(ctx)
    if state_after is not PgvectorState.AVAILABLE:
        raise BootstrapError("pgvector install reported success but the extension is still not available")
    return {"step_name": "pgvector", "status": "completed", "state": "installed"}


# ── Role + database + scram (NO credential value -- Layer 1's job) ─


def probe_role_and_db(ctx: BootstrapContext) -> RoleDbState:
    role_exists = ctx.run(
        ["psql", "-U", _ADMIN_ROLE, "-d", "postgres", "-tAc",
         f"SELECT 1 FROM pg_roles WHERE rolname='{_ROLE_NAME}'"],
        capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S,
    ).stdout.strip() == "1"
    db_exists = ctx.run(
        ["psql", "-U", _ADMIN_ROLE, "-d", "postgres", "-tAc",
         f"SELECT 1 FROM pg_database WHERE datname='{_DATABASE}'"],
        capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S,
    ).stdout.strip() == "1"
    if role_exists and db_exists:
        return RoleDbState.PRESENT_HEALTHY
    if not role_exists and not db_exists:
        return RoleDbState.ABSENT
    # Exactly one of (this homunculus's own role, its own db) exists -- a
    # genuinely INCONSISTENT partial state under per-role isolation (each
    # homunculus's role AND db are BOTH named after it, so a clean second
    # homunculus on an already-provisioned machine is fully ABSENT here, not
    # half-present -- it takes the normal create path below). Layer 0 never
    # drops/resets anything it did not create, so this partial state surfaces
    # as needs_user_action rather than being auto-reconciled.
    return RoleDbState.ROLE_EXISTS_UNKNOWN_PASSWORD


def _pg_hba_path(ctx: BootstrapContext) -> Path | None:
    prefix = ctx.run(["brew", "--prefix"], capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S).stdout.strip()
    if not prefix:
        return None
    candidates = sorted(Path(prefix, "var").glob("postgresql@*"))
    if not candidates:
        return None
    return candidates[-1] / "pg_hba.conf"


def _public_connect_revoked(ctx: BootstrapContext) -> bool:
    """True iff the R4 PUBLIC revoke is in effect on this homunculus's db.

    A NULL `datacl` is Postgres's built-in default ACL -- PUBLIC holds
    CONNECT+TEMP -- and any grantee-less aclitem (rendered `=...`) is an
    explicit PUBLIC grant. Either way a sibling homunculus's role could open a
    connection, the exact hole R4 closes (cold-run finding D3,
    2026-07-13: the create path bundled the revoke but a manually-reconciled
    PRESENT_HEALTHY db skipped it silently).
    """
    out = ctx.run(
        ["psql", "-U", _ADMIN_ROLE, "-d", "postgres", "-tAc",
         f"SELECT COALESCE(datacl::text, '') FROM pg_database WHERE datname='{_DATABASE}'"],
        capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S,
    ).stdout.strip()
    if not out:
        return False
    entries = out.strip("{}").split(",")
    return all(not entry.lstrip('"').startswith("=") for entry in entries if entry)


def _scram_lines_present(pg_hba_path: Path) -> bool:
    if not pg_hba_path.is_file():
        return False
    content = pg_hba_path.read_text()
    return all(line in content for line in _DEFAULT_SCRAM_LINES)


def _hba_with_default_scram(content: str) -> str:
    """Return `content` with the default-scram block inserted immediately
    ABOVE the first blanket `all all ... trust` line (first-match-wins), or
    prepended if no such line exists. Idempotent -- returns `content`
    unchanged when the block is already present. Only INSERTS: existing lines
    (e.g. a previous homunculus's own `ananta` scram lines) stay byte-identical.
    """
    if all(line in content for line in _DEFAULT_SCRAM_LINES):
        return content
    block = "\n".join(_DEFAULT_SCRAM_LINES) + "\n"
    lines = content.splitlines(keepends=True)
    for index, line in enumerate(lines):
        parts = line.split()
        if len(parts) >= 4 and parts[1] == "all" and parts[2] == "all" and parts[-1] == "trust":
            lines.insert(index, block)
            return "".join(lines)
    return block + content


def _inconsistent_role_db_report(state: RoleDbState) -> dict[str, Any]:
    return {
        "step_name": "role_and_db", "status": "needs_user_action", "state": state.value,
        "detail": (
            f"exactly one of (role={_ROLE_NAME!r}, db={_DATABASE!r}) already exists -- "
            "an inconsistent partial state. Under per-homunculus isolation both are "
            "named after this homunculus, so a clean second homunculus on an "
            "already-provisioned machine has NEITHER yet (it takes the normal create "
            "path). bootstrap.py never drops or resets a role/database it did not "
            "create. Reconcile by hand (create the missing half, or drop the stray "
            "one if it is safe), then RE-RUN bootstrap.py."
        ),
    }


def _role_db_action_plan(
    state: RoleDbState, *, scram_ok: bool, revoke_ok: bool, vector_ok: bool, pg_hba_path: Path,
) -> list[str]:
    """The stop-and-present-facts action list for ensure_role_and_db's confirm."""
    actions: list[str] = []
    if state is RoleDbState.ABSENT:
        actions.append(f"createuser -U {_ADMIN_ROLE} {_ROLE_NAME}  (non-superuser)")
        actions.append(f"createdb -U {_ADMIN_ROLE} -O {_ROLE_NAME} {_DATABASE}")
        actions.append("CREATE EXTENSION IF NOT EXISTS vector  (per-db activation, D12)")
        actions.append(f'REVOKE CONNECT, TEMP ON DATABASE "{_DATABASE}" FROM PUBLIC  (per-homunculus isolation, R4)')
    else:
        if not revoke_ok:
            # A reconciled/pre-existing db (cold-run finding D3): PUBLIC can still connect.
            # Idempotent, creates/drops nothing -- enforcing R4 on this
            # homunculus's OWN db is exactly what the ruling's wizard prescribes.
            actions.append(f'REVOKE CONNECT, TEMP ON DATABASE "{_DATABASE}" FROM PUBLIC  (R4 -- missing on this pre-existing db)')
        if not vector_ok:
            # A reconciled/pre-existing db (cold-boot finding D12): brew installing
            # pgvector's files makes the extension AVAILABLE machine-wide but
            # never activates it in any specific database. Idempotent.
            actions.append("CREATE EXTENSION IF NOT EXISTS vector  (D12 -- missing on this pre-existing db)")
    if not scram_ok:
        actions.append(f"insert the default-scram block above the trust block in {pg_hba_path} + reload (KB 20/03, R3)")
    return actions


def _apply_role_db_actions(
    ctx: BootstrapContext, state: RoleDbState, *, scram_ok: bool, revoke_ok: bool, vector_ok: bool, pg_hba_path: Path,
) -> None:
    """Execute exactly what _role_db_action_plan presented (same branch logic)."""
    if state is RoleDbState.ABSENT:
        _create_role_db_and_revoke(ctx)
    else:
        if not revoke_ok:
            _revoke_public_connect(ctx)
        if not vector_ok:
            _create_vector_extension(ctx)
    if not scram_ok:
        _write_default_scram_block(ctx, pg_hba_path)


def ensure_role_and_db(ctx: BootstrapContext) -> dict[str, Any]:
    state = probe_role_and_db(ctx)
    if state is RoleDbState.ROLE_EXISTS_UNKNOWN_PASSWORD:
        return _inconsistent_role_db_report(state)

    pg_hba_path = _pg_hba_path(ctx)
    if pg_hba_path is None:
        raise BootstrapError("could not resolve pg_hba.conf path via `brew --prefix`")

    scram_ok = _scram_lines_present(pg_hba_path)
    # The revoke/vector-extension checks are only probeable (and only
    # meaningful) once the db exists -- the ABSENT create path bundles both
    # into _create_role_db_and_revoke.
    revoke_ok = state is RoleDbState.PRESENT_HEALTHY and _public_connect_revoked(ctx)
    vector_ok = state is RoleDbState.PRESENT_HEALTHY and _vector_extension_installed(ctx)

    if state is RoleDbState.PRESENT_HEALTHY and scram_ok and revoke_ok and vector_ok:
        return {"step_name": "role_and_db", "status": "skipped", "state": "present_healthy_scram_revoke_and_vector_configured"}

    actions = _role_db_action_plan(state, scram_ok=scram_ok, revoke_ok=revoke_ok, vector_ok=vector_ok, pg_hba_path=pg_hba_path)
    if not ctx.confirm("Will perform:\n  " + "\n  ".join(actions)):
        return {"step_name": "role_and_db", "status": "needs_user_action", "state": state.value, "detail": "user declined role/db/scram setup"}

    _apply_role_db_actions(ctx, state, scram_ok=scram_ok, revoke_ok=revoke_ok, vector_ok=vector_ok, pg_hba_path=pg_hba_path)
    return {"step_name": "role_and_db", "status": "completed", "state": "ready"}


def _create_role_db_and_revoke(ctx: BootstrapContext) -> None:
    """createuser (plain -> non-superuser, R2) + createdb -O + per-db vector
    extension activation (D12) + the R4 PUBLIC revoke, all as the trust-
    superuser admin role. Per-homunculus isolation (2026-07-12): the newborn
    owns its own db, and only its owner role (plus the admin superuser) may
    connect to it.
    """
    for cmd in (
        ["createuser", "-U", _ADMIN_ROLE, _ROLE_NAME],
        ["createdb", "-U", _ADMIN_ROLE, "-O", _ROLE_NAME, _DATABASE],
    ):
        _print_command(cmd)
        result = ctx.run(cmd, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S)
        if result.returncode != 0:
            raise BootstrapError(
                f"`{' '.join(cmd)}` failed (exit {result.returncode}). "
                f"bootstrap.py assumes the Homebrew Postgres convention that the "
                f"superuser is your OS login user (resolved here as {_ADMIN_ROLE!r} "
                f"via getpass.getuser()); if that role cannot create the "
                f"role/database, initialize Homebrew Postgres under your login "
                f"user or grant it superuser."
            )
    # D12 (a cold-boot finding, 2026-07-13): a freshly created database
    # never has ANY extension activated -- ensure_pgvector only makes the
    # extension's FILES available machine-wide via brew. Every
    # macos_free_minimal schema that declares a vector column (e.g.
    # session_ledger_summary::embeddings) crash-loops at first boot without
    # this: `psycopg.errors.UndefinedObject: type "vector" does not exist`.
    _create_vector_extension(ctx)
    # R4 (per-homunculus isolation): close the default PUBLIC-can-connect grant
    # so a SIBLING homunculus's role cannot even open a connection to this db
    # (its own owner role keeps implicit ALL; the admin superuser bypasses).
    _revoke_public_connect(ctx)


def _revoke_public_connect(ctx: BootstrapContext) -> None:
    """The R4 `REVOKE ... FROM PUBLIC` statement, shared by the create path and
    the reconciled-db repair path (cold-run finding D3). Idempotent."""
    revoke_cmd = [
        "psql", "-U", _ADMIN_ROLE, "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-c",
        f'REVOKE CONNECT, TEMP ON DATABASE "{_DATABASE}" FROM PUBLIC;',
    ]
    _print_command(revoke_cmd)
    result = ctx.run(revoke_cmd, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S)
    if result.returncode != 0:
        raise BootstrapError(
            f"R4 `REVOKE CONNECT, TEMP ON DATABASE {_DATABASE!r} FROM PUBLIC` failed "
            f"(exit {result.returncode})"
        )


def _vector_extension_installed(ctx: BootstrapContext) -> bool:
    """True iff the ``vector`` extension is CREATEd in THIS homunculus's own
    database (D12). Extensions are per-database, not per-cluster/per-role --
    ``ensure_pgvector`` installing the pgvector files via brew makes the
    extension AVAILABLE machine-wide but does not activate it in any specific
    database. A freshly createdb'd database never has it.
    """
    out = ctx.run(
        ["psql", "-U", _ADMIN_ROLE, "-d", _DATABASE, "-tAc",
         "SELECT 1 FROM pg_extension WHERE extname='vector'"],
        capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S,
    ).stdout.strip()
    return out == "1"


def _create_vector_extension(ctx: BootstrapContext) -> None:
    """``CREATE EXTENSION IF NOT EXISTS vector`` on THIS homunculus's own
    database (D12), as the admin role -- extension activation needs
    superuser or an explicit CREATE grant, and the per-homunculus owner role
    is deliberately non-superuser (R2). Idempotent -- IF NOT EXISTS makes
    re-running safe, shared by the create path and the reconciled-db repair
    path.
    """
    cmd = [
        "psql", "-U", _ADMIN_ROLE, "-d", _DATABASE, "-v", "ON_ERROR_STOP=1", "-c",
        "CREATE EXTENSION IF NOT EXISTS vector;",
    ]
    _print_command(cmd)
    result = ctx.run(cmd, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S)
    if result.returncode != 0:
        raise BootstrapError(
            f"`CREATE EXTENSION IF NOT EXISTS vector` on database {_DATABASE!r} "
            f"failed (exit {result.returncode}). Requires the pgvector "
            f"extension files to be installed machine-wide first (see the "
            f"pgvector step above) and {_ADMIN_ROLE!r} to have CREATE "
            f"privilege on the database."
        )


def _write_default_scram_block(ctx: BootstrapContext, pg_hba_path: Path) -> None:
    """Insert the default-scram block above the blanket trust block (R3) and
    reload. Insert-only (never rewrites existing lines) + reload via
    `pg_reload_conf()`.
    """
    print(f"inserting the default-scram block above the trust block in {pg_hba_path}")
    original = pg_hba_path.read_text() if pg_hba_path.is_file() else ""
    pg_hba_path.write_text(_hba_with_default_scram(original))
    reload_cmd = ["psql", "-U", _ADMIN_ROLE, "-d", "postgres", "-c", "SELECT pg_reload_conf();"]
    _print_command(reload_cmd)
    result = ctx.run(reload_cmd, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S)
    if result.returncode != 0:
        raise BootstrapError(f"pg_reload_conf() failed (exit {result.returncode})")


# ── Local embeddings server (LM Server + nomic) ─────────────────────


def probe_lm_server(http_get: HttpGetter) -> LMServerState:
    try:
        raw = http_get(f"{_LM_SERVER_BASE_URL}/models", _PROBE_TIMEOUT_S)
        payload = json.loads(raw)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return LMServerState.ABSENT
    model_ids = [str(m.get("id", "")) for m in payload.get("data", [])]
    if any(_NOMIC_MODEL_SUBSTRING in model_id.lower() for model_id in model_ids):
        return LMServerState.RUNNING_CORRECT_MODEL
    return LMServerState.RUNNING_NO_MATCHING_MODEL


def ensure_lm_server(ctx: BootstrapContext) -> dict[str, Any]:
    """No cloud, no API key, ever -- a local endpoint only. GUI-app setup +
    model pulls are guided (stop-and-ask), never auto-installed: bootstrap.py
    does not attempt to drive LM Studio's UI or assume a CLI is present.
    """
    state = probe_lm_server(ctx.http_get)
    if state is LMServerState.RUNNING_CORRECT_MODEL:
        return {"step_name": "lm_server", "status": "skipped", "state": state.value}
    if state is LMServerState.RUNNING_NO_MATCHING_MODEL:
        return {
            "step_name": "lm_server", "status": "needs_user_action", "state": state.value,
            "detail": f"LM Server is running but no loaded model matches {_NOMIC_MODEL_SUBSTRING!r} -- load a nomic embedding model.",
        }
    return {
        "step_name": "lm_server", "status": "needs_user_action", "state": state.value,
        "detail": (
            f"no local embeddings server reachable at {_LM_SERVER_BASE_URL}. "
            "Install/launch LM Studio, enable server mode, and load a nomic "
            "embedding model (e.g. nomic-embed-text)."
        ),
    }


# ── venv + SEED install (fail-loud, no partial-install continuation) ─


def probe_venv(ctx: BootstrapContext) -> VenvState:
    return VenvState.PRESENT if (ctx.venv_dir / "bin" / "python3").exists() else VenvState.ABSENT


def ensure_venv_and_seed(ctx: BootstrapContext) -> dict[str, Any]:
    if probe_venv(ctx) is VenvState.PRESENT:
        return {"step_name": "venv_and_seed", "status": "skipped", "state": "present"}

    if not ctx.confirm(
        f"Will create a venv at {ctx.venv_dir} and install the seed "
        f"(ananta + {_VAULT_PACKAGE_NAME} + {_SEED_PACKAGE_NAME})."
    ):
        return {"step_name": "venv_and_seed", "status": "needs_user_action", "state": "absent", "detail": "user declined venv creation"}

    venv_cmd = [sys.executable, "-m", "venv", str(ctx.venv_dir)]
    _print_command(venv_cmd)
    result = ctx.run(venv_cmd, capture_output=True, text=True, timeout=_INSTALL_TIMEOUT_S)
    if result.returncode != 0:
        raise BootstrapError(f"venv creation failed (exit {result.returncode})")

    venv_python = ctx.venv_dir / "bin" / "python3"
    # A stock python3.13 venv ships pip only -- no setuptools/wheel -- and the
    # --no-build-isolation editable installs below then die with
    # BackendUnavailable("Cannot import 'setuptools.build_meta'"). Same F8
    # gotcha the Layer-1 seams patch via BUILD_BACKEND_PACKAGES
    # (github_midwife_plugin/constants.py); Layer 0 is stdlib-only so the
    # package list is inlined here. Caught live by the 2026-07-12 cold-agent
    # seed acceptance test.
    backend_cmd = [str(venv_python), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"]
    _print_command(backend_cmd)
    result = ctx.run(backend_cmd, capture_output=True, text=True, timeout=_INSTALL_TIMEOUT_S)
    if result.returncode != 0:
        raise BootstrapError(
            f"build-backend install (pip/setuptools/wheel) failed (exit {result.returncode}): "
            f"{(result.stderr or '').strip()[:500]}"
        )

    for package_dir in (
        ctx.target / "ananta",
        ctx.target / "plugins" / _VAULT_PACKAGE_NAME,
        ctx.target / "plugins" / _SEED_PACKAGE_NAME,
    ):
        install_cmd = [str(venv_python), "-m", "pip", "install", "--no-build-isolation", "-e", str(package_dir)]
        _print_command(install_cmd)
        result = ctx.run(install_cmd, capture_output=True, text=True, timeout=_INSTALL_TIMEOUT_S)
        if result.returncode != 0:
            # stderr tail included (parity with acquisition.py's sibling): the
            # pip resolution error IS the diagnosis; a bare exit code buried
            # the F-5 finding behind a manual re-run.
            raise BootstrapError(
                f"seed install failed for {package_dir} (exit {result.returncode}): "
                f"{(result.stderr or '').strip()[:500]}"
            )

    return {"step_name": "venv_and_seed", "status": "completed", "state": "created_and_seeded"}


# ── Handoff to Layer 1 ───────────────────────────────────────────────


def handoff(ctx: BootstrapContext) -> dict[str, Any]:
    venv_python = ctx.venv_dir / "bin" / "python3"
    cmd = [str(venv_python), "-m", "github_midwife_plugin.genesis"]
    _print_command(cmd)
    result = ctx.run(cmd, capture_output=True, text=True, timeout=_INSTALL_TIMEOUT_S)
    if result.returncode != 0:
        # Codex must-fix (2026-07-09): genesis.py's main() prints its
        # "FATAL: ..." diagnostic to STDERR, not stdout -- the prior
        # raise only ever included stdout's tail, so a genesis failure
        # surfaced here as a bare "exit <n>" with no FATAL text at all.
        # Every FATAL-able path in genesis.py traces back to
        # CredentialSeedError / AutostartError / LaunchctlObservationError
        # / ProfileInstallError / GenesisError's own step-machine errors
        # -- all secret-free by construction (the credential paths have
        # dedicated smoke pins proving it) -- so it is safe to surface
        # both tails here.
        tails = [t for t in (result.stderr[-500:], result.stdout[-500:]) if t]
        raise BootstrapError(f"Layer 1 handoff failed (exit {result.returncode}): {' | '.join(tails)}")
    # genesis.py's own stdout carries the vault-passphrase/autostart status
    # and the MCP-registration command (_mcp_register_suggestion) -- this is
    # captured above for the failure path's diagnostics, so it must also be
    # surfaced here on success or the driving agent never sees it.
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    return {"step_name": "handoff", "status": "completed"}


_StepRunner = Callable[[BootstrapContext], dict[str, Any]]
BOOTSTRAP_STEP_RUNNERS: tuple[tuple[str, _StepRunner], ...] = (
    ("homebrew", ensure_homebrew),
    ("postgres", ensure_postgres),
    ("pgvector", ensure_pgvector),
    ("role_and_db", ensure_role_and_db),
    ("lm_server", ensure_lm_server),
    ("venv_and_seed", ensure_venv_and_seed),
    ("handoff", handoff),
)

_TERMINAL_STATUSES = ("failed", "needs_user_action")


def run_steps(
    ctx: BootstrapContext, step_runners: Sequence[tuple[str, _StepRunner]] = BOOTSTRAP_STEP_RUNNERS,
) -> list[dict[str, Any]]:
    """Execute `step_runners` in order; stop at the first failure OR
    stop-and-ask. `step_runners` is injectable (mirrors the Layer 1
    `steps.py` composability shape) -- not currently used for a second
    sequence, but keeps the two layers' orchestration pattern consistent.
    """
    for _step_name, runner in step_runners:
        try:
            record = runner(ctx)
        except BootstrapError as exc:
            record = {"step_name": _step_name, "status": "failed", "error": str(exc)}
        ctx.steps.append(record)
        if str(record.get("status", "")) in _TERMINAL_STATUSES:
            break
    return ctx.steps


def _step_summary_line(step: dict[str, Any]) -> str:
    """One human-readable line per step record. A failed record carries its
    reason under 'error' (run_steps' BootstrapError wrap and every step's own
    failed shape), NOT 'detail' -- the pre-fix chain read detail/state only,
    so failures printed with no reason at all (cold-agent acceptance finding
    F-6, 2026-07-12: venv_and_seed '[failed]' with the pip resolution error
    silently swallowed).
    """
    reason = step.get("detail") or step.get("error") or step.get("state") or ""
    return f"[{step['status']}] {step['step_name']}: {reason}"


def main() -> int:
    target = Path(__file__).resolve().parent
    ctx = BootstrapContext(target=target, run=subprocess.run, confirm=confirm_interactive)
    steps = run_steps(ctx)
    for step in steps:
        print(_step_summary_line(step))
    last = steps[-1] if steps else {}
    if last.get("status") == "failed":
        return 1
    if last.get("status") == "needs_user_action":
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
