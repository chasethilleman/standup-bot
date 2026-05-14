import logging
import re

import anthropic
from django.conf import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a concise standup report generator. Given a list of work activities from \
GitHub, Linear, and Slack, produce a daily standup summary.

Rules:
- Group related work items together (e.g., multiple commits on the same PR)
- Use ticket/PR identifiers when available (e.g., "ENG-123", "PR #45")
- "Yesterday" section: use past tense for completed work
- "Today" section: use future tense for planned work. Infer from in-progress items and context.
- "Blockers" section: flag anything that looks blocked or stalled. If none, write "None"
- Do NOT directly quote Slack messages — summarize the topics discussed instead
- Keep each bullet point to one line
- Use markdown bullet points (-)

Format your response EXACTLY like this:
## Yesterday
- Item 1
- Item 2

## Today
- Item 1
- Item 2

## Blockers
- None
"""


def build_user_prompt(activities) -> str:
    grouped = {}
    for activity in activities:
        key = f"{activity.source}_{activity.activity_type}"
        if key not in grouped:
            grouped[key] = []
        grouped[key].append(activity)

    sections = []

    # GitHub commits
    commits = grouped.get("github_commit", [])
    if commits:
        lines = ["### GitHub Commits"]
        for a in commits:
            repo = a.repository.split("/")[-1] if a.repository else ""
            lines.append(f"- [{repo}] {a.title}")
        sections.append("\n".join(lines))

    # GitHub PRs
    for pr_type in ["github_pr_opened", "github_pr_merged", "github_pr_reviewed"]:
        prs = grouped.get(pr_type, [])
        if prs:
            label = pr_type.replace("github_", "").replace("_", " ").title()
            lines = [f"### GitHub {label}"]
            for a in prs:
                repo = a.repository.split("/")[-1] if a.repository else ""
                lines.append(f"- [{repo}] {a.title} ({a.url})")
            sections.append("\n".join(lines))

    # Linear tickets
    for ticket_type in ["linear_ticket_completed", "linear_ticket_status_changed"]:
        tickets = grouped.get(ticket_type, [])
        if tickets:
            label = "Completed" if "completed" in ticket_type else "Status Changes"
            lines = [f"### Linear Tickets — {label}"]
            for a in tickets:
                desc = f" ({a.description})" if a.description else ""
                lines.append(f"- {a.title}{desc}")
            sections.append("\n".join(lines))

    # Slack messages
    messages = grouped.get("slack_message_sent", [])
    if messages:
        lines = ["### Slack Context"]
        by_channel = {}
        for a in messages:
            ch = a.channel_name or "unknown"
            if ch not in by_channel:
                by_channel[ch] = []
            by_channel[ch].append(a.title)
        for channel, msgs in by_channel.items():
            lines.append(f"- #{channel}: {len(msgs)} messages")
            for msg in msgs[:5]:
                lines.append(f"  - {msg}")
        sections.append("\n".join(lines))

    if not sections:
        return "No activities found for this period."

    return "\n\n".join(sections)


def summarize(activities) -> dict:
    if not settings.ANTHROPIC_API_KEY:
        return {
            "yesterday": "Claude API key not configured",
            "today": "",
            "blockers": "",
            "raw_response": "",
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }

    user_prompt = build_user_prompt(activities)
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    with client.messages.stream(
        model=settings.ANTHROPIC_MODEL,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        response = stream.get_final_message()

    raw_text = response.content[0].text
    sections = parse_sections(raw_text)

    return {
        "yesterday": sections.get("yesterday", ""),
        "today": sections.get("today", ""),
        "blockers": sections.get("blockers", ""),
        "raw_response": raw_text,
        "prompt_tokens": response.usage.input_tokens,
        "completion_tokens": response.usage.output_tokens,
    }


def parse_sections(text: str) -> dict:
    sections = {}
    current_section = None
    current_lines = []

    for line in text.split("\n"):
        header_match = re.match(r"^##\s+(.+)", line.strip())
        if header_match:
            if current_section:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = header_match.group(1).lower().strip()
            current_lines = []
        elif current_section is not None:
            current_lines.append(line)

    if current_section:
        sections[current_section] = "\n".join(current_lines).strip()

    return sections
