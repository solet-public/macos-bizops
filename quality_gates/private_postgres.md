# Private PostgreSQL smoke fixtures

From the checkout root, with Python 3.13 and PostgreSQL 17 command-line
tools already installed, choose an unused non5432 TCP port and an absolute,
new directory under existing runtime scratch. For example:

```sh
.venv/bin/python3 quality_gates/provision_test_pg.py \
  --dir /absolute/runtime/scratch/private-pg --port 58983 --exec -- \
  env -u HOMUNCULUS_NAME SOLET_NAME="$SOLET_NAME" .venv/bin/python3 \
  quality_gates/run_smokes.py --jobs 4 \
  --register quality_gates/private_postgres_smokes.txt
```

The focused register has exactly nine consumers: two agent messaging,
four ledger and three external PostgreSQL smokes. Each gets an independent
database inside one command-scoped cluster. Each successful body prints
`PRIVATE_PG_BODY` with its fixture key. Successful provisioned acceptance is
nine passed, zero skipped. This does not provision unrelated live smokes or
make a default full battery exercise every database case.

The provisioner resolves initdb, pg_ctl, createdb and psql from fixed PostgreSQL
binary directories. It never installs dependencies. The server binds only
127.0.0.1, disables Unix sockets, uses the current OS user and private trust
authentication, and holds one host-wide advisory lease until teardown. The
manifest contains the endpoint, exact database mapping and run custody;
it contains no passwords or DSNs. The runner forwards only the explicit
PRIVATE_TEST_PG_MANIFEST variable for this fixture. Legacy enable flags cannot
select a profile. Missing configuration is visible exit 77, reported as skipped
by the runner; malformed custody or a failure after provisioning is a failure.

All endpoint and custody refusals are integrity boundaries. The pass condition
is a fresh, owned directory and marker, a free private port, a live matching
owner/postmaster, and the exact database map. The shipped provisioner establishes
these on every explicit author or reviewer invocation; the lifecycle smoke and
the successful live battery exercise that pass path. PostgreSQL availability
outside a requested provisioned run is not a new progress gate.

Ledger setup initializes a fresh APP_HOME below the owned run root. A fixture
vault accepts only its synthetic password key and returns the private trust
password. Public plugin readiness/start owns the lifecycle substrate, and
SchemaManager installs the complete production declaration through the plugin
schema interface, including indexes. New fixture data operations use the state
management interface. The separate live infrastructure smoke physically proves
summary uniqueness and repeat schema installation. Existing behavioral SQL
probes remain in their original test harnesses.

Lifecycle: create the fresh marked cluster and manifest; read endpoint, process
identity and chronological stdout receipts; retire on success, child failure,
timeout, signal or partial startup. Run configuration is immutable: correction
means end this invocation and start a new identity. There is no reopen or
in-place update, and no mutable database record requiring CAS/history verbs.
The marker, nonce, process identity and host lease prevent foreign/stale cleanup.
Repeated finalization is idempotent. Teardown confirms the owned postmaster has
stopped before removing run data. Stop uncertainty retains all data and exits
nonzero. PostgreSQL logs remain beside the removed directory with the nonce in
their names; retain stdout receipts there for historical readback. No shared
cluster or IPC is cleaned. Child execution defaults to a 900-second bound,
configurable with --timeout; start and stop are bounded separately.

Infrastructure tests are registered in the general smoke register and kept out
of the nine-entry live register. Run them through a separate focused register
with the same provisioner when physical database proof is needed. No profile,
operator vault, dependency installation or production schema change is needed.
