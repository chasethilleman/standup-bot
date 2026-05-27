import logging
from datetime import datetime

from django.conf import settings
from github import Auth, Github

from integrations.models import Activity

from .base import BaseIntegrationService
from .local_git import extract_ticket_from_branch

logger = logging.getLogger(__name__)


def extract_ticket_from_pr(pr) -> str:
    """Extract ticket ID from a PR's branch name or title."""
    import re

    ticket_id = extract_ticket_from_branch(pr.head.ref) if pr.head and pr.head.ref else ""
    if not ticket_id:
        match = re.search(r"\b([A-Z]+-\d+)\b", pr.title or "", re.IGNORECASE)
        ticket_id = match.group(1).upper() if match else ""
    return ticket_id


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

    def discover_repos(self, client: Github, username: str) -> list:
        """Discover repos the authenticated user can access."""
        repo_objects = []

        # Use authenticated user endpoint first so collaborator/private repos
        # across other owners are included.
        try:
            authed_user = client.get_user()
            repo_objects += list(
                authed_user.get_repos(
                    visibility="all",
                    affiliation="owner,collaborator,organization_member",
                    sort="pushed",
                )
            )
        except Exception:
            logger.debug("Failed to fetch accessible repos via authenticated user endpoint")

        # Fall back to explicit username lookups for compatibility.
        user = client.get_user(username)
        repo_objects += list(user.get_repos(type="owner", sort="pushed"))
        repo_objects += list(user.get_repos(type="member", sort="pushed"))

        try:
            for org in user.get_orgs():
                try:
                    repo_objects += list(org.get_repos(type="all", sort="pushed"))
                except Exception:
                    logger.debug("Failed to fetch repos for org %s", org.login)
        except Exception:
            logger.debug("Failed to fetch orgs for %s", username)

        # Deduplicate by full_name
        seen = set()
        unique = []
        for repo in repo_objects:
            if repo.full_name not in seen:
                seen.add(repo.full_name)
                unique.append(repo)
        return unique

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
                repo_objects = self.discover_repos(client, username)

            for repo in repo_objects:
                try:
                    activities.extend(self.sync_commits(repo, username, since, until))
                    activities.extend(self.sync_pull_requests(repo, username, since, until))
                except Exception as e:
                    if "451" in str(e) or "blocked" in str(e).lower():
                        logger.debug("Skipping blocked repo %s", repo.full_name)
                    else:
                        logger.exception("Failed to sync repo %s", repo.full_name)

            # Reviews can happen in repos outside configured repo list.
            # Pull them globally so standups always include reviewed PRs.
            activities.extend(self.sync_reviewed_prs_global(client, username, since, until))
            activities.extend(self.sync_ready_tagged_prs_global(client, username, since, until))

            self.mark_synced()
        except Exception:
            logger.exception("GitHub sync failed")
        finally:
            client.close()

        return activities

    def sync_commits(self, repo, username: str, since: datetime, until: datetime) -> list[Activity]:
        activities = []
        seen_shas = set()
        try:
            commits = repo.get_commits(author=username, since=since, until=until)
            for commit in commits:
                if commit.sha in seen_shas:
                    continue
                seen_shas.add(commit.sha)
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

            # GitHub's commits endpoint defaults to the repo's default branch.
            # Collect authored commits from your active PR head branches so
            # branch-only work is included before merge.
            pulls = repo.get_pulls(state="all", sort="updated", direction="desc")
            for pr in pulls:
                if pr.updated_at < since:
                    break
                if not pr.user or pr.user.login != username:
                    continue
                if not pr.head or not pr.head.ref:
                    continue

                head_repo = pr.head.repo or repo
                branch_ref = pr.head.ref

                try:
                    branch_commits = head_repo.get_commits(
                        sha=branch_ref,
                        since=since,
                        until=until,
                    )
                    for commit in branch_commits:
                        if commit.sha in seen_shas:
                            continue
                        seen_shas.add(commit.sha)
                        activity = self.save_activity(
                            source="github",
                            activity_type="commit",
                            title=commit.commit.message.split("\n")[0][:500],
                            description=commit.commit.message,
                            url=commit.html_url,
                            external_id=commit.sha,
                            repository=head_repo.full_name,
                            branch=branch_ref,
                            occurred_at=commit.commit.author.date,
                            metadata={
                                "additions": commit.stats.additions,
                                "deletions": commit.stats.deletions,
                                "from_pr_branch": True,
                                "pr_number": pr.number,
                            },
                        )
                        if activity:
                            activities.append(activity)
                except Exception:
                    logger.debug(
                        "Failed to fetch branch commits for PR #%s in %s",
                        pr.number,
                        repo.full_name,
                    )
        except Exception as e:
            if "451" in str(e) or "blocked" in str(e).lower():
                raise
            logger.exception("Failed to sync commits for %s", repo.full_name)
        return activities

    def get_pr_commit_highlights(self, pr, max_highlights: int = 4) -> list[str]:
        """Fetch concise commit message highlights for a PR."""
        highlights = []
        seen = set()
        try:
            for commit in pr.get_commits():
                message = (commit.commit.message or "").split("\n")[0].strip()
                if not message:
                    continue
                lower = message.lower()
                if lower.startswith("merge pull request"):
                    continue
                key = message.lower()
                if key in seen:
                    continue
                seen.add(key)
                highlights.append(message[:200])
                if len(highlights) >= max_highlights:
                    break
        except Exception:
            logger.debug("Failed to fetch commit highlights for PR #%s", pr.number)
        return highlights

    def sync_pull_requests(self, repo, username: str, since: datetime, until: datetime) -> list[Activity]:
        activities = []
        try:
            pulls = repo.get_pulls(state="all", sort="updated", direction="desc")
            for pr in pulls:
                if pr.updated_at < since:
                    break
                if pr.updated_at > until:
                    continue

                pr_commit_highlights = self.get_pr_commit_highlights(pr)

                # PRs authored by user
                if pr.user.login == username:
                    labels = [label.name for label in pr.labels]
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
                            ticket_id=extract_ticket_from_pr(pr),
                            status="merged",
                            occurred_at=pr.merged_at,
                            metadata={
                                "labels": labels,
                                "number": pr.number,
                                "commit_highlights": pr_commit_highlights,
                            },
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
                            ticket_id=extract_ticket_from_pr(pr),
                            status=pr.state,
                            occurred_at=pr.created_at,
                            metadata={
                                "labels": labels,
                                "number": pr.number,
                                "commit_highlights": pr_commit_highlights,
                            },
                        )
                        if activity:
                            activities.append(activity)

                # PRs reviewed by user
                try:
                    labels = [label.name for label in pr.labels]
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
                                branch=pr.head.ref,
                                ticket_id=extract_ticket_from_pr(pr),
                                status=review.state,
                                occurred_at=review.submitted_at,
                                metadata={
                                    "labels": labels,
                                    "number": pr.number,
                                    "review_state": review.state,
                                    "commit_highlights": pr_commit_highlights,
                                },
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

    def sync_reviewed_prs_global(self, client: Github, username: str, since: datetime, until: datetime) -> list[Activity]:
        activities = []
        try:
            query = (
                f"is:pr reviewed-by:{username} "
                f"updated:{since.date().isoformat()}..{until.date().isoformat()}"
            )
            prs = client.search_issues(query=query, sort="updated", order="desc")

            for item in prs:
                # Search API returns issues; skip anything that isn't a PR.
                if not item.pull_request:
                    continue

                try:
                    repo_name, number_str = item.repository.full_name, str(item.number)
                    repo = client.get_repo(repo_name)
                    pr = repo.get_pull(int(number_str))
                    labels = [label.name for label in pr.labels]
                    pr_commit_highlights = self.get_pr_commit_highlights(pr)

                    reviews = pr.get_reviews()
                    for review in reviews:
                        if not review.user or review.user.login != username:
                            continue
                        if not review.submitted_at:
                            continue
                        if not (since <= review.submitted_at <= until):
                            continue

                        activity = self.save_activity(
                            source="github",
                            activity_type="pr_reviewed",
                            title=f"Reviewed: {pr.title}",
                            description=pr.body or "",
                            url=pr.html_url,
                            external_id=f"{pr.number}-review-{review.id}",
                            repository=repo.full_name,
                            branch=pr.head.ref,
                            ticket_id=extract_ticket_from_pr(pr),
                            status=review.state,
                            occurred_at=review.submitted_at,
                            metadata={
                                "labels": labels,
                                "number": pr.number,
                                "review_state": review.state,
                                "commit_highlights": pr_commit_highlights,
                            },
                        )
                        if activity:
                            activities.append(activity)
                except Exception:
                    logger.debug("Failed to process global reviewed PR #%s", item.number)
        except Exception:
            logger.exception("Failed to sync globally reviewed PRs")

        return activities

    def find_ready_label_applied_at(self, pr, ready_labels: set[str]) -> datetime | None:
        """Find when a ready label was applied using issue events."""
        try:
            latest = None
            for event in pr.get_issue_events():
                if (
                    event.event == "labeled"
                    and event.label
                    and event.label.name.strip().lower() in ready_labels
                ):
                    if latest is None or event.created_at > latest:
                        latest = event.created_at
            return latest
        except Exception:
            logger.debug("Failed to fetch label events for PR #%s", pr.number)
            return None

    def sync_ready_tagged_prs_global(self, client: Github, username: str, since: datetime, until: datetime) -> list[Activity]:
        activities = []
        ready_label_names = {"ready to merge", "ready for review", "ready to review"}
        try:
            date_range = f"{since.date().isoformat()}..{until.date().isoformat()}"
            queries = [
                f"is:pr author:{username} label:\"ready to merge\" updated:{date_range}",
                f"is:pr author:{username} label:\"ready for review\" updated:{date_range}",
                f"is:pr author:{username} label:\"ready to review\" updated:{date_range}",
            ]
            seen_numbers = set()

            for query in queries:
                prs = client.search_issues(query=query, sort="updated", order="desc")
                for item in prs:
                    if not item.pull_request:
                        continue
                    if item.number in seen_numbers:
                        continue
                    seen_numbers.add(item.number)

                    try:
                        repo = client.get_repo(item.repository.full_name)
                        pr = repo.get_pull(int(item.number))
                        labels = [label.name for label in pr.labels]
                        pr_commit_highlights = self.get_pr_commit_highlights(pr)
                        normalized_labels = {label.strip().lower() for label in labels}
                        ready_labels = sorted(
                            l for l in normalized_labels if l in ready_label_names
                        )
                        if not ready_labels:
                            continue

                        # Use when the ready label was applied, not pr.updated_at
                        # which drifts on every comment/CI run.
                        label_applied_at = self.find_ready_label_applied_at(pr, ready_label_names)
                        occurred_at = label_applied_at or pr.created_at

                        activity = self.save_activity(
                            source="github",
                            activity_type="pr_ready",
                            title=pr.title,
                            description=pr.body or "",
                            url=pr.html_url,
                            external_id=f"{pr.number}-ready",
                            repository=repo.full_name,
                            branch=pr.head.ref,
                            ticket_id=extract_ticket_from_pr(pr),
                            status="ready",
                            occurred_at=occurred_at,
                            metadata={
                                "labels": labels,
                                "number": pr.number,
                                "commit_highlights": pr_commit_highlights,
                            },
                        )
                        if activity:
                            activities.append(activity)
                    except Exception:
                        logger.debug("Failed to process ready-tagged PR #%s", item.number)
        except Exception:
            logger.exception("Failed to sync globally ready-tagged PRs")

        return activities
