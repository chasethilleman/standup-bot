from datetime import date, datetime

from django.core.management.base import BaseCommand

from standups.models import Standup
from standups.services import formatter, generator


class Command(BaseCommand):
    help = "Generate a daily standup report"

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            type=str,
            help="Target date (YYYY-MM-DD). Defaults to today.",
        )
        parser.add_argument(
            "--no-sync",
            action="store_true",
            help="Skip syncing, use cached data.",
        )
        parser.add_argument(
            "--raw",
            action="store_true",
            help="Print raw activities without AI summary.",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Enable verbose output.",
        )

    def handle(self, *args, **options):
        if options["date"]:
            target_date = datetime.strptime(options["date"], "%Y-%m-%d").date()
        else:
            target_date = date.today()

        raw_only = options["raw"]
        skip_sync = options["no_sync"]

        if options["verbose"]:
            import logging
            logging.basicConfig(level=logging.DEBUG)

        if not skip_sync:
            self.stdout.write("Syncing activities...")

        result = generator.generate_standup(
            target_date=target_date,
            skip_sync=skip_sync,
            raw_only=raw_only,
        )

        if raw_only:
            self.stdout.write(formatter.format_raw_activities(result))
        elif isinstance(result, Standup):
            self.stdout.write(formatter.format_standup(result))
        else:
            self.stdout.write(self.style.ERROR("Failed to generate standup"))
