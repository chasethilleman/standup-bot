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


def sync_all(since: datetime, until: datetime, source: str | None = None) -> int:
    total = 0
    for service_cls in ALL_SERVICES:
        if source and service_cls.integration_type != source:
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


def generate_standup(
    target_date: date,
    skip_sync: bool = False,
    raw_only: bool = False,
    extra_context: str = "",
) -> Standup | list[Activity]:
    since, until = get_sync_range(target_date)

    if not skip_sync:
        sync_all(since, until)

    activities = get_activities(since, until)

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
