import logging
import os
import re
import subprocess
from datetime import datetime

from integrations.models import Activity

from .base import BaseIntegrationService

logger = logging.getLogger(__name__)


def extract_ticket_from_branch(branch: str) -> str:
    """Extract a ticket ID like ATH-1462 from branch names like lala/ATH-1462."""
    match = re.search(r"\b([A-Z]+-\d+)\b", branch, re.IGNORECASE)
    return match.group(1).upper() if match else ""


def parse_source_ref(source_ref: str) -> str:
    """Convert a git --source ref like 'refs/heads/lala/ATH-1450' to 'lala/ATH-1450'."""
    if not source_ref:
        return ""
    if source_ref.startswith("refs/heads/"):
        return source_ref[len("refs/heads/"):]
    if source_ref.startswith("refs/remotes/"):
        # refs/remotes/origin/lala/ATH-1450 -> lala/ATH-1450
        parts = source_ref.split("/", 3)
        return parts[3] if len(parts) > 3 else source_ref
    if source_ref.startswith("refs/tags/"):
        return source_ref[len("refs/tags/"):]
    return source_ref


class LocalGitService(BaseIntegrationService):
    integration_type = "local_git"

    def is_configured(self) -> bool:
        return bool(self.config.get("repos"))

    def get_repos(self) -> list[dict]:
        return self.config.get("repos", [])

    def get_author(self) -> str:
        return self.config.get("author", "")

    def run_git(self, repo_path: str, args: list[str]) -> str:
        result = subprocess.run(
            ["git", "-C", repo_path] + args,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.stdout.strip()

    def sync(self, since: datetime, until: datetime) -> list[Activity]:
        self.load_config()
        if not self.is_configured():
            logger.warning("Local git is not configured, skipping sync")
            return []

        activities = []
        author = self.get_author()

        for repo_entry in self.get_repos():
            path = os.path.expanduser(repo_entry["path"])
            name = repo_entry.get("name") or os.path.basename(path)

            if not os.path.isdir(os.path.join(path, ".git")):
                logger.warning("Not a git repo: %s", path)
                continue

            try:
                activities.extend(self.sync_commits(path, name, author, since, until))
                activities.extend(self.sync_uncommitted(path, name, until))
            except Exception:
                logger.exception("Failed to sync local repo %s", path)

        if activities:
            self.mark_synced()

        return activities

    def sync_commits(
        self, repo_path: str, repo_name: str, author: str, since: datetime, until: datetime
    ) -> list[Activity]:
        activities = []

        since_str = since.strftime("%Y-%m-%dT%H:%M:%S")
        until_str = until.strftime("%Y-%m-%dT%H:%M:%S")

        author_flag = [f"--author={author}"] if author else []
        # Use --source with %S to get the ref that led to each commit,
        # so commits on different branches get correct attribution.
        # Subject (%s) is last because it may contain pipe characters.
        log_output = self.run_git(repo_path, [
            "log",
            f"--since={since_str}",
            f"--until={until_str}",
            *author_flag,
            "--format=%H|%aI|%S|%s",
            "--source",
            "--all",
        ])

        if not log_output:
            return activities

        fallback_branch = self.run_git(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"]) or "unknown"

        for line in log_output.split("\n"):
            if not line.strip():
                continue
            parts = line.split("|", 3)
            if len(parts) < 4:
                continue

            sha, timestamp, source_ref, message = parts
            branch = parse_source_ref(source_ref) or fallback_branch

            # Check if this commit has been pushed
            pushed = self.run_git(repo_path, [
                "branch", "-r", "--contains", sha
            ])

            activity = self.save_activity(
                source="local_git",
                activity_type="commit" if pushed else "local_commit",
                title=message[:500],
                description=message,
                url="",
                external_id=sha,
                repository=repo_name,
                branch=branch,
                ticket_id=extract_ticket_from_branch(branch),
                occurred_at=timestamp,
                metadata={"pushed": bool(pushed), "repo_path": repo_path},
            )
            if activity:
                activities.append(activity)

        return activities

    def sync_uncommitted(self, repo_path: str, repo_name: str, until: datetime) -> list[Activity]:
        activities = []

        # Staged changes
        staged = self.run_git(repo_path, ["diff", "--cached", "--stat"])
        # Unstaged changes
        unstaged = self.run_git(repo_path, ["diff", "--stat"])
        # Untracked files
        untracked = self.run_git(repo_path, ["ls-files", "--others", "--exclude-standard"])

        branch = self.run_git(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"]) or "unknown"

        changes = []
        if staged:
            file_count = len([l for l in staged.split("\n") if l.strip() and not l.strip().startswith(("changed", "insertion", "deletion"))])
            changes.append(f"{file_count} staged files")
        if unstaged:
            file_count = len([l for l in unstaged.split("\n") if l.strip() and not l.strip().startswith(("changed", "insertion", "deletion"))])
            changes.append(f"{file_count} modified files")
        if untracked:
            file_count = len([l for l in untracked.split("\n") if l.strip()])
            changes.append(f"{file_count} untracked files")

        if not changes:
            return activities

        summary = ", ".join(changes)
        title = f"Uncommitted work on {branch}: {summary}"

        # Build a description with actual file names
        desc_lines = []
        if staged:
            desc_lines.append("Staged:\n" + staged)
        if unstaged:
            desc_lines.append("Modified:\n" + unstaged)
        if untracked:
            files = [l for l in untracked.split("\n") if l.strip()]
            if len(files) > 20:
                desc_lines.append(f"Untracked: {len(files)} files")
            else:
                desc_lines.append("Untracked:\n" + untracked)

        # Use a stable external_id based on repo+branch so it updates rather than duplicates
        activity = self.save_activity(
            source="local_git",
            activity_type="uncommitted_changes",
            title=title,
            description="\n\n".join(desc_lines),
            url="",
            external_id=f"uncommitted-{repo_name}-{branch}",
            repository=repo_name,
            branch=branch,
            ticket_id=extract_ticket_from_branch(branch),
            occurred_at=until,
            metadata={"repo_path": repo_path},
        )
        if activity:
            activities.append(activity)

        return activities
