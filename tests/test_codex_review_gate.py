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
        api.review_threads = lambda number: {c.get("in_reply_to_id") or c["id"]: False for c in inline or []}
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

    def test_reanchored_old_unresolved_finding_blocks_new_head(self):
        finding = {"id": 10, "commit_id": "a" * 40,
                   "original_commit_id": "b" * 40,
                   "user": {"id": gate.BOT_ID, "type": "Bot"}}
        self.assertEqual(self.api(inline=[finding]).snapshot(1)["findings"], [10])
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


class StructuredReviewTests(unittest.TestCase):
    def fixture(self):
        review = {"commit_sha": "a" * 40, "has_any_findings": True,
                  "has_unresolved_findings": False, "status": "completed"}
        return {"code_review": review, "security_review": dict.fromkeys(review)}

    def api(self, value=None, *, marker=None, status="Completed", inline=None):
        value = self.fixture() if value is None else value
        marker = ("<!-- codex-review-state:v1 " + json.dumps(value) + " -->"
                  if marker is None else marker)
        rows = ("| 📝 **Code Review** | **" + status + "** "
                '<relative-time datetime="2026-10-16T03:16:04.451181Z">done</relative-time>'
                " | `aaaaaaa` | New commits |")
        api = SnapshotTests().api(rows=rows, inline=inline)
        original = api.pages
        def pages(path):
            result = original(path)
            if path == "issues/1/comments":
                result[0]["body"] = gate.MARKER + "\n" + marker + "\n" + rows
                result[0]["updated_at"] = "2026-10-16T03:16:06Z"
            return result
        api.pages = pages
        return api

    def test_structured_resolved_flags_without_reaction_never_approve(self):
        snapshot = self.api().snapshot(1)
        now = gate.timestamp("2026-10-16T03:17:00Z")
        state = gate.initialize(snapshot, now)
        self.assertEqual(gate.observe(state, snapshot, now)[0], "pending")
        self.assertEqual(gate.observe(state, snapshot, now + 30)[0], "pending")
        self.assertNotIn("structured_since", state)

    def test_invalid_structured_marker_cannot_fall_back_to_legacy_reaction(self):
        fixture = self.fixture()
        variants = []
        for key, value in (("commit_sha", "b" * 40), ("commit_sha", "a" * 7),
                           ("has_any_findings", 1), ("has_unresolved_findings", None),
                           ("extra", False), ("status", "unknown")):
            changed = copy.deepcopy(fixture)
            changed["code_review"][key] = value
            variants.append(changed)
        variants += [{"security_review": fixture["security_review"]},
                     fixture | {"extra": {}},
                     fixture | {"code_review": dict.fromkeys(fixture["code_review"])},
                     fixture | {"security_review": fixture["code_review"]}]
        for value in variants:
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                self.api(value).snapshot(1)
        marker = "<!-- codex-review-state:v1 " + json.dumps(fixture) + " -->"
        for invalid in (marker + marker, marker.replace(":v1", ":v2"),
                        '<!-- codex-review-state:v1 {"code_review":{},"code_review":{}} -->'):
            with self.subTest(marker=invalid), self.assertRaises(ValueError):
                self.api(marker=invalid).snapshot(1)

    def test_human_and_machine_status_must_agree(self):
        for status in ("Running", "Failed"):
            with self.assertRaises(ValueError):
                self.api(status=status).snapshot(1)

    def test_untrusted_summary_cannot_supply_structured_proof(self):
        for field, value in (("user", {"id": 123}), ("performed_via_github_app", {"id": 123})):
            api = self.api()
            original = api.pages
            def pages(path, original=original):
                result = original(path)
                if path == "issues/1/comments":
                    result[0][field] = value
                return result
            api.pages = pages
            snapshot = api.snapshot(1)
            self.assertIsNone(snapshot["structured"])
            self.assertFalse(snapshot["completed"])


class ModernReceiptTests(unittest.TestCase):
    def fixture(self):
        return json.loads((ROOT / "tests/fixtures/modern-clean.json").read_text())

    def snapshot(self, fixture):
        return ManualFixtureTests().api(fixture).snapshot(1)

    def with_reaction(self):
        fixture = self.fixture()
        fixture["reactions"] = [{"id": 10, "content": "+1", "created_at": "2026-10-18T03:55:37Z",
                                 "user": {"id": gate.BOT_ID}}]
        return fixture

    def state(self, snapshot):
        origin = gate.timestamp("2026-10-18T03:54:00Z")
        return gate.initialize(snapshot if snapshot.get("manual") is not None else
                               snapshot | {"thumbs": [], "summary": "running-summary"},
                               origin + 120, observed_since=origin)

    def result(self, snapshot):
        now = gate.timestamp("2026-10-18T03:56:00Z")
        state = self.state(snapshot)
        self.assertEqual(gate.observe(state, snapshot, now)[0], "pending")
        return gate.observe(state, snapshot, now + 30)[0], state

    def test_positive_reaction_suffixes_preserve_full_acceptance_guards(self):
        for suffix in ("", "Keep it up!", "Another round soon, please!", ":+1:", ":thumbsup:",
                       "👍", "👍\ufe0f", "👍\ufe0e", "👍🏻", "👍🏿", "👍\ufe0f🏽"):
            for known_flags in (False, True):
                with self.subTest(suffix=suffix, known_flags=known_flags):
                    fixture = self.with_reaction()
                    original = fixture["comments"][1]["body"]
                    line = next(line for line in original.splitlines() if line.startswith("Codex Review:"))
                    clean = "Codex Review: Didn't find any major issues."
                    fixture["comments"][1]["body"] = original.replace(line, clean + (" " + suffix if suffix else ""))
                    if known_flags:
                        fixture["comments"][0]["body"] = fixture["comments"][0]["body"].replace(
                            '"has_any_findings":null,"has_unresolved_findings":null',
                            '"has_any_findings":false,"has_unresolved_findings":false', 1)
                    self.assertEqual(self.result(self.snapshot(fixture))[0], "success")
                    fixture["reactions"] = []
                    self.assertEqual(self.result(self.snapshot(fixture))[0], "pending")

    def test_unknown_flags_reject_unrecognized_receipt_prose(self):
        suffixes = (":warning:", ":unknown:", ":-1:", ":+1: :+1:", ":+1:!", "👍👍", "👍\u200b",
                    "Nice work!", "A critical finding remains.", ":+1: A critical finding remains.",
                    ":+1:\nA critical finding remains.", ":+1: <!-- finding -->", ":+1: [finding](url)")
        for suffix in suffixes:
            for known_flags in (False, True):
                with self.subTest(suffix=suffix, known_flags=known_flags):
                    fixture = self.with_reaction()
                    original = fixture["comments"][1]["body"]
                    line = next(line for line in original.splitlines() if line.startswith("Codex Review:"))
                    fixture["comments"][1]["body"] = original.replace(
                        line, "Codex Review: Didn't find any major issues. " + suffix)
                    if known_flags:
                        fixture["comments"][0]["body"] = fixture["comments"][0]["body"].replace(
                            '"has_any_findings":null,"has_unresolved_findings":null',
                            '"has_any_findings":false,"has_unresolved_findings":false', 1)
                    if known_flags:
                        self.assertEqual(self.result(self.snapshot(fixture))[0], "success")
                    else:
                        with self.assertRaises(ValueError):
                            self.snapshot(fixture)

    def test_unknown_clean_receipt_suffix_is_rejected(self):
        fixture = self.with_reaction()
        fixture["comments"][1]["body"] = fixture["comments"][1]["body"].replace(
            "Didn't find any major issues.", "Didn't find any major issues. A finding remains.")
        with self.assertRaises(ValueError):
            self.snapshot(fixture)

    def test_modern_known_clean_flags_require_fresh_reaction(self):
        fixture = self.with_reaction()
        fixture["comments"][0]["body"] = fixture["comments"][0]["body"].replace(
            '"has_any_findings":null,"has_unresolved_findings":null',
            '"has_any_findings":false,"has_unresolved_findings":false', 1)
        result, state = self.result(self.snapshot(fixture))
        self.assertEqual(result, "success")
        self.assertEqual(state["proof"]["kind"], "clean_codex")
        fixture["reactions"] = []
        self.assertEqual(self.result(self.snapshot(fixture))[0], "pending")

    def test_modern_unknown_flags_require_fresh_receipt_and_reaction(self):
        snapshot = self.snapshot(self.with_reaction())
        self.assertTrue(snapshot["structured"]["unknown_findings"])
        result, state = self.result(snapshot)
        self.assertEqual(result, "success")
        self.assertEqual(state["proof"]["kind"], "clean_codex")
        self.assertEqual(self.result(self.snapshot(self.fixture()))[0], "pending")
        fixture = self.with_reaction()
        fixture["comments"][0]["body"] = fixture["comments"][0]["body"].replace("Manual request", "New commits")
        self.assertEqual(self.result(self.snapshot(fixture))[0], "success")

    def test_receipt_arriving_during_running_poll_remains_fresh(self):
        fixture = self.with_reaction()
        complete = self.snapshot(fixture)
        running_fixture = copy.deepcopy(fixture)
        summary = running_fixture["comments"][0]
        summary["body"] = summary["body"].replace("Manual request", "New commits").replace('"status":"completed"', '"status":"running"').replace("**Completed**", "**Running**")
        running_fixture["comments"] = [summary]
        running_fixture["reactions"] = []
        running = self.snapshot(running_fixture)
        origin = gate.timestamp("2026-10-18T03:54:00Z")
        state = gate.initialize(running, origin)
        gate.observe(state, running, origin + 30)
        running_fixture["comments"].append(fixture["comments"][1])
        summary["updated_at"] = "2026-10-18T03:55:33Z"
        gate.observe(state, self.snapshot(running_fixture), origin + 93)
        self.assertEqual(gate.observe(state, complete, origin + 120)[0], "pending")
        self.assertEqual(gate.observe(state, complete, origin + 150)[0], "success")
        restarted = self.snapshot(running_fixture)
        gate.observe(state, restarted, origin + 160)
        for now in (origin + 180, origin + 210):
            self.assertEqual(gate.observe(state, complete, now)[0], "pending")

    def test_active_same_head_new_cycle_excludes_previous_cycle_receipt(self):
        fixture = self.with_reaction()
        complete = self.snapshot(fixture)
        summary = fixture["comments"][0]
        summary["body"] = summary["body"].replace('"status":"completed"', '"status":"running"').replace("**Completed**", "**Running**")
        receipt = fixture["comments"].pop()
        fixture["reactions"] = []
        origin = gate.timestamp("2026-10-18T03:54:00Z")
        running = self.snapshot(fixture)
        state = gate.initialize(running, origin)
        gate.observe(state, running, origin + 30)
        fixture["comments"].append(receipt)
        for change in ("trigger", "start_time"):
            with self.subTest(change=change):
                changed = copy.deepcopy(fixture)
                body = changed["comments"][0]["body"]
                changed["comments"][0]["body"] = body.replace(
                    "Manual request", "New commits") if change == "trigger" else body.replace(
                    "03:55:34.465182Z", "03:55:35.465182Z")
                restored = copy.deepcopy(state)
                gate.observe(restored, self.snapshot(changed), origin + 96)
                gate.observe(restored, complete, origin + 120)
                self.assertEqual(gate.observe(restored, complete, origin + 150)[0], "pending")

    def test_newer_wrong_head_receipt_blocks_old_current_head_receipt(self):
        fixture = self.with_reaction()
        newest = copy.deepcopy(fixture["comments"][1])
        newest.update(id=3, created_at="2026-10-18T03:55:33Z", updated_at="2026-10-18T03:55:33Z")
        newest["body"] = newest["body"].replace("`aaaaaaaaaa`", "`bbbbbbbbbb`")
        fixture["comments"].append(newest)
        api = ManualFixtureTests().api(fixture)
        original = api.repo_call
        api.repo_call = lambda path: {"sha": "b" * 40} if path == "commits/bbbbbbbbbb" else original(path)
        self.assertEqual(self.result(api.snapshot(1))[0], "pending")
        for created in ("2026-10-18T03:55:32Z", "2026-10-18T03:55:31Z"):
            newest.update(created_at=created, updated_at=created)
            fixture["comments"][-1] = newest
            self.assertEqual(self.result(api.snapshot(1))[0],
                             "pending" if created.endswith("32Z") else "success")

    def test_unknown_flags_require_complete_finding_chronology(self):
        for field in ("created_at", "updated_at"):
            fixture = self.with_reaction()
            finding = copy.deepcopy(fixture["comments"][1])
            finding.update(id=3, body="P2 Badge finding", created_at="2026-10-18T03:50:00Z", updated_at="2026-10-18T03:50:00Z")
            del finding[field]
            fixture["comments"].append(finding)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.snapshot(fixture)

    def test_trusted_prose_can_mention_receipt_marker(self):
        fixture = self.with_reaction()
        fixture["comments"].append(fixture["comments"][1] | {
            "id": 3, "body": "The code uses `codex-review-receipt:v1` as a marker."})
        self.assertEqual(self.result(self.snapshot(fixture))[0], "success")
        fixture["comments"][-1]["body"] = "P2 Badge The code uses `codex-review-receipt:v1` as a marker."
        self.assertEqual(self.result(self.snapshot(fixture))[0], "pending")

    def test_receipt_existing_at_first_active_observation_stays_excluded(self):
        fixture = self.with_reaction()
        complete = self.snapshot(fixture)
        fixture["comments"][0]["body"] = fixture["comments"][0]["body"].replace(
            '"status":"completed"', '"status":"running"').replace("**Completed**", "**Running**")
        running = self.snapshot(fixture)
        origin = gate.timestamp("2026-10-18T03:54:00Z")
        state = gate.initialize(running, origin, observed_since=origin)
        gate.observe(state, running, origin + 93)
        for now in (origin + 120, origin + 150):
            self.assertEqual(gate.observe(state, complete, now)[0], "pending")

    def test_unknown_flags_cannot_use_reaction_without_modern_receipt(self):
        for change in ("missing", "legacy", "spoof_user", "spoof_app", "wrong_head", "extra_text", "duplicate_marker"):
            with self.subTest(change=change):
                fixture = self.with_reaction()
                receipt = fixture["comments"][1]
                if change == "missing":
                    fixture["comments"].pop()
                elif change == "legacy":
                    receipt["body"] = receipt["body"].removeprefix("<!-- codex-review-receipt:v1 -->\n\n")
                elif change == "spoof_user":
                    receipt["user"]["id"] = 123
                elif change == "spoof_app":
                    receipt["performed_via_github_app"]["id"] = 123
                elif change == "wrong_head":
                    receipt["body"] = receipt["body"].replace("`aaaaaaaaaa`", "`bbbbbbbbbb`")
                elif change == "extra_text":
                    receipt["body"] += "\nA finding remains unresolved."
                elif change == "duplicate_marker":
                    receipt["body"] = "<!-- codex-review-receipt:v1 -->\n\n" + receipt["body"]
                try:
                    snapshot = self.snapshot(fixture)
                except ValueError:
                    continue
                self.assertEqual(self.result(snapshot)[0], "pending")

    def test_unknown_flags_cannot_use_stale_ambiguous_or_active_evidence(self):
        for change in ("old_receipt", "old_reaction", "future_reaction", "ambiguous", "eyes", "running", "failed"):
            with self.subTest(change=change):
                fixture = self.with_reaction()
                receipt = fixture["comments"][1]
                if change == "old_receipt":
                    receipt["created_at"] = receipt["updated_at"] = "2026-10-18T03:53:59Z"
                elif change == "old_reaction":
                    fixture["reactions"][0]["created_at"] = "2026-10-18T03:55:34Z"
                elif change == "future_reaction":
                    fixture["reactions"][0]["created_at"] = "2026-10-18T03:57:00Z"
                elif change == "ambiguous":
                    fixture["comments"].append(receipt | {"id": 3})
                elif change == "eyes":
                    fixture["reactions"].append({"id": 11, "content": "eyes", "user": {"id": gate.BOT_ID}})
                else:
                    summary = fixture["comments"][0]
                    summary["body"] = summary["body"].replace('"status":"completed"', '"status":"' + change + '"')
                    summary["body"] = summary["body"].replace("**Completed**", "**" + change.title() + "**")
                snapshot = self.snapshot(fixture)
                if change == "failed":
                    now = gate.timestamp("2026-10-18T03:56:00Z")
                    self.assertEqual(gate.observe(self.state(snapshot), snapshot, now)[0], "failure")
                else:
                    self.assertEqual(self.result(snapshot)[0], "pending")

    def test_partial_null_and_unknown_security_findings_are_rejected(self):
        for flags in ((None, False), (False, None), (True, None), (None, True)):
            fixture = self.with_reaction()
            body = fixture["comments"][0]["body"]
            marker = body.split("<!-- codex-review-state:v1 ")[1].split(" -->")[0]
            value = json.loads(marker)
            value["code_review"].update(has_any_findings=flags[0], has_unresolved_findings=flags[1])
            fixture["comments"][0]["body"] = body.replace(marker, json.dumps(value))
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                self.snapshot(fixture)
        fixture = self.with_reaction()
        body = fixture["comments"][0]["body"]
        marker = body.split("<!-- codex-review-state:v1 ")[1].split(" -->")[0]
        value = json.loads(marker)
        value["security_review"] = copy.deepcopy(value["code_review"])
        fixture["comments"][0]["body"] = body.replace(marker, json.dumps(value)) + '\n| **Security Review** | **Completed** <relative-time datetime="2026-10-18T03:55:34Z">done</relative-time> | `aaaaaaa` | Manual request |'
        with self.assertRaises(ValueError):
            self.snapshot(fixture)

    def test_unknown_flags_cannot_bypass_historical_dispositions(self):
        snapshot = self.snapshot(self.fixture())
        root = {"id": 20, "original_commit_id": "b" * 40, "created_at": "2026-10-18T03:50:00Z"}
        snapshot["inventory"] = [{"key": "inline:20", "root": root, "resolved": True}]
        self.assertEqual(self.result(snapshot)[0], "pending")
        snapshot["dispositions"] = {"inline:20": {"id": 21, "valid": True, "author": 42,
                                                "created": gate.timestamp("2026-10-18T03:55:40Z"), "digest": "verified"}}
        result, state = self.result(snapshot)
        self.assertEqual(result, "success")
        self.assertEqual(state["proof"]["kind"], "verified_dispositions")
        now = gate.timestamp("2026-10-18T03:56:00Z")
        state = self.state(snapshot)
        gate.observe(state, snapshot, now)
        snapshot["manual"]["comments"][0]["digest"] = "changed-receipt"
        self.assertEqual(gate.observe(state, snapshot, now + 30)[0], "pending")
        snapshot["manual"]["comments"] = []
        self.assertEqual(self.result(snapshot)[0], "pending")

    def test_invalid_newest_receipt_cannot_fall_back_to_older_clean_receipt(self):
        for change in ("extra_text", "duplicate_marker", "unknown_marker", "malformed_body"):
            with self.subTest(change=change):
                fixture = self.with_reaction()
                newest = copy.deepcopy(fixture["comments"][1])
                newest.update(id=3, created_at="2026-10-18T03:55:33Z", updated_at="2026-10-18T03:55:33Z")
                if change == "extra_text":
                    newest["body"] += "\nA finding remains unresolved."
                elif change == "duplicate_marker":
                    newest["body"] = "<!-- codex-review-receipt:v1 -->\n\n" + newest["body"]
                elif change == "unknown_marker":
                    newest["body"] = newest["body"].replace("receipt:v1", "receipt:v2")
                else:
                    newest["body"] = newest["body"].replace("Didn't find any major issues.", "A finding remains unresolved.")
                fixture["comments"].append(newest)
                with self.assertRaises(ValueError):
                    self.snapshot(fixture)

    def test_explicit_unresolved_flags_override_modern_clean_receipt(self):
        fixture = self.with_reaction()
        body = fixture["comments"][0]["body"]
        fixture["comments"][0]["body"] = body.replace('"has_any_findings":null,"has_unresolved_findings":null',
                                                                  '"has_any_findings":true,"has_unresolved_findings":true', 1)
        self.assertEqual(self.result(self.snapshot(fixture))[0], "pending")

    def test_unknown_code_flags_cannot_hide_reported_security_findings(self):
        fixture = self.with_reaction()
        body = fixture["comments"][0]["body"]
        marker = body.split("<!-- codex-review-state:v1 ")[1].split(" -->")[0]
        value = json.loads(marker)
        value["security_review"] = {"commit_sha": "a" * 40, "status": "completed",
                                    "has_any_findings": True, "has_unresolved_findings": False}
        fixture["comments"][0]["body"] = body.replace(marker, json.dumps(value)) + '\n| **Security Review** | **Completed** <relative-time datetime="2026-10-18T03:55:34Z">done</relative-time> | `aaaaaaa` | Manual request |'
        self.assertEqual(self.result(self.snapshot(fixture))[0], "pending")


class AnotherRoundReceiptTests(ModernReceiptTests):
    def fixture(self):
        fixture = super().fixture()
        fixture["comments"][1]["body"] = fixture["comments"][1]["body"].replace(
            "Keep it up!", "Another round soon, please!")
        return fixture


class ThumbsUpReceiptTests(ModernReceiptTests):
    def fixture(self):
        fixture = super().fixture()
        fixture["comments"][1]["body"] = fixture["comments"][1]["body"].replace("Keep it up!", ":+1:")
        return fixture


class ReviewChronologyTests(unittest.TestCase):
    def page(self, nodes, more=False, cursor=None):
        return {"data": {"repository": {"pullRequest": {"reviews": {
            "nodes": nodes, "pageInfo": {"hasNextPage": more, "endCursor": cursor}}}}}}

    def test_review_updates_join_by_id_across_pages(self):
        api = gate.GitHub("unused", "owner/repo")
        pages = [self.page([{"databaseId": 2, "updatedAt": "2026-10-18T03:50:00Z"}], True, "next"),
                 self.page([{"databaseId": 1, "updatedAt": "2026-10-18T03:51:00Z"}])]
        api.call = mock.Mock(side_effect=pages)
        self.assertEqual(api.review_updates(1), {2: "2026-10-18T03:50:00Z", 1: "2026-10-18T03:51:00Z"})
        self.assertEqual(api.call.call_args_list[1].args[1]["variables"]["after"], "next")

    def test_incomplete_review_updates_block(self):
        row = {"databaseId": 1, "updatedAt": "2026-10-18T03:50:00Z"}
        for pages in ([{"errors": [{"message": "denied"}]}],
                      [self.page([row | {"updatedAt": None}])],
                      [self.page([row, row])],
                      [self.page([row], True, None)],
                      [self.page([row], True, "next"), self.page([row])]):
            with self.subTest(pages=pages):
                api = gate.GitHub("unused", "owner/repo")
                api.call = mock.Mock(side_effect=pages)
                with self.assertRaises(ValueError):
                    api.review_updates(1)


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
        api.review_threads = lambda number: {}
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

    def test_fractional_completion_accepts_same_second_summary_update(self):
        fixture = self.fixture()
        snapshot = self.api(fixture).snapshot(1)
        snapshot["manual"]["summary_updated"] = int(snapshot["manual"]["completed_at"])
        origin = fixture["state"]["started"]
        state = gate.initialize(snapshot, origin + 3600, observed_since=origin)
        self.assertEqual(gate.observe(state, snapshot, origin + 3600)[0], "pending")
        self.assertEqual(gate.observe(state, snapshot, origin + 3630)[0], "success")

    def test_fractional_completion_rejects_summary_from_previous_second(self):
        fixture = self.fixture()
        snapshot = self.api(fixture).snapshot(1)
        snapshot["manual"]["summary_updated"] = int(snapshot["manual"]["completed_at"]) - 1
        origin = fixture["state"]["started"]
        state = gate.initialize(snapshot, origin + 3600, observed_since=origin)
        self.assertEqual(gate.observe(state, snapshot, origin + 3600)[0], "pending")
        self.assertEqual(gate.observe(state, snapshot, origin + 3630)[0], "pending")

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


class ExplicitCleanReactionTests(unittest.TestCase):
    def fixture(self):
        fixture = ModernReceiptTests().with_reaction()
        fixture["comments"][0]["body"] = fixture["comments"][0]["body"].replace(
            '"has_any_findings":null,"has_unresolved_findings":null',
            '"has_any_findings":false,"has_unresolved_findings":false', 1)
        return fixture

    def snapshot(self, fixture):
        return ManualFixtureTests().api(fixture).snapshot(1)

    def state(self, snapshot):
        origin = gate.timestamp("2026-10-18T03:54:00Z")
        return gate.initialize(snapshot | {"thumbs": [], "summary": "before-review"}, origin)

    def result(self, snapshot, state=None):
        now = gate.timestamp("2026-10-18T03:56:00Z")
        state = self.state(snapshot) if state is None else state
        self.assertEqual(gate.observe(state, snapshot, now)[0], "pending")
        return gate.observe(state, snapshot, now + 30)[0], state

    def test_receipt_text_or_absence_does_not_decide_explicit_clean_review(self):
        for text in ("Bravo.", "Arbitrary notification text.", None):
            with self.subTest(text=text):
                fixture = self.fixture()
                if text is None:
                    fixture["comments"] = fixture["comments"][:1]
                else:
                    fixture["comments"][1]["body"] = fixture["comments"][1]["body"].replace("Keep it up!", text)
                snapshot = self.snapshot(fixture)
                self.assertIsNone(snapshot["manual"])
                result, state = self.result(snapshot)
                self.assertEqual(result, "success")
                self.assertEqual(state["proof"]["kind"], "clean_codex")

    def test_reaction_must_follow_all_observation_and_summary_boundaries(self):
        for change in ("absent", "old", "same_second", "future", "summary_updated", "missing_times", "baseline"):
            with self.subTest(change=change):
                fixture = self.fixture()
                if change == "absent": fixture["reactions"] = []
                elif change == "old": fixture["reactions"][0]["created_at"] = "2026-10-18T03:53:59Z"
                elif change == "same_second": fixture["reactions"][0]["created_at"] = fixture["comments"][0]["updated_at"]
                elif change == "future": fixture["reactions"][0]["created_at"] = "2026-10-18T04:00:00Z"
                elif change == "summary_updated": fixture["comments"][0]["updated_at"] = "2026-10-18T03:55:38Z"
                snapshot = self.snapshot(fixture)
                if change == "missing_times": del snapshot["reaction_times"]
                state = self.state(snapshot)
                if change == "baseline": state["baseline"] = snapshot["thumbs"]
                self.assertEqual(self.result(snapshot, state)[0], "pending")

    def test_explicit_nonclean_lifecycle_never_accepts_arbitrary_receipt(self):
        for status in ("running", "queued", "pending", "failed", "findings", "unresolved"):
            with self.subTest(status=status):
                fixture = self.fixture()
                summary = fixture["comments"][0]
                fixture["comments"][1]["body"] = fixture["comments"][1]["body"].replace("Keep it up!", "Bravo.")
                if status in ("findings", "unresolved"):
                    summary["body"] = summary["body"].replace('"has_any_findings":false', '"has_any_findings":true')
                    if status == "unresolved":
                        summary["body"] = summary["body"].replace('"has_unresolved_findings":false', '"has_unresolved_findings":true')
                else:
                    summary["body"] = summary["body"].replace('"status":"completed"', '"status":"' + status + '"').replace("**Completed**", "**" + status.title() + "**")
                snapshot = self.snapshot(fixture)
                now = gate.timestamp("2026-10-18T03:56:00Z")
                state = self.state(snapshot)
                gate.observe(state, snapshot, now)
                self.assertEqual(gate.observe(state, snapshot, now + 30)[0], "failure" if status == "failed" else "pending")

    def test_same_head_restart_invalidates_old_success_until_new_thumb(self):
        fixture = self.fixture()
        complete = self.snapshot(fixture)
        result, state = self.result(complete)
        self.assertEqual(result, "success")
        now = gate.timestamp("2026-10-18T03:57:00Z")
        for phase in ({"eyes": True}, {"completed": False, "review_completed": False}):
            with self.subTest(phase=phase):
                restarted = copy.deepcopy(state)
                self.assertEqual(gate.observe(restarted, complete | phase, now)[0], "pending")
                self.assertEqual(gate.observe(restarted, complete, now + 30)[0], "pending")
                self.assertEqual(gate.observe(restarted, complete, now + 60)[0], "pending")
                later = copy.deepcopy(complete)
                later["thumbs"] = ["11"]
                later["reaction_times"] = {"11": now + 61}
                self.assertEqual(gate.observe(restarted, later, now + 90)[0], "pending")
                self.assertEqual(gate.observe(restarted, later, now + 120)[0], "success")

    def finding_snapshot(self, *, resolved=True, created="2026-10-18T03:55:00Z", submitted="2026-10-18T03:55:00Z"):
        fixture = self.fixture()
        api = ManualFixtureTests().api(fixture)
        original = api.pages
        head = fixture["pr"]["head"]["sha"]
        root = {"id": 71, "user": {"id": gate.BOT_ID}, "original_commit_id": head,
                "pull_request_review_id": 81, "created_at": created}
        review = {"id": 81, "user": {"id": gate.BOT_ID}, "commit_id": head,
                  "state": "COMMENTED", "body": gate.FINDING_REVIEW_WRAPPER.replace("{commit}", head),
                  "submitted_at": submitted}
        api.pages = lambda path: ([root] if path == "pulls/1/comments" else
                                [review] if path == "pulls/1/reviews" else original(path))
        api.review_threads = lambda number: {71: resolved}
        return api.snapshot(1)

    def test_resolved_associated_same_head_findings_precede_clean_completion(self):
        self.assertEqual(self.result(self.finding_snapshot())[0], "success")
        for changes in ({"resolved": False}, {"created": "2026-10-18T03:55:35Z"},
                        {"created": None}, {"submitted": None}, {"submitted": "2026-10-18T03:55:35Z"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.result(self.finding_snapshot(**changes))[0], "pending")

    def test_standalone_and_unassociated_findings_remain_blocking(self):
        snapshot = self.snapshot(self.fixture())
        for item in ({"key": "issue:99"}, {"key": "review:99"},
                     {"key": "inline:99", "resolved": True, "root": {
                         "original_commit_id": "a" * 40, "created_at": "2026-10-18T03:50:00Z"}}):
            with self.subTest(item=item):
                self.assertEqual(self.result(snapshot | {"inventory": [item]})[0], "pending")


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


class ResolvedFindingTests(unittest.TestCase):
    fixture = ManualFixtureTests.fixture
    api = ManualFixtureTests.api

    def finding_api(self, resolved=True, old=False, review=True):
        fixture = self.fixture()
        api = self.api(fixture)
        original = api.pages
        head = fixture["pr"]["head"]["sha"]
        finding = {"id": 71, "user": {"id": gate.BOT_ID},
                   "original_commit_id": "b" * 40 if old else head,
                   "pull_request_review_id": 81}
        review_row = {"id": 81, "user": {"id": gate.BOT_ID}, "commit_id": head,
                      "state": "COMMENTED", "body": gate.FINDING_REVIEW_WRAPPER.replace("{commit}", head),
                      "submitted_at": "2026-10-10T14:32:00Z"}
        api.pages = lambda path: ([finding] if path == "pulls/1/comments" else
                                [review_row] if path == "pulls/1/reviews" and review else original(path))
        api.review_threads = lambda number: {71: resolved}
        return api, fixture, review_row

    def verdict(self, api, fixture):
        snapshot = api.snapshot(1)
        origin = fixture["state"]["started"]
        state = gate.initialize(snapshot, origin + 3600, observed_since=origin)
        gate.observe(state, snapshot, origin + 3600)
        return gate.observe(state, snapshot, origin + 3630)[0]

    def test_resolved_thread_and_earlier_aggregate_accept_fresh_clean(self):
        api, fixture, _ = self.finding_api()
        self.assertEqual(self.verdict(api, fixture), "success")

    def test_unresolved_old_head_remains_blocking(self):
        api, fixture, _ = self.finding_api(resolved=False, old=True, review=False)
        self.assertNotEqual(self.verdict(api, fixture), "success")

    def test_later_or_missing_aggregate_time_cannot_be_superseded(self):
        for submitted in (None, "2026-10-10T14:33:32Z", "2026-10-10T14:33:33Z"):
            api, fixture, row = self.finding_api()
            row["submitted_at"] = submitted
            self.assertNotEqual(self.verdict(api, fixture), "success")

    def test_unthreaded_review_cannot_be_superseded(self):
        api, fixture, row = self.finding_api()
        row["id"] = 82
        self.assertNotEqual(self.verdict(api, fixture), "success")

    def test_missing_thread_is_incomplete_evidence(self):
        api, _, _ = self.finding_api()
        api.review_threads = lambda number: {}
        with self.assertRaises(ValueError):
            api.snapshot(1)

    def test_latest_clean_completion_supersedes_older_clean(self):
        api, fixture, _ = self.finding_api()
        previous = copy.deepcopy(fixture["comments"][1])
        previous.update(id=3, created_at="2026-10-10T14:31:00Z", updated_at="2026-10-10T14:31:00Z")
        fixture["comments"].append(previous)
        self.assertEqual(self.verdict(api, fixture), "success")


    def test_unresolved_current_head_blocks_even_later_clean(self):
        api, fixture, _ = self.finding_api(resolved=False)
        self.assertNotEqual(self.verdict(api, fixture), "success")

    def test_resolved_thread_without_fresh_acceptance_stays_pending(self):
        api, fixture, _ = self.finding_api()
        snapshot = api.snapshot(1)
        now = fixture["state"]["started"] + 3600
        state = gate.initialize(snapshot, now)
        gate.observe(state, snapshot, now)
        self.assertNotEqual(gate.observe(state, snapshot, now + 30)[0], "success")

    def test_latest_completion_must_not_fall_back_to_older_valid_one(self):
        api, fixture, _ = self.finding_api()
        latest = copy.deepcopy(fixture["comments"][1])
        latest.update(id=3, created_at="2026-10-10T14:33:33Z", updated_at="2026-10-10T14:34:00Z")
        fixture["comments"].append(latest)
        self.assertNotEqual(self.verdict(api, fixture), "success")

    def test_tied_latest_completions_are_ambiguous(self):
        api, fixture, _ = self.finding_api()
        fixture["comments"].append(fixture["comments"][1] | {"id": 3})
        self.assertNotEqual(self.verdict(api, fixture), "success")

    def test_changes_requested_requires_resolved_threads_and_clean_review(self):
        api, fixture, row = self.finding_api()
        row["state"] = "CHANGES_REQUESTED"
        self.assertEqual(self.verdict(api, fixture), "success")
        api.review_threads = lambda number: {71: False}
        self.assertNotEqual(self.verdict(api, fixture), "success")

    def test_thread_not_in_rest_evidence_is_incomplete(self):
        api, _, _ = self.finding_api()
        api.review_threads = lambda number: {71: True, 72: False}
        with self.assertRaises(ValueError):
            api.snapshot(1)

    def test_rest_reply_uses_root_resolution(self):
        api, fixture, _ = self.finding_api()
        original = api.pages
        def pages(path):
            rows = original(path)
            if path == "pulls/1/comments":
                rows.append(rows[0] | {"id": 72, "in_reply_to_id": 71})
            return rows
        api.pages = pages
        self.assertEqual(self.verdict(api, fixture), "success")


class ReviewThreadTests(unittest.TestCase):
    def page(self, root=71, resolved=False, next_page=False, cursor=None):
        return {"data": {"repository": {"pullRequest": {"reviewThreads": {
            "nodes": [{"id": "thread-" + str(root), "isResolved": resolved,
                       "comments": {"nodes": [{"databaseId": root}]}}],
            "pageInfo": {"hasNextPage": next_page, "endCursor": cursor}}}}}}

    def test_all_thread_pages_are_read(self):
        api = gate.GitHub("never-used", "owner/repo")
        pages = [self.page(resolved=True, next_page=True, cursor="next"), self.page(root=72)]
        calls = []
        def call(path, data):
            calls.append((path, data["variables"]["after"]))
            return pages.pop(0)
        api.call = call
        self.assertEqual(api.review_threads(1), {71: True, 72: False})
        self.assertEqual(calls, [("graphql", None), ("graphql", "next")])

    def test_graphql_errors_and_malformed_pages_fail_closed(self):
        valid = self.page()
        variants = [valid | {"errors": [{"message": "partial"}]}, {"data": None}]
        for field, value in (("isResolved", None), ("id", None)):
            page = self.page()
            page["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"][0][field] = value
            variants.append(page)
        page = self.page()
        page["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"][0]["comments"]["nodes"] = []
        variants.append(page)
        variants.append(self.page(next_page=True))
        for response in variants:
            with self.subTest(response=response):
                api = gate.GitHub("never-used", "owner/repo")
                api.call = lambda *args: response
                with self.assertRaises((ValueError, TypeError, KeyError)):
                    api.review_threads(1)

    def test_duplicate_pages_and_cursor_cycles_fail_closed(self):
        api = gate.GitHub("never-used", "owner/repo")
        api.call = lambda *args: self.page(next_page=True, cursor="same")
        with self.assertRaises(ValueError):
            api.review_threads(1)


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
