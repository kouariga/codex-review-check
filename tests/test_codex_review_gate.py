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
    result.update(changes)
    if "structured" not in changes:
        status = "failed" if result["failed"] else "completed" if result["completed"] else "running"
        flag = False if result["completed"] else None
        review = {"commit_sha": result["head"], "status": status,
                  "has_any_findings": flag, "has_unresolved_findings": flag}
        summary = {"id": 1, "body": "<!-- codex-review-state:v1 " + json.dumps({
            "code_review": review, "security_review": dict.fromkeys(review)}) + " -->"}
        result["structured"] = gate.structured_review(summary, result["head"])
    return result


class LifecycleTests(unittest.TestCase):
    def test_eyes_to_fresh_thumb_requires_two_stable_observations(self):
        state = gate.initialize(sample(), 0)
        self.assertEqual(gate.observe(state, sample(eyes=True), 30)[0], "pending")
        clean = sample(thumbs=["new"], summary="complete", completed=True)
        self.assertEqual(gate.observe(state, clean, 60)[0], "pending")
        self.assertEqual(gate.observe(state, clean, 89)[0], "pending")
        self.assertEqual(gate.observe(state, clean, 90)[0], "success")

    def test_explicit_clean_review_accepts_existing_reactions_and_summary(self):
        state = gate.initialize(sample(), 0)
        clean = sample(thumbs=["new"], summary="complete", completed=True)
        gate.observe(state, clean, 30)
        self.assertEqual(gate.observe(state, clean, 60)[0], "success")
        for stale in (sample(completed=True), sample(thumbs=["new"], completed=True),
                      sample(summary="complete", completed=True)):
            state = gate.initialize(stale, 0)
            gate.observe(state, stale, 30)
            self.assertEqual(gate.observe(state, stale, 60)[0], "success")

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
        for changed in (clean | {"findings": [1]}, sample(), clean | {"review_digest": "changed"}):
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

    def test_api_failure_cannot_become_empty_clean_snapshot(self):
        with self.assertRaises(OSError):
            self.api(error=True).snapshot(1)


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
        for key, value in (("commit_sha", "a" * 7),
                           ("has_any_findings", 1), ("has_unresolved_findings", None),
                           ("extra", False), ("status", "unknown")):
            changed = copy.deepcopy(fixture)
            changed["code_review"][key] = value
            variants.append(changed)
        variants += [{"security_review": fixture["security_review"]},
                     fixture | {"extra": {}},
                     fixture | {"code_review": dict.fromkeys(fixture["code_review"])}]
        for value in variants:
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                self.api(value).snapshot(1)
        marker = "<!-- codex-review-state:v1 " + json.dumps(fixture) + " -->"
        for invalid in (marker + marker, marker.replace(":v1", ":v2"),
                        '<!-- codex-review-state:v1 {"code_review":{},"code_review":{}} -->'):
            with self.subTest(marker=invalid), self.assertRaises(ValueError):
                self.api(marker=invalid).snapshot(1)

    def test_display_status_does_not_override_structured_status(self):
        for status in ("Running", "Failed"):
            self.assertTrue(self.api(status=status).snapshot(1)["review_completed"])

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


class ReceiptFixture:
    def fixture(self):
        return json.loads((ROOT / "tests/fixtures/modern-clean.json").read_text())


    def with_reaction(self):
        fixture = self.fixture()
        fixture["reactions"] = [{"id": 10, "content": "+1", "created_at": "2026-10-18T03:55:37Z",
                                 "user": {"id": gate.BOT_ID}}]
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


class FixtureAPI:
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
        api.review_updates = lambda number: {r['id']: r.get('updated_at', r.get('submitted_at'))
                                              for r in api.pages(f'pulls/{number}/reviews')}
        api.pages = lambda path: copy.deepcopy(
            fixture["reactions"] if path.endswith("reactions") else
            fixture["comments"] if path.startswith("issues/") else [])
        return api


class ExplicitCleanVerdictTests(unittest.TestCase):
    def fixture(self):
        fixture = ReceiptFixture().with_reaction()
        fixture["comments"][0]["body"] = fixture["comments"][0]["body"].replace(
            '"has_any_findings":null,"has_unresolved_findings":null',
            '"has_any_findings":false,"has_unresolved_findings":false', 1)
        return fixture

    def snapshot(self, fixture):
        return FixtureAPI().api(fixture).snapshot(1)

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
                self.assertNotIn("manual", snapshot)
                result, state = self.result(snapshot)
                self.assertEqual(result, "success")
                self.assertEqual(state["proof"]["kind"], "clean_codex")

    def test_reaction_and_observation_timing_do_not_affect_clean_verdict(self):
        for change in ("absent", "old", "same_second", "future", "summary_updated", "missing_times", "baseline"):
            with self.subTest(change=change):
                fixture = self.fixture()
                if change == "absent": fixture["reactions"] = []
                elif change == "old": fixture["reactions"][0]["created_at"] = "2026-10-18T03:53:59Z"
                elif change == "same_second": fixture["reactions"][0]["created_at"] = fixture["comments"][0]["updated_at"]
                elif change == "future": fixture["reactions"][0]["created_at"] = "2026-10-18T04:00:00Z"
                elif change == "summary_updated": fixture["comments"][0]["updated_at"] = "2026-10-18T03:55:38Z"
                snapshot = self.snapshot(fixture)
                if change == "missing_times": snapshot.pop("reaction_times", None)
                state = self.state(snapshot)
                if change == "baseline": state["baseline"] = ["ignored legacy reaction"]
                self.assertEqual(self.result(snapshot, state)[0], "success")


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

    def test_same_head_structured_restart_requires_new_confirmation_without_new_thumb(self):
        fixture = self.fixture()
        complete = self.snapshot(fixture)
        result, state = self.result(complete)
        self.assertEqual(result, "success")
        now = gate.timestamp("2026-10-18T03:57:00Z")
        fixture["comments"][0]["body"] = fixture["comments"][0]["body"].replace(
            '\"status\":\"completed\"', '\"status\":\"running\"')
        running = self.snapshot(fixture)
        self.assertEqual(gate.observe(state, running, now)[0], "pending")
        self.assertEqual(gate.observe(state, complete, now + 30)[0], "pending")
        self.assertEqual(gate.observe(state, complete, now + 60)[0], "success")

    def finding_snapshot(self, *, resolved=True, created="2026-10-18T03:55:00Z", submitted="2026-10-18T03:55:00Z", replies=(), updated=None):
        fixture = self.fixture()
        api = FixtureAPI().api(fixture)
        original = api.pages
        head = fixture["pr"]["head"]["sha"]
        root = {"id": 71, "user": {"id": gate.BOT_ID}, "original_commit_id": head,
                "pull_request_review_id": 81, "created_at": created, "updated_at": created}
        review = {"id": 81, "user": {"id": gate.BOT_ID}, "commit_id": head,
                  "state": "COMMENTED", "body": gate.FINDING_REVIEW_WRAPPER.replace("{commit}", head),
                  "submitted_at": submitted, "updated_at": updated or submitted}
        api.pages = lambda path: ([root, *replies] if path == "pulls/1/comments" else
                                [review] if path == "pulls/1/reviews" else original(path))
        api.review_threads = lambda number: {71: resolved}
        return api.snapshot(1)

    def test_structured_metadata_cannot_order_resolved_same_head_findings(self):
        self.assertEqual(self.result(self.finding_snapshot())[0], "pending")
        for changes in ({"resolved": False}, {"created": "2026-10-18T03:55:34Z"},
                        {"created": "2026-10-18T03:55:35Z"}, {"created": None},
                        {"submitted": None}, {"submitted": "2026-10-18T03:55:34Z"},
                        {"submitted": "2026-10-18T03:55:35Z"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.result(self.finding_snapshot(**changes))[0], "pending")

    def test_review_edit_after_clean_completion_remains_blocking(self):
        snapshot = self.finding_snapshot(updated="2026-10-18T03:55:50Z")
        self.assertEqual(self.result(snapshot)[0], "pending")

    def test_later_security_completion_cannot_supersede_new_code_finding(self):
        snapshot = self.finding_snapshot(created="2026-10-18T03:55:35Z")
        later = gate.timestamp("2026-10-18T03:55:36Z")
        snapshot["structured"]["latest_completed_at"] = later
        snapshot["structured"]["summary_updated"] = later
        snapshot["completed_at"] = later
        self.assertEqual(self.result(snapshot)[0], "pending")

    def test_resolved_finding_edited_after_clean_completion_stays_blocking(self):
        for updated in ("2026-10-18T03:55:34Z", "2026-10-18T03:55:50Z", None, "invalid"):
            with self.subTest(updated=updated):
                snapshot = self.finding_snapshot()
                snapshot["inventory"][0]["root"]["updated_at"] = updated
                snapshot["inventory_digest"] = gate.digest(snapshot["inventory"])
                self.assertEqual(self.result(snapshot)[0], "pending")

    def test_later_codex_thread_replies_block_but_maintainer_replies_do_not(self):
        for actor, expected in ((gate.BOT_ID, "pending"), (123, "pending")):
            for field in ("created_at", "updated_at"):
                with self.subTest(actor=actor, field=field):
                    reply = {"id": 72, "in_reply_to_id": 71, "user": {"id": actor}, "created_at": "2026-10-18T03:55:00Z",
                             "updated_at": "2026-10-18T03:55:00Z", "body": "P1 Badge revised finding"}
                    reply[field] = "2026-10-18T03:55:50Z"
                    snapshot = self.finding_snapshot(replies=[reply])
                    self.assertEqual(self.result(snapshot)[0], expected)

    def test_reaction_author_and_timestamp_do_not_affect_structured_verdict(self):
        for change in ("actor", "timestamp"):
            with self.subTest(change=change):
                fixture = self.fixture()
                if change == "actor":
                    fixture["reactions"][0]["user"]["id"] = 123
                else:
                    del fixture["reactions"][0]["created_at"]
                self.assertEqual(self.result(self.snapshot(fixture))[0], "success")

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

    def test_saved_success_for_different_base_cannot_reuse_same_head_metadata(self):
        old = sample(base={"ref": "release", "sha": "e" * 40})
        state = gate.initialize(old, -60)
        gate.observe(state, old | {"thumbs": ["new"], "summary": "complete", "completed": True}, -30)
        saved = {"id": 1, "name": gate.CHECK, "head_sha": "a" * 40,
                 "app": {"id": gate.ACTIONS_APP_ID}, "conclusion": "success",
                 "external_id": "codex-gate-v1:owner/repo:1:" + "a" * 40,
                 "output": {"text": json.dumps(state)}}
        stale = sample(thumbs=["new"], summary="complete", completed=True)
        api = FakeGitHub([stale], saved)
        self.assertEqual(self.run_gate(api), 0)
        self.assertEqual(api.writes[0]["status"], "in_progress")
        self.assertEqual(api.check["conclusion"], "failure")

    def test_unchanged_pending_output_is_not_written_each_poll(self):
        api = FakeGitHub([sample(structured=None)])
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
