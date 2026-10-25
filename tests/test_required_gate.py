import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from test_codex_review_gate import FakeGitHub, gate, sample

HEAD = 'a' * 40
EVIDENCE = 'codex-review-evidence'


def saved_check(name=EVIDENCE, conclusion=None, identifier=1, state=None):
    return dict(id=identifier, name=name, head_sha=HEAD,
                app={'id': gate.ACTIONS_APP_ID}, conclusion=conclusion,
                external_id='codex-gate-v1:owner/repo:1:' + HEAD,
                output={'text': json.dumps(state or gate.initialize(sample(), 0))})


class RequiredGateTests(unittest.TestCase):
    def run_gate(self, api):
        now = [60]
        def sleep(seconds):
            now[0] += seconds
        with contextlib.redirect_stdout(io.StringIO()):
            return gate.run(api, 1, lambda: now[0], sleep, expected_head=HEAD)

    def test_stable_acceptance_returns_true_and_writes_only_evidence_name(self):
        clean = sample(thumbs=['new'], summary='complete', completed=True)
        api = FakeGitHub([sample(), clean, clean])
        self.assertIs(self.run_gate(api), True)
        self.assertEqual(api.writes[0]['name'], 'codex-review-evidence')
        self.assertEqual(api.check['conclusion'], 'success')
        self.assertNotIn('codex-cloud-review', [row.get('name') for row in api.writes])

    def test_initial_incomplete_running_unknown_failed_and_unresolved_never_accept(self):
        clean = sample(thumbs=['new'], summary='complete', completed=True)
        for snapshot in (sample(), clean | {'structured': None}, clean | {'completed': False},
                         clean | {'failed': True}, clean | {'unresolved_threads': True}):
            with self.subTest(snapshot=snapshot):
                api = FakeGitHub([sample(), snapshot])
                self.assertIs(self.run_gate(api), False)
                self.assertEqual(api.check['conclusion'], 'failure')

    def test_clean_review_cannot_bypass_unresolved_older_head_thread(self):
        clean = sample(thumbs=['new'], summary='complete', completed=True,
                       completed_at=30, unresolved_threads=True,
                       inventory=[{'key': 'old-root', 'resolved': False,
                                   'root': {'original_commit_id': 'b' * 40,
                                            'created_at': '1970-01-01T00:00:01Z'}}])
        api = FakeGitHub([sample(), clean])
        self.assertIs(self.run_gate(api), False)
        self.assertEqual(api.check['conclusion'], 'failure')

    def test_event_head_mismatch_draft_and_closed_do_not_publish(self):
        for change in ({'head': {'sha': 'b' * 40}}, {'draft': True}, {'state': 'closed'}):
            api = FakeGitHub([])
            original = api.repo_call
            def call(path, data=None, method=None):
                result = original(path, data, method)
                return result | change if path.startswith('pulls/') else result
            api.repo_call = call
            self.assertIs(self.run_gate(api), False)
            self.assertEqual(api.writes, [])

    def test_head_and_base_changes_stop_observation(self):
        for snapshot in (sample(head='b' * 40), sample(base={'ref': 'main', 'sha': 'e' * 40})):
            api = FakeGitHub([sample(), snapshot])
            self.assertIs(self.run_gate(api), False)
            self.assertEqual(api.check['conclusion'], 'failure')

    def test_base_change_remains_blocked_across_retries_and_same_head_rereviews(self):
        clean = sample(completed=True)
        for prior_base in ({'ref': 'release', 'sha': 'd' * 40},
                           {'ref': 'main', 'sha': 'e' * 40}):
            for conclusion in (None, 'failure', 'success'):
                with self.subTest(base=prior_base, conclusion=conclusion):
                    state = gate.initialize(clean | {'base': prior_base}, 0)
                    saved = saved_check(conclusion=conclusion, state=state)
                    for _ in range(2):
                        api = FakeGitHub([sample(eyes=True), clean], saved)
                        self.assertIs(self.run_gate(api), False)
                        self.assertEqual(api.check['conclusion'], 'failure')
                        self.assertEqual(json.loads(api.check['output']['text'])['base'], prior_base)
                        self.assertIn('base changed', api.check['output']['title'])
                        self.assertNotIn('success', [row.get('conclusion') for row in api.writes])
                        saved = api.check

    def test_new_head_after_base_change_can_accept_its_own_structured_review(self):
        old = gate.initialize(sample(base={'ref': 'release', 'sha': 'e' * 40}), 0)
        clean = sample(head='b' * 40, completed=True)
        api = FakeGitHub([clean], saved_check(conclusion='failure', state=old))
        original = api.repo_call
        def call(path, data=None, method=None):
            result = original(path, data, method)
            return result | {'head': {'sha': 'b' * 40}} if path.startswith('pulls/') else result
        api.repo_call = call
        now = [60]
        with contextlib.redirect_stdout(io.StringIO()):
            result = gate.run(api, 1, lambda: now[0], lambda seconds: now.__setitem__(0, now[0] + seconds),
                              expected_head='b' * 40)
        self.assertIs(result, True)
        self.assertEqual(api.check['head_sha'], 'b' * 40)
        self.assertEqual(api.check['conclusion'], 'success')

    def test_api_errors_cannot_return_acceptance(self):
        api = FakeGitHub([OSError()])
        self.assertIs(self.run_gate(api), False)
        api = FakeGitHub([gate.APIBlocked('denied')])
        with self.assertRaises(gate.APIBlocked):
            self.run_gate(api)

    def test_cached_success_requires_matching_current_proof(self):
        clean = sample(thumbs=['new'], summary='complete', completed=True)
        state = gate.initialize(sample(), 0)
        gate.observe(state, clean, 0)
        gate.observe(state, clean, 30)
        for change, expected in (({}, True), ({'eyes': True}, True), ({'unresolved_threads': True}, False)):
            for name in (EVIDENCE, 'codex-cloud-review'):
                api = FakeGitHub([clean | change], saved_check(name=name, conclusion='success', state=state))
                self.assertIs(self.run_gate(api), expected)

    def test_legacy_pending_is_migrated_but_evidence_pending_is_resumed(self):
        clean = sample(thumbs=['new'], summary='complete', completed=True)
        for name, writes in (('codex-cloud-review', 3), (EVIDENCE, 2)):
            with self.subTest(name=name):
                api = FakeGitHub([clean, clean], saved_check(name=name))
                self.assertIs(self.run_gate(api), True)
                self.assertEqual(api.check['name'], EVIDENCE)
                self.assertEqual(len(api.writes), writes)

    def test_completed_same_head_rereview_uses_structured_verdict_and_new_inventory(self):
        def complete(identifier):
            return sample(completed=True, review_digest=identifier)
        state = gate.initialize(sample(), 0)
        first = complete('first')
        gate.observe(state, first, 20)
        gate.observe(state, first, 50)
        api = FakeGitHub([complete('second')], saved_check(conclusion='success', state=state))
        self.assertIs(self.run_gate(api), True)
        saved = json.loads(api.check['output']['text'])
        self.assertEqual(saved['head'], HEAD)
        self.assertEqual(saved['proof']['kind'], 'clean_codex')

    def test_cached_acceptance_recovers_after_transient_snapshot_error(self):
        clean = sample(completed=True)
        state = gate.initialize(sample(), 0)
        gate.observe(state, clean, 20)
        gate.observe(state, clean, 50)
        api = FakeGitHub([OSError('temporary'), clean],
                         saved_check(conclusion='success', state=state))
        self.assertIs(self.run_gate(api), True)
        self.assertEqual(api.check['conclusion'], 'success')
        self.assertEqual([row['status'] for row in api.writes],
                         ['in_progress', 'in_progress', 'completed'])

    def test_unchanged_proof_survives_review_metadata_enrichment(self):
        clean = sample(thumbs=['fresh'], summary='completed', completed=True,
                       completed_at=10, reaction_times={'fresh': 12})
        disposed = sample(completed=True, inventory=[{'key': 'inline:1'}], dispositions={
            'inline:1': {'id': 2, 'author': 3, 'valid': True, 'created': 10, 'digest': 'record'}})
        for first in (clean, disposed):
            for unavailable in (False, True):
                with self.subTest(dispositions=bool(first.get('dispositions')), unavailable=unavailable):
                    first = first | {'review_digest': 'before-chronology-enrichment'}
                    state = gate.initialize(sample(), 0)
                    gate.observe(state, first, 20)
                    self.assertEqual(gate.observe(state, first, 50)[0], 'success')
                    enriched = first | {'review_digest': 'with-authoritative-update-times'}
                    api = FakeGitHub(([OSError('temporary')] if unavailable else []) + [enriched],
                                     saved_check(conclusion='success', state=state))
                    self.assertIs(self.run_gate(api), True)
                    saved = json.loads(api.check['output']['text'])
                    self.assertNotIn('review_since', saved)
                    self.assertEqual(saved['proof'], state['proof'])

    def test_cached_acceptance_revalidation_has_a_bounded_retry(self):
        clean = sample(thumbs=['fresh'], summary='completed', completed=True)
        state = gate.initialize(sample(), 0)
        gate.observe(state, clean, 0)
        gate.observe(state, clean, 30)
        api = FakeGitHub([OSError('unavailable')],
                         saved_check(conclusion='success', state=state))
        self.assertIs(self.run_gate(api), False)
        self.assertEqual(api.writes[0]['status'], 'in_progress')
        self.assertEqual(api.check['conclusion'], 'failure')
        self.assertIn('renewal', json.loads(api.check['output']['text']))

    def test_cached_revalidation_survives_interruption(self):
        receipt = {'id': 'receipt', 'created': 9, 'updated': 9, 'digest': 'receipt'}
        first = sample(thumbs=['first'], summary='first', completed=True, manual={
            'comments': [receipt], 'thumbs': {'first': 12},
            'completed_at': 10, 'summary_updated': 11})
        state = gate.initialize(sample(), 0)
        gate.observe(state, first, 20)
        gate.observe(state, first, 50)
        api = FakeGitHub([OSError('temporary'), gate.APIBlocked('interrupted')],
                         saved_check(name='codex-cloud-review', conclusion='success', state=state))
        with self.assertRaises(gate.APIBlocked):
            self.run_gate(api)
        self.assertEqual(api.check['status'], 'in_progress')
        pending = api.check
        second = sample(thumbs=['second'], summary='second', completed=True, manual={
            'comments': [receipt], 'thumbs': {'second': 57},
            'completed_at': 55, 'summary_updated': 56})
        for snapshot, expected in ((first, True), (second, True)):
            with self.subTest(unchanged=expected):
                recovered = FakeGitHub([snapshot], json.loads(json.dumps(pending)))
                self.assertIs(self.run_gate(recovered), expected)
                self.assertNotIn('renewal', json.loads(recovered.check['output']['text']))

    def test_snapshot_error_ignores_receipt_changes_for_unchanged_structured_verdict(self):
        receipt = {'id': 'receipt', 'created': 9, 'updated': 9, 'digest': 'receipt'}
        first = sample(thumbs=['first'], summary='first', completed=True, manual={
            'comments': [receipt], 'thumbs': {'first': 12},
            'completed_at': 10, 'summary_updated': 11})
        state = gate.initialize(sample(), 0)
        self.assertEqual(gate.observe(state, first, 20)[0], 'pending')
        self.assertEqual(gate.observe(state, first, 50)[0], 'success')
        second = sample(thumbs=['second'], summary='second', completed=True, manual={
            'comments': [receipt], 'thumbs': {'second': 57},
            'completed_at': 55, 'summary_updated': 56})
        for snapshots in ([second], [OSError('transient'), second]):
            with self.subTest(error=isinstance(snapshots[0], Exception)):
                api = FakeGitHub(snapshots, saved_check(conclusion='success', state=state))
                self.assertIs(self.run_gate(api), True)
                self.assertEqual(json.loads(api.check['output']['text'])['proof']['kind'], 'clean_codex')

    def test_completed_rereview_recovery_preserves_rejection_and_disposition_floors(self):
        first = sample(thumbs=['first'], summary='first', completed=True)
        state = gate.initialize(sample(), 0)
        gate.observe(state, first, 0)
        gate.observe(state, first, 30)
        state['review_since'] = 10
        state['disposition_floors'] = {'finding': 99}
        second = sample(thumbs=['second'], summary='second', completed=True)
        unresolved = second['structured'] | {'clean': False, 'unresolved': True, 'has_findings': True}
        for change in ({'structured': None}, {'completed': False}, {'failed': True},
                       {'unresolved_threads': True}, {'structured': unresolved}):
            with self.subTest(change=change):
                api = FakeGitHub([second | change], saved_check(conclusion='success', state=state))
                self.assertIs(self.run_gate(api), False)
                saved = json.loads(api.check['output']['text'])
                self.assertEqual(saved['head'], HEAD)
                self.assertEqual(saved['head'], HEAD)
                self.assertGreaterEqual(saved['review_since'], 10)
                self.assertEqual(saved['disposition_floors'], {'finding': 99})

    def test_new_evidence_history_takes_precedence_and_native_job_is_not_state(self):
        clean = sample(thumbs=['new'], summary='complete', completed=True)
        legacy = saved_check(name='codex-cloud-review', identifier=3)
        evidence = saved_check(identifier=2)
        native = dict(legacy, id=4, external_id=None, output={'text': ''})
        api = FakeGitHub([clean, clean], evidence)
        api.pages = lambda *args: [legacy, evidence, native]
        self.assertIs(self.run_gate(api), True)
        self.assertEqual(api.writes[0]['status'], 'in_progress')
        self.assertEqual(api.check['id'], 2)

    def test_failed_evidence_retry_preserves_original_baseline(self):
        clean = sample(thumbs=['new'], summary='complete', completed=True)
        api = FakeGitHub([clean, clean], saved_check(conclusion='failure'))
        self.assertIs(self.run_gate(api), True)
        state = json.loads(api.check['output']['text'])
        self.assertEqual(state['head'], HEAD)
        self.assertEqual(api.writes[0]['name'], EVIDENCE)

    def test_duplicate_active_evidence_is_rejected(self):
        api = FakeGitHub([])
        api.pages = lambda *args: [saved_check(), saved_check(identifier=2)]
        with self.assertRaises(ValueError):
            self.run_gate(api)
        self.assertEqual(api.writes, [])

    def test_saved_state_with_unknown_schema_cannot_accept(self):
        api = FakeGitHub([], saved_check(state={'version': 2, 'head': HEAD}))
        with self.assertRaises(ValueError):
            self.run_gate(api)
        self.assertEqual(api.writes, [])

    def test_native_only_history_cannot_supply_state_but_explicit_clean_can_pass(self):
        clean = sample(thumbs=['new'], summary='complete', completed=True)
        native = saved_check(name='codex-cloud-review', conclusion='success')
        native['external_id'] = None
        api = FakeGitHub([clean], native)
        self.assertIs(self.run_gate(api), True)
        self.assertEqual(len(api.writes), 3)
        self.assertEqual(api.writes[0]['name'], EVIDENCE)


class RequiredMainTests(unittest.TestCase):
    def test_required_cli_exits_zero_only_for_explicit_true(self):
        with tempfile.NamedTemporaryFile(mode='w') as event:
            json.dump({'pull_request': {'number': 1, 'head': {'sha': HEAD, 'repo': {'full_name': 'owner/repo'}}}}, event)
            event.flush()
            env = {'CODEX_REQUIRED_REVIEW': 'true', 'GITHUB_EVENT_NAME': 'pull_request_target',
                   'GITHUB_EVENT_PATH': event.name, 'GITHUB_TOKEN': 'unused', 'GITHUB_REPOSITORY': 'owner/repo'}
            for verdict, code in ((True, 0), (False, 1), (None, 1)):
                with mock.patch.dict(os.environ, env), mock.patch.object(gate, 'GitHub'), \
                        mock.patch.object(gate, 'run', return_value=verdict) as run:
                    with self.assertRaises(SystemExit) as result:
                        gate.main()
                    self.assertEqual(result.exception.code, code)
                    self.assertEqual(run.call_args.kwargs, {'expected_head': HEAD})

    def test_required_mode_rejects_fork_dependabot_and_missing_repository(self):
        for repository, author in (("other/fork", "person"), ("owner/repo", "dependabot[bot]"), (None, "person")):
            with self.subTest(repository=repository, author=author), tempfile.NamedTemporaryFile(mode='w') as event:
                json.dump({'pull_request': {'number': 1, 'head': {'sha': HEAD, 'repo': {'full_name': repository}},
                                           'user': {'login': author}}}, event)
                event.flush()
                env = {'CODEX_REQUIRED_REVIEW': 'true', 'GITHUB_EVENT_NAME': 'pull_request_target',
                       'GITHUB_EVENT_PATH': event.name, 'GITHUB_TOKEN': 'unused', 'GITHUB_REPOSITORY': 'owner/repo'}
                with mock.patch.dict(os.environ, env), mock.patch.object(gate, 'GitHub'), \
                        mock.patch.object(gate, 'run', return_value=True) as run:
                    with self.assertRaises(ValueError):
                        gate.main()
                    run.assert_not_called()

    def test_required_mode_rejects_pr_controlled_and_other_events(self):
        for event in ('pull_request', 'workflow_dispatch', 'issue_comment', 'pull_request_review'):
            with self.subTest(event=event), mock.patch.dict(os.environ, {
                    'CODEX_REQUIRED_REVIEW': 'true', 'GITHUB_EVENT_NAME': event}):
                with mock.patch.object(gate, 'GitHub') as api:
                    with self.assertRaises(ValueError):
                        gate.main()
                    api.assert_not_called()

    def test_legacy_cli_remains_report_only(self):
        with tempfile.NamedTemporaryFile(mode='w') as event:
            json.dump({'pull_request': {'number': 1}}, event)
            event.flush()
            env = {'CODEX_REQUIRED_REVIEW': '', 'GITHUB_EVENT_PATH': event.name,
                   'GITHUB_TOKEN': 'unused', 'GITHUB_REPOSITORY': 'owner/repo'}
            for event_name in ('pull_request_target', 'pull_request_review'):
                event.seek(0)
                event.truncate()
                json.dump({'pull_request': {'number': 1}, 'review': {'user': {'id': 199175422}}}, event)
                event.flush()
                with mock.patch.dict(os.environ, env | {'CODEX_REQUIRED_JOB': '',
                                                       'GITHUB_EVENT_NAME': event_name}), \
                        mock.patch.object(gate, 'GitHub') as api, \
                        mock.patch.object(gate, 'run', return_value=False) as run:
                    self.assertIsNone(gate.main())
                    run.assert_called_once_with(api.return_value, 1)
