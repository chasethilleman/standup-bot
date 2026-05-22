from datetime import datetime

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
            "--extra",
            type=str,
            help="Extra context to supplement the standup (e.g. meeting notes, commit details).",
        )
        parser.add_argument(
            "--extra-file",
            type=str,
            help="Path to a file containing extra context to supplement the standup.",
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
            target_date = datetime.now(generator.LOCAL_TZ).date()

        raw_only = options["raw"]
        skip_sync = options["no_sync"]

        extra_context = options.get("extra") or ""
        extra_file = options.get("extra_file")
        if extra_file:
            try:
                with open(extra_file) as f:
                    file_content = f.read().strip()
                if file_content:
                    extra_context = f"{extra_context}\n{file_content}".strip() if extra_context else file_content
            except FileNotFoundError:
                self.stderr.write(self.style.ERROR(f"Extra file not found: {extra_file}"))
                return

        if options["verbose"]:
            import logging
            logging.basicConfig(level=logging.DEBUG)

        if not skip_sync:
            self.stdout.write("Syncing activities...")

        result = generator.generate_standup(
            target_date=target_date,
            skip_sync=skip_sync,
            raw_only=raw_only,
            extra_context=extra_context,
        )

        if raw_only:
            self.stdout.write(formatter.format_raw_activities(result))
        elif isinstance(result, Standup):
            self.stdout.write(formatter.format_standup(result))
        else:
            self.stdout.write(self.style.ERROR("Failed to generate standup"))
