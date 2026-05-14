import logging
from datetime import datetime, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from integrations.services import ALL_SERVICES

logger = logging.getLogger(__name__)

SERVICE_MAP = {svc.integration_type: svc for svc in ALL_SERVICES}


class Command(BaseCommand):
    help = "Sync activities from configured integrations"

    def add_arguments(self, parser):
        parser.add_argument(
            "--since",
            type=str,
            help="Start date (YYYY-MM-DD). Defaults to yesterday.",
        )
        parser.add_argument(
            "--until",
            type=str,
            help="End date (YYYY-MM-DD). Defaults to now.",
        )
        parser.add_argument(
            "--source",
            type=str,
            choices=list(SERVICE_MAP.keys()),
            help="Sync only a specific integration.",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Enable verbose logging.",
        )

    def handle(self, *args, **options):
        if options["verbose"]:
            logging.basicConfig(level=logging.DEBUG)

        now = timezone.now()
        if options["since"]:
            since = timezone.make_aware(datetime.strptime(options["since"], "%Y-%m-%d"))
        else:
            since = now - timedelta(days=1)
            since = since.replace(hour=0, minute=0, second=0, microsecond=0)

        if options["until"]:
            until = timezone.make_aware(
                datetime.strptime(options["until"], "%Y-%m-%d").replace(
                    hour=23, minute=59, second=59
                )
            )
        else:
            until = now

        self.stdout.write(f"Syncing activities from {since.date()} to {until.date()}...")

        if options["source"]:
            services = [SERVICE_MAP[options["source"]]]
        else:
            services = ALL_SERVICES

        total = 0
        for service_cls in services:
            service = service_cls()
            service.load_config()

            if not service.config_obj.is_enabled:
                self.stdout.write(f"  {service.integration_type}: disabled, skipping")
                continue

            if not service.is_configured():
                self.stdout.write(f"  {service.integration_type}: not configured, skipping")
                continue

            try:
                self.stdout.write(f"  {service.integration_type}: syncing...")
                activities = service.sync(since, until)
                count = len(activities)
                total += count
                self.stdout.write(
                    self.style.SUCCESS(f"  {service.integration_type}: {count} activities synced")
                )
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f"  {service.integration_type}: failed - {e}")
                )

        self.stdout.write(self.style.SUCCESS(f"\nTotal: {total} activities synced"))
