from django.core.management.base import BaseCommand

from integrations.models import IntegrationConfig


class Command(BaseCommand):
    help = "Interactive setup wizard for integrations"

    def add_arguments(self, parser):
        parser.add_argument(
            "integration",
            type=str,
            choices=["github", "linear", "slack"],
            help="Integration to configure.",
        )

    def handle(self, *args, **options):
        integration_type = options["integration"]

        config_obj, created = IntegrationConfig.objects.get_or_create(
            integration_type=integration_type,
            defaults={"is_enabled": False, "config": {}},
        )

        self.stdout.write(f"\n{'=' * 40}")
        self.stdout.write(f"  Setup: {integration_type.upper()}")
        self.stdout.write(f"{'=' * 40}\n")

        if integration_type == "github":
            self.setup_github(config_obj)
        elif integration_type == "linear":
            self.setup_linear(config_obj)
        elif integration_type == "slack":
            self.setup_slack(config_obj)

    def setup_github(self, config_obj):
        self.stdout.write("Make sure GITHUB_TOKEN is set in your .env file.\n")

        config = config_obj.config or {}

        username = input(f"GitHub username [{config.get('username', '')}]: ").strip()
        if username:
            config["username"] = username

        self.stdout.write("\nEnter repos to track (owner/repo format), one per line.")
        self.stdout.write("Leave blank and press Enter to finish (empty = all your repos):\n")

        repos = []
        while True:
            repo = input("  repo: ").strip()
            if not repo:
                break
            repos.append(repo)

        if repos:
            config["repos"] = repos
        elif "repos" not in config:
            config["repos"] = []

        config_obj.config = config
        config_obj.is_enabled = True
        config_obj.save()

        self.stdout.write(self.style.SUCCESS(f"\nGitHub configured: username={config.get('username', 'from env')}, repos={config.get('repos', 'all')}"))

    def setup_linear(self, config_obj):
        self.stdout.write("Make sure LINEAR_API_KEY is set in your .env file.\n")
        self.stdout.write("Linear will automatically fetch issues assigned to the API key owner.\n")

        config_obj.config = config_obj.config or {}
        config_obj.is_enabled = True
        config_obj.save()

        self.stdout.write(self.style.SUCCESS("\nLinear configured and enabled."))

    def setup_slack(self, config_obj):
        self.stdout.write("Make sure SLACK_BOT_TOKEN and SLACK_USER_ID are set in your .env file.\n")

        config = config_obj.config or {}

        user_id = input(f"Slack user ID [{config.get('user_id', '')}]: ").strip()
        if user_id:
            config["user_id"] = user_id

        self.stdout.write("\nEnter Slack channel IDs to monitor, one per line.")
        self.stdout.write("(Find IDs by right-clicking a channel > View channel details > scroll to bottom)")
        self.stdout.write("Leave blank and press Enter to finish:\n")

        channels = []
        while True:
            channel = input("  channel ID: ").strip()
            if not channel:
                break
            channels.append(channel)

        if channels:
            config["channels"] = channels
        elif "channels" not in config:
            config["channels"] = []

        config_obj.config = config
        config_obj.is_enabled = True
        config_obj.save()

        self.stdout.write(self.style.SUCCESS(f"\nSlack configured: user_id={config.get('user_id', 'from env')}, channels={len(config.get('channels', []))} channels"))
