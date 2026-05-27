from datetime import timedelta

import httpx
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from integrations.services.github import GitHubService


class Command(BaseCommand):
    help = "Check GitHub token permissions and branch commit visibility"

    def add_arguments(self, parser):
        parser.add_argument(
            "--repo",
            action="append",
            dest="repos",
            help="Repo to verify in owner/repo format. Repeat for multiple repos.",
        )
        parser.add_argument(
            "--days",
            type=int,
            default=14,
            help="How many days back to check commits (default: 14).",
        )

    def handle(self, *args, **options):
        service = GitHubService()
        service.load_config()

        token = settings.GITHUB_TOKEN
        username = service.get_username()
        configured_repos = service.get_repos()
        repos = options.get("repos") or configured_repos
        since = timezone.now() - timedelta(days=options["days"])
        until = timezone.now()

        if not token:
            self.stdout.write(self.style.ERROR("GITHUB_TOKEN is not set."))
            return
        if not username:
            self.stdout.write(
                self.style.ERROR(
                    "GitHub username is not configured. Set GITHUB_USERNAME or run setup_integration github."
                )
            )
            return
        if not repos:
            self.stdout.write(
                self.style.ERROR(
                    "No repos specified. Pass --repo owner/repo (repeatable) or configure repos in setup_integration."
                )
            )
            return

        self.stdout.write(f"Checking GitHub token for user: {username}")
        self._print_scope_headers(token)
        self.stdout.write(f"Checking {len(repos)} repo(s) for commit visibility...\n")

        client = service.get_client()
        inaccessible_repos = []
        branch_visibility_failures = []
        checked_branch_refs = 0

        try:
            for repo_name in repos:
                self.stdout.write(f"- {repo_name}")
                try:
                    repo = client.get_repo(repo_name)
                    self.stdout.write(
                        self.style.SUCCESS("  repo access: OK")
                    )
                except Exception as exc:
                    self.stdout.write(
                        self.style.ERROR(f"  repo access: FAILED ({exc})")
                    )
                    inaccessible_repos.append(repo_name)
                    continue

                try:
                    default_branch_commits = repo.get_commits(
                        author=username,
                        since=since,
                        until=until,
                    ).totalCount
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  default branch authored commits ({options['days']}d): {default_branch_commits}"
                        )
                    )
                except Exception as exc:
                    self.stdout.write(
                        self.style.ERROR(f"  default branch commit query failed ({exc})")
                    )

                pulls = repo.get_pulls(state="all", sort="updated", direction="desc")
                branch_refs = []
                for pr in pulls:
                    if pr.updated_at < since:
                        break
                    if not pr.user or pr.user.login != username:
                        continue
                    if not pr.head or not pr.head.ref:
                        continue
                    branch_refs.append((pr.number, pr.head.repo or repo, pr.head.ref))

                if not branch_refs:
                    self.stdout.write("  PR branches to verify: none found in range")
                    continue

                self.stdout.write(f"  PR branches to verify: {len(branch_refs)}")
                for pr_number, head_repo, branch_ref in branch_refs:
                    checked_branch_refs += 1
                    try:
                        branch_commit_count = head_repo.get_commits(
                            sha=branch_ref,
                            author=username,
                            since=since,
                            until=until,
                        ).totalCount
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"    PR #{pr_number} `{branch_ref}` ({head_repo.full_name}): "
                                f"{branch_commit_count} commit(s)"
                            )
                        )
                    except Exception as exc:
                        self.stdout.write(
                            self.style.ERROR(
                                f"    PR #{pr_number} `{branch_ref}` ({head_repo.full_name}): FAILED ({exc})"
                            )
                        )
                        branch_visibility_failures.append(
                            f"{head_repo.full_name}:{branch_ref}"
                        )
        finally:
            client.close()

        self.stdout.write("\nPermission check summary:")
        if not inaccessible_repos and not branch_visibility_failures:
            self.stdout.write(
                self.style.SUCCESS(
                    "  PASS: Token can access all checked repos and branch commit queries."
                )
            )
        else:
            if inaccessible_repos:
                self.stdout.write(
                    self.style.WARNING(
                        f"  Repos not accessible with this token: {', '.join(inaccessible_repos)}"
                    )
                )
            if branch_visibility_failures:
                self.stdout.write(
                    self.style.WARNING(
                        "  Branches with failed commit visibility: "
                        + ", ".join(branch_visibility_failures)
                    )
                )

        self.stdout.write(
            f"  Checked branch refs: {checked_branch_refs} in last {options['days']} day(s)."
        )

    def _print_scope_headers(self, token: str) -> None:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            response = httpx.get("https://api.github.com/user", headers=headers, timeout=20.0)
            oauth_scopes = response.headers.get("x-oauth-scopes")
            accepted_scopes = response.headers.get("x-accepted-oauth-scopes")

            if response.status_code >= 400:
                self.stdout.write(
                    self.style.ERROR(
                        f"GitHub auth check failed: HTTP {response.status_code} ({response.text[:200]})"
                    )
                )
                return

            if oauth_scopes:
                self.stdout.write(f"Token scopes: {oauth_scopes}")
                scopes = {scope.strip() for scope in oauth_scopes.split(",") if scope.strip()}
                if "repo" in scopes or "public_repo" in scopes:
                    self.stdout.write(
                        self.style.SUCCESS("Scope check: repo access scope is present.")
                    )
                else:
                    self.stdout.write(
                        self.style.WARNING(
                            "Scope check: `repo` scope not listed. Private repo access may be limited."
                        )
                    )
            else:
                self.stdout.write(
                    "Token scopes header not returned (this is common for fine-grained PATs)."
                )
                self.stdout.write(
                    "Fine-grained tokens are validated by the repo/branch checks below."
                )

            if accepted_scopes:
                self.stdout.write(f"Accepted scopes for /user endpoint: {accepted_scopes}")
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f"Unable to inspect token scope headers ({exc})."))
