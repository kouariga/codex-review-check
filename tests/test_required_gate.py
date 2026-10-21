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
        for snapshot in (sample(), clean | {'eyes': True}, clean | {'completed': False},
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
        for change, expected in (({}, True), ({'eyes': True}, False), ({'unresolved_threads': True}, False)):
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

    def test_completed_same_head_rereview_retains_original_observation_baseline(self):
        for manual in (False, True):
            with self.subTest(manual=manual):
                def complete(identifier, completed_at):
                    result = sample(thumbs=[identifier], summary=identifier, completed=True,
                                    completed_at=completed_at)
                    if manual:
                        result['manual'] = {
                            'comments': [{'id': identifier, 'created': completed_at - 1,
                                          'updated': completed_at - 1, 'digest': identifier}],
                            'thumbs': {identifier: completed_at + 2},
                            'completed_at': completed_at, 'summary_updated': completed_at + 1}
                    else:
                        result['reaction_times'] = {identifier: completed_at + 2}
                    return result
                first = complete('first', 10)
                state = gate.initialize(sample(), 0)
                self.assertEqual(gate.observe(state, first, 20)[0], 'pending')
                self.assertEqual(gate.observe(state, first, 50)[0], 'success')
                second = complete('second', 55)
                api = FakeGitHub([second], saved_check(conclusion='success', state=state))
                self.assertIs(self.run_gate(api), True)
                saved = json.loads(api.check['output']['text'])
                self.assertEqual(saved['baseline'], ['old'])
                self.assertEqual(saved['observed_since'], 0)
                self.assertEqual(saved['candidate_at'], 60)
                self.assertEqual([row['status'] for row in api.writes],
                                 ['in_progress', 'in_progress', 'completed'])

    def test_completed_rereview_recovery_preserves_rejection_and_disposition_floors(self):
        first = sample(thumbs=['first'], summary='first', completed=True)
        state = gate.initialize(sample(), 0)
        gate.observe(state, first, 0)
        gate.observe(state, first, 30)
        state['manual_since'] = 10
        state['disposition_floors'] = {'finding': 99}
        second = sample(thumbs=['second'], summary='second', completed=True)
        unresolved = {'completed': True, 'unresolved': True, 'has_findings': False,
                      'latest_completed_at': 50, 'summary_updated': 51}
        for change in ({'eyes': True}, {'completed': False}, {'failed': True},
                       {'unresolved_threads': True}, {'structured': unresolved}):
            with self.subTest(change=change):
                api = FakeGitHub([second | change], saved_check(conclusion='success', state=state))
                self.assertIs(self.run_gate(api), False)
                saved = json.loads(api.check['output']['text'])
                self.assertEqual(saved['baseline'], ['old'])
                self.assertEqual(saved['observed_since'], 0)
                self.assertGreaterEqual(saved['manual_since'], 10)
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
        self.assertEqual(state['baseline'], ['old'])
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

    def test_native_only_history_cannot_supply_a_baseline(self):
        clean = sample(thumbs=['new'], summary='complete', completed=True)
        native = saved_check(name='codex-cloud-review', conclusion='success')
        native['external_id'] = None
        api = FakeGitHub([clean], native)
        self.assertIs(self.run_gate(api), False)
        self.assertEqual(api.writes[0]['name'], EVIDENCE)


class RequiredMainTests(unittest.TestCase):
    def test_required_cli_exits_zero_only_for_explicit_true(self):
        with tempfile.NamedTemporaryFile(mode='w') as event:
            json.dump({'pull_request': {'number': 1, 'head': {'sha': HEAD}}}, event)
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

    def test_required_mode_rejects_pr_controlled_and_other_events(self):
        for event in ('pull_request', 'workflow_dispatch', 'issue_comment'):
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
            env = {'CODEX_REQUIRED_REVIEW': '', 'GITHUB_EVENT_NAME': 'pull_request_target',
                   'GITHUB_EVENT_PATH': event.name, 'GITHUB_TOKEN': 'unused', 'GITHUB_REPOSITORY': 'owner/repo'}
            with mock.patch.dict(os.environ, env), mock.patch.object(gate, 'GitHub'), \
                    mock.patch.object(gate, 'run', return_value=False):
                self.assertIsNone(gate.main())
