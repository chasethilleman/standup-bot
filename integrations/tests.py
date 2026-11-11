import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from integrations.services.claude_code import ClaudeCodeService, clean_text, extract_tickets
from integrations.services.github import GitHubService, branch_tip_date, iter_recent_branch_names


def branch(name: str, tip_date: datetime):
    return SimpleNamespace(
        name=name,
        commit=SimpleNamespace(
            commit=SimpleNamespace(
                author=SimpleNamespace(date=tip_date),
                committer=SimpleNamespace(date=tip_date),
            )
        ),
    )


class BranchCommitSyncTests(SimpleTestCase):
    def setUp(self):
        self.since = datetime(2026, 6, 15, tzinfo=timezone.utc)
        self.until = datetime(2026, 6, 16, 23, 59, 59, tzinfo=timezone.utc)

    def test_iter_recent_branch_names_skips_default_and_stale_branches(self):
        repo = SimpleNamespace(default_branch="main")
        repo.get_branches = MagicMock(
            return_value=[
                branch("main", datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)),
                branch("chase/ATH-1577", datetime(2026, 6, 16, 11, 0, tzinfo=timezone.utc)),
                branch("chase/ATH-1533", datetime(2026, 6, 15, 22, 0, tzinfo=timezone.utc)),
                branch("old/feature", datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)),
            ]
        )

        names = list(iter_recent_branch_names(repo, self.since))

        self.assertEqual(names, ["chase/ATH-1577", "chase/ATH-1533"])

    def test_sync_authored_branch_commits_fetches_recent_branches(self):
        service = GitHubService()
        repo = SimpleNamespace(full_name="Athlete-To-Athlete/delos", default_branch="main")
        repo.get_branches = MagicMock(
            return_value=[
                branch("main", datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)),
                branch("chase/ATH-1577", datetime(2026, 6, 16, 11, 0, tzinfo=timezone.utc)),
            ]
        )

        commit = SimpleNamespace(
            sha="abc123",
            html_url="https://example.com/commit/abc123",
            commit=SimpleNamespace(
                message="[ATH-1577] Store partner quiz answers",
                author=SimpleNamespace(date=datetime(2026, 6, 16, 11, 46, tzinfo=timezone.utc)),
            ),
            stats=SimpleNamespace(additions=10, deletions=2),
        )
        repo.get_commits = MagicMock(return_value=[commit])

        seen_shas = set()
        activities = []
        with patch.object(service, "save_activity", return_value=SimpleNamespace()) as save_activity:
            service.sync_authored_branch_commits(
                repo,
                "chasethilleman",
                self.since,
                self.until,
                seen_shas,
                activities,
            )

        repo.get_commits.assert_called_once_with(
            sha="chase/ATH-1577",
            author="chasethilleman",
            since=self.since,
            until=self.until,
        )
        save_activity.assert_called_once()
        self.assertEqual(save_activity.call_args.kwargs["branch"], "chase/ATH-1577")
        self.assertEqual(save_activity.call_args.kwargs["ticket_id"], "ATH-1577")
        self.assertEqual(len(activities), 1)

    def test_branch_tip_date_falls_back_to_committer(self):
        branch_obj = SimpleNamespace(
            commit=SimpleNamespace(
                commit=SimpleNamespace(
                    author=SimpleNamespace(date=None),
                    committer=SimpleNamespace(
                        date=datetime(2026, 6, 16, 9, 0, tzinfo=timezone.utc)
                    ),
                )
            )
        )

        self.assertEqual(
            branch_tip_date(branch_obj),
            datetime(2026, 6, 16, 9, 0, tzinfo=timezone.utc),
        )


class ClaudeCodeSyncTests(SimpleTestCase):
    """Chat transcripts → one activity per session per local day."""

    def setUp(self):
        import tempfile
        from zoneinfo import ZoneInfo

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.projects = os.path.join(self.tmp.name, "projects")
        self.sessions = os.path.join(self.tmp.name, "sessions")
        os.makedirs(os.path.join(self.projects, "-Users-me-repo"))
        os.makedirs(os.path.join(self.sessions, "acct", "org"))
        self.repo = os.path.join(self.tmp.name, "delos")
        os.makedirs(os.path.join(self.repo, ".git"))

        tz_patch = patch("standups.services.generator.LOCAL_TZ", ZoneInfo("America/Los_Angeles"))
        tz_patch.start()
        self.addCleanup(tz_patch.stop)

        self.service = ClaudeCodeService()
        self.service.config_obj = SimpleNamespace(
            config={
                "projects_dir": self.projects,
                "sessions_dir": self.sessions,
                "ticket_prefixes": ["ATH"],
            }
        )
        self.since = datetime(2026, 11, 9, 8, 0, tzinfo=timezone.utc)  # Nov 9 00:00 PST
        self.until = datetime(2026, 11, 11, 7, 59, tzinfo=timezone.utc)  # Nov 10 23:59 PST

    def write_transcript(self, session_id, entries):
        path = os.path.join(self.projects, "-Users-me-repo", f"{session_id}.jsonl")
        with open(path, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")
        return path

    def user(self, ts, text, origin={"kind": "human"}):
        entry = {"type": "user", "timestamp": ts, "cwd": self.repo, "message": {"role": "user", "content": text}}
        if origin is not None:
            entry["origin"] = origin
        return entry

    def wrap_up(self, ts, recap):
        return {
            "type": "assistant",
            "timestamp": ts,
            "message": {"content": [{"type": "tool_use", "name": "mcp__ccd_turn__wrap_up", "input": {"recap": recap}}]},
        }

    def sync(self, path, desktop=None):
        with patch.object(self.service, "save_activity", side_effect=lambda **kw: SimpleNamespace(**kw)):
            return self.service.sync_transcript(path, desktop or {}, self.since, self.until)

    def test_human_prompts_and_recaps_become_one_activity_per_day(self):
        path = self.write_transcript(
            "abc",
            [
                {"type": "custom-title", "customTitle": "ATH-2626 QA comments"},
                self.user("2026-11-09T18:00:00Z", "address the QA comments on ath-2626"),
                self.wrap_up("2026-11-09T18:30:00Z", "Fixed the cutoff bug and pushed 761016c7."),
                self.user("2026-11-10T17:00:00Z", "<ci-monitor-event>CI failed</ci-monitor-event>", origin=None),
                self.user("2026-11-10T17:05:00Z", "now move it to Product Review"),
                self.wrap_up("2026-11-10T17:06:00Z", "Moved ATH-2626 to Product Review."),
            ],
        )

        activities = self.sync(path, {"abc": {"title": "ignored", "status_detail": "awaiting re-test"}})

        by_day = {a.external_id: a for a in activities}
        self.assertEqual(set(by_day), {"abc:2026-11-09", "abc:2026-11-10"})
        today = by_day["abc:2026-11-10"]
        self.assertEqual(today.source, "claude_code")
        self.assertEqual(today.title, "ATH-2626 QA comments")
        self.assertEqual(today.ticket_id, "ATH-2626")
        self.assertEqual(today.repository, "delos")
        self.assertIn("Asked: now move it to Product Review", today.description)
        self.assertNotIn("ci-monitor", today.description)
        self.assertIn("Outcome: Moved ATH-2626 to Product Review.", today.description)
        self.assertIn("Latest status: awaiting re-test", today.description)
        self.assertNotIn("Latest status", by_day["abc:2026-11-09"].description)

    def test_days_with_only_automated_turns_are_skipped(self):
        path = self.write_transcript(
            "auto",
            [
                self.user("2026-11-10T17:00:00Z", "<ci-monitor-event>CI failed</ci-monitor-event>", origin=None),
                self.user("2026-11-10T17:01:00Z", "<task-notification>done</task-notification>", origin={"kind": "task-notification"}),
                self.wrap_up("2026-11-10T17:06:00Z", "Re-ran CI."),
            ],
        )

        self.assertEqual(self.sync(path), [])

    def test_untitled_cli_session_uses_first_prompt_and_last_reply(self):
        path = self.write_transcript(
            "cli",
            [
                self.user("2026-11-10T16:00:00Z", "pull the parent CSV for announcements", origin=None),
                {"type": "user", "timestamp": "2026-11-10T16:00:05Z", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
                {"type": "assistant", "timestamp": "2026-11-10T16:01:00Z", "message": {"content": [{"type": "text", "text": "Built the CSV: 184 rows."}]}},
            ],
        )

        [activity] = self.sync(path)

        self.assertEqual(activity.title, "pull the parent CSV for announcements")
        self.assertEqual(activity.ticket_id, "")
        self.assertIn("Outcome: Built the CSV: 184 rows.", activity.description)
        self.assertEqual(activity.metadata["prompt_count"], 1)

    def test_pasted_content_is_collapsed(self):
        text = clean_text('see <pasted_content id="35b5">' + "secret " * 50 + '</pasted_content id="35b5"> please', 220)
        self.assertEqual(text, "see [pasted text] please")

    def test_ticket_only_mentioned_in_reply_is_not_the_sessions_ticket(self):
        path = self.write_transcript(
            "ci",
            [
                self.user("2026-11-10T16:00:00Z", "fix failed CI on pr 1795"),
                self.wrap_up("2026-11-10T16:30:00Z", "The red test came in from main via ATH-2705."),
            ],
        )

        [activity] = self.sync(path)

        self.assertEqual(activity.ticket_id, "")
        self.assertEqual(activity.metadata["tickets_mentioned"], ["ATH-2705"])

    def test_ticket_prefixes_filter_false_positives(self):
        self.assertEqual(extract_tickets("ath-12 uses UTF-8 and SHA-256", {"ATH"}), ["ATH-12"])
        self.assertEqual(extract_tickets("ath-12 and ATH-13, UTF-8", set()), ["ATH-13", "UTF-8"])

    def test_load_desktop_sessions_keys_by_cli_session(self):
        with open(os.path.join(self.sessions, "acct", "org", "local_1.json"), "w") as f:
            json.dump({"cliSessionId": "abc", "title": "CSV pull", "postTurnSummary": {"status_detail": "done"}}, f)

        self.assertEqual(
            self.service.load_desktop_sessions(),
            {"abc": {"title": "CSV pull", "status_detail": "done", "needs_action": ""}},
        )
