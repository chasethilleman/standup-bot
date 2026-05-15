import logging
from datetime import datetime

from django.conf import settings
from github import Auth, Github

from integrations.models import Activity

from .base import BaseIntegrationService

logger = logging.getLogger(__name__)


class GitHubService(BaseIntegrationService):
    integration_type = "github"

    def is_configured(self) -> bool:
        return bool(settings.GITHUB_TOKEN and self.get_username())

    def get_username(self) -> str:
        return self.config.get("username") or settings.GITHUB_USERNAME

    def get_repos(self) -> list[str]:
        return self.config.get("repos", [])

    def get_client(self) -> Github:
        return Github(auth=Auth.Token(settings.GITHUB_TOKEN))

    def sync(self, since: datetime, until: datetime) -> list[Activity]:
        self.load_config()
        if not self.is_configured():
            logger.warning("GitHub is not configured, skipping sync")
            return []

        activities = []
        client = self.get_client()
        username = self.get_username()

        try:
            repos = self.get_repos()
            if repos:
                repo_objects = [client.get_repo(r) for r in repos]
            else:
                user = client.get_user(username)
                repo_objects = list(user.get_repos(type="owner", sort="pushed"))
                repo_objects += list(user.get_repos(type="member", sort="pushed"))

            for repo in repo_objects:
                try:
                    activities.extend(self.sync_commits(repo, username, since, until))
                    activities.extend(self.sync_pull_requests(repo, username, since, until))
                except Exception as e:
                    if "451" in str(e) or "blocked" in str(e).lower():
                        logger.debug("Skipping blocked repo %s", repo.full_name)
                    else:
                        logger.exception("Failed to sync repo %s", repo.full_name)

            self.mark_synced()
        except Exception:
            logger.exception("GitHub sync failed")
        finally:
            client.close()

        return activities

    def sync_commits(self, repo, username: str, since: datetime, until: datetime) -> list[Activity]:
        activities = []
        try:
            commits = repo.get_commits(author=username, since=since, until=until)
            for commit in commits:
                activity = self.save_activity(
                    source="github",
                    activity_type="commit",
                    title=commit.commit.message.split("\n")[0][:500],
                    description=commit.commit.message,
                    url=commit.html_url,
                    external_id=commit.sha,
                    repository=repo.full_name,
                    branch=repo.default_branch,
                    occurred_at=commit.commit.author.date,
                    metadata={"additions": commit.stats.additions, "deletions": commit.stats.deletions},
                )
                if activity:
                    activities.append(activity)
        except Exception as e:
            if "451" in str(e) or "blocked" in str(e).lower():
                raise
            logger.exception("Failed to sync commits for %s", repo.full_name)
        return activities

    def sync_pull_requests(self, repo, username: str, since: datetime, until: datetime) -> list[Activity]:
        activities = []
        try:
            pulls = repo.get_pulls(state="all", sort="updated", direction="desc")
            for pr in pulls:
                if pr.updated_at < since:
                    break
                if pr.updated_at > until:
                    continue

                # PRs authored by user
                if pr.user.login == username:
                    if pr.merged and pr.merged_at and since <= pr.merged_at <= until:
                        activity = self.save_activity(
                            source="github",
                            activity_type="pr_merged",
                            title=pr.title,
                            description=pr.body or "",
                            url=pr.html_url,
                            external_id=str(pr.number),
                            repository=repo.full_name,
                            branch=pr.head.ref,
                            status="merged",
                            occurred_at=pr.merged_at,
                        )
                        if activity:
                            activities.append(activity)
                    elif pr.created_at and since <= pr.created_at <= until:
                        activity = self.save_activity(
                            source="github",
                            activity_type="pr_opened",
                            title=pr.title,
                            description=pr.body or "",
                            url=pr.html_url,
                            external_id=str(pr.number),
                            repository=repo.full_name,
                            branch=pr.head.ref,
                            status=pr.state,
                            occurred_at=pr.created_at,
                        )
                        if activity:
                            activities.append(activity)

                # PRs reviewed by user
                try:
                    reviews = pr.get_reviews()
                    for review in reviews:
                        if review.user.login == username and since <= review.submitted_at <= until:
                            activity = self.save_activity(
                                source="github",
                                activity_type="pr_reviewed",
                                title=f"Reviewed: {pr.title}",
                                description="",
                                url=pr.html_url,
                                external_id=f"{pr.number}-review-{review.id}",
                                repository=repo.full_name,
                                status=review.state,
                                occurred_at=review.submitted_at,
                            )
                            if activity:
                                activities.append(activity)
                except Exception:
                    logger.debug("Failed to fetch reviews for PR #%s", pr.number)

        except Exception as e:
            if "451" in str(e) or "blocked" in str(e).lower():
                raise
            logger.exception("Failed to sync PRs for %s", repo.full_name)
        return activities
