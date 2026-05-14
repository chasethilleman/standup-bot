# Standup Bot

A Django-based CLI tool that automatically tracks your work across GitHub, Linear, and Slack, then uses Claude AI to generate concise daily standup summaries.

## How It Works

1. **Syncs** your recent activity from configured integrations (commits, PRs, ticket updates, Slack messages)
2. **Summarizes** everything with Claude AI into a Yesterday/Today/Blockers format
3. **Outputs** a formatted standup report in your terminal

```
══════════════════════════════════════════════════
  Daily Standup — 2026-05-14
══════════════════════════════════════════════════

## Yesterday
- Merged PR #42 for user auth refactor (ENG-301)
- Completed ENG-305: API rate limiting implementation
- Reviewed PR #38: database migration fixes

## Today
- Continue work on ENG-310: webhook integration
- Address review comments on PR #43

## Blockers
- None

══════════════════════════════════════════════════
```

## Setup

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

### Install

```bash
git clone https://github.com/lala010addict/standup-bot.git
cd standup-bot
uv sync
```

### Configure

Copy the example env file and fill in your tokens:

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `GITHUB_TOKEN` | Personal access token with `repo` scope |
| `GITHUB_USERNAME` | Your GitHub username |
| `LINEAR_API_KEY` | Personal API key from Linear Settings > API |
| `SLACK_BOT_TOKEN` | Bot User OAuth Token (`xoxb-...`) |
| `SLACK_USER_ID` | Your Slack member ID |
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `ANTHROPIC_MODEL` | Model to use (default: `claude-opus-4-6`) |

### Initialize the database

```bash
uv run python manage.py migrate
```

### Configure integrations

Run the interactive setup wizard for each integration you want to use:

```bash
uv run python manage.py setup_integration github
uv run python manage.py setup_integration linear
uv run python manage.py setup_integration slack
```

## Usage

### Generate a standup

```bash
uv run python manage.py standup
```

This syncs your latest activity and generates an AI summary.

### Options

```bash
# Standup for a specific date
uv run python manage.py standup --date 2026-05-13

# Skip syncing, use cached data
uv run python manage.py standup --no-sync

# Show raw activities without AI summary
uv run python manage.py standup --raw

# Combine flags
uv run python manage.py standup --raw --no-sync
```

Monday standups automatically cover Friday through Sunday.

### Sync only

```bash
# Sync all integrations
uv run python manage.py sync

# Sync a specific integration
uv run python manage.py sync --source github

# Custom date range
uv run python manage.py sync --since 2026-05-10 --until 2026-05-14
```

## Integrations

| Integration | What it tracks |
|---|---|
| **GitHub** | Commits, PRs opened/merged, PR reviews |
| **Linear** | Ticket completions, status changes |
| **Slack** | Messages in configured channels (used as context, not quoted directly) |

Each integration fails gracefully — if one is misconfigured or errors out, the others still work.

## Project Structure

```
standup-bot/
├── standupbot/          # Django project settings
├── integrations/        # App: external API integrations
│   ├── models.py        # IntegrationConfig, Activity
│   ├── services/        # GitHub, Linear, Slack API clients
│   └── management/commands/
│       ├── sync.py              # Sync activities
│       └── setup_integration.py # Interactive setup wizard
└── standups/            # App: standup generation
    ├── models.py        # Standup
    ├── services/        # Summarizer, generator, formatter
    └── management/commands/
        └── standup.py   # Main CLI command
```
