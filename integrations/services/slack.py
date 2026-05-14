import logging
from datetime import datetime

from django.conf import settings
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from integrations.models import Activity

from .base import BaseIntegrationService

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 300


class SlackService(BaseIntegrationService):
    integration_type = "slack"

    def is_configured(self) -> bool:
        return bool(settings.SLACK_BOT_TOKEN and self.get_user_id())

    def get_user_id(self) -> str:
        return self.config.get("user_id") or settings.SLACK_USER_ID

    def get_channels(self) -> list[str]:
        return self.config.get("channels", [])

    def get_client(self) -> WebClient:
        return WebClient(token=settings.SLACK_BOT_TOKEN)

    def sync(self, since: datetime, until: datetime) -> list[Activity]:
        self.load_config()
        if not self.is_configured():
            logger.warning("Slack is not configured, skipping sync")
            return []

        activities = []
        client = self.get_client()
        channel_ids = self.get_channels()
        user_id = self.get_user_id()

        if not channel_ids:
            logger.info("No Slack channels configured, skipping sync")
            return []

        for channel_id in channel_ids:
            try:
                activities.extend(
                    self.sync_channel(client, channel_id, user_id, since, until)
                )
            except Exception:
                logger.exception("Failed to sync Slack channel %s", channel_id)

        if activities:
            self.mark_synced()

        return activities

    def sync_channel(
        self,
        client: WebClient,
        channel_id: str,
        user_id: str,
        since: datetime,
        until: datetime,
    ) -> list[Activity]:
        activities = []

        try:
            # Get channel info for name
            channel_info = client.conversations_info(channel=channel_id)
            channel_name = channel_info["channel"]["name"]
        except SlackApiError:
            logger.warning("Could not get info for channel %s", channel_id)
            channel_name = channel_id

        try:
            result = client.conversations_history(
                channel=channel_id,
                oldest=str(since.timestamp()),
                latest=str(until.timestamp()),
                limit=200,
            )

            for message in result.get("messages", []):
                if message.get("user") != user_id:
                    continue
                if message.get("subtype"):
                    continue  # skip bot messages, joins, etc.

                text = message.get("text", "")
                truncated = text[:MAX_MESSAGE_LENGTH] + "..." if len(text) > MAX_MESSAGE_LENGTH else text

                ts = message["ts"]
                occurred_at = datetime.fromtimestamp(float(ts), tz=since.tzinfo)

                activity = self.save_activity(
                    source="slack",
                    activity_type="message_sent",
                    title=truncated,
                    description="",
                    url="",
                    external_id=ts,
                    channel_name=channel_name,
                    occurred_at=occurred_at,
                    metadata={"channel_id": channel_id},
                )
                if activity:
                    activities.append(activity)

        except SlackApiError:
            logger.exception("Failed to fetch messages from channel %s", channel_id)

        return activities
