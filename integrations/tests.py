from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

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
