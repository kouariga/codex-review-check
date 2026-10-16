# Codex Review Check

Observe an existing Codex Cloud review and publish `codex-cloud-review` on the
exact PR head. The adapter never requests a review, changes application code,
resolves discussions, or merges PRs. Initial deployment is report-only.

## Install

After the first tested `v1` release is published, copy [examples/caller.yml](examples/caller.yml)
to `.github/workflows/codex-cloud-review.yml` in each caller repository.
Install on its default branch for comment/dispatch events and on any other
trusted target branches used for `pull_request_target` events. Review permissions
before enabling. A new adapter cannot observe its own installation PR before
trusted target/default branches contain the caller.

The caller uses this public reusable workflow with its own repository-scoped
`GITHUB_TOKEN`. It grants `contents`, `issues`, and `pull-requests` read and
`checks: write`. No PAT, OpenAI key, source checkout, or inherited secrets are
needed. Private callers do not publish their code into this repository.
`checks: write` is repository-wide capability; the implementation limits writes
to its observation check but token permissions cannot restrict a check name.
Organization Actions policy must allow this reusable workflow and its caller's
`pull_request_target` event. Public callers need an applicable event policy that
permits `pull_request_target`: GitHub's default public-repository policy begins
enforcement on November 2, 2026. Private/internal callers are exempt from that
default policy. Review [GitHub's event-policy guidance](https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target#default-policy-for-pull_request_target)
before installing; this adapter does not change security policies. The public
reusable-workflow source itself only uses `workflow_call`, not this PR event.

Compatible releases update the moving `v1` reference centrally. Callers need no
update PRs for those releases. Event subscriptions, permissions, or breaking
interface changes may still require caller changes. A full commit SHA can be
used instead when per-repository adoption control is preferred.

## Evidence and limits

Every 30 seconds, for at most 20 minutes, observe trusted Codex metadata:

- Summary author ID `199175422`, GitHub App ID `1144995`. Reactions use the
  same immutable author ID; GitHub can report their `user.type` as `User`.
- All observed review rows completed and their commit IDs resolved to the
  current full head SHA; no unresolved Codex review threads.
- The same target branch and base revision throughout observation. Retargeting
  or base advancement invalidates persisted acceptance on the next observation.
- Either fresh clean Codex approval or a privileged verification receipt, as
  distinguished below. Neither path accepts active eyes or an unfinished review.
- For reaction acceptance, eyes observed during this lifecycle, or a changed
  summary if eyes was missed.
- The same clean evidence on two observations at least 30 seconds apart.

Existing thumbs-up and old-head summaries never supply fresh acceptance.
A new observation cannot retroactively accept an already-finished clean review.
A verification receipt has its own durable, exact-diff evidence binding.
The bounded recovery of an earlier legacy observation is described below.
Rebase/force-push/new head starts separate state. Missing, ambiguous,
failed, or unavailable evidence does not pass; no automatic review retry is sent.
Inline findings use GitHub's authoritative thread resolution state. Every thread
page is read through GraphQL and joined to REST comments by the root comment ID;
REST author IDs remain the bot identity check. Unresolved Codex threads block even
when outdated or attached to an older head. Missing thread mappings, partial API
responses, and incomplete pagination cannot produce acceptance.

Resolving threads alone is not approval. Earlier aggregate review messages,
including changes-requested reviews, can be superseded only by a fresh trusted
manual clean completion on the current head.
The earlier review must have associated inline findings and all their threads
must be resolved. Its submission must strictly precede the manual completion
comment's creation. Unthreaded review findings
or missing timestamps remain blockers. A new finding after clean completion blocks.
Resolved old-head findings must predate the earliest review completion's
whole-second timestamp. Same-second ordering is ambiguous. A later security
review cannot approve a code finding that arrived after Code Review completed.
Trusted priority-badge findings posted as plain issue comments have no reliable
reviewed SHA and block conservatively, including after a new push, until a
maintainer resolves that ambiguous evidence. The adapter does not delete comments.
Manual Code Review is accepted when the trusted summary and a trusted
`Codex Review: Didn't find any major issues.` comment each resolve to the exact
current head. The comment must contain exactly one explicit reviewed commit.
A reaction alone cannot approve a manual review. Unknown triggers block.

Manual acceptance requires a new completion-comment ID and a fresh trusted
thumbs-up. The completion comment must precede the summary completion time,
which must strictly precede the reaction. Equal-second completion and reaction
timestamps are ambiguous and cannot supply acceptance. All evidence must follow the observation's
original start and must not have a future timestamp. Missing or malformed times,
findings, and head or base changes block. When more than one new clean completion
exists, only the latest by creation time is eligible; equal latest creation times
are ambiguous. An invalid newest completion cannot fall back to an older one.
The observer confirms the same comment digest, summary, and reaction IDs and
timestamps twice, at least 30 seconds apart.

Structured `codex-review-state:v1` metadata constrains lifecycle consistency. It
never supplies approval. The marker must contain exactly `code_review` and
`security_review` objects with `commit_sha`, `has_any_findings`,
`has_unresolved_findings`, and `status`. Code Review is required. An absent Security
Review has all four fields null. Advertised reviews must match the current full
head and human summary rows. Malformed, duplicate, contradictory, active, failed,
or unresolved metadata cannot fall back to apparently clean reaction evidence.
Automatic clean approval requires no reported current-head findings, even when
those threads are resolved and structured metadata says there are no findings.
Inline roots use their original commit identity before any reanchored commit.
Resolved old-head roots can precede a clean current-head review, but roots created
at or after completion, or without a known commit or creation time, block automatic
approval. Standalone unthreaded findings remain blockers regardless of head.
A later explicit manual clean review can supersede earlier associated resolved
findings under the chronology above.
Manual evidence is parsed even when structured metadata is present.

When review completion timestamps are available, an automatic clean thumbs-up
must postdate the current completion, structured summary update, original observer
start, and any observed restart. A late API observation of an old reaction does
not make that reaction fresh. Older summaries without timestamps retain the
legacy reaction-ID and changed-summary checks. Reactions are read from the PR
root description, not from inline feedback.

A separate proof kind, `verified_dispositions`, accepts accountable handling of
feedback. It does not mean Codex gave a clean review. A repository writer,
maintainer, or administrator publishes an unedited PR issue comment beginning
with `<!-- codex-review-dispositions:v1 `, followed by JSON and ` -->`. An audit
explanation can follow the marker. JSON has exactly these fields.

```json
{
  "version": 1,
  "repository": "owner/repo",
  "pr": 123,
  "head": "<current full SHA>",
  "base": {"ref": "main", "sha": "<current full base SHA>"},
  "review": "<snapshot review_digest>",
  "inventory": "<snapshot inventory_digest>",
  "dispositions": [
    {"finding": "inline:456", "outcome": "fixed", "rationale": "Verified explanation", "evidence": ["Evidence URL or command result"]}
  ]
}
```

Each inventory key appears exactly once. `fixed` and `wont_fix` both require a
nonempty rationale and nonempty evidence list. The read-only `snapshot` exposes
canonical inventory rows and both digests. Only the exact known Codex boilerplate
wrapper is deduplicated against its inline findings. Additional or unknown
review-body text needs a separate disposition, even when inline findings also
exist. Inventory includes old and current
head findings, resolved and unresolved inline roots, their replies and authoritative
resolution state, priority-badge issue findings, and substantive unthreaded review
findings. Recognized boilerplate reviews with inline roots are bound in the review digest
without requiring a duplicate disposition. The review digest binds the trusted
summary identity, body, update time, and bot review submissions.

The receipt author must have account type `User`. The collaborator permission
endpoint must currently grant write, maintain, or admin and return the same
immutable user ID. Login spelling and author association cannot authorize a
receipt. This validates authority and evidence bindings. It does not prove that
a human personally inspected the code or that an attested semantic judgment is
correct. A maintainer can delegate an audit and publish its attributable result.

The receipt must be unedited, strictly newer than review completion and summary
update, and not in the future. It can predate observer startup because its full
head, base, review, and inventory bindings are revalidated. All Codex inline
threads must be resolved and no review may be running, failed, or unresolved.
The same proof is checked twice at least 30 seconds apart. A changed finding,
reply, resolution, review cycle, head, or base invalidates it. A new observed
review cycle requires a later receipt. Resolved flags alone remain insufficient.

Only the latest privileged candidate is eligible. Malformed or edited newest
receipts cannot fall back to an older one. The observer persists the largest
privileged receipt ID seen, and authorized marker edit/deletion events advance
that boundary. Removing a newer receipt cannot resurrect an older receipt.
Renewing a successful check preserves newly observed receipt and review-restart
boundaries while clearing candidate confirmation and rebaselining reactions.
Unprivileged marker spam is ignored, including collaborator-permission 404s for
noncollaborators. Other permission API errors cannot authorize.
A new receipt after that boundary is needed to recover. Old successful checks
without an explicit proof kind cannot be renewed as though they had this proof.

Successful check titles distinguish `Clean Codex approval confirmed on current
head` from `Feedback handling verified by maintainer attestation`. The latter's
summary explicitly says it is not clean Codex approval. Check output persists
the proof kind, receipt identity, author ID, and proof digest. Switching proof
kinds starts another two-observation confirmation. When both paths qualify,
clean Codex approval takes precedence over the maintainer receipt.

An existing observation that could not initialize can recover a manual review
completed after that observation started. Recovery preserves the original start
when renewing the 20-minute deadline. For legacy failed checks that lack a stored
origin, recovery walks consecutive GitHub Actions observation records in descending
check-ID order. Each record must be uninitialized, failed, and match the exact head,
external ID, state version, and base identity. Start times must be finite, nonnegative,
not in the future, and nonincreasing while walking backward. An initialized, successful,
mismatched, malformed, or nonmonotonic record stops the walk. A stored origin is
preserved except for the narrowly verified migration described below. This recovers an original start
lost by legacy retries without crossing another review lifecycle.
An initialized legacy observation can also recover when every consecutive failed
record has the exact original state shape: an empty reaction baseline, the digest
of an absent summary, no observed eyes, and no candidate. Only the start time may
change. Any lifecycle, identity, malformed-state, or ordering boundary stops recovery.
This path preserves the empty reaction baseline and still requires a new reaction;
it does not enable the uninitialized path's reaction-baseline exception.

The first manual-evidence release could promote this virgin legacy state by storing
the most recent retry time as its origin and the already-completed comment IDs as
its manual baseline. Recovery recognizes only that exact mechanical promotion of
its immediate legacy predecessor, with no extra fields, active lifecycle, or
candidate. The current trusted manual comment IDs must match the promotion's
baseline, and their update times and summary update time must precede the promotion.
Only this verified migration can replace a stored origin and discard its
migration-created manual baseline. Ordinary new observations and states that
observed a review restart cannot rewind their origins.

Recovery cannot accept a review completed before observation began. Observing any running or failed review ends recovery and
records any available manual completion-comment IDs as its baseline. Subsequent manual
evidence must follow the first observation of that active review state. A changed
summary or status advances this boundary; identical active polls do not. Completion
resets the active-state identity so a later restart advances the boundary again. Older initialized
state without these IDs also records a baseline before accepting manual evidence.
This prevents an earlier completion comment from approving an observed same-head
restart. Unobserved overlapping same-head reviews cannot be fully distinguished
without an upstream lifecycle ID; multiple eligible comments fail closed.

State lives in the latest owned check output. Code Review must be present;
Security Review alone cannot approve the check. Per-PR job concurrency, after narrow event filtering, serializes
observations. Ordinary comments do not enter that queue. Receipt creation, edits,
and deletions qualify only when the event sender currently has repository write
authority, checked before any check write. Marker removal uses the previous
comment body supplied with an edit event. A restart resumes an incomplete run. A new authorized invocation creates a new
run when renewing a completed lifecycle, preserving the original reaction baseline
and clearing candidate confirmation. Completed runs remain history; only the
latest owned run supplies resumable state, except for the bounded legacy origin
recovery described above. Multiple active runs block as ambiguous.
Failed or expired attempts get a renewed deadline. Each attempt is
still bounded to 20 minutes and recovery requires two stable observations. GitHub may coalesce pending runs; the observer reads the latest live head.
Same-head review restarts invalidate previous success when running evidence is
observed. A head or base change during evidence collection is rejected. The caller
subscribes to `edited` to observe retargeting. Base-branch pushes alone may not
produce a caller event, so base advancement is detected on the next observation;
this adapter cannot attest an unobserved base update. A change after
the last API read can briefly race check publication; the check is still bound
to the captured SHA and subsequent events re-evaluate evidence.

This is a practical lifecycle observation, not a cryptographic review attestation.
Reaction propagation and webhook delay remain limitations. Only review types
present in the evolving summary format are observed; a missing Security Review
row does not prove a security review ran. Unsupported summary formats block.
Check creation uses the GitHub Actions App, not a dedicated adapter App; other
trusted writable workflows share that identity. Do not execute untrusted PR code
with this write token. Do not make this report-only check required until live
behavior and repository policies have been validated.

Authenticated conditional GETs reuse unchanged responses (304) without consuming
GitHub's primary REST quota; identical check outputs are not repeatedly written.
A denied/rate-limited API response (403/429) stops the job without retrying.
After the API recovers, a later authorized event can resume observation. The
last published check may remain unchanged when GitHub refuses writes; inspect
the failed observer job rather than treating old check evidence as a new pass.

## Development

`tools/codex_review_gate.py` is the canonical implementation. The generator embeds
it once in the central reusable workflow, avoiding checkout of caller code and
version mismatches between the workflow and adapter. Do not edit generated code.

```sh
python3 tools/render_codex_review_gate.py .github/workflows/review.yml
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Tests cover freshness, head changes, failed/unavailable evidence, restart,
repository isolation, same-head reruns, and the exact generated workflow.
CI runs tests with read-only permissions. No live check write is part of unit tests.

## Release and rollback

1. Open an implementation PR; run tests and obtain required review on its exact head.
2. Merge only after those requirements pass. Record the tested commit SHA.
3. Validate an explicitly authorized report-only caller against that SHA; inspect
   pending, two-observation success, and head binding. Do not request paid reviews
   or push empty application commits merely to exercise the adapter.
4. Create an immutable version tag such as `v1.0.0` at the validated commit, then
   create or move `v1` to the same commit. Only maintainers release this pointer;
   merging main does not automatically publish a stable release.
5. Record old/new SHAs. To roll back, move `v1` to the prior tested release.
   Future runs resolve the restored version; already-running jobs do not change.
   Re-running only failed/specific jobs can retain the original workflow SHA,
   so start a fresh authorized observation when validating rollback.

Moving `v1` trades per-repository update work for simultaneous central rollout.
Keep the write-capable workflow small and review release changes carefully.
There is no release daemon, App backend, or new credential to operate.

See GitHub's [reusable workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows)
and [caller permissions/context](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations).

This observer validates during authorized workflow invocations. It is not a
continuous monitor after a run ends. Receipt marker events invalidate stored
proof when delivered, and later invocations revalidate live evidence. The shared
`v1` release is unchanged until its owner explicitly adopts a reviewed release.
