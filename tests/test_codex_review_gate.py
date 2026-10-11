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

    def test_live_reaction_user_type_completes_fresh_lifecycle(self):
        eyes = {"id": 4, "content": "eyes", "user": {"id": gate.BOT_ID, "type": "User"}}
        thumb = {"id": 5, "content": "+1", "user": {"id": gate.BOT_ID, "type": "User"}}
        running = self.api(rows="| 📝 **Code Review** | 🔄 **Running** | `aaaaaaa` | PR opened |",
                           reactions=[eyes]).snapshot(1)
        state = gate.initialize(running, 0)
        complete = self.api(reactions=[thumb]).snapshot(1)
        self.assertTrue(state["eyes"])
        self.assertEqual(gate.observe(state, complete, 30)[0], "pending")
        self.assertEqual(gate.observe(state, complete, 60)[0], "success")

    def test_reaction_identity_rejects_same_name_with_another_id(self):
        fake = {"id": 6, "content": "+1", "user": {
            "id": 1, "type": "User", "login": "chatgpt-codex-connector[bot]"}}
        self.assertEqual(self.api(reactions=[fake]).snapshot(1)["thumbs"], [])

    def test_short_sha_resolves_to_exact_head(self):
        api = self.api()
        original = api.repo_call
        api.repo_call = lambda path: {"sha": "b" * 40} if path.startswith("commits/") else original(path)
        self.assertFalse(api.snapshot(1)["completed"])

    def test_current_head_finding_blocks_even_without_app_metadata(self):
        finding = {"id": 10, "commit_id": "a" * 40,
                   "user": {"id": gate.BOT_ID, "type": "Bot"}}
        self.assertEqual(self.api(inline=[finding]).snapshot(1)["findings"], [10])

    def test_reanchored_old_finding_does_not_block_new_head(self):
        finding = {"id": 10, "commit_id": "a" * 40,
                   "original_commit_id": "b" * 40,
                   "user": {"id": gate.BOT_ID, "type": "Bot"}}
        self.assertEqual(self.api(inline=[finding]).snapshot(1)["findings"], [])
        finding.update(commit_id="b" * 40, original_commit_id="a" * 40)
        self.assertEqual(self.api(inline=[finding]).snapshot(1)["findings"], [10])

    def test_manual_and_unknown_review_triggers_are_not_accepted(self):
        for trigger in ("Manual request", "Unknown trigger"):
            rows = f"| 📝 **Code Review** | ✅ **Completed** | `aaaaaaa` | {trigger} |"
            with self.assertRaises(ValueError):
                self.api(rows=rows).snapshot(1)
        for trigger in ("PR opened", "New commits", "Draft marked ready"):
            rows = f"| 📝 **Code Review** | ✅ **Completed** | `aaaaaaa` | {trigger} |"
            self.assertTrue(self.api(rows=rows).snapshot(1)["completed"])

    def test_unscoped_trusted_issue_finding_blocks_without_guessing_head(self):
        api = self.api()
        original = api.pages
        finding = {"id": 99, "body": "**![P1 Badge](url)** Finding",
                   "user": {"id": gate.BOT_ID, "type": "Bot"},
                   "performed_via_github_app": {"id": gate.CODEX_APP_ID}}
        api.pages = lambda path: original(path) + [finding] if path == "issues/1/comments" else original(path)
        self.assertEqual(api.snapshot(1)["findings"], [99])
        finding["user"]["id"] = 123
        self.assertEqual(api.snapshot(1)["findings"], [])

    def test_null_historical_authors_are_ignored(self):
        self.assertFalse(gate.bot({"user": None}))
        api = self.api(inline=[{"id": 1, "user": None}],
                       reactions=[{"id": 2, "user": None, "content": "+1"}])
        self.assertEqual(api.snapshot(1)["findings"], [])
        self.assertEqual(api.snapshot(1)["thumbs"], [])

    def test_api_failure_cannot_become_empty_clean_snapshot(self):
        with self.assertRaises(OSError):
            self.api(error=True).snapshot(1)

    def test_unknown_review_row_cannot_hide_running_or_failed_review(self):
        for label in ("New Audit", "Code Review", "**Other Review**"):
            rows = ("| 📝 **Code Review** | ✅ **Completed** | `aaaaaaa` | New commits |\n"
                    f"| {label} | **Running** | `aaaaaaa` | New commits |")
            with self.assertRaises(ValueError):
                self.api(rows=rows).snapshot(1)

    def test_security_only_summary_cannot_approve_code_review(self):
        rows = "| **Security Review** | **Completed** | `aaaaaaa` | New commits |"
        self.assertFalse(self.api(rows=rows).snapshot(1)["completed"])

    def test_all_review_rows_must_complete(self):
        rows = ("| 📝 **Code Review** | ✅ **Completed** | `aaaaaaa` | New commits |\n"
                "| **Security Review** | **Running** | `aaaaaaa` | New commits |")
        self.assertFalse(self.api(rows=rows).snapshot(1)["completed"])


class ManualFixtureTests(unittest.TestCase):
    def fixture(self, number=1):
        return json.loads((ROOT / f"tests/fixtures/manual-{number}.json").read_text())

    def api(self, fixture):
        api = gate.GitHub("never-used", "owner/repo")
        def call(path):
            if path.startswith("pulls/"):
                return copy.deepcopy(fixture["pr"])
            short = path.removeprefix("commits/")
            head = fixture["pr"]["head"]["sha"]
            if not head.startswith(short):
                raise ValueError("Unknown or ambiguous commit")
            return {"sha": head}
        api.repo_call = call
        api.pages = lambda path: copy.deepcopy(
            fixture["reactions"] if path.endswith("reactions") else
            fixture["comments"] if path.startswith("issues/") else [])
        return api

    def test_manual_review_can_recover_original_observation(self):
        for number in (1, 2):
            fixture = self.fixture(number)
            snapshot = self.api(fixture).snapshot(1)
            self.assertTrue(snapshot["completed"])
            origin = fixture["state"]["started"]
            state = gate.initialize(snapshot, origin + 3600, observed_since=origin)
            self.assertEqual(gate.observe(state, snapshot, origin + 3600)[0], "pending")
            self.assertEqual(gate.observe(state, snapshot, origin + 3630)[0], "success")

    def test_existing_completion_is_not_fresh_for_new_observer(self):
        fixture = self.fixture()
        snapshot = self.api(fixture).snapshot(1)
        now = fixture["state"]["started"] + 3600
        state = gate.initialize(snapshot, now)
        self.assertEqual(gate.observe(state, snapshot, now + 30)[0], "pending")
        self.assertEqual(gate.observe(state, snapshot, now + 60)[0], "pending")

    def test_recovery_rejects_untrusted_wrong_stale_or_ambiguous_evidence(self):
        def duplicate(f):
            f["comments"].append(f["comments"][1] | {"id": 99})
        changes = {
            "author": lambda f: f["comments"][1].update(user={"id": 1}),
            "app": lambda f: f["comments"][1].update(performed_via_github_app={"id": 1}),
            "summary-author": lambda f: f["comments"][0].update(user={"id": 1}),
            "reaction-author": lambda f: f["reactions"][0].update(user={"id": 1}),
            "wrong-sha": lambda f: f["comments"][1].update(body=f["comments"][1]["body"].replace("a" * 10, "c" * 10)),
            "ambiguous-sha": lambda f: f["comments"][1].update(body=f["comments"][1]["body"] + "\n**Reviewed commit:** `aaaaaaa`"),
            "stale-comment": lambda f: f["comments"][1].update(created_at="2026-10-09T00:00:00Z"),
            "stale-reaction": lambda f: f["reactions"][0].update(created_at="2026-10-09T00:00:00Z"),
            "early-reaction": lambda f: f["reactions"][0].update(created_at=f["comments"][1]["created_at"]),
            "malformed-time": lambda f: f["reactions"][0].update(created_at="yesterday"),
            "missing-timezone": lambda f: f["comments"][1].update(created_at="2026-10-10T14:33:32"),
            "future-summary": lambda f: f["comments"][0].update(updated_at="2099-01-01T00:00:00Z"),
            "edited-after-completion": lambda f: f["comments"][1].update(updated_at="2026-10-10T14:34:00Z"),
            "duplicate": duplicate,
            "unknown-trigger": lambda f: f["comments"][0].update(body=f["comments"][0]["body"].replace("Manual request", "Unknown")),
        }
        for label, change in changes.items():
            with self.subTest(label=label):
                fixture = self.fixture()
                change(fixture)
                origin = fixture["state"]["started"]
                try:
                    snapshot = self.api(fixture).snapshot(1)
                except ValueError:
                    continue
                state = gate.initialize(snapshot, origin + 3600, observed_since=origin)
                for now in (origin + 3600, origin + 3630):
                    self.assertNotEqual(gate.observe(state, snapshot, now)[0], "success")

    def test_manual_findings_head_and_base_changes_block(self):
        fixture = self.fixture()
        snapshot = self.api(fixture).snapshot(1)
        origin = fixture["state"]["started"]
        for change in ({"findings": [1]}, {"head": "c" * 40},
                       {"base": {"ref": "main", "sha": "e" * 40}}, {"failed": True}):
            state = gate.initialize(snapshot, origin + 3600, observed_since=origin)
            gate.observe(state, snapshot, origin + 3600)
            self.assertNotEqual(gate.observe(state, snapshot | change, origin + 3630)[0], "success")

    def test_reaction_or_comment_mutation_requires_new_confirmation(self):
        fixture = self.fixture()
        snapshot = self.api(fixture).snapshot(1)
        origin = fixture["state"]["started"]
        for field in ("reaction", "comment"):
            state = gate.initialize(snapshot, origin + 3600, observed_since=origin)
            gate.observe(state, snapshot, origin + 3600)
            changed = copy.deepcopy(snapshot)
            if field == "reaction":
                changed["manual"]["thumbs"]["10"] += 1
            else:
                changed["manual"]["comments"][0]["digest"] = "edited"
            self.assertEqual(gate.observe(state, changed, origin + 3630)[0], "pending")

    def test_legacy_initialized_state_cannot_assume_empty_manual_baseline(self):
        fixture = self.fixture()
        snapshot = self.api(fixture).snapshot(1)
        origin = fixture["state"]["started"]
        state = gate.initialize(sample(head=snapshot["head"]), origin)
        del state["manual_baseline"]
        state["baseline"] = []
        for now in (origin + 300, origin + 330):
            self.assertEqual(gate.observe(state, snapshot, now)[0], "pending")

    def test_manual_observed_running_accepts_only_new_completion(self):
        fixture = self.fixture()
        complete = self.api(fixture).snapshot(1)
        origin = fixture["state"]["started"]
        running = copy.deepcopy(complete)
        running.update(completed=False, eyes=True)
        running["manual"]["comments"] = []
        running["thumbs"] = []
        running["summary"] = "running"
        state = gate.initialize(running, origin)
        self.assertEqual(gate.observe(state, complete, origin + 300)[0], "pending")
        self.assertEqual(gate.observe(state, complete, origin + 330)[0], "success")
        gate.observe(state, complete | {"completed": False, "eyes": True}, origin + 360)
        self.assertEqual(gate.observe(state, complete, origin + 390)[0], "pending")

    def test_late_old_comment_after_running_cannot_approve_new_cycle(self):
        fixture = self.fixture()
        complete = self.api(fixture).snapshot(1)
        origin = fixture["state"]["started"]
        running = copy.deepcopy(complete)
        running.update(completed=False, eyes=True)
        running["manual"]["comments"] = []
        state = gate.initialize(running, origin)
        gate.observe(state, running, origin + 500)
        later = copy.deepcopy(complete)
        later["manual"]["completed_at"] = origin + 550
        later["manual"]["summary_updated"] = origin + 551
        later["manual"]["thumbs"] = {"11": origin + 552}
        later["thumbs"] = ["11"]
        for now in (origin + 560, origin + 590):
            self.assertEqual(gate.observe(state, later, now)[0], "pending")

    def test_automatic_restart_invalidates_old_manual_completion(self):
        fixture = self.fixture()
        complete = self.api(fixture).snapshot(1)
        origin = fixture["state"]["started"]
        for phase in ({"completed": False, "eyes": True},
                      {"completed": False, "failed": True}):
            with self.subTest(phase=phase):
                state = gate.initialize(complete, origin + 300, observed_since=origin)
                self.assertEqual(gate.observe(state, complete, origin + 300)[0], "pending")
                restarted = complete | phase | {"manual": None, "summary": "automatic-restart"}
                self.assertEqual(gate.observe(state, restarted, origin + 310)[0],
                                 "failure" if phase.get("failed") else "pending")
                later = copy.deepcopy(complete)
                later["summary"] = "later-manual-completion"
                later["manual"]["completed_at"] = origin + 350
                later["manual"]["summary_updated"] = origin + 351
                later["manual"]["thumbs"] = {"11": origin + 352}
                later["thumbs"] = ["11"]
                for now in (origin + 360, origin + 390):
                    self.assertEqual(gate.observe(state, later, now)[0], "pending")
                later["manual"]["comments"] = [later["manual"]["comments"][0] | {
                    "id": 3, "created": origin + 348, "updated": origin + 348,
                    "digest": "fresh-manual-completion"}]
                self.assertEqual(gate.observe(state, later, origin + 400)[0], "pending")
                self.assertEqual(gate.observe(state, later, origin + 430)[0], "success")
                gate.observe(state, restarted, origin + 440)
                later["summary"] = "another-manual-completion"
                later["manual"]["completed_at"] = origin + 480
                later["manual"]["summary_updated"] = origin + 481
                later["manual"]["thumbs"] = {"12": origin + 482}
                later["thumbs"] = ["12"]
                for now in (origin + 490, origin + 520):
                    self.assertEqual(gate.observe(state, later, now)[0], "pending")

    def test_completed_same_head_rerun_cannot_reuse_previous_comment(self):
        fixture = self.fixture()
        complete = self.api(fixture).snapshot(1)
        origin = fixture["state"]["started"]
        state = gate.initialize(complete, origin + 300, observed_since=origin)
        gate.observe(state, complete, origin + 300)
        gate.observe(state, complete, origin + 330)
        saved = {"id": 1, "name": gate.CHECK, "app": {"id": gate.ACTIONS_APP_ID},
                 "conclusion": "success", "external_id": "codex-gate-v1:owner/repo:1:" + complete["head"],
                 "output": {"text": json.dumps(state)}}
        rerun = copy.deepcopy(complete)
        rerun["summary"] = "new-completed-lifecycle"
        rerun["thumbs"] = ["11"]
        rerun["manual"]["completed_at"] += 120
        rerun["manual"]["summary_updated"] += 120
        rerun["manual"]["thumbs"] = {"11": complete["manual"]["thumbs"]["10"] + 120}
        api = FakeGitHub([rerun], saved)
        now = [origin + 600]
        with contextlib.redirect_stdout(io.StringIO()):
            gate.run(api, 1, lambda: now[0], lambda seconds: now.__setitem__(0, now[0] + seconds))
        self.assertEqual(api.check["conclusion"], "failure")
        self.assertNotIn("success", [write.get("conclusion") for write in api.writes])

    def test_failed_uninitialized_observer_preserves_origin_on_retry(self):
        fixture = self.fixture()
        snapshot = self.api(fixture).snapshot(1)
        origin = fixture["state"]["started"]
        state = fixture["state"]
        saved = {"id": 1, "name": gate.CHECK, "app": {"id": gate.ACTIONS_APP_ID},
                 "conclusion": "failure", "external_id": "codex-gate-v1:owner/repo:1:" + snapshot["head"],
                 "output": {"text": json.dumps(state)}}
        api = FakeGitHub([snapshot], saved)
        now = [origin + 3600]
        with contextlib.redirect_stdout(io.StringIO()):
            gate.run(api, 1, lambda: now[0], lambda seconds: now.__setitem__(0, now[0] + seconds))
        self.assertEqual(api.check["conclusion"], "success")
        restored = json.loads(api.check["output"]["text"])
        self.assertEqual(restored["observed_since"], origin)
        self.assertEqual(now[0], origin + 3630)


    def retry_history(self):
        fixture = self.fixture()
        snapshot = self.api(fixture).snapshot(1)
        origin = fixture["state"]["started"]
        def record(identifier, started):
            return {"id": identifier, "name": gate.CHECK,
                    "head_sha": snapshot["head"], "app": {"id": gate.ACTIONS_APP_ID},
                    "conclusion": "failure",
                    "external_id": "codex-gate-v1:owner/repo:1:" + snapshot["head"],
                    "output": {"text": json.dumps(fixture["state"] | {"started": started})}}
        return snapshot, origin, [record(3, origin + 3600), record(2, origin + 1800), record(1, origin)]

    def run_history(self, snapshot, origin, history):
        api = FakeGitHub([snapshot], history[0])
        api.pages = lambda path, key: copy.deepcopy(history)
        now = [origin + 7200]
        with contextlib.redirect_stdout(io.StringIO()):
            gate.run(api, 1, lambda: now[0], lambda seconds: now.__setitem__(0, now[0] + seconds))
        return api, now[0]

    def virgin_history(self, bridge=False):
        snapshot, origin, history = self.retry_history()
        for record in history:
            previous = json.loads(record["output"]["text"])
            record["output"]["text"] = json.dumps({
                "version": 1, "head": snapshot["head"], "base": snapshot["base"],
                "started": previous["started"], "baseline": [],
                "baseline_summary": gate.digest([None, "", None]), "eyes": False,
                "candidate": None, "candidate_at": None})
        if bridge:
            state = json.loads(history[0]["output"]["text"])
            promoted = state | {"started": origin + 6000, "observed_since": state["started"],
                                "manual_baseline": [c["id"] for c in snapshot["manual"]["comments"]],
                                "manual_recovery": False}
            history.insert(0, history[0] | {"id": 4, "output": {"text": json.dumps(promoted)}})
        return snapshot, origin, history

    def test_virgin_legacy_observation_recovers_after_initialized_retries(self):
        for bridge in (False, True):
            with self.subTest(bridge=bridge):
                snapshot, origin, history = self.virgin_history(bridge)
                api, ended = self.run_history(snapshot, origin, history)
                self.assertEqual(api.check["conclusion"], "success")
                restored = json.loads(api.check["output"]["text"])
                self.assertEqual(restored["observed_since"], origin)
                self.assertEqual(restored["baseline"], [])
                self.assertFalse(restored["manual_recovery"])
                self.assertEqual(ended, origin + 7230)

    def test_virgin_recovery_stops_at_lifecycle_and_identity_boundaries(self):
        changes = {"baseline": ["10"], "baseline_summary": "prior-completion", "eyes": True,
                   "candidate": "accepted", "candidate_at": 1, "initialized": True,
                   "manual_since": 1, "manual_active": "running", "unknown": None,
                   "observed_since": 1, "version": True, "started": float("inf"),
                   "base": {"ref": "other", "sha": "d" * 40}, "head": "b" * 40}
        for bridge in (False, True):
            for key, value in changes.items():
                with self.subTest(bridge=bridge, key=key):
                    snapshot, origin, history = self.virgin_history(bridge)
                    boundary = history[-1]
                    state = json.loads(boundary["output"]["text"])
                    state[key] = value
                    boundary["output"]["text"] = json.dumps(state)
                    api, _ = self.run_history(snapshot, origin, history)
                    self.assertEqual(api.check["conclusion"], "failure")
                    self.assertNotIn("success", [write.get("conclusion") for write in api.writes])

    def test_virgin_recovery_does_not_cross_invalid_check_records(self):
        for change in ("success", "external", "head", "malformed", "duplicate_id", "future", "null"):
            with self.subTest(change=change):
                snapshot, origin, history = self.virgin_history(True)
                boundary = history[-1]
                if change == "success": boundary["conclusion"] = "success"
                elif change == "external": boundary["external_id"] += "other"
                elif change == "head": boundary["head_sha"] = "b" * 40
                elif change == "malformed": boundary["output"]["text"] = "invalid"
                elif change == "duplicate_id": boundary["id"] = history[-2]["id"]
                elif change == "null": boundary["output"]["text"] = "null"
                elif change == "future":
                    state = json.loads(boundary["output"]["text"])
                    state["started"] = origin + 9000
                    boundary["output"]["text"] = json.dumps(state)
                api, _ = self.run_history(snapshot, origin, history)
                self.assertEqual(api.check["conclusion"], "failure")

    def test_only_exact_mechanical_migration_can_replace_stored_origin(self):
        for change in ("origin", "baseline", "recovery", "new_field", "active", "cycle", "candidate", "late_comment"):
            with self.subTest(change=change):
                snapshot, origin, history = self.virgin_history(True)
                bridge = json.loads(history[0]["output"]["text"])
                if change == "origin": bridge["observed_since"] -= 1
                elif change == "baseline": bridge["manual_baseline"] = []
                elif change == "recovery": bridge["manual_recovery"] = True
                elif change == "new_field": bridge["future_version_field"] = None
                elif change == "active": bridge["manual_active"] = "running"
                elif change == "cycle": bridge["manual_since"] = origin + 3000
                elif change == "candidate": bridge["candidate"] = "accepted"
                elif change == "late_comment": bridge["started"] = origin + 100
                history[0]["output"]["text"] = json.dumps(bridge)
                api, _ = self.run_history(snapshot, origin, history)
                self.assertEqual(api.check["conclusion"], "failure")
                restored = json.loads(api.check["output"]["text"])
                self.assertEqual(restored["observed_since"], bridge["observed_since"])

    def test_virgin_recovery_still_rejects_stale_or_ambiguous_evidence(self):
        for change in ("old_comment", "ambiguous", "equal_reaction_time", "finding", "running", "failed"):
            with self.subTest(change=change):
                snapshot, origin, history = self.virgin_history(True)
                if change == "old_comment": snapshot["manual"]["comments"][0]["created"] = origin - 1
                elif change == "ambiguous":
                    snapshot["manual"]["comments"].append(snapshot["manual"]["comments"][0] | {"id": 999})
                elif change == "equal_reaction_time":
                    snapshot["manual"]["thumbs"] = dict.fromkeys(snapshot["thumbs"], snapshot["manual"]["completed_at"])
                elif change == "finding": snapshot["findings"] = [123]
                elif change == "running": snapshot["completed"] = False
                elif change == "failed": snapshot["failed"] = True
                api, _ = self.run_history(snapshot, origin, history)
                self.assertEqual(api.check["conclusion"], "failure")

    def test_legacy_failed_retry_chain_recovers_original_origin(self):
        snapshot, origin, history = self.retry_history()
        api, ended = self.run_history(snapshot, origin, history)
        self.assertEqual(api.check["conclusion"], "success")
        self.assertEqual(json.loads(api.check["output"]["text"])["observed_since"], origin)
        self.assertEqual(ended, origin + 7230)

    def test_recovery_rejects_reaction_at_exact_completion_second(self):
        snapshot, origin, history = self.retry_history()
        completed_at = snapshot["manual"]["completed_at"]
        snapshot["manual"]["thumbs"] = dict.fromkeys(snapshot["thumbs"], completed_at)
        api, _ = self.run_history(snapshot, origin, history)
        self.assertEqual(api.check["conclusion"], "failure")
        self.assertNotIn("success", [write.get("conclusion") for write in api.writes])

    def test_retry_history_stops_at_invalid_predecessor(self):
        for change in ("base", "head", "version", "initialized", "success", "malformed",
                       "external", "started", "boolean_time", "nan_time", "duplicate_id",
                       "observed_since", "null_state", "record_head", "missing_started"):
            with self.subTest(change=change):
                snapshot, origin, history = self.retry_history()
                boundary = history[1]
                state = json.loads(boundary["output"]["text"])
                if change == "base": state["base"]["sha"] = "e" * 40
                elif change == "head": state["head"] = "b" * 40
                elif change == "version": state["version"] = 2
                elif change == "initialized": state["initialized"] = True
                elif change == "success": boundary["conclusion"] = "success"
                elif change == "external": boundary["external_id"] += "other"
                elif change == "started": state["started"] = origin + 4000
                elif change == "boolean_time": state["started"] = False
                elif change == "nan_time": state["started"] = float("nan")
                elif change == "duplicate_id": boundary["id"] = history[0]["id"]
                elif change == "observed_since": state["observed_since"] = origin
                elif change == "record_head": boundary["head_sha"] = "b" * 40
                elif change == "missing_started": del state["started"]
                elif change == "null_state": state = None
                boundary["output"]["text"] = "invalid" if change == "malformed" else json.dumps(state)
                api, _ = self.run_history(snapshot, origin, history)
                self.assertEqual(api.check["conclusion"], "failure")
                self.assertNotIn("success", [write.get("conclusion") for write in api.writes])

    def test_recovery_keeps_valid_prefix_without_crossing_boundary(self):
        snapshot, origin, history = self.retry_history()
        middle = json.loads(history[1]["output"]["text"])
        middle["started"] = origin + 10
        history[1]["output"]["text"] = json.dumps(middle)
        history[2]["output"]["text"] = "invalid"
        api, ended = self.run_history(snapshot, origin, history)
        self.assertEqual(api.check["conclusion"], "success")
        self.assertEqual(json.loads(api.check["output"]["text"])["observed_since"], origin + 10)
        self.assertEqual(ended, origin + 7230)

    def test_initialized_latest_record_cannot_recover_older_origin(self):
        snapshot, origin, history = self.retry_history()
        history[0]["output"]["text"] = json.dumps(gate.initialize(snapshot, origin + 3600))
        api, _ = self.run_history(snapshot, origin, history)
        self.assertEqual(api.check["conclusion"], "failure")
        self.assertEqual(json.loads(api.check["output"]["text"])["observed_since"], origin + 3600)

    def test_saved_origin_never_rewinds_using_legacy_history(self):
        snapshot, origin, history = self.retry_history()
        state = json.loads(history[0]["output"]["text"])
        state["observed_since"] = origin + 3600
        history[0]["output"]["text"] = json.dumps(state)
        api, _ = self.run_history(snapshot, origin, history)
        self.assertEqual(api.check["conclusion"], "failure")
        self.assertEqual(json.loads(api.check["output"]["text"])["observed_since"], origin + 3600)


class HTTPTests(unittest.TestCase):
    def test_authenticated_conditional_get_reuses_304_body(self):
        api = gate.GitHub("example-token", "owner/repo")
        response = io.BytesIO(b'{"value": 1}')
        response.headers = {"ETag": '"version-1"'}
        unchanged = gate.urllib.error.HTTPError("https://api.github.com/test", 304, "unchanged", {}, None)
        with mock.patch.object(gate.urllib.request, "urlopen", side_effect=[response, unchanged]) as get:
            first = api.call("test")
            first["value"] = 99
            self.assertEqual(api.call("test"), {"value": 1})
            request = get.call_args.args[0]
            self.assertEqual(request.get_header("If-none-match"), '"version-1"')
            self.assertEqual(request.get_header("Authorization"), "Bearer example-token")

    def test_rate_limit_stops_without_another_network_request(self):
        for code in (403, 429):
            api = gate.GitHub("example-token", "owner/repo")
            error = gate.urllib.error.HTTPError("https://api.github.com/test", code, "blocked", {}, None)
            with mock.patch.object(gate.urllib.request, "urlopen", side_effect=error) as get:
                with self.assertRaises(gate.APIBlocked):
                    api.call("test")
                self.assertEqual(get.call_count, 1)


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
            self.check = dict(data, id=(self.check["id"] + 1 if self.check else 1), app={"id": gate.ACTIONS_APP_ID}, conclusion=None)
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

    def test_unchanged_pending_output_is_not_written_each_poll(self):
        api = FakeGitHub([sample()])
        self.run_gate(api)
        self.assertEqual(len(api.writes), 3)  # create, initial observation, timeout

    def test_rate_limit_does_not_trigger_failure_patch_or_retry(self):
        api = FakeGitHub([gate.APIBlocked("stop")])
        with self.assertRaises(gate.APIBlocked):
            self.run_gate(api)
        self.assertEqual(len(api.writes), 1)  # pending creation only

    def test_failed_attempt_gets_new_run_but_incomplete_attempt_resumes(self):
        for conclusion in ("failure", None):
            for recovered in (True, False):
                state = gate.initialize(sample(), -gate.TIMEOUT)
                saved = {"id": 1, "name": gate.CHECK, "head_sha": "a" * 40,
                         "app": {"id": gate.ACTIONS_APP_ID}, "conclusion": conclusion,
                         "external_id": "codex-gate-v1:owner/repo:1:" + "a" * 40,
                         "output": {"text": json.dumps(state)}}
                evidence = sample(thumbs=["new"], summary="complete", completed=True) if recovered else sample()
                api = FakeGitHub([evidence], saved)
                self.assertEqual(self.run_gate(api), 30 if recovered else gate.TIMEOUT)
                self.assertEqual(api.check["id"], 2 if conclusion else 1)
                self.assertEqual(api.check["conclusion"], "success" if recovered else "failure")
                self.assertEqual("name" in api.writes[0], conclusion == "failure")

    def test_terminal_history_uses_latest_run_and_multiple_active_runs_block(self):
        state = gate.initialize(sample(), -gate.TIMEOUT)
        old = {"id": 1, "name": gate.CHECK, "head_sha": "a" * 40,
               "app": {"id": gate.ACTIONS_APP_ID}, "conclusion": "failure",
               "external_id": "codex-gate-v1:owner/repo:1:" + "a" * 40,
               "output": {"text": json.dumps(state)}}
        latest = old | {"id": 2}
        clean = sample(thumbs=["new"], summary="complete", completed=True)
        api = FakeGitHub([clean], latest)
        api.pages = lambda path, key: [latest, old]
        self.assertEqual(self.run_gate(api), 30)
        self.assertEqual(api.check["id"], 3)
        self.assertEqual(api.check["conclusion"], "success")
        api = FakeGitHub([clean])
        api.pages = lambda path, key: [old | {"conclusion": None}, latest | {"conclusion": None}]
        with self.assertRaises(ValueError):
            self.run_gate(api)
        self.assertEqual(api.writes, [])

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
        self.assertIn("fromJSON(github.event.inputs.pr || '0')", generated)
        self.assertIn("type: number", (ROOT / "examples/caller.yml").read_text())


if __name__ == "__main__":
    unittest.main()
