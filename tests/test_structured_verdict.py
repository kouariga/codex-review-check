import contextlib
import copy
import io
import json
import unittest

import test_codex_review_gate as fixtures
from test_codex_review_gate import gate, FakeGitHub


class StructuredVerdictTests(unittest.TestCase):
    def metadata(self, **changes):
        review = {'commit_sha': 'a' * 40, 'status': 'completed',
                  'has_any_findings': False, 'has_unresolved_findings': False}
        review.update(changes)
        return {'code_review': review, 'security_review': dict.fromkeys(review)}

    def api(self, metadata=None, *, display='', reactions=(), updated=None):
        metadata = self.metadata() if metadata is None else metadata
        api = fixtures.StructuredReviewTests().api(metadata)
        original = api.pages
        def pages(path):
            if path.endswith('/reactions'):
                return copy.deepcopy(list(reactions))
            result = copy.deepcopy(original(path))
            if path == 'issues/1/comments':
                result[0]['body'] = gate.MARKER + '\n<!-- codex-review-state:v1 ' + json.dumps(metadata) + ' -->\n' + display
                result[0]['updated_at'] = updated
            return result
        api.pages = pages
        return api

    def result(self, snapshot):
        state = gate.initialize(snapshot, 1000)
        self.assertEqual(gate.observe(state, snapshot, 1000)[0], 'pending')
        return gate.observe(state, snapshot, 1030), state

    def run_gate(self, api):
        now = [1000]
        def sleep(seconds):
            now[0] += seconds
        with contextlib.redirect_stdout(io.StringIO()):
            return gate.run(api, 1, lambda: now[0], sleep, expected_head='a' * 40)

    def test_structured_clean_needs_no_display_or_commit_lookup(self):
        for display in ('', 'Review complete.', '| a completely different table |',
                        '| **Code Review** | **Failed** | `bbbbbbb` | Unknown trigger |'):
            with self.subTest(display=display):
                api = self.api(display=display)
                original = api.repo_call
                def call(path):
                    self.assertFalse(path.startswith('commits/'))
                    return original(path)
                api.repo_call = call
                snapshot = api.snapshot(1)
                self.assertEqual(self.result(snapshot)[0][0], 'success')

    def test_running_summary_and_newer_reaction_cannot_poison_later_clean_verdict(self):
        reaction = {'id': 2, 'content': '+1', 'user': {'id': gate.BOT_ID},
                    'created_at': '1970-01-01T00:16:33Z'}
        display = ('| **Code Review** | **{status}** '
                   '<relative-time datetime="1970-01-01T00:16:32Z">done</relative-time>'
                   ' | `aaaaaaa` | New commits |')
        running = self.api(self.metadata(status='running'), reactions=[reaction],
                           display=display.format(status='Running'),
                           updated='1970-01-01T00:16:34Z').snapshot(1)
        complete = self.api(reactions=[reaction], display=display.format(status='Completed'),
                            updated='1970-01-01T00:16:34Z').snapshot(1)
        state = gate.initialize(running, 990)
        self.assertEqual(gate.observe(state, running, 994)[0], 'pending')
        self.assertEqual(state['review_since'], 994)
        self.assertEqual(gate.observe(state, complete, 1024)[0], 'pending')
        self.assertEqual(gate.observe(state, complete, 1054)[0], 'success')
        self.assertTrue(self.run_gate(FakeGitHub([running, complete, complete])))

    def test_cosmetic_edits_reactions_and_display_times_keep_same_proof(self):
        original = self.api().snapshot(1)
        state = gate.initialize(original, 1000)
        gate.observe(state, original, 1000)
        proof, candidate = copy.deepcopy(state['proof']), state['candidate']
        changed = self.api(display='| **Code Review** | **Running** | `bbbbbbb` | New wording |',
                           updated='2099-01-01T00:00:00Z', reactions=[
                               {'id': 99, 'content': 'eyes', 'user': {'id': gate.BOT_ID}},
                               {'id': 100, 'content': '+1', 'user': {'id': gate.BOT_ID}}]).snapshot(1)
        self.assertNotEqual(original['summary'], changed['summary'])
        self.assertEqual(gate.observe(state, changed, 1030)[0], 'success')
        self.assertEqual(state['proof'], proof)
        self.assertEqual(state['candidate'], candidate)
        self.assertEqual(state['evidence'], {'expected_sha': 'a' * 40, 'comment_id': 1,
                                            'reviews': changed['structured']['reviews'], 'observed_at': 1030})

    def test_old_sha_and_new_thumb_wait_for_expected_sha(self):
        snapshot = self.api(self.metadata(commit_sha='b' * 40), reactions=[
            {'id': 99, 'content': '+1', 'user': {'id': gate.BOT_ID}}]).snapshot(1)
        result, state = self.result(snapshot)
        self.assertEqual(result[0], 'pending')
        self.assertIn('expected head', result[1])
        self.assertEqual(state['evidence']['reviews']['code_review']['commit_sha'], 'b' * 40)
        api = FakeGitHub([snapshot])
        self.assertFalse(self.run_gate(api))
        self.assertEqual(api.check['head_sha'], 'a' * 40)
        self.assertEqual(api.check['conclusion'], 'failure')

    def test_populated_security_is_required_without_a_display_row(self):
        for status in ('running', 'queued', 'pending', 'failed', 'completed'):
            for security_head in ('a' * 40, 'b' * 40):
                with self.subTest(status=status, head=security_head):
                    metadata = self.metadata()
                    metadata['security_review'] = metadata['code_review'] | {'status': status, 'commit_sha': security_head}
                    snapshot = self.api(metadata).snapshot(1)
                    state = gate.initialize(snapshot, 1000)
                    gate.observe(state, snapshot, 1000)
                    result = gate.observe(state, snapshot, 1030)[0]
                    expected = ('success' if status == 'completed' else 'failure' if status == 'failed' else 'pending') if security_head == 'a' * 40 else 'pending'
                    self.assertEqual(result, expected)


    def test_prose_mention_of_security_does_not_change_review_scope(self):
        snapshot = self.api(display='Security Review is optional and was not requested.').snapshot(1)
        self.assertEqual(self.result(snapshot)[0][0], 'success')


    def test_link_targets_do_not_advertise_security_scope(self):
        for label in ('[Code Review](https://example.com/security)', '[Code Review][security-docs]'):
            snapshot = self.api(display=f'| {label} | Completed | `aaaaaaa` | New commits |').snapshot(1)
            self.assertEqual(self.result(snapshot)[0][0], 'success')

    def test_unknown_flags_cannot_use_reaction_as_clean_verdict(self):
        for flags in ((None, None), (None, False), (False, None), (False, True)):
            metadata = self.metadata(has_any_findings=flags[0], has_unresolved_findings=flags[1])
            api = self.api(metadata, reactions=[{'id': 99, 'content': '+1', 'user': {'id': gate.BOT_ID}}])
            with self.subTest(flags=flags):
                try:
                    snapshot = api.snapshot(1)
                except ValueError:
                    continue
                self.assertNotEqual(self.result(snapshot)[0][0], 'success')

    def test_unknown_flags_without_display_preserve_diagnostic_fields(self):
        snapshot = self.api(self.metadata(has_any_findings=None, has_unresolved_findings=None)).snapshot(1)
        result, state = self.result(snapshot)
        self.assertEqual(result[0], 'pending')
        self.assertIn('unknown finding flags', result[1])
        self.assertIsNone(state['evidence']['reviews']['code_review']['has_any_findings'])
        self.assertIsNone(state['evidence']['reviews']['code_review']['has_unresolved_findings'])

    def test_findings_and_active_status_invalidate_structured_clean(self):
        complete = self.api().snapshot(1)
        for changes in ({'status': 'running'}, {'status': 'queued'}, {'has_any_findings': True},
                        {'has_any_findings': True, 'has_unresolved_findings': True}):
            with self.subTest(changes=changes):
                _, state = self.result(complete)
                changed = self.api(self.metadata(**changes)).snapshot(1)
                self.assertEqual(gate.observe(state, changed, 1060)[0], 'pending')
                self.assertNotIn('proof', state)
                self.assertEqual(gate.observe(state, complete, 1090)[0], 'pending')
                self.assertEqual(gate.observe(state, complete, 1120)[0], 'success')

    def test_missing_metadata_never_falls_back_to_reaction(self):
        api = fixtures.StructuredReviewTests().api(marker='', status='Completed')
        snapshot = api.snapshot(1)
        snapshot.update(thumbs=['new'], eyes=False)
        self.assertEqual(self.result(snapshot)[0][0], 'pending')
        self.assertFalse(self.run_gate(FakeGitHub([snapshot])))

    def test_multiple_trusted_summaries_fail_closed(self):
        api = self.api()
        original = api.pages
        api.pages = lambda path: original(path) * 2 if path == 'issues/1/comments' else original(path)
        with self.assertRaises(ValueError):
            api.snapshot(1)

    def test_head_or_base_change_during_collection_is_unavailable(self):
        for change in ({'head': {'sha': 'b' * 40}}, {'base': {'ref': 'main', 'sha': 'e' * 40}}):
            api = self.api()
            original, count = api.repo_call, [0]
            def call(path):
                count[0] += 1
                return original(path) | (change if count[0] > 1 else {})
            api.repo_call = call
            with self.subTest(change=change), self.assertRaises(ValueError):
                api.snapshot(1)

    def test_head_or_base_change_before_publication_cannot_pass(self):
        complete = self.api().snapshot(1)
        for change in ({'head': {'sha': 'b' * 40}}, {'base': {'ref': 'main', 'sha': 'e' * 40}},
                       {'draft': True}, {'state': 'closed'}):
            api = FakeGitHub([complete])
            original, reads = api.repo_call, [0]
            def call(path, data=None, method=None):
                result = original(path, data, method)
                if path.startswith('pulls/'):
                    reads[0] += 1
                    return result | (change if reads[0] > 1 else {})
                return result
            api.repo_call = call
            with self.subTest(change=change):
                self.assertFalse(self.run_gate(api))
                self.assertEqual(api.check['head_sha'], 'a' * 40)
                self.assertEqual(api.check['conclusion'], 'failure')
                self.assertNotIn('success', [write.get('conclusion') for write in api.writes])

    def test_saved_success_cannot_bypass_final_identity_read(self):
        complete = self.api().snapshot(1)
        _, state = self.result(complete)
        saved = {'id': 1, 'name': gate.EVIDENCE_CHECK, 'head_sha': 'a' * 40,
                 'app': {'id': gate.ACTIONS_APP_ID}, 'conclusion': 'success',
                 'external_id': 'codex-gate-v1:owner/repo:1:' + 'a' * 40,
                 'output': {'text': json.dumps(state)}}
        api = FakeGitHub([complete], saved)
        original, reads = api.repo_call, [0]
        def call(path, data=None, method=None):
            result = original(path, data, method)
            if path.startswith('pulls/'):
                reads[0] += 1
                if reads[0] > 1:
                    result['head']['sha'] = 'b' * 40
            return result
        api.repo_call = call
        self.assertFalse(self.run_gate(api))
        self.assertEqual(api.check['head_sha'], 'a' * 40)
        self.assertEqual(api.check['conclusion'], 'failure')

    def test_malformed_metadata_diagnostic_does_not_echo_external_body(self):
        api = self.api()
        original = api.pages
        def pages(path):
            result = original(path)
            if path == 'issues/1/comments':
                result[0]['body'] = gate.MARKER + '\n<!-- codex-review-state:v2 private body -->'
            return result
        api.pages = pages
        with self.assertRaises(gate.StructuredEvidenceError) as error:
            api.snapshot(1)
        runner = FakeGitHub([error.exception])
        self.assertFalse(self.run_gate(runner))
        self.assertIn('structured metadata', runner.check['output']['title'])
        self.assertNotIn('private body', json.dumps(runner.writes))
        self.assertEqual(json.loads(runner.check['output']['text'])['unavailable']['expected_sha'], 'a' * 40)
