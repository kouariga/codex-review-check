import copy
import importlib.util
import json
import contextlib
import io
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gate", ROOT / "tools/codex_review_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def sample(**changes):
    result = dict(head="a" * 40, base={"ref": "main", "sha": "d" * 40}, eyes=False, thumbs=["old"], summary="old-summary",
                  completed=False, findings=[], failed=False)
    return result | changes


class LifecycleTests(unittest.TestCase):
    def test_eyes_to_fresh_thumb_requires_two_stable_observations(self):
        state = gate.initialize(sample(), 0)
        self.assertEqual(gate.observe(state, sample(eyes=True), 30)[0], "pending")
        clean = sample(thumbs=["new"], summary="complete", completed=True)
        self.assertEqual(gate.observe(state, clean, 60)[0], "pending")
        self.assertEqual(gate.observe(state, clean, 89)[0], "pending")
        self.assertEqual(gate.observe(state, clean, 90)[0], "success")

    def test_fast_review_requires_fresh_id_and_changed_summary(self):
        state = gate.initialize(sample(), 0)
        clean = sample(thumbs=["new"], summary="complete", completed=True)
        gate.observe(state, clean, 30)
        self.assertEqual(gate.observe(state, clean, 60)[0], "success")
        for stale in (sample(completed=True), sample(thumbs=["new"], completed=True),
                      sample(summary="complete", completed=True)):
            self.assertEqual(gate.observe(gate.initialize(sample(), 0), stale, 60)[0], "pending")

    def test_multiple_pushes_never_reuse_acceptance(self):
        state = gate.initialize(sample(), 0)
        for head in ("b" * 40, "c" * 40):
            self.assertEqual(gate.observe(state, sample(head=head, completed=True), 30)[0], "stale")

    def test_retarget_or_base_revision_change_invalidates_acceptance(self):
        clean = sample(thumbs=["new"], summary="complete", completed=True)
        for base in ({"ref": "release", "sha": "d" * 40},
                     {"ref": "main", "sha": "e" * 40}):
            state = gate.initialize(sample(), 0)
            gate.observe(state, clean, 30)
            self.assertEqual(gate.observe(state, clean | {"base": base}, 60)[0], "stale")

    def test_restart_preserves_baseline_and_confirmation(self):
        state = gate.initialize(sample(), 0)
        clean = sample(thumbs=["new"], summary="complete", completed=True)
        gate.observe(state, clean, 30)
        restored = json.loads(json.dumps(state))
        self.assertEqual(gate.observe(restored, clean, 60)[0], "success")

    def test_findings_running_and_changed_evidence_reset_confirmation(self):
        clean = sample(thumbs=["new"], summary="complete", completed=True)
        for changed in (clean | {"findings": [1]}, clean | {"eyes": True},
                        clean | {"completed": False}, clean | {"summary": "other"}):
            state = gate.initialize(sample(), 0)
            gate.observe(state, clean, 30)
            self.assertEqual(gate.observe(state, changed, 60)[0], "pending")
            self.assertEqual(gate.observe(state, clean, 61)[0], "pending")

    def test_failure_and_timeout_never_pass(self):
        self.assertEqual(gate.observe(gate.initialize(sample(), 0), sample(failed=True), 30)[0], "failure")
        self.assertEqual(gate.observe(gate.initialize(sample(), 0), sample(), gate.TIMEOUT)[0], "failure")

    def test_failed_service_evidence_overrides_apparently_clean_evidence(self):
        state = gate.initialize(sample(), 0)
        clean = sample(thumbs=["new"], summary="complete", completed=True)
        gate.observe(state, clean, 30)
        self.assertEqual(gate.observe(state, clean | {"failed": True}, 60)[0], "failure")

    def test_identity_does_not_trust_names_or_markers(self):
        real = {"user": {"id": gate.BOT_ID, "type": "Bot"},
                "performed_via_github_app": {"id": gate.CODEX_APP_ID}}
        self.assertTrue(gate.trusted(real))
        for fake in (real | {"user": {"id": 1, "type": "Bot"}},
                     real | {"performed_via_github_app": None},
                     {"body": gate.MARKER, "user": {"login": "chatgpt-codex-connector[bot]"}}):
            self.assertFalse(gate.trusted(fake))


class SnapshotTests(unittest.TestCase):
    def api(self, *, rows=None, reactions=None, inline=None, error=False):
        api = gate.GitHub("never-used", "owner/repo")
        head = "a" * 40
        pr = {"state": "open", "draft": False, "head": {"sha": head}, "base": {"ref": "main", "sha": "d" * 40}}
        rows = rows or "| 📝 **Code Review** | ✅ **Completed** | `aaaaaaa` | New commits |"
        comment = {"id": 1, "body": gate.MARKER + "\n" + rows, "updated_at": "now",
                   "user": {"id": gate.BOT_ID, "type": "Bot"},
                   "performed_via_github_app": {"id": gate.CODEX_APP_ID}}
        def call(path):
            if error:
                raise OSError("API unavailable")
            return copy.deepcopy(pr if path.startswith("pulls/") else {"sha": head})
        api.repo_call = call
        api.pages = lambda path: (reactions or []) if path.endswith("reactions") else (
            inline or [] if path == "pulls/1/comments" else [] if path.endswith("reviews") else [comment])
        return api

    def test_summary_and_reaction_metadata(self):
        reaction = {"id": 5, "content": "+1", "user": {"id": gate.BOT_ID, "type": "Bot"}}
        snapshot = self.api(reactions=[reaction]).snapshot(1)
        self.assertTrue(snapshot["completed"])
        self.assertEqual(snapshot["thumbs"], ["5"])

    def test_short_sha_resolves_to_exact_head(self):
        api = self.api()
        original = api.repo_call
        api.repo_call = lambda path: {"sha": "b" * 40} if path.startswith("commits/") else original(path)
        self.assertFalse(api.snapshot(1)["completed"])

    def test_current_head_finding_blocks_even_without_app_metadata(self):
        finding = {"id": 10, "commit_id": "a" * 40,
                   "user": {"id": gate.BOT_ID, "type": "Bot"}}
        self.assertEqual(self.api(inline=[finding]).snapshot(1)["findings"], [10])

    def test_api_failure_cannot_become_empty_clean_snapshot(self):
        with self.assertRaises(OSError):
            self.api(error=True).snapshot(1)

    def test_all_review_rows_must_complete(self):
        rows = ("| 📝 **Code Review** | ✅ **Completed** | `aaaaaaa` | New commits |\n"
                "| **Security Review** | **Running** | `aaaaaaa` | New commits |")
        self.assertFalse(self.api(rows=rows).snapshot(1)["completed"])


class FakeGitHub:
    repo = "owner/repo"

    def __init__(self, snapshots, saved=None):
        self.snapshots = iter(snapshots)
        self.last = sample()
        self.check = saved
        self.writes = []

    def pages(self, path, key):
        return [self.check] if self.check else []

    def repo_call(self, path, data=None, method=None):
        if path.startswith("pulls/"):
            return {"state": "open", "draft": False, "head": {"sha": "a" * 40}, "base": sample()["base"]}
        self.writes.append(copy.deepcopy(data))
        if path == "check-runs":
            self.check = dict(data, id=1, app={"id": gate.ACTIONS_APP_ID}, conclusion=None)
        else:
            self.check.update(data)
        return copy.deepcopy(self.check)

    def snapshot(self, number):
        self.last = next(self.snapshots, self.last)
        if isinstance(self.last, Exception):
            raise self.last
        return copy.deepcopy(self.last)


class EntryPointTests(unittest.TestCase):
    def test_untrusted_caller_event_is_rejected_before_api_access(self):
        with mock.patch.dict(gate.os.environ, {"GITHUB_EVENT_NAME": "pull_request"}), \
                mock.patch.object(gate, "GitHub") as api:
            with self.assertRaises(ValueError):
                gate.main()
            api.assert_not_called()


class RunnerTests(unittest.TestCase):
    def run_gate(self, api):
        now = [0]
        def sleep(seconds):
            self.assertEqual(seconds, 30)
            now[0] += seconds
        with contextlib.redirect_stdout(io.StringIO()):
            gate.run(api, 1, lambda: now[0], sleep)
        return now[0]

    def test_synthetic_check_lifecycle_and_api_error_between_confirmations(self):
        clean = sample(thumbs=["new"], summary="complete", completed=True)
        api = FakeGitHub([sample(), clean, OSError(), clean, clean])
        self.assertEqual(self.run_gate(api), 120)
        self.assertEqual(api.writes[0]["head_sha"], "a" * 40)
        self.assertEqual(api.writes[0]["status"], "in_progress")
        self.assertEqual([v.get("conclusion") for v in api.writes].count("success"), 1)
        self.assertEqual(api.check["conclusion"], "success")

    def test_initial_api_failure_still_has_pending_check_and_times_out(self):
        api = FakeGitHub([OSError()])
        self.assertEqual(self.run_gate(api), gate.TIMEOUT)
        self.assertEqual(api.writes[0]["status"], "in_progress")
        self.assertEqual(api.check["conclusion"], "failure")
        self.assertNotIn("success", [v.get("conclusion") for v in api.writes])

    def test_push_during_observation_cannot_pass(self):
        api = FakeGitHub([sample(), sample(head="b" * 40)])
        self.run_gate(api)
        self.assertEqual(api.check["head_sha"], "a" * 40)
        self.assertEqual(api.check["conclusion"], "failure")

    def test_restart_resumes_owned_check_without_duplicate(self):
        clean = sample(thumbs=["new"], summary="complete", completed=True)
        state = gate.initialize(sample(), -60)
        gate.observe(state, clean, -30)
        saved = {"id": 1, "name": gate.CHECK, "head_sha": "a" * 40,
                 "app": {"id": gate.ACTIONS_APP_ID}, "conclusion": None,
                 "external_id": "codex-gate-v1:owner/repo:1:" + "a" * 40,
                 "output": {"text": json.dumps(state)}}
        api = FakeGitHub([clean], saved)
        self.assertEqual(self.run_gate(api), 0)
        self.assertEqual(len(api.writes), 1)
        self.assertEqual(api.check["conclusion"], "success")

    def test_other_repository_check_cannot_supply_acceptance(self):
        targets = ("alice/project", "org-one/service", "org-two/client")
        clean = sample(thumbs=["new"], summary="complete", completed=True)
        foreign = {"id": 99, "name": gate.CHECK, "head_sha": "a" * 40,
                   "app": {"id": gate.ACTIONS_APP_ID}, "conclusion": "success",
                   "external_id": "codex-gate-v1:other/repo:1:" + "a" * 40,
                   "output": {"text": "not reusable state"}}
        for repo in targets:
            with self.subTest(repo=repo):
                api = FakeGitHub([sample(), clean, clean], copy.deepcopy(foreign))
                api.repo = repo
                self.assertEqual(self.run_gate(api), 60)
                self.assertEqual(api.writes[0]["status"], "in_progress")
                self.assertEqual(api.check["external_id"],
                                 "codex-gate-v1:" + repo + ":1:" + "a" * 40)
                self.assertEqual(api.check["conclusion"], "success")

    def test_success_is_not_reused_after_same_head_review_restarts(self):
        clean = sample(thumbs=["new"], summary="complete", completed=True)
        state = gate.initialize(sample(), -60)
        gate.observe(state, clean, -30)
        saved = {"id": 1, "name": gate.CHECK, "head_sha": "a" * 40,
                 "app": {"id": gate.ACTIONS_APP_ID}, "conclusion": "success",
                 "external_id": "codex-gate-v1:owner/repo:1:" + "a" * 40,
                 "output": {"text": json.dumps(state)}}
        rerun = sample(eyes=True, thumbs=["new"], summary="running")
        accepted = sample(thumbs=["another"], summary="new-complete", completed=True)
        api = FakeGitHub([rerun, rerun, accepted, accepted], saved)
        self.assertEqual(self.run_gate(api), 60)
        self.assertEqual(api.writes[0]["status"], "in_progress")
        self.assertEqual(api.check["conclusion"], "success")

    def test_saved_success_for_different_base_requires_new_evidence(self):
        old = sample(base={"ref": "release", "sha": "e" * 40})
        state = gate.initialize(old, -60)
        gate.observe(state, old | {"thumbs": ["new"], "summary": "complete", "completed": True}, -30)
        saved = {"id": 1, "name": gate.CHECK, "head_sha": "a" * 40,
                 "app": {"id": gate.ACTIONS_APP_ID}, "conclusion": "success",
                 "external_id": "codex-gate-v1:owner/repo:1:" + "a" * 40,
                 "output": {"text": json.dumps(state)}}
        stale = sample(thumbs=["new"], summary="complete", completed=True)
        api = FakeGitHub([stale], saved)
        self.assertEqual(self.run_gate(api), gate.TIMEOUT)
        self.assertEqual(api.writes[0]["status"], "in_progress")
        self.assertEqual(api.check["conclusion"], "failure")

    def test_generated_workflow_matches_single_source_and_compiles(self):
        spec = importlib.util.spec_from_file_location("render", ROOT / "tools/render_codex_review_gate.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        generated = module.render()
        self.assertEqual(generated, (ROOT / ".github/workflows/review.yml").read_text())
        embedded = generated.split("python3 - <<'CODEX_GATE_PY'\n", 1)[1].rsplit("          CODEX_GATE_PY", 1)[0]
        import textwrap
        compile(textwrap.dedent(embedded), "workflow observer", "exec")
        self.assertNotIn("actions/checkout", generated)
        self.assertNotIn("secrets.", generated)
        self.assertNotIn("\nconcurrency:", generated)
        self.assertIn("\n    concurrency:", generated)
        self.assertIn("reopened, edited]", (ROOT / "examples/caller.yml").read_text())
        self.assertIn("workflow_call:", generated)
        self.assertNotIn("pull_request_target:", generated)
        self.assertIn("GITHUB_TOKEN: ${{ github.token }}", generated)
        self.assertIn("github.event.inputs.pr", generated)


if __name__ == "__main__":
    unittest.main()
