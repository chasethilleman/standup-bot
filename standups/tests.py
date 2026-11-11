from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase

from standups.services import summarizer


def activity(
    *,
    source,
    activity_type,
    ticket_id="",
    title="",
    branch="",
    occurred_at,
):
    return SimpleNamespace(
        source=source,
        activity_type=activity_type,
        ticket_id=ticket_id,
        title=title,
        branch=branch,
        description="",
        occurred_at=occurred_at,
        metadata={},
    )


class CodeGroundedTodayTests(SimpleTestCase):
    def setUp(self):
        self.target_date = date(2026, 6, 15)
        self.tz = ZoneInfo("America/Los_Angeles")
        self.today_dt = datetime(2026, 6, 15, 12, 0, tzinfo=self.tz)
        self.yesterday_dt = datetime(2026, 6, 14, 12, 0, tzinfo=self.tz)

    def test_allowed_today_ticket_ids_uses_code_not_linear(self):
        activities = [
            activity(
                source="linear",
                activity_type="ticket_completed",
                ticket_id="ATH-1578",
                title="ATH-1578: Add UTM medium filter",
                occurred_at=self.today_dt,
            ),
            activity(
                source="github",
                activity_type="commit",
                ticket_id="ATH-1533",
                title="Fire purchase pixel",
                branch="feature/ATH-1533",
                occurred_at=self.today_dt,
            ),
        ]

        allowed = summarizer.allowed_today_ticket_ids(activities, self.target_date)

        self.assertIn("ATH-1533", allowed)
        self.assertNotIn("ATH-1578", allowed)

    def test_filter_activities_for_summary_drops_linear_only(self):
        activities = [
            activity(
                source="linear",
                activity_type="ticket_completed",
                ticket_id="ATH-1578",
                title="ATH-1578: Add UTM medium filter",
                occurred_at=self.today_dt,
            ),
            activity(
                source="github",
                activity_type="pr_reviewed",
                ticket_id="ATH-1574",
                title="Reviewed: refresh baseball bake-off",
                occurred_at=self.today_dt,
            ),
        ]

        filtered = summarizer.filter_activities_for_summary(activities, self.target_date)

        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].activity_type, "pr_reviewed")

    def test_enforce_code_grounded_today_removes_linear_only_bullets(self):
        activities = [
            activity(
                source="github",
                activity_type="commit",
                ticket_id="ATH-1533",
                title="Fire purchase pixel",
                occurred_at=self.today_dt,
            ),
            activity(
                source="linear",
                activity_type="ticket_completed",
                ticket_id="ATH-1578",
                title="ATH-1578: Add UTM medium filter",
                occurred_at=self.today_dt,
            ),
        ]
        today = "\n".join(
            [
                "- ATH-1533: Fire Purchase pixel + CAPI on dashboard purchase",
                "- ATH-1578: Add UTM medium filter to DealLead admin",
                "- ATH-1567: Update Landing Page Bake-Off Tests (released)",
            ]
        )

        result = summarizer.enforce_code_grounded_today(today, activities, self.target_date)

        self.assertIn("ATH-1533", result)
        self.assertNotIn("ATH-1578", result)
        self.assertNotIn("ATH-1567", result)

    def test_reviews_count_as_today_work(self):
        activities = [
            activity(
                source="github",
                activity_type="pr_reviewed",
                ticket_id="ATH-1574",
                title="Reviewed: refresh baseball bake-off",
                occurred_at=self.today_dt,
            ),
        ]
        today = "- ATH-1574: Reviewed PR to refresh baseball bake-off utm_id allow-list"

        result = summarizer.enforce_code_grounded_today(today, activities, self.target_date)

        self.assertIn("ATH-1574", result)


class ClaudeCodeSummaryTests(SimpleTestCase):
    def setUp(self):
        self.target_date = date(2026, 6, 15)
        self.tz = ZoneInfo("America/Los_Angeles")
        self.today_dt = datetime(2026, 6, 15, 12, 0, tzinfo=self.tz)
        self.yesterday_dt = datetime(2026, 6, 14, 12, 0, tzinfo=self.tz)

    def chat(self, ticket_id, title, occurred_at, description=""):
        a = activity(source="claude_code", activity_type="chat_session", ticket_id=ticket_id, title=title, occurred_at=occurred_at)
        a.description = description
        a.repository = "delos"
        return a

    def test_chat_ticket_survives_code_grounding(self):
        activities = [self.chat("ATH-2626", "ATH-2626 QA comments", self.today_dt)]
        today = "- ATH-2626: Answered QA comments and moved to Product Review\n- ATH-9999: Unrelated"

        result = summarizer.enforce_code_grounded_today(today, activities, self.target_date)

        self.assertIn("ATH-2626", result)
        self.assertNotIn("ATH-9999", result)

    def test_chat_section_lists_target_date_sessions_only(self):
        activities = [
            self.chat("", "Parent CSV for announcements", self.today_dt, "Asked: pull the CSV\nOutcome: 184 rows"),
            self.chat("ATH-1", "ATH-1 old work", self.yesterday_dt),
        ]

        prompt = summarizer.build_user_prompt(activities, target_date=self.target_date)

        self.assertIn("### Claude Code Sessions", prompt)
        self.assertIn("[delos] Parent CSV for announcements", prompt)
        self.assertIn("  Outcome: 184 rows", prompt)
        self.assertNotIn("ATH-1 old work", prompt)


class ExcludeSourcesTests(SimpleTestCase):
    def test_generate_standup_drops_excluded_sources(self):
        from unittest.mock import patch

        from standups.services import generator

        chat = activity(source="claude_code", activity_type="chat_session", title="chat", occurred_at=datetime(2026, 6, 15, 12, 0, tzinfo=ZoneInfo("UTC")))
        commit = activity(source="github", activity_type="commit", title="commit", occurred_at=chat.occurred_at)

        with patch.object(generator, "get_activities", return_value=[chat, commit]), patch.object(generator, "sync_all") as sync_all:
            result = generator.generate_standup(
                date(2026, 6, 15), raw_only=True, exclude_sources={"claude_code"}
            )

        self.assertEqual(result, [commit])
        self.assertEqual(sync_all.call_args.kwargs["exclude_sources"], {"claude_code"})


class SimplifyCommitTitleTests(SimpleTestCase):
    def test_replacements_match_whole_words_only(self):
        self.assertEqual(
            summarizer.simplify_commit_title("ATH-1: Fix UI bug in quiz build"),
            "fixing user experience issue in quiz build",
        )
