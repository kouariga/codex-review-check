# Codex Review Check

Observe an existing automatic Codex Cloud review and publish `codex-cloud-review` on the
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
  current full head SHA; no current-head findings.
- The same target branch and base revision throughout observation. Retargeting
  or base advancement invalidates persisted acceptance on the next observation.
- A thumbs-up reaction ID absent from the initial baseline and no active eyes.
- Eyes observed during this lifecycle, or a changed summary if eyes was missed.
- The same clean evidence on two observations at least 30 seconds apart.

Existing thumbs-up and old-head summaries never supply fresh acceptance.
An already-finished review first seen after completion cannot be retroactively
accepted. Rebase/force-push/new head starts separate state. Missing, ambiguous,
failed, or unavailable evidence does not pass; no automatic review retry is sent.
Resolving a current-head finding does not erase it from this conservative gate.
A new head and fresh review is the normal correction path. Inline findings are
attributed to their original reviewed commit, not a later re-anchored location.
Trusted priority-badge findings posted as plain issue comments have no reliable
reviewed SHA and block conservatively, including after a new push, until a
maintainer resolves that ambiguous evidence. The adapter does not delete comments.
Manually requested reviews are outside this adapter's acceptance contract:
comment reactions do not provide a reliable current-head binding. Only observed
automatic trigger labels (`PR opened`, `New commits`, `Draft marked ready`) are
accepted. Manual or unknown trigger labels block; no fallback request is posted.

State lives in the latest owned check output. Code Review must be present;
Security Review alone cannot approve the check. Per-PR job concurrency, after trusted-event filtering, serializes
observations; ordinary comments do not enter that queue. A restart resumes an incomplete run. A new authorized invocation creates a new
run when renewing a completed lifecycle, preserving the original reaction baseline
and clearing candidate confirmation. Completed runs remain history; only the
latest owned run supplies resumable state. Multiple active runs block as ambiguous.
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
python3 -m unittest discover -s tests -p 'test_codex_review_gate.py' -v
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
