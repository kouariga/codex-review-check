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
- Either fresh clean Codex approval or privileged per-finding decisions, as
  distinguished below. Neither path accepts active eyes or an unfinished review.
- For reaction acceptance, eyes observed during this lifecycle, or a changed
  summary if eyes was missed.
- The same clean evidence on two observations at least 30 seconds apart.

Existing thumbs-up and old-head summaries never supply fresh acceptance.
A new observation cannot retroactively accept an already-finished clean review.
Per-finding decisions name the full head and base revision, so a new observer cannot reuse them for a different diff.
The bounded recovery of an earlier legacy observation is described below.
Rebase/force-push/new head starts separate state. Missing, ambiguous,
failed, or unavailable evidence does not pass; no automatic review retry is sent.
Inline findings use GitHub's authoritative thread resolution state. Every thread
page is read through GraphQL and joined to REST comments by the root comment ID;
REST author IDs remain the bot identity check. Unresolved Codex threads block even
when outdated or attached to an older head. Missing thread mappings, partial API
responses, and incomplete pagination cannot produce acceptance.

Resolving threads alone is not approval. Earlier aggregate review messages,
including changes-requested reviews, can be superseded by fresh trusted clean
approval on the current head. The earlier review must have associated inline
findings and all their threads must be resolved. With explicit clean structured
metadata, review submission, root creation, and the root's last edit must precede
the earliest present review completion. Legacy manual evidence uses the completion comment's
creation as its boundary. Unthreaded review findings or missing timestamps remain
blockers. A new finding after clean completion blocks.
Resolved old-head findings must predate the earliest review completion's
whole-second timestamp. Same-second ordering is ambiguous. A later security
review cannot approve a code finding that arrived after Code Review completed.
Trusted priority-badge findings posted as plain issue comments have no reliable
reviewed SHA and block conservatively, including after a new push, until a
maintainer resolves that ambiguous evidence. The adapter does not delete comments.
Without structured metadata, Manual Code Review requires the trusted summary and
a trusted `Codex Review: Didn't find any major issues.` comment to resolve to the
exact current head. The comment must contain exactly one explicit reviewed commit.
A reaction alone cannot approve a legacy manual review. Unknown triggers block.

Legacy manual acceptance requires a new completion-comment ID and a fresh trusted
thumbs-up. The completion comment must precede the summary completion time,
which must strictly precede the reaction. Equal-second completion and reaction
timestamps are ambiguous and cannot supply acceptance. All evidence must follow the observation's
original start and must not have a future timestamp. Missing or malformed times,
findings, and head or base changes block. When more than one new clean completion
exists, only the latest by creation time is eligible; equal latest creation times
are ambiguous. An invalid newest completion cannot fall back to an older one.
The observer confirms the same comment digest, summary, and reaction IDs and
timestamps twice, at least 30 seconds apart.

Structured `codex-review-state:v1` metadata supplies review status and finding
flags. It still requires a fresh trusted root thumbs-up for clean approval.
The marker must contain exactly `code_review` and
`security_review` objects with `commit_sha`, `has_any_findings`,
`has_unresolved_findings`, and `status`. Code Review is required. An absent Security
Review has all four fields null. Advertised reviews must match the current full
head and human summary rows. Malformed, duplicate, contradictory, active, failed,
or unresolved metadata cannot fall back to apparently clean reaction evidence.
When every present review is completed with explicit false finding flags, manual
and automatic reviews use the same fresh-reaction path. Notification comments are
not parsed, so closing prose such as `Bravo.` or an absent notification does not
change acceptance. Resolved associated findings can be superseded under the
chronology above, including on the same head. Inline roots use their original
commit identity before any reanchored commit. Standalone and unassociated
findings remain blockers. Running, failed, or explicitly unresolved metadata cannot
use a notification to supply acceptance.

A completed Code Review may report both finding flags as null. The observer treats
those flags as unknown and requires a fresh trusted `codex-review-receipt:v1`
completion on the exact current head. Only the known clean receipt template is
accepted, including its reviewed commit and boilerplate. After the exact clean
assertion, the optional suffix may be `Keep it up!`, `Another round soon, please!`,
or one positive thumbs-up token (`:+1:`, `:thumbsup:`, or `👍` with an optional
variation selector and skin tone). Unknown prose, other reaction tokens, and
additional text are rejected. This normalizes equivalent positive reactions; it
does not accept arbitrary future closing sentences. Partial null flags,
unknown completed Security Review flags, and additional receipt text block.
The receipt follows the same start, restart, timestamp, and ambiguity checks as
manual completion. It cannot supply clean approval without a fresh trusted
thumbs-up. When any historical findings exist, every finding still needs a valid
current disposition and every inline thread must be resolved. That path can
produce only `verified_dispositions`, which does not require a thumbs-up.
Active structured review identity includes the status cell's start time, reviewed
commit, and trigger. A changed cycle excludes earlier receipts. An ordinary
summary update timestamp change during the same running cycle preserves a newly
arrived receipt. The newest eligible modern receipt must name the current head;
a newer wrong-head receipt blocks fallback to an older current-head receipt.

Finding comments require both creation and update timestamps. Substantive review
chronology requires submission and update timestamps. GitHub REST reviews omit
the update timestamp, so unknown-flags observations enrich Codex review rows from
the paginated GraphQL reviews connection, joining by database ID. Missing,
malformed, ambiguous, or unavailable chronology blocks acceptance.

The receipt must strictly follow every finding's creation or latest update and
its associated substantive review submission or update. Missing timestamps and
same-second ordering cannot establish that chronology. Its proof binds the
receipt digest as well as each decision. A receipt alone
with no findings and no fresh reaction remains pending.

When review completion timestamps are available, an automatic clean thumbs-up
must postdate the current completion, structured summary update, original observer
start, and any observed restart. A late API observation of an old reaction does
not make that reaction fresh. Structured summaries require reaction timestamps.
An observed running state, failure, or eyes reaction advances the restart boundary
and invalidates earlier thumbs. A same-head re-review that starts and remains
unobserved can leave an earlier success visible until an active signal or changed
summary is observed.
Reactions do not identify a review generation or emit workflow events. If Codex
keeps its old thumbs-up instead of replacing it, a new observation stays pending.
Older summaries without timestamps retain the
legacy reaction-ID and changed-summary checks. Reactions are read from the PR
root description, not from inline feedback.

A separate proof kind, `verified_dispositions`, accepts accountable handling of
feedback. It does not mean Codex gave a clean review. A repository writer,
maintainer, or administrator replies directly to each inline finding and resolves
its thread. Use this exact first line and these fields, replacing the example
revisions with the current full 40-character SHAs.

```text
Codex review: fixed
Head: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
Base: main@dddddddddddddddddddddddddddddddddddddddd
Reason: The transaction now checks the lease before saving.
Evidence: Commit URL and the test command or run URL showing the fix.
```

For an accepted decision not to change the code, use `Codex review: won't-fix`
and explain the reason and supporting evidence. `wont-fix` is also accepted.
`Reason` and `Evidence` may continue on subsequent lines. Field names are
case-sensitive, cannot repeat, and must be nonempty. The inline reply's parent
identifies the finding automatically. Do not add a `Finding` field inline.

A finding posted as a top-level issue comment or a substantive review body has
no inline thread to resolve. Post a top-level PR comment with the same fields
and one additional `Finding: issue:123` or `Finding: review:456` field, using
that finding's REST comment or review ID. Every finding needs its own decision.
Old aggregate JSON verification comments are not accepted.

Wait for the current Codex review to finish before posting decisions. Verify
all findings, including older resolved threads. Post a new unedited reply after
the latest review completion, summary update, and any change or non-decision follow-up
to that finding, including human context. Decisions must also follow every bot
review submission or update. Replies must bind the current head and base. A new commit or
base revision requires new replies after its review completes. Clicking Resolve
alone is insufficient. A running, failed, or unresolved review cannot pass.

After replying and resolving the threads, explicitly dispatch the caller
workflow for the PR. GitHub has no supported review-thread-resolution Actions
trigger. The example caller subscribes to `pull_request_review_comment` and
`issue_comment` creation, edit, and deletion, plus pushes via
`pull_request_target`. Those subscriptions must be installed on the trusted
branches before they can trigger. For a manual recheck, run:

```sh
gh workflow run codex-cloud-review.yml --repo owner/repo -f pr=123
```

The full inventory includes old and current inline findings, authoritative
resolution states and replies, priority-badge issue findings, and substantive
unthreaded reviews. Only the exact known Codex boilerplate wrapper is deduplicated
against its inline findings. Unknown or additional review text needs a decision.

For each finding, only the newest privileged recognized decision is eligible.
Malformed or edited newest decisions block older ones. The observer persists
per-finding reply ID boundaries, including across successful-check renewal.
Authorized edit and deletion events preserve the previous target as well as the
new target, so removing or retargeting a decision cannot restore an older reply.
A newer valid reply recovers it. Outsider comments cannot advance these boundaries.

Decision authors must have account type `User`. The collaborator permission
endpoint must currently grant write, maintain, or admin and return the same
immutable user ID. Author associations cannot authorize a decision. Permission
lookups are shared across the same author's comments within one snapshot and
rechecked in each new snapshot. A noncollaborator's 404 is ignored; other errors
cannot authorize. This checks authority and evidence bindings, not whether the
semantic judgment is correct or a human personally inspected the code. An agent
can publish a delegated audit under an authorized account.

Canonical fingerprints include substantive comment and review fields, immutable
user and app IDs, and thread resolution. Viewer-dependent author associations,
profile URLs, avatars, and reaction counters are excluded. The same content has
the same fingerprints under a user token and the workflow token. Two identical
observations at least 30 seconds apart are required. Changes to review evidence
reset confirmation; an observed new review cycle requires newer decisions.

Successful titles distinguish `Clean Codex approval confirmed on current head`
from `Feedback handling verified by maintainer attestation`. The latter explicitly
denies clean Codex approval. Check output records the proof kind, each finding's
reply ID, author ID and digest, plus a combined proof digest. Switching proof
kinds restarts confirmation. Clean Codex approval takes precedence when both
paths qualify. The workflow is event-driven, not a continuous monitor; changes
are detected on its next invocation.

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
structured review state or legacy summary/status advances this boundary; identical
active polls and structured-summary timestamp updates alone do not. Receipts
created after the first active observation remain eligible when seen before
summary completion. Receipts already present at that observation stay excluded. Completion
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
