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
- Each activity is tagged with its date (e.g., [May 14] or [May 15]). The target standup date \
is provided at the top of the activity list.
- ONLY include work from the target date in "Today". Activities from earlier dates are provided \
for context only — do NOT list them as today's work unless they clearly continued into the target date \
(e.g., an in-progress ticket with commits on both days).
- Group related work together (e.g., multiple commits on the same ticket/PR = one bullet)
- When a ticket ID is available, it MUST be the first thing in the bullet point \
(e.g., "ATH-1408: Worked on guardian dashboard" not "Worked on ATH-1408 guardian dashboard"). \
Format: "TICKET-ID: description"
- "Today" section: past tense. Include ALL meaningful work completed or progressed on the target date.
- "Tomorrow" section: future tense. Infer from in-progress items, uncommitted changes, and context.
- If an item is still in progress, include the progress made under "Today" and the continuation under "Tomorrow".
- Explicitly include PR reviews in "Today" when they occurred on the target date.
- PRs tagged "ready for review" or "ready to merge" are finished work for "Today", not "Tomorrow".
- Any ticket moved to "In Progress" on the target date MUST appear in both sections.
- Tickets that were only moved to a terminal state like "Released" or "Done" (with no commits or \
PRs in the same period) should be briefly noted as released/completed — do NOT describe the full \
scope of work on those tickets. Group multiple released tickets into a single bullet when possible.
- For in-progress tickets, include concrete progress details from related commits, PR activity, or uncommitted changes when available.
- Prefer plain-English functionality summaries over file names, counts, or low-level code terminology.
- Use the actual ticket or PR title as provided in the activity data. Do NOT rephrase, \
paraphrase, or invent a new description for the ticket — use the original title verbatim.
- Do NOT directly quote Slack messages — summarize the topics discussed instead
- Use markdown bullet points (-)
- If there are fewer than 5 major items in "Today", expand each item with indented sub-bullets \
that describe specific features, functionalities, and changes worked on in more detail. \
Pull details from commit messages, PR descriptions, file changes, and ticket context. \
Each sub-bullet should describe a distinct piece of work (e.g., a specific fix, a new page added, \
a rendering improvement). Do NOT pad with vague filler — only add sub-bullets when there are \
real details available.
- If there are 5 or more major items, keep each bullet point to one line without sub-bullets.

Format your response EXACTLY like this (sub-bullets only when fewer than 5 major items):
## Today
- ATH-1234: Description of work done
  - Detail about a specific feature or change
  - Detail about another aspect of the work
- ATH-5678: Another item
  - Detail

## Tomorrow
- ATH-1234: Continue remaining work
- ATH-9999: Next planned item
"""


MAX_RETRIES = 4
INITIAL_RETRY_DELAY_SECONDS = 1.5


def format_activity_date(activity, target_date) -> str:
    """Return a short date tag like [May 14] for the activity's occurred_at date in local time."""
    from standups.services.generator import activity_local_date
    local_date = activity_local_date(activity.occurred_at)
    return f"[{local_date.strftime('%b %-d')}]"


def is_on_target_date(activity, target_date) -> bool:
    """Check if an activity occurred on the target date in the user's local timezone."""
    if target_date is None:
        return True
    from standups.services.generator import activity_local_date
    return activity_local_date(activity.occurred_at) == target_date


def build_user_prompt(activities, extra_context: str = "", target_date=None) -> str:
    grouped = {}
    for activity in activities:
        key = f"{activity.source}_{activity.activity_type}"
        if key not in grouped:
            grouped[key] = []
        grouped[key].append(activity)

    sections = []

    # Header with target date context
    if target_date:
        sections.append(f"**Target standup date: {target_date.strftime('%B %-d, %Y')}**")

    # Local git commits
    for commit_type in ["local_git_commit", "local_git_local_commit"]:
        local_commits = grouped.get(commit_type, [])
        if local_commits:
            label = "Local Commits (unpushed)" if "local_commit" in commit_type else "Local Commits (pushed)"
            lines = [f"### {label}"]
            for a in local_commits:
                date_tag = format_activity_date(a, target_date) if target_date else ""
                lines.append(f"- {date_tag} [{a.repository}:{a.branch}] {a.title}")
            sections.append("\n".join(lines))

    # Local uncommitted changes
    uncommitted = grouped.get("local_git_uncommitted_changes", [])
    if uncommitted:
        lines = ["### Work In Progress (uncommitted changes)"]
        for a in uncommitted:
            date_tag = format_activity_date(a, target_date) if target_date else ""
            lines.append(f"- {date_tag} [{a.repository}:{a.branch}] {a.title}")
        sections.append("\n".join(lines))

    # GitHub commits
    commits = grouped.get("github_commit", [])
    if commits:
        lines = ["### GitHub Commits"]
        for a in commits:
            repo = a.repository.split("/")[-1] if a.repository else ""
            date_tag = format_activity_date(a, target_date) if target_date else ""
            lines.append(f"- {date_tag} [{repo}] {a.title}")
        sections.append("\n".join(lines))

    # GitHub PRs
    for pr_type in ["github_pr_opened", "github_pr_merged", "github_pr_reviewed"]:
        prs = grouped.get(pr_type, [])
        if prs:
            label = pr_type.replace("github_", "").replace("_", " ").title()
            lines = [f"### GitHub {label}"]
            for a in prs:
                repo = a.repository.split("/")[-1] if a.repository else ""
                pr_labels = get_ready_labels(a)
                tag = f" [{', '.join(sorted(pr_labels))}]" if pr_labels else ""
                date_tag = format_activity_date(a, target_date) if target_date else ""
                lines.append(f"- {date_tag} [{repo}] {a.title}{tag} ({a.url})")
            sections.append("\n".join(lines))

    # Linear tickets
    for ticket_type in ["linear_ticket_completed", "linear_ticket_status_changed"]:
        tickets = grouped.get(ticket_type, [])
        if tickets:
            label = "Completed" if "completed" in ticket_type else "Status Changes"
            lines = [f"### Linear Tickets — {label}"]
            for a in tickets:
                if "completed" in ticket_type:
                    # Show the terminal state, not the full issue description
                    state = a.status or "Done"
                    desc = f" (moved to {state})"
                else:
                    desc = f" ({a.description})" if a.description else ""
                date_tag = format_activity_date(a, target_date) if target_date else ""
                lines.append(f"- {date_tag} {a.title}{desc}")
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

    if extra_context:
        lines = ["### Additional Context (user-provided, use as supplementary info)"]
        lines.append(extra_context)
        sections.append("\n".join(lines))

    if not sections:
        return "No activities found for this period."

    return "\n\n".join(sections)


def summarize(activities, extra_context: str = "", target_date=None) -> dict:
    if not settings.ANTHROPIC_API_KEY:
        return {
            "yesterday": "Claude API key not configured",
            "today": "",
            "blockers": "",
            "raw_response": "",
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }

    user_prompt = build_user_prompt(activities, extra_context=extra_context, target_date=target_date)
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    response = request_summary_with_retries(client, user_prompt)

    raw_text = response.content[0].text
    sections = parse_sections(raw_text)
    today, tomorrow = enforce_in_progress_ticket_overlap(
        sections.get("today", ""),
        sections.get("tomorrow", ""),
        activities,
        target_date=target_date,
    )
    today, tomorrow = enforce_local_wip_overlap(today, tomorrow, activities)
    today = enforce_reviewed_prs_today(today, activities, target_date=target_date)
    today, tomorrow = enforce_ready_tagged_prs_finished(today, tomorrow, activities, target_date=target_date)
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


def enforce_in_progress_ticket_overlap(today: str, tomorrow: str, activities, target_date=None) -> tuple[str, str]:
    # Collect ticket IDs that have ready-tagged PRs — those are finished, not in-progress.
    finished_ticket_ids = set()
    for activity in activities:
        if is_ready_tagged_pr(activity):
            finished_ticket_ids.update(extract_ticket_ids(activity.title or ""))

    in_progress = {}
    for activity in activities:
        if (
            activity.source == "linear"
            and activity.activity_type == "ticket_status_changed"
            and activity.ticket_id
            and "progress" in (activity.status or "").lower()
            and is_on_target_date(activity, target_date)
            and activity.ticket_id.upper() not in finished_ticket_ids
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


def enforce_reviewed_prs_today(today: str, activities, target_date=None) -> str:
    reviewed = [
        a
        for a in activities
        if a.source == "github" and a.activity_type == "pr_reviewed"
        and is_on_target_date(a, target_date)
    ]
    if not reviewed:
        return today

    lines = [line for line in today.splitlines() if line.strip()]
    existing = [line.lower() for line in lines if line.lstrip().startswith("-")]
    seen = set()

    for activity in sorted(reviewed, key=lambda a: a.occurred_at):
        title = (activity.title or "").strip()
        cleaned_title = re.sub(r"^\s*Reviewed:\s*", "", title, flags=re.IGNORECASE).strip()
        normalized_title = cleaned_title.lower()
        repo = activity.repository.split("/")[-1] if activity.repository else "repo"
        dedupe_key = (repo.lower(), normalized_title)
        if dedupe_key in seen:
            continue

        ticket_ids = extract_ticket_ids(cleaned_title)
        already_present = normalized_title and any(normalized_title in line for line in existing)
        if not already_present and ticket_ids:
            already_present = any(
                any(ticket_id.lower() in line for ticket_id in ticket_ids) and "review" in line
                for line in existing
            )
        if already_present:
            seen.add(dedupe_key)
            continue

        if ticket_ids:
            ticket_id = sorted(ticket_ids)[0]
            bullet = f"- {ticket_id}: Reviewed PR in {repo} — {cleaned_title}"
        else:
            bullet = f"- Reviewed PR in {repo} — {cleaned_title}"
        lines.append(bullet)
        existing.append(bullet.lower())
        seen.add(dedupe_key)

    return "\n".join(lines).strip()


def enforce_ready_tagged_prs_finished(today: str, tomorrow: str, activities, target_date=None) -> tuple[str, str]:
    ready_prs = [
        a for a in activities
        if is_ready_tagged_pr(a)
        and is_on_target_date(a, target_date)
    ]
    if not ready_prs:
        return today, tomorrow

    today_lines = [line for line in today.splitlines() if line.strip()]
    today_blob = today.lower()
    seen_finished = set()
    finished_markers = set()

    for activity in sorted(ready_prs, key=lambda a: a.occurred_at):
        cleaned_title = re.sub(r"^\s*Reviewed:\s*", "", (activity.title or "").strip(), flags=re.IGNORECASE)
        normalized_title = cleaned_title.lower()
        dedupe_key = (activity.repository or "", normalized_title)
        if dedupe_key in seen_finished:
            continue

        ticket_ids = extract_ticket_ids(cleaned_title)
        labels = ", ".join(sorted(get_ready_labels(activity)))

        if normalized_title and normalized_title not in today_blob:
            repo = activity.repository.split("/")[-1] if activity.repository else "repo"
            if ticket_ids:
                ticket_id = sorted(ticket_ids)[0]
                today_lines.append(f"- {ticket_id}: Finished PR in {repo} — {cleaned_title} ({labels})")
            else:
                today_lines.append(f"- Finished PR in {repo} — {cleaned_title} ({labels})")
            seen_finished.add(dedupe_key)

        if normalized_title:
            finished_markers.add(normalized_title)
        for ticket_id in ticket_ids:
            finished_markers.add(ticket_id.lower())

    # Rewrite existing today bullets that reference finished tickets
    today_lines = [
        rewrite_as_finished(line) if line.lstrip().startswith("-") and any(
            marker in line.lower() for marker in finished_markers
        ) else line
        for line in today_lines
    ]

    tomorrow_lines = [line for line in tomorrow.splitlines() if line.strip()]
    filtered_tomorrow = []
    for line in tomorrow_lines:
        if not line.lstrip().startswith("-"):
            filtered_tomorrow.append(line)
            continue

        lower_line = line.lower()
        if any(marker in lower_line for marker in finished_markers):
            continue
        filtered_tomorrow.append(line)

    return "\n".join(today_lines).strip(), "\n".join(filtered_tomorrow).strip()


def rewrite_as_finished(line: str) -> str:
    result = re.sub(r"\bStarted\b", "Finished", line)
    result = re.sub(r"\bstarted\b", "finished", result)
    result = re.sub(r"\bBegan\b", "Finished", result)
    result = re.sub(r"\bbegan\b", "finished", result)
    result = re.sub(
        r"Moved to In Progress and (?:advanced|progressed) (?:the|this) work(?: today)?",
        "Completed this work",
        result,
        flags=re.IGNORECASE,
    )
    result = re.sub(
        r"Moved to In Progress",
        "Completed",
        result,
        flags=re.IGNORECASE,
    )
    return result


def is_ready_tagged_pr(activity) -> bool:
    if activity.source != "github" or activity.activity_type not in {"pr_opened", "pr_reviewed", "pr_merged", "pr_ready"}:
        return False

    labels = get_ready_labels(activity)
    if labels:
        return True

    text = " ".join([activity.title or "", activity.description or ""]).lower()
    return "ready for review" in text or "ready to merge" in text


def get_ready_labels(activity) -> set[str]:
    metadata = getattr(activity, "metadata", {}) or {}
    labels = metadata.get("labels", []) if isinstance(metadata, dict) else []
    normalized = {str(label).strip().lower() for label in labels if str(label).strip()}
    return {l for l in normalized if l in {"ready for review", "ready to merge"}}
