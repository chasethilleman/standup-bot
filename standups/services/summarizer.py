import logging
import re
import time

import anthropic
from django.conf import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a concise standup report generator. Given a list of work activities, \
produce a daily standup summary.

Rules:
- The report has exactly two sections: "Today" (what was done today) and "Tomorrow" (planned work)
- Group related work together (e.g., multiple commits on the same ticket/PR = one bullet)
- Use ticket/PR identifiers when available (e.g., "ENG-123", "PR #45")
- "Today" section: past tense. Include ALL meaningful work completed or progressed today.
- "Tomorrow" section: future tense. Infer from in-progress items, uncommitted changes, and context.
- If an item is still in progress, include the progress made under "Today" and the continuation under "Tomorrow".
- Explicitly include PR reviews in "Today" when present.
- Any ticket moved to "In Progress" today MUST appear in both sections.
- For in-progress tickets, include concrete progress details from related commits, PR activity, or uncommitted changes when available.
- Prefer plain-English functionality summaries over file names, counts, or low-level code terminology.
- Do NOT directly quote Slack messages — summarize the topics discussed instead
- Keep each bullet point to one line
- Use markdown bullet points (-)

Format your response EXACTLY like this:
## Today
- Item 1
- Item 2

## Tomorrow
- Item 1
- Item 2
"""


MAX_RETRIES = 4
INITIAL_RETRY_DELAY_SECONDS = 1.5


def build_user_prompt(activities) -> str:
    grouped = {}
    for activity in activities:
        key = f"{activity.source}_{activity.activity_type}"
        if key not in grouped:
            grouped[key] = []
        grouped[key].append(activity)

    sections = []

    # Local git commits
    for commit_type in ["local_git_commit", "local_git_local_commit"]:
        local_commits = grouped.get(commit_type, [])
        if local_commits:
            label = "Local Commits (unpushed)" if "local_commit" in commit_type else "Local Commits (pushed)"
            lines = [f"### {label}"]
            for a in local_commits:
                lines.append(f"- [{a.repository}:{a.branch}] {a.title}")
            sections.append("\n".join(lines))

    # Local uncommitted changes
    uncommitted = grouped.get("local_git_uncommitted_changes", [])
    if uncommitted:
        lines = ["### Work In Progress (uncommitted changes)"]
        for a in uncommitted:
            lines.append(f"- [{a.repository}:{a.branch}] {a.title}")
        sections.append("\n".join(lines))

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

    # Cross-source context by ticket
    ticket_context = build_ticket_context_section(activities)
    if ticket_context:
        sections.append(ticket_context)

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
    response = request_summary_with_retries(client, user_prompt)

    raw_text = response.content[0].text
    sections = parse_sections(raw_text)
    today, tomorrow = enforce_in_progress_ticket_overlap(
        sections.get("today", ""),
        sections.get("tomorrow", ""),
        activities,
    )
    today, tomorrow = enforce_local_wip_overlap(today, tomorrow, activities)
    tomorrow = enforce_grounded_tomorrow(tomorrow, activities)

    return {
        "today": today,
        "tomorrow": tomorrow,
        "raw_response": raw_text,
        "prompt_tokens": response.usage.input_tokens,
        "completion_tokens": response.usage.output_tokens,
    }


def request_summary_with_retries(client, user_prompt):
    delay = INITIAL_RETRY_DELAY_SECONDS
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with client.messages.stream(
                model=settings.ANTHROPIC_MODEL,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            ) as stream:
                return stream.get_final_message()
        except Exception as exc:
            last_error = exc
            if not is_retryable_anthropic_error(exc) or attempt == MAX_RETRIES:
                raise

            logger.warning(
                "Anthropic request failed (attempt %s/%s): %s. Retrying in %.1fs.",
                attempt,
                MAX_RETRIES,
                exc,
                delay,
            )
            time.sleep(delay)
            delay *= 2

    # Defensive fallback; loop always returns or raises.
    raise last_error


def is_retryable_anthropic_error(exc: Exception) -> bool:
    # Covers transient overloads and network blips returned by Anthropic SDK.
    name = exc.__class__.__name__
    text = str(exc).lower()
    retryable_names = {"APIConnectionError", "APITimeoutError", "RateLimitError"}
    retryable_markers = {"overloaded_error", "overloaded", "rate_limit", "timeout", "temporar"}

    if name in retryable_names:
        return True
    return any(marker in text for marker in retryable_markers)


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


def build_ticket_context_section(activities) -> str:
    ticket_ids = sorted(
        {
            a.ticket_id
            for a in activities
            if a.ticket_id
        }
    )
    if not ticket_ids:
        return ""

    lines = ["### Cross-source Ticket Context"]
    for ticket_id in ticket_ids:
        related = related_activities_for_ticket(ticket_id, activities)
        if not related:
            continue

        lines.append(f"- {ticket_id}:")
        for activity in related[:3]:
            lines.append(f"  - {format_activity_context_line(activity)}")

    return "\n".join(lines) if len(lines) > 1 else ""


def related_activities_for_ticket(ticket_id: str, activities):
    id_pattern = re.compile(rf"\b{re.escape(ticket_id)}\b", re.IGNORECASE)
    related = []

    for activity in activities:
        if activity.source == "linear":
            continue

        haystack = " ".join(
            [
                activity.title or "",
                activity.description or "",
                activity.branch or "",
                activity.ticket_id or "",
            ]
        )
        if id_pattern.search(haystack):
            related.append(activity)

    return sorted(related, key=lambda a: a.occurred_at, reverse=True)


def format_activity_context_line(activity) -> str:
    source = f"{activity.source}/{activity.activity_type}".replace("_", " ")
    title = (activity.title or "").replace("\n", " ").strip()
    if len(title) > 120:
        title = f"{title[:117]}..."
    return f"{source}: {title}"


def enforce_in_progress_ticket_overlap(today: str, tomorrow: str, activities) -> tuple[str, str]:
    in_progress = {}
    for activity in activities:
        if (
            activity.source == "linear"
            and activity.activity_type == "ticket_status_changed"
            and activity.ticket_id
            and "progress" in (activity.status or "").lower()
        ):
            existing = in_progress.get(activity.ticket_id)
            if existing is None or activity.occurred_at > existing.occurred_at:
                in_progress[activity.ticket_id] = activity

    if not in_progress:
        return today, tomorrow

    today_lines = [line for line in today.splitlines() if line.strip()]
    tomorrow_lines = [line for line in tomorrow.splitlines() if line.strip()]
    today_blob = today.lower()
    tomorrow_blob = tomorrow.lower()

    for ticket_id, linear_activity in in_progress.items():
        ticket_key = ticket_id.lower()
        has_today = ticket_key in today_blob
        has_tomorrow = ticket_key in tomorrow_blob

        detail = build_ticket_progress_detail(ticket_id, linear_activity, activities)

        if not has_today:
            today_lines.append(f"- {detail['today']}")
        if not has_tomorrow:
            tomorrow_lines.append(f"- {detail['tomorrow']}")

    return "\n".join(today_lines).strip(), "\n".join(tomorrow_lines).strip()


def build_ticket_progress_detail(ticket_id: str, linear_activity, activities) -> dict[str, str]:
    title = (linear_activity.title or ticket_id).strip()
    transition = (linear_activity.description or "").strip()
    related = related_activities_for_ticket(ticket_id, activities)

    if related:
        sample = "; ".join(format_activity_context_line(a) for a in related[:2])
        today = f"{title}: Progressed this work today ({sample})"
        tomorrow = f"Continue {title}: carry forward remaining work from today's progress"
        return {"today": today, "tomorrow": tomorrow}

    if transition:
        today = f"{title}: Moved to In Progress and advanced the work ({transition})"
    else:
        today = f"{title}: Moved to In Progress and progressed this work today"

    tomorrow = f"Continue {title}: complete remaining in-progress tasks"
    return {"today": today, "tomorrow": tomorrow}


def enforce_local_wip_overlap(today: str, tomorrow: str, activities) -> tuple[str, str]:
    local_wip = [
        a
        for a in activities
        if a.source == "local_git" and a.activity_type == "uncommitted_changes"
    ]
    if not local_wip:
        return today, tomorrow

    today_lines = [line for line in today.splitlines() if line.strip()]
    tomorrow_lines = [line for line in tomorrow.splitlines() if line.strip()]
    today_blob = today.lower()
    tomorrow_blob = tomorrow.lower()

    for wip in sorted(local_wip, key=lambda a: a.occurred_at, reverse=True):
        topic = infer_local_wip_topic(wip)
        markers = [m for m in [wip.branch, topic, wip.title] if m]
        marker_hits_today = any(m.lower() in today_blob for m in markers)
        marker_hits_tomorrow = any(m.lower() in tomorrow_blob for m in markers)

        detail = build_local_wip_detail(wip, activities)

        if not marker_hits_today:
            today_lines.append(f"- {detail['today']}")
        if not marker_hits_tomorrow:
            tomorrow_lines.append(f"- {detail['tomorrow']}")

    return "\n".join(today_lines).strip(), "\n".join(tomorrow_lines).strip()


def infer_local_wip_topic(activity) -> str:
    haystack = " ".join(
        [
            activity.title or "",
            activity.branch or "",
            activity.description or "",
        ]
    ).lower()
    if "guardian" in haystack and "reschedule" in haystack:
        return "guardian reschedule"

    ticket = re.search(r"\b[A-Z]+-\d+\b", (activity.branch or "").upper())
    if ticket:
        return ticket.group(0)

    return activity.branch or "local work"


def build_local_wip_detail(wip_activity, activities) -> dict[str, str]:
    related_commits = [
        a
        for a in activities
        if (
            a.source == "local_git"
            and a.activity_type in {"commit", "local_commit"}
            and a.repository == wip_activity.repository
            and a.branch == wip_activity.branch
        )
    ]
    topic = infer_local_wip_topic(wip_activity).title()
    summary = summarize_local_progress(topic, wip_activity, related_commits)
    today = f"{topic}: Made progress on {summary}"
    tomorrow = f"Continue {topic}: finish and validate the remaining in-progress updates"
    return {"today": today, "tomorrow": tomorrow}


def extract_touched_files(description: str, max_files: int = 3) -> list[str]:
    if not description:
        return []

    files = []
    for raw in description.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line in {"Staged:", "Modified:", "Untracked:"}:
            continue
        if re.match(r"^\d+\s+files?\s+changed", line):
            continue

        if "|" in line:
            candidate = line.split("|", 1)[0].strip()
        elif "/" in line or "." in line:
            candidate = line
        else:
            continue

        if candidate and candidate not in files:
            files.append(candidate)
        if len(files) >= max_files:
            break

    return files


def summarize_local_progress(topic: str, wip_activity, related_commits) -> str:
    """Return plain-English, functionality-focused progress details."""
    hints = []

    # Use commit titles first; they are usually closest to user intent.
    for commit in sorted(related_commits, key=lambda a: a.occurred_at, reverse=True)[:3]:
        cleaned = simplify_commit_title(commit.title or "")
        if cleaned and cleaned not in hints:
            hints.append(cleaned)

    # Fall back to file-pattern functionality hints for ongoing uncommitted work.
    touched_files = extract_touched_files(wip_activity.description)
    for file_path in touched_files:
        inferred = infer_functionality_from_path(file_path, topic)
        if inferred and inferred not in hints:
            hints.append(inferred)

    if hints:
        return join_phrases(hints[:3])

    return "the core feature flow and related follow-up tasks"


def simplify_commit_title(title: str) -> str:
    cleaned = re.sub(r"\b[A-Z]+-\d+\b[:\s-]*", "", title, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return ""

    lower = cleaned.lower()
    replacements = [
        ("refactor", "improving"),
        ("fix ", "fixing "),
        ("bug", "issue"),
        ("ui", "user experience"),
        ("ux", "user experience"),
        ("api", "integration"),
    ]
    for old, new in replacements:
        lower = lower.replace(old, new)

    return lower


def infer_functionality_from_path(file_path: str, topic: str) -> str:
    p = (file_path or "").lower()
    t = (topic or "").lower()

    if "guardian" in t and "reschedule" in t:
        if "state" in p or "view" in p:
            return "the guardian reschedule steps and on-screen guidance"
        if "modal" in p:
            return "the pop-up flow and confirmations during rescheduling"
        if "analytics" in p:
            return "tracking for important reschedule actions"
        return "the guardian reschedule experience"

    if "dashboard" in p:
        return "dashboard behavior and workflow updates"
    if "provider" in p:
        return "shared app behavior used across the feature"
    if "service" in p:
        return "background business logic supporting the feature"

    return ""


def join_phrases(phrases: list[str]) -> str:
    if not phrases:
        return ""
    if len(phrases) == 1:
        return phrases[0]
    if len(phrases) == 2:
        return f"{phrases[0]} and {phrases[1]}"
    return f"{phrases[0]}, {phrases[1]}, and {phrases[2]}"


def enforce_grounded_tomorrow(tomorrow: str, activities) -> str:
    lines = [line for line in tomorrow.splitlines() if line.strip()]
    if not lines:
        return tomorrow

    allowed_tickets = allowed_tomorrow_ticket_ids(activities)
    filtered = []

    for line in lines:
        if not line.lstrip().startswith("-"):
            filtered.append(line)
            continue

        ticket_ids = extract_ticket_ids(line)
        if not ticket_ids:
            filtered.append(line)
            continue

        if any(ticket_id in allowed_tickets for ticket_id in ticket_ids):
            filtered.append(line)

    return "\n".join(filtered).strip()


def allowed_tomorrow_ticket_ids(activities) -> set[str]:
    allowed = set()

    for activity in activities:
        ticket_ids = set()
        if activity.ticket_id:
            ticket_ids.add(activity.ticket_id.upper())
        ticket_ids.update(extract_ticket_ids(activity.title or ""))
        ticket_ids.update(extract_ticket_ids(activity.description or ""))
        ticket_ids.update(extract_ticket_ids(activity.branch or ""))

        if not ticket_ids:
            continue

        # Ticket explicitly moved to In Progress.
        if (
            activity.source == "linear"
            and activity.activity_type == "ticket_status_changed"
            and "progress" in (activity.status or "").lower()
        ):
            allowed.update(ticket_ids)
            continue

        # Local WIP/commits indicate active ongoing implementation.
        if activity.source == "local_git" and activity.activity_type in {
            "uncommitted_changes",
            "local_commit",
            "commit",
        }:
            allowed.update(ticket_ids)

    return allowed


def extract_ticket_ids(text: str) -> set[str]:
    if not text:
        return set()
    return {match.upper() for match in re.findall(r"\b[A-Z]+-\d+\b", text, re.IGNORECASE)}
