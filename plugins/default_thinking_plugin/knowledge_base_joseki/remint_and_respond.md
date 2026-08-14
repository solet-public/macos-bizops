# Remint And Respond

Article Layer: 2

Article Role: joseki_catalog

Article Tags: planning-stage:post-approval, planning-stage:wbs-execution, evidence-category:joseki, domain:solet-lifecycle, domain:platform-operations


JOSEKI_KEY: remint_and_respond
DESCRIPTION: The full release cycle for a seed repository that already has adopters: read the published predecessor's provenance stamp without cloning, assemble from the landed ref with that predecessor so the lineage chains, census the assembled bundle by asserting every release-critical path BY NAME, seal it, put it through the born-clone publication gate, publish the sealed commit, cut a release from the notes entry, and — in the same session, never deferred — close the outstanding feedback items citing the landings that fixed them. The successor to mint_seed: mint_seed produces and publishes a bundle, this card runs a release to people who are waiting on one. Use for every update to a live seed repository. Not for a first mint with no predecessor and no audience.
EMBEDDING_DESCRIPTION: Ship an update to a seed repository that people are already running, and answer them in the same pass. Read the currently published provenance stamp straight from the remote so the new build chains onto it, build from a committed reference, then check the built folder by name for every file the release depends on rather than eyeballing a file count. Seal it, prove a fresh clone of it can pass its own checks on a machine carrying only what the bundle guarantees, publish, cut a release from the written notes, and close the reported issues with the specific changes that fixed them cited. Publishing and answering are one act here, because a release nobody is told about is not a response.

## Input Contract

- An existing published seed repository with adopters, whose current head carries a readable provenance stamp
- A landed, committed ref carrying everything the release claims, and a capability selection (a named bundle or an explicit plugin list) for the assemble
- A written release-notes entry for this update, and the outstanding feedback items this release answers, each with the landing that fixed it identified
- A census list: every release-critical path this update depends on, named exactly as it must appear inside the bundle
- Bindings: predecessor_stamp_path, bundle_name, output_dir, ref, repo_name, owner, visibility, census_paths, release_notes_entry, feedback_items

## Output Contract

- A sealed bundle whose provenance chains onto the published predecessor's stamp, with the census recorded as an assert-by-name result rather than a count
- A born-clone gate verdict keyed to that bundle's sealed sha, written only on a pass — its absence is a refusal, and publish re-verifies it
- The sealed commit published to the target seed repository as a create or an append-only re-mint, plus a release cut from the notes entry on that repository
- Every outstanding feedback item this release answers, closed in the same session with the landing that fixed it cited, and the release notes standing as the response surface

## Sequence

[ ] 1. Read the published predecessor's provenance stamp without cloning
    a) Fetch the provenance stamp from the published seed repository's current head over the repository API and write it to a scratch file, so the lineage source is what adopters actually have rather than what a local folder remembers [agent-executed: ambient repository authority, no clone, no token into the runtime]

[ ] 2. Assemble the bundle from the landed ref, chaining the predecessor
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Assemble a clean seed bundle from the committed ref, declaring the predecessor stamp (plugin::seed_factory_plugin::assemble_seed)
        Arguments:
        {"bundle_name": "<<BIND:bundle_name>>", "output_dir": "<<BIND:output_dir>>", "ref": "<<BIND:ref>>", "predecessor": "<<BIND:predecessor_stamp_path>>"}

[ ] 3. Census the assembled bundle by asserting every release-critical path by name
    a) Assert the presence of EVERY release-critical path inside the assembled bundle, by exact path, and fail the mint on the first absence [agent-executed: an assert-by-name list; a file count or an eyeballed listing is not a census]

[ ] 4. Validate and seal the assembled bundle
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Re-validate the actual folder and seal it into a fresh neutral-identity commit (plugin::seed_factory_plugin::validate_and_seal_seed_bundle)
        Arguments:
        {"output_dir": "<<BIND:output_dir>>", "expected_ref": "<<BIND:ref>>", "manifest_hash": "<<BIND:manifest_hash>>"}

[ ] 5. Run the born-clone publication gate against the sealed bundle
    a) Birth a throwaway clone of the sealed bundle under a short workdir and run its own shipped register twice — once under the declared minimum environment, once unconstrained — then write the verdict keyed to the sealed sha only if it passed [agent-executed: the gate lives with the seed factory source; the verdict is a minting-side sidecar, never written into the bundle]

[ ] 6. (Public only) Obtain explicit confirmation before publishing publicly
    a) Obtain an explicit operator order for public visibility; private is the default and is never overridden by silence [agent-executed: a human consent gate, skipped entirely for the private default]

[ ] 7. Publish the sealed commit to the seed repository
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Publish the sealed commit — create the repository or append a re-mint commit to the existing seed repository (plugin::seed_factory_plugin::publish_seed)
        Arguments:
        {"repo_path": "<<BIND:repo_path>>", "sealed_commit_sha": "<<BIND:sealed_commit_sha>>", "repo_name": "<<BIND:repo_name>>", "owner": "<<BIND:owner>>", "visibility": "<<BIND:visibility>>"}

[ ] 8. Cut the release from the written notes entry
    a) Create the release on the published repository from this update's release-notes entry, tagged per the release convention, so adopters who subscribe to releases are actually told [agent-executed: ambient repository authority on the published repository]

[ ] 9. Close the feedback items this release answers, in the same session
    a) Close each outstanding feedback item this release answers, citing the specific landing that fixed it, and let the release notes carry the full response [agent-executed: the response half of the act, never deferred to a later session]

## Expected Step Count

9 steps for a full remint-and-respond cycle; step 6 is skipped for the private default.

## Binding Guidance

- Bind `predecessor_stamp_path` to the stamp fetched in step 1, read from the PUBLISHED head rather than from a local copy of a previous mint. The predecessor input may point at a seed directory or at the stamp file itself; a missing or malformed stamp fails closed rather than quietly minting an unchained lineage.
- Bind `bundle_name` (or an explicit plugin list — exactly one of the two) and `ref` to the LANDED commit that carries everything this release claims. Assemble reads the committed ref, never the working tree, so anything still dirty is simply absent from the bundle and any claim about it in the notes is false.
- Bind `census_paths` to the paths this release actually depends on, and assert each by exact name inside the assembled bundle. The class this catches is a runbook step that names a file the copy allowlist never shipped: the instruction resolves for whoever wrote it and resolves to nothing in an adopter's clone, and no gate that runs on source can see it.
- Bind step 4's `output_dir` to step 2's returned bundle path, `expected_ref` to step 2's `ref`, and `manifest_hash` to step 2's returned manifest hash — all from the same run, so the seal's provenance describes what was actually assembled. A manifest hash identical to the predecessor's where a payload change was expected is the tell that the assemble ran against stale deployed policy.
- Bind step 5's workdir to a SHORT path. The gate refuses a workdir too long to hold a unix socket, and it refuses up front rather than letting socket-binding entries fail and be reported as bundle defects.
- Bind step 7's `repo_path` and `sealed_commit_sha` to step 4's returned values — same-run provenance, mirroring the step 2 to step 4 binding rule. Bind `repo_name` to one of the two accepted forms: the dated-artifact convention, or the durable-home form, a stable kebab-case product name for a long-lived seed repository that accumulates releases. Underscores always require the full dated form, so a typo'd dated name still fails loud instead of passing as a stable one. It is the product name, never the solet's own name.
- Bind `visibility` to private unless step 6 produced an explicit order for public. There is no default-public path.
- Bind `feedback_items` to the items this release actually answers, each paired with the landing that fixed it. Verify each landing is an ancestor of the main branch before writing it into a closure — a closure citing work that is not on the branch is a false report to the person who raised it.

## Coherence Obligations

- Publish and respond are ONE act. The response step runs in the same session as the publish, never queued for a later one: a release that ships without its closures leaves the people who reported the defects with no disposition, and "we will answer next session" reliably becomes silence. The release notes are the response surface; the item closures point at them.
- Census failure means no mint. A missing release-critical path is not a note in the release notes and not a follow-up item — it is a stop, because the release's own instructions would then name a file the adopter does not have. Re-assemble after fixing what ships, and re-census.
- A census is an assertion by name, not a count. Counting files, eyeballing a listing, or confirming that the directory "looks right" cannot distinguish a bundle missing exactly the one path this release depends on from a bundle that has it.
- The gate verdict is keyed to the sealed sha and never transfers. A verdict for a different sha is a verdict about a different artifact; absence of a verdict is a refusal, not a neutral state, and publish re-verifies it rather than trusting a verdict handed to it. Passing a gate once does not authorize a later, differently sealed bundle.
- The gate's environment is the whole point: the throwaway runs under what the bundle itself declares an adopter needs, not under the minting machine's own environment. A leaner minting host yields a stricter verdict, never a looser one — so a mint that blocks where a previous one published is that property working, and the reference environment is what to check before calling it a regression.
- Never work around a publish refusal with hand-run repository commands. Each refusal is a distinct gate doing its job: a name matching neither accepted form, a repository that is not factory-sealed, an unsealed bundle, a lineage that does not chain onto the published head, a visibility that differs from the request. Updates are append-only and fast-forward; there is no force path and no clobber path.
- Nothing in the shipped release identifies a person. Adopter reports are credited by their substance, not by handle; issue text from an adopter is re-derived generically before any of it reaches a shipped surface, and machine paths, deployment names, and employer or policy names never cross into release notes or knowledge-base content.

## Next Joseki

Explicitly absent — a completed release ends the cycle, and what follows depends on what the next change turns out to be. The predecessor card is `mint_seed`, which covers the narrower act of producing and publishing a bundle with no adopters to answer; this card supersedes it whenever the seed repository already has them.

## Repair Joseki

Explicitly absent as a card. Each stage fails closed in its own way and none of them is repaired by re-running the whole ladder. An assemble failure names its own cause and leaves no partial bundle — read the code and re-run. A census failure is fixed in what ships, then re-assembled and re-censused; it is never waived. A failed seal leaves the target untouched: fix the source, re-assemble, re-seal. A gate that could not run is not a gate that failed — a workdir refusal or a bundle with no usable register means the verification never happened, so publication stays refused until it does. A failed publish is one code per refusal, and transient network codes are safely re-invoked because an identical re-mint short-circuits as unchanged. A failure AFTER a successful publish — release creation or a closure — is finished in the same session rather than deferred: the artifact is already in adopters' hands and the response is what is missing.
