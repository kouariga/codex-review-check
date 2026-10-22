# Codex Review Check

Observe an existing Codex Cloud review and publish `codex-cloud-review` on the
exact PR head. The adapter never requests a review, changes application code,
resolves discussions, or merges PRs. Initial deployment is report-only.

## Install

After the first tested `v1` release is published, copy [examples/caller.yml](examples/caller.yml)
to `.github/workflows/codex-cloud-review.yml` in each caller repository.
Install on its default branch for `issue_comment`, `workflow_dispatch`, and
`pull_request_target`. `pull_request_review` and inline
`pull_request_review_comment` events use the PR merge-ref workflow, which must
also contain the subscriptions. Review permissions
before enabling. An unmerged installation PR cannot replace the default caller.

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

## Required native Actions job

Required mode supports same-repository PRs except Dependabot PRs. It rejects fork,
Dependabot, and missing-source-repository payloads before evaluating acceptance.
Their review-event tokens cannot invalidate a successful native job. A trusted
writable relay is required before this mode can support those PRs.

Use a dedicated review workflow with no jobs depending on the native gate through
`needs`. GitHub reruns the selected job and its dependent jobs, so downstream
build, publish, or deployment jobs must stay in separate workflows.

Use strict up-to-date branch protection with required mode. Base-branch pushes
do not start this observer, so loose required checks cannot detect an unobserved
base update.
If a saved observation for the same head has a different base, acceptance fails
and retains the original base across retries. Update the PR branch to a new head
before relying on a review of the new diff. The structured marker does not report
a reviewed base, so a same-head rereview cannot clear this observed base mismatch.
The adapter cannot detect historical base changes that no observation recorded.

Merge queues are unsupported. Do not configure this gate as a required merge-queue
check. Reviews bind to a PR head and base, and do not attest the synthetic
`merge_group` commit. Queue support needs a separate review contract.

For enforcement, define a native job named `codex-cloud-review` in a trusted
`pull_request_target` caller. Its step uses `kouariga/codex-review-check@<reviewed-commit-sha>`
as a composite action, with the same repository permissions shown above. Do not
check out or execute PR code. Required mode rejects `pull_request` because that
event lets the PR replace its own workflow definition.

The action binds the event head to the live PR, polls the existing acceptance
rules, and exits zero only for explicit acceptance. Unfinished reviews,
unresolved threads, stale identity, API errors, and timeouts do not succeed.
It persists retry state in `codex-review-evidence`, including migration from
owned legacy observations. The native job supplies the required check result.
To re-evaluate this native job after comment events, pass `required-job:
codex-cloud-review` to the reusable workflow. Grant `actions: write` to that
observer job, alongside `checks: write` and the existing read permissions. The reusable workflow
inherits its caller's permissions. Without this input, it retains legacy
report-only behavior and needs `checks: write` instead.
Native callers must also subscribe to `pull_request_review` with `submitted`,
`edited`, and `dismissed` so review-body changes trigger re-evaluation. Reviews
use the same immutable Codex author ID as the snapshot evaluator.

Thread resolution changes do not trigger Actions. After resolving threads,
manually dispatch the caller workflow to re-evaluate the native job. Reopening
a resolved thread after a successful check does not automatically invalidate
that result. Automatic handling of this case is outside the supported contract;
do not treat a completed check as a continuous guarantee of thread state.
Retargeting a PR while an older `pull_request_target` run is queued is also
unsupported. That run is not bound to the event's original base. Cancel stale
runs and start a new run for the current target before relying on its result.

For each authenticated Codex update or authorized disposition, the observer
finds the latest native job for the current PR head and base. It verifies the
job's check, original `pull_request_target` event, PR identity, and caller
workflow path before requesting a job rerun. GitHub queues a new attempt in the
original Actions suite to re-evaluate the required job. The native
action then applies the existing acceptance rules to current review evidence.
If the latest native attempt started after this observer's server timestamp and
any saved feedback event, that attempt already covers the update. The observer
uses it without another rerun. Equal-second timestamps require another attempt.
After a competing rerun, the observer rechecks only when GitHub confirms the
owning run's attempt number advanced. Other API denials remain failures.
An active workflow is allowed to finish before the observer requests the job rerun. This
covers a job that was about to succeed using evidence from before the update.
Each opted-in observer has its own concurrency group so later updates cannot
replace a pending feedback event. Before waiting for the native job, the observer
appends authorized disposition edits and deletions to `codex-review-feedback-event`.
The native evaluator reads these records for the current PR head and base and
keeps the highest decision ID for each finding. An active native job cannot
overwrite these records, and an older reply cannot become valid again when a
newer decision disappears. The observer creates no legacy `codex-cloud-review`
check in this mode.

Pin both paths to tested commits when using per-repository adoption. The trusted
default caller must be updated before its installation PR can exercise this native
job or the updated observer. Verify the
actual merge box after adoption, including a review restart on the same commit.
Invalidation waits for comment delivery, runner availability, and any active
native job to finish. The observer waits up to 30 minutes for a completed native
workflow and has a 35-minute job limit. This leaves time for the native job's
20-minute observation window and 25-minute job limit.
GitHub limits reruns to runs less than 30 days old and at most 50 attempts.
An unavailable job, denied rerun, or exhausted limit fails the observer visibly;
it cannot revoke an old result without a successful rerun request. A new PR
commit creates a new native run when an existing run cannot be rerun.

An ordinary required status check selects a check name and its GitHub App. It
does not bind that check to a specific trusted workflow. The rerun observer
verifies workflow provenance, but these changes do not configure a workflow-bound
branch ruleset. A PR author whose workflows execute can create another Actions
job with the same name, including a fork author allowed by workflow approval
policy. An author with a writable Actions token can also forge auxiliary checks
or saved state. Merge-box acceptance of those checks is unverified here.

## Evidence and limits

Every 30 seconds, for at most 20 minutes, the adapter reads the trusted Codex
summary and the complete finding inventory. The summary must have author ID
`199175422` and GitHub App ID `1144995`. It captures the full PR head SHA and base
identity at startup, checks both after collection and immediately before success,
and publishes only to the check created for that captured head.

Automatic clean acceptance uses one trusted `codex-review-state:v1` JSON marker.
For every required review, the same object must contain:

- `commit_sha` equal to the captured full 40-character PR head SHA.
- `status` equal to `"completed"`.
- `has_any_findings` equal to the boolean `false`.
- `has_unresolved_findings` equal to the boolean `false`.

Code Review is mandatory. The marker must contain exactly `code_review` and
`security_review`, each with these four fields. Security Review is optional only
when all four of its fields are null and the summary does not advertise Security
Review. A populated Security Review must also satisfy the clean rule, even when
there is no display row. An all-null object never counts as a successful review.
Missing or null finding flags do not mean false.
Scope detection normalizes table indentation, optional leading pipes, HTML
entities, tags, and Markdown labels. A table label that mentions security
conservatively requires Security Review metadata. Ordinary prose and link targets
do not advertise review scope.

The adapter trusts Codex's reported reviewed SHA. This is observed, undocumented
Codex output, not a verified stable public API contract. An unsupported version,
duplicate marker or JSON field, malformed value, contradictory flag, or ambiguous
trusted summary cannot pass. Missing or unavailable metadata retries until the
20-minute deadline and then fails. A result for another SHA or a running, queued,
or pending review waits. An explicit failed review fails the current attempt.
Findings block automatic clean acceptance and remain distinct from unavailable
results. Terminal non-success evidence checks use `failure`, never `neutral` or
`skipped`. The native required job exits nonzero unless acceptance succeeds.

A PR-level 👍 is informational for structured clean acceptance. Its absence, age,
identity, or timing does not authorize or veto a structured verdict. An old 👀
also cannot veto it. Markdown wording, table layout, display status, display
timestamps, and the summary's update time do not establish the structured verdict.
An observer that starts or restarts after completion can accept an existing result
for the captured head. This removes the reproduced false rejection where an older
running-summary read and a newer thumbs-up read advanced a freshness boundary past
that thumbs-up. The original incident's API responses were not retained, so that
precise historical interleaving is not established.

Two observations at least 30 seconds apart must agree on the canonical metadata,
head and base, and independent finding and review inventories. Cosmetic summary
edits and reaction changes do not reset that confirmation. Check output records
the expected SHA, comment ID, reported SHAs, statuses, finding flags, observation
time, and decision reason. Observation time is diagnostic for this path and does
not identify the reviewed commit. Malformed metadata reports a bounded diagnostic
without copying external comment bodies.

Independent finding protections still apply. Every thread page is read through
GraphQL and joined to REST comments by root comment ID. Unresolved Codex threads
block, including outdated threads and threads on older heads. Missing thread
mappings, partial reads, and incomplete pagination cannot supply acceptance.
Priority-badge issue findings have no reliable reviewed SHA and block conservatively.
Substantive unthreaded reviews also block. The adapter does not delete comments.

Resolving a thread alone is not approval. Structured metadata has no completion
time or review-run ID, so it cannot prove that another contradictory finding
predates the clean result. Resolved findings therefore require verified
dispositions when using this path, including resolved findings on the same head.
Markdown completion timestamps cannot erase those blockers. The separate legacy
receipt path retains its existing chronology rules.

An observed same-head structured running or queued review invalidates an earlier
clean proof. A subsequent completed clean result can pass after two stable
observations without a new reaction. SHA identity cannot distinguish every rerun.
An unobserved overlapping rerun that returns to identical metadata is not
identifiable without an upstream review-run ID. Poll time and summary edit time
do not supply that identity. Event delivery and native job rerun limitations
above still apply.

Legacy receipt compatibility remains separate. Without structured metadata, a
manual Code Review needs the trusted summary and a trusted terminal clean receipt
with exactly one explicit reviewed commit that resolves to the captured full SHA.
Reaction-only automatic acceptance is no longer supported. A completed structured
Code Review with both finding flags null can use the recognized
`codex-review-receipt:v1` clean template for this compatibility path. The template
includes the reviewed commit and known boilerplate. Its optional positive suffix
may be `Keep it up!`, `Another round soon, please!`, `:+1:`, `:thumbsup:`, or `👍`
with an optional variation selector and skin tone. Unknown prose, additional
text, and conflicting receipts fail closed. Explicit structured findings cannot
be turned into a clean review by a receipt.

Receipt compatibility retains its original observer-start, restart, timestamp,
and ambiguity checks, plus a fresh trusted thumbs-up. The receipt precedes the
summary completion, which strictly precedes the reaction. Same-second ordering
is insufficient. The newest eligible receipt must identify the current head.
A newer wrong-head receipt cannot fall back to an older valid one. Every required
review must be complete. A receipt cannot substitute for a required Security Review.
When unknown structured flags coexist with findings, every finding needs a valid
disposition and every inline thread must be resolved. This produces only
`verified_dispositions` and does not require a reaction.

Finding chronology still requires creation and update timestamps. Dispositions
use their creation time for ordering and fresh GraphQL `lastEditedAt` and `editor`
metadata to prove that their text was never edited. The GraphQL comment must match
the REST comment identity, type, body, and creation time. Both edit fields must be
explicitly null; missing or unavailable edit evidence blocks acceptance. A later
REST `updated_at` alone does not mean the text was edited.
Substantive reviews require submission and update timestamps. GitHub REST reviews
omit update time, so observations enrich Codex reviews from the complete paginated
GraphQL connection by database ID. A qualifying receipt strictly follows all
finding and substantive review changes. Missing or ambiguous chronology blocks
receipt and disposition acceptance. Display chronology remains necessary for
these compatibility paths, but not for structured clean acceptance.

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
`pull_request_target`. Install the default-branch subscriptions and the PR merge-ref inline subscription
before relying on them. For a manual recheck, run:

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
Once an authorized edit or deletion event is saved, it preserves the previous
target as well as the new target, so that event cannot restore an older reply.
A current maintainer can invalidate an edited or deleted decision even if its
author no longer has write access. Creation still requires a privileged author;
acceptance always rechecks the decision author's current permission.
A newer valid reply recovers it. Events initiated by users without write access cannot advance these boundaries.

If an observer fails before saving a deletion, a manual dispatch cannot rebuild
the missing payload. Rerun the original failed event with its retained payload,
or post a new decision reply and verify it with a manual dispatch. Do not treat
a resolved thread or a later green observer as proof that the deletion was saved.
Fork and Dependabot review and inline-comment events normally receive read-only tokens, so
they cannot persist these records or rerun the native job. Their inline event
handling is unsupported without a trusted writable route. See [GitHub event
permissions](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request_review_comment).

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

Legacy receipt recovery cannot accept a review completed before observation began. This restriction does not apply to explicit structured clean results. Observing any running or failed review ends recovery and
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

Observation state lives in the latest owned evidence check output. Native mode
also reads the append-only feedback records. Code Review must be present;
Security Review alone cannot approve the check. In legacy report-only mode,
per-PR job concurrency serializes observations after event filtering. Ordinary
issue comments do not enter that queue. All inline comment events trigger
revalidation because a new reply can invalidate a prior decision. Receipt creation, edits,
and deletions qualify only when the event sender currently has repository write
authority, checked before any check write. Marker removal uses the previous
comment body supplied with an edit event. A restart resumes an incomplete run. A new authorized invocation creates a new
run when renewing a completed lifecycle, preserving the original reaction baseline
and clearing candidate confirmation. Completed runs remain history; only the
latest owned run supplies resumable state, except for the bounded legacy origin
recovery described above. Multiple active runs block as ambiguous.
Failed or expired attempts get a renewed deadline. Each attempt is
still bounded to 20 minutes and recovery requires two stable observations.
GitHub may coalesce pending runs in legacy report-only mode. Opted-in native
observers retain separate event runs and read the latest live head.
Same-head review restarts invalidate previous success when running evidence is
observed. A head or base change during evidence collection is rejected. The caller
subscribes to `edited` to observe retargeting. Base-branch pushes alone may not
produce a caller event, so base advancement is detected on the next observation;
this adapter cannot attest an unobserved base update. A change after
the last API read can briefly race check publication; the check is still bound
to the captured SHA and subsequent events re-evaluate evidence.

This is a practical lifecycle observation, not a cryptographic review attestation.
Webhook delay remains a limitation. Reaction propagation also affects legacy receipt compatibility. Only required review types reported in the evolving summary format are observed; absent optional Security metadata does not prove a security review ran. Unsupported structured metadata fails closed.
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
