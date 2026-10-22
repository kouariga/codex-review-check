import copy
import json
import unittest
from unittest import mock

from test_codex_review_gate import gate, sample, FakeGitHub, ROOT
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
                        'original_commit_id': 'b' * 40, 'created_at': iso(50), 'updated_at': iso(50),
                        'pull_request_review_id': 31},
                       {'id': 12, 'user': self.user, 'body': 'Fixed with test',
                        'in_reply_to_id': 11, 'created_at': iso(80), 'updated_at': iso(80)},
                       {'id': 13, 'user': {'id': gate.BOT_ID}, 'body': 'Current issue',
                        'commit_id': 'a' * 40, 'created_at': iso(70), 'updated_at': iso(70),
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
                              'body': 'P2 Badge plain finding', 'created_at': iso(50), 'updated_at': iso(50)})
        self.permission = {'permission': 'write', 'user': {'id': 42}}
        self.reactions = []
        self.api.repo_call = self.call
        self.api.pages = self.pages
        self.api.review_threads = lambda number: copy.deepcopy(self.resolved)
        self.review_updated = {}
        self.api.review_updates = lambda number: {r["id"]: self.review_updated.get(r["id"], r.get("updated_at", r["submitted_at"])) for r in self.reviews}
        self.edit_metadata = {}
        self.api.call = mock.Mock(side_effect=self.graphql)
        self.refresh_summary()

    def graphql(self, path, data=None, method=None):
        self.assertEqual(path, 'graphql')
        identifier = data['variables']['id']
        comment = next(row for row in self.comments + self.inline
                       if row.get('node_id') == identifier)
        node = {'__typename': 'PullRequestReviewComment' if comment.get('in_reply_to_id') else 'IssueComment',
                'id': identifier, 'databaseId': comment['id'], 'body': comment['body'],
                'createdAt': comment['created_at'], 'lastEditedAt': None, 'editor': None}
        for field, value in self.edit_metadata.get(identifier, {}).items():
            if value is ...:
                node.pop(field, None)
            else:
                node[field] = value
        return {'data': {'node': node}}

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

    def decision(self, key, identifier=101, created=110, outcome='fixed'):
        body = (f"Codex review: {outcome}\nHead: {'a' * 40}\nBase: main@{'d' * 40}\n"
                + (f"Finding: {key}\n" if not key.startswith('inline:') else '')
                + "Reason: Verified the fix against the code.\nEvidence: Test passed.\n  More evidence.")
        comment = {'id': identifier, 'node_id': f'Decision_{identifier}',
                   'user': copy.deepcopy(self.user), 'created_at': iso(created),
                   'updated_at': iso(created), 'body': body}
        if key.startswith('inline:'):
            comment['in_reply_to_id'] = int(key.split(':')[1])
            self.inline.append(comment)
        else:
            self.comments.append(comment)
        return comment

    def decisions(self):
        return [self.decision(item['key'], 101 + index) for index, item in enumerate(self.api.snapshot(1)['inventory'])]

    def verdict(self, snapshot=None):
        snapshot = self.api.snapshot(1) if snapshot is None else snapshot
        state = gate.initialize(snapshot, 120)
        first = gate.observe(state, snapshot, 120)
        second = gate.observe(state, snapshot, 150)
        return state, first, second

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

    def test_old_thumb_cannot_approve_new_review_even_if_first_seen_late(self):
        clean = sample(thumbs=['new'], summary='complete', completed=True,
                       reaction_times={'new': 20}, completed_at=50, structured=None)
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

    def test_no_findings_structured_metadata_without_thumb_succeeds(self):
        self.inline, self.reviews, self.resolved = [], [], {}
        self.comments = self.comments[:1]
        self.metadata['code_review']['has_any_findings'] = False
        self.refresh_summary()
        self.assertEqual(self.verdict()[2][0], 'success')

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
    def test_missing_and_null_semantic_fields_have_same_projection(self):
        self.assertEqual(gate.review_evidence({'id': 1}),
                         gate.review_evidence({'id': 1, 'path': None, 'user': None,
                                               'performed_via_github_app': None}))

    def test_all_findings_need_decisions_and_resolved_inline_threads(self):
        self.assertEqual(self.verdict()[2][0], 'pending')
        decisions = self.decisions()
        state, first, second = self.verdict()
        self.assertEqual(first[0], 'pending')
        self.assertEqual(second[0], 'success')
        self.assertEqual(state['proof']['kind'], 'verified_dispositions')
        self.assertEqual(len(state['proof']['replies']), 4)
        for decision in decisions:
            source = self.inline if decision.get('in_reply_to_id') else self.comments
            source.remove(decision)
            self.assertEqual(self.verdict()[2][0], 'pending')
            source.append(decision)
        self.resolved[11] = False
        self.assertEqual(self.verdict()[2][0], 'pending')

    def test_wont_fix_and_multiline_prose(self):
        for decision in self.decisions():
            decision['body'] = decision['body'].replace('Codex review: fixed', "Codex review: won't-fix")
        self.assertEqual(self.verdict()[2][0], 'success')

    def test_strict_fields_and_binding(self):
        first = self.decisions()[0]
        original = first['body']
        for changed in [original.replace('a' * 40, 'b' * 40),
                        original.replace('main@', 'release@'), original.replace('d' * 40, 'c' * 40),
                        original.replace('Reason: Verified the fix against the code.', 'Reason: '),
                        original.split('Evidence:')[0], original + '\nHead: ' + 'a' * 40,
                        original.replace('Codex review: fixed', 'Codex review: resolved')]:
            with self.subTest(changed=changed):
                first['body'] = changed
                self.assertEqual(self.verdict()[2][0], 'pending')
        first['body'] = original
        self.assertEqual(self.verdict()[2][0], 'success')

    def test_metadata_only_updates_accept_unedited_inline_and_top_level_decisions(self):
        decisions = self.decisions()
        for key in ('inline:11', 'issue:21'):
            with self.subTest(key=key):
                decision = decisions[0] if key == 'inline:11' else next(
                    row for row in decisions if 'Finding: issue:21' in row['body'])
                decision['updated_at'] = iso(111)
                try:
                    self.assertEqual(self.verdict()[2][0], 'success')
                    self.assertTrue(self.api.call.called)
                finally:
                    decision['updated_at'] = decision['created_at']

    def test_explicit_edit_metadata_rejects_even_equal_rest_timestamps(self):
        decisions = self.decisions()
        for decision in (decisions[0], next(row for row in decisions if 'Finding: issue:21' in row['body'])):
            for metadata in ({'lastEditedAt': iso(111)}, {'editor': {'login': 'maintainer'}},
                             {'lastEditedAt': iso(110), 'editor': {'login': 'maintainer'}}):
                with self.subTest(node=decision['node_id'], metadata=metadata):
                    self.edit_metadata = {decision['node_id']: metadata}
                    self.assertEqual(self.verdict()[2][0], 'pending')

    def test_missing_edit_fields_cannot_attest_that_decisions_are_unedited(self):
        decisions = self.decisions()
        for decision in (decisions[0], next(row for row in decisions if 'Finding: issue:21' in row['body'])):
            for field in ('lastEditedAt', 'editor'):
                with self.subTest(node=decision['node_id'], field=field):
                    self.edit_metadata = {decision['node_id']: {field: ...}}
                    self.assertEqual(self.verdict()[2][0], 'pending')

    def test_graphql_comment_must_match_the_selected_rest_decision(self):
        decisions = self.decisions()
        for decision in (decisions[0], next(row for row in decisions if 'Finding: issue:21' in row['body'])):
            for field, value in (('__typename', 'PullRequestReview'), ('id', 'Other_node'),
                                 ('databaseId', 999), ('body', 'Different decision'),
                                 ('createdAt', iso(109))):
                with self.subTest(node=decision['node_id'], field=field):
                    self.edit_metadata = {decision['node_id']: {field: value}}
                    self.assertEqual(self.verdict()[2][0], 'pending')

    def test_newest_edited_decision_does_not_restore_an_older_valid_reply(self):
        self.decisions()
        for key in ('inline:11', 'issue:21'):
            with self.subTest(key=key):
                decision = self.decision(key, 202 if key == 'inline:11' else 203, 111)
                self.edit_metadata[decision['node_id']] = {'lastEditedAt': iso(112)}
                try:
                    snapshot = self.api.snapshot(1)
                    self.assertEqual(snapshot['dispositions'][key]['id'], decision['id'])
                    self.assertEqual(self.verdict(snapshot)[2][0], 'pending')
                finally:
                    (self.inline if key.startswith('inline:') else self.comments).remove(decision)
                    self.edit_metadata.clear()

    def test_decision_must_be_unedited_after_review_and_finding_changes(self):
        first = self.decisions()[0]
        for created, updated in [(90, 90), (100, 100), (110, 111), (160, 160)]:
            first['created_at'], first['updated_at'] = iso(created), iso(updated)
            self.assertEqual(self.verdict()[2][0], 'pending')
        first['created_at'] = first['updated_at'] = iso(110)
        self.inline[0]['updated_at'] = iso(111)
        self.assertEqual(self.verdict()[2][0], 'pending')
        self.inline[0]['updated_at'] = iso(50)
        self.inline.append({'id': 200, 'in_reply_to_id': 11, 'body': 'More detail',
                            'user': {'id': gate.BOT_ID}, 'updated_at': iso(111)})
        self.assertEqual(self.verdict()[2][0], 'pending')

    def test_newest_invalid_decision_blocks_older_valid_decision(self):
        self.decisions()
        newer = self.decision('inline:11', 202, 111)
        newer['body'] = 'Codex review: unsupported'
        self.assertFalse(self.api.snapshot(1)['dispositions']['inline:11']['valid'])
        self.assertEqual(self.verdict()[2][0], 'pending')

    def test_deleted_or_edited_away_decision_cannot_resurrect_older_reply(self):
        self.decisions()
        newer = self.decision('inline:11', 202, 111)
        state, _, second = self.verdict()
        self.assertEqual(second[0], 'success')
        newer['body'] = 'ordinary comment'
        self.assertEqual(gate.observe(state, self.api.snapshot(1), 180)[0], 'pending')
        self.inline.remove(newer)
        self.assertEqual(gate.observe(state, self.api.snapshot(1), 210)[0], 'pending')
        self.assertEqual(state['disposition_floors']['inline:11'], 202)

    def test_permission_and_author_identity_required(self):
        self.decisions()
        for permission in [{'permission': 'read', 'user': {'id': 42}},
                           {'permission': 'write', 'user': {'id': 1}}, {},
                           {'permission': 'admin'}]:
            self.permission = permission
            self.assertEqual(self.verdict()[2][0], 'pending')
        self.permission = gate.APIBlocked('denied')
        with self.assertRaises(gate.APIBlocked):
            self.api.snapshot(1)

    def test_outsiders_do_not_poison_floors_and_404_is_ignored(self):
        self.decisions()
        outsider = self.decision('inline:11', 999, 111)
        outsider['user'] = {'id': 99, 'login': 'outsider', 'type': 'User'}
        def call(path):
            if path == 'collaborators/outsider/permission':
                raise urllib.error.HTTPError('url', 404, 'Not found', {}, None)
            return self.call(path)
        self.api.repo_call = call
        state, _, result = self.verdict()
        self.assertEqual(result[0], 'success')
        self.assertEqual(state['disposition_floors']['inline:11'], 101)

    def test_bot_author_cannot_attest(self):
        decisions = self.decisions()
        decisions[0]['user']['type'] = 'Bot'
        self.assertEqual(self.verdict()[2][0], 'pending')

    def test_active_failed_and_unresolved_reviews_block_dispositions(self):
        self.decisions()
        for status in ['running', 'failed']:
            self.metadata['code_review']['status'] = status
            self.refresh_summary(status.title())
            self.assertEqual(self.verdict()[2][0], 'failure' if status == 'failed' else 'pending')
        self.metadata['code_review']['status'] = 'completed'
        self.metadata['code_review']['has_unresolved_findings'] = True
        self.refresh_summary()
        self.assertEqual(self.verdict()[2][0], 'pending')

    def test_restart_after_observed_active_review_requires_new_replies(self):
        self.decisions()
        state, _, _ = self.verdict()
        snapshot = self.api.snapshot(1)
        gate.observe(state, snapshot | {'eyes': True}, 160)
        self.assertEqual(gate.observe(state, snapshot, 190)[0], 'pending')

    def test_new_finding_requires_additional_decision(self):
        self.decisions()
        self.inline.append({'id': 14, 'user': {'id': gate.BOT_ID}, 'body': 'New finding',
                            'created_at': iso(111), 'updated_at': iso(111)})
        self.resolved[14] = True
        self.assertEqual(self.verdict()[2][0], 'pending')

    def test_new_head_or_base_rejects_replies_even_for_fresh_observer(self):
        self.decisions()
        for field, value in [('head', {'sha': 'f' * 40}), ('base', {'ref': 'main', 'sha': 'e' * 40})]:
            old = self.pr[field]
            self.pr[field] = value
            if field == 'head':
                self.metadata['code_review']['commit_sha'] = 'f' * 40
                self.refresh_summary()
            self.assertEqual(self.verdict()[2][0], 'pending')
            self.pr[field] = old
            self.metadata['code_review']['commit_sha'] = 'a' * 40
            self.refresh_summary()

    def test_stable_second_poll_and_reply_identity(self):
        self.decisions()
        snapshot = self.api.snapshot(1)
        state = gate.initialize(snapshot, 120)
        gate.observe(state, snapshot, 120)
        self.assertEqual(gate.observe(state, snapshot, 149)[0], 'pending')
        self.decision('inline:11', 202, 111)
        changed = self.api.snapshot(1)
        self.assertEqual(gate.observe(state, changed, 150)[0], 'pending')
        self.assertEqual(gate.observe(state, changed, 180)[0], 'success')

    def test_metadata_labels_do_not_change_disposition_or_inventory_fingerprints(self):
        decisions = self.decisions()
        original = self.api.snapshot(1)
        for row in self.comments + self.inline + self.reviews:
            row['author_association'] = 'OWNER'
            row['reactions'] = {'total_count': 12}
            row['user']['avatar_url'] = 'https://other.example/avatar'
        changed = self.api.snapshot(1)
        for key in ['inventory_digest', 'review_digest', 'dispositions']:
            self.assertEqual(original[key], changed[key])

    def test_old_aggregate_receipt_has_no_authority(self):
        self.comments.append({'id': 999, 'user': self.user,
                              'body': '<!-- codex-review-dispositions:v1 {} -->'})
        self.assertEqual(self.verdict()[2][0], 'pending')

    def test_manual_completed_findings_need_no_clean_comment(self):
        self.decisions()
        self.comments[0]['body'] = self.comments[0]['body'].replace('New commits', 'Manual request')
        snapshot = self.api.snapshot(1)
        self.assertTrue(snapshot['completed'])
        self.assertTrue(snapshot['review_completed'])
        self.assertTrue(snapshot['structured']['has_findings'])
        self.assertEqual(self.verdict(snapshot | {'dispositions': {}})[2][0], 'pending')
        self.assertEqual(self.verdict(snapshot)[2][0], 'success')

    def test_unknown_completed_flags_need_fresh_receipt_and_every_disposition_without_reaction(self):
        self.metadata['code_review'].update(has_any_findings=None, has_unresolved_findings=None)
        self.refresh_summary()
        receipt = json.loads((ROOT / 'tests/fixtures/modern-clean.json').read_text())['comments'][1]
        receipt.update(created_at=iso(88), updated_at=iso(88))
        self.comments.append(receipt)
        decisions = self.decisions()
        snapshot = self.api.snapshot(1)
        state = gate.initialize(snapshot, 120, observed_since=80)
        self.assertEqual(gate.observe(state, snapshot, 120)[0], 'pending')
        self.assertEqual(gate.observe(state, snapshot, 150)[0], 'success')
        self.assertEqual(state['proof']['kind'], 'verified_dispositions')
        self.assertEqual(len(state['proof']['replies']), 4)
        self.assertIn('receipt', state['proof'])
        for change in ('missing', 'head', 'base', 'outsider', 'stale', 'unresolved', 'receipt'):
            with self.subTest(change=change):
                saved_inline, saved_comments = copy.deepcopy(self.inline), copy.deepcopy(self.comments)
                if change == 'missing':
                    self.inline.remove(decisions[0])
                elif change in ('head', 'base', 'stale'):
                    decision = next(c for c in self.inline if c['id'] == decisions[0]['id'])
                    if change == 'head':
                        decision['body'] = decision['body'].replace('Head: ' + 'a' * 40, 'Head: ' + 'c' * 40)
                    elif change == 'base':
                        decision['body'] = decision['body'].replace('Base: main@', 'Base: other@')
                    else:
                        decision['created_at'] = decision['updated_at'] = iso(89)
                elif change == 'outsider':
                    self.permission = {'permission': 'read', 'user': {'id': 42}}
                elif change == 'unresolved':
                    self.resolved[11] = False
                else:
                    self.comments.remove(receipt)
                changed = self.api.snapshot(1)
                state = gate.initialize(changed, 120, observed_since=80)
                self.assertEqual(gate.observe(state, changed, 120)[0], 'pending')
                self.assertEqual(gate.observe(state, changed, 150)[0], 'pending')
                self.inline, self.comments = saved_inline, saved_comments
                self.permission = {'permission': 'write', 'user': {'id': 42}}
                self.resolved[11] = True

    def test_unknown_flags_receipt_must_follow_every_finding(self):
        self.metadata['code_review'].update(has_any_findings=None, has_unresolved_findings=None)
        self.refresh_summary()
        receipt = json.loads((ROOT / 'tests/fixtures/modern-clean.json').read_text())['comments'][1]
        receipt.update(created_at=iso(88), updated_at=iso(88))
        self.comments.append(receipt)
        for evidence in (self.inline[0], self.inline[2], self.comments[1], self.reviews[0], self.reviews[1], self.reviews[2]):
            for time in (88, 89):
                with self.subTest(evidence=evidence['id'], time=time):
                    original = copy.deepcopy(evidence)
                    field = 'submitted_at' if evidence in self.reviews else 'updated_at'
                    evidence[field] = iso(time)
                    self.decisions()
                    snapshot = self.api.snapshot(1)
                    state = gate.initialize(snapshot, 120, observed_since=80)
                    gate.observe(state, snapshot, 120)
                    self.assertEqual(gate.observe(state, snapshot, 150)[0], 'pending')
                    evidence.clear()
                    evidence.update(original)

    def test_known_flags_reject_disposition_after_review_edit(self):
        self.decisions()
        self.assertEqual(self.verdict(self.api.snapshot(1))[2][0], 'success')
        self.review_updated[33] = iso(120)
        snapshot = self.api.snapshot(1)
        self.assertFalse(snapshot['dispositions']['review:33']['valid'])
        self.assertEqual(self.verdict(snapshot)[2][0], 'pending')

    def test_unknown_flags_require_authoritative_review_update_chronology(self):
        self.metadata['code_review'].update(has_any_findings=None, has_unresolved_findings=None)
        self.refresh_summary()
        receipt = json.loads((ROOT / 'tests/fixtures/modern-clean.json').read_text())['comments'][1]
        receipt.update(created_at=iso(88), updated_at=iso(88))
        self.comments.append(receipt)
        self.decisions()
        for update in (iso(88), iso(89)):
            self.review_updated[33] = update
            snapshot = self.api.snapshot(1)
            state = gate.initialize(snapshot, 120, observed_since=80)
            gate.observe(state, snapshot, 120)
            self.assertEqual(gate.observe(state, snapshot, 150)[0], 'pending')
        self.review_updated[33] = None
        with self.assertRaises(ValueError):
            self.api.snapshot(1)
        self.api.review_updates = lambda number: {}
        with self.assertRaises(KeyError):
            self.api.snapshot(1)

    def event(self, name, comment, previous=None, action='created'):
        import tempfile
        event = {'action': action, 'issue': {'number': 1, 'pull_request': {'url': 'pr'}},
                 'pull_request': {'number': 1}, 'sender': self.user, 'comment': comment}
        if previous is not None:
            event['changes'] = {'body': {'from': previous}}
        with tempfile.NamedTemporaryFile('w+', suffix='.json') as handle:
            json.dump(event, handle)
            handle.flush()
            with mock.patch.dict(gate.os.environ, {'GITHUB_EVENT_NAME': name,
                    'GITHUB_TOKEN': 'unused', 'GITHUB_REPOSITORY': 'owner/repo',
                    'GITHUB_EVENT_PATH': handle.name}), mock.patch.object(gate, 'GitHub', return_value=self.api), \
                    mock.patch.object(gate, 'run') as run:
                gate.main()
                return run.called

    def test_reply_events_and_marker_removal_preserve_per_finding_floors(self):
        comment = self.decision('inline:11')
        self.assertTrue(self.event('pull_request_review_comment', comment))
        self.assertEqual(self.api.disposition_floors, {'inline:11': 101})
        previous = comment['body']
        comment['body'] = 'Removed decision'
        self.assertTrue(self.event('pull_request_review_comment', comment, previous, 'edited'))
        self.assertEqual(self.api.disposition_floors, {'inline:11': 101})
        comment['body'] = previous
        self.assertTrue(self.event('pull_request_review_comment', comment, action='deleted'))

    def test_current_maintainer_can_edit_or_delete_revoked_authors_decision(self):
        comment = self.decision('inline:11')
        comment['user'] = {'id': 99, 'login': 'former-maintainer', 'type': 'User'}
        with mock.patch.object(self.api, 'privileged', side_effect=lambda user: user['id'] == 42):
            for event_name in ('issue_comment', 'pull_request_review_comment'):
                with self.subTest(event=event_name):
                    self.assertEqual(self.event(event_name, comment, action='created'),
                                     event_name == 'pull_request_review_comment')
                    self.assertFalse(hasattr(self.api, 'disposition_floors'))
                    for action in ('edited', 'deleted'):
                        self.assertTrue(self.event(event_name, comment, action=action))
                        self.assertEqual(self.api.disposition_floors, {'inline:11': 101})
                    del self.api.disposition_floors
            self.user = {'id': 98, 'login': 'outsider', 'type': 'User'}
            for action in ('edited', 'deleted'):
                self.assertFalse(self.event('issue_comment', comment, action=action))
                self.assertTrue(self.event('pull_request_review_comment', comment, action=action))
                self.assertFalse(hasattr(self.api, 'disposition_floors'))

    def test_top_level_target_edits_floor_both_old_and_new_targets(self):
        comment = self.decision('issue:21')
        previous = comment['body']
        comment['body'] = previous.replace('issue:21', 'review:33')
        self.assertTrue(self.event('issue_comment', comment, previous, 'edited'))
        self.assertEqual(self.api.disposition_floors, {'issue:21': 101, 'review:33': 101})

    def test_outsider_decisions_cannot_advance_floors(self):
        comment = self.decision('inline:11')
        self.permission = {'permission': 'read', 'user': {'id': 42}}
        self.assertTrue(self.event('pull_request_review_comment', comment))
        self.assertFalse(self.event('issue_comment', comment))
        self.assertFalse(hasattr(self.api, 'disposition_floors'))
        self.permission = {'permission': 'write', 'user': {'id': 42}}
        comment['body'] = 'Ordinary conversation'
        self.assertFalse(self.event('issue_comment', comment))
        self.assertTrue(self.event('pull_request_review_comment', comment))
        self.assertFalse(hasattr(self.api, 'disposition_floors'))

    def test_deleted_reply_floor_survives_new_observer_and_saved_success_renewal(self):
        self.decisions()
        snapshot = self.api.snapshot(1)
        state, _, _ = self.verdict(snapshot)
        saved = {'id': 5, 'name': gate.CHECK, 'head_sha': 'a' * 40,
                 'app': {'id': gate.ACTIONS_APP_ID}, 'conclusion': 'success',
                 'external_id': 'codex-gate-v1:owner/repo:1:' + 'a' * 40,
                 'output': {'text': json.dumps(state)}}
        for previous in [None, saved]:
            api = FakeGitHub([snapshot], previous)
            api.disposition_floors = {'inline:11': 202}
            now = [180]
            def sleep(seconds):
                now[0] += seconds
            with contextlib.redirect_stdout(io.StringIO()):
                gate.run(api, 1, lambda: now[0], sleep)
            self.assertEqual(api.check['conclusion'], 'failure')
            self.assertEqual(json.loads(api.check['output']['text'])['disposition_floors']['inline:11'], 202)

    def test_renewal_keeps_newly_observed_reply_floor(self):
        self.decisions()
        original = self.api.snapshot(1)
        state, _, _ = self.verdict(original)
        saved = {'id': 5, 'name': gate.CHECK, 'head_sha': 'a' * 40,
                 'app': {'id': gate.ACTIONS_APP_ID}, 'conclusion': 'success',
                 'external_id': 'codex-gate-v1:owner/repo:1:' + 'a' * 40,
                 'output': {'text': json.dumps(state)}}
        self.decision('inline:11', 202, 111)
        api = FakeGitHub([self.api.snapshot(1), original], saved)
        now = [180]
        def sleep(seconds):
            now[0] += seconds
        with contextlib.redirect_stdout(io.StringIO()):
            gate.run(api, 1, lambda: now[0], sleep)
        self.assertEqual(api.check['conclusion'], 'failure')
        self.assertEqual(json.loads(api.check['output']['text'])['disposition_floors']['inline:11'], 202)
    def test_permission_lookup_is_once_per_author_per_snapshot_then_rechecked(self):
        self.decisions()
        for identifier in range(300, 400):
            self.decision('inline:11', identifier)
        calls = []
        def call(path):
            calls.append(path)
            return self.call(path)
        self.api.repo_call = call
        self.assertEqual(self.verdict()[2][0], 'success')
        self.assertEqual(calls.count('collaborators/maintainer/permission'), 1)
        self.permission['permission'] = 'read'
        self.assertEqual(self.verdict()[2][0], 'pending')
        self.assertEqual(calls.count('collaborators/maintainer/permission'), 2)

    def test_inline_comment_cannot_supply_top_level_finding_decision(self):
        self.decisions()
        self.comments = [comment for comment in self.comments
                         if 'Finding: issue:21' not in comment['body']]
        wrong_surface = self.decision('issue:21', 500)
        self.comments.remove(wrong_surface)
        self.inline.append(wrong_surface)
        self.assertEqual(self.verdict()[2][0], 'pending')

    def test_noninline_finding_changes_require_new_decision(self):
        self.decisions()
        for row, field in [(self.comments[1], 'updated_at'), (self.reviews[2], 'submitted_at')]:
            old = row[field]
            row[field] = iso(111)
            self.assertEqual(self.verdict()[2][0], 'pending')
            row[field] = old

    def test_published_attestation_keeps_distinct_proof_and_summary(self):
        self.decisions()
        state, _, result = self.verdict()
        api = mock.Mock()
        api.last_publish = {}
        gate.publish(api, 5, state, *result)
        payload = api.repo_call.call_args.args[1]
        self.assertIn('not clean Codex approval', payload['output']['summary'])
        self.assertEqual(json.loads(payload['output']['text'])['proof']['kind'], 'verified_dispositions')

    def test_invalidation_removes_prior_success_proof(self):
        self.decisions()
        state, _, _ = self.verdict()
        snapshot = self.api.snapshot(1)
        self.assertIn('proof', state)
        gate.observe(state, snapshot | {'dispositions': {}}, 180)
        self.assertNotIn('proof', state)
        state['proof'] = {'kind': 'verified_dispositions'}
        gate.observe(state, snapshot | {'head': 'f' * 40}, 180)
        self.assertNotIn('proof', state)
    def test_human_context_requires_new_decision_but_decision_replies_do_not_deadlock(self):
        self.decisions()
        self.assertEqual(self.verdict()[2][0], 'success')
        self.inline[1]['updated_at'] = iso(111)
        self.assertEqual(self.verdict()[2][0], 'pending')
        self.decision('inline:11', 205, 112)
        self.assertEqual(self.verdict()[2][0], 'success')

    def test_every_bot_review_submission_and_update_precedes_decisions(self):
        self.decisions()
        for field in ['submitted_at', 'updated_at']:
            with self.subTest(field=field):
                old = self.reviews[0].get(field)
                self.reviews[0][field] = iso(111)
                self.assertEqual(self.verdict()[2][0], 'pending')
                if old is None:
                    self.reviews[0].pop(field)
                else:
                    self.reviews[0][field] = old

    def test_newer_duplicate_field_reply_blocks_previous_valid_reply(self):
        self.decisions()
        malformed = self.decision('inline:11', 205, 111)
        malformed['body'] += '\nReason: Duplicate reason'
        self.assertEqual(self.verdict()[2][0], 'pending')

    def test_top_level_target_duplicate_is_invalid_and_preserves_event_previous_target(self):
        self.decisions()
        malformed = self.decision('issue:21', 205, 111)
        previous = malformed['body']
        malformed['body'] += '\nFinding: review:33'
        self.assertTrue(self.event('issue_comment', malformed, previous, 'edited'))
        self.assertEqual(self.api.disposition_floors, {'issue:21': 205, 'review:33': 205})
        self.assertEqual(self.verdict()[2][0], 'pending')
