import logging
from datetime import datetime

import httpx
from django.conf import settings

from integrations.models import Activity

from .base import BaseIntegrationService

logger = logging.getLogger(__name__)

LINEAR_API_URL = "https://api.linear.app/graphql"


class LinearService(BaseIntegrationService):
    integration_type = "linear"

    def is_configured(self) -> bool:
        return bool(settings.LINEAR_API_KEY)

    def get_headers(self) -> dict:
        return {
            "Authorization": settings.LINEAR_API_KEY,
            "Content-Type": "application/json",
        }

    def graphql(self, query: str, variables: dict | None = None) -> dict:
        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        response = httpx.post(LINEAR_API_URL, json=payload, headers=self.get_headers(), timeout=30)
        response.raise_for_status()
        data = response.json()

        if "errors" in data:
            logger.error("Linear GraphQL errors: %s", data["errors"])
        return data.get("data", {})

    def sync(self, since: datetime, until: datetime) -> list[Activity]:
        self.load_config()
        if not self.is_configured():
            logger.warning("Linear is not configured, skipping sync")
            return []

        activities = []
        try:
            activities.extend(self.sync_issues(since, until))
            self.mark_synced()
        except Exception:
            logger.exception("Linear sync failed")

        return activities

    def sync_issues(self, since: datetime, until: datetime) -> list[Activity]:
        activities = []
        since_iso = since.isoformat()
        until_iso = until.isoformat()

        query = """
        query($since: DateTime!, $until: DateTime!) {
            viewer {
                assignedIssues(
                    filter: {
                        updatedAt: { gte: $since, lte: $until }
                    }
                    orderBy: updatedAt
                    first: 100
                ) {
                    nodes {
                        id
                        identifier
                        title
                        description
                        url
                        state { name type }
                        createdAt
                        updatedAt
                        completedAt
                        team { name key }
                        history(first: 50) {
                            nodes {
                                id
                                createdAt
                                fromState { name type }
                                toState { name type }
                            }
                        }
                    }
                }
            }
        }
        """

        data = self.graphql(query, {"since": since_iso, "until": until_iso})
        issues = data.get("viewer", {}).get("assignedIssues", {}).get("nodes", [])

        for issue in issues:
            identifier = issue.get("identifier", "")
            title = issue.get("title", "")
            url = issue.get("url", "")
            team_key = issue.get("team", {}).get("key", "")

            # Check for completion
            if issue.get("completedAt"):
                completed_at = issue["completedAt"]
                if since_iso <= completed_at <= until_iso:
                    activity = self.save_activity(
                        source="linear",
                        activity_type="ticket_completed",
                        title=f"{identifier}: {title}",
                        description=issue.get("description", "") or "",
                        url=url,
                        external_id=issue["id"],
                        ticket_id=identifier,
                        status=issue.get("state", {}).get("name", ""),
                        occurred_at=completed_at,
                        metadata={"team": team_key},
                    )
                    if activity:
                        activities.append(activity)

            # Process status changes from history
            for event in issue.get("history", {}).get("nodes", []):
                if not event.get("toState"):
                    continue

                event_time = event["createdAt"]
                if not (since_iso <= event_time <= until_iso):
                    continue

                from_state = event.get("fromState", {}).get("name", "") if event.get("fromState") else ""
                to_state = event.get("toState", {}).get("name", "")

                # Skip if we already recorded a completion for this issue
                if event.get("toState", {}).get("type") == "completed":
                    continue

                activity = self.save_activity(
                    source="linear",
                    activity_type="ticket_status_changed",
                    title=f"{identifier}: {title}",
                    description=f"{from_state} → {to_state}",
                    url=url,
                    external_id=f"{issue['id']}-history-{event['id']}",
                    ticket_id=identifier,
                    status=to_state,
                    previous_status=from_state,
                    occurred_at=event_time,
                    metadata={"team": team_key},
                )
                if activity:
                    activities.append(activity)

        return activities
