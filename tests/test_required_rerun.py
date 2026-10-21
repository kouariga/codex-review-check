import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from test_codex_review_gate import gate

HEAD = 'a' * 40
BASE = 'b' * 40
WORKFLOW = '.github/workflows/codex-cloud-review.yml'


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
        self.workflow = {'id': 7, 'event': 'pull_request_target', 'path': WORKFLOW,
                         'pull_requests': [{'number': 1, 'head': {'sha': HEAD},
                                            'base': {'sha': BASE, 'ref': 'main'}}]}
        self.writes = []
        self.denied = False
        self.pr_reads = 0
        self.change_pr = None

    def pages(self, path, key):
        return [copy.deepcopy(self.check)] if self.check else []

    def repo_call(self, path, data=None, method=None):
        if path == 'pulls/1':
            self.pr_reads += 1
            if self.change_pr and self.pr_reads > 1:
                self.pr.update(self.change_pr)
            return copy.deepcopy(self.pr)
        if path == 'actions/jobs/8':
            return copy.deepcopy(self.job)
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
    def rerun(self, api, on_sleep=None):
        now = [0]
        def sleep(seconds):
            now[0] += seconds
            if on_sleep:
                on_sleep()
        with contextlib.redirect_stdout(io.StringIO()):
            return gate.rerun_required_job(api, 1, 'codex-cloud-review', WORKFLOW,
                                           lambda: now[0], sleep)

    def test_completed_success_is_replaced_by_queued_native_rerun(self):
        api = NativeGitHub()
        self.assertIs(self.rerun(api), True)
        self.assertEqual(api.writes, [('actions/jobs/8/rerun', {}, 'POST')])
        self.assertEqual((api.check['status'], api.check['conclusion']), ('queued', None))

    def test_active_job_is_rerun_after_finishing_so_its_success_cannot_escape(self):
        api = NativeGitHub()
        api.job['status'] = api.check['status'] = 'in_progress'
        def finish():
            api.job['status'] = api.check['status'] = 'completed'
        self.assertIs(self.rerun(api, finish), True)
        self.assertEqual(api.writes, [('actions/jobs/8/rerun', {}, 'POST')])
        self.assertEqual(api.pr_reads, 3)

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
                   'GITHUB_WORKFLOW_REF': 'owner/repo/' + WORKFLOW + '@refs/heads/main'}
            with mock.patch.dict(os.environ, env), mock.patch.object(gate, 'GitHub', return_value=api):
                gate.main()
        self.assertEqual(api.writes, [('actions/jobs/8/rerun', {}, 'POST')])
        self.assertEqual(api.check['status'], 'queued')

    def test_empty_successful_rerun_response_is_accepted_by_api(self):
        response = mock.MagicMock()
        response.read.return_value = b''
        response.headers = {}
        response.__enter__.return_value = response
        with mock.patch.object(gate.urllib.request, 'urlopen', return_value=response):
            self.assertIsNone(gate.GitHub('unused', 'owner/repo').repo_call('actions/jobs/8/rerun', {}, 'POST'))
