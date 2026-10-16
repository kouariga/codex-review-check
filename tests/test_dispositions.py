import copy
import json
import unittest
from unittest import mock

from test_codex_review_gate import gate, sample, FakeGitHub
import contextlib
import io
import urllib.error


def iso(seconds):
    return gate.datetime.datetime.fromtimestamp(seconds, gate.datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class DispositionTests(unittest.TestCase):
    def setUp(self):
        self.api = gate.GitHub('unused', 'owner/repo')
        self.user = {'id': 42, 'login': 'maintainer', 'type': 'User'}
        self.pr = {'state': 'open', 'draft': False, 'head': {'sha': 'a' * 40},
                   'base': {'ref': 'main', 'sha': 'd' * 40}}
        review = {'commit_sha': 'a' * 40, 'status': 'completed',
                  'has_any_findings': True, 'has_unresolved_findings': False}
        self.metadata = {'code_review': review, 'security_review': dict.fromkeys(review)}
        self.comments = [{'id': 1, 'user': {'id': gate.BOT_ID, 'type': 'Bot'},
                          'performed_via_github_app': {'id': gate.CODEX_APP_ID},
                          'created_at': iso(10), 'updated_at': iso(100)}]
        self.inline = [{'id': 11, 'user': {'id': gate.BOT_ID}, 'body': 'Fix old issue',
                        'original_commit_id': 'b' * 40, 'updated_at': iso(50),
                        'pull_request_review_id': 31},
                       {'id': 12, 'user': self.user, 'body': 'Fixed with test',
                        'in_reply_to_id': 11, 'updated_at': iso(80)},
                       {'id': 13, 'user': {'id': gate.BOT_ID}, 'body': 'Current issue',
                        'commit_id': 'a' * 40, 'updated_at': iso(70),
                        'pull_request_review_id': 32}]
        self.resolved = {11: True, 13: True}
        self.reviews = [{'id': 31, 'user': {'id': gate.BOT_ID}, 'state': 'COMMENTED',
                         'body': gate.FINDING_REVIEW_WRAPPER.replace('{commit}', 'b' * 10), 'commit_id': 'b' * 40,
                         'submitted_at': iso(50)},
                        {'id': 32, 'user': {'id': gate.BOT_ID}, 'state': 'COMMENTED',
                         'body': gate.FINDING_REVIEW_WRAPPER.replace('{commit}', 'a' * 10), 'commit_id': 'a' * 40,
                         'submitted_at': iso(70)},
                        {'id': 33, 'user': {'id': gate.BOT_ID}, 'state': 'COMMENTED',
                         'body': 'Substantive unthreaded issue', 'commit_id': 'b' * 40,
                         'submitted_at': iso(50)}]
        self.comments.append({'id': 21, 'user': {'id': gate.BOT_ID},
                              'performed_via_github_app': {'id': gate.CODEX_APP_ID},
                              'body': 'P2 Badge plain finding', 'updated_at': iso(50)})
        self.permission = {'permission': 'write', 'user': {'id': 42}}
        self.reactions = []
        self.api.repo_call = self.call
        self.api.pages = self.pages
        self.api.review_threads = lambda number: copy.deepcopy(self.resolved)
        self.refresh_summary()

    def refresh_summary(self, status='Completed', completion=90):
        self.comments[0]['body'] = (gate.MARKER + '\n<!-- codex-review-state:v1 ' + json.dumps(self.metadata)
                                   + ' -->\n| **Code Review** | **' + status + '** <relative-time datetime="'
                                   + iso(completion) + '">done</relative-time> | `aaaaaaa` | New commits |')

    def call(self, path):
        if path.startswith('pulls/'):
            return copy.deepcopy(self.pr)
        if path.startswith('collaborators/'):
            if isinstance(self.permission, Exception):
                raise self.permission
            return copy.deepcopy(self.permission)
        return {'sha': self.pr['head']['sha']}

    def pages(self, path):
        return copy.deepcopy(self.comments if path == 'issues/1/comments' else
                             self.inline if path == 'pulls/1/comments' else
                             self.reviews if path == 'pulls/1/reviews' else self.reactions)

    def receipt(self, identifier=101, created=110, outcome='fixed'):
        snapshot = self.api.snapshot(1)
        value = {'version': 1, 'repository': 'owner/repo', 'pr': 1, 'head': snapshot['head'],
                 'base': snapshot['base'], 'review': snapshot['review_digest'],
                 'inventory': snapshot['inventory_digest'],
                 'dispositions': [{'finding': item['key'], 'outcome': outcome,
                                   'rationale': 'Verified against the code and behavior',
                                   'evidence': ['https://example.test/commit/abc']}
                                  for item in snapshot['inventory']]}
        comment = {'id': identifier, 'user': copy.deepcopy(self.user), 'created_at': iso(created),
                   'updated_at': iso(created), 'body': gate.DISPOSITIONS + json.dumps(value) + ' -->\nAudit.'}
        self.comments.append(comment)
        return comment, value

    def rewrite(self, comment, value):
        comment['body'] = gate.DISPOSITIONS + json.dumps(value) + ' -->'

    def verdict(self, snapshot=None):
        snapshot = self.api.snapshot(1) if snapshot is None else snapshot
        state = gate.initialize(snapshot, 120)
        first = gate.observe(state, snapshot, 120)
        second = gate.observe(state, snapshot, 150)
        return state, first, second

    def test_runner_renewal_preserves_new_receipt_and_restart_boundaries(self):
        self.receipt()
        original = self.api.snapshot(1)
        state, _, _ = self.verdict(original)
        saved = {"id": 1, "name": gate.CHECK, "head_sha": original["head"],
                 "app": {"id": gate.ACTIONS_APP_ID}, "conclusion": "success",
                 "external_id": "codex-gate-v1:owner/repo:1:" + original["head"],
                 "output": {"text": json.dumps(state)}}
        self.receipt(identifier=102, created=111)
        newer = self.api.snapshot(1)
        for changed, expected in [(newer, {"receipt_floor": 102}),
                                  (original | {"eyes": True}, {"manual_since": 180})]:
            with self.subTest(expected=expected):
                api = FakeGitHub([changed, original], copy.deepcopy(saved))
                now = [180]
                def sleep(seconds):
                    now[0] += seconds
                with contextlib.redirect_stdout(io.StringIO()):
                    gate.run(api, 1, lambda: now[0], sleep)
                self.assertEqual(api.check["conclusion"], "failure")
                self.assertNotIn("success", [w.get("conclusion") for w in api.writes])
                final = json.loads(api.check["output"]["text"])
                for key, value in expected.items():
                    self.assertEqual(final[key], value)

    def test_noncollaborator_404_marker_does_not_block_valid_receipt(self):
        self.receipt()
        self.comments.append({"id": 999, "body": gate.DISPOSITIONS + "broken",
                              "user": {"id": 99, "login": "outsider", "type": "User"}})
        def call(path):
            if path == "collaborators/outsider/permission":
                raise urllib.error.HTTPError("url", 404, "Not found", {}, None)
            return self.call(path)
        self.api.repo_call = call
        self.assertEqual(self.verdict()[2][0], "success")
        for status in (403, 429, 500):
            self.permission = urllib.error.HTTPError("url", status, "Unavailable", {}, None)
            with self.assertRaises(urllib.error.HTTPError):
                self.api.snapshot(1)

    def automatic_history(self, original_head, created):
        self.comments = self.comments[:1]
        self.inline = [self.inline[0] | {"original_commit_id": original_head,
                        "commit_id": self.pr["head"]["sha"], "created_at": iso(created)}]
        self.resolved = {11: True}
        self.reviews = []
        self.metadata["code_review"]["has_any_findings"] = False
        self.refresh_summary()
        initial = self.api.snapshot(1)
        state = gate.initialize(initial | {"summary": "old-summary"}, 80)
        self.reactions = [{"id": 901, "content": "+1", "user": {"id": gate.BOT_ID},
                           "created_at": iso(101)}]
        return state

    def test_current_head_resolved_root_blocks_stale_no_findings_metadata(self):
        state = self.automatic_history("a" * 40, 70)
        gate.observe(state, self.api.snapshot(1), 120)
        self.assertEqual(gate.observe(state, self.api.snapshot(1), 150)[0], "pending")

    def test_late_old_head_root_requires_new_review_or_audit(self):
        state = self.automatic_history("b" * 40, 95)
        gate.observe(state, self.api.snapshot(1), 120)
        self.assertEqual(gate.observe(state, self.api.snapshot(1), 150)[0], "pending")
        self.refresh_summary(completion=160)
        self.comments[0]["updated_at"] = iso(160)
        self.reactions = [{"id": 902, "content": "+1", "user": {"id": gate.BOT_ID},
                           "created_at": iso(161)}]
        gate.observe(state, self.api.snapshot(1), 180)
        self.assertEqual(gate.observe(state, self.api.snapshot(1), 210)[0], "success")

    def test_old_resolved_root_allows_current_clean_review_and_prefers_clean_proof(self):
        state = self.automatic_history("b" * 40, 70)
        self.receipt()
        gate.observe(state, self.api.snapshot(1), 120)
        self.assertEqual(gate.observe(state, self.api.snapshot(1), 150)[0], "success")
        self.assertEqual(state["proof"]["kind"], "clean_codex")

    def test_old_unthreaded_review_blocks_automatic_clean(self):
        state = self.automatic_history("b" * 40, 70)
        self.reviews = [{"id": 99, "user": {"id": gate.BOT_ID}, "state": "COMMENTED",
                         "body": "Unthreaded finding", "commit_id": "b" * 40,
                         "submitted_at": iso(60)}]
        gate.observe(state, self.api.snapshot(1), 120)
        self.assertEqual(gate.observe(state, self.api.snapshot(1), 150)[0], "pending")

    def test_complete_inventory_includes_old_resolved_current_plain_and_unthreaded(self):
        snapshot = self.api.snapshot(1)
        self.assertEqual([row['key'] for row in snapshot['inventory']],
                         ['inline:11', 'inline:13', 'issue:21', 'review:33'])
        self.assertEqual(snapshot['inventory'][0]['replies'][0]['id'], 12)

    def test_no_receipt_or_resolved_flags_cannot_approve(self):
        self.assertEqual(self.verdict()[2][0], 'pending')

    def test_verified_receipt_is_durable_and_separate_from_codex_approval(self):
        self.receipt()
        state, first, second = self.verdict()
        self.assertEqual(first[0], 'pending')
        self.assertEqual(second, ('success', 'Feedback handling verified by maintainer attestation'))
        self.assertEqual(state['proof']['kind'], 'verified_dispositions')
        self.assertEqual(state['proof']['author'], 42)
        self.assertEqual(state['proof']['receipt'], 101)

    def test_justified_wont_fix_is_explicit_attestation(self):
        self.receipt(outcome='wont_fix')
        self.assertEqual(self.verdict()[2][0], 'success')

    def test_invalid_newest_privileged_receipt_never_falls_back(self):
        self.receipt()
        newer, _ = self.receipt(identifier=102, created=111)
        newer['body'] = gate.DISPOSITIONS + '{broken -->'
        self.assertEqual(self.api.snapshot(1)['disposition_receipt']['id'], 102)
        self.assertEqual(self.verdict()[2][0], 'pending')

    def test_unprivileged_marker_spam_is_ignored(self):
        self.receipt()
        self.comments.append({'id': 999, 'body': gate.DISPOSITIONS + 'broken',
                              'created_at': iso(111), 'updated_at': iso(111),
                              'user': {'id': 99, 'login': 'impostor', 'type': 'User'}})
        self.assertEqual(self.verdict()[2][0], 'success')

    def test_permission_requires_immutable_identity_and_write_scope(self):
        self.receipt()
        for permission in ({'permission': 'read', 'user': {'id': 42}},
                           {'permission': 'admin', 'user': {'id': 999}},
                           {'permission': 'write'}, {}):
            self.permission = permission
            self.assertEqual(self.verdict()[2][0], 'pending')

    def test_permission_api_error_cannot_authorize(self):
        self.receipt()
        self.permission = gate.APIBlocked('denied')
        with self.assertRaises(gate.APIBlocked):
            self.api.snapshot(1)

    def test_bot_receipt_is_not_human_attestation(self):
        comment, _ = self.receipt()
        comment['user']['type'] = 'Bot'
        self.assertEqual(self.verdict()[2][0], 'pending')

    def test_strict_receipt_fields_exact_binding_and_disposition_coverage(self):
        comment, original = self.receipt()
        alterations = [dict(original, version=True), dict(original, repository='other/repo'),
                       dict(original, pr=True), dict(original, head='b' * 40),
                       dict(original, base={'ref': 'release', 'sha': 'd' * 40}),
                       dict(original, review='bad'), dict(original, inventory='bad'),
                       dict(original, extra=1), dict(original, dispositions=[]),
                       dict(original, dispositions=original['dispositions'][:-1]),
                       dict(original, dispositions=original['dispositions'] + [original['dispositions'][0]])]
        for field, value in [('outcome', 'resolved'), ('rationale', ' '), ('evidence', []),
                             ('evidence', [' ']), ('extra', True)]:
            altered = copy.deepcopy(original)
            altered['dispositions'][0][field] = value
            alterations.append(altered)
        for altered in alterations:
            self.rewrite(comment, altered)
            self.assertEqual(self.verdict()[2][0], 'pending')

    def test_duplicate_json_keys_and_markers_fail_closed(self):
        comment, value = self.receipt()
        comment['body'] = gate.DISPOSITIONS + json.dumps(value)[:-1] + ', "version":1} -->'
        self.assertEqual(self.verdict()[2][0], 'pending')
        self.rewrite(comment, value)
        comment['body'] += comment['body']
        self.assertEqual(self.verdict()[2][0], 'pending')

    def test_receipt_must_be_unedited_after_completion_and_not_future(self):
        comment, _ = self.receipt()
        for created, updated in [(90, 90), (100, 100), (110, 111), (160, 160)]:
            comment['created_at'], comment['updated_at'] = iso(created), iso(updated)
            self.assertEqual(self.verdict()[2][0], 'pending')

    def test_changed_reply_finding_review_or_resolution_invalidates_receipt(self):
        self.receipt()
        for rows, index, field, value in [(self.inline, 1, 'body', 'Edited fix reply'),
                                           (self.inline, 0, 'body', 'Changed finding'),
                                           (self.reviews, 0, 'body', 'Changed review')]:
            old = rows[index][field]
            rows[index][field] = value
            self.assertEqual(self.verdict()[2][0], 'pending')
            rows[index][field] = old
        self.resolved[11] = False
        self.assertEqual(self.verdict()[2][0], 'pending')

    def test_new_finding_and_new_head_or_base_invalidate(self):
        self.receipt()
        self.inline.append({'id': 14, 'user': {'id': gate.BOT_ID}, 'body': 'New finding'})
        self.resolved[14] = True
        self.assertEqual(self.verdict()[2][0], 'pending')
        self.inline.pop()
        self.resolved.pop(14)
        state = gate.initialize(self.api.snapshot(1), 120)
        for changed in [self.api.snapshot(1) | {'head': 'c' * 40},
                        self.api.snapshot(1) | {'base': {'ref': 'main', 'sha': 'e' * 40}}]:
            self.assertEqual(gate.observe(state, changed, 150)[0], 'stale')

    def test_running_and_failed_codex_block_attestation(self):
        self.receipt()
        for status in ['running', 'failed']:
            self.metadata['code_review']['status'] = status
            self.refresh_summary(status.title())
            verdict = self.verdict()[2][0]
            self.assertEqual(verdict, 'failure' if status == 'failed' else 'pending')

    def test_structured_unresolved_blocks_even_resolved_threads(self):
        self.receipt()
        self.metadata['code_review']['has_unresolved_findings'] = True
        self.refresh_summary()
        self.assertEqual(self.verdict()[2][0], 'pending')

    def test_deleting_latest_receipt_does_not_resurrect_older_one(self):
        self.receipt()
        newer, _ = self.receipt(identifier=102, created=111)
        snapshot = self.api.snapshot(1)
        state = gate.initialize(snapshot, 120)
        gate.observe(state, snapshot, 120)
        self.comments.remove(newer)
        self.assertEqual(gate.observe(state, self.api.snapshot(1), 150)[0], 'pending')
        self.assertEqual(state['receipt_floor'], 102)

    def test_removing_latest_marker_does_not_resurrect_older_receipt(self):
        self.receipt()
        newer, _ = self.receipt(identifier=102, created=111)
        snapshot = self.api.snapshot(1)
        state = gate.initialize(snapshot, 120)
        gate.observe(state, snapshot, 120)
        newer['body'] = 'Removed receipt'
        self.assertEqual(gate.observe(state, self.api.snapshot(1), 150)[0], 'pending')

    def test_new_review_cycle_invalidates_old_receipt_after_eyes_disappear(self):
        self.receipt()
        snapshot = self.api.snapshot(1)
        state = gate.initialize(snapshot, 120)
        gate.observe(state, snapshot, 120)
        gate.observe(state, snapshot | {'eyes': True}, 130)
        self.assertEqual(gate.observe(state, snapshot, 160)[0], 'pending')

    def test_second_observation_and_receipt_identity_must_be_stable(self):
        self.receipt()
        snapshot = self.api.snapshot(1)
        state = gate.initialize(snapshot, 120)
        gate.observe(state, snapshot, 120)
        self.assertEqual(gate.observe(state, snapshot, 149)[0], 'pending')
        self.receipt(identifier=102, created=111)
        changed = self.api.snapshot(1)
        self.assertEqual(gate.observe(state, changed, 150)[0], 'pending')
        self.assertEqual(gate.observe(state, changed, 180)[0], 'success')

    def test_proof_kind_switch_requires_new_confirmation(self):
        clean = sample(thumbs=['new'], summary='complete', completed=True)
        state = gate.initialize(sample(), 0)
        gate.observe(state, clean, 30)
        self.assertEqual(gate.observe(state, clean, 60)[0], 'success')
        self.receipt()
        verified = self.api.snapshot(1)
        self.assertEqual(gate.observe(state, verified, 120)[0], 'pending')
        self.assertEqual(gate.observe(state, verified, 150)[0], 'success')
        self.assertEqual(state['proof']['kind'], 'verified_dispositions')

    def test_attestation_publication_explicitly_denies_clean_codex_approval(self):
        self.receipt()
        state, _, result = self.verdict()
        api = mock.Mock()
        api.last_publish = {}
        gate.publish(api, 5, state, *result)
        payload = api.repo_call.call_args.args[1]
        self.assertIn('not clean Codex approval', payload['output']['summary'])
        self.assertEqual(json.loads(payload['output']['text'])['proof']['kind'], 'verified_dispositions')

    def test_old_thumb_cannot_approve_new_review_even_if_first_seen_late(self):
        clean = sample(thumbs=['new'], summary='complete', completed=True,
                       reaction_times={'new': 20}, completed_at=50)
        state = gate.initialize(sample(), 0)
        self.assertEqual(gate.observe(state, clean, 60)[0], 'pending')
        self.assertEqual(gate.observe(state, clean, 90)[0], 'pending')

    def test_modern_clean_approval_requires_no_findings_and_post_summary_thumb(self):
        self.inline, self.reviews, self.resolved = [], [], {}
        self.comments = self.comments[:1]
        self.metadata['code_review']['has_any_findings'] = False
        self.refresh_summary()
        initial = self.api.snapshot(1)
        state = gate.initialize(initial | {"summary": "old-summary"}, 80)
        for created in [85, 95, 100]:
            self.reactions = [{'id': 901, 'content': '+1', 'user': {'id': gate.BOT_ID},
                               'created_at': iso(created)}]
            self.assertEqual(gate.observe(state, self.api.snapshot(1), 120)[0], 'pending')
        self.reactions[0]['created_at'] = iso(101)
        self.assertEqual(gate.observe(state, self.api.snapshot(1), 120)[0], 'pending')
        self.assertEqual(gate.observe(state, self.api.snapshot(1), 150)[0], 'success')
        self.assertEqual(state['proof']['kind'], 'clean_codex')
        self.metadata['code_review']['has_any_findings'] = True
        self.refresh_summary()
        self.assertEqual(gate.observe(state, self.api.snapshot(1), 180)[0], 'pending')

    def test_no_findings_structured_metadata_without_thumb_stays_pending(self):
        self.inline, self.reviews, self.resolved = [], [], {}
        self.comments = self.comments[:1]
        self.metadata['code_review']['has_any_findings'] = False
        self.refresh_summary()
        self.assertEqual(self.verdict()[2][0], 'pending')

    def test_missing_summary_does_not_make_receipt_approval(self):
        self.receipt()
        self.comments.pop(0)
        self.assertEqual(self.verdict()[2][0], 'pending')

    def test_event_sender_authorized_before_any_run_or_check_mutation(self):
        import tempfile
        event = {'issue': {'number': 1, 'pull_request': {}}, 'sender': self.user,
                 'comment': {'id': 123, 'body': gate.DISPOSITIONS + 'invalid', 'user': self.user}}
        event['issue']['pull_request'] = {'url': 'https://example.test/pr/1'}
        for permission, authorized in [('read', False), ('write', True)]:
            self.permission['permission'] = permission
            with tempfile.NamedTemporaryFile('w+', suffix='.json') as handle:
                json.dump(event, handle)
                handle.flush()
                with mock.patch.dict(gate.os.environ, {'GITHUB_EVENT_NAME': 'issue_comment',
                        'GITHUB_TOKEN': 'unused', 'GITHUB_REPOSITORY': 'owner/repo',
                        'GITHUB_EVENT_PATH': handle.name}), mock.patch.object(gate, 'GitHub', return_value=self.api), \
                        mock.patch.object(gate, 'run') as run:
                    gate.main()
                    self.assertEqual(run.called, authorized)
                    if authorized:
                        self.assertEqual(self.api.receipt_floor, 123)

    def test_marker_removal_and_deletion_events_invalidate_receipt(self):
        import tempfile
        for action in ['edited', 'deleted']:
            event = {'action': action, 'issue': {'number': 1, 'pull_request': {'url': 'pr'}},
                     'sender': self.user, 'comment': {'id': 123, 'body': 'Removed marker', 'user': self.user}}
            if action == 'edited':
                event['changes'] = {'body': {'from': gate.DISPOSITIONS + 'old -->'}}
            else:
                event['comment']['body'] = gate.DISPOSITIONS + 'deleted -->'
            with tempfile.NamedTemporaryFile('w+', suffix='.json') as handle:
                json.dump(event, handle)
                handle.flush()
                with mock.patch.dict(gate.os.environ, {'GITHUB_EVENT_NAME': 'issue_comment',
                        'GITHUB_TOKEN': 'unused', 'GITHUB_REPOSITORY': 'owner/repo',
                        'GITHUB_EVENT_PATH': handle.name}), mock.patch.object(gate, 'GitHub', return_value=self.api), \
                        mock.patch.object(gate, 'run') as run:
                    gate.main()
                    run.assert_called_once()
                    self.assertEqual(self.api.receipt_floor, 123)

    def test_ordinary_comments_and_permission_failure_do_not_start_run(self):
        import tempfile
        event = {'issue': {'number': 1, 'pull_request': {'url': 'pr'}}, 'sender': self.user,
                 'comment': {'id': 123, 'body': 'ordinary comment', 'user': self.user}}
        with tempfile.NamedTemporaryFile('w+', suffix='.json') as handle:
            json.dump(event, handle)
            handle.flush()
            with mock.patch.dict(gate.os.environ, {'GITHUB_EVENT_NAME': 'issue_comment',
                    'GITHUB_TOKEN': 'unused', 'GITHUB_REPOSITORY': 'owner/repo',
                    'GITHUB_EVENT_PATH': handle.name}), mock.patch.object(gate, 'GitHub', return_value=self.api), \
                    mock.patch.object(gate, 'run') as run:
                gate.main()
                run.assert_not_called()
        self.permission = OSError('cannot verify permission')
        with self.assertRaises(OSError):
            self.api.privileged(self.user)

    def test_event_receipt_floor_survives_first_snapshot_initialization(self):
        from test_codex_review_gate import FakeGitHub
        self.receipt()
        snapshot = self.api.snapshot(1)
        api = FakeGitHub([snapshot])
        api.receipt_floor = 102
        now = [120]
        def sleep(seconds):
            now[0] += seconds
        with mock.patch('builtins.print'):
            gate.run(api, 1, lambda: now[0], sleep)
        self.assertEqual(api.check['conclusion'], 'failure')
        self.assertEqual(json.loads(api.check['output']['text'])['receipt_floor'], 102)

    def test_manual_completed_review_with_findings_accepts_verified_receipt_without_clean_comment(self):
        self.comments[0]['body'] = self.comments[0]['body'].replace('New commits', 'Manual request')
        self.receipt()
        snapshot = self.api.snapshot(1)
        self.assertFalse(snapshot['completed'])
        self.assertTrue(snapshot['review_completed'])
        self.assertEqual(snapshot['manual']['comments'], [])
        state, _, result = self.verdict(snapshot)
        self.assertEqual(result[0], 'success')
        self.assertEqual(state['proof']['kind'], 'verified_dispositions')

    def test_invalidated_or_stale_proof_is_removed_from_pending_output(self):
        self.receipt()
        state, _, _ = self.verdict()
        snapshot = self.api.snapshot(1)
        self.assertIn('proof', state)
        gate.observe(state, snapshot | {'disposition_receipt': None}, 180)
        self.assertNotIn('proof', state)
        state['proof'] = {'kind': 'verified_dispositions'}
        gate.observe(state, snapshot | {'head': 'f' * 40}, 180)
        self.assertNotIn('proof', state)

    def test_deletion_event_floor_applies_before_saved_success_fast_path(self):
        from test_codex_review_gate import FakeGitHub
        self.receipt()
        snapshot = self.api.snapshot(1)
        state, _, _ = self.verdict(snapshot)
        saved = {'id': 5, 'name': gate.CHECK, 'head_sha': 'a' * 40,
                 'app': {'id': gate.ACTIONS_APP_ID}, 'conclusion': 'success',
                 'external_id': 'codex-gate-v1:owner/repo:1:' + 'a' * 40,
                 'output': {'text': json.dumps(state)}}
        api = FakeGitHub([snapshot], saved)
        api.receipt_floor = 102
        now = [180]
        def sleep(seconds):
            now[0] += seconds
        with mock.patch('builtins.print'):
            gate.run(api, 1, lambda: now[0], sleep)
        self.assertEqual(api.check['conclusion'], 'failure')
        self.assertEqual(json.loads(api.check['output']['text'])['receipt_floor'], 102)

    def test_additional_body_finding_is_not_hidden_by_associated_inline_threads(self):
        self.reviews[0]['body'] += '\nAdditional unthreaded correctness finding.'
        snapshot = self.api.snapshot(1)
        self.assertIn('review:31', {item['key'] for item in snapshot['inventory']})

    def test_only_known_boilerplate_with_inline_threads_is_deduplicated(self):
        self.reviews[0]['body'] = 'Unrecognized substantive review format'
        snapshot = self.api.snapshot(1)
        self.assertIn('review:31', {item['key'] for item in snapshot['inventory']})

    def test_clean_phrase_inside_a_finding_body_cannot_hide_the_finding(self):
        self.automatic_history('b' * 40, 70)
        for body in ("Earlier review said Didn't find any major issues, but this revision has a P1 authorization bypass.",
                     "Codex Review: Didn't find any major issues. Additional correctness finding."):
            with self.subTest(body=body):
                self.reviews = [{'id': 99, 'user': {'id': gate.BOT_ID}, 'state': 'COMMENTED',
                                 'body': body, 'commit_id': 'a' * 40, 'submitted_at': iso(90)}]
                snapshot = self.api.snapshot(1)
                self.assertIn('review:99', {item['key'] for item in snapshot['inventory']})
                self.assertEqual([row['id'] for row in snapshot['review_findings']], [99])
                state = gate.initialize(sample(), 0)
                gate.observe(state, snapshot, 120)
                self.assertEqual(gate.observe(state, snapshot, 150)[0], 'pending')

    def test_same_second_old_finding_is_not_assumed_to_predate_completion(self):
        self.automatic_history('b' * 40, 90)
        body = self.comments[0]['body']
        self.comments[0]['body'] = body.replace(iso(90), '1970-01-01T00:01:30.451181Z')
        snapshot = self.api.snapshot(1)
        self.assertEqual(snapshot['earliest_completed_at'], 90.451181)
        state = gate.initialize(sample(), 0)
        gate.observe(state, snapshot, 120)
        self.assertEqual(gate.observe(state, snapshot, 150)[0], 'pending')

    def test_later_security_completion_does_not_approve_a_late_code_finding(self):
        self.automatic_history('b' * 40, 95)
        snapshot = self.api.snapshot(1)
        snapshot['earliest_completed_at'] = 90
        snapshot['completed_at'] = 100
        state = gate.initialize(sample(), 0)
        gate.observe(state, snapshot, 120)
        self.assertEqual(gate.observe(state, snapshot, 150)[0], 'pending')
