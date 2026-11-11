import logging
import os
from datetime import date, datetime, timedelta, timezone as _utc_tz
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

from integrations.models import Activity
from integrations.services import ALL_SERVICES
from standups.models import Standup

from . import summarizer

logger = logging.getLogger(__name__)


def detect_local_timezone():
    """Detect the user's local timezone.

    Django sets TZ=UTC in the process environment, so we cannot rely on
    time.timezone / datetime.now().astimezone().  Instead we read the
    system timezone from /etc/localtime (macOS / Linux) or fall back to
    an explicit STANDUP_TIMEZONE Django setting.
    """
    # Explicit override in settings takes priority.
    explicit = getattr(settings, "STANDUP_TIMEZONE", None)
    if explicit:
        try:
            return ZoneInfo(explicit)
        except KeyError:
            logger.warning("Invalid STANDUP_TIMEZONE %r, falling back", explicit)

    # Read from /etc/localtime symlink (macOS / most Linux distros).
    try:
        link = os.readlink("/etc/localtime")
        tz_name = link.split("/zoneinfo/")[-1]
        return ZoneInfo(tz_name)
    except (OSError, KeyError):
        pass

    logger.warning("Could not detect local timezone; defaulting to UTC")
    return _utc_tz.utc


LOCAL_TZ = detect_local_timezone()


def get_sync_range(target_date: date) -> tuple[datetime, datetime]:
    # Monday: cover Friday through Sunday
    if target_date.weekday() == 0:  # Monday
        since_date = target_date - timedelta(days=3)
    else:
        since_date = target_date - timedelta(days=1)

    since = datetime.combine(since_date, datetime.min.time(), tzinfo=LOCAL_TZ)
    until = datetime.combine(
        target_date, datetime.max.time().replace(microsecond=0), tzinfo=LOCAL_TZ
    )
    return since, until


def activity_local_date(occurred_at: datetime) -> date:
    """Return the date of an activity in the user's local timezone."""
    return occurred_at.astimezone(LOCAL_TZ).date()


def sync_all(
    since: datetime,
    until: datetime,
    source: str | None = None,
    exclude_sources: set[str] | frozenset[str] = frozenset(),
) -> int:
    total = 0
    for service_cls in ALL_SERVICES:
        if source and service_cls.integration_type != source:
            continue
        if service_cls.integration_type in exclude_sources:
            continue

        service = service_cls()
        service.load_config()

        if not service.config_obj.is_enabled or not service.is_configured():
            continue

        try:
            activities = service.sync(since, until)
            total += len(activities)
        except Exception:
            logger.exception("Failed to sync %s", service.integration_type)

    return total


def get_activities(since: datetime, until: datetime) -> list[Activity]:
    return list(
        Activity.objects.filter(
            occurred_at__gte=since,
            occurred_at__lte=until,
        ).order_by("occurred_at")
    )


def assigned_ticket_ids(activities) -> set[str]:
    return {
        activity.ticket_id.upper()
        for activity in activities
        if activity.source == "linear" and activity.ticket_id
    }


def filter_my_work(activities: list[Activity]) -> list[Activity]:
    """Keep only activities representing the user's own work."""
    assigned_tickets = assigned_ticket_ids(activities)
    if not assigned_tickets:
        return activities

    github_own_work = {
        "commit",
        "local_commit",
        "pr_opened",
        "pr_merged",
        "pr_ready",
    }
    local_own_work = {"commit", "local_commit", "uncommitted_changes"}

    filtered = []
    for activity in activities:
        if activity.source in {"linear", "slack", "claude_code"}:
            filtered.append(activity)
            continue

        if activity.source == "github" and activity.activity_type == "pr_reviewed":
            filtered.append(activity)
            continue

        ticket_id = (activity.ticket_id or "").upper()
        if ticket_id and ticket_id in assigned_tickets:
            filtered.append(activity)
            continue

        if activity.source == "github" and activity.activity_type in github_own_work:
            if not ticket_id:
                filtered.append(activity)
            continue

        if activity.source == "local_git" and activity.activity_type in local_own_work:
            if not ticket_id:
                filtered.append(activity)

    return filtered


def generate_standup(
    target_date: date,
    skip_sync: bool = False,
    raw_only: bool = False,
    extra_context: str = "",
    exclude_sources: set[str] | frozenset[str] = frozenset(),
) -> Standup | list[Activity]:
    since, until = get_sync_range(target_date)

    if not skip_sync:
        sync_all(since, until, exclude_sources=exclude_sources)

    activities = [
        activity
        for activity in filter_my_work(get_activities(since, until))
        if activity.source not in exclude_sources
    ]

    if raw_only:
        return activities

    result = summarizer.summarize(activities, extra_context=extra_context, target_date=target_date)

    standup = Standup.objects.create(
        date=target_date,
        today=result["today"],
        tomorrow=result["tomorrow"],
        raw_ai_response=result["raw_response"],
        prompt_tokens=result["prompt_tokens"],
        completion_tokens=result["completion_tokens"],
    )
    standup.activities.set(activities)

    return standup
