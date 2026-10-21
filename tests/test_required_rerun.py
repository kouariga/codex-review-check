import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import test_dispositions
from test_codex_review_gate import FakeGitHub, gate, ROOT

HEAD = 'a' * 40
BASE = 'b' * 40
WORKFLOW = '.github/workflows/codex-cloud-review.yml'
BOUNDARY = 100


def server_time(seconds):
    return '1970-01-01T00:%02d:%02dZ' % divmod(seconds, 60)


class NativeGitHub:
    repo = 'owner/repo'

    def __init__(self):
        self.pr = {'state': 'open', 'draft': False, 'number': 1,
                   'head': {'sha': HEAD}, 'base': {'sha': BASE, 'ref': 'main'}}
        self.check = {'id': 9, 'name': 'codex-cloud-review', 'head_sha': HEAD,
                      'app': {'id': 15368}, 'status': 'completed', 'conclusion': 'success',
                      'details_url': 'https://github.com/owner/repo/actions/runs/7/job/8'}
        self.job = {'id': 8, 'run_id': 7, 'name': 'codex-cloud-review', 'status': 'completed',
                    'check_run_url': 'https://api.github.com/repos/owner/repo/check-runs/9'}
        self.workflow = {'run_attempt': 1, 'id': 7, 'event': 'pull_request_target', 'path': WORKFLOW, 'status': 'completed',
                         'pull_requests': [{'number': 1, 'head': {'sha': HEAD},
                                            'base': {'sha': BASE, 'ref': 'main', 'repo': {'id': 2}}}]}
        self.evidence = None
        self.feedback = []
        self.writes = []
        self.denied = False
        self.pr_reads = 0
        self.change_pr = None

    def pages(self, path, key):
        return copy.deepcopy([row for row in (self.check, self.evidence) if row] + self.feedback)

    def repo_call(self, path, data=None, method=None):
        if path == 'pulls/1':
            self.pr_reads += 1
            if self.change_pr and self.pr_reads > 1:
                self.pr.update(self.change_pr)
            return copy.deepcopy(self.pr)
        if path == 'check-runs':
            self.writes.append((path, copy.deepcopy(data), method))
            result = dict(copy.deepcopy(data), id=20 + len(self.feedback), app={'id': 15368},
                          completed_at=server_time(120))
            if data['name'] == 'codex-review-feedback-event':
                self.feedback.append(result)
            else:
                self.evidence = result
            return copy.deepcopy(result)
        if path == 'check-runs/20':
            self.writes.append((path, copy.deepcopy(data), method))
            self.evidence.update(copy.deepcopy(data))
            return copy.deepcopy(self.evidence)
        if path == 'actions/jobs/8':
            return copy.deepcopy(self.job)
        if path == 'actions/runs/17':
            return {'id': 17, 'run_started_at': server_time(100)}
        if path == 'actions/runs/7':
            return copy.deepcopy(self.workflow)
        if path == 'actions/jobs/8/rerun' and method == 'POST':
            if self.denied:
                raise gate.APIBlocked('denied')
            self.writes.append((path, data, method))
            self.check.update(status='queued', conclusion=None)
            return None
        raise AssertionError((path, data, method))


class NativeRerunTests(unittest.TestCase):
    def rerun(self, api, on_sleep=None, observed_after=None):
        now = [0]
        def sleep(seconds):
            now[0] += seconds
            if on_sleep:
                on_sleep()
        with contextlib.redirect_stdout(io.StringIO()):
            return gate.rerun_required_job(api, 1, 'codex-cloud-review', WORKFLOW,
                                           lambda: now[0], sleep, observed_after=observed_after)

    def test_later_native_attempt_covers_both_observers_without_extra_rerun(self):
        api = NativeGitHub()
        self.assertIs(self.rerun(api, observed_after=BOUNDARY), True)
        api.job.update(status='in_progress', started_at=server_time(101))
        api.workflow.update(status='in_progress', run_attempt=2)
        self.assertIs(self.rerun(api, observed_after=BOUNDARY), True)
        self.assertEqual(api.writes, [('actions/jobs/8/rerun', {}, 'POST')])

    def test_feedback_marker_is_saved_before_coverage_and_sets_later_boundary(self):
        api = NativeGitHub()
        api.disposition_floors = {'inline:11': 202}
        api.job.update(status='in_progress', started_at=server_time(121))
        api.workflow['status'] = 'in_progress'
        self.assertIs(self.rerun(api, observed_after=BOUNDARY), True)
        self.assertEqual([path for path, _, _ in api.writes], ['check-runs'])
        self.assertEqual(json.loads(api.feedback[0]['output']['text'])['disposition_floors'],
                         {'inline:11': 202})
        api = NativeGitHub()
        api.disposition_floors = {'inline:11': 202}
        api.job['started_at'] = server_time(110)
        self.assertIs(self.rerun(api, observed_after=BOUNDARY), True)
        self.assertEqual([path for path, _, _ in api.writes], ['check-runs', 'actions/jobs/8/rerun'])

    def test_equal_earlier_or_missing_start_cannot_cover_event(self):
        for start in (None, '', 'bad', server_time(99), server_time(100)):
            with self.subTest(start=start):
                api = NativeGitHub()
                api.job['started_at'] = start
                self.assertIs(self.rerun(api, observed_after=BOUNDARY), True)
                self.assertEqual(api.writes, [('actions/jobs/8/rerun', {}, 'POST')])

    def test_marker_without_valid_server_time_cannot_claim_coverage(self):
        for missing in (None, '', 'bad'):
            api = NativeGitHub()
            api.disposition_floors = {'inline:11': 202}
            api.job['started_at'] = server_time(121)
            call = api.repo_call
            def damaged(path, data=None, method=None):
                result = call(path, data, method)
                if path == 'check-runs':
                    result['completed_at'] = missing
                return result
            api.repo_call = damaged
            with self.assertRaises(ValueError):
                self.rerun(api, observed_after=BOUNDARY)
            self.assertFalse(any(path.endswith('/rerun') for path, _, _ in api.writes))

    def test_racing_post_403_only_recovers_after_observed_attempt_advance(self):
        api = NativeGitHub()
        call = api.repo_call
        def raced(path, data=None, method=None):
            if path.endswith('/rerun'):
                api.workflow.update(status='in_progress', run_attempt=2)
                api.job.update(status='in_progress', started_at=server_time(101))
                raise gate.APIBlocked('already rerunning')
            return call(path, data, method)
        api.repo_call = raced
        sleeps = []
        self.assertIs(self.rerun(api, lambda: sleeps.append(True), observed_after=BOUNDARY), True)
        self.assertEqual(len(sleeps), 1)

    def test_preboundary_active_attempt_still_reruns_after_finishing(self):
        api = NativeGitHub()
        api.job.update(status='in_progress', started_at=server_time(99))
        api.workflow['status'] = 'in_progress'
        def finish():
            api.job['status'] = api.workflow['status'] = 'completed'
        self.assertIs(self.rerun(api, finish, observed_after=BOUNDARY), True)
        self.assertEqual(api.writes, [('actions/jobs/8/rerun', {}, 'POST')])

    def test_race_recheck_failure_preserves_original_rerun_denial(self):
        api = NativeGitHub()
        api.denied = True
        call = api.repo_call
        workflow_reads = [0]
        original = gate.APIBlocked('original rerun denial')
        def blocked(path, data=None, method=None):
            if path.endswith('/rerun'):
                raise original
            if path == 'actions/runs/7':
                workflow_reads[0] += 1
                if workflow_reads[0] > 1:
                    raise gate.APIBlocked('workflow reread denied')
            return call(path, data, method)
        api.repo_call = blocked
        with self.assertRaises(gate.APIBlocked) as raised:
            self.rerun(api, observed_after=BOUNDARY)
        self.assertIs(raised.exception, original)
        self.assertEqual(api.writes, [])

    def test_completed_success_is_replaced_by_queued_native_rerun(self):
        api = NativeGitHub()
        self.assertIs(self.rerun(api), True)
        self.assertEqual(api.writes, [('actions/jobs/8/rerun', {}, 'POST')])
        self.assertEqual((api.check['status'], api.check['conclusion']), ('queued', None))

    def test_workflow_path_accepts_unqualified_and_ref_qualified_api_forms(self):
        for suffix in ('', '@main', '@refs/heads/main'):
            with self.subTest(suffix=suffix):
                api = NativeGitHub()
                api.workflow['path'] = WORKFLOW + suffix
                self.assertIs(self.rerun(api), True)
                self.assertEqual(api.writes, [('actions/jobs/8/rerun', {}, 'POST')])
                self.assertEqual((api.check['status'], api.check['conclusion']), ('queued', None))
        api = NativeGitHub()
        api.workflow['path'] = '.github/workflows/other.yml@main'
        self.assertIs(self.rerun(api), False)
        self.assertEqual(api.writes, [])

    def test_opted_in_pull_request_target_leaves_native_job_to_its_own_workflow(self):
        api = NativeGitHub()
        api.job['status'] = api.check['status'] = api.workflow['status'] = 'in_progress'
        api.check['conclusion'] = None
        with tempfile.NamedTemporaryFile(mode='w') as event:
            json.dump({'pull_request': {'number': 1, 'head': {'sha': HEAD}}}, event)
            event.flush()
            env = {'CODEX_REQUIRED_REVIEW': '', 'CODEX_REQUIRED_JOB': 'codex-cloud-review',
                   'GITHUB_EVENT_NAME': 'pull_request_target', 'GITHUB_EVENT_PATH': event.name,
                   'GITHUB_TOKEN': 'unused', 'GITHUB_REPOSITORY': 'owner/repo',
                   'GITHUB_WORKFLOW_REF': 'owner/repo/' + WORKFLOW + '@refs/heads/main',
                   'GITHUB_RUN_ID': '17'}
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(gate, 'GitHub', return_value=api), \
                    mock.patch.object(gate, 'rerun_required_job', side_effect=AssertionError('self rerun')):
                self.assertIsNone(gate.main())
        self.assertEqual(api.pr_reads, 0)
        self.assertEqual(api.writes, [])
        self.assertEqual((api.check['status'], api.check['conclusion']), ('in_progress', None))

    def test_active_job_is_rerun_after_finishing_so_its_success_cannot_escape(self):
        api = NativeGitHub()
        api.job['status'] = api.check['status'] = 'in_progress'
        def finish():
            api.job['status'] = api.check['status'] = 'completed'
        self.assertIs(self.rerun(api, finish), True)
        self.assertEqual(api.writes, [('actions/jobs/8/rerun', {}, 'POST')])
        self.assertEqual(api.pr_reads, 3)

    def test_completed_job_waits_until_its_parent_workflow_finishes(self):
        api = NativeGitHub()
        api.workflow['status'] = 'in_progress'
        def finish():
            api.workflow['status'] = 'completed'
        self.assertIs(self.rerun(api, finish), True)
        self.assertEqual(api.pr_reads, 3)
        self.assertEqual(api.check['status'], 'queued')

    def test_bootstrap_waits_for_the_native_job(self):
        api = NativeGitHub()
        check = api.check
        api.check = None
        self.assertIs(self.rerun(api, lambda: setattr(api, 'check', check)), True)
        self.assertEqual(api.check['status'], 'queued')

    def test_untrusted_or_unrelated_checks_are_never_rerun(self):
        changes = [('check', {'name': 'other'}), ('check', {'head_sha': 'c' * 40}),
                   ('check', {'app': {'id': 1}}),
                   ('check', {'details_url': 'https://github.com/other/repo/actions/runs/7/job/8'}),
                   ('job', {'check_run_url': 'https://api.github.com/repos/owner/repo/check-runs/10'}),
                   ('job', {'id': 10}), ('job', {'run_id': 10}),
                   ('workflow', {'event': 'pull_request'}), ('workflow', {'path': 'other.yml'}),
                   ('workflow', {'id': 10}), ('workflow', {'pull_requests': []})]
        for target, changeset in changes:
            with self.subTest(target=target, changes=changeset):
                api = NativeGitHub()
                getattr(api, target).update(changeset)
                self.assertIs(self.rerun(api), False)
                self.assertEqual(api.writes, [])
        for changeset in ({'number': 2}, {'head': {'sha': 'c' * 40}},
                          {'base': {'sha': 'c' * 40, 'ref': 'main'}},
                          {'base': {'sha': BASE, 'ref': 'other'}}):
            with self.subTest(pr=changeset):
                api = NativeGitHub()
                api.workflow['pull_requests'][0].update(changeset)
                self.assertIs(self.rerun(api), False)
                self.assertEqual(api.writes, [])

    def test_identity_is_rechecked_immediately_before_rerun(self):
        for change in ({'head': {'sha': 'c' * 40}}, {'base': {'sha': 'c' * 40, 'ref': 'main'}},
                       {'draft': True}, {'state': 'closed'}):
            api = NativeGitHub()
            api.change_pr = change
            self.assertIs(self.rerun(api), False)
            self.assertEqual(api.writes, [])

    def test_denied_rerun_propagates_instead_of_reporting_success(self):
        api = NativeGitHub()
        api.denied = True
        with self.assertRaises(gate.APIBlocked):
            self.rerun(api)
        self.assertEqual(api.check['conclusion'], 'success')

    def test_opted_in_comment_observer_reruns_native_without_legacy_writes(self):
        api = NativeGitHub()
        event_body = {'issue': {'number': 1, 'pull_request': {}},
                      'comment': {'user': {'id': 199175422},
                                  'performed_via_github_app': {'id': 1144995},
                                  'body': 'Codex review running'}}
        event_body['issue']['pull_request'] = {'url': 'https://api.github.com/repos/owner/repo/pulls/1'}
        with tempfile.NamedTemporaryFile(mode='w') as event:
            json.dump(event_body, event)
            event.flush()
            env = {'CODEX_REQUIRED_REVIEW': '', 'CODEX_REQUIRED_JOB': 'codex-cloud-review',
                   'GITHUB_EVENT_NAME': 'issue_comment', 'GITHUB_EVENT_PATH': event.name,
                   'GITHUB_TOKEN': 'unused', 'GITHUB_REPOSITORY': 'owner/repo',
                   'GITHUB_WORKFLOW_REF': 'owner/repo/' + WORKFLOW + '@refs/heads/main',
                   'GITHUB_RUN_ID': '17'}
            with mock.patch.dict(os.environ, env), mock.patch.object(gate, 'GitHub', return_value=api):
                gate.main()
        self.assertEqual(api.writes, [('actions/jobs/8/rerun', {}, 'POST')])
        self.assertEqual(api.check['status'], 'queued')

    def test_inline_replies_rerun_native_without_unauthorized_decision_floors(self):
        for action in ('created', 'edited', 'deleted'):
            for body, previous in (('This still needs another change.', None),
                                   ('Now ordinary text.', 'Codex review: fixed'),
                                   ('Codex review: fixed', None)):
                with self.subTest(action=action, body=body, previous=previous):
                    api = NativeGitHub()
                    api.privileged = lambda user: False
                    event_body = {'action': action, 'pull_request': {'number': 1},
                                  'sender': {'id': 456, 'type': 'User'},
                                  'comment': {'id': 123, 'in_reply_to_id': 11,
                                              'user': {'id': 456, 'type': 'User'}, 'body': body}}
                    if previous:
                        event_body['changes'] = {'body': {'from': previous}}
                    with tempfile.NamedTemporaryFile(mode='w') as event:
                        json.dump(event_body, event)
                        event.flush()
                        env = {'CODEX_REQUIRED_REVIEW': '', 'CODEX_REQUIRED_JOB': 'codex-cloud-review',
                               'GITHUB_EVENT_NAME': 'pull_request_review_comment', 'GITHUB_EVENT_PATH': event.name,
                               'GITHUB_TOKEN': 'unused', 'GITHUB_REPOSITORY': 'owner/repo',
                               'GITHUB_WORKFLOW_REF': 'owner/repo/' + WORKFLOW + '@refs/heads/main',
                               'GITHUB_RUN_ID': '17'}
                        with mock.patch.dict(os.environ, env), mock.patch.object(gate, 'GitHub', return_value=api):
                            gate.main()
                    self.assertEqual(api.writes, [('actions/jobs/8/rerun', {}, 'POST')])
                    self.assertFalse(hasattr(api, 'disposition_floors'))

    def test_top_level_codex_review_events_rerun_native_and_ignore_other_authors(self):
        for action in ('submitted', 'edited', 'dismissed'):
            for author in (199175422, 123):
                with self.subTest(action=action, author=author):
                    api = NativeGitHub()
                    event_body = {'action': action, 'pull_request': {'number': 1},
                                  'sender': {'id': 456},
                                  'review': {'user': {'id': author, 'login': 'chatgpt-codex-connector[bot]'},
                                             'body': '[P1] A top-level finding without inline comments'}}
                    with tempfile.NamedTemporaryFile(mode='w') as event:
                        json.dump(event_body, event)
                        event.flush()
                        env = {'CODEX_REQUIRED_REVIEW': '', 'CODEX_REQUIRED_JOB': 'codex-cloud-review',
                               'GITHUB_EVENT_NAME': 'pull_request_review', 'GITHUB_EVENT_PATH': event.name,
                               'GITHUB_TOKEN': 'unused', 'GITHUB_REPOSITORY': 'owner/repo',
                               'GITHUB_WORKFLOW_REF': 'owner/repo/' + WORKFLOW + '@refs/heads/main',
                               'GITHUB_RUN_ID': '17'}
                        with mock.patch.dict(os.environ, env), \
                                mock.patch.object(gate, 'GitHub', return_value=api), \
                                mock.patch.object(api, 'repo_call', wraps=api.repo_call) as requests, \
                                mock.patch.object(api, 'pages', wraps=api.pages) as pages:
                            gate.main()
                    if author == 199175422:
                        self.assertEqual(api.writes, [('actions/jobs/8/rerun', {}, 'POST')])
                        self.assertEqual(api.check['status'], 'queued')
                    else:
                        requests.assert_not_called()
                        pages.assert_not_called()
                        self.assertEqual(api.writes, [])
                        self.assertEqual(api.check['conclusion'], 'success')

    def test_comment_observer_uses_server_run_start_and_rejects_invalid_identity(self):
        event_body = {'issue': {'number': 1, 'pull_request': {'url': 'unused'}},
                      'comment': {'user': {'id': 199175422},
                                  'performed_via_github_app': {'id': 1144995},
                                  'body': 'Codex review running'}}
        for run_id, started in (('17', server_time(100)), ('17', ''), ('17', 'bad'), ('0', server_time(100)),
                                ('bad', server_time(100))):
            with self.subTest(run_id=run_id, started=started):
                api = NativeGitHub()
                call = api.repo_call
                def run_metadata(path, data=None, method=None):
                    if path == 'actions/runs/17':
                        return {'id': 17, 'run_started_at': started}
                    return call(path, data, method)
                api.repo_call = run_metadata
                with tempfile.NamedTemporaryFile(mode='w') as event:
                    json.dump(event_body, event)
                    event.flush()
                    env = {'CODEX_REQUIRED_REVIEW': '', 'CODEX_REQUIRED_JOB': 'codex-cloud-review',
                           'GITHUB_EVENT_NAME': 'issue_comment', 'GITHUB_EVENT_PATH': event.name,
                           'GITHUB_TOKEN': 'unused', 'GITHUB_REPOSITORY': 'owner/repo',
                           'GITHUB_WORKFLOW_REF': 'owner/repo/' + WORKFLOW + '@refs/heads/main',
                           'GITHUB_RUN_ID': run_id}
                    with mock.patch.dict(os.environ, env), mock.patch.object(gate, 'GitHub', return_value=api), \
                            mock.patch.object(gate, 'rerun_required_job', return_value=True) as rerun:
                        if run_id == '17' and started == server_time(100):
                            gate.main()
                            rerun.assert_called_once_with(api, 1, 'codex-cloud-review', WORKFLOW,
                                                          observed_after=BOUNDARY)
                        else:
                            with self.assertRaises(ValueError):
                                gate.main()
                            rerun.assert_not_called()

    def test_deleted_decision_floor_survives_native_rerun_and_blocks_older_reply(self):
        fixture = test_dispositions.DispositionTests()
        fixture.setUp()
        fixture.decisions()
        snapshot = fixture.api.snapshot(1)
        state, _, accepted = fixture.verdict(snapshot)
        self.assertEqual(accepted[0], 'success')
        for previous in (None, state):
            api = NativeGitHub()
            api.pr['base'] = snapshot['base']
            api.workflow['pull_requests'][0]['base'] = snapshot['base']
            api.disposition_floors = {'inline:11': 202}
            if previous:
                api.evidence = {'id': 20, 'name': 'codex-review-evidence', 'head_sha': HEAD,
                                'app': {'id': 15368}, 'conclusion': 'success',
                                'external_id': 'codex-gate-v1:owner/repo:1:' + HEAD,
                                'output': {'text': json.dumps(previous)}}
            self.assertIs(self.rerun(api), True)
            saved = json.loads(api.feedback[0]['output']['text'])
            self.assertEqual(saved['disposition_floors']['inline:11'], 202)
            self.assertNotIn('proof', saved)
            self.assertEqual(api.writes[-1], ('actions/jobs/8/rerun', {}, 'POST'))
            native = FakeGitHub([snapshot], api.evidence)
            native.pages = lambda *args: api.pages(*args)
            native.repo_call = self.native_calls(native, api.pr)
            now = [180]
            def sleep(seconds):
                now[0] += seconds
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertIs(gate.run(native, 1, lambda: now[0], sleep, expected_head=HEAD), False)
            self.assertEqual(native.check['conclusion'], 'failure')

    def test_feedback_is_saved_before_waiting_for_missing_or_busy_native_job(self):
        for missing in (False, True):
            api = NativeGitHub()
            api.disposition_floors = {'inline:11': 202}
            if missing:
                api.check = None
            else:
                api.job['status'] = api.workflow['status'] = 'in_progress'
            def during_wait():
                self.assertEqual(len(api.feedback), 1)
                self.assertEqual(json.loads(api.feedback[0]['output']['text'])['disposition_floors'],
                                 {'inline:11': 202})
            self.assertIs(self.rerun(api, during_wait), False)
            self.assertEqual(len(api.writes), 1)
            self.assertEqual(api.feedback[0]['status'], 'completed')
            self.assertEqual(api.feedback[0]['conclusion'], 'neutral')

    def test_native_publication_cannot_erase_concurrent_feedback_event(self):
        api = NativeGitHub()
        api.disposition_floors = {'inline:11': 202}
        api.job['status'] = 'in_progress'
        def finish_with_older_state():
            self.assertEqual(len(api.feedback), 1)
            api.evidence = {'id': 10, 'name': 'codex-review-evidence', 'head_sha': HEAD,
                            'app': {'id': 15368}, 'conclusion': 'success',
                            'external_id': 'codex-gate-v1:owner/repo:1:' + HEAD,
                            'output': {'text': json.dumps({'disposition_floors': {'inline:11': 101}})}}
            api.job['status'] = 'completed'
        self.assertIs(self.rerun(api, finish_with_older_state), True)
        self.assertEqual(json.loads(api.feedback[0]['output']['text'])['disposition_floors'],
                         {'inline:11': 202})
        self.assertEqual([path for path, _, _ in api.writes], ['check-runs', 'actions/jobs/8/rerun'])

    def test_observer_waits_longer_than_native_observation_timeout(self):
        api = NativeGitHub()
        api.job['status'] = api.workflow['status'] = 'in_progress'
        elapsed = [0]
        def finish_after_native_timeout():
            elapsed[0] += gate.INTERVAL
            if elapsed[0] > gate.TIMEOUT:
                api.job['status'] = api.workflow['status'] = 'completed'
        self.assertIs(self.rerun(api, finish_after_native_timeout), True)
        self.assertGreater(elapsed[0], gate.TIMEOUT)

    def test_feedback_history_is_scoped_to_current_identity_and_preserves_maximum(self):
        fixture = test_dispositions.DispositionTests()
        fixture.setUp()
        fixture.decisions()
        snapshot = fixture.api.snapshot(1)
        state, _, accepted = fixture.verdict(snapshot)
        self.assertEqual(accepted[0], 'success')
        evidence = {'id': 20, 'name': 'codex-review-evidence', 'head_sha': HEAD,
                    'app': {'id': 15368}, 'conclusion': 'success',
                    'external_id': 'codex-gate-v1:owner/repo:1:' + HEAD,
                    'output': {'text': json.dumps(state)}}
        api = NativeGitHub()
        api.disposition_floors = {'inline:11': 202}
        gate.persist_disposition_floors(api, 1, HEAD, snapshot['base'])
        marker = api.feedback[0]
        other_base = {'ref': 'main', 'sha': 'c' * 40}
        for change in ({'head_sha': 'c' * 40}, {'app': {'id': 1}},
                       {'external_id': 'codex-feedback-v1:owner/other:1:' + HEAD},
                       {'external_id': f'codex-feedback-v1:owner/repo:1:{HEAD}:{gate.digest(other_base)}'}):
            native = FakeGitHub([snapshot], copy.deepcopy(evidence))
            native.pages = lambda *args: [evidence, marker | change]
            native.repo_call = self.native_calls(native, fixture.pr)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertIs(gate.run(native, 1, lambda: 180, expected_head=HEAD), True)
        lower = copy.deepcopy(marker)
        payload = json.loads(lower['output']['text'])
        payload['disposition_floors']['inline:11'] = 201
        lower['output']['text'] = json.dumps(payload)
        native = FakeGitHub([snapshot], copy.deepcopy(evidence))
        native.pages = lambda *args: [evidence, marker, lower]
        native.repo_call = self.native_calls(native, fixture.pr)
        now = [180]
        def sleep(seconds):
            now[0] += seconds
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIs(gate.run(native, 1, lambda: now[0], sleep, expected_head=HEAD), False)
        self.assertEqual(json.loads(native.check['output']['text'])['disposition_floors']['inline:11'], 202)
        for invalid in ({'version': 2}, {'head': 'c' * 40}, {'base': other_base},
                        {'disposition_floors': {'inline:11': -1}},
                        {'disposition_floors': {'inline:11': True}}):
            damaged = copy.deepcopy(marker)
            damaged['output']['text'] = json.dumps(json.loads(marker['output']['text']) | invalid)
            native = FakeGitHub([snapshot], copy.deepcopy(evidence))
            native.pages = lambda *args: [evidence, damaged]
            native.repo_call = self.native_calls(native, fixture.pr)
            with self.assertRaises(ValueError):
                gate.run(native, 1, lambda: 180, expected_head=HEAD)
            self.assertEqual(native.writes, [])

    def test_opted_in_observers_do_not_share_pending_event_slot(self):
        workflow = (ROOT / '.github/workflows/review.yml').read_text()
        self.assertIn("inputs.required-job != '' && github.run_id ||", workflow)
        self.assertIn("timeout-minutes: ${{ inputs.required-job != '' && 35 || 25 }}", workflow)

    @staticmethod
    def native_calls(native, pr):
        call = native.repo_call
        return lambda path, data=None, method=None: copy.deepcopy(pr) if path == 'pulls/1' else call(path, data, method)

    def test_empty_successful_rerun_response_is_accepted_by_api(self):
        response = mock.MagicMock()
        response.read.return_value = b''
        response.headers = {}
        response.__enter__.return_value = response
        with mock.patch.object(gate.urllib.request, 'urlopen', return_value=response):
            self.assertIsNone(gate.GitHub('unused', 'owner/repo').repo_call('actions/jobs/8/rerun', {}, 'POST'))
