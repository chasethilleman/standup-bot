import logging
from abc import ABC, abstractmethod
from datetime import datetime

from django.utils import timezone

from integrations.models import Activity, IntegrationConfig

logger = logging.getLogger(__name__)


class BaseIntegrationService(ABC):
    integration_type: str = ""

    def __init__(self):
        self.config_obj = None

    def load_config(self):
        self.config_obj, _ = IntegrationConfig.objects.get_or_create(
            integration_type=self.integration_type,
            defaults={"is_enabled": False, "config": {}},
        )
        return self.config_obj

    @property
    def config(self) -> dict:
        if self.config_obj is None:
            self.load_config()
        return self.config_obj.config or {}

    @abstractmethod
    def is_configured(self) -> bool:
        pass

    @abstractmethod
    def sync(self, since: datetime, until: datetime) -> list[Activity]:
        pass

    def save_activity(self, **kwargs) -> Activity | None:
        try:
            activity, created = Activity.objects.update_or_create(
                source=kwargs["source"],
                external_id=kwargs["external_id"],
                activity_type=kwargs["activity_type"],
                defaults={k: v for k, v in kwargs.items() if k not in ("source", "external_id", "activity_type")},
            )
            if created:
                logger.debug("Created activity: %s", activity)
            return activity
        except Exception:
            logger.exception("Failed to save activity: %s", kwargs.get("title", "unknown"))
            return None

    def mark_synced(self):
        if self.config_obj:
            self.config_obj.last_synced_at = timezone.now()
            self.config_obj.save(update_fields=["last_synced_at"])
