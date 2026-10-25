import copy
import json
import unittest

import test_structured_verdict as fixtures
from test_codex_review_gate import gate, FakeGitHub, sample
import test_dispositions as dispositions
from test_required_gate import saved_check


class JSONOnlyTests(unittest.TestCase):
    def api(self, status='running', flags=None, *, display='', prose='Nice work!', metadata=True):
        fixture = fixtures.StructuredVerdictTests()
        api = fixture.api(fixture.metadata(status=status, has_any_findings=flags,
                                          has_unresolved_findings=flags), display=display)
        original = api.pages
        def pages(path):
            self.assertNotIn('reactions', path)
            comments = copy.deepcopy(original(path))
            if path == 'issues/1/comments':
                if not metadata:
                    comments[0]['body'] = gate.MARKER + '\n' + display
                comments.append({'id': 9, 'user': {'id': gate.BOT_ID},
                                 'performed_via_github_app': {'id': gate.CODEX_APP_ID},
                                 'created_at': '1970-01-01T00:10:00Z',
                                 'updated_at': '1970-01-01T00:10:00Z',
                                 'body': '<!-- codex-review-receipt:v1 -->\n'
                                         "Codex Review: Didn't find any major issues. " + prose
                                         + '\n**Reviewed commit:** `bbbbbbbbbb`'})
            return comments
        api.pages = pages
        original_call = api.repo_call
        def call(path):
            self.assertFalse(path.startswith('commits/'), 'Never resolve a prose receipt SHA')
            return original_call(path)
        api.repo_call = call
        return api

    def test_running_unknown_results_ignore_old_receipt_wording_and_display_completion(self):
        display = ('| **Code Review** | **Completed** '
                   '<relative-time datetime="1970-01-01T00:16:32Z">done</relative-time>'
                   ' | `aaaaaaa` | Manual request |')
        for status in ('running', 'queued', 'pending'):
            for prose in ('Nice work!', 'Already looking forward to the next diff.',
                          'What shall we delve into next?', 'arbitrary text', '<!-- malformed'):
                with self.subTest(status=status, prose=prose):
                    snapshot = self.api(status, display=display, prose=prose).snapshot(1)
                    state = gate.initialize(snapshot, 1000)
                    for now in (1000, 1030):
                        self.assertEqual(gate.observe(state, snapshot, now)[0], 'pending')
                        self.assertNotIn('proof', state)
                    self.assertEqual(gate.observe(state, snapshot, 1000 + gate.TIMEOUT)[0], 'failure')

    def test_completed_unknown_results_never_fall_back_to_receipts(self):
        for prose in ('Keep it up!', 'Nice work!', '👍'):
            snapshot = self.api('completed', prose=prose).snapshot(1)
            state = gate.initialize(snapshot, 1000)
            self.assertEqual(gate.observe(state, snapshot, 1000)[0], 'pending')
            self.assertEqual(gate.observe(state, snapshot, 1030)[0], 'pending')
            self.assertNotIn('proof', state)

    def test_missing_json_never_uses_a_completed_display_or_receipt(self):
        display = '| **Code Review** | **Completed** | `aaaaaaa` | Manual request |'
        snapshot = self.api('completed', False, metadata=False, display=display).snapshot(1)
        state = gate.initialize(snapshot, 1000)
        self.assertEqual(gate.observe(state, snapshot, 1000)[0], 'pending')
        self.assertEqual(gate.observe(state, snapshot, 1030)[0], 'pending')

    def test_failed_json_remains_failed_without_parsing_receipts(self):
        snapshot = self.api('failed').snapshot(1)
        self.assertEqual(gate.observe(gate.initialize(snapshot, 1000), snapshot, 1000)[0], 'failure')

    def test_security_scope_comes_from_json_not_display_labels(self):
        display = '| **Security Review** | **Running** | `bbbbbbb` | Unknown wording |'
        snapshot = self.api('completed', False, display=display).snapshot(1)
        state = gate.initialize(snapshot, 1000)
        self.assertEqual(gate.observe(state, snapshot, 1000)[0], 'pending')
        self.assertEqual(gate.observe(state, snapshot, 1030)[0], 'success')
        self.assertEqual(state['proof']['kind'], 'clean_codex')

    def test_running_then_completed_clean_needs_two_fresh_json_observations(self):
        running = self.api().snapshot(1)
        complete = self.api('completed', False, prose='New arbitrary notification text').snapshot(1)
        state = gate.initialize(running, 1000)
        self.assertEqual(gate.observe(state, running, 1000)[0], 'pending')
        self.assertEqual(gate.observe(state, complete, 1030)[0], 'pending')
        self.assertEqual(gate.observe(state, complete, 1060)[0], 'success')

    def test_old_saved_receipt_proof_cannot_approve_missing_or_running_json(self):
        for snapshot in (self.api(metadata=False).snapshot(1), self.api().snapshot(1)):
            state = gate.initialize(sample(completed=True), 0)
            state.update(proof={'kind': 'clean_codex', 'digest': 'old-receipt-proof'},
                         candidate='old-candidate', candidate_at=0, baseline=['old-thumb'],
                         manual_baseline=[], manual_recovery=True, eyes=False)
            api = FakeGitHub([snapshot], saved_check(conclusion='success', state=state))
            self.assertFalse(fixtures.StructuredVerdictTests().run_gate(api))
            self.assertEqual(api.check['conclusion'], 'failure')
            self.assertNotIn('proof', json.loads(api.check['output']['text']))

    def test_dispositions_use_json_completion_and_api_chronology_without_display_table(self):
        fixture = dispositions.DispositionTests()
        fixture.setUp()
        fixture.comments[0]['body'] = (gate.MARKER + '\n<!-- codex-review-state:v1 '
                                       + json.dumps(fixture.metadata) + ' -->')
        fixture.decisions()
        self.assertEqual(fixture.verdict()[2][0], 'success')
        for status, flags in (('running', True), ('completed', None)):
            fixture.metadata['code_review'].update(status=status, has_any_findings=flags,
                                                  has_unresolved_findings=False if flags else None)
            fixture.comments[0]['body'] = (gate.MARKER + '\n<!-- codex-review-state:v1 '
                                           + json.dumps(fixture.metadata) + ' -->')
            self.assertEqual(fixture.verdict()[2][0], 'pending')

    def test_legacy_clean_phrase_is_not_an_approval_exemption_for_a_review_body(self):
        fixture = dispositions.DispositionTests()
        fixture.setUp()
        fixture.inline, fixture.resolved = [], {}
        fixture.comments = fixture.comments[:1]
        fixture.metadata['code_review'].update(has_any_findings=False)
        fixture.refresh_summary()
        fixture.reviews = [{'id': 42, 'user': {'id': gate.BOT_ID}, 'state': 'COMMENTED',
                            'body': "Codex Review: Didn't find any major issues.",
                            'commit_id': 'a' * 40, 'submitted_at': dispositions.iso(80)}]
        snapshot = fixture.api.snapshot(1)
        self.assertEqual({item['key'] for item in snapshot['inventory']}, {'review:42'})
        self.assertEqual(fixture.verdict(snapshot)[2][0], 'pending')

    def test_required_code_review_cannot_be_an_all_null_object(self):
        fixture = fixtures.StructuredVerdictTests()
        metadata = fixture.metadata()
        metadata['code_review'] = dict.fromkeys(metadata['code_review'])
        with self.assertRaises(gate.StructuredEvidenceError):
            fixture.api(metadata).snapshot(1)

    def test_partial_security_metadata_cannot_be_treated_as_absent(self):
        fixture = fixtures.StructuredVerdictTests()
        metadata = fixture.metadata()
        metadata['security_review']['status'] = 'running'
        with self.assertRaises(gate.StructuredEvidenceError):
            fixture.api(metadata).snapshot(1)
