# Shared custody store

`custody_store.py` is the canonical stdlib-only Python 3.11 implementation.
Run `plugins/github_midwife_plugin/tools/materialize_hook_common.py --write`
after editing it. With no destination, this keeps the original
`plugins/agent_messaging_plugin/src/agent_messaging_plugin/custody_store.py`
copy. `--check` reports any diff and exits successfully.

For a hook directory, use `--write --dest DIR` to copy every module named by
`working_tree_recovery.py`'s `MODULES` inventory. Use repeated `--module NAME`
arguments to select a subset, for example `--write --dest DIR --module
custody_store.py --module working_tree_recovery.py`. `--check --dest DIR`
reports differences without writing. A missing source is named in the report;
the command still exits successfully. The identity smoke checks bytes and modes
for all 23 modules in a temporary destination. Shipped Claude Code and Codex
hook directories remain frozen for their separate reviews.

Lane and recovery callers use this engine directly. The lane modules do not
re-export moved names. `canonical_bytes`, `blob_oid`, `PacketWriter`,
`LocalEntry`, `read_entry`, `Payload`, `open_store_directory`, `publish_packet`,
`read_verified`, `read_payload` and `verify_payload_set` share one packet
encoding, reader and publisher. Packets contain `manifest.json` and exactly
the named `payload/<sha256>` regular files. A caller supplies the manifest's
domain rules; the engine verifies the stored byte identities and exact sets.

`publish_packet(root, parts, manifest_bytes, payloads, writer, *, dir_mode,
file_mode, pool, validate=None, retain_failed_staging=False)` stages and
fsyncs before promotion. Recovery uses 0700 directories, 0400 files and
`pool=("pool",)`: each pooled payload is hard-linked into its packets.
`EPERM` or `EXDEV` on a hard link uses a verified copy and reports that
digest in `Publication.copied_payloads`; other errors remain explicit failures.
Root descriptor locks serialize pool publication with lifecycle and sweeping.
The original unpooled lane publication uses atomic rename arbitration.

The two optional keywords extend design section 3.1 under seat ruling
`agm-_ec75421800754da4911d90bb4037e668`:

- `validate(staging_component)` runs before promotion. Lane custody supplies
  its second inventory and staged manifest/history/index checks here.
- Default failure handling rolls back its own publication and removes its
  staging and newly orphaned pool files. Lane custody explicitly passes
  `retain_failed_staging=True`, `pool=None`, and 0755/0444 modes: its existing
  failure receipts name retained, unusable staging for inspection. Validation
  failures follow the same explicit policy. Cleanup never removes a different
  publisher's inode. Neither policy leaves a failed packet published.

The lifecycle is publish/read, immutable successor publication for correction,
`trash`, `untrash`, then terminal `purge`. `observe_packet` reads the current
state with an inode/ctime token. Each transition requires that exact token and
the appropriate present or trashed state; a changed observation raises
`recovery_stale_state`. The pass condition is a fresh observation; the caller's
on-demand CLI will obtain it, and the store smoke covers stale and current
tokens, reversal, ABA replacement and terminal removal. `sweep_pool` removes
only regular digest files whose link count is one, leaving live packet and
staging links intact. Grace periods, delivery decisions, authorization and
append-only lifecycle history remain the recovery caller's responsibility.

The new store smoke drives these public entry points and both publication
policies. Golden manifests captured from the pre-extraction lane engine prove
the lane packet ids, payloads, layout and modes remain unchanged. The only
extended-fence consumer edits are direct moved-name imports and matching
`PacketWriter` call-site renames in `lane_worktrees.py`,
`lane_worktrees_smoke.py` and `retire_worktree_disposition_smoke.py`. seat
ruling `agm-_9faec8c7703243b480627954322b0bea` additionally permits two
typing-only baseline repairs in those smokes: an unused binding rename and an
explicit dictionary annotation before keyword expansion. Assertions and
behavior are unchanged. Python 3.11 grammar and the stdlib API census are
checked; a real 3.11 runtime run on this author host is unmeasured because no
interpreter was found. No host installation is performed.
