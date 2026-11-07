#!/usr/bin/env python3
"""Run the gate-eligible smoke suite in the effective gate-smoke register.

Project policy is no pytest: smokes are standalone scripts with an
``if __name__ == "__main__"`` entry point that exit 0 on pass, the dedicated
SKIP code on a disclosed, non-blocking dependency gap (see ``_SKIP_EXIT_CODE``
below), and any other non-zero on genuine fail. This runner executes each
smoke named in the tracked register (one repo-relative path per line; ``#``
comments and blank lines ignored) to its natural exit, with no execution
deadline, and aggregates the verdicts. It is the behavioral half of the commit
gate — the static gates live in ``code_quality_check.py``; this runs the smokes.

THE REGISTER IS THE UNION of ``quality_gates/gate_smokes.txt`` and every ``*.txt``
fragment under ``quality_gates/gate_smokes.d/`` beside it, resolved by
``gate_smoke_register.load``, which every reader in the repository calls. A
candidate registers a smoke by writing its OWN fragment file, so two candidates
registering smokes edit no common path and neither invalidates the other's
frozen base entries (prb_ae6d81ea, iss_6ec80ecb). The fragment directory is
resolved beside whichever register ``--register`` or
``RUN_SMOKES_DEFAULT_REGISTER_PATH`` names, so a fixture register gets a
coherent pair of its own. A missing fragment directory is normal and silent; an
unreadable fragment is one advisory on stderr and the readable entries still
run.

The register is a tracked, growing allowlist (it mirrors the god-class / radon
allowlists): a smoke gates only once it is registered, so adding a smoke to the
gate is an explicit, reviewable act and the gate never silently broadens to
smokes that need a live platform — with one ratified exception class (GTE-09,
Coordinator-Day Q2): a smoke whose load-bearing proof genuinely requires a live
external dependency may register if it fails LOUD on an unreachable dependency
(see quality_gates/gate_smokes.txt's header for the full statement).

Skip visibility (2026-08-08, undeclared-dependency audit follow-on:
workbench/2026-08-08_undeclared_system_dependencies_findings_d3-impl.md):
before this, a smoke that self-skipped on a missing OPTIONAL tool (printing
"SKIP" and returning normally, exit 0) was indistinguishable from a smoke
that genuinely ran and passed — this runner determined pass/fail purely from
the process exit code and never surfaced a passing smoke's own stdout, so
the skip text was captured and silently discarded. The aggregate "N/M
passed" figure was therefore an upper bound on real coverage, not a
statement of it. A smoke now signals a disclosed, non-blocking skip by
exiting with ``_SKIP_EXIT_CODE`` (77) instead of 0 — the reserved SKIP exit
code from the automake/Meson/CTest test-tooling convention, chosen
deliberately for that portability precedent rather than invented fresh, and
verified against every currently gate-registered smoke's own exit codes to
carry zero collision risk (none use anything but 0/1 today). REPORTING a
skip and treating it as FATAL are separate concerns: this runner always
reports passed/skipped/failed counts distinctly; whether a skip should fail
the *suite* is caller policy via ``--fail-on-skip`` (default: tolerant — the
normal commit gate accepts a disclosed skip; a future hermeticity/seal check
against a born clone, where a skip IS the hollow-gate condition, passes
``--fail-on-skip`` to make it fatal there without this runner needing two
copies of itself).

Concurrency (2026-08-30, lane-fable-parallelize-run-smokes): smokes run in a
bounded thread pool (``--jobs``, default ``_DEFAULT_JOBS``); each worker thread
only spawns and waits on one smoke subprocess, so threads are the right tool —
the parallelism is between child processes, not Python bytecode. Results are
PRINTED in register order regardless of completion order, so per-smoke
attribution and the output format are unchanged from the serial runner; a
smoke's stdout/stderr is still captured and reported individually. The one
measured hazard of intra-battery concurrency is smokes holding host-exclusive
resources (fixed TCP ports, the host tmux server, the shared venv itself) —
the same contention class already filed cross-lane as GTE-16/GTE-17 in the
workbench backlog. Two escapes exist, each naming the specific resource
it isolates: smokes that contend only with EACH OTHER share an
``_EXCLUSIVE_GROUPS`` chain (sequential on one worker, concurrent with the
rest), and smokes whose resource the whole suite shares are named in the
tracked schedule ``quality_gates/serial_only_smokes.txt`` (the ONE serial-only
source, shared with the born-clone publication gate since 2026-09-20,
iss_f939bae7) and run one at a time BEFORE the pool starts, while no other
smoke is running at all; their results are cached and printed at their true
register slots during the ordered walk. Since 2026-09-04 (iss_0fb8f519), the
serial-only smokes additionally hold a per-user host-global advisory flock,
so separate batteries cannot overlap those host-exclusive smokes. Each smoke
acquires and releases the flock separately; visible serial-slot tickets order
the next acquisition by priority and FIFO, allowing phases to interleave. A
ticket waiting twenty minutes may bypass priority, while still requiring the
physical flock. The pooled
phase never takes that lock. The ``--serial-only-override`` option exists only
as a focused test/diagnostic seam; it replaces that named set but preserves the
same host-global lock. The suite's result set is identical to a fully serial
run either way. ``--jobs 1`` degenerates to one worker (plus the same
host-serialized serial-only phase) for diagnosis of suspected
concurrency-induced flakes.

Host capacity admission (iss_ec9399ee): before either phase, a battery publishes
a per-user ticket under ``~/.ananta/locks/run_smokes_admission/`` with its
``--jobs`` demand and ``--priority`` (``RUN_SMOKES_PRIORITY``, default normal;
critical > normal, FIFO within a priority). The waiting head
starts when ``max(one-minute load, running reserved demand) + demand <= CPUs``.
Several batteries can run concurrently. A highest-priority oldest ticket can
also start when no tracked battery is running, with an advisory. Waiting reason
lines are quoted on stderr every 30 seconds and retained in the ticket JSON;
capacity is remeasured every second. At 20 minutes a waiter proceeds with an
ADVISORY regardless of load or queue position, so it cannot wait forever.
Dead-PID tickets are swept at admission; normal exit and interruption release
the owner's ticket. Missing capacity/storage advises and proceeds. Nested
fixture runners share their parent's live admission. This short queue-update
flock never replaces the separate serial-only host lock above.

Contention signal (2026-09-20, gates-cluster-A5): a battery that ran while
sharing the host with real fleet load can red or skip for reasons that have
nothing to do with the candidate under test — see ``contention_signal.py``'s
module docstring for the five issues this addresses. Every run now measures
and prints ``contention_signal.BatteryContention`` (concurrent live batteries,
liveness-verified, sampled before and after the suite; and how long this
battery waited for the host serial-only lock), and a full-battery run records
it on its own receipt (``contention`` key) so a later reader can tell "this
red co-occurred with real host contention" from "this is a clean regression"
without re-running anything.

Flaky-leg retry (2026-10-11, chg_9b5ec5f8): in the full registered battery
only, every smoke that failed is re-run ONCE, alone, under the host lock, after
the pooled and serial phases (``_retry_failures``; policy constants beside
``_SKIP_EXIT_CODE``). A smoke that fails first and passes its retry is reported
on a ``FLAKY`` line and in the receipt's ``retries`` list, and a run that is
green only through retries ends with receipt result ``passed_with_retries``,
never ``passed``. A failed retry, or more than three first-time failures
(nothing is retried then), leaves the run red exactly as before. A focused
``--register`` run never retries. This complements the fixture fixes tracked
under iss_a36d6dac; it does not replace them.

Full-battery receipts also carry ``summary``: the actual process exit code
(null on interruption), result, final verdict counts, ordered retried/flaky
names and final failed/missing names. These observations establish no base
parity or pre-existing-red classification. ``repeat_flaky`` is a nonblocking
advisory comparing only this receipt with its immediate ``reauthorized_from``:
available history includes prior digest/campaign and the ordered intersection;
missing or malformed prior detail is explicitly unavailable. Prior evidence
is retained unchanged, with no receipt-tree search or fleet-history claim.

No execution deadline (2026-10-21, chg_61fb25cd): ``_wait_for_smoke`` hands
each started smoke to the owned-process collector ``smoke_process.py``, which
drains both output streams until the smoke's natural exit. Every
``SMOKE_PROCESS_ATTENTION_SECONDS`` it prints an ``ATTENTION`` observation to
stderr; attention never stops, retries or fails a smoke. The supervisor, which
also owns the start-rate fuse and interrupt handling, terminates the smoke's
whole session once: after its natural exit (releasing descendants that still
hold its pipes), on explicit owner cancellation (battery interruption or a
tripped start-rate fuse), or after a collection error. A cancelled smoke is
failed with a ``CANCELLED`` line naming the reason and the real exit code,
never passed. A smoke's own exit 124 (a timeout wrapper's status) is a
harness defect to file, never a test verdict: it is reported ``HARNESS``
with a ``HARNESS-DEFECT:`` line and on the summary's ``harness:`` line, its
row is recorded ``not_run``, and it never changes the exit code, is never
counted as failed and is never retried. That holds for a first run, a retry
and a cancelled smoke alike, and a retry that exits 124 does not clear the
genuine first failure it was retrying. Every structured result carries the
same ordered ``harness_defects`` list, always present and empty when there
was none: the full-battery receipt, the ``--results-output`` campaign file
and the proof engine's run. Each defect is collected as it is classified, so
an interrupted battery still lists the defects it observed before the
interruption. Each stream keeps its last ``SMOKE_PROCESS_TAIL_BYTES`` bytes;
earlier output is disclosed by an ``OUTPUT-TRUNCATED:`` line and makes that
observation ineligible for proof reuse. ``smoke_process.py`` documents every
``SMOKE_PROCESS_*`` setting and its named default. There is no ``--timeout``
option, and argparse refuses it. The historical per-entry rows
(``_HISTORICAL_ENTRY_BUDGETS_SECONDS`` and ``smoke_budgets.json``) feed only
``full_smoke_campaign``'s advisory estimate.


Exit codes:
  0 - every listed smoke passed (skips, if any, did not trip --fail-on-skip);
      a smoke that failed first and passed its one retry counts as passed;
      harness defects (exit 124) are disclosed advisories and never change it
  1 - one or more smokes failed (an owner-cancelled smoke included), is a
      missing path, or (only with --fail-on-skip) one or more smokes skipped

Usage:
  .venv/bin/python3 quality_gates/run_smokes.py [--register PATH] [--list] [--fail-on-skip] [--jobs N] [--priority normal|critical] [--campaign NAME] [--reauthorize] [--serial-only-override PATH]
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from run_smokes_scratch import BatteryRuntime

    from quality_gates.smoke_process import Attention, OutputTail

# This file is dual-invoked: directly as a script (sys.path[0] is this
# directory) and imported as `quality_gates.run_smokes` by
# run_smokes_battery_containment_smoke.py (sys.path[0] is the repo root).
# Inserting this file's own directory makes the flat sibling import below
# resolve identically in both cases, matching code_quality_check.py's flat
# `from gate_scope import ...` sibling-import style.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import battery_admission  # noqa: E402
import contention_signal  # noqa: E402
import host_load  # noqa: E402
from battery_summary import SmokeResult as _SmokeResult  # noqa: E402
from battery_summary import build_summary, repeat_flaky_advisory  # noqa: E402
from gate_smoke_register import RegisterRow  # noqa: E402
from gate_smoke_register import load as _load_effective_register  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[1]
# Overridable so an out-of-process regression fixture (contention_signal_smoke.py)
# can make a small, disposable fixture register "the" full battery for receipt
# purposes without touching the real gate_smokes.txt or the real candidate
# tree -- the same isolation _HOST_SERIAL_LOCK_ENV and _BATTERY_RECEIPT_DIR_ENV
# already give a subprocess-level test below.  The fragment directory resolves
# BESIDE whichever register file is in use (gate_smoke_register.FRAGMENT_DIRNAME),
# so an override still gets a coherent (register, fragments) pair and the real
# fragments never bleed into a disposable fixture register.
_DEFAULT_REGISTER_ENV = "RUN_SMOKES_DEFAULT_REGISTER_PATH"
_DEFAULT_REGISTER = Path(os.environ.get(_DEFAULT_REGISTER_ENV, str(Path(__file__).resolve().parent / "gate_smokes.txt")))
_DEFAULT_SERIAL_ONLY = Path(__file__).resolve().parent / "serial_only_smokes.txt"


# Historical calibration evidence only: the runner applies no deadline, and
# full_smoke_campaign merges these rows under smoke_budgets.json for its
# advisory estimate. The identity gate pins one row below by content anchor,
# so the rows stay byte-identical in this file.
# Measured slow-smoke budgets in seconds. The five entries covered together by
# iss_b83def70 include two observed at 191s and 215s under host load; the
# identity census is about 118s and update_runtime_execution reached 187s in a
# battery. A 240s cap gives these valid runs bounded headroom. The writes_5 and
# applies entries have separate evidence of passing alone but timing out in
# batteries; 180s keeps their timeout finite while allowing contention
# headroom. iss_6aa466e3 adds the measured heartbeat battery evidence:
# /private/tmp/gc_heartbeat_landing_sitting3.OFWWWy/run_smokes.stdout
# update_hydration_block_carry and update_setup_contract_advance timed out at
# 120s in that battery, but passed alone in 60.9s and 60.4s; both get 240s.
# Near-cap battery entries get 180s: update_plugin_settled_preview 103.5s,
# update_legacy_direct_runtime 102.8s, update_runtime_prereqs_preview 99.3s,
# claude_seat_launcher 97.3s, and cold_host_matrix_2 95.8s.
_HISTORICAL_ENTRY_BUDGETS_SECONDS: dict[str, int] = {
    "quality_gates/tests/macos_bizops_identity_gate_smoke.py": 240,
    "solet_cli/tests/real_style_sweep_writes_1_smoke.py": 240,
    "solet_cli/tests/real_style_sweep_writes_2_smoke.py": 240,
    "solet_cli/tests/update_transition_bundle_candidates_smoke.py": 240,
    "solet_cli/tests/update_single_colour_census_smoke.py": 240,
    "solet_cli/tests/update_plist_adopt_smoke.py": 240,
    "solet_cli/tests/update_runtime_execution_smoke.py": 240,
    "solet_cli/tests/real_style_sweep_writes_5_smoke.py": 180,
    "solet_cli/tests/real_style_sweep_applies_smoke.py": 180,
    "solet_cli/tests/update_hydration_block_carry_smoke.py": 240,
    "solet_cli/tests/update_setup_contract_advance_smoke.py": 240,
    "solet_cli/tests/update_plugin_settled_preview_smoke.py": 180,
    "solet_cli/tests/update_legacy_direct_runtime_smoke.py": 180,
    "solet_cli/tests/update_runtime_prereqs_preview_smoke.py": 180,
    "deployment/scripts/tests/claude_seat_launcher_smoke.py": 180,
    "solet_cli/tests/cold_host_matrix_2_smoke.py": 180,
}

_HOST_SERIAL_LOCK_ENV = "RUN_SMOKES_HOST_SERIAL_LOCK_PATH"
_HOST_SERIAL_LOCK_PATH = Path(
    os.environ.get(
        _HOST_SERIAL_LOCK_ENV,
        str(Path.home() / ".ananta" / "locks" / "run_smokes_serial_only.lock"),
    )
)
_HOST_LOCK_WAIT_REPORT_SECONDS = 30.0
_BATTERY_RECEIPT_DIR_ENV = "RUN_SMOKES_BATTERY_RECEIPT_DIR"
_BATTERY_RECEIPT_DIR = Path.home() / ".ananta" / "runtime" / "gate_battery_receipts"
_DEFAULT_CAMPAIGN = "default"

# The normal worker pool starts at most twelve children simultaneously.  A real
# full-battery calibration showed its 25th legitimate start can occur inside
# the initial one-second window, so the cap permits ten ordinary pool turnovers
# while still stopping a fork churn above 120 top-level starts per second. This
# is a fuse, not a throttle: the first attempted start past the cap aborts the
# whole battery before it can fork that child.
_MAX_SMOKE_STARTS_PER_WINDOW = 120
_SMOKE_START_WINDOW_SECONDS = 1.0

# The smoke runner is a boundary between the driving shell and each smoke.  A
# copied-and-scrubbed environment is not a boundary: it only excludes the
# variables whose leaks have already been discovered.  These explicit caller
# values serve the registered smoke population and the provisioned private
# PostgreSQL fixtures consumed by explicitly routed live smokes. PATH is
# constructed below rather than inherited, so a caller cannot select a tool
# binary for a smoke by prepending its own directories.
_CHILD_ENVIRONMENT_KEYS: tuple[str, ...] = (
    "APP_HOME",
    "SOLET_NAME",
    "SOLET_HOME",
    "SOLET_WORKSPACE_ROOT",
    "XDG_RUNTIME_DIR",
    "HOME",
    "PROJECT_SOLET_DESK_TEST_DSN",
    "PRIVATE_TEST_PG_MANIFEST",
    battery_admission.DIRECTORY_ENV,
    battery_admission.INHERITED_ENV,
)
# These are not caller configuration.  The runner-under-test receives them
# from ``run_smokes_host_lock_smoke.py`` and must carry them to its child: that
# child is the smoke's stub fixture.  Dropping the mode makes the fixture take
# its test path and recursively launch another runner.  Keep this exact list;
# accepting a RUN_SMOKES_* prefix would recreate the subtractive-filter error
# in reverse.
_RUNNER_CONTROL_ENVIRONMENT_KEYS: tuple[str, ...] = (
    "RUN_SMOKES_HOST_LOCK_STUB_MODE",
    "RUN_SMOKES_HOST_LOCK_STUB_EVENTS",
    "RUN_SMOKES_HOST_LOCK_STUB_LABEL",
    "RUN_SMOKES_HOST_LOCK_STUB_SLEEP",
    "RUN_SMOKES_HOST_SERIAL_LOCK_PATH",
    "RUN_SMOKES_ADMISSION_FIXTURE",
)
_NESTED_SELF_TEST_ENV = "RUN_SMOKES_NESTED_SELF_TEST"
_SYSTEM_PATH_DIRECTORIES: tuple[str, ...] = (
    # Homebrew's Apple Silicon prefix is machine-global like /usr/local, not
    # a caller-provided path segment.  Smokes use node, rg, brew, and tmux
    # from here; omitting it silently turns real checks into no-op passes or
    # disclosed skips on Apple Silicon hosts.
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)

# The automake/Meson/CTest SKIP_RETURN_CODE convention — a smoke exits this
# code to report a disclosed, non-blocking dependency gap. Chosen for that
# portability precedent, not invented fresh. See the module docstring's
# "Skip visibility" section for the full rationale and the collision check.
_SKIP_EXIT_CODE = 77

# The conventional exit status of a timeout wrapper. This runner has no
# deadline, so a smoke exiting 124 reports that its own harness timed out: a
# harness defect to file, never a test verdict (rul_a8eed648 Clarification
# 3). It is disclosed loudly and listed in every structured result's
# ``harness_defects``. Its row stays ``not_run`` because no test outcome was observed.
# It is never counted as failed and never retried. This holds on every path:
# first run, retry and cancellation. A retry that exits 124 does not clear
# the genuine first failure it was retrying.
_HARNESS_DEFECT_EXIT_CODE = 124

# Flaky-leg policy (full registered battery only; a focused --register never
# retries). A smoke that failed is re-run exactly ONCE, alone, under the host
# lock, after the pooled and serial phases (``_retry_failures`` makes one
# ``_run_serial_phase`` call and has no loop, so "once" is structural, not a
# tunable). More than ``_MAX_FIRST_FAILURES_TO_RETRY`` first-time failures is
# a regression, not a flake: nothing is retried and the run is red as before.
# Every retried smoke is reported loudly (FLAKY line, receipt ``retries``) with
# its first failure's last ``_FIRST_FAILURE_TAIL_LINES`` output lines; a run
# that only passes through retries is ``passed_with_retries``, never ``passed``.
_MAX_FIRST_FAILURES_TO_RETRY = 3
_FIRST_FAILURE_TAIL_LINES = 40
_RESULT_PASSED_WITH_RETRIES = "passed_with_retries"

# Worker-pool width. Smokes are subprocess-bound (the pool threads just wait on
# children), so this is really "how many smoke processes share the host at
# once"; the cap keeps concurrent live-DB smokes comfortably inside Postgres's
# connection budget and avoids CPU oversubscription from the handful of
# venv-building smokes.
_DEFAULT_JOBS = min(12, os.cpu_count() or 1)

# Fixture builders timed out inside two otherwise uncontended 12-worker
# batteries (iss_b738f791/iss_64a56163). Four heavy workers give the recorded
# durations a bounded wall-clock estimate within the 25% battery allowance.
# Keep light work on separate workers so queued builders cannot occupy the
# whole pool while waiting for a heavy permit. Both pools together obey jobs.
_HEAVY_JOBS = 4
_HEAVY_PREFIXES = (
    "update_",
    "real_style_",
    "create_origin_",
    "plain_clone_",
    "hook_interpreter_pin_",
    "cold_host_matrix",
    "existing_install_router_failure_sweep_",
)


def _is_heavy_unit(unit: tuple[str, ...]) -> bool:
    """Keep an exclusive chain intact if any member builds heavy fixtures."""
    return any(entry.startswith("solet_cli/tests/") and Path(entry).name.startswith(_HEAVY_PREFIXES) for entry in unit)


# Smokes that hold a HOST-EXCLUSIVE resource for their whole run and therefore
# cannot share the host with ANY concurrently-running smoke are listed, with
# the specific resource each one measures, in serial_only_smokes.txt beside the
# register (see _read_serial_only). They are excluded from the worker pool and
# run one at a time before it starts; this serial phase is also host-serialized
# across separate batteries by _HOST_SERIAL_LOCK_PATH (iss_0fb8f519), while the
# pooled phase remains cross-process concurrent.

# Smokes that conflict ONLY WITH EACH OTHER: every smoke in a group contends
# for the same fixed resource that no smoke outside the group touches, so the
# whole group is chained onto a single pool worker (sequential within the
# group, concurrent with everything else). Cheaper than the serial-only
# schedule, which is reserved for resources the entire suite shares.
_EXCLUSIVE_GROUPS: tuple[tuple[str, ...], ...] = (
    # The swap family shares two fixed resources no smoke outside it touches:
    # TCP ports 50001/50002 (_BLUE_TEST_PORT/_GREEN_TEST_PORT — bound by
    # swap_round_trip_smoke, and again by its scratch-cleanup pair, which
    # re-runs it as a child process; GTE-16), and the pending-finisher record
    # for the fixture solet name "smoke" at the REAL
    # ~/.ananta/runtime/smoke.pending_finisher.json — swap_round_trip and
    # cutover_failure write it through production machinery and
    # rollback_release unlinks it in cleanup (measured: cutover_failure lost
    # its .tmp mid-os.replace when pooled alongside rollback_release). The
    # other solet_name="smoke" deployment smokes drive recording-stub routers
    # and never reach the runtime dir; complete_swap_crash_consistency uses
    # per-scenario-unique solet names.
    (
        "plugins/macos_self_deployment_plugin/tests/swap_round_trip_smoke.py",
        "plugins/macos_self_deployment_plugin/tests/swap_round_trip_scratch_cleanup_smoke.py",
        "plugins/macos_self_deployment_plugin/tests/cutover_failure_smoke.py",
        "plugins/macos_self_deployment_plugin/tests/rollback_release_smoke.py",
    ),
    # The WS5 Step-7 real-style smokes each drive the Solet Manager end to end
    # over a real ~4,000-file seed clone (real git, real genesis writers) many
    # times per process: a cold-host matrix row, a tree row, or a crash/resume
    # boundary is 5-15s of CPU-bound subprocess work, and a smoke holds 5-16 of
    # them. They are registered as one contiguous block, so a pool runs every
    # one of them at once: measured 2026-09-19, 12-wide with all 22 pooled,
    # every one TIMEOUT at 120s (140-300s each); 3-wide, 25-100s; alone,
    # 20-85s. The contended resource is this host's CPU and temp-volume
    # bandwidth, which the whole battery shares, so this is not a fixed
    # resource in the sense of the group above -- it is used here as the only
    # mechanism the runner has for bounding heavy-smoke concurrency. Three
    # chains for Step 7 (the matrix + tree + Homebrew rows, and the crash sweep
    # in two halves) and two for the Step-6 sweeps registered just before them
    # (the same class of work on a smaller fixture), so at most five of them
    # run concurrently with the light pool. The Step-6 sweep files hold at most
    # five crash points each (about 33s typical, 45s worst standalone): the old
    # 10-12-point slices ran 57-70s alone, and a transient external host-load
    # spike pushed two of them past the 120s cap (iss_d1a9649b).
    (
        "solet_cli/tests/update_crash_sweep_smoke.py",
        "solet_cli/tests/update_crash_sweep_writes_1_smoke.py",
        "solet_cli/tests/update_crash_sweep_writes_2_smoke.py",
        "solet_cli/tests/update_crash_sweep_writes_3_smoke.py",
        "solet_cli/tests/update_crash_sweep_writes_4_smoke.py",
        "solet_cli/tests/update_crash_sweep_writes_5_smoke.py",
        "solet_cli/tests/update_crash_sweep_writes_6_smoke.py",
        "solet_cli/tests/update_crash_sweep_writes_7_smoke.py",
        "solet_cli/tests/update_crash_sweep_writes_8_smoke.py",
        "solet_cli/tests/update_crash_sweep_writes_9_smoke.py",
        "solet_cli/tests/update_crash_sweep_writes_10_smoke.py",
        "solet_cli/tests/update_crash_sweep_writes_11_smoke.py",
        "solet_cli/tests/update_crash_sweep_writes_12_smoke.py",
        "solet_cli/tests/update_crash_sweep_applies_smoke.py",
    ),
    (
        "solet_cli/tests/existing_install_router_failure_sweep_1_smoke.py",
        "solet_cli/tests/existing_install_router_failure_sweep_2_smoke.py",
        "solet_cli/tests/existing_install_router_failure_sweep_3_smoke.py",
        "solet_cli/tests/existing_install_router_failure_sweep_4_smoke.py",
        "solet_cli/tests/existing_install_router_failure_sweep_5_smoke.py",
        "solet_cli/tests/existing_install_router_failure_sweep_6_smoke.py",
        "solet_cli/tests/existing_install_router_failure_sweep_7_smoke.py",
        "solet_cli/tests/existing_install_router_failure_sweep_8_smoke.py",
        "solet_cli/tests/existing_install_router_failure_sweep_9_smoke.py",
        "solet_cli/tests/existing_install_router_failure_sweep_10_smoke.py",
        "solet_cli/tests/existing_install_router_failure_sweep_applies_1_smoke.py",
        "solet_cli/tests/existing_install_router_failure_sweep_applies_2_smoke.py",
    ),
    (
        "solet_cli/tests/cold_host_matrix_smoke.py",
        "solet_cli/tests/cold_host_matrix_2_smoke.py",
        "solet_cli/tests/cold_host_matrix_3_smoke.py",
        "solet_cli/tests/cold_host_matrix_4_smoke.py",
        "solet_cli/tests/real_style_tree_smoke.py",
        "solet_cli/tests/real_style_tree_2_smoke.py",
        "solet_cli/tests/real_style_tree_3_smoke.py",
        "solet_cli/tests/real_style_tree_4_smoke.py",
        "solet_cli/tests/real_style_tree_5_smoke.py",
        "solet_cli/tests/real_style_tree_6_smoke.py",
        "solet_cli/tests/homebrew_import_lifecycle_smoke.py",
        "solet_cli/tests/homebrew_upgrade_persistence_smoke.py",
        "solet_cli/tests/homebrew_real_style_smoke.py",
    ),
    (
        "solet_cli/tests/real_style_sweep_writes_1_smoke.py",
        "solet_cli/tests/real_style_sweep_writes_2_smoke.py",
        "solet_cli/tests/real_style_sweep_writes_3_smoke.py",
        "solet_cli/tests/real_style_sweep_writes_4_smoke.py",
        "solet_cli/tests/real_style_sweep_applies_smoke.py",
    ),
    (
        "solet_cli/tests/real_style_sweep_writes_5_smoke.py",
        "solet_cli/tests/real_style_sweep_writes_6_smoke.py",
        "solet_cli/tests/real_style_sweep_writes_7_smoke.py",
        "solet_cli/tests/real_style_sweep_writes_8_smoke.py",
    ),
)

_Verdict = Literal["passed", "skipped", "failed", "harness"]

_ReceiptVerdict = Literal["passed", "skipped", "failed", "missing", "not_run"]


def _receipt_verdict(verdict: _Verdict) -> _ReceiptVerdict:
    """A harness defect carries no test observation; receipts keep it not_run."""
    return "not_run" if verdict == "harness" else verdict


def _retry_receipt_verdict(verdict: _Verdict) -> _ReceiptVerdict:
    """Only a passed retry clears a failure; a harness retry stays unmeasured."""
    return _receipt_verdict(verdict) if verdict in ("passed", "harness") else "failed"


def _initial_smoke_results(entries: list[str]) -> dict[str, _SmokeResult]:
    """Keep register order and disclose entries with no completed observation."""
    return {entry: _SmokeResult(smoke=entry, verdict="not_run", seconds=None, retried=False, output_tail="", output_truncated=False, output_ref=f"#/smoke_results/{index}/output_tail", exit_code=None) for index, entry in enumerate(entries)}


def _output_tail(output: str) -> tuple[str, bool]:
    """Retain at most 1024 UTF-8 bytes, dropping any sliced leading codepoint."""
    encoded = output.encode("utf-8")
    return encoded[-1024:].decode("utf-8", errors="ignore"), len(encoded) > 1024


def _record_smoke_result(
    results: dict[str, _SmokeResult] | None,
    entry: str,
    verdict: _ReceiptVerdict,
    output: str,
    elapsed: float | None,
    *,
    retried: bool = False,
    exit_code: int | None = None,
) -> None:
    """Correct final verdict/tail and sum measured attempts, rounded to 0.1s."""
    if results is None:
        return
    from quality_gates.smoke_proof import SmokeObservations as _ProofObservations

    if isinstance(results, _ProofObservations):
        results.record_attempt(entry, verdict, output, elapsed, retried, exit_code)
    row = results[entry]
    previous = row["seconds"] or 0.0
    row["exit_code"] = exit_code
    row["verdict"] = verdict
    row["seconds"] = round(previous + elapsed, 1) if elapsed is not None else None
    row["retried"] = retried
    row["output_tail"], row["output_truncated"] = _output_tail(output)


def _prior_failed_smokes(rows: object) -> list[str] | None:
    """Optional historical detail is advisory; malformed rows disclose no list."""
    if not isinstance(rows, list):
        return None
    failed: list[str] = []
    for item in cast(list[object], rows):
        if not isinstance(item, dict):
            return None
        row = cast(dict[str, object], item)
        name, verdict = row.get("smoke"), row.get("verdict")
        if not isinstance(name, str) or verdict not in ("passed", "skipped", "failed", "missing", "not_run"):
            return None
        if verdict in ("failed", "missing"):
            failed.append(name)
    return failed


class BatteryReceiptError(RuntimeError):
    """A full-battery receipt makes this invocation unsafe to start."""


class SmokeStartRateError(RuntimeError):
    """The battery attempted to fork faster than its fixed safety fuse permits."""


def _utc_timestamp() -> str:
    """Return an unambiguous receipt timestamp."""
    return dt.datetime.now(tz=dt.UTC).isoformat(timespec="seconds")


def _candidate_path_exists(path: Path) -> bool:
    """Keep existing files and links; omit an unstaged deletion."""
    return path.exists() or path.is_symlink()


def _indexed_candidate_paths(root: Path, output: bytes) -> tuple[Path, ...]:
    """Resolve existing index entries, excluding an unstaged deletion."""
    return tuple(path for item in output.split(b"\0") if item for path in (root / item.decode("utf-8"),) if _candidate_path_exists(path))


def _candidate_paths_for_digest(root: Path) -> tuple[Path, ...]:
    """Return the source paths that define the candidate being executed.

    A materialized candidate with a private Git directory has an index whose
    paths already describe its frozen tree, including its reviewed overlay.
    A working-tree deletion remains in that index until staging, but cannot be
    candidate source; omit it while retaining symlinks for their own byte/link
    validation below.
    A focused fixture or an intentionally Git-free candidate has no such
    index, so it falls back to a complete recursive census.  Generated venv
    and cache artifacts are excluded in both cases: they are runner byproducts,
    not candidate source bytes.
    """
    # Listing a minimal born-seed runner needs no candidate/Git dependencies.
    from quality_gates.git_environment import scrubbed_git_environment

    listed = subprocess.run(
        ("git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"),
        cwd=root,
        env=scrubbed_git_environment(),
        check=False,
        capture_output=True,
    )
    if listed.returncode == 0:
        indexed_paths = _indexed_candidate_paths(root, listed.stdout)
        if indexed_paths:
            return tuple(sorted(indexed_paths))

    ignored_directories = frozenset({".git", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache"})
    paths: list[Path] = []
    for directory, names, filenames in os.walk(root):
        names[:] = sorted(name for name in names if name not in ignored_directories)
        for filename in sorted(filenames):
            path = Path(directory) / filename
            if path.suffix in {".pyc", ".pyo"}:
                continue
            paths.append(path)
    return tuple(sorted(paths))


def _candidate_head_commit(root: Path) -> str | None:
    """Return a valid HEAD commit ID, or advise that receipt identity is tree-only."""
    from quality_gates.git_environment import scrubbed_git_environment

    try:
        head = subprocess.run(
            ("git", "rev-parse", "--verify", "HEAD^{commit}"),
            cwd=root,
            env=scrubbed_git_environment(),
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        head_problem = str(exc)
    else:
        head_commit = head.stdout.strip()
        if head.returncode == 0 and len(head_commit) in {40, 64} and all(character in "0123456789abcdef" for character in head_commit):
            return head_commit
        if head.returncode == 0:
            head_problem = "git returned an invalid commit object ID"
        else:
            head_problem = head.stderr.strip() or f"git exited {head.returncode}"

    print(
        f"ADVISORY: Git HEAD commit unavailable at {root}; using tree-only full-battery receipt identity ({head_problem}).",
        file=sys.stderr,
    )
    return None


def _candidate_digest(root: Path) -> str:
    """Hash candidate files with a versioned Git HEAD marker when available.

    Without a valid HEAD, advise and retain the existing tree-only identity.
    """
    digest = hashlib.sha256()
    head_commit = _candidate_head_commit(root)
    if head_commit:
        digest.update(b"run-smokes-candidate-digest-head-v1\0")
        digest.update(head_commit.encode("ascii"))
        digest.update(b"\0")

    for path in _candidate_paths_for_digest(root):
        try:
            relative = path.relative_to(root).as_posix()
            stat_result = path.lstat()
        except OSError as exc:
            raise BatteryReceiptError(f"candidate digest cannot read {path}: {exc}") from exc
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(oct(stat_result.st_mode).encode("ascii"))
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(b"link\0")
            digest.update(os.readlink(path).encode("utf-8"))
            digest.update(b"\0")
            continue
        if not path.is_file():
            raise BatteryReceiptError(f"candidate digest found non-file path: {relative}")
        digest.update(b"file\0")
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise BatteryReceiptError(f"candidate digest cannot read {relative}: {exc}") from exc
        digest.update(b"\0")
    return digest.hexdigest()


def _receipt_directory() -> Path:
    """Return the host-external receipt location, creating it on demand."""
    configured = os.environ.get(_BATTERY_RECEIPT_DIR_ENV)
    directory = Path(configured) if configured else _BATTERY_RECEIPT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _receipt_path(directory: Path, candidate_digest: str, campaign: str) -> Path:
    campaign_digest = hashlib.sha256(campaign.encode("utf-8")).hexdigest()[:16]
    return directory / f"{candidate_digest}.{campaign_digest}.json"


def _read_receipt(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BatteryReceiptError(f"battery receipt is unreadable: {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise BatteryReceiptError(f"battery receipt is not an object: {path}")
    return cast(dict[str, object], loaded)


def _write_receipt(path: Path, receipt: dict[str, object]) -> None:
    """Atomically publish one receipt state so crashes never leave partial JSON."""
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class _BatteryReceipt:
    """A held per-candidate lock plus the receipt it protects for one battery."""

    def __init__(self, path: Path, lock: IO[str], receipt: dict[str, object]) -> None:
        self.path = path
        self._lock = lock
        self._receipt = receipt
        self._results: dict[str, _SmokeResult] = {}

    def record_contention(self, contention: dict[str, object]) -> None:
        """Attach this battery's contention snapshot; written out by ``finish``."""
        self._receipt["contention"] = contention

    def record_retries(self, retries: list[dict[str, object]]) -> None:
        """Attach every retried smoke's first failure and retry outcome; a run with none gets no key."""
        if retries:
            self._receipt["retries"] = retries

    def record_harness_defects(self, smokes: list[str]) -> None:
        """Attach the smokes whose exit 124 is a harness defect to file; an empty list says none was observed."""
        self._receipt["harness_defects"] = list(smokes)

    def record_smoke_results(self, results: dict[str, _SmokeResult]) -> None:
        """Attach ordered observations; serial interruption may leave started work not_run."""
        self._receipt["smoke_results"] = list(results.values())
        self._results = results

    def record_import_environment(self, environment: dict[str, object]) -> None:
        """Retain bounded origins from this run, including explicit unknowns."""
        self._receipt["import_environment"] = environment

    def finish(self, result: str, exit_code: int | None = None) -> None:
        self._receipt["finished_at"] = _utc_timestamp()
        self._receipt["result"] = result
        summary = build_summary(self._results, result, exit_code)
        self._receipt["summary"] = summary
        self._receipt["repeat_flaky"] = repeat_flaky_advisory(self._receipt.get("reauthorized_from"), summary["flaky_smokes"])
        _write_receipt(self.path, self._receipt)

    def close(self) -> None:
        fcntl.flock(self._lock, fcntl.LOCK_UN)
        self._lock.close()


def _prior_receipt_detail(receipt: dict[str, object]) -> str:
    started = receipt.get("started_at", "unknown timestamp")
    result = receipt.get("result", "unknown result")
    aggregate = f"at {started}; prior result={result}"
    failed = _prior_failed_smokes(receipt.get("smoke_results"))
    if failed is None:
        return f"{aggregate}; per-smoke detail unavailable"
    omitted = max(0, len(failed) - 5)
    return f"{aggregate}; failed/missing smokes={len(failed)}: {', '.join(failed[:5])}; {omitted} omitted"


def _acquire_battery_receipt(candidate_digest: str, campaign: str, *, reauthorize: bool) -> _BatteryReceipt:
    """Claim the only full battery permitted for this candidate/campaign."""
    directory = _receipt_directory()
    path = _receipt_path(directory, candidate_digest, campaign)
    lock = path.with_suffix(".lock").open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        existing = _read_receipt(path)
        lock.close()
        detail = _prior_receipt_detail(existing) if existing else "with no readable receipt"
        raise BatteryReceiptError(f"full battery refused: the same candidate is already running in campaign {campaign!r} {detail}") from exc

    existing = _read_receipt(path)
    stale_running = existing is not None and existing.get("result") == "running"
    if existing is not None and not stale_running and not reauthorize:
        lock.close()
        raise BatteryReceiptError(f"full battery refused: candidate digest {candidate_digest} already ran in campaign {campaign!r} {_prior_receipt_detail(existing)}; pass --reauthorize to run it again")
    if stale_running:
        print("STALE RUNNING BATTERY RECEIPT REPLACED")

    receipt: dict[str, object] = {
        "schema_version": 1,
        "candidate_digest": candidate_digest,
        "campaign": campaign,
        "started_at": _utc_timestamp(),
        "result": "running",
        # Recorded so a sibling battery's contention count (contention_signal
        # .count_concurrent_live_batteries) can tell a genuinely live battery
        # apart from one whose receipt says "running" only because it was
        # hard-killed before reaching `finally: _close_receipt`.
        "pid": os.getpid(),
    }
    if existing is not None:
        receipt["reauthorized_from"] = existing
    _write_receipt(path, receipt)
    return _BatteryReceipt(path, lock, receipt)


def _terminate_process_group(process_group: int) -> None:
    """Terminate a smoke session, escalating only if its descendants linger."""
    try:
        os.killpg(process_group, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            os.killpg(process_group, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.02)
    try:
        os.killpg(process_group, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


class _SmokeProcessSupervisor:
    """Own smoke process sessions, their cancellation and the fork-rate fuse."""

    def __init__(self, maximum_starts: int, window_seconds: float) -> None:
        self._maximum_starts = maximum_starts
        self._window_seconds = window_seconds
        self._starts: deque[float] = deque()
        self._active: dict[int, subprocess.Popen[str]] = {}
        self._started_groups: set[int] = set()
        self._aborted_reason: str | None = None
        self._lock = threading.RLock()

    def reserve_start(self) -> None:
        with self._lock:
            if self._aborted_reason is not None:
                raise SmokeStartRateError(self._aborted_reason)
            now = time.monotonic()
            while self._starts and now - self._starts[0] >= self._window_seconds:
                self._starts.popleft()
            if len(self._starts) >= self._maximum_starts:
                reason = f"start-rate fuse tripped: attempted more than {self._maximum_starts} smoke starts in {self._window_seconds:.1f}s"
                self._aborted_reason = reason
                groups = tuple(self._started_groups)
            else:
                self._starts.append(now)
                return
        for process_group in groups:
            _terminate_process_group(process_group)
        raise SmokeStartRateError(reason)

    def register(self, process: subprocess.Popen[str]) -> bool:
        """Track one new session; return false if a concurrent fuse tripped."""
        with self._lock:
            self._started_groups.add(process.pid)
            if self._aborted_reason is None:
                self._active[process.pid] = process
                return True
            reason = self._aborted_reason
        _terminate_process_group(process.pid)
        process.wait()
        raise SmokeStartRateError(reason)

    def cancel_snapshot(self) -> tuple[tuple[int, ...], tuple[subprocess.Popen[str], ...]]:
        with self._lock:
            self._aborted_reason = "battery interrupted"
            return tuple(self._started_groups), tuple(self._active.values())

    def cancellation_reason(self) -> str | None:
        """Return the owner's explicit cancellation (interrupt or fuse), if any."""
        with self._lock:
            return self._aborted_reason

    def unregister(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._active.pop(process.pid, None)

    def terminate(self, process: subprocess.Popen[str]) -> None:
        _terminate_process_group(process.pid)


def _scratch_runtime() -> type[BatteryRuntime]:
    from run_smokes_scratch import BatteryRuntime

    return BatteryRuntime


def _venv_python() -> Path:
    """Return the repo venv interpreter, failing loud if it is absent."""
    candidate = _REPO_ROOT / ".venv" / "bin" / "python3"
    if not candidate.exists():
        raise FileNotFoundError(f"repo venv interpreter not found: {candidate}")
    return candidate


def _require_solet_name() -> str:
    """Fail closed ONCE, here, when ``SOLET_NAME`` is unset.

    Many smokes resolve the platform identity at import time and fail closed
    without it, so an unset variable turns a suite run into a wall of unrelated
    tracebacks whose shared cause is invisible -- the failure signature reads as
    a broken platform rather than a missing export. Checking at the entry point
    turns that into one discoverable message before any smoke is spawned.

    The wording is bootstrap.py's, deliberately: the platform already states
    this requirement there, and a second phrasing of the same requirement is a
    divergence waiting to rot. Presence only -- name VALIDATION belongs at
    genesis (bootstrap.py), and duplicating the pattern here would be the same
    divergence in another form.
    """
    name = os.environ.get("SOLET_NAME", "").strip()
    if not name:
        raise RuntimeError("SOLET_NAME env var is required -- it is this solet's database name (database per solet, named after it). The driving agent must export it before running the smoke suite: SOLET_NAME=<name> .venv/bin/python3 quality_gates/run_smokes.py")
    return name


def _read_register(register: Path) -> list[str]:
    """Parse and validate the effective register's unique, existing smoke paths.

    The effective register is ``register`` plus every fragment beside it
    (``gate_smoke_register.load``), so a smoke registered only in a fragment runs
    in the full suite. Validation is unchanged in meaning: a repeated path and a
    path with no file are both still errors, and the message now names the exact
    file and line of BOTH the first registration and the repeat, because the two
    can now sit in different files. An unreadable fragment is an advisory the
    caller prints, never an error (rul_becb4bed).
    """
    if not register.exists():
        raise FileNotFoundError(f"gate-smoke register not found: {register}")
    effective = _load_effective_register(register)
    entries: list[str] = []
    first_rows: dict[str, RegisterRow] = {}
    errors: list[str] = []
    for row in effective.rows:
        if row.path in first_rows:
            first = first_rows[row.path]
            errors.append(f"{row.source}:{row.line}: duplicate smoke path {row.path!r} (first registered at {first.source}:{first.line})")
            continue
        first_rows[row.path] = row
        if not (_REPO_ROOT / row.path).is_file():
            errors.append(f"{row.source}:{row.line}: registered smoke path does not exist: {row.path!r}")
            continue
        entries.append(row.path)
    if errors:
        raise ValueError("invalid gate-smoke register:\n" + "\n".join(errors))
    for advisory in effective.advisories:
        print(advisory, file=sys.stderr, flush=True)
    return entries


def _read_serial_only(schedule: Path) -> frozenset[str]:
    """Parse the serial-only schedule: register grammar, no duplicates.

    Entries are not checked against the filesystem or the register here — a
    focused ``--register`` run legitimately names none of them, and an entry
    absent from the register being run is simply inert. The parity smoke
    (``serial_only_schedule_parity_smoke.py``) holds every entry to the
    canonical register, so a renamed smoke cannot silently fall out of the
    serial phase.
    """
    if not schedule.exists():
        raise FileNotFoundError(f"serial-only smoke schedule not found: {schedule}")
    entries: dict[str, int] = {}
    errors: list[str] = []
    for number, raw in enumerate(schedule.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line in entries:
            errors.append(f"{schedule}:{number}: duplicate serial-only smoke path {line!r} (first listed at line {entries[line]})")
            continue
        entries[line] = number
    if errors:
        raise ValueError("invalid serial-only smoke schedule:\n" + "\n".join(errors))
    return frozenset(entries)


def _import_roots(root: Path) -> list[Path]:
    """Enumerate this runner tree's sources ahead of installed editable roots."""
    root = root.resolve()
    platform = root / "ananta" / "src"
    if not platform.is_dir() or not os.access(platform, os.R_OK | os.X_OK):
        raise OSError(f"required source root missing or unreadable: {platform}")
    roots = [platform]
    plugins = root / "plugins"
    if plugins.is_dir():
        roots.extend(sorted(child / "src" for child in plugins.iterdir() if (child / "src").is_dir()))
    for package in ("solet_setup_contracts", "solet_cli"):
        source = root / package / "src"
        if source.is_dir():
            roots.append(source)
    return roots


def _child_environment(python: Path) -> dict[str, str]:
    """Build the deliberate environment for one smoke subprocess.

    The exact pass-through keys provide platform/profile identity, runtime-home
    locations, and explicitly provisioned private PostgreSQL fixtures.
    ``GIT_CONTROLLER_NAME`` is absent even though
    gate smoke code names it: its fixtures set it themselves, while inheriting
    a caller value could arm a gate case that is meant to be unarmed.
    Caller PYTHONPATH is excluded; computed tree roots precede installed
    editable sources while site packages remain available for dependencies.
    """
    environment = {
        "PATH": os.pathsep.join((str(python.parent), *_SYSTEM_PATH_DIRECTORIES)),
    }
    for key in _CHILD_ENVIRONMENT_KEYS:
        value = os.environ.get(key)
        if value is not None:
            environment[key] = value
    if os.environ.get(_NESTED_SELF_TEST_ENV) == "1":
        for key in (_NESTED_SELF_TEST_ENV, *_RUNNER_CONTROL_ENVIRONMENT_KEYS):
            value = os.environ.get(key)
            if value is not None:
                environment[key] = value
    try:
        roots = _import_roots(_REPO_ROOT)
    except OSError as exc:
        print(f"ADVISORY run_smokes import roots unavailable: {_REPO_ROOT}: {exc}", file=sys.stderr)
    else:
        environment["PYTHONPATH"] = os.pathsep.join(str(path) for path in roots)
    environment.update(_scratch_runtime().environment())
    return environment


def _run_one(
    python: Path,
    smoke: Path,
    supervisor: _SmokeProcessSupervisor | None = None,
) -> tuple[_Verdict, str, int]:
    """Run a single smoke to its natural exit; return (verdict, captured_output, exit_code).

    Verdict is "skipped" ONLY on the dedicated ``_SKIP_EXIT_CODE``, and
    "harness" ONLY on ``_HARNESS_DEFECT_EXIT_CODE`` (a harness defect, never
    a test verdict). Every other non-zero code is "failed", never silently
    folded into either. A smoke choosing to exit 77 for reasons other than a
    genuine disclosed gap is a smoke misusing the convention, not something
    this runner can or should second-guess.
    """
    active_supervisor = supervisor or _scratch_runtime().select_supervisor(lambda: _SmokeProcessSupervisor(_MAX_SMOKE_STARTS_PER_WINDOW, _SMOKE_START_WINDOW_SECONDS))
    proc = _start_smoke(python, smoke, active_supervisor)
    return _wait_for_smoke(proc, active_supervisor, smoke)


def _start_smoke(python: Path, smoke: Path, supervisor: _SmokeProcessSupervisor) -> subprocess.Popen[str]:
    """Start one smoke in a session owned by the supervisor."""
    supervisor.reserve_start()
    with _scratch_runtime().defer_signals():
        proc = subprocess.Popen(
            [str(python), str(smoke)],
            cwd=_REPO_ROOT,
            env=_child_environment(python),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        supervisor.register(proc)
    return proc


def _wait_for_smoke(proc: subprocess.Popen[str], supervisor: _SmokeProcessSupervisor, smoke: Path) -> tuple[_Verdict, str, int]:
    """Collect one smoke at its natural exit; there is no execution deadline.

    The collector drains both pipes and reports attention, which never stops
    the smoke. Its single cleanup (natural exit, owner cancellation or a
    collection error) terminates the smoke's whole session through the
    supervisor before the collector reaps the root. Only a reaped root is
    unregistered: after a failed cleanup the supervisor keeps it for the
    battery's abort path. A cancelled smoke is never reported as passed, and
    an exit 124 is reported as a harness defect, never as a test verdict,
    on every path: a cancelled smoke's own 124 keeps both disclosures.
    """
    from quality_gates.smoke_process import collect_owned_process

    result = collect_owned_process(
        proc,
        cleanup_owned=lambda _reason: supervisor.terminate(proc),
        on_reaped=lambda: supervisor.unregister(proc),
        on_attention=lambda event: _report_attention(smoke, event),
        cancellation_requested=lambda: supervisor.cancellation_reason() is not None,
    )
    output = _stream_text("stdout", result.stdout) + _stream_text("stderr", result.stderr)
    if result.cancelled:
        output = _disclosed(output, f"CANCELLED: {supervisor.cancellation_reason()}; real exit code {result.returncode}")
    if result.returncode == _HARNESS_DEFECT_EXIT_CODE:
        return "harness", _disclosed(output, f"HARNESS-DEFECT: exit {result.returncode} is a harness defect to file, not a test verdict; not counted as failed and not retried"), result.returncode
    if result.cancelled:
        return "failed", output, result.returncode
    if result.returncode == 0:
        return "passed", output, result.returncode
    if result.returncode == _SKIP_EXIT_CODE:
        return "skipped", output, result.returncode
    return "failed", output, result.returncode


def _disclosed(output: str, line: str) -> str:
    """Append one runner disclosure line after the smoke's own output."""
    separator = "\n" if output and not output.endswith("\n") else ""
    return f"{output}{separator}{line}"


def _stream_text(name: str, stream: OutputTail) -> str:
    """Decode a retained tail as text-mode pipes did, disclosing omitted bytes."""
    text = stream.tail.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    if not stream.truncated:
        return text
    from quality_gates.smoke_proof import TRUNCATED_OUTPUT_MARKER

    return f"{TRUNCATED_OUTPUT_MARKER} {name} retained the last {len(stream.tail)} of {stream.total_bytes} bytes\n{text}"


def _report_attention(smoke: Path, event: Attention) -> None:
    """Print one observation for the battery owner; the smoke keeps running."""
    load = "unknown" if event.host_load is None else "/".join(f"{value:.2f}" for value in event.host_load)
    print(
        f"ATTENTION {smoke}: running {event.elapsed_seconds:.1f}s, {event.since_output_seconds:.1f}s since output, host load {load}, stdout {event.stdout_bytes} bytes, stderr {event.stderr_bytes} bytes; observation only, the smoke is not stopped or retried",
        file=sys.stderr,
        flush=True,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the gate-eligible smoke suite.")
    parser.add_argument("--register", type=Path, default=_DEFAULT_REGISTER)
    parser.add_argument("--list", action="store_true", help="List the register and exit.")
    parser.add_argument("--proof-request", type=Path, help="Run the computed platform-smoke-proof/v1 selection with clean-only reuse.")
    parser.add_argument("--proof-output", type=Path, help="Write the selected-run structured receipt.")
    parser.add_argument("--results-output", type=Path, help="Retain all original full-battery attempts for a scheduled campaign.")
    parser.add_argument(
        "--campaign",
        default=_DEFAULT_CAMPAIGN,
        help=("Receipt campaign for this full battery (default: %(default)s). A candidate may run once per campaign unless --reauthorize is explicit."),
    )
    parser.add_argument(
        "--reauthorize",
        action="store_true",
        help="Explicitly replace this candidate/campaign's terminal battery receipt.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=_DEFAULT_JOBS,
        help=(f"Worker-pool width for pool-eligible smokes (default: {_DEFAULT_JOBS} on this host). The serial-only holders of host-exclusive resources always run one at a time before the pool starts, whatever this is set to; 1 runs everything serially."),
    )
    parser.add_argument(
        "--priority",
        choices=tuple(battery_admission.PRIORITIES),
        default=battery_admission.priority_value(),
        help="Battery admission priority (normal, critical); defaults to RUN_SMOKES_PRIORITY or normal. Does not preempt running batteries.",
    )
    parser.add_argument(
        "--fail-on-skip",
        action="store_true",
        help=("Treat any skipped smoke as a suite failure. Off by default (the commit gate tolerates a disclosed skip); a hermeticity/seal check against a born clone should pass this, since a skip there IS the hollow-gate condition being checked for."),
    )
    parser.add_argument(
        "--serial-only-override",
        action="append",
        default=None,
        metavar="PATH",
        help=("Replace the serial-only schedule for focused test/diagnostic runs; repeated once per repo-relative smoke path. Each replacement still takes the host-global serial-smoke lock."),
    )
    return parser.parse_args()


def _run_timed(
    python: Path,
    entry: str,
    supervisor: _SmokeProcessSupervisor,
) -> tuple[_Verdict, str, float, int | None]:
    """Run one smoke; return ``(verdict, output, elapsed_seconds, exit_code)``."""
    start = time.monotonic()
    try:
        verdict, output, exit_code = _run_one(python, _REPO_ROOT / entry, supervisor)
    except SmokeStartRateError as exc:
        verdict, output, exit_code = "failed", f"START-RATE-FUSE: {exc}", None
    return verdict, output, time.monotonic() - start, exit_code


_FAILURE_CONTEXT_LINES = 24
_FAILURE_TAIL_LINES = 12


def _failure_marker(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith(("FAIL", "FAILED", "ERROR", "✗", "✘", "❌", "START-RATE-FUSE:", "CANCELLED:", "TIMEOUT"))


def _failure_excerpt(output: str) -> list[str]:
    """Keep all markers and bounded context in original order.

    Marker lines are exempt from the context cap: a total-line cap cannot
    guarantee retaining every FAIL when a smoke emits more FAILs than the cap.
    """
    lines = output.strip().splitlines()
    markers = {i for i, line in enumerate(lines) if _failure_marker(line)}
    tail = set(range(max(0, len(lines) - _FAILURE_TAIL_LINES), len(lines)))
    selected = markers | tail
    context_budget = _FAILURE_CONTEXT_LINES - len(tail - markers)
    for i in sorted(markers):
        for neighbor in (i - 1, i + 1):
            if context_budget > 0 and 0 <= neighbor < len(lines) and neighbor not in selected:
                selected.add(neighbor)
                context_budget -= 1
    return _render_excerpt(lines, selected)


def _render_excerpt(lines: list[str], selected: set[int]) -> list[str]:
    rendered: list[str] = []
    previous = -1
    for i in sorted(selected):
        if i > previous + 1:
            rendered.append(f"[{i - previous - 1} lines elided]")
        rendered.append(lines[i])
        previous = i
    if previous < len(lines) - 1:
        rendered.append(f"[{len(lines) - previous - 1} lines elided]")
    return rendered


def _report_result(
    entry: str,
    verdict: _Verdict,
    output: str,
    elapsed: float,
    skipped: list[str],
    failures: list[str],
    harness: list[str],
    first_failures: dict[str, str] | None = None,
) -> None:
    """Print one smoke's verdict line (serial runner's exact format).

    A harness defect is listed in ``harness`` only: never a failure, so it
    is never retried. ``first_failures``, when given, collects each failed
    smoke's full output so the retry step can quote its first failure.
    """
    label = {"passed": "ok", "skipped": "SKIP", "failed": "FAIL", "harness": "HARNESS"}[verdict]
    print(f"  {label:7} {entry}  ({elapsed:.1f}s)")
    if verdict == "passed":
        for line in output.splitlines():
            if line.startswith("PRIVATE_PG_BODY "):
                print(f"    {line}")
        return
    {"skipped": skipped, "failed": failures, "harness": harness}[verdict].append(entry)
    if verdict == "failed" and first_failures is not None:
        first_failures[entry] = output
    tail = "\n    ".join(_failure_excerpt(output) if verdict == "failed" else output.strip().splitlines()[-12:])
    print(f"    {tail}")


def _schedule_units(pooled: list[str]) -> list[tuple[str, ...]]:
    """Coalesce ``pooled`` into scheduling units: one smoke, or one
    ``_EXCLUSIVE_GROUPS`` chain run sequentially by whichever worker picks it
    up. Group members keep their register-order slot in the printed output
    either way."""
    grouped: dict[str, tuple[str, ...]] = {member: group for group in _EXCLUSIVE_GROUPS for member in group}
    units: list[tuple[str, ...]] = []
    seen_groups: set[tuple[str, ...]] = set()
    for entry in pooled:
        group = grouped.get(entry)
        if group is None:
            units.append((entry,))
        elif group not in seen_groups:
            seen_groups.add(group)
            units.append(tuple(member for member in group if member in pooled))
    return units


def _try_host_serial_lock(handle: IO[str]) -> bool:
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _acquire_host_serial_lock() -> tuple[IO[str], float]:
    """Acquire the host-wide advisory lock for one serial-only smoke.

    Blocking flock alone would make a queued battery look hung. Retry with a
    non-blocking acquisition so the caller gets a durable stderr progress line
    every 30 seconds, while the kernel still releases the lock if its owner
    exits unexpectedly. Returns the wait duration alongside the handle: this
    is a direct, always-present measurement of contention for the resource
    the ``_SERIAL_ONLY`` smokes share (iss_c35292ca/358, iss_dfca317c/357) —
    see ``contention_signal``'s module docstring.
    """
    _HOST_SERIAL_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = _HOST_SERIAL_LOCK_PATH.open("a+", encoding="utf-8")
    waited_since = time.monotonic()
    try:
        battery_admission.wait_for_serial_lock(lambda: _try_host_serial_lock(handle), resource=str(_HOST_SERIAL_LOCK_PATH.resolve()), report_seconds=_HOST_LOCK_WAIT_REPORT_SECONDS)
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} cwd={Path.cwd()}\n")
        handle.flush()
        return handle, time.monotonic() - waited_since
    except BaseException:
        handle.close()
        raise


def _run_serial_phase(
    python: Path,
    serial_entries: list[str],
    supervisor: _SmokeProcessSupervisor,
) -> tuple[dict[str, tuple[_Verdict, str, float, int | None]], float]:
    """Run each serial smoke under the per-user host lock, releasing between smokes.

    Returns the per-smoke results plus how long THIS battery waited to
    acquire the lock across all its serial smokes — 0.0 when there was nothing to serialize, so a register
    with no ``_SERIAL_ONLY`` member reports "did not contend for this
    resource" rather than a zero that looks identical to an instant
    acquisition.
    """
    if not serial_entries:
        return {}, 0.0
    results: dict[str, tuple[_Verdict, str, float, int | None]] = {}
    host_lock_wait_seconds = 0.0
    for entry in serial_entries:
        handle, waited = _acquire_host_serial_lock()
        host_lock_wait_seconds += waited
        try:
            results[entry] = _run_timed(python, entry, supervisor)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
    return results, host_lock_wait_seconds


def _submit_units(
    units: list[tuple[str, ...]],
    run_unit: Callable[[tuple[str, ...]], list[tuple[_Verdict, str, float, int | None]]],
    pool: ThreadPoolExecutor,
    heavy_pool: ThreadPoolExecutor,
    split_heavy: bool,
) -> dict[str, tuple[Future[list[tuple[_Verdict, str, float, int | None]]], int]]:
    futures: dict[str, tuple[Future[list[tuple[_Verdict, str, float, int | None]]], int]] = {}
    for unit in units:
        target_pool = heavy_pool if split_heavy and _is_heavy_unit(unit) else pool
        future = target_pool.submit(run_unit, unit)
        for index, entry in enumerate(unit):
            futures[entry] = (future, index)
    return futures


def _phase_entries(entries: list[str], serial_only: frozenset[str]) -> tuple[set[str], list[str], list[str]]:
    """Return the register's present entries, then its pooled and serial-only phases in register order."""
    present = [entry for entry in entries if (_REPO_ROOT / entry).exists()]
    pooled = [entry for entry in present if entry not in serial_only]
    serial_entries = [entry for entry in present if entry in serial_only]
    return set(present), pooled, serial_entries


def _run_suite(
    python: Path,
    entries: list[str],
    jobs: int,
    serial_only: frozenset[str],
    *,
    first_failures: dict[str, str] | None = None,
    smoke_results: dict[str, _SmokeResult] | None = None,
    harness: list[str] | None = None,
) -> tuple[list[str], list[str], list[str], list[str], float]:
    """Run every smoke in ``entries``; return ``(skipped, failures, missing, harness, host_lock_wait_seconds)``.

    The ``serial_only`` holders of host-exclusive resources run FIRST, one at
    a time, before the pool exists — no other smoke in this battery runs during
    any of them — and their results are cached. Each takes the per-user
    host-global lock separately; pool-eligible smokes then run without it on a bounded
    ``jobs``-wide thread pool. Fixture-building scheduling units use at most
    four of those workers, with a separate queue so light work can progress.
    Printing walks the register in its own order —
    MISSING lines, cached serial results, and pooled results each appear at
    their true register slot, in the serial runner's exact per-smoke format —
    so neither completion order nor scheduling phase ever leaks into the output.
    ``host_lock_wait_seconds`` is this battery's own wait to acquire that lock
    (0.0 when the register has no ``_SERIAL_ONLY`` member) — half of the
    contention signal ``main()`` records on the battery receipt.
    ``first_failures``, when given, is filled with each failed smoke's output
    for the retry step. ``harness`` lists exit-124 harness defects, which
    are neither failures nor retried; when given, that list is extended as
    each defect is reported, so an interrupted caller keeps what was observed.
    """
    skipped: list[str] = []
    failures: list[str] = []
    missing: list[str] = []
    harness = [] if harness is None else harness
    print(f"Running {len(entries)} gate-eligible smokes (no execution deadline; each runs to its natural exit)\n")
    present_set, pooled, serial_entries = _phase_entries(entries, serial_only)
    supervisor = _scratch_runtime().select_supervisor(lambda: _SmokeProcessSupervisor(_MAX_SMOKE_STARTS_PER_WINDOW, _SMOKE_START_WINDOW_SECONDS))
    serial_results, host_lock_wait_seconds = _run_serial_phase(python, serial_entries, supervisor)

    def _run_unit(unit: tuple[str, ...]) -> list[tuple[_Verdict, str, float, int | None]]:
        return [_run_timed(python, entry, supervisor) for entry in unit]

    units = _schedule_units(pooled)
    workers = max(1, jobs)
    split_heavy = workers > _HEAVY_JOBS and any(_is_heavy_unit(unit) for unit in units)
    light_workers = workers - _HEAVY_JOBS if split_heavy else workers
    with (
        ThreadPoolExecutor(max_workers=light_workers) as pool,
        ThreadPoolExecutor(max_workers=_HEAVY_JOBS) as heavy_pool,
        _scratch_runtime().abort_on_error(),
    ):
        futures = _submit_units(units, _run_unit, pool, heavy_pool, split_heavy)
        _walk_register(
            entries,
            present_set,
            serial_results,
            futures,
            skipped,
            failures,
            missing,
            harness,
            first_failures,
            smoke_results=smoke_results,
        )
    return skipped, failures, missing, harness, host_lock_wait_seconds


def _walk_register(
    entries: list[str],
    present_set: set[str],
    serial_results: dict[str, tuple[_Verdict, str, float, int | None]],
    futures: dict[str, tuple[Future[list[tuple[_Verdict, str, float, int | None]]], int]],
    skipped: list[str],
    failures: list[str],
    missing: list[str],
    harness: list[str],
    first_failures: dict[str, str] | None = None,
    *,
    smoke_results: dict[str, _SmokeResult] | None = None,
) -> None:
    """Print every register row at its true slot: MISSING lines, cached
    serial-only results, and pooled results (blocking on each pooled future
    in register order) all interleave exactly where the register puts them."""
    for entry in entries:
        if entry not in present_set:
            missing.append(entry)
            _record_smoke_result(smoke_results, entry, "missing", "", None)
            print(f"  MISSING  {entry}")
            continue
        if entry in serial_results:
            verdict, output, elapsed, exit_code = serial_results[entry]
        else:
            future, index = futures[entry]
            verdict, output, elapsed, exit_code = future.result()[index]
        _record_smoke_result(smoke_results, entry, _receipt_verdict(verdict), output, elapsed, exit_code=exit_code)
        _report_result(entry, verdict, output, elapsed, skipped, failures, harness, first_failures)


def _tail(output: str, lines: int) -> str:
    return "\n".join(output.strip().splitlines()[-lines:])


def _retry_failures(
    python: Path,
    failures: list[str],
    first_failures: dict[str, str],
    *,
    smoke_results: dict[str, _SmokeResult] | None = None,
    harness: list[str] | None = None,
) -> tuple[list[str], list[dict[str, object]], list[str], list[str]]:
    """Re-run each first-time failure ONCE, alone, under the host lock.

    Returns ``(final_failures, retries, flaky, harness)``: the smokes that are
    still red (all of ``failures`` when the cap is exceeded), one receipt
    record per retried smoke, the smokes that failed first but passed on
    retry, and ``harness`` (a new list when none is given) extended by each
    retry that exited 124 as it is classified. A failure is retried only after
    its first attempt reached its terminal state; attention never retries.
    Only a ``passed`` retry clears a failure; a retry that fails, is
    cancelled or reports a skip leaves it red. A retry that exits 124 is a
    harness defect: its row is recorded not_run and it is listed in
    ``harness``, while the genuine first failure stays in its retry record
    and is not cleared.
    """
    harness = [] if harness is None else harness
    if not failures:
        return [], [], [], harness
    if len(failures) > _MAX_FIRST_FAILURES_TO_RETRY:
        print(f"\nretry skipped: {len(failures)} first-time failures exceed the cap of {_MAX_FIRST_FAILURES_TO_RETRY}")
        return list(failures), [], [], harness
    print(f"\nretrying {len(failures)} failed smoke(s) once, alone, under the host lock: {', '.join(failures)}")
    supervisor = _scratch_runtime().select_supervisor(lambda: _SmokeProcessSupervisor(_MAX_SMOKE_STARTS_PER_WINDOW, _SMOKE_START_WINDOW_SECONDS))
    results, _lock_wait_seconds = _run_serial_phase(python, failures, supervisor)
    final_failures: list[str] = []
    retries: list[dict[str, object]] = []
    flaky: list[tuple[str, float]] = []
    for entry in failures:
        verdict, output, elapsed, exit_code = results[entry]
        _record_smoke_result(smoke_results, entry, _retry_receipt_verdict(verdict), output, elapsed, retried=True, exit_code=exit_code)
        label = {"passed": "ok", "harness": "HARNESS"}.get(verdict, "FAIL")
        print(f"  {label:7} {entry}  (retry, {elapsed:.1f}s)")
        if verdict == "harness":
            harness.append(entry)
        if verdict == "passed":
            flaky.append((entry, elapsed))
        else:
            final_failures.append(entry)
            print("    " + "\n    ".join(_failure_excerpt(output) if verdict == "failed" else output.strip().splitlines()[-12:]))
        retries.append(
            {
                "smoke": entry,
                "first_exit_or_verdict": "failed",
                "first_failure_tail": _tail(first_failures[entry], _FIRST_FAILURE_TAIL_LINES),
                "retry_verdict": verdict,
                "retry_seconds": round(elapsed, 1),
            }
        )
    for entry, elapsed in flaky:
        print(f"FLAKY    {entry}  failed first (failed), passed on retry ({elapsed:.1f}s)")
    return final_failures, retries, [entry for entry, _elapsed in flaky], harness


def _run_suite_with_retry(
    entries: list[str],
    args: argparse.Namespace,
    serial_only: frozenset[str],
    *,
    retry: bool,
    smoke_results: dict[str, _SmokeResult] | None = None,
    harness: list[str] | None = None,
) -> tuple[list[str], list[str], list[str], list[str], float, list[dict[str, object]], list[str]]:
    """Admit this battery before either phase, retaining its ticket through retries."""
    with battery_admission.admit(args.jobs, getattr(args, "priority", None)):
        return _run_admitted_suite(entries, args, serial_only, retry=retry, smoke_results=smoke_results, harness=harness)


def _run_admitted_suite(
    entries: list[str],
    args: argparse.Namespace,
    serial_only: frozenset[str],
    *,
    retry: bool,
    smoke_results: dict[str, _SmokeResult] | None = None,
    harness: list[str] | None = None,
) -> tuple[list[str], list[str], list[str], list[str], float, list[dict[str, object]], list[str]]:
    """Run the suite; for the full battery (``retry``), re-run first-time failures once.

    Returns ``_run_suite``'s five values with ``failures`` replaced by the
    FINAL failure list, plus the retry records and the flaky smokes. A given
    ``harness`` list collects every first-run and retry harness defect as it
    is classified, so the caller still holds them if the battery is interrupted.
    """
    python = _venv_python()
    try:
        from quality_gates.smoke_import_environment import SmokeImportEnvironment
    except ImportError as exc:
        detail = f"import environment helper unavailable: {exc}"
        print(f"ADVISORY run_smokes UNKNOWN_ORIGIN_ADVISES_AND_RUNS: {detail}", file=sys.stderr)
        args.import_environment = {"state": "advisory", "private": False, "runner_root": str(_REPO_ROOT), "interpreter": str(python), "advisory": detail}
        return _run_selected_suite(python, entries, args, serial_only, retry=retry, smoke_results=smoke_results, harness=harness)
    scratch = _scratch_runtime().environment().get("TMPDIR")
    environment = SmokeImportEnvironment(_REPO_ROOT, python, Path(scratch) if scratch else None)
    try:
        with environment, _scratch_runtime().abort_on_error():
            python = environment.python
            try:
                return _run_selected_suite(python, entries, args, serial_only, retry=retry, smoke_results=smoke_results, harness=harness)
            finally:
                _quiesce_smoke_children()
    finally:
        args.import_environment = environment.receipt


def _quiesce_smoke_children() -> None:
    """Drain remembered groups before removing their private interpreter."""
    supervisor = _scratch_runtime().select_supervisor(lambda: _SmokeProcessSupervisor(_MAX_SMOKE_STARTS_PER_WINDOW, _SMOKE_START_WINDOW_SECONDS))
    groups, processes = supervisor.cancel_snapshot()
    for group in groups:
        _terminate_process_group(group)
    for process in processes:
        process.wait()


def _run_selected_suite(
    python: Path,
    entries: list[str],
    args: argparse.Namespace,
    serial_only: frozenset[str],
    *,
    retry: bool,
    smoke_results: dict[str, _SmokeResult] | None = None,
    harness: list[str] | None = None,
) -> tuple[list[str], list[str], list[str], list[str], float, list[dict[str, object]], list[str]]:
    """Keep initial and retry phases on the same selected interpreter and harness list."""
    first_failures: dict[str, str] = {}
    skipped, failures, missing, harness, host_lock_wait_seconds = _run_suite(python, entries, args.jobs, serial_only, first_failures=first_failures, smoke_results=smoke_results, harness=harness)
    retries: list[dict[str, object]] = []
    flaky: list[str] = []
    if retry:
        failures, retries, flaky, harness = _retry_failures(python, failures, first_failures, smoke_results=smoke_results, harness=harness)
    return skipped, failures, missing, harness, host_lock_wait_seconds, retries, flaky


def _battery_result(exit_code: int, flaky: list[str]) -> str:
    """The receipt's final value: a retry-assisted pass is never plain ``passed``."""
    if exit_code != 0:
        return "failed"
    return _RESULT_PASSED_WITH_RETRIES if flaky else "passed"


def _summarize(
    total: int,
    skipped: list[str],
    failures: list[str],
    missing: list[str],
    *,
    fail_on_skip: bool,
    flaky: list[str],
    harness: list[str],
) -> int:
    """Print the aggregate verdict; return the process exit code.

    Skips are ALWAYS reported distinctly from passes — never folded into the
    passed count. Whether they trip the exit code is the one policy knob
    (``fail_on_skip``); everything else about the reporting is unconditional.
    Harness defects are neither passes nor failures: they are disclosed as an
    advisory to file and leave the exit code to the measured test outcomes.
    A genuine failure whose one retry hit a harness defect is listed under
    both: the failure was not cleared, and its retry was not measured.
    """
    passed = total - len({*skipped, *failures, *missing, *harness})
    print(f"\nsmokes: {passed}/{total} passed, {len(skipped)}/{total} skipped")
    for label, names in (("skipped", skipped), ("missing", missing), ("failed", failures), ("flaky", flaky)):
        if names:
            print(f"{label}: {len(names)} ({', '.join(names)})")
    if harness:
        print(f"harness: {len(harness)} ({', '.join(harness)}) -- ADVISORY: exit {_HARNESS_DEFECT_EXIT_CODE} is a harness defect to file, not a test verdict, and is never retried; a genuine failure it was retrying is not cleared")
    blocking_skips = fail_on_skip and bool(skipped)
    if blocking_skips:
        print("--fail-on-skip: skips above are treated as failures for this run.")
    return 1 if (failures or missing or blocking_skips) else 0


def _receipt_for_full_battery(args: argparse.Namespace) -> _BatteryReceipt | None:
    """Claim a receipt only for the canonical registered full battery."""
    if args.register.resolve() != _DEFAULT_REGISTER.resolve():
        return None
    return _acquire_battery_receipt(_candidate_digest(_REPO_ROOT), args.campaign, reauthorize=args.reauthorize)


def _finish_receipt(receipt: _BatteryReceipt | None, result: str, smoke_results: dict[str, _SmokeResult], harness: list[str], import_environment: dict[str, object] | None = None, *, exit_code: int | None = None) -> None:
    """Publish the terminal receipt; completed and interrupted runs record the same fields."""
    if receipt is not None:
        if import_environment is not None:
            receipt.record_import_environment(import_environment)
        receipt.record_smoke_results(smoke_results)
        receipt.record_harness_defects(harness)
        receipt.finish(result, exit_code)


def _close_receipt(receipt: _BatteryReceipt | None) -> None:
    if receipt is not None:
        receipt.close()


def _run_proof_request(args: argparse.Namespace) -> int:
    """Carry CLI priority into proof misses, whose executor builds its own args."""
    from quality_gates.smoke_proof import run_request_cli

    previous = os.environ.get(battery_admission.PRIORITY_ENV)
    os.environ[battery_admission.PRIORITY_ENV] = args.priority
    try:
        return run_request_cli(args, _REPO_ROOT)
    finally:
        if previous is None:
            os.environ.pop(battery_admission.PRIORITY_ENV, None)
        else:
            os.environ[battery_admission.PRIORITY_ENV] = previous


def main() -> int:
    args = _parse_args()
    if args.proof_request is not None:
        return _run_proof_request(args)
    try:
        entries = _read_register(args.register)
        serial_only = frozenset(args.serial_only_override) if args.serial_only_override is not None else _read_serial_only(_DEFAULT_SERIAL_ONLY)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.list:
        print("\n".join(entries))
        return 0
    _require_solet_name()
    # A focused/custom register is not a full candidate battery.  In
    # particular, the host-lock regression starts tiny nested fixture runners;
    # allowing one of those to consume the receipt for the real registered
    # suite would make the next full battery refuse for the wrong reason.
    try:
        receipt = _receipt_for_full_battery(args)
    except BatteryReceiptError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    from quality_gates.smoke_proof import campaign_results, observations_for_run

    smoke_results = observations_for_run(entries, args.results_output, _initial_smoke_results(entries))
    receipt_dir = _receipt_directory()
    exclude_path = receipt.path if receipt is not None else None
    concurrent_at_start = contention_signal.count_concurrent_live_batteries(receipt_dir, exclude_path=exclude_path)
    # Filled as each exit 124 is classified, so an interrupted battery still
    # publishes the harness defects it observed.
    harness: list[str] = []
    try:
        with host_load.HostLoadMonitor() as load_monitor, _scratch_runtime()(_SmokeProcessSupervisor(_MAX_SMOKE_STARTS_PER_WINDOW, _SMOKE_START_WINDOW_SECONDS), _terminate_process_group):
            skipped, failures, missing, harness, host_lock_wait_seconds, retries, flaky = _run_suite_with_retry(entries, args, serial_only, retry=receipt is not None, smoke_results=smoke_results, harness=harness)
        concurrent_at_finish = contention_signal.count_concurrent_live_batteries(receipt_dir, exclude_path=exclude_path)
        contention = contention_signal.measure(
            concurrent_live_batteries_start=concurrent_at_start,
            concurrent_live_batteries_finish=concurrent_at_finish,
            host_lock_wait_seconds=host_lock_wait_seconds,
            host_load=load_monitor.snapshot(),
        )
        print(contention.render())
        if receipt is not None:
            receipt.record_contention(contention.as_dict())
            receipt.record_retries(retries)
        result = _summarize(
            len(entries),
            skipped,
            failures,
            missing,
            fail_on_skip=args.fail_on_skip,
            flaky=flaky,
            harness=harness,
        )
    except BaseException:
        _finish_receipt(receipt, "interrupted", smoke_results, harness, getattr(args, "import_environment", None))
        campaign_results(args.results_output, smoke_results, "interrupted", None, sys.modules[__name__], harness=harness)
        raise
    else:
        _finish_receipt(receipt, _battery_result(result, flaky), smoke_results, harness, getattr(args, "import_environment", None), exit_code=result)
        campaign_results(args.results_output, smoke_results, _battery_result(result, flaky), result, sys.modules[__name__], harness=harness)
        return result
    finally:
        _close_receipt(receipt)


def execute_selected(entries: list[str]) -> dict[str, Any]:
    """Run selected misses through this exact canonical runner instance."""
    from quality_gates.smoke_proof import execute_registered

    return execute_registered(entries, sys.modules[__name__])


if __name__ == "__main__":
    raise SystemExit(main())
