# TODO Gateway PR #185 reconciliation capability

This capability is dormant by default. `governed_reconciliations` is empty in the
versioned policy and no installation or host activation is implied by this file.

## Scope

The helper `scripts/reconcile_todo_gateway_pr185_main_governed.py` is limited to:

- repository `ericson-j-santos/chatgpt-operational-rules`;
- branch `codex/todo-gateway-ci-matrix` at reviewed head
  `68b9098e4348495045c24e92bba8e6b912f6b3a9`;
- merge base `d6af41001bf641727a35a990e7c93b4c757538cb`;
- the three reviewed functional blobs embedded in the helper; and
- host `DESKTOP-RP23OGS`.

It rejects an unexpected PR path, a differing overlap with main, redirected paths,
symlink index entries, an unclean worktree, ref drift, a changed origin, or an
unreviewed proposal.

## Review artifact and deterministic output

The final main SHA is deliberately not compiled into the capability. After this
capability itself is merged, a separate review must create one immutable JSON
proposal. The proposal fixes the final main commit and tree, manifest timestamp,
commit timestamp, file count, manifest blob, combined tree, and merge commit.
Its SHA-256 and absolute path must then be installed in exactly one
`governed_reconciliations` record together with the exact script, repository path,
the script SHA-256, and confirmation phrase. This binds execution to the final
approved main and reviewed helper bytes without allowing an arbitrary moving
`main` or in-place script replacement.

This capability PR intentionally carries the already-reviewed generator bootstrap
and its regression from PR #185 because the canonical manifest cannot otherwise run
under the installed embeddable Python. Once this PR is integrated, those two blobs
will already be present on main. The later combined tree can therefore differ from
that final main only in the workflow and canonical manifest even though the source
PR provenance still contains four changed paths. Identical reviewed overlaps are
accepted; a differing overlap is rejected. No file count such as 221 and no old
main SHA may be reused: the post-integration proposal must pin the then-current
count and main tree. Any pre-integration proposal fails the remote-main and tree
readbacks.

The helper builds the candidate with a temporary Git index, uses the approved
timestamp instead of the clock, fixes commit identity and time, and verifies both
parents and every expected object ID before changing a ref or worktree.
Candidate construction can leave unreachable blob, tree, or commit objects in the
Git object store when an expected object ID fails after object creation. Those
objects do not update refs, the index, or the worktree, but they are residual
repository state. The capability reports the failure and does not prune or delete
objects as part of recovery; it must not describe that outcome as zero state change.

## Execution and publication semantics

Future execution requires a separate human approval for both policy installation
and the exact proposal, explicitly covering both local reconciliation and remote
publication. The Gateway admits the helper only at risk 2. The proposal also pins
the exact Git executable path and SHA-256. The helper rejects inherited Git
redirection or trace variables, unsafe fsmonitor/filter/hook configuration,
ambiguous fetch or push URLs, and non-GitHub destinations. Every Git invocation
sets `http.followRedirects=false`. The helper fast-forwards the local branch with hooks
disabled through an empty, verified hooks directory, then checks HEAD, tree, index,
and worktree before a plain fast-forward push. The same empty hooks directory is
enforced for push. It never uses force push.

The remote comparison is **not atomic** (`remote_compare_atomic=false`): two remote
reads and a normal push are not a remote compare-and-swap transaction. Git's
fast-forward check still rejects a branch that advanced outside the proposed
lineage. A failed publication must be reported as incomplete, not retried through a
different transport.

Replay is accepted only when the exact proposed commit, parents, tree, local clean
state, and remote state are all observed. A local expected commit with the old
remote head is `publication_pending`, not success. A timeout or local push error is
classified by remote readback: unchanged remote is `publication_incomplete`, an
unexpected remote is `publication_outcome_unknown`, and an accepted commit is
reported as `RECONCILED_AND_PUBLISHED_AFTER_READBACK`. Main is reread immediately
before and after push; a concurrent main change is reported as a partial outcome.
No SQL, service, gateway probe, Render,
Neon, Docker, or physical TODO Gateway inventory is part of this capability.
